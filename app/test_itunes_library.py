#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for itunes_library.py — pure logic, no network/DB involved.
Builds real plist files with plistlib (both directions of the same
library the module reads) rather than hand-crafted XML strings.

    python3 -m unittest test_itunes_library -v
"""
import plistlib
import tempfile
import unittest
from pathlib import Path


import itunes_library


def _write_library(tmp_path: Path, tracks: dict, playlists: list) -> Path:
    xml_path = tmp_path / "Library.xml"
    with xml_path.open("wb") as f:
        plistlib.dump({"Tracks": tracks, "Playlists": playlists}, f)
    return xml_path


class ParseLibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.root = self.tmp_path / "music"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_empty_list(self):
        result = itunes_library.parse_library(self.tmp_path / "nope.xml", self.root)
        self.assertEqual(result, [])

    def test_not_a_plist_returns_empty_list(self):
        bad = self.tmp_path / "bad.xml"
        bad.write_text("not a plist", encoding="utf-8")
        self.assertEqual(itunes_library.parse_library(bad, self.root), [])

    def test_basic_playlist_with_one_track(self):
        track_path = self.root / "Artist" / "Song.mp3"
        track_path.parent.mkdir(parents=True)
        track_path.touch()
        xml_path = _write_library(
            self.tmp_path,
            tracks={"1": {
                "Track ID": 1, "Name": "Song", "Artist": "Artist",
                "Album": "Album", "Location": f"file://{track_path}",
            }},
            playlists=[{
                "Name": "Road Trip", "Playlist Persistent ID": "AAAA1111",
                "Playlist Items": [{"Track ID": 1}],
            }],
        )
        result = itunes_library.parse_library(xml_path, self.root)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "AAAA1111")
        self.assertEqual(result[0]["title"], "Road Trip")
        self.assertEqual(result[0]["tracks"], [{
            "position": 0, "title": "Song", "artist": "Artist",
            "path": "Artist/Song.mp3", "album": "Album",
        }])

    def test_distinguished_kind_playlists_are_excluded(self):
        xml_path = _write_library(
            self.tmp_path,
            tracks={},
            playlists=[
                {"Name": "Music", "Playlist Persistent ID": "SYS1", "Distinguished Kind": 4, "Playlist Items": []},
                {"Name": "Library", "Playlist Persistent ID": "SYS2", "Master": True, "Playlist Items": []},
                {"Name": "My Mix", "Playlist Persistent ID": "USR1", "Playlist Items": []},
            ],
        )
        result = itunes_library.parse_library(xml_path, self.root)
        self.assertEqual([p["title"] for p in result], ["My Mix"])

    def test_playlist_item_referencing_missing_track_is_skipped(self):
        xml_path = _write_library(
            self.tmp_path,
            tracks={"1": {"Track ID": 1, "Name": "Only Track", "Location": "file:///music/a.mp3"}},
            playlists=[{
                "Name": "Mix", "Playlist Persistent ID": "P1",
                "Playlist Items": [{"Track ID": 1}, {"Track ID": 999}],
            }],
        )
        result = itunes_library.parse_library(xml_path, self.root)
        self.assertEqual(len(result[0]["tracks"]), 1)

    def test_track_with_no_location_is_skipped(self):
        xml_path = _write_library(
            self.tmp_path,
            tracks={"1": {"Track ID": 1, "Name": "Streamed Track"}},
            playlists=[{
                "Name": "Mix", "Playlist Persistent ID": "P1",
                "Playlist Items": [{"Track ID": 1}],
            }],
        )
        result = itunes_library.parse_library(xml_path, self.root)
        self.assertEqual(result[0]["tracks"], [])

    def test_playlist_missing_name_or_id_is_skipped(self):
        xml_path = _write_library(
            self.tmp_path,
            tracks={},
            playlists=[
                {"Playlist Persistent ID": "NOTITLE", "Playlist Items": []},
                {"Name": "NoId", "Playlist Items": []},
            ],
        )
        self.assertEqual(itunes_library.parse_library(xml_path, self.root), [])

    def test_path_outside_music_root_kept_absolute(self):
        xml_path = _write_library(
            self.tmp_path,
            tracks={"1": {
                "Track ID": 1, "Name": "Elsewhere", "Location": "file:///Users/alex/Music/a.mp3",
            }},
            playlists=[{
                "Name": "Mix", "Playlist Persistent ID": "P1",
                "Playlist Items": [{"Track ID": 1}],
            }],
        )
        result = itunes_library.parse_library(xml_path, self.root)
        self.assertEqual(result[0]["tracks"][0]["path"], "/Users/alex/Music/a.mp3")

    def test_windows_style_file_url_is_decoded(self):
        xml_path = _write_library(
            self.tmp_path,
            tracks={"1": {
                "Track ID": 1, "Name": "Windows Track",
                "Location": "file://localhost/C:/Users/alex/Music/a.mp3",
            }},
            playlists=[{
                "Name": "Mix", "Playlist Persistent ID": "P1",
                "Playlist Items": [{"Track ID": 1}],
            }],
        )
        result = itunes_library.parse_library(xml_path, self.root)
        self.assertEqual(result[0]["tracks"][0]["path"], "C:/Users/alex/Music/a.mp3")

    def test_percent_encoded_location_is_decoded(self):
        track_dir = self.root / "Artist"
        track_dir.mkdir()
        (track_dir / "Song (Live).mp3").touch()
        xml_path = _write_library(
            self.tmp_path,
            tracks={"1": {
                "Track ID": 1, "Name": "Song (Live)",
                "Location": f"file://{self.root}/Artist/Song%20(Live).mp3",
            }},
            playlists=[{
                "Name": "Mix", "Playlist Persistent ID": "P1",
                "Playlist Items": [{"Track ID": 1}],
            }],
        )
        result = itunes_library.parse_library(xml_path, self.root)
        self.assertEqual(result[0]["tracks"][0]["path"], "Artist/Song (Live).mp3")

    def test_missing_title_falls_back_to_filename_stem(self):
        xml_path = _write_library(
            self.tmp_path,
            tracks={"1": {"Track ID": 1, "Location": "file:///music/Untitled Track.mp3"}},
            playlists=[{
                "Name": "Mix", "Playlist Persistent ID": "P1",
                "Playlist Items": [{"Track ID": 1}],
            }],
        )
        result = itunes_library.parse_library(xml_path, self.root)
        self.assertEqual(result[0]["tracks"][0]["title"], "Untitled Track")


if __name__ == "__main__":
    unittest.main()
