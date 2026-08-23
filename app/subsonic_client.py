#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subsonic API client — connectivity, playlist browsing, artist images, and
(#189) a playlist-mirroring WRITE sink, against any Subsonic-compatible
server (Navidrome, Airsonic, Gonic, etc.).

The mirror_*() functions at the bottom are a second, independent
connection (db.get_mirror_subsonic_config()) from the active-provider one
every other function here uses (_current_config()) — see that config
getter's own docstring for why the two must never be conflated. They're
grouped in this file rather than a separate module because the wire
protocol, auth scheme, and every mechanic below are identical; only which
credentials and which few endpoints get called differ.

Deliberately generic ("Subsonic", not "Navidrome") since there is no
Navidrome-specific API — Navidrome's own integration surface for third-party
clients *is* the Subsonic API, same as every other server in that family.

Unlike roon_client.py, there's no persistent connection/pairing handshake to
maintain — Subsonic is stateless HTTP, authenticated per-request via a
token/salt pair (t = md5(password + salt), fresh salt every call). No
"pending_approval" state exists either: a valid ping.view means paired,
anything else means disconnected.

Playlist tracks here carry a real `path` (and `album`), unlike Roon's Browse
API which only ever exposes a display title/subtitle — see matching.py's
match_playlist_track_by_path, which this enables as the primary match
strategy instead of relying on the fuzzy artist/title heuristics built for
Roon's fewer-than-ideal metadata.

Artist images deliberately use getCoverArt (the authenticated /rest/ path),
not the artistImageUrl field getArtists also returns — that field is a
/share/img/... link gated by the same Authentik ForwardAuth that fronts the
whole Navidrome domain in this deployment, confirmed directly (a plain GET
redirects to the Authentik login page). getCoverArt stays on the
token-authenticated /rest/ path and was confirmed live to return the image
directly.
"""

import hashlib
import secrets
import threading

import requests

import db
import matching

_API_VERSION = "1.16.1"
_CLIENT_ID = "trobar"

_artist_image_key_map: dict[str, str] | None = None
_artist_image_key_map_lock = threading.Lock()


def _current_config() -> tuple[str, str, str]:
    conn = db.get_conn()
    try:
        url = db.get_config(conn, "subsonic_url") or ""
        username = db.get_config(conn, "subsonic_username") or ""
        password = db.get_config(conn, "subsonic_password") or ""
        return url, username, password
    finally:
        conn.close()


def _auth_params(username: str, password: str) -> dict:
    # The Subsonic API mandates token auth as md5(password + per-request salt)
    # — this is the wire protocol every Subsonic/Navidrome client implements,
    # not password-at-rest hashing. The md5 is required by the spec and can't
    # be substituted. (CodeQL py/weak-sensitive-data-hashing flags this;
    # dismissed as a protocol requirement.)
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()  # noqa: S324
    return {"u": username, "t": token, "s": salt, "v": _API_VERSION, "c": _CLIENT_ID}


def _request_as(
    url: str, username: str, password: str, endpoint: str, params: dict | None = None
) -> dict | None:
    """Low-level JSON Subsonic call against an EXPLICIT server/credential
    set. Returns the parsed 'subsonic-response' dict (check its own
    "status" key), or None if not configured or the request itself failed
    (network error, non-2xx, unparseable body).

    #189: pulled out from the (still-present) _request() below so the
    mirror-target Subsonic connection -- a distinct write destination
    from the active-provider one _current_config() resolves, see
    db.get_mirror_subsonic_config() -- can drive the exact same request/
    auth mechanics without reading through _current_config()."""
    if not url or not username:
        return None
    query = {**_auth_params(username, password), "f": "json", **(params or {})}
    try:
        resp = requests.get(f"{url.rstrip('/')}/rest/{endpoint}", params=query, timeout=10)
        resp.raise_for_status()
        return resp.json()["subsonic-response"]
    except (requests.RequestException, ValueError, KeyError):
        return None


def _request(endpoint: str, params: dict | None = None) -> dict | None:
    """Every existing (read-side, active-provider) call site's own helper
    — unchanged behaviour, now just a thin wrapper over _request_as()."""
    url, username, password = _current_config()
    return _request_as(url, username, password, endpoint, params)


def _as_list(value) -> list:
    """Subsonic's JSON mapping from XML collapses a single child element to
    a bare object instead of a one-item array in some server implementations
    (not observed against Navidrome directly, but not guaranteed either) —
    normalize defensively rather than assume every server behaves the same."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def ensure_started() -> None:
    """No persistent connection to establish — kept for interface parity
    with roon_client so main.py's startup dispatch needs no special case."""
    pass


def status() -> dict:
    url, _, _ = _current_config()
    if not url:
        return {"state": "disconnected", "url": url, "provider": "subsonic"}
    resp = _request("ping.view")
    if resp is not None and resp.get("status") == "ok":
        return {"state": "paired", "url": url, "provider": "subsonic"}
    return {"state": "disconnected", "url": url, "provider": "subsonic"}


def retry_pairing() -> dict:
    """Subsonic has no Roon-style manual-approval step to retry — a fresh
    ping is the whole story, so this is just status(). Kept as its own
    function purely for interface parity with roon_client.retry_pairing,
    so main.py's dispatch never needs a provider-specific branch."""
    return status()


def test_connection(url: str, username: str, password: str) -> dict:
    """#509 item 3: the same ping status() does, against EXPLICIT
    credentials rather than the stored config — never touches db.py, never
    persists anything. For the admin config form's live pre-save check, so
    "Connected" can mean verified just now with what's in the boxes rather
    than only discoverable after Save. Serves BOTH the active-provider
    Subsonic connection and the #189 mirror-target one (main.py's dispatch
    table points "subsonic" and "mirror_subsonic" at this same function) —
    it's the identical question ("is this server reachable with these
    creds") regardless of which config namespace the caller will persist
    the answer under."""
    resp = _request_as(url, username, password, "ping.view")
    if resp is not None and resp.get("status") == "ok":
        return {"state": "paired", "url": url, "provider": "subsonic"}
    return {"state": "disconnected", "url": url, "provider": "subsonic"}


def reconnect(url: str, username: str, password: str) -> dict:
    """Admin (re)configured the Subsonic connection from the web UI —
    persist it and report whether a ping succeeds against the new details."""
    conn = db.get_conn()
    try:
        db.set_config(conn, "subsonic_url", url)
        db.set_config(conn, "subsonic_username", username)
        db.set_config(conn, "subsonic_password", password)
        conn.commit()
    finally:
        conn.close()
    return status()


def list_playlists() -> dict:
    """Returns {"status": "ok", "playlists": [{"id", "title"}, ...]} — the
    common shape every provider client returns (#75), so playlist_sync.py's
    sync loop needs no provider-specific branch. Subsonic exposes a real,
    stable playlist id, so `id` is set (stringified) and drives the sync's
    composite key — two same-named Subsonic playlists coexist as separate
    rows instead of collapsing."""
    resp = _request("getPlaylists")
    if resp is None:
        return {"status": "error", "reason": "not_paired"}
    if resp.get("status") != "ok":
        return {"status": "error", "reason": resp.get("error", {}).get("message", "unknown error")}
    playlists = _as_list(resp.get("playlists", {}).get("playlist"))
    return {"status": "ok", "playlists": [
        {"id": str(p["id"]), "title": p["name"]}
        for p in playlists if p.get("name") and p.get("id") is not None
    ]}


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist", "path", "album"} — the extra `path`/`album` fields (Roon never
    has these) are what let matching.py skip straight to an exact-path match
    instead of its Roon-Browse-API-specific fuzzy fallback.

    Fetched by `source_playlist_id` directly (from list_playlists above) —
    fetching by title would be ambiguous now that same-named playlists can
    coexist. Falls back to a title match only if no id is given (belt-and-
    suspenders; the sync always passes the id)."""
    if source_playlist_id is not None:
        playlist_id: str | None = source_playlist_id
    else:
        list_resp = _request("getPlaylists")
        if list_resp is None or list_resp.get("status") != "ok":
            return {"status": "error", "reason": "not_paired"}
        playlists = _as_list(list_resp.get("playlists", {}).get("playlist"))
        match = next((p for p in playlists if p.get("name") == playlist_title), None)
        if match is None:
            return {"status": "not_found", "failed_segment": playlist_title}
        playlist_id = str(match["id"])

    resp = _request("getPlaylist", {"id": playlist_id})
    if resp is None or resp.get("status") != "ok":
        return {"status": "error", "reason": "getPlaylist failed"}
    entries = _as_list(resp.get("playlist", {}).get("entry"))

    tracks = [
        {
            "position": i,
            "title": e.get("title", ""),
            "artist": e.get("artist", ""),
            "path": e.get("path"),
            "album": e.get("album"),
        }
        for i, e in enumerate(entries)
    ]
    return {"status": "ok", "playlist": playlist_title, "tracks": tracks}


def _build_artist_image_key_map() -> dict[str, str]:
    resp = _request("getArtists")
    if resp is None or resp.get("status") != "ok":
        return {}
    out: dict[str, str] = {}
    for index in _as_list(resp.get("artists", {}).get("index")):
        for artist in _as_list(index.get("artist")):
            if artist.get("name") and artist.get("coverArt"):
                out[artist["name"]] = artist["coverArt"]
    return out


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
    cover_id = _get_artist_image_key_map().get(artist_name)
    if not cover_id:
        return None

    url, username, password = _current_config()
    if not url or not username:
        return None
    query = {**_auth_params(username, password), "id": cover_id}
    try:
        resp = requests.get(f"{url.rstrip('/')}/rest/getCoverArt", params=query, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return resp.content, content_type


# ---------------------------------------------------------------------------
# #189 mirror-target sink — a Subsonic/Navidrome server as a WRITE
# destination, not a read source. Own connection (db.get_mirror_subsonic_
# config()), own functions below; see the module docstring for why they
# live here rather than a separate client file.
# ---------------------------------------------------------------------------

# Empirically fine against the dev sandbox's Navidrome at 15 songs; not
# verified against a large real library, so this is a starting point, not
# a confirmed-safe ceiling — revisit if pagination proves slow. A server
# that caps songCount below this is handled correctly either way (the walk
# below only stops on a truly empty page, never on "fewer than requested").
_MIRROR_PAGE_SIZE = 500

# Backstop against a server that ignores songOffset and would otherwise
# hand back a full page forever — bounds the walk at 2M songs, comfortably
# past any real library, so a real target still indexes completely while a
# broken one fails closed instead of looping inside the sync worker.
_MIRROR_MAX_PAGES = 4000

# Subsonic's "data not found" error code — returned by createPlaylist when
# the given playlistId no longer exists on the target (deleted there since
# Trobar's last write). See mirror_subsonic.write_mirror()'s handling of
# this: the stored remote id is stale, not a real write failure.
ERROR_DATA_NOT_FOUND = 70


def mirror_status() -> dict:
    """Same shape as status() above, for the mirror-TARGET connection —
    see db.get_mirror_subsonic_config()'s docstring for why this is a
    distinct connection from the active-provider one status() reports."""
    config = db.get_mirror_subsonic_config()
    if config is None:
        return {"state": "disconnected", "url": "", "provider": "subsonic"}
    url, username, password = config
    resp = _request_as(url, username, password, "ping.view")
    if resp is not None and resp.get("status") == "ok":
        return {"state": "paired", "url": url, "provider": "subsonic"}
    return {"state": "disconnected", "url": url, "provider": "subsonic"}


def mirror_reconnect(url: str, username: str, password: str) -> dict:
    """Admin (re)configured the mirror-TARGET Subsonic connection —
    persist it and report whether a ping succeeds, same contract as
    reconnect() above for the active-provider one."""
    conn = db.get_conn()
    try:
        db.set_config(conn, "mirror_subsonic_url", url)
        db.set_config(conn, "mirror_subsonic_username", username)
        db.set_config(conn, "mirror_subsonic_password", password)
        conn.commit()
    finally:
        conn.close()
    return mirror_status()


def mirror_build_tag_index() -> dict[tuple[str, str, str], list[dict]] | None:
    """{(normalized artist, normalized album, normalized title): [{"id",
    "track_no"}, ...]} for the WHOLE mirror-target library, or None if not
    configured or the request failed.

    Keyed on tags, NOT the song object's own `path` field — Navidrome (and
    presumably every other Subsonic server without a "report real path"
    escape hatch a client can't turn on remotely anyway) synthesizes that
    path from the same tags rather than reporting the real one, so it's a
    lossy derivative of exactly the data available directly on the same
    song object, not an independent, more-reliable identity. Confirmed
    live against Navidrome 0.63.2: an on-disk "Artist/Album (2001)/01 -
    Title.flac" comes back with path "Artist/Album/01 - Title.flac" — the
    tag-derived path drops the year suffix a real filesystem path would
    keep, among other lossy reformatting (compilations folded onto
    AlbumArtist, disc-numbered albums gaining a prefix, etc). The tags
    themselves round-trip untouched on the same song object, so read those
    instead of the derived path they produced.

    Normalized with matching.normalize() — the same Unicode-correct
    casefold + whitespace collapse already used for the read-side Roon
    matcher — rather than a second ad hoc scheme.

    Not keyed all the way down to track_no: two tracks can legitimately
    share (artist, album, title) — write_mirror() disambiguates using
    track_no only as a tiebreaker when a key has more than one candidate,
    which is why every candidate for a key is kept (a plain one-id-per-key
    dict would silently last-write-wins if a target library holds the same
    album twice, e.g. a FLAC and an MP3 copy).

    Any page failing mid-walk fails the whole index (returns None) rather
    than returning a partial one: a partial index would make present
    tracks look target-absent and silently drop them from the mirrored
    playlist, which is worse than write_mirror() leaving the previous
    mirror content stale until the next successful sync — this module's
    functions never raise, so failing "whole" here is what lets the
    caller tell the two apart. The walk only stops on a page that comes
    back with zero songs (not merely fewer than requested) — a server
    that clamps songCount below _MIRROR_PAGE_SIZE would otherwise look
    indistinguishable from having reached the end, silently truncating
    the index. _MIRROR_MAX_PAGES bounds the opposite failure: a server
    that ignores songOffset and keeps returning the same full page."""
    config = db.get_mirror_subsonic_config()
    if config is None:
        return None
    url, username, password = config
    index: dict[tuple[str, str, str], list[dict]] = {}
    offset = 0
    for _page in range(_MIRROR_MAX_PAGES):
        resp = _request_as(url, username, password, "search3", {
            "query": "", "songCount": _MIRROR_PAGE_SIZE, "songOffset": offset,
            "artistCount": 0, "albumCount": 0,
        })
        if resp is None or resp.get("status") != "ok":
            return None
        songs = _as_list(resp.get("searchResult3", {}).get("song"))
        if not songs:
            return index
        for song in songs:
            if song.get("id") is None:
                continue
            key = (
                matching.normalize(song.get("artist", "")),
                matching.normalize(song.get("album", "")),
                matching.normalize(song.get("title", "")),
            )
            index.setdefault(key, []).append(
                {"id": str(song["id"]), "track_no": song.get("track")})
        offset += _MIRROR_PAGE_SIZE
    return None


def mirror_create_or_replace_playlist(
    name: str, song_ids: list[str], remote_id: str | None
) -> dict:
    """Creates (remote_id is None) or FULLY REPLACES (remote_id given) a
    playlist's song list in one call — confirmed live against Navidrome:
    re-calling createPlaylist with an existing playlistId replaces the
    song list outright (verified: a 2-song playlist re-created with one
    different songId came back with exactly 1 song, not a union), not an
    incremental add. That's the entire "one-way, golden-wins, full
    idempotent rewrite, never a delta" semantics #189 settled on, in a
    single request — no delete+recreate, no updatePlaylist add/remove-by-
    index dance. Returns {"status": "ok", "remote_id": str} or
    {"status": "error", "reason": str, "code": int | None} — `code` is
    Subsonic's own numeric error code when the server returned one (None
    for a request that failed before getting a response at all), so a
    caller can react to a specific one (see ERROR_DATA_NOT_FOUND) without
    string-matching `reason`."""
    config = db.get_mirror_subsonic_config()
    if config is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, username, password = config
    params: dict = {"name": name, "songId": song_ids}
    if remote_id is not None:
        params["playlistId"] = remote_id
    resp = _request_as(url, username, password, "createPlaylist", params)
    if resp is None or resp.get("status") != "ok" or "playlist" not in resp:
        error = (resp or {}).get("error", {})
        return {"status": "error", "reason": error.get("message", "request failed"),
                "code": error.get("code")}
    return {"status": "ok", "remote_id": str(resp["playlist"]["id"])}


def mirror_set_playlist_metadata(remote_id: str, name: str, comment: str) -> None:
    """Best-effort name + subset-transparency comment (confirmed live:
    updatePlaylist with no songId params touches only the fields given,
    round-tripped via getPlaylist, and leaves the song list untouched).
    Silently no-ops on any failure — the song list is what write_mirror()
    reports errors for; a missing/stale name or comment is cosmetic and
    not worth failing the whole write over.

    #189 review: `name` matters here specifically because
    mirror_create_or_replace_playlist()'s own createPlaylist call does
    NOT rename on replace — confirmed live, Navidrome ignores `name` when
    `playlistId` is given (it only applies on the create branch). Without
    this, retitling a playlist in Trobar left the mirror's old name on
    the target forever."""
    config = db.get_mirror_subsonic_config()
    if config is None:
        return
    url, username, password = config
    _request_as(
        url, username, password, "updatePlaylist",
        {"playlistId": remote_id, "name": name, "comment": comment},
    )


def mirror_delete_playlist(remote_id: str) -> bool:
    """True on a confirmed-ok delete. False otherwise (not configured, or
    the request failed) — the caller decides what that means for its own
    stored state."""
    config = db.get_mirror_subsonic_config()
    if config is None:
        return False
    url, username, password = config
    resp = _request_as(url, username, password, "deletePlaylist", {"id": remote_id})
    return resp is not None and resp.get("status") == "ok"
