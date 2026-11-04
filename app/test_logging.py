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
import ast
import logging
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import db

# Same load-bearing preamble as test_routes.py: importing `main` writes a secret
# key into DATA_DIR, so it must point somewhere writable BEFORE that import.
_TMP = tempfile.mkdtemp(prefix="trobar-test-logging-")
os.environ["DATA_DIR"] = _TMP
db.DATA_DIR = Path(_TMP)
db.DB_PATH = Path(_TMP) / "music-sync.db"

import main  # noqa: E402 — must follow the DATA_DIR setup above
import settings  # noqa: E402

# The one print() allowed: `python3 -m scanner` writing its result.
_SCANNER_CLI_PRINT = next(
    i for i, line in enumerate((Path(__file__).parent / "scanner.py").read_text().splitlines(), 1)
    if line.strip() == "print(result, file=sys.stderr)")


def setUpModule():
    # unittest calls this every time it enters the module, and
    # tearDownModule every time it leaves; a selection that interleaves
    # modules (`test_routes.A test_maloja test_routes.B`) comes back after
    # the dir was removed. _TMP is made once, at import.
    os.makedirs(_TMP, exist_ok=True)


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

    def test_every_module_has_a_logger(self):
        # Without one, a module's messages go to print() or nowhere, and
        # LOG_LEVEL can't reach them (#258). 'main' uses logging.getLogger("main")
        # and Flask's app.logger instead (it runs as __main__ in the container).
        app_dir = Path(__file__).parent
        pattern = re.compile(r"^_log = logging\.getLogger\(__name__\)", re.M)
        without = {
            path.stem for path in app_dir.glob("*.py")
            if not path.stem.startswith("test_") and path.stem != "main"
            and not pattern.search(path.read_text())
        }
        self.assertEqual(without, set(), f"modules without `_log`: {sorted(without)}")

    def test_no_module_prints(self):
        # print() bypasses LOG_LEVEL and the log format. scanner's CLI entry
        # point printing its own result is output, not logging.
        app_dir = Path(__file__).parent
        printing = {
            f"{path.stem}:{i}" for path in app_dir.glob("*.py")
            if not path.stem.startswith("test_")
            for i, line in enumerate(path.read_text().splitlines(), 1)
            if re.match(r"\s*print\(", line)
        } - {f"scanner:{_SCANNER_CLI_PRINT}"}
        self.assertEqual(printing, set(), f"print() calls: {sorted(printing)}")

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


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class StartupWarningTests(_LoggingTestBase):
    """The startup security warnings were print()s, shown whatever LOG_LEVEL
    said. LOG_LEVEL=ERROR must not hide them. Captured on root's side, with
    no level touched: assertLogs would set the logger's level itself and so
    prove nothing about what LOG_LEVEL leaves visible."""

    def _warnings_with_log_level_error(self, **patches):
        self._configure("ERROR")
        capture = _Capture()
        root = logging.getLogger()
        root.addHandler(capture)
        self.addCleanup(root.removeHandler, capture)
        defaults: dict[str, object] = {"_music_root_is_readonly": lambda: True,
                    "_network_data_dir_warning": lambda: None,
                    "_forward_auth_warnings": lambda: [],
                    "EMERGENCY_PORT": None}
        merged = {**defaults, **patches}
        # The settings live in their own module; the rest are main's.
        on_settings = {k: v for k, v in merged.items() if k in ("AUTH_MODE", "EMERGENCY_PORT")}
        on_main = {k: v for k, v in merged.items() if k not in on_settings}
        with mock.patch.multiple(settings, **on_settings), mock.patch.multiple(main, **on_main):
            main._log_startup_warnings()
        return [r.getMessage() for r in capture.records if r.levelno >= logging.WARNING]

    def test_the_forward_auth_warning_survives_log_level_error(self):
        messages = self._warnings_with_log_level_error(
            _forward_auth_warnings=lambda: ["anyone can spoof X-authentik-username"])
        self.assertEqual(messages, ["anyone can spoof X-authentik-username"])

    def test_the_music_root_warning_survives_log_level_error(self):
        with mock.patch.object(db, "get_music_root", return_value=Path("/music")):
            messages = self._warnings_with_log_level_error(_music_root_is_readonly=lambda: False)
        self.assertEqual(len(messages), 1)
        self.assertIn("not mounted read-only", messages[0])

    def test_an_ignored_emergency_port_survives_log_level_error(self):
        messages = self._warnings_with_log_level_error(EMERGENCY_PORT=9443, AUTH_MODE="local")
        self.assertEqual(len(messages), 1)
        self.assertIn("EMERGENCY_PORT=9443", messages[0])

    def test_the_startup_logger_is_out_of_log_levels_reach(self):
        self.assertNotIn("trobar.startup", main._APP_LOGGERS)
        self.assertNotIn("trobar", main._APP_LOGGERS)


class StartupCallSiteTests(unittest.TestCase):
    """Nothing runs main.py's __main__ block under test, so pin statically
    that it calls _log_startup_warnings(): a dropped call would otherwise
    pass every other test and silently lose the warnings."""

    def test_the_main_block_calls_the_startup_warnings(self):
        tree = ast.parse((Path(__file__).parent / "main.py").read_text())
        blocks = [n for n in tree.body if isinstance(n, ast.If)
                  and isinstance(n.test, ast.Compare)
                  and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__"]
        self.assertEqual(len(blocks), 1)
        calls = [c for stmt in blocks[0].body for c in ast.walk(stmt)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "_log_startup_warnings"]
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
