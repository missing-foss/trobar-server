#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signed-in user's device management, /api/devices and /api/devices/*:
session-authenticated, unlike the Bearer-token device API in device_api.py.
Every route resolves the caller with get_current_user_id. Every route that
names a device checks that the caller may act on it with
_require_device_access, directly or (usage) through _device_usage.
"""

import logging

from flask import jsonify, request, abort, Blueprint, current_app
from flask_babel import gettext as _

import artist_images
import db
import playlist_sync
import selection_store
import sync_state
from web_common import (
    DEVICE_ICONS,
    VALID_SELECTION_TYPES,
    _RankingUnavailable,
    _device_list,
    _device_usage,
    _is_admin,
    _ranked_album_keys_for_user,
    _refresh_autofit_after_limit_change,
    _require_device_access,
    _send_projection,
    _validated_device_name,
    _validated_device_type,
    _validated_max_size_bytes,
    _validated_source_of_truth,
    _validated_transcode_format,
    get_current_user_id,
)

_log = logging.getLogger(__name__)

# A whole Library grid ticked at once is a few hundred albums; the bound
# keeps one read from resolving an unbounded list.
_MAX_PROJECTION_TARGETS = 1000

bp = Blueprint("devices", __name__)


def _validated_autofit_percent(value):
    """#217: a whole number 1-100 — 0 would mean "fill nothing", which is
    just disabling auto-fit through a confusing back door, so it's excluded
    rather than silently accepted."""
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 100):
        abort(400, description=_("Fill percentage must be a whole number between 1 and 100."))
    return value


def _validated_artist_images(value):
    imgs = value or None
    if imgs not in (None, "small", "full"):
        abort(400, description=_("Unsupported artist image setting."))
    return imgs


def _validated_device_icon(value):
    """None (follow the type) or one of DEVICE_ICONS; anything else is a 400."""
    if value is None:
        return None
    if value not in DEVICE_ICONS:
        abort(400, description=_("Unknown device icon."))
    return value


def _validated_play_playlist(conn, device_id: int, value):
    """The device's Play choice: null for Everything, or the id of a
    playlist selected for this device. Anything else is a 400."""
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, int)
            or value not in sync_state.device_playlist_ids(conn, device_id)):
        abort(400, description=_("Choose one of the playlists selected for this device."))
    return value


@bp.route("/api/devices", methods=["GET", "POST"])
def api_devices():
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        if request.method == "POST":
            body = request.get_json(force=True)
            name = _validated_device_name(body)
            # #97: honour transcode_format/artist_images at create time (PATCH
            # already did) so an API client can pair + configure in one call,
            # instead of silently dropping them and creating a plain device.
            device_id, raw_token = sync_state.create_device(
                conn, user_id, name, _validated_device_type(body.get("device_type", "phone")),
                _validated_max_size_bytes(body.get("max_size_bytes")),
                transcode_format=_validated_transcode_format(body.get("transcode_format")),
                artist_images=_validated_artist_images(body.get("artist_images")),
            )
            return jsonify({"id": device_id, "name": name, "token": raw_token})

        return jsonify(_device_list(conn, user_id))


@bp.route("/api/devices/delegatable")
def api_devices_delegatable():
    """The pool a delegate picks from to pin a device into their own
    Appareils list: devices owned by someone who's granted them delegation,
    not already pinned. See."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        rows = conn.execute(
            "SELECT d.id, d.name, d.device_type, d.icon, u.username AS owner_username "
            "FROM devices d JOIN users u ON u.id = d.owner_user_id "
            "WHERE d.owner_user_id IN (SELECT target_user_id FROM device_delegations WHERE grantee_user_id = ?) "
            "AND d.id NOT IN (SELECT device_id FROM device_pins WHERE user_id = ?) "
            "ORDER BY u.username, d.name",
            (user_id, user_id),
        ).fetchall()
        return jsonify([dict(r) for r in rows])


@bp.route("/api/devices/<int:device_id>/pin", methods=["POST", "DELETE"])
def api_device_pin(device_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        if request.method == "POST":
            conn.execute(
                "INSERT OR IGNORE INTO device_pins (user_id, device_id) VALUES (?, ?)",
                (user_id, device_id),
            )
        else:
            conn.execute(
                "DELETE FROM device_pins WHERE user_id = ? AND device_id = ?",
                (user_id, device_id),
            )
        conn.commit()
        return jsonify({"status": "ok"})


@bp.route("/api/devices/<int:device_id>", methods=["PATCH", "DELETE"])
def api_device_update(device_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        if request.method == "DELETE":
            # selection_devices and device_track_state both have ON DELETE
            # CASCADE on device_id — removing the device alone is enough to
            # clean those up. Selections themselves are left untouched even
            # if this was their only assigned device (just shows "no
            # device" in the Selections tab — still reassignable later).
            # Collected before the delete: basket_item_devices cascades on
            # device_id, so after the DELETE there is nothing left to say
            # which items this device was the last destination for.
            orphan_candidates = [
                row["basket_item_id"]
                for row in conn.execute(
                    "SELECT DISTINCT basket_item_id FROM basket_item_devices WHERE device_id = ?",
                    (device_id,),
                )
            ]
            # Playlists made on this device go with it, the usual way, so
            # devices they were sent to are told and their mirrors go too.
            playlist_sync.remove_device_playlists(conn, device_id)
            conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            # Not cascaded — users.basket_last_destinations is JSON, not a
            # join table. Same transaction as the delete, so the device and
            # every memory of it go together or not at all.
            sync_state.prune_device_from_last_destinations(conn, device_id, commit=False)
            # Cascaded, but only halfway: the link rows go, the items they
            # were the last link of do not. Same transaction for the same
            # reason.
            sync_state.prune_linkless_basket_items(conn, orphan_candidates, commit=False)
            conn.commit()
            return jsonify({"status": "ok"})

        # Partial update: only touch fields the caller actually sent, so the
        # long-standing "always send max_size_bytes, null clears the limit"
        # contract (updateDeviceLimit()) keeps working unchanged alongside
        # the newer name/device_type editing (follow-up).
        body = request.get_json(force=True)
        updates: list[str] = []
        params: list[object] = []
        if "name" in body:
            updates.append("name = ?")
            params.append(_validated_device_name(body))
        if "device_type" in body:
            updates.append("device_type = ?")
            params.append(_validated_device_type(body["device_type"]))
        limit_changed = False
        if "max_size_bytes" in body:
            new_limit = _validated_max_size_bytes(body.get("max_size_bytes"))
            limit_changed = new_limit != (conn.execute(
                "SELECT max_size_bytes FROM devices WHERE id = ?", (device_id,)
            ).fetchone()["max_size_bytes"] or None)
            updates.append("max_size_bytes = ?")
            params.append(new_limit)
        if "artist_images" in body:
            updates.append("artist_images = ?")
            params.append(_validated_artist_images(body.get("artist_images")))
        if "icon" in body:
            updates.append("icon = ?")
            params.append(_validated_device_icon(body.get("icon")))
        if "play_playlist_id" in body:
            updates.append("play_playlist_id = ?")
            params.append(_validated_play_playlist(conn, device_id, body.get("play_playlist_id")))
        transcode_changed = False
        if "transcode_format" in body:
            fmt = _validated_transcode_format(body.get("transcode_format"))
            current = conn.execute(
                "SELECT transcode_format FROM devices WHERE id = ?", (device_id,)
            ).fetchone()["transcode_format"]
            transcode_changed = fmt != current
            updates.append("transcode_format = ?")
            params.append(fmt)
        source_of_truth_changed = False
        if "source_of_truth" in body:
            updates.append("source_of_truth = ?")
            params.append(_validated_source_of_truth(body.get("source_of_truth")))
            source_of_truth_changed = True
        if updates:
            params.append(device_id)
            conn.execute(f"UPDATE devices SET {', '.join(updates)} WHERE id = ?", params)
            # a format change renames every expected on-device
            # file (extension swap), so everything already synced goes around
            # again — downloaded flips back to pending (excluded stays
            # excluded: the user chose not to have those files). The desktop
            # client's orphan sweep then removes the old-extension files.
            if transcode_changed:
                conn.execute(
                    "UPDATE device_track_state SET status='pending', bytes_on_device=NULL, "
                    "updated_at=datetime('now') WHERE device_id=? AND status='downloaded'",
                    (device_id,),
                )
            conn.commit()
            # #63: flipping back to 'server' re-enables pruning of tracks no
            # selection requires; recompute so it takes effect immediately.
            if source_of_truth_changed:
                sync_state.recompute_device_state(conn, device_id)
            if limit_changed:
                _refresh_autofit_after_limit_change(conn, device_id)
        return jsonify({"status": "ok"})


@bp.route("/api/devices/<int:device_id>/regenerate-token", methods=["POST"])
def api_device_regenerate_token(device_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        token = sync_state.regenerate_token(conn, device_id)
        name = conn.execute("SELECT name FROM devices WHERE id = ?", (device_id,)).fetchone()["name"]
        return jsonify({"name": name, "token": token})


@bp.route("/api/devices/<int:device_id>/repair-code", methods=["POST"])
def api_device_repair_code(device_id: int):
    """Mint an enrollment code that re-attaches a client to THIS device
    instead of creating a new one.

    The reinstall case. A device's Bearer token is shown once and never
    stored, so an app that has been uninstalled cannot get back to its own
    device: the enrollment wizard only speaks 8-char codes, and every code
    until now created a new device — leaving the old one orphaned with its
    selections, track state and settings still on it.

    Deliberately a separate route from regenerate-token rather than a flag on
    it. That one hands out a raw device token, the only format desktop builds
    from before enrollment codes read (pasted config, or .trobar/device.json
    on the card); this one hands out a code, which the Android app, current
    desktop builds and the Garmin watch read. Same outcome, two client
    contracts, and the older one still has users. The web UI's device action
    and its new-device flow for DAPs, cards and folders both mint this; the
    token is a secondary action for the older desktop builds."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        owner_id = conn.execute(
            "SELECT owner_user_id FROM devices WHERE id = ?", (device_id,)
        ).fetchone()["owner_user_id"]
        # Bound to the device's OWNER, not to whoever minted it. An admin or a
        # delegate may legitimately mint this on someone else's behalf, and the
        # device must still belong to its owner afterwards -- a re-pair is a
        # recovery, not a transfer of ownership.
        code = sync_state.create_enrollment_grant(conn, owner_id, device_id=device_id)
        return jsonify({"code": code, "expires_in": sync_state.ENROLLMENT_TTL_SECONDS})


@bp.route("/api/devices/<int:device_id>/transfer-from", methods=["POST"])
def api_device_transfer(device_id: int):
    """#440: "device_id replaces from_device_id" -- reassigns
    device_track_state/selections/settings from the old device onto this
    one, then deletes the old device. See device_store.transfer_device for
    the mechanics and why settings are copied before track-state is.

    Both devices need _require_device_access, checked independently -- a
    delegate with access to someone else's OLD device (helping them
    replace hardware) still needs matching access to the NEW device. That
    alone isn't enough to stop the case the issue calls out by name
    though: a delegate legitimately has access to BOTH their own device
    (new) and the person they're delegated for (old), which would let them
    siphon that person's synced content onto their own hardware. Same-
    owner transfers (by far the common case: my old phone -> my new
    phone) are unaffected; a genuine cross-owner move additionally
    requires admin."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True)
        from_device_id = int(body["from_device_id"])
        if from_device_id == device_id:
            abort(400, description=_("A device can't replace itself."))
        new_owner_id = _require_device_access(conn, user_id, device_id)
        old_owner_id = _require_device_access(conn, user_id, from_device_id)
        if new_owner_id != old_owner_id and not _is_admin(conn, user_id):
            abort(403, description=_("Transferring between different owners requires admin."))
        # #442 review: defaults to False (safe) unless the caller says the
        # new device is known to already hold the content -- see
        # device_store.transfer_device's docstring for why the default matters.
        assume_present = bool(body.get("assume_present", False))
        summary = sync_state.transfer_device(conn, from_device_id, device_id, assume_present=assume_present)
        return jsonify({"status": "ok", **summary})


@bp.route("/api/devices/<int:device_id>/unknown-tracks")
def api_device_unknown_tracks(device_id: int):
    """#161: the device's "unknown" extras — manifest paths that match no live
    library track (side-loaded, or kept past a library deletion) — for the web
    review list. Owner/admin/delegate only (session)."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        return jsonify(sync_state.list_device_unknown_tracks(conn, device_id))


@bp.route("/api/devices/<int:device_id>/unknown-tracks/adopt", methods=["POST"])
def api_device_unknown_tracks_adopt(device_id: int):
    """#161: {"paths": [...], "adopted": true|false} — acknowledge the given
    device paths as device-owned extras (recorded, never managed by the server)
    so they stop being flagged, or un-adopt them. Returns the resulting
    unknown_track_count (non-adopted)."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        body = request.get_json(force=True)
        paths = body.get("paths")
        if not isinstance(paths, list):
            abort(400, description=_("paths must be a list of device paths."))
        adopted = bool(body.get("adopted", True))
        count = sync_state.set_device_unknown_adopted(conn, device_id, paths, adopted)
        return jsonify({"unknown_track_count": count})


@bp.route("/api/devices/<int:device_id>/usage")
def api_device_usage(device_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        return jsonify(_device_usage(conn, user_id, device_id))


@bp.route("/api/devices/<int:device_id>/autofit", methods=["POST", "DELETE"])
def api_device_autofit(device_id: int):
    """Enable/refresh (POST) or disable (DELETE) storage-budget auto-fit for a
    device. Ranks the device owner's most-played albums (Last.fm) and
    fits whole albums into the device's remaining budget; on-demand only, so the
    synced set is stable until the next refresh. #217: an optional "percent"
    in the POST body persists a new fill-percentage cap before refreshing —
    omit it to refresh at whatever's already set."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        device = conn.execute(
            "SELECT owner_user_id, max_size_bytes FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if device is None:
            abort(404)

        selection_id = sync_state.autofit_selection_id_for_device(conn, device_id)

        if request.method == "DELETE":
            if selection_id is not None:
                sync_state.delete_selection(conn, selection_id)  # cascades + recomputes
            return jsonify({"enabled": False})

        body = request.get_json(silent=True) or {}
        period = body.get("period") or sync_state.DEFAULT_AUTOFIT_PERIOD
        if "percent" in body:
            conn.execute(
                "UPDATE devices SET autofit_percent = ? WHERE id = ?",
                (_validated_autofit_percent(body.get("percent")), device_id),
            )
            conn.commit()
        if selection_id is None:
            selection_id = sync_state.create_autofit_selection(conn, device_id, user_id, period)
        else:
            conn.execute("UPDATE selections SET target = ? WHERE id = ?", (period, selection_id))
            conn.commit()

        # Rank by the device OWNER's Last.fm taste (it's their device/library).
        try:
            ranked = _ranked_album_keys_for_user(conn, device["owner_user_id"], period)
        except _RankingUnavailable:
            # Keep the set and what the device holds as they are, and say why
            # nothing changed; the UI merges this into the device's status.
            current_app.logger.warning("device %d: auto-fit refresh skipped: Last.fm didn't answer", device_id)
            status: dict = dict(sync_state.autofit_status(conn, device_id))
            status["reason"] = "lastfm_unavailable"
            status["period"] = period
            return jsonify(status)
        # dict(...) rather than autofit.AutofitSummary directly: the
        # endpoint response extends it below with "enabled"/"period", which
        # aren't part of refresh_autofit()'s own (narrower) contract, and
        # mypy won't let a TypedDict absorb arbitrary extra keys even via a
        # plain `dict` variable annotation.
        summary: dict = dict(sync_state.refresh_autofit(conn, selection_id, ranked or []))
        if ranked is None and summary["reason"] is None:
            summary["reason"] = "no_lastfm"
        sync_state.recompute_device_state(conn, device_id)
        conn.commit()
        summary["enabled"] = True
        summary["period"] = period
        return jsonify(summary)


@bp.route("/api/devices/<int:device_id>/prune-preview")
def api_device_prune_preview(device_id: int):
    """How many tracks switching Keep device content off would remove from
    the device at its next sync, for the question the switch asks first.
    Owner/admin/delegate only (session). No write."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        return jsonify({"tracks": selection_store.prune_preview_count(conn, device_id)})


@bp.route("/api/devices/send-projection")
def api_device_send_projection():
    """Per device the user may see, what sending `type` with one or more
    `target`s would add to it, beside its usage, limit and reported free
    space: the Sync to… picker's bars. Session only. No write."""
    sel_type = request.args.get("type", "")
    targets = request.args.getlist("target")
    if sel_type not in VALID_SELECTION_TYPES:
        abort(400, description=_("Unsupported selection type."))
    if len(targets) > _MAX_PROJECTION_TARGETS:
        abort(400, description=_("Too many items to measure at once."))
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        return jsonify(_send_projection(conn, user_id, sel_type, targets))


@bp.route("/api/devices/<int:device_id>/autofit/preview")
def api_device_autofit_preview(device_id: int):
    """#217: the percent-independent basis (max_size_bytes/manual_bytes/
    avg_track_bytes) for the live GB/track-count preview in the Devices
    panel — fetched once when the auto-fit mini-panel opens, not per slider
    drag; the frontend does the cheap percent arithmetic itself from there.
    No Last.fm call, no write."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        result = sync_state.autofit_fill_basis(conn, device_id)
        if result is None:
            abort(404)
        return jsonify(result)
