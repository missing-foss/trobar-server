#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Check the inline <script> blocks in the HTML templates: syntax (node) and
lint (eslint).

A Jinja template that *renders* fine can still contain broken JavaScript —
and that JS only fails at runtime in the browser, where a single syntax
error stops Alpine from initialising and the whole page renders blank
(this happened once: an `async loadHealth() {` lost its parens and the UI
went blank while every server-side check stayed green). CI never caught it
because nothing checked the JS itself.

This extracts each inline <script>, replaces Jinja expressions with valid
JS stubs (so the *structure* is checkable without rendering), and:
- runs `node --check` on each block individually (syntax only — exit
  non-zero on the first failure);
- runs eslint (dev/eslint.inline-js.config.js) once per *template*, on all
  of that template's blocks concatenated in document order — a real page
  shares one global scope across its inline <script> tags (e.g. setup.html
  defines t() in one block and calls it from another), so linting them
  separately would flag real, correct cross-block references as undefined.

Requires `node` on PATH for the syntax check. The lint pass additionally
needs `npm install` run once (needs node_modules/.bin/eslint) — skipped
with a message if that hasn't happened, same as verify.sh does for
gitleaks; CI always has it.

Run from the repo root: `python dev/check_inline_js.py`.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TEMPLATES = Path("app/templates")
ESLINT_CONFIG = Path("dev/eslint.inline-js.config.js")
SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
JINJA_STMT = re.compile(r"\{%.*?%\}", re.S)


def stub_jinja(js: str) -> str:
    # A {{ }} expression becomes a value, a {% %} statement becomes nothing —
    # enough for the *structure* to be checkable without actually rendering.
    js = JINJA_EXPR.sub("null", js)
    js = JINJA_STMT.sub("", js)
    return js


def check_syntax(js: str, label: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js)
        path = fh.name
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"FAIL {label}\n{r.stderr.strip()}\n", file=sys.stderr)
        return False
    return True


def run_eslint(template_scripts: dict[str, str]) -> bool:
    eslint_bin = Path("node_modules/.bin/eslint")
    if not eslint_bin.exists():
        print("SKIP eslint (run `npm install` first) — CI always has it")
        return True
    # Under dev/, not the system temp dir: ESLint 10's flat config refuses to
    # lint anything outside the config file's own directory ("base path").
    with tempfile.TemporaryDirectory(dir="dev") as tmpdir:
        for tpl_name, combined_js in template_scripts.items():
            (Path(tmpdir) / f"{tpl_name}.js").write_text(combined_js, encoding="utf-8")
        r = subprocess.run(
            [str(eslint_bin), "--config", str(ESLINT_CONFIG), "--no-config-lookup", tmpdir],
            capture_output=True, text=True,
        )
    if r.returncode != 0:
        print(f"FAIL eslint\n{r.stdout.strip()}\n{r.stderr.strip()}\n", file=sys.stderr)
        return False
    return True


def main() -> int:
    if not TEMPLATES.is_dir():
        print(f"no {TEMPLATES} dir — run from the repo root", file=sys.stderr)
        return 2
    ok = True
    checked = 0
    template_scripts: dict[str, str] = {}
    for tpl in sorted(TEMPLATES.glob("*.html")):
        blocks = [stub_jinja(b) for b in SCRIPT_RE.findall(tpl.read_text(encoding="utf-8"))]
        blocks = [b for b in blocks if b.strip()]
        for i, block in enumerate(blocks):
            checked += 1
            if not check_syntax(block, f"{tpl.name} <script> #{i}"):
                ok = False
        if blocks:
            template_scripts[tpl.stem] = "\n".join(blocks)
    print(f"checked {checked} inline script block(s)")
    if not run_eslint(template_scripts):
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
