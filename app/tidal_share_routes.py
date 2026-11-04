#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/tidal-shares*: sharing a Tidal playlist into another member's Tidal
account (see tidal_shares.py for the sync itself).

A share starts as an invitation the recipient accepts or declines; an admin
may push one, which starts it active. Either side may end it, which stops
the sync and leaves both playlists as they are. Nothing here talks to
Tidal: every Tidal call happens in the playlist sync, on the one token
refresh per user that pass makes. A second refresh from a request could
race the sync's and lose the link.

All of it is hidden (404) until the admin has said their Tidal app has
playlists.write. Session-authenticated: main's before-request gate covers
these paths.
"""

import json
import logging

from flask import jsonify, request, abort, Blueprint
from flask_babel import gettext as _

import db
import tidal_client
from web_common import _is_admin, get_current_user_id

_log = logging.getLogger(__name__)

bp = Blueprint("tidal_share_routes", __name__)


def _require_enabled(conn) -> None:
    if db.get_config(conn, "tidal_playlists_write") != "1":
        abort(404)


def _track_count(conn, playlist_id: int) -> int:
    """Tracks in Trobar's own copy of the playlist, matched to the library
    or not: what the invitation shows before anything is synced."""
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?)"
        " + (SELECT COUNT(*) FROM unresolved_playlist_tracks WHERE playlist_id = ?)",
        (playlist_id, playlist_id)).fetchone()
    return row[0]


def _share_dict(conn, row) -> dict:
    names = {r["id"]: r["username"] for r in conn.execute(
        "SELECT id, username FROM users WHERE id IN (?, ?)",
        (row["owner_user_id"], row["recipient_user_id"]))}
    playlist = conn.execute(
        "SELECT id FROM playlists WHERE source_provider = 'tidal' AND source_playlist_id = ? "
        "AND owner_user_id = ?", (row["source_remote_id"], row["owner_user_id"])).fetchone()
    snapshot = _list_len(row["snapshot"])
    return {
        "id": row["id"],
        "title": row["title"],
        "owner": {"id": row["owner_user_id"], "username": names.get(row["owner_user_id"])},
        "recipient": {"id": row["recipient_user_id"], "username": names.get(row["recipient_user_id"])},
        "status": row["status"],
        "two_way": bool(row["two_way"]),
        "playlist_id": playlist["id"] if playlist else None,
        "track_count": snapshot if snapshot is not None else (
            _track_count(conn, playlist["id"]) if playlist else None),
        "unavailable_count": _list_len(row["unavailable_recipient"]) or 0,
        "last_synced_at": row["last_synced_at"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "accepted_at": row["accepted_at"],
        "ended_at": row["ended_at"],
        "delete_original_requested_at": row["delete_original_requested_at"],
        "original_deleted_at": row["original_deleted_at"],
        "delete_blocked_tracks": _blocked(row["delete_blocked_tracks"]),
    }


def _blocked(value: str | None) -> list[dict]:
    """The tracks that kept the original, as the last refusal stored them."""
    try:
        out = json.loads(value) if value else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _list_len(value: str | None) -> int | None:
    """Length of a stored JSON list, None when nothing is stored."""
    if value is None:
        return None
    try:
        out = json.loads(value)
    except ValueError:
        return None
    return len(out) if isinstance(out, list) else None


def _load_share(conn, share_id: int):
    row = conn.execute("SELECT * FROM tidal_shares WHERE id = ?", (share_id,)).fetchone()
    if row is None:
        abort(404, description=_("Share not found"))
    return row


@bp.route("/api/tidal-shares", methods=["GET"])
def api_tidal_shares():
    """The current user's shares: those they started, and those shared with
    them, ended ones included so either side can see what stopped."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_enabled(conn)
        rows = conn.execute(
            "SELECT * FROM tidal_shares WHERE owner_user_id = ? OR recipient_user_id = ? "
            "ORDER BY id DESC", (user_id, user_id)).fetchall()
        shares = [_share_dict(conn, r) for r in rows]
        return jsonify({
            "outgoing": [s for s in shares if s["owner"]["id"] == user_id],
            "incoming": [s for s in shares if s["recipient"]["id"] == user_id],
        })


@bp.route("/api/tidal-shares/recipients", methods=["GET"])
def api_tidal_share_recipients():
    """Who a playlist can be shared with: every other user with Tidal
    linked, and whether their link can already receive a copy."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_enabled(conn)
        rows = conn.execute(
            "SELECT id, username, tidal_scopes FROM users WHERE tidal_refresh_token IS NOT NULL "
            "AND id != ? ORDER BY username COLLATE NOCASE", (user_id,)).fetchall()
        return jsonify([{"id": r["id"], "username": r["username"],
                         "can_receive": tidal_client.has_write_scope(r["tidal_scopes"])} for r in rows])


@bp.route("/api/tidal-shares", methods=["POST"])
def api_tidal_share_create():
    """{"playlist_id", "recipient_user_id", "two_way"?, "push"?}. The
    playlist must be the caller's own Tidal playlist in Trobar. `push`
    (admins only) starts the share active, without an invitation."""
    body = request.get_json(force=True, silent=True) or {}
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_enabled(conn)
        try:
            playlist_id = int(body["playlist_id"])
            recipient = int(body["recipient_user_id"])
        except (KeyError, TypeError, ValueError):
            abort(400, description=_("A playlist and a recipient are needed."))
        push = bool(body.get("push"))
        if push and not _is_admin(conn, user_id):
            abort(403, description=_("Only an admin can share without an invitation."))
        playlist = conn.execute(
            "SELECT id, title, source_playlist_id FROM playlists WHERE id = ? AND source_provider = 'tidal' "
            "AND owner_user_id = ?", (playlist_id, user_id)).fetchone()
        if playlist is None or not playlist["source_playlist_id"]:
            abort(404, description=_("Only your own Tidal playlists can be shared."))
        if recipient == user_id:
            abort(400, description=_("Choose someone else to share with."))
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (recipient,)).fetchone() is None:
            abort(404, description=_("That user doesn't exist."))
        if conn.execute(
                "SELECT 1 FROM tidal_shares WHERE source_remote_id = ? AND recipient_user_id = ? "
                "AND status != 'ended'", (playlist["source_playlist_id"], recipient)).fetchone():
            abort(409, description=_("This playlist is already shared with them."))
        cur = conn.execute(
            "INSERT INTO tidal_shares (owner_user_id, recipient_user_id, source_remote_id, title, status, "
            "two_way, accepted_at) VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ? THEN datetime('now') END)",
            (user_id, recipient, playlist["source_playlist_id"], playlist["title"],
             "active" if push else "pending", 1 if body.get("two_way") else 0, push))
        conn.commit()
        return jsonify(_share_dict(conn, _load_share(conn, db._new_id(cur)))), 201


@bp.route("/api/tidal-shares/<int:share_id>/accept", methods=["POST"])
def api_tidal_share_accept(share_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_enabled(conn)
        share = _load_share(conn, share_id)
        if share["recipient_user_id"] != user_id:
            abort(403, description=_("Only the person it was shared with can accept it."))
        if share["status"] != "pending":
            abort(409, description=_("This share isn't waiting for an answer."))
        conn.execute("UPDATE tidal_shares SET status = 'active', accepted_at = datetime('now') WHERE id = ?",
                     (share_id,))
        conn.commit()
        return jsonify(_share_dict(conn, _load_share(conn, share_id)))


@bp.route("/api/tidal-shares/<int:share_id>/decline", methods=["POST"])
def api_tidal_share_decline(share_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_enabled(conn)
        share = _load_share(conn, share_id)
        if share["recipient_user_id"] != user_id:
            abort(403, description=_("Only the person it was shared with can decline it."))
        if share["status"] != "pending":
            abort(409, description=_("This share isn't waiting for an answer."))
        conn.execute("UPDATE tidal_shares SET status = 'ended', ended_at = datetime('now') WHERE id = ?",
                     (share_id,))
        conn.commit()
        return jsonify(_share_dict(conn, _load_share(conn, share_id)))


@bp.route("/api/tidal-shares/<int:share_id>/end", methods=["POST"])
def api_tidal_share_end(share_id: int):
    """Stops the sync. Both playlists stay as they are: the copy simply
    becomes the recipient's own."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_enabled(conn)
        share = _load_share(conn, share_id)
        if user_id not in (share["owner_user_id"], share["recipient_user_id"]) and not _is_admin(conn, user_id):
            abort(403, description=_("Only the two people sharing it can end it."))
        if share["status"] == "ended":
            abort(409, description=_("This share has already ended."))
        conn.execute("UPDATE tidal_shares SET status = 'ended', ended_at = datetime('now') WHERE id = ?",
                     (share_id,))
        conn.commit()
        return jsonify(_share_dict(conn, _load_share(conn, share_id)))


@bp.route("/api/tidal-shares/<int:share_id>", methods=["PATCH"])
def api_tidal_share_update(share_id: int):
    """{"two_way": bool}, by the owner, while the share hasn't ended."""
    body = request.get_json(force=True, silent=True) or {}
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_enabled(conn)
        share = _load_share(conn, share_id)
        if share["owner_user_id"] != user_id:
            abort(403, description=_("Only the owner can change how a share syncs."))
        if share["status"] == "ended":
            abort(409, description=_("This share has already ended."))
        if "two_way" not in body:
            abort(400, description=_("Nothing to change."))
        conn.execute("UPDATE tidal_shares SET two_way = ? WHERE id = ?",
                     (1 if body.get("two_way") else 0, share_id))
        conn.commit()
        return jsonify(_share_dict(conn, _load_share(conn, share_id)))


@bp.route("/api/tidal-shares/<int:share_id>/delete-original", methods=["POST", "DELETE"])
def api_tidal_share_delete_original(share_id: int):
    """POST asks for the owner's original to be deleted once the share has
    ended, which hands the playlist over: the recipient's copy stays. The
    next playlist sync does it, and only after checking the copy still
    holds every track (see tidal_shares.delete_original). DELETE withdraws
    the request before that happens."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_enabled(conn)
        share = _load_share(conn, share_id)
        if share["owner_user_id"] != user_id:
            abort(403, description=_("Only the owner can delete their playlist."))
        if share["original_deleted_at"] is not None:
            abort(409, description=_("The original has already been deleted."))
        if request.method == "DELETE":
            conn.execute("UPDATE tidal_shares SET delete_original_requested_at = NULL WHERE id = ?", (share_id,))
        else:
            if share["status"] != "ended":
                abort(409, description=_("End the share before deleting the original."))
            if not share["copy_remote_id"]:
                abort(409, description=_("No copy was ever made, so the original is the only one."))
            if conn.execute("SELECT 1 FROM tidal_shares WHERE source_remote_id = ? AND owner_user_id = ? "
                            "AND status != 'ended' AND id != ?",
                            (share["source_remote_id"], user_id, share_id)).fetchone():
                abort(409, description=_("This playlist is still shared with someone else."))
            conn.execute("UPDATE tidal_shares SET delete_original_requested_at = datetime('now'), "
                         "last_error = NULL, delete_blocked_tracks = NULL WHERE id = ?", (share_id,))
        conn.commit()
        return jsonify(_share_dict(conn, _load_share(conn, share_id)))
