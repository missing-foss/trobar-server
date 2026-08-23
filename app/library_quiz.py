#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#508: pair-selection logic for the web UI's "duel the bard" Easter egg —
"which of these two artists has more albums?", scoped to the local library
(no MusicBrainz/Lidarr dependency, so it works offline and needs nothing an
install might not have configured).

Kept as a plain, DB-free module (same shape as matching.py/sync_state.py)
so the actual game logic is unit-testable without a Flask app or a
database — main.py's api_library_quiz_pair() is just the DB read plus a
call into pick_pair() below.

Deliberately excludes "Various Artists"/compilation-style entries: the
`tracks` table has no `album_artist` column, so a compilation's per-track
`artist` values would otherwise assemble into a phantom mega-artist that
wins every round on album count alone."""

import random

MIN_ALBUMS = 3  # an artist needs at least this many albums to be a candidate at all
_MIN_GAP = 2  # ...and the pair's counts must differ by at least this many albums...
_MIN_RATIO = 1.4  # ...and the larger must be at least this many times the smaller —
# both conditions together rule out both "4 vs 5" (gap only 1) and "20 vs 25"
# (a 25% edge that still reads as a near-coin-flip) while still allowing
# "4 vs 7" or "10 vs 15".

EXCLUDED_ARTIST_NAMES = frozenset({"various artists", "various", "unknown artist", "unknown"})


def is_eligible_artist(name: str | None) -> bool:
    name = (name or "").strip()
    return bool(name) and name.lower() not in EXCLUDED_ARTIST_NAMES


def eligible_candidates(rows: list[dict]) -> list[dict]:
    """rows: [{"artist": str, "album_count": int}, ...], one per artist in
    the library (already grouped/counted by the caller's SQL). Returns only
    those that could plausibly appear in a round."""
    return [
        r for r in rows
        if is_eligible_artist(r.get("artist")) and (r.get("album_count") or 0) >= MIN_ALBUMS
    ]


def _has_real_gap(lo: int, hi: int) -> bool:
    return hi - lo >= _MIN_GAP and hi >= lo * _MIN_RATIO


def pick_pair(candidates: list[dict], rng: random.Random | None = None) -> tuple[dict, dict] | None:
    """Pick two distinct candidates with a real album-count gap between
    them (see _has_real_gap). Returns (a, b) in random order, or None if
    fewer than two candidates were given, or every candidate ties on
    album_count (genuinely no game to offer).

    `rng` is a random.Random instance, passed explicitly in tests for
    deterministic, seedable output; production code can omit it and get a
    freshly-constructed one (a plain default arg here would share one
    Random across every call, which is harmless for a game but not what
    the type signature should imply)."""
    if rng is None:
        rng = random.Random()
    pool = list(candidates)
    if len(pool) < 2:
        return None

    rng.shuffle(pool)
    # A shuffled list makes any nearby pair as good as a global O(n^2) scan
    # — this stays cheap even on a library with thousands of artists.
    for i, a in enumerate(pool):
        for b in pool[i + 1:i + 6]:
            lo, hi = sorted((a["album_count"], b["album_count"]))
            if _has_real_gap(lo, hi):
                return (a, b) if rng.random() < 0.5 else (b, a)

    # Nothing met the gap threshold — a small or unusually homogeneous
    # library. Fall back to the single widest gap available rather than
    # reporting no game at all; only actually give up if every candidate
    # ties (there's no "more albums" to ask about).
    pool.sort(key=lambda c: c["album_count"])
    lo_c, hi_c = pool[0], pool[-1]
    if lo_c["album_count"] == hi_c["album_count"]:
        return None
    return (lo_c, hi_c) if rng.random() < 0.5 else (hi_c, lo_c)
