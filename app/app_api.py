#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The phone app's own API, /api/app/*: the screens the Trobar app shows,
acting as the device's owner. Bearer-authenticated, not by session: main's
before-request gate exempts the whole prefix, so every route here must call
_authenticated_app_user() itself.
"""

import logging

from flask import jsonify, request, abort, Blueprint
from flask_babel import gettext as _

import db
import device_plays
import playlist_sync
import sync_state
from web_common import (
    APP_API_VERSION,
    APP_DASHBOARD_WIDGET_CATALOG,
    DEFAULT_PLAYLISTS_WIDGET_LIMIT,
    PLAYLISTS_WIDGET_LIMIT_RANGE,
    VALID_SELECTION_TYPES,
    _playlists_to_sync,
    _require_device_access,
    _add_subscription,
    _admin_counts,
    _apply_dashboard_widgets_patch,
    _artist_image_config,
    _artist_image_response,
    _authenticated_device,
    _basket_add,
    _basket_fan_out,
    _basket_last_destinations_dict,
    _basket_set_last_destination,
    _client_ip,
    _cover_response,
    _create_playlist,
    _delete_playlist,
    _delete_subscription,
    _device_dashboard_widgets,
    _device_list,
    _device_usage,
    _exclude_unresolved_tracks,
    _library_album_artist_albums,
    _library_album_artists,
    _library_album_tracks,
    _library_appears_on,
    _library_cover_row,
    _library_similar_artists,
    _library_stats,
    _most_played_for,
    _most_played_limit,
    _own_subscriptions,
    _recently_added_for,
    _recently_released_for,
    _record_failure,
    _refresh_subscription,
    _require_playlist_visible_by_id,
    _scrobble_sources,
    _selections_for,
    _set_lidarr_requests,
    _start_playlist_sync,
    _suggestions_for,
    _unresolved_tracks_of,
    _update_playlist,
    _validated_period,
    _visible_playlists,
    _widget_catalog,
    _write_playlist_tracks,
)

_log = logging.getLogger(__name__)

bp = Blueprint("app_api", __name__)


def _library_artists(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT artist, COUNT(*) AS track_count, "
        "COUNT(DISTINCT album) AS album_count FROM tracks "
        "WHERE deleted_at IS NULL GROUP BY artist ORDER BY artist"
    ).fetchall()
    # Each artist's album titles, so a client can search them without one
    # request per artist. A second query rather than GROUP_CONCAT: a title
    # may contain any separator.
    albums: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT artist, album FROM tracks WHERE deleted_at IS NULL AND album != '' "
        "GROUP BY artist, album ORDER BY artist, album"
    ):
        albums.setdefault(r["artist"], []).append(r["album"])
    return [{**dict(r), "albums": albums.get(r["artist"], [])} for r in rows]


def _library_albums(conn, artist: str) -> list[dict]:
    rows = conn.execute(
        "SELECT album, MAX(year) AS year, MAX(reissue_year) AS reissue_year, "
        "COUNT(*) AS track_count FROM tracks "
        "WHERE deleted_at IS NULL AND artist = ? GROUP BY album "
        "ORDER BY year IS NULL, year DESC, album",
        (artist,),
    ).fetchall()
    return [dict(r) for r in rows]


def _scrobble_configured(conn, user_id: int) -> bool:
    """Whether the suggestion and most-played feeds have a listening
    history to draw on at all -- the signal the web's amber hint shows; the
    App API returns it beside the items rather than leaving an empty list
    to mean two different things."""
    user, _, _, _ = _scrobble_sources(conn, user_id)
    return (bool(user["lastfm_username"] or user["listenbrainz_username"] or user["maloja_url"])
            or device_plays.is_source(conn, user_id))


def _authenticated_app_user(conn):
    """Resolve the acting user for an /api/app/ route: the owner of the
    device whose token authenticates the request. Always the owner -- a
    device pinned to, or delegated to, another user still acts as the
    account it belongs to, never as the viewer who pinned it. The owner's
    admin flag is read fresh on every call, as the integration token's is,
    so a demoted admin loses admin-only routes on the next request rather
    than at the next pairing. Fails closed if the owner row is gone (an
    orphaned device cannot act as anyone; user deletion refuses while
    devices remain, so this is belt-and-braces). Shares the device API's
    rate-limit bucket: a wrong token here is the same wrong token there.

    Returns (device_row, user_id, is_admin)."""
    device = _authenticated_device(conn)
    owner = conn.execute(
        "SELECT id, is_admin FROM users WHERE id = ?", (device["owner_user_id"],)).fetchone()
    if owner is None:
        _record_failure("device:" + _client_ip())
        abort(401, description=_("Invalid or revoked token — re-pair the device"))
    return device, owner["id"], bool(owner["is_admin"])


def _require_app_admin(is_admin: bool) -> None:
    """403, not 401, for an admin-only App API route reached by a non-admin
    owner: the credential is valid, the account merely lacks the right.
    Distinct from the integration token's 401-on-demotion, whose caller is
    an unattended automation that must not learn whether its token exists;
    here the caller is the account holder's own phone."""
    if not is_admin:
        abort(403, description=_("Admin access required"))


_APP_PLAYLIST_OMITTED_PREFIXES = ("mirror_", "subsonic_mirror_", "jellyfin_mirror_", "emby_mirror_",
                                  "music_assistant_mirror_", "plex_mirror_")


def _playlist_entry_list(conn, playlist_id: int) -> list:
    """A playlist's entries in playlist order, in the App API's shape: each
    with whether it resolved to a track in the library, its entry_id (what
    a PUT names to keep it), its track_id (the library track, or null
    when it matched none or that track has left the library), and that
    track's library_artist and library_album, which name its place in the
    library: a playlist's own artist can be a guest on someone's album. The
    unresolved review stays its own route, since it carries the excluded
    flag."""
    rows = conn.execute(
        "SELECT pt.id AS entry_id, pt.position, pt.artist, pt.title, pt.album, "
        "pt.matched_track_id IS NOT NULL AS matched, t.id AS track_id, "
        "COALESCE(t.album_artist, t.artist) AS library_artist, t.album AS library_album "
        "FROM playlist_tracks pt LEFT JOIN tracks t "
        "ON t.id = pt.matched_track_id AND t.deleted_at IS NULL "
        "WHERE pt.playlist_id = ? ORDER BY pt.position, pt.id",
        (playlist_id,),
    ).fetchall()
    return [{**dict(r), "matched": bool(r["matched"])} for r in rows]


@bp.route("/api/app/whoami")
def api_app_whoami():
    """The App API's smallest route, and the one that proves its boundary:
    which account this device's token acts as, and with what rights. A
    client shows the name; the tests exercise the rest."""
    with db.connection() as conn:
        device, user_id, is_admin = _authenticated_app_user(conn)
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        return jsonify({"user_id": user_id, "username": row["username"], "is_admin": is_admin,
                        "device_id": device["id"], "device_name": device["name"],
                        "app_api": APP_API_VERSION})


@bp.route("/api/app/library/artists")
def api_app_library_artists():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        return jsonify(_library_artists(conn))


@bp.route("/api/app/library/albums")
def api_app_library_albums():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        return jsonify(_library_albums(conn, request.args.get("artist", "")))


@bp.route("/api/app/library/album-tracks")
def api_app_library_album_tracks():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        return jsonify(_library_album_tracks(
            conn, request.args.get("artist", ""), request.args.get("album", "")))


@bp.route("/api/app/library/album-artists")
def api_app_library_album_artists():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        return jsonify(_library_album_artists(conn))


@bp.route("/api/app/library/album-artist-albums")
def api_app_library_album_artist_albums():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        return jsonify(_library_album_artist_albums(conn, request.args.get("artist", "")))


@bp.route("/api/app/library/appears-on")
def api_app_library_appears_on():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        return jsonify(_library_appears_on(conn, request.args.get("artist", "")))


@bp.route("/api/app/library/similar-artists")
def api_app_library_similar_artists():
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return jsonify(_library_similar_artists(conn, user_id, request.args.get("artist", "")))


@bp.route("/api/app/library/cover")
def api_app_library_cover():
    artist = request.args.get("artist", "")
    album = request.args.get("album", "")
    with db.connection() as conn:
        _authenticated_app_user(conn)
        row = _library_cover_row(conn, artist, album)
    return _cover_response(artist, album, row)


@bp.route("/api/app/library/artist-image")
def api_app_library_artist_image():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        provider, audiodb_key = _artist_image_config(conn)
    return _artist_image_response(request.args.get("artist", ""), provider, audiodb_key,
                                  small=request.args.get("size") == "small")


@bp.route("/api/app/basket", methods=["GET", "POST", "DELETE"])
def api_app_basket():
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        if request.method == "POST":
            return jsonify({"id": _basket_add(conn, user_id, request.get_json(force=True))})
        if request.method == "DELETE":
            sync_state.clear_basket(conn, user_id)
            return jsonify({"status": "ok"})
        return jsonify(sync_state.list_basket(conn, user_id))


@bp.route("/api/app/basket/<int:item_id>", methods=["DELETE"])
def api_app_basket_item_delete(item_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        sync_state.remove_basket_item(conn, user_id, item_id)
        return jsonify({"status": "ok"})


@bp.route("/api/app/basket/<int:item_id>/devices/<int:device_id>", methods=["DELETE"])
def api_app_basket_item_device_delete(item_id: int, device_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        sync_state.unstage_basket_item_device(conn, user_id, item_id, device_id)
        return jsonify({"status": "ok"})


@bp.route("/api/app/basket/fan-out", methods=["POST"])
def api_app_basket_fan_out():
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        body = request.get_json(force=True)
        device_ids = [int(d) for d in body.get("device_ids", [])]
        return jsonify(_basket_fan_out(conn, user_id, device_ids))


@bp.route("/api/app/basket/last-destination", methods=["PATCH"])
def api_app_basket_last_destination():
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        body = request.get_json(force=True)
        device_ids = [int(d) for d in body.get("device_ids", [])]
        return jsonify(_basket_set_last_destination(conn, user_id, body["surface"], device_ids))


@bp.route("/api/app/selections")
def api_app_selections():
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return jsonify(_with_titles(conn, _selections_for(conn, user_id)))


def _with_titles(conn, selections: list[dict]) -> list[dict]:
    """Each selection with what a list shows for it: a target is the server's
    key, and for a playlist or a track only an id. Null once that playlist or
    track has gone."""
    ids: dict[str, set[int]] = {"playlist": set(), "track": set()}
    for sel in selections:
        if sel["type"] in ids and sel["target"].isascii() and sel["target"].isdigit():
            ids[sel["type"]].add(int(sel["target"]))
    titles = {}
    for sel_type, table in (("playlist", "playlists"), ("track", "tracks")):
        if ids[sel_type]:
            marks = ",".join("?" * len(ids[sel_type]))
            for row in conn.execute(f"SELECT id, title FROM {table} WHERE id IN ({marks})", tuple(ids[sel_type])):
                titles[(sel_type, str(row["id"]))] = row["title"]
    for sel in selections:
        if sel["type"] == "album":
            sel["title"] = sel["target"].partition("||")[2]
        elif sel["type"] == "artist":
            sel["title"] = sel["target"]
        else:
            sel["title"] = titles.get((sel["type"], sel["target"]))
    return selections


def _selection_on_device(conn, user_id: int) -> tuple[str, str, int]:
    """A body's type, target and device_id, once the owner may manage that
    device. Auto-fit is set in the browser: it is never removed here."""
    body = request.get_json(force=True)
    device_id = int(body["device_id"])
    _require_device_access(conn, user_id, device_id)
    if body["type"] not in VALID_SELECTION_TYPES:
        abort(400, description=_("Unsupported selection type."))
    return body["type"], str(body["target"]), device_id


@bp.route("/api/app/selections/removal-count", methods=["POST"])
def api_app_selections_removal_count():
    """What removing a selection would delete from that device: {"tracks": n}."""
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        sel_type, target, device_id = _selection_on_device(conn, user_id)
        return jsonify({"tracks": sync_state.removal_count(conn, user_id, sel_type, target, device_id)})


@bp.route("/api/app/selections/remove", methods=["POST"])
def api_app_selections_remove():
    """Takes a selection off one device and leaves any others it covers. As the
    browser's Selections table does, it only ever removes the owner's own: a
    409 names whoever else's keeps it on the device."""
    with db.connection() as conn:
        _device, user_id, _admin = _authenticated_app_user(conn)
        sel_type, target, device_id = _selection_on_device(conn, user_id)
        sync_state.toggle_selection_device(conn, user_id, sel_type, target, device_id, False)
        others = sync_state.selected_by_others(conn, user_id, sel_type, target, device_id)
        if others:
            resp = jsonify({"error": _("Selected by %(names)s, so it stays on this device.",
                                       names=", ".join(others)),
                            "selected_by": others})
            resp.status_code = 409
            abort(resp)
        return jsonify({"status": "ok"})


@bp.route("/api/app/devices")
def api_app_devices():
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return jsonify(_device_list(conn, user_id))


@bp.route("/api/app/devices/<int:device_id>/usage")
def api_app_device_usage(device_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return jsonify(_device_usage(conn, user_id, device_id))


@bp.route("/api/app/dashboard/catalog")
def api_app_dashboard_catalog():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        return jsonify(_widget_catalog(APP_DASHBOARD_WIDGET_CATALOG))


@bp.route("/api/app/dashboard/playlists")
def api_app_dashboard_playlists():
    """The Playlists widget: what has most left to sync to this device."""
    with db.connection() as conn:
        _device, user_id, _admin = _authenticated_app_user(conn)
        device_id = request.args.get("device_id", type=int)
        if device_id is None:
            abort(400, description=_("Device not found"))
        _require_device_access(conn, user_id, device_id)
        limit = request.args.get("limit", DEFAULT_PLAYLISTS_WIDGET_LIMIT, type=int)
        return jsonify(_playlists_to_sync(conn, user_id, device_id, max(1, min(limit, PLAYLISTS_WIDGET_LIMIT_RANGE[1]))))


@bp.route("/api/app/dashboard/widgets", methods=["GET", "PATCH"])
def api_app_dashboard_widgets():
    with db.connection() as conn:
        # The requesting device's own set, not the browser's.
        device, user_id, _ = _authenticated_app_user(conn)
        if request.method == "PATCH":
            return jsonify(_apply_dashboard_widgets_patch(
                conn, user_id, request.get_json(force=True, silent=True), device_id=device["id"]))
        return jsonify(_device_dashboard_widgets(conn, device["id"], user_id))


@bp.route("/api/app/library/stats")
def api_app_library_stats():
    with db.connection() as conn:
        _authenticated_app_user(conn)
        return jsonify(_library_stats(conn))


@bp.route("/api/app/suggestions")
def api_app_suggestions():
    with db.connection() as conn:
        device, user_id, _ = _authenticated_app_user(conn)
        period = _validated_period(request.args.get("period", "6month"))
        # Level 7: what is already on the phone asking is not a suggestion for it.
        items, sources = _suggestions_for(conn, user_id, period, {device["id"]})
        return jsonify({"configured": _scrobble_configured(conn, user_id),
                        "items": items, "sources": sources})


@bp.route("/api/app/suggestions/most-played")
def api_app_most_played():
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        period = _validated_period(request.args.get("period", "6month"))
        limit = _most_played_limit(request.args.get("limit"))
        items, sources = _most_played_for(conn, user_id, period, limit)
        return jsonify({"configured": _scrobble_configured(conn, user_id),
                        "items": items, "sources": sources})


@bp.route("/api/app/library/recently-added")
def api_app_library_recently_added():
    with db.connection() as conn:
        device, user_id, _ = _authenticated_app_user(conn)
        return jsonify(_recently_added_for(
            conn, user_id, request.args.get("months", 3, type=int), {device["id"]}))


@bp.route("/api/app/library/recently-released")
def api_app_library_recently_released():
    with db.connection() as conn:
        device, user_id, _ = _authenticated_app_user(conn)
        return jsonify(_recently_released_for(
            conn, user_id, request.args.get("months", 3, type=int), {device["id"]}))


@bp.route("/api/app/profile")
def api_app_profile():
    """The owner's display preferences the library and basket screens
    follow -- list or cover grid, whether to show reissue years -- and
    the devices last chosen per surface, so a picker can preselect them.
    A read of the owner's own row and nothing more: no usernames, no keys,
    no whole-profile write (the phone has the narrow widget PATCH and the
    last-destination PATCH, and needs no other)."""
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        row = conn.execute(
            "SELECT cover_view_mode, show_reissue_year, basket_last_destinations FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return jsonify({
            "cover_view_mode": row["cover_view_mode"] or "list",
            "show_reissue_year": bool(row["show_reissue_year"]),
            "basket_last_destinations": _basket_last_destinations_dict(row["basket_last_destinations"]),
        })


@bp.route("/api/app/playlists", methods=["GET", "POST"])
def api_app_playlists():
    """The browser list without its mirror fields, plus can_share: whether
    this user may change the row's `shared` flag (it has an owner, and the
    user is that owner or an admin), so the app hides a control it could
    not use instead of letting it fail. POST creates a playlist made in
    Trobar, as the browser's create does."""
    with db.connection() as conn:
        _, user_id, is_admin = _authenticated_app_user(conn)
        if request.method == "POST":
            return _create_playlist(conn, user_id)
        rows = []
        for row in _visible_playlists(conn, user_id):
            row = {k: v for k, v in row.items() if not k.startswith(_APP_PLAYLIST_OMITTED_PREFIXES)}
            owner = row["owner_user_id"]
            row["can_share"] = owner is not None and (owner == user_id or bool(is_admin))
            rows.append(row)
        return jsonify(rows)


@bp.route("/api/app/playlists/sync", methods=["POST"])
def api_app_playlists_sync():
    with db.connection() as conn:
        _authenticated_app_user(conn)
    return _start_playlist_sync()


@bp.route("/api/app/playlists/sync/status")
def api_app_playlists_sync_status():
    with db.connection() as conn:
        _device, user_id, is_admin = _authenticated_app_user(conn)
        return jsonify(playlist_sync.sync_status_for(conn, user_id, is_admin))


@bp.route("/api/app/playlists/<int:playlist_id>", methods=["PATCH", "DELETE"])
def api_app_playlist_update(playlist_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        if request.method == "DELETE":
            return _delete_playlist(conn, user_id, playlist_id)
        return _update_playlist(conn, user_id, playlist_id)


@bp.route("/api/app/playlists/<int:playlist_id>/tracks", methods=["GET", "PUT", "POST"])
def api_app_playlist_tracks(playlist_id: int):
    """GET lists a visible playlist's entries (_playlist_entry_list). PUT
    and POST are the browser's writes (_write_playlist_tracks), with the
    entries in the response in this route's own shape."""
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        if request.method == "GET":
            _require_playlist_visible_by_id(conn, user_id, playlist_id)
            return jsonify(_playlist_entry_list(conn, playlist_id))
        return _write_playlist_tracks(conn, user_id, playlist_id, _playlist_entry_list)


@bp.route("/api/app/playlists/<int:playlist_id>/unresolved-tracks")
def api_app_playlist_unresolved_tracks(playlist_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return _unresolved_tracks_of(conn, user_id, playlist_id)


@bp.route("/api/app/playlists/<int:playlist_id>/unresolved-tracks/exclude", methods=["POST"])
def api_app_playlist_unresolved_tracks_exclude(playlist_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return _exclude_unresolved_tracks(conn, user_id, playlist_id)


@bp.route("/api/app/playlists/<int:playlist_id>/lidarr-requests", methods=["POST"])
def api_app_playlist_lidarr_requests(playlist_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return _set_lidarr_requests(conn, user_id, playlist_id)


@bp.route("/api/app/playlist-subscriptions", methods=["GET", "POST"])
def api_app_playlist_subscriptions():
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        if request.method == "POST":
            return _add_subscription(conn, user_id)
        return _own_subscriptions(conn, user_id)


@bp.route("/api/app/playlist-subscriptions/<int:sub_id>/refresh", methods=["POST"])
def api_app_playlist_subscription_refresh(sub_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return _refresh_subscription(conn, user_id, sub_id)


@bp.route("/api/app/playlist-subscriptions/<int:sub_id>", methods=["DELETE"])
def api_app_playlist_subscription_delete(sub_id: int):
    with db.connection() as conn:
        _, user_id, _ = _authenticated_app_user(conn)
        return _delete_subscription(conn, user_id, sub_id)


@bp.route("/api/app/admin/counts")
def api_app_admin_counts():
    with db.connection() as conn:
        _, _, is_admin = _authenticated_app_user(conn)
        _require_app_admin(is_admin)
        return jsonify(_admin_counts(conn))
