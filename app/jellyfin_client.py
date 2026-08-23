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
        url = db.get_config(conn, "jellyfin_url") or ""
        api_key = db.get_config(conn, "jellyfin_api_key") or ""
        user_id = db.get_config(conn, "jellyfin_user_id") or ""
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


def ensure_started() -> None:
    """No persistent connection to establish — kept for interface parity
    with roon_client so main.py's startup dispatch needs no special case."""
    pass


def status() -> dict:
    url, api_key, user_id = _current_config()
    if not url:
        return {"state": "disconnected", "url": url, "provider": "jellyfin"}
    # Confirms both API-key validity and the resolved userId still exists in
    # one call, rather than two separate checks.
    resp = _get(f"/Users/{user_id}", api_key, url) if user_id else None
    if resp is not None and resp.get("Id") == user_id:
        return {"state": "paired", "url": url, "provider": "jellyfin"}
    return {"state": "disconnected", "url": url, "provider": "jellyfin"}


def retry_pairing() -> dict:
    """Jellyfin has no Roon-style manual-approval step to retry — a fresh
    check is the whole story, so this is just status(). Kept as its own
    function purely for interface parity with roon_client.retry_pairing,
    so main.py's dispatch never needs a provider-specific branch."""
    return status()


def _resolve_user_id(url: str, api_key: str, username: str) -> str:
    """Resolves `username` to Jellyfin's internal userId via GET /Users —
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
        return {"state": "paired", "url": url, "provider": "jellyfin"}
    return {"state": "disconnected", "url": url, "provider": "jellyfin"}


def reconnect(url: str, api_key: str, username: str) -> dict:
    """Admin (re)configured the Jellyfin connection from the web UI —
    persist it, resolve `username` to Jellyfin's internal userId (playlist
    endpoints need that id explicitly, not just the API key), and report
    whether the result checks out."""
    user_id = _resolve_user_id(url, api_key, username)

    conn = db.get_conn()
    try:
        db.set_config(conn, "jellyfin_url", url)
        db.set_config(conn, "jellyfin_api_key", api_key)
        db.set_config(conn, "jellyfin_username", username)
        db.set_config(conn, "jellyfin_user_id", user_id)
        conn.commit()
    finally:
        conn.close()
    return status()


def _list_playlist_items(user_id: str | None = None) -> list[dict] | None:
    """`user_id`: #262's per-Trobar-user override — pass a specific
    Jellyfin userId to list THAT user's own visible playlists (their
    private ones included) instead of the server-wide configured
    default's. None (every pre-#262 call site) keeps today's behaviour."""
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
    common shape every provider client returns (#75). Jellyfin exposes a
    real, stable item Id, so `id` is set and drives the sync's composite
    key — two same-named Jellyfin playlists coexist as separate rows.

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
    account on the active-provider Jellyfin server, for #262's
    Administration > Configuration per-Trobar-user mapping UI (same shape
    roon_client.list_profiles() already returns for the Roon equivalent).
    Not gated on Jellyfin actually being the active provider — harmless
    either way, the frontend only shows this section when Jellyfin is
    configured."""
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


def mirror_status() -> dict:
    """Same shape as status() above, for the mirror-TARGET connection —
    see db.get_mirror_jellyfin_config()'s docstring for why this is a
    distinct connection from the active-provider one status() reports."""
    config = db.get_mirror_jellyfin_config()
    if config is None:
        return {"state": "disconnected", "url": "", "provider": "jellyfin"}
    url, api_key, user_id = config
    resp = _get(f"/Users/{user_id}", api_key, url) if user_id else None
    if resp is not None and resp.get("Id") == user_id:
        return {"state": "paired", "url": url, "provider": "jellyfin"}
    return {"state": "disconnected", "url": url, "provider": "jellyfin"}


def mirror_reconnect(url: str, api_key: str, username: str) -> dict:
    """Admin (re)configured the mirror-TARGET Jellyfin connection — persist
    it, resolve `username` to Jellyfin's internal userId (same reason
    reconnect() above needs to: playlist endpoints need that id
    explicitly), and report whether the result checks out."""
    user_id = ""
    users = _get("/Users", api_key, url)
    if isinstance(users, list):
        match = next((u for u in users if u.get("Name") == username), None)
        if match is not None:
            user_id = match.get("Id", "")

    conn = db.get_conn()
    try:
        db.set_config(conn, "mirror_jellyfin_url", url)
        db.set_config(conn, "mirror_jellyfin_api_key", api_key)
        db.set_config(conn, "mirror_jellyfin_username", username)
        db.set_config(conn, "mirror_jellyfin_user_id", user_id)
        conn.commit()
    finally:
        conn.close()
    return mirror_status()


def mirror_build_tag_index() -> dict[tuple[str, str, str], list[dict]] | None:
    """{(normalized artist, normalized album, normalized title): [{"id",
    "track_no"}, ...]} for the WHOLE mirror-target library, or None if not
    configured or the request failed. Same shape and same reasoning as
    subsonic_client.mirror_build_tag_index() — see that function's own
    docstring for why tags, not Path, and why every candidate for a key is
    kept rather than a single one.

    `Artists` is a list here (unlike Subsonic's single string), joined
    with ", " — the exact same convention this file's own read-side
    get_playlist_tracks() already uses for matching.py, so a track's
    identity is computed the same way whichever direction it's used.
    `IndexNumber` is Jellyfin's own track-number field.

    Paginated via StartIndex/Limit (confirmed live: a short final page is
    genuinely the end, StartIndex past the end returns zero items rather
    than erroring) — walks until a page comes back with zero items, not
    merely fewer than requested, same defensive stance as the Subsonic
    sink for a server that might clamp the page size lower than asked."""
    config = db.get_mirror_jellyfin_config()
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
    playlist empty, but confirmed live it doesn't work: re-adding an id
    that's already present is deduped (204, no second entry), so adding
    the new set before removing the old would delete the overlap right
    back out along with the old entries. A failure between the DELETE and
    the POST here does leave the remote playlist empty until the next
    successful sync repairs it — self-healing (the failure is recorded as
    `write_failed` with the remote id kept, so the next run retries the
    same replace), but a narrower guarantee than Subsonic's atomic single-
    call replace, which never has an empty window at all."""
    config = db.get_mirror_jellyfin_config()
    if config is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, api_key, user_id = config

    if remote_id is None:
        status, body = _request_as(
            "POST", "/Playlists", api_key, url,
            json_body={"Name": name, "Ids": song_ids, "UserId": user_id, "MediaType": "Audio"},
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


def mirror_set_playlist_metadata(remote_id: str, name: str, comment: str) -> None:
    """Best-effort name + subset-transparency comment. Silently no-ops on
    any failure — the song list is what write_mirror() reports errors
    for; a missing/stale name or comment is cosmetic and not worth
    failing the whole write over.

    Confirmed live there's no partial-update endpoint for this: neither
    a bare `{"Name": ...}` POST to /Playlists/{id} nor a fuller
    Name+Ids+UserId body ever returned anything but 400 against a real
    10.11.11 instance. What actually works is Jellyfin's generic
    metadata-edit path — GET the playlist's own full item representation,
    mutate just the fields this cares about, POST the WHOLE thing back to
    /Items/{id} — the same round-trip the admin "Edit Metadata" UI
    feature itself does. `Overview` (normally a movie/show synopsis
    field) is the closest thing a playlist item has to Subsonic's
    `comment`; confirmed live it round-trips correctly and leaves the
    song list untouched."""
    config = db.get_mirror_jellyfin_config()
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
    config = db.get_mirror_jellyfin_config()
    if config is None:
        return False
    url, api_key, _user_id = config
    status, _ = _request_as("DELETE", f"/Items/{remote_id}", api_key, url)
    return status is not None and status < 400
