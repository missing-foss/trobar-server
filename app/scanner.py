#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Incremental library scanner — walks the NFS music root, reads tags via
tinytag, and keeps the `tracks` table in sync.

Incremental by design: a file is only re-read (tag parse) when its mtime or
size has changed since the last scan. Files that vanish are not hard-deleted
immediately — they're soft-deleted (`deleted_at` set) so any device that
still has them in `device_track_state` can be told to remove them on its next
sync before the row is garbage-collected.

tinytag (MIT, read-only) replaced mutagen (GPL) in read-only is
all this app ever needs, it decouples the license from GPL, and its API is a
touch simpler (track/disc come pre-parsed, one uniform image accessor across
formats). The one subtlety it forces is the original-vs-reissue year handling
below.
"""

import json
import os
import logging
import re
import threading
from pathlib import Path

from tinytag import TinyTag

import covers
import db
import fingerprint
import jobs
import provenance

_log = logging.getLogger(__name__)

# A full NFS scan takes tens of minutes and holds the DB; this guard makes a
# second concurrent trigger a fast no-op instead of piling on another walk
# (any authenticated user can hit /api/library/scan).
# #141 (single process): module-global, so it only coordinates within ONE
# process — fine under single-process waitress, but a multi-process server
# (gunicorn -w N) would need a shared lock instead. playlist_sync no longer
# has an equivalent — #297 step 3 moved it onto the job queue, whose
# dedupe_key guard is a DB constraint and holds across a restart too.
_SCAN_LOCK = threading.Lock()

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".alac"}

# Commit every N processed files instead of once at the very end — a full
# scan over NFS took ~50min in production and held a single uncommitted
# transaction the whole time, during which every other request got
# "database is locked" (see db.py's busy_timeout/WAL fix, which papers over
# the symptom but periodic commits also avoid losing all progress if the
# process dies mid-scan).
# 500 → 50 (follow-up): on a force rescan every file's tags are
# re-read from NFS *inside* the open write transaction, so a 500-track batch
# held the write lock past db.py's 30s busy_timeout and live device syncs
# got 500s ("database is locked"). 50 tracks ≈ a couple of seconds of lock,
# comfortably inside the timeout.
_COMMIT_EVERY = 50
_YEAR_RE = re.compile(r"(\d{4})")
_DATE_RE = re.compile(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


def _int_field(value) -> int | None:
    """tinytag usually hands back track/disc as ints already, but be defensive:
    also accept a raw "3/12"-style string and take the leading number."""
    if value is None:
        return None
    try:
        return int(str(value).split("/")[0])
    except (ValueError, TypeError):
        return None


def _first(value):
    """tinytag's `.other` values are lists; grab the first, or None."""
    return value[0] if value else None


def _year4(value) -> int | None:
    """First 4-digit year found in a date-ish string, or None."""
    if not value:
        return None
    match = _YEAR_RE.search(str(value))
    return int(match.group(1)) if match else None


def _release_date4(value) -> str | None:
    """Best-effort YYYY-MM-DD from the same date-ish tag string _year4
    reads, defaulting a missing month/day to 01 — kept at full precision
    when the tag actually has one (most don't), since "recently released"
    needs real month granularity that a bare year can't give it."""
    if not value:
        return None
    match = _DATE_RE.search(str(value))
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{month or '01'}-{day or '01'}"


def _read_tags(path: Path) -> dict:
    """Best-effort tag extraction with folder/filename fallbacks.

    Folder layout is always Artist/Album/Track — fall back to that
    structure when a file is missing or has malformed tags rather than
    dropping it from the catalog.
    """
    artist = album = title = None
    track_no = disc_no = year = reissue_year = None
    duration = release_date = isrc = None

    try:
        tag = TinyTag.get(path)
    except Exception:
        tag = None

    if tag is not None:
        artist = tag.artist or None
        album = tag.album or None
        title = tag.title or None
        duration = tag.duration or None # seconds — (autofit estimates)
        track_no = _int_field(tag.track)
        disc_no = _int_field(tag.disc)
        # `year` = ORIGINAL release year: tinytag's `.year` is the file's
        # date/reissue field (for a remaster, the reissue year — e.g. 2022 for
        # a 2022 pressing of a 1966 album), so prefer originaldate/originalyear
        # from the extended tags and only fall back to `.year`. The library
        # sorts by this. `reissue_year` = the file's own date/pressing year,
        # kept separately so it can be shown when it differs.
        reissue_year = _year4(tag.year)
        year = _year4(_first(tag.other.get("originaldate"))) \
            or _year4(_first(tag.other.get("originalyear"))) \
            or reissue_year
        # Same precedence as `year` above, just kept at whatever precision
        # the tag actually has instead of truncated to a bare year.
        release_date = _release_date4(_first(tag.other.get("originaldate"))) \
            or _release_date4(_first(tag.other.get("originalyear"))) \
            or _release_date4(tag.year)
        # #200: free — tinytag already parses this from ID3 TSRC/TRC,
        # FLAC/Vorbis comments, WM/ISRC. NULL for most files (poorly-tagged
        # rips, formats that never carried it) — the identity resolver
        # copes with that via its later tiers.
        isrc = _first(tag.other.get("isrc"))

    parts = path.parts
    if album is None and len(parts) >= 2:
        album = parts[-2]
    if artist is None and len(parts) >= 3:
        artist = parts[-3]
    if title is None:
        title = path.stem

    return {
        "artist": artist or "Unknown Artist",
        "album": album or "Unknown Album",
        "title": title,
        "track_no": track_no,
        "disc_no": disc_no,
        "year": year,
        "reissue_year": reissue_year,
        "duration": duration,
        "release_date": release_date,
        "isrc": isrc,
    }


def _iter_audio_files(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if Path(fname).suffix.lower() in AUDIO_EXTENSIONS:
                yield Path(dirpath) / fname


def scan_library(root: Path, force: bool = False) -> dict:
    """Walk `root`, upsert changed/new tracks, soft-delete vanished ones.

    `force=True` re-reads tags for every file even if (size, mtime) didn't
    change — needed once after adding a new extracted field (e.g. `year`):
    an incremental scan would otherwise never touch already-cataloged files
    again, so the new field would stay NULL forever for existing rows.

    Returns counts: {"added": n, "updated": n, "removed": n, "unchanged": n}.
    If a scan is already in progress, returns {"already_running": True} without
    starting a second one.
    """
    if not _SCAN_LOCK.acquire(blocking=False):
        return {"added": 0, "updated": 0, "removed": 0, "unchanged": 0,
                "already_running": True}
    try:
        return _scan_library(root, force=force)
    finally:
        _SCAN_LOCK.release()


# #297 step 3: the scan is a JOB now, not a bespoke thread.
#
# It used to be a daemon thread guarded by _SCAN_LOCK, with its result in a
# module global. Three consequences, all of which bit in production on
# 2026-07-26 when a full rescan of 58,783 tracks was started and nobody could
# tell whether it was alive:
#
#   - the only evidence a scan was running was a lock nobody can query, and the
#     status endpoint needs auth, so there was no way to check at all;
#   - a restart killed the thread silently and reset _last_scan_result to None,
#     which is indistinguishable from "never ran";
#   - there was no progress anywhere, so "still going" and "died" looked alike.
#
# As a job it gets a durable row, live progress in the Background jobs panel,
# and the boot reaper requeues it after a restart (cheap: unchanged files are
# skipped on the re-run). Runs in the LONG lane so it can't starve a device's
# provenance rematch — see jobs._LANE_BY_TYPE.
JOB_TYPE = "library_scan"
# One scan queued or running at a time, enforced by the partial unique index on
# jobs.dedupe_key instead of by _SCAN_LOCK. Not per-root: there is exactly one
# music library.
JOB_DEDUPE = "library_scan"


def run_job(payload: dict, report) -> dict:
    """Job handler. `force` comes from the payload; the music root is resolved
    HERE rather than stored in it, so a scan requeued after a restart uses the
    currently-configured library instead of a path that may have changed."""
    root = db.get_music_root()
    if not root.is_dir():
        # Raise: a missing library is a real failure the admin should see in the
        # panel, not a silent zero-count "success".
        raise FileNotFoundError(f"music root {root} is not a readable directory")
    # Still take _SCAN_LOCK: the dedupe key stops a second scan JOB, but the
    # synchronous scan_library() path (the `python3 -m scanner` CLI) doesn't go
    # through the queue, and two concurrent walks writing the same rows is what
    # the lock has always been for.
    if not _SCAN_LOCK.acquire(blocking=False):
        raise RuntimeError("another library scan is already running in this process")
    try:
        result = _scan_library(root, force=bool(payload.get("force")), report=report)
    finally:
        _SCAN_LOCK.release()
    # The scan SUCCEEDED by this point, so nothing below may turn it into a
    # failure: the counts are the user-visible outcome and must survive a broken
    # follow-up. Previously guaranteed by stashing the result before enqueueing;
    # now the enqueue comes first in the return path, so it needs the guard.
    try:
        _queue_post_scan_jobs()
    except Exception:
        _log.exception("scan finished but queueing the fingerprint backfill failed")
    return result


def scan_status() -> dict:
    """{"running": bool, "last_result": ..., "progress": ..., "last_scan_at": ...}.

    Same shape #140 established (the web UI polls it; nothing else consumes it —
    checked across the android, desktop and garmin clients), but read from the
    jobs table, so it now survives a restart and can report how far along a
    running scan is. `last_scan_at` (#475) is a later, purely additive field —
    existing consumers that don't know about it simply ignore an extra key.

    `running` covers QUEUED as well as running: from the caller's point of view a
    scan that is waiting for the long lane has been started and is going to
    happen, and reporting it as not-running would make the UI offer to start
    another one."""
    conn = db.get_conn()
    try:
        pending = conn.execute(
            "SELECT progress FROM jobs WHERE type = ? AND state IN ('queued', 'running') "
            "ORDER BY id LIMIT 1", (JOB_TYPE,)).fetchone()
        finished = conn.execute(
            "SELECT state, result, last_error, finished_at FROM jobs WHERE type = ? "
            "AND state IN ('done', 'failed') ORDER BY id DESC LIMIT 1",
            (JOB_TYPE,)).fetchone()
    finally:
        conn.close()

    last_result = None
    last_scan_at = None
    # #141: a poll DURING a run must not report the previous scan's counts — that
    # reads as "finished, and here are your numbers" while it is still going. The
    # old implementation cleared its global on start; the DB keeps every finished
    # job, so this suppresses it instead. last_scan_at is suppressed the same way
    # and for the same reason -- it's "when did the last scan we're reporting on
    # finish", which has no answer while one is in progress.
    if pending is not None:
        finished = None
    if finished is not None:
        last_scan_at = finished["finished_at"]
        if finished["state"] == "failed":
            last_result = {"status": "error",
                           "reason": finished["last_error"] or "error"}
        elif finished["result"]:
            last_result = json.loads(finished["result"])
    progress = None
    if pending is not None and pending["progress"]:
        progress = json.loads(pending["progress"])
    return {"running": pending is not None, "last_result": last_result,
            "progress": progress, "last_scan_at": last_scan_at}


def start_scan(root: Path, force: bool = False) -> dict:
    """Queue a scan and return immediately.

    `root` is accepted for call-site compatibility but deliberately NOT stored in
    the payload — run_job resolves the configured library at run time. Kept in
    the signature because the setup wizard validates the path it is about to
    scan, and dropping the argument would make that read as though the two were
    unrelated.

    Returns {"status": "started"}, or {"status": "error", "already_running":
    True} when one is already queued or running — the dedupe index decides that,
    not a lock, so it holds across a restart too."""
    conn = db.get_conn()
    try:
        job_id = jobs.enqueue(conn, JOB_TYPE, {"force": bool(force)},
                              dedupe_key=JOB_DEDUPE)
    finally:
        conn.close()
    if job_id is None:
        return {"status": "error", "already_running": True}
    return {"status": "started", "job_id": job_id}


def _config_hours(conn) -> int:
    raw = db.get_config(conn, "scan_interval_hours")
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


def next_scheduled_scan_at(conn) -> str | None:
    """#362: when the next scheduled scan becomes due, as a UTC datetime
    string (same shape as jobs.finished_at) — or None when scheduling is off
    (scan_interval_hours <= 0). "Make the next scheduled run visible" was
    explicit in the issue's maintainer decision: a schedule nobody can see
    recreates the exact invisible-background-mechanism problem #297 set out
    to fix. Shares its due-time math with maybe_schedule_rescan below rather
    than each recomputing it separately."""
    hours = _config_hours(conn)
    if hours <= 0:
        return None
    last = conn.execute(
        "SELECT finished_at FROM jobs WHERE type = ? AND state = 'done' "
        "ORDER BY finished_at DESC LIMIT 1", (JOB_TYPE,),
    ).fetchone()
    if last is None:
        return None  # never scanned: due now, not at some future timestamp
    return conn.execute(
        "SELECT datetime(?, ?)", (last["finished_at"], f"+{hours} hours"),
    ).fetchone()[0]


def maybe_schedule_rescan() -> None:
    """#362: registered with jobs.on_idle (see main.py), so this runs once
    per worker idle cycle regardless of whether anything is being enqueued.

    Enqueues an incremental scan (never force=1 — a periodic forced rescan
    would re-read every tag in the library on a timer) when scheduled
    scanning is enabled and enough time has passed since the last scan
    FINISHED, per the issue's explicit choice: measuring from completion
    rather than a wall-clock cadence means an hours-long scan doesn't
    immediately become eligible again the moment it ends.

    A library that has never been scanned is treated as immediately due —
    enabling this on a fresh install shouldn't mean waiting a full interval
    before the first automatic scan ever runs. start_scan's own dedupe_key
    is what stops this from stacking on a scan already queued or running,
    the same guard a manual click goes through, so no separate lock is
    needed here."""
    conn = db.get_conn()
    try:
        hours = _config_hours(conn)
        if hours <= 0:
            return
        last = conn.execute(
            "SELECT finished_at FROM jobs WHERE type = ? AND state = 'done' "
            "ORDER BY finished_at DESC LIMIT 1", (JOB_TYPE,),
        ).fetchone()
        due = last is None or conn.execute(
            "SELECT ? <= datetime('now', ?)", (last["finished_at"], f"-{hours} hours"),
        ).fetchone()[0]
    finally:
        conn.close()
    if due:
        start_scan(db.get_music_root())


def _queue_post_scan_jobs() -> None:
    """#200/#297: queue the AcoustID/MusicBrainz fingerprint backfill that
    follows a scan — real audio decode plus two paced external HTTP calls per
    track.

    Enqueued rather than called, which changes two things. It no longer runs in
    the scan thread at all, so "never do real I/O while holding _SCAN_LOCK"
    stops being a rule kept by careful ordering inside start_scan's _run() and
    becomes structural: the job worker is a different thread that has never
    heard of _SCAN_LOCK. And a failure now persists in jobs.last_error, where
    an admin can see and retry it, rather than in a `_log.exception` nobody
    reads.

    dedupe_key is the job type itself, so two scans finishing close together
    queue one backfill, not two — the same guarantee fingerprint.py's own
    _FINGERPRINT_LOCK gives, but enforced by a unique index instead of a lock.

    Its own function (not inline in _run) so it's a single seam: the whole
    step, connection included, can be mocked in tests that deliberately have no
    DB. Swallows everything — a completed scan must still report its counts
    even if queueing the follow-up work fails."""
    try:
        conn = db.get_conn()
        try:
            # TWO jobs, and the split matters. Computing a chromaprint is local
            # and needs no API key; only resolving it to an ISRC calls AcoustID.
            # Until this queued both, an install with no AcoustID key got NEITHER
            # — the backfill returned early on the missing key, and the keyless
            # pass was only ever triggered by a device pushing provenance. So
            # tracks.fingerprint stayed NULL and #239's recovery-by-fingerprint
            # could not work at all. Found in production on v2.3.0, after a
            # completed full rescan left every fingerprint NULL.
            # #321: only if a device is enrolled. The keyless pass exists solely
            # for device recovery — #200's playlist matching uses
            # acoustid_isrc, which needs the API key — so on an install that syncs
            # nothing it is hours of audio decode for zero benefit. Gating on
            # devices is correct by default and adds no setting to explain.
            #
            # No hook on enrolment is needed: the device-facing routes already
            # queue this pass when a device syncs and the library is incomplete
            # (see api_device_changes / the provenance push), so a newly enrolled
            # device triggers it without waiting for the next scan.
            #
            # After a server-DB loss — #239's actual disaster case — `devices` is
            # empty by definition, so nothing is queued until a device is
            # re-enrolled. That is the right order anyway: rematch_device already
            # defers while the library is incomplete.
            if conn.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
                jobs.enqueue(conn, provenance.JOB_TYPE_LIBRARY_FINGERPRINTS,
                             dedupe_key=provenance.JOB_TYPE_LIBRARY_FINGERPRINTS)
            jobs.enqueue(conn, fingerprint.JOB_TYPE, dedupe_key=fingerprint.JOB_TYPE)
        finally:
            conn.close()
    except Exception:
        _log.exception("could not enqueue the post-scan jobs")


def _scan_library(root: Path, force: bool = False, report=None) -> dict:
    conn = db.get_conn()
    counts = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
    seen_paths: set[str] = set()
    # Albums whose art may have changed — their cached cover is dropped at the
    # end so replaced/edited art propagates without a manual refresh.
    touched_albums: set[tuple[str, str]] = set()

    # A forced rescan re-reads the whole library, so start the cover cache clean.
    if force:
        covers.clear_all()

    # #297 step 3: a counting pre-pass, so progress can read "12431 / 58783"
    # rather than a bare number that says nothing about how much is left. Costs a
    # second os.walk but NOT a second round of tag reads, which is where a scan's
    # time actually goes. Skipped entirely when nobody is watching (the CLI and
    # the synchronous scan_library path pass no report).
    processed = 0
    total = None
    if report is not None:
        report(0, None, "counting files")
        total = sum(1 for _ in _iter_audio_files(root))
        report(0, total)

    def tick() -> None:
        """Progress may only be written when this connection has NOTHING pending:
        writes are batched to _COMMIT_EVERY, and a second connection writing
        mid-batch is how you get SQLITE_BUSY under WAL. Every call site below is
        immediately after a commit, or on a path that wrote nothing at all."""
        if report is not None and processed % _COMMIT_EVERY == 0:
            report(processed, total)

    try:
        existing = {
            row["relative_path"]: row
            for row in conn.execute(
                "SELECT id, relative_path, artist, album, size, mtime, deleted_at FROM tracks"
            )
        }

        for abs_path in _iter_audio_files(root):
            rel_path = str(abs_path.relative_to(root))
            seen_paths.add(rel_path)
            stat = abs_path.stat()
            size, mtime = stat.st_size, stat.st_mtime

            row = existing.get(rel_path)
            # Derived once, right next to the skip test below that shares its
            # terms, so the two can't drift apart: "the bytes on disk differ
            # from what we recorded". NOT the same thing as reaching the
            # update branch — a force=True rescan re-reads tags for every
            # file, unchanged ones included, and the fingerprint
            # invalidation further down must not fire for those.
            content_changed = row is None or row["size"] != size \
                or abs(row["mtime"] - mtime) >= 1.0
            if not force and row is not None and row["deleted_at"] is None \
                    and not content_changed:
                counts["unchanged"] += 1
                processed += 1
                tick()  # nothing was written for this file, so no batch is open
                continue

            tags = _read_tags(abs_path)
            touched_albums.add((tags["artist"], tags["album"]))
            if row is None:
                conn.execute(
                    "INSERT INTO tracks (relative_path, artist, album, title, track_no, "
                    "disc_no, year, reissue_year, duration, release_date, isrc, size, mtime, "
                    "scanned_at, deleted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), NULL)",
                    (rel_path, tags["artist"], tags["album"], tags["title"],
                     tags["track_no"], tags["disc_no"], tags["year"], tags["reissue_year"],
                     tags["duration"], tags["release_date"], tags["isrc"], size, mtime),
                )
                counts["added"] += 1
            else:
                # If the track moved to a different album/artist, the old
                # album's cached cover may now be stale too.
                touched_albums.add((row["artist"], row["album"]))
                # scanned_at is deliberately NOT touched here — it means
                # "first seen", not "last touched" (see suggestions.py's
                # recently_added() and the dashboard's Recently Added
                # widget, both of which read it as an "added" signal). A
                # forced full rescan re-verifying every unchanged tag must
                # not reset it for the whole library.
                conn.execute(
                    "UPDATE tracks SET artist=?, album=?, title=?, track_no=?, disc_no=?, "
                    "year=?, reissue_year=?, duration=?, release_date=?, isrc=?, size=?, mtime=?, "
                    "deleted_at=NULL "
                    "WHERE id=?",
                    (tags["artist"], tags["album"], tags["title"], tags["track_no"],
                     tags["disc_no"], tags["year"], tags["reissue_year"], tags["duration"],
                     tags["release_date"], tags["isrc"], size, mtime, row["id"]),
                )
                if content_changed:
                    # #239: the fingerprint/AcoustID cache describes the AUDIO,
                    # so different bytes invalidate all of it. Until this
                    # existed, a re-encoded or re-tagged file kept a
                    # fingerprint of its OLD content forever — harmless while
                    # the column was write-only, but provenance now ships it to
                    # clients as identity and matches on it during recovery, so
                    # a stale value means confidently identifying the wrong
                    # track. Clearing fingerprint_checked_at too lets the
                    # AcoustID backfill re-resolve an ISRC for the new content.
                    #
                    # Gated on content_changed, NOT merely on being in this
                    # branch: force=True lands every unchanged file here as
                    # well, and clearing unconditionally would wipe the whole
                    # library's fingerprints and re-trigger a full AcoustID
                    # re-lookup (rate-limited to ~1 track/sec) on every forced
                    # rescan.
                    # fingerprint_failed_at goes too: new bytes deserve a
                    # full-priority attempt. The common case is exactly a
                    # half-copied file that failed to decode being replaced by
                    # the complete one — it must not stay deprioritised for a
                    # failure that no longer applies to the file that's there.
                    conn.execute(
                        "UPDATE tracks SET fingerprint=NULL, acoustid_isrc=NULL, "
                        "acoustid_mbid=NULL, fingerprint_checked_at=NULL, "
                        "fingerprint_failed_at=NULL, fingerprint_seq=NULL WHERE id=?",
                        (row["id"],),
                    )
                counts["updated"] += 1

            processed += 1
            if (counts["added"] + counts["updated"]) % _COMMIT_EVERY == 0:
                conn.commit()
                if report is not None:
                    report(processed, total)
            else:
                tick()

        # #322: deleted_at MUST be UTC, like every other timestamp in the schema.
        # This used to be time.strftime(), which uses the process timezone, so
        # deleted_at sat in a different zone from scanned_at in the same row.
        #
        # The issue that reported it assumed this was latent because the image
        # lacked tzdata and the process was pinned to UTC. That is wrong —
        # verified in the shipped image: tzdata IS installed (pulled in
        # transitively), TZ=Europe/Paris resolves, and python reports CEST +0200.
        # So this was LIVE, not latent, on any install with TZ set. Nothing had
        # been corrupted yet only because no track had been deleted.
        #
        # Written by SQLite rather than Python so there is one source of "now" for
        # the whole schema and no second place to get the zone wrong.
        for rel_path, row in existing.items():
            if rel_path not in seen_paths and row["deleted_at"] is None:
                conn.execute(
                    "UPDATE tracks SET deleted_at=datetime('now') WHERE id=?",
                    (row["id"],)
                )
                touched_albums.add((row["artist"], row["album"]))
                counts["removed"] += 1

        # Garbage-collect soft-deleted tracks no device still references.
        conn.execute(
            "DELETE FROM tracks WHERE deleted_at IS NOT NULL AND id NOT IN "
            "(SELECT track_id FROM device_track_state)"
        )

        conn.commit()
    finally:
        conn.close()

    # Drop cached covers for every album that gained/lost/changed a track, so
    # the next browse re-extracts current art. (No-op after a forced rescan,
    # which already cleared the whole cache.)
    if not force:
        for artist, album in touched_albums:
            covers.invalidate(artist, album)

    return counts


if __name__ == "__main__":
    import sys
    db.init_db()
    result = scan_library(db.get_music_root())
    print(result, file=sys.stderr)
