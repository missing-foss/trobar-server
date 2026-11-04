# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""track_merge: a vanished track and the new one its file became, told apart
by the scan but the same audio by fingerprint, are merged."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import provenance
import sync_state
import track_merge


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(db.SCHEMA)
        db._run_migrations(self.conn)
        db._migrate_device_plays(self.conn)
        self.user = sync_state._new_id(self.conn.execute("INSERT INTO users (username) VALUES ('o')"))
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")

    def _track(self, path, title="Song", deleted_at=None, scanned_at="2026-10-02 10:00:00", fp="FP"):
        return sync_state._new_id(self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime, "
            "scanned_at, deleted_at, fingerprint) VALUES (?, 'Aphelion', 'Event Horizon', ?, 1, 100, 0, ?, ?, ?)",
            (path, title, scanned_at, deleted_at, fp)))

    def _play(self, track_id, played_at):
        self.conn.execute("INSERT INTO device_plays (user_id, track_id, played_at) VALUES (?, ?, ?)",
                          (self.user, track_id, played_at))

    def test_plays_follow_a_merged_track_and_fold_into_one_it_already_has(self):
        old = self._track("a/1.flac", deleted_at="2026-10-02 09:00:00")
        new = self._track("b/1.flac")
        self._play(old, "2026-10-01 10:00:00")
        self._play(old, "2026-10-01 11:00:00")
        self._play(new, "2026-10-01 11:00:00")
        track_merge._move_references(self.conn, old, new)
        self.assertEqual(sorted(tuple(r) for r in self.conn.execute("SELECT track_id, played_at FROM device_plays")),
                         [(new, "2026-10-01 10:00:00"), (new, "2026-10-01 11:00:00")])

    def _state(self, track_id, status, delivered_path=None):
        self.conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status, delivered_path) VALUES (?, ?, ?, ?)",
            (self.device, track_id, status, delivered_path))

    def _states(self):
        return {r["track_id"]: r["status"] for r in self.conn.execute(
            "SELECT track_id, status FROM device_track_state WHERE device_id = ?", (self.device,))}

    def _vanished_and_new(self, new_title="Song"):
        old = self._track("Old/01.mp3", deleted_at="2026-10-01 10:00:00", scanned_at="2026-09-01 10:00:00")
        new = self._track("New/01.mp3", title=new_title)
        # Delivered once (the ack recorded where), then told to delete.
        self._state(old, "removed", delivered_path="Aphelion/Event Horizon/01 - Song.mp3")
        self._state(new, "pending")
        return old, new

    def test_a_moved_file_with_the_same_device_path_stays_on_the_device(self):
        old, new = self._vanished_and_new()
        sync_state.create_selection(self.conn, "track", str(new), self.user, [self.device])
        self.assertEqual(track_merge.merge_moved_tracks(self.conn), 1)
        self.assertEqual(self._states(), {new: "downloaded"}, "no delete, no download")

    def test_a_different_device_path_still_needs_the_new_file(self):
        old, new = self._vanished_and_new(new_title="Song (Remastered)")
        self.assertEqual(track_merge.merge_moved_tracks(self.conn), 1)
        self.assertEqual(self._states()[old], "removed")

    def test_an_exclusion_carries_over(self):
        old = self._track("Old/01.mp3", deleted_at="2026-10-01 10:00:00", scanned_at="2026-09-01 10:00:00")
        new = self._track("New/01.mp3")
        self._state(old, "excluded")
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.conn.execute("UPDATE device_track_state SET status = 'excluded' WHERE track_id = ?", (old,))
        self.conn.execute("DELETE FROM device_track_state WHERE track_id = ?", (new,))
        track_merge.merge_moved_tracks(self.conn)
        self.assertEqual(self._states(), {new: "excluded"})

    def test_a_track_never_delivered_is_not_marked_as_on_the_device(self):
        # pending: the device never got the old file, so there's nothing to keep;
        # marking the new one downloaded would claim a file the device lacks.
        # The recompute below turns the pending row into removed, as the
        # scan's does: removed alone isn't proof of delivery.
        old = self._track("Old/01.mp3", deleted_at="2026-10-01 10:00:00", scanned_at="2026-09-01 10:00:00")
        new = self._track("New/01.mp3")
        self._state(old, "pending")
        sync_state.create_selection(self.conn, "track", str(new), self.user, [self.device])
        self.assertEqual(track_merge.merge_moved_tracks(self.conn), 1)
        self.assertEqual(self._states(), {new: "pending"})

    def test_references_move_to_the_new_track(self):
        old, new = self._vanished_and_new()
        playlist = sync_state._new_id(self.conn.execute(
            "INSERT INTO playlists (title, source_provider, last_synced_at) VALUES ('Mix', 'roon', datetime('now'))"))
        self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, matched_track_id) VALUES (?, 0, ?)",
                          (playlist, old))
        sync_state.create_selection(self.conn, "track", str(old), self.user, [self.device])
        autofit = sync_state.create_autofit_selection(self.conn, self.device, self.user)
        self.conn.execute("INSERT INTO autofit_tracks (selection_id, track_id) VALUES (?, ?)", (autofit, old))
        track_merge.merge_moved_tracks(self.conn)
        self.assertEqual(self.conn.execute("SELECT matched_track_id FROM playlist_tracks").fetchone()[0], new)
        self.assertEqual([r[0] for r in self.conn.execute(
            "SELECT target FROM selections WHERE type = 'track'")], [str(new)])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT track_id FROM autofit_tracks")], [new])

    def test_a_track_selection_the_new_track_already_has_absorbs_the_old_one(self):
        old, new = self._vanished_and_new()
        other, _ = sync_state.create_device(self.conn, self.user, "player")
        sync_state.create_selection(self.conn, "track", str(new), self.user, [self.device])
        sync_state.create_selection(self.conn, "track", str(old), self.user, [other])
        track_merge.merge_moved_tracks(self.conn)
        rows = self.conn.execute(
            "SELECT s.target, sd.device_id FROM selections s JOIN selection_devices sd ON sd.selection_id = s.id "
            "WHERE s.type = 'track' ORDER BY sd.device_id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(str(new), self.device), (str(new), other)])

    def test_a_move_found_in_one_scan_is_merged(self):
        # The real ordering within a scan: the new file is inserted during the
        # walk, a minute before the vanished track is processed, and the
        # vanished track is stamped with the scan's start.
        old = self._track("Old/01.mp3", deleted_at="2026-10-02 10:00:00", scanned_at="2026-09-01 10:00:00")
        new = self._track("New/01.mp3", scanned_at="2026-10-02 10:00:30")
        self._state(old, "removed", delivered_path="Aphelion/Event Horizon/01 - Song.mp3")
        sync_state.create_selection(self.conn, "track", str(new), self.user, [self.device])
        self.assertEqual(track_merge.merge_moved_tracks(self.conn), 1)
        self.assertEqual(self._states(), {new: "downloaded"})

    def test_an_ambiguous_fingerprint_is_left_alone(self):
        old, new = self._vanished_and_new()
        self._track("Copy/01.mp3")  # a second live file with the same audio
        self.assertEqual(track_merge.merge_moved_tracks(self.conn), 0)
        self.assertEqual(self._states(), {old: "removed", new: "pending"})

    def test_a_live_track_that_was_there_before_the_other_vanished_is_not_its_new_file(self):
        old = self._track("Old/01.mp3", deleted_at="2026-10-01 10:00:00", scanned_at="2026-09-01 10:00:00")
        self._track("Other/01.mp3", scanned_at="2026-09-15 10:00:00")
        self._state(old, "removed")
        self.assertEqual(track_merge.merge_moved_tracks(self.conn), 0)


class FingerprintPassTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-track-merge-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._orig

    def test_the_library_pass_merges_after_computing_fingerprints(self):
        rows = [[{"id": 1, "relative_path": "a.mp3"}], []]
        with mock.patch.object(provenance, "_library_track_rows", side_effect=lambda: rows.pop(0)), \
                mock.patch.object(provenance, "_compute_one", return_value=True), \
                mock.patch.object(provenance, "library_fingerprints_pending", side_effect=[1, 0]), \
                mock.patch.object(track_merge, "merge_moved_tracks", return_value=2) as merge:
            result = provenance.ensure_library_fingerprints()
        merge.assert_called_once()
        self.assertEqual(result["merged"], 2)

    def test_a_pass_that_computed_nothing_does_not_merge(self):
        with mock.patch.object(provenance, "_library_track_rows", return_value=[]), \
                mock.patch.object(provenance, "library_fingerprints_pending", return_value=0), \
                mock.patch.object(track_merge, "merge_moved_tracks") as merge:
            provenance.ensure_library_fingerprints()
        merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
