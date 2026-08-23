#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve a playlist track against the locally-scanned `tracks` catalog.

match_playlist_track_by_path() is the primary strategy for providers whose
playlist entries carry a real file path (Subsonic) — a filesystem-identity
match, not a heuristic. match_playlist_track() (artist/title fuzzy matching)
exists specifically for Roon, whose Browse API never exposes a path or
album — only a display title/subtitle — and is used as a fallback for any
path-bearing provider entry that doesn't resolve by path either.

normalize() (Unicode-correct casefold + whitespace collapse) is exported
for reuse beyond this module's own matching: mirror_subsonic.py's
tag-based target index normalizes both sides of its (artist, album,
title) lookup key the same way, rather than inventing a second scheme."""

import difflib
import re
import sqlite3


def normalize(s: str) -> str:
    # casefold(), not lower(): Unicode-correct case folding (handles "ß",
    # accented capitals, etc.) — and it MUST be applied on both the query
    # value and the stored column via the same Python function, because
    # SQLite's built-in lower() folds ASCII A–Z only, so `lower(artist) = ?`
    # against a Python-folded value silently missed any artist with an
    # uppercase non-ASCII letter ("Édith Piaf", "CÉLINE DION"). See
    # _register_fold() below.
    return re.sub(r"\s+", " ", (s or "").strip().casefold())


def _register_fold(conn: sqlite3.Connection) -> None:
    """Expose normalize() to SQL as pynorm(), so the candidate query folds the
    stored artist with the exact same Unicode-aware normalization used on the
    query value — rather than SQLite's ASCII-only lower() (#94). Deterministic
    and idempotent; cheap to re-register per call."""
    conn.create_function("pynorm", 1, normalize, deterministic=True)


_TRAILING_ANNOTATION = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$")


def _strip_annotations(title: str) -> str:
    """Drop trailing "(...)"/"[...]" annotations — remaster/version/live tags
    (e.g. "Rebel Rebel (1999 Remaster)") that Roon's playlist title includes
    but the locally-tagged title often doesn't. Confirmed directly: this was
    the single biggest remaining cause of misses after fixing the
    credits-list artist issue — exact title match fails, and even fuzzy
    matching (0.85 cutoff) scores ~0.58 against the same song with a bare
    title. Applied repeatedly in case of multiple trailing annotations."""
    prev = title
    while True:
        stripped = _TRAILING_ANNOTATION.sub("", prev).strip()
        if stripped == prev:
            return prev
        prev = stripped


def _artist_candidates(artist: str) -> list[str]:
    """Roon's playlist subtitle is often a full credits list (songwriters
    first, performer last) rather than just the performing artist — e.g.
    "Martin L. Gore, Johnny Cash" for Johnny Cash's cover of a Depeche Mode
    song. Confirmed directly on a real playlist sync: every multi-name
    subtitle in the data followed that order, and matching only on the full
    string as-is matched 39/1607 tracks (2%) — almost all comma-separated
    ones failed. Try the full string first (single-artist case), then the
    last comma-separated segment (the performer, in the credits-list case)."""
    candidates = [artist]
    if "," in artist:
        candidates.append(artist.rsplit(",", 1)[-1].strip())
    return candidates


def match_playlist_track_by_path(conn: sqlite3.Connection, path: str) -> int | None:
    """Returns a `tracks.id` by matching a playlist entry's real file path
    against `tracks.relative_path`, or None if no match.

    Compares by trailing path segments rather than requiring byte-identical
    strings, since the source server's configured music root and this app's
    MUSIC_ROOT may be different mount points of the same physical files
    (e.g. differing NFS mount prefixes) even though the underlying
    Artist/Album/Filename structure is identical — confirmed this is exactly
    the shape Subsonic/Navidrome paths come in (e.g.
    "Placebo/Placebo/01-01 - Come Home.flac")."""
    if not path:
        return None
    segments = tuple(p for p in path.replace("\\", "/").split("/") if p)
    if not segments:
        return None

    rows = conn.execute(
        "SELECT id, relative_path FROM tracks WHERE deleted_at IS NULL "
        "AND relative_path LIKE ?",
        (f"%{segments[-1]}",),
    ).fetchall()
    for row in rows:
        row_segments = tuple(p for p in row["relative_path"].split("/") if p)
        n = min(len(segments), len(row_segments))
        if n and segments[-n:] == row_segments[-n:]:
            return row["id"]
    return None


def match_playlist_track(conn: sqlite3.Connection, artist: str, title: str) -> int | None:
    """Returns a `tracks.id`, or None if this track isn't in the local library
    (e.g. it's a Tidal/Qobuz-only entry Roon streams but never downloads)."""
    title_n = normalize(title)
    if not title_n:
        return None

    _register_fold(conn)
    for candidate in _artist_candidates(artist):
        artist_n = normalize(candidate)
        if not artist_n:
            continue

        rows = conn.execute(
            "SELECT id, artist, title FROM tracks WHERE deleted_at IS NULL "
            "AND pynorm(artist) = ?",
            (artist_n,),
        ).fetchall()
        if not rows:
            continue

        for row in rows:
            if normalize(row["title"]) == title_n:
                return row["id"]

        title_stripped = normalize(_strip_annotations(title))
        if title_stripped != title_n:
            for row in rows:
                if normalize(_strip_annotations(row["title"])) == title_stripped:
                    return row["id"]

        titles = [row["title"] for row in rows]
        close = difflib.get_close_matches(title_stripped, [_strip_annotations(t) for t in titles],
                                           n=1, cutoff=0.85)
        if close:
            for row in rows:
                if _strip_annotations(row["title"]) == close[0]:
                    return row["id"]
    return None
