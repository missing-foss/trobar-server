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
            client.lookup_album.return_value = {"status": "error", "reason": "unreachable", "code": 500}
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


if __name__ == "__main__":
    unittest.main()
