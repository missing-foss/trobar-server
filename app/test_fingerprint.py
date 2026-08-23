#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for fingerprint.py's AcoustID/MusicBrainz local-track ISRC
backfill (#200 step 3). Mocks acoustid.fingerprint_file/acoustid.lookup
and requests.get — same convention every other provider client's tests in
this codebase already use (see test_lms_client.py's _resp helper); this
never hits a real external service.

    python3 -m unittest test_fingerprint -v      # from app/
"""
import shutil
import tempfile
import pathlib
import unittest
from pathlib import Path
from unittest import mock

import db
import fingerprint


def _resp(status_code=200, json_body=None):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    if status_code >= 400:
        import requests
        err = requests.HTTPError(f"{status_code}")
        err.response = r
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    return r


class _FingerprintTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-fingerprint-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore_db_globals)
        conn = db.get_conn()
        try:
            db.set_config(conn, "music_root", self._tmp)
            conn.commit()
        finally:
            conn.close()
        self.addCleanup(self._release_stray_lock)

    def _restore_db_globals(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def _release_stray_lock(self):
        if fingerprint._FINGERPRINT_LOCK.locked():
            fingerprint._FINGERPRINT_LOCK.release()

    def _set_key(self, key="test-acoustid-key"):
        conn = db.get_conn()
        try:
            db.set_config(conn, "acoustid_api_key", key)
            conn.commit()
        finally:
            conn.close()

    def _add_track(self, relative_path="Artist/Album/Track.flac",
                   fingerprint="FPDATA", duration=180.0) -> int:
        # #334: the backfill is the LOOKUP and no longer decodes audio — it only
        # selects tracks that already carry a fingerprint and a duration, produced
        # by provenance.ensure_library_fingerprints. So a seeded track needs both
        # to be a lookup candidate at all. Pass fingerprint=None to exercise the
        # case where the producer hasn't run yet.
        conn = db.get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, "
                "fingerprint, duration) "
                "VALUES (?, 'Artist', 'Album', 'Track', 1, 0.0, ?, ?)",
                (relative_path, fingerprint, duration),
            )
            conn.commit()
            assert cur.lastrowid is not None  # always set right after an INSERT
            return cur.lastrowid
        finally:
            conn.close()

    def _track_row(self, track_id: int):
        conn = db.get_conn()
        try:
            return conn.execute(
                "SELECT fingerprint, acoustid_isrc, acoustid_mbid, fingerprint_checked_at "
                "FROM tracks WHERE id = ?", (track_id,),
            ).fetchone()
        finally:
            conn.close()


class NoKeyConfiguredTests(_FingerprintTestBase):
    def test_no_op_when_no_key_is_configured(self):
        self._add_track()
        with mock.patch("fingerprint.acoustid.fingerprint_file") as fp_mock, \
             mock.patch("fingerprint.requests.get") as get_mock:
            result = fingerprint.resolve_pending_fingerprints()
        # No LOOKUP without a key — and it now says so, rather than looking
        # indistinguishable from "nothing to do". The keyless fingerprint
        # COMPUTATION lives in provenance.ensure_library_fingerprints; conflating
        # the two is what made #239's recovery inert on key-less installs.
        self.assertEqual(result, {"checked": 0, "resolved": 0, "no_api_key": True})
        fp_mock.assert_not_called()
        get_mock.assert_not_called()


class ConfidentMatchTests(_FingerprintTestBase):
    def test_confident_match_persists_fingerprint_isrc_and_mbid(self):
        self._set_key()
        track_id = self._add_track()
        lookup_response = {"status": "ok", "results": [
            {"score": 0.95, "recordings": [{"id": "mbid-123", "title": "Track", "artists": []}]},
        ]}
        with mock.patch("fingerprint.acoustid.fingerprint_file", return_value=(180.0, "FPDATA")), \
             mock.patch("fingerprint.acoustid.lookup", return_value=lookup_response), \
             mock.patch("fingerprint.requests.get",
                        return_value=_resp(json_body={"isrcs": ["USRC17607839", "USRC00000000"]})), \
             mock.patch("fingerprint.time.sleep") as sleep_mock:
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result, {"checked": 1, "resolved": 1})
        row = self._track_row(track_id)
        self.assertEqual(row["fingerprint"], "FPDATA")
        self.assertEqual(row["acoustid_isrc"], "USRC17607839")  # first of several — documented limitation
        self.assertEqual(row["acoustid_mbid"], "mbid-123")
        self.assertIsNotNone(row["fingerprint_checked_at"])
        sleep_mock.assert_called_once()  # MusicBrainz's 1 req/sec pacing

    def test_this_module_no_longer_decodes_audio_at_all(self):
        # #334: the bytes-to-str decode guard that used to live here is gone with
        # the decode itself. pyacoustid returns ASCII bytes and storing them
        # unconverted in a TEXT column silently persists a BLOB — that guard still
        # matters, and now lives where the decoding does
        # (test_provenance's BytesFingerprintTests).
        #
        # What belongs here instead is the structural property: this module must
        # not acquire a decode again, because a second decode site is what made
        # the two jobs order-dependent.
        import ast
        tree = ast.parse(pathlib.Path(fingerprint.__file__).read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "fingerprint_file"]
        self.assertEqual(calls, [], "fingerprint.py decodes audio again — that is "
                                    "provenance.py's job (#334)")

    def test_acoustid_lookup_is_not_separately_paced(self):
        # pyacoustid's own _rate_limit decorator already paces AcoustID
        # calls — fingerprint.py must not add a redundant sleep for it,
        # only for the MusicBrainz hop.
        self._set_key()
        self._add_track()
        with mock.patch("fingerprint.acoustid.fingerprint_file", return_value=(180.0, "FPDATA")), \
             mock.patch("fingerprint.acoustid.lookup", return_value={"status": "ok", "results": []}), \
             mock.patch("fingerprint.time.sleep") as sleep_mock:
            fingerprint.resolve_pending_fingerprints()
        sleep_mock.assert_not_called()


class BelowThresholdTests(_FingerprintTestBase):
    def test_weak_match_is_not_persisted_as_a_resolution(self):
        self._set_key()
        track_id = self._add_track()
        lookup_response = {"status": "ok", "results": [
            {"score": 0.4, "recordings": [{"id": "mbid-weak", "title": "Track", "artists": []}]},
        ]}
        with mock.patch("fingerprint.acoustid.fingerprint_file", return_value=(180.0, "FPDATA")), \
             mock.patch("fingerprint.acoustid.lookup", return_value=lookup_response), \
             mock.patch("fingerprint.requests.get") as get_mock:
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result, {"checked": 1, "resolved": 0})
        get_mock.assert_not_called()  # never even attempts the MusicBrainz hop
        row = self._track_row(track_id)
        self.assertEqual(row["fingerprint"], "FPDATA")  # still cached — real work, don't discard it
        self.assertIsNone(row["acoustid_isrc"])
        self.assertIsNone(row["acoustid_mbid"])
        self.assertIsNotNone(row["fingerprint_checked_at"])  # not retried next scan


class NoAcoustidMatchTests(_FingerprintTestBase):
    def test_no_results_at_all_still_marks_checked(self):
        self._set_key()
        track_id = self._add_track()
        with mock.patch("fingerprint.acoustid.fingerprint_file", return_value=(180.0, "FPDATA")), \
             mock.patch("fingerprint.acoustid.lookup", return_value={"status": "ok", "results": []}):
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result, {"checked": 1, "resolved": 0})
        row = self._track_row(track_id)
        self.assertIsNone(row["acoustid_isrc"])
        self.assertIsNotNone(row["fingerprint_checked_at"])


class FingerprintFailureTests(_FingerprintTestBase):
    def test_an_unfingerprintable_track_is_never_a_candidate_here(self):
        # There is no "fingerprinting failed" path in this module any more (#334):
        # a decode failure is provenance.py's to record (fingerprint_failed_at),
        # and a track it could not fingerprint simply never becomes a lookup
        # candidate, because the query requires a fingerprint.
        self._set_key()
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, "
                "duration, fingerprint_failed_at) "
                "VALUES ('bad.flac', 'A', 'B', 'C', 1, 0.0, 180.0, datetime('now'))")
            conn.commit()
        finally:
            conn.close()
        with mock.patch("fingerprint.acoustid.lookup") as lookup_mock:
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result, {"checked": 0, "resolved": 0})
        lookup_mock.assert_not_called()


class AcoustidLookupFailureTests(_FingerprintTestBase):
    def test_lookup_failure_still_caches_the_fingerprint(self):
        self._set_key()
        track_id = self._add_track()
        with mock.patch("fingerprint.acoustid.fingerprint_file", return_value=(180.0, "FPDATA")), \
             mock.patch("fingerprint.acoustid.lookup", side_effect=RuntimeError("network down")):
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result, {"checked": 1, "resolved": 0})
        row = self._track_row(track_id)
        self.assertEqual(row["fingerprint"], "FPDATA")
        self.assertIsNone(row["acoustid_isrc"])
        self.assertIsNotNone(row["fingerprint_checked_at"])


class MusicBrainzFailureTests(_FingerprintTestBase):
    def test_musicbrainz_failure_persists_mbid_but_no_isrc(self):
        self._set_key()
        track_id = self._add_track()
        lookup_response = {"status": "ok", "results": [
            {"score": 0.95, "recordings": [{"id": "mbid-123", "title": "Track", "artists": []}]},
        ]}
        with mock.patch("fingerprint.acoustid.fingerprint_file", return_value=(180.0, "FPDATA")), \
             mock.patch("fingerprint.acoustid.lookup", return_value=lookup_response), \
             mock.patch("fingerprint.requests.get", return_value=_resp(status_code=503)), \
             mock.patch("fingerprint.time.sleep"):
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result, {"checked": 1, "resolved": 0})
        row = self._track_row(track_id)
        self.assertEqual(row["acoustid_mbid"], "mbid-123")
        self.assertIsNone(row["acoustid_isrc"])


class PersistOnceTests(_FingerprintTestBase):
    def test_a_checked_track_is_never_reselected(self):
        self._set_key()
        self._add_track()
        with mock.patch("fingerprint.acoustid.fingerprint_file", return_value=(180.0, "FPDATA")), \
             mock.patch("fingerprint.acoustid.lookup", return_value={"status": "ok", "results": []}):
            first = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(first, {"checked": 1, "resolved": 0})

        with mock.patch("fingerprint.acoustid.fingerprint_file") as fp_mock:
            second = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(second, {"checked": 0, "resolved": 0})
        fp_mock.assert_not_called()

    def test_a_track_with_its_own_isrc_tag_is_never_selected(self):
        # scanner.py already populated isrc from the file's own tags —
        # nothing to backfill.
        self._set_key()
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, isrc) "
                "VALUES ('a.flac', 'A', 'B', 'C', 1, 0.0, 'USRC17607839')"
            )
            conn.commit()
        finally:
            conn.close()
        with mock.patch("fingerprint.acoustid.fingerprint_file") as fp_mock:
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result, {"checked": 0, "resolved": 0})
        fp_mock.assert_not_called()


class ReusesProvenanceFingerprintTests(_FingerprintTestBase):
    """#239: provenance.py computes fingerprints on its own (device-sync)
    trigger, from the same audio. When one is already cached, this backfill
    must go straight to the AcoustID lookup rather than decode the file a
    second time to reproduce a value it already has."""

    def _add_track_with_fingerprint(self, fingerprint="CACHEDFP", duration=180.0):
        conn = db.get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, "
                "duration, fingerprint) "
                "VALUES ('Artist/Album/Track.flac', 'Artist', 'Album', 'Track', 1, 0.0, ?, ?)",
                (duration, fingerprint),
            )
            conn.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid
        finally:
            conn.close()

    def _lookup_response(self):
        return {"status": "ok", "results": [
            {"score": 0.95, "recordings": [{"id": "mbid-123", "title": "T",
                                            "artists": [{"name": "A"}]}]}]}

    def test_a_cached_fingerprint_skips_the_decode(self):
        self._set_key()
        track_id = self._add_track_with_fingerprint()
        with mock.patch("fingerprint.acoustid.fingerprint_file") as fp_mock, \
             mock.patch("fingerprint.acoustid.lookup",
                        return_value=self._lookup_response()) as lookup_mock, \
             mock.patch("fingerprint.requests.get",
                        return_value=_resp(json_body={"isrcs": ["USRC17607839"]})), \
             mock.patch("fingerprint.time.sleep"):
            result = fingerprint.resolve_pending_fingerprints()

        fp_mock.assert_not_called()  # the whole point: no second decode
        # ...but the lookup still happened, with the cached fingerprint and the
        # track's own tagged duration.
        lookup_mock.assert_called_once()
        self.assertEqual(lookup_mock.call_args[0][1], "CACHEDFP")
        self.assertEqual(lookup_mock.call_args[0][2], 180.0)
        self.assertEqual(result, {"checked": 1, "resolved": 1})
        row = self._track_row(track_id)
        self.assertEqual(row["fingerprint"], "CACHEDFP")
        self.assertEqual(row["acoustid_isrc"], "USRC17607839")

    def test_a_track_with_no_duration_is_left_alone_too(self):
        # acoustid.lookup() needs a duration, and this job no longer decodes to
        # obtain one (#334). A fingerprinted track with no tagged duration is
        # therefore skipped rather than decoded — an acknowledged gap: only a
        # rescan can supply the duration, so such a track never gets an ISRC.
        # Rare (the scanner sets duration for anything tinytag can read) and
        # strictly better than the alternative, which was this job silently doing
        # the producer's work inside an externally rate-limited loop.
        self._set_key()
        self._add_track_with_fingerprint(duration=None)
        with mock.patch("fingerprint.acoustid.fingerprint_file") as fp_mock, \
             mock.patch("fingerprint.acoustid.lookup") as lookup_mock:
            result = fingerprint.resolve_pending_fingerprints()
        fp_mock.assert_not_called()
        lookup_mock.assert_not_called()
        self.assertEqual(result, {"checked": 0, "resolved": 0})

    def test_a_track_with_no_fingerprint_is_left_for_the_library_pass(self):
        # #334: this job is the LOOKUP. It used to decode the audio itself when a
        # track had no fingerprint, which duplicated
        # provenance.ensure_library_fingerprints and made the two order-dependent
        # — a constraint documented only in a release note, and ignored within
        # hours of being written.
        self._set_key()
        track_id = self._add_track(fingerprint=None, duration=None)
        with mock.patch("fingerprint.acoustid.fingerprint_file") as fp_mock, \
             mock.patch("fingerprint.acoustid.lookup") as lookup_mock:
            result = fingerprint.resolve_pending_fingerprints()
        fp_mock.assert_not_called()
        lookup_mock.assert_not_called()
        self.assertEqual(result, {"checked": 0, "resolved": 0})
        # NOT stamped: fingerprint_checked_at would exclude this track from the
        # candidate query forever and silently lose its ISRC once the producer
        # does compute a fingerprint.
        row = self._track_row(track_id)
        self.assertIsNone(row["fingerprint_checked_at"])


class BatchCapTests(_FingerprintTestBase):
    def test_batch_is_capped_when_more_pending_tracks_exist(self):
        self._set_key()
        conn = db.get_conn()
        try:
            for i in range(fingerprint._BATCH_LIMIT + 20):
                conn.execute(
                    # #334: fingerprint+duration are the lookup's precondition
                    # now — a row without them is not a candidate at all.
                    "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, "
                    "fingerprint, duration) "
                    "VALUES (?, 'A', 'B', 'C', 1, 0.0, 'FP', 180.0)", (f"track-{i}.flac",),
                )
            conn.commit()
        finally:
            conn.close()
        # This used to assert that ONE batch ran and the rest were abandoned —
        # it was pinning the defect. The only trigger is a completed scan, so
        # "100 per scan" meant a 59,000-track library resolved 100 ISRCs per
        # rescan, which in production was indistinguishable from not working.
        # The batch LIMIT is still the unit of work (short transactions, paced
        # HTTP); what changed is that it keeps going until nothing is claimable.
        # #334: this job no longer decodes, so the work is counted in LOOKUPS.
        with mock.patch("fingerprint.acoustid.fingerprint_file") as fp_mock, \
             mock.patch("fingerprint.acoustid.lookup",
                        return_value={"status": "ok", "results": []}) as lookup_mock:
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result["checked"], fingerprint._BATCH_LIMIT + 20)
        self.assertEqual(lookup_mock.call_count, fingerprint._BATCH_LIMIT + 20)
        fp_mock.assert_not_called()  # all decoding lives in provenance.py now

    def test_it_queries_in_batch_sized_chunks_rather_than_all_at_once(self):
        # The cap still governs each pass: one long transaction over 59,000
        # tracks is what the batching exists to avoid.
        self._set_key()
        conn = db.get_conn()
        try:
            for i in range(fingerprint._BATCH_LIMIT + 20):
                conn.execute(
                    # #334: fingerprint+duration are the lookup's precondition
                    # now — a row without them is not a candidate at all.
                    "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, "
                    "fingerprint, duration) "
                    "VALUES (?, 'A', 'B', 'C', 1, 0.0, 'FP', 180.0)", (f"track-{i}.flac",),
                )
            conn.commit()
        finally:
            conn.close()
        with mock.patch("fingerprint.acoustid.fingerprint_file", return_value=(1.0, "FP")), \
             mock.patch("fingerprint.acoustid.lookup", return_value={"status": "ok", "results": []}), \
             mock.patch.object(fingerprint, "_resolve_one_batch",
                               wraps=fingerprint._resolve_one_batch) as batch:
            fingerprint.resolve_pending_fingerprints()
        self.assertEqual(batch.call_count, 2, "should be two passes of <=_BATCH_LIMIT")


class ProgressReportingTests(_FingerprintTestBase):
    def test_it_reports_progress_against_the_initial_total(self):
        # #360: mirrors provenance.py's
        # test_it_reports_progress_against_the_initial_total — same shape,
        # same fixed initial total, same final call landing exactly on it.
        self._set_key()
        conn = db.get_conn()
        try:
            for i in range(fingerprint._BATCH_LIMIT + 20):
                conn.execute(
                    "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, "
                    "fingerprint, duration) "
                    "VALUES (?, 'A', 'B', 'C', 1, 0.0, 'FP', 180.0)", (f"track-{i}.flac",),
                )
            conn.commit()
        finally:
            conn.close()
        seen = []
        with mock.patch("fingerprint.acoustid.fingerprint_file"), \
             mock.patch("fingerprint.acoustid.lookup", return_value={"status": "ok", "results": []}):
            fingerprint.resolve_pending_fingerprints(
                lambda done, total=None, label=None: seen.append((done, total)))
        total = fingerprint._BATCH_LIMIT + 20
        self.assertEqual(seen[0], (0, total))
        self.assertEqual(seen[-1], (total, total))


class OverlapGuardTests(_FingerprintTestBase):
    def test_a_second_call_while_one_is_in_flight_is_a_no_op(self):
        self._set_key()
        self._add_track()
        self.assertTrue(fingerprint._FINGERPRINT_LOCK.acquire(blocking=False))
        try:
            with mock.patch("fingerprint.acoustid.fingerprint_file") as fp_mock:
                result = fingerprint.resolve_pending_fingerprints()
        finally:
            fingerprint._FINGERPRINT_LOCK.release()
        self.assertTrue(result.get("already_running"))
        fp_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
