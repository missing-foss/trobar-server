#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Last.fm: suggestions read through the public API (an api_key and the user's
Last.fm username), and the signed calls that link an account and send plays
(api_signature, auth_url, get_session; play_forward sends)."""

import hashlib
import logging
import os
import random
from urllib.parse import urlencode

import requests

import history_cache
import suggestions as suggestions_mod

_log = logging.getLogger(__name__)

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
# Base URL for the Last.fm API. Deployment-time default (env var, e.g. a dev
# environment pointing at the local mock — see dev/lastfm-mock); admins can
# also override it live from Administration (app_config key
# "lastfm_api_base") to point at a self-hosted alternative such as Libre.fm
# — that override is threaded through as the api_base param on every
# function below rather than read here, since it can change per-request.
# Defaults to the real endpoint, over HTTPS: every call carries the API key
# and most carry the username.
API_BASE = os.environ.get("LASTFM_API_BASE", "https://ws.audioscrobbler.com/2.0/")

# Last.fm returns this same image (a plain gray "no cover" square) for every
# album it has no real art for, keyed by this fixed hash regardless of size —
# well-documented Last.fm API quirk. Treat it as "no image" rather than
# showing the placeholder as if it were real art.
_LASTFM_PLACEHOLDER_HASH = "2a96cbd8b46e442fc41c2b86b821562f"


def _log_failure(method: str, error: Exception, key: str) -> None:
    """Log a failed call without the API key. A requests error's text holds
    the request URL, and the key is in its query string."""
    _log.warning("Last.fm %s failed: %s", method, str(error).replace(key, "[key]"))


def _album_image_url(image_list: list[dict]) -> str | None:
    by_size = {img.get("size"): img.get("#text") for img in image_list or []}
    url = by_size.get("extralarge") or by_size.get("large") or by_size.get("medium") or by_size.get("small")
    if not url or _LASTFM_PLACEHOLDER_HASH in url:
        return None
    return url


def check_connection(username: str, api_key: str = "", api_base: str = "") -> bool:
    """Lightweight reachability/validity check for the header status dot —
    True iff Last.fm actually answered for this username+key, using the
    cheapest call the API offers (no track/album list). top_albums() and the
    functions above it also tell a failure (None) from an empty answer ([]),
    but they fetch whole lists; this is the check the status dot can afford
    to run on every page load."""
    key = api_key or LASTFM_API_KEY
    if not key or not username:
        return False
    try:
        resp = requests.get(
            api_base or API_BASE,
            params={"method": "user.getInfo", "user": username, "api_key": key, "format": "json"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        return "user" in data and "error" not in data
    except Exception:
        return False


@history_cache.cached("lastfm.top_albums")
def top_albums(username: str, api_key: str = "", period: str = "6month", limit: int = 50,
                api_base: str = "") -> list[dict] | None:
    """Raw Last.fm top-albums for a username. [] when there are none or the
    config is missing; None when Last.fm didn't answer (any failure), so a
    caller can tell an outage from "nothing played". `api_key` is the per-user key set in Profil; falls back to the
    LASTFM_API_KEY env var if the user hasn't set one (so a single
    app-wide key, if provisioned, still works without every user having to
    paste it themselves — Last.fm keys are tied to the registered
    application, not the listener, so sharing one is normal)."""
    key = api_key or LASTFM_API_KEY
    if not key or not username:
        return []
    try:
        params: dict[str, str | int] = {
            "method": "user.getTopAlbums", "user": username,
            "api_key": key, "format": "json",
            "period": period, "limit": limit,
        }
        resp = requests.get(api_base or API_BASE, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("topalbums", {}).get("album", [])
    except Exception as e:
        _log_failure("user.getTopAlbums", e, key)
        return None


def similar_artists(artist: str, api_key: str = "", limit: int = 30, api_base: str = "") -> list[str]:
    """Last.fm `artist.getSimilar` — related-artist names, most-similar first.
    Global data (no username needed), so any valid key works; falls back to the
    app-wide LASTFM_API_KEY like top_albums(). [] on any failure/missing config.
    Returns more than the caller ultimately shows so it can be intersected with
    the local library first and still have enough to display."""
    key = api_key or LASTFM_API_KEY
    if not key or not artist:
        return []
    try:
        params: dict[str, str | int] = {
            "method": "artist.getSimilar", "artist": artist,
            "api_key": key, "format": "json", "limit": limit,
            "autocorrect": 1,
        }
        resp = requests.get(api_base or API_BASE, params=params, timeout=10)
        resp.raise_for_status()
        similar = resp.json().get("similarartists", {}).get("artist", [])
        return [a["name"] for a in similar if a.get("name")]
    except Exception as e:
        _log_failure("artist.getSimilar", e, key)
        return []


def suggestions(conn, username: str, api_key: str = "", period: str = "6month", limit: int = 50,
                 user_device_ids: set[int] | None = None, api_base: str = "") -> list[dict] | None:
    """Top albums already in the local catalog (under MUSIC_ROOT) but not yet
    synced to every device the caller manages — "you listen to this a lot,
    sync it" suggestions. Albums Last.fm reports that aren't in the library
    at all are dropped entirely (this is a sync prioritization tool, not a
    music-discovery one — nothing to act on for something you don't have
    ripped). a suggestion is only suppressed once it's synced to
    *every* device `user_device_ids` covers — partially-synced (e.g. only on
    one of two devices) still surfaces, since there's still something to do."""
    albums = top_albums(username, api_key, period, limit, api_base=api_base)
    if albums is None:
        return None  # Last.fm didn't answer
    if not albums:
        return []

    library = suggestions_mod.local_library_index(conn)
    covered = suggestions_mod.covered_devices(conn, library)

    out = []
    for a in albums:
        artist = a.get("artist", {}).get("name", "")
        album = a.get("name", "")
        key = (artist.lower(), album.lower())
        local = library.get(key)
        if local is None:
            continue  # not on the NFS library — nothing to sync, don't suggest it
        if suggestions_mod.is_fully_synced(covered, key, user_device_ids):
            continue
        out.append({
            "artist": artist,
            "album": album,
            "playcount": int(a.get("playcount", 0)),
            "library_artist": local[0],
            "library_album": local[1],
            "image_url": _album_image_url(a.get("image", [])),
            "source": "lastfm",
        })
    # Shuffled rather than left in Last.fm's playcount-descending order — the
    # qualifying pool (up to `limit` candidates, filtered down to what's
    # locally available and not fully synced) is usually bigger than what
    # any single view actually displays, so a fixed order means the same
    # handful of albums forever. Order doesn't carry meaning worth keeping
    # once "top played" has already selected the candidate pool.
    random.shuffle(out)
    return out


def most_played(username: str, api_key: str = "", period: str = "6month", limit: int = 10,
                 api_base: str = "") -> list[dict] | None:
    """#267: ranked top albums by playcount — the user's actual Last.fm
    listening, unlike suggestions() above which intersects with the local
    catalog (dropping anything not ripped) and shuffles the result. No
    library filter here on purpose: a most-played chart is about what you
    listen to, not what you have. Already in Last.fm's own
    playcount-descending order, so no re-sort needed."""
    albums = top_albums(username, api_key, period, limit, api_base=api_base)
    if albums is None:
        return None  # Last.fm didn't answer
    out = []
    for a in albums:
        artist = a.get("artist", {}).get("name", "")
        album = a.get("name", "")
        if not artist or not album:
            continue
        out.append({
            "artist": artist,
            "album": album,
            "playcount": int(a.get("playcount", 0)),
            "image_url": _album_image_url(a.get("image", [])),
        })
    return out


@history_cache.cached("lastfm.recent_tracks")
def recent_tracks(username: str, api_key: str = "", limit: int = 50,
                  api_base: str = "") -> list[dict] | None:
    """Raw Last.fm recent-tracks for a username. Same contract as
    top_albums(): [] for none or missing config, None on any failure."""
    key = api_key or LASTFM_API_KEY
    if not key or not username:
        return []
    try:
        params: dict[str, str | int] = {
            "method": "user.getRecentTracks", "user": username,
            "api_key": key, "format": "json", "limit": limit,
        }
        resp = requests.get(api_base or API_BASE, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("recenttracks", {}).get("track", [])
    except Exception as e:
        _log_failure("user.getRecentTracks", e, key)
        return None


def recently_played_suggestions(conn, username: str, api_key: str = "", limit: int = 50,
                                 user_device_ids: set[int] | None = None,
                                 api_base: str = "") -> list[dict] | None:
    """Distinct albums from the user's most recent scrobbles — 's
    "recently played" half. Roon itself doesn't expose play history via
    Browse at all (verified live — see discussion), so this reuses
    the Last.fm connection the app already has instead. Same locally-
    available + not-fully-synced-everywhere filter as suggestions() above."""
    tracks = recent_tracks(username, api_key, limit, api_base=api_base)
    if tracks is None:
        return None  # Last.fm didn't answer
    if not tracks:
        return []

    library = suggestions_mod.local_library_index(conn)
    covered = suggestions_mod.covered_devices(conn, library)

    seen: set[tuple[str, str]] = set()
    out = []
    for t in tracks:
        if not isinstance(t, dict) or t.get("@attr", {}).get("nowplaying"):
            continue  # the currently-playing entry has no timestamp yet
        artist = t.get("artist", {}).get("#text", "")
        album = t.get("album", {}).get("#text", "")
        if not artist or not album:
            continue
        key = (artist.lower(), album.lower())
        if key in seen:
            continue
        seen.add(key)
        local = library.get(key)
        if local is None:
            continue
        if suggestions_mod.is_fully_synced(covered, key, user_device_ids):
            continue
        out.append({
            "artist": local[0],
            "album": local[1],
            "library_artist": local[0],
            "library_album": local[1],
            "image_url": _album_image_url(t.get("image", [])),
            "source": "lastfm-recent",
        })
    return out


def api_signature(params: dict, secret: str) -> str:
    """Last.fm's request signature: every parameter but `format`, sorted by
    name in ASCII order, joined as namevalue, then the shared secret, as MD5.
    The API requires MD5; the secret itself is never sent."""
    joined = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "format")
    return hashlib.md5((joined + secret).encode("utf-8")).hexdigest()  # noqa: S324


def auth_url(api_key: str, callback: str) -> str:
    """Where to send someone to grant this install access to their account."""
    return "https://www.last.fm/api/auth/?" + urlencode({"api_key": api_key, "cb": callback})


def get_session(api_key: str, secret: str, token: str, api_base: str = "") -> dict | None:
    """The session for a token from the auth redirect: {"name", "key"}, or None
    if Last.fm didn't give one."""
    params = {"method": "auth.getSession", "api_key": api_key, "token": token}
    try:
        resp = requests.post(api_base or API_BASE, timeout=15,
                             data={**params, "api_sig": api_signature(params, secret), "format": "json"})
        session = resp.json().get("session") or {}
    except (requests.RequestException, ValueError) as e:
        _log.warning("Last.fm session request failed: %s", type(e).__name__)
        return None
    if not session.get("key") or not session.get("name"):
        return None
    return {"name": session["name"], "key": session["key"]}
