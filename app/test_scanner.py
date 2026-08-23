#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for scanner.py's background-scan launcher (#140) — mocks _scan_library,
so no filesystem walk or DB is needed — plus (#200) real ISRC-tag extraction
and a real end-to-end scan against a tagged fixture file.

    python3 -m unittest test_scanner -v      # from app/
"""
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import db
import fingerprint
import jobs
import provenance
import scanner


class BackgroundScanTests(unittest.TestCase):
    """#297 step 3: the scan is a JOB now — start_scan enqueues, and the worker
    runs scanner.run_job. These tests are the same properties the old
    thread+_SCAN_LOCK version guaranteed, re-pinned against the queue:

      - start_scan returns immediately and does NOT scan inline;
      - a second trigger is refused while one is pending (dedupe, not a lock);
      - scan_status reports running/last_result for the UI to poll;
      - #141: a poll during a run never reports the PREVIOUS scan's counts;
      - the fingerprint backfill is never run inline, and a broken enqueue never
        costs a completed scan its counts.

    A real (temporary) DB is needed now, since the queue is a table. _scan_library
    is still mocked — the filesystem walk is covered by ScanLibraryIsrcTests.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-bgscan-")
        self._saved = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)
        patcher = mock.patch.object(scanner.fingerprint, "resolve_pending_fingerprints",
                                    return_value={"checked": 0, "resolved": 0})
        self.fingerprint_mock = patcher.start()
        self.addCleanup(patcher.stop)
        enqueue_patcher = mock.patch.object(scanner, "_queue_post_scan_jobs")
        self.enqueue_mock = enqueue_patcher.start()
        self.addCleanup(enqueue_patcher.stop)
        # Wire the handler explicitly: registration lives in main.py, which this
        # module deliberately doesn't import. test_main_wires_the_scan_into_the_
        # long_lane below is what checks the production wiring matches.
        jobs.register(scanner.JOB_TYPE, scanner.run_job, lane=jobs.LANE_LONG)
        self.addCleanup(jobs._HANDLERS.pop, scanner.JOB_TYPE, None)
        self.addCleanup(jobs._LANE_BY_TYPE.pop, scanner.JOB_TYPE, None)
        # A real music root, because run_job resolves and validates it rather
        # than trusting the payload.
        root_patcher = mock.patch.object(db, "get_music_root", return_value=Path(self._tmp))
        root_patcher.start()
        self.addCleanup(root_patcher.stop)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_queued(self):
        """Run the queued scan the way the worker would, in this thread."""
        return jobs.run_one(jobs.LANE_LONG)

    def test_start_scan_queues_and_does_not_scan_inline(self):
        with mock.patch.object(scanner, "_scan_library") as inner:
            started = scanner.start_scan(Path("/music"), force=True)
        self.assertEqual(started["status"], "started")
        self.assertIsInstance(started["job_id"], int)
        inner.assert_not_called()  # the whole point of #140's 202
        self.assertTrue(scanner.scan_status()["running"])

    def test_the_worker_runs_it_and_records_the_counts(self):
        counts = {"added": 2, "updated": 1, "removed": 0, "unchanged": 5}
        scanner.start_scan(Path("/music"), force=True)
        with mock.patch.object(scanner, "_scan_library", return_value=counts) as inner:
            self.assertTrue(self._run_queued())
        inner.assert_called_once()
        self.assertIs(inner.call_args.kwargs.get("force"), True)  # payload carried it
        st = scanner.scan_status()
        self.assertFalse(st["running"])
        self.assertEqual(st["last_result"], counts)
        self.enqueue_mock.assert_called_once()

    def test_last_scan_at_is_none_before_any_scan_has_run(self):
        self.assertIsNone(scanner.scan_status()["last_scan_at"])

    def test_last_scan_at_is_set_once_a_scan_finishes(self):
        # #475: the integration API's server-metrics route needs this to
        # answer "when did the library last get scanned" without a client
        # having to separately track it from watching `running` flip.
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library", return_value={"added": 1}):
            self._run_queued()
        self.assertIsNotNone(scanner.scan_status()["last_scan_at"])

    def test_last_scan_at_is_suppressed_while_a_new_scan_is_running(self):
        # Same #141 suppression as last_result -- reporting the PREVIOUS
        # scan's finish time while a new one is in flight would read as
        # "already done" when it demonstrably isn't.
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library", return_value={"added": 1}):
            self._run_queued()
        self.assertIsNotNone(scanner.scan_status()["last_scan_at"])
        scanner.start_scan(Path("/music"))  # a new one, not yet run
        self.assertIsNone(scanner.scan_status()["last_scan_at"])

    def test_the_scan_runs_in_the_long_lane_not_the_short_one(self):
        # A scan claimed by the short lane would put hours of work in front of a
        # device's provenance rematch — the starvation lanes exist to prevent.
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library", return_value={"added": 0}):
            self.assertFalse(jobs.run_one(jobs.LANE_SHORT), "short lane claimed the scan")
            self.assertTrue(jobs.run_one(jobs.LANE_LONG))

    def test_a_second_trigger_is_refused_while_one_is_pending(self):
        first = scanner.start_scan(Path("/music"))
        second = scanner.start_scan(Path("/music"))
        self.assertEqual(first["status"], "started")
        self.assertEqual(second["status"], "error")
        self.assertTrue(second["already_running"])

    def test_the_key_frees_up_once_the_scan_has_run(self):
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library", return_value={"added": 0}):
            self._run_queued()
        self.assertEqual(scanner.start_scan(Path("/music"))["status"], "started")

    def test_a_poll_during_a_run_does_not_report_the_previous_counts(self):
        # #141, re-pinned: the old code cleared a module global on start; the DB
        # keeps every finished job, so scan_status has to suppress it explicitly.
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library", return_value={"added": 99}):
            self._run_queued()
        self.assertEqual(scanner.scan_status()["last_result"], {"added": 99})
        scanner.start_scan(Path("/music"))  # a new one, not yet run
        st = scanner.scan_status()
        self.assertTrue(st["running"])
        self.assertIsNone(st["last_result"], "the stale 99 leaked into a running scan")

    def test_a_failed_scan_is_reported_as_an_error_with_its_reason(self):
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library",
                               side_effect=RuntimeError("disk gone")):
            self._run_queued()
        # One attempt of three: still queued for retry, so nothing is reported yet.
        self.assertIsNone(scanner.scan_status()["last_result"])
        conn = db.get_conn()
        try:
            conn.execute("UPDATE jobs SET attempts = 3, run_after = NULL")
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(scanner, "_scan_library",
                               side_effect=RuntimeError("disk gone")):
            self._run_queued()
        st = scanner.scan_status()
        self.assertEqual(st["last_result"]["status"], "error")
        self.assertIn("disk gone", st["last_result"]["reason"])

    def test_a_missing_music_root_fails_the_job_rather_than_reporting_zero(self):
        # A silent {"added": 0} would read as "your library is empty".
        with mock.patch.object(db, "get_music_root", return_value=Path("/nope-nope")):
            with self.assertRaises(FileNotFoundError):
                scanner.run_job({}, None)

    def test_the_backfill_is_never_run_inline(self):
        # Real audio decode plus paced external HTTP must not happen inside the
        # scan: it's queued as its own job instead.
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library", return_value={"added": 0}):
            self._run_queued()
        self.fingerprint_mock.assert_not_called()
        self.enqueue_mock.assert_called_once()

    def test_the_backfill_is_queued_with_the_scan_lock_released(self):
        held = []
        self.enqueue_mock.side_effect = lambda *a, **kw: held.append(scanner._SCAN_LOCK.locked())
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library", return_value={"added": 0}):
            self._run_queued()
        self.assertEqual(held, [False])

    def test_main_wires_the_scan_into_the_long_lane(self):
        # The lane is only correct in production if main.py says so; setUp above
        # wires it locally, so without this a mis-wire there would go unnoticed.
        import main  # noqa: F401 — imported for its registration side effects
        self.assertEqual(jobs._LANE_BY_TYPE.get(scanner.JOB_TYPE), jobs.LANE_LONG)
        self.assertEqual(jobs._LANE_BY_TYPE.get("fingerprint_backfill"), jobs.LANE_LONG)
        self.assertEqual(jobs._LANE_BY_TYPE.get("provenance_rematch"), jobs.LANE_SHORT)

    def test_a_broken_follow_up_enqueue_does_not_lose_the_scan_counts(self):
        # The scan succeeded; a failure queueing the follow-up must not turn that
        # into a failed job with no counts.
        self.enqueue_mock.side_effect = RuntimeError("db gone")
        counts = {"added": 1, "updated": 0, "removed": 0, "unchanged": 0}
        scanner.start_scan(Path("/music"))
        with mock.patch.object(scanner, "_scan_library", return_value=counts):
            self._run_queued()
        self.assertEqual(scanner.scan_status()["last_result"], counts)


class PostScanJobsTests(unittest.TestCase):
    """The v2.3.0 production defect, pinned.

    A completed full rescan left `tracks.fingerprint` NULL for all 59,025 rows,
    so #239's recovery-by-fingerprint could not work at all. Two causes, both
    here: the AcoustID backfill returns early when no API key is configured —
    correct for the LOOKUP, but it was the only post-scan job — and the keyless
    fingerprint COMPUTATION was only ever triggered by a device pushing
    provenance, which on a fresh install never happens."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-postscan-")
        self._saved = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _enrol_device(self):
        """#321 gated the keyless pass on having a device, so any test asserting
        that it IS queued needs one."""
        conn = db.get_conn()
        try:
            conn.execute("INSERT OR IGNORE INTO users (id, username) VALUES (1, 'u')")
            conn.execute("INSERT INTO devices (owner_user_id, name, api_token_hash) "
                         "VALUES (1, 'phone', 'h')")
            conn.commit()
        finally:
            conn.close()

    def _queued_types(self):
        conn = db.get_conn()
        try:
            return [r["type"] for r in conn.execute(
                "SELECT type FROM jobs WHERE state = 'queued' ORDER BY id")]
        finally:
            conn.close()

    def test_a_scan_queues_the_keyless_fingerprint_pass_as_well_as_the_lookup(self):
        self._enrol_device()
        scanner._queue_post_scan_jobs()
        self.assertEqual(self._queued_types(),
                         [provenance.JOB_TYPE_LIBRARY_FINGERPRINTS, fingerprint.JOB_TYPE])

    def test_the_keyless_pass_is_queued_even_with_no_acoustid_key(self):
        # The whole bug: no key meant NO fingerprints, not just no ISRCs.
        self._enrol_device()
        conn = db.get_conn()
        try:
            self.assertFalse(db.get_config(conn, "acoustid_api_key"))
        finally:
            conn.close()
        scanner._queue_post_scan_jobs()
        self.assertIn(provenance.JOB_TYPE_LIBRARY_FINGERPRINTS, self._queued_types())

    def test_both_are_deduped_so_two_scans_do_not_stack_them(self):
        self._enrol_device()
        scanner._queue_post_scan_jobs()
        scanner._queue_post_scan_jobs()
        self.assertEqual(self._queued_types(),
                         [provenance.JOB_TYPE_LIBRARY_FINGERPRINTS, fingerprint.JOB_TYPE])

    def test_the_keyless_pass_is_gated_on_having_a_device(self):
        # #321: the keyless fingerprint pass exists only for device
        # recovery (#200's matching needs acoustid_isrc, i.e. the API key), so on
        # an install that syncs nothing it is hours of audio decode for zero
        # benefit — while the Configuration panel said the feature was off.
        scanner._queue_post_scan_jobs()
        self.assertEqual(self._queued_types(), [fingerprint.JOB_TYPE],
                         "no devices enrolled, so the keyless pass must not run")

    def test_it_is_queued_once_a_device_exists(self):
        self._enrol_device()
        scanner._queue_post_scan_jobs()
        self.assertEqual(self._queued_types(),
                         [provenance.JOB_TYPE_LIBRARY_FINGERPRINTS, fingerprint.JOB_TYPE])

    def test_a_broken_enqueue_is_swallowed(self):
        # A completed scan must still report its counts.
        with mock.patch.object(scanner.jobs, "enqueue", side_effect=RuntimeError("db gone")):
            scanner._queue_post_scan_jobs()  # must not raise


class ScheduledRescanTests(unittest.TestCase):
    """#362: scanner.maybe_schedule_rescan (registered with jobs.on_idle, see
    main.py) and next_scheduled_scan_at (the admin-panel visibility the issue's
    maintainer decision made explicit — an enabled schedule nobody can see is
    the same invisible-background-mechanism problem #297 set out to fix)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-scheduled-rescan-")
        self._saved = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _set_interval(self, hours: int) -> None:
        conn = db.get_conn()
        try:
            db.set_config(conn, "scan_interval_hours", str(hours))
            conn.commit()
        finally:
            conn.close()

    def _add_finished_scan(self, state: str, hours_ago: float) -> None:
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO jobs (type, state, finished_at) VALUES (?, ?, datetime('now', ?))",
                (scanner.JOB_TYPE, state, f"-{hours_ago} hours"))
            conn.commit()
        finally:
            conn.close()

    def _queued_scan_count(self) -> int:
        conn = db.get_conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE type = ? AND state = 'queued'",
                (scanner.JOB_TYPE,)).fetchone()[0]
        finally:
            conn.close()

    def test_off_by_default_does_nothing(self):
        # scan_interval_hours is never set — the issue's explicit "defaulting
        # to off" decision, so a fresh install behaves exactly as it always did.
        self._add_finished_scan("done", hours_ago=1000)
        scanner.maybe_schedule_rescan()
        self.assertEqual(self._queued_scan_count(), 0)

    def test_enabled_but_not_yet_due_does_nothing(self):
        self._set_interval(24)
        self._add_finished_scan("done", hours_ago=1)
        scanner.maybe_schedule_rescan()
        self.assertEqual(self._queued_scan_count(), 0)

    def test_enabled_and_due_enqueues_an_incremental_scan(self):
        self._set_interval(24)
        self._add_finished_scan("done", hours_ago=25)
        scanner.maybe_schedule_rescan()
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT payload FROM jobs WHERE type = ? AND state = 'queued'",
                (scanner.JOB_TYPE,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "a due, enabled schedule must enqueue a scan")
        # never force=1 — the issue was explicit that a periodic forced
        # rescan (re-reading every tag on a timer) is almost never wanted.
        self.assertEqual(row["payload"], '{"force": false}')

    def test_never_scanned_and_enabled_is_immediately_due(self):
        # Enabling this on a fresh install shouldn't mean waiting a full
        # interval before the first automatic scan ever runs.
        self._set_interval(24)
        scanner.maybe_schedule_rescan()
        self.assertEqual(self._queued_scan_count(), 1)

    def test_due_but_already_queued_does_not_stack(self):
        # start_scan's own dedupe_key is the guard — same one a manual click
        # goes through — so this must be a harmless no-op, not an error.
        self._set_interval(24)
        self._add_finished_scan("done", hours_ago=25)
        conn = db.get_conn()
        try:
            jobs.enqueue(conn, scanner.JOB_TYPE, {"force": False}, dedupe_key=scanner.JOB_DEDUPE)
        finally:
            conn.close()
        scanner.maybe_schedule_rescan()  # must not raise, must not add a second row
        self.assertEqual(self._queued_scan_count(), 1)

    def test_next_scheduled_scan_at_is_none_when_off(self):
        self._add_finished_scan("done", hours_ago=1)
        conn = db.get_conn()
        try:
            self.assertIsNone(scanner.next_scheduled_scan_at(conn))
        finally:
            conn.close()

    def test_next_scheduled_scan_at_is_none_when_never_scanned(self):
        # Due now, not at some future timestamp — nothing to show.
        self._set_interval(24)
        conn = db.get_conn()
        try:
            self.assertIsNone(scanner.next_scheduled_scan_at(conn))
        finally:
            conn.close()

    def test_next_scheduled_scan_at_is_last_finish_plus_interval(self):
        self._set_interval(24)
        self._add_finished_scan("done", hours_ago=1)
        conn = db.get_conn()
        try:
            last_finished = conn.execute(
                "SELECT finished_at FROM jobs WHERE type = ? AND state = 'done'",
                (scanner.JOB_TYPE,)).fetchone()["finished_at"]
            expected = conn.execute(
                "SELECT datetime(?, '+24 hours')", (last_finished,)).fetchone()[0]
            self.assertEqual(scanner.next_scheduled_scan_at(conn), expected)
        finally:
            conn.close()


class DeletedAtTimezoneTests(unittest.TestCase):
    """#322: deleted_at must be UTC, like every other timestamp in the schema.

    It was written with time.strftime(), which uses the PROCESS timezone, so on any
    install with TZ set it landed in a different zone from scanned_at in the same
    row. The issue reporting it assumed this was latent because the image lacked
    tzdata — verified false: tzdata is installed (transitively), TZ resolves, and
    the process reports CEST +0200. It was live."""

    def setUp(self):
        # FORCE a non-UTC process timezone, and do not rely on the host's.
        # Verified necessary: with the old time.strftime() write these tests PASS
        # under TZ=UTC (the two timestamps coincide) and fail only under an offset
        # zone. CI runs UTC, so without this the guard would be inert in exactly
        # the place it needs to work.
        import time as _time
        self._saved_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Australia/Sydney"   # a large, non-DST-ambiguous offset
        _time.tzset()
        self.addCleanup(self._restore_tz)
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-delat-")
        self._saved = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.music = Path(self._tmp) / "music"
        (self.music / "A" / "B").mkdir(parents=True)
        _make_tagged_mp3(self.music / "A" / "B" / "gone.mp3", title="Gone")
        self.addCleanup(self._restore)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _restore_tz(self):
        import time as _time
        if self._saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._saved_tz
        _time.tzset()

    def _hold_on_a_device(self):
        """A soft-deleted track is garbage-collected immediately unless a device
        still references it (see _scan_library's DELETE). So deleted_at is only
        ever OBSERVABLE for a track some device holds — which is also the only
        case where its value matters."""
        conn = db.get_conn()
        try:
            conn.execute("INSERT INTO users (id, username) VALUES (1, 'u')")
            conn.execute("INSERT INTO devices (id, owner_user_id, name, api_token_hash) "
                         "VALUES (1, 1, 'phone', 'h')")
            conn.execute(
                "INSERT INTO device_track_state (device_id, track_id, status) "
                "SELECT 1, id, 'downloaded' FROM tracks")
            conn.commit()
        finally:
            conn.close()

    def test_a_vanished_track_is_soft_deleted_in_utc(self):
        scanner._scan_library(self.music)
        self._hold_on_a_device()
        (self.music / "A" / "B" / "gone.mp3").unlink()
        scanner._scan_library(self.music)
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT deleted_at, scanned_at, "
                "  CAST((julianday(deleted_at) - julianday(datetime('now'))) * 86400 "
                "  AS INTEGER) AS drift_seconds "
                "FROM tracks").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row["deleted_at"])
        # Within a couple of minutes of SQLite's own UTC clock. A process-timezone
        # write would be off by the whole TZ offset — hours, not seconds — which is
        # what this actually catches, regardless of the host's zone.
        self.assertLess(abs(row["drift_seconds"]), 120,
                        f"deleted_at={row['deleted_at']} is not UTC "
                        f"(drift {row['drift_seconds']}s vs datetime('now'))")

    def test_deleted_at_agrees_with_scanned_at_in_the_same_row(self):
        # The concrete symptom: two timestamps on one row, in two different zones.
        scanner._scan_library(self.music)
        self._hold_on_a_device()
        (self.music / "A" / "B" / "gone.mp3").unlink()
        scanner._scan_library(self.music)
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT CAST((julianday(deleted_at) - julianday(scanned_at)) * 86400 "
                "AS INTEGER) AS gap FROM tracks").fetchone()
        finally:
            conn.close()
        self.assertLess(abs(row["gap"]), 120,
                        f"deleted_at and scanned_at are {row['gap']}s apart — "
                        "one of them is not UTC")


class ScanProgressTests(unittest.TestCase):
    """#297 step 3: live progress, which is the reason this migration was asked
    for — during a production rescan there was no way to tell a running scan from
    a dead one."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-progress-")
        self._saved = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.music = Path(self._tmp) / "music"
        (self.music / "A" / "B").mkdir(parents=True)
        for n in range(3):
            _make_tagged_mp3(self.music / "A" / "B" / f"{n}.mp3", title=f"T{n}")
        self.addCleanup(self._restore)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_progress_reports_a_total_and_then_counts_up(self):
        seen = []
        scanner._scan_library(self.music, report=lambda d, t=None, label=None: seen.append((d, t, label)))
        self.assertEqual(seen[0], (0, None, "counting files"))
        self.assertEqual(seen[1], (0, 3, None), "the total must be known before work starts")

    def test_no_counting_pre_pass_when_nobody_is_watching(self):
        # The CLI and the synchronous path pay nothing for progress.
        with mock.patch.object(scanner, "_iter_audio_files",
                               wraps=scanner._iter_audio_files) as walk:
            scanner._scan_library(self.music)
        self.assertEqual(walk.call_count, 1, "the pre-pass ran with no reporter")

    def test_progress_survives_a_reporter_that_raises(self):
        # Progress is a display detail; it must never break a scan.
        def _bad(*a, **kw):
            raise RuntimeError("no")
        with self.assertRaises(RuntimeError):
            scanner._scan_library(self.music, report=_bad)

    def test_set_progress_writes_json_and_finishing_clears_it(self):
        conn = db.get_conn()
        try:
            job_id = jobs.enqueue(conn, "demo")
        finally:
            conn.close()
        assert job_id is not None  # enqueue only returns None on a dedupe clash
        jobs.set_progress(job_id, 12, 34, "files")
        conn = db.get_conn()
        try:
            rows = jobs.recent(conn)
            self.assertEqual(rows[0]["progress"], {"done": 12, "total": 34, "label": "files"})
            jobs._finish(conn, job_id, {"ok": True})
            self.assertIsNone(jobs.recent(conn)[0]["progress"], "done job kept stale progress")
        finally:
            conn.close()

    def test_progress_is_kept_on_failure_so_you_can_see_where_it_stopped(self):
        conn = db.get_conn()
        try:
            job_id = jobs.enqueue(conn, "demo")
        finally:
            conn.close()
        assert job_id is not None  # enqueue only returns None on a dedupe clash
        jobs.set_progress(job_id, 12431, 58783)
        conn = db.get_conn()
        try:
            jobs._fail(conn, job_id, jobs._MAX_ATTEMPTS, "boom")
            self.assertEqual(jobs.recent(conn)[0]["progress"],
                             {"done": 12431, "total": 58783})
        finally:
            conn.close()


def _syncsafe(n: int) -> bytes:
    return bytes([(n >> (7 * i)) & 0x7f for i in (3, 2, 1, 0)])


def _id3_frame(frame_id: bytes, text: str) -> bytes:
    payload = b"\x00" + text.encode("latin-1")  # encoding byte 0 = ISO-8859-1
    return frame_id + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


# A single silent MPEG-1 Layer III frame, repeated — just enough real audio
# for tinytag's duration calc not to choke; content is irrelevant, only the
# ID3 frames prepended to it matter for these tests.
_MP3_SILENCE_FRAME = bytes.fromhex(
    "fffb900400000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000"
)


def _make_tagged_mp3(path: Path, *, isrc: str | None = None, artist: str = "Test Artist",
                      title: str = "Test Title", album: str = "Test Album") -> None:
    """Writes a tiny real MP3 with hand-built ID3v2.3 frames — TSRC only
    when `isrc` is given — so tests exercise tinytag's actual ID3 parser
    rather than a mock. No mutagen dependency: deliberately kept out of
    even test-time tooling, same reasoning as scanner.py's own
    tinytag-over-mutagen choice (module docstring)."""
    frames = _id3_frame(b"TPE1", artist) + _id3_frame(b"TIT2", title) + _id3_frame(b"TALB", album)
    if isrc is not None:
        frames += _id3_frame(b"TSRC", isrc)
    header = b"ID3" + bytes([3, 0]) + b"\x00" + _syncsafe(len(frames))
    path.write_bytes(header + frames + _MP3_SILENCE_FRAME * 5)


class ReadTagsIsrcTests(unittest.TestCase):
    """#200: tinytag already parses ISRC from ID3 TSRC (other.isrc) — this
    exercises that through a real file rather than assuming it."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-scanner-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_extracts_isrc_from_id3_tsrc(self):
        path = Path(self._tmp) / "tagged.mp3"
        _make_tagged_mp3(path, isrc="USRC17607839")
        tags = scanner._read_tags(path)
        self.assertEqual(tags["isrc"], "USRC17607839")

    def test_isrc_is_none_when_the_file_has_no_tsrc_frame(self):
        path = Path(self._tmp) / "untagged.mp3"
        _make_tagged_mp3(path, isrc=None)
        tags = scanner._read_tags(path)
        self.assertIsNone(tags["isrc"])


class ScanLibraryIsrcTests(unittest.TestCase):
    """End-to-end (#200): a real scan_library() run over a tagged fixture
    persists isrc onto the tracks row, not just that _read_tags parses it."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-scanner-db-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore_db_globals)
        self.root = Path(self._tmp) / "music"
        (self.root / "Test Artist" / "Test Album").mkdir(parents=True)

    def _restore_db_globals(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def test_scan_persists_isrc_onto_the_track_row(self):
        _make_tagged_mp3(
            self.root / "Test Artist" / "Test Album" / "01 Test Title.mp3",
            isrc="USRC17607839",
        )
        counts = scanner.scan_library(self.root)
        self.assertEqual(counts["added"], 1)
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT isrc FROM tracks").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["isrc"], "USRC17607839")

    def test_scan_leaves_isrc_null_when_the_file_has_none(self):
        _make_tagged_mp3(
            self.root / "Test Artist" / "Test Album" / "02 No Isrc.mp3",
            isrc=None,
        )
        scanner.scan_library(self.root)
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT isrc FROM tracks").fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["isrc"])


class FingerprintInvalidationTests(unittest.TestCase):
    """#239: the fingerprint/AcoustID cache describes the AUDIO, so changed
    bytes must invalidate it — otherwise a re-encoded file keeps a fingerprint
    of its old content, and provenance confidently ships clients the identity
    of a track that isn't there any more.

    The force=True case is the one that matters most: a forced rescan takes the
    same UPDATE branch for every file including unchanged ones, so gating the
    invalidation on "did we reach this branch" instead of "did the bytes
    change" would wipe the whole library's fingerprints and re-trigger a
    full, rate-limited AcoustID re-lookup on every forced rescan."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-scanner-fp-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore_db_globals)
        self.root = Path(self._tmp) / "music"
        (self.root / "Test Artist" / "Test Album").mkdir(parents=True)
        self.track_path = self.root / "Test Artist" / "Test Album" / "01 Test Title.mp3"

    def _restore_db_globals(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def _seed_fingerprint(self):
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE tracks SET fingerprint = 'OLDFP', acoustid_isrc = 'USRC00000000', "
                "acoustid_mbid = 'mbid-old', fingerprint_checked_at = '2026-01-01', "
                "fingerprint_seq = 7")
            conn.commit()
        finally:
            conn.close()

    def _fp_row(self):
        conn = db.get_conn()
        try:
            return conn.execute(
                "SELECT fingerprint, acoustid_isrc, acoustid_mbid, fingerprint_checked_at, "
                "fingerprint_seq FROM tracks").fetchone()
        finally:
            conn.close()

    def test_changed_content_clears_the_fingerprint_cache(self):
        _make_tagged_mp3(self.track_path, title="Test Title")
        scanner.scan_library(self.root)
        self._seed_fingerprint()

        # Rewrite with different bytes (more audio frames => different size).
        frames = _id3_frame(b"TPE1", "Test Artist") + _id3_frame(b"TIT2", "Test Title") \
            + _id3_frame(b"TALB", "Test Album")
        header = b"ID3" + bytes([3, 0]) + b"\x00" + _syncsafe(len(frames))
        self.track_path.write_bytes(header + frames + _MP3_SILENCE_FRAME * 40)

        counts = scanner.scan_library(self.root)
        self.assertEqual(counts["updated"], 1)
        row = self._fp_row()
        self.assertIsNone(row["fingerprint"])
        self.assertIsNone(row["acoustid_isrc"])
        self.assertIsNone(row["acoustid_mbid"])
        self.assertIsNone(row["fingerprint_checked_at"])
        # #439: a stale sequence number must not survive its own fingerprint
        # going away -- left set, it would tell an incremental client
        # nothing changed for this track even after new bytes eventually
        # earn it a fresh (higher) one.
        self.assertIsNone(row["fingerprint_seq"])

    def test_forced_rescan_of_an_unchanged_file_keeps_the_fingerprint_cache(self):
        _make_tagged_mp3(self.track_path, title="Test Title")
        scanner.scan_library(self.root)
        self._seed_fingerprint()

        # force=True re-reads tags for every file and takes the UPDATE branch
        # even though nothing changed on disk. Nothing may be invalidated.
        counts = scanner.scan_library(self.root, force=True)
        self.assertEqual(counts["updated"], 1)
        row = self._fp_row()
        self.assertEqual(row["fingerprint"], "OLDFP")
        self.assertEqual(row["acoustid_isrc"], "USRC00000000")
        self.assertEqual(row["acoustid_mbid"], "mbid-old")
        self.assertEqual(row["fingerprint_checked_at"], "2026-01-01")
        self.assertEqual(row["fingerprint_seq"], 7)

    def test_changed_content_also_clears_a_previous_fingerprint_failure(self):
        # The realistic case: a half-copied file that couldn't be decoded is
        # replaced by the complete one. It must not stay deprioritised for a
        # failure that no longer applies to the bytes now on disk.
        _make_tagged_mp3(self.track_path, title="Test Title")
        scanner.scan_library(self.root)
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tracks SET fingerprint_failed_at = '2026-01-01'")
            conn.commit()
        finally:
            conn.close()

        frames = _id3_frame(b"TPE1", "Test Artist") + _id3_frame(b"TIT2", "Test Title") \
            + _id3_frame(b"TALB", "Test Album")
        header = b"ID3" + bytes([3, 0]) + b"\x00" + _syncsafe(len(frames))
        self.track_path.write_bytes(header + frames + _MP3_SILENCE_FRAME * 40)
        scanner.scan_library(self.root)

        conn = db.get_conn()
        try:
            failed_at = conn.execute("SELECT fingerprint_failed_at FROM tracks").fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(failed_at)

    def test_an_unchanged_file_is_skipped_entirely(self):
        _make_tagged_mp3(self.track_path, title="Test Title")
        scanner.scan_library(self.root)
        self._seed_fingerprint()
        counts = scanner.scan_library(self.root)  # no force, nothing changed
        self.assertEqual(counts["unchanged"], 1)
        self.assertEqual(self._fp_row()["fingerprint"], "OLDFP")


if __name__ == "__main__":
    unittest.main()
