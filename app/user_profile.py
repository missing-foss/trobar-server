#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signed-in user's own profile, /api/profile*: settings, dashboard
widget preferences, the avatar, and disconnecting Tidal or Spotify.
Session-authenticated: main's before-request gate covers these paths. Not
`profile.py`, which would shadow the standard library's profiler module.
"""

import json
import logging
import sqlite3

from flask import jsonify, request, send_file, abort, Blueprint
from flask_babel import gettext as _

import credential_store
import db
import device_plays
import play_forward
import settings
import tidal_client
import url_guard
from web_common import (
    ADMIN_ONLY_WIDGETS,
    _apply_dashboard_widgets_patch,
    _basket_last_destinations_dict,
    _contained,
    _dashboard_widgets_dict,
    _gravatar_url,
    _invalid_url_message,
    _is_admin,
    _is_valid_url,
    _normalize_dashboard_widgets,
    _session_user_id,
    _spotify_experimental_enabled,
    get_current_user_id,
)

_log = logging.getLogger(__name__)

bp = Blueprint("user_profile", __name__)

# The web UI's thank-you card: shown to an admin once this many successful
# device syncs have been reported to the server (all accounts together),
# until that admin dismisses it.
THANKS_CARD_SYNCS = 50


# Uploaded local-user avatars — this Authentik instance has no
# AUTHENTIK_AVATARS override, so it's running Authentik's own default
# (gravatar,initials); computing the same Gravatar URL ourselves from the
# forwarded email means an Authentik-authenticated user's picture here
# matches what they'd see inside Authentik itself, with no need to call
# Authentik's own API (which Traefik's ForwardAuth headers don't expose an
# avatar URL for at all — checked the actual middleware config).
AVATAR_DIR = db.DATA_DIR / "avatars"
AVATAR_CONTENT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
AVATAR_MAX_BYTES = 2 * 1024 * 1024


def _unlink_avatar(stored: str) -> None:
    """Deletes a stored avatar, and does nothing at all if the stored value
    does not resolve inside AVATAR_DIR. Separate from the read path because
    an escaping value here would unlink a file outside the directory, which
    is a worse outcome than serving one."""
    path = _contained(AVATAR_DIR, stored)
    if path is not None:
        path.unlink(missing_ok=True)


#: users column -> name in the API and the encryption context.
_SEND_CREDENTIALS = {"maloja_api_key_enc": "maloja_api_key", "listenbrainz_token_enc": "listenbrainz_token"}
_SEND_CREDENTIAL_TARGETS = {"maloja_api_key": "maloja", "listenbrainz_token": "listenbrainz"}
_SEND_CREDENTIAL_MAX = 500


def _profile_dict(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        "SELECT username, lastfm_username, lastfm_api_key, listenbrainz_username, maloja_url, "
        "cover_view_mode, show_reissue_year, dashboard_widgets, basket_last_destinations, "
        "hide_zero_match_playlists, thanks_card_dismissed, device_plays_enabled, "
        "maloja_api_key_enc, listenbrainz_token_enc, lastfm_session_enc, lastfm_session_name, "
        "is_admin, email, avatar_path, tidal_refresh_token, tidal_display_name, tidal_scopes, "
        "spotify_refresh_token, spotify_display_name, "
        "(password_hash IS NOT NULL) AS has_local_password FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    result = dict(row)
    # Whether changing the password must ask for the current one.
    result["has_local_password"] = bool(result["has_local_password"])
    # A real JSON boolean, not the raw 0/1 SQLite stores — same treatment
    # as mirror_enabled/mirror_folder_configured elsewhere in this file.
    result["hide_zero_match_playlists"] = bool(result["hide_zero_match_playlists"])
    result["device_plays_enabled"] = bool(result["device_plays_enabled"])
    result["device_plays_count"] = device_plays.count(conn, user_id)
    result["play_sends"] = play_forward.status(conn, user_id)
    result["lastfm_session_state"] = credential_store.state(result.pop("lastfm_session_enc"), user_id, "lastfm_session")
    result["lastfm_app_configured"] = play_forward.lastfm_app(conn) is not None
    # Their state only: these values never leave the server.
    for column, name in _SEND_CREDENTIALS.items():
        result[f"{name}_state"] = credential_store.state(result.pop(column), user_id, name)
    dismissed = bool(result.pop("thanks_card_dismissed"))
    result["thanks_card"] = (bool(result["is_admin"]) and not dismissed
                             and db.device_syncs_completed(conn) >= THANKS_CARD_SYNCS)
    result["dashboard_widgets"] = _dashboard_widgets_dict(result["dashboard_widgets"])
    result["basket_last_destinations"] = _basket_last_destinations_dict(
        result["basket_last_destinations"])
    avatar_path = result.pop("avatar_path")
    email = result.pop("email")
    # The refresh token itself is never sent to the frontend — connected
    # status + display name is all the UI needs (see /profile/tidal/* below).
    result["tidal_connected"] = result.pop("tidal_refresh_token") is not None
    # Sharing needs playlists.write, which Connect requests only once the
    # admin has enabled it; a link made before then is read-only until the
    # user reconnects.
    write_ok = tidal_client.has_write_scope(result.pop("tidal_scopes"))
    sharing = db.get_config(conn, "tidal_playlists_write") == "1"
    result["tidal_can_share"] = sharing and result["tidal_connected"] and write_ok
    result["tidal_needs_reconnect"] = sharing and result["tidal_connected"] and not write_ok
    result["spotify_connected"] = result.pop("spotify_refresh_token") is not None
    # #398: Spotify is experimental (unverified against a live account,
    # #146) and off by default -- the frontend uses this to hide the whole
    # "Connect Spotify" block rather than just leaving it there advertising
    # a feature the routes below will refuse.
    result["spotify_experimental_enabled"] = db.get_config(conn, "experimental_spotify_enabled") == "1"
    # See get_current_user_id: a local session (AUTH_MODE=local, or the
    # admin's break-glass login) always takes precedence over Authentik
    # headers when both are present, so this is the same check that decides
    # *who* the current user is, reused here for the upload-availability note.
    is_local_session = _session_user_id(conn) is not None
    result["is_local_session"] = is_local_session
    # #235: the web UI needs this to decide whether/where to show a local-
    # password form at all (Profile → Account under local/forward, moved to
    # Administration and admin-only under oidc, replaced by an "account is
    # managed externally" notice for non-admins under oidc) — not a secret,
    # just the deployment's auth mode and (when relevant) its IdP's own URL.
    result["auth_mode"] = settings.AUTH_MODE
    result["oidc_issuer"] = settings.OIDC_ISSUER if settings.AUTH_MODE == "oidc" and settings.OIDC_ISSUER else None
    # An uploaded picture wins regardless of session type (checked: Authentik
    # itself has no self-service avatar upload at all in this version —
    # user.avatar is a read-only field — so this is the only way an
    # Authentik-authenticated household member gets anything other than
    # Gravatar without an admin hand-editing their Authentik attributes).
    # Authentik sessions without an upload fall back to Gravatar, matching
    # Authentik's own default avatar mode for this instance; a local session
    # without an upload has no such fallback identity to compute one from.
    if avatar_path:
        result["avatar_url"] = "/api/profile/avatar-image"
    elif not is_local_session:
        result["avatar_url"] = _gravatar_url(email)
    else:
        result["avatar_url"] = None
    return result


def _blocked_url_message(kind: str, field_label: str) -> str:
    """Why url_guard refused a user-settable URL, per kind of address,
    with the field named as _invalid_url_message() names it."""
    reasons = {
        "loopback": _("Loopback addresses, which lead back to the Trobar server itself, aren't allowed."),
        "link-local": _("Link-local addresses aren't allowed."),
        "unspecified": _("The unspecified address (0.0.0.0 or ::) isn't allowed."),
        "multicast": _("Multicast addresses aren't allowed."),
    }
    return f"{reasons[kind]} ({field_label})"


@bp.route("/api/profile", methods=["GET", "PUT"])
def api_profile():
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        if request.method == "PUT":
            body = request.get_json(force=True)
            widgets = _normalize_dashboard_widgets(body.get("dashboard_widgets"))
            if not _is_admin(conn, user_id):
                # A non-admin's own request can only ever ask to hide an
                # admin-only widget further, never un-hide one — see
                # ADMIN_ONLY_WIDGETS.
                widgets["disabled"] = sorted(set(widgets["disabled"]) | ADMIN_ONLY_WIDGETS)
            maloja_url = (body.get("maloja_url") or "").strip() or None
            if maloja_url is not None:
                error: str | None
                if not _is_valid_url(maloja_url):
                    error = _invalid_url_message("Maloja URL")
                else:
                    blocked = url_guard.check_url(maloja_url)
                    error = _blocked_url_message(blocked, "Maloja URL") if blocked else None
                if error:
                    resp = jsonify({"error": error, "field": "maloja_url"})
                    resp.status_code = 400
                    abort(resp)
            conn.execute(
                "UPDATE users SET lastfm_username = ?, lastfm_api_key = ?, listenbrainz_username = ?, "
                "maloja_url = ?, "
                "cover_view_mode = ?, show_reissue_year = ?, dashboard_widgets = ?, "
                "hide_zero_match_playlists = ? WHERE id = ?",
                (body.get("lastfm_username") or None, body.get("lastfm_api_key") or None,
                 body.get("listenbrainz_username") or None, maloja_url,
                 body.get("cover_view_mode", "list"), 1 if body.get("show_reissue_year") else 0,
                 json.dumps(widgets), 1 if body.get("hide_zero_match_playlists") else 0, user_id),
            )
            conn.commit()
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/send-credentials", methods=["PUT"])
def api_profile_send_credentials():
    """The caller's own credentials for sending plays, stored encrypted. A
    field left out stays as it is; an empty one is removed. They are never
    returned, only whether each is set. Returns the profile."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            abort(400, description=_("The request needs a JSON object."))
        given = {column: body[name] for column, name in _SEND_CREDENTIALS.items() if name in body}
        if not all(isinstance(v, str) and len(v) <= _SEND_CREDENTIAL_MAX for v in given.values()):
            abort(400, description=_("That value can't be used."))
        for column, value in given.items():
            name, value = _SEND_CREDENTIALS[column], value.strip()
            try:
                stored = credential_store.encrypt(value, user_id, name) if value else None
            except credential_store.CredentialKeyError as e:
                conn.rollback()
                _log.error("Can't store a credential: %s", e)
                abort(503, description=_("The server's credential key can't be used."))
            conn.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (stored, user_id))
            if stored is None:
                play_forward.set_enabled(conn, user_id, _SEND_CREDENTIAL_TARGETS[name], False, commit=False)
        conn.commit()
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/lastfm-session", methods=["DELETE"])
def api_profile_lastfm_disconnect():
    """Forgets the caller's Last.fm session, which turns its send switch off."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        conn.execute("UPDATE users SET lastfm_session_enc = NULL, lastfm_session_name = NULL WHERE id = ?",
                     (user_id,))
        play_forward.set_enabled(conn, user_id, "lastfm", False)
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/play-sends", methods=["PUT"])
def api_profile_play_sends():
    """One target's send switch, the caller's own: {"target", "enabled"}. On
    needs its credential. From then on, new plays go; old ones never do.
    Returns the profile."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True, silent=True)
        body = body if isinstance(body, dict) else {}
        target, enabled = body.get("target"), body.get("enabled")
        if target not in play_forward.TARGETS or not isinstance(enabled, bool):
            abort(400, description=_("That value can't be used."))
        if enabled and not play_forward.status(conn, user_id)[target]["ready"]:
            abort(409, description=_("Add its details under Scrobbler accounts first."))
        play_forward.set_enabled(conn, user_id, target, enabled)
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/device-plays", methods=["PUT", "DELETE"])
def api_profile_device_plays():
    """The caller's own play history from devices. PUT {"enabled": bool} turns
    keeping it on or off; off keeps what is stored. DELETE deletes what is
    stored and leaves the switch alone. Returns the profile."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        if request.method == "DELETE":
            device_plays.clear(conn, user_id)
        else:
            body = request.get_json(force=True, silent=True)
            enabled = body.get("enabled") if isinstance(body, dict) else None
            if not isinstance(enabled, bool):
                abort(400, description=_("enabled must be true or false."))
            device_plays.set_enabled(conn, user_id, enabled)
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/thanks-card", methods=["DELETE"])
def api_profile_thanks_card():
    """Dismiss the thank-you card for good, for this user only: either of
    its buttons sends this. Returns the profile, now without the card."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        conn.execute("UPDATE users SET thanks_card_dismissed = 1 WHERE id = ?", (user_id,))
        conn.commit()
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/dashboard-widgets", methods=["PATCH"])
def api_profile_dashboard_widgets():
    """Change only the dashboard widget preferences, merging into what is
    stored. The whole-profile PUT above sends every field the client
    holds, which is last-write-wins across two clients: a phone toggling a
    widget would overwrite a scrobble username the browser had just set.
    This route touches nothing but users.dashboard_widgets, so the two
    can interleave safely.

    Merge semantics: `disabled` and `order` replace the stored list when
    present; `settings` merges key by key, so a months change leaves the
    cover limit alone. The admin-only union is applied after the merge,
    exactly as the PUT does. Returns the stored, normalized preferences."""
    with db.connection() as conn:
        return jsonify(_apply_dashboard_widgets_patch(conn, get_current_user_id(conn), request.get_json(force=True, silent=True)))


@bp.route("/api/profile/tidal", methods=["DELETE"])
def api_profile_tidal_disconnect():
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        conn.execute(
            "UPDATE users SET tidal_refresh_token = NULL, tidal_user_id = NULL, tidal_display_name = NULL, "
            "tidal_scopes = NULL WHERE id = ?",
            (user_id,),
        )
        conn.commit()
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/spotify", methods=["DELETE"])
def api_profile_spotify_disconnect():
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        # #398: same toggle as connect/callback -- the Profile panel this
        # button lives on is hidden entirely when the feature is off, so
        # reaching here means a stale bookmark/direct call, not a real
        # in-UI action. Refusing keeps the "off" state honest rather than
        # leaving one route quietly still live.
        if not _spotify_experimental_enabled(conn):
            abort(400, description=_("Spotify is an experimental feature and isn't enabled on this server."))
        conn.execute(
            "UPDATE users SET spotify_refresh_token = NULL, spotify_user_id = NULL, spotify_display_name = NULL "
            "WHERE id = ?",
            (user_id,),
        )
        conn.commit()
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/avatar", methods=["POST", "DELETE"])
def api_profile_avatar():
    """Available regardless of session type (follow-up) — Authentik
    itself has no self-service avatar upload in this version at all (its
    `avatar` API field is read-only), so this is the only way an Authentik-
    authenticated household member gets a custom picture without an admin
    hand-editing their Authentik attributes. An upload here always takes
    priority over the Gravatar fallback; see _profile_dict."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        row = conn.execute("SELECT avatar_path FROM users WHERE id = ?", (user_id,)).fetchone()

        if request.method == "DELETE":
            if row["avatar_path"]:
                _unlink_avatar(row["avatar_path"])
            conn.execute("UPDATE users SET avatar_path = NULL WHERE id = ?", (user_id,))
            conn.commit()
            return jsonify(_profile_dict(conn, user_id))

        file = request.files.get("avatar")
        if file is None or not file.filename:
            return jsonify({"error": _("No file received.")}), 400
        ext = AVATAR_CONTENT_TYPES.get(file.mimetype)
        if ext is None:
            return jsonify({"error": _("Unsupported format (JPEG, PNG, or WebP only).")}), 400
        data = file.read(AVATAR_MAX_BYTES + 1)
        if len(data) > AVATAR_MAX_BYTES:
            return jsonify({"error": _("Image too large (2 MB max).")}), 400

        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        if row["avatar_path"]:
            _unlink_avatar(row["avatar_path"])
        filename = f"{user_id}.{ext}"
        (AVATAR_DIR / filename).write_bytes(data)
        conn.execute("UPDATE users SET avatar_path = ? WHERE id = ?", (filename, user_id))
        conn.commit()
        return jsonify(_profile_dict(conn, user_id))


@bp.route("/api/profile/avatar-image")
def api_profile_avatar_image():
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        row = conn.execute("SELECT avatar_path FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row["avatar_path"]:
        abort(404)
    path = _contained(AVATAR_DIR, row["avatar_path"])
    if path is None or not path.exists():
        abort(404)
    content_type = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(path.suffix.lstrip("."))
    return send_file(path, mimetype=content_type)
