#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#297: one durable queue for long-running background work.

Before this, every long task — library scan, playlist sync, fingerprint
backfill, device provenance — was its own arrangement of a module-level
`threading.Lock`, a fire-and-forget daemon thread, and a hand-rolled "last
result" global. Each was individually well-reasoned; collectively they
duplicated the same plumbing four times and shared one real problem: **a
failure in any of them was invisible to the person running the server.**
`_log.exception("fingerprint backfill failed")` was the entire failure
story, and for self-hosted software the user *is* the admin, so "what's
running / what failed / retry it" is user-facing, not internal hygiene.

What the queue adds that no per-module mechanism had:

  * failures that persist and can be looked at (`state='failed'`,
    `last_error`) instead of scrolling past in a log;
  * retry with backoff, so a transient network failure mid-sync isn't
    simply lost until someone notices and re-triggers by hand;
  * survival across a restart — `requeue_interrupted()` picks up work that
    was mid-flight when the process died. Nothing previously did this at
    all: a daemon thread killed by a restart left no trace;
  * one overlap guard (a partial unique index on `dedupe_key`) in place of
    per-module locks, enforced by the database rather than by discipline.

Deliberately NOT solved here, so this isn't oversold: it makes nothing
faster, and it does not unlock multi-process serving (other in-process
state — the transcode semaphore, per-IP rate-limit counters — still pins
that).

Concurrency model: ONE worker thread, started from main.py's __main__
bootstrap (never at import, so tests and the `python3 -m scanner` CLI don't
silently acquire a background worker). Jobs run strictly one at a time,
which is the same effective serialisation the old locks provided — the
point of this module is observability and durability, not parallelism.
"""

import json
import logging
import sqlite3
import threading
import time

import db

_log = logging.getLogger(__name__)

# {job type: callable(payload: dict) -> dict | None}. Handlers are registered
# by the modules that own the work (see register), so this module never
# imports them — that would be a circular import, and it would also make the
# queue "know" about every feature.
_HANDLERS: dict = {}

# #297 step 3: TWO worker lanes, not one.
#
# The scan and the fingerprint backfill run for hours on a large library. With a
# single worker they hold the queue for that whole time, so a device's provenance
# rematch — seconds of work — waits behind them. That starvation already existed
# between the backfill and provenance; putting the scan on the queue would have
# made it the normal case rather than the rare one.
#
# A lane is a property of the job TYPE, taken from the registry, so it is
# deliberately NOT a column: nothing can queue a job into the wrong lane, and
# there is no schema to migrate or keep in sync with the code.
#
# The short lane claims everything NOT registered long, rather than only what is
# registered short. That keeps the existing "queued type with no handler fails
# fast" path working — a type nobody registered still gets claimed, and run_one
# raises LookupError on it — instead of leaving it queued forever, invisible.
LANE_SHORT = "short"
LANE_LONG = "long"
_LANES = (LANE_SHORT, LANE_LONG)
_LANE_BY_TYPE: dict[str, str] = {}
# A job type's payload transform for an automatic second run (a retry after a
# failure, a requeue after a restart): see register(on_retry=).
_ON_RETRY: dict = {}


def _long_types() -> list:
    return sorted(t for t, lane in _LANE_BY_TYPE.items() if lane == LANE_LONG)

# Retries are for TRANSIENT failure (a provider's API refusing mid-sync, a
# file briefly unreadable). Three attempts with a widening gap, then the job
# is left `failed` for a human to look at rather than retried forever — a
# permanently broken job that retries indefinitely is just a log flood that
# hides everything else.
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (60, 300)  # after attempt 1, then attempt 2

# #361: finished jobs are kept for the admin overview, but not forever — and
# not by a global row-count cap either, which is what this replaced. A count
# budget shared by every job type let a client-polled hot path
# (provenance_device_fingerprints, enqueued on every /api/device/changes
# poll) evict the rare, valuable runs — an overnight library scan or
# fingerprint backfill — that the panel exists to let an admin inspect. See
# _prune_finished for the time-based replacement.
#
# Admin-configurable via app_config's "job_retention_days" key (see
# _job_retention_days); this is only the default before that's set.
DEFAULT_JOB_RETENTION_DAYS = 7
# failed jobs get a longer window than done ones before being collapsed —
# it's both the row an admin most needs and the one most likely to be
# evicted while nobody's looking (#361's maintainer decision). A fixed
# multiple of the configured window, not a second setting: the issue was
# explicit that the per-type collapse and the failure handling should be
# fixed behaviour, with retention-in-days as the only knob.
_FAILED_RETENTION_MULTIPLIER = 4

# The worker sleeps on this rather than polling on a fixed tick, so an
# enqueue is picked up promptly while an idle server does nothing. The
# timeout is the fallback that makes run_after (retry backoff) fire without
# needing a second timer.
#
# ONE EVENT PER LANE, which matters: two threads sharing a single Event with
# wait()/clear() lose wakeups — whichever clears first swallows the other's
# signal, and that job then waits out the full idle poll for no reason.
_wakes = {lane: threading.Event() for lane in _LANES}
_IDLE_POLL_SECONDS = 30.0

_worker_started = threading.Lock()

# Why a running job was asked to stop. An admin's cancel ends it `cancelled`;
# a shutdown puts it back in the queue, since stopping the server is not a
# decision to drop the work.
CANCEL_ADMIN = "admin"
CANCEL_SHUTDOWN = "shutdown"

# {job id: reason} for running jobs asked to stop. In memory, not a column: the
# worker that has to see it is in this process, and a restart before the job's
# next safe point leaves it `running`, which requeue_interrupted already handles.
_cancel_requests: dict[int, str] = {}
_cancel_lock = threading.Lock()
# Job types an admin has cancelled since this process started, for an idle
# hook that would otherwise queue the same work again (see cancelled_this_run).
_cancelled_types: set[str] = set()


class JobCancelled(BaseException):
    """Raised by a running job's `report` once the job has been asked to stop.

    A BaseException, like asyncio.CancelledError, so a handler's own
    `except Exception` (several keep going past a per-item failure) can't
    swallow it and carry on. run_one catches it by name. `report` may only be
    called between the handler's own commits (see set_progress), so where it
    raises is a point the handler can stop at without leaving half a write."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def request_cancel(job_id: int, reason: str = CANCEL_ADMIN) -> None:
    """Ask a running job to stop at its next `report`. A second request keeps
    the first one's reason, so a shutdown can't turn an admin's cancel into a
    requeue."""
    with _cancel_lock:
        _cancel_requests.setdefault(job_id, reason)


def cancel_requested(job_id: int) -> bool:
    with _cancel_lock:
        return job_id in _cancel_requests


def cancelled_this_run(job_type: str) -> bool:
    """Whether an admin has cancelled a job of this type since the server
    started. For an on_idle hook that queues its work whenever it is
    missing: without this, a cancel would be undone at the next idle check.
    A restart clears it, so cancelled work still runs eventually."""
    with _cancel_lock:
        return job_type in _cancelled_types


def _check_cancelled(job_id: int) -> None:
    with _cancel_lock:
        reason = _cancel_requests.get(job_id)
    if reason is not None:
        raise JobCancelled(reason)

# Set to bring the worker loop down. Only the test suite uses it in practice —
# in production the loop runs for the process's lifetime and the daemon thread
# dies with it — but without a way to stop it, a test that starts a real worker
# leaves it polling for the rest of the run, claiming other tests' jobs against
# whatever db.DB_PATH has become by then. That's cross-test mutation, not just
# noise, and it's how this was found.
_stop = threading.Event()
_worker_threads: list = []

# #362: periodic checks that don't belong to any one job's handler — e.g.
# scanner.py's own "is a scheduled rescan due?" check. A plain list, not
# per-lane: these are cheap, infrequent, and have no reason to duplicate the
# two-lane split that exists for JOB EXECUTION time, not for a config check.
_idle_hooks: list = []


def on_idle(callback) -> None:
    """Register a callable to run once per worker idle cycle (same cadence
    as _prune_finished — see _worker_loop) on EACH of the two worker lanes —
    so with both lanes idling, a registered hook runs twice per cycle,
    concurrently, on two different threads (_worker_loop's own comment at
    the call site: harmless, since anything a hook does is already safe to
    call concurrently with itself — but that safety is the caller's
    responsibility, not something this function arranges). Exists so a
    module can react to the passage of time without this module knowing
    anything about it: jobs.py deliberately never imports the modules that
    own job types (see the module docstring), and a scheduling decision like
    #362's is exactly that kind of feature-specific logic, not queue
    plumbing.

    Called with no arguments; a hook that needs a connection opens its own,
    same as a job handler does. Errors are caught per-hook in the loop, not
    here, so one broken hook can't stop the others from running."""
    _idle_hooks.append(callback)


def wake() -> None:
    """Nudge the workers to look now instead of at their next idle poll. For
    callers that queue work by writing the table directly (the admin retry
    route resets a failed job's state itself, rather than inserting a new
    one, so it can't go through enqueue).

    Wakes BOTH lanes: this is called without knowing the job's type (the retry
    route has only an id), and a spurious wake costs one empty claim query."""
    for event in _wakes.values():
        event.set()


def register(job_type: str, handler, lane: str = LANE_SHORT, on_retry=None) -> None:
    """Wire a job type to the function that performs it. Called at import
    time by the owning module (or by main.py for cross-module wiring).

    `handler(payload, report)` — `report(done, total=None, label=None)` writes
    live progress for the Background jobs panel. A handler with nothing useful
    to report accepts the argument and ignores it.

    `lane` is LANE_LONG for a handler that RUNS TO COMPLETION internally —
    loops until its whole backlog is drained in one execution, with no fixed
    upper bound on how long that takes — and LANE_SHORT for a handler that
    processes one capped batch and returns, relying on being re-triggered
    for the rest.

    #356: this is NOT "does it decode audio" — that would put a job in the
    wrong lane in both directions in this codebase. A capped-batch handler
    that happens to decode audio per item (provenance's per-device
    fingerprint pass, its rematch) is still LANE_SHORT: its worst case is
    bounded by the batch size, not by the size of whatever backlog triggered
    it. A run-to-completion handler that does NO audio decoding at all
    (fingerprint.py's AcoustID/MusicBrainz lookup, purely network-bound
    since #334) is still LANE_LONG: it loops until nothing claimable
    remains, which on a large library is legitimately hours. See
    _LANE_BY_TYPE, and the registration site in main.py for the specific
    per-type reasoning.

    `on_retry(payload) -> payload`, when given, rewrites the payload each time
    the job goes back to the queue on its own (a retry after a failure, a
    requeue after a restart), for work that should be cheaper the second
    time. An admin's explicit retry keeps the payload as it is."""
    if lane not in _LANES:
        raise ValueError(f"unknown lane {lane!r}")
    _HANDLERS[job_type] = handler
    _LANE_BY_TYPE[job_type] = lane
    if on_retry is not None:
        _ON_RETRY[job_type] = on_retry
    else:
        _ON_RETRY.pop(job_type, None)


def _apply_on_retry(conn, where: str, params: tuple = ()) -> None:
    """Rewrite, per its type's on_retry, the payload of every job matching
    `where` that is about to go back to the queue."""
    for row in conn.execute(f"SELECT id, type, payload FROM jobs WHERE {where}", params).fetchall():
        transform = _ON_RETRY.get(row["type"])
        if transform is None:
            continue
        payload = json.loads(row["payload"]) if row["payload"] else {}
        conn.execute("UPDATE jobs SET payload = ? WHERE id = ?",
                     (json.dumps(transform(payload)), row["id"]))


def enqueue(conn, job_type: str, payload: dict | None = None,
            dedupe_key: str | None = None) -> int | None:
    """Queue a job. Returns its id, or None if `dedupe_key` matched a job
    that's already queued or running — that's the overlap guard, and a
    None return is a normal, expected outcome, not an error.

    Commits, because a queued job must be durable the moment the caller is
    told it exists; a caller that rolled back afterwards would leave the
    worker chasing a job the DB no longer has."""
    _prune_finished(conn)
    try:
        cur = conn.execute(
            "INSERT INTO jobs (type, payload, dedupe_key) VALUES (?, ?, ?)",
            (job_type, json.dumps(payload) if payload is not None else None, dedupe_key),
        )
        conn.commit()
    except db.sqlite3.IntegrityError:
        # The partial unique index on dedupe_key fired: one is already
        # pending. Roll back the failed INSERT so the caller's connection
        # isn't left in a broken transaction.
        conn.rollback()
        return None
    _wakes[_LANE_BY_TYPE.get(job_type, LANE_SHORT)].set()
    return cur.lastrowid


def _job_retention_days(conn) -> int:
    raw = db.get_config(conn, "job_retention_days")
    try:
        return int(raw) if raw else DEFAULT_JOB_RETENTION_DAYS
    except (TypeError, ValueError):
        return DEFAULT_JOB_RETENTION_DAYS


def _prune_finished(conn) -> None:
    """#361: time-based, not count-based, and collapses rather than deletes
    outright once a row ages out of its window.

    Within the window (job_retention_days, `failed` getting
    _FAILED_RETENTION_MULTIPLIER times as long), every finished row is kept
    as-is regardless of type or count. Past it, only the single most recent
    row per (type, state) survives — so "when did the backfill last
    succeed, and when did it last fail?" stays answerable indefinitely, at
    one row each, rather than a type vanishing from the panel once its last
    run ages out.

    Commits itself: called both from enqueue (piggybacking on its own
    commit was the old shape) and from the worker's idle loop, which has no
    other commit to ride along with — a purely enqueue-gated prune would
    freeze a quiet instance's history at whatever it looked like when
    activity stopped, since nothing would ever trigger it again."""
    days = _job_retention_days(conn)
    conn.execute(
        "DELETE FROM jobs WHERE id IN ("
        " SELECT j.id FROM jobs j WHERE j.state IN ('done', 'failed', 'cancelled')"
        " AND j.finished_at < datetime('now', CASE j.state WHEN 'failed' THEN ? ELSE ? END)"
        " AND j.id != ("
        "   SELECT id FROM jobs j2 WHERE j2.type = j.type AND j2.state = j.state"
        # finished_at, not id: "most recent" means most recently completed.
        # They coincide in the ordinary case (a single worker runs jobs
        # roughly in id order), but finished_at is the actual thing this
        # function is about, and id only breaks a same-second tie.
        "   ORDER BY j2.finished_at DESC, j2.id DESC LIMIT 1))",
        (f"-{days * _FAILED_RETENTION_MULTIPLIER} days", f"-{days} days"),
    )
    conn.commit()


def requeue_interrupted(conn) -> int:
    """Boot-time reaper: anything still `running` is the residue of a
    process that died mid-job (restart, crash, OOM), since only the worker
    sets that state and only one worker exists. Put it back in the queue and
    record why, so the run isn't silently lost — the failure mode no
    previous mechanism handled at all.

    POISON-PILL GUARD, and the subtle part of this function. A job that
    reliably kills the process — an OOM on one pathological file, say — must
    not be resurrected forever: crash, restart, requeue, claim, crash, with
    the queue dutifully reviving the thing taking the server down.

    Charging an attempt is NOT sufficient on its own to stop that, which is
    worth stating because it looks like it should be. `claim()` does
    increment, so attempts climbs — but _MAX_ATTEMPTS is only ever enforced
    in _fail(), and _fail() only runs when a HANDLER RAISES. On this path the
    process died, so nothing enforces anything and the job is handed out
    again regardless of how high attempts has climbed. (Found by testing the
    claim rather than reasoning about it: a job driven round this loop
    reached 5 attempts against a limit of 3.)

    So the budget is enforced HERE too: an interrupted job that has already
    spent its attempts goes straight to `failed` instead of back to
    `queued`. Everything else is requeued with its history intact."""
    rows = conn.execute("SELECT id FROM jobs WHERE state = 'running'").fetchall()
    if rows:
        # WORDING MATTERS HERE, and the previous version cost real time (#329).
        # It said "this job may be crashing the server" — a correct inference
        # phrased as a guess. Faced with it in production I talked myself out of
        # it, decided the reaper was misfiring on ordinary restarts, and proposed
        # a change that would have turned a bounded three-crash episode into an
        # unbounded crash loop. It was right and I didn't believe it.
        #
        # So: state the EVIDENCE (the process died N times while this job was
        # running — that is what the row proves) and name the check that
        # distinguishes the two causes, rather than hedging the conclusion. The
        # reaper genuinely cannot tell a deliberate restart from a crash; it can
        # report how many times it happened, which is the number that matters.
        # Count the death FIRST, on every interrupted row, so both messages below
        # report a true number.
        #
        # This is the second correction to this message, and the reason is the same
        # each time: it must not assert something the row does not prove. The first
        # version hedged a correct inference into a guess ("may be crashing the
        # server"). The replacement reported `attempts` as a death count — but
        # claim() increments attempts once per claim, so a job that raised twice
        # and was interrupted once said "the server died 3 times", which is false
        # and sends an operator hunting for crashes that never happened.
        conn.execute(
            "UPDATE jobs SET interruptions = interruptions + 1 WHERE state = 'running'")
        conn.execute(
            "UPDATE jobs SET state = 'failed', finished_at = datetime('now'), "
            "last_error = 'not retried automatically — the server stopped while this "
            "job was running (interruptions: ' || interruptions || ', attempts used: ' "
            "|| attempts || ' of ' || ? || '). If you did not restart the server that "
            "often, this job may be crashing it; check the log around the times above.' "
            "WHERE state = 'running' AND attempts >= ?",
            (_MAX_ATTEMPTS, _MAX_ATTEMPTS),
        )
        _apply_on_retry(conn, "state = 'running'")
        conn.execute(
            "UPDATE jobs SET state = 'queued', started_at = NULL, "
            "last_error = 'the server stopped while this job was running; "
            "it has been queued again' "
            "WHERE state = 'running'"
        )
        conn.commit()
        # warning, not info, for two reasons: work being interrupted mid-flight
        # is genuinely notable (the server died while doing something), and the
        # app configures no logging at all — so the root logger sits at WARNING
        # and an info() here would be discarded unseen. Checked directly in a
        # running container, not assumed. It only fires when a job actually was
        # in flight, so a normal deploy stays quiet.
        _log.warning("requeued %d job(s) interrupted by a server restart", len(rows))
    return len(rows)


def claim(conn, lane: str = LANE_SHORT):
    """Atomically take the oldest claimable job IN THIS LANE. Returns its row,
    or None.

    Single statement on purpose: SELECT-then-UPDATE would be a race even with
    one worker, because requeue_interrupted and an admin retry can also move
    rows — and with two lanes there are now genuinely concurrent claimers.
    `RETURNING` needs SQLite 3.35+ (shipped image has 3.46).

    The lane filter is built from the registry, not from a column: the long lane
    takes exactly the types registered long, and the short lane takes everything
    else — including types with no handler at all, so those still fail fast in
    run_one instead of sitting queued forever."""
    long_types = _long_types()
    if lane == LANE_LONG:
        if not long_types:
            return None
        predicate = f"AND type IN ({', '.join('?' * len(long_types))})"
        params = tuple(long_types)
    else:
        predicate = (f"AND type NOT IN ({', '.join('?' * len(long_types))})"
                     if long_types else "")
        params = tuple(long_types)
    row = conn.execute(
        "UPDATE jobs SET state = 'running', attempts = attempts + 1, "
        "started_at = datetime('now') WHERE id = ("
        "  SELECT id FROM jobs WHERE state = 'queued' "
        "  AND (run_after IS NULL OR run_after <= datetime('now')) "
        f" {predicate} "
        "  ORDER BY id LIMIT 1"
        ") RETURNING *", params
    ).fetchone()
    conn.commit()
    return row


def set_progress(job_id: int, done: int, total: int | None = None,
                 label: str | None = None) -> None:
    """Record live progress for a running job.

    Its own short connection and transaction, deliberately: the caller is
    mid-job and may be holding batched writes of its own, and a progress update
    must never widen that transaction or fail the job it is only describing.

    MUST be called between the caller's own commits, not inside one — two
    concurrent writers under WAL is how you get SQLITE_BUSY. scanner._scan_library
    commits each file and reports right after, which is that safe
    point.

    Failure here is swallowed: progress is a display detail, and losing a
    progress tick must never abort real work that is otherwise succeeding."""
    payload: dict = {"done": done}
    if total is not None:
        payload["total"] = total
    if label is not None:
        payload["label"] = label
    try:
        with db.write() as conn:
            conn.execute("UPDATE jobs SET progress = ? WHERE id = ?",
                         (json.dumps(payload), job_id))
    except Exception:
        _log.debug("could not record progress for job %s", job_id, exc_info=True)


def _finish(conn, job_id: int, result) -> None:
    conn.execute(
        # progress cleared: `result` now says what the run did, and a lingering
        # "12431 / 58783" next to a finished job reads as though it stalled.
        "UPDATE jobs SET state = 'done', finished_at = datetime('now'), "
        "result = ?, last_error = NULL, progress = NULL WHERE id = ?",
        (json.dumps(result) if result is not None else None, job_id),
    )
    conn.commit()


def _stop_cancelled(conn, job_id: int, reason: str) -> None:
    """A job that stopped at a `report` because it was asked to. An admin's
    cancel ends it `cancelled`, which nothing retries. A shutdown queues it
    again and gives back the attempt its claim charged: it didn't fail, so it
    mustn't use up the retry budget. Progress is kept either way, so the panel
    still says where it stopped."""
    if reason == CANCEL_SHUTDOWN:
        _apply_on_retry(conn, "id = ?", (job_id,))
        conn.execute(
            "UPDATE jobs SET state = 'queued', started_at = NULL, "
            "attempts = MAX(attempts - 1, 0), "
            "last_error = 'the server shut down while this job was running; "
            "it has been queued again' WHERE id = ?",
            (job_id,),
        )
    else:
        conn.execute(
            "UPDATE jobs SET state = 'cancelled', finished_at = datetime('now') "
            "WHERE id = ?",
            (job_id,),
        )
    conn.commit()


def _fail(conn, job_id: int, attempts: int, error: str) -> None:
    """Back to `queued` with a delay while retries remain, else `failed`."""
    if attempts < _MAX_ATTEMPTS:
        delay = _BACKOFF_SECONDS[min(attempts, len(_BACKOFF_SECONDS)) - 1]
        _apply_on_retry(conn, "id = ?", (job_id,))
        conn.execute(
            "UPDATE jobs SET state = 'queued', last_error = ?, "
            "run_after = datetime('now', ?) WHERE id = ?",
            (error, f"+{delay} seconds", job_id),
        )
    else:
        conn.execute(
            "UPDATE jobs SET state = 'failed', finished_at = datetime('now'), "
            "last_error = ? WHERE id = ?",
            (error, job_id),
        )
    conn.commit()


def run_one(lane: str = LANE_SHORT) -> bool:
    """Claim and run a single job from `lane`. Returns True if one was run (so a
    caller can keep going while work remains), False if that lane was empty.

    Each job gets its own short-lived connection, released before the
    handler runs — the handler is exactly the kind of long I/O (audio
    decode, provider HTTP) that must never sit on an open connection, which
    was previously a rule enforced only by comments in four modules."""
    conn = db.get_conn()
    try:
        job = claim(conn, lane)
    finally:
        conn.close()
    if job is None:
        return False

    try:
        _run_claimed(job)
    finally:
        with _cancel_lock:
            _cancel_requests.pop(job["id"], None)
    return True


def _run_claimed(job) -> None:
    handler = _HANDLERS.get(job["type"])
    payload = json.loads(job["payload"]) if job["payload"] else {}
    try:
        if handler is None:
            # A queued type with no handler is a wiring bug, not a transient
            # failure — surface it as failed immediately rather than burning
            # the retry budget on something that can't succeed.
            raise LookupError(f"no handler registered for job type {job['type']!r}")
        def report(done: int, total: int | None = None,
                   label: str | None = None) -> None:
            _check_cancelled(job["id"])
            set_progress(job["id"], done, total, label)

        result = handler(payload, report)
    except JobCancelled as exc:
        _log.warning("job %s (%s) stopped: %s", job["id"], job["type"],
                     "cancelled by an admin" if exc.reason == CANCEL_ADMIN else "server shutting down")
        if exc.reason == CANCEL_ADMIN:
            with _cancel_lock:
                _cancelled_types.add(job["type"])
        conn = db.get_conn()
        try:
            _stop_cancelled(conn, job["id"], exc.reason)
        finally:
            conn.close()
        return
    except Exception as exc:
        _log.exception("job %s (%s) failed", job["id"], job["type"])
        conn = db.get_conn()
        try:
            attempts = _MAX_ATTEMPTS if isinstance(exc, LookupError) else job["attempts"]
            _fail(conn, job["id"], attempts, f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()
        return

    conn = db.get_conn()
    try:
        _finish(conn, job["id"], result)
    finally:
        conn.close()


def _worker_loop(lane: str) -> None:
    event = _wakes[lane]
    while not _stop.is_set():
        try:
            # Drain rather than one-per-wake, so a burst doesn't sit behind
            # the idle timeout. Not past a stop: a shutdown has just queued
            # the stopped job again, and would otherwise watch it be claimed.
            while not _stop.is_set() and run_one(lane):
                pass
            # #361: the other prune trigger (enqueue) never fires on an
            # instance where nothing is being queued, so a quiet instance's
            # job history would otherwise freeze at whatever it looked like
            # when activity stopped. Once per drain-then-wait cycle is
            # frequent enough without making every idle tick do a write.
            conn = db.get_conn()
            try:
                _prune_finished(conn)
            finally:
                conn.close()
            # #362: each hook gets its own try/except, not the blanket one
            # below — a broken hook must not stop _prune_finished (already
            # run above) or the OTHER hooks from happening this cycle. Runs
            # on both lanes' threads; harmless, since anything a hook does
            # (scanner.py's rescan check enqueues via the normal dedupe_key
            # guard) is already safe to call concurrently with itself.
            for hook in _idle_hooks:
                try:
                    hook()
                except Exception:
                    _log.exception("idle hook error")
        except Exception:
            # The loop itself must never die — that would silently end all
            # background work for the process's lifetime, which is precisely
            # the class of invisible failure this module exists to remove.
            _log.exception("job worker loop error (%s lane)", lane)
        event.wait(_IDLE_POLL_SECONDS)
        event.clear()


def start_worker() -> bool:
    """Start the single worker thread. Idempotent — a second call is a
    no-op, so an accidental double-bootstrap can't produce two workers
    racing for jobs. Returns True if this call started it.

    Called from main.py's __main__ only, never at import: a worker started
    at import would run inside the test suite (546 tests import main) and
    inside the `python3 -m scanner` CLI, in both cases operating on whatever
    db.DB_PATH happened to be set to."""
    if not _worker_started.acquire(blocking=False):
        return False
    conn = db.get_conn()
    try:
        requeue_interrupted(conn)
    finally:
        conn.close()
    global _worker_threads
    _stop.clear()
    _worker_threads = []
    for lane in _LANES:
        thread = threading.Thread(target=_worker_loop, args=(lane,),
                                  name=f"job-worker-{lane}", daemon=True)
        thread.start()
        _worker_threads.append(thread)
    return True


def stop_worker(timeout: float = 5.0) -> bool:
    """Bring the worker down and wait for it. Returns True if it stopped.

    Exists for tests: a real worker left running polls for the remainder of the
    suite and will claim jobs another test enqueued, executing them against
    whatever db.DB_PATH points at by then — cross-test mutation, not just log
    noise. In production, shutdown() calls it on SIGTERM.

    A thread still alive at the deadline stays in _worker_threads, and the
    worker stays started: a later stop_worker joins it, and start_worker
    can't start a second worker beside it, which would run with the stop
    cleared and take requests meant for the new one."""
    global _worker_threads
    _stop.set()
    for event in _wakes.values():  # don't wait out the idle poll
        event.set()
    threads = _worker_threads
    # One deadline for all the lanes, so the whole stop takes at most `timeout`.
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    _worker_threads = [thread for thread in threads if thread.is_alive()]
    if _worker_threads:
        return False
    if _worker_started.locked():
        _worker_started.release()
    return True


def shutdown(timeout: float = 5.0) -> bool:
    """The graceful stop for SIGTERM: ask every running job to stop at its next
    `report`, then stop the worker. A job that stops in time goes back in the
    queue (see _stop_cancelled); one that doesn't, because its handler is
    between reports or never reports, is still `running` when the process
    exits, and requeue_interrupted picks it up at the next start, as after a
    crash. Returns True if the worker stopped within `timeout`.

    `timeout` stays under docker stop's default of 10 seconds, so the process
    exits on its own instead of being killed."""
    # The stop first: a job asked to stop is queued again as soon as it
    # reaches a report, and the worker must not claim it back before the stop
    # is in place, or it would run to its end with no request left to stop it.
    _stop.set()
    conn = db.get_conn()
    try:
        running = [row["id"] for row in
                   conn.execute("SELECT id FROM jobs WHERE state = 'running'")]
    finally:
        conn.close()
    for job_id in running:
        request_cancel(job_id, CANCEL_SHUTDOWN)
    return stop_worker(timeout)


def status(conn) -> dict:
    """Counts by state, for the admin overview (#297 step 2) and for tests
    to assert against without reaching into SQL."""
    rows = conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
    counts = {row["state"]: row["n"] for row in rows}
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
    }


def recent(conn, limit: int = 50) -> list:
    """Most recent jobs first, for the admin overview."""
    rows = conn.execute(
        "SELECT id, type, state, attempts, last_error, result, progress, "
        "created_at, started_at, finished_at FROM jobs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {**dict(row),
         "result": json.loads(row["result"]) if row["result"] else None,
         "progress": json.loads(row["progress"]) if row["progress"] else None,
         # True from an admin's cancel until the job reaches its next `report`,
         # so the panel can say "Cancelling…" rather than look ignored.
         "cancelling": row["state"] == "running" and cancel_requested(row["id"])}
        for row in rows
    ]
