#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for LOG_LEVEL handling.

The property that actually matters is the SECURITY one: raising the level must
never turn on third-party request logging. urllib3 logs full request URLs at
DEBUG, and subsonic_client puts auth material in the query string, so a root
logger at DEBUG would write recoverable credentials to the log. main.py uses an
allowlist of its own loggers to make that impossible — and the allowlist has to
stay complete, which the first test here enforces.

    python3 -m unittest test_logging -v      # from app/
"""
import logging
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import db

# Same load-bearing preamble as test_routes.py: importing `main` writes a secret
# key into DATA_DIR, so it must point somewhere writable BEFORE that import.
_TMP = tempfile.mkdtemp(prefix="trobar-test-logging-")
os.environ["DATA_DIR"] = _TMP
db.DATA_DIR = Path(_TMP)
db.DB_PATH = Path(_TMP) / "music-sync.db"

import main  # noqa: E402 — must follow the DATA_DIR setup above


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class AllowlistCompletenessTests(unittest.TestCase):
    """main._APP_LOGGERS is hand-maintained, so this is what stops it rotting:
    a new module with a logger would otherwise silently ignore LOG_LEVEL, and
    nobody would notice until they needed its output to debug something."""

    def test_every_module_with_a_logger_is_in_the_allowlist(self):
        app_dir = Path(__file__).parent
        pattern = re.compile(r"^_log = logging\.getLogger\(__name__\)", re.M)
        with_loggers = {
            path.stem for path in app_dir.glob("*.py")
            if not path.stem.startswith("test_") and pattern.search(path.read_text())
        }
        missing = with_loggers - set(main._APP_LOGGERS)
        self.assertEqual(
            missing, set(),
            f"these modules define a logger but LOG_LEVEL won't reach them: {sorted(missing)} "
            f"— add them to main._APP_LOGGERS")

    def test_the_allowlist_has_no_stale_entries(self):
        # 'main' is legitimately there without the `_log =` idiom (it's Flask's
        # app.logger, named after this module), so it's excluded from the check.
        app_dir = Path(__file__).parent
        existing = {p.stem for p in app_dir.glob("*.py")}
        stale = {n for n in main._APP_LOGGERS if n != "main"} - existing
        self.assertEqual(stale, set(), f"allowlist names non-existent modules: {sorted(stale)}")


class _LoggingTestBase(unittest.TestCase):
    def setUp(self):
        # Snapshot and restore, so one test's level can't leak into another or
        # into the rest of the suite.
        self._saved = {name: logging.getLogger(name).level for name in main._APP_LOGGERS}
        self._saved_third_party = {
            n: logging.getLogger(n).level for n in ("urllib3", "urllib3.connectionpool", "waitress")
        }
        self._saved_root = logging.getLogger().level
        self._saved_env = os.environ.get("LOG_LEVEL")
        self.addCleanup(self._restore)

    def _restore(self):
        for name, lvl in {**self._saved, **self._saved_third_party}.items():
            logging.getLogger(name).setLevel(lvl)
        logging.getLogger().setLevel(self._saved_root)
        if self._saved_env is None:
            os.environ.pop("LOG_LEVEL", None)
        else:
            os.environ["LOG_LEVEL"] = self._saved_env

    def _configure(self, value=None):
        if value is None:
            os.environ.pop("LOG_LEVEL", None)
        else:
            os.environ["LOG_LEVEL"] = value
        main._configure_logging()


class LevelApplicationTests(_LoggingTestBase):
    def test_default_is_warning_so_current_behaviour_is_unchanged(self):
        self._configure(None)
        for name in main._APP_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.WARNING, name)

    def test_debug_reaches_our_loggers(self):
        self._configure("DEBUG")
        for name in main._APP_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.DEBUG, name)

    def test_level_is_case_insensitive_and_tolerates_whitespace(self):
        self._configure("  debug  ")
        self.assertEqual(logging.getLogger("scanner").level, logging.DEBUG)

    def test_an_unknown_level_falls_back_to_warning_without_raising(self):
        # A typo in an env var must not stop the server booting.
        self._configure("VERBOSE")
        self.assertEqual(logging.getLogger("scanner").level, logging.WARNING)

    def test_an_empty_level_falls_back_to_warning(self):
        self._configure("")
        self.assertEqual(logging.getLogger("scanner").level, logging.WARNING)


class ThirdPartyIsolationTests(_LoggingTestBase):
    """The security property. urllib3 logs full request URLs at DEBUG and
    subsonic_client puts `u`/`t`/`s` auth material in the query string, so a
    root logger at DEBUG would write recoverable credentials to the log."""

    def test_debug_does_not_lower_third_party_loggers(self):
        self._configure("DEBUG")
        for name in ("urllib3", "urllib3.connectionpool", "waitress"):
            self.assertNotEqual(logging.getLogger(name).level, logging.DEBUG, name)

    def test_debug_does_not_lower_the_root_logger(self):
        # Root staying at WARNING is what keeps every library we haven't
        # enumerated quiet by default.
        self._configure("DEBUG")
        self.assertEqual(logging.getLogger().level, logging.WARNING)

    def test_a_third_party_debug_record_is_not_emitted_while_ours_is(self):
        self._configure("DEBUG")
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.name + ":" + record.getMessage())

        handler = _Capture()
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)

        logging.getLogger("scanner").debug("ours-debug")
        logging.getLogger("urllib3.connectionpool").debug(
            'GET /rest/ping?u=alice&t=TOKEN&s=SALT')

        self.assertIn("scanner:ours-debug", records)
        self.assertFalse([r for r in records if "TOKEN" in r],
                         "a third-party DEBUG record leaked through")


if __name__ == "__main__":
    unittest.main()
