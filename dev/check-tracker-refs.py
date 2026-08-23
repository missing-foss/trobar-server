#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Fail on tracker references in published prose.

    dev/check-tracker-refs.py [paths...]

Public docs must stand on their own. An issue number that survives the repo
being recreated points at nothing, and one that resolves points somewhere the
reader cannot follow.

THE HARD PART IS NOT DETECTING, IT IS DISTINGUISHING. A guard that fires on hex
colours or on `#404` inside a fenced block gets switched off within a week, and
then it protects nothing. So:

  - fenced code blocks (``` and ~~~) are skipped entirely
  - inline code spans (`...`) are stripped before matching
  - hex colours (#0e8a16, #fff) never match: a tracker ref is #digits only
  - markdown heading anchors ([text](#some-heading)) never match: the target
    of a link is not a tracker ref
  - HTML comments are skipped
  - a bare # followed by 1-4 digits, as a word, IS a tracker ref
"""
import pathlib
import re
import sys

# Anchored on the # itself, so this catches BOTH bare `#123` and the
# qualified `trobar-server#446` / `tracker#12` forms. An earlier version required
# a non-word character before the #, which silently passed every qualified
# reference -- and those are the ones that survive a repo being recreated
# looking like they still resolve.
#
# Hex colours cannot match: #0e8a16 needs a word boundary after the digits and
# there is none before a letter. A URL fragment (example.test/#404) is excluded
# by the lookbehind.
REF = re.compile(r"(?<!/)#(\d{1,4})\b")

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`]*`")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# [label](#anchor) -- the link target is a heading, not an issue
MD_ANCHOR = re.compile(r"\]\(#[^)]*\)")


# --- source comments -------------------------------------------------------
# The rules above cover published prose. This narrower one covers code, and
# fails ONLY on pointer forms -- a "see"/"per"/"fixes" followed by a number --
# citations that send a reader somewhere the repository no longer has.
#
# Incidental mentions in comments are deliberately left alone. There are around
# 1800 of them, they are read by people working in the code rather than by
# users, and a sweep that size risks rewriting reasoning it does not follow.
POINTER = re.compile(
    r"(?<!/)\b(?:see|per|refs?|closes?|fixes?)\s+(?:[\w.-]+)?#\d{1,4}\b", re.I)

SOURCE_EXT = {".dart", ".kt", ".kts", ".java", ".py", ".sh", ".yml", ".yaml",
              ".gradle", ".mc", ".xml", ".toml", ".jsx"}

SOURCE_SKIP = (".git", "build", ".dart_tool", "node_modules", ".gradle")


def scan_source(root="."):
    """Pointer-form tracker references anywhere in source.

    Deliberately NOT gated on the line starting with a comment marker. That
    gate was the bug, and it was invisible because the guard still reported
    success: a citation written as a whole-line comment was caught, the
    identical text as a trailing comment was missed, and it was missed again
    on any continuation line of a docstring -- which is where most of them
    actually live. Proven with a control pair, same text, one caught and one
    passed; three real citations were shipping in this repo while the guard
    printed a clean result.

    (The examples above are described rather than written out. Spelling them
    literally would make this file fail its own check -- the recurring
    self-match problem, where the guard is the one file that legitimately
    talks about what it detects.)

    Dropping the gate only widens what is checked. Incidental mentions are
    still left alone, because POINTER requires a citation verb: a bare
    `repo#NN built the config flow` does not match, and that remains the
    decision -- around 1800 such mentions exist, they are read by people
    working in the code rather than by users, and a sweep that size risks
    rewriting reasoning it does not follow.
    """
    hits = []
    for f in sorted(pathlib.Path(root).rglob("*")):
        if not f.is_file() or f.suffix not in SOURCE_EXT:
            continue
        if any(p in f.parts for p in SOURCE_SKIP):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if POINTER.search(line):
                hits.append((f, n, line.strip()[:90]))
    return hits


def scan(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    text = HTML_COMMENT.sub("", text)
    hits, in_fence = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = MD_ANCHOR.sub("](#a)", line)
        stripped = INLINE_CODE.sub("``", stripped)
        for m in REF.finditer(stripped):
            hits.append((n, m.group(0), line.strip()[:90]))
    return hits


def main():
    defaults = ["docs", "README.md", "SECURITY.md", "CONTRIBUTING.md"]
    roots = [pathlib.Path(a) for a in (sys.argv[1:] or defaults)]
    files = []
    for r in roots:
        if r.is_dir():
            files += sorted(r.rglob("*.md"))
        elif r.is_file():
            files.append(r)
    bad = 0
    for f in files:
        for n, ref, ctx in scan(f):
            print(f"{f}:{n}: tracker reference {ref} -- {ctx}")
            bad += 1
    for f, n, ctx in scan_source():
        print(f"{f}:{n}: tracker pointer -- {ctx}")
        bad += 1
    if bad:
        print(f"\n{bad} tracker reference(s) in prose or source comments.")
        print("Public docs must stand alone: state the fact and drop the citation.")
        return 1
    # Say what was actually checked. The old message -- "no tracker references
    # in published prose" -- read as a clean bill of health for the whole tree,
    # which it never was and was never meant to be.
    print("no tracker citations found: prose checked in full, source checked "
          "for citation forms only")
    print("(incidental mentions in source comments are deliberately allowed "
          "-- see scan_source)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
