#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for jobs.py (#297): the durable background-work queue.

Real file-backed DB throughout — the whole point of this module is that job
state survives things (a restart, a crash mid-job), which an in-memory DB
can't meaningfully exercise, and the atomic claim is a real SQL statement
whose behaviour under concurrent access is the thing worth testing.

    python3 -m unittest test_jobs -v      # from app/
"""
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import db
import jobs


class _JobsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-jobs-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)

        self._orig_handlers = dict(jobs._HANDLERS)
        self.addCleanup(self._restore_handlers)

        self._orig_idle_hooks = list(jobs._idle_hooks)
        self.addCleanup(self._restore_idle_hooks)

        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def _restore_handlers(self):
        jobs._HANDLERS.clear()
        jobs._HANDLERS.update(self._orig_handlers)

    def _restore_idle_hooks(self):
        jobs._idle_hooks[:] = self._orig_idle_hooks

    def _enqueue(self, job_type: str, **kwargs) -> int:
        """enqueue() legitimately returns None when a dedupe_key is already
        pending, so narrow it here for the majority of tests that are asserting
        on a job they expect to exist. The tests that care about the None case
        call jobs.enqueue directly."""
        job_id = jobs.enqueue(self.conn, job_type, **kwargs)
        self.assertIsNotNone(job_id, "expected the enqueue to succeed")
        assert job_id is not None  # narrows int | None for mypy
        return job_id

    def _row(self, job_id: int):
        conn = db.get_conn()
        try:
            return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()


class EnqueueTests(_JobsTestBase):
    def test_enqueue_returns_an_id_and_persists_the_job(self):
        job_id = self._enqueue("demo", payload={"n": 1})
        self.assertIsNotNone(job_id)
        row = self._row(job_id)
        self.assertEqual(row["type"], "demo")
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["payload"], '{"n": 1}')

    def test_a_payloadless_job_stores_null(self):
        job_id = self._enqueue("demo")
        self.assertIsNone(self._row(job_id)["payload"])

    def test_enqueue_commits_so_another_connection_sees_it(self):
        # A queued job must be durable the moment the caller is told it exists.
        job_id = self._enqueue("demo")
        other = db.get_conn()
        try:
            self.assertIsNotNone(
                other.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone())
        finally:
            other.close()


class DedupeTests(_JobsTestBase):
    """The overlap guard, enforced by a partial unique index rather than by a
    per-module threading.Lock — this is what replaces the bespoke
    'already_running' checks."""

    def test_a_second_job_with_the_same_key_is_refused_while_queued(self):
        first = jobs.enqueue(self.conn, "demo", dedupe_key="k")
        second = jobs.enqueue(self.conn, "demo", dedupe_key="k")
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # a normal outcome, not an error
        self.assertEqual(jobs.status(self.conn)["queued"], 1)

    def test_the_key_is_still_held_while_the_job_is_running(self):
        jobs.enqueue(self.conn, "demo", dedupe_key="k")
        conn = db.get_conn()
        try:
            jobs.claim(conn)
        finally:
            conn.close()
        self.assertEqual(jobs.status(self.conn)["running"], 1)
        self.assertIsNone(jobs.enqueue(self.conn, "demo", dedupe_key="k"))

    def test_the_key_frees_up_once_the_job_finishes(self):
        jobs.register("demo", lambda payload, report=None: None)
        jobs.enqueue(self.conn, "demo", dedupe_key="k")
        jobs.run_one()
        self.assertEqual(jobs.status(self.conn)["done"], 1)
        # a finished job must never block the next one with the same key
        self.assertIsNotNone(jobs.enqueue(self.conn, "demo", dedupe_key="k"))

    def test_a_refused_enqueue_leaves_the_connection_usable(self):
        # The failed INSERT must be rolled back, or the caller's connection is
        # left in a broken transaction and its next write blows up.
        jobs.enqueue(self.conn, "demo", dedupe_key="k")
        self.assertIsNone(jobs.enqueue(self.conn, "demo", dedupe_key="k"))
        self.assertIsNotNone(jobs.enqueue(self.conn, "other", dedupe_key="k2"))

    def test_jobs_without_a_key_never_collide(self):
        self.assertIsNotNone(jobs.enqueue(self.conn, "demo"))
        self.assertIsNotNone(jobs.enqueue(self.conn, "demo"))
        self.assertEqual(jobs.status(self.conn)["queued"], 2)


class ReaperMessageTests(_JobsTestBase):
    """#329: the message a reaper-failed job carries.

    Not cosmetic. The previous wording ("this job may be crashing the server")
    was a correct inference phrased as a guess, and in production I read it as
    the reaper misfiring and proposed a change that would have converted a
    bounded three-crash episode into an unbounded crash loop. The message has to
    state what the row proves and name the check that resolves the ambiguity."""

    def test_an_exhausted_job_reports_how_many_times_the_server_died(self):
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO jobs (type, state, attempts) VALUES ('demo', 'running', ?)",
                (jobs._MAX_ATTEMPTS,))
            conn.commit()
            jobs.requeue_interrupted(conn)
            row = conn.execute("SELECT state, last_error FROM jobs").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["state"], "failed")
        # Both numbers, and they mean different things: deaths vs budget spent.
        self.assertIn("interruptions: 1", row["last_error"])
        self.assertIn(f"attempts used: {jobs._MAX_ATTEMPTS}", row["last_error"])
        self.assertIn("check the log", row["last_error"].lower())

    def test_it_reports_process_DEATHS_not_total_attempts(self):
        """The scenario the previous wording got wrong: a job that RAISED twice and
        was interrupted once. `attempts` is 3, but the server died once — so a
        message reading "the server died 3 times" is false, and sends an operator
        hunting for crashes that never happened."""
        jobs.register("demo", lambda payload, report=None:
                      (_ for _ in ()).throw(RuntimeError("handler raised")))
        job_id = self._enqueue("demo")
        # Two handler failures. _fail requeues with a backoff, so clear run_after
        # to make the next claim possible without waiting it out.
        for _ in range(2):
            jobs.run_one()
            self.conn.execute("UPDATE jobs SET run_after = NULL WHERE id = ?", (job_id,))
            self.conn.commit()
        # Then one process death: claimed, and the process dies mid-handler.
        conn = db.get_conn()
        try:
            jobs.claim(conn)
        finally:
            conn.close()
        jobs.requeue_interrupted(self.conn)

        row = self._row(job_id)
        self.assertEqual(row["attempts"], 3, "three claims were made")
        self.assertEqual(row["interruptions"], 1, "but the server died only ONCE")
        self.assertEqual(row["state"], "failed")
        self.assertIn("interruptions: 1", row["last_error"])
        self.assertIn("attempts used: 3", row["last_error"])
        # The specific lie this replaces.
        self.assertNotIn("died 3 times", row["last_error"])

    def test_a_pure_crash_loop_reports_every_death(self):
        # The genuine poison-pill case must still be reported accurately.
        job_id = self._enqueue("demo")
        for _ in range(jobs._MAX_ATTEMPTS):
            conn = db.get_conn()
            try:
                if jobs.claim(conn) is None:
                    break
            finally:
                conn.close()
            jobs.requeue_interrupted(self.conn)
        row = self._row(job_id)
        self.assertEqual(row["interruptions"], jobs._MAX_ATTEMPTS)
        self.assertEqual(row["state"], "failed")
        self.assertIn(f"interruptions: {jobs._MAX_ATTEMPTS}", row["last_error"])

    def test_a_requeued_job_does_not_claim_it_is_out_of_retries(self):
        # A job that still has attempts left is going to run again; saying
        # anything about retries being exhausted would be false.
        conn = db.get_conn()
        try:
            conn.execute("INSERT INTO jobs (type, state, attempts) VALUES ('demo', 'running', 1)")
            conn.commit()
            jobs.requeue_interrupted(conn)
            row = conn.execute("SELECT state, last_error FROM jobs").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["state"], "queued")
        self.assertIn("queued again", row["last_error"])
        self.assertNotIn("out of retries", row["last_error"])


class ClaimTests(_JobsTestBase):
    def test_claim_takes_the_oldest_and_marks_it_running(self):
        first = self._enqueue("demo")
        jobs.enqueue(self.conn, "demo")
        conn = db.get_conn()
        try:
            claimed = jobs.claim(conn)
        finally:
            conn.close()
        self.assertEqual(claimed["id"], first)
        self.assertEqual(claimed["state"], "running")
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNotNone(self._row(first)["started_at"])

    def test_claim_returns_none_on_an_empty_queue(self):
        conn = db.get_conn()
        try:
            self.assertIsNone(jobs.claim(conn))
        finally:
            conn.close()

    def test_a_job_is_only_claimed_once_under_concurrency(self):
        # The reason claim() is a single UPDATE..RETURNING rather than
        # SELECT-then-UPDATE. Even with one worker, requeue_interrupted and an
        # admin retry can move rows concurrently.
        for _ in range(20):
            jobs.enqueue(self.conn, "demo")
        claimed_ids = []
        lock = threading.Lock()

        def _worker():
            conn = db.get_conn()
            try:
                while True:
                    row = jobs.claim(conn)
                    if row is None:
                        return
                    with lock:
                        claimed_ids.append(row["id"])
            finally:
                conn.close()

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(claimed_ids), 20)
        self.assertEqual(len(set(claimed_ids)), 20)  # no job taken twice

    def test_a_job_held_back_by_run_after_is_not_claimed(self):
        job_id = self._enqueue("demo")
        self.conn.execute(
            "UPDATE jobs SET run_after = datetime('now', '+1 hour') WHERE id = ?", (job_id,))
        self.conn.commit()
        conn = db.get_conn()
        try:
            self.assertIsNone(jobs.claim(conn))
        finally:
            conn.close()

    def test_a_job_whose_run_after_has_passed_is_claimable(self):
        job_id = self._enqueue("demo")
        self.conn.execute(
            "UPDATE jobs SET run_after = datetime('now', '-1 hour') WHERE id = ?", (job_id,))
        self.conn.commit()
        conn = db.get_conn()
        try:
            self.assertEqual(jobs.claim(conn)["id"], job_id)
        finally:
            conn.close()


class RunOneTests(_JobsTestBase):
    def test_a_handler_receives_the_payload_and_its_result_is_stored(self):
        seen = {}

        def _handler(payload, report=None):
            seen.update(payload)
            return {"ok": True, "n": payload["n"]}

        jobs.register("demo", _handler)
        job_id = self._enqueue("demo", payload={"n": 7})
        self.assertTrue(jobs.run_one())
        self.assertEqual(seen, {"n": 7})
        row = self._row(job_id)
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["result"], '{"ok": true, "n": 7}')
        self.assertIsNone(row["last_error"])
        self.assertIsNotNone(row["finished_at"])

    def test_a_payloadless_job_gets_an_empty_dict(self):
        seen = []
        jobs.register("demo", lambda payload, report=None: seen.append(payload))
        jobs.enqueue(self.conn, "demo")
        jobs.run_one()
        self.assertEqual(seen, [{}])

    def test_run_one_returns_false_on_an_empty_queue(self):
        self.assertFalse(jobs.run_one())

    def test_a_none_returning_handler_still_completes(self):
        jobs.register("demo", lambda payload, report=None: None)
        job_id = self._enqueue("demo")
        jobs.run_one()
        row = self._row(job_id)
        self.assertEqual(row["state"], "done")
        self.assertIsNone(row["result"])


class RetryTests(_JobsTestBase):
    def test_a_failure_requeues_with_a_backoff_and_records_the_error(self):
        jobs.register("demo", lambda payload, report=None: (_ for _ in ()).throw(RuntimeError("nope")))
        job_id = self._enqueue("demo")
        jobs.run_one()
        row = self._row(job_id)
        self.assertEqual(row["state"], "queued")  # retryable, not failed
        self.assertEqual(row["attempts"], 1)
        self.assertIn("RuntimeError: nope", row["last_error"])
        self.assertIsNotNone(row["run_after"])  # held back, not hammered

    def test_it_gives_up_after_max_attempts(self):
        calls = []

        def _always_fails(payload, report=None):
            calls.append(1)
            raise RuntimeError("nope")

        jobs.register("demo", _always_fails)
        job_id = self._enqueue("demo")
        for _ in range(jobs._MAX_ATTEMPTS):
            # clear the backoff so the retry is immediately claimable
            self.conn.execute("UPDATE jobs SET run_after = NULL WHERE id = ?", (job_id,))
            self.conn.commit()
            jobs.run_one()
        row = self._row(job_id)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempts"], jobs._MAX_ATTEMPTS)
        self.assertEqual(len(calls), jobs._MAX_ATTEMPTS)
        # and it stays failed — a permanently broken job must not retry forever
        self.assertFalse(jobs.run_one())

    def test_a_transient_failure_that_heals_completes_normally(self):
        state = {"fail": True}

        def _flaky(payload, report=None):
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("transient")
            return {"healed": True}

        jobs.register("demo", _flaky)
        job_id = self._enqueue("demo")
        jobs.run_one()
        self.assertEqual(self._row(job_id)["state"], "queued")
        self.conn.execute("UPDATE jobs SET run_after = NULL WHERE id = ?", (job_id,))
        self.conn.commit()
        jobs.run_one()
        row = self._row(job_id)
        self.assertEqual(row["state"], "done")
        self.assertIsNone(row["last_error"])  # cleared on success

    def test_an_unregistered_type_fails_immediately_without_burning_retries(self):
        # A missing handler is a wiring bug, not a transient failure — retrying
        # it three times with backoff just delays the diagnosis.
        job_id = self._enqueue("never-registered")
        jobs.run_one()
        row = self._row(job_id)
        self.assertEqual(row["state"], "failed")
        self.assertIn("no handler registered", row["last_error"])


class RequeueInterruptedTests(_JobsTestBase):
    """The boot-time reaper. Nothing before #297 handled this at all: a daemon
    thread killed by a restart left no trace and its work was simply lost."""

    def test_a_running_job_is_requeued_at_boot(self):
        jobs.enqueue(self.conn, "demo")
        conn = db.get_conn()
        try:
            jobs.claim(conn)  # simulate: worker took it, then the process died
        finally:
            conn.close()
        self.assertEqual(jobs.status(self.conn)["running"], 1)

        n = jobs.requeue_interrupted(self.conn)

        self.assertEqual(n, 1)
        self.assertEqual(jobs.status(self.conn)["queued"], 1)
        self.assertEqual(jobs.status(self.conn)["running"], 0)

    def test_the_reaper_itself_does_not_touch_attempts(self):
        # Narrowly about the reaper. It does NOT mean an interrupted job
        # retries for free — see the poison-pill test below, which pins the
        # net behaviour that actually matters.
        job_id = self._enqueue("demo")
        conn = db.get_conn()
        try:
            jobs.claim(conn)
        finally:
            conn.close()
        attempts_before = self._row(job_id)["attempts"]
        jobs.requeue_interrupted(self.conn)
        row = self._row(job_id)
        self.assertEqual(row["attempts"], attempts_before)
        self.assertIn("the server stopped while this job was running", row["last_error"])
        self.assertIsNone(row["started_at"])

    def test_a_job_that_keeps_being_interrupted_eventually_gives_up(self):
        # The poison-pill guard, and the reason re-claims must NOT be free.
        # A job that reliably kills the process would otherwise loop forever:
        # crash -> restart -> requeued -> claimed -> crash, with the queue
        # dutifully resurrecting the thing taking the server down. Because
        # claim() charges an attempt, it lands in `failed` and stops.
        job_id = self._enqueue("demo")
        for _ in range(jobs._MAX_ATTEMPTS + 2):
            conn = db.get_conn()
            try:
                claimed = jobs.claim(conn)
            finally:
                conn.close()
            if claimed is None:
                break  # exhausted: nothing claimable any more
            jobs.requeue_interrupted(self.conn)  # "the process died again"

        row = self._row(job_id)
        self.assertEqual(row["attempts"], jobs._MAX_ATTEMPTS)
        # The budget is enforced by the reaper, so the job ends up `failed`
        # with a message that names the suspicion, rather than being handed
        # out again forever.
        self.assertEqual(row["state"], "failed")
        # #329 + follow-up: report what the row proves — deaths and budget, as two
        # distinct numbers — and point at the log, rather than hedging "may be
        # crashing the server", wording I then talked myself out of believing
        # during a real incident.
        self.assertIn(f"attempts used: {jobs._MAX_ATTEMPTS}", row["last_error"])
        self.assertIn("crashing it", row["last_error"])
        # And the crash loop is genuinely over: nothing left to claim.
        conn = db.get_conn()
        try:
            self.assertIsNone(jobs.claim(conn))
        finally:
            conn.close()

    def test_a_job_with_retries_left_is_still_requeued_after_an_interruption(self):
        # The guard above must not turn every interruption into a failure —
        # a normal deploy mid-job should resume, not give up.
        job_id = self._enqueue("demo")
        conn = db.get_conn()
        try:
            jobs.claim(conn)  # attempts = 1, well under the limit
        finally:
            conn.close()
        jobs.requeue_interrupted(self.conn)
        row = self._row(job_id)
        self.assertEqual(row["state"], "queued")
        self.assertIn("queued again", row["last_error"])

    def test_queued_done_and_failed_jobs_are_left_alone(self):
        jobs.register("demo", lambda payload, report=None: None)
        jobs.enqueue(self.conn, "demo")          # stays queued
        done = self._enqueue("other")
        jobs.register("other", lambda payload, report=None: None)
        jobs.run_one()
        jobs.run_one()
        before = jobs.status(self.conn)
        self.assertEqual(jobs.requeue_interrupted(self.conn), 0)
        self.assertEqual(jobs.status(self.conn), before)
        self.assertEqual(self._row(done)["state"], "done")


class StrictTableTests(_JobsTestBase):
    """#298: the jobs table is STRICT, so the bytes-silently-stored-as-BLOB
    trap — already hit twice, in fingerprint.py and provenance.py, each needing
    its own hand-written .decode() guard — is rejected at insert time here
    instead of persisting quietly."""

    def test_the_table_is_declared_strict(self):
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
        self.assertIn("STRICT", sql)

    def test_bytes_into_a_text_column_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO jobs (type, payload) VALUES (?, ?)", ("demo", b"raw-bytes"))


class PruneTests(_JobsTestBase):
    """#361: time-based retention with a per-(type, state) collapse past the
    window, replacing the old global row-count cap."""

    def _insert_finished(self, job_type: str, state: str, days_ago: float,
                          job_id: int | None = None) -> int:
        """Insert a job row directly with a controlled finished_at, rather
        than going through run_one() — the thing under test IS age-based
        behaviour, which needs rows that claim to be days old."""
        cur = self.conn.execute(
            "INSERT INTO jobs (id, type, state, finished_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (job_id, job_type, state, f"-{days_ago} days"))
        self.conn.commit()
        assert cur.lastrowid is not None  # always set right after an INSERT
        return cur.lastrowid

    def test_unfinished_jobs_are_never_pruned_regardless_of_age(self):
        # A real queued/running row never carries a finished_at, but the
        # prune query filters on state, not on finished_at being set — this
        # confirms that filter, not just that NULL happens to sort safely.
        self._insert_finished("demo", "queued", days_ago=365)
        jobs._prune_finished(self.conn)
        self.assertEqual(jobs.status(self.conn)["queued"], 1)

    def test_recent_finished_jobs_are_kept_regardless_of_count(self):
        # All well within the default 7-day window — a count-based cap
        # would have pruned these under the old _KEEP_FINISHED=200 shape
        # once enough chatty jobs piled up; a time-based one doesn't care
        # how many there are, only how old.
        for i in range(20):
            self._insert_finished("provenance_device_fingerprints", "done", days_ago=0.1)
        jobs._prune_finished(self.conn)
        self.assertEqual(jobs.status(self.conn)["done"], 20)

    def test_old_done_jobs_collapse_to_the_most_recent_per_type(self):
        old_ids = [self._insert_finished("demo", "done", days_ago=30) for _ in range(5)]
        jobs._prune_finished(self.conn)
        remaining = self.conn.execute(
            "SELECT id FROM jobs WHERE type = 'demo' AND state = 'done'").fetchall()
        self.assertEqual([r["id"] for r in remaining], [max(old_ids)])

    def test_failed_jobs_get_a_longer_window_than_done(self):
        # 10 days old: past done's default 7-day window but within failed's
        # 4x (28-day) window — the whole point of #361's differential
        # treatment, since a failed run is both the row an admin most needs
        # and the most likely to be evicted while nobody's looking.
        for _ in range(3):
            self._insert_finished("demo", "done", days_ago=10)
        for _ in range(3):
            self._insert_finished("demo", "failed", days_ago=10)
        jobs._prune_finished(self.conn)
        st = jobs.status(self.conn)
        self.assertEqual(st["done"], 1, "done jobs past their window collapse to one")
        self.assertEqual(st["failed"], 3, "failed jobs are still within their longer window")

    def test_collapse_keeps_success_and_failure_history_separately(self):
        # "when did the backfill last succeed, and when did it last fail?"
        # must both stay answerable — a collapse keyed on type alone (not
        # type+state) would let one silently evict the other.
        newest_done = self._insert_finished("demo", "done", days_ago=30)
        self._insert_finished("demo", "done", days_ago=31)
        newest_failed = self._insert_finished("demo", "failed", days_ago=40)
        self._insert_finished("demo", "failed", days_ago=41)
        jobs._prune_finished(self.conn)
        remaining = {r["id"] for r in self.conn.execute("SELECT id FROM jobs").fetchall()}
        self.assertEqual(remaining, {newest_done, newest_failed})

    def test_retention_window_is_configurable(self):
        db.set_config(self.conn, "job_retention_days", "1")
        self.conn.commit()
        old_ids = [self._insert_finished("demo", "done", days_ago=3) for _ in range(3)]
        jobs._prune_finished(self.conn)
        remaining = self.conn.execute(
            "SELECT id FROM jobs WHERE type = 'demo'").fetchall()
        self.assertEqual([r["id"] for r in remaining], [max(old_ids)])

    def test_enqueue_still_triggers_a_prune(self):
        # The enqueue-time trigger from before #361 must still work — the
        # worker's idle loop is an ADDITION (for a quiet instance), not a
        # replacement for the busy-instance case.
        for _ in range(3):
            self._insert_finished("demo", "done", days_ago=30)
        pending = self._enqueue("other")
        self.assertEqual(jobs.status(self.conn)["done"], 1)
        self.assertEqual(self._row(pending)["state"], "queued")


class StatusTests(_JobsTestBase):
    def test_status_counts_every_state(self):
        self.assertEqual(
            jobs.status(self.conn),
            {"queued": 0, "running": 0, "done": 0, "failed": 0})
        jobs.enqueue(self.conn, "demo")
        self.assertEqual(jobs.status(self.conn)["queued"], 1)

    def test_recent_returns_newest_first_with_a_decoded_result(self):
        jobs.register("demo", lambda payload, report=None: {"n": 1})
        jobs.enqueue(self.conn, "demo")
        jobs.run_one()
        jobs.enqueue(self.conn, "demo")
        rows = jobs.recent(self.conn)
        self.assertEqual(rows[0]["state"], "queued")   # newest first
        self.assertEqual(rows[1]["result"], {"n": 1})  # JSON decoded, not a str


class WorkerTests(_JobsTestBase):
    def test_the_worker_drains_the_queue_and_is_started_only_once(self):
        done = threading.Event()
        seen = []

        def _handler(payload, report=None):
            seen.append(payload.get("n"))
            if len(seen) == 3:
                done.set()

        jobs.register("demo", _handler)
        for n in range(3):
            jobs.enqueue(self.conn, "demo", payload={"n": n})

        # Genuinely stopped in cleanup, not just have its lock released: the
        # loop is `while True` on a daemon thread, so a released lock left the
        # thread polling for the whole remainder of the suite. It then claimed
        # jobs OTHER tests enqueued and ran them against whatever db.DB_PATH had
        # become — visible as "no such table: jobs" once a later test started
        # queueing work. Cross-test mutation, so worth stopping properly.
        self.assertTrue(jobs.start_worker())
        self.addCleanup(jobs.stop_worker)
        self.assertTrue(done.wait(10), "worker did not drain the queue")
        # a second call must not create a competing worker
        self.assertFalse(jobs.start_worker())
        self.assertEqual(sorted(seen), [0, 1, 2])

    def test_stop_worker_leaves_no_thread_claiming_jobs(self):
        jobs.register("demo", lambda payload, report=None: None)
        self.assertTrue(jobs.start_worker())
        self.assertTrue(jobs.stop_worker(), "worker did not stop")
        self.assertFalse(any(t.name == "job-worker" and t.is_alive()
                             for t in threading.enumerate()))
        # and the slot is free again, so a later start_worker() works
        self.assertTrue(jobs.start_worker())
        self.addCleanup(jobs.stop_worker)


class IdleHookTests(_JobsTestBase):
    """#362: jobs.on_idle — the generic extension point scanner.py's
    scheduled-rescan check is registered through, added so a periodic
    feature-specific decision doesn't require jobs.py to import the module
    that owns it (see the module docstring's "never imports handlers")."""

    def test_a_registered_hook_runs_once_the_worker_goes_idle(self):
        fired = threading.Event()
        jobs.on_idle(fired.set)
        self.assertTrue(jobs.start_worker())
        self.addCleanup(jobs.stop_worker)
        self.assertTrue(fired.wait(10), "idle hook never ran")

    def test_a_hook_that_raises_does_not_stop_the_others(self):
        fired = threading.Event()
        jobs.on_idle(lambda: (_ for _ in ()).throw(RuntimeError("nope")))
        jobs.on_idle(fired.set)
        self.assertTrue(jobs.start_worker())
        self.addCleanup(jobs.stop_worker)
        self.assertTrue(fired.wait(10), "a broken hook blocked a later one")


if __name__ == "__main__":
    unittest.main()
