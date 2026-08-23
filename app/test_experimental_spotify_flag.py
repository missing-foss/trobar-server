#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests that the Spotify experimental-features flag defaults to ON
for an existing install that already has Spotify credentials configured,
OFF otherwise -- decided ONCE at migration time (db._seed_spotify_
experimental_default), never re-evaluated live. Same in-memory-SQLite
harness as test_identity_schema.py -- no Flask, no DATA_DIR.

    python3 -m unittest test_experimental_spotify_flag -v      # from app/
"""
import sqlite3
import unittest

import db


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    return conn


class SpotifyExperimentalDefaultTests(unittest.TestCase):
    def test_fresh_install_defaults_off(self):
        conn = _make_conn()
        db._seed_spotify_experimental_default(conn)
        self.assertEqual(db.get_config(conn, "experimental_spotify_enabled"), "0")

    def test_existing_credentials_default_on(self):
        # The upgrade-safety case #398 was actually filed for: an install
        # that already has both Spotify credentials configured must not
        # come up disabled and silently drop already-linked users.
        conn = _make_conn()
        db.set_config(conn, "spotify_client_id", "cid")
        db.set_config(conn, "spotify_client_secret", "csec")
        db._seed_spotify_experimental_default(conn)
        self.assertEqual(db.get_config(conn, "experimental_spotify_enabled"), "1")

    def test_only_client_id_configured_still_defaults_off(self):
        conn = _make_conn()
        db.set_config(conn, "spotify_client_id", "cid")
        db._seed_spotify_experimental_default(conn)
        self.assertEqual(db.get_config(conn, "experimental_spotify_enabled"), "0")

    def test_only_client_secret_configured_still_defaults_off(self):
        conn = _make_conn()
        db.set_config(conn, "spotify_client_secret", "csec")
        db._seed_spotify_experimental_default(conn)
        self.assertEqual(db.get_config(conn, "experimental_spotify_enabled"), "0")

    def test_an_explicit_prior_value_is_never_overwritten(self):
        # The real upgrade path: seeding runs on every startup (init_db is
        # called on every process start), but an admin's own decision --
        # here, explicitly OFF despite credentials being present -- must
        # survive every subsequent call, not get silently re-derived.
        conn = _make_conn()
        db.set_config(conn, "spotify_client_id", "cid")
        db.set_config(conn, "spotify_client_secret", "csec")
        db.set_config(conn, "experimental_spotify_enabled", "0")
        db._seed_spotify_experimental_default(conn)
        self.assertEqual(db.get_config(conn, "experimental_spotify_enabled"), "0")

    def test_seeding_runs_cleanly_twice(self):
        conn = _make_conn()
        db._seed_spotify_experimental_default(conn)
        db._seed_spotify_experimental_default(conn)
        self.assertEqual(db.get_config(conn, "experimental_spotify_enabled"), "0")


if __name__ == "__main__":
    unittest.main()
