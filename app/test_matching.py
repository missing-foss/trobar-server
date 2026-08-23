#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for matching.py's playlist-track resolution.

    python3 -m unittest test_matching -v      # from app/

Focus: #94 — the Roon fuzzy-match path (match_playlist_track) narrowed
candidates with SQLite's ASCII-only lower(), so any artist with an uppercase
non-ASCII letter ("Édith Piaf", "CÉLINE DION") silently never matched and
never synced. The fix folds both sides with the same Unicode-aware Python
normalization via a registered pynorm() function.
"""
import sqlite3
import unittest

import matching


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "relative_path TEXT UNIQUE NOT NULL, artist TEXT NOT NULL, "
        "album TEXT NOT NULL, title TEXT NOT NULL, deleted_at TEXT)"
    )
    return conn


def _add(conn, artist, title, relative_path=None, deleted_at=None):
    relative_path = relative_path or f"{artist}/{title}.flac"
    cur = conn.execute(
        "INSERT INTO tracks (relative_path, artist, album, title, deleted_at) "
        "VALUES (?, ?, '', ?, ?)", (relative_path, artist, title, deleted_at))
    return cur.lastrowid


class MatchPlaylistTrackArtistFoldTests(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_uppercase_accented_artist_matches(self):
        # #94 regression: stored "Édith Piaf" vs a query for the same — the
        # É must fold on both sides. Under SQLite lower() this returned None.
        tid = _add(self.conn, "Édith Piaf", "La Vie en rose")
        self.assertEqual(matching.match_playlist_track(self.conn, "Édith Piaf", "La Vie en rose"), tid)

    def test_all_caps_accented_artist_matches(self):
        tid = _add(self.conn, "CÉLINE DION", "Pour que tu m'aimes encore")
        self.assertEqual(
            matching.match_playlist_track(self.conn, "Céline Dion", "Pour que tu m'aimes encore"), tid)

    def test_query_casing_differs_from_stored(self):
        # Stored lower, queried upper — folding is symmetric.
        tid = _add(self.conn, "étienne daho", "Week-end à Rome")
        self.assertEqual(matching.match_playlist_track(self.conn, "ÉTIENNE DAHO", "Week-end à Rome"), tid)

    def test_eszett_casefold(self):
        # casefold() folds "ß" -> "ss"; a query for the "STRASSE" spelling
        # resolves the "Straße" tag. lower() would not.
        tid = _add(self.conn, "Die Straße", "Lied")
        self.assertEqual(matching.match_playlist_track(self.conn, "Die STRASSE", "Lied"), tid)

    def test_ascii_artist_still_matches(self):
        tid = _add(self.conn, "ABBA", "Waterloo")
        self.assertEqual(matching.match_playlist_track(self.conn, "abba", "Waterloo"), tid)

    def test_no_match_returns_none(self):
        _add(self.conn, "Édith Piaf", "La Vie en rose")
        self.assertIsNone(matching.match_playlist_track(self.conn, "Ólafur Arnalds", "Near Light"))

    def test_deleted_rows_are_ignored(self):
        _add(self.conn, "Édith Piaf", "Non, je ne regrette rien", deleted_at="2026-01-01")
        self.assertIsNone(
            matching.match_playlist_track(self.conn, "Édith Piaf", "Non, je ne regrette rien"))


class MatchPlaylistTrackTitleTests(unittest.TestCase):
    """The title side already compared in Python; casefold() must not regress
    the existing annotation-stripping / fuzzy behaviour for accented titles."""

    def setUp(self):
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_trailing_annotation_stripped(self):
        tid = _add(self.conn, "Ólafur Arnalds", "Near Light")
        self.assertEqual(
            matching.match_playlist_track(self.conn, "Ólafur Arnalds", "Near Light (Live)"), tid)

    def test_credits_list_last_segment_is_the_performer(self):
        # Roon subtitle "songwriter, performer"; the accented performer name
        # is the last segment and must fold correctly.
        tid = _add(self.conn, "Céline Dion", "S'il suffisait d'aimer")
        self.assertEqual(
            matching.match_playlist_track(
                self.conn, "Jean-Jacques Goldman, CÉLINE DION", "S'il suffisait d'aimer"), tid)


class MatchByPathTests(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_trailing_segments_match_across_mount_prefixes(self):
        tid = _add(self.conn, "Placebo", "Come Home",
                   relative_path="Placebo/Placebo/01-01 - Come Home.flac")
        self.assertEqual(
            matching.match_playlist_track_by_path(
                self.conn, "/some/other/root/Placebo/Placebo/01-01 - Come Home.flac"), tid)

    def test_no_path_match_returns_none(self):
        _add(self.conn, "Placebo", "Come Home", relative_path="Placebo/Placebo/Come Home.flac")
        self.assertIsNone(matching.match_playlist_track_by_path(self.conn, "Nirvana/Nevermind/Breed.flac"))


if __name__ == "__main__":
    unittest.main()
