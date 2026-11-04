#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for settings.py, the auth settings read from the environment.

Every reader goes through `settings.X`, so a test that patches settings.X
reaches all of them. `from settings import X`, or a module binding one of these
names itself, would make a second copy that such a patch does not reach: a
test could then run half in one auth mode and half in another, and pass.
"""

import ast
import importlib
import os
import unittest
from pathlib import Path
from unittest import mock

import settings

APP_DIR = Path(__file__).resolve().parent
NAMES = ("ADMIN_USERNAME", "AUTH_MODE", "DEV_SERVER", "FORWARD_AUTH_SECRET", "EMERGENCY_PORT",
         "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_SCOPES", "OIDC_USERNAME_CLAIM", "OIDC_LOGOUT")


def _modules():
    for path in sorted(APP_DIR.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _from_settings_imports(tree):
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "settings"]


def _top_level_bindings(tree):
    """(name, line) for each of NAMES bound at a module's top level."""
    found = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found += [(t.id, node.lineno) for t in targets if isinstance(t, ast.Name) and t.id in NAMES]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found += [(a.asname or a.name, node.lineno) for a in node.names if (a.asname or a.name) in NAMES]
    return found


class OneBindingPerSettingTests(unittest.TestCase):
    def test_the_scan_sees_the_modules(self):
        # A glob that matched nothing would pass both tests below.
        names = {path.name for path, _ in _modules()}
        self.assertIn("main.py", names)
        self.assertIn("settings.py", names)

    def test_no_module_imports_names_from_settings(self):
        offenders = [f"{path.name}:{line}" for path, tree in _modules() for line in _from_settings_imports(tree)]
        self.assertEqual(offenders, [], "read settings as settings.X, never `from settings import`")

    def test_only_settings_binds_the_setting_names(self):
        offenders = [f"{path.name}:{line} {name}" for path, tree in _modules() if path.name != "settings.py"
                     for name, line in _top_level_bindings(tree)]
        self.assertEqual(offenders, [], "these names belong to settings.py alone")

    def test_settings_binds_all_of_them(self):
        tree = ast.parse((APP_DIR / "settings.py").read_text(encoding="utf-8"))
        self.assertEqual({name for name, _ in _top_level_bindings(tree)}, set(NAMES))

    def test_the_scan_catches_both_forms(self):
        # The two detectors, on code that has each problem.
        self.assertEqual(_from_settings_imports(ast.parse("from settings import AUTH_MODE\n")), [1])
        self.assertEqual(_top_level_bindings(ast.parse("x = 1\nAUTH_MODE = 'local'\n")), [("AUTH_MODE", 2)])


class EnvironmentTests(unittest.TestCase):
    """What settings.py makes of the environment, read at import."""

    def _load(self, **env):
        self.addCleanup(importlib.reload, settings)  # back to the real environment
        keys = ("ADMIN_USERNAME", "AUTH_MODE", "TROBAR_DEV_SERVER", "FORWARD_AUTH_SECRET", "EMERGENCY_PORT",
                "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_SCOPES", "OIDC_USERNAME_CLAIM",
                "OIDC_LOGOUT")
        clean = {k: v for k, v in os.environ.items() if k not in keys}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            return importlib.reload(settings)

    def test_defaults(self):
        s = self._load()
        self.assertEqual((s.ADMIN_USERNAME, s.AUTH_MODE, s.DEV_SERVER, s.FORWARD_AUTH_SECRET, s.EMERGENCY_PORT),
                         ("", "local", False, "", 0))

    def test_authentik_is_the_old_name_for_forward(self):
        self.assertEqual(self._load(AUTH_MODE="authentik").AUTH_MODE, "forward")
        self.assertEqual(self._load(AUTH_MODE="oidc").AUTH_MODE, "oidc")

    def test_the_dev_server_flag(self):
        for value, expected in (("1", True), ("true", True), ("YES", True), ("0", False), ("no", False)):
            with self.subTest(value=value):
                self.assertIs(self._load(TROBAR_DEV_SERVER=value).DEV_SERVER, expected)

    def test_the_emergency_port_is_a_number(self):
        self.assertEqual(self._load(EMERGENCY_PORT="9443").EMERGENCY_PORT, 9443)

    def test_oidc_defaults(self):
        s = self._load()
        self.assertEqual((s.OIDC_ISSUER, s.OIDC_CLIENT_ID, s.OIDC_CLIENT_SECRET, s.OIDC_SCOPES,
                          s.OIDC_USERNAME_CLAIM, s.OIDC_LOGOUT),
                         ("", "", "", "openid profile email", "preferred_username", False))

    def test_the_oidc_logout_flag(self):
        for value, expected in (("1", True), ("true", True), ("Yes", True), ("0", False), ("off", False)):
            with self.subTest(value=value):
                self.assertIs(self._load(OIDC_LOGOUT=value).OIDC_LOGOUT, expected)


if __name__ == "__main__":
    unittest.main()
