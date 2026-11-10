# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public Tidal playlists, fetched by link with no Tidal account.

The Tidal counterpart of spotify_public_client.py, with the same contract: a
user pastes the link of a public playlist, and its tracks are matched against
the local library. A subscription row is the whole setup.

Unlike the Spotify embed page, this is Tidal's documented API. It reads with
the server's own Tidal app credentials (the client id and secret the admin set
for Tidal accounts), through the client-credentials grant: no user login. As
measured against the live API with production's credentials:

- a client-credentials token reads a public user playlist and its items;
- every included track carries an ISRC, so these imports reach matching's
  ISRC tier;
- items come 20 to a page whatever page[size] asks, following links.next;
- about seven quick calls in, Tidal answers 429 with Retry-After: 4 and no
  rate-limit headers beforehand. tidal_client._auth_get waits that out and
  retries, as it does for Tidal accounts; if its retries run out, the fetch
  is an error, never a shortened playlist.

The track walk is tidal_client.get_playlist_tracks, the one Tidal accounts
use, given this token instead of a user's.

The module contacts nothing until a subscription exists: playlist_sync only
calls it for a subscription row."""

import logging
import re
import threading
import time
from urllib.parse import urlparse

import requests

import db
import tidal_client

_log = logging.getLogger(__name__)

PROVIDER_ID = "tidal_public"

_TIMEOUT = 15
# A token is renewed this long before Tidal says it expires.
_EXPIRY_MARGIN = 300

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HOSTS = {"tidal.com", "www.tidal.com", "listen.tidal.com"}

_token_lock = threading.Lock()
_token: dict = {"value": None, "expires_at": 0.0, "for": None}


def parse_playlist_url(raw: str) -> str | None:
    """The playlist id in a pasted Tidal link, or None.

    Accepts `tidal.com/playlist/<id>`, `tidal.com/browse/playlist/<id>` and
    `listen.tidal.com/playlist/<id>`, with or without a scheme and query.
    The id is a UUID. Anything else is None, a bare id included: the other
    link kinds' parsers come after this one."""
    raw = (raw or "").strip()
    if "/" not in raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.hostname is None or parsed.hostname.lower() not in _HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0] == "browse":
        parts = parts[1:]
    if len(parts) != 2 or parts[0] != "playlist" or not _UUID.fullmatch(parts[1]):
        return None
    return parts[1].lower()


def _credentials() -> tuple[str, str] | None:
    conn = db.get_conn()
    try:
        client_id = db.get_config(conn, "tidal_client_id")
        client_secret = db.get_config(conn, "tidal_client_secret")
    finally:
        conn.close()
    return (client_id, client_secret) if client_id and client_secret else None


def is_configured() -> bool:
    """Whether the server has the Tidal app credentials these links need."""
    return _credentials() is not None


def _access_token() -> str | None:
    """A client-credentials token, from the cache while it has more than
    _EXPIRY_MARGIN left and the credentials haven't changed; None when the
    credentials aren't set or Tidal refuses them. Raises on a network
    error, like the reads it serves."""
    credentials = _credentials()
    if credentials is None:
        return None
    with _token_lock:
        if (_token["value"] and _token["for"] == credentials
                and time.monotonic() < _token["expires_at"] - _EXPIRY_MARGIN):
            return _token["value"]
        resp = requests.post(tidal_client.TOKEN_URL, data={"grant_type": "client_credentials"},
                             auth=credentials, timeout=_TIMEOUT)
        if resp.status_code != 200:
            _log.warning("[tidal_public] token refused: HTTP %s", resp.status_code)
            return None
        body = resp.json()
        _token["value"] = body["access_token"]
        _token["expires_at"] = time.monotonic() + float(body.get("expires_in") or 0)
        _token["for"] = credentials
        return _token["value"]


def fetch_playlist(playlist_id: str) -> dict:
    """One public playlist by id.

    Returns `{"status": "ok", "title": str, "tracks": [...]}`, or
    `{"status": "unavailable"}` when Tidal has no such public playlist
    (deleted, private, or never existed), or `{"status": "error", "reason":
    str}` for anything else: `not_configured` when the server has no Tidal
    app credentials, `token_refused` when Tidal rejects them, or the read's
    own failure, a rate limit that outlasted the retries included."""
    try:
        token = _access_token()
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"status": "error", "reason": tidal_client._error_reason(e)}
    if token is None:
        return {"status": "error", "reason": "not_configured" if not is_configured() else "token_refused"}
    try:
        body = tidal_client._auth_get(token, f"/playlists/{playlist_id}")
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (403, 404):
            _log.info("[tidal_public] playlist %s unavailable (HTTP %s)", playlist_id, status)
            return {"status": "unavailable"}
        return {"status": "error", "reason": tidal_client._error_reason(e)}
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"status": "error", "reason": tidal_client._error_reason(e)}
    attributes = (body.get("data") or {}).get("attributes") or {}
    if attributes.get("accessType") == "PRIVATE":
        _log.info("[tidal_public] playlist %s is private", playlist_id)
        return {"status": "unavailable"}
    result = tidal_client.get_playlist_tracks("", playlist_id, access_token=token)
    if result["status"] != "ok":
        return result
    return {"status": "ok", "title": attributes.get("name") or playlist_id, "tracks": result["tracks"]}


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """The provider-shaped call playlist_sync._sync_one_playlist() makes;
    the same contract as spotify_public_client.get_playlist_tracks()."""
    if not source_playlist_id:
        return {"status": "not_found", "failed_segment": playlist_title}
    result = fetch_playlist(source_playlist_id)
    if result["status"] != "ok":
        return result
    return {"status": "ok", "playlist": result["title"] or playlist_title,
            "tracks": result["tracks"]}
