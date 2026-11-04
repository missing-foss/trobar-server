#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signing in and setting up: /login, /logout, OIDC (/oidc/*), the setup
wizard (/setup, /api/setup/*), device enrollment (/api/enrollment/*), the
password form (/api/account/password), and connecting Tidal, Spotify and
Last.fm (/profile/tidal/*, /profile/spotify/*, /profile/lastfm/*). main's before-request gate exempts
/login, /logout, /oidc/* and /api/enrollment/redeem by path; the rest are
session-authenticated. The OAuth registry these routes use is
web_common.oauth, bound to the app in main.
"""

import logging
import secrets
from pathlib import Path

from flask import jsonify, request, render_template, abort, session, redirect, url_for, Blueprint, current_app
from flask_babel import gettext as _
from werkzeug.security import generate_password_hash, check_password_hash

import credential_store
import db
import lastfm
import play_forward
import scanner
import settings
import sync_state
import spotify_client
import tidal_client
from web_common import (
    _RL_LOCK,
    _app_authenticates,
    _client_ip,
    _end_other_sessions,
    _is_admin,
    _music_root_is_readonly,
    _provision_user,
    _rate_limited,
    _record_failure,
    _rl_failures,
    _spotify_experimental_enabled,
    _validated_device_name,
    _validated_device_type,
    _validated_max_size_bytes,
    _validated_transcode_format,
    get_current_user_id,
    oauth,
    require_admin,
)

_log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)


def _clear_failures(bucket: str) -> None:
    with _RL_LOCK:
        _rl_failures.pop(bucket, None)


def _start_session(conn, user_id: int) -> None:
    """Sign a user in: a fresh session (nothing carried over from before the
    login), permanent so its lifetime slides, and stamped with the user's
    session generation."""
    row = conn.execute("SELECT session_generation FROM users WHERE id = ?", (user_id,)).fetchone()
    session.clear()
    session["local_user_id"] = user_id
    session["session_generation"] = row["session_generation"] if row else 0
    session["sid"] = secrets.token_urlsafe(16)  # for ending this session alone
    session.permanent = True


def _end_this_session(conn) -> None:
    """Log out: this session is over for good, including any copy of its
    cookie that a request still in flight hands back later."""
    sid = session.get("sid")
    if sid is not None:
        days = current_app.config["PERMANENT_SESSION_LIFETIME"].days + 1
        conn.execute("DELETE FROM ended_sessions WHERE ended_at < datetime('now', ?)", (f"-{days} days",))
        conn.execute("INSERT OR IGNORE INTO ended_sessions (sid) VALUES (?)", (sid,))
        conn.commit()
    session.clear()


def _tidal_oauth_client(conn):
    """Registers (idempotently) and returns the Tidal OAuth2 client, or
    None if the admin hasn't configured tidal_client_id/tidal_client_secret
    yet (Administration > Configuration). Unlike the OIDC client above
    (registered once at startup from env vars, fixed for the process
    lifetime), these can change at runtime via the admin UI —
    api_admin_config's PUT handler evicts the cached client below when
    they do, since Authlib's create_client() caches its result forever
    otherwise (oauth._clients), so a naive re-register() alone wouldn't
    pick up new credentials without a restart."""
    client_id = db.get_config(conn, "tidal_client_id")
    client_secret = db.get_config(conn, "tidal_client_secret")
    if not client_id or not client_secret:
        return None
    # The scope is fixed at registration too, so the same PUT handler also
    # evicts the client when tidal_playlists_write changes.
    scope = tidal_client.scopes(tidal_write_enabled(conn))
    if "tidal" not in oauth._clients:
        oauth.register(
            name="tidal",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url=tidal_client.AUTHORIZE_URL,
            access_token_url=tidal_client.TOKEN_URL,
            # PKCE (S256) alongside the client secret — same defense-in-depth
            # posture as the OIDC registration above.
            client_kwargs={"scope": scope, "code_challenge_method": "S256"},
        )
    return oauth.tidal


def tidal_write_enabled(conn) -> bool:
    """The admin's statement that their registered Tidal app has
    playlists.write enabled. Off by default; sharing stays hidden until on."""
    return db.get_config(conn, "tidal_playlists_write") == "1"


def _spotify_oauth_client(conn):
    """Registers (idempotently) and returns the Spotify OAuth2 client, or None
    if the admin hasn't configured spotify_client_id/spotify_client_secret yet
    (Administration > Configuration). Same runtime-configurable shape as
    _tidal_oauth_client — api_admin_config's PUT evicts the cached client
    (oauth._clients) when the credentials change, since Authlib caches
    create_client() forever otherwise."""
    client_id = db.get_config(conn, "spotify_client_id")
    client_secret = db.get_config(conn, "spotify_client_secret")
    if not client_id or not client_secret:
        return None
    if "spotify" not in oauth._clients:
        oauth.register(
            name="spotify",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url=spotify_client.AUTHORIZE_URL,
            access_token_url=spotify_client.TOKEN_URL,
            client_kwargs={"scope": spotify_client.SCOPES},
        )
    return oauth.spotify


def _build_setup_js_i18n() -> dict:
    return {
        "genericFailure": _("Failed ({error})"),
        "providerState": {
            "paired": _("Connected"),
            "pending_approval": _("Awaiting approval"),
            "disconnected": _("Disconnected"),
        },
        "savingLabel": _("Saving…"),
        "continueLabel": _("Continue"),
        "testingLabel": _("Testing…"),
        "testConnectionLabel": _("Test connection"),
        "finishingLabel": _("Finishing…"),
        "finishSetupLabel": _("Finish setup"),
    }


@bp.route("/login", methods=["GET", "POST"])
def login():
    with db.connection() as conn:
        user_count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        bootstrap = settings.AUTH_MODE == "local" and user_count == 0
        error = None
        if request.method == "POST":
            ip = _client_ip()
            if _rate_limited("login:" + ip, max_failures=10, window_s=300):
                return render_template(
                    "login.html", bootstrap=bootstrap,
                    error=_("Too many failed attempts. Please wait a few minutes and try again."),
                    auth_mode=settings.AUTH_MODE,
                ), 429
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not username or not password:
                error = _("Username and password are required.")
            elif bootstrap and settings.ADMIN_USERNAME and username != settings.ADMIN_USERNAME:
                # #96: when ADMIN_USERNAME is configured, the first-run admin
                # account may only be claimed under that exact username.
                # Without this, whoever reaches a fresh local-mode instance
                # first could POST /login and claim the sole admin account
                # with an arbitrary name + password, locking out the operator
                # (the forward/OIDC paths already gate admin on ADMIN_USERNAME
                # in _provision_user; this is the missing local-bootstrap
                # counterpart). Rate-limited like a failed login so the
                # configured name — which acts as a shared secret here — can't
                # be brute-forced. Deliberately does NOT reveal the expected
                # value in the error or template.
                _record_failure("login:" + ip)
                error = _("The first account must use the configured admin username.")
            elif bootstrap:
                cur = conn.execute(
                    "INSERT INTO users (username, is_admin, password_hash) VALUES (?, 1, ?)",
                    (username, generate_password_hash(password)),
                )
                conn.commit()
                _start_session(conn, sync_state._new_id(cur))
                return redirect(url_for("index"))
            else:
                row = conn.execute(
                    "SELECT id, password_hash, is_admin FROM users WHERE username = ?", (username,)
                ).fetchone()
                # #235: under oidc, a local password is break-glass — everyone
                # else authenticates through the IdP. Honouring one for a
                # non-admin would make it a standing, unmonitored IdP bypass
                # (MFA/disablement/policy the IdP enforces, sidestepped), so
                # it's admin-only there. check_password_hash always runs first
                # (short-circuit order below) so a non-admin's correct password
                # takes exactly as long to reject as a wrong one — no timing
                # tell for "this account has a working local password".
                if (
                    row is not None and row["password_hash"]
                    and check_password_hash(row["password_hash"], password)
                    and (settings.AUTH_MODE != "oidc" or row["is_admin"])
                ):
                    _clear_failures("login:" + ip)
                    _start_session(conn, row["id"])
                    return redirect(url_for("index"))
                _record_failure("login:" + ip)
                error = _("Invalid credentials.")
        return render_template("login.html", bootstrap=bootstrap, error=error, auth_mode=settings.AUTH_MODE)


@bp.route("/oidc/login")
def oidc_login():
    """Kick off the OIDC authorization-code flow — Authlib builds the authorize
    URL with state + nonce + PKCE (stashed in the session) and redirects to the
    IdP. No-op guard for the wrong mode so the route can't be abused to start a
    flow that /oidc/callback would then reject anyway."""
    if settings.AUTH_MODE != "oidc":
        return redirect(url_for("auth.login"))
    try:
        return oauth.oidc.authorize_redirect(url_for("auth.oidc_callback", _external=True))
    except Exception as exc:
        # Building the redirect fetches the IdP's discovery document: an IdP
        # that's down (restarting, unreachable) lands here. Say so instead of a
        # 500. Under oidc only an admin's local password works (the
        # break-glass rule in login()), so the notice says exactly that: a
        # regular user told to try theirs would be refused, and their failures
        # would count toward the login rate limit the admin then hits.
        current_app.logger.warning("OIDC sign-in: identity provider unreachable: %s: %s",
                           type(exc).__name__, exc)
        return render_template(
            "login.html", bootstrap=False, auth_mode=settings.AUTH_MODE,
            error=_("SSO is unreachable right now. An administrator can sign in with a local password.")), 503


@bp.route("/oidc/callback")
def oidc_callback():
    """IdP redirect target. authorize_access_token() exchanges the code and
    fully validates the returned ID token (signature via JWKS, iss/aud/exp and
    the nonce) — an invalid/tampered/expired token raises here and never
    establishes a session. The verified username claim is then provisioned the
    same way the forward-header path is, and the local session is set."""
    if settings.AUTH_MODE != "oidc":
        return redirect(url_for("auth.login"))
    try:
        token = oauth.oidc.authorize_access_token()
    except Exception:
        return render_template("login.html", bootstrap=False,
                               error=_("Sign-in failed. Please try again."), auth_mode=settings.AUTH_MODE), 401
    claims = token.get("userinfo") or {}
    username = (claims.get(settings.OIDC_USERNAME_CLAIM) or "").strip()
    if not username:
        return render_template("login.html", bootstrap=False,
                               error=_("Your identity provider did not return a username."), auth_mode=settings.AUTH_MODE), 401
    email = claims.get("email", "") or ""
    with db.connection() as conn:
        _start_session(conn, _provision_user(conn, username, email))
    return redirect(url_for("index"))


@bp.route("/logout", methods=["POST"])
def logout():
    with db.connection() as conn:
        _end_this_session(conn)
    # RP-initiated logout (opt-in): also clear the SSO session at the IdP so a
    # re-login actually re-prompts, rather than silently re-using the still-open
    # IdP session. Falls back to a plain redirect if the IdP's discovery doc has
    # no end_session_endpoint.
    if settings.AUTH_MODE == "oidc" and settings.OIDC_LOGOUT:
        try:
            metadata = oauth.oidc.load_server_metadata()
        except Exception as exc:
            # The local session is already gone; an IdP that can't be reached
            # only means its own session stays open. Finish the logout here.
            current_app.logger.warning("OIDC logout: identity provider unreachable, logged out locally only: "
                               "%s: %s", type(exc).__name__, exc)
            metadata = {}
        end_session = metadata.get("end_session_endpoint")
        if end_session:
            return redirect(f"{end_session}?post_logout_redirect_uri={url_for('index', _external=True)}")
    return redirect(url_for("auth.login") if _app_authenticates() else url_for("index"))


@bp.route("/api/enrollment/grant", methods=["POST"])
def api_enrollment_grant():
    """#163: an authenticated web session mints a short-lived enrollment code
    for the current user; the web UI shows it as a QR + human code for a mobile
    client to redeem (see /api/enrollment/redeem). Keeps auth in the browser —
    the app never holds user credentials (#162)."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        code = sync_state.create_enrollment_grant(conn, user_id)
        return jsonify({"code": code, "expires_in": sync_state.ENROLLMENT_TTL_SECONDS})


@bp.route("/api/enrollment/redeem", methods=["POST"])
def api_enrollment_redeem():
    """#163: a mobile client (no session, no device token yet) redeems an
    enrollment code to create its own device and receive a device Bearer token
    (the same token used for all /api/device/* calls thereafter). Rate-limited
    per IP like the other device endpoints."""
    ip = _client_ip()
    if _rate_limited("enroll:" + ip, max_failures=30, window_s=300):
        abort(429, description=_("Too many attempts. Please wait a few minutes."))
    with db.connection() as conn:
        body = request.get_json(force=True)
        code = (body.get("code") or "").strip().upper()
        name = _validated_device_name(body)
        result = sync_state.redeem_enrollment_grant(
            conn, code, name, _validated_device_type(body.get("device_type", "phone")),
            _validated_max_size_bytes(body.get("max_size_bytes")),
            transcode_format=_validated_transcode_format(body.get("transcode_format")),
        )
        if result is None:
            _record_failure("enroll:" + ip)
            abort(400, description=_("Invalid or expired enrollment code."))
        device_id, raw_token = result
        # Read the name back rather than echoing what was sent. For a new
        # device they are the same string; for a re-pairing code they are not
        # -- the device keeps the name it already had, and the app has to be
        # told that name or it would display its own placeholder for a device
        # the server calls something else.
        name = conn.execute(
            "SELECT name FROM devices WHERE id = ?", (device_id,)).fetchone()["name"]
        return jsonify({"id": device_id, "name": name, "token": raw_token})


@bp.route("/profile/tidal/connect")
def tidal_connect():
    """Kicks off the per-user Tidal link (#21) — see tidal_client.py's
    module docstring for why this is a per-user OAuth flow rather than an
    admin-configured shared connection like Roon/Subsonic/Jellyfin. Requires
    an existing Trobar session; the linked account then belongs to whichever
    Trobar user was logged in when they clicked "Connect"."""
    with db.connection() as conn:
        get_current_user_id(conn)  # 401s here if not logged in, before redirecting anywhere
        client = _tidal_oauth_client(conn)
        if client is None:
            abort(400, description=_("Tidal isn't configured yet — ask an admin to set it up in Administration."))
        # What was asked for, in case the token answer doesn't echo the
        # granted scope back.
        session["tidal_requested_scope"] = client.client_kwargs.get("scope")
        return client.authorize_redirect(url_for("auth.tidal_callback", _external=True))


@bp.route("/profile/tidal/callback")
def tidal_callback():
    """Tidal's redirect target. Exchanges the code, fetches the linked
    account's own id/display name once (get_current_user), and stores both
    on whichever Trobar user's session started the flow — never on a
    session established by this callback itself, since Tidal login is an
    account *link*, not a Trobar login. Error redirects put the query
    param BEFORE the `#` (`?tidal_error=1#/profile`, not the reverse) —
    the frontend's hash-router (stateFromHash()) splits the fragment on
    `/`, so anything appended after `#/profile` would corrupt that parse
    instead of being read as a query param at all."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        client = _tidal_oauth_client(conn)
        if client is None:
            return redirect(url_for("index") + "#/profile")
        try:
            token = client.authorize_access_token()
        except Exception:
            return redirect(url_for("index") + "?tidal_error=1#/profile")
        # The initial code exchange already returns both tokens together —
        # no separate refresh_access_token() call needed here, unlike a
        # later sync pass (see playlist_sync.py) which only ever starts
        # from a stored refresh_token.
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        requested_scope = session.pop("tidal_requested_scope", None)
        if not access_token or not refresh_token:
            return redirect(url_for("index") + "?tidal_error=1#/profile")
        info = tidal_client.get_current_user(access_token)
        if info["status"] != "ok":
            return redirect(url_for("index") + "?tidal_error=1#/profile")
        granted = token.get("scope") or requested_scope
        conn.execute(
            "UPDATE users SET tidal_refresh_token = ?, tidal_user_id = ?, tidal_display_name = ?, "
            "tidal_scopes = ? WHERE id = ?",
            (refresh_token, info["user_id"], info["display_name"], granted, user_id),
        )
        conn.commit()
        return redirect(url_for("index") + "#/profile")


@bp.route("/profile/lastfm/connect")
def lastfm_connect():
    """Sends someone to Last.fm to let this install scrobble for them. The
    state comes back in the callback URL and has to match this session, so a
    link made by someone else can't attach their account here."""
    with db.connection() as conn:
        get_current_user_id(conn)
        app = play_forward.lastfm_app(conn)
        if app is None:
            abort(400, description=_("Last.fm isn't set up yet: ask an admin to add its API account."))
        state = secrets.token_urlsafe(32)
        session["lastfm_link_state"] = state
        return redirect(lastfm.auth_url(app["key"], url_for("auth.lastfm_callback", state=state, _external=True)))


@bp.route("/profile/lastfm/callback")
def lastfm_callback():
    """Last.fm's redirect target: turns its token into a session and stores it,
    encrypted, on the Trobar user who started the link."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        expected = session.pop("lastfm_link_state", None)
        given = request.args.get("state", "")
        app = play_forward.lastfm_app(conn)
        token = request.args.get("token", "")
        if not expected or not secrets.compare_digest(expected, given) or app is None or not token:
            return redirect(url_for("index") + "?lastfm_error=1#/profile")
        linked = lastfm.get_session(app["key"], app["secret"], token, db.get_config(conn, "lastfm_api_base") or "")
        if linked is None:
            return redirect(url_for("index") + "?lastfm_error=1#/profile")
        try:
            stored = credential_store.encrypt(linked["key"], user_id, "lastfm_session")
        except credential_store.CredentialKeyError:
            return redirect(url_for("index") + "?lastfm_error=1#/profile")
        conn.execute("UPDATE users SET lastfm_session_enc = ?, lastfm_session_name = ? WHERE id = ?",
                     (stored, linked["name"], user_id))
        conn.commit()
        return redirect(url_for("index") + "#/profile")


@bp.route("/profile/spotify/connect")
def spotify_connect():
    """Kicks off the per-user Spotify link (#10 Part B) — see
    spotify_client.py for why this is a per-user OAuth flow rather than an
    admin-configured shared connection. Requires an existing Trobar session;
    the linked account belongs to whichever Trobar user clicked "Connect"."""
    with db.connection() as conn:
        get_current_user_id(conn)  # 401s here if not logged in, before redirecting anywhere
        # #398: refuse even if credentials happen to be configured -- a
        # bookmarked/typed URL must not be a back door around the toggle
        # the Profile UI hides this behind.
        if not _spotify_experimental_enabled(conn):
            abort(400, description=_("Spotify is an experimental feature and isn't enabled on this server."))
        client = _spotify_oauth_client(conn)
        if client is None:
            abort(400, description=_("Spotify isn't configured yet — ask an admin to set it up in Administration."))
        return client.authorize_redirect(url_for("auth.spotify_callback", _external=True))


@bp.route("/profile/spotify/callback")
def spotify_callback():
    """Spotify's redirect target. Exchanges the code, fetches the linked
    account's own id/display name once (get_current_user), and stores both on
    whichever Trobar user's session started the flow. Error redirects put the
    query param BEFORE the `#` (`?spotify_error=1#/profile`) — the frontend's
    hash-router splits the fragment on `/`, so anything after `#/profile` would
    corrupt that parse."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        # #398: same guard as spotify_connect() above -- if the feature got
        # toggled off mid-flow (or this URL is hit directly), don't
        # complete a link the toggle says shouldn't exist. Redirects
        # quietly rather than aborting: this is Spotify's own redirect
        # target, not a page a user navigated to on purpose, so an error
        # page here would be a confusing dead end rather than useful
        # feedback.
        if not _spotify_experimental_enabled(conn):
            return redirect(url_for("index") + "#/profile")
        client = _spotify_oauth_client(conn)
        if client is None:
            return redirect(url_for("index") + "#/profile")
        try:
            token = client.authorize_access_token()
        except Exception:
            return redirect(url_for("index") + "?spotify_error=1#/profile")
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        if not access_token or not refresh_token:
            return redirect(url_for("index") + "?spotify_error=1#/profile")
        info = spotify_client.get_current_user(access_token)
        if info["status"] != "ok":
            return redirect(url_for("index") + "?spotify_error=1#/profile")
        conn.execute(
            "UPDATE users SET spotify_refresh_token = ?, spotify_user_id = ?, spotify_display_name = ? WHERE id = ?",
            (refresh_token, info["user_id"], info["display_name"], user_id),
        )
        conn.commit()
        return redirect(url_for("index") + "#/profile")


@bp.route("/setup")
def setup_wizard():
    with db.connection() as conn:
        if settings.AUTH_MODE != "local" or not _is_admin(conn, get_current_user_id(conn)):
            return redirect(url_for("index"))
    return render_template("setup.html", js_i18n=_build_setup_js_i18n())


@bp.route("/api/setup/status")
def api_setup_status():
    with db.connection() as conn:
        require_admin(conn)
        return jsonify({
            "completed": bool(db.get_config(conn, "setup_completed")),
            "music_root": str(db.get_music_root()),
            "music_root_writable": _music_root_is_readonly() is False,
        })


@bp.route("/api/setup/music-root", methods=["POST"])
def api_setup_music_root():
    with db.connection() as conn:
        require_admin(conn)
        body = request.get_json(force=True)
        path_str = (body.get("path") or "").strip()
        if not path_str or not Path(path_str).is_dir():
            return jsonify({"error": _("This folder doesn't exist or isn't readable by the server.")}), 400
        # A re-run of the wizard checks the folder here and saves it only at
        # its finish, so leaving it half-way changes nothing.
        if not body.get("check_only"):
            db.set_config(conn, "music_root", path_str)
            conn.commit()
        # flag a writable mount so the wizard can warn right here.
        return jsonify({
            "music_root": path_str,
            "music_root_writable": _music_root_is_readonly(Path(path_str)) is False,
        })


@bp.route("/api/setup/complete", methods=["POST"])
def api_setup_complete():
    """Finishes setup and kicks the first library scan off in the BACKGROUND.

    #309: this used to call the synchronous scan_library() so the wizard's last
    screen could show real counts. That blocked the request for the length of a
    full first index, which behind a reverse proxy — the deployment the docs
    actively describe — outlives the read timeout (nginx defaults to 60s) and
    the wizard shows a fetch failure. Reproduced in the wild behind Traefik. The
    scan itself completed fine and setup was already saved, so nothing was ever
    lost; the screen simply said the opposite of the truth, at the one moment a
    first-time user has no idea what to do about it.

    MUSIC_ROOT is validated BEFORE setup_completed is committed, which is the
    part that has to be in this order. The wizard is the last place a wrong
    music path is easy to fix; once setup_completed is durable, any reload sends
    the user to an empty main UI with no route back (see setup.html's on-load
    redirect). Committing first and validating after would make the "stay in the
    wizard" behaviour unimplementable.

    Kept deliberately thin: #297 step 3 moves the scan onto the job queue, at
    which point start_scan() here becomes an enqueue and progress gets a real
    home in the Background jobs panel."""
    with db.connection() as conn:
        require_admin(conn)
        root = db.get_music_root()
        if not root.is_dir():
            # Not committed, so the wizard can legitimately stay put.
            return jsonify({"error": _(
                "Can't read the music folder %(path)s — check the path and the "
                "container's volume mount, then try again.", path=str(root))}), 400
        db.set_config(conn, "setup_completed", "1")
        conn.commit()

    # Past this point setup IS saved, so nothing here may trap the user. A scan
    # that fails to start is reported (never silently swallowed) but
    # doesn't block them from the UI — the Library tab's Rescan is right there.
    # `already_running` is success, not failure: a scan being underway is the
    # desired end state.
    scan_started = False
    try:
        result = scanner.start_scan(root)
        scan_started = result.get("status") == "started" or bool(result.get("already_running"))
    except Exception:
        current_app.logger.exception("could not start the first library scan")
    return jsonify({"scan_started": scan_started})


@bp.route("/api/account/password", methods=["POST"])
def api_account_password():
    """Any authenticated user can set/change their own local password —
    this is what makes the admin's break-glass login possible in oidc/forward
    mode (usable when the IdP/proxy is down), and it's how any user sets their
    password in AUTH_MODE=local. #235: under oidc specifically, break-glass is
    admin-only — /login already ignores a non-admin's local password there, so
    this blocks the write too (not just hiding the form client-side), keeping
    "no non-admin local password exists under oidc" true rather than merely
    "unused". forward mode is untouched — its own break-glass already requires
    the separate emergency port, a stronger gate oidc's plain /login lacks."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        if settings.AUTH_MODE == "oidc" and not _is_admin(conn, user_id):
            abort(400, description=_(
                "Local passwords are admin-only while AUTH_MODE=oidc — your account is managed by "
                "the identity provider."))
        body = request.get_json(force=True)
        password = body.get("password") or ""
        if len(password) < 8:
            abort(400, description=_("Password must be at least 8 characters"))
        # An account that has a password must give it: a session left open on
        # someone else's screen mustn't be enough to take the account over.
        # Wrong guesses are throttled per account, like logins per address.
        current_hash = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()["password_hash"]
        if current_hash is not None:
            bucket = f"password:{user_id}"
            if _rate_limited(bucket, max_failures=10, window_s=300):
                abort(429, description=_("Too many attempts. Please wait a few minutes."))
            if not check_password_hash(current_hash, body.get("current_password") or ""):
                _record_failure(bucket)
                resp = jsonify({"error": _("That isn't your current password."), "field": "current_password"})
                resp.status_code = 400
                abort(resp)
            _clear_failures(bucket)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        # Every other session of this user ends; this one stays signed in.
        _end_other_sessions(conn, user_id)
        conn.commit()
        return jsonify({"status": "ok"})
