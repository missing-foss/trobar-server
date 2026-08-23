#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for filesystem_client.py's iTunes Library.xml merge (#171) — its
own .m3u discovery predates this file and has no tests of its own; these
cover only the new merge behavior. Config persistence goes through a real
temp-file SQLite DB (db.get_conn() opens by DB_PATH internally, so an
in-memory connection passed in wouldn't be reachable from inside the
module) rather than mocking db itself.

    python3 -m unittest test_filesystem_client -v
"""
import os
import plistlib
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="trobar-test-filesystem-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import filesystem_client  # noqa: E402


def _write_library(xml_path: Path, tracks: dict, playlists: list) -> None:
    with xml_path.open("wb") as f:
        plistlib.dump({"Tracks": tracks, "Playlists": playlists}, f)


class _FilesystemClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

        self._music_dir = tempfile.TemporaryDirectory(dir=_TMP)
        self.root = Path(self._music_dir.name)
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self.root))
        conn.commit()
        conn.close()

    def tearDown(self):
        self._music_dir.cleanup()
        self._db_path.unlink(missing_ok=True)

    def _set_itunes_path(self, path: str | None):
        conn = db.get_conn()
        db.set_config(conn, "itunes_library_path", path)
        conn.commit()
        conn.close()


class ListPlaylistsMergeTests(_FilesystemClientTestBase):
    def test_no_itunes_path_configured_yields_only_m3u_playlists(self):
        (self.root / "mix.m3u").write_text("#EXTM3U\n/music/a.mp3\n", encoding="utf-8")
        result = filesystem_client.list_playlists()
        self.assertEqual(result["playlists"], [{"id": "mix", "title": "mix"}])

    def test_itunes_playlists_are_appended_with_prefixed_id(self):
        xml_path = self.root / "Library.xml"
        _write_library(
            xml_path,
            tracks={"1": {"Track ID": 1, "Name": "Song", "Location": "file:///music/a.mp3"}},
            playlists=[{"Name": "Road Trip", "Playlist Persistent ID": "AAAA1111",
                         "Playlist Items": [{"Track ID": 1}]}],
        )
        self._set_itunes_path(str(xml_path))
        result = filesystem_client.list_playlists()
        self.assertEqual(result["playlists"], [{"id": "itunes:AAAA1111", "title": "Road Trip"}])

    def test_m3u_and_itunes_playlists_coexist(self):
        (self.root / "mix.m3u").write_text("#EXTM3U\n/music/a.mp3\n", encoding="utf-8")
        xml_path = self.root / "Library.xml"
        _write_library(
            xml_path, tracks={},
            playlists=[{"Name": "Road Trip", "Playlist Persistent ID": "AAAA1111", "Playlist Items": []}],
        )
        self._set_itunes_path(str(xml_path))
        result = filesystem_client.list_playlists()
        ids = {p["id"] for p in result["playlists"]}
        self.assertEqual(ids, {"mix", "itunes:AAAA1111"})


class GetPlaylistTracksMergeTests(_FilesystemClientTestBase):
    def setUp(self):
        super().setUp()
        self.xml_path = self.root / "Library.xml"
        _write_library(
            self.xml_path,
            tracks={"1": {"Track ID": 1, "Name": "Song", "Artist": "Artist", "Album": "Album",
                          "Location": f"file://{self.root}/Artist/Song.mp3"}},
            playlists=[{"Name": "Road Trip", "Playlist Persistent ID": "AAAA1111",
                         "Playlist Items": [{"Track ID": 1}]}],
        )
        self._set_itunes_path(str(self.xml_path))

    def test_fetches_by_prefixed_source_id(self):
        result = filesystem_client.get_playlist_tracks("Road Trip", source_playlist_id="itunes:AAAA1111")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tracks"], [{
            "position": 0, "title": "Song", "artist": "Artist", "path": "Artist/Song.mp3", "album": "Album",
        }])

    def test_unknown_prefixed_id_reports_not_found(self):
        result = filesystem_client.get_playlist_tracks("Road Trip", source_playlist_id="itunes:NOPE")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Road Trip"})

    def test_prefixed_id_with_no_configured_path_reports_not_paired(self):
        self._set_itunes_path(None)
        result = filesystem_client.get_playlist_tracks("Road Trip", source_playlist_id="itunes:AAAA1111")
        self.assertEqual(result, {"status": "error", "reason": "not_paired"})

    def test_falls_back_to_itunes_title_match_when_no_id_and_no_m3u_match(self):
        result = filesystem_client.get_playlist_tracks("Road Trip")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["tracks"]), 1)

    def test_m3u_file_takes_precedence_over_itunes_title_when_no_id_given(self):
        (self.root / "Road Trip.m3u").write_text("#EXTM3U\n/music/other.mp3\n", encoding="utf-8")
        result = filesystem_client.get_playlist_tracks("Road Trip")
        self.assertEqual(result["tracks"][0]["path"], "/music/other.mp3")


if __name__ == "__main__":
    unittest.main()
