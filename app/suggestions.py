#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-provider suggestion helpers shared by every suggestion source
(Last.fm top-played/recently-played in lastfm.py, recently-added below) —
library lookup and per-album device coverage (a suggestion is only
suppressed once it's synced to *every* device the caller manages, not just
"a selection exists somewhere")."""

import sqlite3


def local_library_index(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, str]]:
    """Lowercased (artist, album) -> exact on-disk casing. Providers' casing/
    punctuation can differ from the tags actually on disk, and selection
    resolution matches exactly (see sync_state._resolve_selection_track_ids)
    — passing a provider's own casing as a selection target could silently
    resolve to zero tracks."""
    return {
        (row["artist"].lower(), row["album"].lower()): (row["artist"], row["album"])
        for row in conn.execute("SELECT DISTINCT artist, album FROM tracks WHERE deleted_at IS NULL")
    }


def covered_devices(conn: sqlite3.Connection,
                     library: dict[tuple[str, str], tuple[str, str]]) -> dict[tuple[str, str], set[int]]:
    """(artist.lower(), album.lower()) -> set of device_ids already covering
    it, via either an exact album-level selection or a whole-artist one."""
    sel_devices: dict[int, set[int]] = {}
    for row in conn.execute("SELECT selection_id, device_id FROM selection_devices"):
        sel_devices.setdefault(row["selection_id"], set()).add(row["device_id"])

    covered: dict[tuple[str, str], set[int]] = {}
    for row in conn.execute("SELECT id, type, target FROM selections WHERE type IN ('artist', 'album')"):
        devs = sel_devices.get(row["id"])
        if not devs:
            continue
        if row["type"] == "album" and "||" in row["target"]:
            artist, album = row["target"].split("||", 1)
            covered.setdefault((artist.lower(), album.lower()), set()).update(devs)
        else:
            target_artist = row["target"].lower()
            for lib_artist, lib_album in library:
                if lib_artist == target_artist:
                    covered.setdefault((lib_artist, lib_album), set()).update(devs)
    return covered


def is_fully_synced(covered: dict[tuple[str, str], set[int]], key: tuple[str, str],
                     user_device_ids: set[int] | None) -> bool:
    if not user_device_ids:
        return False
    return covered.get(key, set()) >= user_device_ids


def recently_added(conn: sqlite3.Connection, user_device_ids: set[int] | None = None,
                    limit: int = 10) -> list[dict]:
    """Albums the local scanner has seen most recently — a Roon-independent
    stand-in for "recently added". Roon's Browse API doesn't
    expose that as a traversable menu item at all (verified live against the
    real Core: Library > Albums is a flat alphabetical catalog, no sort/
    filter items) — but the scanner already timestamps every track with
    `scanned_at` the first time it sees it, which is exactly the same
    signal, and doesn't depend on Roon being online."""
    library = local_library_index(conn)
    covered = covered_devices(conn, library)
    rows = conn.execute(
        "SELECT artist, album, MIN(scanned_at) AS added_at FROM tracks "
        "WHERE deleted_at IS NULL GROUP BY artist, album ORDER BY added_at DESC LIMIT ?",
        (limit * 3,),  # overfetch — some will drop out via the coverage filter below
    ).fetchall()
    out = []
    for row in rows:
        key = (row["artist"].lower(), row["album"].lower())
        if is_fully_synced(covered, key, user_device_ids):
            continue
        out.append({
            "artist": row["artist"],
            "album": row["album"],
            "added_at": row["added_at"],
            "library_artist": row["artist"],
            "library_album": row["album"],
            "image_url": None,  # no provider art for a local-only source; frontend falls back to the local cover
            "source": "recent",
        })
        if len(out) >= limit:
            break
    return out


def recently_added_widget(conn: sqlite3.Connection, since: str,
                           user_device_ids: set[int] | None = None, limit: int = 60) -> list[dict]:
    """Albums first scanned into the library on/after `since` (an ISO-ish
    "YYYY-MM-DD..." string — scanned_at's own format) — the home
    dashboard's standalone Recently Added widget. Deliberately separate
    from recently_added() above (which feeds the Suggestions tab as a
    fixed top-N, no explicit window): this is threshold-based, driven by
    the widget's own configurable "within N months" setting, so nothing
    changes at all for the existing Suggestions feed."""
    library = local_library_index(conn)
    covered = covered_devices(conn, library)
    rows = conn.execute(
        "SELECT artist, album, MIN(scanned_at) AS added_at FROM tracks "
        "WHERE deleted_at IS NULL GROUP BY artist, album HAVING added_at >= ? "
        "ORDER BY added_at DESC LIMIT ?",
        (since, limit * 3),  # overfetch — some will drop out via the coverage filter below
    ).fetchall()
    out = []
    for row in rows:
        key = (row["artist"].lower(), row["album"].lower())
        if is_fully_synced(covered, key, user_device_ids):
            continue
        out.append({
            "artist": row["artist"],
            "album": row["album"],
            "added_at": row["added_at"],
            "library_artist": row["artist"],
            "library_album": row["album"],
            "image_url": None,
            "source": "recent",
        })
        if len(out) >= limit:
            break
    return out


def recently_released_widget(conn: sqlite3.Connection, since: str,
                              user_device_ids: set[int] | None = None, limit: int = 60) -> list[dict]:
    """Albums whose tag-derived release_date falls on/after `since` — new
    to the world, independent of when the scanner happened to see the
    file. release_date is best-effort YYYY-MM-DD (see scanner.py); albums
    with no release_date at all (nothing in the tags to derive one from)
    are excluded rather than guessed at."""
    library = local_library_index(conn)
    covered = covered_devices(conn, library)
    rows = conn.execute(
        "SELECT artist, album, MAX(release_date) AS released_at FROM tracks "
        "WHERE deleted_at IS NULL AND release_date IS NOT NULL "
        "GROUP BY artist, album HAVING released_at >= ? "
        "ORDER BY released_at DESC LIMIT ?",
        (since, limit * 3),
    ).fetchall()
    out = []
    for row in rows:
        key = (row["artist"].lower(), row["album"].lower())
        if is_fully_synced(covered, key, user_device_ids):
            continue
        out.append({
            "artist": row["artist"],
            "album": row["album"],
            "released_at": row["released_at"],
            "library_artist": row["artist"],
            "library_album": row["album"],
            "image_url": None,
            "source": "recent",
        })
        if len(out) >= limit:
            break
    return out
