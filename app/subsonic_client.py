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
import logging
import secrets

import requests

import db
import provider_config
from artist_image_keys import ArtistImageKeys
import matching

_log = logging.getLogger(__name__)

_API_VERSION = "1.16.1"
_CLIENT_ID = "trobar"

_artist_image_key_map = ArtistImageKeys()


def _current_config() -> tuple[str, str, str]:
    # An unsaved connection the switch preview reads through: provider_config.
    unsaved = provider_config.override("subsonic")
    if unsaved is not None:
        return unsaved
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
        # Whether it is a Navidrome admin, which members' own playlists and
        # mirror copies need (None when the server doesn't say).
        return {"state": "paired", "url": url, "provider": "subsonic",
                "is_admin": is_admin(url, username, password)}
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
    # `owner` (the server's username) and `public` as the server gives them:
    # Navidrome lists every user's playlists to an admin account, private
    # ones included, so the sync needs both to attribute and protect them.
    # A server that sends no owner gives None, and nothing changes for it.
    return {"status": "ok", "playlists": [
        {"id": str(p["id"]), "title": p["name"], "owner": p.get("owner") or None,
         "public": bool(p.get("public"))}
        for p in playlists if p.get("name") and p.get("id") is not None
    ]}


def account_username() -> str:
    """The library account's own username, as configured."""
    return _current_config()[1]


def check_login(url: str, username: str, password: str) -> bool | None:
    """Whether the server accepts this username and password: one ping,
    nothing kept. None when the server can't be reached (or answers with
    something other than a Subsonic response)."""
    resp = _request_as(url, username, password, "ping.view")
    if resp is None:
        return None
    return resp.get("status") == "ok"


def is_admin(url: str, username: str, password: str) -> bool | None:
    """Whether this account is an admin on its server: Subsonic's getUser on
    the account itself, whose `adminRole` Navidrome reports. None when the
    server can't be asked or doesn't say."""
    resp = _request_as(url, username, password, "getUser", {"username": username})
    if resp is None or resp.get("status") != "ok":
        return None
    role = resp.get("user", {}).get("adminRole")
    return role if isinstance(role, bool) else None


def _navidrome_token(url: str, username: str, password: str) -> str | None:
    """A session token for Navidrome's own API, which an admin needs for
    what the Subsonic API won't do (listing users, giving a playlist to its
    owner). None if the login fails or the server isn't Navidrome."""
    if not url or not username:
        return None
    try:
        resp = requests.post(f"{url.rstrip('/')}/auth/login",
                             json={"username": username, "password": password}, timeout=10)
        resp.raise_for_status()
        token = resp.json().get("token")
    except (requests.RequestException, ValueError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


def _navidrome_users(url: str, token: str) -> list[dict] | None:
    try:
        resp = requests.get(f"{url.rstrip('/')}/api/user",
                            headers={"X-ND-Authorization": f"Bearer {token}"}, timeout=10)
        resp.raise_for_status()
        users = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(users, list):
        return None
    return [u for u in users if isinstance(u, dict) and isinstance(u.get("userName"), str)
            and isinstance(u.get("id"), str)]


def navidrome_give_playlist(url: str, username: str, password: str, playlist_id: str,
                            owner: str) -> bool:
    """Moves a playlist into `owner`'s account on a Navidrome server, with
    an admin account, through its own API: the Subsonic API can't, and
    Navidrome ignores an owner given at creation. Name and tracks stay, and
    the admin account can go on writing it. False when it can't be done
    (not Navidrome, not an admin, no such user)."""
    token = _navidrome_token(url, username, password)
    if token is None:
        return False
    users = _navidrome_users(url, token)
    target = next((u["id"] for u in users or [] if u["userName"].casefold() == owner.casefold()), None)
    if target is None:
        return False
    try:
        resp = requests.put(f"{url.rstrip('/')}/api/playlist/{playlist_id}", json={"ownerId": target},
                            headers={"X-ND-Authorization": f"Bearer {token}"}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return False
    return True


def navidrome_usernames(url: str, username: str, password: str) -> set[str] | None:
    """Every username on a Navidrome server, case-folded, read through its
    own API with an admin account (Subsonic's getUser and getUsers won't
    name other users, even to an admin). None when that can't be read: not
    Navidrome, not an admin, or not reachable."""
    token = _navidrome_token(url, username, password)
    users = _navidrome_users(url, token) if token is not None else None
    if users is None:
        return None
    return {u["userName"].casefold() for u in users}


def _first_isrc(value) -> str | None:
    """OpenSubsonic servers (Navidrome) give a song's ISRCs as a list; older
    Subsonic servers give none. The first one, or None."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist", "path", "album", "mbid", "isrc"} — the extra `path`/`album`
    fields (Roon never has these) are what let matching.py skip straight to
    an exact-path match instead of its Roon-Browse-API-specific fuzzy
    fallback. `mbid` (the MusicBrainz recording id) and `isrc` come from
    OpenSubsonic servers such as Navidrome, and are None elsewhere. Note that
    Navidrome's `path` is rebuilt in its own naming pattern, not the file's.

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
            "mbid": e.get("musicBrainzId") or None,
            "isrc": _first_isrc(e.get("isrc")),
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
    return _artist_image_key_map.get(_build_artist_image_key_map)


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


def mirror_read_items(remote_id: str) -> list[dict] | None:
    """The copy's entries as they stand, in order, as {"id", "artist",
    "title"}, or None when it can't be read (not configured, gone, or no
    answer). Read before a replace, so the sink can say what the replace
    overwrote; a failed read only means nothing is said."""
    config = db.get_mirror_subsonic_config()
    if config is None:
        return None
    url, username, password = config
    resp = _request_as(url, username, password, "getPlaylist", {"id": remote_id})
    if resp is None or resp.get("status") != "ok" or "playlist" not in resp:
        return None
    return [{"id": str(e.get("id")), "artist": e.get("artist") or "", "title": e.get("title") or ""}
            for e in _as_list(resp["playlist"].get("entry")) if e.get("id") is not None]


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
