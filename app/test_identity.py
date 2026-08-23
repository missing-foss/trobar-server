#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for identity.py's tiered resolver (#200): exact ISRC, fingerprint-
backfilled ISRC, and the unchanged matching.py fallback.

    python3 -m unittest test_identity -v      # from app/

Uses the real db.SCHEMA + db._run_migrations() (not a minimal ad-hoc table
like test_matching.py) since isrc/acoustid_isrc are migration-added
columns this module actually reads.
"""
import sqlite3
import unittest

import db
import identity


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)
    return conn


def _add_track(conn, artist, title, *, relative_path=None, isrc=None,
               acoustid_isrc=None, deleted=False) -> int:
    relative_path = relative_path or f"{artist}/{title}.flac"
    cur = conn.execute(
        "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, isrc, "
        "acoustid_isrc, deleted_at) VALUES (?, ?, '', ?, 1, 0.0, ?, ?, ?)",
        (relative_path, artist, title, isrc, acoustid_isrc, "now" if deleted else None),
    )
    conn.commit()
    return cur.lastrowid


class ExactIsrcTierTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()

    def test_matches_by_isrc_even_with_completely_different_metadata(self):
        # The point of tier 1: garbled/renamed metadata doesn't matter once
        # the ISRC lines up.
        track_id = _add_track(self.conn, "Correct Artist", "Correct Title", isrc="USRC17607839")
        matched = identity.resolve_playlist_track(
            self.conn, artist="Totally Different", title="Also Different",
            isrc="USRC17607839",
        )
        self.assertEqual(matched, track_id)

    def test_isrc_present_but_no_track_carries_it_falls_through_to_later_tiers(self):
        # Tier 1 misses (no track has this ISRC) but tier 3's fuzzy match
        # still finds it on artist/title — the cascade must fall through,
        # not stop dead just because isrc was supplied.
        track_id = _add_track(self.conn, "An Artist", "A Song")
        matched = identity.resolve_playlist_track(
            self.conn, artist="An Artist", title="A Song", isrc="USRC00000000",
        )
        self.assertEqual(matched, track_id)

    def test_deleted_track_is_not_matched_by_isrc(self):
        _add_track(self.conn, "Gone Artist", "Gone Song", isrc="USRC17607839", deleted=True)
        matched = identity.resolve_playlist_track(
            self.conn, artist="Gone Artist", title="Gone Song", isrc="USRC17607839",
        )
        self.assertIsNone(matched)

    def test_no_isrc_supplied_skips_tier_1_entirely(self):
        # No provider populates isrc today (#200) — confirm the resolver is
        # still fully usable via tier 3 alone when isrc is None.
        track_id = _add_track(self.conn, "An Artist", "A Song")
        matched = identity.resolve_playlist_track(
            self.conn, artist="An Artist", title="A Song", isrc=None,
        )
        self.assertEqual(matched, track_id)


class FingerprintBackfilledIsrcTierTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()

    def test_matches_by_acoustid_isrc_when_the_files_own_tag_has_none(self):
        # Simulates the "persist-once" payoff: a previous fingerprint run
        # (tier 4, a later PR) already resolved this track's ISRC via
        # AcoustID and cached it — no fingerprinting needed on THIS call.
        track_id = _add_track(self.conn, "An Artist", "A Song", acoustid_isrc="USRC17607839")
        matched = identity.resolve_playlist_track(
            self.conn, artist="Different Artist", title="Different Song",
            isrc="USRC17607839",
        )
        self.assertEqual(matched, track_id)

    def test_own_tag_isrc_is_tried_before_the_backfilled_one(self):
        # Two different tracks: one has the real ISRC in its own tag, the
        # other only has a (wrong, for this test) acoustid-backfilled one.
        # Tier 1 must win — it's checked first and is the more direct
        # signal (the file's own tag, not a fingerprint's best guess).
        direct_id = _add_track(self.conn, "Direct", "Match", isrc="USRC17607839",
                               relative_path="a.flac")
        _add_track(self.conn, "Backfilled", "Match", acoustid_isrc="USRC17607839",
                  relative_path="b.flac")
        matched = identity.resolve_playlist_track(
            self.conn, artist="Whatever", title="Whatever", isrc="USRC17607839",
        )
        self.assertEqual(matched, direct_id)


class MatcherFallbackTierTests(unittest.TestCase):
    """Tier 3 is matching.py, completely unchanged — these just confirm the
    resolver actually delegates to it rather than reimplementing it."""

    def setUp(self):
        self.conn = _make_conn()

    def test_path_based_match_still_works_with_no_isrc_involved(self):
        track_id = _add_track(self.conn, "A", "B", relative_path="Placebo/Placebo/01 - Come Home.flac")
        matched = identity.resolve_playlist_track(
            self.conn, artist="A", title="B",
            path="Music/Placebo/Placebo/01 - Come Home.flac",
        )
        self.assertEqual(matched, track_id)

    def test_fuzzy_artist_title_match_still_works(self):
        # Unicode-aware casefold (#94), not just ASCII lower() — a query in
        # a different case of the SAME accented letters must still match.
        track_id = _add_track(self.conn, "Édith Piaf", "Non, je ne regrette rien")
        matched = identity.resolve_playlist_track(
            self.conn, artist="ÉDITH PIAF", title="Non, je ne regrette rien",
        )
        self.assertEqual(matched, track_id)

    def test_genuine_miss_returns_none(self):
        matched = identity.resolve_playlist_track(
            self.conn, artist="Nobody", title="Nothing",
        )
        self.assertIsNone(matched)

    def test_genuine_miss_with_an_unrelated_isrc_and_track_present_returns_none(self):
        # Confirms all three tiers miss cleanly together, not just tier 3
        # alone — an unrelated track existing in the library must not
        # produce a false positive on any tier.
        _add_track(self.conn, "Unrelated Artist", "Unrelated Song")
        matched = identity.resolve_playlist_track(
            self.conn, artist="Nobody", title="Nothing", isrc="USRC99999999",
        )
        self.assertIsNone(matched)


if __name__ == "__main__":
    unittest.main()
