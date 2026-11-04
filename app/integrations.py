#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integrations: the read-mostly API external tools use (Home Assistant,
monitors), /api/integrations/*, and the admin's tokens for it,
/api/integration-tokens*. The two authenticate differently. main's
before-request gate exempts /api/integrations/ as a whole prefix, so every
route under it calls _authenticated_integration_token() itself; the token
routes are session-authenticated and admin-only.
"""

import logging

from flask import jsonify, request, abort, Blueprint
from flask_babel import gettext as _

import db
import mirror_runs
import scanner
import sync_state
from web_common import (
    _client_ip,
    _device_list,
    _is_admin,
    _meta_file,
    _rate_limited,
    _record_failure,
    get_current_user_id,
    require_admin,
)

_log = logging.getLogger(__name__)

bp = Blueprint("integrations", __name__)


# #498: (enabled_column, error_code_column, last_written_column) per sink.
# "filesystem" keeps its historical unprefixed `mirror_*` columns (it
# predates the other three sinks, added); subsonic/jellyfin/emby
# each got their own `{sink}_mirror_*` columns when added. A dict (not a
# tuple of tuples) so both api_integrations_mirrors and its response's
# `by_sink` object iterate in the same order — filesystem, subsonic,
# jellyfin, emby — matching #189's own introduction order and every other
# sink listing (api_playlist_mirror_toggle here, api_admin_mirrors in admin.py).
_MIRROR_SINK_COLUMNS = {
    "filesystem": ("mirror_enabled", "mirror_last_error_code", "mirror_last_written_at"),
    "subsonic": ("subsonic_mirror_enabled", "subsonic_mirror_last_error_code",
                 "subsonic_mirror_last_written_at"),
    "jellyfin": ("jellyfin_mirror_enabled", "jellyfin_mirror_last_error_code",
                 "jellyfin_mirror_last_written_at"),
    "emby": ("emby_mirror_enabled", "emby_mirror_last_error_code", "emby_mirror_last_written_at"),
    "music_assistant": ("music_assistant_mirror_enabled", "music_assistant_mirror_last_error_code",
                        "music_assistant_mirror_last_written_at"),
    "plex": ("plex_mirror_enabled", "plex_mirror_last_error_code", "plex_mirror_last_written_at"),
}


# #498: unlike Health's 200 (opened occasionally, in a browser), this
# endpoint is meant to be polled unattended every few minutes — a single
# dead mirror target can fail every playlist mirrored to it, so the cap
# needs to stay well under a size that would make a routine poll heavy.
# mirrors_failing/by_sink are computed from the FULL set regardless of this
# cap and stay exact; only the `failing` worklist itself is capped.
_MIRRORS_FAILING_LIMIT = 50


def _authenticated_integration_token(conn) -> int:
    """#446/#474/#498: admin-minted Bearer token for external integrations
    (Home Assistant, Grafana, uptime monitors...) — neither a browser
    session nor a device. Deliberately used ONLY by the dedicated
    /api/integrations/* routes below (devices, server, mirrors,
    actions/scan — all four), never folded into get_current_user_id: that
    function backs every session-authenticated route in this file,
    including every mutating one, so keeping this entirely separate means
    a leaked integration token can never reach anything outside these
    four routes, regardless of what's added under /api/devices or
    elsewhere later.

    One credential authenticates reads AND the rescan action — see
    db.py's integration_tokens comment for why that split moved from "which
    token type" to "who was allowed to mint one" (api_integration_tokens'
    require_admin() gate) instead. Own rate-limit bucket
    ("integration_token:" + ip) so a wrong-token integration polling loop
    can never contribute to, or be blocked by, #382's login backoff.

    #479: require_admin() at api_integration_tokens is a mint-time gate,
    checked once and never again — so it re-verifies admin status here
    too, at every use, not just at creation. Without this, demoting an
    admin (should that ever become a reachable action; no code path does
    it today) would leave their tokens working
    indefinitely, outliving the trust they were minted under. Same 401
    and the same failure-record as an unknown token — deliberately: a
    demoted user's client should be told its credential is dead, not
    given a response that lets it distinguish "revoked" from "never
    existed" (403 would leak that the token is real).

    Returns owner_user_id."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        abort(401, description=_("Missing authentication header"))
    ip = _client_ip()
    if _rate_limited("integration_token:" + ip, max_failures=30, window_s=300):
        abort(429, description=_("Too many attempts. Please wait a few minutes."))
    token = sync_state.authenticate_integration_token(conn, auth[len("Bearer "):])
    if token is None or not _is_admin(conn, token["owner_user_id"]):
        _record_failure("integration_token:" + ip)
        abort(401, description=_("Invalid or revoked integration token."))
    return token["owner_user_id"]


@bp.route("/api/integrations/devices")
def api_integrations_devices():
    """#446: the read-only counterpart to GET /api/devices for external
    integrations (Home Assistant, Grafana, uptime monitors) — same shape,
    same per-viewer scoping (an admin's token still sees every device, a
    regular user's still sees only their own plus pinned/delegated ones,
    exactly like a session would — kept general even though #474's revision
    means every token is an admin's in practice today), authenticated by
    the integration token instead of a browser session, and with no
    POST/create sibling on this route at all: unlike /api/devices, there is
    nothing here a token could ever be tricked into reaching beyond a
    read."""
    with db.connection() as conn:
        user_id = _authenticated_integration_token(conn)
        out = _device_list(conn, user_id)
        for d in out:
            # The device icon is a web/app picture, and the Play choice a
            # sync setting; neither is part of this published shape
            # (integration-api.md), so they are left out and the contract is
            # unchanged.
            d.pop("icon", None)
            d.pop("play_playlist_id", None)
        return jsonify(out)


@bp.route("/api/integrations/server")
def api_integrations_server():
    """#475: the server half of what trobar-ha#25's "server" device needs —
    read-only, instance-wide (not scoped per caller: track_count/total_bytes
    describe the whole library, same numbers every logged-in user's own
    dashboard already shows them, so a token seeing them is not a new
    disclosure). The *actions* that live alongside this on the same HA
    device (rescan, provider refresh) are #474's — this route itself stays
    read-only, but as of #474's revision it shares its authenticator and
    credential with the action route rather than being structurally barred
    from it.

    Deliberately NOT /api/library/stats reused or extended: that endpoint's
    own docstring says what it is — "a snapshot computed on dashboard load,
    not a hot path worth a fancier query" — and reads every non-deleted
    track's full row (path, size, duration, year) to aggregate codec/decade
    breakdowns in Python. trobar-ha polls every 5 minutes, indefinitely;
    reusing that endpoint would turn an occasional full-table scan into a
    permanent one on every install running the integration. This route asks
    only for track_count and total_bytes (COUNT/SUM on the already-cheap
    `size` column — measured against a synthetic 59,000-row table:
    ~2ms), never by_codec/by_decade, which change slowly and nobody
    automates on — someone who wants them can open the dashboard.

    No `online` field: a response arriving at all IS that signal, and a
    field that can only ever read `true` (an unreachable server can't
    answer this request to say otherwise) would be pure noise. trobar-ha#25
    derives reachability from whether the poll itself succeeded."""
    with db.connection() as conn:
        _authenticated_integration_token(conn)
        version_file = _meta_file("VERSION")
        version = version_file.read_text(encoding="utf-8").strip() if version_file else "unknown"
        row = conn.execute(
            "SELECT COUNT(*) AS track_count, COALESCE(SUM(size), 0) AS total_bytes "
            "FROM tracks WHERE deleted_at IS NULL"
        ).fetchone()
        status = scanner.scan_status()
        return jsonify({
            "version": version,
            "track_count": row["track_count"],
            "total_bytes": row["total_bytes"],
            "scan_running": status["running"],
            "last_scan_at": status["last_scan_at"],
        })


@bp.route("/api/integrations/mirrors")
def api_integrations_mirrors():
    """#498: the monitoring surface #189's three added sinks (Subsonic,
    Jellyfin, Emby) never got alongside the pre-existing filesystem one —
    each sink's failure state (`unset_target`/`unreachable`/
    `no_target_matches`/`write_failed`, plus filesystem's own
    `unset_folder`/`not_writable`/`bad_filename`/`marker_unsafe`) was only
    ever visible in the session-authenticated web UI (GET
    /api/provider/playlists and GET /api/admin/mirrors), so a self-hoster
    whose Navidrome moved or whose Jellyfin API key rotated found out by
    happening to open the admin panel. Mirrors are unattended background
    work — device sync already has this kind of surface
    (/api/integrations/devices exists precisely so HA can alert on it);
    this is the mirrors equivalent.

    Its own route rather than growing /api/integrations/server: mirrors
    aren't a server metric, and that payload is a captured reference
    (trobar-ha#2) that shouldn't gain an unrelated dimension.

    `by_sink[sink].enabled`/`.failing` count playlist×sink PAIRS, not
    playlists — a playlist mirrored to both Subsonic and Jellyfin counts
    once in each sink's `enabled`. That's what makes `by_sink.*.failing`
    sum to `mirrors_failing`, the property that makes this payload
    self-checking rather than needing the two cross-referenced by hand.

    "Failing" means `{sink}_mirror_enabled = 1 AND
    {sink}_mirror_last_error_code IS NOT NULL` — no per-code allowlist, so
    `unset_target` counts (a mirror target cleared out from under playlists
    still enabled against it is exactly the silent-drift case this
    endpoint exists to surface, not a state to special-case away).

    `failing` is a worklist, not an inventory: only entries currently in
    error, capped at _MIRRORS_FAILING_LIMIT (`failing_truncated: true` if
    the real count exceeds it) — the opposite of /api/admin/mirrors, whose
    own docstring calls itself "small and unpaginated... a status, not a
    problem set expected to grow large." Each entry's `error_code` is the
    raw code, not the rendered message: #428 made these deliberately
    language-independent so a client renders its own strings rather than
    displaying server-side English."""
    with db.connection() as conn:
        _authenticated_integration_token(conn)
        # Built from _MIRROR_SINK_COLUMNS (fixed identifiers, never input),
        # so a sink added there is counted here without a second list.
        columns = ", ".join(c for cols in _MIRROR_SINK_COLUMNS.values() for c in cols)
        any_enabled = " OR ".join(f"{cols[0]} = 1" for cols in _MIRROR_SINK_COLUMNS.values())
        rows = conn.execute(
            f"SELECT id, title, {columns} FROM playlists WHERE {any_enabled} ORDER BY title"
        ).fetchall()

        by_sink = {sink: {"enabled": 0, "failing": 0, "short": 0} for sink in _MIRROR_SINK_COLUMNS}
        failing = []
        # Coverage: a pair is "short" when its last write left tracks off
        # the target (mirror_runs; the filesystem mirror records none).
        # Counted like failing, so by_sink.*.short sums to mirrors_short.
        runs = mirror_runs.latest(conn, [r["id"] for r in rows])
        short = []
        for row in rows:
            for sink, (enabled_col, error_col, written_col) in _MIRROR_SINK_COLUMNS.items():
                if not row[enabled_col]:
                    continue
                by_sink[sink]["enabled"] += 1
                run = runs.get((row["id"], sink))
                if run is not None and (run["lacked"] or run["refused"]):
                    by_sink[sink]["short"] += 1
                    short.append({
                        "playlist_id": row["id"],
                        "title": row["title"],
                        "sink": sink,
                        "on_target": run["on_target"],
                        "total": run["total"],
                        "lacked": run["lacked"],
                        "refused": run["refused"],
                        "run_at": run["run_at"],
                    })
                if row[error_col] is not None:
                    by_sink[sink]["failing"] += 1
                    failing.append({
                        "playlist_id": row["id"],
                        "title": row["title"],
                        "sink": sink,
                        "error_code": row[error_col],
                        "last_written_at": row[written_col],
                    })

        return jsonify({
            "mirrors_failing": len(failing),
            "by_sink": by_sink,
            "failing": failing[:_MIRRORS_FAILING_LIMIT],
            "failing_truncated": len(failing) > _MIRRORS_FAILING_LIMIT,
            "mirrors_short": len(short),
            "short": short[:_MIRRORS_FAILING_LIMIT],
            "short_truncated": len(short) > _MIRRORS_FAILING_LIMIT,
        })


@bp.route("/api/integrations/actions/scan", methods=["POST"])
def api_integrations_actions_scan():
    """#474: the integration-token-authenticated counterpart to POST
    /api/library/scan, for an integration (trobar-ha#25) to trigger a
    rescan rather than only observe one. Deliberately its own route under
    /api/integrations/actions/ rather than teaching /api/library/scan a
    second auth path — but as of #474's revision it shares
    _authenticated_integration_token with the read-only
    devices/server routes rather than requiring a second credential; the
    thing that keeps this safe is that only an admin could mint the token
    in the first place (see db.py's integration_tokens comment). Same
    202/409 response shape as /api/library/scan so a caller already
    handling that shape needs no new branch.

    force is read from the JSON body, not query string, since this is a
    machine-to-machine POST rather than a browser link; defaults to False
    like the web UI's own default scan."""
    with db.connection() as conn:
        _authenticated_integration_token(conn)
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    result = scanner.start_scan(db.get_music_root(), force=force)
    if result.get("already_running"):
        return jsonify({"error": _("A library scan is already running.")}), 409
    return jsonify(result), 202


@bp.route("/api/integration-tokens", methods=["GET", "POST"])
def api_integration_tokens():
    """#446/#474: manage integration tokens — admin-only, unlike every
    other per-user credential in this file (device tokens, the old
    api_tokens/action_tokens split). That's the actual safety property
    now: db.py's integration_tokens comment and #474's PR discussion cover
    why gating who may mint one replaced the earlier read-only/action
    table split. require_admin() on both GET and POST, not just POST — a
    non-admin's own list is always empty under this model, so there is
    nothing for them to legitimately see here either."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        require_admin(conn)
        if request.method == "POST":
            body = request.get_json(force=True)
            name = (body.get("name") or "").strip()
            if not name:
                abort(400, description=_("Name cannot be empty."))
            token_id, raw_token = sync_state.create_integration_token(conn, user_id, name)
            return jsonify({"id": token_id, "name": name, "token": raw_token})
        return jsonify([dict(r) for r in sync_state.list_integration_tokens(conn, user_id)])


@bp.route("/api/integration-tokens/<int:token_id>", methods=["DELETE"])
def api_integration_token_delete(token_id: int):
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        require_admin(conn)
        if not sync_state.revoke_integration_token(conn, user_id, token_id):
            abort(404, description=_("Integration token not found."))
        return jsonify({"status": "ok"})
