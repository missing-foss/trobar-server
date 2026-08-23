#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ListenBrainz suggestions — same contracts as lastfm.py
(suggestions() / recently_played_suggestions() feeding /api/suggestions),
backed by the ListenBrainz public API instead. Notable differences from
Last.fm: reads need no API key at all (just the username), and album art
comes from the Cover Art Archive via the MBIDs the stats endpoint already
returns — no separate art lookup."""

import os
import random

import requests

import suggestions as suggestions_mod

# Deployment-time default; overridable live via Administration
# (app_config key "listenbrainz_api_base") for a self-hosted ListenBrainz
# instance — same reasoning as lastfm.API_BASE.
API_BASE = os.environ.get("LISTENBRAINZ_API_BASE", "https://api.listenbrainz.org")

_HEADERS = {"User-Agent": "Trobar/1.0 (+https://github.com/missing-foss)"}


def _caa_image_url(item: dict) -> str | None:
    """Cover Art Archive URL from the caa_* fields ListenBrainz stats
    responses carry inline (verified live: release-group stats items include
    caa_id + caa_release_mbid when art exists)."""
    mbid = item.get("caa_release_mbid")
    caa_id = item.get("caa_id")
    if not mbid or not caa_id:
        return None
    return f"https://coverartarchive.org/release/{mbid}/{caa_id}-250.jpg"


def check_connection(username: str, api_base: str = "") -> bool:
    """Header status-dot check — the cheapest authenticated-truth the API
    offers for "does this username exist and answer": one listen. A valid
    but silent account still returns 200 with an empty list."""
    if not username:
        return False
    try:
        resp = requests.get(
            f"{api_base or API_BASE}/1/user/{username}/listens",
            params={"count": 1}, headers=_HEADERS, timeout=8,
        )
        return resp.status_code == 200
    except Exception:
        return False


def top_release_groups(username: str, range_: str = "half_yearly", limit: int = 50,
                        api_base: str = "") -> list[dict]:
    """Raw ListenBrainz top release-groups (albums) for a username. [] on any
    failure or missing config, same contract as lastfm.top_albums().
    `half_yearly` mirrors the Last.fm default period of 6month."""
    if not username:
        return []
    try:
        params: dict[str, str | int] = {"range": range_, "count": limit}
        resp = requests.get(
            f"{api_base or API_BASE}/1/stats/user/{username}/release-groups",
            params=params, headers=_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("payload", {}).get("release_groups", [])
    except Exception as e:
        print(f"[listenbrainz] error: {e}")
        return []


def suggestions(conn, username: str, range_: str = "half_yearly", limit: int = 50,
                user_device_ids: set[int] | None = None, api_base: str = "") -> list[dict]:
    """Top-played albums already in the local catalog but not yet synced to
    every device the caller manages — the ListenBrainz counterpart of
    lastfm.suggestions(), same library-match + coverage filter + shuffle."""
    albums = top_release_groups(username, range_, limit, api_base=api_base)
    if not albums:
        return []

    library = suggestions_mod.local_library_index(conn)
    covered = suggestions_mod.covered_devices(conn, library)

    out = []
    for a in albums:
        artist = a.get("artist_name", "")
        album = a.get("release_group_name", "")
        key = (artist.lower(), album.lower())
        local = library.get(key)
        if local is None:
            continue  # not in the local library — nothing to sync
        if suggestions_mod.is_fully_synced(covered, key, user_device_ids):
            continue
        out.append({
            "artist": artist,
            "album": album,
            "playcount": int(a.get("listen_count", 0)),
            "library_artist": local[0],
            "library_album": local[1],
            "image_url": _caa_image_url(a),
            "source": "listenbrainz",
        })
    random.shuffle(out)  # same reasoning as lastfm.suggestions()
    return out


def most_played(username: str, range_: str = "half_yearly", limit: int = 10,
                 api_base: str = "") -> list[dict]:
    """ListenBrainz counterpart of lastfm.most_played() — same contract:
    ranked by listen count, no local-library filter, no shuffle."""
    albums = top_release_groups(username, range_, limit, api_base=api_base)
    out = []
    for a in albums:
        artist = a.get("artist_name", "")
        album = a.get("release_group_name", "")
        if not artist or not album:
            continue
        out.append({
            "artist": artist,
            "album": album,
            "playcount": int(a.get("listen_count", 0)),
            "image_url": _caa_image_url(a),
        })
    return out


def recent_listens(username: str, limit: int = 50, api_base: str = "") -> list[dict]:
    """Raw recent listens for a username. [] on any failure, same contract
    as lastfm.recent_tracks()."""
    if not username:
        return []
    try:
        resp = requests.get(
            f"{api_base or API_BASE}/1/user/{username}/listens",
            params={"count": min(limit, 100)}, headers=_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("payload", {}).get("listens", [])
    except Exception as e:
        print(f"[listenbrainz] error: {e}")
        return []


def recently_played_suggestions(conn, username: str, limit: int = 50,
                                 user_device_ids: set[int] | None = None, api_base: str = "") -> list[dict]:
    """Distinct albums from the user's most recent listens — ListenBrainz
    counterpart of lastfm.recently_played_suggestions()."""
    listens = recent_listens(username, limit, api_base=api_base)
    if not listens:
        return []

    library = suggestions_mod.local_library_index(conn)
    covered = suggestions_mod.covered_devices(conn, library)

    seen: set[tuple[str, str]] = set()
    out = []
    for l in listens:
        meta = l.get("track_metadata") or {}
        artist = meta.get("artist_name", "")
        album = meta.get("release_name", "")
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
            "image_url": None,  # listens don't carry caa ids; frontend falls back to the local cover
            "source": "listenbrainz-recent",
        })
    return out
