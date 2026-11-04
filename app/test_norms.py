#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""tracks.artist_norm/title_norm: filled at startup, kept by the scanner,
recomputed when normalize() changes, and what the matcher looks up by."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import db
import matching
import scanner
from test_scanner import _make_tagged_mp3


class NormColumnTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-norms-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        self.addCleanup(self._restore)
        db.init_db()
        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._orig

    def _add(self, artist, title, artist_norm=None, title_norm=None):
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, artist_norm, title_norm) "
            "VALUES (?, ?, 'Album', ?, 1, 0, ?, ?)", (f"{artist}/{title}.flac", artist, title, artist_norm, title_norm))
        self.conn.commit()
        return cur.lastrowid

    def _norms(self, track_id):
        row = self.conn.execute("SELECT artist_norm, title_norm FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return row["artist_norm"], row["title_norm"]

    def test_startup_fills_rows_without_norms_and_leaves_the_rest(self):
        empty = self._add("Ondine  LAROCHE", "Été Indien")
        kept = self._add("Glass Meridian", "Latitude", "kept as is", "kept as is")
        db._refresh_norms(self.conn)
        self.assertEqual(self._norms(empty), ("ondine laroche", "été indien"))
        self.assertEqual(self._norms(kept), ("kept as is", "kept as is"))

    def test_a_new_normalize_version_recomputes_every_row(self):
        stale = self._add("Glass Meridian", "Latitude", "stale", "stale")
        db.set_config(self.conn, "tracks_norm_version", str(matching.NORM_VERSION - 1))
        db._refresh_norms(self.conn)
        self.assertEqual(self._norms(stale), ("glass meridian", "latitude"))
        self.assertEqual(db.get_config(self.conn, "tracks_norm_version"), str(matching.NORM_VERSION))

    def test_the_matcher_looks_up_the_stored_norm(self):
        # The stored value is what's looked up: a row whose artist_norm says
        # "glass meridian" is found by that name whatever its artist column
        # holds, and a row with none is found by folding its artist.
        stored = self._add("GM", "Latitude", "glass meridian", "latitude")
        unnormalised = self._add("Cinder & Ash", "Smoulder")
        self.assertEqual(matching.match_playlist_track(self.conn, "Glass Meridian", "Latitude"), stored)
        self.assertEqual(matching.match_playlist_track(self.conn, "CINDER & ASH", "Smoulder"), unnormalised)

    def test_the_scanner_writes_them_and_a_retag_updates_them(self):
        root = Path(self._tmp) / "music"
        (root / "A" / "B").mkdir(parents=True)
        path = root / "A" / "B" / "01.mp3"
        _make_tagged_mp3(path, artist="Ondine LAROCHE", title="Été  Indien")
        scanner.scan_library(root)
        track_id = self.conn.execute("SELECT id FROM tracks").fetchone()["id"]
        self.assertEqual(self._norms(track_id), ("ondine laroche", "été indien"))
        _make_tagged_mp3(path, artist="Glass Meridian", title="Latitude")
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
        scanner.scan_library(root)
        self.assertEqual(self._norms(track_id), ("glass meridian", "latitude"))


if __name__ == "__main__":
    unittest.main()
