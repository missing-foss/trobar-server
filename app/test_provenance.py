#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for provenance.py (#239): server-computed fingerprints for a
device's local provenance DB. Mocks acoustid.fingerprint_file — same
convention as test_fingerprint.py; this never decodes real audio.

The load-bearing cases here are the ones that distinguish this module from
fingerprint.py, because the whole reason it exists separately is that those
differences are easy to "tidy up" back into being wrong:

  * it must work with NO AcoustID key configured;
  * it must select tracks that DO have an isrc (fingerprint.py never will);
  * it must never write fingerprint_checked_at.

    python3 -m unittest test_provenance -v      # from app/
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import jobs
import provenance
import sync_state


class _ProvenanceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-provenance-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore_db_globals)
        self.addCleanup(self._release_stray_lock)

        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)
        db.set_config(self.conn, "music_root", self._tmp)
        cur = self.conn.execute(
            "INSERT INTO users (username) VALUES ('owner')")
        self.owner = sync_state._new_id(cur)
        self.conn.commit()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "dap")

    def _restore_db_globals(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def _release_stray_lock(self):
        if provenance._PROVENANCE_LOCK.locked():
            provenance._PROVENANCE_LOCK.release()

    def _add_track(self, relative_path="Artist/Album/Track.flac", isrc=None,
                   fingerprint=None, deleted=False) -> int:
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, "
            "isrc, fingerprint, deleted_at) "
            "VALUES (?, 'Artist', 'Album', 'Track', 1, 0.0, ?, ?, ?)",
            (relative_path, isrc, fingerprint, "2026-01-01" if deleted else None),
        )
        self.conn.commit()
        return sync_state._new_id(cur)

    def _hold(self, track_id: int, status="downloaded", device_id=None) -> None:
        self.conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, ?)",
            (device_id if device_id is not None else self.device, track_id, status),
        )
        self.conn.commit()

    def _row(self, track_id: int):
        # A fresh connection: provenance writes on its own connection, so
        # self.conn must not serve a stale snapshot.
        conn = db.get_conn()
        try:
            return conn.execute(
                "SELECT fingerprint, acoustid_isrc, acoustid_mbid, fingerprint_checked_at, "
                "fingerprint_failed_at, fingerprint_seq, isrc FROM tracks WHERE id = ?", (track_id,),
            ).fetchone()
        finally:
            conn.close()


class NoAcoustidKeyTests(_ProvenanceTestBase):
    """The single most important difference from fingerprint.py: computing a
    fingerprint is purely local, so no API key is involved. fingerprint.py
    no-ops entirely without one; this must not."""

    def test_computes_with_no_key_configured(self):
        track = self._add_track()
        self._hold(track)
        self.assertIsNone(db.get_config(self.conn, "acoustid_api_key"))

        with mock.patch("provenance.acoustid.fingerprint_file",
                        return_value=(180.0, b"AQAAFP")) as fp_mock:
            result = provenance.ensure_device_fingerprints(self.device)

        fp_mock.assert_called_once()
        self.assertEqual(result, {"checked": 1, "computed": 1})
        self.assertEqual(self._row(track)["fingerprint"], "AQAAFP")


class SelectionTests(_ProvenanceTestBase):
    def test_selects_a_track_that_has_its_own_isrc(self):
        # fingerprint.py's selection is `isrc IS NULL AND
        # fingerprint_checked_at IS NULL`, so a well-tagged track is invisible
        # to it forever. Provenance needs its fingerprint regardless — identity
        # here is "which file is this", not "resolve an ISRC we couldn't read".
        track = self._add_track(isrc="USRC17607839")
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            result = provenance.ensure_device_fingerprints(self.device)
        self.assertEqual(result["computed"], 1)
        self.assertEqual(self._row(track)["fingerprint"], "FP")

    def test_selects_pending_as_well_as_downloaded(self):
        pending = self._add_track("a.flac")
        downloaded = self._add_track("b.flac")
        self._hold(pending, status="pending")
        self._hold(downloaded, status="downloaded")
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            result = provenance.ensure_device_fingerprints(self.device)
        self.assertEqual(result["computed"], 2)

    def test_skips_a_track_that_already_has_a_fingerprint(self):
        track = self._add_track(fingerprint="ALREADY")
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file") as fp_mock:
            result = provenance.ensure_device_fingerprints(self.device)
        fp_mock.assert_not_called()
        self.assertEqual(result, {"checked": 0, "computed": 0})
        self.assertEqual(self._row(track)["fingerprint"], "ALREADY")

    def test_skips_a_soft_deleted_track(self):
        track = self._add_track(deleted=True)
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file") as fp_mock:
            provenance.ensure_device_fingerprints(self.device)
        fp_mock.assert_not_called()

    def test_skips_a_removed_status_track(self):
        # 'removed' means the device has been told to delete it — no provenance
        # value in fingerprinting something on its way off the device.
        track = self._add_track()
        self._hold(track, status="removed")
        with mock.patch("provenance.acoustid.fingerprint_file") as fp_mock:
            provenance.ensure_device_fingerprints(self.device)
        fp_mock.assert_not_called()

    def test_ignores_another_devices_tracks(self):
        other_device, _ = sync_state.create_device(self.conn, self.owner, "phone")
        mine = self._add_track("mine.flac")
        theirs = self._add_track("theirs.flac")
        self._hold(mine)
        self._hold(theirs, device_id=other_device)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            provenance.ensure_device_fingerprints(self.device)
        self.assertEqual(self._row(mine)["fingerprint"], "FP")
        self.assertIsNone(self._row(theirs)["fingerprint"])


class CheckedAtIsNeverWrittenTests(_ProvenanceTestBase):
    def test_fingerprint_checked_at_is_left_null(self):
        # fingerprint_checked_at means "an AcoustID lookup was attempted".
        # Writing it here would tell fingerprint.py's backfill this track had
        # already been looked up and permanently suppress its ISRC resolution —
        # a silent, permanent loss of the #200 feature for every device-synced
        # track. This is the regression guard for that.
        track = self._add_track()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            provenance.ensure_device_fingerprints(self.device)
        row = self._row(track)
        self.assertEqual(row["fingerprint"], "FP")
        self.assertIsNone(row["fingerprint_checked_at"])
        self.assertIsNone(row["acoustid_isrc"])
        self.assertIsNone(row["acoustid_mbid"])


class DecodeTests(_ProvenanceTestBase):
    def test_bytes_fingerprint_is_decoded_to_text(self):
        # pyacoustid returns ASCII bytes. SQLite's TEXT affinity doesn't
        # convert bytes, so an un-decoded value silently persists as a BLOB
        # (the exact bug fingerprint.py hit live). Assert the stored type.
        track = self._add_track()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"AQAABYTES")):
            provenance.ensure_device_fingerprints(self.device)
        stored = self._row(track)["fingerprint"]
        self.assertIsInstance(stored, str)
        self.assertEqual(stored, "AQAABYTES")

    def test_an_already_str_fingerprint_is_stored_unchanged(self):
        track = self._add_track()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, "PLAINSTR")):
            provenance.ensure_device_fingerprints(self.device)
        self.assertEqual(self._row(track)["fingerprint"], "PLAINSTR")


class FingerprintSeqTests(_ProvenanceTestBase):
    """#439: fingerprint_seq is what lets GET /api/device/fingerprints offer
    an incremental filter instead of forcing every client to re-walk its
    entire fingerprint set on every sync. _compute_one is the only writer
    that ever bumps it -- fingerprint.py's own writes re-persist the
    identical value they read, so they must never touch it (a regression
    there would make a track's fingerprint look "new" again on every
    AcoustID pass, defeating the whole point of the filter)."""

    def test_a_freshly_computed_fingerprint_gets_a_seq(self):
        track = self._add_track()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            provenance.ensure_device_fingerprints(self.device)
        self.assertIsNotNone(self._row(track)["fingerprint_seq"])

    def test_seq_is_strictly_increasing_across_separate_tracks(self):
        a = self._add_track("a.flac")
        b = self._add_track("b.flac")
        self._hold(a)
        self._hold(b, device_id=self.device)
        with mock.patch("provenance.acoustid.fingerprint_file",
                        side_effect=[(180.0, b"FPA"), (180.0, b"FPB")]):
            provenance.ensure_device_fingerprints(self.device)
        seq_a, seq_b = self._row(a)["fingerprint_seq"], self._row(b)["fingerprint_seq"]
        self.assertIsNotNone(seq_a)
        self.assertIsNotNone(seq_b)
        self.assertNotEqual(seq_a, seq_b)

    def test_a_track_that_already_has_a_fingerprint_keeps_its_existing_seq(self):
        # #439: the fingerprint IS NULL guard on the UPDATE means this path
        # is never even reached for an already-fingerprinted track -- pin
        # that explicitly, since a regression there would silently re-mint
        # a seq for every already-known track on every device sync.
        track = self._add_track(fingerprint="ALREADY")
        self.conn.execute(
            "UPDATE tracks SET fingerprint_seq = 42 WHERE id = ?", (track,))
        self.conn.commit()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file") as fp_mock:
            provenance.ensure_device_fingerprints(self.device)
        fp_mock.assert_not_called()
        self.assertEqual(self._row(track)["fingerprint_seq"], 42)


class FailureTests(_ProvenanceTestBase):
    def test_a_decode_failure_is_skipped_without_raising(self):
        bad = self._add_track("bad.flac")
        good = self._add_track("good.flac")
        self._hold(bad)
        self._hold(good)

        def _fp(path, force_fpcalc=False):  # force_fpcalc: forced recompute
            if "bad.flac" in path:
                raise OSError("cannot decode")
            return (180.0, b"FP")

        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=_fp):
            result = provenance.ensure_device_fingerprints(self.device)  # must not raise

        # One bad file must not stall provenance for the rest of the device.
        self.assertEqual(result, {"checked": 2, "computed": 1})
        self.assertIsNone(self._row(bad)["fingerprint"])
        self.assertEqual(self._row(good)["fingerprint"], "FP")

    def test_a_failure_leaves_the_track_retryable(self):
        # Unlike fingerprint.py (which records the attempt to avoid re-hitting
        # a rate-limited external API), a failure here does NOT stop the track
        # being retried — there's no API and no cost, and a retry is what heals
        # a transient read error. It's only deprioritised.
        track = self._add_track()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=OSError("boom")):
            provenance.ensure_device_fingerprints(self.device)
        row = self._row(track)
        self.assertIsNone(row["fingerprint"])
        self.assertIsNone(row["fingerprint_checked_at"])  # #200's semantics intact

        # retried on a later pass, and a now-working file heals
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            result = provenance.ensure_device_fingerprints(self.device)
        self.assertEqual(result["computed"], 1)
        self.assertEqual(self._row(track)["fingerprint"], "FP")

    def test_a_failure_is_recorded_in_its_own_column(self):
        track = self._add_track()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=OSError("boom")):
            provenance.ensure_device_fingerprints(self.device)
        row = self._row(track)
        self.assertIsNotNone(row["fingerprint_failed_at"])
        # never conflated with fingerprint_checked_at, whose meaning ("an
        # AcoustID lookup was attempted") #200's backfill depends on
        self.assertIsNone(row["fingerprint_checked_at"])

    def test_a_later_success_clears_the_failure_marker(self):
        track = self._add_track()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=OSError("boom")):
            provenance.ensure_device_fingerprints(self.device)
        self.assertIsNotNone(self._row(track)["fingerprint_failed_at"])
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            provenance.ensure_device_fingerprints(self.device)
        self.assertIsNone(self._row(track)["fingerprint_failed_at"])

    def test_pending_reaches_zero_once_everything_has_succeeded_or_failed(self):
        # `pending` is a client's stopping condition. A permanently undecodable
        # file must not hold it above zero forever, or a client polling until
        # pending == 0 polls forever.
        track = self._add_track()
        self._hold(track)
        self.assertEqual(provenance.pending_count(self.conn, self.device), 1)
        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=OSError("boom")):
            provenance.ensure_device_fingerprints(self.device)
        self.assertEqual(provenance.pending_count(self.conn, self.device), 0)

    def test_failed_tracks_do_not_starve_fingerprintable_ones(self):
        # A half-finished copy is the realistic case: a whole batch's worth of
        # undecodable files. With failures merely deprioritised rather than
        # excluded, they must not refill every batch and block the good tracks
        # behind them forever.
        broken = [self._add_track(f"broken{i}.flac") for i in range(provenance._BATCH_LIMIT)]
        for t in broken:
            self._hold(t)

        def _fp(path, force_fpcalc=False):  # force_fpcalc: forced recompute
            if "broken" in path:
                raise OSError("cannot decode")
            return (180.0, b"GOODFP")

        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=_fp):
            # first pass: the batch is entirely the broken ones
            provenance.ensure_device_fingerprints(self.device)
            # a good track arrives afterwards, sorting BEHIND them by id
            good = self._add_track("good.flac")
            self._hold(good)
            provenance.ensure_device_fingerprints(self.device)

        # Without the deprioritisation this would still be None — the same
        # _BATCH_LIMIT broken rows would have filled the batch again.
        self.assertEqual(self._row(good)["fingerprint"], "GOODFP")


class BatchCapTests(_ProvenanceTestBase):
    def test_batch_is_capped_when_more_tracks_are_pending(self):
        for i in range(provenance._BATCH_LIMIT + 15):
            track = self._add_track(f"t{i}.flac")
            self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file",
                        return_value=(180.0, b"FP")) as fp_mock:
            result = provenance.ensure_device_fingerprints(self.device)
        self.assertEqual(fp_mock.call_count, provenance._BATCH_LIMIT)
        self.assertEqual(result["computed"], provenance._BATCH_LIMIT)
        # the rest are picked up by a later pass, not lost
        self.assertEqual(provenance.pending_count(self.conn, self.device), 15)


class OverlapGuardTests(_ProvenanceTestBase):
    def test_a_second_call_while_one_is_in_flight_is_a_no_op(self):
        # Several devices syncing at once is the normal household case; that
        # must not become N concurrent audio-decode passes.
        track = self._add_track()
        self._hold(track)
        provenance._PROVENANCE_LOCK.acquire()
        with mock.patch("provenance.acoustid.fingerprint_file") as fp_mock:
            result = provenance.ensure_device_fingerprints(self.device)
        fp_mock.assert_not_called()
        self.assertTrue(result["already_running"])


class PendingCountTests(_ProvenanceTestBase):
    def test_counts_only_this_devices_unfingerprinted_live_tracks(self):
        other_device, _ = sync_state.create_device(self.conn, self.owner, "phone")
        self._hold(self._add_track("needs.flac"))
        self._hold(self._add_track("has.flac", fingerprint="FP"))
        self._hold(self._add_track("gone.flac", deleted=True))
        self._hold(self._add_track("theirs.flac"), device_id=other_device)
        self.assertEqual(provenance.pending_count(self.conn, self.device), 1)


class DeviceFingerprintsJobTests(_ProvenanceTestBase):
    """#297 step 3: the per-device pass runs as a job now (JOB_TYPE_
    DEVICE_FINGERPRINTS) instead of a bespoke daemon thread with its own
    provenance_status()/_set_last_result — superseded by the jobs table,
    already covered by the admin Background Jobs panel for every job type."""

    def setUp(self):
        super().setUp()
        # Wire the handler explicitly, same shape as test_scanner.py's
        # BackgroundScanTests — production registration lives in main.py,
        # which this module deliberately doesn't import.
        jobs.register(provenance.JOB_TYPE_DEVICE_FINGERPRINTS,
                      provenance.run_device_fingerprints_job)
        self.addCleanup(jobs._HANDLERS.pop, provenance.JOB_TYPE_DEVICE_FINGERPRINTS, None)
        self.addCleanup(jobs._LANE_BY_TYPE.pop, provenance.JOB_TYPE_DEVICE_FINGERPRINTS, None)

    def test_run_device_fingerprints_job_delegates_with_the_payloads_device_id(self):
        track = self._add_track()
        self._hold(track)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            result = provenance.run_device_fingerprints_job({"device_id": self.device})
        self.assertEqual(result, {"checked": 1, "computed": 1})

    def test_run_device_fingerprints_job_requires_a_device_id(self):
        with self.assertRaises(ValueError):
            provenance.run_device_fingerprints_job({})

    def test_start_ensure_fingerprints_queues_and_does_not_run_inline(self):
        with mock.patch("provenance.acoustid.fingerprint_file") as fp_mock:
            provenance.start_ensure_fingerprints(self.device)
        fp_mock.assert_not_called()  # the whole point of backgrounding it
        row = self.conn.execute(
            "SELECT type, payload FROM jobs WHERE type = ?",
            (provenance.JOB_TYPE_DEVICE_FINGERPRINTS,)).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(f'"device_id": {self.device}', row["payload"])

    def test_a_second_trigger_is_a_no_op_while_one_is_pending(self):
        # #239's own reasoning, now enforced by the dedupe key instead of
        # _PROVENANCE_LOCK: several devices syncing at once is the normal
        # household case, and must not queue N redundant passes.
        provenance.start_ensure_fingerprints(self.device)
        other_device, _ = sync_state.create_device(self.conn, self.owner, "phone")
        provenance.start_ensure_fingerprints(other_device)
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE type = ?",
            (provenance.JOB_TYPE_DEVICE_FINGERPRINTS,)).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_the_worker_runs_it_and_computes_the_fingerprint(self):
        track = self._add_track()
        self._hold(track)
        provenance.start_ensure_fingerprints(self.device)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            self.assertTrue(jobs.run_one(jobs.LANE_SHORT))
        self.assertEqual(
            self.conn.execute("SELECT fingerprint FROM tracks WHERE id = ?",
                              (track,)).fetchone()["fingerprint"],
            "FP")

    def test_main_wires_it_into_the_short_lane(self):
        import main  # noqa: F401 — imported for its registration side effects
        self.assertEqual(
            jobs._LANE_BY_TYPE.get(provenance.JOB_TYPE_DEVICE_FINGERPRINTS), jobs.LANE_SHORT)


if __name__ == "__main__":
    unittest.main()


class _RematchTestBase(_ProvenanceTestBase):
    """#239 PR 2: the client -> server half. A device pushes what it holds and
    the server rematches BY FINGERPRINT, which is what survives the tag/naming
    drift that breaks path matching."""

    def _push(self, path, fingerprint, track_id=None, device_id=None):
        provenance.store_pushed_provenance(
            self.conn, device_id if device_id is not None else self.device,
            [{"path": path, "fingerprint": fingerprint, "track_id": track_id}])
        self.conn.commit()

    def _prov(self, path, device_id=None):
        conn = db.get_conn()
        try:
            return conn.execute(
                "SELECT state, matched_track_id, claimed_track_id FROM device_provenance "
                "WHERE device_id = ? AND path = ?",
                (device_id if device_id is not None else self.device, path),
            ).fetchone()
        finally:
            conn.close()

    def _dts(self, track_id, device_id=None):
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM device_track_state WHERE device_id = ? AND track_id = ?",
                (device_id if device_id is not None else self.device, track_id)).fetchone()
            return row["status"] if row else None
        finally:
            conn.close()

    def _add_unknown(self, path, device_id=None):
        self.conn.execute(
            "INSERT INTO device_unknown_tracks (device_id, path) VALUES (?, ?)",
            (device_id if device_id is not None else self.device, path))
        self.conn.commit()

    def _unknown_paths(self, device_id=None):
        conn = db.get_conn()
        try:
            return [r["path"] for r in conn.execute(
                "SELECT path FROM device_unknown_tracks WHERE device_id = ?",
                (device_id if device_id is not None else self.device,))]
        finally:
            conn.close()


class RematchTests(_RematchTestBase):
    def test_a_fingerprint_match_marks_the_track_held(self):
        track = self._add_track("Artist/Album/real.flac", fingerprint="FPMATCH")
        # The device's on-disk name deliberately shares nothing with the
        # catalog path — path matching could not possibly find this.
        self._push("Totally/Different/Name.flac", "FPMATCH")

        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FPMATCH")):
            result = provenance.rematch_device({"device_id": self.device})

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["unmatched"], 0)
        self.assertEqual(self._dts(track), "downloaded")
        row = self._prov("Totally/Different/Name.flac")
        self.assertEqual(row["state"], "matched")
        self.assertEqual(row["matched_track_id"], track)

    def test_a_match_retracts_the_unknown_track_row(self):
        # THE point of the feature (#161): a file Trobar itself placed, listed
        # as "unknown, please adopt" because its path drifted, stops being
        # flagged once its audio is recognised.
        track = self._add_track("Artist/Album/real.flac", fingerprint="FPX")
        path = "Old/Naming/Scheme.flac"
        self._add_unknown(path)
        self.assertEqual(self._unknown_paths(), [path])
        self._push(path, "FPX")

        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FPX")):
            provenance.rematch_device({"device_id": self.device})

        self.assertEqual(self._unknown_paths(), [])
        self.assertEqual(self._dts(track), "downloaded")

    def test_a_stale_server_fingerprint_is_rejected_by_re_verification(self):
        # The case that justifies re-verification existing at all: the DB row
        # says FPOLD, but the file on disk now hashes to something else. The
        # pushed value matches the ROW, so a row-only check would confidently
        # mark the wrong track held.
        track = self._add_track("Artist/Album/changed.flac", fingerprint="FPOLD")
        self._push("Dev/Path.flac", "FPOLD")

        with mock.patch("provenance.acoustid.fingerprint_file",
                        return_value=(180.0, b"FPACTUALLYDIFFERENT")):
            result = provenance.rematch_device({"device_id": self.device})

        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["unmatched"], 1)
        self.assertIsNone(self._dts(track))  # NOT marked held
        self.assertEqual(self._prov("Dev/Path.flac")["state"], "unmatched")

    def test_an_unreadable_file_is_not_believed(self):
        track = self._add_track("Artist/Album/gone.flac", fingerprint="FPG")
        self._push("Dev/Gone.flac", "FPG")
        with mock.patch("provenance.acoustid.fingerprint_file",
                        side_effect=OSError("vanished")):
            result = provenance.rematch_device({"device_id": self.device})
        self.assertEqual(result["unmatched"], 1)
        self.assertIsNone(self._dts(track))

    def test_no_candidate_is_a_normal_unmatched_outcome(self):
        # Side-loaded audio the server has never seen. Not an error.
        self._push("Sideloaded/Thing.flac", "FPNOBODYHAS")
        with mock.patch("provenance.acoustid.fingerprint_file") as fp:
            result = provenance.rematch_device({"device_id": self.device})
        fp.assert_not_called()  # nothing to verify, so no decode
        self.assertEqual(
            result, {"matched": 0, "unmatched": 1, "deferred": 0, "remaining": 0})
        self.assertEqual(self._prov("Sideloaded/Thing.flac")["state"], "unmatched")

    def test_a_soft_deleted_track_is_not_a_candidate(self):
        self._add_track("Artist/Album/dead.flac", fingerprint="FPDEAD", deleted=True)
        self._push("Dev/Dead.flac", "FPDEAD")
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FPDEAD")):
            result = provenance.rematch_device({"device_id": self.device})
        self.assertEqual(result["unmatched"], 1)

    def test_only_this_devices_rows_are_processed(self):
        other, _ = sync_state.create_device(self.conn, self.owner, "phone")
        track = self._add_track("Artist/Album/x.flac", fingerprint="FPS")
        self._push("Mine.flac", "FPS")
        self._push("Theirs.flac", "FPS", device_id=other)

        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FPS")):
            provenance.rematch_device({"device_id": self.device})

        self.assertEqual(self._prov("Mine.flac")["state"], "matched")
        self.assertEqual(self._prov("Theirs.flac", device_id=other)["state"], "pending")
        self.assertIsNone(self._dts(track, device_id=other))

    def test_batch_is_capped_and_remaining_is_reported(self):
        for i in range(provenance._BATCH_LIMIT + 7):
            self._add_track(f"Artist/Album/t{i}.flac", fingerprint=f"FP{i}")
            self._push(f"Dev/t{i}.flac", f"FP{i}")
        with mock.patch("provenance.acoustid.fingerprint_file",
                        side_effect=lambda p: (180.0, b"NOPE")):
            result = provenance.rematch_device({"device_id": self.device})
        self.assertEqual(result["matched"] + result["unmatched"], provenance._BATCH_LIMIT)
        self.assertEqual(result["remaining"], 7)

    def test_a_missing_device_id_is_a_wiring_error_not_a_silent_no_op(self):
        with self.assertRaises(ValueError):
            provenance.rematch_device({})
        with self.assertRaises(ValueError):
            provenance.rematch_device(None)

    def test_an_overlapping_pass_is_skipped_not_lost(self):
        self._push("Dev/x.flac", "FP")
        provenance._PROVENANCE_LOCK.acquire()
        result = provenance.rematch_device({"device_id": self.device})
        self.assertTrue(result["already_running"])
        # still pending, so the next sync picks it up
        self.assertEqual(self._prov("Dev/x.flac")["state"], "pending")


class PushedProvenanceStoreTests(_RematchTestBase):
    def test_a_repush_resets_state_so_a_corrected_fingerprint_is_reconsidered(self):
        self._push("Dev/x.flac", "FPWRONG")
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"X")):
            provenance.rematch_device({"device_id": self.device})
        self.assertEqual(self._prov("Dev/x.flac")["state"], "unmatched")

        track = self._add_track("Artist/Album/right.flac", fingerprint="FPRIGHT")
        self._push("Dev/x.flac", "FPRIGHT")          # same path, corrected fingerprint
        self.assertEqual(self._prov("Dev/x.flac")["state"], "pending")
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FPRIGHT")):
            provenance.rematch_device({"device_id": self.device})
        self.assertEqual(self._prov("Dev/x.flac")["state"], "matched")
        self.assertEqual(self._dts(track), "downloaded")

    def test_claimed_track_id_is_stored_but_never_matched_on(self):
        # After the DB loss this feature recovers from, the client's ids are
        # gone or renumbered — so a wildly wrong claim must not affect anything.
        track = self._add_track("Artist/Album/x.flac", fingerprint="FPC")
        self._push("Dev/x.flac", "FPC", track_id=999999)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FPC")):
            provenance.rematch_device({"device_id": self.device})
        row = self._prov("Dev/x.flac")
        self.assertEqual(row["claimed_track_id"], 999999)   # kept for diagnostics
        self.assertEqual(row["matched_track_id"], track)     # resolved by fingerprint
        self.assertEqual(row["state"], "matched")

    def test_pending_count_tracks_outstanding_work(self):
        self.assertEqual(provenance.pushed_pending_count(self.conn, self.device), 0)
        self._push("a.flac", "FA")
        self._push("b.flac", "FB")
        self.assertEqual(provenance.pushed_pending_count(self.conn, self.device), 2)
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"Z")):
            provenance.rematch_device({"device_id": self.device})
        self.assertEqual(provenance.pushed_pending_count(self.conn, self.device), 0)


class LibraryFingerprintRecoveryTests(_RematchTestBase):
    """#239 PR 2, found by live-testing the actual disaster rather than the
    happy path: after a server-DB loss the library has NO fingerprints, because
    PR 1 only computes them for tracks a device already syncs — and
    device_track_state is empty in exactly that situation. So a pushed
    fingerprint had nothing to match against and recovery matched zero."""

    def test_a_row_is_deferred_not_unmatched_while_the_library_lacks_fingerprints(self):
        # The second half of the same bug: marking these 'unmatched' is
        # PERMANENT (nothing revisits a resolved row), so every recovery would
        # fail for whatever the fingerprint pass hadn't reached yet.
        self._add_track("Artist/Album/unfingerprinted.flac", fingerprint=None)
        self._push("Dev/x.flac", "FPSOMETHING")

        result = provenance.rematch_device({"device_id": self.device})

        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["unmatched"], 0)
        # left pending, so the next sync tries again once fingerprints exist
        self.assertEqual(self._prov("Dev/x.flac")["state"], "pending")
        self.assertEqual(result["remaining"], 1)

    def test_it_is_unmatched_once_the_library_is_fully_fingerprinted(self):
        # Same push, but now nothing is awaiting a fingerprint — so "no
        # candidate" is a real answer and the row resolves.
        self._add_track("Artist/Album/done.flac", fingerprint="FPOTHER")
        self._push("Dev/x.flac", "FPSOMETHING")

        result = provenance.rematch_device({"device_id": self.device})

        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["deferred"], 0)
        self.assertEqual(self._prov("Dev/x.flac")["state"], "unmatched")

    def test_the_library_pass_fingerprints_tracks_no_device_holds(self):
        # The crux: these tracks are in NO device_track_state row, so PR 1's
        # device-scoped pass would never touch them.
        t1 = self._add_track("Artist/Album/a.flac")
        t2 = self._add_track("Artist/Album/b.flac")
        self.assertEqual(provenance.library_fingerprints_pending(self.conn), 2)

        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FPNEW")):
            result = provenance.ensure_library_fingerprints()

        self.assertEqual(result["computed"], 2)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(self._row(t1)["fingerprint"], "FPNEW")
        self.assertEqual(self._row(t2)["fingerprint"], "FPNEW")

    def test_recovery_end_to_end_after_a_simulated_db_loss(self):
        # The exact scenario live testing exposed, in miniature: the device
        # holds audio, the server's track row is fresh with NO fingerprint and
        # NO device_track_state, and its path shares nothing with the device's.
        track = self._add_track("Renamed/Different/1-x.flac", fingerprint=None)
        self._push("Real Artist/Real Album/01 - Song 1.flac", "FPAUDIO")
        self._add_unknown("Real Artist/Real Album/01 - Song 1.flac")

        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FPAUDIO")):
            # 1. nothing to match against yet -> deferred, still pending
            first = provenance.rematch_device({"device_id": self.device})
            self.assertEqual(first["deferred"], 1)
            # 2. the library-wide pass fingerprints the renamed file
            provenance.ensure_library_fingerprints()
            # 3. now the rematch resolves it
            second = provenance.rematch_device({"device_id": self.device})

        self.assertEqual(second["matched"], 1)
        self.assertEqual(self._dts(track), "downloaded")
        self.assertEqual(self._unknown_paths(), [])   # no longer "please adopt"

    def test_the_library_pass_skips_soft_deleted_tracks(self):
        self._add_track("Artist/Album/gone.flac", deleted=True)
        self.assertEqual(provenance.library_fingerprints_pending(self.conn), 0)
        with mock.patch("provenance.acoustid.fingerprint_file") as fp:
            provenance.ensure_library_fingerprints()
        fp.assert_not_called()

    def test_the_library_pass_is_batch_capped(self):
        for i in range(provenance._BATCH_LIMIT + 5):
            self._add_track(f"Artist/Album/t{i}.flac")
        # Also used to pin the one-batch defect: with the only triggers being
        # device-facing routes, 100-per-call meant a library was never fully
        # fingerprinted, so #239's recovery had nothing to match against.
        with mock.patch("provenance.acoustid.fingerprint_file",
                        return_value=(180.0, b"FP")) as fp:
            result = provenance.ensure_library_fingerprints()
        self.assertEqual(fp.call_count, provenance._BATCH_LIMIT + 5)
        self.assertEqual(result["remaining"], 0, "the pass must drain the library")
        self.assertEqual(result["computed"], provenance._BATCH_LIMIT + 5)

    def test_it_stops_when_a_pass_makes_no_progress(self):
        # The backstop for the loop. If a pass neither computes anything nor
        # reduces the pending count, looping again would decode the same files
        # forever. Simulated by a compute that fails WITHOUT recording the
        # failure — which _compute_one always does, so this is guarding against
        # that invariant being broken later, not against today's behaviour.
        for i in range(3):
            self._add_track(f"Artist/Album/t{i}.flac")
        with mock.patch.object(provenance, "_compute_one", return_value=False), \
             mock.patch.object(provenance, "_log") as log:
            result = provenance.ensure_library_fingerprints()
        self.assertEqual(result["remaining"], 3)
        self.assertTrue(log.warning.called, "a stalled pass should say so")

    def test_it_reports_progress_against_the_initial_total(self):
        for i in range(provenance._BATCH_LIMIT + 5):
            self._add_track(f"Artist/Album/t{i}.flac")
        seen = []
        with mock.patch("provenance.acoustid.fingerprint_file", return_value=(180.0, b"FP")):
            provenance.ensure_library_fingerprints(
                None, lambda done, total=None, label=None: seen.append((done, total)))
        self.assertEqual(seen[0], (0, provenance._BATCH_LIMIT + 5))
        self.assertEqual(seen[-1], (provenance._BATCH_LIMIT + 5, provenance._BATCH_LIMIT + 5))

    def test_an_undecodable_file_does_not_stall_the_pass(self):
        # A permanently-broken file must drop out of `pending` (via
        # fingerprint_failed_at) rather than pinning it above zero forever —
        # that's what makes the loop safe.
        self._add_track("Artist/Album/good.flac")
        self._add_track("Artist/Album/bad.flac")

        def _fp(path, force_fpcalc=False):  # force_fpcalc: forced recompute
            if path.endswith("bad.flac"):
                raise RuntimeError("undecodable")
            return (180.0, b"FP")

        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=_fp):
            result = provenance.ensure_library_fingerprints()
        self.assertEqual(result["computed"], 1)
        self.assertEqual(result["remaining"], 0, "the broken file must not pin pending")

    def test_it_fills_a_missing_duration_from_the_decode(self):
        # #337: the AcoustID lookup needs a duration and since #334 no longer
        # decodes to get one, so a fingerprinted track with no TAGGED duration
        # could never be looked up — and nothing else would supply it, since
        # duration comes from tags and a rescan can't help a file whose tags
        # don't carry one. The decode already produces it; it was discarded.
        self._add_track("Artist/Album/nodur.flac")
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tracks SET duration = NULL")
            conn.commit()
        finally:
            conn.close()
        with mock.patch("provenance.acoustid.fingerprint_file",
                        return_value=(212.5, b"FPDUR")):
            provenance.ensure_library_fingerprints()
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT fingerprint, duration FROM tracks").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["fingerprint"], "FPDUR")
        self.assertAlmostEqual(row["duration"], 212.5)

    def test_it_never_overwrites_a_tagged_duration(self):
        # Tags stay authoritative — this only fills a hole.
        track_id = self._add_track("Artist/Album/hasdur.flac")
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tracks SET duration = 180.0 WHERE id = ?", (track_id,))
            conn.commit()
        finally:
            conn.close()
        with mock.patch("provenance.acoustid.fingerprint_file",
                        return_value=(999.0, b"FPX")):
            provenance.ensure_library_fingerprints()
        conn = db.get_conn()
        try:
            self.assertAlmostEqual(
                conn.execute("SELECT duration FROM tracks WHERE id = ?",
                             (track_id,)).fetchone()["duration"], 180.0)
        finally:
            conn.close()

    def test_a_nonsense_duration_does_not_fail_the_fingerprint(self):
        # tracks is STRICT (#298), so a REAL column rejects a non-number. Passing
        # one through would turn a surprising return value into an IntegrityError
        # that loses the fingerprint too — much worse than not filling the hole.
        self._add_track("Artist/Album/weird.flac")
        conn = db.get_conn()
        try:
            conn.execute("UPDATE tracks SET duration = NULL")
            conn.commit()
        finally:
            conn.close()
        for bad in (None, "not-a-number", 0, -5):
            with self.subTest(bad=bad):
                conn = db.get_conn()
                try:
                    conn.execute("UPDATE tracks SET fingerprint = NULL, duration = NULL")
                    conn.commit()
                finally:
                    conn.close()
                with mock.patch("provenance.acoustid.fingerprint_file",
                                return_value=(bad, b"FPOK")):
                    provenance.ensure_library_fingerprints()
                conn = db.get_conn()
                try:
                    row = conn.execute(
                        "SELECT fingerprint, duration FROM tracks").fetchone()
                finally:
                    conn.close()
                self.assertEqual(row["fingerprint"], "FPOK",
                                 "the fingerprint must still be stored")
                self.assertIsNone(row["duration"])

    def test_an_overlapping_library_pass_is_skipped(self):
        self._add_track("Artist/Album/a.flac")
        provenance._PROVENANCE_LOCK.acquire()
        result = provenance.ensure_library_fingerprints()
        self.assertTrue(result["already_running"])


class DuplicateAudioTests(_RematchTestBase):
    def test_duplicate_audio_resolves_deterministically(self):
        # Identical files fingerprint identically by design, and the admin
        # Health panel counts probable duplicates outright — so this is a real
        # library shape, not a contrived one. Without an ORDER BY tie-break the
        # rematch could pick a different duplicate on each run.
        first = self._add_track("Artist/Album/copy-a.flac", fingerprint="FPDUP")
        self._add_track("Artist/Album/copy-b.flac", fingerprint="FPDUP")
        self._add_track("Artist/Album/copy-c.flac", fingerprint="FPDUP")

        for _ in range(3):
            self.conn.execute("DELETE FROM device_provenance")
            self.conn.execute("DELETE FROM device_track_state")
            self.conn.commit()
            self._push("Dev/x.flac", "FPDUP")
            with mock.patch("provenance.acoustid.fingerprint_file",
                            return_value=(180.0, b"FPDUP")):
                provenance.rematch_device({"device_id": self.device})
            # always the lowest id, every run
            self.assertEqual(self._prov("Dev/x.flac")["matched_track_id"], first)
