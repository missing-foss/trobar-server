#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plays reported by a device, kept as a listening history of Trobar's own.
Optional and per user: nothing is stored unless the device's owner turned it
on, which is off by default. Turning it off stops storing and keeps what is
there; clear() deletes it.

A play is stored once however often a device retries: one track, one second,
one person. It keeps its own artist, title and album, so it survives the
track leaving the library.

What is stored is also a history source like Last.fm, ListenBrainz or Maloja:
most_played(), suggestions() and recently_played_suggestions() answer in the
shape theirs do, while the switch is on. With it off they stay stored but
aren't used, and count again if it goes back on.
"""

import logging
import random
import time

import suggestions as suggestions_mod

_log = logging.getLogger(__name__)

PLAYS_MAX = 500
# A device clock can run a little fast; further ahead than this, it is wrong.
_FUTURE_SLACK_S = 86400
_EARLIEST = 946684800  # 2000-01-01: a watch that lost its clock reads as 1970 or so


def valid_plays(plays) -> bool:
    """Whether `plays` is a list of at most PLAYS_MAX {"track_id", "played_at"}
    objects, both whole numbers SQLite can hold, played_at in Unix seconds."""
    def whole(x):
        return isinstance(x, int) and not isinstance(x, bool) and abs(x) < 2 ** 63
    return (isinstance(plays, list) and len(plays) <= PLAYS_MAX
            and all(isinstance(p, dict) and whole(p.get("track_id")) and whole(p.get("played_at"))
                    for p in plays))


def is_enabled(conn, user_id: int) -> bool:
    row = conn.execute("SELECT device_plays_enabled FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


def set_enabled(conn, user_id: int, enabled: bool) -> None:
    """Commits."""
    conn.execute("UPDATE users SET device_plays_enabled = ? WHERE id = ?", (1 if enabled else 0, user_id))
    conn.commit()


def count(conn, user_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM device_plays WHERE user_id = ?", (user_id,)).fetchone()[0]


def clear(conn, user_id: int) -> int:
    """Deletes this person's stored plays; returns how many. Commits."""
    deleted = conn.execute("DELETE FROM device_plays WHERE user_id = ?", (user_id,)).rowcount
    conn.commit()
    return deleted


def record(conn, device_id: int | None, user_id: int, plays: list[dict]) -> dict:
    """Stores `plays`, which valid_plays() has passed, for the device's owner.
    With the history off nothing is stored: {"enabled": false}, so a client can
    drop them. Otherwise the counts: stored, duplicates (already here), unknown
    (no such track), bad_time (a played_at no clock should give). Commits."""
    if not is_enabled(conn, user_id):
        return {"enabled": False, "stored": 0, "duplicates": 0, "unknown": 0, "bad_time": 0}
    latest = int(time.time()) + _FUTURE_SLACK_S
    stored = duplicates = unknown = bad_time = 0
    for play in plays:
        if not _EARLIEST <= play["played_at"] <= latest:
            bad_time += 1
            continue
        track = conn.execute("SELECT artist, title, album FROM tracks WHERE id = ?",
                             (play["track_id"],)).fetchone()
        if track is None:
            unknown += 1
            continue
        added = conn.execute(
            "INSERT OR IGNORE INTO device_plays (user_id, device_id, track_id, artist, title, album, played_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
            (user_id, device_id, play["track_id"], track["artist"], track["title"], track["album"],
             play["played_at"])).rowcount
        stored += added
        duplicates += 1 - added
    conn.commit()
    return {"enabled": True, "stored": stored, "duplicates": duplicates, "unknown": unknown, "bad_time": bad_time}


_PERIOD_DAYS = {"overall": None, "7day": 7, "1month": 30, "3month": 91, "6month": 182, "12month": 365}


def is_source(conn, user_id: int) -> bool:
    """Whether this person's plays count as a history source: only while the
    switch is on, whatever is stored."""
    return is_enabled(conn, user_id)


def _album_chart(conn, user_id: int, period: str, limit: int) -> list:
    """Albums by plays in `period`, most played first. A play with no album
    or artist counts for none."""
    days = _PERIOD_DAYS.get(period)
    since = "" if days is None else f"AND played_at >= datetime('now', '-{int(days)} days') "
    return conn.execute(
        "SELECT artist, album, COUNT(*) AS plays FROM device_plays "
        "WHERE user_id = ? AND COALESCE(artist, '') != '' AND COALESCE(album, '') != '' " + since +
        "GROUP BY lower(artist), lower(album) ORDER BY plays DESC, MAX(played_at) DESC LIMIT ?",
        (user_id, limit)).fetchall()


def most_played(conn, user_id: int, period: str = "6month", limit: int = 10) -> list[dict]:
    """The counterpart of lastfm.most_played(), with the library's names where
    it has the album."""
    library = suggestions_mod.local_library_index(conn)
    out = []
    for row in _album_chart(conn, user_id, period, limit):
        artist, album = library.get((row["artist"].lower(), row["album"].lower()), (row["artist"], row["album"]))
        out.append({"artist": artist, "album": album, "playcount": row["plays"], "image_url": None})
    return out


def _suggestion(library: dict, covered: dict, user_device_ids, artist: str, album: str) -> dict | None:
    """An album as a suggestion, or None if the library lacks it or every
    device already has it."""
    key = (artist.lower(), album.lower())
    local = library.get(key)
    if local is None or suggestions_mod.is_fully_synced(covered, key, user_device_ids):
        return None
    return {"artist": local[0], "album": local[1], "library_artist": local[0], "library_album": local[1],
            "image_url": None}


def suggestions(conn, user_id: int, period: str = "6month", limit: int = 50,
                user_device_ids: set[int] | None = None) -> list[dict]:
    """The counterpart of lastfm.suggestions(): most-played albums in the
    library and not yet on every device the caller manages."""
    library = suggestions_mod.local_library_index(conn)
    covered = suggestions_mod.covered_devices(conn, library)
    out = []
    for row in _album_chart(conn, user_id, period, limit):
        found = _suggestion(library, covered, user_device_ids, row["artist"], row["album"])
        if found is not None:
            out.append({**found, "playcount": row["plays"], "source": "device_plays"})
    random.shuffle(out)  # same reasoning as lastfm.suggestions()
    return out


def recently_played_suggestions(conn, user_id: int, limit: int = 50,
                                user_device_ids: set[int] | None = None) -> list[dict]:
    """The counterpart of lastfm.recently_played_suggestions(): albums of the
    latest plays, newest first, once each."""
    library = suggestions_mod.local_library_index(conn)
    covered = suggestions_mod.covered_devices(conn, library)
    out = []
    for row in conn.execute(
            "SELECT artist, album FROM device_plays WHERE user_id = ? AND COALESCE(artist, '') != '' "
            "AND COALESCE(album, '') != '' GROUP BY lower(artist), lower(album) "
            "ORDER BY MAX(played_at) DESC LIMIT ?", (user_id, limit)):
        found = _suggestion(library, covered, user_device_ids, row["artist"], row["album"])
        if found is not None:
            out.append({**found, "source": "device_plays-recent"})
    return out
