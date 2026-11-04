# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Merge a vanished track into the new track its file became.

The scan matches a moved file by its size and tags (scanner._match_moves).
What it can't match, because two copies moved at once or the file changed as
it moved, becomes a vanished (soft-deleted) track and a new one. Once the
library's fingerprints are computed, the audio itself says they are the same:
this moves what pointed at the vanished track onto the new one.

- A device whose copy has the same device path under both (the file only
  moved, or changed in a way the device path doesn't show) keeps it: its row
  moves to the new track as downloaded, and the vanished track's "delete"
  never reaches it. A device whose path would differ still needs the new
  file, and is left as the scan left it.
- Playlist matches, single-track selections and auto-fit sets move to the
  new track.

Only an unambiguous pair is merged: exactly one vanished track still held
by a device, and exactly one live track, with that fingerprint, the live
one first seen no earlier than the vanished one went. The scan stamps a
vanished track with its own start time, so a file first seen in the scan
that removed the old one qualifies, and one already in the library before
that scan doesn't."""

import logging

import sync_state

_log = logging.getLogger(__name__)


def _pairs(conn) -> list:
    """(vanished row, live row) for each fingerprint shared by exactly one
    soft-deleted track a device still holds and exactly one live track."""
    rows = conn.execute(
        "SELECT t.* FROM tracks t WHERE t.fingerprint IS NOT NULL AND t.fingerprint IN ("
        "  SELECT g.fingerprint FROM tracks g WHERE g.deleted_at IS NOT NULL AND g.fingerprint IS NOT NULL"
        "  AND EXISTS (SELECT 1 FROM device_track_state s WHERE s.track_id = g.id))"
    ).fetchall()
    by_fp: dict = {}
    for r in rows:
        by_fp.setdefault(r["fingerprint"], []).append(r)
    pairs = []
    for group in by_fp.values():
        gone = [r for r in group if r["deleted_at"] is not None]
        live = [r for r in group if r["deleted_at"] is None]
        if len(gone) == 1 and len(live) == 1 and live[0]["scanned_at"] >= gone[0]["deleted_at"]:
            pairs.append((gone[0], live[0]))
    return pairs


def _move_device_rows(conn, old, new) -> None:
    for state in conn.execute(
            "SELECT s.device_id, s.status, s.bytes_on_device, s.delivered_path, d.transcode_format "
            "FROM device_track_state s JOIN devices d ON d.id = s.device_id WHERE s.track_id = ?",
            (old["id"],)).fetchall():
        fmt = state["transcode_format"]
        if sync_state.device_path(old, fmt) != sync_state.device_path(new, fmt):
            continue  # the device must get the new file under its new name
        # downloaded: the file is on the device under the name it would have
        # now. removed: told to delete it, not yet acked; but the scan's
        # recompute also turns a never-delivered pending row into removed, so
        # it counts as on the device only with a delivered_path, which every
        # delivery records (the ack, the manifest, provenance). Without one it
        # isn't carried, and the device at worst downloads it once more, never
        # the reverse. excluded: the owner chose not to have it, and still
        # doesn't. pending: never delivered, nothing to keep.
        delivered = state["status"] == "downloaded" or (
            state["status"] == "removed" and state["delivered_path"] is not None)
        status = "downloaded" if delivered else ("excluded" if state["status"] == "excluded" else None)
        if status is not None:
            conn.execute(
                "INSERT INTO device_track_state (device_id, track_id, status, bytes_on_device, delivered_path) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(device_id, track_id) DO UPDATE SET "
                "status = excluded.status, bytes_on_device = excluded.bytes_on_device, "
                "delivered_path = excluded.delivered_path, updated_at = datetime('now')",
                (state["device_id"], new["id"], status, state["bytes_on_device"], state["delivered_path"]))
        conn.execute("DELETE FROM device_track_state WHERE device_id = ? AND track_id = ?",
                     (state["device_id"], old["id"]))


def _move_references(conn, old_id: int, new_id: int) -> None:
    conn.execute("UPDATE playlist_tracks SET matched_track_id = ? WHERE matched_track_id = ?", (new_id, old_id))
    # A selection or auto-fit entry the new track already has wins; the
    # vanished track's duplicate folds into it.
    for sel in conn.execute(
            "SELECT id, created_by_user_id FROM selections WHERE type = 'track' AND target = ?",
            (str(old_id),)).fetchall():
        existing = conn.execute(
            "SELECT id FROM selections WHERE type = 'track' AND target = ? AND created_by_user_id = ?",
            (str(new_id), sel["created_by_user_id"])).fetchone()
        if existing is None:
            conn.execute("UPDATE selections SET target = ? WHERE id = ?", (str(new_id), sel["id"]))
        else:
            conn.execute("INSERT OR IGNORE INTO selection_devices (selection_id, device_id) "
                         "SELECT ?, device_id FROM selection_devices WHERE selection_id = ?",
                         (existing["id"], sel["id"]))
            conn.execute("DELETE FROM selections WHERE id = ?", (sel["id"],))
    conn.execute("UPDATE OR IGNORE autofit_tracks SET track_id = ? WHERE track_id = ?", (new_id, old_id))
    conn.execute("DELETE FROM autofit_tracks WHERE track_id = ?", (old_id,))
    # The same for a play: one the new track already has at that second wins.
    conn.execute("UPDATE OR IGNORE device_plays SET track_id = ? WHERE track_id = ?", (new_id, old_id))
    conn.execute("DELETE FROM device_plays WHERE track_id = ?", (old_id,))


def merge_moved_tracks(conn) -> int:
    """Merge every unambiguous vanished/live pair, recompute the devices if
    any was, and commit. Returns how many were merged."""
    pairs = _pairs(conn)
    for old, new in pairs:
        _move_device_rows(conn, old, new)
        _move_references(conn, old["id"], new["id"])
    if pairs:
        for device in conn.execute("SELECT id FROM devices").fetchall():
            sync_state.recompute_device_state(conn, device["id"], commit=False)
        _log.info("merged %d moved track(s) into the files they became", len(pairs))
    conn.commit()
    return len(pairs)
