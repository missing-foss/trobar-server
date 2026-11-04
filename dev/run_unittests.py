#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`python -m unittest` with dev/write_guard.py watching. Arguments are
unittest's. After the run it compares what the guard found with
dev/write-guard-known.txt and fails on an offence not listed there, and on a
listed one that no longer happens, so the list only ever shrinks."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import write_guard  # noqa: E402

KNOWN = Path(__file__).resolve().parent / "write-guard-known.txt"


def _known() -> set[str]:
    lines = (line.split("#")[0].strip() for line in KNOWN.read_text().splitlines())
    return {line for line in lines if line}


def main() -> int:
    write_guard.install()
    result = unittest.main(module=None, argv=["unittest", *sys.argv[1:]], exit=False).result
    found, known = write_guard.found(), _known()
    new, gone = sorted(found - known), sorted(known - found)
    for line in new:
        print(f"write guard: NEW {line}")
    for line in gone:
        print(f"write guard: FIXED, remove it from {KNOWN.name}: {line}")
    print(f"write guard: {len(found)} known offences, {len(new)} new, {len(gone)} fixed")
    return 1 if new or gone or not result.testsRun or not result.wasSuccessful() else 0


if __name__ == "__main__":
    sys.exit(main())
