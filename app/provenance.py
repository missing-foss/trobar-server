#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#239: server-computed track fingerprints, for clients to store as
provenance ("these files came from Trobar, and here's the identity the
server itself assigned them").

The governing principle, which replaces #175's abandoned client-side half:
**clients never compute fingerprints — the server computes them and ships
them; the client is a dumb store of server-provided identity.** Computing
on-device was always the painful part — Garmin can't do it at all (no
audio-decode/DSP path exists in Monkey C, so a client-fingerprinting design
excluded the watch by construction) and on Android decoding a whole DAP
library is exactly the kind of background CPU burn users notice. None of the
*value* (provenance, recovery) actually needs the client to compute
anything.

Deliberately a separate module from fingerprint.py rather than more
functions inside it, because the two agree on almost nothing:

  * different trigger — fingerprint.py runs post-library-scan; this runs on
    device sync;
  * different selection — fingerprint.py only ever looks at tracks with NO
    isrc tag (`isrc IS NULL AND fingerprint_checked_at IS NULL`), so a
    well-tagged track never gets a fingerprint from it, ever. Provenance
    needs one for every device-synced track regardless of tag quality;
  * different gating — fingerprint.py no-ops entirely without an AcoustID
    API key. This must not, and doesn't: computing a fingerprint is purely
    local (chromaprint via ctypes + an audioread/ffmpeg decode). Only
    AcoustID *lookup* needs the key, and provenance never looks anything up.
    Verified directly, not inferred from the docs.

Folding this into fingerprint.py would have meant either breaking its
"opt-in, blank key = skip" contract or gating provenance behind an API key
it has no use for.

WHY THE SOURCE FINGERPRINT, even for a device holding transcoded audio:
measured directly, chromaprint is deterministic and survives a lossless
re-encode byte-for-byte, but a transcoded copy's fingerprint is NOT the
same string (FLAC vs MP3-320 differ; raw-vector similarity 0.9987, so
they're recognisably the same recording but only under fuzzy comparison).
The recovery flow this feeds compares a client-pushed fingerprint against
files in the server's own *source* filesystem, and wants plain SQL
equality — which SQLite can do and vector similarity is not. So the source
fingerprint is the only form that actually rematches.
"""

import logging
import threading

import acoustid

import db
import jobs

_log = logging.getLogger(__name__)

# Mirrors scanner._SCAN_LOCK / playlist_sync._SYNC_LOCK / fingerprint's own:
# non-reentrant, so an overlapping trigger (several devices syncing at once,
# which is the normal case in a household) is a no-op rather than N
# concurrent audio-decode passes. Single-process assumption, same as those.
_PROVENANCE_LOCK = threading.Lock()

# Same reasoning and value as fingerprint._BATCH_LIMIT: a first sync of a
# large device could need thousands of fingerprints, and each one is a real
# audio decode. Cap per run so one sync's background pass doesn't churn for
# hours — the next sync picks up where this left off, since the selection
# below naturally excludes what's already done. This is why the serving
# endpoint reports a `pending` count: it's normal for the first response to
# be incomplete.
_BATCH_LIMIT = 100

#: #239 PR 2: the job type the rematch runs as. Registered in main.py (which
#: already imports both modules), so neither this module nor jobs.py has to
#: import the other. A job rather than a thread of its own so it inherits
#: retry-with-backoff, restart survival and admin visibility from #297 —
#: recovery is exactly the flow where "did that finish, and what failed?" has
#: to be answerable.
JOB_TYPE_REMATCH = "provenance_rematch"

#: #239 PR 2: the library-wide fingerprint pass recovery depends on. Separate
#: from the rematch so each retries and reports independently — and separate
#: from ensure_device_fingerprints because that one is scoped to what a device
#: already syncs, which is empty precisely when recovery needs it most.
JOB_TYPE_LIBRARY_FINGERPRINTS = "provenance_library_fingerprints"

#: #297 step 3: the per-device fingerprint pass — the one thing this module
#: still ran as a bespoke daemon thread after JOB_TYPE_REMATCH and
#: JOB_TYPE_LIBRARY_FINGERPRINTS were already migrated. One dedupe key across
#: every device (not per-device): matches what _PROVENANCE_LOCK already
#: enforced — several devices syncing at once (the normal household case)
#: collapse onto whichever pass is already in flight rather than each
#: queueing its own, and a device whose trigger was dropped this way simply
#: gets it again on its next poll (the same "best-effort, retried by
#: repeated polling" behaviour the lock always gave, now just durable across
#: a restart too).
JOB_TYPE_DEVICE_FINGERPRINTS = "provenance_device_fingerprints"


def pending_count(conn, device_id: int) -> int:
    """How many of this device's tracks could still plausibly gain a
    fingerprint: no fingerprint yet, and no recorded failure.

    Shipped to the client so it knows to come back rather than assume one
    empty/short page means it has everything — a fingerprint often isn't
    computed yet at the moment the file itself is downloaded.

    Excluding already-failed tracks is what makes this a usable STOPPING
    CONDITION rather than a number that can never reach zero. A permanently
    undecodable file would otherwise keep `pending` above zero forever and a
    client polling until `pending == 0` would poll forever. Those tracks are
    still retried (see _pending_track_rows) — they're just not counted as
    outstanding work, so a transient failure that later heals simply shows up
    in `entries` on a subsequent call."""
    return conn.execute(
        "SELECT COUNT(*) FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
        "WHERE dts.device_id = ? AND dts.status IN ('pending', 'downloaded') "
        "AND t.deleted_at IS NULL AND t.fingerprint IS NULL "
        "AND t.fingerprint_failed_at IS NULL",
        (device_id,),
    ).fetchone()[0]


def _pending_track_rows(device_id: int) -> list:
    """Tracks this device holds or is about to hold that lack a fingerprint.

    Note what's absent from the WHERE compared with fingerprint.py's own
    selection: no `isrc IS NULL` and no `fingerprint_checked_at IS NULL`. A
    well-tagged track is invisible to that backfill forever, but provenance
    needs its fingerprint just the same — identity here is about which file
    this *is*, not about resolving an ISRC we couldn't read from tags.

    Previously-failed tracks are still selected (a retry is what heals a
    transient read error, and there's no external cost to retrying) but sort
    LAST. Without that, a device whose _BATCH_LIMIT-worth of files are
    permanently undecodable — a half-finished copy is exactly this case, and
    the docstring below lists it as normal — would refill every batch with the
    same doomed rows and starve every fingerprintable track behind them
    indefinitely. `fingerprint_failed_at IS NOT NULL` sorts 0 before 1, so
    never-failed rows come first."""
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT t.id, t.relative_path FROM device_track_state dts "
            "JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status IN ('pending', 'downloaded') "
            "AND t.deleted_at IS NULL AND t.fingerprint IS NULL "
            "ORDER BY (t.fingerprint_failed_at IS NOT NULL), t.id LIMIT ?",
            (device_id, _BATCH_LIMIT),
        ).fetchall()
    finally:
        conn.close()


def ensure_device_fingerprints(device_id: int) -> dict:
    """Computes and persists tracks.fingerprint for up to _BATCH_LIMIT of
    this device's tracks that don't have one. Returns
    {"checked": n, "computed": n}, or {..., "already_running": True} if a
    previous pass is still in flight.

    Never raises on a per-track failure — an undecodable or vanished file
    logs and is skipped, so one bad file can't stall provenance for the
    whole device. Not gated on any API key (see the module docstring)."""
    if not _PROVENANCE_LOCK.acquire(blocking=False):
        return {"checked": 0, "computed": 0, "already_running": True}
    try:
        rows = _pending_track_rows(device_id)
        root = db.get_music_root()
        checked = computed = 0
        for row in rows:
            checked += 1
            if _compute_one(root, row["id"], row["relative_path"]):
                computed += 1
        return {"checked": checked, "computed": computed}
    finally:
        _PROVENANCE_LOCK.release()


def run_device_fingerprints_job(payload: dict, _report=None) -> dict:
    """#297 job handler for JOB_TYPE_DEVICE_FINGERPRINTS. Thin adapter over
    ensure_device_fingerprints — same shape as scanner.run_job wrapping
    _scan_library: the payload/report calling convention lives here, kept
    separate from the worker's own plain typed signature (which stays as
    ensure_device_fingerprints(device_id), unchanged, so its existing direct
    unit tests didn't need to learn about payloads)."""
    device_id = (payload or {}).get("device_id")
    if device_id is None:
        raise ValueError("provenance_device_fingerprints requires a device_id in its payload")
    return ensure_device_fingerprints(device_id)


def _clean_duration(value) -> float | None:
    """#337: a duration fit for tracks.duration, or None to leave it alone.

    tracks is a STRICT table (#298), so a REAL column rejects anything that isn't a
    number — passing pyacoustid's value through unchecked would turn a surprising
    return into an IntegrityError that fails the whole fingerprint, which is a far
    worse outcome than simply not filling the hole. Zero and negatives are dropped
    too: acoustid.lookup would reject them anyway, and a 0.0 would look like a
    real answer while permanently satisfying the COALESCE."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _compute_one(root, track_id: int, relative_path: str) -> bool:
    """Own short transaction per track, never one long one — same principle
    as scanner.py's periodic commits and fingerprint._resolve_one's. Returns
    True iff a fingerprint was computed and persisted."""
    try:
        duration, raw_fingerprint = acoustid.fingerprint_file(
            # force_fpcalc (#329): the decode MUST happen in a subprocess.
            # pyacoustid's in-process ctypes path hands a partial decode (ffmpeg
            # timing out on a malformed file) to native libchromaprint, whose
            # assert() abort()s — SIGABRT, killing the whole server, uncatchable
            # from Python. One FLAC crashed production six times on v2.4.0. With
            # fpcalc the abort kills only the child and surfaces here as an
            # ordinary exception the handler below already records.
            str(root / relative_path), force_fpcalc=True)
    except Exception:
        # A missing/undecodable file is a normal thing to meet here (the
        # library moved, a partial copy, an exotic codec). The track stays
        # RETRYABLE — unlike fingerprint.py, which records the attempt to avoid
        # re-hitting a rate-limited external API; there's no API and no cost
        # here, and a retry is what heals a transient read error.
        #
        # But the failure is recorded (in fingerprint_failed_at, never in
        # fingerprint_checked_at — see db.py) so it can be deprioritised and
        # excluded from `pending`. Without that, a permanently-broken file both
        # keeps `pending` above zero forever and, in bulk, starves every
        # fingerprintable track behind it.
        _log.warning("fingerprinting failed for %s", relative_path, exc_info=True)
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE tracks SET fingerprint_failed_at = datetime('now') WHERE id = ?",
                (track_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return False

    # pyacoustid hands back ASCII bytes, not str. Decode before storing:
    # SQLite's TEXT affinity only converts INTEGER/REAL inputs, so an
    # un-decoded value silently persists as a BLOB instead of TEXT (the
    # exact bug fingerprint.py hit live on its first pass — same trap, same
    # fix, kept in both places deliberately rather than shared, since
    # neither module should have to import the other for this).
    fingerprint_text = raw_fingerprint.decode("ascii") if isinstance(raw_fingerprint, bytes) \
        else raw_fingerprint

    conn = db.get_conn()
    try:
        # fingerprint ONLY — never fingerprint_checked_at. That column means
        # "an AcoustID lookup was attempted"; writing it here would tell
        # fingerprint.py's backfill this track was already looked up and
        # permanently suppress its ISRC resolution. Guarded on `fingerprint
        # IS NULL` so a value written by that backfill in between (it runs on
        # its own trigger, and both are computing the same thing from the
        # same bytes) is left alone rather than churned.
        # fingerprint_failed_at is cleared on success, so a track that only
        # failed transiently stops being deprioritised once it works.
        # #337: also fill `duration` when it is missing, from the value the decode
        # just produced and this function used to discard.
        #
        # It closes a real gap rather than being a tidy-up. fingerprint.py's
        # AcoustID lookup needs a duration (acoustid.lookup takes one) and since
        # #334 it no longer decodes to obtain one — so a track with a fingerprint
        # but no TAGGED duration could never be looked up, and nothing else would
        # ever supply it: duration normally comes from tags, so a rescan cannot
        # help a file whose tags don't carry one.
        #
        # COALESCE, so a tag-derived duration is never overwritten — tags stay
        # authoritative, and this only fills a hole. The value is audioread's
        # full-file duration, unaffected by the 120s cap on how much audio gets
        # fingerprinted, which is the same semantic tracks.duration already has.
        #
        # Residual case, stated rather than hidden: a track that ALREADY has a
        # fingerprint but no duration is not selected by _library_track_rows (it
        # filters on fingerprint IS NULL), so it stays unlookupable. Not reachable
        # from a normal path — a fingerprint only gets written here or by the
        # backfill, and both now write a duration alongside it.
        # #439: fingerprint_seq is bumped HERE, and only here — this is the
        # sole code path that ever changes what `fingerprint` actually
        # holds (fingerprint.py's own writes always re-persist the
        # identical value they read). The subquery, not a Python read-
        # then-write, is what keeps this atomic: SQLite serializes writers
        # at the whole-database-file level, so no other connection's write
        # can land between this statement's MAX() read and its own UPDATE.
        conn.execute(
            "UPDATE tracks SET fingerprint = ?, duration = COALESCE(duration, ?), "
            "fingerprint_failed_at = NULL, "
            "fingerprint_seq = (SELECT COALESCE(MAX(fingerprint_seq), 0) + 1 FROM tracks) "
            "WHERE id = ? AND fingerprint IS NULL",
            (fingerprint_text, _clean_duration(duration), track_id),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def start_ensure_fingerprints(device_id: int) -> None:
    """Queue the per-device fingerprint pass instead of computing inline, for
    the device-sync path to call.

    Backgrounded for the reason stated three times over in this codebase
    (fingerprint.py's docstring, scanner.py's post-lock call, db.py's column
    comments): real audio decode must never happen inline in a request that
    holds a DB connection, or SQLite's busy_timeout gets blown and live
    device syncs start failing with "database is locked". /api/device/changes
    is a client-polled hot path, so this must add nothing to its latency.

    #297 step 3: a job now, not a bespoke daemon thread — the last of this
    module's own background work to make that move (JOB_TYPE_REMATCH and
    JOB_TYPE_LIBRARY_FINGERPRINTS already had). Same fire-and-forget contract
    as before from the caller's side: no return value, and a redundant call
    while one is already queued/running is a normal no-op (the dedupe key),
    not an error — the next device poll asks again."""
    conn = db.get_conn()
    try:
        jobs.enqueue(conn, JOB_TYPE_DEVICE_FINGERPRINTS, {"device_id": device_id},
                     dedupe_key=JOB_TYPE_DEVICE_FINGERPRINTS)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #239 PR 2: the client -> server half. A device pushes back the provenance DB
# it built from GET /api/device/fingerprints, and the server rematches those
# fingerprints against its own library instead of comparing paths.
# ---------------------------------------------------------------------------

def store_pushed_provenance(conn, device_id: int, entries: list) -> int:
    """Upsert a page of client-pushed provenance rows. Returns how many were
    stored. Does not match anything — that's rematch_device, run as a job.

    Re-pushing a path resets it to `pending` so a corrected fingerprint gets
    reconsidered; without that, a client that fixed a bad local record could
    never get the server to look again. Committed by the caller."""
    stored = 0
    for entry in entries:
        conn.execute(
            "INSERT INTO device_provenance "
            "  (device_id, path, fingerprint, claimed_track_id, state, matched_track_id) "
            "VALUES (?, ?, ?, ?, 'pending', NULL) "
            "ON CONFLICT(device_id, path) DO UPDATE SET "
            "  fingerprint = excluded.fingerprint, "
            "  claimed_track_id = excluded.claimed_track_id, "
            "  state = 'pending', matched_track_id = NULL, "
            "  pushed_at = datetime('now')",
            (device_id, entry["path"], entry["fingerprint"], entry.get("track_id")),
        )
        stored += 1
    return stored


def pushed_pending_count(conn, device_id: int) -> int:
    """How many pushed rows this device still has awaiting a rematch."""
    return conn.execute(
        "SELECT COUNT(*) FROM device_provenance WHERE device_id = ? AND state = 'pending'",
        (device_id,),
    ).fetchone()[0]


def library_fingerprints_pending(conn) -> int:
    """Live library tracks that could still gain a fingerprint.

    Recovery needs this and PR 1 didn't provide it. PR 1 computes fingerprints
    only for tracks in device_track_state — tracks a device is already syncing.
    After the server-DB loss this whole feature exists to recover from,
    device_track_state is EMPTY by definition, so the library has no
    fingerprints and a pushed fingerprint has nothing to match against.
    Found by live-testing the actual disaster rather than the happy path.

    So a provenance push also drives a LIBRARY-WIDE fingerprint pass. It costs
    one audio decode per file, once — unavoidable, since identifying audio by
    content means reading the content — and every future match benefits."""
    return conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL "
        "AND fingerprint IS NULL AND fingerprint_failed_at IS NULL"
    ).fetchone()[0]


def _library_track_rows() -> list:
    """Live tracks lacking a fingerprint, previously-failed ones last — the
    same deprioritisation _pending_track_rows uses, for the same reason (a
    batch of undecodable files must not starve the rest)."""
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT id, relative_path FROM tracks WHERE deleted_at IS NULL "
            "AND fingerprint IS NULL "
            "ORDER BY (fingerprint_failed_at IS NOT NULL), id LIMIT ?",
            (_BATCH_LIMIT,),
        ).fetchall()
    finally:
        conn.close()


def ensure_library_fingerprints(_payload: dict | None = None, report=None) -> dict:
    """#297 job handler: fingerprint every live library track that lacks one, so
    pushed provenance has something to match against.

    RUNS TO COMPLETION, in _BATCH_LIMIT batches, rather than doing one batch and
    relying on someone to re-enqueue it. That was a real gap: the only callers
    were two device-facing routes, so on an install where no device had pushed
    provenance yet this never ran at all — and when it did, it fingerprinted 100
    tracks and stopped. On a 59,000-track library that is indistinguishable from
    not working, which is how it was found (v2.3.0, in production: a completed
    full rescan left `fingerprint` NULL for every row).

    Safe to loop because `library_fingerprints_pending` excludes tracks with
    `fingerprint_failed_at` set, and _compute_one sets it on every failure — so
    an undecodable file drops out of the count instead of pinning it above zero
    forever. The no-progress guard below is the backstop for the case that
    reasoning is wrong: if a pass neither computes anything nor reduces the
    remaining count, stop and say so rather than spinning on audio decodes.

    Lives in the LONG lane, so taking hours can't delay a device's rematch.
    """
    if not _PROVENANCE_LOCK.acquire(blocking=False):
        return {"checked": 0, "computed": 0, "already_running": True}
    try:
        conn = db.get_conn()
        try:
            total = library_fingerprints_pending(conn)
        finally:
            conn.close()
        root = db.get_music_root()
        checked = computed = 0
        remaining = total
        if report is not None:
            report(0, total)
        while True:
            rows = _library_track_rows()
            if not rows:
                break
            for row in rows:
                checked += 1
                if _compute_one(root, row["id"], row["relative_path"]):
                    computed += 1
            conn = db.get_conn()
            try:
                still_pending = library_fingerprints_pending(conn)
            finally:
                conn.close()
            if report is not None:
                # `total - still_pending` rather than `checked`: it's the honest
                # measure of work retired, and it doesn't drift when a batch
                # re-reads a track that failed earlier.
                report(max(0, total - still_pending), total)
            if still_pending == 0:
                remaining = 0
                break
            if still_pending >= remaining:
                # Neither computed nor excluded anything — looping again would
                # decode the same files forever.
                _log.warning(
                    "library fingerprinting made no progress (%d still pending) — "
                    "stopping this pass", still_pending)
                remaining = still_pending
                break
            remaining = still_pending
        return {"checked": checked, "computed": computed, "remaining": remaining}
    finally:
        _PROVENANCE_LOCK.release()


def _verify_track_fingerprint(root, track_id: int, relative_path: str, expected: str) -> bool:
    """Re-fingerprint the located file and compare against `expected`.

    This is the locked trust posture from #239: a client-pushed fingerprint is
    a HINT for which file to check, never ground truth. Being honest about what
    it's worth, since it costs an audio decode per entry:

    Its value is smaller than when the issue was written. PR 1 made scanner.py
    invalidate tracks.fingerprint whenever a file's content changes, so a stale
    row now needs content to change with size AND mtime within 1s of the old
    values. And a pushed fingerprint can only ever match a value the server
    itself computed, so forging one buys a device nothing beyond declining its
    own re-download (per-device file authorization is separate, #110).

    Kept anyway, and not just because it's locked: it's the difference between
    trusting a database row and trusting the file on disk, the outcome here is
    "this device is believed to hold this track" (which suppresses a
    re-download), and recovery runs once per disaster so the cost is paid once.
    A read failure counts as NOT verified — the same conservative posture as
    mirror.py's marker check."""
    try:
        # force_fpcalc: subprocess isolation, see _compute_one (#329).
        _duration, raw = acoustid.fingerprint_file(
            str(root / relative_path), force_fpcalc=True)
    except Exception:
        _log.warning("re-verification could not fingerprint %s (track %s)",
                     relative_path, track_id, exc_info=True)
        return False
    actual = raw.decode("ascii") if isinstance(raw, bytes) else raw
    return actual == expected


def _rematch_one(conn, root, device_id: int, row, library_incomplete: bool) -> str:
    """Resolve one pushed row. Returns 'matched', 'unmatched', or 'deferred'.

    The fingerprint lookup here is what db.py's idx_tracks_fingerprint partial
    index was created for in PR 1 — until now nothing read that column back."""
    candidate = conn.execute(
        # ORDER BY id, not a bare LIMIT 1: duplicate audio is normal (the admin
        # Health panel counts probable duplicates outright), and identical files
        # fingerprint identically by design — so without a tie-break, two runs
        # could associate the device with different duplicate rows. Harmless in
        # itself (same audio either way), but a recovery should be reproducible.
        "SELECT id, relative_path, fingerprint FROM tracks "
        "WHERE fingerprint = ? AND deleted_at IS NULL ORDER BY id LIMIT 1",
        (row["fingerprint"],),
    ).fetchone()

    if candidate is None:
        if library_incomplete:
            # NOT unmatched — just not answerable yet. Library tracks are still
            # being fingerprinted, so "no candidate" here means "nothing to
            # compare against so far". Marking it unmatched would be permanent
            # (nothing revisits a resolved row), so every recovery would fail
            # for whatever the fingerprint pass hadn't reached. Left pending;
            # the next sync tries again once more fingerprints exist.
            return "deferred"
        # Genuinely unmatched: the library is fully fingerprinted and nothing
        # holds this audio. Side-loaded content the server has never seen.
        conn.execute(
            "UPDATE device_provenance SET state = 'unmatched', matched_track_id = NULL "
            "WHERE device_id = ? AND path = ?", (device_id, row["path"]))
        return "unmatched"

    if not _verify_track_fingerprint(root, candidate["id"], candidate["relative_path"],
                                     candidate["fingerprint"]):
        conn.execute(
            "UPDATE device_provenance SET state = 'unmatched', matched_track_id = NULL "
            "WHERE device_id = ? AND path = ?", (device_id, row["path"]))
        return "unmatched"

    # Believed. Mark it held so it isn't re-downloaded — same upsert
    # record_device_manifest uses for its own path-based matches.
    conn.execute(
        "INSERT INTO device_track_state (device_id, track_id, status) "
        "VALUES (?, ?, 'downloaded') "
        "ON CONFLICT(device_id, track_id) DO UPDATE SET "
        "  status = 'downloaded', updated_at = datetime('now')",
        (device_id, candidate["id"]),
    )
    conn.execute(
        "UPDATE device_provenance SET state = 'matched', matched_track_id = ? "
        "WHERE device_id = ? AND path = ?", (candidate["id"], device_id, row["path"]))
    # THE point of the feature: this file is no longer "unknown, please adopt".
    # device_unknown_tracks is keyed on (device_id, path), the same identity
    # used here, so the row a path-based manifest upload created is exactly the
    # one a fingerprint match can now retract.
    conn.execute(
        "DELETE FROM device_unknown_tracks WHERE device_id = ? AND path = ?",
        (device_id, row["path"]))
    return "matched"


def rematch_device(payload: dict | None = None, _report=None) -> dict:
    """#297 job handler for JOB_TYPE_REMATCH. Rematches up to _BATCH_LIMIT of
    one device's pushed provenance rows by fingerprint.

    Returns {"matched": n, "unmatched": n, "remaining": n}. `remaining` > 0 is
    normal and expected: each entry can cost an audio decode, so the batch cap
    applies and one huge recovery must not occupy the single worker
    indefinitely.

    CONTINUATION is driven from outside, deliberately. This handler does NOT
    re-enqueue itself, because it can't: the job is still `running` while the
    handler executes, and it holds the dedupe_key that a follow-up would need —
    the enqueue would simply be refused. Instead both of the natural triggers
    queue another pass while work remains: another push, and every
    /api/device/changes (see main.py). A recovering device syncs repeatedly, so
    the work drains without the client needing to know any of this.

    Takes _PROVENANCE_LOCK so a rematch and a fingerprint-computation pass
    can't both be decoding audio at once."""
    device_id = (payload or {}).get("device_id")
    if device_id is None:
        raise ValueError("provenance_rematch requires a device_id in its payload")

    if not _PROVENANCE_LOCK.acquire(blocking=False):
        # A fingerprint pass (or another device's rematch) is decoding audio
        # right now. Not lost: the next sync queues this again while rows
        # remain pending.
        return {"matched": 0, "unmatched": 0, "already_running": True}
    try:
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT path, fingerprint FROM device_provenance "
                "WHERE device_id = ? AND state = 'pending' ORDER BY path LIMIT ?",
                (device_id, _BATCH_LIMIT),
            ).fetchall()
        finally:
            conn.close()

        root = db.get_music_root()
        # Read once per run, not per row: whether the library still has tracks
        # awaiting a fingerprint decides if "no candidate" means "not yet" or
        # "genuinely not here".
        conn = db.get_conn()
        try:
            library_incomplete = library_fingerprints_pending(conn) > 0
        finally:
            conn.close()

        matched = unmatched = deferred = 0
        for row in rows:
            # Own short transaction per row, same principle as _compute_one:
            # a long recovery must leave partial progress durable.
            conn = db.get_conn()
            try:
                outcome = _rematch_one(conn, root, device_id, row, library_incomplete)
                if outcome == "matched":
                    matched += 1
                elif outcome == "unmatched":
                    unmatched += 1
                else:
                    deferred += 1
                conn.commit()
            finally:
                conn.close()

        conn = db.get_conn()
        try:
            remaining = pushed_pending_count(conn, device_id)
        finally:
            conn.close()
        return {"matched": matched, "unmatched": unmatched,
                "deferred": deferred, "remaining": remaining}
    finally:
        _PROVENANCE_LOCK.release()
