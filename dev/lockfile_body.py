#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Print the lines of a pip-compile lockfile that a drift check compares:
every line that is neither blank nor a comment, with each requirement's
package name normalised the way pip reads it (PEP 503: lower case, any run
of "-", "_" and "." as one "-").

The CI lockfile drift guard diffs this output for the committed file and for
a fresh compile. Without the normalisation, Renovate's lockfile bumps fail
it: Renovate rewrites a bumped package with hashin, which spells the name as
PyPI does ("Werkzeug==3.1.9"), where pip-compile writes "werkzeug==3.1.9".
The two install the same thing. Versions and hashes are compared exactly.

    python3 dev/lockfile_body.py app/requirements.txt
"""
import re
import sys

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?=[\[=<>!~ ;]|$)")


def body(lines):
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _NAME.match(line)
        if m:
            line = re.sub(r"[-_.]+", "-", m.group(0)).lower() + line[m.end():]
        yield line


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        for out in body(f):
            print(out)
