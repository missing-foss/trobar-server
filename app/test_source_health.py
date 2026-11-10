# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""source_health: failures in a row per source, the alert rule (ALERT_RUNS
runs spanning ALERT_HOURS), the clear on success, and forgetting what a run
no longer tries."""

import sqlite3
import unittest

import db
import source_health as sh


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)
    db._migrate_source_health(conn)
    return conn


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        self.alice = self.conn.execute("INSERT INTO users (username) VALUES ('alice')").lastrowid
        self.conn.commit()

    def _tidal(self, ok, reason="http_404"):
        return sh.outcome(sh.LINKED_ACCOUNT, "tidal", ok, reason, user_id=self.alice)

    def _library(self, ok=True):
        return sh.outcome(sh.LIBRARY_SOURCE, "jellyfin", ok, "not_paired")

    def _row(self):
        return {r["provider"]: r for r in sh.rows(self.conn)}["tidal"]

    def _age(self, hours):
        """Moves the failure's start back, as if the runs so far spanned `hours`."""
        self.conn.execute("UPDATE source_health SET failing_since = datetime('now', ?) "
                          "WHERE provider = 'tidal'", (f"-{hours} hours",))
        self.conn.commit()

    def test_two_failures_do_not_alert(self):
        for _ in range(2):
            sh.record(self.conn, [self._library(), self._tidal(False)])
        row = self._row()
        self.assertEqual((row["runs"], row["alerting"], row["reason"]), (2, False, "http_404"))
        self.assertEqual(row["account"], "alice")

    def test_three_failures_alert_only_once_they_span_twelve_hours(self):
        for _ in range(3):
            sh.record(self.conn, [self._library(), self._tidal(False)])
        self.assertFalse(self._row()["alerting"], "three quick runs are not half a day")
        self._age(sh.ALERT_HOURS + 1)
        sh.record(self.conn, [self._library(), self._tidal(False)])
        self.assertTrue(self._row()["alerting"])
        self.assertEqual(self._row()["runs"], 4)

    def test_starting_to_alert_is_logged_once(self):
        for _ in range(3):
            sh.record(self.conn, [self._library(), self._tidal(False)])
        self._age(sh.ALERT_HOURS + 1)
        with self.assertLogs("source_health", "WARNING") as logs:
            sh.record(self.conn, [self._library(), self._tidal(False)])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("linked_account tidal keeps failing (4 runs): http_404", logs.output[0])
        with self.assertNoLogs("source_health", "WARNING"):
            sh.record(self.conn, [self._library(), self._tidal(False)])

    def test_a_long_failure_with_fewer_runs_does_not_alert(self):
        sh.record(self.conn, [self._library(), self._tidal(False)])
        self._age(48)
        sh.record(self.conn, [self._library(), self._tidal(False)])
        self.assertFalse(self._row()["alerting"], "two runs, however far apart")

    def test_the_first_success_clears_it(self):
        for _ in range(3):
            sh.record(self.conn, [self._library(), self._tidal(False)])
        self._age(sh.ALERT_HOURS + 1)
        sh.record(self.conn, [self._library(), self._tidal(False)])
        sh.record(self.conn, [self._library(), self._tidal(True)])
        row = self._row()
        self.assertEqual((row["runs"], row["alerting"], row["failing_since"], row["reason"]), (0, False, None, None))
        self.assertIsNotNone(row["last_success_at"])

    def test_the_first_failure_keeps_its_start(self):
        sh.record(self.conn, [self._library(), self._tidal(False)])
        self._age(5)
        since = self._row()["failing_since"]
        sh.record(self.conn, [self._library(), self._tidal(False, "timeout")])
        self.assertEqual(self._row()["failing_since"], since)
        self.assertEqual(self._row()["reason"], "timeout")

    def test_a_source_no_longer_tried_is_forgotten(self):
        for _ in range(4):
            sh.record(self.conn, [self._library(), self._tidal(False)])
        self._age(sh.ALERT_HOURS + 1)
        sh.record(self.conn, [self._library(), self._tidal(False)])
        sh.record(self.conn, [self._library()])  # unlinked: nothing to try
        self.assertEqual([r["provider"] for r in sh.rows(self.conn)], ["jellyfin"])

    def test_alerting_sources_come_first(self):
        for _ in range(3):
            sh.record(self.conn, [self._library(), self._tidal(False)])
        self._age(sh.ALERT_HOURS + 1)
        sh.record(self.conn, [self._library(), self._tidal(False)])
        self.assertEqual([r["provider"] for r in sh.rows(self.conn)], ["tidal", "jellyfin"])
        self.assertEqual([r["provider"] for r in sh.rows(self.conn, alerting_only=True)], ["tidal"])

    def test_a_reason_is_one_short_line(self):
        o = sh.outcome(sh.LIBRARY_SOURCE, "plex", False, "boom\nWARNING forged\r\n" + "x" * 500)
        self.assertNotIn("\n", o["reason"])
        self.assertNotIn("\r", o["reason"])
        self.assertLessEqual(len(o["reason"]), 200)
        self.assertEqual(sh.outcome(sh.LIBRARY_SOURCE, "plex", False)["reason"], "no reason given")

    def test_one_outcome_per_source_per_run(self):
        sh.record(self.conn, [self._tidal(False), self._tidal(True)])
        self.assertEqual(self._row()["runs"], 1, "the first outcome for a source says it")


if __name__ == "__main__":
    unittest.main()
