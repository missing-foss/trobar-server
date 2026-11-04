#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#200: the tiered identity/matching resolver playlist_sync.py calls to
resolve a provider playlist entry against the local `tracks` catalog. Sits
in FRONT of matching.py rather than replacing it — tier 3 below is
matching.py, completely unchanged.

The tiers, cascading, cheapest and most certain first:

1. Exact ISRC: the provider's own ISRC for this entry (Tidal, Spotify and
   Navidrome give one) against tracks.isrc, read from local file tags by the
   scanner. Free, no I/O.
2. Fingerprint-backfilled ISRC: the same ISRC against tracks.acoustid_isrc.
   That column is filled by fingerprint.py's AcoustID/MusicBrainz backfill,
   an independent process run after a library scan, not from here:
   fingerprinting needs real audio bytes, and the only audio Trobar can read
   is its own scanned library, never a playlist entry's provider-side
   stream. This tier is what makes that backfill pay off: a track
   fingerprinted once then matches by ISRC on every later sync.
3. MusicBrainz recording id: the provider's recording id for this entry
   (Jellyfin and Navidrome give one) against tracks.mbid, read from file
   tags, then against tracks.acoustid_mbid, the recording AcoustID resolved
   the track's fingerprint to. A recording id names the song, the same on
   every release that carries it.
4. Today's matcher, unchanged: match_playlist_track_by_path /
   match_playlist_track. Still free, and first for anything with a path.

Deliberately not a class — matching.py isn't one either, and there's no
state to carry between calls."""

import logging
import sqlite3

import matching

_log = logging.getLogger(__name__)


def _by_id(conn: sqlite3.Connection, column: str, value: str,
           path: str | None, album: str | None) -> int | None:
    """The library track whose `column` (one of the id columns, never input)
    holds `value`. Several can: the same recording, and so the same ISRC
    and recording id, on an album and on a compilation or a deluxe edition.
    Then the copy the entry's path names wins, then the entry's album
    (matching._prefer_album), then the lowest id, so the answer never
    depends on row order."""
    rows = conn.execute(
        f"SELECT id, album FROM tracks WHERE deleted_at IS NULL AND {column} = ? ORDER BY id", (value,)
    ).fetchall()
    if len(rows) <= 1:
        return rows[0]["id"] if rows else None
    if path:
        by_path = matching.match_playlist_track_by_path(conn, path, album)
        if by_path in {row["id"] for row in rows}:
            return by_path
    return matching._prefer_album(rows, album)


def resolve_playlist_track(
    conn: sqlite3.Connection, *, artist: str, title: str,
    path: str | None = None, isrc: str | None = None, album: str | None = None,
    mbid: str | None = None,
) -> int | None:
    """Returns a tracks.id for this playlist entry, or None if no tier
    resolves it.

    `isrc` is the PROVIDER's own ISRC for this entry, when the caller has
    one: tidal_client, spotify_client and subsonic_client (Navidrome) pass
    it. `mbid` is the provider's MusicBrainz recording id: jellyfin_client
    and subsonic_client pass it; Emby's id is the release-track one and
    Plex has none, so neither does. `album`, when the source gives one, breaks a tie
    between equally good matches in every tier (matching._prefer_album),
    and in the id tiers the entry's path does first (_by_id); neither is
    ever required."""
    if isrc:
        for column in ("isrc", "acoustid_isrc"):
            matched = _by_id(conn, column, isrc, path, album)
            if matched is not None:
                return matched
    if mbid:
        mbid = mbid.strip().lower()
        for column in ("mbid", "acoustid_mbid"):
            matched = _by_id(conn, column, mbid, path, album)
            if matched is not None:
                return matched

    matched_id = None
    if path:
        matched_id = matching.match_playlist_track_by_path(conn, path, album)
    if matched_id is None:
        matched_id = matching.match_playlist_track(conn, artist, title, album)
    return matched_id
