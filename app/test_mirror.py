#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for mirror.py's playlist-mirroring MVP (#285): filename
computation, marker-safety (never clobber a non-Trobar file), idempotent
rewrite, and the admin-error-surfacing failure modes. Real filesystem I/O
against a tmp dir (mirror.py writes real files) — same DATA_DIR/DB_PATH
override pattern as test_scanner.py's ScanLibraryIsrcTests, since
db.get_mirror_folder()/get_music_root() open their own connections.

    python3 -m unittest test_mirror -v      # from app/
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import db
import mirror
import sync_state


class _MirrorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-mirror-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore_db_globals)

        self.music_root = Path(self._tmp) / "music"
        self.mirror_folder = Path(self._tmp) / "mirrors"
        self.music_root.mkdir()
        # mirror_folder is NOT created here on purpose — write_mirror must
        # create it itself (mkdir(parents=True, exist_ok=True)).

        self.conn = db.get_conn()
        db.set_config(self.conn, "music_root", str(self.music_root))
        db.set_config(self.conn, "mirror_folder", str(self.mirror_folder))
        self.conn.commit()

    def _restore_db_globals(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def _make_playlist(self, title: str, mirror_enabled: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO playlists (title, mirror_enabled) VALUES (?, ?)",
            (title, 1 if mirror_enabled else 0),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _make_track(self, artist: str, title: str, relative_path: str, duration: float = 180.0) -> int:
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, duration) "
            "VALUES (?, ?, 'Album', ?, 1, 0.0, ?)",
            (relative_path, artist, title, duration),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _add_playlist_track(self, playlist_id: int, position: int, track_id: int | None,
                            artist: str = "A", title: str = "T") -> None:
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (playlist_id, position, artist, title, track_id),
        )
        self.conn.commit()

    def _row(self, playlist_id: int):
        return self.conn.execute(
            "SELECT mirror_enabled, mirror_filename, mirror_last_written_at, "
            "mirror_last_error, mirror_last_error_code "
            "FROM playlists WHERE id = ?", (playlist_id,),
        ).fetchone()


class NoOpTests(_MirrorTestBase):
    def test_disabled_playlist_is_a_no_op(self):
        pid = self._make_playlist("Chill", mirror_enabled=False)
        mirror.write_mirror(self.conn, pid)
        self.assertIsNone(self._row(pid)["mirror_filename"])
        self.assertEqual(list(self.mirror_folder.glob("*")) if self.mirror_folder.exists() else [], [])

    def test_no_mirror_folder_configured_sets_an_error(self):
        db.set_config(self.conn, "mirror_folder", None)
        self.conn.commit()
        pid = self._make_playlist("Chill")
        mirror.write_mirror(self.conn, pid)
        row = self._row(pid)
        self.assertIsNone(row["mirror_filename"])
        # #428: fully translatable client-side, so no detail is stored --
        # unlike every other error code, which pairs a translated prefix
        # with an untranslatable detail (an OS exception's text, or a
        # filename).
        self.assertEqual(row["mirror_last_error_code"], "unset_folder")
        self.assertIsNone(row["mirror_last_error"])


class WriteContentTests(_MirrorTestBase):
    def test_writes_marker_and_resolved_tracks_in_order(self):
        pid = self._make_playlist("Road Trip")
        t1 = self._make_track("Artist A", "Song A", "Artist A/Album/01 Song A.flac", duration=200.0)
        t2 = self._make_track("Artist B", "Song B", "Artist B/Album/01 Song B.flac", duration=150.0)
        self._add_playlist_track(pid, 0, t1, "Artist A", "Song A")
        self._add_playlist_track(pid, 1, t2, "Artist B", "Song B")

        mirror.write_mirror(self.conn, pid)

        row = self._row(pid)
        self.assertIsNotNone(row["mirror_filename"])
        self.assertIsNotNone(row["mirror_last_written_at"])
        self.assertIsNone(row["mirror_last_error"])
        self.assertIsNone(row["mirror_last_error_code"])

        content = (self.mirror_folder / row["mirror_filename"]).read_text(encoding="utf-8")
        lines = content.splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertEqual(lines[1], sync_state.M3U_MARKER)
        self.assertEqual(lines[2], "#PLAYLIST:Road Trip")
        self.assertIn("2 of 2 present", lines[3])
        self.assertEqual(lines[4], "#EXTINF:200,Artist A - Song A")
        self.assertEqual(lines[5], str(self.music_root / "Artist A/Album/01 Song A.flac"))
        self.assertEqual(lines[6], "#EXTINF:150,Artist B - Song B")
        self.assertEqual(lines[7], str(self.music_root / "Artist B/Album/01 Song B.flac"))

    def test_coverage_reflects_unmatched_entries(self):
        pid = self._make_playlist("Partial")
        t1 = self._make_track("A", "T1", "A/Album/1.flac")
        self._add_playlist_track(pid, 0, t1)
        self._add_playlist_track(pid, 1, None, "B", "T2")  # never matched locally

        mirror.write_mirror(self.conn, pid)

        row = self._row(pid)
        content = (self.mirror_folder / row["mirror_filename"]).read_text(encoding="utf-8")
        self.assertIn("1 of 2 present", content)
        # only the matched track's #EXTINF/path pair appears
        self.assertEqual(content.count("#EXTINF"), 1)

    def test_filename_computed_from_sanitized_title(self):
        pid = self._make_playlist("Song Machine: Strange Timez")
        mirror.write_mirror(self.conn, pid)
        row = self._row(pid)
        # fs_segment() turns ": " into " - " first; werkzeug.secure_filename()
        # (applied on top, see MIRROR_SUFFIX's own comment on why the suffix
        # is plain ASCII) then collapses the remaining spaces to '_'.
        self.assertEqual(row["mirror_filename"], "Song_Machine_-_Strange_Timez_Trobar_.m3u")

    def test_two_playlists_with_colliding_titles_disambiguate_by_id(self):
        pid1 = self._make_playlist("Same Name")
        pid2 = self._make_playlist("Same Name")
        mirror.write_mirror(self.conn, pid1)
        mirror.write_mirror(self.conn, pid2)
        row1, row2 = self._row(pid1), self._row(pid2)
        self.assertEqual(row1["mirror_filename"], "Same_Name_Trobar_.m3u")
        # secure_filename() strips the disambiguating parens entirely,
        # leaving the id directly appended after the suffix's own
        # trailing underscore.
        self.assertEqual(row2["mirror_filename"], f"Same_Name_Trobar__{pid2}.m3u")
        self.assertNotEqual(row1["mirror_filename"], row2["mirror_filename"])

    def test_non_latin_title_falls_back_to_an_id_based_filename(self):
        # #294: werkzeug.secure_filename() NFKD-normalizes then drops every
        # non-ASCII codepoint, so a title with no Latin characters at all
        # sanitizes to nothing — without the id fallback every such
        # playlist would collide onto the same bare "_Trobar_.m3u" name.
        pid = self._make_playlist("ロック ベスト")
        mirror.write_mirror(self.conn, pid)
        row = self._row(pid)
        self.assertEqual(row["mirror_filename"], f"playlist-{pid}_Trobar_.m3u")
        # the real title still survives inside the file content itself
        content = (self.mirror_folder / row["mirror_filename"]).read_text(encoding="utf-8")
        self.assertIn("#PLAYLIST:ロック ベスト", content)

    def test_two_non_latin_titles_get_distinct_id_based_filenames(self):
        pid1 = self._make_playlist("Русский рок")
        pid2 = self._make_playlist("中文歌單")
        mirror.write_mirror(self.conn, pid1)
        mirror.write_mirror(self.conn, pid2)
        row1, row2 = self._row(pid1), self._row(pid2)
        self.assertEqual(row1["mirror_filename"], f"playlist-{pid1}_Trobar_.m3u")
        self.assertEqual(row2["mirror_filename"], f"playlist-{pid2}_Trobar_.m3u")
        self.assertNotEqual(row1["mirror_filename"], row2["mirror_filename"])


class IdempotentRewriteTests(_MirrorTestBase):
    def test_second_write_with_unchanged_tracks_produces_identical_content(self):
        pid = self._make_playlist("Stable")
        t1 = self._make_track("A", "T", "A/Album/1.flac")
        self._add_playlist_track(pid, 0, t1)

        mirror.write_mirror(self.conn, pid)
        first_filename = self._row(pid)["mirror_filename"]
        first_content = (self.mirror_folder / first_filename).read_text(encoding="utf-8")

        mirror.write_mirror(self.conn, pid)
        second_row = self._row(pid)
        self.assertEqual(second_row["mirror_filename"], first_filename)
        self.assertIsNone(second_row["mirror_last_error"])
        self.assertIsNone(second_row["mirror_last_error_code"])
        second_content = (self.mirror_folder / first_filename).read_text(encoding="utf-8")
        self.assertEqual(first_content, second_content)

    def test_title_change_deletes_the_old_file_and_writes_the_new_one(self):
        pid = self._make_playlist("Old Title")
        mirror.write_mirror(self.conn, pid)
        old_filename = self._row(pid)["mirror_filename"]
        old_path = self.mirror_folder / old_filename
        self.assertTrue(old_path.exists())

        self.conn.execute("UPDATE playlists SET title = ? WHERE id = ?", ("New Title", pid))
        self.conn.commit()
        mirror.write_mirror(self.conn, pid)

        new_filename = self._row(pid)["mirror_filename"]
        self.assertNotEqual(new_filename, old_filename)
        self.assertFalse(old_path.exists())
        self.assertTrue((self.mirror_folder / new_filename).exists())


class PathContainmentTests(_MirrorTestBase):
    """CodeQL flagged folder/filename joins as path injection (py/path-
    injection) — sync_state.fs_segment() already strips every directory-
    separator character (verified directly: '/', '\\', and all control
    chars become '-'/'_'), and the mandatory MIRROR_SUFFIX+'.m3u' suffix
    means the result can never be a bare '..'/'.' token either, so this
    was a false positive against the real sanitizer. Three fix attempts
    (pathlib resolve()+relative_to(), os.path.basename() alone, then
    os.path.normpath()+startswith()) were all still flagged when
    re-checked against the real CodeQL SARIF taint flow — confirmed after
    each push, not assumed; werkzeug.secure_filename() upstream (see
    _compute_filename) is what actually cleared it. mirror._safe_path()
    also checks the joined path's PARENT is exactly `folder` — a plain
    startswith(folder) containment check accepts any descendant, not just
    a direct child, so it would silently allow 'sub/dir/x.m3u' through
    (unreachable today given secure_filename()'s guarantees, but this
    closes that gap structurally rather than relying on every future
    caller to keep sanitizing upstream). These tests cover it directly,
    and end to end through a hostile title."""

    def test_safe_path_accepts_a_normal_filename(self):
        self.mirror_folder.mkdir()
        result = mirror._safe_path(self.mirror_folder, "Chill_Trobar_.m3u")
        self.assertEqual(result, self.mirror_folder / "Chill_Trobar_.m3u")

    def test_safe_path_rejects_a_traversal_attempt(self):
        self.mirror_folder.mkdir()
        # secure_filename() can never actually produce a string containing
        # '/' (verified separately) — this exercises _safe_path()'s own
        # normpath+dirname guard directly, as if that upstream guarantee
        # had somehow failed.
        self.assertIsNone(mirror._safe_path(self.mirror_folder, "../../outside.m3u"))
        self.assertIsNone(mirror._safe_path(self.mirror_folder, "/etc/passwd"))
        self.assertIsNone(mirror._safe_path(self.mirror_folder, ".."))

    def test_safe_path_rejects_a_nested_subdirectory(self):
        # A plain startswith(folder)-style containment check would accept
        # this (it genuinely does resolve somewhere under folder) — the
        # direct-child check specifically closes that gap.
        self.mirror_folder.mkdir()
        self.assertIsNone(mirror._safe_path(self.mirror_folder, "sub/dir/x.m3u"))

    def test_safe_path_accepts_a_folder_containing_dotdot(self):
        # #294: base_str used to be str(folder) unnormalized, so an
        # admin-configured folder path containing '..' (e.g.
        # '/srv/../srv/mirror') could never equal full_str's own
        # normalized dirname — every single write failed. _safe_path()
        # now normpath()s the base too.
        self.mirror_folder.mkdir()
        dotdot_folder = self.mirror_folder.parent / ".." / self.mirror_folder.parent.name / self.mirror_folder.name
        result = mirror._safe_path(dotdot_folder, "Chill_Trobar_.m3u")
        self.assertEqual(result, self.mirror_folder / "Chill_Trobar_.m3u")

    def test_hostile_title_never_escapes_the_mirror_folder(self):
        # End to end: a playlist titled like a traversal attempt still
        # produces a file safely inside mirror_folder, never outside it.
        pid = self._make_playlist("../../../etc/passwd")
        mirror.write_mirror(self.conn, pid)
        row = self._row(pid)
        self.assertIsNotNone(row["mirror_filename"])
        self.assertNotIn("/", row["mirror_filename"])
        self.assertNotIn("\\", row["mirror_filename"])
        written = self.mirror_folder / row["mirror_filename"]
        self.assertTrue(written.exists())
        self.assertEqual(written.resolve().parent, self.mirror_folder.resolve())


class MarkerSafetyTests(_MirrorTestBase):
    def test_refuses_to_overwrite_a_non_trobar_file(self):
        self.mirror_folder.mkdir()
        pid = self._make_playlist("Chill")
        # Must be the actual filename write_mirror will compute for this
        # title (sanitized title + MIRROR_SUFFIX, passed through
        # werkzeug.secure_filename()) for this test to exercise the real
        # conflict — a mismatched path would just have write_mirror
        # succeed at a different, uncontested filename instead.
        target = self.mirror_folder / "Chill_Trobar_.m3u"
        target.write_text("#EXTM3U\nnot ours\n", encoding="utf-8")

        mirror.write_mirror(self.conn, pid)

        row = self._row(pid)
        self.assertIsNone(row["mirror_filename"])
        self.assertEqual(row["mirror_last_error_code"], "marker_unsafe")
        # #428: the detail is just the computed filename now -- the
        # English sentence around it is the client's job to translate.
        self.assertEqual(row["mirror_last_error"], "Chill_Trobar_.m3u")
        self.assertEqual(target.read_text(encoding="utf-8"), "#EXTM3U\nnot ours\n")  # untouched

    def test_delete_mirror_refuses_to_delete_a_non_trobar_file(self):
        self.mirror_folder.mkdir()
        pid = self._make_playlist("Chill")
        target = self.mirror_folder / "impostor.m3u"
        target.write_text("#EXTM3U\nnot ours\n", encoding="utf-8")
        self.conn.execute(
            "UPDATE playlists SET mirror_filename = ? WHERE id = ?", ("impostor.m3u", pid)
        )
        self.conn.commit()

        mirror.delete_mirror(self.conn, pid)

        self.assertTrue(target.exists())  # never deleted
        # but the DB reference is cleared regardless, so it stops being tracked
        self.assertIsNone(self._row(pid)["mirror_filename"])

    def test_delete_mirror_removes_a_genuinely_trobar_marked_file(self):
        pid = self._make_playlist("Chill")
        mirror.write_mirror(self.conn, pid)
        filename = self._row(pid)["mirror_filename"]
        path = self.mirror_folder / filename
        self.assertTrue(path.exists())

        mirror.delete_mirror(self.conn, pid)

        self.assertFalse(path.exists())
        self.assertIsNone(self._row(pid)["mirror_filename"])

    def test_delete_mirror_with_no_filename_is_a_clean_no_op(self):
        pid = self._make_playlist("Never Written")
        mirror.delete_mirror(self.conn, pid)  # must not raise
        self.assertIsNone(self._row(pid)["mirror_filename"])


class UnwritableFolderTests(_MirrorTestBase):
    def test_unwritable_folder_sets_a_graceful_error(self):
        # A file where a directory is expected — mkdir(parents=True) raises.
        self.mirror_folder.parent.mkdir(parents=True, exist_ok=True)
        self.mirror_folder.write_text("not a directory", encoding="utf-8")
        pid = self._make_playlist("Chill")

        mirror.write_mirror(self.conn, pid)  # must not raise

        row = self._row(pid)
        self.assertIsNone(row["mirror_filename"])
        self.assertEqual(row["mirror_last_error_code"], "not_writable")
        self.assertIsNotNone(row["mirror_last_error"])  # the OSError's own text


if __name__ == "__main__":
    unittest.main()
