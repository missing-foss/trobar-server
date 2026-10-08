#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public Spotify playlists, fetched by URL with no account.

The Spotify counterpart of ytmusic_client.py, with the same contract and
for the same reason: a user pastes the link of a public playlist, and its
tracks are matched against the local library. No credential, no linked
account, nothing to configure. A subscription row is the whole setup.

WHY NOT THE WEB API: Spotify's Get Playlist returns a playlist's items only
to the user who owns or collaborates on it. A stranger's public playlist
comes back as metadata with no tracks, whatever token asks. That is what
spotify_client.py's linked accounts can and cannot do, and it is not
something a token can work around.

WHAT THIS DEPENDS ON, SAID PLAINLY: the embed player's page,
`open.spotify.com/embed/playlist/<id>`, the one sites put in an iframe.
It is a web page, not an API. Its `__NEXT_DATA__` script carries the
playlist's name and a `trackList` of title, artist line and duration. As
measured:

- it answers without a cookie or a token, for editorial and user-made
  playlists alike;
- **it lists at most 100 tracks.** A longer playlist comes back as its
  first 100, and the page carries no total, so a playlist of exactly 100
  cannot be told apart from a longer one. LIMIT says so to the page and
  the docs instead of letting the missing tail look like failed matches;
- **there is no album**, so matching is on artist and title alone;
- **the HTTP status is 200 even when there is no playlist.** A missing
  playlist is a `status` of 404 inside the page data; a malformed id is a
  500 there.

Its shape can change without notice, so, as for YouTube Music, every way
the fetch can fail is expected: a page that no longer parses is an error on
the subscription, never an empty playlist.

The module is imported eagerly but contacts nothing until a subscription
exists: playlist_sync only calls it for a subscription row.
"""

import json
import logging
import re
from urllib.parse import urlparse

import requests

_log = logging.getLogger(__name__)

PROVIDER_ID = "spotify_public"

# The most tracks the embed page lists.
LIMIT = 100

_EMBED_URL = "https://open.spotify.com/embed/playlist/{id}"
_TIMEOUT = 20
_HEADERS = {"User-Agent": "Trobar (public playlist import)"}

# Spotify ids are base62, 22 characters. Unlike YouTube's, the format has
# not changed, and checking it here means a malformed id is refused before
# any request rather than coming back as the page's 500.
_ID = re.compile(r"[0-9A-Za-z]{22}")

_HOSTS = {"open.spotify.com"}

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def parse_playlist_url(raw: str) -> str | None:
    """The playlist id in a pasted Spotify link, or None.

    Accepts the Share link (`open.spotify.com/playlist/<id>?si=…`), the same
    with a locale segment (`/intl-fr/playlist/<id>`), an embed link, and a
    `spotify:playlist:<id>` URI. Not a bare id: a bare id is YouTube Music's
    to parse, and the two can't be told apart by their shape alone.

    Returns None for anything else, including a `spotify.link` short link,
    which only says where it leads by redirecting. The page's error says to
    open it and copy the full link."""
    raw = (raw or "").strip()
    if raw.startswith("spotify:playlist:"):
        return _clean_id(raw[len("spotify:playlist:"):])
    if "/" not in raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.hostname is None or parsed.hostname.lower() not in _HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0].startswith("intl-"):
        parts = parts[1:]
    if parts and parts[0] == "embed":
        parts = parts[1:]
    if len(parts) != 2 or parts[0] != "playlist":
        return None
    return _clean_id(parts[1])


def _clean_id(value: str) -> str | None:
    value = value.strip()
    return value if _ID.fullmatch(value) else None


def _entry(index: int, item: dict) -> dict:
    """One track in the shape playlist_sync/identity.py consume.

    The page joins several artists with a comma and a no-break space
    (`Artist A,\\u00a0Artist B`). The no-break space becomes a plain one,
    giving the ", " join the other sources hand over. A plain comma is
    left as it is: it belongs to a name, as in "Tyler, The Creator"."""
    return {
        "position": index,
        "title": item.get("title"),
        "artist": str(item.get("subtitle") or "").replace("\u00a0", " "),
        "album": None,
        "path": None,
    }


def fetch_playlist(playlist_id: str) -> dict:
    """One public playlist by id.

    Returns `{"status": "ok", "title": str, "tracks": [...]}`, or
    `{"status": "unavailable"}` when the page says there is no such
    playlist (deleted, made private, or never existed), or
    `{"status": "error", "reason": str}` for anything else, which includes
    the page changing shape. Callers treat the last two alike; they are
    kept apart only so the page can say which happened."""
    try:
        resp = requests.get(_EMBED_URL.format(id=playlist_id), headers=_HEADERS,
                            timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        _log.info("[spotify_public] playlist %s unreachable (%s)", playlist_id, type(e).__name__)
        return {"status": "error", "reason": type(e).__name__}
    if resp.status_code != 200:
        _log.info("[spotify_public] playlist %s: HTTP %s", playlist_id, resp.status_code)
        return {"status": "error", "reason": f"http_{resp.status_code}"}

    match = _NEXT_DATA.search(resp.text)
    if match is None:
        return {"status": "error", "reason": "unexpected_response"}
    try:
        props = json.loads(match.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        return {"status": "error", "reason": "unexpected_response"}
    if not isinstance(props, dict):
        return {"status": "error", "reason": "unexpected_response"}
    if props.get("status") == 404:
        _log.info("[spotify_public] playlist %s unavailable", playlist_id)
        return {"status": "unavailable"}
    try:
        entity = props["state"]["data"]["entity"]
        items = entity["trackList"]
    except (KeyError, TypeError):
        return {"status": "error", "reason": "unexpected_response"}
    if not isinstance(items, list):
        return {"status": "error", "reason": "unexpected_response"}

    tracks: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = _entry(len(tracks), item)
        if not entry["title"]:
            continue  # nothing for the matcher to work with
        tracks.append(entry)
    return {"status": "ok", "title": entity.get("name") or playlist_id, "tracks": tracks}


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """The provider-shaped call playlist_sync._sync_one_playlist() makes;
    the same contract as ytmusic_client.get_playlist_tracks()."""
    if not source_playlist_id:
        return {"status": "not_found", "failed_segment": playlist_title}
    result = fetch_playlist(source_playlist_id)
    if result["status"] != "ok":
        return result
    return {"status": "ok", "playlist": result["title"] or playlist_title,
            "tracks": result["tracks"]}
