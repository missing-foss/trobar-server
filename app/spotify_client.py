#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct (non-Roon) Spotify playlist provider (#10 Part B) — read-only, per
Trobar user's own linked Spotify account.

Same shape as tidal_client.py: NOT an admin-configured shared connection (like
roon/subsonic/jellyfin), but a per-user OAuth link — each household member
links their own Spotify account, and every read is scoped by that user's own
access token. The redirect/session half of the OAuth dance lives in main.py via
Authlib (it needs the Flask session for state); this module only does the token
refresh + authenticated API calls, plain `requests`, matching the other
provider clients.

Refresh and "use" are deliberately separate: Spotify's refresh grant MAY rotate
the refresh token, so callers refresh once per sync pass and reuse the access
token for that pass's list_playlists() + get_playlist_tracks() calls, then
persist the (possibly new) refresh token immediately — refreshing per call could
invalidate a token a previous call just rotated past.

Endpoints (Spotify Web API v1, https://developer.spotify.com/documentation/web-api):
  * POST https://accounts.spotify.com/api/token   — refresh
  * GET  /me                                       — link-time identity
  * GET  /me/playlists                             — the user's playlists
  * GET  /playlists/{id}/tracks                    — a playlist's tracks
All list endpoints paginate by a `next` field carrying a full follow-up URL
(offset/limit based), walked to the end here the same way tidal_client walks
its cursor. Rate limits are 429 + Retry-After (seconds), handled by _auth_get's
retry/backoff (mirrors #127).
"""

import logging
import time

import requests

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
# Read-only playlist scopes. (There is no "playlist-read-public" scope — public
# playlists are readable without one — so despite #10 listing three, only these
# two are real.) Space-separated per OAuth2 convention.
SCOPES = "playlist-read-private playlist-read-collaborative"

_TIMEOUT = 15
# Spotify caps /me/playlists at 50/page and /playlists/{id}/tracks at 100/page;
# both paginate by a `next` URL. Walk to the end, with a runaway guard.
_MAX_PAGES = 500
# #127-style resilience: retry a rate-limited (429) / 5xx / blipped call with
# backoff rather than failing the whole playlist on one bad page.
_RETRY_ATTEMPTS = 4          # 1 initial try + 3 retries
_RETRY_BASE_DELAY = 1.0      # seconds; exponential per attempt, capped
_RETRY_MAX_DELAY = 30.0

_log = logging.getLogger(__name__)


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before retrying (attempt is 1-based). Honors a numeric
    Retry-After header when present, else exponential backoff — both capped."""
    if retry_after:
        try:
            return min(float(retry_after), _RETRY_MAX_DELAY)
        except ValueError:
            pass
    return min(_RETRY_BASE_DELAY * (2 ** (attempt - 1)), _RETRY_MAX_DELAY)


def _auth_get(access_token: str, url: str) -> dict:
    """Authenticated GET returning parsed JSON, raising on HTTP error. `url` may
    be absolute or server-relative — Spotify's pagination `next` is a full URL,
    so it can be fed straight back in.

    Retries on 429, 5xx, and connection errors with backoff (honoring
    Retry-After) up to _RETRY_ATTEMPTS; other 4xx raise immediately (no point
    retrying a 400/404). The final attempt's failure propagates to the caller."""
    if url.startswith("/"):
        url = API_BASE + url
    headers = {"Authorization": f"Bearer {access_token}"}
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        last = attempt == _RETRY_ATTEMPTS
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException:
            if last:
                raise
            time.sleep(_retry_delay(attempt, None))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if last:
                resp.raise_for_status()  # give up — raises HTTPError
            _log.info("Spotify %s -> %s, retry %d/%d",
                      url, resp.status_code, attempt, _RETRY_ATTEMPTS)
            time.sleep(_retry_delay(attempt, resp.headers.get("Retry-After")))
            continue
        resp.raise_for_status()  # other 4xx: raise, don't retry
        return resp.json()
    raise RuntimeError("unreachable")  # pragma: no cover


def _error_reason(exc: Exception) -> str:
    """A distinguishable reason for the {"status": "error"} returns, so a
    caller/UI can tell a rate-limit from a not-found from a parse error."""
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return "rate_limited" if status == 429 else "http_error"
    if isinstance(exc, requests.RequestException):
        return "network"
    return "parse_error"


class SpotifyAuthError(Exception):
    """Refresh token rejected (revoked at Spotify's end, expired, or the admin's
    client_id/client_secret changed) — caller's cue to clear the stored
    refresh_token and show the user "reconnect" rather than retry. Distinct from
    SpotifyTransientError: this one means the credential itself is dead, that one
    means "ask again later"."""


class SpotifyTransientError(Exception):
    """Network failure, timeout, a 5xx, or a malformed body while refreshing —
    nothing wrong with the stored refresh_token itself. Caller's cue to retry
    next sync."""


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    """Returns (access_token, refresh_token) — the second may be a new value
    (Spotify's refresh grant can rotate it) or the same one handed in; callers
    persist the returned value. Raises SpotifyAuthError on a rejected token,
    SpotifyTransientError on anything else that went wrong making the call."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret),
            timeout=_TIMEOUT,
        )
        if resp.status_code in (400, 401):
            raise SpotifyAuthError(f"Spotify refresh token rejected ({resp.status_code})")
        resp.raise_for_status()
        body = resp.json()
        return body["access_token"], body.get("refresh_token", refresh_token)
    except (requests.RequestException, KeyError, ValueError) as e:
        raise SpotifyTransientError(str(e)) from e


def get_current_user(access_token: str) -> dict:
    """{"status": "ok", "user_id": ..., "display_name": ...} or
    {"status": "error"}. Called once at link time to populate the
    "Connected as: X" label. display_name can be null on Spotify — fall back to
    the stable id."""
    try:
        resp = requests.get(
            f"{API_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        display_name = data.get("display_name") or data["id"]
        return {"status": "ok", "user_id": data["id"], "display_name": display_name}
    except (requests.RequestException, KeyError, ValueError):
        return {"status": "error"}


def list_playlists(access_token: str) -> dict:
    """{"status": "ok", "playlists": [{"id", "title"}, ...]} or an error dict —
    the common shape every provider client returns (#75). Each row's `id` is
    Spotify's stable playlist id (drives the composite key; two same-named
    playlists coexist). Uses /me/playlists — the authed user's own + followed
    playlists — walked to the end via the `next` URL."""
    try:
        playlists = []
        seen_ids: set[str] = set()
        url = "/me/playlists?limit=50"
        for _ in range(_MAX_PAGES):
            body = _auth_get(access_token, url)
            for item in body.get("items", []):
                pid, name = item.get("id"), item.get("name")
                if not pid or not name or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                playlists.append({"id": pid, "title": name})
            nxt = body.get("next")
            if not nxt:
                break
            url = nxt
        else:
            _log.warning("Spotify playlists: hit _MAX_PAGES=%d, list may be truncated",
                         _MAX_PAGES)
        return {"status": "ok", "playlists": playlists}
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}


def get_playlist_tracks(title: str, source_playlist_id: str | None = None,
                        access_token: str = "", **_kwargs) -> dict:
    """{"status": "ok", "tracks": [{"position", "artist", "title", "album"}, ...]}
    or an error dict. No `path` (Spotify has no local file path) — matching.py
    falls back to its fuzzy artist/title heuristic, same as Roon's/Tidal's
    tracks. Fetched by `source_playlist_id` directly, walked to the end via the
    `next` URL. `title` is accepted for call-shape uniformity.

    Each item's `track` can be null (a removed/unavailable track) or a podcast
    episode — both are skipped. artist is the first credited artist's name."""
    if not source_playlist_id:
        return {"status": "error", "reason": "not_found"}
    try:
        tracks = []
        position = 0
        # /playlists/{id}/items, NOT /tracks — Spotify removed the /tracks alias
        # in Feb 2026. Same paging-object shape (items[].track). Live shape
        # confirmation is pending a Premium account (#146).
        url = f"/playlists/{source_playlist_id}/items?limit=100"
        for _ in range(_MAX_PAGES):
            body = _auth_get(access_token, url)
            for item in body.get("items", []):
                track = item.get("track") or {}
                if not track or track.get("type") == "episode":
                    continue
                artists = track.get("artists") or []
                artist = (artists[0].get("name", "")
                          if artists and isinstance(artists[0], dict) else "")
                track_title = track.get("name", "")
                if not artist and not track_title:
                    continue
                album = track.get("album") or {}
                tracks.append({
                    "position": position,
                    "artist": artist,
                    "title": track_title,
                    "album": album.get("name") if isinstance(album, dict) else None,
                })
                position += 1
            nxt = body.get("next")
            if not nxt:
                break
            url = nxt
        else:
            _log.warning("Spotify playlist %s: hit _MAX_PAGES=%d, tracks may be truncated",
                         source_playlist_id, _MAX_PAGES)
        return {"status": "ok", "tracks": tracks}
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}
