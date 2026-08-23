#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#200 step 3: AcoustID/MusicBrainz-based local-track identity backfill.

Runs after a library scan (see scanner.start_scan's _run(), outside
_SCAN_LOCK — real audio decode plus two paced external HTTP calls per
track must never hold the scan lock, same reasoning already established
for playlist_sync's _SYNC_LOCK). Deliberately NOT triggered by a playlist-
track miss: fingerprinting needs real audio bytes, and the only audio
Trobar can ever read is its own already-scanned local library — never a
playlist entry's provider-side stream. See identity.py's module docstring
for the full reasoning.

For each local track missing its own ISRC tag (isrc IS NULL) and never
yet attempted (fingerprint_checked_at IS NULL), computes a chromaprint
fingerprint (pyacoustid, binding straight to libchromaprint1 via ctypes
and decoding via audioread's ffmpeg backend — no fpcalc CLI involved,
verified directly), looks it up against AcoustID (returns a MusicBrainz
recording id, NOT an ISRC — confirmed against AcoustID's own webservice
docs), then resolves that recording's ISRC via a second call to the
MusicBrainz web service. Persists fingerprint/acoustid_isrc/acoustid_mbid/
fingerprint_checked_at on every outcome — including "no match" and "match
too weak" — so a genuine miss isn't retried every scan.

This doesn't resolve any playlist miss today: identity.py's tiers 1/2
still need a PROVIDER to supply its own ISRC to compare against, which no
provider client does yet (#200 step 1's own finding). What this backfill
does is make those tiers actually useful the moment that future PR lands
— for poorly-tagged or untagged local files too, not just well-tagged
ones a rescan alone could catch.

Self-hosters who never set an AcoustID key (Administration > Configuration)
get tiers 1-3 only — this module no-ops entirely without one."""

import logging
import threading
import time

import acoustid
import requests

import db

_log = logging.getLogger(__name__)

# Mirrors scanner._SCAN_LOCK / playlist_sync._SYNC_LOCK: non-reentrant, so
# an overlapping trigger (e.g. two scans finishing close together) is a
# no-op rather than a second concurrent run. Single-process assumption,
# same as those two.
_FINGERPRINT_LOCK = threading.Lock()

# A brand-new or never-fingerprinted library could have thousands of
# pending tracks; MusicBrainz's own 1 req/sec means a big batch takes a
# genuinely long time regardless. Cap per run so one scan's post-lock
# phase doesn't block for hours — later scans keep picking up where this
# one left off (the WHERE clause below naturally excludes what's already
# checked).
_BATCH_LIMIT = 100

# AcoustID's score is 0-1. This value is later compared with SQL `=`, not
# fuzzy-matched, so a wrong cached ISRC is a real correctness bug, not
# just a missed opportunity — conservative on purpose, favoring "leave it
# unresolved" over "confidently wrong."
_MATCH_SCORE_THRESHOLD = 0.7

_MUSICBRAINZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/{mbid}"
# MusicBrainz's own strict webservice policy.
_MUSICBRAINZ_RATE_LIMIT_SECONDS = 1.0
# MusicBrainz actively blocks generic/missing User-Agents — a static,
# clearly-identifying string with a contact satisfies their policy; no
# need to couple this to the running app version.
_USER_AGENT = "Trobar-Server (+https://github.com/missing-foss/trobar-server; missing_foss@etik.com)"


#: #297: the job type this module's work runs as. scanner.py enqueues it
#: after a scan rather than calling resolve_pending_fingerprints() inline, so
#: a failure lands in jobs.last_error where an admin can see it instead of
#: only in the log, and gets retried with backoff. Registered in main.py
#: (which already imports both modules) so neither this module nor jobs.py
#: has to import the other.
JOB_TYPE = "fingerprint_backfill"


def run_job(_payload: dict | None = None, report=None) -> dict:
    """#297 job handler. Thin adapter: the queue passes a payload dict and
    stores the returned dict as the job's result, which is how the admin
    overview can show what a run actually did — previously this return value
    was discarded entirely by scanner.py's fire-and-forget call."""
    return resolve_pending_fingerprints(report)


def resolve_pending_fingerprints(report=None) -> dict:
    """No-ops entirely if no AcoustID key is configured — same "opt-in,
    blank = skip" shape as lastfm_api_key_default. Returns
    {"checked": n, "resolved": n}, or {"already_running": True} if a
    previous call is still in flight.

    Still directly callable (and still self-guarded by _FINGERPRINT_LOCK):
    the lock is now belt-and-braces behind the queue's own dedupe_key, which
    already prevents a second queued-or-running backfill, but this stays a
    public function that must be safe to call on its own."""
    conn = db.get_conn()
    try:
        api_key = db.get_config(conn, "acoustid_api_key")
    finally:
        conn.close()
    if not api_key:
        # No key means no LOOKUP — it does not mean no fingerprints. Computing a
        # chromaprint is purely local; only resolving it to an ISRC needs the
        # API. Those two were conflated here, and the cost was that #239's
        # recovery-by-fingerprint silently could not work on any install without
        # an AcoustID key: tracks.fingerprint stayed NULL, so a device's pushed
        # fingerprint had nothing to match. The keyless computation lives in
        # provenance.ensure_library_fingerprints and is queued after every scan
        # (see scanner._queue_post_scan_jobs), so this early return is now only
        # about the lookup it says it is about.
        return {"checked": 0, "resolved": 0, "no_api_key": True}

    if not _FINGERPRINT_LOCK.acquire(blocking=False):
        return {"checked": 0, "resolved": 0, "already_running": True}
    try:
        return _resolve_pending_fingerprints(api_key, report)
    finally:
        _FINGERPRINT_LOCK.release()


#: The WHERE clause every candidate query below shares — one place, so
#: _pending_lookup_count and _resolve_one_batch's SELECT can't drift apart.
_PENDING_LOOKUP_WHERE = (
    "deleted_at IS NULL AND isrc IS NULL AND fingerprint_checked_at IS NULL "
    "AND fingerprint IS NOT NULL AND duration IS NOT NULL"
)


def _pending_lookup_count(conn) -> int:
    """#360: how many tracks are still candidates for this job's
    AcoustID/MusicBrainz lookup — the denominator jobProgressPct() needs.
    Same shape as provenance.library_fingerprints_pending, that module's
    equivalent for its own (different) candidate predicate."""
    return conn.execute(
        f"SELECT COUNT(*) FROM tracks WHERE {_PENDING_LOOKUP_WHERE}"
    ).fetchone()[0]


def _resolve_pending_fingerprints(api_key: str, report=None) -> dict:
    """Batches until nothing claimable is left, rather than one batch per call.

    Same gap as provenance.ensure_library_fingerprints had: the only trigger is a
    completed scan, so "one batch" meant 100 ISRCs resolved per rescan on a
    59,000-track library. Terminates because _resolve_one always stamps
    fingerprint_checked_at — success or failure — and the query below excludes
    stamped rows, so every pass strictly shrinks the candidate set.

    Paced external HTTP per track, so this is deliberately a LONG-lane job.

    #360: reports a real `total` now, same shape as
    provenance.ensure_library_fingerprints — `total` is captured ONCE before
    the loop (not re-read per batch, matching that sibling's own choice), so
    `total - still_pending` is what's reported. If the candidate set GROWS
    mid-run (a scan landing new tracks while this is going), still_pending
    can exceed the captured total; jobProgressPct()'s existing
    Math.min(100, …) clamp is what absorbs that, exactly as it already does
    for the sibling job — not re-reading `total` here is a deliberate choice
    to match it, not an oversight."""
    root = db.get_music_root()
    conn = db.get_conn()
    try:
        total = _pending_lookup_count(conn)
    finally:
        conn.close()
    checked = resolved = 0
    if report is not None:
        report(0, total, None)
    while True:
        batch_checked, batch_resolved, exhausted = _resolve_one_batch(api_key, root)
        checked += batch_checked
        resolved += batch_resolved
        if report is not None:
            conn = db.get_conn()
            try:
                still_pending = _pending_lookup_count(conn)
            finally:
                conn.close()
            # total - still_pending, not `checked`: the honest measure of work
            # retired, matching provenance.ensure_library_fingerprints's own
            # reasoning — it doesn't drift if a batch re-reads a track whose
            # candidacy changed for a reason other than this job checking it.
            report(max(0, total - still_pending), total, None)
        if exhausted:
            break
    return {"checked": checked, "resolved": resolved}


def _resolve_one_batch(api_key: str, root) -> tuple:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            # #334: fingerprint/duration are REQUIRED, not opportunistic. This
            # job is the AcoustID/MusicBrainz LOOKUP; the fingerprint it needs is
            # produced by provenance.ensure_library_fingerprints, and until #334
            # this query took tracks without one and decoded the audio itself as
            # a fallback. That made the two jobs order-dependent — run this one
            # first and it did the other's work, inside a loop already paced by
            # AcoustID (<=3/sec) and MusicBrainz (1/sec) — with the ordering
            # documented only in a release note, which is where it was promptly
            # ignored.
            #
            # Now the producer/consumer split is real: all decoding happens in
            # exactly one place, this job is purely network-bound, and running it
            # early is a harmless no-op rather than a slow duplicate. main.py
            # queues it again once the producer has computed something.
            f"SELECT id, relative_path, fingerprint, duration FROM tracks WHERE {_PENDING_LOOKUP_WHERE} "
            "LIMIT ?",
            (_BATCH_LIMIT,),
        ).fetchall()
    finally:
        conn.close()

    checked = resolved = 0
    for row in rows:
        checked += 1
        if _resolve_one(api_key, root, row["id"], row["relative_path"],
                        row["fingerprint"], row["duration"]):
            resolved += 1
    # Short batch => the candidate set is drained; no need for another query.
    return checked, resolved, len(rows) < _BATCH_LIMIT


def _resolve_one(api_key: str, root, track_id: int, relative_path: str,
                 cached_fingerprint: str | None = None,
                 cached_duration: float | None = None) -> bool:
    """Own short transaction per track — never one long one, same
    principle as scanner.py's periodic commits. Returns True iff an ISRC
    was actually resolved and persisted."""
    if cached_fingerprint and cached_duration:
        # #239: provenance.py may already have computed this exact
        # fingerprint from these exact bytes, on its own (device-sync)
        # trigger. Decoding the audio again to reproduce a value we already
        # hold is pure waste, so reuse it and go straight to the lookup.
        # Needs a duration too, since acoustid.lookup() takes one:
        # tracks.duration (from tags) is the right equivalent — the
        # duration fingerprint_file() returns is audioread's own full-file
        # duration, unaffected by the 120s cap on how much audio actually
        # gets fingerprinted (checked against pyacoustid's source, not
        # assumed). Falls through to a real decode when either is missing.
        fingerprint, _duration = cached_fingerprint, float(cached_duration)
    else:
        # #334: this job no longer decodes audio. It used to fall through to a
        # full decode here, which made it a duplicate of
        # provenance.ensure_library_fingerprints and made the two order-dependent
        # — the constraint that lived only in a release note.
        #
        # Nothing is stamped and nothing is persisted: leaving the row untouched
        # is what lets the producer compute a fingerprint and this job pick the
        # track up on a later run. Writing fingerprint_checked_at here would
        # exclude it from this query forever and quietly lose the ISRC.
        #
        # The batch query already filters these out, so this is defensive; it can
        # only be reached by calling _resolve_one directly.
        _log.debug("no fingerprint yet for %s — leaving it for the library pass",
                   relative_path)
        return False

    try:
        # pyacoustid's own _rate_limit decorator on _api_request already
        # paces AcoustID lookups to <=3 req/sec (their documented limit)
        # — no separate sleep needed here.
        response = acoustid.lookup(api_key, fingerprint, _duration)
        best = max(acoustid.parse_lookup_result(response), key=lambda r: r[0], default=None)
    except Exception:
        _log.warning("AcoustID lookup failed for %s", relative_path, exc_info=True)
        _persist(track_id, fingerprint, None, None)
        return False

    if best is None or best[0] < _MATCH_SCORE_THRESHOLD:
        _persist(track_id, fingerprint, None, None)
        return False

    _score, mbid, _title, _artist = best
    isrc = _fetch_musicbrainz_isrc(mbid)
    _persist(track_id, fingerprint, isrc, mbid)
    return isrc is not None


def _fetch_musicbrainz_isrc(mbid: str) -> str | None:
    time.sleep(_MUSICBRAINZ_RATE_LIMIT_SECONDS)  # MusicBrainz's own 1 req/sec policy
    try:
        resp = requests.get(
            _MUSICBRAINZ_RECORDING_URL.format(mbid=mbid),
            params={"inc": "isrcs", "fmt": "json"},
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        isrcs = resp.json().get("isrcs") or []
    except Exception:
        _log.warning("MusicBrainz ISRC lookup failed for recording %s", mbid, exc_info=True)
        return None
    # A recording can carry several regional-release ISRCs — take the
    # first. A future provider-supplied ISRC for the same recording could
    # still be a different valid one and miss an equality check; an
    # accepted limitation (see identity.py's tier 2 docstring), not fixed
    # here.
    return isrcs[0] if isrcs else None


def _persist(track_id: int, fingerprint: str | None, isrc: str | None, mbid: str | None) -> None:
    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE tracks SET fingerprint = ?, acoustid_isrc = ?, acoustid_mbid = ?, "
            "fingerprint_checked_at = datetime('now') WHERE id = ?",
            (fingerprint, isrc, mbid, track_id),
        )
        conn.commit()
    finally:
        conn.close()
