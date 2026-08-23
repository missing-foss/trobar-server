#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#329: every fingerprint decode must run in a subprocess.

Why this file exists at all. pyacoustid's in-process ctypes+audioread path hands
the decoded buffer to native libchromaprint. When ffmpeg times out on a malformed
file, that buffer is partial, and libchromaprint fails a C++ assertion:

    Assertion 'length % m_num_channels == 0' failed

`assert()` calls `abort()`. That is SIGABRT — it kills the whole Python process,
and NO `try`/`except` anywhere in this codebase can catch it. A single FLAC
(Miles Davis, "Summertime") took production down six times on v2.4.0: crash,
`restart: unless-stopped`, boot reaper requeues the job, same track, crash again.

`force_fpcalc=True` moves the decode into the fpcalc subprocess, where an abort
kills only the child and pyacoustid raises an ordinary catchable exception.

The AST test below is the important one. Dropping the flag is a ONE-WORD change
that looks like a harmless performance win — the in-process path really is
faster — and its consequence is not a failing test but a server that dies on
certain files in production. So the guard is at the source level, and it covers
call sites nobody has written yet.

    python3 -m unittest test_fingerprint_isolation -v      # from app/
"""
import ast
import unittest
from pathlib import Path

import acoustid
import db
import provenance


def _fingerprint_file_calls():
    """Every acoustid.fingerprint_file(...) call in the app's own modules."""
    found = []
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.stem.startswith("test_"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "fingerprint_file":
                found.append((path.name, node.lineno, node))
    return found


class SubprocessIsolationTests(unittest.TestCase):
    def test_there_is_at_least_one_call_site_to_check(self):
        # Guards the guard: if the helper stopped finding calls (a rename, a
        # refactor to a wrapper), every assertion below would vacuously pass.
        # Two, both in provenance.py. It was three until #334 removed
        # fingerprint.py's decode fallback — that job is the AcoustID lookup and
        # no longer decodes anything, so all decoding now lives in one module.
        self.assertGreaterEqual(len(_fingerprint_file_calls()), 2)

    def test_every_call_site_forces_fpcalc(self):
        offenders = []
        for name, lineno, node in _fingerprint_file_calls():
            forced = any(
                kw.arg == "force_fpcalc"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords)
            if not forced:
                offenders.append(f"{name}:{lineno}")
        self.assertEqual(
            offenders, [],
            "these decode in-process, where a malformed file can abort() the "
            f"whole server (#329): {offenders} — pass force_fpcalc=True")

    def test_pyacoustid_still_supports_the_flag(self):
        # If an upgrade ever drops force_fpcalc, the calls above would raise
        # TypeError at runtime in a background job — i.e. silently, in the logs.
        import inspect
        params = inspect.signature(acoustid.fingerprint_file).parameters
        self.assertIn("force_fpcalc", params)


class NativeFailureIsRecordedTests(unittest.TestCase):
    """The behaviour subprocess isolation buys: a lethal file becomes a normal
    per-file failure instead of a process death."""

    def setUp(self):
        import shutil
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-fpiso-")
        self._saved = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                "VALUES ('Miles Davis/Porgy/Summertime.flac', 'Miles Davis', 'Porgy', "
                "'Summertime', 1, 0.0)")
            conn.commit()
            self.track_id = conn.execute(
                "SELECT id FROM tracks").fetchone()["id"]
        finally:
            conn.close()

    def _restore(self):
        import shutil
        db.DATA_DIR, db.DB_PATH = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_fingerprint_generation_error_is_recorded_not_raised(self):
        # What fpcalc dying (including on SIGABRT) looks like to Python.
        from unittest import mock
        err = acoustid.FingerprintGenerationError("fpcalc exited with status -6")
        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=err):
            ok = provenance._compute_one(Path("/music"), self.track_id,
                                         "Miles Davis/Porgy/Summertime.flac")
        self.assertFalse(ok)
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT fingerprint, fingerprint_failed_at FROM tracks WHERE id = ?",
                (self.track_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["fingerprint"])
        self.assertIsNotNone(row["fingerprint_failed_at"],
                             "a lethal file must be recorded so it stops being retried first")

    def test_the_pass_continues_past_a_lethal_file(self):
        # The whole point: one bad file must not stop the other 59,000.
        from unittest import mock
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                "VALUES ('Good/Album/fine.flac', 'A', 'B', 'C', 1, 0.0)")
            conn.commit()
        finally:
            conn.close()

        def _fp(path, force_fpcalc=False):
            if "Summertime" in path:
                raise acoustid.FingerprintGenerationError("fpcalc exited with status -6")
            return (180.0, b"AQAAGOOD")

        with mock.patch("provenance.acoustid.fingerprint_file", side_effect=_fp), \
             mock.patch.object(db, "get_music_root", return_value=Path("/music")):
            result = provenance.ensure_library_fingerprints()
        self.assertEqual(result["computed"], 1, "the healthy track must still be done")
        self.assertEqual(result["remaining"], 0,
                         "the lethal file must drop out of pending, not pin it")


class ProducerConsumerOrderTests(unittest.TestCase):
    """#334: the two fingerprint jobs had an ordering dependency that existed only
    in a release note — and the note was ignored within hours of being written.

    The lookup CONSUMES what the library pass PRODUCES. It used to decode the audio
    itself when a track had no fingerprint, so running it first silently did the
    producer's work inside a loop already paced by AcoustID (<=3/sec) and
    MusicBrainz (1/sec). Wasted time, not corruption — but unenforceable by
    instruction, so it is now structural."""

    def setUp(self):
        import shutil
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-order-")
        self._saved = (db.DATA_DIR, db.DB_PATH)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)
        self._shutil = shutil

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved
        self._shutil.rmtree(self._tmp, ignore_errors=True)

    def test_running_the_lookup_first_is_a_no_op_not_a_slow_duplicate(self):
        import fingerprint
        from unittest import mock
        conn = db.get_conn()
        try:
            db.set_config(conn, "acoustid_api_key", "k")
            conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, duration) "
                "VALUES ('a.flac', 'A', 'B', 'C', 1, 0.0, 180.0)")
            conn.commit()
        finally:
            conn.close()
        with mock.patch("fingerprint.acoustid.lookup") as lookup:
            result = fingerprint.resolve_pending_fingerprints()
        self.assertEqual(result, {"checked": 0, "resolved": 0})
        lookup.assert_not_called()

    def test_the_producer_queues_the_lookup_once_it_has_produced_something(self):
        # main.py wires this, so the wrapper is what must be registered — not
        # provenance.ensure_library_fingerprints bare.
        import main
        from unittest import mock
        handler = main.jobs._HANDLERS[main.provenance.JOB_TYPE_LIBRARY_FINGERPRINTS]
        with mock.patch.object(main.provenance, "ensure_library_fingerprints",
                               return_value={"checked": 5, "computed": 5, "remaining": 0}):
            handler({}, None)
        conn = db.get_conn()
        try:
            queued = [r["type"] for r in conn.execute(
                "SELECT type FROM jobs WHERE state = 'queued'")]
        finally:
            conn.close()
        self.assertIn(main.fingerprint.JOB_TYPE, queued,
                      "the producer must tell the consumer there is work")

    def test_it_does_not_queue_the_lookup_when_nothing_was_computed(self):
        # An empty pass means the library is already fingerprinted; the lookup was
        # queued alongside this job by the scanner anyway.
        import main
        from unittest import mock
        handler = main.jobs._HANDLERS[main.provenance.JOB_TYPE_LIBRARY_FINGERPRINTS]
        with mock.patch.object(main.provenance, "ensure_library_fingerprints",
                               return_value={"checked": 0, "computed": 0, "remaining": 0}):
            handler({}, None)
        conn = db.get_conn()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
