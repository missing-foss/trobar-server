#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playlists on the web, /api/provider/* and /api/playlist-subscriptions*:
the provider's status and pairing, listing, editing and syncing playlists,
mirrors, unresolved tracks and Lidarr requests, and the public playlists
followed by link. Session-authenticated: main's before-request gate covers
these paths.
"""

import json
import logging
import posixpath
from urllib.parse import quote

from flask import jsonify, request, abort, Response, Blueprint
from flask_babel import gettext as _

import db
import filesystem_client
import gaps
import mirror_sinks
import playlist_sync
import rule_playlists
import sync_state
from web_common import (
    _DEVICE_PLAYLIST_ENTRIES_MAX,
    _active_provider,
    _add_subscription,
    _create_playlist,
    _delete_playlist,
    _delete_subscription,
    _exclude_unresolved_tracks,
    _is_admin,
    _mirror_coverage,
    _mirror_overwritten,
    _playlist_title,
    _own_subscriptions,
    _refresh_subscription,
    _require_playlist_visible_by_id,
    _rule_playlist_for_edit,
    _set_lidarr_requests,
    _start_playlist_sync,
    _unresolved_tracks_of,
    _update_playlist,
    _visible_playlists,
    _write_playlist_tracks,
    get_current_user_id,
    require_admin,
)

_log = logging.getLogger(__name__)

bp = Blueprint("playlists", __name__)


def _playlist_entry_rows(conn, playlist_id: int) -> list[dict]:
    """A playlist's entries in order, as the tracks route returns them: the
    library track's tags where the entry matches a track still in the
    library, otherwise the entry's own artist and title with a null
    track_id. entry_id names the entry for a write that keeps it."""
    rows = conn.execute(
        "SELECT pt.id AS entry_id, t.id AS track_id, COALESCE(t.artist, pt.artist) AS artist, "
        "COALESCE(t.title, pt.title) AS title, COALESCE(t.album, pt.album) AS album, t.duration "
        "FROM playlist_tracks pt LEFT JOIN tracks t "
        "ON t.id = pt.matched_track_id AND t.deleted_at IS NULL "
        "WHERE pt.playlist_id = ? ORDER BY pt.position", (playlist_id,)).fetchall()
    return [dict(r) for r in rows]


_UPLOAD_NAME_MAX = 200


@bp.route("/api/provider/status")
def api_provider_status():
    with db.connection() as conn:
        return jsonify(_active_provider(conn).status())


@bp.route("/api/provider/pair", methods=["POST"])
def api_provider_pair():
    # Admin-only (#378): re-pairing is configuration, like the rest of the
    # provider settings. Status stays readable by everyone (the header shows it).
    with db.connection() as conn:
        require_admin(conn)
        return jsonify(_active_provider(conn).retry_pairing())


@bp.route("/api/provider/playlists/sync", methods=["POST"])
def api_provider_playlists_sync():
    return _start_playlist_sync()


@bp.route("/api/provider/playlists/sync/status")
def api_provider_playlists_sync_status():
    # #138: running/idle + the last completed run's counts, polled by the UI.
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        return jsonify(playlist_sync.sync_status_for(conn, user_id, _is_admin(conn, user_id)))


@bp.route("/api/provider/playlists")
def api_provider_playlists():
    """#28: an owned-and-unshared playlist is invisible to everyone but its
    owner and the admin — same "the admin is fully trusted" precedent as
    everywhere else in this app (see SECURITY.md). An unowned playlist
    (owner_user_id IS NULL — every Subsonic/Jellyfin/filesystem/primary-
    Roon-connection playlist, same as before this feature existed) is
    never filtered; `shared` only has an effect once a playlist has an
    owner.

    #81 (golden-source, per-viewer attribution): a Roon row can be
    "dual-source" — the same playlist is also reachable via the owner's
    directly-linked Tidal account (its golden_source_id points at that
    Tidal row). For such a row, attribution is relative to the viewer:
    - if the viewer can already SEE the golden Tidal row (they own it, or
      it's shared, or they're admin), suppress the Roon duplicate — they
      see the Tidal golden copy instead;
    - otherwise keep the Roon row and decorate it with the golden owner's
      name ("shared by X"), since it legitimately still reaches them via
      the shared Roon connection.
    The Roon row stays unowned for enforcement (never hidden by #28); this
    is display-only. A Tidal-only playlist has no Roon row, so it keeps its
    #28 privacy untouched."""
    with db.connection() as conn:
        return jsonify(_visible_playlists(conn, get_current_user_id(conn)))


@bp.route("/api/provider/playlists/<int:playlist_id>", methods=["PATCH"])
def api_provider_playlist_update(playlist_id: int):
    """The `shared` toggle, owner or admin only (#28's own proposed
    design), and `title`, which renames a playlist made in Trobar (owner or
    admin, see _trobar_playlist_for_edit). A playlist from a provider is
    that provider's data: it isn't renamed or reordered here."""
    with db.connection() as conn:
        return _update_playlist(conn, get_current_user_id(conn), playlist_id)


@bp.route("/api/provider/playlists", methods=["POST"])
def api_playlist_create():
    """A new, empty playlist made in Trobar, owned by the caller and
    private (playlist_sync.create_trobar_playlist). Body: {"title"}."""
    with db.connection() as conn:
        return _create_playlist(conn, get_current_user_id(conn))


def _rule_body() -> dict:
    """The request's JSON object, empty when it sent anything else."""
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


def _rule_of(body: dict) -> dict:
    """The `rule` of a request body, normalised, or a 400 saying what is wrong."""
    try:
        return rule_playlists.validate(body.get("rule"))
    except rule_playlists.RuleError as e:
        abort(400, description=str(e))


@bp.route("/api/provider/playlists/rule/count", methods=["POST"])
def api_rule_playlist_count():
    """The live count of the rule dialog, nothing saved. Body: {"rule"}.
    Answers {"matches", "tracks"} (rule_playlists.preview)."""
    rule = _rule_of(_rule_body())
    with db.connection() as conn:
        return jsonify(rule_playlists.preview(conn, rule))


@bp.route("/api/provider/playlists/rule", methods=["POST"])
def api_rule_playlist_create():
    """A new rule playlist, owned by the caller, private, and filled at once.
    Body: {"title", "rule"}."""
    body = _rule_body()
    title, rule = _playlist_title(body), _rule_of(body)
    with db.connection() as conn:
        playlist_id = rule_playlists.create(conn, get_current_user_id(conn), title, rule)
        return jsonify({"id": playlist_id, "title": title}), 201


@bp.route("/api/provider/playlists/<int:playlist_id>/rule", methods=["GET", "PUT"])
def api_rule_playlist(playlist_id: int):
    """A rule playlist's title and rule, owner or admin. PUT replaces both,
    body {"title", "rule"}, and refills the playlist at once. Its tracks
    can't be written any other way: the next sync would undo it."""
    with db.connection() as conn:
        row = _rule_playlist_for_edit(conn, get_current_user_id(conn), playlist_id)
        if request.method == "GET":
            return jsonify({"title": row["title"], "rule": json.loads(row["rule"])})
        body = _rule_body()
        title, rule = _playlist_title(body), _rule_of(body)
        return jsonify({"status": "ok", "tracks": rule_playlists.update(conn, playlist_id, title, rule)})


@bp.route("/api/provider/playlists/<int:playlist_id>/tracks", methods=["GET", "PUT", "POST"])
def api_playlist_tracks(playlist_id: int):
    """The entries of a playlist. GET lists any visible playlist's entries
    in order: {"tracks": [{"entry_id", "track_id", "artist", "title",
    "album", "duration"}]} (_playlist_entry_rows).

    Writes are for a playlist made in Trobar only, owner or admin. PUT
    replaces its entries, in order: adding, removing and reordering are all
    one write. Its body is {"items": [{"track_id": n} or {"entry_id": n}]},
    where an entry_id keeps that entry as it is, so a track since removed
    from the library stays in place; or {"track_ids": [...]}. POST appends
    {"track_ids": [...]}, or {"albums": [{"artist", "album"}, ...]} for
    whole albums from the Library in their own order (_album_track_ids),
    keeping every existing entry. Every new track id must name a library
    track, and the total is capped at playlist_sync.TROBAR_TRACKS_MAX.
    Writes return the entries as they now are, with fresh entry ids."""
    with db.connection() as conn:
        if request.method == "GET":
            _require_playlist_visible_by_id(conn, get_current_user_id(conn), playlist_id)
            return jsonify({"tracks": _playlist_entry_rows(conn, playlist_id)})
        return _write_playlist_tracks(conn, get_current_user_id(conn), playlist_id, _playlist_entry_rows)


@bp.route("/api/provider/playlists/<int:playlist_id>", methods=["DELETE"])
def api_playlist_delete(playlist_id: int):
    """Delete a playlist made in Trobar, uploaded as a file (#287) or made
    from a rule, owner or admin. Devices it was sent to are told, and its mirrors go with it
    (playlist_sync.delete_trobar_playlist)."""
    with db.connection() as conn:
        return _delete_playlist(conn, get_current_user_id(conn), playlist_id, also_uploads=True)


@bp.route("/api/playlist-subscriptions")
def api_playlist_subscriptions():
    """This user's own public-playlist URL subscriptions.

    Own only, admin included -- an admin can already see every playlist a
    subscription produces through the playlists list, and listing other
    people's subscribed URLs here would add a second, less obvious place
    for one household member's tastes to leak to another."""
    with db.connection() as conn:
        return _own_subscriptions(conn, get_current_user_id(conn))


@bp.route("/api/playlist-subscriptions", methods=["POST"])
def api_playlist_subscriptions_add():
    """Subscribe to a public playlist by URL, and import it immediately.

    Any logged-in household member, not admin-only: this stores no
    credential, reaches no configured server, and produces a playlist owned
    by and private to whoever added it -- the same footing a personally
    linked Tidal account is on.

    The import runs synchronously rather than waiting for the next
    playlist sync, because the counts it returns are the answer to "did
    this work". A playlist of hour-long mix videos parses perfectly and
    matches nothing, and the URL of one is indistinguishable from the URL
    of a good playlist -- so 0 of 79 has to come back while the user is
    still looking at the box they pasted into. Re-adding a URL already
    subscribed to refreshes it rather than failing or duplicating: the
    same playlist has several URL spellings, and the user's intent is the
    same either way."""
    with db.connection() as conn:
        return _add_subscription(conn, get_current_user_id(conn))


@bp.route("/api/playlist-subscriptions/<int:sub_id>/refresh", methods=["POST"])
def api_playlist_subscription_refresh(sub_id: int):
    """Re-fetch one subscription now. A remote playlist that came back
    after being unavailable has no other way to clear its error state
    before the next playlist sync."""
    with db.connection() as conn:
        return _refresh_subscription(conn, get_current_user_id(conn), sub_id)


@bp.route("/api/playlist-subscriptions/<int:sub_id>", methods=["DELETE"])
def api_playlist_subscription_delete(sub_id: int):
    """Unsubscribe, and remove the playlist it produced.

    Deliberately unlike a failed fetch, which leaves everything alone: this
    is the user asking for it to go, so the playlist goes through the same
    removal path a source-side deletion would -- devices told to remove the
    files, mirrors cleaned up -- rather than being left to be pruned
    silently on some later sync."""
    with db.connection() as conn:
        return _delete_subscription(conn, get_current_user_id(conn), sub_id)


@bp.route("/api/provider/playlists/upload", methods=["POST"])
def api_playlist_upload():
    """A .m3u/.m3u8 file the user uploads becomes one of their playlists
    (playlist_sync.import_uploaded_playlist): private, matched like any
    playlist, and replaced by a later upload of a file with the same name.
    Multipart, field "file". The request size limit (MAX_CONTENT_LENGTH)
    bounds the file, and a playlist made on a device's entry limit bounds
    its entries."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": _("No file received.")}), 400
        name = posixpath.basename(file.filename.replace("\\", "/")).strip()
        if not name.lower().endswith((".m3u", ".m3u8")) or len(name) > _UPLOAD_NAME_MAX:
            return jsonify({"error": _("Choose a .m3u or .m3u8 file.")}), 400
        tracks = filesystem_client.uploaded_playlist_tracks(file.read(), name)
        if not tracks:
            return jsonify({"error": _("That file lists no tracks.")}), 400
        if len(tracks) > _DEVICE_PLAYLIST_ENTRIES_MAX:
            return jsonify({"error": _("A playlist can hold at most %(n)d entries.",
                                       n=_DEVICE_PLAYLIST_ENTRIES_MAX)}), 400
        return jsonify(playlist_sync.import_uploaded_playlist(conn, user_id, name, tracks))


@bp.route("/api/provider/playlists/<int:playlist_id>/export.m3u8")
def api_playlist_export(playlist_id: int):
    """Any playlist the user can see, as an .m3u8 download
    (playlist_sync.export_m3u8): matched tracks only, or with
    ?unresolved=1 the unmatched entries too, as comments."""
    with db.connection() as conn:
        _require_playlist_visible_by_id(conn, get_current_user_id(conn), playlist_id)
        title = conn.execute("SELECT title FROM playlists WHERE id = ?", (playlist_id,)).fetchone()["title"]
        body = playlist_sync.export_m3u8(conn, playlist_id, request.args.get("unresolved") == "1")
    name = (title or "playlist").replace("/", "-").replace("\\", "-").strip() or "playlist"
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace("?", "_").replace('"', "_")
    return Response(body, mimetype="audio/x-mpegurl", headers={
        "Content-Disposition":
            f"attachment; filename=\"{ascii_name}.m3u8\"; filename*=UTF-8''{quote(name)}.m3u8"})


@bp.route("/api/provider/playlists/<int:playlist_id>/unresolved-tracks")
def api_playlist_unresolved_tracks(playlist_id: int):
    """#200: this playlist's unresolved entries (identity.py's resolver
    missed every tier) for the web review list — same visibility as the
    playlist itself."""
    with db.connection() as conn:
        return _unresolved_tracks_of(conn, get_current_user_id(conn), playlist_id)


@bp.route("/api/provider/playlists/<int:playlist_id>/unresolved-tracks/exclude", methods=["POST"])
def api_playlist_unresolved_tracks_exclude(playlist_id: int):
    """#200: {"ids": [...], "excluded": true|false} — acknowledge the given
    unresolved_playlist_tracks rows as "not actually a gap" (e.g. a
    provider-only track that's streamed but never downloaded, so it will
    never resolve locally) so they stop being flagged, or un-exclude them.
    Returns the resulting non-excluded count for this playlist. Excluding
    doesn't change matching itself — it's purely a review-list
    acknowledgment, same relationship set_device_unknown_adopted's
    `adopted` has to that other review surface."""
    with db.connection() as conn:
        return _exclude_unresolved_tracks(conn, get_current_user_id(conn), playlist_id)


@bp.route("/api/provider/playlists/<int:playlist_id>/unresolved-tracks/accept", methods=["POST"])
def api_playlist_unresolved_tracks_accept(playlist_id: int):
    """{"ids": [...], "accepted": true|false}: accept (or take back) the
    file-name guesses of the given review rows of a playlist made on a
    device. An accepted guess is matched, from now on and at once: the
    entry gets its suggested track and the playlist's mirrors are
    rewritten. Accepting changes what the playlist sends, so it is for the
    playlist's owner or an admin, not anyone who can see it. Returns the
    ids changed and the playlist's remaining unresolved count."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_playlist_visible_by_id(conn, user_id, playlist_id)
        owner = conn.execute("SELECT owner_user_id FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if owner["owner_user_id"] != user_id and not _is_admin(conn, user_id):
            abort(403, description=_("Only this playlist's owner can accept a guess."))
        body = request.get_json(force=True, silent=True) or {}
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            abort(400, description=_("ids must be a list of unresolved-track row ids."))
        changed = sync_state.set_unresolved_playlist_tracks_accepted(
            conn, playlist_id, ids, bool(body.get("accepted", True)))
        if changed:
            sync_state.recompute_playlist_devices(conn, playlist_id)
        playlist_sync.write_playlist_mirrors(conn, playlist_id)
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM unresolved_playlist_tracks WHERE playlist_id = ? AND excluded = 0 "
            "AND accepted = 0", (playlist_id,)).fetchone()[0]
        return jsonify({"changed": changed, "unresolved_count": count})


@bp.route("/api/provider/playlists/gaps")
def api_playlist_gaps():
    """The Gaps view: the unresolved tracks of every playlist the caller can
    see (the playlist list's own rule), grouped by album, each with the
    playlists it is in and what Lidarr did with it. Query: `owner` (a
    username, or empty for unowned playlists), `source` (a provider), and
    `show_excluded=1` to list excluded tracks too. `open_count` ignores the
    filters: it is the tab's count of open gaps."""
    with db.connection() as conn:
        playlists = _visible_playlists(conn, get_current_user_id(conn))
        owner = request.args.get("owner")
        source = request.args.get("source") or None
        out = gaps.list_gaps(conn, playlists, owner=owner, source=source,
                             show_excluded=request.args.get("show_excluded") == "1")
        out["lidarr_configured"] = db.get_lidarr_config() is not None
        return jsonify(out)


@bp.route("/api/provider/playlists/gaps/exclude", methods=["POST"])
def api_playlist_gaps_exclude():
    """{"ids": [...], "excluded": true|false}: exclude (or un-exclude)
    unresolved tracks across playlists, as each playlist's own review does.
    Rows of a playlist the caller can't see are left alone. Returns how
    many changed."""
    with db.connection() as conn:
        playlists = _visible_playlists(conn, get_current_user_id(conn))
        body = request.get_json(force=True, silent=True) or {}
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            abort(400, description=_("ids must be a list of unresolved-track row ids."))
        changed = gaps.set_excluded(conn, playlists, ids, bool(body.get("excluded", True)))
        conn.commit()
        return jsonify({"changed": changed})


@bp.route("/api/provider/playlists/gaps/request", methods=["POST"])
def api_playlist_gaps_request():
    """{"albums": [{"artist", "album"}, ...]}: ask Lidarr for these albums,
    through the same job and dedupe as a playlist's "Request missing
    albums". Like that switch, open to anyone who can see the playlists the
    albums are missing from, and only once an admin has set Lidarr up. An
    album that isn't an open gap of a playlist the caller can see is
    dropped. Returns how many albums were queued."""
    with db.connection() as conn:
        if db.get_lidarr_config() is None:
            abort(409, description=_("Lidarr isn't set up."))
        playlists = _visible_playlists(conn, get_current_user_id(conn))
        body = request.get_json(force=True, silent=True) or {}
        albums = body.get("albums")
        if not isinstance(albums, list) or not all(
                isinstance(a, dict) and isinstance(a.get("artist"), str) and isinstance(a.get("album"), str)
                for a in albums):
            abort(400, description=_("albums must be a list of {artist, album}."))
        return jsonify(gaps.request_albums(conn, playlists, [(a["artist"], a["album"]) for a in albums]))


@bp.route("/api/provider/playlists/<int:playlist_id>/mirror", methods=["POST"])
def api_playlist_mirror_toggle(playlist_id: int):
    """#285/#189: {"enabled": true|false, "sink": "filesystem"|"subsonic"|
    "jellyfin"|"emby"|"music_assistant"|"plex"} — toggle this playlist's mirror to one sink. `sink`
    defaults to "filesystem" (the shape this route originally had, before
    #189 added more sinks) so an old cached frontend mid-deploy keeps
    working. Same #28 visibility as the unresolved-tracks routes (any user
    who can see the playlist, not owner/admin-gated — the issue explicitly
    calls this "not admin-only"). Writes/deletes the mirror immediately
    (the sink module's own write_mirror/delete_mirror), rather than
    waiting for the next playlist sync, so the toggle has visible effect
    right away. Returns the refreshed row for EVERY sink (not just the one
    toggled) so the UI can update its whole mirror-status display without
    a separate reload."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        _require_playlist_visible_by_id(conn, user_id, playlist_id)
        body = request.get_json(force=True)
        if "enabled" not in body:
            abort(400, description=_("Nothing to update."))
        enabled = bool(body["enabled"])
        sink = body.get("sink", "filesystem")
        # Not a dict lookup on the raw value: a list or dict body would raise
        # TypeError there, a 500 instead of this 400.
        if not isinstance(sink, str) or sink not in mirror_sinks.BY_KEY:
            abort(400, description=_("Unknown mirror sink."))

        target = mirror_sinks.BY_KEY[sink]
        # Waits for a write in progress, so switching off deletes the copy
        # that write made.
        with mirror_sinks.playlist_lock(playlist_id):
            conn.execute(
                f"UPDATE playlists SET {target.enabled_column} = ? WHERE id = ?",
                (1 if enabled else 0, playlist_id),
            )
            conn.commit()  # the sink calls out, and must not hold the write lock
            (target.backend.write_mirror if enabled else target.backend.delete_mirror)(conn, playlist_id)
            conn.commit()

        row = conn.execute(
            "SELECT mirror_enabled, mirror_filename, mirror_last_written_at, "
            "mirror_last_error, mirror_last_error_code, "
            "subsonic_mirror_enabled, subsonic_mirror_remote_id, "
            "subsonic_mirror_last_written_at, subsonic_mirror_last_error, "
            "subsonic_mirror_last_error_code, "
            "jellyfin_mirror_enabled, jellyfin_mirror_remote_id, "
            "jellyfin_mirror_last_written_at, jellyfin_mirror_last_error, "
            "jellyfin_mirror_last_error_code, "
            "emby_mirror_enabled, emby_mirror_remote_id, "
            "emby_mirror_last_written_at, emby_mirror_last_error, "
            "emby_mirror_last_error_code, "
            "music_assistant_mirror_enabled, music_assistant_mirror_remote_id, "
            "music_assistant_mirror_last_written_at, music_assistant_mirror_last_error, "
            "music_assistant_mirror_last_error_code, "
            "plex_mirror_enabled, plex_mirror_remote_id, plex_mirror_last_written_at, "
            "plex_mirror_last_error, plex_mirror_last_error_code "
            "FROM playlists WHERE id = ?", (playlist_id,),
        ).fetchone()
        return jsonify({
            **dict(row),
            "mirror_enabled": bool(row["mirror_enabled"]),
            "subsonic_mirror_enabled": bool(row["subsonic_mirror_enabled"]),
            "jellyfin_mirror_enabled": bool(row["jellyfin_mirror_enabled"]),
            "emby_mirror_enabled": bool(row["emby_mirror_enabled"]),
            "music_assistant_mirror_enabled": bool(row["music_assistant_mirror_enabled"]),
            "plex_mirror_enabled": bool(row["plex_mirror_enabled"]),
            "mirror_coverage": _mirror_coverage(conn, [playlist_id]).get(playlist_id, {}),
            "mirror_overwritten": (
                _mirror_overwritten(conn, [playlist_id]).get(playlist_id, {})
                if _owner_or_admin(conn, user_id, playlist_id) else {}),
        })


def _owner_or_admin(conn, user_id: int, playlist_id: int) -> bool:
    row = conn.execute("SELECT owner_user_id FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    return (row is not None and row["owner_user_id"] == user_id) or _is_admin(conn, user_id)


@bp.route("/api/provider/playlists/<int:playlist_id>/lidarr-requests", methods=["POST"])
def api_playlist_lidarr_requests_toggle(playlist_id: int):
    """#494: {"enabled": true|false}. A separate route from
    api_playlist_mirror_toggle above, deliberately — this isn't a mirror
    (nothing is copied anywhere), and that route's own `sink` values
    enumerate mirror DESTINATIONS; a "lidarr" sink there would misdescribe
    the feature. Same #28 visibility as the mirror toggle (any user who
    can see the playlist, not owner/admin-gated). Queues a run of
    lidarr_requests on enable, so it starts at once rather than at the next
    playlist sync, and answers without waiting for it
    (`lidarr_request_queued`); the outcome shows in the playlist's
    lidarr_request_last_* fields once the job has run. Nothing to do on disable: by settled
    design, un-ticking stops FUTURE requests only and never un-monitors or
    removes anything Lidarr already has — there is no delete_mirror()
    equivalent to call here."""
    with db.connection() as conn:
        return _set_lidarr_requests(conn, get_current_user_id(conn), playlist_id)
