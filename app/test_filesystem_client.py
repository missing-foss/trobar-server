#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for filesystem_client.py's two secondary playlist sources: the
iTunes Library.xml merge (#171) and the extra playlist folder. Its .m3u
discovery under MUSIC_ROOT predates this file and has no tests of its own,
so what is covered here is the merging, the id namespaces that keep the
three sources apart, and the containment of the lookup by id. Config persistence goes through a real
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

    def _set_extra_folder(self, path: str | None):
        conn = db.get_conn()
        db.set_config(conn, "extra_playlist_folder", path)
        conn.commit()
        conn.close()

    def _make_extra_folder(self):
        """A second root OUTSIDE the music root — which is the whole point
        of the setting, and also what makes the relative-entry case below
        behave differently from a music-root playlist's."""
        d = tempfile.TemporaryDirectory(dir=_TMP)
        self.addCleanup(d.cleanup)
        self._set_extra_folder(d.name)
        return Path(d.name)


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


class ExtraPlaylistFolderListTests(_FilesystemClientTestBase):
    def test_unconfigured_folder_changes_nothing(self):
        (self.root / "mix.m3u").write_text("#EXTM3U\n/music/a.mp3\n", encoding="utf-8")
        self.assertEqual(
            filesystem_client.list_playlists()["playlists"], [{"id": "mix", "title": "mix"}])

    def test_playlists_in_the_extra_folder_are_appended_with_prefixed_id(self):
        extra = self._make_extra_folder()
        (extra / "Chill.m3u").write_text("#EXTM3U\n/music/a.mp3\n", encoding="utf-8")
        self.assertEqual(
            filesystem_client.list_playlists()["playlists"],
            [{"id": "extra:Chill", "title": "Chill"}])

    def test_nested_playlists_keep_their_folder_in_the_title(self):
        extra = self._make_extra_folder()
        (extra / "moods").mkdir()
        (extra / "moods" / "Chill.m3u8").write_text("#EXTM3U\n/music/a.mp3\n", encoding="utf-8")
        self.assertEqual(
            filesystem_client.list_playlists()["playlists"],
            [{"id": "extra:moods/Chill", "title": "moods/Chill"}])

    def test_same_relative_path_in_both_roots_yields_two_distinct_playlists(self):
        """The reason the extra folder needs an id prefix at all: the two
        roots are unrelated directories and may well hold the same file
        name. Same title (they ARE both called that), different ids —
        which is what playlist_sync keys the rows on."""
        extra = self._make_extra_folder()
        (self.root / "Chill.m3u").write_text("#EXTM3U\n/music/a.mp3\n", encoding="utf-8")
        (extra / "Chill.m3u").write_text("#EXTM3U\n/music/b.mp3\n", encoding="utf-8")
        playlists = filesystem_client.list_playlists()["playlists"]
        self.assertEqual([p["id"] for p in playlists], ["Chill", "extra:Chill"])
        self.assertEqual({p["title"] for p in playlists}, {"Chill"})

    def test_a_configured_but_missing_folder_is_not_an_error(self):
        """An unmounted volume or a typo must not stop the music root's own
        playlists from syncing."""
        (self.root / "mix.m3u").write_text("#EXTM3U\n/music/a.mp3\n", encoding="utf-8")
        self._set_extra_folder(str(self.root / "nope" / "gone"))
        result = filesystem_client.list_playlists()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["playlists"], [{"id": "mix", "title": "mix"}])


class M3uEncodingTests(_FilesystemClientTestBase):
    """A plain .m3u declares no encoding. Windows media players write it in
    the local codepage, so an accented path read as UTF-8 comes back full of
    U+FFFD, and the entry can never match a file. .m3u8 is UTF-8 by
    definition, so only .m3u gets a fallback."""

    _LINES = "#EXTM3U\n#EXTINF:180,Édith Piaf - L'hymne à l'amour\n/music/Édith Piaf/01 - L'hymne à l'amour.flac\n"

    def _only_track(self, name):
        result = filesystem_client.get_playlist_tracks(name)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["tracks"]), 1)
        return result["tracks"][0]

    def test_a_windows_codepage_m3u_keeps_its_accents(self):
        (self.root / "Piaf.m3u").write_bytes(self._LINES.encode("cp1252"))
        track = self._only_track("Piaf")
        self.assertEqual(track["path"], "/music/Édith Piaf/01 - L'hymne à l'amour.flac")
        self.assertEqual(track["artist"], "Édith Piaf")
        self.assertEqual(track["title"], "L'hymne à l'amour")

    def test_a_utf8_m3u_is_not_reread_as_the_codepage(self):
        """The control for the fallback: valid UTF-8 must win. Decoding these
        bytes as cp1252 would succeed too, and yield "Ã‰dith"."""
        (self.root / "Piaf.m3u").write_bytes(self._LINES.encode("utf-8"))
        self.assertEqual(self._only_track("Piaf")["artist"], "Édith Piaf")

    def test_a_byte_order_mark_does_not_become_an_entry(self):
        """Some Windows tools prefix a plain .m3u with a UTF-8 BOM. Read as
        bare UTF-8 it survives into the first line, so "#EXTM3U" no longer
        looks like a comment and is taken for a path."""
        (self.root / "Piaf.m3u").write_bytes(self._LINES.encode("utf-8-sig"))
        self.assertEqual(self._only_track("Piaf")["artist"], "Édith Piaf")

    def test_an_m3u8_is_never_guessed_at(self):
        """Codepage bytes in an .m3u8 are a broken file, not a Windows export:
        they are not reinterpreted."""
        (self.root / "Piaf.m3u8").write_bytes(self._LINES.encode("cp1252"))
        self.assertIn("�", self._only_track("Piaf")["artist"])


class ExtraPlaylistFolderTrackTests(_FilesystemClientTestBase):
    def test_absolute_entries_are_passed_through(self):
        extra = self._make_extra_folder()
        (extra / "Chill.m3u").write_text(
            "#EXTM3U\n#EXTINF:210,Artist - Song\n/music/Artist/Song.mp3\n", encoding="utf-8")
        result = filesystem_client.get_playlist_tracks("Chill", source_playlist_id="extra:Chill")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tracks"], [{
            "position": 0, "title": "Song", "artist": "Artist",
            "path": "/music/Artist/Song.mp3", "album": None,
        }])

    def test_relative_entries_resolve_against_the_music_root(self):
        """A relative entry in an extra-folder playlist names music, and
        there is no music beside that file — so it resolves against
        MUSIC_ROOT, giving the same exact-match tracks.relative_path form a
        music-root playlist's relative entry gives. Resolving it beside the
        playlist instead would produce a path pointing into the extra
        folder, matchable only by trailing segment."""
        extra = self._make_extra_folder()
        (extra / "Chill.m3u").write_text("#EXTM3U\nArtist/Song.mp3\n", encoding="utf-8")
        result = filesystem_client.get_playlist_tracks("Chill", source_playlist_id="extra:Chill")
        self.assertEqual(result["tracks"][0]["path"], "Artist/Song.mp3")

    def test_a_music_root_playlist_still_resolves_beside_itself(self):
        """The control for the test above: this is the behaviour the
        entry_base default preserves, and it is the m3u format's own
        meaning of a relative entry."""
        (self.root / "Artist").mkdir()
        (self.root / "Artist" / "Chill.m3u").write_text("#EXTM3U\nSong.mp3\n", encoding="utf-8")
        result = filesystem_client.get_playlist_tracks("Artist/Chill")
        self.assertEqual(result["tracks"][0]["path"], "Artist/Song.mp3")

    def test_unknown_id_reports_not_found(self):
        self._make_extra_folder()
        result = filesystem_client.get_playlist_tracks("Gone", source_playlist_id="extra:Gone")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Gone"})

    def test_prefixed_id_with_no_configured_folder_reports_not_found(self):
        result = filesystem_client.get_playlist_tracks("Chill", source_playlist_id="extra:Chill")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Chill"})

    def test_prefixed_id_never_falls_back_to_the_music_root(self):
        """A same-named file in the music root must not answer a lookup
        addressed to the extra folder — that would silently return the
        wrong playlist's tracks rather than a miss."""
        self._make_extra_folder()
        (self.root / "Chill.m3u").write_text("#EXTM3U\n/music/a.mp3\n", encoding="utf-8")
        result = filesystem_client.get_playlist_tracks("Chill", source_playlist_id="extra:Chill")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Chill"})

    def test_a_traversing_id_reads_nothing_outside_its_root(self):
        """Defence in depth: no caller supplies an id from a request today.
        The control is the pair — the same file IS readable by its
        legitimate id from the root it belongs to."""
        extra = self._make_extra_folder()
        (self.root / "Secret.m3u").write_text("#EXTM3U\n/music/secret.mp3\n", encoding="utf-8")
        escaping = f"extra:../{self.root.name}/Secret"
        self.assertEqual(
            filesystem_client.get_playlist_tracks("Secret", source_playlist_id=escaping),
            {"status": "not_found", "failed_segment": "Secret"})
        # control: reachable by the id it actually has
        self.assertEqual(
            filesystem_client.get_playlist_tracks("Secret", source_playlist_id="Secret")["tracks"],
            [{"position": 0, "title": "secret", "artist": "", "path": "/music/secret.mp3",
              "album": None}])
        self.assertTrue((extra).is_dir())


if __name__ == "__main__":
    unittest.main()
