# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How much of each mirrored playlist reached its target, run by run.

A server sink (Subsonic, Jellyfin, Emby, Music Assistant, Plex) places the
tracks Trobar matched locally; the ones its target doesn't have are left
out of the copy. Each write attempt records here how many it tried, how
many ended up on the target, and which did not, with a reason:

- `not_on_target`: the sink found no item for the track on the target.
- `refused`: the target accepted the write, then its copy didn't hold the
  track. Only Music Assistant and Plex read their copy back, so only they
  can report it.

The filesystem mirror has no target and records nothing: its gaps are
local matching gaps, listed by the Unresolved view.

A run is recorded for every write that got as far as matching, including
one that found the copy already right and wrote nothing, so the numbers
stay current. A write that failed before or during the write records
nothing; its error is on the playlist row. Only the last KEEP runs per
(playlist, sink) are kept, and each lists at most LIST_MAX missing tracks.

`record` doesn't commit: it runs inside the caller's write, like the
sinks' own error columns."""

import json
import logging

_log = logging.getLogger(__name__)

KEEP = 5
LIST_MAX = 100

NOT_ON_TARGET = "not_on_target"
REFUSED = "refused"

SINKS = ("subsonic", "jellyfin", "emby", "music_assistant", "plex")


def record(conn, playlist_id: int, sink: str, total: int, on_target: int,
           lacked=(), refused=()) -> None:
    """One run: `total` tracks tried, `on_target` on the copy afterwards.
    `lacked` and `refused` are the tracks missing for each reason, as rows
    or dicts with "artist" and "title"; their full counts are stored, the
    list is cut at LIST_MAX (lacked first)."""
    lacked, refused = list(lacked), list(refused)
    missing = [{"artist": r["artist"], "title": r["title"], "reason": NOT_ON_TARGET}
               for r in lacked]
    missing += [{"artist": r["artist"], "title": r["title"], "reason": REFUSED}
                for r in refused]
    conn.execute(
        "INSERT INTO mirror_runs (playlist_id, sink, total, on_target, lacked, refused, missing) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (playlist_id, sink, total, on_target, len(lacked), len(refused),
         json.dumps(missing[:LIST_MAX])))
    conn.execute(
        "DELETE FROM mirror_runs WHERE playlist_id = ? AND sink = ? AND id NOT IN "
        "(SELECT id FROM mirror_runs WHERE playlist_id = ? AND sink = ? "
        " ORDER BY id DESC LIMIT ?)",
        (playlist_id, sink, playlist_id, sink, KEEP))


def latest(conn, playlist_ids=None) -> dict:
    """{(playlist_id, sink): {"total", "on_target", "lacked", "refused",
    "run_at", "history"}} for each pair's newest run, `history` being the
    kept runs' on_target counts, oldest first. Every pair, or those of
    `playlist_ids`."""
    sql = ("SELECT id, playlist_id, sink, run_at, total, on_target, lacked, refused "
           "FROM mirror_runs")
    args: tuple = ()
    if playlist_ids is not None:
        ids = [int(i) for i in playlist_ids]
        if not ids:
            return {}
        sql += f" WHERE playlist_id IN ({','.join('?' * len(ids))})"
        args = tuple(ids)
    out: dict = {}
    for r in conn.execute(sql + " ORDER BY id", args):
        key = (r["playlist_id"], r["sink"])
        history = out[key]["history"] if key in out else []
        history.append(r["on_target"])
        out[key] = {"total": r["total"], "on_target": r["on_target"], "lacked": r["lacked"],
                    "refused": r["refused"], "run_at": r["run_at"], "history": history}
    return out


def missing(conn, playlist_id: int, sink: str) -> dict | None:
    """The newest run of one pair with its missing tracks, or None when it
    has no run."""
    r = conn.execute(
        "SELECT run_at, total, on_target, lacked, refused, missing FROM mirror_runs "
        "WHERE playlist_id = ? AND sink = ? ORDER BY id DESC LIMIT 1",
        (playlist_id, sink)).fetchone()
    if r is None:
        return None
    return {"run_at": r["run_at"], "total": r["total"], "on_target": r["on_target"],
            "lacked": r["lacked"], "refused": r["refused"], "missing": json.loads(r["missing"])}
