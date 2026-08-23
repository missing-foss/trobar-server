#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests that the `tracks` and `unresolved_playlist_tracks` STRICT
conversions.

The bug this closes is silent, which is why it needed a schema fix rather than
another call-site fix. SQLite's TEXT affinity accepts Python `bytes` and stores
a BLOB without complaint; the value then never matches an `=` comparison. That
cost us twice (#292 fingerprint.py, #296 provenance.py), and `tracks.fingerprint`
is now *identity* for #239's recovery rematch, so a wrong-typed value there means
confidently failing to recognise a file rather than raising.

Two properties are load-bearing here, for each table:
  - STRICT rejects the bad write at insert time (RejectsMistypedWritesTests);
  - the rebuild that gets an existing DB there preserves ids, rows, columns and
    indexes, and repairs any already-stored BLOB (RebuildTests /
    UnresolvedPlaylistTracksRebuildTests).

    python3 -m unittest test_db_strict -v      # from app/
"""
import logging
import sqlite3
import unittest

import db

# The pre-#298 `tracks` — the original 13 columns, no STRICT. SCHEMA now creates
# the STRICT version, and CREATE TABLE IF NOT EXISTS won't replace this one, so
# building it first is what simulates an existing deployment's database.
_LEGACY_TRACKS = """
CREATE TABLE tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path TEXT UNIQUE NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    title TEXT NOT NULL,
    track_no INTEGER,
    disc_no INTEGER,
    year INTEGER,
    reissue_year INTEGER,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    scanned_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);
"""


def _fresh_conn() -> sqlite3.Connection:
    """A current-code database: SCHEMA's tracks is already STRICT."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)
    return conn


def _legacy_conn() -> sqlite3.Connection:
    """An existing deployment's database: non-STRICT tracks, then the columns
    _MIGRATIONS adds — the exact state _migrate_tracks_strict has to convert."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_LEGACY_TRACKS)
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)
    return conn


_LEGACY_UNRESOLVED_PLAYLIST_TRACKS = """
CREATE TABLE unresolved_playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    artist TEXT,
    title TEXT,
    album TEXT,
    isrc TEXT,
    excluded INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _legacy_unresolved_conn() -> sqlite3.Connection:
    """An existing deployment's database: non-STRICT unresolved_playlist_tracks
    — the exact state _migrate_unresolved_playlist_tracks_strict has to
    convert. The FK forward-references `playlists`, which SCHEMA (executed
    next) hasn't created yet at this point — SQLite allows that, it isn't
    resolved until an FK-checked write happens."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_LEGACY_UNRESOLVED_PLAYLIST_TRACKS)
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)
    return conn


def _insert_unresolved(conn, playlist_id, position=0, **over):
    row = dict(playlist_id=playlist_id, position=position)
    row.update(over)
    cols = ", ".join(row)
    conn.execute(
        f"INSERT INTO unresolved_playlist_tracks ({cols}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_track(conn, path="a/b.flac", **over):
    row = dict(relative_path=path, artist="A", album="B", title="T",
               size=1, mtime=1.0)
    row.update(over)
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO tracks ({cols}) VALUES ({', '.join('?' * len(row))})",
                 tuple(row.values()))
    return conn.execute("SELECT id FROM tracks WHERE relative_path = ?",
                        (path,)).fetchone()["id"]


class LegacyHarnessTests(unittest.TestCase):
    """If _legacy_conn stopped producing a non-STRICT table, every RebuildTests
    case below would pass without exercising the rebuild at all."""

    def test_the_legacy_harness_really_is_not_strict(self):
        conn = _legacy_conn()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='tracks'").fetchone()["sql"]
        self.assertNotIn("STRICT", sql.upper())

    def test_the_legacy_harness_has_the_migrated_columns(self):
        # The rebuild lists 21 columns; if _run_migrations hadn't run, the
        # INSERT...SELECT would fail on a missing column instead of converting.
        cols = {r["name"] for r in _legacy_conn().execute("PRAGMA table_info(tracks)")}
        self.assertIn("fingerprint", cols)
        self.assertIn("acoustid_isrc", cols)


class RejectsMistypedWritesTests(unittest.TestCase):
    """The point of the whole issue."""

    def test_bytes_into_fingerprint_is_rejected(self):
        conn = _fresh_conn()
        _insert_track(conn)
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            conn.execute("UPDATE tracks SET fingerprint = ? WHERE id = 1", (b"AQAAAA",))
        self.assertIn("BLOB", str(caught.exception))

    def test_bytes_into_isrc_and_acoustid_isrc_are_rejected(self):
        conn = _fresh_conn()
        _insert_track(conn)
        for col in ("isrc", "acoustid_isrc"):
            with self.subTest(col=col), self.assertRaises(sqlite3.IntegrityError):
                conn.execute(f"UPDATE tracks SET {col} = ? WHERE id = 1", (b"GBAYE",))

    def test_a_correctly_typed_str_still_works(self):
        # STRICT must not break the normal path.
        conn = _fresh_conn()
        _insert_track(conn)
        conn.execute("UPDATE tracks SET fingerprint = ? WHERE id = 1", ("AQAAAA",))
        self.assertEqual(
            conn.execute("SELECT fingerprint FROM tracks WHERE id = 1").fetchone()[0],
            "AQAAAA")

    def test_fresh_schema_creates_tracks_strict(self):
        sql = _fresh_conn().execute(
            "SELECT sql FROM sqlite_master WHERE name='tracks'").fetchone()["sql"]
        self.assertIn("STRICT", sql.upper())


class RebuildTests(unittest.TestCase):
    def test_converts_an_existing_table_to_strict(self):
        conn = _legacy_conn()
        _insert_track(conn)
        db._migrate_tracks_strict(conn)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='tracks'").fetchone()["sql"]
        self.assertIn("STRICT", sql.upper())

    def test_preserves_every_column(self):
        conn = _legacy_conn()
        before = [r["name"] for r in conn.execute("PRAGMA table_info(tracks)")]
        db._migrate_tracks_strict(conn)
        after = [r["name"] for r in conn.execute("PRAGMA table_info(tracks)")]
        # Ordering too: sqlite3.Row lookups are by name, but a SELECT * consumer
        # positionally unpacking would break on a reorder.
        self.assertEqual(before, after)

    def test_preserves_rows_and_ids(self):
        # id preservation is the correctness guarantee for every FK into tracks.
        conn = _legacy_conn()
        ids = [_insert_track(conn, f"d/{n}.flac", title=f"T{n}") for n in range(5)]
        conn.execute("DELETE FROM tracks WHERE relative_path = 'd/2.flac'")
        expected = [i for i in ids if i != ids[2]]
        db._migrate_tracks_strict(conn)
        self.assertEqual(
            [r["id"] for r in conn.execute("SELECT id FROM tracks ORDER BY id")],
            expected)

    def test_preserves_values_including_real_and_integer(self):
        conn = _legacy_conn()
        _insert_track(conn, "x.flac", track_no=7, year=1969, mtime=12.5, duration=210.25)
        db._migrate_tracks_strict(conn)
        row = conn.execute(
            "SELECT track_no, year, mtime, duration FROM tracks").fetchone()
        self.assertEqual((row["track_no"], row["year"]), (7, 1969))
        self.assertAlmostEqual(row["mtime"], 12.5)
        self.assertAlmostEqual(row["duration"], 210.25)

    def test_repairs_a_blob_fingerprint_so_equality_matches_again(self):
        # The actual damage from #292/#296: written as bytes, stored as a BLOB,
        # and therefore invisible to the lookup #239's rematch does.
        conn = _legacy_conn()
        track_id = _insert_track(conn, "blob.flac")
        conn.execute("UPDATE tracks SET fingerprint = ? WHERE id = ?",
                     (b"AQAAAA", track_id))
        self.assertEqual(
            conn.execute("SELECT typeof(fingerprint) FROM tracks").fetchone()[0], "blob")
        # Precondition: this is what silently fails today.
        self.assertIsNone(conn.execute(
            "SELECT id FROM tracks WHERE fingerprint = ?", ("AQAAAA",)).fetchone())

        db._migrate_tracks_strict(conn)

        self.assertEqual(
            conn.execute("SELECT typeof(fingerprint) FROM tracks").fetchone()[0], "text")
        found = conn.execute("SELECT id FROM tracks WHERE fingerprint = ?",
                             ("AQAAAA",)).fetchone()
        self.assertIsNotNone(found, "the repaired fingerprint still doesn't match")
        self.assertEqual(found["id"], track_id)

    def test_logs_which_column_it_repaired(self):
        conn = _legacy_conn()
        _insert_track(conn, "blob.flac")
        conn.execute("UPDATE tracks SET fingerprint = ? WHERE id = 1", (b"AQAAAA",))
        with self.assertLogs("db", level=logging.WARNING) as caught:
            db._migrate_tracks_strict(conn)
        self.assertTrue(any("tracks.fingerprint" in m for m in caught.output),
                        f"the warning should name the column: {caught.output}")

    def test_a_mistyped_integer_fails_loudly_instead_of_becoming_zero(self):
        # INTEGER/REAL are copied uncast on purpose: CAST('abc' AS INTEGER) is 0,
        # which would turn data we don't understand into plausible-looking data.
        conn = _legacy_conn()
        _insert_track(conn, "bad.flac")
        conn.execute("UPDATE tracks SET track_no = ? WHERE id = 1", ("abc",))
        self.assertEqual(
            conn.execute("SELECT typeof(track_no) FROM tracks").fetchone()[0], "text")
        with self.assertRaises(sqlite3.IntegrityError):
            db._migrate_tracks_strict(conn)

    def test_a_failed_rebuild_leaves_the_original_table_intact(self):
        # The rollback path: startup may fail, but it must not lose the library.
        conn = _legacy_conn()
        _insert_track(conn, "bad.flac", title="Keep me")
        conn.execute("UPDATE tracks SET track_no = ? WHERE id = 1", ("abc",))
        with self.assertRaises(sqlite3.IntegrityError):
            db._migrate_tracks_strict(conn)
        self.assertEqual(
            conn.execute("SELECT title FROM tracks").fetchone()["title"], "Keep me")
        self.assertIsNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE name='tracks_new'").fetchone())

    def test_recreates_both_indexes(self):
        # DROP TABLE takes them with it, and neither creator runs again. Losing
        # idx_tracks_fingerprint wouldn't error — it would quietly make #239's
        # rematch a full scan per pushed entry.
        conn = _legacy_conn()
        db._migrate_tracks_strict(conn)
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tracks' "
            "AND sql IS NOT NULL")}
        self.assertIn("idx_tracks_artist_album", names)
        self.assertIn("idx_tracks_fingerprint", names)

    def test_the_fingerprint_index_is_actually_used_by_the_rematch_query(self):
        conn = _legacy_conn()
        db._migrate_tracks_strict(conn)
        plan = " ".join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id, relative_path, fingerprint FROM tracks "
            "WHERE fingerprint = ? AND deleted_at IS NULL ORDER BY id LIMIT 1",
            ("AQAAAA",)))
        self.assertIn("idx_tracks_fingerprint", plan)

    def test_does_not_cascade_delete_rows_referencing_tracks(self):
        # DROP TABLE tracks with foreign_keys ON would take device_track_state
        # and playlist_tracks with it. This is why the swap turns FKs off.
        conn = _legacy_conn()
        track_id = _insert_track(conn, "keep.flac")
        conn.execute("INSERT INTO users (id, username) VALUES (1, 'u')")
        conn.execute(
            "INSERT INTO devices (id, owner_user_id, name, api_token_hash) "
            "VALUES (1, 1, 'D', 'h')")
        conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status) VALUES (1, ?, 'downloaded')",
            (track_id,))
        conn.commit()
        db._migrate_tracks_strict(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM device_track_state").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM device_track_state d JOIN tracks t ON t.id = d.track_id"
        ).fetchone()[0], 1, "the FK no longer resolves — ids were not preserved")

    def test_foreign_keys_are_back_on_afterwards(self):
        conn = _legacy_conn()
        db._migrate_tracks_strict(conn)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_is_idempotent(self):
        conn = _legacy_conn()
        _insert_track(conn)
        db._migrate_tracks_strict(conn)
        db._migrate_tracks_strict(conn)  # the every-startup path
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 1)

    def test_is_a_no_op_on_an_already_strict_table(self):
        # Cheapness matters: this runs on every single startup.
        conn = _fresh_conn()
        _insert_track(conn)
        with self.assertNoLogs("db", level=logging.WARNING):
            db._migrate_tracks_strict(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 1)


class UnresolvedPlaylistTracksRejectsMistypedWritesTests(unittest.TestCase):
    def test_bytes_into_isrc_is_rejected(self):
        conn = _fresh_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        pid = _insert_unresolved(conn, 1)
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            conn.execute(
                "UPDATE unresolved_playlist_tracks SET isrc = ? WHERE id = ?",
                (b"GBAYE0000001", pid))
        self.assertIn("BLOB", str(caught.exception))

    def test_a_correctly_typed_str_still_works(self):
        conn = _fresh_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        pid = _insert_unresolved(conn, 1)
        conn.execute(
            "UPDATE unresolved_playlist_tracks SET isrc = ? WHERE id = ?",
            ("GBAYE0000001", pid))
        self.assertEqual(
            conn.execute("SELECT isrc FROM unresolved_playlist_tracks WHERE id = ?",
                         (pid,)).fetchone()[0],
            "GBAYE0000001")

    def test_fresh_schema_creates_the_table_strict(self):
        sql = _fresh_conn().execute(
            "SELECT sql FROM sqlite_master WHERE name='unresolved_playlist_tracks'"
        ).fetchone()["sql"]
        self.assertIn("STRICT", sql.upper())


class UnresolvedPlaylistTracksRebuildTests(unittest.TestCase):
    def test_converts_an_existing_table_to_strict(self):
        conn = _legacy_unresolved_conn()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='unresolved_playlist_tracks'"
        ).fetchone()["sql"]
        self.assertNotIn("STRICT", sql.upper())  # precondition
        db._migrate_unresolved_playlist_tracks_strict(conn)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='unresolved_playlist_tracks'"
        ).fetchone()["sql"]
        self.assertIn("STRICT", sql.upper())

    def test_preserves_every_column(self):
        conn = _legacy_unresolved_conn()
        before = [r["name"] for r in conn.execute(
            "PRAGMA table_info(unresolved_playlist_tracks)")]
        db._migrate_unresolved_playlist_tracks_strict(conn)
        after = [r["name"] for r in conn.execute(
            "PRAGMA table_info(unresolved_playlist_tracks)")]
        self.assertEqual(before, after)

    def test_preserves_rows_and_ids(self):
        conn = _legacy_unresolved_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        ids = [_insert_unresolved(conn, 1, position=n, title=f"T{n}") for n in range(5)]
        conn.execute("DELETE FROM unresolved_playlist_tracks WHERE title = 'T2'")
        expected = [i for i in ids if i != ids[2]]
        db._migrate_unresolved_playlist_tracks_strict(conn)
        self.assertEqual(
            [r["id"] for r in conn.execute(
                "SELECT id FROM unresolved_playlist_tracks ORDER BY id")],
            expected)

    def test_repairs_a_blob_isrc_so_equality_matches_again(self):
        conn = _legacy_unresolved_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        row_id = _insert_unresolved(conn, 1)
        conn.execute(
            "UPDATE unresolved_playlist_tracks SET isrc = ? WHERE id = ?",
            (b"GBAYE0000001", row_id))
        self.assertEqual(
            conn.execute("SELECT typeof(isrc) FROM unresolved_playlist_tracks"
                         ).fetchone()[0], "blob")
        self.assertIsNone(conn.execute(
            "SELECT id FROM unresolved_playlist_tracks WHERE isrc = ?",
            ("GBAYE0000001",)).fetchone())

        db._migrate_unresolved_playlist_tracks_strict(conn)

        self.assertEqual(
            conn.execute("SELECT typeof(isrc) FROM unresolved_playlist_tracks"
                         ).fetchone()[0], "text")
        found = conn.execute(
            "SELECT id FROM unresolved_playlist_tracks WHERE isrc = ?",
            ("GBAYE0000001",)).fetchone()
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], row_id)

    def test_a_failed_rebuild_leaves_the_original_table_intact(self):
        conn = _legacy_unresolved_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        row_id = _insert_unresolved(conn, 1, title="Keep me")
        conn.execute(
            "UPDATE unresolved_playlist_tracks SET position = ? WHERE id = ?",
            ("abc", row_id))
        with self.assertRaises(sqlite3.IntegrityError):
            db._migrate_unresolved_playlist_tracks_strict(conn)
        self.assertEqual(
            conn.execute("SELECT title FROM unresolved_playlist_tracks"
                         ).fetchone()["title"], "Keep me")
        self.assertIsNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE name='unresolved_playlist_tracks_new'"
        ).fetchone())

    def test_recreates_both_indexes(self):
        conn = _legacy_unresolved_conn()
        db._migrate_unresolved_playlist_tracks_strict(conn)
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='unresolved_playlist_tracks' AND sql IS NOT NULL")}
        self.assertIn("idx_unresolved_playlist_tracks_playlist", names)
        self.assertIn("idx_unresolved_playlist_tracks_identity", names)

    def test_the_identity_index_still_enforces_uniqueness_after_rebuild(self):
        conn = _legacy_unresolved_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        db._migrate_unresolved_playlist_tracks_strict(conn)
        _insert_unresolved(conn, 1, artist="A", title="T", album="B")
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_unresolved(conn, 1, artist="A", title="T", album="B")

    def test_does_not_cascade_delete_via_a_playlist_delete_after_rebuild(self):
        # The FK itself (ON DELETE CASCADE from playlists) must still behave
        # normally post-rebuild — this is the one real behaviour the rebuilt
        # table's DDL needs to reproduce exactly, not just "some FK clause".
        conn = _legacy_unresolved_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        _insert_unresolved(conn, 1)
        db._migrate_unresolved_playlist_tracks_strict(conn)
        conn.execute("DELETE FROM playlists WHERE id = 1")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM unresolved_playlist_tracks").fetchone()[0], 0)

    def test_foreign_keys_are_back_on_afterwards(self):
        conn = _legacy_unresolved_conn()
        db._migrate_unresolved_playlist_tracks_strict(conn)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_is_idempotent(self):
        conn = _legacy_unresolved_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        _insert_unresolved(conn, 1)
        db._migrate_unresolved_playlist_tracks_strict(conn)
        db._migrate_unresolved_playlist_tracks_strict(conn)  # the every-startup path
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM unresolved_playlist_tracks").fetchone()[0], 1)

    def test_is_a_no_op_on_an_already_strict_table(self):
        conn = _fresh_conn()
        conn.execute("INSERT INTO playlists (id, title) VALUES (1, 'P')")
        _insert_unresolved(conn, 1)
        with self.assertNoLogs("db", level=logging.WARNING):
            db._migrate_unresolved_playlist_tracks_strict(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM unresolved_playlist_tracks").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
