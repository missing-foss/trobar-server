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
# The glob is counted rather than trusted: an unmatched app/*.py would compile
# nothing and exit 0, which is indistinguishable from compiling everything.
_py=(app/*.py)
if [ ! -e "${_py[0]}" ]; then
  echo "FAIL: no Python sources matched app/*.py -- nothing was compiled"; fail=1
elif python3 -m py_compile "${_py[@]}"; then
  echo "ok (${#_py[@]} files)"
else
  fail=1
fi

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
  _t=$( (PYTHONPATH=app coverage run -m unittest discover -s app -p 'test_*.py' -v \
    && coverage report) 2>&1 ); _rc=$?
else
  echo "SKIP coverage (not installed — pip install -r app/requirements-dev.txt), running plain unittest"
  _t=$( (PYTHONPATH=app python3 -m unittest discover -s app -p 'test_*.py' -v) 2>&1 ); _rc=$?
fi
printf '%s\n' "$_t"
# `discover` that matches no test files exits 0 and prints "Ran 0 tests" -- a
# green suite that ran nothing. The count is read from unittest's own summary
# rather than assumed.
_n=$(printf '%s' "$_t" | sed -n 's/^Ran \([0-9][0-9]*\) test.*/\1/p' | tail -1)
if [ "$_rc" -ne 0 ]; then
  fail=1
elif [ -z "$_n" ]; then
  echo "FAIL: the suite exited 0 but its test count could not be read -- output format changed?"; fail=1
elif [ "$_n" -eq 0 ]; then
  echo "FAIL: the suite ran 0 tests -- discovery matched nothing"; fail=1
else
  echo "ok ($_n tests)"
fi

step "mypy (type checks)"
if command -v mypy >/dev/null 2>&1; then
  _m=$(mypy 2>&1); _rc=$?
  printf '%s\n' "$_m"
  # mypy reports "no issues found in N source files"; N=0 is a pass over nothing.
  _n=$(printf '%s' "$_m" | sed -n 's/.*no issues found in \([0-9][0-9]*\) source file.*/\1/p' | tail -1)
  if [ "$_rc" -ne 0 ]; then fail=1
  elif [ -z "$_n" ]; then
    echo "FAIL: mypy exited 0 but its file count could not be read -- output format changed?"; fail=1
  elif [ "$_n" -eq 0 ]; then echo "FAIL: mypy checked 0 source files"; fail=1
  else echo "ok ($_n source files)"; fi
else
  echo "SKIP (mypy not installed — pip install -r app/requirements-dev.txt) — the gate runs it"
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
# CI's own drift guard enforces the same thing on every push and pull request,
# so a missed rebuild cannot reach main either way.
#
# Skips itself when the standalone binary isn't around, same as the mypy/babel
# steps above. TAILWIND_BIN overrides the lookup. Version matters: it must be
# the one CI's drift guard pins (see app/tailwind.config.js's header) or the
# rebuild differs from CI's and this reports drift that isn't there.
_tw="${TAILWIND_BIN:-}"
if [ -z "$_tw" ]; then
  for _c in "$HOME/tools/tailwind/tailwindcss" "$(command -v tailwindcss 2>/dev/null || true)"; do
    [ -n "$_c" ] && [ -x "$_c" ] && { _tw="$_c"; break; }
  done
fi
if [ -z "$_tw" ]; then
  echo "SKIP (no standalone tailwindcss found — set TAILWIND_BIN, see app/tailwind.config.js) — the gate runs it"
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
      # Names the generator actually used, not the one that was wanted: the
      # comparison is only meaningful against the pinned version, and a
      # mismatch is caught above, so saying which binary produced the
      # comparison is the part a reader cannot otherwise recover.
      echo "ok (no drift; rebuilt with tailwindcss $_have)"
    else
      echo "DRIFT: app/static/css/tailwind.css is out of date. Classes used in templates but not built:"
      # LC_ALL=C throughout: comm compares bytewise and warns "not in sorted
      # order" against a locale-collated sort, and its output is then not
      # trustworthy -- which matters here because this list is what someone
      # reads while fixing the drift.
      LC_ALL=C grep -o '\.[a-zA-Z0-9\\:_-]*{' app/static/css/tailwind.css | LC_ALL=C sort -u > /tmp/trobar-committed.classes
      LC_ALL=C grep -o '\.[a-zA-Z0-9\\:_-]*{' /tmp/trobar-fresh.css | LC_ALL=C sort -u > /tmp/trobar-fresh.classes
      LC_ALL=C comm -13 /tmp/trobar-committed.classes /tmp/trobar-fresh.classes | head -20 | sed 's/^/  /'
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
  # No `ok` of our own: check_translations.py already names each catalog, its
  # message count and that placeholders match, which is the evidence -- the
  # word "ok" underneath it added nothing and made the step look like the
  # bare-ok ones #31 is about.
  python3 dev/check_translations.py || fail=1
else
  echo "SKIP (babel not installed — pip install -r app/requirements.txt) — the gate runs it"
fi

step "leak scan (strings that must never ship)"
# #404: `grep -f` on a missing terms file exits 2 (swallowed by 2>/dev/null
# below), the `if` is then false, and this printed "ok" having scanned
# nothing — fail-open, not fail-safe. `-s` catches missing AND empty in one
# test, skipping the grep entirely so this doesn't ALSO scan (and pass)
# against a pattern file with nothing in it.
# The pattern list is NOT in this repository. A denylist that ships the terms
# it exists to exclude publishes exactly what it is protecting -- which is what
# used to happen here. Supply one via LEAK_PATTERNS to run it; with no
# list configured this reports that it did not run rather than passing.
# A `-f` list has no comment syntax and no blank-line syntax: grep takes every
# line in the file as a pattern, including the ones a human wrote as structure.
# So anything not meant as a pattern is stripped before grep sees the list. If
# stripping leaves nothing the list is malformed, and that fails loudly rather
# than scanning against an empty pattern set and reporting "ok". The count is
# printed on success so a list that quietly shrinks is visible.
# The filtered list is fed to grep through process substitution and never
# written to disk. A 0600 file removed on EXIT is still a copy of a list whose
# one handling rule is that it is not copied, and EXIT does not run on SIGKILL,
# on a full disk, or when a CI container is torn down mid-step. This removes
# the cleanup path rather than managing it.
# ONE grep invocation, deliberately. `git ls-files | xargs grep` may run grep
# several times: a temp file is re-opened by each, but a process substitution
# is a pipe and is drained by the first, so later batches would scan against an
# empty pattern set and report clean -- no error, no change in exit status. The
# file list is passed as arguments instead, in an array so paths containing
# spaces survive. Should that list ever outgrow ARG_MAX, this fails loudly with
# E2BIG rather than quietly under-scanning.
if [ -n "${LEAK_PATTERNS:-}" ] && [ -s "${LEAK_PATTERNS}" ]; then
  mapfile -t leak_files < <(git ls-files)
  leak_n=$(grep -vcE '^[[:space:]]*(#|$)' "${LEAK_PATTERNS}")
  if [ "${#leak_files[@]}" -eq 0 ]; then
    # grep with no file operands reads stdin: it would hang rather than scan,
    # and a scan of zero files reporting "ok" would be vacuous either way.
    echo "FAIL: no tracked files to scan"; fail=1
  elif [ "${leak_n:-0}" -eq 0 ]; then
    echo "FAIL: pattern list has no usable patterns (comments and blanks only)"; fail=1
  else
    # grep exits 0 on match, 1 on no match, and 2 on error. An `if` reads 1 and
    # 2 alike, so a tracked file missing from the working tree -- an ordinary
    # mid-edit state -- turned a found leak into "ok": the match was printed and
    # the step still passed. Branch on the status so an incomplete scan fails
    # loudly instead of reporting a clean tree it never finished reading.
    grep -InE -f <(grep -vE '^[[:space:]]*(#|$)' "${LEAK_PATTERNS}") -- "${leak_files[@]}"
    leak_rc=$?
    case "$leak_rc" in
      0) echo "LEAK: forbidden term(s) above"; fail=1 ;;
      1) echo "ok ($leak_n patterns)" ;;
      *) echo "FAIL: leak scan errored (grep exit $leak_rc) -- scanned set incomplete"; fail=1 ;;
    esac
  fi
else
  echo "SKIP (no LEAK_PATTERNS configured)"
fi

step "gitleaks (secrets)"
if command -v gitleaks >/dev/null 2>&1; then
  _g=$(gitleaks git --no-banner . 2>&1); _rc=$?
  printf '%s\n' "$_g"
  # gitleaks reports how many commits it scanned; zero is a pass over nothing.
  _n=$(printf '%s' "$_g" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) commits scanned.*/\1/p' | tail -1)
  if [ "$_rc" -ne 0 ]; then fail=1
  elif [ -z "$_n" ]; then
    echo "FAIL: gitleaks exited 0 but its commit count could not be read -- output format changed?"; fail=1
  elif [ "$_n" -eq 0 ]; then echo "FAIL: gitleaks scanned 0 commits"; fail=1
  else echo "ok ($_n commits scanned)"; fi
else
  echo "SKIP (gitleaks not installed) — the gate runs it"
fi

step "REUSE (per-file SPDX licensing, #122)"
if command -v reuse >/dev/null 2>&1; then
  _r=$(reuse lint 2>&1); _rc=$?
  # reuse reports "Files with copyright information: N / M". A lint over zero
  # files is a pass over nothing, and is reported as such.
  _n=$(printf '%s' "$_r" | sed -n 's/.*Files with copyright information: \([0-9][0-9]*\) \/ [0-9][0-9]*.*/\1/p' | tail -1)
  _d=$(printf '%s' "$_r" | sed -n 's/.*Files with copyright information: [0-9][0-9]* \/ \([0-9][0-9]*\).*/\1/p' | tail -1)
  if [ "$_rc" -ne 0 ]; then printf '%s\n' "$_r" | tail -20; fail=1
  elif [ -z "$_n" ]; then
    echo "FAIL: reuse exited 0 but its file count could not be read -- output format changed?"; fail=1
  elif [ "$_n" -eq 0 ]; then echo "FAIL: reuse checked 0 files"; fail=1
  else echo "ok ($_n/$_d files)"; fi
else
  echo "FAIL: reuse is not installed (pip install -r app/requirements-dev.txt) — the licensing check cannot run"; fail=1
fi

step "vendored guards match their declared versions"
# The guards below are copies of missing-foss/dev-guards. This recomputes each
# one's hash and compares it to the version this repository claims, so a guard
# edited in place -- or left behind when the canonical copy moved on -- is a red
# check rather than a silent fork. Six copies of one guard drifting, with
# nothing reporting it, is why the canonical repository exists.
dev/guards/verify-vendored.sh || fail=1

step "Tracker references in published prose"
# Public docs must stand alone: an issue number that outlives the tracker it
# points at is worse than no citation. Excludes fenced blocks, inline code, hex
# colours and heading anchors -- a guard that false-positives gets switched
# off, and then protects nothing.
# No `ok` line of our own here, deliberately. The guard prints its own
# summary -- which roots it scanned in full, and that source files were
# checked for citation forms only -- and that sentence is strictly better
# evidence than the word "ok" appended underneath it. #31 is about steps
# stating what they checked; a step whose tool already does that should get
# out of the way rather than add a token on top.
if ! python3 dev/guards/check-tracker-refs.py; then
  fail=1
fi

echo
if [ "$fail" -eq 0 ]; then echo "VERIFY OK"; else echo "VERIFY FAILED"; fi
exit "$fail"
