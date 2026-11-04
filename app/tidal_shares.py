#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tidal playlist shares: a copy of one member's Tidal playlist kept in
another member's own Tidal account.

A share is a row in `tidal_shares`. Once active, every playlist sync pass
brings the copy up to date: one-way, the copy is rewritten to equal the
owner's playlist; two-way, both sides are merged against the track list
as it stood after the previous pass (the snapshot). Removals win, both
sides' additions are kept, and the order follows the owner's playlist with
the recipient's additions appended in their order.

Rules this module never bends:

* It writes only to the copy whose id it recorded when it created it, and
  to the owner's playlist when two-way is on. A same-named playlist is
  never touched; names are not used to find anything.
* Every write is read back. A copy that doesn't come back as intended is
  an error on the share (`last_error`) and a warning in the log, and the
  snapshot is left as it was so the next pass starts from known ground.
* A track Tidal refuses on one side (not available in that account's
  region) is recorded for that side, reported, and counted as present when
  merging, so its absence never reads as a removal.

Tracks are compared by Tidal track id: both accounts use the same catalogue,
so nothing is resolved against the local library.
"""

import json
import logging
from collections import Counter
from collections.abc import Sequence

import db
import tidal_client

_log = logging.getLogger(__name__)

# Applying edits is planned, written and read back; a second round fixes
# the order when a track refused earlier is accepted at the end. More than
# that means the copy is changing under us, which is an error to report.
_APPLY_ROUNDS = 2


def merge_two_way(base: list[str], owner: list[str], recipient: list[str],
                  owner_unavailable: Sequence[str] = (), recipient_unavailable: Sequence[str] = ()) -> list[str]:
    """The merged track list for a two-way share.

    Per track, counting occurrences: if either side has fewer than the
    snapshot, it was removed there, and the smaller count wins; otherwise
    the larger count wins, so an addition on either side is kept and the
    same track added on both sides appears once. A side's unavailable
    tracks count as present on that side."""
    b = Counter(base)
    o = Counter(owner) + Counter(owner_unavailable)
    r = Counter(recipient) + Counter(recipient_unavailable)
    final = {}
    for t in set(b) | set(o) | set(r):
        if o[t] < b[t] or r[t] < b[t]:
            final[t] = min(o[t], r[t])
        else:
            final[t] = max(o[t], r[t])
    out: list[str] = []
    used: Counter = Counter()
    # Owner order first, then the recipient's additions in their order, then
    # anything only an unavailable list still holds (kept where it was).
    for seq in (owner, recipient, list(base) + list(owner_unavailable) + list(recipient_unavailable)):
        for t in seq:
            if used[t] < final.get(t, 0):
                out.append(t)
                used[t] += 1
    return out


def plan_edits(current: list[dict], desired: list[str]) -> tuple[list[dict], list[str]]:
    """(occurrences to remove, track ids to append) that turn `current`
    ({"track_id", "item_id"} in order) into `desired`.

    Surplus occurrences are removed first. If what remains is the start of
    `desired`, the rest is appended, which covers removals and additions
    without disturbing anything else. Otherwise everything after the
    longest common start is removed and re-added in order."""
    need = Counter(desired)
    seen: Counter = Counter()
    keep, removals = [], []
    for item in current:
        if seen[item["track_id"]] < need[item["track_id"]]:
            keep.append(item)
            seen[item["track_id"]] += 1
        else:
            removals.append(item)
    p = 0
    while p < len(keep) and p < len(desired) and keep[p]["track_id"] == desired[p]:
        p += 1
    removals += keep[p:]
    return removals, desired[p:]


def _without(ids: list[str], drop: list[str]) -> list[str]:
    """`ids` minus one occurrence per entry of `drop`."""
    left = Counter(drop)
    out = []
    for t in ids:
        if left[t]:
            left[t] -= 1
        else:
            out.append(t)
    return out


def apply_tracks(token: str, playlist_id: str, desired: list[str],
                 unavailable: list[str]) -> dict:
    """Makes the playlist hold `desired`, then reads it back.

    {"status": "ok", "unavailable": [...]} when the read-back equals
    `desired` minus the tracks Tidal refused; {"status": "error", "reason":
    ...} otherwise. Tracks refused last time are tried again at the end; if
    one is accepted, the next round puts it in its place."""
    for _ in range(_APPLY_ROUNDS):
        read = tidal_client.get_playlist_items(token, playlist_id)
        if read["status"] != "ok":
            return {"status": "error", "reason": read["reason"]}
        target = _without(desired, unavailable)
        removals, additions = plan_edits(read["items"], target)
        retry = [t for t in unavailable if t in desired]
        if removals:
            res = tidal_client.remove_items(token, playlist_id, removals)
            if res["status"] != "ok":
                return {"status": "error", "reason": res["reason"]}
        refused: list[str] = []
        if additions or retry:
            res = tidal_client.add_tracks(token, playlist_id, additions + retry)
            if res["status"] != "ok":
                return {"status": "error", "reason": res["reason"]}
            refused = res["not_found"]
        after = tidal_client.get_playlist_items(token, playlist_id)
        if after["status"] != "ok":
            return {"status": "error", "reason": after["reason"]}
        got = [i["track_id"] for i in after["items"]]
        if got == _without(desired, refused):
            return {"status": "ok", "unavailable": refused}
        unavailable = refused
    return {"status": "error", "reason": "read_back_mismatch"}


def _load(value: str | None) -> list[str]:
    try:
        out = json.loads(value) if value else []
    except ValueError:
        return []
    return [t for t in out if isinstance(t, str)] if isinstance(out, list) else []


def _fail(conn, share, reason: str, end: bool = False) -> dict:
    _log.warning("Tidal share %s (%r): %s%s", share["id"], share["title"], reason,
                 ", share ended" if end else "")
    if end:
        conn.execute("UPDATE tidal_shares SET status = 'ended', ended_at = datetime('now'), "
                     "last_error = ? WHERE id = ?", (reason, share["id"]))
    else:
        conn.execute("UPDATE tidal_shares SET last_error = ? WHERE id = ?", (reason, share["id"]))
    conn.commit()
    return {"status": "error", "reason": reason}


def sync_share(conn, share, tokens: dict[int, str], scopes: dict[int, str | None],
               tidal_ids: dict[int, str | None] | None = None) -> dict:
    """Brings one active share up to date. `tokens` holds this pass's
    access token per linked user (refreshed once by the playlist sync),
    `scopes` each user's granted Tidal scopes, `tidal_ids` each user's
    Tidal account id."""
    owner, recipient = share["owner_user_id"], share["recipient_user_id"]
    two_way = bool(share["two_way"])
    if owner not in tokens or recipient not in tokens:
        return _fail(conn, share, "not_linked")
    if not tidal_client.has_write_scope(scopes.get(recipient)) or (
            two_way and not tidal_client.has_write_scope(scopes.get(owner))):
        return _fail(conn, share, "reconnect_needed")
    o_tok, r_tok = tokens[owner], tokens[recipient]

    source = tidal_client.get_playlist(o_tok, share["source_remote_id"])
    if source["status"] != "ok":
        return _fail(conn, share, "source_deleted" if source["reason"] == "not_found" else source["reason"],
                     end=source["reason"] == "not_found")
    # Two-way writes to the owner's playlist, which only works on one they
    # own: a playlist they merely follow is read-only to them.
    if two_way and (tidal_ids or {}).get(owner) not in source.get("owner_ids", []):
        return _fail(conn, share, "two_way_needs_own_playlist")
    owner_items = tidal_client.get_playlist_items(o_tok, share["source_remote_id"])
    if owner_items["status"] != "ok":
        return _fail(conn, share, owner_items["reason"])
    owner_ids = [i["track_id"] for i in owner_items["items"]]

    copy_id = share["copy_remote_id"]
    copy_title = share["copy_title"]
    if not copy_id:
        created = tidal_client.create_playlist(r_tok, source["name"])
        if created["status"] != "ok":
            return _fail(conn, share, created["reason"])
        copy_id, copy_title = created["id"], source["name"]
        # Recorded before anything else is written, so the copy is never
        # orphaned: whatever fails next, the next pass finds it by this id.
        conn.execute("UPDATE tidal_shares SET copy_remote_id = ?, copy_title = ? WHERE id = ?",
                     (copy_id, copy_title, share["id"]))
        conn.commit()
    else:
        copy = tidal_client.get_playlist(r_tok, copy_id)
        if copy["status"] != "ok":
            return _fail(conn, share, "copy_deleted" if copy["reason"] == "not_found" else copy["reason"],
                         end=copy["reason"] == "not_found")
        # The title follows the owner unless the recipient renamed the copy.
        if source["name"] != copy_title and copy["name"] == copy_title:
            renamed = tidal_client.rename_playlist(r_tok, copy_id, source["name"])
            if renamed["status"] != "ok":
                return _fail(conn, share, renamed["reason"])
            copy_title = source["name"]

    owner_unavail = _load(share["unavailable_owner"])
    recip_unavail = _load(share["unavailable_recipient"])
    if two_way and share["snapshot"] is not None:
        copy_items = tidal_client.get_playlist_items(r_tok, copy_id)
        if copy_items["status"] != "ok":
            return _fail(conn, share, copy_items["reason"])
        merged = merge_two_way(_load(share["snapshot"]), owner_ids,
                               [i["track_id"] for i in copy_items["items"]],
                               owner_unavail, recip_unavail)
        on_owner = apply_tracks(o_tok, share["source_remote_id"], merged, owner_unavail)
        if on_owner["status"] != "ok":
            return _fail(conn, share, f"owner_{on_owner['reason']}")
        owner_unavail = on_owner["unavailable"]
    else:
        merged = owner_ids
    on_copy = apply_tracks(r_tok, copy_id, merged, recip_unavail)
    if on_copy["status"] != "ok":
        return _fail(conn, share, f"copy_{on_copy['reason']}")

    conn.execute(
        "UPDATE tidal_shares SET title = ?, copy_title = ?, snapshot = ?, unavailable_owner = ?, "
        "unavailable_recipient = ?, last_synced_at = datetime('now'), last_error = NULL WHERE id = ?",
        (source["name"], copy_title, json.dumps(merged), json.dumps(owner_unavail),
         json.dumps(on_copy["unavailable"]), share["id"]))
    conn.commit()
    return {"status": "ok", "tracks": len(merged), "unavailable": len(on_copy["unavailable"])}


def _refuse_delete(conn, share, reason: str, blocked: list[dict] | None = None) -> dict:
    """Drops a delete request that must not go ahead, and says why: with
    `blocked`, the tracks of the original the copy lacks."""
    _log.warning("Tidal share %s (%r): original kept, %s", share["id"], share["title"], reason)
    conn.execute("UPDATE tidal_shares SET delete_original_requested_at = NULL, last_error = ?, "
                 "delete_blocked_tracks = ? WHERE id = ?",
                 (reason, json.dumps(blocked) if blocked else None, share["id"]))
    conn.commit()
    return {"status": "error", "reason": reason}


def _blocking(items: list[dict], missing: Counter) -> list[dict]:
    """The occurrences of the original the copy lacks, as {"track_id",
    "position"} (1-based, as the owner sees the playlist in Tidal). For a
    track the copy has fewer times, the last occurrences are listed."""
    left = Counter(missing)
    out = []
    for position in range(len(items), 0, -1):
        track = items[position - 1]["track_id"]
        if left[track]:
            left[track] -= 1
            out.append({"track_id": track, "position": position})
    return sorted(out, key=lambda b: b["position"])


def delete_original(conn, share, tokens: dict[int, str], scopes: dict[int, str | None],
                    tidal_ids: dict[int, str | None]) -> dict:
    """Deletes the owner's playlist of an ended share, as its owner asked:
    the last step of handing a playlist over. It can't be undone, so it
    goes ahead only when the recipient's copy still exists and holds every
    track of the original, the owner owns it, and no live share still
    reads from it. A track Tidal refuses on the recipient's side blocks it
    too: the copy never had it, so deleting the original would lose it.
    A refusal drops the request and records why; a failure to reach Tidal
    keeps it for the next pass."""
    owner, recipient = share["owner_user_id"], share["recipient_user_id"]
    if owner not in tokens or recipient not in tokens:
        return _fail(conn, share, "not_linked")
    if not tidal_client.has_write_scope(scopes.get(owner)):
        return _fail(conn, share, "reconnect_needed")
    if conn.execute("SELECT 1 FROM tidal_shares WHERE source_remote_id = ? AND owner_user_id = ? "
                    "AND status != 'ended' AND id != ?", (share["source_remote_id"], owner, share["id"])).fetchone():
        return _refuse_delete(conn, share, "original_still_shared")
    o_tok, r_tok = tokens[owner], tokens[recipient]
    source = tidal_client.get_playlist(o_tok, share["source_remote_id"])
    if source["status"] != "ok":
        if source["reason"] != "not_found":
            return _fail(conn, share, source["reason"])
        gone = True
    else:
        gone = False
        if tidal_ids.get(owner) not in source["owner_ids"]:
            return _refuse_delete(conn, share, "original_not_owned")
        copy = tidal_client.get_playlist_items(r_tok, share["copy_remote_id"])
        if copy["status"] != "ok":
            if copy["reason"] == "not_found":
                return _refuse_delete(conn, share, "copy_missing")
            return _fail(conn, share, copy["reason"])
        original = tidal_client.get_playlist_items(o_tok, share["source_remote_id"])
        if original["status"] != "ok":
            return _fail(conn, share, original["reason"])
        missing = (Counter(i["track_id"] for i in original["items"])
                   - Counter(i["track_id"] for i in copy["items"]))
        if missing:
            return _refuse_delete(conn, share, "copy_lacks_tracks", _blocking(original["items"], missing))
        deleted = tidal_client.delete_playlist(o_tok, share["source_remote_id"])
        if deleted["status"] != "ok":
            return _fail(conn, share, deleted["reason"])
    _log.info("Tidal share %s (%r): original %s", share["id"], share["title"],
              "already gone" if gone else "deleted")
    conn.execute("UPDATE tidal_shares SET original_deleted_at = datetime('now'), last_error = NULL, "
                 "delete_blocked_tracks = NULL WHERE id = ?", (share["id"],))
    conn.commit()
    return {"status": "ok"}


def sync_all(conn, tokens: dict[int, str]) -> dict:
    """Syncs every active share, unless sharing is off. {"synced": n,
    "failed": n}. One share's failure never stops the others."""
    if db.get_config(conn, "tidal_playlists_write") != "1":
        return {"synced": 0, "failed": 0}
    linked = conn.execute(
        "SELECT id, tidal_scopes, tidal_user_id FROM users WHERE tidal_refresh_token IS NOT NULL").fetchall()
    scopes = {row["id"]: row["tidal_scopes"] for row in linked}
    tidal_ids = {row["id"]: row["tidal_user_id"] for row in linked}
    synced = failed = 0
    for share in conn.execute("SELECT * FROM tidal_shares WHERE status = 'active' ORDER BY id").fetchall():
        try:
            result = sync_share(conn, share, tokens, scopes, tidal_ids)
        except Exception:  # noqa: BLE001 -- one share must not stop the rest
            _log.exception("Tidal share %s failed", share["id"])
            result = _fail(conn, share, "internal_error")
        if result["status"] == "ok":
            synced += 1
        else:
            failed += 1
    for share in conn.execute(
            "SELECT * FROM tidal_shares WHERE status = 'ended' AND delete_original_requested_at IS NOT NULL "
            "AND original_deleted_at IS NULL ORDER BY id").fetchall():
        try:
            delete_original(conn, share, tokens, scopes, tidal_ids)
        except Exception:  # noqa: BLE001 -- one share must not stop the rest
            _log.exception("Tidal share %s: deleting the original failed", share["id"])
            _fail(conn, share, "internal_error")
    return {"synced": synced, "failed": failed}
