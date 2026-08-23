#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Emby API client — connectivity, playlist browsing, and artist images.

#168: Jellyfin (already supported, see jellyfin_client.py) is a 2018 fork of
Emby's MediaBrowser server, and the two APIs remain near-identical — same
/Users, /Items, /Playlists model, same object shapes. This client is
deliberately its own module rather than a "server type" flag threaded through
jellyfin_client.py: the one real behavioral difference is the auth header
(Emby's own docs specify X-Emby-Token for API-key auth not tied to a user
context, rather than Jellyfin's `Authorization: MediaBrowser ...` scheme),
and keeping that isolated per-module matches how every other provider here
gets its own file, dispatched through main.py's _PROVIDERS — safer than
risking a shared code path silently breaking Jellyfin while adding Emby.

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
unaffected. See mirror_set_playlist_metadata()'s own docstring — this is
accepted as an Emby-side limitation, not chased further.
"""

import threading

import requests

import db
import matching

_CLIENT_ID = "trobar"

_artist_image_key_map: dict[str, str] | None = None
_artist_image_key_map_lock = threading.Lock()
_music_library_id: str | None = None
_music_library_id_lock = threading.Lock()


def _current_config() -> tuple[str, str, str]:
    conn = db.get_conn()
    try:
        url = db.get_config(conn, "emby_url") or ""
        api_key = db.get_config(conn, "emby_api_key") or ""
        user_id = db.get_config(conn, "emby_user_id") or ""
        return url, api_key, user_id
    finally:
        conn.close()


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


def ensure_started() -> None:
    """No persistent connection to establish — kept for interface parity
    with roon_client so main.py's startup dispatch needs no special case."""
    pass


def status() -> dict:
    url, api_key, user_id = _current_config()
    if not url:
        return {"state": "disconnected", "url": url, "provider": "emby"}
    # Confirms both API-key validity and the resolved userId still exists in
    # one call, rather than two separate checks.
    resp = _get(f"/Users/{user_id}", api_key, url) if user_id else None
    if resp is not None and resp.get("Id") == user_id:
        return {"state": "paired", "url": url, "provider": "emby"}
    return {"state": "disconnected", "url": url, "provider": "emby"}


def retry_pairing() -> dict:
    """Emby has no Roon-style manual-approval step to retry — a fresh check
    is the whole story, so this is just status(). Kept as its own function
    purely for interface parity with roon_client.retry_pairing, so main.py's
    dispatch never needs a provider-specific branch."""
    return status()


def _resolve_user_id(url: str, api_key: str, username: str) -> str:
    """Resolves `username` to Emby's internal userId via GET /Users —
    playlist endpoints need that id explicitly, not just the API key.
    Shared by reconnect() (persists the result) and test_connection()
    (#509 item 3, never persists) so the resolution logic exists once."""
    users = _get("/Users", api_key, url)
    if isinstance(users, list):
        match = next((u for u in users if u.get("Name") == username), None)
        if match is not None:
            return match.get("Id", "")
    return ""


def test_connection(url: str, api_key: str, username: str) -> dict:
    """#509 item 3: same check as status(), against EXPLICIT credentials
    rather than the stored config — never persists anything. See
    subsonic_client.test_connection's own docstring for the full
    rationale (the admin config form's live pre-save check)."""
    user_id = _resolve_user_id(url, api_key, username)
    resp = _get(f"/Users/{user_id}", api_key, url) if user_id else None
    if resp is not None and resp.get("Id") == user_id:
        return {"state": "paired", "url": url, "provider": "emby"}
    return {"state": "disconnected", "url": url, "provider": "emby"}


def reconnect(url: str, api_key: str, username: str) -> dict:
    """Admin (re)configured the Emby connection from the web UI — persist it,
    resolve `username` to Emby's internal userId (playlist endpoints need
    that id explicitly, not just the API key), and report whether the
    result checks out."""
    user_id = _resolve_user_id(url, api_key, username)

    conn = db.get_conn()
    try:
        db.set_config(conn, "emby_url", url)
        db.set_config(conn, "emby_api_key", api_key)
        db.set_config(conn, "emby_username", username)
        db.set_config(conn, "emby_user_id", user_id)
        conn.commit()
    finally:
        conn.close()
    return status()


def _list_playlist_items(user_id: str | None = None) -> list[dict] | None:
    """`user_id`: #262's per-Trobar-user override — pass a specific Emby
    userId to list THAT user's own visible playlists (their private ones
    included) instead of the server-wide configured default's. None
    (every pre-#262 call site) keeps today's behaviour."""
    url, api_key, default_user_id = _current_config()
    uid = user_id or default_user_id
    if not uid:
        return None
    resp = _get(f"/Users/{uid}/Items", api_key, url,
                {"IncludeItemTypes": "Playlist", "Recursive": "true"})
    if resp is None:
        return None
    return resp.get("Items", [])


def list_playlists(user_id: str | None = None) -> dict:
    """Returns {"status": "ok", "playlists": [{"id", "title"}, ...]} — the
    common shape every provider client returns (#75). Emby exposes a real,
    stable item Id, so `id` is set and drives the sync's composite key — two
    same-named Emby playlists coexist as separate rows.

    `user_id`: see _list_playlist_items()'s own docstring — #262's
    per-Trobar-user mapping passes this for a mapped user's own listing;
    the ordinary default-account pass (main.py's active-provider dispatch)
    leaves it unset."""
    items = _list_playlist_items(user_id)
    if items is None:
        return {"status": "error", "reason": "not_paired"}
    return {"status": "ok", "playlists": [
        {"id": i["Id"], "title": i["Name"]}
        for i in items if i.get("Name") and i.get("Id")
    ]}


def list_users() -> dict:
    """Returns {"status": "ok", "users": [{"id", "name"}, ...]} — every
    account on the active-provider Emby server, for #262's Administration >
    Configuration per-Trobar-user mapping UI (same shape
    roon_client.list_profiles() already returns for the Roon equivalent).
    Not gated on Emby actually being the active provider — harmless either
    way, the frontend only shows this section when Emby is configured."""
    url, api_key, _user_id = _current_config()
    users = _get("/Users", api_key, url)
    if not isinstance(users, list):
        return {"status": "error", "reason": "not_paired"}
    return {"status": "ok", "users": [
        {"id": u["Id"], "name": u["Name"]} for u in users if u.get("Id") and u.get("Name")
    ]}


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


def _get_music_library_id() -> str | None:
    """Resolved once per process — the music library's own item id, needed
    to scope the artist listing below to just that library."""
    global _music_library_id
    with _music_library_id_lock:
        if _music_library_id is None:
            url, api_key, _user_id = _current_config()
            folders = _get("/Library/VirtualFolders", api_key, url) or []
            music = next((f for f in folders if f.get("CollectionType") == "music"), None)
            _music_library_id = music.get("ItemId", "") if music else ""
        return _music_library_id or None


def _build_artist_image_key_map() -> dict[str, str]:
    url, api_key, user_id = _current_config()
    library_id = _get_music_library_id()
    if not user_id or not library_id:
        return {}
    resp = _get(f"/Users/{user_id}/Items", api_key, url,
                {"IncludeItemTypes": "MusicArtist", "Recursive": "true", "ParentId": library_id})
    if resp is None:
        return {}
    return {a["Name"]: a["Id"] for a in resp.get("Items", []) if a.get("Name") and a.get("Id")}


def _get_artist_image_key_map() -> dict[str, str]:
    """Mirrors roon_client's exact caching pattern — one full artist walk
    per process lifetime, every lookup after that a plain dict get."""
    global _artist_image_key_map
    with _artist_image_key_map_lock:
        if _artist_image_key_map is None:
            _artist_image_key_map = _build_artist_image_key_map()
        return _artist_image_key_map


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


def mirror_status() -> dict:
    """Same shape as status() above, for the mirror-TARGET connection —
    see db.get_mirror_emby_config()'s docstring for why this is a distinct
    connection from the active-provider one status() reports."""
    config = db.get_mirror_emby_config()
    if config is None:
        return {"state": "disconnected", "url": "", "provider": "emby"}
    url, api_key, user_id = config
    resp = _get(f"/Users/{user_id}", api_key, url) if user_id else None
    if resp is not None and resp.get("Id") == user_id:
        return {"state": "paired", "url": url, "provider": "emby"}
    return {"state": "disconnected", "url": url, "provider": "emby"}


def mirror_reconnect(url: str, api_key: str, username: str) -> dict:
    """Admin (re)configured the mirror-TARGET Emby connection — persist it,
    resolve `username` to Emby's internal userId (same reason reconnect()
    above needs to: playlist endpoints need that id explicitly), and
    report whether the result checks out."""
    user_id = ""
    users = _get("/Users", api_key, url)
    if isinstance(users, list):
        match = next((u for u in users if u.get("Name") == username), None)
        if match is not None:
            user_id = match.get("Id", "")

    conn = db.get_conn()
    try:
        db.set_config(conn, "mirror_emby_url", url)
        db.set_config(conn, "mirror_emby_api_key", api_key)
        db.set_config(conn, "mirror_emby_username", username)
        db.set_config(conn, "mirror_emby_user_id", user_id)
        conn.commit()
    finally:
        conn.close()
    return mirror_status()


def mirror_build_tag_index() -> dict[tuple[str, str, str], list[dict]] | None:
    """{(normalized artist, normalized album, normalized title): [{"id",
    "track_no"}, ...]} for the WHOLE mirror-target library, or None if not
    configured or the request failed. Same shape and same reasoning as
    jellyfin_client.mirror_build_tag_index() — see that function's own
    docstring, and subsonic_client.mirror_build_tag_index()'s, for why
    tags, and why every candidate for a key is kept rather than a single
    one.

    Paginated via StartIndex/Limit (confirmed live: same well-behaved
    pagination as Jellyfin's) — walks until a page comes back with zero
    items, not merely fewer than requested."""
    config = db.get_mirror_emby_config()
    if config is None:
        return None
    url, api_key, user_id = config
    if not user_id:
        return None
    index: dict[tuple[str, str, str], list[dict]] = {}
    start = 0
    for _page in range(_MIRROR_MAX_PAGES):
        resp = _get(f"/Users/{user_id}/Items", api_key, url, {
            "IncludeItemTypes": "Audio", "Recursive": "true",
            "StartIndex": start, "Limit": _MIRROR_PAGE_SIZE,
        })
        if resp is None:
            return None
        items = resp.get("Items", [])
        if not items:
            return index
        for item in items:
            if item.get("Id") is None:
                continue
            key = (
                matching.normalize(", ".join(item.get("Artists") or [])),
                matching.normalize(item.get("Album") or ""),
                matching.normalize(item.get("Name") or ""),
            )
            index.setdefault(key, []).append(
                {"id": item["Id"], "track_no": item.get("IndexNumber")})
        start += _MIRROR_PAGE_SIZE
    return None


def mirror_create_or_replace_playlist(
    name: str, song_ids: list[str], remote_id: str | None
) -> dict:
    """Creates (remote_id is None) or FULLY REPLACES (remote_id given) a
    playlist's song list. Returns {"status": "ok", "remote_id": str} or
    {"status": "error", "reason": str, "code": int | None}.

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
      equivalent response has no such second id, so that sink uses the
      track Id directly; this one cannot.

    A stale/nonexistent remote_id does NOT surface cleanly from the replace
    path itself the way Jellyfin's does — GET .../Items for a bad playlist
    id is a bare 500 there (confirmed live), not a 404. So existence is
    checked up front via a dedicated generic-item lookup (confirmed live to
    404 cleanly for a stale id) before ever reaching the replace-specific
    calls."""
    config = db.get_mirror_emby_config()
    if config is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, api_key, user_id = config

    if remote_id is None:
        params: dict = {"Name": name, "UserId": user_id, "MediaType": "Audio"}
        if song_ids:
            params["Ids"] = ",".join(song_ids)
        status, body = _request_as("POST", "/Playlists", api_key, url, params=params)
        if status is None or status >= 400 or not body or "Id" not in body:
            return {"status": "error", "reason": "create failed", "code": status}
        return {"status": "ok", "remote_id": body["Id"]}

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
    return {"status": "ok", "remote_id": remote_id}


def mirror_set_playlist_metadata(remote_id: str, name: str, comment: str) -> None:
    """Best-effort name + subset-transparency comment. Silently no-ops on
    any failure — the song list is what write_mirror() reports errors
    for; a missing/stale name or comment is cosmetic and not worth
    failing the whole write over.

    Same GET-mutate-POST /Items/{id} round trip as jellyfin_client's
    equivalent — confirmed live there's no partial-update endpoint here
    either (a direct POST to /Playlists/{id} is a 404, that path simply
    doesn't exist for Emby).

    The rename half of this reliably sticks (confirmed live), but the
    comment usually does NOT for a playlist that has any items in it —
    confirmed live and reproducible: Emby schedules its own internal
    metadata-refresh pass shortly after a playlist's item list changes
    (add or replace, whether via create or a later write), and that pass
    resets `Overview` back to whatever it was before (empty, the first
    time), a few seconds later — with or without `Overview`/`Name` added
    to the item's own `LockedFields` first, which made no difference.
    Confirmed this is item-list-triggered specifically: the same call
    against a playlist created with an EMPTY id list keeps its comment
    with no reversion. There's no known way from this API to suppress
    that refresh pass, so this is accepted as an Emby-side limitation
    already covered by this function's own best-effort contract above,
    not a bug to chase further here."""
    config = db.get_mirror_emby_config()
    if config is None:
        return
    url, api_key, user_id = config
    if not user_id:
        return
    item = _get(f"/Users/{user_id}/Items/{remote_id}", api_key, url)
    if item is None:
        return
    item["Name"] = name
    item["Overview"] = comment
    _request_as("POST", f"/Items/{remote_id}", api_key, url, json_body=item)


def mirror_delete_playlist(remote_id: str) -> bool:
    """True on a confirmed-ok delete. False otherwise (not configured, or
    the request failed) — the caller decides what that means for its own
    stored state."""
    config = db.get_mirror_emby_config()
    if config is None:
        return False
    url, api_key, _user_id = config
    status, _ = _request_as("DELETE", f"/Items/{remote_id}", api_key, url)
    return status is not None and status < 400
