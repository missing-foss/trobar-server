# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""mirror_runs: recording, pruning and reading a mirror's coverage."""

import shutil
import tempfile
import unittest
from pathlib import Path

import db
import mirror_runs
import sync_state


class MirrorRunsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-mirror-runs-")
        self._saved = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)
        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)
        self.pid = self._playlist("Road Trip")

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _playlist(self, title):
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, last_synced_at) "
            "VALUES (?, 'jellyfin', ?, datetime('now'))", (title, f"j-{title}"))
        self.conn.commit()
        return sync_state._new_id(cur)

    @staticmethod
    def _tracks(n, prefix="T"):
        return [{"artist": "A", "title": f"{prefix}{i}"} for i in range(n)]

    def test_a_run_reads_back_with_its_reasons(self):
        mirror_runs.record(self.conn, self.pid, "jellyfin", 5, 2, self._tracks(2), self._tracks(1, "R"))
        run = mirror_runs.missing(self.conn, self.pid, "jellyfin")
        assert run is not None
        self.assertEqual((run["total"], run["on_target"], run["lacked"], run["refused"]), (5, 2, 2, 1))
        self.assertEqual([m["reason"] for m in run["missing"]],
                         ["not_on_target", "not_on_target", "refused"])

    def test_only_the_last_runs_of_a_pair_are_kept(self):
        for n in range(mirror_runs.KEEP + 3):
            mirror_runs.record(self.conn, self.pid, "jellyfin", 10, n)
        mirror_runs.record(self.conn, self.pid, "plex", 10, 10)
        latest = mirror_runs.latest(self.conn)
        self.assertEqual(latest[(self.pid, "jellyfin")]["history"], [3, 4, 5, 6, 7])
        self.assertEqual(latest[(self.pid, "jellyfin")]["on_target"], 7)
        self.assertEqual(latest[(self.pid, "plex")]["history"], [10], "another sink keeps its own")
        count = self.conn.execute("SELECT COUNT(*) FROM mirror_runs WHERE sink = 'jellyfin'").fetchone()[0]
        self.assertEqual(count, mirror_runs.KEEP)

    def test_the_list_is_capped_but_the_counts_are_not(self):
        lacked = self._tracks(mirror_runs.LIST_MAX + 20)
        mirror_runs.record(self.conn, self.pid, "emby", 200, 70, lacked, self._tracks(10, "R"))
        run = mirror_runs.missing(self.conn, self.pid, "emby")
        assert run is not None
        self.assertEqual(len(run["missing"]), mirror_runs.LIST_MAX)
        self.assertEqual((run["lacked"], run["refused"]), (mirror_runs.LIST_MAX + 20, 10))

    def test_latest_can_be_limited_to_some_playlists(self):
        other = self._playlist("Focus")
        mirror_runs.record(self.conn, self.pid, "subsonic", 3, 3)
        mirror_runs.record(self.conn, other, "subsonic", 4, 4)
        self.assertEqual(set(mirror_runs.latest(self.conn, [other])), {(other, "subsonic")})
        self.assertEqual(mirror_runs.latest(self.conn, []), {})

    def test_a_pair_with_no_run_has_none(self):
        self.assertIsNone(mirror_runs.missing(self.conn, self.pid, "plex"))

    def test_runs_go_with_their_playlist(self):
        mirror_runs.record(self.conn, self.pid, "plex", 3, 3)
        self.conn.execute("DELETE FROM playlists WHERE id = ?", (self.pid,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM mirror_runs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
