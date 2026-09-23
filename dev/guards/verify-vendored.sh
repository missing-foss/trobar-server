#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Fails when a vendored guard no longer matches the version it claims.
#
# Run from a product repository's root, as one step of its dev/verify.sh:
#   dev/guards/verify-vendored.sh
#
# It reads dev/guards/VERSIONS -- one line per guard, "<file> <version>
# <sha256>" -- recomputes each file's hash, and compares. A guard edited in
# place, or left behind when the canonical copy moved on, is then a red check
# instead of a silent fork.
#
# WHAT THIS DOES NOT DO: it is drift detection, not tamper resistance. Anyone
# editing a guard can update VERSIONS in the same commit and this stays green.
# That is the intended strength -- the failure being designed against is a fix
# landing in one repository and not the others, which is what happened, not an
# attacker with commit access.
set -uo pipefail

versions="dev/guards/VERSIONS"
if [ ! -r "$versions" ]; then
  echo "GUARD vendored: FAIL -- $versions is missing or unreadable" >&2
  exit 1
fi

fail=0
checked=0
while read -r file version want; do
  case "$file" in ''|'#'*) continue ;; esac
  path="dev/guards/$file"
  if [ ! -r "$path" ]; then
    echo "GUARD vendored: FAIL -- $path is named in VERSIONS but missing" >&2
    fail=1; continue
  fi
  got=$(sha256sum "$path" | awk '{print $1}')
  if [ "$got" != "$want" ]; then
    echo "GUARD vendored: FAIL -- $file claims $version but its contents do not match" >&2
    echo "  expected $want" >&2
    echo "  found    $got" >&2
    echo "  Either re-vendor it from missing-foss/dev-guards, or if you changed" >&2
    echo "  it deliberately, land that change upstream first." >&2
    fail=1; continue
  fi
  echo "GUARD vendored: OK $file at $version"
  checked=$((checked + 1))
done < "$versions"

# A run that verified nothing must not report success. An empty or
# comment-only VERSIONS, a mangled file, or a guards directory that lost its
# contents would otherwise leave this printing nothing and exiting 0 -- which
# is indistinguishable from a clean check, and is the exact failure this
# mechanism exists to prevent, one level up.
if [ "$checked" -eq 0 ] && [ "$fail" -eq 0 ]; then
  echo "GUARD vendored: FAIL -- VERSIONS named no guards; nothing was verified" >&2
  exit 1
fi

if [ "$fail" -eq 0 ]; then
  echo "GUARD vendored: verified $checked guard(s) against $versions"
fi
exit "$fail"
