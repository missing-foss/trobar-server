#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What goes on which device, /api/selections* and /api/basket*: the
selections themselves (which album, artist or playlist on which devices) and
the basket that stages them across several devices before they are sent.
Session-authenticated: main's before-request gate covers these paths.
"""

import logging

from flask import jsonify, request, abort, Blueprint
from flask_babel import gettext as _

import db
import sync_state
from web_common import (
    VALID_SELECTION_TYPES,
    _basket_add,
    _basket_fan_out,
    _basket_set_last_destination,
    _has_delegation,
    _is_admin,
    _require_device_access,
    _require_playlist_visible,
    _selections_for,
    get_current_user_id,
)

_log = logging.getLogger(__name__)

bp = Blueprint("selections", __name__)


def _require_selection_access(conn, user_id: int, selection_id: int) -> None:
    """Creator, admin, or manages at least one device the selection is
    currently linked to — lets a delegate tidy up sync content on a device
    they've been granted, even if someone else originally created it."""
    row = conn.execute("SELECT created_by_user_id FROM selections WHERE id = ?", (selection_id,)).fetchone()
    if row is None:
        abort(404, description=_("Selection not found"))
    if row["created_by_user_id"] == user_id or _is_admin(conn, user_id):
        return
    device_ids = [r["device_id"] for r in conn.execute(
        "SELECT device_id FROM selection_devices WHERE selection_id = ?", (selection_id,)
    )]
    for d in device_ids:
        owner = conn.execute("SELECT owner_user_id FROM devices WHERE id = ?", (d,)).fetchone()
        if owner is not None and (owner["owner_user_id"] == user_id or _has_delegation(conn, user_id, owner["owner_user_id"])):
            return
    abort(403, description=_("Unauthorized access to this selection"))


@bp.route("/api/selections", methods=["GET", "POST"])
def api_selections():
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        if request.method == "POST":
            body = request.get_json(force=True)
            if body.get("type") not in VALID_SELECTION_TYPES:
                abort(400, description=_("Unsupported selection type."))
            device_ids = [int(d) for d in body.get("device_ids", [])]
            for device_id in device_ids:
                _require_device_access(conn, user_id, device_id)
            _require_playlist_visible(conn, user_id, body["type"], body["target"])
            selection_id = sync_state.create_selection(
                conn, body["type"], body["target"], user_id, device_ids
            )
            return jsonify({"id": selection_id})

        return jsonify(_selections_for(conn, user_id))


@bp.route("/api/selections/<int:selection_id>", methods=["DELETE"])
def api_selection_delete(selection_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_selection_access(conn, user_id, selection_id)
        sync_state.delete_selection(conn, selection_id)
        return jsonify({"status": "ok"})


@bp.route("/api/selections/removal-count", methods=["POST"])
def api_selections_removal_count():
    """What unticking a Selections cell would delete from that device:
    {"tracks": n}, for the confirmation the page asks first."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True)
        device_id = int(body["device_id"])
        _require_device_access(conn, user_id, device_id)
        return jsonify({"tracks": sync_state.removal_count(
            conn, user_id, body["type"], body["target"], device_id)})


@bp.route("/api/selections/toggle-device", methods=["POST"])
def api_selections_toggle_device():
    """Used by the Selections matrix views (albums/artists/playlists) — one
    checkbox cell, one call. Creates the selection on first check; an
    unchecked-to-zero selection is left in place (device-less) rather than
    auto-deleted, so an accidental click is easy to undo, and the table
    offers Forget for it (DELETE /api/selections/<id>). Unticking only ever
    removes the caller's own selection: a 409 names whoever else's selection
    keeps the device ticked."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True)
        device_id = int(body["device_id"])
        _require_device_access(conn, user_id, device_id)
        checked = bool(body["checked"])
        if checked:
            # Only the "add" direction needs the check — unchecking only
            # ever removes access, never grants it, so it's harmless even
            # against a target the matrix would no longer show this user.
            _require_playlist_visible(conn, user_id, body["type"], body["target"])
        selection_id = sync_state.toggle_selection_device(
            conn, user_id, body["type"], body["target"], device_id, checked,
        )
        if not checked:
            # Unticking takes away this user's own selection only. Another
            # person's still keeps it on the device: say so, rather than
            # answering ok and letting the page tick the box again unexplained.
            others = sync_state.selected_by_others(conn, user_id, body["type"], body["target"], device_id)
            if others:
                resp = jsonify({"error": _("Selected by %(names)s, so it stays on this device.",
                                           names=", ".join(others)),
                                "selected_by": others})
                resp.status_code = 409
                abort(resp)
        return jsonify({"status": "ok", "selection_id": selection_id})


@bp.route("/api/basket", methods=["GET", "POST", "DELETE"])
def api_basket():
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        if request.method == "POST":
            return jsonify({"id": _basket_add(conn, user_id, request.get_json(force=True))})
        if request.method == "DELETE":
            sync_state.clear_basket(conn, user_id)
            return jsonify({"status": "ok"})
        return jsonify(sync_state.list_basket(conn, user_id))


@bp.route("/api/basket/<int:item_id>", methods=["DELETE"])
def api_basket_item_delete(item_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        sync_state.remove_basket_item(conn, user_id, item_id)
        return jsonify({"status": "ok"})


@bp.route("/api/basket/<int:item_id>/devices/<int:device_id>", methods=["DELETE"])
def api_basket_item_device_delete(item_id: int, device_id: int):
    """#501: the per-device basket panel's own per-section × — unstages
    this item from just THIS device's section, leaving it staged for any
    others. Ownership is enforced inside sync_state.unstage_basket_item_
    device itself (scoped to user_id in the same query), matching
    api_basket_item_delete's own style, so a bad item_id or one belonging
    to someone else is silently a no-op rather than a 404/403 — same
    tolerance the whole-item DELETE above already has."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        sync_state.unstage_basket_item_device(conn, user_id, item_id, device_id)
        return jsonify({"status": "ok"})


@bp.route("/api/basket/fan-out", methods=["POST"])
def api_basket_fan_out():
    """#501: device-scoped. For each device_id in `device_ids`, sends THAT
    device's own current section — every basket item currently staged
    against it — and unlinks only that device from each one afterward. An
    item also staged for some other device not named in this call is left
    completely untouched: this is what makes "Add & send now" for a quick
    single item safe even when the same device's section already has
    other things staged in it, and what makes fanning out to several
    devices in one call not cross-contaminate their sections (an item
    staged only for device A is never sent to device B just because B was
    also in this request — every create_selection() call below passes
    only the device_ids a given item is ACTUALLY linked to, intersected
    with what was requested, never the raw request list itself).

    Supersedes #497's `items`-explicit-list bypass (removed): that existed
    so a direct pick could send without touching whatever else was staged
    in the basket. Device-scoping gives the same guarantee more generally
    — "other stuff" now means "other devices' sections," and a direct
    pick already stages-then-sends through the ordinary
    POST /api/basket -> POST /api/basket/fan-out pair, so there is no
    remaining need for a basket-bypassing path."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True)
        device_ids = [int(d) for d in body.get("device_ids", [])]
        return jsonify(_basket_fan_out(conn, user_id, device_ids))


@bp.route("/api/basket/last-destination", methods=["PATCH"])
def api_basket_last_destination():
    """Persists "device_ids last chosen for a pick from this surface" —
    the device picker's cheap smart-default (#303). Called by both the
    pre-existing single-item picker and the basket's own fan-out step
    ('basket' is just another surface name)."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True)
        surface = body["surface"]
        device_ids = [int(d) for d in body.get("device_ids", [])]
        return jsonify(_basket_set_last_destination(conn, user_id, surface, device_ids))
