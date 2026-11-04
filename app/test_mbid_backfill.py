#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one-time recording-id pass over files scanned before tracks.mbid
existed: what it fills, what it leaves, and that it runs once."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import jobs
import mbid_backfill
from test_scanner import _MBID, _make_tagged_flac


class MbidBackfillTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-mbid-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        self.addCleanup(self._restore)
        db.init_db()
        self.root = Path(self._tmp) / "music"
        (self.root / "A" / "B").mkdir(parents=True)
        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)
        db.set_config(self.conn, "music_root", str(self.root))
        self.conn.commit()

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._orig

    def _track(self, name, *, mbid=None, deleted=False):
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, mbid, deleted_at) "
            "VALUES (?, 'A', 'B', ?, 1, 0, ?, ?)",
            (f"A/B/{name}", name, mbid, "now" if deleted else None))
        self.conn.commit()
        return cur.lastrowid

    def _mbid(self, track_id):
        return self.conn.execute("SELECT mbid FROM tracks WHERE id = ?", (track_id,)).fetchone()["mbid"]

    def test_fills_the_ids_files_carry_and_marks_itself_done(self):
        _make_tagged_flac(self.root / "A" / "B" / "tagged.flac", [("MUSICBRAINZ_TRACKID", _MBID)])
        _make_tagged_flac(self.root / "A" / "B" / "plain.flac", [("TITLE", "Plain")])
        tagged = self._track("tagged.flac")
        plain = self._track("plain.flac")
        missing = self._track("missing.flac")
        progress = []
        result = mbid_backfill.run_job(None, lambda done, total=None, label=None: progress.append((done, total)))
        self.assertEqual(result, {"checked": 3, "found": 1, "unreadable": 1})
        self.assertEqual((self._mbid(tagged), self._mbid(plain), self._mbid(missing)), (_MBID, None, None))
        self.assertEqual(progress[-1], (3, 3))
        self.assertTrue(db.get_config(self.conn, mbid_backfill.DONE_KEY))

    def test_leaves_a_known_id_and_a_deleted_track_alone(self):
        other = "11111111-2222-3333-4444-555555555555"
        _make_tagged_flac(self.root / "A" / "B" / "known.flac", [("MUSICBRAINZ_TRACKID", _MBID)])
        _make_tagged_flac(self.root / "A" / "B" / "gone.flac", [("MUSICBRAINZ_TRACKID", _MBID)])
        known = self._track("known.flac", mbid=other)
        gone = self._track("gone.flac", deleted=True)
        result = mbid_backfill.run_job()
        self.assertEqual(result["checked"], 0)
        self.assertEqual((self._mbid(known), self._mbid(gone)), (other, None))

    def _second_writer(self):
        """A write on another connection that won't wait: it fails at once if
        the pass has a transaction open."""
        other = sqlite3.connect(db.DB_PATH, timeout=0)
        self.addCleanup(other.close)

        def write():
            other.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('other_writer', '1')")
            other.commit()
        return write

    def test_another_connection_can_write_while_files_are_read(self):
        for name in ("one.flac", "two.flac", "three.flac"):
            _make_tagged_flac(self.root / "A" / "B" / name, [("MUSICBRAINZ_TRACKID", _MBID)])
            self._track(name)
        write = self._second_writer()
        real_get = mbid_backfill.TinyTag.get
        reads = []

        def get_while_someone_writes(path):
            write()
            reads.append(path)
            return real_get(path)
        with mock.patch.object(mbid_backfill.TinyTag, "get", get_while_someone_writes):
            result = mbid_backfill.run_job()
        self.assertEqual(len(reads), 3)
        self.assertEqual(result["found"], 3)

    def test_progress_is_reported_with_nothing_pending(self):
        count = mbid_backfill.BATCH * 2 + 1
        ids = []
        for n in range(count):
            _make_tagged_flac(self.root / "A" / "B" / f"{n}.flac", [("MUSICBRAINZ_TRACKID", _MBID)])
            ids.append(self._track(f"{n}.flac"))
        write = self._second_writer()
        seen = []

        def report(done, total=None, label=None):
            write()  # as jobs.set_progress does, on its own connection
            seen.append(done)
        mbid_backfill.run_job(None, report)
        self.assertEqual(seen, [0, mbid_backfill.BATCH, mbid_backfill.BATCH * 2, count])
        self.assertEqual({self._mbid(i) for i in ids}, {_MBID})

    def test_writing_nothing_takes_no_lock(self):
        holder = sqlite3.connect(db.DB_PATH, timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        waiter = sqlite3.connect(db.DB_PATH, timeout=0)
        self.addCleanup(waiter.close)
        mbid_backfill._write(waiter, [])  # raises if it asks for the lock

    def test_a_cancel_keeps_the_ids_already_read(self):
        count = mbid_backfill.BATCH + 1
        ids = []
        for n in range(count):
            _make_tagged_flac(self.root / "A" / "B" / f"{n}.flac", [("MUSICBRAINZ_TRACKID", _MBID)])
            ids.append(self._track(f"{n}.flac"))

        def report(done, total=None, label=None):
            if done:
                raise jobs.JobCancelled("stopped")
        with self.assertRaises(jobs.JobCancelled):
            mbid_backfill.run_job(None, report)
        self.assertEqual([self._mbid(i) for i in ids],
                         [_MBID] * mbid_backfill.BATCH + [None])

    def test_is_queued_until_it_has_run_once(self):
        def queued():
            return self.conn.execute("SELECT COUNT(*) FROM jobs WHERE type = ?",
                                     (mbid_backfill.JOB_TYPE,)).fetchone()[0]
        mbid_backfill.maybe_queue()
        mbid_backfill.maybe_queue()
        self.assertEqual(queued(), 1, "deduplicated however often the idle hook runs")
        self.conn.execute("DELETE FROM jobs")
        self.conn.commit()
        mbid_backfill.run_job()
        mbid_backfill.maybe_queue()
        self.assertEqual(queued(), 0, "never again once done")

    def test_an_admins_cancel_holds_the_pass_until_the_next_start(self):
        self.addCleanup(jobs._cancelled_types.clear)
        mbid_backfill.maybe_queue()
        job_id = self.conn.execute("SELECT id FROM jobs").fetchone()["id"]
        self.addCleanup(jobs._HANDLERS.pop, mbid_backfill.JOB_TYPE, None)
        self.addCleanup(jobs._LANE_BY_TYPE.pop, mbid_backfill.JOB_TYPE, None)

        def stopped(payload, report):
            jobs.request_cancel(job_id)
            return mbid_backfill.run_job(payload, report)

        _make_tagged_flac(self.root / "A" / "B" / "tagged.flac", [("MUSICBRAINZ_TRACKID", _MBID)])
        self._track("tagged.flac")
        jobs.register(mbid_backfill.JOB_TYPE, stopped, lane=jobs.LANE_LONG)
        self.assertTrue(jobs.run_one(jobs.LANE_LONG))
        self.assertEqual(self.conn.execute("SELECT state FROM jobs").fetchone()["state"], "cancelled")
        # not a failure: the retry budget is untouched
        self.assertIsNone(db.get_config(self.conn, mbid_backfill.FAILURES_KEY))
        mbid_backfill.maybe_queue()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0], 0)
        jobs._cancelled_types.clear()  # what a restart does
        mbid_backfill.maybe_queue()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0], 1)

    def test_a_failing_pass_is_retried_a_few_times_then_left_alone(self):
        _make_tagged_flac(self.root / "A" / "B" / "tagged.flac", [("MUSICBRAINZ_TRACKID", _MBID)])
        self._track("tagged.flac")

        def queued():
            return self.conn.execute("SELECT COUNT(*) FROM jobs WHERE type = ?",
                                     (mbid_backfill.JOB_TYPE,)).fetchone()[0]
        with mock.patch.object(mbid_backfill.scanner, "_recording_mbid", side_effect=RuntimeError("boom")):
            for attempt in range(1, mbid_backfill.MAX_FAILURES + 1):
                self.conn.execute("DELETE FROM jobs")
                self.conn.commit()
                mbid_backfill.maybe_queue()
                self.assertEqual(queued(), 1, f"queued again for attempt {attempt}")
                with self.assertLogs("mbid_backfill", "WARNING"), self.assertRaises(RuntimeError):
                    mbid_backfill.run_job()
        self.conn.execute("DELETE FROM jobs")
        self.conn.commit()
        mbid_backfill.maybe_queue()
        self.assertEqual(queued(), 0, "left alone after the last allowed failure")
        self.assertIsNone(db.get_config(self.conn, mbid_backfill.DONE_KEY))


if __name__ == "__main__":
    unittest.main()
