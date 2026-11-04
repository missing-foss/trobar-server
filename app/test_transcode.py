#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for app/transcode.py and the transcode-decision logic in
sync_state.py it depends on. Uses only the standard library (unittest) —
no new dependency, and it can be run without Flask/flask-babel installed
(this module and sync_state.py are themselves stdlib-only):

    python3 -m unittest app.test_transcode -v      # from repo root
    python3 -m unittest test_transcode -v          # from app/

There's no automated test suite elsewhere in this repo (see
CONTRIBUTING.md — changes are verified against a running instance); this
file covers only the two things too easy to silently regress here: the
transcode-eligibility decision (same source of truth /api/device/changes
and /api/device/file/<id> must agree on), the concurrency gate (wrong
by one and either transcoding is unlimited or the app deadlocks), and the
cache of finished transcodes (what it keeps, and what it evicts first).
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import sync_state
import transcode


def _row(relative_path: str, **extra) -> sqlite3.Row:
    """A sqlite3.Row with relative_path plus whatever other columns a
    caller needs (device_path() also reads track_no/disc_no/artist/
    album_artist/album/title) — built via a real query since sqlite3.Row
    can't be constructed directly."""
    # lossless None: not yet known, so the extension decides (a library from
    # before tracks.lossless); tests of the column pass it explicitly.
    # album_artist None: not read yet, so the folder is the track's artist.
    columns = {"relative_path": relative_path, "lossless": None, "album_artist": None, **extra}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    placeholders = ", ".join(f"? AS {name}" for name in columns)
    row = conn.execute(f"SELECT {placeholders}", list(columns.values())).fetchone()
    conn.close()
    return row


class WantsTranscodeTests(unittest.TestCase):
    def test_lossless_source_with_format_wants_transcode(self):
        for ext in ("flac", "wav", "aiff", "aif", "FLAC"):  # case-insensitive
            with self.subTest(ext=ext):
                self.assertTrue(
                    sync_state.wants_transcode(_row(f"Artist/Album/01 - Track.{ext}"), "mp3_320")
                )

    def test_lossy_source_never_wants_transcode(self):
        for ext in ("mp3", "m4a", "ogg", "opus"):
            with self.subTest(ext=ext):
                self.assertFalse(
                    sync_state.wants_transcode(_row(f"Artist/Album/01 - Track.{ext}"), "mp3_320")
                )

    def test_an_m4a_is_transcoded_when_the_scan_found_alac(self):
        alac = _row("Artist/Album/01 - Track.m4a", lossless=1)
        aac = _row("Artist/Album/01 - Track.m4a", lossless=0)
        unknown = _row("Artist/Album/01 - Track.m4a")
        self.assertEqual([sync_state.wants_transcode(r, "mp3_320") for r in (alac, aac, unknown)],
                         [True, False, False])

    def test_an_alac_m4a_is_named_mp3_on_a_transcoding_device(self):
        row = _row("Artist/Album/01 - Track.m4a", lossless=1, artist="A", album="B", title="T",
                   track_no=1, disc_no=None)
        self.assertEqual(sync_state.device_path(row, "mp3_320"), "A/B/01 - T.mp3")
        self.assertEqual(sync_state.device_path(row, None), "A/B/01 - T.m4a")

    def test_no_format_means_originals(self):
        self.assertFalse(sync_state.wants_transcode(_row("Artist/Album/01 - Track.flac"), None))

    def test_unknown_format_string_is_not_a_crash(self):
        # Defensive: an unvalidated/garbage transcode_format must not raise,
        # just resolve to "don't transcode" like None does.
        self.assertFalse(sync_state.wants_transcode(_row("Artist/Album/01 - Track.flac"), "bogus"))

    def test_device_path_extension_matches_wants_transcode(self):
        # The two must never disagree — /api/device/changes names the file
        # with the target extension exactly when the download endpoint
        # would also decide to transcode it.
        row = _row("Artist/Album/01 - Track.flac", artist="Artist", album="Album",
                    title="Track", track_no=1, disc_no=None)
        self.assertTrue(sync_state.wants_transcode(row, "mp3_256"))
        self.assertTrue(sync_state.device_path(row, "mp3_256").endswith(".mp3"))


class BitrateMapTests(unittest.TestCase):
    def test_covers_every_format_sync_state_knows_about(self):
        # transcode.BITRATES and sync_state.TRANSCODE_FORMATS must never
        # drift apart — a format sync_state considers valid but transcode.py
        # has no bitrate for would 500 at download time.
        self.assertEqual(set(transcode.BITRATES), set(sync_state.TRANSCODE_FORMATS))

    def test_values_are_ffmpeg_bitrate_syntax(self):
        for fmt, bitrate in transcode.BITRATES.items():
            with self.subTest(fmt=fmt):
                self.assertRegex(bitrate, r"^\d+k$")


class ConcurrencyGateTests(unittest.TestCase):
    def setUp(self):
        # The gate is module-level global state — reset between tests.
        transcode._active = 0

    def test_acquire_blocks_at_limit_and_release_frees_a_slot(self):
        transcode.acquire_slot(1)
        self.assertEqual(transcode._active, 1)

        acquired = threading.Event()

        def acquire_then_signal():
            transcode.acquire_slot(1)
            acquired.set()

        t = threading.Thread(target=acquire_then_signal)
        t.start()
        # With the limit already held, the second acquire must not proceed.
        self.assertFalse(acquired.wait(timeout=0.2))

        transcode.release_slot()
        self.assertTrue(acquired.wait(timeout=1), "second acquire never unblocked after release")
        t.join(timeout=1)
        self.assertEqual(transcode._active, 1)  # first released, second now holds it

    def test_limit_of_two_allows_two_concurrent_holders(self):
        transcode.acquire_slot(2)
        transcode.acquire_slot(2)
        self.assertEqual(transcode._active, 2)
        transcode.release_slot()
        transcode.release_slot()
        self.assertEqual(transcode._active, 0)

    def test_zero_or_negative_limit_still_allows_one(self):
        # max(1, limit) in acquire_slot — an admin fat-fingering 0 must not
        # wedge transcoding entirely.
        transcode.acquire_slot(0)
        self.assertEqual(transcode._active, 1)
        transcode.release_slot()


class StartCommandTests(unittest.TestCase):
    def test_builds_expected_ffmpeg_invocation(self):
        # Mirrors trobar-desktop's lib/transcoder.dart flag-for-flag —
        # verifies the constructed argv, not a real ffmpeg run. Output
        # goes to a real temp file (trobar-server#223), not pipe:1.
        import unittest.mock as mock

        with mock.patch("subprocess.Popen") as popen:
            popen.return_value = mock.Mock()
            proc, out_path, err_path = transcode.start("/music/a.flac", "mp3_320", 10)
            try:
                (cmd,), kwargs = popen.call_args
                self.assertEqual(cmd, [
                    "nice", "-n", "10",
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", "/music/a.flac",
                    "-map", "0:a:0", "-map", "0:v:0?",
                    "-c:a", "libmp3lame", "-b:a", "320k",
                    "-c:v", "copy",
                    "-map_metadata", "0",
                    "-id3v2_version", "3",
                    "-f", "mp3", out_path,
                ])
                self.assertTrue(out_path.endswith(".mp3"))
                self.assertTrue(os.path.isfile(out_path))
                self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
                # A real fd, not subprocess.PIPE — a review of an earlier
                # version caught that an unread PIPE can deadlock (ffmpeg
                # blocks writing to a full pipe buffer, proc.wait() never
                # returns, the concurrency slot never frees). A file can't
                # fill up like that.
                self.assertIsInstance(kwargs["stderr"], int)
                self.assertNotEqual(kwargs["stderr"], subprocess.PIPE)
                self.assertTrue(err_path.endswith(".log"))
                self.assertTrue(os.path.isfile(err_path))
            finally:
                os.remove(out_path)
                os.remove(err_path)

    def test_missing_binary_raises_transcode_start_error_and_cleans_up_temp_files(self):
        import unittest.mock as mock

        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError("no ffmpeg")):
            with self.assertRaises(transcode.TranscodeStartError):
                transcode.start("/music/a.flac", "mp3_320", 10)
        # No leaked temp files: mkstemp's own paths were captured internally
        # and removed on the Popen failure path, not left behind.
        leftovers = [f for f in os.listdir(tempfile.gettempdir())
                     if f.startswith("trobar-transcode-")]
        self.assertEqual(leftovers, [])


class CacheTests(unittest.TestCase):
    """The on-disk cache of finished transcodes: named by track, source
    modification time and format, evicted least recently used first."""

    def setUp(self):
        transcode._active = 0
        self.data = Path(tempfile.mkdtemp(prefix="trobar-test-transcode-cache-"))
        self.addCleanup(shutil.rmtree, self.data, True)
        self.dir = transcode.cache_dir(self.data)
        self.dir.mkdir()

    def _file(self, name, size, age_seconds):
        path = self.dir / name
        path.write_bytes(b"x" * size)
        when = time.time() - age_seconds
        os.utime(path, (when, when))
        return path

    def _names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_the_name_carries_the_track_the_source_time_and_the_format(self):
        self.assertEqual(transcode.cache_path(self.data, 7, 123, "mp3_192"),
                         self.data / "transcode" / "7-123-mp3_192.mp3")

    def test_a_hit_marks_the_file_used_and_a_miss_is_false(self):
        path = self._file("1-1-mp3_320.mp3", 10, age_seconds=3600)
        mtime = path.stat().st_mtime_ns
        self.assertTrue(transcode.cache_hit(path))
        self.assertGreater(path.stat().st_atime, time.time() - 60)
        # the ETag and Last-Modified a client may have kept stay valid
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertFalse(transcode.cache_hit(self.dir / "2-1-mp3_320.mp3"))

    def test_eviction_removes_the_least_recently_used_first(self):
        self._file("old.mp3", 10, age_seconds=300)
        self._file("mid.mp3", 10, age_seconds=200)
        self._file("new.mp3", 10, age_seconds=100)
        # written longest ago, but just served: it stays
        transcode.cache_hit(self._file("used.mp3", 10, age_seconds=900))
        transcode.evict(self.dir, 30)
        self.assertEqual(self._names(), ["mid.mp3", "new.mp3", "used.mp3"])
        transcode.evict(self.dir, 30)  # already within the cap: nothing more
        self.assertEqual(self._names(), ["mid.mp3", "new.mp3", "used.mp3"])

    def test_a_cap_of_zero_removes_every_transcode(self):
        self._file("a.mp3", 10, age_seconds=10)
        self._file("b.mp3", 10, age_seconds=10)
        transcode.evict(self.dir, 0)
        self.assertEqual(self._names(), [])

    def test_the_file_about_to_be_served_is_never_evicted(self):
        self._file("old.mp3", 10, age_seconds=300)
        spare = self._file("big.mp3", 50, age_seconds=1000)
        transcode.evict(self.dir, 20, spare=spare)
        self.assertEqual(self._names(), ["big.mp3"])

    def test_a_crash_leftover_goes_but_an_encode_in_progress_stays(self):
        stale = 2 * transcode.ENCODE_TIMEOUT_SECONDS + 60
        self._file("trobar-transcode-old.mp3", 10, age_seconds=stale)
        self._file("trobar-transcode-now.mp3", 10, age_seconds=5)
        self._file("notes.txt", 10, age_seconds=stale)
        transcode.evict(self.dir, 0)
        self.assertEqual(self._names(), ["notes.txt", "trobar-transcode-now.mp3"])

    def test_keep_moves_the_encode_in_removes_the_log_and_frees_the_slot(self):
        self.assertTrue(transcode.acquire_slot(1))
        fd, out_path = tempfile.mkstemp(dir=self.dir, prefix="trobar-transcode-")
        os.write(fd, b"ID3")
        os.close(fd)
        fd, err_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        final = self.dir / "3-9-mp3_320.mp3"
        self.assertTrue(transcode.keep(out_path, err_path, final, 2 * 1024 ** 3))
        self.assertEqual(final.read_bytes(), b"ID3")
        self.assertFalse(os.path.exists(out_path))
        self.assertFalse(os.path.exists(err_path))
        self.assertEqual(transcode._active, 0)

    def test_keeping_a_new_encode_evicts_older_ones_down_to_the_cap(self):
        self._file("old.mp3", 10, age_seconds=300)
        self.assertTrue(transcode.acquire_slot(1))
        fd, out_path = tempfile.mkstemp(dir=self.dir, prefix="trobar-transcode-")
        os.write(fd, b"y" * 10)
        os.close(fd)
        fd, err_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        self.assertTrue(transcode.keep(out_path, err_path, self.dir / "new.mp3", 15))
        self.assertEqual(self._names(), ["new.mp3"])

    def test_a_failed_keep_leaves_the_encode_and_the_slot_to_the_caller(self):
        self.assertTrue(transcode.acquire_slot(1))
        fd, out_path = tempfile.mkstemp(dir=self.dir, prefix="trobar-transcode-")
        os.close(fd)
        fd, err_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        self.addCleanup(os.remove, err_path)
        with self.assertLogs("transcode", level="WARNING"):
            kept = transcode.keep(out_path, err_path, self.dir / "missing" / "x.mp3", 10)
        self.assertFalse(kept)
        self.assertTrue(os.path.exists(out_path))
        self.assertTrue(os.path.exists(err_path))
        self.assertEqual(transcode._active, 1)
        transcode.release_slot()

    def test_start_writes_into_the_cache_folder_when_given_one(self):
        import unittest.mock as mock
        target = self.dir / "sub"
        with mock.patch("subprocess.Popen"):
            _proc, out_path, err_path = transcode.start("/music/a.flac", "mp3_320", 10, out_dir=target)
        self.addCleanup(os.remove, err_path)
        self.assertEqual(Path(out_path).parent, target)
        self.assertTrue(Path(out_path).name.startswith("trobar-transcode-"))
        self.assertNotEqual(Path(err_path).parent, target)


class WaitForAndIterFileTests(unittest.TestCase):
    """wait_for() decides, before a byte is sent, whether the encode
    succeeded; iter_file() streams a finished one. Every path releases the
    slot and removes both temp files."""

    def setUp(self):
        transcode._active = 0

    def _temp_file(self, content: bytes, suffix: str = ".mp3") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="trobar-transcode-test-")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        return path

    def _proc(self, returncode=0):
        import unittest.mock as mock
        proc = mock.Mock()
        proc.wait.return_value = None
        proc.returncode = returncode
        return proc

    def _gone(self, *paths):
        for path in paths:
            self.assertFalse(os.path.exists(path), path)

    def test_a_finished_encode_is_streamed_then_cleaned_up(self):
        out_path, err_path = self._temp_file(b"abcdef"), self._temp_file(b"", ".log")
        proc = self._proc(0)
        self.assertTrue(transcode.acquire_slot(1))
        self.assertTrue(transcode.wait_for(proc, out_path, err_path))
        self.assertEqual(transcode._active, 1, "held until the file has been sent")
        self.assertEqual(list(transcode.iter_file(out_path, err_path)), [b"abcdef"])
        self.assertEqual(transcode._active, 0)
        proc.kill.assert_not_called()
        self._gone(out_path, err_path)

    def test_a_failed_encode_is_reported_logged_and_cleaned_up_before_anything_is_sent(self):
        out_path = self._temp_file(b"partial-garbage")
        err_path = self._temp_file(b"Invalid data found when processing input\n", ".log")
        transcode.acquire_slot(1)
        with self.assertLogs("transcode", level="WARNING") as log:
            self.assertFalse(transcode.wait_for(self._proc(1), out_path, err_path))
        self.assertIn("Invalid data found when processing input", log.output[0])
        self.assertEqual(transcode._active, 0)
        self._gone(out_path, err_path)

    def test_an_encode_past_its_deadline_is_killed(self):
        out_path, err_path = self._temp_file(b"x"), self._temp_file(b"", ".log")
        proc = self._proc(0)
        proc.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 1), None]
        transcode.acquire_slot(1)
        with self.assertLogs("transcode", level="WARNING"):
            self.assertFalse(transcode.wait_for(proc, out_path, err_path, timeout=1))
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_args_list[0].kwargs, {"timeout": 1})
        self.assertEqual(transcode._active, 0)
        self._gone(out_path, err_path)

    def test_a_client_leaving_mid_stream_still_releases_and_cleans_up(self):
        out_path, err_path = self._temp_file(b"x" * 200_000), self._temp_file(b"", ".log")
        transcode.acquire_slot(1)
        body = transcode.iter_file(out_path, err_path)
        next(iter(body))
        body.close()
        self.assertEqual(transcode._active, 0)
        self._gone(out_path, err_path)

    def test_a_client_leaving_before_the_first_chunk_still_releases_and_cleans_up(self):
        out_path, err_path = self._temp_file(b"x"), self._temp_file(b"", ".log")
        transcode.acquire_slot(1)
        body = transcode.iter_file(out_path, err_path)
        body.close()
        self.assertEqual(transcode._active, 0)
        self._gone(out_path, err_path)

    def test_the_slot_is_released_once_however_often_the_body_is_closed(self):
        out_path, err_path = self._temp_file(b"x"), self._temp_file(b"", ".log")
        transcode.acquire_slot(2)
        transcode.acquire_slot(2)
        body = transcode.iter_file(out_path, err_path)
        list(body)
        body.close()
        self.assertEqual(transcode._active, 1, "the other holder keeps its slot")
        transcode.release_slot()

    def test_waiting_for_a_slot_gives_up_without_taking_one(self):
        self.assertTrue(transcode.acquire_slot(1))
        self.assertFalse(transcode.acquire_slot(1, timeout=0.05))
        self.assertEqual(transcode._active, 1)
        transcode.release_slot()
        self.assertTrue(transcode.acquire_slot(1, timeout=0.05))
        transcode.release_slot()


if __name__ == "__main__":
    unittest.main()
