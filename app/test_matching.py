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
import random
import re
import sqlite3
import time
import unicodedata
import unittest

import matching


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "relative_path TEXT UNIQUE NOT NULL, artist TEXT NOT NULL, "
        "album TEXT NOT NULL, title TEXT NOT NULL, deleted_at TEXT, "
        # Left NULL by _add: the matcher must still find such rows, as it
        # does any row a writer other than the scanner adds.
        "artist_norm TEXT, title_norm TEXT)"
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

    # An accent can be stored precomposed (NFC, "é" as one code point) or
    # decomposed (NFD, "e" plus a combining accent). Both render identically
    # and neither is wrong: macOS tools tend to write NFD, most others NFC. A
    # playlist written in one form and a library on disk in the other are the
    # same files, and must match.
    _NFC_PATH = unicodedata.normalize("NFC", "Édith Piaf/Chansons/01 - L'hymne à l'amour.flac")
    _NFD_PATH = unicodedata.normalize("NFD", _NFC_PATH)

    def test_decomposed_entry_matches_a_precomposed_file(self):
        self.assertNotEqual(self._NFC_PATH, self._NFD_PATH)  # the fixture really differs
        tid = _add(self.conn, "Édith Piaf", "L'hymne à l'amour", relative_path=self._NFC_PATH)
        self.assertEqual(
            matching.match_playlist_track_by_path(self.conn, "/Volumes/Music/" + self._NFD_PATH), tid)

    def test_precomposed_entry_matches_a_decomposed_file(self):
        tid = _add(self.conn, "Édith Piaf", "L'hymne à l'amour", relative_path=self._NFD_PATH)
        self.assertEqual(
            matching.match_playlist_track_by_path(self.conn, "/music/" + self._NFC_PATH), tid)

    def test_a_different_accent_still_does_not_match(self):
        """Normalising the form must not turn into ignoring accents: "à"
        and "a" are different names."""
        _add(self.conn, "Édith Piaf", "L'hymne à l'amour", relative_path=self._NFC_PATH)
        self.assertIsNone(matching.match_playlist_track_by_path(
            self.conn, "Édith Piaf/Chansons/01 - L'hymne a l'amour.flac"))



class AlbumTiebreakTests(unittest.TestCase):
    """The same title by the same artist on several albums: the entry's
    album picks among them when the source gives one; without it, or when
    none matches, the first match wins as before."""

    def setUp(self):
        self.conn = _conn()
        self.studio = self._add("Glass Meridian", "Latitude", "Latitude")
        self.live = self._add("Glass Meridian", "Live at the Harbour", "Latitude")
        self.best = self._add("Glass Meridian", "The Best Of", "Latitude")

    def tearDown(self):
        self.conn.close()

    def _add(self, artist, album, title, relative_path=None):
        relative_path = relative_path or f"{artist}/{album}/{title}.flac"
        return self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title) VALUES (?, ?, ?, ?)",
            (relative_path, artist, album, title)).lastrowid

    def test_the_entrys_album_picks_the_version(self):
        self.assertEqual(matching.match_playlist_track(self.conn, "Glass Meridian", "Latitude",
                                                       "Live at the Harbour"), self.live)
        self.assertEqual(matching.match_playlist_track(self.conn, "Glass Meridian", "Latitude",
                                                       "the best of"), self.best, "album is normalised")

    def test_no_album_or_an_unknown_one_keeps_the_first_match(self):
        for album in (None, "", "Some Other Album"):
            self.assertEqual(matching.match_playlist_track(self.conn, "Glass Meridian", "Latitude", album),
                             self.studio, album)

    def test_album_breaks_ties_in_the_annotation_stripped_and_close_steps(self):
        self.assertEqual(matching.match_playlist_track(self.conn, "Glass Meridian", "Latitude (Remastered)",
                                                       "The Best Of"), self.best)
        a = self._add("Cinder & Ash", "Emberwake", "night drive")
        b = self._add("Cinder & Ash", "Embers Live", "night drive")
        self.assertEqual(matching.match_playlist_track(self.conn, "Cinder & Ash", "night driv"), a)
        self.assertEqual(matching.match_playlist_track(self.conn, "Cinder & Ash", "night driv", "Embers Live"), b)

    def test_album_never_overrides_a_better_title_match(self):
        other = self._add("Glass Meridian", "Live at the Harbour", "Two Suns")
        self.assertEqual(matching.match_playlist_track(self.conn, "Glass Meridian", "Two Suns", "Latitude"),
                         other)

    def test_a_bare_file_name_shared_by_several_files_is_picked_by_album(self):
        a = self._add("A", "First", "Intro", "A/First/01 Intro.flac")
        b = self._add("A", "Second", "Intro", "A/Second/01 Intro.flac")
        self.assertEqual(matching.match_playlist_track_by_path(self.conn, "01 Intro.flac"), a)
        self.assertEqual(matching.match_playlist_track_by_path(self.conn, "01 Intro.flac", "Second"), b)
        self.assertEqual(matching.match_playlist_track_by_path(self.conn, "/x/A/First/01 Intro.flac", "Second"),
                         a, "a longer path that names one file is not overridden by album")



class CloseMatchNormalisationTests(unittest.TestCase):
    """The close step compares normalised titles on both sides, like the
    exact and annotation-stripped steps before it."""

    def setUp(self):
        self.conn = _conn()
        self.track = _add(self.conn, "Glass Meridian", "Latitude")

    def tearDown(self):
        self.conn.close()

    def test_a_near_miss_of_a_capitalised_title_matches(self):
        # 0.93 with both sides normalised; 0.80 against "Latitude" as stored.
        self.assertEqual(matching.match_playlist_track(self.conn, "Glass Meridian", "Latitud"), self.track)

    def test_an_accented_capital_is_case_folded_too(self):
        # normalize() folds case, accented capitals included; it doesn't
        # strip accents, on either side.
        accented = _add(self.conn, "Ondine Laroche", "Été Indien")
        self.assertEqual(matching.match_playlist_track(self.conn, "Ondine Laroche", "été indie"), accented)

    def test_the_cutoff_still_holds(self):
        self.assertIsNone(matching.match_playlist_track(self.conn, "Glass Meridian", "Longitude"))

    def test_two_different_songs_a_few_letters_apart_do_not_match(self):
        # "copper days" against "paper days" scores 0.857: different songs,
        # and a wrong match is worse than a miss.
        _add(self.conn, "Marigold Avenue", "Paper Days")
        self.assertIsNone(matching.match_playlist_track(self.conn, "Marigold Avenue", "Copper Days"))


# The trailing-annotation expression _strip_annotations used to apply in a
# loop. On a long title of "(" or spaces it was quadratic, so the scan that
# replaced it is checked here against it, on short titles only.
_OLD_ANNOTATION = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$")


def _old_strip_annotations(title: str) -> str:
    prev = title
    while True:
        stripped = _OLD_ANNOTATION.sub("", prev).strip()
        if stripped == prev:
            return prev
        prev = stripped


class StripAnnotationsTests(unittest.TestCase):
    def test_trailing_annotations_go(self):
        cases = {
            "Copper Days (2011 Remaster)": "Copper Days",
            "Copper Days (Live) [Remastered]": "Copper Days",
            "Copper Days [Demo)": "Copper Days",
            "Copper Days (Live) - Encore": "Copper Days (Live) - Encore",
            "(Intro)": "",
            "  Copper Days  ": "Copper Days",
            "Copper Days (Live": "Copper Days (Live",
            "Copper Days (a (b)": "Copper Days",
            "Copper Days (a) (b) \t": "Copper Days",
            "": "",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(matching._strip_annotations(title), expected)

    def test_same_result_as_the_expression_it_replaced(self):
        rng = random.Random(572)
        alphabet = "ab ([)]\t\u00a0"
        for _ in range(20000):
            title = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
            with self.subTest(title=title):
                self.assertEqual(matching._strip_annotations(title), _old_strip_annotations(title))

    def test_a_long_title_is_linear(self):
        # The old expression took about 14 s on the first of these.
        n = 50_000
        for name, title in (("open parens", "(" * n), ("spaces", " " * n + "x"),
                            ("open then spaces", "(" + " " * n + "x"),
                            ("many annotations", "x" + "(y)" * (n // 3))):
            with self.subTest(title=name):
                start = time.perf_counter()
                matching._strip_annotations(title)
                self.assertLess(time.perf_counter() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
