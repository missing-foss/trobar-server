#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# Pre-push verification gate for trobar-server. Run from the repo root:
#   dev/verify.sh
# CI (.github/workflows/ci.yml) runs all of this PLUS a pip lockfile drift
# guard, a docker build, and a Playwright end-to-end suite that boots a real
# server and drives it with a browser — so a green run here is necessary but
# not sufficient. Only the leak scan and the tracker-reference check are
# local-only.
#
# Run it before opening a PR to catch things without a round-trip. Every check
# here exists because something once slipped past without it.
set -uo pipefail
fail=0
step() { echo; echo "== $1 =="; }

step "python compile"
python3 -m py_compile app/*.py && echo ok || fail=1

step "unit tests"
# unittest discover (not a hardcoded module list): every test_*.py under
# app/ runs automatically now — a #252/#253-style gap (test_emby_client.py
# and test_plex_client.py both existed but were invisible to CI/this script
# until manually added to an explicit list, in three separate places, twice
# in a row) can't recur.
if command -v coverage >/dev/null 2>&1; then
  # PYTHONPATH=app, not `cd app`: coverage.py only auto-discovers a config
  # file (pyproject.toml's [tool.coverage.*]) in the current directory, it
  # doesn't walk up like mypy/pytest do — cd-ing in would silently pick up
  # no config. Report only, no enforced threshold: this exists to
  # make the untested-code gap visible in every run, not to gate on it.
  (PYTHONPATH=app coverage run -m unittest discover -s app -p 'test_*.py' -v \
    && coverage report) && echo ok || fail=1
else
  echo "SKIP coverage (not installed — pip install -r app/requirements-dev.txt), running plain unittest"
  (PYTHONPATH=app python3 -m unittest discover -s app -p 'test_*.py' -v) && echo ok || fail=1
fi

step "mypy (type checks)"
if command -v mypy >/dev/null 2>&1; then
  mypy && echo ok || fail=1
else
  echo "SKIP (mypy not installed — pip install -r app/requirements-dev.txt) — CI still runs it"
fi

step "inline JS checks (templates)"
# A rendered template can hold broken JS that only fails in the browser and
# blanks the whole page (syntax), or throws on a runtime path nothing else
# exercises (lint). Needs node on PATH; the lint pass also needs
# node_modules (npm install) — skips itself with a message otherwise.
python3 dev/check_inline_js.py || fail=1

step "tailwind CSS drift (#306)"
# app/static/css/tailwind.css is a committed build artifact. #285 added classes
# to a template without rebuilding it, so that control rendered UNSTYLED from
# the moment it merged — the template diff was correct, the classes were valid,
# and nothing else in this script or CI builds CSS. Catch it before push here;
# CI enforces the same thing (see the drift guard in ci.yml).
#
# Skips itself when the standalone binary isn't around, same as the mypy/babel
# steps above. TAILWIND_BIN overrides the lookup. Version matters: it must be
# the one ci.yml pins (see app/tailwind.config.js's header) or the rebuild
# differs from CI's and this reports drift that isn't there.
_tw="${TAILWIND_BIN:-}"
if [ -z "$_tw" ]; then
  for _c in "$HOME/tools/tailwind/tailwindcss" "$(command -v tailwindcss 2>/dev/null || true)"; do
    [ -n "$_c" ] && [ -x "$_c" ] && { _tw="$_c"; break; }
  done
fi
if [ -z "$_tw" ]; then
  echo "SKIP (no standalone tailwindcss found — set TAILWIND_BIN, see app/tailwind.config.js) — CI still runs it"
else
  _want=$(grep -m1 -oE 'USE v[0-9]+\.[0-9]+\.[0-9]+' app/tailwind.config.js | sed 's/USE v//')
  _have=$("$_tw" --help 2>&1 | grep -m1 -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | sed 's/^v//')
  if [ -n "$_want" ] && [ "$_want" != "$_have" ]; then
    echo "SKIP (tailwindcss $_have found, but the pinned version is $_want — a rebuild would differ from CI's)"
  else
    # Build from app/: the config's `content` glob is relative to the working
    # directory, so building from the repo root matches no templates and emits
    # a purged stylesheet — "drift" that's really this check misconfigured.
    # Output silenced (tailwind writes progress and a browserslist notice to
    # stderr); surfaced only if the build itself fails, since otherwise the
    # diff below is the real signal.
    if ! (cd app && "$_tw" -c tailwind.config.js -i tailwind-input.css \
            -o /tmp/trobar-fresh.css --minify) >/tmp/trobar-tw.log 2>&1; then
      echo "tailwindcss build failed:"; sed 's/^/  /' /tmp/trobar-tw.log; fail=1
    elif diff -q app/static/css/tailwind.css /tmp/trobar-fresh.css >/dev/null; then
      echo ok
    else
      echo "DRIFT: app/static/css/tailwind.css is out of date. Classes used in templates but not built:"
      grep -o '\.[a-zA-Z0-9\\:_-]*{' app/static/css/tailwind.css | sort -u > /tmp/trobar-committed.classes
      grep -o '\.[a-zA-Z0-9\\:_-]*{' /tmp/trobar-fresh.css | sort -u > /tmp/trobar-fresh.classes
      comm -13 /tmp/trobar-committed.classes /tmp/trobar-fresh.classes | head -20 | sed 's/^/  /'
      echo "Rebuild it — see the header comment in app/tailwind.config.js."
      fail=1
    fi
    rm -f /tmp/trobar-fresh.css /tmp/trobar-committed.classes /tmp/trobar-fresh.classes /tmp/trobar-tw.log
  fi
fi

step "translations (FR catalog complete, #187)"
# A new _() string missing from the FR .po silently renders as English in
# French mode — this fails on any untranslated/fuzzy entry or placeholder
# mismatch, so the gap is visible every run.
if python3 -c "import babel" 2>/dev/null; then
  python3 dev/check_translations.py && echo ok || fail=1
else
  echo "SKIP (babel not installed — pip install -r app/requirements.txt) — CI still runs it"
fi

step "leak scan (strings that must never ship)"
# Patterns come from two files (#313): dev/forbidden-terms.txt, committed and
# shared by every checkout, plus dev/forbidden-terms.local.txt, gitignored and
# optional, for terms that only make sense on one machine. Both are stripped of
# comments and blank lines before grep sees them — a `-f` list takes every line
# as a pattern, and an empty one matches every file in the repo.
# #404: `grep -f` on a missing terms file exits 2 (swallowed by 2>/dev/null
# below), the `if` is then false, and this printed "ok" having scanned
# nothing — fail-open, not fail-safe. `-s` catches missing AND empty in one
# test, skipping the grep entirely so this doesn't ALSO scan (and pass)
# against a pattern file with nothing in it.
# The pattern list is NOT in this repository. A denylist that ships the terms
# it exists to exclude publishes exactly what it is protecting -- which is what
# used to happen here. Supply one via LEAK_PATTERNS to run it; with no
# list configured this reports that it did not run rather than passing.
if [ -n "${LEAK_PATTERNS:-}" ] && [ -s "${LEAK_PATTERNS}" ]; then
  if git ls-files | xargs grep -InE -f "${LEAK_PATTERNS}" 2>/dev/null; then
    echo "LEAK: forbidden term(s) above"; fail=1
  else
    echo "ok"
  fi
else
  echo "SKIP (no LEAK_PATTERNS configured)"
fi

step "gitleaks (secrets)"
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks git --no-banner . && echo ok || fail=1
else
  echo "SKIP (gitleaks not installed) — CI still runs it"
fi

step "REUSE (per-file SPDX licensing, #122)"
if command -v reuse >/dev/null 2>&1; then
  reuse lint --quiet && echo ok || { reuse lint; fail=1; }
else
  echo "FAIL: reuse is not installed (pip install -r app/requirements-dev.txt) — the licensing check cannot run"; fail=1
fi

step "Tracker references in published prose"
# Public docs must stand alone: an issue number that outlives the tracker it
# points at is worse than no citation. Excludes fenced blocks, inline code, hex
# colours and heading anchors -- a guard that false-positives gets switched
# off, and then protects nothing.
if python3 dev/check-tracker-refs.py docs README.md SECURITY.md CONTRIBUTING.md; then
  echo ok
else
  fail=1
fi

echo
if [ "$fail" -eq 0 ]; then echo "VERIFY OK"; else echo "VERIFY FAILED"; fi
exit "$fail"
