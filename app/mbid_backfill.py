#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A one-time pass that fills tracks.mbid, the MusicBrainz recording id, for
files scanned before that column existed.

The scanner only re-reads a file whose size or modification time changed, so
a library scanned before tracks.mbid existed would otherwise keep it empty
until each file happened to change. This pass re-reads the tags of every
library track whose mbid is still empty, once, and records that it ran in
app_config, so a library whose files carry no MusicBrainz ids isn't re-read
on every start.

It runs in the long job lane (the same lane as a library scan, so the two
never overlap), with progress in the Background jobs panel. Reading tags is
local file I/O only: no audio decoding, no network."""

import logging
from datetime import datetime, timezone

from tinytag import TinyTag

import db
import jobs
import scanner

_log = logging.getLogger(__name__)

JOB_TYPE = "mbid_backfill"
DONE_KEY = "mbid_backfill_done_at"
# A pass that fails is queued again on a later idle tick, up to this many
# times; then it stops and the log says why, rather than retrying for ever.
FAILURES_KEY = "mbid_backfill_failures"
MAX_FAILURES = 3
# Tags are read with no transaction open, and what they gave is written at
# this many files, in one short transaction: a login or a sync must never wait
# for file reads. Progress is reported at the same point, with nothing pending.
BATCH = 50


def maybe_queue() -> None:
    """jobs.on_idle hook: queue the pass until it has run once. The queue's
    dedupe key keeps it to one queued or running job, however often the
    hook runs and on whichever lane. Not after an admin has cancelled it,
    until the server restarts."""
    conn = db.get_conn()
    try:
        if db.get_config(conn, DONE_KEY):
            return
        if int(db.get_config(conn, FAILURES_KEY) or 0) >= MAX_FAILURES:
            return
        # An admin's cancel holds the pass until the next start.
        if jobs.cancelled_this_run(JOB_TYPE):
            return
        jobs.enqueue(conn, JOB_TYPE, dedupe_key=JOB_TYPE)
    finally:
        conn.close()


def run_job(_payload: dict | None = None, report=None) -> dict:
    """Job handler. Reads the recording id from the tags of every library
    track that has none, records each one found, and marks the pass done.
    Returns counts for the jobs panel."""
    root = db.get_music_root()
    conn = db.get_conn()
    try:
        return _run(conn, root, report)
    except Exception:
        conn.rollback()
        failures = int(db.get_config(conn, FAILURES_KEY) or 0) + 1
        db.set_config(conn, FAILURES_KEY, str(failures))
        conn.commit()
        _log.warning("MusicBrainz recording-id pass failed (%d of %d attempts)%s", failures, MAX_FAILURES,
                     "; giving up until the failure is cleared from app_config" if failures >= MAX_FAILURES else "",
                     exc_info=True)
        raise
    finally:
        conn.close()


def _write(conn, pending: list[tuple[str, int]]) -> None:
    """One short transaction for the ids read so far. With none, no
    transaction: taking the write lock can wait, and could fail the pass."""
    if not pending:
        return
    with db.write(conn):
        conn.executemany("UPDATE tracks SET mbid = ? WHERE id = ? AND mbid IS NULL", pending)


def _run(conn, root, report) -> dict:
    """The pass itself, on `conn`; run_job handles a failure."""
    rows = conn.execute(
        "SELECT id, relative_path FROM tracks WHERE deleted_at IS NULL AND mbid IS NULL ORDER BY id"
    ).fetchall()
    total = len(rows)
    found = unreadable = 0
    pending: list[tuple[str, int]] = []
    for done, row in enumerate(rows):
        if done % BATCH == 0:
            _write(conn, pending)
            pending = []
            if report is not None:
                report(done, total)
        try:
            tag = TinyTag.get(root / row["relative_path"])
        except Exception:
            unreadable += 1
            continue
        mbid = scanner._recording_mbid(tag.other)
        if mbid:
            pending.append((mbid, row["id"]))
            found += 1
    _write(conn, pending)
    db.set_config(conn, DONE_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    conn.commit()
    if report is not None:
        report(total, total)
    _log.info("MusicBrainz recording ids: %d found in %d files (%d unreadable)", found, total, unreadable)
    return {"checked": total, "found": found, "unreadable": unreadable}
