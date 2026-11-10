# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a mirror write overwrites on its target.

Mirrors are one-way: each write replaces the target's copy with Trobar's.
An edit someone made to the copy on the target is lost, and this module is
how the sync says so. It keeps each sink's copy of a playlist as Trobar
last left it (the baseline). Before the next write, the sink reads the copy
as it stands now; the difference from the baseline is what was edited on
the target, as opposed to what changed in Trobar's own playlist.

An edit is reported only when the write undoes it:
- a track added on the target that the write leaves out is *discarded*;
- a track removed on the target that the write puts back is *restored*.
An added track Trobar's playlist now also has, or a removed one it also
dropped, is not lost, and is not reported. Order is not compared.

With no baseline (the first write, or the first after this was added, or
a copy that was gone and is made afresh) nothing is reported.

Items are dicts {"id", "artist", "title"}: `id` is the target's own
identifier for the track, and artist/title are only for showing. Neither
function commits: they run inside the sink's write, like mirror_runs."""

import json
import logging

_log = logging.getLogger(__name__)


def _ids(items) -> set:
    return {str(i["id"]) for i in items}


def overwritten(conn, playlist_id: int, sink: str, current, written_ids) -> tuple[list, list]:
    """(discarded, restored) for a write of `written_ids` over the copy
    `current` (the items on the target now). Both empty without a
    baseline."""
    row = conn.execute("SELECT items FROM mirror_copies WHERE playlist_id = ? AND sink = ?",
                       (playlist_id, sink)).fetchone()
    if row is None or current is None:
        return [], []
    baseline = json.loads(row["items"])
    before, now, after = _ids(baseline), _ids(current), {str(i) for i in written_ids}
    discarded, seen = [], set()
    for item in current:
        key = str(item["id"])
        if key not in before and key not in after and key not in seen:
            discarded.append(item)
            seen.add(key)
    restored, seen = [], set()
    for item in baseline:
        key = str(item["id"])
        if key not in now and key in after and key not in seen:
            restored.append(item)
            seen.add(key)
    return discarded, restored


def remember(conn, playlist_id: int, sink: str, items) -> None:
    """Stores `items` as the copy Trobar has just left on the target."""
    conn.execute(
        "INSERT INTO mirror_copies (playlist_id, sink, items) VALUES (?, ?, ?) "
        "ON CONFLICT (playlist_id, sink) DO UPDATE SET items = excluded.items",
        (playlist_id, sink, json.dumps([{"id": str(i["id"]), "artist": i.get("artist") or "",
                                         "title": i.get("title") or ""} for i in items])))


def forget(conn, playlist_id: int, sink: str) -> None:
    """Drops the baseline: the copy is gone, or no longer Trobar's to compare."""
    conn.execute("DELETE FROM mirror_copies WHERE playlist_id = ? AND sink = ?", (playlist_id, sink))
