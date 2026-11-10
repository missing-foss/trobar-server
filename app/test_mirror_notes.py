#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""mirror_notes (#499): the notes Emby drops are written again once per run,
after its last write, without anyone waiting on it.

    python3 -m unittest test_mirror_notes -v
"""
import threading
import time
import unittest
from unittest import mock

import mirror_notes
import playlist_sync

_SHORT = 0.3


class _Base(unittest.TestCase):
    def setUp(self):
        delay = mock.patch.object(mirror_notes, "DELAY_SECONDS", _SHORT)
        delay.start()
        self.addCleanup(delay.stop)
        self.addCleanup(mirror_notes.wait_for_pending, 5)
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def writer(self, label):
        def write():
            with self.lock:
                self.calls.append(label)
        return write


class RunTests(_Base):
    def test_a_run_writes_its_notes_again_once_after_it_ends(self):
        with mirror_notes.run():
            mirror_notes.note_written("emby", "1", self.writer("a"))
            mirror_notes.note_written("emby", "2", self.writer("b"))
            time.sleep(_SHORT * 1.5)
            self.assertEqual(self.calls, [], "nothing until the run is over")
        self.assertTrue(mirror_notes.wait_for_pending(5))
        self.assertEqual(sorted(self.calls), ["a", "b"])

    def test_the_wait_starts_from_the_last_write_and_does_not_block(self):
        started = time.monotonic()
        with mirror_notes.run():
            mirror_notes.note_written("emby", "1", self.writer("a"))
        self.assertLess(time.monotonic() - started, _SHORT / 2, "the run's caller doesn't wait")
        time.sleep(_SHORT / 3)
        self.assertEqual(self.calls, [])
        self.assertTrue(mirror_notes.wait_for_pending(5))
        self.assertEqual(self.calls, ["a"])

    def test_a_playlist_written_twice_is_written_again_once_with_its_last_note(self):
        with mirror_notes.run():
            mirror_notes.note_written("emby", "1", self.writer("first"))
            mirror_notes.note_written("emby", "1", self.writer("last"))
        mirror_notes.wait_for_pending(5)
        self.assertEqual(self.calls, ["last"])

    def test_a_run_inside_a_run_is_the_same_run(self):
        with mirror_notes.run():
            with mirror_notes.run():
                mirror_notes.note_written("emby", "1", self.writer("a"))
            time.sleep(_SHORT * 1.5)
            self.assertEqual(self.calls, [], "the inner run's end isn't the run's end")
        mirror_notes.wait_for_pending(5)
        self.assertEqual(self.calls, ["a"])

    def test_outside_a_run_a_write_is_a_run_of_its_own(self):
        mirror_notes.note_written("emby", "1", self.writer("a"))
        mirror_notes.wait_for_pending(5)
        self.assertEqual(self.calls, ["a"])

    def test_a_write_again_that_fails_is_logged_and_the_others_still_run(self):
        def boom():
            raise RuntimeError("server gone")
        with self.assertLogs("mirror_notes", level="WARNING"):
            with mirror_notes.run():
                mirror_notes.note_written("emby", "1", boom)
                mirror_notes.note_written("emby", "2", self.writer("b"))
            mirror_notes.wait_for_pending(5)
        self.assertEqual(self.calls, ["b"])


class TwoRunsTests(_Base):
    def test_a_slower_run_does_not_put_an_older_note_back(self):
        # reviewer-bot's probe on #578: a sync writes P, an edit writes P anew,
        # then the sync's last write lands after the edit's.
        edit_done = threading.Event()

        def sync():
            with mirror_notes.run():
                mirror_notes.note_written("emby", "P", self.writer("sync: P old"))
                edit_done.wait(5)
                time.sleep(_SHORT / 2)
                mirror_notes.note_written("emby", "Q", self.writer("sync: Q"))

        thread = threading.Thread(target=sync)
        thread.start()
        time.sleep(_SHORT / 3)
        mirror_notes.note_written("emby", "P", self.writer("edit: P new"))
        edit_done.set()
        thread.join(5)
        self.assertTrue(mirror_notes.wait_for_pending(5))
        self.assertEqual(sorted(self.calls), ["edit: P new", "sync: Q"])


class SyncIsOneRunTests(_Base):
    def test_a_sync_is_one_run(self):
        def sync_two_playlists(provider, provider_id, report=None):
            mirror_notes.note_written("emby", "1", self.writer("a"))
            mirror_notes.note_written("emby", "2", self.writer("b"))
            time.sleep(_SHORT * 1.5)
            with self.lock:
                seen = list(self.calls)
            return {"status": "ok", "seen_during": seen}

        with mock.patch.object(playlist_sync, "_sync_playlists", side_effect=sync_two_playlists):
            result = playlist_sync.sync_playlists(object(), "subsonic")
        self.assertEqual(result["seen_during"], [], "not rewritten while the sync is still running")
        mirror_notes.wait_for_pending(5)
        self.assertEqual(sorted(self.calls), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
