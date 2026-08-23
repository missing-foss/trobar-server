#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests that 
the five new `tracks` identity columns and the new
`unresolved_playlist_tracks` table, both added via db.py's _MIGRATIONS list
/ SCHEMA. Same in-memory-SQLite harness as test_sync_state.py /
test_playlist_sync.py — no Flask, no DATA_DIR.

    python3 -m unittest test_identity_schema -v      # from app/
"""
import sqlite3
import unittest

import db


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)
    return conn


class TracksIdentityColumnsTests(unittest.TestCase):
    def test_new_tracks_columns_exist(self):
        conn = _make_conn()
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
        for col in ("isrc", "fingerprint", "acoustid_isrc", "acoustid_mbid",
                    "fingerprint_checked_at"):
            self.assertIn(col, cols)

    def test_migration_runs_cleanly_twice(self):
        # The real upgrade path: an already-migrated DB gets _run_migrations
        # called again on every init_db() — must stay a no-op, not raise
        # sqlite3's "duplicate column name".
        conn = _make_conn()
        db._run_migrations(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
        self.assertIn("isrc", cols)

    def test_new_columns_default_to_null(self):
        conn = _make_conn()
        conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('a/b/c.flac', 'A', 'B', 'C', 100, 0.0)"
        )
        conn.commit()
        row = conn.execute(
            "SELECT isrc, fingerprint, acoustid_isrc, acoustid_mbid, "
            "fingerprint_checked_at FROM tracks"
        ).fetchone()
        self.assertEqual(tuple(row), (None, None, None, None, None))


class UnresolvedPlaylistTracksTableTests(unittest.TestCase):
    def _make_playlist(self, conn: sqlite3.Connection) -> int:
        cur = conn.execute("INSERT INTO playlists (title) VALUES ('P')")
        conn.commit()
        assert cur.lastrowid is not None  # always set right after an INSERT
        return cur.lastrowid

    def test_table_exists(self):
        conn = _make_conn()
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("unresolved_playlist_tracks", tables)

    def test_insert_defaults_excluded_to_zero(self):
        conn = _make_conn()
        playlist_id = self._make_playlist(conn)
        conn.execute(
            "INSERT INTO unresolved_playlist_tracks "
            "(playlist_id, position, artist, title, album, isrc) "
            "VALUES (?, 0, 'Art', 'Tit', 'Alb', 'ISRCXYZ')",
            (playlist_id,),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM unresolved_playlist_tracks").fetchone()
        self.assertEqual(row["excluded"], 0)
        self.assertEqual(row["isrc"], "ISRCXYZ")

    def test_cascade_deletes_with_playlist(self):
        # Same ON DELETE CASCADE style as playlist_tracks — a deleted
        # playlist must not leave orphaned review rows behind.
        conn = _make_conn()
        playlist_id = self._make_playlist(conn)
        conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title) "
            "VALUES (?, 0, 'Art', 'Tit')",
            (playlist_id,),
        )
        conn.commit()
        conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM unresolved_playlist_tracks"
        ).fetchone()["c"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
