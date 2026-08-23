#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#200: the tiered identity/matching resolver playlist_sync.py calls to
resolve a provider playlist entry against the local `tracks` catalog. Sits
in FRONT of matching.py rather than replacing it — tier 3 below is
matching.py, completely unchanged.

Three tiers, cascading, cheapest/most-certain first:

1. Exact ISRC — the provider's own ISRC for this entry (once a future
   provider-client PR starts supplying one — see resolve_playlist_track's
   docstring for why none does yet) against tracks.isrc (scanner-populated
   from local file tags, #200 step 1). Free, no I/O.
2. Fingerprint-backfilled ISRC — same provider ISRC against
   tracks.acoustid_isrc. That column is populated by fingerprint.py's
   AcoustID/MusicBrainz backfill, an entirely independent process triggered
   after a library SCAN (not from here) — see that module's docstring for
   why: fingerprinting needs real audio bytes, and the only audio Trobar
   can ever read is its own already-scanned local library, never a
   playlist entry's provider-side stream. By the time a playlist entry
   reaches this resolver, there's no specific local file left to fingerprint
   for it even on a miss. This tier is what makes that earlier backfill pay
   off: a track fingerprinted once (on scan) then matches by ISRC on every
   later sync without ever being touched again.
3. Today's matcher, unchanged — match_playlist_track_by_path /
   match_playlist_track. Still free, still first for anything with a path.

Deliberately not a class — matching.py isn't one either, and there's no
state to carry between calls."""

import sqlite3

import matching


def resolve_playlist_track(
    conn: sqlite3.Connection, *, artist: str, title: str,
    path: str | None = None, isrc: str | None = None,
) -> int | None:
    """Returns a tracks.id for this playlist entry, or None if no tier
    resolves it.

    `isrc` is the PROVIDER's own ISRC for this entry, when the caller has
    one. Checked directly at #200 planning time: no provider client
    (roon_client/subsonic_client/tidal_client/spotify_client/jellyfin_
    client/emby_client/plex_client/lms_client/filesystem_client) populates
    this today, so tiers 1/2 are currently unreachable in practice — they
    activate automatically, with no change needed here, the moment a future
    PR starts passing a real value through."""
    if isrc:
        row = conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND isrc = ?", (isrc,)
        ).fetchone()
        if row is not None:
            return row["id"]
        row = conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND acoustid_isrc = ?", (isrc,)
        ).fetchone()
        if row is not None:
            return row["id"]

    matched_id = None
    if path:
        matched_id = matching.match_playlist_track_by_path(conn, path)
    if matched_id is None:
        matched_id = matching.match_playlist_track(conn, artist, title)
    return matched_id
