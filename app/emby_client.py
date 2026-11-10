#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Emby API client — connectivity, playlist browsing, and artist images.

#168: Jellyfin (already supported, see jellyfin_client.py) is a 2018 fork of
Emby's MediaBrowser server, and the two APIs remain near-identical — same
/Users, /Items, /Playlists model, same object shapes. This client is its own
module, like every other provider dispatched through main.py's _PROVIDERS,
but most of its functions are the ones jellyfin_client.py uses too: they live
in mediabrowser_client.py and are bound below. What stays here is what
differs: the auth header (Emby's own docs specify X-Emby-Token for API-key
auth not tied to a user context, rather than Jellyfin's `Authorization:
MediaBrowser ...` scheme) with Emby's error handling in _get() and
_request_as(), the playlist-track fields, the artist-image request and the
playlist create/replace sequence.

Same rationale as jellyfin_client.py otherwise: stateless HTTP, authenticated
per-request via a static API key, no persistent connection/pairing handshake
and no "pending_approval" state — a valid authenticated call means paired,
anything else means disconnected.

Auth model: the API key is server-wide, but playlist visibility is scoped to
a specific *user's* library view, same as Jellyfin — so config needs a
chosen username, resolved once (at reconnect time) to Emby's internal userId
via GET /Users, and persisted rather than re-resolved on every call.

Playlist tracks carry a real `Path` (and `Album`), same as Jellyfin — see
matching.py's match_playlist_track_by_path, the primary match strategy this
enables instead of the fuzzy artist/title heuristics built for Roon.

Not live-verified against a real Emby server (no instance available in this
environment) — implemented directly from Emby's published API docs
(dev.emby.media) and the shared Jellyfin lineage this client mirrors, which
*was* confirmed live. Worth a real-server pass before calling #168 fully
done, same as the issue's own acceptance checklist asks for.

The mirror_*() functions at the bottom (#189, fourth sink) WERE confirmed
live against a real Emby 4.9.5 instance, and turned up four real
divergences from jellyfin_client.py's own mirror_*() functions despite the
shared lineage — none of these are guesses, all confirmed by hitting a live
server: (1) /Playlists create/replace takes query-string params, not a JSON
body, unlike Jellyfin's POST /Playlists; (2) removing existing entries on a
replace needs the per-entry `PlaylistItemId` from GET .../Items, not the
track's own `Id` — Emby returns both fields and they're genuinely
different values, unlike Jellyfin's response which only has the track Id;
(3) GET /Playlists/{id}/Items for a stale/nonexistent id is a bare 500
("Object reference not set to an instance of an object"), not a clean 404
the way Jellyfin's own equivalent call is — so the stale-remote-id signal
here comes from a dedicated existence check (GET /Users/{userId}/Items/
{id}, confirmed to 404 cleanly) done up front, rather than reacting to the
replace call's own status the way the Jellyfin sink does; (4) the mirror
comment (Overview) reliably gets reverted a few seconds after any write
that includes items — Emby schedules its own metadata-refresh pass on a
playlist's item-list change, which resets Overview regardless of what was
just POSTed (LockedFields made no difference); the rename half of the same
call sticks fine, and a genuinely empty-item playlist's comment is
unaffected. See mirror_set_playlist_metadata()'s own docstring, in
mediabrowser_client.py — this is accepted as an Emby-side limitation, not
chased further.
"""

import logging
import sys
import threading

import requests

import db
from artist_image_keys import ArtistImageKeys
from mediabrowser_client import MediaBrowserClient, copy_item

_log = logging.getLogger(__name__)

_CLIENT_ID = "trobar"

_artist_image_key_map = ArtistImageKeys()
_music_library_id: str | None = None
_music_library_id_lock = threading.Lock()


def _request_as(
    method: str, endpoint: str, api_key: str, url: str,
    params: dict | None = None, json_body: dict | None = None,
) -> tuple[int | None, dict | None]:
    """Low-level authenticated call against an EXPLICIT server/api-key, any
    HTTP method. Returns (status_code, parsed JSON body) — status_code is
    None only when the request itself never got a response at all (network
    error, unparseable body); a real HTTP status (including a 4xx/5xx) is
    always surfaced, unlike _get() below which collapses every failure to
    None. A caller can then react to a SPECIFIC status (see
    mirror_create_or_replace_playlist()'s stale-remote-id handling) without
    string-matching a message.

    Both `params` and `json_body` are supported because Emby itself isn't
    consistent about which one a given write endpoint wants — confirmed
    live: /Playlists create/replace calls (Name/Ids/UserId/EntryIds etc.)
    read from the query string and silently ignore a JSON body, while the
    generic /Items/{id} metadata-edit call (mirror_set_playlist_metadata's
    GET-mutate-POST round trip) needs the full object as a JSON body — a
    query-string equivalent isn't practical for a whole item payload.
    `Ids`/`EntryIds` passed via `params` are comma-joined strings, not a
    JSON array, to match the query-string calls' expected shape.

    An error response's body is plain text here ("The requested item could
    not be found...", confirmed live), NOT JSON the way Jellyfin's error
    bodies are — a body that fails to parse as JSON must still surface its
    real status code (a caller checking for 404 specifically needs it),
    not collapse to None the way a genuine network failure does. Only a
    request that never got a response at all reports status_code None.

    #189: a separate helper from _get() above (not built from it, unlike
    jellyfin_client.py's own split) so the mirror-target write functions get
    POST/DELETE and status-code-aware error handling without touching
    _get()'s own already-tested read-side behavior at all — see _get()'s
    own docstring for why."""
    if not url or not api_key:
        return None, None
    headers = {"X-Emby-Token": api_key}
    try:
        resp = requests.request(
            method, f"{url.rstrip('/')}{endpoint}", headers=headers,
            params=params, json=json_body, timeout=10,
        )
    except requests.RequestException:
        return None, None
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = None
    return resp.status_code, body


def _get(endpoint: str, api_key: str, url: str, params: dict | None = None):
    """Low-level authenticated GET. Returns the parsed JSON body, or None if
    not configured or the request itself failed (network error, non-2xx,
    unparseable body).

    Deliberately NOT rebuilt on top of _request_as() below (unlike
    jellyfin_client.py's own _get(), which is) — this function already had
    real test coverage (test_emby_client.py) mocking requests.get directly
    before #189's mirror-target work landed; routing it through
    _request_as()'s requests.request() call would silently break every one
    of those tests for a refactor with no behavioral upside on the read
    side. _request_as() exists purely for the NEW mirror write paths
    below, which have no such pre-existing coverage to disturb."""
    if not url or not api_key:
        return None
    headers = {"X-Emby-Token": api_key}
    try:
        resp = requests.get(f"{url.rstrip('/')}{endpoint}", headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except (requests.RequestException, ValueError):
        return None


def get_playlist_tracks(
    playlist_title: str, source_playlist_id: str | None = None, user_id: str | None = None,
) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist", "path", "album"} — the extra `path`/`album` fields (Roon never
    has these) are what let matching.py skip straight to an exact-path match
    instead of its Roon-Browse-API-specific fuzzy fallback.

    Fetched by `source_playlist_id` directly (the Item Id from
    list_playlists) — fetching by title would be ambiguous now that
    same-named playlists can coexist. Falls back to a title match only if
    no id is given (the sync always passes it).

    `user_id`: #262's per-Trobar-user override, forwarded to the Items
    fetch below (this is the id Emby actually needs to scope the request;
    `_list_playlist_items()`'s own use of it, above, is only reached on
    the title-fallback path)."""
    if source_playlist_id is not None:
        item_id: str | None = source_playlist_id
    else:
        items = _list_playlist_items(user_id)
        if items is None:
            return {"status": "error", "reason": "not_paired"}
        match = next((i for i in items if i.get("Name") == playlist_title), None)
        if match is None:
            return {"status": "not_found", "failed_segment": playlist_title}
        item_id = match["Id"]

    url, api_key, default_user_id = _current_config()
    uid = user_id or default_user_id
    resp = _get(f"/Playlists/{item_id}/Items", api_key, url,
                {"userId": uid, "Fields": "Path"})
    if resp is None:
        return {"status": "error", "reason": "playlist items fetch failed"}
    entries = resp.get("Items", [])

    tracks = [
        {
            "position": i,
            "title": e.get("Name", ""),
            "artist": ", ".join(e.get("Artists") or []),
            "path": e.get("Path"),
            "album": e.get("Album"),
        }
        for i, e in enumerate(entries)
    ]
    return {"status": "ok", "playlist": playlist_title, "tracks": tracks}


def get_artist_image(artist_name: str) -> tuple[bytes, str] | None:
    """Returns (bytes, content_type) or None if not configured / not found —
    a miss here just means no picture, never a sync failure (same contract
    as roon_client.get_artist_image)."""
    artist_id = _get_artist_image_key_map().get(artist_name)
    if not artist_id:
        return None

    url, api_key, _user_id = _current_config()
    if not url or not api_key:
        return None
    headers = {"X-Emby-Token": api_key}
    try:
        resp = requests.get(f"{url.rstrip('/')}/Items/{artist_id}/Images/Primary",
                             headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return resp.content, content_type


# ---------------------------------------------------------------------------
# #189 mirror-target sink — an Emby server as a WRITE destination, not a
# read source. Own connection (db.get_mirror_emby_config()), own functions
# below; see the module docstring for the four confirmed-live divergences
# from jellyfin_client.py's own mirror_*() functions despite the shared
# lineage.
# ---------------------------------------------------------------------------

# Same values as jellyfin_client.py's/subsonic_client.py's — safety
# backstops, not anything protocol-mandated. Confirmed live: Emby's own
# StartIndex/Limit pagination behaves the same as Jellyfin's (a short final
# page is genuinely the end; StartIndex past the end returns 0 items, not
# an error) — same "walk until empty page" defense anyway, since that
# behaviour isn't something to trust blindly a second time either.
_MIRROR_PAGE_SIZE = 500
_MIRROR_MAX_PAGES = 4000


def mirror_create_or_replace_playlist(
    name: str, song_ids: list[str], remote_id: str | None, user_id: str | None = None
) -> dict:
    """Creates (remote_id is None) or FULLY REPLACES (remote_id given) a
    playlist's song list. Returns {"status": "ok", "remote_id": str,
    "previous": the replaced copy's items as {"id", "artist", "title"}, or
    None for a new playlist} or {"status": "error", "reason": str, "code":
    int | None}.

    Confirmed live against a real Emby 4.9.5 instance — two real
    divergences from jellyfin_client.mirror_create_or_replace_playlist(),
    despite the shared MediaBrowser lineage:

    - Every call here is query-string params, not a JSON body (a JSON body
      is silently ignored, not an error — confirmed by watching it have no
      effect). `Ids`/`EntryIds` are comma-joined strings, not a JSON array.
    - Removing existing entries needs the per-entry `PlaylistItemId` field
      from GET .../Items, not the track's own `Id` — Emby's response
      carries both and they're genuinely different values (confirmed live:
      deleting by the track Id silently removed nothing). Jellyfin's
      equivalent response does carry a `PlaylistItemId`, but it comes back
      equal to the item's own Id — measured on both 10.11.11 and 12.0.0 —
      so it identifies the track rather than the entry, and that sink uses
      the track Id directly. This one cannot.

    A stale/nonexistent remote_id does NOT surface cleanly from the replace
    path itself the way Jellyfin's does — GET .../Items for a bad playlist
    id is a bare 500 there (confirmed live), not a 404. So existence is
    checked up front via a dedicated generic-item lookup (confirmed live to
    404 cleanly for a stale id) before ever reaching the replace-specific
    calls."""
    config = db.get_mirror_emby_config()
    if config is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, api_key, configured_user_id = config
    # The account the copy is written as: the playlist owner's, when the
    # sink resolved one, else the configured mirror account.
    user_id = user_id or configured_user_id

    if remote_id is None:
        params: dict = {"Name": name, "UserId": user_id, "MediaType": "Audio"}
        if song_ids:
            params["Ids"] = ",".join(song_ids)
        status, body = _request_as("POST", "/Playlists", api_key, url, params=params)
        if status is None or status >= 400 or not body or "Id" not in body:
            return {"status": "error", "reason": "create failed", "code": status}
        return {"status": "ok", "remote_id": body["Id"], "previous": None}

    exists_status, _exists_body = _request_as(
        "GET", f"/Users/{user_id}/Items/{remote_id}", api_key, url)
    if exists_status == 404:
        return {"status": "error", "reason": "playlist not found", "code": 404}
    if exists_status is None or exists_status >= 400:
        return {"status": "error", "reason": "failed to check playlist", "code": exists_status}

    status, current = _request_as(
        "GET", f"/Playlists/{remote_id}/Items", api_key, url, params={"userId": user_id})
    if status is None or status >= 400 or current is None:
        return {"status": "error", "reason": "failed to read current items", "code": status}

    entry_ids = [item["PlaylistItemId"] for item in current.get("Items", [])
                 if item.get("PlaylistItemId")]
    if entry_ids:
        status, _ = _request_as(
            "DELETE", f"/Playlists/{remote_id}/Items", api_key, url,
            params={"EntryIds": ",".join(entry_ids)},
        )
        if status is None or status >= 400:
            return {"status": "error", "reason": "failed to clear existing items", "code": status}
    if song_ids:
        status, _ = _request_as(
            "POST", f"/Playlists/{remote_id}/Items", api_key, url,
            params={"Ids": ",".join(song_ids), "userId": user_id},
        )
        if status is None or status >= 400:
            return {"status": "error", "reason": "failed to add items", "code": status}
    # The item's own Id, not its PlaylistItemId: the sink writes item ids,
    # and compares like with like.
    return {"status": "ok", "remote_id": remote_id,
            "previous": [copy_item(i) for i in current.get("Items", []) if i.get("Id")]}


# The definitions both servers share live in mediabrowser_client.py; each
# reads this module's names (_get, the constants above, _music_library_id)
# through it, so patching one here still reaches every caller.
_client = MediaBrowserClient(
    "emby", sys.modules[__name__], lambda: db.get_mirror_emby_config())
_current_config = _client._current_config
ensure_started = _client.ensure_started
status = _client.status
retry_pairing = _client.retry_pairing
_resolve_user_id = _client._resolve_user_id
test_connection = _client.test_connection
reconnect = _client.reconnect
_list_playlist_items = _client._list_playlist_items
list_playlists = _client.list_playlists
_norm_id = _client._norm_id
_mirror_ids = _client._mirror_ids
list_users = _client.list_users
_get_music_library_id = _client._get_music_library_id
_build_artist_image_key_map = _client._build_artist_image_key_map
_get_artist_image_key_map = _client._get_artist_image_key_map
mirror_status = _client.mirror_status
mirror_reconnect = _client.mirror_reconnect
mirror_build_tag_index = _client.mirror_build_tag_index
_server_id = _client._server_id
mirror_target_is_library_server = _client.mirror_target_is_library_server
mirror_set_playlist_metadata = _client.mirror_set_playlist_metadata
mirror_delete_playlist = _client.mirror_delete_playlist
