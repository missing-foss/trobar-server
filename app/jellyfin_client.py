#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Jellyfin API client — connectivity, playlist browsing, and artist images.

Unlike roon_client.py, there's no persistent connection/pairing handshake to
maintain — Jellyfin's REST API is stateless HTTP, authenticated per-request
via a static API key in the Authorization header. No "pending_approval"
state exists either: a valid authenticated call means paired, anything else
means disconnected.

Auth model differs from subsonic_client.py in one real way: Jellyfin's API
key is server-wide (effectively admin-level), but playlist visibility is
scoped to a specific *user's* library view — so config needs a chosen
username, resolved once (at reconnect time) to Jellyfin's internal userId
via GET /Users, and persisted rather than re-resolved on every call.

Confirmed live against a real Jellyfin 10.11.11 instance that general web
guidance claiming playlist endpoints don't work with API-key auth is wrong
*once an explicit userId rides alongside the key* — GET /Playlists/{id}/Items
?userId=... works fine with just the API key, no separate username/password
login flow needed.

Playlist tracks here carry a real `Path` (and `Album`), unlike Roon's Browse
API which only ever exposes a display title/subtitle — see matching.py's
match_playlist_track_by_path, which this enables as the primary match
strategy instead of relying on the fuzzy artist/title heuristics built for
Roon's fewer-than-ideal metadata. Multi-artist `Artists` arrays are joined
with ", " and left for matching.py's existing credits-list fallback to
split if a fuzzy match is ever needed — no Jellyfin-specific artist parsing.

Artist images use GET /Items/{id}/Images/Primary directly — unlike Subsonic
(where the equivalent convenience URL was Authentik-gated and getCoverArt
had to be used instead), this endpoint was confirmed live to just work.

The mirror_*() functions at the bottom are a second, independent
connection (db.get_mirror_jellyfin_config()) from the active-provider one
every other function here uses (_current_config()) — see that config
getter's own docstring for why the two must never be conflated. #189:
this sink's tag index is keyed on (artist, album, title), NOT Jellyfin's
own `Path` field, even though Path is genuinely the real on-disk path
here (confirmed live) rather than Subsonic's tag-derived synthesis — a
live check for the "Path is only visible to privileged accounts" trap
flagged going into this work was inconclusive after testing several
permission axes (IsAdministrator, EnableContentDownloading,
EnableContentDeletion) against a real Jellyfin 10.11.11 instance, so the
design doesn't bet on an unverified account-privilege condition; tags
are the same join key already proven for the Subsonic sink and sidestep
the question entirely. Confirm/refute this with your own live check
before assuming otherwise.

Most of the functions are the ones emby_client.py uses too: they live in
mediabrowser_client.py and are bound at the bottom of this module. What
stays here is what differs from Emby: the MediaBrowser Authorization header
in _request_as() (which _get() is built on) and get_artist_image(), the
playlist-track fields (ProviderIds, for the recording MBID) and the
playlist create/replace sequence.
"""

import logging
import sys
import threading

import requests

import db
from artist_image_keys import ArtistImageKeys
from mediabrowser_client import MediaBrowserClient

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
    error, unparseable body); a real HTTP status (including a 4xx) is
    always surfaced, unlike _get() below which collapses every failure to
    None. A caller can then react to a SPECIFIC status (see
    mirror_create_or_replace_playlist()'s stale-remote-id handling, which
    needs to tell a 404 "Playlist not found" apart from every other
    failure) without string-matching a message.

    #189: split out from _get() so the mirror-target write functions can
    drive POST/DELETE calls through the exact same auth mechanics without
    _get()'s own read-side callers changing."""
    if not url or not api_key:
        return None, None
    headers = {"Authorization": f'MediaBrowser Token="{api_key}", Client="Trobar", '
                                 f'Device="Trobar", DeviceId="{_CLIENT_ID}", Version="1.0.0"'}
    try:
        resp = requests.request(
            method, f"{url.rstrip('/')}{endpoint}", headers=headers,
            params=params, json=json_body, timeout=10,
        )
        body = resp.json() if resp.content else {}
        return resp.status_code, body
    except (requests.RequestException, ValueError):
        return None, None


def _get(endpoint: str, api_key: str, url: str, params: dict | None = None):
    """Low-level authenticated GET. Returns the parsed JSON body, or None if
    not configured or the request itself failed (network error, non-2xx,
    unparseable body)."""
    status, body = _request_as("GET", endpoint, api_key, url, params=params)
    if status is None or status >= 400:
        return None
    return body


def get_playlist_tracks(
    playlist_title: str, source_playlist_id: str | None = None, user_id: str | None = None,
) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist", "path", "album", "mbid"} — the extra `path`/`album` fields (Roon never
    has these) are what let matching.py skip straight to an exact-path match
    instead of its Roon-Browse-API-specific fuzzy fallback.

    Fetched by `source_playlist_id` directly (the Item Id from
    list_playlists) — fetching by title would be ambiguous now that
    same-named playlists can coexist. Falls back to a title match only if
    no id is given (the sync always passes it).

    `user_id`: #262's per-Trobar-user override, forwarded to the Items
    fetch below (this is the id Jellyfin actually needs to scope the
    request; `_list_playlist_items()`'s own use of it, above, is only
    reached on the title-fallback path)."""
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
                {"userId": uid, "Fields": "Path,ProviderIds"})
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
            # The recording id: Jellyfin's MusicBrainzTrack is the
            # release-track id, which names one copy on one release.
            "mbid": (e.get("ProviderIds") or {}).get("MusicBrainzRecording") or None,
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
    headers = {"Authorization": f'MediaBrowser Token="{api_key}"'}
    try:
        resp = requests.get(f"{url.rstrip('/')}/Items/{artist_id}/Images/Primary",
                             headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return resp.content, content_type


# ---------------------------------------------------------------------------
# #189 mirror-target sink — a Jellyfin server as a WRITE destination, not a
# read source. Own connection (db.get_mirror_jellyfin_config()), own
# functions below; see the module docstring for why they live here rather
# than a separate client file, and for why the join key is tags rather than
# Path even though Path is genuinely real on this provider.
# ---------------------------------------------------------------------------

# Same values as subsonic_client.py's — safety backstops, not anything
# protocol-mandated, so there's no reason to pick different numbers per
# provider. Confirmed live: Jellyfin's own StartIndex/Limit pagination is
# well-behaved (a short final page is genuinely the end; StartIndex past
# the end returns 0 items, not an error) — same "walk until empty page"
# defense as the Subsonic sink anyway, since that behaviour isn't something
# to trust blindly a second time either.
_MIRROR_PAGE_SIZE = 500
_MIRROR_MAX_PAGES = 4000


def mirror_create_or_replace_playlist(
    name: str, song_ids: list[str], remote_id: str | None, user_id: str | None = None
) -> dict:
    """Creates (remote_id is None) or FULLY REPLACES (remote_id given) a
    playlist's song list. Returns {"status": "ok", "remote_id": str} or
    {"status": "error", "reason": str, "code": int | None} — `code` is the
    HTTP status Jellyfin returned when there was one (confirmed live: a
    stale/nonexistent playlistId gives a clean 404 with body "Playlist not
    found" on every playlist-scoped call), so a caller can react to a
    SPECIFIC one (see mirror_jellyfin.write_mirror()'s stale-remote-id
    handling) without string-matching `reason`.

    Unlike Subsonic's single-call createPlaylist-with-playlistId replace,
    Jellyfin has no one-shot "set the song list to exactly this" call —
    confirmed live, there's no update-in-place equivalent. A full replace
    is GET the current entries, DELETE all of them in one call (if any),
    then POST the new set in one call (if any) — three requests instead
    of one, but still a clean idempotent rewrite: re-running it against
    unchanged data removes and re-adds the same ids, ending at the same
    state. An empty `song_ids` is not special-cased on the create branch
    either (confirmed live: POST /Playlists with Ids: [] succeeds).

    #189 review: this ordering (DELETE, then POST) is forced, not a design
    choice — the reverse (add the new set first, then remove the old one)
    would narrow the window where a mid-sequence failure leaves the remote
    playlist empty, but confirmed live on 10.11.11 it doesn't work:
    re-adding an id that's already present is deduped there (204, no second
    entry), so adding the new set before removing the old would delete the
    overlap right back out along with the old entries.

    That dedupe is a version property, not a property of Jellyfin, and it
    is already gone. Measured against both, same probe:

        re-add an id already present   10.11.11: 204, 1 entry  -> 1 entry
                                       12.0.0 : 204, 1 entry  -> 2 entries

    So on 12.0 the premise above is simply false: 12.0 dropped the database
    constraint that made a repeated entry impossible, and a re-add creates
    a second one.

    THE ORDERING SURVIVES ANYWAY, which is why the code is untouched.
    Clearing first is correct whether or not the server dedupes, and the
    add-first alternative is still wrong on 12.0 for a different reason
    than it was on 10.11.11 — measured too:

        one DELETE by item id          10.11.11: 2 entries -> 1
                                       12.0.0 : 3 entries -> 1

    DELETE removes EVERY entry carrying that id, duplicates included, on
    both. So adding the new set before removing the old would still delete
    the overlap right back out. Different mechanism, same conclusion.

    One thing worth knowing before anything tries to address a single
    entry: `PlaylistItemId` does NOT distinguish duplicates on either
    version — it comes back equal to the item's own Id, so two entries for
    the same track are indistinguishable in the response. Passing item Ids
    as `entryIds`, which is what the clear below does, stays correct
    precisely because it is meant to remove all of them.

    A failure between the DELETE and the POST here does leave the remote
    playlist empty until the next successful sync repairs it — self-healing
    (the failure is recorded as `write_failed` with the remote id kept, so
    the next run retries the same replace), but a narrower guarantee than
    Subsonic's atomic single-call replace, which never has an empty window
    at all."""
    config = db.get_mirror_jellyfin_config()
    if config is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, api_key, configured_user_id = config
    # The account the copy is written as: the playlist owner's, when the
    # sink resolved one, else the configured mirror account.
    user_id = user_id or configured_user_id

    if remote_id is None:
        body_json: dict = {"Name": name, "Ids": song_ids, "UserId": user_id, "MediaType": "Audio"}
        if user_id != configured_user_id:
            # A member's own copy is theirs alone: without IsPublic false,
            # Jellyfin marks a playlist OpenAccess and every user lists it
            # (measured on 10.11.11 and 12.2.0). Copies in the mirror
            # account stay household-visible, as before.
            body_json["IsPublic"] = False
        status, body = _request_as(
            "POST", "/Playlists", api_key, url,
            json_body=body_json,
        )
        if status is None or status >= 400 or not body or "Id" not in body:
            return {"status": "error", "reason": "create failed", "code": status}
        return {"status": "ok", "remote_id": body["Id"]}

    status, current = _request_as(
        "GET", f"/Playlists/{remote_id}/Items", api_key, url, params={"userId": user_id})
    if status == 404:
        return {"status": "error", "reason": "playlist not found", "code": 404}
    if status is None or status >= 400 or current is None:
        return {"status": "error", "reason": "failed to read current items", "code": status}

    existing_ids = [item["Id"] for item in current.get("Items", []) if item.get("Id")]
    if existing_ids:
        status, _ = _request_as(
            "DELETE", f"/Playlists/{remote_id}/Items", api_key, url,
            params={"entryIds": ",".join(existing_ids)},
        )
        if status is None or status >= 400:
            return {"status": "error", "reason": "failed to clear existing items", "code": status}
    if song_ids:
        status, _ = _request_as(
            "POST", f"/Playlists/{remote_id}/Items", api_key, url,
            params={"ids": ",".join(song_ids), "userId": user_id},
        )
        if status is None or status >= 400:
            return {"status": "error", "reason": "failed to add items", "code": status}
    return {"status": "ok", "remote_id": remote_id}


# The definitions both servers share live in mediabrowser_client.py; each
# reads this module's names (_get, the constants above, _music_library_id)
# through it, so patching one here still reaches every caller.
_client = MediaBrowserClient(
    "jellyfin", sys.modules[__name__], lambda: db.get_mirror_jellyfin_config())
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
