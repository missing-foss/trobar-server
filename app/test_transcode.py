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
and /api/device/file/<id> must agree on) and the concurrency gate (wrong
by one and either transcoding is unlimited or the app deadlocks).
"""
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest

import sync_state
import transcode


def _row(relative_path: str, **extra) -> sqlite3.Row:
    """A sqlite3.Row with relative_path plus whatever other columns a
    caller needs (device_path() also reads track_no/disc_no/artist/album/
    title) — built via a real query since sqlite3.Row can't be constructed
    directly."""
    columns = {"relative_path": relative_path, **extra}
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


class IterOutputTests(unittest.TestCase):
    def setUp(self):
        transcode._active = 0

    def _temp_file(self, content: bytes, suffix: str = ".mp3") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="trobar-transcode-test-")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        return path

    def test_streams_finished_file_and_releases_slot_on_completion(self):
        import unittest.mock as mock

        out_path = self._temp_file(b"abcdef")
        err_path = self._temp_file(b"", suffix=".log")
        proc = mock.Mock()
        proc.wait.return_value = None
        proc.returncode = 0
        proc.poll.return_value = 0  # already exited by the time we're done

        transcode.acquire_slot(1)
        chunks = list(transcode.iter_output(proc, out_path, err_path))

        self.assertEqual(chunks, [b"abcdef"])
        self.assertEqual(transcode._active, 0)  # slot released
        proc.kill.assert_not_called()  # already exited, no need to kill
        self.assertFalse(os.path.exists(out_path))  # temp files cleaned up
        self.assertFalse(os.path.exists(err_path))

    def test_nonzero_exit_yields_nothing_logs_stderr_and_still_cleans_up(self):
        # ffmpeg failed partway through (e.g. bad source file) — no
        # partial/corrupt file should ever reach the client, and the
        # reason should actually be discoverable somewhere (a returncode
        # != 0 used to yield nothing with zero logged information).
        import unittest.mock as mock

        out_path = self._temp_file(b"partial-garbage")
        err_path = self._temp_file(b"Invalid data found when processing input\n", suffix=".log")
        proc = mock.Mock()
        proc.wait.return_value = None
        proc.returncode = 1
        proc.poll.return_value = 1

        transcode.acquire_slot(1)
        with self.assertLogs("transcode", level="WARNING") as log:
            chunks = list(transcode.iter_output(proc, out_path, err_path))

        self.assertEqual(chunks, [])
        self.assertEqual(transcode._active, 0)
        self.assertFalse(os.path.exists(out_path))
        self.assertFalse(os.path.exists(err_path))
        self.assertIn("Invalid data found when processing input", log.output[0])

    def test_early_close_kills_process_and_releases_slot(self):
        # A client disconnecting while we're still blocked in proc.wait()
        # has no yield point to interrupt at — this simulates the only
        # place close() can actually land: after the first chunk, while
        # reading the rest of the (large) finished file.
        import unittest.mock as mock

        out_path = self._temp_file(b"x" * 200_000)
        err_path = self._temp_file(b"", suffix=".log")
        proc = mock.Mock()
        proc.wait.return_value = None
        proc.returncode = 0
        proc.poll.return_value = None  # "still running" for the finally check

        transcode.acquire_slot(1)
        gen = transcode.iter_output(proc, out_path, err_path)
        next(gen)  # pull one chunk to actually start the generator
        gen.close()  # simulate the client disconnecting mid-stream

        proc.kill.assert_called_once()
        self.assertEqual(transcode._active, 0)
        self.assertFalse(os.path.exists(out_path))
        self.assertFalse(os.path.exists(err_path))


if __name__ == "__main__":
    unittest.main()
