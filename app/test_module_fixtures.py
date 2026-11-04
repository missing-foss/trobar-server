# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A test module that removes its module-level temp dir in tearDownModule
must recreate it in setUpModule. unittest leaves and re-enters a module
when a selection interleaves modules (`test_routes.A test_maloja
test_routes.B`), and the dir is made only once, at import: without the
setUpModule, every test after the re-entry fails on a missing directory."""

import ast
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _mentions(node: ast.AST, text: str) -> bool:
    return text in ast.unparse(node)


class ModuleTempDirFixtureTests(unittest.TestCase):
    def test_a_module_that_removes_its_temp_dir_recreates_it_on_entry(self):
        checked = []
        for path in sorted(HERE.glob("test_*.py")):
            tree = ast.parse(path.read_text())
            teardown = _function(tree, "tearDownModule")
            if teardown is None or not _mentions(teardown, "rmtree(_TMP"):
                continue
            checked.append(path.name)
            setup = _function(tree, "setUpModule")
            with self.subTest(module=path.name):
                self.assertIsNotNone(setup, "tearDownModule removes _TMP but no setUpModule recreates it")
                assert setup is not None
                self.assertTrue(_mentions(setup, "makedirs(_TMP, exist_ok=True)"))
        # The check could otherwise pass by finding nothing to check.
        self.assertIn("test_routes.py", checked)
        self.assertGreater(len(checked), 10)


if __name__ == "__main__":
    unittest.main()
