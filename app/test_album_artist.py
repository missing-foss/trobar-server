# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The album artist (album_artist.py): the rule on its own, then through
real scans of tagged files, and the startup backfill."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import album_artist
import db
import scanner
from test_scanner import _MP3_SILENCE_FRAME, _id3_frame, _syncsafe


def _mp3(path: Path, artist: str, title: str, album: str, album_artist_tag: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = _id3_frame(b"TPE1", artist) + _id3_frame(b"TIT2", title) + _id3_frame(b"TALB", album)
    if album_artist_tag:
        frames += _id3_frame(b"TPE2", album_artist_tag)
    path.write_bytes(b"ID3" + bytes([3, 0]) + b"\x00" + _syncsafe(len(frames)) + frames + _MP3_SILENCE_FRAME * 5)


def _row(i, artist, tag=None, read=1):
    return {"id": i, "artist": artist, "album_artist_tag": tag, "album_artist_read": read}


class RuleTests(unittest.TestCase):
    def test_a_single_artist_album_is_that_artist(self):
        self.assertEqual(album_artist.effective([_row(1, "Aphelion"), _row(2, "Aphelion")]),
                         {1: "Aphelion", 2: "Aphelion"})

    def test_an_untagged_album_of_several_artists_is_various_artists(self):
        self.assertEqual(set(album_artist.effective([_row(1, "Aphelion"), _row(2, "Glass Meridian")]).values()),
                         {"Various Artists"})

    def test_the_tag_wins_and_a_half_tagged_album_stays_one_album(self):
        out = album_artist.effective([_row(1, "Aphelion", "Night Radio"), _row(2, "Glass Meridian")])
        self.assertEqual(out, {1: "Night Radio", 2: "Night Radio"})

    def test_an_album_with_an_unread_tag_is_not_guessed_a_compilation(self):
        self.assertEqual(album_artist.effective([_row(1, "Aphelion"), _row(2, "Glass Meridian", read=0)]),
                         {1: "Aphelion", 2: "Glass Meridian"})

    def test_an_album_is_its_folder_and_name(self):
        self.assertEqual(album_artist.album_key("A/Night Radio/01.mp3", "Night Radio"), ("A/Night Radio", "night radio"))


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-album-artist-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)
        self.root = Path(self._tmp) / "music"

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._orig

    def _album_artists(self):
        conn = db.get_conn()
        try:
            return {r["title"]: (r["album_artist"], r["album_artist_tag"]) for r in conn.execute(
                "SELECT title, album_artist, album_artist_tag FROM tracks")}
        finally:
            conn.close()

    def test_a_compilation_without_tags_is_various_artists_and_a_tagged_album_keeps_its_tag(self):
        _mp3(self.root / "Night Radio" / "01.mp3", "Aphelion", "One", "Night Radio")
        _mp3(self.root / "Night Radio" / "02.mp3", "Glass Meridian", "Two", "Night Radio")
        _mp3(self.root / "Latitude" / "01.mp3", "Glass Meridian", "Three", "Latitude", "Glass Meridian & Friends")
        scanner.scan_library(self.root)
        self.assertEqual(self._album_artists(), {
            "One": ("Various Artists", None), "Two": ("Various Artists", None),
            "Three": ("Glass Meridian & Friends", "Glass Meridian & Friends")})

    def test_a_track_added_by_another_artist_turns_an_album_into_a_compilation(self):
        _mp3(self.root / "Night Radio" / "01.mp3", "Aphelion", "One", "Night Radio")
        scanner.scan_library(self.root)
        self.assertEqual(self._album_artists()["One"][0], "Aphelion")
        # An incremental scan: "One" is unchanged and not re-read, but its album is.
        _mp3(self.root / "Night Radio" / "02.mp3", "Glass Meridian", "Two", "Night Radio")
        scanner.scan_library(self.root)
        self.assertEqual({v[0] for v in self._album_artists().values()}, {"Various Artists"})

    def test_a_track_retagged_out_of_a_compilation_leaves_a_single_artist_album(self):
        _mp3(self.root / "Night Radio" / "01.mp3", "Aphelion", "One", "Night Radio")
        two = self.root / "Night Radio" / "02.mp3"
        _mp3(two, "Glass Meridian", "Two", "Night Radio")
        scanner.scan_library(self.root)
        self.assertEqual(self._album_artists()["One"][0], "Various Artists")
        # "Two" is retagged into another album; "One" isn't re-read, but the
        # album it was left alone in is recomputed.
        _mp3(two, "Glass Meridian", "Two", "Latitude", "Glass Meridian")
        two.touch()
        scanner.scan_library(self.root)
        self.assertEqual(self._album_artists()["One"][0], "Aphelion")

    def test_files_at_the_root_and_many_albums_at_once(self):
        # Root-level files form their own album; and past the threshold a
        # scan's refresh reads the library once, with the same result.
        _mp3(self.root / "01.mp3", "Aphelion", "One", "Loose")
        _mp3(self.root / "02.mp3", "Glass Meridian", "Two", "Loose")
        _mp3(self.root / "Latitude" / "01.mp3", "Glass Meridian", "Three", "Latitude")
        scanner.scan_library(self.root)
        expected = {"One": "Various Artists", "Two": "Various Artists", "Three": "Glass Meridian"}
        self.assertEqual({k: v[0] for k, v in self._album_artists().items()}, expected)
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tracks SET album_artist = 'stale'")
            many = {("", "loose"), ("Latitude", "latitude")} | {(f"x{i}", "y") for i in range(600)}
            album_artist.refresh(conn, many)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual({k: v[0] for k, v in self._album_artists().items()}, expected)

    def test_an_upgraded_library_reads_the_tags_it_never_read_before_deciding(self):
        # An ordinary album with a "feat." track, tagged with its album artist.
        _mp3(self.root / "Latitude" / "01.mp3", "Glass Meridian", "One", "Latitude", "Glass Meridian")
        _mp3(self.root / "Latitude" / "02.mp3", "Glass Meridian feat. Aphelion", "Two", "Latitude", "Glass Meridian")
        scanner.scan_library(self.root)
        self.assertEqual({v for v in self._album_artists().values()}, {("Glass Meridian", "Glass Meridian")})
        # What a library from before these columns looks like.
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tracks SET album_artist = NULL, album_artist_tag = NULL, album_artist_read = 0")
            conn.commit()
        finally:
            conn.close()
        db.init_db()
        # Not guessed to be a compilation while the tags are unread...
        self.assertNotIn("Various Artists", {v[0] for v in self._album_artists().values()})
        # ...and the next (incremental) scan reads them and decides.
        scanner.scan_library(self.root)
        self.assertEqual({v for v in self._album_artists().values()}, {("Glass Meridian", "Glass Meridian")})

    def test_an_upgraded_compilation_whose_files_changed_is_decided_by_the_full_read(self):
        one, two = self.root / "Night Radio" / "01.mp3", self.root / "Night Radio" / "02.mp3"
        _mp3(one, "Aphelion", "One", "Night Radio")
        _mp3(two, "Glass Meridian", "Two", "Night Radio")
        scanner.scan_library(self.root)
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tracks SET album_artist = NULL, album_artist_read = 0")
            conn.commit()
        finally:
            conn.close()
        db.init_db()
        for path in (one, two):  # both changed: the scan reads them in full
            _mp3(path, *(("Aphelion", "One") if path == one else ("Glass Meridian", "Two")), "Night Radio")
            # Past the scanner's one-second mtime test, so it's a full re-read.
            os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
        scanner.scan_library(self.root)
        self.assertEqual({v[0] for v in self._album_artists().values()}, {"Various Artists"})

    def test_a_library_from_before_the_column_is_filled_at_startup(self):
        _mp3(self.root / "Night Radio" / "01.mp3", "Aphelion", "One", "Night Radio")
        _mp3(self.root / "Night Radio" / "02.mp3", "Glass Meridian", "Two", "Night Radio")
        scanner.scan_library(self.root)
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tracks SET album_artist = NULL")
            conn.commit()
        finally:
            conn.close()
        db.init_db()
        self.assertEqual({v[0] for v in self._album_artists().values()}, {"Various Artists"})


if __name__ == "__main__":
    unittest.main()
