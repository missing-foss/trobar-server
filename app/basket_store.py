# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The basket: albums, artists and playlists a user has staged for devices
before sending them, and the devices each was last sent to.

Server-side so it survives a reload and follows the user across machines.
An item is staged for one or more devices (basket_item_devices); sending
the basket turns each item into a selection (selection_store). The last
destinations a user picked are remembered per user, as JSON, so the next
stage can default to them. Split out of sync_state, which re-exports these
names for now.
"""
import json
import logging
import sqlite3

from db import _new_id
from selection_store import parse_target_id

_log = logging.getLogger(__name__)


def list_basket(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """#413: enriched with a resolved display title -- and, for playlists,
    the source provider for an icon -- so the panel doesn't have to guess
    from the raw target string. That guess (`'||' in target`) is exactly how
    a basket'd playlist ended up rendering as a bare row id: target is
    String(p.id) for that type, and nothing else identifies it.

    #501: also enriched with `device_ids` -- the sorted list of devices
    this item is currently staged against -- so the basket panel can group
    items into per-device sections and api_basket_fan_out can filter each
    device's own section. Batched (one IN query), same reasoning as the
    playlist/track lookups below: a basket can hold several items, and
    each one needs this, not just the ones of one type.

    Playlist/track lookups are batched (one IN query each) rather than N+1,
    since a basket can hold several of either. `missing` is true only when
    a playlist/track target no longer resolves (deleted after being
    basketed) -- artist/album targets have no equivalent existence check,
    so they're never flagged missing here."""
    rows = conn.execute(
        "SELECT id, type, target, added_at FROM basket_items "
        "WHERE user_id = ? ORDER BY added_at, id", (user_id,),
    ).fetchall()

    item_ids = [r["id"] for r in rows]
    device_ids_by_item: dict[int, list[int]] = {item_id: [] for item_id in item_ids}
    if item_ids:
        placeholders = ",".join("?" * len(item_ids))
        for link in conn.execute(
            f"SELECT basket_item_id, device_id FROM basket_item_devices "
            f"WHERE basket_item_id IN ({placeholders}) ORDER BY device_id",
            tuple(item_ids),
        ):
            device_ids_by_item[link["basket_item_id"]].append(link["device_id"])

    # #424: a target that isn't numeric (a malformed POST /api/basket, or a
    # hand-edited DB -- #352 validates the type, not the target's format)
    # used to raise here and 500 the whole endpoint, taking the panel and
    # its own Clear button down with it -- the one unrecoverable-basket path
    # #414/#419 didn't already close. parse_target_id() skips it when building
    # the id sets; the per-row loop below then falls into the same
    # already-missing=true handling a deleted playlist/track gets.
    playlist_ids = {pid for r in rows if r["type"] == "playlist"
                     and (pid := parse_target_id(r["target"])) is not None}
    playlists: dict[int, sqlite3.Row] = {}
    if playlist_ids:
        placeholders = ",".join("?" * len(playlist_ids))
        for p in conn.execute(
            f"SELECT id, title, source_provider FROM playlists WHERE id IN ({placeholders})",
            tuple(playlist_ids),
        ):
            playlists[p["id"]] = p

    track_ids = {tid for r in rows if r["type"] == "track"
                 and (tid := parse_target_id(r["target"])) is not None}
    tracks: dict[int, sqlite3.Row] = {}
    if track_ids:
        placeholders = ",".join("?" * len(track_ids))
        for t in conn.execute(
            f"SELECT id, title FROM tracks WHERE id IN ({placeholders})",
            tuple(track_ids),
        ):
            tracks[t["id"]] = t

    items = []
    for r in rows:
        item = dict(r)
        item["device_ids"] = device_ids_by_item[r["id"]]
        item["source_provider"] = None
        item["missing"] = False
        if r["type"] == "artist":
            item["title"] = r["target"]
        elif r["type"] == "album":
            _artist, _, album = r["target"].partition("||")
            item["title"] = album
        elif r["type"] == "playlist":
            pid = parse_target_id(r["target"])
            p = playlists.get(pid) if pid is not None else None
            item["title"] = p["title"] if p else None
            item["source_provider"] = p["source_provider"] if p else None
            item["missing"] = p is None
        elif r["type"] == "track":
            tid = parse_target_id(r["target"])
            t = tracks.get(tid) if tid is not None else None
            item["title"] = t["title"] if t else None
            item["missing"] = t is None
        else:
            # Pre-#352 invalid type, or a hand-edited DB -- fall back to the
            # raw target rather than crashing on an unrecognized type.
            item["title"] = r["target"]
        items.append(item)
    return items


def add_basket_item(conn: sqlite3.Connection, user_id: int, item_type: str, target: str,
                     device_ids: list[int]) -> int:
    """Find-or-create on (user_id, type, target) — matches the basket_items
    unique index, so adding an item already in the basket (from the same
    surface, or a different one that shares it) joins `device_ids` onto the
    existing row instead of creating a duplicate — same find-or-create shape
    create_selection() already uses for `selections`/`selection_devices`.

    #501: `device_ids` is the destination(s) this item is staged against,
    chosen in the device picker at add time — required and non-empty (the
    caller validates that; see api_basket()'s own check, matching
    api_basket_fan_out's)."""
    existing = conn.execute(
        "SELECT id FROM basket_items WHERE user_id=? AND type=? AND target=?",
        (user_id, item_type, target),
    ).fetchone()
    if existing is not None:
        item_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO basket_items (user_id, type, target) VALUES (?, ?, ?)",
            (user_id, item_type, target),
        )
        item_id = _new_id(cur)
    for device_id in device_ids:
        conn.execute(
            "INSERT OR IGNORE INTO basket_item_devices (basket_item_id, device_id) VALUES (?, ?)",
            (item_id, device_id),
        )
    conn.commit()
    return item_id


def unstage_basket_item_device(conn: sqlite3.Connection, user_id: int, item_id: int,
                                device_id: int, *, commit: bool = True) -> None:
    """#501: removes one device from one item's staging — the per-device
    basket panel's own per-section × (unstage this item from just THIS
    device's section, leaving it staged for any others). Ownership-scoped
    via user_id in the same query, matching remove_basket_item's own style,
    rather than a separate existence check first.

    If that was the item's last remaining device link, the basket_items row
    itself is deleted too — see this table's own SCHEMA comment for why a
    device-less basket item can't be left lying around the way a
    device-less `selections` row deliberately is.

    `commit=False` lets api_basket_fan_out call this once per (item,
    device) pair it just sent, as part of its own single all-or-nothing
    transaction — same convention create_selection's own commit flag
    uses."""
    conn.execute(
        "DELETE FROM basket_item_devices WHERE device_id = ? AND basket_item_id IN "
        "(SELECT id FROM basket_items WHERE id = ? AND user_id = ?)",
        (device_id, item_id, user_id),
    )
    remaining = conn.execute(
        "SELECT 1 FROM basket_item_devices WHERE basket_item_id = ?", (item_id,)
    ).fetchone()
    if remaining is None:
        conn.execute("DELETE FROM basket_items WHERE id = ? AND user_id = ?", (item_id, user_id))
    if commit:
        conn.commit()


def prune_linkless_basket_items(conn: sqlite3.Connection, item_ids: list[int], *,
                                commit: bool = True) -> None:
    """Removes basket_items rows left with no device links by a cascade.

    basket_item_devices cascades on device_id, so DELETE FROM devices takes
    the link rows out with no application code in the path -- and an item
    whose only staged destination was that device is left behind with zero
    links. That state is the one this table's SCHEMA comment says never
    happens: the basket panel renders per device, so a link-less item is not
    merely stale but unreachable, impossible to see, act on or clear through
    the UI, and one more accumulates per delete.

    Takes the candidate ids rather than sweeping every link-less row, and
    the caller collects them BEFORE the cascade removes the links. A blanket
    "delete every item with no links" in a request path would also quietly
    clear rows orphaned by some other cause -- convenient, and it would hide
    the next producer of this state instead of surfacing it. The migration
    that sweeps globally is a one-off repair with no such caller to mask.

    `commit=False` keeps this inside the caller's own transaction, so the
    device and the items it orphaned go together or not at all -- same
    convention as prune_device_from_last_destinations."""
    for item_id in item_ids:
        remaining = conn.execute(
            "SELECT 1 FROM basket_item_devices WHERE basket_item_id = ?", (item_id,)
        ).fetchone()
        if remaining is None:
            conn.execute("DELETE FROM basket_items WHERE id = ?", (item_id,))
    if commit:
        conn.commit()


def remove_basket_item(conn: sqlite3.Connection, user_id: int, item_id: int) -> None:
    conn.execute("DELETE FROM basket_items WHERE id = ? AND user_id = ?", (item_id, user_id))
    conn.commit()


def clear_basket(conn: sqlite3.Connection, user_id: int, *, commit: bool = True) -> None:
    conn.execute("DELETE FROM basket_items WHERE user_id = ?", (user_id,))
    if commit:
        conn.commit()


# --- Remembered picker destinations ------------------------------
# users.basket_last_destinations maps a surface name to the device ids last
# chosen for a pick from it -- the device picker's smart-default. It is a
# JSON column, so a device row disappearing does NOT cascade into it the way
# selection_devices and device_track_state cascade. Every path that removes a
# device therefore has to say what happens to the memory of it, and the two
# paths want different answers: an outright delete prunes, a transfer remaps.
#
# These live here rather than in main.py because transfer_device() needs them
# inside its own transaction, and it cannot import main (main imports this).
def basket_last_destinations_dict(raw: str | None) -> dict:
    """Parses the users.basket_last_destinations JSON column, tolerating
    NULL/missing/malformed text (a fresh column, or a hand-edited DB) and
    dropping any entry that isn't a surface name mapped to a list of ints —
    same tolerate-garbage shape as _dashboard_widgets_dict."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result = {}
    for surface, device_ids in parsed.items():
        if isinstance(surface, str) and isinstance(device_ids, list) \
                and all(isinstance(d, int) for d in device_ids):
            result[surface] = device_ids
    return result


def _rewrite_last_destinations(conn: sqlite3.Connection, rewrite, *,
                               commit: bool = True) -> None:
    """Applies `rewrite` (a list of ids -> a list of ids) to every surface of
    every user's remembered destinations, writing back only the rows it
    actually changed.

    Every user, not just whoever triggered this: an admin or a delegate can
    have remembered a device they do not own, and would otherwise be left
    holding the stale id while the person who acted never sees a problem."""
    rows = conn.execute(
        "SELECT id, basket_last_destinations FROM users "
        "WHERE basket_last_destinations IS NOT NULL"
    ).fetchall()  # fetchall, not a live cursor: the UPDATE below writes the
                  # same table this is iterating.
    for row in rows:
        current = basket_last_destinations_dict(row["basket_last_destinations"])
        updated = {surface: rewrite(ids) for surface, ids in current.items()}
        if updated == current:
            continue
        conn.execute(
            "UPDATE users SET basket_last_destinations = ? WHERE id = ?",
            (json.dumps(updated), row["id"]),
        )
    if commit:
        conn.commit()


def prune_device_from_last_destinations(conn: sqlite3.Connection, device_id: int, *,
                                        commit: bool = True) -> None:
    """Drops a deleted device's id from every user's remembered destinations.

    Call with the DELETE that removes the device, in the same transaction. A
    leftover id is invisible in the picker, which only renders checkboxes for
    devices that still exist -- so it cannot be unchecked, while still being
    re-injected into every pick from that surface and 404ing the whole send.

    Surfaces whose list empties keep the key as an empty list: the picker
    already treats "remembered nothing" and "never used" identically, so
    there is nothing to gain from reasoning about which surface names are
    still live."""
    _rewrite_last_destinations(
        conn, lambda ids: [d for d in ids if d != device_id], commit=commit)


def remap_device_in_last_destinations(conn: sqlite3.Connection, old_device_id: int,
                                      new_device_id: int, *, commit: bool = True) -> None:
    """Points a transferred device's remembered destinations at its
    replacement, rather than forgetting them.

    Transfer is a swap, not a removal: transfer_device() deliberately MOVES
    selection_devices onto the new device instead of dropping them, and the
    remembered destination is the same kind of state -- "where picks from
    this surface go" is still true after the hardware behind it changed.
    Pruning here would be correct but lossy, silently resetting a
    smart-default the user never asked to lose.

    A user who remembered both devices collapses to one entry rather than
    ending up with the new id twice; order is otherwise preserved, since it
    is the order the checkboxes were ticked in."""
    def rewrite(ids: list[int]) -> list[int]:
        out: list[int] = []
        for d in ids:
            mapped = new_device_id if d == old_device_id else d
            if mapped not in out:
                out.append(mapped)
        return out

    _rewrite_last_destinations(conn, rewrite, commit=commit)
