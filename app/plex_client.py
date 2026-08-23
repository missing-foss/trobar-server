#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plex Media Server API client — connectivity, playlist browsing.

Server-provider, like subsonic_client.py/jellyfin_client.py: an
admin-configured connection (server URL + a server-scoped X-Plex-Token),
not a per-user OAuth streaming link. Unlike jellyfin_client.py, there's no
separate userId resolution step — the token itself is scoped to the Plex
account that owns (or was shared) the server, so playlist visibility
follows the token, not a chosen username. Plex is the one
directly-buildable provider among the streaming candidates surveyed: an
official, documented 2025 API, no
credential/partner gate.

Unlike roon_client.py, there's no persistent connection/pairing handshake
to maintain — Plex's HTTP API is stateless, authenticated per-request via
a static token (X-Plex-Token header). No "pending_approval" state exists
either: a valid authenticated call means paired, anything else means
disconnected.

Plex returns XML by default; `Accept: application/json` gets the JSON
shape used throughout this module (`{"MediaContainer": {"Metadata": [...]}}`
for every list-shaped response).

Playlist tracks here carry a real on-disk path (Media[0].Part[0].file) and
album (parentTitle), unlike Roon's Browse API which only ever exposes a
display title/subtitle — see matching.py's match_playlist_track_by_path,
which this enables as the primary match strategy instead of relying on the
fuzzy artist/title heuristics built for Roon's fewer-than-ideal metadata.
This is a real accuracy win over the streaming providers (Tidal/Spotify),
which have no local path to give — free here since Plex, as a server that
scans and tags the same kind of local library Trobar does, already has it.

Not live-verified against a real Plex server (none available in this
environment) — built directly from Plex's official API docs
(developer.plex.tv / plexapi.dev) and the python-plexapi reference
implementation. This is worth a real-server pass (confirming the exact JSON
shapes and current token/JWT behavior) before treating the provider as
fully proven out — same caveat as emby_client.py carries for Emby.

get_artist_image() is deliberately a stub (see its own docstring) — #158
explicitly scopes Plex artist images as an "(optional follow-up)", not
part of this provider's initial acceptance criteria.
"""

import requests

import db

_MUSIC_PLAYLIST_TYPE = "audio"


def _current_config() -> tuple[str, str]:
    conn = db.get_conn()
    try:
        url = db.get_config(conn, "plex_url") or ""
        token = db.get_config(conn, "plex_token") or ""
        return url, token
    finally:
        conn.close()


def _get(endpoint: str, token: str, url: str, params: dict | None = None):
    """Low-level authenticated GET. Returns the parsed JSON body, or None if
    not configured or the request itself failed (network error, non-2xx,
    unparseable body)."""
    if not url or not token:
        return None
    headers = {"Accept": "application/json", "X-Plex-Token": token}
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
    url, token = _current_config()
    if not url:
        return {"state": "disconnected", "url": url, "provider": "plex"}
    # The PMS root ("/") returns server identity (MediaContainer with
    # friendlyName/machineIdentifier) for any request carrying a valid
    # token, and a 401 (-> None from _get) otherwise — the simplest
    # available "is this token still good" check.
    resp = _get("/", token, url)
    if resp is not None and resp.get("MediaContainer") is not None:
        return {"state": "paired", "url": url, "provider": "plex"}
    return {"state": "disconnected", "url": url, "provider": "plex"}


def retry_pairing() -> dict:
    """Plex has no Roon-style manual-approval step to retry — a fresh
    check is the whole story, so this is just status(). Kept as its own
    function purely for interface parity with roon_client.retry_pairing,
    so main.py's dispatch never needs a provider-specific branch."""
    return status()


def test_connection(url: str, token: str) -> dict:
    """#509 item 3: same check as status(), against an EXPLICIT token
    rather than the stored config — never persists anything. See
    subsonic_client.test_connection's own docstring for the full
    rationale (the admin config form's live pre-save check)."""
    resp = _get("/", token, url)
    if resp is not None and resp.get("MediaContainer") is not None:
        return {"state": "paired", "url": url, "provider": "plex"}
    return {"state": "disconnected", "url": url, "provider": "plex"}


def reconnect(url: str, token: str) -> dict:
    """Admin (re)configured the Plex connection from the web UI — persist
    it and report whether the result checks out. No username to resolve
    (unlike jellyfin_client.reconnect): the token is already scoped to one
    Plex account."""
    conn = db.get_conn()
    try:
        db.set_config(conn, "plex_url", url)
        db.set_config(conn, "plex_token", token)
        conn.commit()
    finally:
        conn.close()
    return status()


def list_playlists() -> dict:
    """Returns {"status": "ok", "playlists": [{"id", "title"}, ...]} — the
    common shape every provider client returns (#75). `playlistType=audio`
    filters out Plex's video/photo playlists at the source. Plex exposes a
    real, stable `ratingKey`, so `id` is set (stringified) and drives the
    sync's composite key — two same-named Plex playlists coexist as
    separate rows instead of collapsing."""
    url, token = _current_config()
    resp = _get("/playlists", token, url, {"playlistType": _MUSIC_PLAYLIST_TYPE})
    if resp is None:
        return {"status": "error", "reason": "not_paired"}
    items = resp.get("MediaContainer", {}).get("Metadata") or []
    return {"status": "ok", "playlists": [
        {"id": str(i["ratingKey"]), "title": i["title"]}
        for i in items if i.get("title") and i.get("ratingKey") is not None
    ]}


def _first_file(entry: dict) -> str | None:
    """A track item's on-disk path lives three levels deep:
    Media[0].Part[0].file — a multi-version track (rare for typical
    libraries) would have more than one Media entry, but the first is
    always the one Plex itself would play."""
    media = entry.get("Media") or []
    if not media:
        return None
    parts = media[0].get("Part") or []
    if not parts:
        return None
    return parts[0].get("file")


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist", "path", "album"} — path/album are what let matching.py skip
    straight to an exact-path match instead of its Roon-Browse-API-specific
    fuzzy fallback.

    Fetched by `source_playlist_id` directly (the ratingKey from
    list_playlists) — fetching by title would be ambiguous now that
    same-named playlists can coexist. Falls back to a title match only if
    no id is given (the sync always passes it)."""
    url, token = _current_config()
    if source_playlist_id is not None:
        rating_key: str | None = source_playlist_id
    else:
        listed = list_playlists()
        if listed["status"] != "ok":
            return {"status": "error", "reason": "not_paired"}
        match = next((p for p in listed["playlists"] if p["title"] == playlist_title), None)
        if match is None:
            return {"status": "not_found", "failed_segment": playlist_title}
        rating_key = match["id"]

    resp = _get(f"/playlists/{rating_key}/items", token, url)
    if resp is None:
        return {"status": "error", "reason": "playlist items fetch failed"}
    entries = resp.get("MediaContainer", {}).get("Metadata") or []

    tracks = [
        {
            "position": i,
            "title": e.get("title", ""),
            "artist": e.get("grandparentTitle", ""),
            "path": _first_file(e),
            "album": e.get("parentTitle"),
        }
        for i, e in enumerate(entries)
    ]
    return {"status": "ok", "playlist": playlist_title, "tracks": tracks}


def get_artist_image(artist_name: str) -> tuple[bytes, str] | None:
    """Always None — #158 explicitly scopes Plex artist images (via each
    artist's `thumb` field, under a music-library section walk mirroring
    jellyfin_client's _build_artist_image_key_map) as an optional
    follow-up, not part of this provider's initial acceptance criteria.
    A miss here just means no picture (same contract as every other
    provider's get_artist_image) — artist_images.py still falls back to
    the filesystem provider, so this never blocks a sync."""
    return None
