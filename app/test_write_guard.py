# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""db.write(), and that dev/write_guard.py can fire: a guard that can't fail
looks exactly like one that passed."""

import importlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import db

_ROOT = Path(__file__).resolve().parent.parent
_DEV = _ROOT / "dev"
# The same module the runner imported, never a second copy: a copy would wrap
# sqlite3.connect again and everything after it would go unseen by the runner.
if str(_DEV) not in sys.path:
    sys.path.insert(0, str(_DEV))
write_guard = importlib.import_module("write_guard")


class _DbCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-guard-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._saved = (db.DATA_DIR, db.DB_PATH)
        self.addCleanup(self._restore)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved

    def _value(self, key):
        with db.connection() as conn:
            return db.get_config(conn, key)


class WriteHelperTests(_DbCase):

    def test_commits_when_the_block_ends_and_closes_its_own_connection(self):
        with db.write() as conn:
            db.set_config(conn, "k", "v")
        self.assertEqual(self._value("k"), "v")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_rolls_back_and_closes_when_the_block_raises(self):
        with self.assertRaises(ValueError):
            with db.write() as conn:
                db.set_config(conn, "k", "v")
                raise ValueError("no")
        self.assertIsNone(self._value("k"))
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_leaves_a_connection_it_was_given_open(self):
        with db.connection() as conn:
            with db.write(conn):
                db.set_config(conn, "k", "v")
            self.assertFalse(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual(self._value("k"), "v")

    def test_takes_the_write_lock_on_entry(self):
        other = sqlite3.connect(db.DB_PATH, timeout=0)
        self.addCleanup(other.close)
        with db.write():
            with self.assertRaises(sqlite3.OperationalError):
                other.execute("INSERT INTO app_config (key, value) VALUES ('a', 'b')")

    def test_refuses_to_commit_someone_elses_open_transaction(self):
        with db.connection() as conn:
            db.set_config(conn, "k", "v")
            with self.assertRaises(RuntimeError):
                with db.write(conn):
                    pass
            self.assertTrue(conn.in_transaction, "the open transaction was touched")


_PLANTED = """
import tempfile, unittest
from pathlib import Path

import db


class Planted(unittest.TestCase):

    def test_offends(self):
        db.DATA_DIR = Path(tempfile.mkdtemp())
        db.DB_PATH = db.DATA_DIR / "music-sync.db"
        db.init_db()
        code = compile(
            "def late_offender():\\n"
            "    conn = db.get_conn()\\n"
            "    db.set_config(conn, 'k', 'v')\\n"
            "    os.listdir('.')\\n"
            "    conn.rollback()\\n"
            "    conn.close()\\n",
            APP + "/planted_offender.py", "exec")
        namespace = {"db": db, "os": __import__("os")}
        exec(code, namespace)
        namespace["late_offender"]()
"""


def _app_code(tmp, body: str):
    """`body` as code in a file under the guard's app folder, which the guard
    tests point at their temporary folder: coverage must not meet a source
    file that doesn't exist."""
    source = textwrap.dedent(body)
    path = Path(write_guard._APP) / "offender_for_guard_test.py"
    namespace = {"db": db, "time": time, "tmp": tmp, "subprocess": subprocess,
                 "sys": sys, "write_guard": write_guard}
    exec(compile(source, str(path), "exec"), namespace)
    return namespace["offender"]


class RunnerTests(unittest.TestCase):
    """The runner itself, in a process of its own."""

    def _run(self, *modules):
        tmp = tempfile.mkdtemp(prefix="trobar-test-runner-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "test_planted.py").write_text(f"APP = {str(_ROOT / 'app')!r}\n" + _PLANTED)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(_ROOT / "app"), tmp])}
        return subprocess.run(
            [sys.executable, str(_DEV / "run_unittests.py"), *modules],
            env=env, cwd=tmp, capture_output=True, text=True, timeout=60, check=False)

    def test_still_watching_after_the_guards_own_tests(self):
        # They must run first: their capture() is what once blinded the run.
        run = self._run("test_write_guard.GuardTests", "test_planted")
        self.assertIn("write guard: NEW planted_offender.py:late_offender file", run.stdout)
        self.assertEqual(run.returncode, 1)

    def test_fails_on_a_listed_offence_that_no_longer_happens(self):
        run = self._run("test_write_guard.WriteHelperTests")
        self.assertIn("write guard: FIXED", run.stdout)
        self.assertEqual(run.returncode, 1)


class GuardTests(_DbCase):

    def _offences(self, body):
        file = Path(self._tmp) / "track.flac"
        file.write_bytes(b"x")
        with mock.patch.object(write_guard, "_APP", self._tmp + "/"), write_guard.capture() as found:
            _app_code(file, body)()
        return found

    def test_fires_on_a_file_read_inside_a_transaction(self):
        found = self._offences("""
            def offender():
                conn = db.get_conn()
                db.set_config(conn, "k", "v")
                tmp.read_bytes()
                conn.commit()
                conn.close()
            """)
        self.assertEqual(found, {"offender_for_guard_test.py:offender file"})

    def test_fires_on_a_read_inside_db_write(self):
        found = self._offences("""
            def offender():
                with db.write() as conn:
                    tmp.read_bytes()
            """)
        self.assertEqual(found, {"offender_for_guard_test.py:offender file"})

    def test_fires_on_a_subprocess_and_a_sleep(self):
        found = self._offences("""
            def offender():
                with db.write():
                    subprocess.run([sys.executable, "-c", ""], check=True)
                    time.sleep(0)
            """)
        self.assertEqual(found, {"offender_for_guard_test.py:offender subprocess",
                                 "offender_for_guard_test.py:offender sleep"})

    def test_fires_on_a_network_connection(self):
        found = self._offences("""
            import socket
            def offender():
                with db.write():
                    s = socket.socket()
                    try:
                        s.settimeout(0.1)
                        s.connect_ex(("127.0.0.1", 9))
                    finally:
                        s.close()
            """)
        self.assertEqual(found, {"offender_for_guard_test.py:offender network"})

    def test_fires_on_a_second_writer_inside_a_transaction(self):
        found = self._offences("""
            def offender():
                with db.write():
                    other = db.get_conn()
                    other.execute("PRAGMA busy_timeout = 0")
                    try:
                        with db.write(other):
                            pass
                    except Exception:
                        pass
                    other.close()
            """)
        self.assertEqual(found, {"offender_for_guard_test.py:offender second-writer"})

    def test_quiet_on_a_read_inside_a_read_transaction(self):
        found = self._offences("""
            def offender():
                conn = db.get_conn()
                conn.execute("BEGIN")
                conn.execute("SELECT 1").fetchone()
                tmp.read_bytes()
                other = db.get_conn()
                other.close()
                conn.rollback()
                conn.close()
            """)
        self.assertEqual(found, set())

    def test_quiet_on_a_read_connection_inside_a_transaction(self):
        found = self._offences("""
            def offender():
                with db.write():
                    other = db.get_conn()
                    other.execute("SELECT 1").fetchone()
                    other.close()
            """)
        self.assertEqual(found, set())

    def test_fires_on_a_transaction_past_the_time_limit(self):
        with mock.patch.object(write_guard, "LIMIT_SECONDS", 0):
            found = self._offences("""
                def offender():
                    with db.write():
                        pass
                """)
        self.assertEqual(found, {"offender_for_guard_test.py:offender held-too-long"})

    def test_fires_on_the_492_shape(self):
        # The recording-id pass as it was: a write, then more files read with
        # the transaction still open.
        found = self._offences("""
            def offender():
                conn = db.get_conn()
                for done in range(3):
                    tmp.read_bytes()
                    db.set_config(conn, "k", str(done))
                    if done % 200 == 199:
                        conn.commit()
                conn.commit()
                conn.close()
            """)
        self.assertEqual(found, {"offender_for_guard_test.py:offender file"})

    def test_fires_on_every_shape_of_write(self):
        for name, statement in {
            "lowercase": 'conn.execute("update app_config set value = 1")',
            "leading newline": 'conn.execute("\\n  UPDATE app_config SET value = 1")',
            "executemany": 'conn.executemany("UPDATE app_config SET value = ?", [(1,)])',
            "executescript": 'conn.executescript("BEGIN IMMEDIATE; UPDATE app_config SET value = 1;")',
        }.items():
            with self.subTest(name):
                found = self._offences(f"""
                    def offender():
                        conn = db.get_conn()
                        {statement}
                        tmp.read_bytes()
                        conn.rollback()
                        conn.close()
                    """)
                self.assertEqual(found, {"offender_for_guard_test.py:offender file"})

    def test_quiet_on_a_read_after_a_commit(self):
        found = self._offences("""
            def offender():
                conn = db.get_conn()
                db.set_config(conn, "k", "v")
                conn.commit()
                conn.execute("BEGIN")
                conn.execute("SELECT 1").fetchone()
                tmp.read_bytes()
                conn.rollback()
                conn.close()
            """)
        self.assertEqual(found, set())

    def test_quiet_on_a_read_transaction_past_the_time_limit(self):
        with mock.patch.object(write_guard, "LIMIT_SECONDS", 0):
            found = self._offences("""
                def offender():
                    conn = db.get_conn()
                    conn.execute("BEGIN")
                    conn.execute("SELECT 1").fetchone()
                    conn.rollback()
                    conn.close()
                """)
        self.assertEqual(found, set())

    def test_quiet_when_the_read_comes_first(self):
        found = self._offences("""
            def offender():
                value = tmp.read_bytes()
                with db.write() as conn:
                    db.set_config(conn, "k", value.decode())
            """)
        self.assertEqual(found, set())

    def test_a_tests_own_connection_is_not_an_offence(self):
        file = Path(self._tmp) / "track.flac"
        file.write_bytes(b"x")
        conn = db.get_conn()
        self.addCleanup(conn.close)
        db.set_config(conn, "k", "v")
        with mock.patch.object(write_guard, "_APP", self._tmp + "/"), write_guard.capture() as found:
            _app_code(file, """
                def offender():
                    tmp.read_bytes()
                """)()
        self.assertEqual(found, set())
