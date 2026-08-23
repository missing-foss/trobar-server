#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lyrion Music Server (LMS, formerly Logitech Media Server / Squeezebox
Server) API client — connectivity, playlist browsing.

Server-provider, like subsonic_client.py/jellyfin_client.py: an
admin-configured server URL, with an OPTIONAL username/password — LMS's own
"Authorize" setting is off by default (self-hosted, LAN, no partner gate,
gate is not on by default), and when on, it gates the whole interface with
plain HTTP Basic Auth (confirmed live: enabling it makes every endpoint,
including this one, answer 401 with `WWW-Authenticate: Basic`). The
CLI-protocol `login` command documented for LMS's raw telnet interface
(port 9090) is a different, unrelated interface — this client only ever
talks to the JSON-RPC-over-HTTP bridge (port 9000, `/jsonrpc.js`).

Unlike roon_client.py, there's no persistent connection/pairing handshake
to maintain — the JSON-RPC bridge is stateless HTTP. No "pending_approval"
state exists either: a valid `serverstatus` call means paired, anything
else means disconnected.

Confirmed live against a real Lyrion Music Server 9.1.1
(lmscommunity/lyrionmusicserver, the official community image) with a
small scanned library and a real `.m3u`-sourced saved playlist:
- Every JSON-RPC call is `POST {base}/jsonrpc.js` with body
  `{"id": 1, "method": "slim.request", "params": ["", [<command>, <args...>]]}`
  — the empty first params element is LMS's per-player scope, left blank
  for these player-agnostic database/playlist queries.
- `["playlists", start, count, "tags:u"]` → `{"playlists_loop": [{"id",
  "playlist", "url"}, ...]}` — note the *response* key is `playlist`
  (the name), not `name`.
- `["playlists", "tracks", start, count, "playlist_id:<id>", "tags:galdtu"]`
  → `{"playlisttracks_loop": [{"playlist index", "id", "title", "artist",
  "album", "url", ...}, ...]}` — `"playlist index"` is a literal
  space-containing key, LMS's own CLI-derived naming.
- Each track's `url` is a `file://` URL to the file on the LMS SERVER's own
  filesystem (typically the same share Trobar's MUSIC_ROOT points at, in a
  self-hosted setup) — decoded and root-relative-ized the same way
  itunes_library.py handles Library.xml's `Location` field, for the same
  reason: matching.py's trailing-segment comparison tolerates whatever
  mount-prefix mismatch is left over.

get_artist_image() is deliberately a stub (see its own docstring) — #172's
acceptance scope is `list_playlists`/`get_playlist_tracks` only.
"""

from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

import db

# Large enough for a typical home-library playlist/playlist-count without
# real pagination — LMS's own "start count" params require a concrete
# upper bound, not an "all" sentinel.
_MAX_ITEMS = 999


def _current_config() -> tuple[str, str, str]:
    conn = db.get_conn()
    try:
        url = db.get_config(conn, "lms_url") or ""
        username = db.get_config(conn, "lms_username") or ""
        password = db.get_config(conn, "lms_password") or ""
        return url, username, password
    finally:
        conn.close()


def _request(command: list, url: str, username: str, password: str):
    """Low-level JSON-RPC call. Returns the parsed `result` object, or None
    if not configured or the request itself failed (network error, non-2xx,
    unparseable body). Basic Auth is only sent when a username is
    configured — LMS's "Authorize" setting (and therefore this gate) is
    off by default."""
    if not url:
        return None
    auth = (username, password) if username else None
    try:
        resp = requests.post(
            f"{url.rstrip('/')}/jsonrpc.js",
            json={"id": 1, "method": "slim.request", "params": ["", command]},
            auth=auth, timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("result")
    except (requests.RequestException, ValueError):
        return None


def ensure_started() -> None:
    """No persistent connection to establish — kept for interface parity
    with roon_client so main.py's startup dispatch needs no special case."""
    pass


def status() -> dict:
    url, username, password = _current_config()
    if not url:
        return {"state": "disconnected", "url": url, "provider": "lms"}
    result = _request(["serverstatus", "0", "10"], url, username, password)
    if result is not None and result.get("uuid"):
        return {"state": "paired", "url": url, "provider": "lms"}
    return {"state": "disconnected", "url": url, "provider": "lms"}


def retry_pairing() -> dict:
    """LMS has no Roon-style manual-approval step to retry — a fresh
    check is the whole story, so this is just status(). Kept as its own
    function purely for interface parity with roon_client.retry_pairing,
    so main.py's dispatch never needs a provider-specific branch."""
    return status()


def test_connection(url: str, username: str, password: str) -> dict:
    """#509 item 3: same check as status(), against EXPLICIT credentials
    rather than the stored config — never persists anything. username/
    password stay optional here too (LMS's "Authorize" setting is off by
    default) — main.py's dispatch only requires `url` for this provider,
    unlike the other test_connection()s where every field is required."""
    result = _request(["serverstatus", "0", "10"], url, username, password)
    if result is not None and result.get("uuid"):
        return {"state": "paired", "url": url, "provider": "lms"}
    return {"state": "disconnected", "url": url, "provider": "lms"}


def reconnect(url: str, username: str, password: str) -> dict:
    """Admin (re)configured the LMS connection from the web UI — persist
    it and report whether the result checks out. username/password are
    optional (blank when the server's "Authorize" setting is off, the
    self-hosted default)."""
    conn = db.get_conn()
    try:
        db.set_config(conn, "lms_url", url)
        db.set_config(conn, "lms_username", username)
        db.set_config(conn, "lms_password", password)
        conn.commit()
    finally:
        conn.close()
    return status()


def list_playlists() -> dict:
    """Returns {"status": "ok", "playlists": [{"id", "title"}, ...]} — the
    common shape every provider client returns (#75). LMS exposes a real,
    stable playlist id, so `id` is set (stringified) and drives the sync's
    composite key — two same-named LMS playlists coexist as separate rows
    instead of collapsing."""
    url, username, password = _current_config()
    result = _request(["playlists", "0", str(_MAX_ITEMS), "tags:u"], url, username, password)
    if result is None:
        return {"status": "error", "reason": "not_paired"}
    items = result.get("playlists_loop") or []
    return {"status": "ok", "playlists": [
        {"id": str(i["id"]), "title": i["playlist"]}
        for i in items if i.get("playlist") and i.get("id") is not None
    ]}


def _url_to_path(url_value: str | None, music_root: Path) -> str | None:
    """Mirrors itunes_library.py's Location-field handling: decode the
    file:// URL (percent-decoding, the Windows-drive-letter leading-slash
    fix), then convert to the same root-relative form tracks.relative_path
    uses when it falls under MUSIC_ROOT — the common case for a
    self-hosted LMS pointed at the same share as Trobar — leaving the raw
    absolute path otherwise (matching.py's trailing-segment comparison
    tolerates a differing mount prefix)."""
    if not url_value:
        return None
    parts = urlsplit(url_value)
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    try:
        return str(Path(path).relative_to(music_root))
    except ValueError:
        return path


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist", "path", "album"} — path/album are what let matching.py skip
    straight to an exact-path match instead of its Roon-Browse-API-specific
    fuzzy fallback.

    Fetched by `source_playlist_id` directly (the id from list_playlists)
    — fetching by title would be ambiguous now that same-named playlists
    can coexist. Falls back to a title match only if no id is given (the
    sync always passes it)."""
    url, username, password = _current_config()
    music_root = db.get_music_root()

    if source_playlist_id is not None:
        playlist_id: str | None = source_playlist_id
    else:
        listed = list_playlists()
        if listed["status"] != "ok":
            return {"status": "error", "reason": "not_paired"}
        match = next((p for p in listed["playlists"] if p["title"] == playlist_title), None)
        if match is None:
            return {"status": "not_found", "failed_segment": playlist_title}
        playlist_id = match["id"]

    result = _request(
        ["playlists", "tracks", "0", str(_MAX_ITEMS), f"playlist_id:{playlist_id}", "tags:galdtu"],
        url, username, password,
    )
    if result is None:
        return {"status": "error", "reason": "playlist items fetch failed"}
    entries = result.get("playlisttracks_loop") or []

    tracks = [
        {
            "position": i,
            "title": e.get("title", ""),
            "artist": e.get("artist", ""),
            "path": _url_to_path(e.get("url"), music_root),
            "album": e.get("album"),
        }
        for i, e in enumerate(entries)
    ]
    return {"status": "ok", "playlist": playlist_title, "tracks": tracks}


def get_artist_image(artist_name: str) -> tuple[bytes, str] | None:
    """Always None — #172 scopes this provider to list_playlists/
    get_playlist_tracks only. LMS does expose artist art (an `artists`
    query plus a `/music/{id}/cover` image endpoint), so this is a
    reasonable follow-up, not a hard limitation — just out of scope here.
    A miss here just means no picture (same contract as every other
    provider's get_artist_image) — artist_images.py still falls back to
    the filesystem provider, so this never blocks a sync."""
    return None
