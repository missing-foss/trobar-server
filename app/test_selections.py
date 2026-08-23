#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests that selections had no uniqueness guard, so a
double-click/retried POST /api/selections could create duplicate rows for
the same (type, target, created_by_user_id) — which the Selections matrix
UI's `x-for :key="row.target"` can't render distinctly.

    python3 -m unittest test_selections -v      # from app/

Uses an in-memory sqlite DB built from db.SCHEMA — no Flask, no fixtures
on disk.
"""
import sqlite3
import unittest

import db
import sync_state


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)  # devices.transcode_format/artist_images are migration-added
    return conn


def _make_user(conn: sqlite3.Connection, username: str = "alice") -> int:
    cur = conn.execute("INSERT INTO users (username) VALUES (?)", (username,))
    conn.commit()
    return sync_state._new_id(cur)


def _make_device(conn: sqlite3.Connection, owner_user_id: int, name: str = "phone1") -> int:
    device_id, _token = sync_state.create_device(conn, owner_user_id, name)
    return device_id


class CreateSelectionIdempotencyTests(unittest.TestCase):
    """create_selection() must be find-or-create, matching
    toggle_selection_device()'s existing shape — a repeat call for the same
    (type, target, user) should join device_ids onto the existing row, not
    create a second one."""

    def setUp(self):
        self.conn = _make_conn()
        self.user_id = _make_user(self.conn)
        self.device1 = _make_device(self.conn, self.user_id, "phone1")
        self.device2 = _make_device(self.conn, self.user_id, "phone2")

    def test_repeat_call_reuses_row(self):
        first = sync_state.create_selection(self.conn, "album", "Artist||Album", self.user_id, [self.device1])
        second = sync_state.create_selection(self.conn, "album", "Artist||Album", self.user_id, [self.device2])
        self.assertEqual(first, second)
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM selections WHERE type='album' AND target='Artist||Album'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_repeat_call_unions_device_ids(self):
        selection_id = sync_state.create_selection(self.conn, "album", "Artist||Album", self.user_id, [self.device1])
        sync_state.create_selection(self.conn, "album", "Artist||Album", self.user_id, [self.device2])
        device_ids = {
            row["device_id"] for row in self.conn.execute(
                "SELECT device_id FROM selection_devices WHERE selection_id = ?", (selection_id,)
            )
        }
        self.assertEqual(device_ids, {self.device1, self.device2})

    def test_different_users_get_separate_rows(self):
        other_user = _make_user(self.conn, "bob")
        first = sync_state.create_selection(self.conn, "album", "Artist||Album", self.user_id, [self.device1])
        second = sync_state.create_selection(self.conn, "album", "Artist||Album", other_user, [self.device1])
        self.assertNotEqual(first, second)


class DedupeSelectionsMigrationTests(unittest.TestCase):
    """db._dedupe_selections() cleans up rows created before the
    find-or-create fix existed, and installs the unique index that stops
    it recurring (excluding autofit, which is legitimately one row per
    device even when type/target/user match)."""

    def setUp(self):
        self.conn = _make_conn()
        self.user_id = _make_user(self.conn)
        self.device1 = _make_device(self.conn, self.user_id, "phone1")
        self.device2 = _make_device(self.conn, self.user_id, "phone2")

    def _raw_insert_selection(self, sel_type: str, target: str, device_ids: list[int]) -> int:
        # Bypasses create_selection() on purpose, to simulate rows that
        # predate the find-or-create fix.
        cur = self.conn.execute(
            "INSERT INTO selections (type, target, created_by_user_id) VALUES (?, ?, ?)",
            (sel_type, target, self.user_id),
        )
        selection_id = sync_state._new_id(cur)
        for device_id in device_ids:
            self.conn.execute(
                "INSERT INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
                (selection_id, device_id),
            )
        self.conn.commit()
        return selection_id

    def test_merges_duplicates_and_unions_devices(self):
        keep_id = self._raw_insert_selection("playlist", "7", [self.device1])
        dupe_id = self._raw_insert_selection("playlist", "7", [self.device2])

        db._dedupe_selections(self.conn)
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT id FROM selections WHERE type='playlist' AND target='7'"
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], [keep_id])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM selections WHERE id = ?", (dupe_id,)
            ).fetchone()["n"],
            0,
        )
        device_ids = {
            row["device_id"] for row in self.conn.execute(
                "SELECT device_id FROM selection_devices WHERE selection_id = ?", (keep_id,)
            )
        }
        self.assertEqual(device_ids, {self.device1, self.device2})

    def test_unique_index_blocks_future_duplicates(self):
        db._dedupe_selections(self.conn)
        self.conn.commit()
        self.conn.execute(
            "INSERT INTO selections (type, target, created_by_user_id) VALUES ('artist', 'Some Artist', ?)",
            (self.user_id,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO selections (type, target, created_by_user_id) VALUES ('artist', 'Some Artist', ?)",
                (self.user_id,),
            )

    def test_autofit_duplicates_are_left_alone(self):
        # Two devices, same user, same period — a legitimate, non-duplicate
        # situation for autofit (see create_autofit_selection).
        sync_state.create_autofit_selection(self.conn, self.device1, self.user_id, "12month")
        sync_state.create_autofit_selection(self.conn, self.device2, self.user_id, "12month")

        db._dedupe_selections(self.conn)
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM selections WHERE type='autofit' AND target='12month'"
        ).fetchone()
        self.assertEqual(rows["n"], 2)


if __name__ == "__main__":
    unittest.main()
