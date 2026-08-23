#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#187: fail the build if a translation catalog is incomplete, stale, or its
placeholders don't match the English msgids — so a newly-added `_()` string
can't silently ship English-only. Same "visible every run" gate as reuse/gitleaks.

Four independent things are checked:

- **Completeness (#345)**: a fresh `pybabel extract` (to memory, never touching
  the committed .pot) is diffed against each catalog's msgids. This is the
  half the check is actually named for — a string can only be untranslated/
  fuzzy/mismatched if it *reached* the .po in the first place, and nothing
  else here (or in CI) ever re-derives that from source. #339 and #341 both
  shipped English fallback to French users past the old per-entry-only check,
  which validated the catalog against itself and had nothing external to
  compare it to.
- **Per-entry validation**: untranslated entries, fuzzy entries, per-message
  placeholder parity ({name}/{count}/%(x)s), and Babel's own format validation
  (catches %-format / brace-format breakage that would fail `pybabel compile`).
- **Duplicated msgstr (#467)**: a translation whose text is glued to itself
  twice with no separator — #465's data bug (two real instances, found only
  by a manual sweep) survived every check above, since a doubled string is
  still complete, non-fuzzy, and placeholder-clean *if it has no placeholder
  to begin with* (one that does is already caught incidentally, since
  `placeholders()` is count-sensitive and doubles every placeholder too).
  Checked as an exact-halves match (`s == s[:len(s)//2] * 2`, gated on
  `len(s) > 30` to leave short strings alone) rather than fuzzy/near-duplicate
  detection: both real cases were exact doublings, and a near-duplicate check
  would need a per-hit judgement call, which is how a gate starts getting
  ignored. Server-only by design (#467's own measurement): the other three
  clients' catalogue formats store one value per line, so the same
  corruption is a conspicuously long line, visible in any diff without
  tooling — only `.po`'s multi-line wrapping hides it.
- **Run-on words (#486)**: a lowercase letter directly followed by an
  uppercase one with nothing between them — `écoutésConfigurez` — the
  signature of an old translation and its replacement getting concatenated
  instead of one replacing the other (#464's tu→vous rewrite did this in two
  entries, invisible to every check above since the result is still
  complete/non-fuzzy/placeholder-clean text, just wrong). `BRAND_ALLOWLIST`
  masks out legitimately camel-cased product names first
  (`ListenBrainz`/`MusicBrainz`/`TheAudioDB`/`AcoustID`/`iTunes`/`YouTube`)
  so their own internal case transitions don't self-trigger this.
- **Duplicated span (#486)**: the same ~30-character run appearing twice
  anywhere in one `msgstr`, catalogue-internal so it needs no French
  knowledge. Catches the case the exact-halves check above and the run-on
  check both miss: two DIFFERENT rewordings of the same string glued
  together, where the join happens to land before a capital-plus-space
  rather than touching a lowercase-then-uppercase pair, and where the
  halves aren't identical text so `s == s[:len(s)//2] * 2` never matches.
  A 30-char threshold (`DUPLICATED_SPAN_WINDOW`) is long enough that a
  coincidental match in ordinary prose is effectively impossible, same
  reasoning as the exact-halves check's own 30-char gate.
- **.pot drift (#347)**: the committed `messages.pot` is a hand-updated
  artifact that nothing above ever opens — a
  stale entry in it, or one that no longer matches source, previously passed
  clean. Checked the same way as catalog completeness: msgid SETS compared
  against a fresh extract, not a raw file diff — a literal diff would be
  noisy on every commit regardless of content, since the .pot's own
  POT-Creation-Date header and its `#:` file/line comments shift on nearly
  every regeneration. This is a hard failure like the .po completeness
  check, since a .pot only exists to hand off to translators and a wrong one
  hands off the wrong work.

Every `app/translations/*/LC_MESSAGES/messages.po` is checked (not just fr)
so a second language can't end up silently unguarded the way the fr-only
path once was. Each catalog is also checked for the reverse drift — an entry
that's in the .po but no longer in source (a deleted `_()` call left behind)
— as a **warning**, not a failure: stale entries are dead weight and mildly
misleading translator work, not user-visible breakage, and a hard failure
here would break the build on any refactor that removes a string before
catalogs are regenerated to match.

#274: `pybabel extract` must run against `app/` as the base directory — from
the repo root it prefixes every `#:` path comment with `app/`, which is
irrelevant for a real .pot on disk but would corrupt the identity of nothing
here since only msgids (not comments) are compared. Still resolved as an
absolute path derived from this file's location, not the process cwd, so it
can't drift with how/where the script happens to be invoked.
"""
import re
import sys
from pathlib import Path

from babel.messages import frontend
from babel.messages.extract import extract_from_dir
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
CATALOGS = sorted(APP.glob("translations/*/LC_MESSAGES/messages.po"))
PLACEHOLDER = re.compile(r"\{[^}]+\}|%\([^)]+\)[sd]|%[sd]")

# #486: product names with a legitimate internal lower->upper transition —
# masked out before the run-on check below so e.g. TheAudioDB's own "oD"
# doesn't self-trigger it. Cross-checked against every brand name actually
# appearing in messages.pot's own msgids (grep -ohE '\b[A-Z][a-z]+[A-Z]
# [A-Za-z]*\b|iTunes' translations/messages.pot) rather than guessed —
# GitHub and WebP are both real, both caught this check on their own text
# once added here.
BRAND_ALLOWLIST = [
    "ListenBrainz", "MusicBrainz", "TheAudioDB", "AcoustID", "iTunes", "YouTube", "GitHub", "WebP",
]
DUPLICATED_SPAN_WINDOW = 30

# The source-language catalog's msgstr is intentionally blank throughout
# (gettext falls back to msgid) — that's not an "untranslated" defect, so it's
# exempt from that one per-entry check. It still gets the completeness/drift
# check like every other locale.
SOURCE_LOCALE = "en"


def placeholders(s):
    return sorted(PLACEHOLDER.findall(s or ""))


def find_runon(s):
    """#486: a lowercase letter immediately followed by an uppercase one,
    e.g. "écoutésConfigurez" — the signature of two strings concatenated
    with no separator. Brand names in BRAND_ALLOWLIST are masked out first
    (replaced with spaces, so positions/length stay stable for the
    surrounding-context slice) since several have their own internal
    lower->upper transition (TheAudioDB's "oD"). Returns the first hit's
    surrounding context (for a readable error), or None."""
    masked = s
    for brand in BRAND_ALLOWLIST:
        masked = masked.replace(brand, " " * len(brand))
    for i in range(len(masked) - 1):
        a, b = masked[i], masked[i + 1]
        if a.isalpha() and b.isalpha() and a.islower() and b.isupper():
            return s[max(0, i - 10):i + 11]
    return None


def find_duplicated_span(s, window=DUPLICATED_SPAN_WINDOW):
    """#486: the same `window`-character run appearing twice anywhere in
    `s` — catches two different reworkings of the same string glued
    together (not necessarily identical halves, which is what the
    exact-halves duplicate check above requires). Returns the repeated
    span, or None."""
    if len(s) < window * 2:
        return None
    seen: dict[str, int] = {}
    for i in range(len(s) - window + 1):
        span = s[i:i + window]
        first_seen = seen.get(span)
        if first_seen is not None and i - first_seen >= window:
            return span
        if first_seen is None:
            seen[span] = i
    return None


def singular(msgid):
    """Catalogs key entries by (msgid, msgid_plural) for plural forms; the
    singular is what `_()` is actually called with, so that's the identity
    used for completeness comparison."""
    return msgid[0] if isinstance(msgid, (list, tuple)) else msgid


def extract_source_msgids():
    with (APP / "babel.cfg").open() as fh:
        method_map, options_map = frontend.parse_mapping_cfg(fh)
    return {
        singular(message)
        for _filename, _lineno, message, _comments, _context in extract_from_dir(
            APP, method_map, options_map
        )
    }


def check_catalog(path, source_msgids, locale):
    with path.open("rb") as fh:
        cat = read_po(fh)

    problems = []
    catalog_msgids = set()
    for m in cat:
        if not m.id:  # skip the header
            continue
        catalog_msgids.add(singular(m.id))
        strings = list(m.string) if isinstance(m.string, (list, tuple)) else [m.string]
        if m.fuzzy:
            problems.append((m.id, "fuzzy (needs review/confirmation)"))
        elif not any(strings) and locale != SOURCE_LOCALE:
            problems.append((m.id, "untranslated"))
        else:
            ref = placeholders(m.id[-1] if isinstance(m.id, (list, tuple)) else m.id)
            for s in strings:
                if not s:
                    continue
                if placeholders(s) != ref:
                    problems.append((m.id, f"placeholder mismatch: {placeholders(s)} vs {ref}"))
                if len(s) > 30 and s == s[: len(s) // 2] * 2:
                    problems.append((m.id, "translation appears duplicated (msgstr is its own text twice)"))
                runon = find_runon(s)
                if runon is not None:
                    problems.append((m.id, f"run-on words, likely a botched concatenation: ...{runon}..."))
                span = find_duplicated_span(s)
                if span is not None:
                    problems.append((m.id, f"duplicated {DUPLICATED_SPAN_WINDOW}-char span: ...{span}..."))

    # Babel's own checkers (python-format / python-brace-format) — the same
    # validation `pybabel compile` runs, surfaced here as a build gate.
    for m, errors in cat.check():
        for err in errors:
            problems.append((m.id, f"format: {err}"))

    for missing in sorted(source_msgids - catalog_msgids):
        problems.append((missing, "in source but missing from catalog entirely"))

    # #347 part B: the reverse direction — an entry left behind after its
    # _() call was deleted from source. Dead weight and mildly misleading
    # translator work, not user-visible breakage, so this is returned
    # separately from `problems` and never fails the build on its own.
    warnings = [
        (obsolete, "in catalog but no longer in source (obsolete)")
        for obsolete in sorted(catalog_msgids - source_msgids)
    ]

    return problems, warnings, sum(1 for m in cat if m.id)


def check_pot_drift(source_msgids):
    """#347 part A: messages.pot is updated by hand ( job-cancel
    string) and nothing else here ever opens it — a stale or invented entry
    previously passed clean. Compared the same way as catalog completeness
    (msgid sets against a fresh extract), not a raw file diff: the .pot's
    own POT-Creation-Date header and #: file/line comments shift on nearly
    every regeneration, so a literal diff would be noisy on every commit
    regardless of whether the actual msgids drifted. Read-only — never
    regenerates or overwrites the committed file."""
    pot_path = APP / "translations/messages.pot"
    with pot_path.open("rb") as fh:
        pot = read_po(fh)
    pot_msgids = {singular(m.id) for m in pot if m.id}

    problems = []
    for missing in sorted(source_msgids - pot_msgids):
        problems.append((missing, "in source but missing from messages.pot"))
    for stale in sorted(pot_msgids - source_msgids):
        problems.append((stale, "in messages.pot but no longer in source"))
    return problems


def main():
    if not CATALOGS:
        print(f"No catalogs found under {APP / 'translations'}")
        return 1

    source_msgids = extract_source_msgids()

    fail = False

    pot_problems = check_pot_drift(source_msgids)
    if pot_problems:
        fail = True
        print(f"messages.pot: {len(pot_problems)} problem(s) — regenerate it (pybabel extract):")
        for mid, why in pot_problems:
            print(f"  [{why}] {mid!r}"[:160])
    else:
        print("messages.pot OK — matches source.")

    for path in CATALOGS:
        locale = path.relative_to(APP / "translations").parts[0]
        problems, warnings, total = check_catalog(path, source_msgids, locale)
        if problems:
            fail = True
            print(f"{locale} catalog: {len(problems)} problem(s) — fill/fix these:")
            for mid, why in problems:
                print(f"  [{why}] {mid!r}"[:160])
        else:
            print(f"{locale} catalog OK — {total} messages, all translated, placeholders match.")
        if warnings:
            print(f"{locale} catalog: {len(warnings)} obsolete entrie(s) — safe to remove:")
            for mid, why in warnings:
                print(f"  [{why}] {mid!r}"[:160])

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
