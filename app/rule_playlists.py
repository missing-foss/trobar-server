#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rule playlists: a playlist whose tracks come from a rule over the library
(year, format, when it was added, artist, album, duration), recomputed on
every playlist sync. Once written, it is an ordinary playlist row: devices
select it and mirrors copy it like any other.

A rule is JSON:

    {"match": "all" | "any",
     "conditions": [condition | group, ...],
     "sort": "added" | "artist" | "random",
     "seed": int,            # random only: the same seed, the same order
     "limit": int | null}

A condition is {"field", "op", "value"}; a group is {"match", "conditions"},
nested at most MAX_DEPTH deep. Only the fields and operators in _FIELDS are
accepted, and every value reaches SQL as a bound parameter, never as text.
"""

import hashlib
import json
import logging
import re
import uuid

import playlist_sync

_log = logging.getLogger(__name__)

RULE_SOURCE = "rule"
MAX_DEPTH = 2
MAX_CONDITIONS = 50
SORTS = ("added", "artist", "random")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EXT = re.compile(r"^[a-z0-9]{1,8}$")


class RuleError(ValueError):
    """A rule that can't be used, with what is wrong in its message."""


def _int(value, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise RuleError(f"{what} must be a whole number from {lo} to {hi}")
    return value


def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise RuleError(f"{what} must be text of 1 to 200 characters")
    return value.strip()


def _like(text: str) -> str:
    """`text` as a LIKE pattern matching it anywhere, its own % and _ literal."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _year(op, value):
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise RuleError("a year range needs two years")
        lo, hi = (_int(v, "year", 1000, 3000) for v in value)
        if lo > hi:
            raise RuleError("a year range must go from the earlier year to the later one")
        return "year BETWEEN ? AND ?", [lo, hi]
    sql = {"is": "year = ?", "from": "year >= ?", "until": "year <= ?"}.get(op)
    if sql is None:
        raise RuleError(f"year can't use '{op}'")
    return sql, [_int(value, "year", 1000, 3000)]


def _format(op, value):
    if op != "in" or not isinstance(value, list) or not value or len(value) > 20:
        raise RuleError("format takes 'in' and a list of 1 to 20 file types")
    exts = []
    for v in value:
        if not isinstance(v, str) or not _EXT.match(v.lower().lstrip(".")):
            raise RuleError(f"'{v}' isn't a file type")
        exts.append(v.lower().lstrip("."))
    return ("(" + " OR ".join(["lower(relative_path) LIKE ? ESCAPE '\\'"] * len(exts)) + ")",
            ["%." + e for e in exts])


def _lossless(op, value):
    if op != "is" or not isinstance(value, bool):
        raise RuleError("lossless takes 'is' and true or false")
    return "lossless = ?", [1 if value else 0]


def _added(op, value):
    if op == "since":
        if not isinstance(value, str) or not _DATE.match(value):
            raise RuleError("'added since' needs a date as YYYY-MM-DD")
        return "scanned_at >= ?", [value]
    if op == "within_days":
        return "scanned_at >= datetime('now', ?)", [f"-{_int(value, 'days', 1, 36500)} days"]
    raise RuleError(f"added can't use '{op}'")


def _text_field(column):
    def build(op, value):
        text = _text(value, column)
        if op == "contains":
            return f"{column} LIKE ? ESCAPE '\\'", [_like(text)]
        if op == "is":
            return f"{column} = ? COLLATE NOCASE", [text]
        raise RuleError(f"{column} can't use '{op}'")
    return build


def _duration(op, value):
    sql = {"at_least": "duration >= ?", "at_most": "duration <= ?"}.get(op)
    if sql is None:
        raise RuleError(f"duration can't use '{op}'")
    return sql, [_int(value, "seconds", 0, 86400)]


_FIELDS = {
    "year": _year,
    "format": _format,
    "lossless": _lossless,
    "added": _added,
    "artist": _text_field("artist"),
    "album": _text_field("album"),
    "duration": _duration,
}


def _group(node, depth: int, counter: list[int]) -> tuple[str, list]:
    if not isinstance(node, dict):
        raise RuleError("a rule and each of its groups must be an object")
    if depth > MAX_DEPTH:
        raise RuleError(f"groups can nest at most {MAX_DEPTH} deep")
    match = node.get("match")
    if match not in ("all", "any"):
        raise RuleError("match must be 'all' or 'any'")
    conditions = node.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise RuleError("a group needs at least one condition")
    parts, params = [], []
    for c in conditions:
        counter[0] += 1
        if counter[0] > MAX_CONDITIONS:
            raise RuleError(f"a rule can have at most {MAX_CONDITIONS} conditions")
        if isinstance(c, dict) and "conditions" in c:
            sql, p = _group(c, depth + 1, counter)
        else:
            if not isinstance(c, dict) or not isinstance(c.get("field"), str) or c["field"] not in _FIELDS:
                raise RuleError("each condition needs a known field")
            if not isinstance(c.get("op"), str):
                raise RuleError("each condition needs a test")
            sql, p = _FIELDS[c["field"]](c.get("op"), c.get("value"))
        parts.append(f"({sql})")
        params += p
    return (" AND " if match == "all" else " OR ").join(parts), params


def _clean(node: dict) -> dict:
    """A checked group with only the keys a rule has, at every level."""
    if "conditions" in node:
        return {"match": node["match"], "conditions": [_clean(c) for c in node["conditions"]]}
    return {"field": node["field"], "op": node.get("op"), "value": node.get("value")}


def validate(rule) -> dict:
    """The rule, normalised, or RuleError. What is stored is what this returns."""
    if not isinstance(rule, dict):
        raise RuleError("a rule must be an object")
    _group(rule, 1, [0])
    sort = rule.get("sort", "added")
    if sort not in SORTS:
        raise RuleError(f"sort must be one of {', '.join(SORTS)}")
    out = {**_clean(rule), "sort": sort}
    if sort == "random":
        out["seed"] = _int(rule.get("seed", 1), "seed", 0, 2**31 - 1)
    limit = rule.get("limit")
    if limit is not None:
        out["limit"] = _int(limit, "limit", 1, playlist_sync.TROBAR_TRACKS_MAX)
    return out


def _where(rule: dict) -> tuple[str, list]:
    sql, params = _group(rule, 1, [0])
    return f"deleted_at IS NULL AND ({sql})", params


def count(conn, rule: dict) -> int:
    """How many library tracks the rule matches, before any limit."""
    where, params = _where(rule)
    return conn.execute(f"SELECT COUNT(*) FROM tracks WHERE {where}", params).fetchone()[0]


def track_ids(conn, rule: dict) -> list[int]:
    """The matching tracks, in the rule's order, cut to its limit and to what
    a playlist can hold."""
    where, params = _where(rule)
    order = {
        "added": "scanned_at DESC, id",
        "artist": "artist COLLATE NOCASE, album COLLATE NOCASE, disc_no, track_no, id",
        "random": "id",
    }[rule["sort"]]
    ids = [r[0] for r in conn.execute(f"SELECT id FROM tracks WHERE {where} ORDER BY {order}", params)]
    if rule["sort"] == "random":
        # Seeded, so the order holds from one sync to the next; a track keeps
        # its place when others come and go.
        seed = str(rule["seed"]).encode()
        ids.sort(key=lambda i: hashlib.sha256(seed + b":" + str(i).encode()).digest())
    cap = min(rule.get("limit") or playlist_sync.TROBAR_TRACKS_MAX, playlist_sync.TROBAR_TRACKS_MAX)
    return ids[:cap]


def preview(conn, rule: dict) -> dict:
    """What the dialog's live count shows: how many tracks match, and how many
    of them the playlist would hold after its limit."""
    matches = count(conn, rule)
    cap = min(rule.get("limit") or playlist_sync.TROBAR_TRACKS_MAX, playlist_sync.TROBAR_TRACKS_MAX)
    return {"matches": matches, "tracks": min(matches, cap)}


def create(conn, user_id: int, title: str, rule: dict) -> int:
    """A new rule playlist owned by `user_id`, private, filled from `rule`,
    which validate() has returned. Commits."""
    cur = conn.execute(
        "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, rule, "
        "last_synced_at) VALUES (?, ?, ?, ?, 0, ?, datetime('now'))",
        (title, RULE_SOURCE, uuid.uuid4().hex, user_id, json.dumps(rule)))
    playlist_id = cur.lastrowid
    refresh(conn, playlist_id, json.dumps(rule))
    return playlist_id


def update(conn, playlist_id: int, title: str, rule: dict) -> int:
    """Replaces a rule playlist's title and rule and refills it; returns how
    many tracks it now holds. Commits."""
    conn.execute("UPDATE playlists SET title = ?, rule = ? WHERE id = ?",
                 (title, json.dumps(rule), playlist_id))
    return refresh(conn, playlist_id, json.dumps(rule))


def refresh(conn, playlist_id: int, rule_json: str) -> int:
    """Rewrites one rule playlist's tracks from its rule; returns how many.
    Devices and mirrors follow, as for any playlist edit."""
    rule = validate(json.loads(rule_json))
    return playlist_sync.set_trobar_playlist_tracks(conn, playlist_id, track_ids(conn, rule))


def refresh_all(conn) -> dict:
    """Every rule playlist, from its rule. A rule that no longer validates is
    left as it was and counted, never emptied."""
    done = failed = 0
    for row in conn.execute(
            "SELECT id, rule FROM playlists WHERE source_provider = ?", (RULE_SOURCE,)).fetchall():
        try:
            refresh(conn, row["id"], row["rule"])
            done += 1
        except (RuleError, ValueError, TypeError) as e:
            _log.warning("Rule playlist %s kept as it was: %s", row["id"], e)
            failed += 1
    return {"refreshed": done, "failed": failed}
