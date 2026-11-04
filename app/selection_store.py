# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Selections: what each user picked for which devices, and the device state
they add up to.

A selection names an album, an artist or a playlist (or an auto-fill set)
and the devices it goes to. recompute_device_state turns the selections
that reach a device into that device's device_track_state rows; everything
that adds, changes or removes a selection calls it, so the two live here
together. Split out of sync_state, which re-exports these names for now.

Core invariant (see the approved plan): device_track_state is a union over
all of a device's assigned selections, deduplicated by track_id — not by
selection. A track already `downloaded` and still required stays untouched
even if a second selection (e.g. a playlist sharing a track with an already-
synced album) also requires it. A track no longer required by *any* assigned
selection is marked `removed` so the device is told to delete it.

A 4th status, `excluded`, covers a track the user deliberately
deleted on-device and chose not to resync: recompute_device_state treats it
exactly like `pending`/`downloaded` — untouched while still required, so it's
never silently re-queued — but it still falls out of the required set the
normal way (-> `removed`) if a later selection change actually drops it.
"""
import logging
import sqlite3

from db import _new_id

_log = logging.getLogger(__name__)


def create_selection(conn: sqlite3.Connection, sel_type: str, target: str,
                      created_by_user_id: int, device_ids: list[int], *,
                      commit: bool = True) -> int:
    """`target` convention: artist name for type='artist'; 'Artist||Album' for
    type='album'; playlists.id (as str) for type='playlist'; tracks.id
    (as str) for type='track'.

    Find-or-create on (type, target, created_by_user_id) — a repeat call for
    a target the user already selected (double-click, retried request) joins
    device_ids onto the existing row instead of creating a duplicate
    (unguarded duplicate rows once broke the Selections matrix UI's
    `x-for :key="row.target"`). Matches the unique index in db.py and the
    same find-or-create shape toggle_selection_device already used.

    #351: `commit=False` lets a caller doing several of these as one
    all-or-nothing operation (the basket's fan-out) defer every commit to a
    single one of its own at the end — the default stays True so every other
    existing call site is unaffected."""
    existing = conn.execute(
        "SELECT id FROM selections WHERE type=? AND target=? AND created_by_user_id=?",
        (sel_type, target, created_by_user_id),
    ).fetchone()
    if existing is not None:
        selection_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO selections (type, target, created_by_user_id) VALUES (?, ?, ?)",
            (sel_type, target, created_by_user_id),
        )
        selection_id = _new_id(cur)
    for device_id in device_ids:
        conn.execute(
            "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
            (selection_id, device_id),
        )
    if commit:
        conn.commit()
    for device_id in device_ids:
        recompute_device_state(conn, device_id, commit=commit)
    return selection_id


def removal_count(conn: sqlite3.Connection, user_id: int, sel_type: str, target: str,
                  device_id: int) -> int:
    """How many tracks this user's `sel_type`/`target` selection holds on the
    device that nothing else selected for it still needs: what unticking it
    there deletes from the device at its next sync. 0 when the user has no
    such selection."""
    row = conn.execute(
        "SELECT id FROM selections WHERE type=? AND target=? AND created_by_user_id=?",
        (sel_type, target, user_id),
    ).fetchone()
    if row is None:
        return 0
    mine = _resolve_selection_track_ids(conn, sel_type, target, row["id"])
    others: set[int] = set()
    for other in conn.execute(
            "SELECT s.id, s.type, s.target FROM selections s JOIN selection_devices sd ON sd.selection_id = s.id "
            "WHERE sd.device_id = ? AND s.id != ?", (device_id, row["id"])):
        others |= _resolve_selection_track_ids(conn, other["type"], other["target"], other["id"])
    on_device = {r["track_id"] for r in conn.execute(
        "SELECT track_id FROM device_track_state WHERE device_id = ? AND status = 'downloaded'", (device_id,))}
    return len((mine - others) & on_device)


def selected_by_others(conn: sqlite3.Connection, user_id: int, sel_type: str, target: str,
                       device_id: int) -> list[str]:
    """Who else has `sel_type`/`target` selected for this device: the people
    whose selection keeps it there when this user unticks it."""
    return [r["username"] for r in conn.execute(
        "SELECT u.username FROM selections s JOIN selection_devices sd ON sd.selection_id = s.id "
        "JOIN users u ON u.id = s.created_by_user_id "
        "WHERE s.type = ? AND s.target = ? AND sd.device_id = ? AND s.created_by_user_id != ? "
        "ORDER BY u.username", (sel_type, target, device_id, user_id))]


def toggle_selection_device(conn: sqlite3.Connection, user_id: int, sel_type: str,
                             target: str, device_id: int, checked: bool) -> int | None:
    """Add/remove one device from a `sel_type` selection for `target`
    ("Artist||Album" for albums, an artist name, or a playlist id), creating
    the selection on first check and leaving it in place (just device-less)
    on a last uncheck rather than deleting it — used by the Selections
    matrix views (albums/artists/playlists), where every cell is independent
    of how the selection happened to get created. Returns the selection id,
    or None if there was nothing to do (unchecking when no selection exists)."""
    row = conn.execute(
        "SELECT id FROM selections WHERE type=? AND target=? AND created_by_user_id=?",
        (sel_type, target, user_id),
    ).fetchone()

    if row is None:
        if not checked:
            return None
        return create_selection(conn, sel_type, target, user_id, [device_id])

    selection_id = row["id"]
    if checked:
        conn.execute(
            "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
            (selection_id, device_id),
        )
    else:
        conn.execute(
            "DELETE FROM selection_devices WHERE selection_id=? AND device_id=?",
            (selection_id, device_id),
        )
    conn.commit()
    recompute_device_state(conn, device_id)
    return selection_id


def delete_selection(conn: sqlite3.Connection, selection_id: int) -> None:
    affected = [row["device_id"] for row in conn.execute(
        "SELECT device_id FROM selection_devices WHERE selection_id = ?", (selection_id,)
    )]
    conn.execute("DELETE FROM selections WHERE id = ?", (selection_id,))
    conn.commit()
    for device_id in affected:
        recompute_device_state(conn, device_id)


def parse_target_id(v: str | None) -> int | None:
    """#434: strict on purpose -- ids are always positive integers, so a
    strict parse loses nothing legitimate. Plain `int()` accepts things no
    client ever sends (leading/trailing whitespace, PEP-515 underscore
    separators like '1_0' -> 10, non-ASCII digits like the Arabic-Indic
    '١٠'), each of which disagreed with SQLite's `CAST(? AS INTEGER)` (which
    takes only a leading-integer prefix) at main.py's
    _require_playlist_visible() -- the gate and this reader parsing the same
    string differently meant the object authorized wasn't always the object
    served. `isascii()` matters alongside `isdigit()`: non-ASCII digit
    strings are `isdigit()`-true and `int()` parses them, so without the
    ASCII guard the strict version would still admit a form no client
    sends."""
    s = "" if v is None else str(v)
    return int(s) if s.isascii() and s.isdigit() else None


def _resolve_selection_track_ids(conn: sqlite3.Connection, sel_type: str, target: str,
                                 selection_id: int | None = None) -> set[int]:
    if sel_type == "autofit":
        # A materialized set frozen at refresh time — resolved by
        # selection id, not target, since the chosen tracks live in autofit_tracks.
        rows = conn.execute(
            "SELECT track_id AS id FROM autofit_tracks WHERE selection_id = ?", (selection_id,)
        )
        return {row["id"] for row in rows}
    if sel_type == "artist":
        # The artist's own tracks and every track of the albums it is the
        # album artist of: "Various Artists" is every compilation, as the
        # Library groups them.
        rows = conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND (artist = ? OR album_artist = ?)",
            (target, target),
        )
    elif sel_type == "album":
        # The artist is the album artist (the whole album, a compilation
        # included) or one of its track artists (that artist's tracks on it:
        # what a selection made before albums were grouped by album artist
        # is, and what "Also appears on" makes). One key form for both.
        artist, _, album = target.partition("||")
        rows = conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND album = ? "
            "AND (album_artist = ? OR artist = ?)",
            (album, artist, artist),
        )
    elif sel_type == "playlist":
        rows = conn.execute(
            "SELECT matched_track_id AS id FROM playlist_tracks "
            "WHERE playlist_id = ? AND matched_track_id IS NOT NULL",
            (int(target),),
        )
    elif sel_type == "track":
        rows = conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND id = ?", (int(target),)
        )
    else:
        return set()
    return {row["id"] for row in rows}


def recompute_playlist_devices(conn: sqlite3.Connection, playlist_id: int) -> int:
    """Recompute every device a selection of this playlist reaches, after its
    matched tracks changed: a track it gained is queued, one it lost is
    removed unless something else still needs it. Does not commit. Returns
    how many devices."""
    devices = [r["device_id"] for r in conn.execute(
        "SELECT DISTINCT sd.device_id FROM selections s "
        "JOIN selection_devices sd ON sd.selection_id = s.id "
        "WHERE s.type = 'playlist' AND s.target = ?", (str(playlist_id),))]
    for device_id in devices:
        recompute_device_state(conn, device_id, commit=False)
    return len(devices)


def required_track_ids_for_device(conn: sqlite3.Connection, device_id: int,
                                  exclude_autofit: bool = False) -> set[int]:
    required: set[int] = set()
    sql = (
        "SELECT s.id, s.type, s.target FROM selections s "
        "JOIN selection_devices sd ON sd.selection_id = s.id "
        "WHERE sd.device_id = ?"
    )
    if exclude_autofit:
        sql += " AND s.type != 'autofit'"
    for row in conn.execute(sql, (device_id,)):
        required |= _resolve_selection_track_ids(conn, row["type"], row["target"], row["id"])
    return required


def recompute_device_state(conn: sqlite3.Connection, device_id: int, *,
                            commit: bool = True) -> None:
    required = required_track_ids_for_device(conn, device_id)

    existing = {
        row["track_id"]: row["status"]
        for row in conn.execute(
            "SELECT track_id, status FROM device_track_state WHERE device_id = ?", (device_id,)
        )
    }

    for track_id in required:
        status = existing.get(track_id)
        if status is None:
            conn.execute(
                "INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, 'pending')",
                (device_id, track_id),
            )
        elif status == "removed":
            conn.execute(
                "UPDATE device_track_state SET status='pending', updated_at=datetime('now') "
                "WHERE device_id=? AND track_id=?",
                (device_id, track_id),
            )
        # status already 'pending', 'downloaded', or 'excluded' and still
        # required: untouched, never re-queued.

    # #63: a 'device'-sourced device is never told to delete a track just
    # because no selection currently requires it — the server can still ADD
    # (the required-tracks loop above is untouched) but won't prune, so the
    # device survives a server-DB loss. 'server' (the default) prunes as usual;
    # flipping back to 'server' re-marks the now-unrequired tracks 'removed' on
    # the next recompute. `excluded` and an already-`removed` track are left
    # alone either way.
    row = conn.execute(
        "SELECT source_of_truth FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    if row is None or row["source_of_truth"] != "device":
        for track_id, status in existing.items():
            if track_id not in required and status != "removed":
                conn.execute(
                    "UPDATE device_track_state SET status='removed', updated_at=datetime('now') "
                    "WHERE device_id=? AND track_id=?",
                    (device_id, track_id),
                )

    # A Play choice whose playlist is no longer selected for this device
    # goes back to Everything. The device-facing selection changes all end in
    # this recompute; a path that retargets a selection without one (the
    # mirror-copy carry in playlist_sync) leaves a playlist row that is then
    # deleted, and the column's ON DELETE SET NULL clears the choice there.
    conn.execute(
        "UPDATE devices SET play_playlist_id = NULL WHERE id = ? AND play_playlist_id IS NOT NULL "
        "AND play_playlist_id NOT IN (" + _DEVICE_PLAYLIST_IDS_SQL + ")",
        (device_id, device_id),
    )

    if commit:
        conn.commit()


def prune_preview_count(conn: sqlite3.Connection, device_id: int) -> int:
    """How many tracks the device holds that no selection requires: what
    switching Keep device content off (source_of_truth back to 'server')
    removes from it at its next sync, since recompute_device_state then marks
    them 'removed'. Only 'downloaded' counts: a pending or excluded track
    isn't on the device, so marking it removed deletes nothing there. Reads
    the same required set recompute_device_state prunes against, and writes
    nothing."""
    required = required_track_ids_for_device(conn, device_id)
    rows = conn.execute(
        "SELECT track_id FROM device_track_state WHERE device_id = ? AND status = 'downloaded'",
        (device_id,),
    )
    return sum(1 for row in rows if row["track_id"] not in required)


# The playlists selected for a device: the ids its Play choice may name.
_DEVICE_PLAYLIST_IDS_SQL = (
    "SELECT CAST(s.target AS INTEGER) FROM selections s "
    "JOIN selection_devices sd ON sd.selection_id = s.id "
    "WHERE s.type = 'playlist' AND sd.device_id = ?"
)
