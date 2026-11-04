#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""db.connection(): one connection per `with` block, closed either way,
rolled back when the block raises, and only explicit commits persist."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db


class ConnectionContextTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("DATA_DIR", Path(tmp.name)), ("DB_PATH", Path(tmp.name) / "t.db")):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with db.connection() as conn:
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.commit()

    def _values(self):
        with db.connection() as conn:
            return [r["v"] for r in conn.execute("SELECT v FROM t ORDER BY v")]

    def test_the_connection_is_closed_after_the_block(self):
        with db.connection() as conn:
            conn.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_a_commit_persists(self):
        with db.connection() as conn:
            conn.execute("INSERT INTO t VALUES ('kept')")
            conn.commit()
        self.assertEqual(self._values(), ["kept"])

    def test_an_uncommitted_write_does_not(self):
        with db.connection() as conn:
            conn.execute("INSERT INTO t VALUES ('dropped')")
        self.assertEqual(self._values(), [])

    def test_a_raising_block_is_rolled_back_closed_and_reraised(self):
        with self.assertRaises(ValueError):
            with db.connection() as conn:
                conn.execute("INSERT INTO t VALUES ('kept')")
                conn.commit()
                conn.execute("INSERT INTO t VALUES ('dropped')")
                raise ValueError("boom")
        self.assertEqual(self._values(), ["kept"])
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
