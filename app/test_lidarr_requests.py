#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for lidarr_requests.py — #494's orchestration module: enabled/
configured gating, eligibility (excluded rows, no-album-data rows), the
cross-playlist dedup that's the feature's headline design constraint,
outcome bookkeeping, and the never-retry policy for 'partial'/'failed'
rows. lidarr_client itself is mocked at the module boundary here, same as
test_mirror_subsonic.py mocks subsonic_client. Same DATA_DIR/DB_PATH
tmp-dir override pattern.

    python3 -m unittest test_lidarr_requests -v      # from app/
"""
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import db
import jobs
import lidarr_requests
import matching


class _LidarrRequestsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-lidarr-requests-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore_db_globals)
        self.conn = db.get_conn()
        # run_for_playlist() checks db.get_lidarr_config() directly (not
        # through the mocked lidarr_client) to distinguish "unset" from
        # "configured but unreachable" — same convention as the mirror
        # sinks' own unset_target check.
        db.set_config(self.conn, "lidarr_url", "http://lidarr.local")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        db.set_config(self.conn, "lidarr_root_folder_path", "/music")
        db.set_config(self.conn, "lidarr_quality_profile_id", "1")
        db.set_config(self.conn, "lidarr_metadata_profile_id", "2")
        self.conn.commit()

    def _restore_db_globals(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def _clear_lidarr_config(self) -> None:
        db.set_config(self.conn, "lidarr_url", None)
        db.set_config(self.conn, "lidarr_api_key", None)
        db.set_config(self.conn, "lidarr_root_folder_path", None)
        db.set_config(self.conn, "lidarr_quality_profile_id", None)
        db.set_config(self.conn, "lidarr_metadata_profile_id", None)
        self.conn.commit()

    def _make_playlist(self, title: str, lidarr_request_enabled: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO playlists (title, lidarr_request_enabled) VALUES (?, ?)",
            (title, 1 if lidarr_request_enabled else 0),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _add_unresolved(self, playlist_id: int, artist: str, album: str | None,
                         title: str = "T", excluded: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album, excluded) "
            "VALUES (?, 0, ?, ?, ?, ?)",
            (playlist_id, artist, title, album, 1 if excluded else 0),
        )
        self.conn.commit()

    def _row(self, playlist_id: int):
        return self.conn.execute(
            "SELECT lidarr_request_enabled, lidarr_request_last_run_at, "
            "lidarr_request_last_count, lidarr_request_last_error, "
            "lidarr_request_last_error_code FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()

    def _requested_row(self, artist: str, album: str):
        return self.conn.execute(
            "SELECT * FROM lidarr_requested_albums WHERE normalized_artist = ? AND normalized_album = ?",
            (matching.normalize(artist), matching.normalize(album)),
        ).fetchone()

    def _candidate(self, artist: str, foreign_album_id="fa1", foreign_artist_id="far1"):
        return {
            "foreignAlbumId": foreign_album_id,
            "artist": {"artistName": artist, "foreignArtistId": foreign_artist_id},
        }


class NoOpTests(_LidarrRequestsTestBase):
    def test_disabled_playlist_is_a_no_op(self):
        pid = self._make_playlist("Chill", lidarr_request_enabled=False)
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()
        self.assertIsNone(self._row(pid)["lidarr_request_last_run_at"])

    def test_missing_playlist_is_a_no_op(self):
        with mock.patch("lidarr_requests.lidarr_client") as client:
            lidarr_requests.run_for_playlist(self.conn, 999)
        client.lookup_album.assert_not_called()

    def test_not_configured_sets_unset_target_and_never_calls_the_client(self):
        self._clear_lidarr_config()
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()
        row = self._row(pid)
        self.assertEqual(row["lidarr_request_last_error_code"], "unset_target")
        self.assertIsNone(row["lidarr_request_last_run_at"])

    def test_zero_eligible_rows_still_updates_last_run_at(self):
        pid = self._make_playlist("Chill")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()
        row = self._row(pid)
        self.assertIsNotNone(row["lidarr_request_last_run_at"])
        self.assertEqual(row["lidarr_request_last_count"], 0)
        self.assertIsNone(row["lidarr_request_last_error_code"])


class EligibilityTests(_LidarrRequestsTestBase):
    def test_excluded_rows_are_skipped(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album", excluded=True)
        with mock.patch("lidarr_requests.lidarr_client") as client:
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()

    def test_null_album_rows_are_skipped(self):
        # #494 item 9: Roon/iTunes unresolved rows always have album IS
        # NULL — this is the SAME condition as the exclusion rule, not a
        # separately-cased one.
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", None)
        with mock.patch("lidarr_requests.lidarr_client") as client:
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()

    def test_empty_string_album_rows_are_skipped(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()

    def test_already_attempted_pairs_are_skipped_without_a_client_call(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        self.conn.execute(
            "INSERT INTO lidarr_requested_albums "
            "(normalized_artist, normalized_album, artist, album, status) "
            "VALUES (?, ?, 'Artist', 'Album', 'failed')",
            (matching.normalize("Artist"), matching.normalize("Album")),
        )
        self.conn.commit()
        with mock.patch("lidarr_requests.lidarr_client") as client:
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()


class CandidateSelectionTests(unittest.TestCase):
    def test_exact_normalized_artist_match_wins(self):
        candidates = [
            {"artist": {"artistName": "Tribute to Artist"}},
            {"artist": {"artistName": "  Artist  "}},
        ]
        picked = lidarr_requests._pick_candidate(candidates, "artist")
        self.assertEqual(picked, candidates[1])

    def test_no_match_returns_none(self):
        candidates = [{"artist": {"artistName": "Someone Else"}}]
        self.assertIsNone(lidarr_requests._pick_candidate(candidates, "Artist"))

    def test_missing_artist_field_is_treated_as_no_match(self):
        candidates: list[dict] = [{}]
        self.assertIsNone(lidarr_requests._pick_candidate(candidates, "Artist"))


class WriteOutcomeTests(_LidarrRequestsTestBase):
    def test_a_requested_outcome_is_recorded_and_counted(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate("Artist")]}
            client.add_and_monitor_album.return_value = {"status": "ok", "artist_id": 7, "album_id": 99}
            lidarr_requests.run_for_playlist(self.conn, pid)
        row = self._row(pid)
        self.assertEqual(row["lidarr_request_last_count"], 1)
        self.assertIsNone(row["lidarr_request_last_error_code"])
        requested = self._requested_row("Artist", "Album")
        self.assertEqual(requested["status"], "requested")
        self.assertEqual(requested["lidarr_artist_id"], 7)
        self.assertEqual(requested["lidarr_album_id"], 99)

    def test_a_partial_outcome_is_recorded_but_not_counted(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate("Artist")]}
            client.add_and_monitor_album.return_value = {
                "status": "error", "reason": "monitor_failed", "code": 500,
                "stage": "monitor", "artist_id": 7, "album_id": 99,
            }
            lidarr_requests.run_for_playlist(self.conn, pid)
        row = self._row(pid)
        self.assertEqual(row["lidarr_request_last_count"], 0)
        self.assertEqual(row["lidarr_request_last_error_code"], "partial")
        self.assertEqual(row["lidarr_request_last_error"], "monitor_failed")
        requested = self._requested_row("Artist", "Album")
        self.assertEqual(requested["status"], "partial")
        self.assertEqual(requested["lidarr_artist_id"], 7)

    def test_a_create_stage_failure_is_recorded_as_failed(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate("Artist")]}
            client.add_and_monitor_album.return_value = {
                "status": "error", "reason": "create_failed", "code": 500,
                "stage": "create", "artist_id": None, "album_id": None,
            }
            lidarr_requests.run_for_playlist(self.conn, pid)
        row = self._row(pid)
        self.assertEqual(row["lidarr_request_last_error_code"], "failed")
        self.assertEqual(self._requested_row("Artist", "Album")["status"], "failed")

    def test_a_lookup_failure_is_recorded_as_failed(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {"status": "error", "reason": "unreachable", "code": 500}
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.add_and_monitor_album.assert_not_called()
        row = self._row(pid)
        self.assertEqual(row["lidarr_request_last_error_code"], "failed")
        self.assertEqual(row["lidarr_request_last_error"], "unreachable")

    def test_no_candidate_match_is_recorded_as_failed_no_artist_match(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {
                "status": "ok", "candidates": [self._candidate("Some Other Artist")],
            }
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.add_and_monitor_album.assert_not_called()
        requested = self._requested_row("Artist", "Album")
        self.assertEqual(requested["status"], "failed")
        self.assertEqual(requested["error"], "no_artist_match")

    def test_partial_and_failed_rows_are_never_retried_on_a_later_run(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate("Artist")]}
            client.add_and_monitor_album.return_value = {
                "status": "error", "reason": "monitor_failed", "code": 500,
                "stage": "monitor", "artist_id": 7, "album_id": 99,
            }
            lidarr_requests.run_for_playlist(self.conn, pid)
            client.lookup_album.reset_mock()
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()


class CrossPlaylistDedupTests(_LidarrRequestsTestBase):
    def test_the_same_album_missing_from_two_playlists_is_requested_exactly_once(self):
        pid1 = self._make_playlist("Playlist One")
        pid2 = self._make_playlist("Playlist Two")
        self._add_unresolved(pid1, "Artist", "Album")
        self._add_unresolved(pid2, "Artist", "Album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate("Artist")]}
            client.add_and_monitor_album.return_value = {"status": "ok", "artist_id": 7, "album_id": 99}
            lidarr_requests.run_for_playlist(self.conn, pid1)
            lidarr_requests.run_for_playlist(self.conn, pid2)
        self.assertEqual(client.add_and_monitor_album.call_count, 1)
        self.assertEqual(self._row(pid1)["lidarr_request_last_count"], 1)
        self.assertEqual(self._row(pid2)["lidarr_request_last_count"], 0)

    def test_dedup_key_is_normalized_not_literal(self):
        pid1 = self._make_playlist("Playlist One")
        pid2 = self._make_playlist("Playlist Two")
        self._add_unresolved(pid1, "The Artist", "  An Album  ")
        self._add_unresolved(pid2, "the   artist", "an album")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate("The Artist")]}
            client.add_and_monitor_album.return_value = {"status": "ok", "artist_id": 7, "album_id": 99}
            lidarr_requests.run_for_playlist(self.conn, pid1)
            lidarr_requests.run_for_playlist(self.conn, pid2)
        self.assertEqual(client.add_and_monitor_album.call_count, 1)


class RunBookkeepingTests(_LidarrRequestsTestBase):
    def test_error_fields_are_cleared_on_a_clean_run_after_a_failing_one(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist One", "Album One")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            # A permanent failure: Lidarr answers, but nothing matches.
            client.lookup_album.return_value = {"status": "ok", "candidates": []}
            lidarr_requests.run_for_playlist(self.conn, pid)
        self.assertEqual(self._row(pid)["lidarr_request_last_error_code"], "failed")

        self._add_unresolved(pid, "Artist Two", "Album Two")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate("Artist Two")]}
            client.add_and_monitor_album.return_value = {"status": "ok", "artist_id": 1, "album_id": 2}
            lidarr_requests.run_for_playlist(self.conn, pid)
        row = self._row(pid)
        self.assertIsNone(row["lidarr_request_last_error_code"])
        self.assertIsNone(row["lidarr_request_last_error"])
        # The stuck 'Artist One'/'Album One' row from the first run is
        # never retried — this run's count reflects only the new pair.
        self.assertEqual(row["lidarr_request_last_count"], 1)

    def test_last_count_reflects_only_this_runs_requested_rows(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album One")
        self._add_unresolved(pid, "Artist", "Album Two")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            # Same artist on both eligible rows so the fixed candidate list
            # matches regardless of which row is being attempted.
            client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate("Artist")]}
            client.add_and_monitor_album.return_value = {"status": "ok", "artist_id": 1, "album_id": 2}
            lidarr_requests.run_for_playlist(self.conn, pid)
        self.assertEqual(self._row(pid)["lidarr_request_last_count"], 2)



class CancelTests(_LidarrRequestsTestBase):
    """The job's `report` runs before each album; a cancel raised there stops
    the run after the albums already asked for, and still records it."""

    def test_a_cancel_stops_before_the_next_album_and_records_the_run(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "First", "Album One")
        self._add_unresolved(pid, "Second", "Album Two")
        seen = []

        def report(done, total):
            seen.append((done, total))
            if done == 1:
                raise jobs.JobCancelled(jobs.CANCEL_ADMIN)

        with mock.patch.object(lidarr_requests, "_attempt_one",
                               return_value=("requested", None, 11, 12)) as attempt, \
                self.assertRaises(jobs.JobCancelled):
            lidarr_requests.run_for_playlist(self.conn, pid, report)
        self.assertEqual(seen, [(0, 2), (1, 2)])
        attempt.assert_called_once()
        self.assertEqual(self._requested_row("First", "Album One")["status"], "requested")
        self.assertIsNone(self._requested_row("Second", "Album Two"))
        row = self._row(pid)
        self.assertIsNotNone(row["lidarr_request_last_run_at"])
        self.assertEqual(row["lidarr_request_last_count"], 1)

    def test_the_job_passes_its_report_through(self):
        pid = self._make_playlist("Chill")
        report = mock.Mock()
        with mock.patch.object(lidarr_requests, "run_for_playlist") as run:
            lidarr_requests.run_job({"playlist_id": pid}, report)
        self.assertIs(run.call_args.args[2], report)


if __name__ == "__main__":
    unittest.main()


class WriteLockTests(_LidarrRequestsTestBase):
    """The production failure: the write lock held across every Lidarr call
    in a run, so other writers timed out. While a run waits on Lidarr, any
    other connection must be able to take the write lock."""

    def _lock_free(self) -> bool:
        other = db.sqlite3.connect(str(db.DB_PATH), timeout=0.05)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            return True
        except db.sqlite3.OperationalError:
            return False
        finally:
            other.close()

    def test_another_connection_can_write_while_lidarr_is_being_asked(self):
        pid = self._make_playlist("Chill")
        for n in range(3):
            self._add_unresolved(pid, f"Artist {n}", f"Album {n}")
        seen = []

        def slow_lidarr(artist, album):
            seen.append(self._lock_free())
            return "requested", None, 1, 2

        with mock.patch.object(lidarr_requests, "_attempt_one", side_effect=slow_lidarr):
            lidarr_requests.run_for_playlist(self.conn, pid)
        # Every call, including the ones after earlier outcomes were written.
        self.assertEqual(seen, [True, True, True])
        self.assertEqual(self._row(pid)["lidarr_request_last_count"], 3)
        self.assertFalse(self.conn.in_transaction)

    def test_each_outcome_is_committed_before_the_next_call(self):
        """A run cut short keeps what it already did."""
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist One", "Album One")
        self._add_unresolved(pid, "Artist Two", "Album Two")
        committed = []

        def lidarr(artist, album):
            other = db.get_conn()
            try:
                committed.append(other.execute(
                    "SELECT COUNT(*) FROM lidarr_requested_albums").fetchone()[0])
            finally:
                other.close()
            return "requested", None, 1, 2

        with mock.patch.object(lidarr_requests, "_attempt_one", side_effect=lidarr):
            lidarr_requests.run_for_playlist(self.conn, pid)
        self.assertEqual(committed, [0, 1])


class TransientFailureTests(_LidarrRequestsTestBase):
    """Lidarr not answering says nothing about the album: no row is written,
    so the next run asks again. A real answer, even a failing one, is still
    recorded once and never retried."""

    def _attempted(self):
        return [(r["artist"], r["status"]) for r in self.conn.execute(
            "SELECT artist, status FROM lidarr_requested_albums ORDER BY artist")]

    def test_an_unreachable_lidarr_is_asked_again_next_run(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch.object(lidarr_requests, "_attempt_one",
                               return_value=("failed", "unreachable", None, None)):
            lidarr_requests.run_for_playlist(self.conn, pid)
        self.assertEqual(self._attempted(), [])
        row = self._row(pid)
        self.assertEqual((row["lidarr_request_last_error_code"], row["lidarr_request_last_error"]),
                         ("failed", "unreachable"))

        with mock.patch.object(lidarr_requests, "_attempt_one",
                               return_value=("requested", None, 1, 2)) as attempt:
            lidarr_requests.run_for_playlist(self.conn, pid)
        attempt.assert_called_once_with("Artist", "Album")
        self.assertEqual(self._attempted(), [("Artist", "requested")])

    def test_a_permanent_failure_is_recorded_and_not_retried(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch.object(lidarr_requests, "_attempt_one",
                               return_value=("failed", "no_artist_match", None, None)):
            lidarr_requests.run_for_playlist(self.conn, pid)
        with mock.patch.object(lidarr_requests, "_attempt_one") as attempt:
            lidarr_requests.run_for_playlist(self.conn, pid)
        attempt.assert_not_called()
        self.assertEqual(self._attempted(), [("Artist", "failed")])

    def test_init_clears_old_unreachable_rows_and_keeps_the_rest(self):
        for artist, status, error in (("A", "failed", "unreachable"),
                                      ("B", "failed", "no_artist_match"),
                                      ("C", "requested", None)):
            self.conn.execute(
                "INSERT INTO lidarr_requested_albums (normalized_artist, normalized_album, "
                "artist, album, status, error) VALUES (?, 'x', ?, 'X', ?, ?)",
                (artist.lower(), artist, status, error))
        # Only a database from before schema versioning can hold such rows
        # (nothing writes them now), so the cleanup is part of migration 1.
        self.conn.execute("PRAGMA user_version = 0")
        self.conn.commit()
        db.init_db()
        self.assertEqual(self._attempted(), [("B", "failed"), ("C", "requested")])


class LastOutcomeTests(_LidarrRequestsTestBase):
    def test_a_run_that_attempts_nothing_keeps_the_last_outcome(self):
        """The production case: a retried enable found everything already
        asked for and stored 0, hiding the real result."""
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "One", "Album One")
        self._add_unresolved(pid, "Two", "Album Two")
        outcomes = iter([("requested", None, 1, 2), ("failed", "no_artist_match", None, None)])
        with mock.patch.object(lidarr_requests, "_attempt_one", side_effect=lambda a, b: next(outcomes)):
            lidarr_requests.run_for_playlist(self.conn, pid)
        self.conn.execute("UPDATE playlists SET lidarr_request_last_run_at = '2000-01-01 00:00:00' WHERE id = ?", (pid,))
        self.conn.commit()

        with mock.patch.object(lidarr_requests, "_attempt_one") as attempt:
            lidarr_requests.run_for_playlist(self.conn, pid)
        attempt.assert_not_called()
        row = self._row(pid)
        self.assertEqual((row["lidarr_request_last_count"], row["lidarr_request_last_error_code"],
                          row["lidarr_request_last_error"]), (1, "failed", "no_artist_match"))
        self.assertNotEqual(row["lidarr_request_last_run_at"], "2000-01-01 00:00:00")


class JobTests(_LidarrRequestsTestBase):
    def _queued(self):
        return [r["payload"] for r in self.conn.execute(
            "SELECT payload FROM jobs WHERE type = ?", (lidarr_requests.JOB_TYPE,))]

    def test_enqueue_queues_one_run_per_playlist_only_when_requests_are_on(self):
        on, off = self._make_playlist("On"), self._make_playlist("Off", lidarr_request_enabled=False)
        self.assertTrue(lidarr_requests.enqueue(self.conn, on))
        self.assertTrue(lidarr_requests.enqueue(self.conn, on))  # already pending
        self.assertFalse(lidarr_requests.enqueue(self.conn, off))
        self.assertFalse(lidarr_requests.enqueue(self.conn, 999))
        self.assertEqual(self._queued(), [f'{{"playlist_id": {on}}}'])

    def test_the_job_runs_the_playlist_on_its_own_connection(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Artist", "Album")
        with mock.patch.object(lidarr_requests, "_attempt_one",
                               return_value=("requested", None, 1, 2)) as attempt:
            self.assertEqual(lidarr_requests.run_job({"playlist_id": pid}), {"playlist_id": pid})
        attempt.assert_called_once_with("Artist", "Album")
        self.assertEqual(self._row(pid)["lidarr_request_last_count"], 1)


class ChosenAlbumsTests(_LidarrRequestsTestBase):
    """The Gaps view's "Request from Lidarr": albums chosen by hand, through
    the same job and the same dedupe as a playlist's run."""

    def _ok_client(self, client, artist):
        client.lookup_album.return_value = {"status": "ok", "candidates": [self._candidate(artist)]}
        client.add_and_monitor_album.return_value = {"status": "ok", "artist_id": 1, "album_id": 2}

    def test_requests_each_album_and_records_it(self):
        with mock.patch("lidarr_requests.lidarr_client") as client:
            self._ok_client(client, "Aphelion")
            out = lidarr_requests.run_for_albums(self.conn, [("Aphelion", "Parallax")])
        self.assertEqual(out["requested"], 1)
        self.assertEqual(self._requested_row("Aphelion", "Parallax")["status"], "requested")

    def test_an_album_a_playlist_run_already_asked_for_is_not_asked_again(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Aphelion", "Parallax")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            self._ok_client(client, "Aphelion")
            lidarr_requests.run_for_playlist(self.conn, pid)
            client.reset_mock()
            out = lidarr_requests.run_for_albums(self.conn, [("aphelion", "PARALLAX")])
        client.lookup_album.assert_not_called()
        self.assertEqual(out["attempted"], 0)

    def test_and_a_chosen_album_is_not_asked_again_by_a_playlist_run(self):
        pid = self._make_playlist("Chill")
        self._add_unresolved(pid, "Aphelion", "Parallax")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            self._ok_client(client, "Aphelion")
            lidarr_requests.run_for_albums(self.conn, [("Aphelion", "Parallax")])
            client.reset_mock()
            lidarr_requests.run_for_playlist(self.conn, pid)
        client.lookup_album.assert_not_called()

    def test_records_nothing_on_any_playlist(self):
        pid = self._make_playlist("Chill")
        with mock.patch("lidarr_requests.lidarr_client") as client:
            self._ok_client(client, "Aphelion")
            lidarr_requests.run_for_albums(self.conn, [("Aphelion", "Parallax")])
        self.assertIsNone(self._row(pid)["lidarr_request_last_run_at"])

    def test_not_configured_asks_nothing(self):
        self._clear_lidarr_config()
        with mock.patch("lidarr_requests.lidarr_client") as client:
            out = lidarr_requests.run_for_albums(self.conn, [("Aphelion", "Parallax")])
        client.lookup_album.assert_not_called()
        self.assertEqual(out["status"], "unset_target")

    def test_the_job_runs_an_album_list_payload(self):
        # run_job opens its own connection, to this test's database.
        with mock.patch("lidarr_requests.lidarr_client") as client:
            self._ok_client(client, "Aphelion")
            out = lidarr_requests.run_job({"albums": [["Aphelion", "Parallax"]]})
        self.assertEqual(out["requested"], 1)
        self.assertEqual(self._requested_row("Aphelion", "Parallax")["status"], "requested")

    def test_enqueue_albums_queues_one_job_for_the_same_list(self):
        self.assertTrue(lidarr_requests.enqueue_albums(self.conn, [("Aphelion", "Parallax")]))
        self.assertTrue(lidarr_requests.enqueue_albums(self.conn, [("APHELION", "parallax")]))
        n = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE type = ?", (lidarr_requests.JOB_TYPE,)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertTrue(lidarr_requests.enqueue_albums(self.conn, [("Wren", "Kite")]))
        n = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE type = ?", (lidarr_requests.JOB_TYPE,)).fetchone()[0]
        self.assertEqual(n, 2)

    def test_enqueue_albums_queues_nothing_when_empty_or_not_set_up(self):
        self.assertFalse(lidarr_requests.enqueue_albums(self.conn, []))
        self._clear_lidarr_config()
        self.assertFalse(lidarr_requests.enqueue_albums(self.conn, [("Aphelion", "Parallax")]))
        n = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(n, 0)
