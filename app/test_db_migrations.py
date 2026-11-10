#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""db.init_db()'s numbered migrations: each runs once, in order, recorded in
PRAGMA user_version; a database from a newer build stops the start; the
every-start maintenance still runs on every start."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db


def _user_version() -> int:
    with db.connection() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_user_version(version: int) -> None:
    with db.connection() as conn:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("DATA_DIR", Path(tmp.name)), ("DB_PATH", Path(tmp.name) / "t.db")):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _patch_migrations(self, *steps):
        """The real migration 1 (the every-start steps need its schema),
        then these as migrations 2, 3, ..."""
        patcher = mock.patch.object(db, "MIGRATIONS", (db._migration_1, *steps))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_new_database_ends_at_the_latest_version(self):
        db.init_db()
        self.assertEqual(_user_version(), len(db.MIGRATIONS))
        with db.connection() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'tracks'").fetchone())

    def test_each_migration_runs_once(self):
        calls = []
        self._patch_migrations(lambda conn: calls.append(2), lambda conn: calls.append(3))
        db.init_db()
        db.init_db()
        self.assertEqual(calls, [2, 3])
        self.assertEqual(_user_version(), 3)

    def test_only_the_missing_migrations_run(self):
        calls = []
        self._patch_migrations(lambda conn: calls.append(2), lambda conn: calls.append(3))
        db.init_db()
        _set_user_version(2)
        calls.clear()
        db.init_db()
        self.assertEqual(calls, [3])

    def test_a_failing_migration_keeps_the_ones_before_it(self):
        def first(conn):
            conn.execute("CREATE TABLE first_step (v TEXT)")

        def second(conn):
            conn.execute("CREATE TABLE second_step (v TEXT)")
            raise RuntimeError("boom")

        self._patch_migrations(first, second)
        with self.assertRaises(RuntimeError):
            db.init_db()
        # The first is committed with its version; the second left nothing.
        self.assertEqual(_user_version(), 2)
        with db.connection() as conn:
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertIn("first_step", names)
        self.assertNotIn("second_step", names)

    def test_a_database_from_a_newer_build_stops_the_start(self):
        _set_user_version(len(db.MIGRATIONS) + 1)
        with self.assertRaises(SystemExit) as raised:
            db.init_db()
        message = str(raised.exception.code)
        self.assertIn(f"schema version {len(db.MIGRATIONS) + 1}", message)
        self.assertIn(f"up to {len(db.MIGRATIONS)}", message)
        # Nothing was run against it.
        with db.connection() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'tracks'").fetchone())
        self.assertEqual(_user_version(), len(db.MIGRATIONS) + 1)

    def test_the_every_start_maintenance_runs_on_every_start(self):
        with mock.patch.object(db, "_refresh_norms", wraps=db._refresh_norms) as refresh:
            db.init_db()
            db.init_db()
        self.assertEqual(refresh.call_count, 2)

    def test_a_link_less_basket_item_is_removed_at_every_start(self):
        # Replacing a device still leaves one behind, so this is not a
        # one-off cleanup.
        db.init_db()
        with db.connection() as conn:
            uid = conn.execute("INSERT INTO users (username) VALUES ('u')").lastrowid
            conn.execute("INSERT INTO basket_items (user_id, type, target) VALUES (?, 'album', 'x')", (uid,))
            conn.commit()
        db.init_db()
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0], 0)

    def test_an_android_device_becomes_a_phone_at_every_start(self):
        # Rows written as 'android' while the API still accepted it.
        db.init_db()
        with db.connection() as conn:
            uid = conn.execute("INSERT INTO users (username) VALUES ('u')").lastrowid
            conn.execute("INSERT INTO devices (owner_user_id, name, device_type, api_token_hash) "
                         "VALUES (?, 'd', 'android', 'h')", (uid,))
            conn.commit()
        db.init_db()
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT device_type FROM devices").fetchone()[0], "phone")

    def _orphan_device(self):
        """A device whose owner is gone: the kind of orphan a real database
        collects, written with foreign keys off as an old bug would have."""
        with db.connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO devices (owner_user_id, name, api_token_hash) "
                         "VALUES (999, 'orphan', 'h')")
            conn.commit()

    def test_an_orphan_the_database_already_had_does_not_stop_a_migration(self):
        db.init_db()
        self._orphan_device()
        ran = []
        self._patch_migrations(lambda conn: ran.append(2))
        _set_user_version(1)
        db.init_db()
        self.assertEqual(ran, [2])
        self.assertEqual(_user_version(), 2)

    def test_a_migration_that_orphans_rows_is_rolled_back(self):
        db.init_db()
        with db.connection() as conn:
            uid = conn.execute("INSERT INTO users (username) VALUES ('u')").lastrowid
            conn.execute("INSERT INTO devices (owner_user_id, name, api_token_hash) "
                         "VALUES (?, 'd', 'h')", (uid,))
            conn.commit()
        self._orphan_device()  # one old orphan, which is not this step's doing

        def orphaning(conn):
            conn.execute("DELETE FROM users")

        self._patch_migrations(orphaning)
        _set_user_version(1)
        with self.assertRaises(RuntimeError):
            db.init_db()
        self.assertEqual(_user_version(), 1)
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_a_migration_that_fails_after_dropping_a_table_keeps_the_table(self):
        # What a crash in the middle of a rebuild looks like: the old table is
        # already dropped when it stops.
        db.init_db()
        with db.connection() as conn:
            conn.execute("INSERT INTO users (username) VALUES ('kept')")
            conn.commit()

        def interrupted(conn):
            conn.execute("CREATE TABLE users_new (id INTEGER)")
            conn.execute("DROP TABLE users")
            raise RuntimeError("stopped mid-rebuild")

        self._patch_migrations(interrupted)
        _set_user_version(1)
        with self.assertRaises(RuntimeError):
            db.init_db()
        with db.connection() as conn:
            self.assertEqual([r["username"] for r in conn.execute("SELECT username FROM users")],
                             ["kept"])
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'users_new'").fetchone())

    def test_a_database_from_before_strict_tables_is_rebuilt_by_its_own_step(self):
        from test_db_strict import _LEGACY_TRACKS
        with db.connection() as conn:
            conn.executescript(_LEGACY_TRACKS)
            conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                         "VALUES ('a/b.flac', 'A', 'B', 'T', 1, 1.0)")
            conn.commit()
        db.init_db()
        self.assertEqual(_user_version(), len(db.MIGRATIONS))
        with db.connection() as conn:
            self.assertIn("STRICT", conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'tracks'").fetchone()[0].upper())
            self.assertEqual(conn.execute("SELECT relative_path FROM tracks").fetchone()[0],
                             "a/b.flac")

    def test_a_database_from_before_versioning_keeps_its_rows(self):
        # What every existing install looks like on its first start with
        # this build: the full schema, its data, and user_version 0.
        db.init_db()
        with db.connection() as conn:
            conn.execute("INSERT INTO users (username, is_admin) VALUES ('kept', 1)")
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
        db.init_db()
        self.assertEqual(_user_version(), len(db.MIGRATIONS))
        with db.connection() as conn:
            self.assertEqual(
                [r["username"] for r in conn.execute("SELECT username FROM users")], ["kept"])


def _shape(conn) -> dict:
    """Every table's column names and every index's name: what a migration
    must bring an older database to."""
    shape: dict = {}
    for row in conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"):
        if row["type"] == "table":
            shape[("table", row["name"])] = sorted(
                c["name"] for c in conn.execute(f"PRAGMA table_info({row['name']})"))
        elif row["type"] == "index":
            shape[("index", row["name"])] = None
    return shape


class UpgradeMatchesNewTests(unittest.TestCase):
    """A database from an earlier build, upgraded, has exactly what a new one
    has. Migration 1 runs only on a database below version 1, so a column
    added to SCHEMA or _MIGRATIONS alone reaches new databases and never an
    existing one; only a new numbered migration reaches both."""

    def test_a_version_4_database_upgrades_to_the_shape_of_a_new_one(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fresh, old = Path(tmp.name) / "fresh.db", Path(tmp.name) / "old.db"
        with mock.patch.object(db, "DATA_DIR", Path(tmp.name)), mock.patch.object(db, "DB_PATH", fresh):
            db.init_db()
            with db.connection() as conn:
                want = _shape(conn)
        fixture = (Path(__file__).parent / "testdata" / "schema-v4.sql").read_text()
        with mock.patch.object(db, "DATA_DIR", Path(tmp.name)), mock.patch.object(db, "DB_PATH", old):
            with db.connection() as conn:
                conn.executescript(fixture)
                conn.execute("PRAGMA user_version = 4")
                conn.commit()
            db.init_db()
            with db.connection() as conn:
                got = _shape(conn)
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        self.assertEqual({k: v for k, v in want.items() if got.get(k) != v},
                         {}, "a new database has these and an upgraded one does not")
        self.assertEqual(set(got) - set(want), set())


class SubsonicOwnersMigrationTests(unittest.TestCase):
    """A real version-4 database gains the Navidrome owner columns, and its
    users and playlists keep everything they had: an existing playlist is
    not private."""

    def test_a_version_4_database_gains_the_columns(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fixture = (Path(__file__).parent / "testdata" / "schema-v4.sql").read_text()
        with mock.patch.object(db, "DATA_DIR", Path(tmp.name)), \
                mock.patch.object(db, "DB_PATH", Path(tmp.name) / "old.db"):
            with db.connection() as conn:
                conn.executescript(fixture)
                conn.execute("PRAGMA user_version = 4")
                conn.execute("INSERT INTO users (username) VALUES ('alice')")
                conn.execute("INSERT INTO playlists (title, source_provider) VALUES ('Road Trip', 'subsonic')")
                conn.commit()
            db.init_db()
            with db.connection() as conn:
                user = conn.execute("SELECT username, subsonic_username FROM users "
                                    "WHERE username = 'alice'").fetchone()
                playlist = conn.execute("SELECT title, source_owner, source_private FROM playlists "
                                        "WHERE title = 'Road Trip'").fetchone()
        self.assertEqual(tuple(user), ("alice", None))
        self.assertEqual(tuple(playlist), ("Road Trip", None, 0))


class SetupCompletedForOldInstallsTests(unittest.TestCase):
    """An install older than the setup wizard, on a real version-4 database,
    and every install that must keep going through the wizard."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("DATA_DIR", Path(tmp.name)), ("DB_PATH", Path(tmp.name) / "t.db")):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _old_database(self, user=True, tracks=0, scans=(), flag=None):
        fixture = (Path(__file__).parent / "testdata" / "schema-v4.sql").read_text()
        with db.connection() as conn:
            conn.executescript(fixture)
            conn.execute("PRAGMA user_version = 4")
            if user:
                conn.execute("INSERT INTO users (username) VALUES ('u')")
            for i in range(tracks):
                conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                             "VALUES (?, 'A', 'B', 'T', 1, 0)", (f"{i}.flac",))
            for state in scans:
                conn.execute("INSERT INTO jobs (type, state) VALUES ('library_scan', ?)", (state,))
            if flag is not None:
                conn.execute("INSERT INTO app_config (key, value) VALUES ('setup_completed', ?)", (flag,))
            conn.commit()

    def _flag(self):
        db.init_db()
        with db.connection() as conn:
            return db.get_config(conn, "setup_completed")

    def test_a_user_and_a_finished_scan_set_it(self):
        self._old_database(scans=("done",))
        self.assertEqual(self._flag(), "1")

    def test_a_user_and_tracks_set_it_once_the_scan_job_has_been_pruned(self):
        self._old_database(tracks=2)
        self.assertEqual(self._flag(), "1")

    def test_a_scan_under_way_is_not_a_finished_one(self):
        for state in ("queued", "running"):
            with self.subTest(state=state):
                db.DB_PATH.unlink(missing_ok=True)
                self._old_database(tracks=2, scans=(state,))
                self.assertIsNone(self._flag())

    def test_a_finished_scan_counts_with_another_under_way(self):
        for state in ("queued", "running"):
            with self.subTest(state=state):
                db.DB_PATH.unlink(missing_ok=True)
                self._old_database(scans=("done", state))
                self.assertEqual(self._flag(), "1")

    def test_a_failed_scan_with_nothing_read_does_not_set_it(self):
        self._old_database(scans=("failed",))
        self.assertIsNone(self._flag())

    def test_a_user_alone_does_not_set_it(self):
        self._old_database()
        self.assertIsNone(self._flag())

    def test_a_library_with_no_user_does_not_set_it(self):
        self._old_database(user=False, tracks=2, scans=("done",))
        self.assertIsNone(self._flag())

    def test_deleted_tracks_are_no_library(self):
        self._old_database(tracks=1)
        with db.connection() as conn:
            conn.execute("UPDATE tracks SET deleted_at = datetime('now')")
            conn.commit()
        self.assertIsNone(self._flag())

    def test_a_fresh_install_does_not_get_it(self):
        self.assertIsNone(self._flag())
        with db.connection() as conn:
            self.assertEqual(db.get_config(conn, "playlist_sync_interval_hours"), "6")

    def test_an_install_with_the_flag_already_set_keeps_it(self):
        self._old_database(tracks=1, scans=("done",), flag="by hand")
        self.assertEqual(self._flag(), "by hand")

    def test_an_old_install_stays_without_a_schedule_it_never_chose(self):
        self._old_database(tracks=1)
        self._flag()
        with db.connection() as conn:
            self.assertEqual(db.get_config(conn, "playlist_sync_interval_hours"), "0")


if __name__ == "__main__":
    unittest.main()
