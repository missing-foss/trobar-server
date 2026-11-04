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
import time
from pathlib import Path

from tinytag import TinyTag

import covers
import db
import fingerprint
import jobs
import album_artist
import matching
import provenance
import sync_state

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

# What the scan reads into the library. ALAC lives in .m4a files (there is no
# .alac extension); AIFF is scanned so the lossless set sync_state transcodes
# (_LOSSLESS_EXTS) is all reachable. docs/using/library-selections.md lists
# these, with what a transcoding device gets for each.
AUDIO_EXTENSIONS = {".flac", ".wav", ".aiff", ".aif", ".mp3", ".m4a", ".ogg", ".opus", ".aac"}

# How many files between progress reports. Writes aren't batched: each file's
# are committed before the next file is read (see tick() in _scan_library),
# because a transaction held across file reads over NFS shuts out every other
# writer, and a scan can take close to an hour.
_REPORT_EVERY = 50
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


_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _recording_mbid(other: dict) -> str | None:
    """The file's MusicBrainz recording id, from the tag each format keeps it
    in as tinytag reports it: FLAC/Vorbis `musicbrainz_trackid`, MP4
    `musicbrainz track id`, and ID3's UFID frame, which tinytag gives raw as
    "http://musicbrainz.org" NUL id. Not the release-track id
    (`musicbrainz_releasetrackid`), which names one copy on one release.
    Lower-cased; anything that isn't a UUID is ignored."""
    candidates = [_first(other.get("musicbrainz_trackid")), _first(other.get("musicbrainz track id"))]
    for ufid in other.get("ufid") or []:
        if isinstance(ufid, bytes):
            ufid = ufid.decode("latin-1")
        owner, sep, value = str(ufid).partition("\x00")
        if sep and owner == "http://musicbrainz.org":
            candidates.append(value)
    for value in candidates:
        if isinstance(value, str) and _MBID.fullmatch(value.strip().lower()):
            return value.strip().lower()
    return None


# Lossless whatever their content. An .m4a is lossless when it holds ALAC,
# which tinytag reports with a bit depth; AAC has none.
_LOSSLESS_SUFFIXES = {".flac", ".wav", ".aiff", ".aif"}


def _lossless(path: Path, tag) -> int:
    suffix = path.suffix.lower()
    if suffix in _LOSSLESS_SUFFIXES:
        return 1
    if suffix == ".m4a":
        return 1 if tag is not None and getattr(tag, "bitdepth", None) else 0
    return 0


def _redeliver_as_transcoded(conn, track_ids: list) -> None:
    """These .m4a tracks turned out to be ALAC, so a device set to an MP3
    format should have them as .mp3, not the .m4a it was given: queue them
    again, the way a format change does. delivered_path keeps naming the .m4a
    on the device. Does not commit."""
    for start in range(0, len(track_ids), 500):
        chunk = track_ids[start:start + 500]
        conn.execute(
            "UPDATE device_track_state SET status = 'pending', bytes_on_device = NULL, "
            "updated_at = datetime('now') "
            f"WHERE status = 'downloaded' AND track_id IN ({','.join('?' * len(chunk))}) "
            "AND device_id IN (SELECT id FROM devices WHERE transcode_format IS NOT NULL)",
            chunk)


def _move_key(size: int, tags) -> tuple:
    """What a moved file keeps: its size and its tags. tags is _read_tags()'s
    dict or a tracks row."""
    duration = tags["duration"]
    return (size, tags["artist"], tags["album"], tags["title"], tags["track_no"], tags["disc_no"],
            round(duration, 1) if duration else None)


def _match_moves(vanished: list, added: list) -> list:
    """Pairs (vanished row, added file) whose keys match one-to-one. A key
    shared by two vanished tracks, or by two added files, is ambiguous and
    left as a removal and an addition."""
    by_key_old: dict = {}
    for row in vanished:
        by_key_old.setdefault(_move_key(row["size"], row), []).append(row)
    by_key_new: dict = {}
    for new in added:
        by_key_new.setdefault(new["key"], []).append(new)
    return [(olds[0], by_key_new[key][0]) for key, olds in by_key_old.items()
            if len(olds) == 1 and len(by_key_new.get(key, [])) == 1]


# Copied from the file's new row onto the moved track: where it is now and
# what was read from it. The id, scanned_at ("first seen") and the
# fingerprint columns stay the moved track's.
_MOVED_COLUMNS = ("relative_path", "size", "mtime", "year", "reissue_year", "release_date",
                  "isrc", "mbid", "artist_norm", "title_norm", "album_artist_tag",
                  "album_artist", "album_artist_read", "lossless")


# What can point at a track. The scan commits file by file, so a new row is
# visible to a playlist sync or a provenance rematch running beside a long
# scan, which may link to it before the move is found.
_REFERENCES = (
    "UPDATE playlist_tracks SET matched_track_id = :old WHERE matched_track_id = :new",
    "UPDATE device_provenance SET matched_track_id = :old WHERE matched_track_id = :new",
    "UPDATE unresolved_playlist_tracks SET suggested_track_id = :old WHERE suggested_track_id = :new",
    "UPDATE OR IGNORE device_track_state SET track_id = :old WHERE track_id = :new",
    "UPDATE OR IGNORE autofit_tracks SET track_id = :old WHERE track_id = :new",
    "UPDATE OR IGNORE selections SET target = CAST(:old AS TEXT) WHERE type = 'track' AND target = CAST(:new AS TEXT)",
)


def _move_track(conn, old_id: int, new_id: int) -> None:
    """The track old_id moved to the file the scan just added as new_id:
    give old_id that file, point whatever already found new_id at old_id
    (a duplicate of what old_id already has is dropped with new_id; a track
    selection's devices join the one old_id has), and drop new_id."""
    cols = ", ".join(_MOVED_COLUMNS)
    values = conn.execute(f"SELECT {cols} FROM tracks WHERE id = ?", (new_id,)).fetchone()
    for statement in _REFERENCES:
        conn.execute(statement, {"old": old_id, "new": new_id})
    # A track selection left on new_id is one its user also has on old_id
    # (one per type, target and user): its devices move to that one.
    conn.execute(
        "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) "
        "SELECT o.id, sd.device_id FROM selections n "
        "JOIN selection_devices sd ON sd.selection_id = n.id "
        "JOIN selections o ON o.type = 'track' AND o.target = ? AND o.created_by_user_id = n.created_by_user_id "
        "WHERE n.type = 'track' AND n.target = ?", (str(old_id), str(new_id)))
    conn.execute("DELETE FROM selections WHERE type = 'track' AND target = ?", (str(new_id),))
    conn.execute("DELETE FROM tracks WHERE id = ?", (new_id,))
    conn.execute(f"UPDATE tracks SET {', '.join(c + ' = ?' for c in _MOVED_COLUMNS)} WHERE id = ?",
                 (*values, old_id))


def _read_tags(path: Path) -> dict:
    """Best-effort tag extraction with folder/filename fallbacks.

    Folder layout is always Artist/Album/Track — fall back to that
    structure when a file is missing or has malformed tags rather than
    dropping it from the catalog.
    """
    artist = album = title = album_artist_tag = None
    track_no = disc_no = year = reissue_year = None
    duration = release_date = isrc = mbid = None

    try:
        tag = TinyTag.get(path)
    except Exception:
        tag = None

    if tag is not None:
        artist = tag.artist or None
        album_artist_tag = (tag.albumartist or "").strip() or None
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
        mbid = _recording_mbid(tag.other)

    # Counted by the scan (`from_folder` in its result, over the files it read,
    # not the ones it skipped as unchanged): a file whose artist, album or
    # title had to come from its folder or file name has tags worth fixing.
    from_folder = tag is None or artist is None or album is None or title is None
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
        "mbid": mbid,
        "album_artist_tag": album_artist_tag,
        "lossless": _lossless(path, tag),
        "from_folder": from_folder,
    }


# A scan that would remove more than this share of the library's live
# tracks, or that found no audio files at all in a library that has some, is
# refused unless an admin confirms it: an unmounted share or a mount that came
# up empty looks exactly like "everything was deleted", and the removal is
# followed by a hard delete of every track no device holds.
MASS_REMOVAL_FRACTION = 0.5


class MassRemovalRefused(RuntimeError):
    """The scan would remove too much of the library to be believed; nothing
    was removed. Raised so the job fails, and says why, in the admin panel."""


def _iter_audio_files(root: Path, unreadable: list | None = None):
    """Every audio file under `root`. A folder that can't be listed
    (permission denied, a mount gone mid-walk) is skipped, and its path is
    appended to `unreadable` so the caller can tell "not there" from "not
    seen"."""
    def onerror(err: OSError) -> None:
        if unreadable is not None:
            unreadable.append(err.filename)
            _log.warning("library scan: can't read %s: %s", err.filename, err.strerror)

    for dirpath, _dirnames, filenames in os.walk(root, onerror=onerror):
        for fname in filenames:
            if Path(fname).suffix.lower() in AUDIO_EXTENSIONS:
                yield Path(dirpath) / fname


def bump_covers_epoch() -> None:
    """Changes app_config's covers_epoch, the cache-busting part of every
    cover URL the web page builds."""
    with db.connection() as conn:
        db.set_config(conn, "covers_epoch", str(time.time_ns()))
        conn.commit()


def scan_library(root: Path, force: bool = False, confirm_mass_removal: bool = False) -> dict:
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
        return _scan_library(root, force=force, confirm_mass_removal=confirm_mass_removal)
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


def retry_payload(payload: dict) -> dict:
    """jobs.register(on_retry=): a scan that failed or was interrupted runs
    again incremental. A forced scan re-reads every file, which on a large
    library is most of an hour; run three times it is three; the files it
    already re-read are recorded, and the rest are picked up as changed or
    by the next forced scan an admin asks for."""
    return {**payload, "force": False}


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
        result = _scan_library(root, force=bool(payload.get("force")), report=report,
                               confirm_mass_removal=bool(payload.get("confirm_mass_removal")))
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
            "AND state IN ('done', 'failed', 'cancelled') ORDER BY id DESC LIMIT 1",
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
        if finished["state"] == "cancelled":
            # Not the previous scan's counts: they'd read as this one's.
            last_result = {"status": "error", "reason": "cancelled"}
        elif finished["state"] == "failed":
            last_result = {"status": "error",
                           "reason": finished["last_error"] or "error"}
        elif finished["result"]:
            last_result = json.loads(finished["result"])
    progress = None
    if pending is not None and pending["progress"]:
        progress = json.loads(pending["progress"])
    return {"running": pending is not None, "last_result": last_result,
            "progress": progress, "last_scan_at": last_scan_at}


def start_scan(root: Path, force: bool = False, confirm_mass_removal: bool = False) -> dict:
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
        job_id = jobs.enqueue(conn, JOB_TYPE, {"force": bool(force),
                                               "confirm_mass_removal": bool(confirm_mass_removal)},
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
        "SELECT finished_at FROM jobs WHERE type = ? AND state IN ('done', 'cancelled') "
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
    immediately become eligible again the moment it ends. A cancelled scan
    counts as finished here, so an admin's cancel waits out the interval
    rather than being undone at the next idle check.

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
            "SELECT finished_at FROM jobs WHERE type = ? AND state IN ('done', 'cancelled') "
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


def _scan_library(root: Path, force: bool = False, report=None,
                  confirm_mass_removal: bool = False) -> dict:
    conn = db.get_conn()
    counts = {"added": 0, "updated": 0, "removed": 0, "moved": 0, "unchanged": 0, "skipped_dirs": 0,
              "vanished_files": 0, "unreadable_files": 0, "from_folder": 0}
    unreadable: list[str] = []
    seen_paths: set[str] = set()
    # Albums whose art may have changed — their cached cover is dropped at the
    # end so replaced/edited art propagates without a manual refresh.
    touched_albums: set[tuple[str, str]] = set()
    # The same albums keyed as album_artist groups them (folder, album), whose
    # album artist is recomputed at the end, whichever way the scan ends.
    touched_groups: set[tuple[str, str]] = set()
    # .m4a files found to be ALAC on a library from before tracks.lossless: a
    # device set to MP3 that holds one has the .m4a, and gets the .mp3 now.
    became_lossless: list[int] = []
    # Files new to the library this scan, for _match_moves.
    added: list[dict] = []

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
        total = sum(1 for _ in _iter_audio_files(root, []))
        report(0, total)

    def tick() -> None:
        """One file done. Its writes are committed here, before the next file
        is read: a transaction held across file reads shuts every other writer
        out for as long as they take. Progress follows, with nothing pending,
        because it writes on its own connection."""
        conn.commit()
        if report is not None and processed % _REPORT_EVERY == 0:
            report(processed, total)

    try:
        # A track that vanishes is stamped with the scan's START, not the
        # moment of the update after the walk: every file first seen during
        # this scan then counts as seen no earlier than the removal, which is
        # what track_merge pairs a vanished track with its moved file by.
        scan_started = conn.execute("SELECT datetime('now')").fetchone()[0]
        existing = {
            row["relative_path"]: row
            for row in conn.execute(
                "SELECT id, relative_path, artist, album, title, track_no, disc_no, duration, "
                "size, mtime, deleted_at, album_artist_read, lossless FROM tracks"
            )
        }

        for abs_path in _iter_audio_files(root, unreadable):
            rel_path = str(abs_path.relative_to(root))
            try:
                stat = abs_path.stat()
            except FileNotFoundError:
                # Listed by the walk, gone by the time it was looked at:
                # deleted or moved mid-scan. Not there, so a track it was is
                # removed like any other vanished file, and the scan carries
                # on instead of failing and being retried whole.
                counts["vanished_files"] += 1
                processed += 1
                tick()
                continue
            except OSError as err:
                # There but unreadable (permissions, an I/O error on a flaky
                # share): not seen, not removed, the same rule as a folder the
                # walk couldn't read. Its track, if it has one, stays as it is.
                _log.warning("library scan: can't read %s: %s", abs_path, err.strerror)
                seen_paths.add(rel_path)
                counts["unreadable_files"] += 1
                processed += 1
                tick()
                continue
            seen_paths.add(rel_path)
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
                if not row["album_artist_read"] or row["lossless"] is None:
                    # A file from before these columns existed: read what's
                    # missing, once. Only an .m4a or an unread album artist
                    # needs the file opened; the rest is the extension.
                    read = (_read_tags(abs_path) if not row["album_artist_read"]
                            or abs_path.suffix.lower() == ".m4a" else None)
                    if not row["album_artist_read"]:
                        assert read is not None
                        conn.execute(
                            "UPDATE tracks SET album_artist_tag = ?, album_artist_read = 1 WHERE id = ?",
                            (read["album_artist_tag"], row["id"]))
                        touched_groups.add(album_artist.album_key(rel_path, row["album"]))
                    if row["lossless"] is None:
                        lossless = read["lossless"] if read is not None else _lossless(abs_path, None)
                        conn.execute("UPDATE tracks SET lossless = ? WHERE id = ?", (lossless, row["id"]))
                        if lossless and abs_path.suffix.lower() == ".m4a":
                            became_lossless.append(row["id"])
                tick()
                continue

            tags = _read_tags(abs_path)
            counts["from_folder"] += tags["from_folder"]
            touched_albums.add((tags["artist"], tags["album"]))
            touched_groups.add(album_artist.album_key(rel_path, tags["album"]))
            if row is None:
                cur = conn.execute(
                    "INSERT INTO tracks (relative_path, artist, album, title, track_no, "
                    "disc_no, year, reissue_year, duration, release_date, isrc, mbid, size, mtime, "
                    "artist_norm, title_norm, album_artist_tag, album_artist, album_artist_read, "
                    "lossless, scanned_at, deleted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, datetime('now'), NULL)",
                    (rel_path, tags["artist"], tags["album"], tags["title"],
                     tags["track_no"], tags["disc_no"], tags["year"], tags["reissue_year"],
                     tags["duration"], tags["release_date"], tags["isrc"], tags["mbid"], size, mtime,
                     matching.normalize(tags["artist"]), matching.normalize(tags["title"]),
                     tags["album_artist_tag"], tags["album_artist_tag"] or tags["artist"], tags["lossless"]),
                )
                added.append({"id": sync_state._new_id(cur), "relative_path": rel_path,
                              "key": _move_key(size, tags)})
                counts["added"] += 1
            else:
                # If the track moved to a different album/artist, the old
                # album's cached cover may now be stale too.
                touched_albums.add((row["artist"], row["album"]))
                touched_groups.add(album_artist.album_key(rel_path, row["album"]))
                # scanned_at is deliberately NOT touched here — it means
                # "first seen", not "last touched" (see suggestions.py's
                # recently_added() and the dashboard's Recently Added
                # widget, both of which read it as an "added" signal). A
                # forced full rescan re-verifying every unchanged tag must
                # not reset it for the whole library.
                if tags["lossless"] and not row["lossless"] and abs_path.suffix.lower() == ".m4a":
                    became_lossless.append(row["id"])
                conn.execute(
                    "UPDATE tracks SET artist=?, album=?, title=?, track_no=?, disc_no=?, "
                    "year=?, reissue_year=?, duration=?, release_date=?, isrc=?, mbid=?, size=?, mtime=?, "
                    "artist_norm=?, title_norm=?, album_artist_tag=?, album_artist_read=1, lossless=?, "
                    "deleted_at=NULL "
                    "WHERE id=?",
                    (tags["artist"], tags["album"], tags["title"], tags["track_no"],
                     tags["disc_no"], tags["year"], tags["reissue_year"], tags["duration"],
                     tags["release_date"], tags["isrc"], tags["mbid"], size, mtime,
                     matching.normalize(tags["artist"]), matching.normalize(tags["title"]),
                     tags["album_artist_tag"], tags["lossless"], row["id"]),
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
        # A track under a folder the walk couldn't read wasn't seen, not
        # removed: it stays.
        skipped = []
        for path in unreadable:
            try:
                skipped.append(str(Path(path).relative_to(root)))
            except ValueError:
                skipped.append("")  # root itself, or outside it: covers everything
        counts["skipped_dirs"] = len(unreadable)

        def under_skipped(rel_path: str) -> bool:
            return any(d in ("", ".") or rel_path.startswith(d + "/") for d in skipped)

        vanished = [row for rel_path, row in existing.items()
                    if rel_path not in seen_paths and row["deleted_at"] is None
                    and not under_skipped(rel_path)]
        # A file that only moved (a folder renamed, files reorganised) is a
        # vanished track and a new one with the same size and tags: the old
        # row takes the new path, keeping its id and so its devices' copies.
        # Before the mass-removal guard, so a reorganised library isn't
        # mistaken for a vanished one.
        moved = _match_moves(vanished, added)
        for old, new in moved:
            _move_track(conn, old["id"], new["id"])
            touched_albums.add((old["artist"], old["album"]))
            touched_groups.add(album_artist.album_key(old["relative_path"], old["album"]))
        counts["moved"] = len(moved)
        counts["added"] -= len(moved)
        moved_ids = {old["id"] for old, _new in moved}
        vanished = [row for row in vanished if row["id"] not in moved_ids]
        live = sum(1 for row in existing.values() if row["deleted_at"] is None)
        if vanished and not confirm_mass_removal and (
                not seen_paths or len(vanished) > live * MASS_REMOVAL_FRACTION):
            album_artist.refresh(conn, touched_groups)
            conn.commit()  # what the walk did find is kept; nothing is removed
            if not seen_paths:
                reason = (f"found no audio files under {root}, but the library has {live} tracks. "
                          "Is the music folder mounted?")
            else:
                reason = (f"would remove {len(vanished)} of the library's {live} tracks, more than "
                          f"{int(MASS_REMOVAL_FRACTION * 100)}%.")
            raise MassRemovalRefused(
                f"The scan {reason} Nothing was removed. If the library really shrank, "
                "an admin can run the scan again with confirm_mass_removal=1.")
        for row in vanished:
            conn.execute(
                "UPDATE tracks SET deleted_at=? WHERE id=?",
                (scan_started, row["id"])
            )
            touched_albums.add((row["artist"], row["album"]))
            touched_groups.add(album_artist.album_key(row["relative_path"], row["album"]))
            counts["removed"] += 1

        album_artist.refresh(conn, touched_groups)
        _redeliver_as_transcoded(conn, became_lossless)

        # Garbage-collect soft-deleted tracks no device still references.
        conn.execute(
            "DELETE FROM tracks WHERE deleted_at IS NOT NULL AND id NOT IN "
            "(SELECT track_id FROM device_track_state)"
        )

        # What each device should hold follows the library: an album added
        # under a selected artist is queued, a vanished file is marked for
        # removal. device_track_state is only ever materialised here and on a
        # selection change, and /api/device/changes reads it as it stands.
        if counts["added"] or counts["updated"] or counts["removed"]:
            for device in conn.execute("SELECT id FROM devices").fetchall():
                sync_state.recompute_device_state(conn, device["id"], commit=False)

        conn.commit()
    finally:
        conn.close()

    # Drop cached covers for every album that gained/lost/changed a track, so
    # the next browse re-extracts current art. (No-op after a forced rescan,
    # which already cleared the whole cache.)
    if not force:
        for artist, album in touched_albums:
            covers.invalidate(artist, album)
    # The page asks for a cover by a URL the browser keeps for a day, so a
    # cover dropped here would show stale that long. A new epoch in the URL
    # (main's index route puts it into coverUrl()) makes the next page load
    # ask afresh.
    if force or touched_albums:
        bump_covers_epoch()

    return counts


if __name__ == "__main__":
    import sys
    db.init_db()
    result = scan_library(db.get_music_root())
    print(result, file=sys.stderr)
