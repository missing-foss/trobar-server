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
import logging
import re
import sqlite3
import unicodedata

_log = logging.getLogger(__name__)


# Bump whenever normalize() changes: tracks.artist_norm/title_norm hold its
# output, and db._refresh_norms recomputes every row when this differs from
# the version they were computed with.
NORM_VERSION = 1


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


_OPENERS = "(["
_CLOSERS = ")]"


def _strip_annotations(title: str) -> str:
    """Drop trailing "(...)"/"[...]" annotations — remaster/version/live tags
    (e.g. "Rebel Rebel (1999 Remaster)") that Roon's playlist title includes
    but the locally-tagged title often doesn't. Confirmed directly: this was
    the single biggest remaining cause of misses after fixing the
    credits-list artist issue — exact title match fails, and even fuzzy
    matching (0.88 cutoff) scores ~0.58 against the same song with a bare
    title. Applied repeatedly in case of multiple trailing annotations.

    One backward scan, linear in the title's length: titles come from
    playlist providers and tags, so their length is not ours to bound. An
    annotation is an opener, then anything but a closer, then a closer, then
    only whitespace; an opener and a closer need not be a pair, and an
    annotation may hold more openers ("(a (b)" goes whole), as the regular
    expression this replaced had it (test_matching keeps that expression,
    to check the scan against it)."""
    end = len(title)
    while True:
        close = end
        while close > 0 and title[close - 1].isspace():
            close -= 1
        if close == 0 or title[close - 1] not in _CLOSERS:
            break
        # The annotation's body runs back to the previous closer; it opens
        # at the first opener after that one.
        body_start = close - 1
        while body_start > 0 and title[body_start - 1] not in _CLOSERS:
            body_start -= 1
        opener = next((i for i in range(body_start, close - 1) if title[i] in _OPENERS), None)
        if opener is None:
            break
        end = opener
        while end > 0 and title[end - 1].isspace():
            end -= 1
    return title[:end].strip()


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


def _prefer_album(rows: list, album: str | None) -> int | None:
    """The id of the first row whose album matches `album` (normalised), or
    else of the first row: a tie-breaker between candidates that match
    equally well, never a requirement, since many sources give no album
    (Roon, .m3u files, link imports). None when `rows` is empty."""
    if not rows:
        return None
    album_n = normalize(album) if album else ""
    if album_n:
        for row in rows:
            if normalize(row["album"] or "") == album_n:
                return row["id"]
    return rows[0]["id"]


def match_playlist_track_by_path(conn: sqlite3.Connection, path: str,
                                 album: str | None = None) -> int | None:
    """Returns a `tracks.id` by matching a playlist entry's real file path
    against `tracks.relative_path`, or None if no match.

    Compares by trailing path segments rather than requiring byte-identical
    strings, since the source server's configured music root and this app's
    MUSIC_ROOT may be different mount points of the same physical files
    (e.g. differing NFS mount prefixes) even though the underlying
    Artist/Album/Filename structure is identical — confirmed this is exactly
    the shape Subsonic/Navidrome paths come in (e.g.
    "Placebo/Placebo/01-01 - Come Home.flac").

    Segments are compared after NFC normalisation on both sides. An accent
    can be stored precomposed or decomposed, the two render identically, and
    a playlist written on a platform that prefers one form (macOS tools
    write decomposed) names the same file as a library stored in the other.
    The SQL prefilter is a byte-level LIKE, so it is asked for both forms of
    the last segment; otherwise the candidate row would never reach the
    comparison. A name mixing both forms within one segment is not covered.

    Several files can share the entry's trailing segments, when the entry
    is just a file name. `album` then picks among them (_prefer_album)."""
    if not path:
        return None
    segments = tuple(unicodedata.normalize("NFC", p)
                     for p in path.replace("\\", "/").split("/") if p)
    if not segments:
        return None

    last_forms = sorted({segments[-1], unicodedata.normalize("NFD", segments[-1])})
    rows = conn.execute(
        "SELECT id, relative_path, album FROM tracks WHERE deleted_at IS NULL AND ("
        + " OR ".join("relative_path LIKE ?" for _ in last_forms) + ")",
        tuple(f"%{form}" for form in last_forms),
    ).fetchall()
    hits = []
    for row in rows:
        row_segments = tuple(unicodedata.normalize("NFC", p)
                             for p in row["relative_path"].split("/") if p)
        n = min(len(segments), len(row_segments))
        if n and segments[-n:] == row_segments[-n:]:
            hits.append(row)
    return _prefer_album(hits, album)


def match_playlist_track(conn: sqlite3.Connection, artist: str, title: str,
                         album: str | None = None) -> int | None:
    """Returns a `tracks.id`, or None if this track isn't in the local library
    (e.g. it's a Tidal/Qobuz-only entry Roon streams but never downloads).

    Each step takes every track that matches it, not the first: the same
    title by the same artist can be on a studio album, a live album and a
    compilation, and `album`, when the source gives one, picks among them
    (_prefer_album). Without it, the first match wins, as before."""
    title_n = normalize(title)
    if not title_n:
        return None

    _register_fold(conn)
    for candidate in _artist_candidates(artist):
        artist_n = normalize(candidate)
        if not artist_n:
            continue

        # An index lookup on the stored artist_norm. A row nobody normalised
        # (a writer other than the scanner) is still found, through the same
        # index's NULL entries, by folding its artist here instead.
        rows = conn.execute(
            "SELECT id, artist, title, album, title_norm FROM tracks WHERE deleted_at IS NULL "
            "AND (artist_norm = ? OR (artist_norm IS NULL AND pynorm(artist) = ?))",
            (artist_n, artist_n),
        ).fetchall()
        if not rows:
            continue

        exact = [row for row in rows
                 if (row["title_norm"] if row["title_norm"] is not None else normalize(row["title"])) == title_n]
        if exact:
            return _prefer_album(exact, album)

        title_stripped = normalize(_strip_annotations(title))
        if title_stripped != title_n:
            stripped = [row for row in rows
                        if normalize(_strip_annotations(row["title"])) == title_stripped]
            if stripped:
                return _prefer_album(stripped, album)

        # Both sides normalised, as in the two steps above: the query is,
        # and comparing it with titles as stored penalised every
        # capitalised or accented title before similarity was even judged.
        # The cutoff favours precision: a wrong match puts the wrong song on
        # a device or a mirror unseen, while a miss shows as unresolved.
        stripped_n = [normalize(_strip_annotations(row["title"])) for row in rows]
        close = difflib.get_close_matches(title_stripped, stripped_n, n=1, cutoff=0.88)
        if close:
            return _prefer_album([row for row, t in zip(rows, stripped_n) if t == close[0]], album)
    return None
