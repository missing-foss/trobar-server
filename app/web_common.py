#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers the routes in main.py and the blueprints both use (#253).

A blueprint never imports main: under `python main.py` that module is
__main__, and `import main` would load a second copy of it, with its own app
and its own state. What both sides need lives here instead, and main imports
it back. See CONTRIBUTING.md.
"""

import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from authlib.integrations.flask_client import OAuth
from flask import request, abort, current_app, session, Response, jsonify
from flask_babel import gettext as _

import artist_images
import covers
import db
import device_plays
import emby_client
import filesystem_client
import jellyfin_client
import lastfm
import lidarr_requests
import listenbrainz
import lms_client
import maloja
import mirror_runs
import music_assistant_client
import playlist_sync
import plex_client
import roon_client
import rule_playlists
import settings
import spotify_public_client
import subsonic_client
import suggestions
import sync_state
import ytmusic_client

_log = logging.getLogger(__name__)


# --- Brute-force backoff -----------------------------------------
# Tiny in-memory per-IP failure counter for the two credential-verifying paths
# (password /login and device Bearer auth). Strong tokens/scrypt hashes already
# make guessing impractical; this just adds cheap per-IP backoff. In-process
# only (single worker today). A bucket is dropped once its window has passed,
# so an address is held no longer than the window, not until a restart.
_RL_LOCK = threading.Lock()


_rl_failures: dict[str, deque] = defaultdict(deque)


# The longest window any caller has asked for, and when every bucket was last
# swept against it: a bucket that is never checked again still goes.
_rl_max_window_s = 0


_rl_last_sweep = 0.0


_RL_SWEEP_EVERY_S = 60


def _client_ip() -> str:
    # #382: this used to hand-parse X-Forwarded-For and take
    # the LEFT-most entry, which is whatever the client itself claimed — the
    # trusted proxy only ever APPENDS its own hop to the right. That let an
    # attacker rotate a fake leftmost IP every request and never accumulate
    # failures in any one rate-limit bucket. ProxyFix(x_for=1) above already
    # did the equivalent-but-correct rewrite (RIGHT-most trusted hop) into
    # environ["REMOTE_ADDR"] before this ever runs, so the fix is to just
    # read that instead of re-deriving it here by hand.
    return request.remote_addr or "unknown"


def _rate_limited(bucket: str, max_failures: int, window_s: int) -> bool:
    """True if `bucket` has already logged >= max_failures within window_s.

    Checking never creates a bucket, and one left empty by the pruning is
    dropped, so a request that fails nothing leaves no address behind."""
    global _rl_max_window_s, _rl_last_sweep
    now = time.time()
    with _RL_LOCK:
        _rl_max_window_s = max(_rl_max_window_s, window_s)
        if now - _rl_last_sweep >= _RL_SWEEP_EVERY_S:
            _rl_last_sweep = now
            cutoff = now - _rl_max_window_s
            for stale in [b for b, d in _rl_failures.items() if not d or d[-1] < cutoff]:
                del _rl_failures[stale]
        dq = _rl_failures.get(bucket)
        if dq is None:
            return False
        while dq and dq[0] < now - window_s:
            dq.popleft()
        if not dq:
            del _rl_failures[bucket]
            return False
        return len(dq) >= max_failures


def _record_failure(bucket: str) -> None:
    with _RL_LOCK:
        _rl_failures[bucket].append(time.time())


# "one or the other" — never more than one simultaneously.
# roon_client/subsonic_client/jellyfin_client/emby_client/plex_client/
# lms_client/filesystem_client expose the same function names/shapes
# (status, reconnect, list_playlists, get_playlist_tracks, get_artist_image,
# ensure_started) so every call site just dispatches through this rather
# than branching per route.
_PROVIDERS = {
    "roon": roon_client, "subsonic": subsonic_client, "jellyfin": jellyfin_client,
    "emby": emby_client, "plex": plex_client, "lms": lms_client,
    "filesystem": filesystem_client, "music_assistant": music_assistant_client,
}


def _active_provider(conn):
    return _PROVIDERS.get(db.get_config(conn, "provider") or "roon", roon_client)


def _contained(root: Path, relative: str) -> Path | None:
    """`root / relative`, or None if the result escapes `root`.

    Defence in depth, not a fix for a live defect: every current caller
    passes a server-generated value (an avatar filename built from an
    integer user id plus a mimetype allowlist, or a relative path the
    scanner produced by walking the music root). The point is that the read
    and delete sides stop depending on that being true of every writer
    forever -- containment becomes a property of this function rather than
    an invariant maintained at a distance.

    Same normpath()-based shape as mirror._safe_join(), and see its comment
    for why not resolve()+relative_to(). It differs in one way deliberately:
    that one requires the result to be a DIRECT child, which is correct for
    a single filename and wrong here, because a track's relative_path is a
    multi-segment path underneath the music root. So containment is a prefix
    test rather than parent equality. An absolute `relative` is rejected too
    -- os.path.join() would discard the base entirely, and the result then
    fails the prefix test rather than silently escaping."""
    base = os.path.normpath(str(root))
    full = os.path.normpath(os.path.join(base, relative))
    if full != base and not full.startswith(base + os.sep):
        return None
    return Path(full)


# SQLite stores INTEGER as signed 64-bit; a larger value raises OverflowError
# on INSERT (a 500). Reject it up front so bad input never 500s (#100).
_MAX_SIZE_BYTES_LIMIT = 2**63 - 1


def _validated_max_size_bytes(value):
    """A storage cap is either null (no limit) or a positive integer number
    of bytes that fits a signed 64-bit column (#100). Rejects negatives,
    non-integers, booleans, strings, and absurdly-large values — an unvalidated
    value flows into the usage/autofit math and would blow up there or on INSERT
    later. 0 is stored as null: it means "no limit" everywhere, where autofit
    already read it that way but the usage bar called every byte over it.
    Returns the int or None."""
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 0 or value > _MAX_SIZE_BYTES_LIMIT):
        abort(400, description=_("Storage limit must be a whole number of bytes, or empty for no limit."))
    return value or None


def _refresh_autofit_after_limit_change(conn, device_id: int) -> None:
    """A device's auto-fit set is frozen at its last refresh against the limit
    then; a new limit refreshes it, or a lowered one leaves it over budget.
    No-op for a device without auto-fit, and when there is no ranking to
    refresh from: Last.fm didn't answer (_RankingUnavailable), or answered
    with nothing. Refreshing from nothing would empty the set and remove
    every auto-fit track from the device on a limit change nobody meant that
    way. The set then stays as it is until the next refresh that has a
    ranking."""
    selection_id = sync_state.autofit_selection_id_for_device(conn, device_id)
    if selection_id is None:
        return
    row = conn.execute(
        "SELECT s.target, d.owner_user_id FROM selections s, devices d WHERE s.id = ? AND d.id = ?",
        (selection_id, device_id),
    ).fetchone()
    period = row["target"] or sync_state.DEFAULT_AUTOFIT_PERIOD
    try:
        ranked = _ranked_album_keys_for_user(conn, row["owner_user_id"], period)
    except _RankingUnavailable:
        ranked = None
    if not ranked:
        current_app.logger.warning(
            "device %d: limit changed, but auto-fit kept as it was: no Last.fm ranking", device_id)
        return
    sync_state.refresh_autofit(conn, selection_id, ranked)
    sync_state.recompute_device_state(conn, device_id)


def _validated_source_of_truth(value):
    if value not in ("server", "device"):
        abort(400, description=_("source_of_truth must be 'server' or 'device'."))
    return value


class _RankingUnavailable(Exception):
    """Last.fm didn't answer: there is no ranking, which is not the same as
    an empty one. Refreshing auto-fit from nothing would empty the set and
    remove every auto-fit track from the device."""


def _ranked_album_keys_for_user(conn, user_id: int, period: str):
    """(artist_lower, album_lower) tuples in descending Last.fm play order for a
    user — the ranking auto-fit greedily fills from. None if the user
    has no Last.fm username configured (auto-fit can't rank without it).
    Raises _RankingUnavailable when Last.fm didn't answer."""
    row = conn.execute(
        "SELECT lastfm_username, lastfm_api_key FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None or not row["lastfm_username"]:
        return None
    api_key = row["lastfm_api_key"] or db.get_config(conn, "lastfm_api_key_default") or ""
    # A generous limit so a large device has enough candidate albums to fill.
    albums = lastfm.top_albums(row["lastfm_username"], api_key, period=period, limit=500,
                                api_base=db.get_config(conn, "lastfm_api_base") or "")
    if albums is None:
        raise _RankingUnavailable()
    keys = []
    for a in albums:
        name = (a.get("name") or "").strip()
        artist = ((a.get("artist") or {}).get("name") or "").strip()
        if name and artist:
            keys.append((artist.lower(), name.lower()))
    return keys


def _authenticated_device(conn):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        abort(401, description=_("Missing authentication header"))
    # Per-IP backoff on bad tokens. Only failures are counted and
    # they age out, so a legit device (which never fails) is never throttled;
    # the threshold is generous enough to tolerate several devices behind one
    # household NAT re-pairing at once.
    ip = _client_ip()
    if _rate_limited("device:" + ip, max_failures=30, window_s=300):
        abort(429, description=_("Too many attempts. Please wait a few minutes."))
    device = sync_state.authenticate_device(conn, auth[len("Bearer "):])
    if device is None:
        _record_failure("device:" + ip)
        abort(401, description=_("Invalid or revoked token — re-pair the device"))
    return device


# The App API: the phone's own screens (library, basket, Home dashboard)
# call routes under /api/app/ with the device token they already hold from
# pairing, and act as the device's OWNER. A separate prefix from
# /api/device/ on purpose -- that one is the sync protocol and stays its
# own contract -- and never folded into get_current_user_id, for the same
# reason the integration token is not: every session-authenticated route,
# mutating ones included, resolves identity there. Bump APP_API_VERSION on
# any breaking change to a route under the prefix, and when a section
# arrives that a client gates a whole screen on (2: the playlist section;
# 4: removing a selection from a device; 5: an album's tracks; 6: the library
# by album artist); /api/device/info advertises it so a client can refuse
# gracefully instead of probing.
APP_API_VERSION = 6


def _config_int(conn, key: str, default: int) -> int:
    raw = db.get_config(conn, key)
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


_DEVICE_PLAYLIST_ENTRIES_MAX = 20000


def _is_emergency_request() -> bool:
    # SERVER_PORT reflects the actual listening socket a connection arrived
    # on (verified directly — it's not derived from the client-sent Host
    # header and can't be spoofed by one), unlike everything else on the
    # request that a client controls. Traefik always connects to Flask's one
    # normal port regardless of what external hostname/port the original
    # client used, so this is a reliable way to tell "came in via the
    # un-proxied emergency port" from "came in via Traefik."
    return settings.EMERGENCY_PORT != 0 and request.environ.get("SERVER_PORT") == str(settings.EMERGENCY_PORT)


def _app_authenticates() -> bool:
    """True when the app itself signs users in for this request (AUTH_MODE
    local or oidc, or the emergency port in any mode), so a session exists
    to end. In forward mode the proxy signs users in, and logging out of
    Trobar alone would only sign them straight back in."""
    return settings.AUTH_MODE in ("local", "oidc") or _is_emergency_request()


def _provision_user(conn, username: str, email: str = "") -> int:
    """Find-or-create a user by username, and promote them to admin if they
    match ADMIN_USERNAME. Shared by the forward-header path and the OIDC
    callback — both take an externally-authenticated username and need the
    same auto-provision + self-healing admin promotion (works whether it's
    the user's very first visit or an existing install picking this up)."""
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if row is not None:
        user_id = row["id"]
    else:
        cur = conn.execute("INSERT INTO users (username, email) VALUES (?, ?)", (username, email))
        conn.commit()
        user_id = cur.lastrowid
    if username and username == settings.ADMIN_USERNAME:
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ? AND is_admin = 0", (user_id,))
        conn.commit()
    return user_id


def _session_user_id(conn) -> int | None:
    """The user this request's session signs in, or None: no session, the
    user is gone, or the session predates the user's last password change
    or reset (its generation is older). A session that's over is cleared."""
    user_id = session.get("local_user_id")
    if user_id is None:
        return None
    row = conn.execute("SELECT session_generation FROM users WHERE id = ?", (user_id,)).fetchone()
    # A session from before the column existed carries no generation: 0.
    if row is None or session.get("session_generation", 0) != row["session_generation"]:
        session.clear()
        return None
    sid = session.get("sid")
    if sid is not None and conn.execute("SELECT 1 FROM ended_sessions WHERE sid = ?", (sid,)).fetchone():
        session.clear()  # logged out; this is a copy an in-flight request carried back
        return None
    return user_id


def get_current_user_id(conn) -> int:
    # A local session (set by /login's local-password path, or the OIDC
    # callback) always wins. In local/oidc mode it's the *only* accepted
    # identity — headers are never trusted.
    local_user_id = _session_user_id(conn)
    if local_user_id is not None:
        return local_user_id

    if _app_authenticates():
        # before_request already redirects/401s any request that could reach
        # here without a session — getting here anyway means that guard was
        # somehow bypassed, so fail closed rather than fall through to
        # trusting an identity header. In local/oidc mode the app authenticates
        # users itself (password / verified ID token), so a forwarded header
        # is meaningless and must never be honoured; and the emergency port
        # deliberately bypasses the very proxy that would set that header.
        abort(401, description=_("Login required"))

    # forward mode only: trust the proxy-set identity header. If a shared
    # secret is configured, the proxy must also have injected it — a request
    # lacking it reached us without going through the trusted proxy (e.g. a
    # directly-exposed port) and must not be honoured. See SECURITY.md.
    if settings.FORWARD_AUTH_SECRET and request.headers.get("X-Forward-Auth-Secret") != settings.FORWARD_AUTH_SECRET:
        abort(401, description=_("Login required"))
    username = request.headers.get("X-authentik-username")
    if not username:
        # No identity header: a misconfigured proxy, or a request that reached
        # the app's port directly. Signing it in as anyone would hand out an
        # account; only a local development server does that, on purpose.
        if not settings.DEV_SERVER:
            current_app.logger.warning("forward auth: request to %s without the X-authentik-username "
                               "header refused", request.path)
            abort(401, description=_("Login required"))
        username = os.environ.get("DEV_USER", "dev")
    email = request.headers.get("X-authentik-email", "")
    return _provision_user(conn, username, email)


def _is_admin(conn, user_id: int) -> bool:
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


def _has_delegation(conn, grantee_id: int, target_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM device_delegations WHERE grantee_user_id = ? AND target_user_id = ?",
        (grantee_id, target_id),
    ).fetchone() is not None


def _require_device_access(conn, user_id: int, device_id: int) -> int:
    """a device action is allowed for its owner, an admin
    (unconditional oversight), or anyone the admin has granted delegation
    over the owner. Aborts 404 if the device doesn't exist, 403 if it exists
    but isn't manageable by the current user. Returns owner_user_id."""
    row = conn.execute("SELECT owner_user_id FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        abort(404, description=_("Device not found"))
    owner_id = row["owner_user_id"]
    if owner_id != user_id and not _is_admin(conn, user_id) and not _has_delegation(conn, user_id, owner_id):
        abort(403, description=_("Unauthorized access to this device"))
    return owner_id


def _device_rows_for_user(conn, user_id: int, admin: bool) -> list[dict]:
    """Admin sees every device unconditionally (oversight) — no pin
    needed. A non-admin sees their own devices plus any they've pinned (the
    visibility half of delegation; the delegation grant itself is what makes
    the pin, and every action on the device, actually permitted — see
    _require_device_access).

    Returns dicts, not sqlite3.Row, so is_own/is_pinned can be coerced to
    real booleans here rather than each caller remembering to (#449):
    SQLite has no native boolean type, so these compute as 0/1 integers,
    which jsonify would otherwise serialize as 0/1 rather than false/true.
    Harmless for the session-based device list (JS truthiness treats them
    the same), but #446 made this the response shape for an external,
    non-JS integration API too, where that distinction is real -- Home
    Assistant surfaces raw attribute values in its UI, so an entity
    attribute would read 1 where every other boolean reads true. Matches
    the existing mirror_enabled precedent in api_provider_playlists()."""
    base = (
        "SELECT d.id, d.name, d.device_type, d.icon, d.max_size_bytes, d.transcode_format, d.artist_images, "
        "d.source_of_truth, d.unknown_track_count, d.play_playlist_id, "
        "d.reported_free_bytes, "
        "d.reported_total_bytes, d.free_bytes_reported_at, "
        "d.reported_foreign_bytes, d.foreign_bytes_reported_at, d.created_at, d.last_seen_at, "
        "d.owner_user_id, u.username AS owner_username, "
        "(d.owner_user_id = :uid) AS is_own, "
        "EXISTS(SELECT 1 FROM device_pins p WHERE p.user_id = :uid AND p.device_id = d.id) AS is_pinned "
        "FROM devices d JOIN users u ON u.id = d.owner_user_id "
    )
    if admin:
        query = base + "ORDER BY is_own DESC, u.username, d.name"
    else:
        query = base + (
            "WHERE d.owner_user_id = :uid OR d.id IN (SELECT device_id FROM device_pins WHERE user_id = :uid) "
            "ORDER BY is_own DESC, u.username, d.name"
        )
    out = []
    for r in conn.execute(query, {"uid": user_id}):
        d = dict(r)
        d["is_own"] = bool(d["is_own"])
        d["is_pinned"] = bool(d["is_pinned"])
        out.append(d)
    return out


def _validated_device_name(body: dict) -> str:
    """A device name is required and non-blank (#100 — a missing key used to
    KeyError → 500)."""
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description=_("Name cannot be empty."))
    return name


# The device types the web UI offers and labels (js_i18n's "deviceType").
DEVICE_TYPES = ("phone", "tablet", "watch", "dap", "sdcard", "folder")


def _validated_device_type(value):
    if value not in DEVICE_TYPES:
        abort(400, description=_("Unknown device type."))
    return value


def _validated_transcode_format(value):
    """None (originals) or one of the supported MP3 formats (#97)."""
    fmt = value or None
    if fmt is not None and fmt not in sync_state.TRANSCODE_FORMATS:
        abort(400, description=_("Unsupported transcode format."))
    return fmt


# The icons a device can be shown with, whatever its type: a picture, never
# behaviour. Generic shapes, no brand marks. A client that meets an id it
# does not know draws the type's own icon, so the set can grow.
DEVICE_ICONS = (
    "phone", "phone-button", "tablet", "tablet-small", "clickwheel", "cassette",
    "dap-buttons", "headphones", "watch", "sdcard", "usb", "folder",
)


def _device_list(conn, user_id: int) -> list[dict]:
    """Every device the user may see, with its auto-fit and sync status, read
    in one transaction: a device deleted while the list is built (in another
    tab, by another user) is then either wholly in it or wholly out, instead of
    a status lookup finding its row gone and failing the whole list."""
    conn.execute("BEGIN")
    try:
        rows = _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))
        out = []
        for r in rows:
            d = dict(r)
            d["autofit"] = sync_state.autofit_status(conn, d["id"])
            d["sync_status"] = sync_state.sync_status(conn, d["id"])
            out.append(d)
        return out
    finally:
        conn.rollback()  # read-only: nothing to keep


def _device_usage(conn, user_id: int, device_id: int) -> dict:
    _require_device_access(conn, user_id, device_id)
    track_ids = sync_state.required_track_ids_for_device(conn, device_id)
    # A track the user chose to leave deleted is still
    # nominally "required" by its selection, but deliberately isn't
    # occupying any space on the device — counting it here would
    # overstate real usage and could show a false over-limit.
    excluded_ids = {row["track_id"] for row in conn.execute(
        "SELECT track_id FROM device_track_state WHERE device_id = ? AND status = 'excluded'",
        (device_id,),
    )}
    track_ids -= excluded_ids
    used_bytes = 0
    if track_ids:
        # Real on-device bytes where the client reported them at ack
        # (a transcoding device writes MP3s much smaller than
        # the FLAC originals); tracks.size as the fallback for
        # not-yet-synced tracks and clients that don't report.
        placeholders = ",".join("?" * len(track_ids))
        used_bytes = conn.execute(
            f"SELECT COALESCE(SUM(COALESCE(dts.bytes_on_device, t.size)), 0) AS total "
            f"FROM tracks t LEFT JOIN device_track_state dts "
            f"ON dts.track_id = t.id AND dts.device_id = ? "
            f"WHERE t.id IN ({placeholders})",
            (device_id, *track_ids),
        ).fetchone()["total"]
    device = conn.execute(
        "SELECT max_size_bytes, reported_free_bytes, reported_total_bytes, free_bytes_reported_at, "
        "reported_foreign_bytes, foreign_bytes_reported_at "
        "FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    max_size = device["max_size_bytes"] or None  # a 0 stored before it meant null
    free_bytes = device["reported_free_bytes"]
    total_bytes = device["reported_total_bytes"]
    foreign_bytes = device["reported_foreign_bytes"]
    device_used_bytes = (total_bytes - free_bytes) if (total_bytes is not None and free_bytes is not None) else None
    return ({
        "used_bytes": used_bytes, "max_size_bytes": max_size,
        "track_count": len(track_ids),
        "over_limit": max_size is not None and used_bytes > max_size,
        # The limit is on the FOLDER, so the honest over-limit test
        # counts what is already in it. Kept beside `over_limit` rather
        # than replacing it: that one is still the right question for
        # "is Trobar itself over its share", and a device that has
        # never reported foreign bytes can only answer the first.
        "folder_over_limit": (
            max_size is not None and foreign_bytes is not None
            and (used_bytes + foreign_bytes) > max_size
        ),
        "reported_free_bytes": free_bytes,
        "reported_total_bytes": total_bytes,
        # whole-device storage usage — distinct from used_bytes, which is
        # only Trobar's own share of it (the device may have other
        # apps/files using the rest).
        "device_used_bytes": device_used_bytes,
        "free_bytes_reported_at": device["free_bytes_reported_at"],
        # Music in the sync folder that Trobar did not put there. null
        # is "this device has never reported it" -- an older client, or
        # one that has not synced since upgrading -- and the UI says so
        # rather than drawing a zero, because a folder nobody measured
        # and an empty folder are not the same claim.
        "reported_foreign_bytes": foreign_bytes,
        "foreign_bytes_reported_at": device["foreign_bytes_reported_at"],
        # What the folder actually holds against its limit: foreign
        # content plus Trobar's own. This is the number the limit is
        # about, and it is the one that can exceed it on a fresh
        # install pointed at a folder that already had music in it.
        "folder_used_bytes": (used_bytes + foreign_bytes) if foreign_bytes is not None else None,
        # the limit set in the web UI claiming more space than the
        # device's own storage actually has free right now (independent
        # of how much is already used by this sync's own files).
        "limit_exceeds_physical_capacity": (
            max_size is not None and free_bytes is not None and max_size > (used_bytes + free_bytes)
        ),
    })


def _send_projection(conn, user_id: int, sel_type: str, targets: list[str]) -> dict:
    """What sending these items would add to each device the user may see,
    for the bars in the Sync to… picker. Read-only.

    `new_bytes` counts only the tracks the device doesn't already have or
    require: not required by another selection, not already downloaded, and
    not excluded there (an excluded track is never re-queued). It is sized
    the way autofit sizes a device's tracks, so a transcoding device gets
    its estimate. `on_device_bytes` is what of `used_bytes` is downloaded
    already: the device's reported free space is room beside that, and the
    pending rest still has to fit in it, which is what the phone's own
    check before downloading compares. `reserve_bytes` is the space the
    device keeps free on its volume, as it last reported it (null: it hasn't),
    which its check also won't let a sync eat into."""
    # One read transaction, as _device_list reads: a device deleted while
    # this runs (in another tab, by another user) is then wholly in it or
    # wholly out, instead of its usage lookup failing the whole projection.
    conn.execute("BEGIN")
    try:
        item_ids: set[int] = set()
        for target in targets:
            _require_playlist_visible(conn, user_id, sel_type, target)
            item_ids |= sync_state._resolve_selection_track_ids(conn, sel_type, target)
        item_bytes = 0
        if item_ids:
            placeholders = ",".join("?" * len(item_ids))
            item_bytes = conn.execute(
                f"SELECT COALESCE(SUM(size), 0) FROM tracks WHERE id IN ({placeholders})", tuple(item_ids),
            ).fetchone()[0]
        devices = {}
        for d in _device_rows_for_user(conn, user_id, _is_admin(conn, user_id)):
            usage = _device_usage(conn, user_id, d["id"])
            state = {r["track_id"]: r["status"] for r in conn.execute(
                "SELECT track_id, status FROM device_track_state WHERE device_id = ?", (d["id"],))}
            required = sync_state.required_track_ids_for_device(conn, d["id"])
            new_ids = {t for t in item_ids - required if state.get(t) not in ("downloaded", "excluded")}
            on_device = {t for t in required if state.get(t) == "downloaded"}
            devices[str(d["id"])] = {
                "new_tracks": len(new_ids),
                "new_bytes": sync_state._sum_device_bytes(conn, new_ids, d["id"], d["transcode_format"]),
                "used_bytes": usage["used_bytes"],
                "on_device_bytes": sync_state._sum_device_bytes(conn, on_device, d["id"], d["transcode_format"]),
                "max_size_bytes": usage["max_size_bytes"],
                "reported_free_bytes": usage["reported_free_bytes"],
                "reserve_bytes": conn.execute(
                    "SELECT reported_reserve_bytes FROM devices WHERE id = ?", (d["id"],)).fetchone()[0],
            }
        return {"tracks": len(item_ids), "bytes": item_bytes, "devices": devices}
    finally:
        conn.rollback()  # read-only: nothing to keep


# The web Library groups albums by album artist (#240): a compilation once,
# under Various Artists, and a guest track with its album. The App API keeps
# _library_artists/_library_albums above, by track artist, for an app older
# than level 6, and serves this grouping beside them.
_ALBUM_ARTIST = "COALESCE(album_artist, artist)"


def _library_stats(conn) -> dict:
    rows = conn.execute(
        "SELECT relative_path, size, duration, year FROM tracks WHERE deleted_at IS NULL"
    ).fetchall()
    total_duration = 0.0
    by_codec: dict[str, dict[str, int]] = {}
    by_decade: dict[str, int] = {}
    for row in rows:
        total_duration += row["duration"] or 0

        path = row["relative_path"]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "unknown"
        codec = by_codec.setdefault(ext, {"tracks": 0, "bytes": 0})
        codec["tracks"] += 1
        codec["bytes"] += row["size"] or 0

        if row["year"]:
            decade = f"{(row['year'] // 10) * 10}s"
            by_decade[decade] = by_decade.get(decade, 0) + 1

    return {
        "total_tracks": len(rows),
        "total_duration_seconds": int(total_duration),
        "by_codec": by_codec,
        "by_decade": dict(sorted(by_decade.items())),
    }


def _library_similar_artists(conn, user_id: int, artist: str) -> list[str]:
    artist = artist.strip()
    if not artist:
        return []
    row = conn.execute("SELECT lastfm_api_key FROM users WHERE id = ?", (user_id,)).fetchone()
    api_key = (row["lastfm_api_key"] if row else None) or db.get_config(conn, "lastfm_api_key_default") or ""
    similar = lastfm.similar_artists(artist, api_key, limit=40, api_base=db.get_config(conn, "lastfm_api_base") or "")
    if not similar:
        return []
    library = {r["artist"].lower(): r["artist"] for r in conn.execute(
        "SELECT DISTINCT artist FROM tracks WHERE deleted_at IS NULL"
    )}
    out: list[str] = []
    seen: set[str] = set()
    for name in similar:
        if name.lower() == artist.lower():
            continue  # skip the artist itself
        exact = library.get(name.lower())
        if exact and exact not in seen:
            seen.add(exact)
            out.append(exact)
        if len(out) >= 8:
            break
    return out


def _library_album_artists(conn) -> list[dict]:
    """Every artist with albums of their own (by album artist), plus every
    artist who only appears on others' albums: album_count counts the
    former, appears_on_count the latter."""
    own: dict[str, dict] = {}
    for r in conn.execute(
            f"SELECT {_ALBUM_ARTIST} AS artist, COUNT(*) AS track_count, COUNT(DISTINCT album) AS album_count "
            f"FROM tracks WHERE deleted_at IS NULL GROUP BY {_ALBUM_ARTIST}"):
        own[r["artist"]] = {"artist": r["artist"], "track_count": r["track_count"],
                            "album_count": r["album_count"], "appears_on_count": 0, "albums": []}
    for r in conn.execute(
            # By album and album artist, as _library_appears_on lists them, so
            # the count and the list agree when two albums share a name.
            f"SELECT artist, COUNT(DISTINCT album || char(0) || {_ALBUM_ARTIST}) AS n FROM tracks "
            f"WHERE deleted_at IS NULL AND artist != {_ALBUM_ARTIST} GROUP BY artist"):
        row = own.setdefault(r["artist"], {"artist": r["artist"], "track_count": 0, "album_count": 0,
                                           "appears_on_count": 0, "albums": []})
        row["appears_on_count"] = r["n"]
    for r in conn.execute(
            f"SELECT {_ALBUM_ARTIST} AS artist, album FROM tracks WHERE deleted_at IS NULL AND album != '' "
            f"GROUP BY {_ALBUM_ARTIST}, album ORDER BY album"):
        own[r["artist"]]["albums"].append(r["album"])
    return sorted(own.values(), key=lambda a: a["artist"])


def _library_album_artist_albums(conn, artist: str) -> list[dict]:
    """The artist's own albums: those it is the album artist of."""
    rows = conn.execute(
        "SELECT album, MAX(year) AS year, MAX(reissue_year) AS reissue_year, "
        f"COUNT(*) AS track_count FROM tracks WHERE deleted_at IS NULL AND {_ALBUM_ARTIST} = ? "
        "GROUP BY album ORDER BY year IS NULL, year DESC, album",
        (artist,),
    ).fetchall()
    return [dict(r) for r in rows]


def _library_appears_on(conn, artist: str) -> list[dict]:
    """Albums by other album artists with this artist on them, and how many
    of their tracks are this artist's ("Also appears on")."""
    rows = conn.execute(
        f"SELECT album, {_ALBUM_ARTIST} AS album_artist, MAX(year) AS year, COUNT(*) AS track_count "
        f"FROM tracks WHERE deleted_at IS NULL AND artist = ? AND {_ALBUM_ARTIST} != ? "
        "GROUP BY album, album_artist ORDER BY year IS NULL, year DESC, album",
        (artist, artist),
    ).fetchall()
    return [dict(r) for r in rows]


def _library_album_tracks(conn, artist: str, album: str) -> list[dict]:
    """An album's live tracks in disc and track order: the album as an album
    selection of `artist||album` resolves it (sync_state, album type), so
    the list shows what selecting the album syncs."""
    rows = conn.execute(
        "SELECT id, track_no, disc_no, title, artist, album_artist, duration FROM tracks "
        "WHERE deleted_at IS NULL AND album = ? AND (album_artist = ? OR artist = ?) "
        "ORDER BY disc_no IS NULL, disc_no, track_no IS NULL, track_no, relative_path",
        (album, artist, artist),
    ).fetchall()
    return [dict(r) for r in rows]


def _library_cover_row(conn, artist: str, album: str):
    """The one track row a cover lookup needs; the disk/NFS work happens
    after the connection is closed, so callers split the two."""
    return conn.execute(
        "SELECT relative_path FROM tracks WHERE deleted_at IS NULL "
        "AND album = ? AND (album_artist = ? OR artist = ?) LIMIT 1",
        (album, artist, artist),
    ).fetchone()


def _cover_response(artist: str, album: str, row, *, small: bool = False) -> Response:
    if row is None:
        abort(404)
    # Disk-cached: only the first browse of an album touches NFS.
    track_path = _contained(db.get_music_root(), row["relative_path"])
    get = covers.get_small_cover if small else covers.get_cover
    cover = get(artist, album, track_path) if track_path else None
    if cover is None:
        abort(404)
    data, mime = cover
    return Response(data, mimetype=mime, headers={"Cache-Control": "public, max-age=86400"})


def _artist_image_config(conn) -> tuple:
    return _active_provider(conn), db.get_config(conn, "audiodb_api_key")


def _artist_image_response(artist: str, provider, audiodb_key, *, small: bool = False) -> Response:
    if not artist:
        abort(404)
    found = artist_images.get_artist_image(artist, provider, audiodb_key)
    if found is None:
        abort(404, description=_("No artist image available"))
    data, content_type = found
    if small:  # the phone-grid / DAP variant the device API already serves
        data, content_type = artist_images.downscale(data, content_type)
    return Response(data, mimetype=content_type, headers={"Cache-Control": "public, max-age=86400"})


def _months_ago_iso(months: int) -> str:
    """ISO date cutoff for a "within N months" filter — a 30-day-per-month
    approximation, not calendar-exact, which is plenty for a "recent"
    threshold nobody's going to audit to the day. String-comparable
    against both scanned_at (has a time component, same YYYY-MM-DD
    prefix ordering) and release_date (plain YYYY-MM-DD). Upper-bounded
    as well as lower-bounded — the frontend already clamps to 1-24, but
    this reads straight off request.args, and an unclamped large value
    overflows datetime's range (OverflowError -> unhandled 500) rather
    than degrading gracefully. 1200 months = 100 years, comfortably past
    any real "recently released" use case."""
    months = max(1, min(months, 1200))
    return (datetime.now() - timedelta(days=30 * months)).strftime("%Y-%m-%d")


def _recently_added_for(conn, user_id: int, months: int) -> list[dict]:
    user_device_ids = {row["id"] for row in _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))}
    return suggestions.recently_added_widget(conn, _months_ago_iso(months), user_device_ids)


def _recently_released_for(conn, user_id: int, months: int) -> list[dict]:
    user_device_ids = {row["id"] for row in _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))}
    return suggestions.recently_released_widget(conn, _months_ago_iso(months), user_device_ids)


def _gravatar_url(email: str) -> str:
    digest = hashlib.md5((email or "").strip().lower().encode("utf-8")).hexdigest()
    # d=404 so a not-found gravatar 404s instead of returning a placeholder
    # image — lets the frontend fall back to its own default via @error.
    return f"https://www.gravatar.com/avatar/{digest}?d=404&s=80"


def _basket_last_destinations_dict(raw: str | None) -> dict:
    """Thin alias. The parser lives in sync_state alongside the prune/remap
    that device deletion and device transfer need, since transfer_device()
    applies one inside its own transaction and cannot import this module."""
    return sync_state.basket_last_destinations_dict(raw)


# Widget ids a non-admin must never see as enabled, kept in sync by hand
# with the frontend catalog's adminOnly flags (templates/index.html,
# dashboardWidgetCatalog). Belt-and-suspenders, not a real privilege
# boundary — the Administration card is only ever a shortcut/preview, the
# actual admin routes are already separately gated by require_admin/
# is_admin elsewhere, so this only prevents a UI inconsistency.
# The home dashboard's widget catalog, in default order: the one copy. The
# web client reads it from /api/dashboard/catalog and the phone from the
# same route under the App API prefix, so neither carries a list of its own;
# the labels in _build_js_i18n() are keyed by these ids and a test holds
# the two together. A client still needs rendering code per id -- the
# catalog governs order, the admin-only flag and unknown-id handling.
DASHBOARD_WIDGET_CATALOG = (
    ("library", False),
    ("devices", False),
    ("suggestions", False),
    ("recently_added", False),
    ("recently_released", False),
    ("most_played", False),
    ("administration", True),
)


ADMIN_ONLY_WIDGETS = {wid for wid, admin_only in DASHBOARD_WIDGET_CATALOG if admin_only}

# The app's Home has one more: the browser's Playlists page already shows this.
APP_DASHBOARD_WIDGET_CATALOG = (*DASHBOARD_WIDGET_CATALOG, ("playlists", False))
PLAYLISTS_WIDGET = "playlists"
PLAYLISTS_WIDGET_LIMIT_RANGE = (1, 10)
DEFAULT_PLAYLISTS_WIDGET_LIMIT = 3


# #269: the home cover grid is grid-cols-3 sm:grid-cols-4 md:grid-cols-5 —
# only multiples of 15 fill complete rows at both the 3-col mobile and 5-col
# desktop widths (60 also being clean at the 4-col tablet width, as LCM(3,4,5)).
# A curated set, not a free integer, so a user can't pick a count that leaves
# a ragged row — and the server clamps to it too, since settings.cover_limit
# arrives over the API and a client isn't the only way to set it.
DASHBOARD_COVER_LIMITS = (15, 30, 45, 60)


DEFAULT_COVER_LIMIT = 15


# The two widgets with a per-widget setting: how many months back their
# cover grid looks. The web client clamps this to 1-24 before sending; the
# server used to store whatever arrived, which was fine with one client
# writing and is not with two. The stored value is now held to the same
# range, and anything else under settings is dropped rather than kept --
# a per-user JSON column is not a place for keys nothing reads.
WIDGET_MONTHS_RANGE = (1, 24)


MONTHS_WIDGETS = ("recently_added", "recently_released")


# The three widgets that draw a cover grid, each with its own cover count,
# settings[<widget>]["cover_limit"]. It used to be one value for all three,
# settings["cover_limit"], and that key is kept in every answer, holding
# Suggestions' count: an app built before the split reads it, and applies
# it to all three. A stored set with only the old single value reads as
# that value for each of the three, so nothing looks different after the
# upgrade. A PATCH may still send the single value; it sets all three.
COVER_WIDGETS = ("suggestions", "recently_added", "recently_released")


def _widget_months(value):
    """The stored form of a months setting, or None if the value is not a
    whole number in range. bool is excluded on purpose: True is 1 to int()."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    lo, hi = WIDGET_MONTHS_RANGE
    return value if lo <= value <= hi else None


def _sanitize_widget_settings(settings) -> dict:
    """The tolerant half of settings validation, for values already stored
    or arriving through the whole-profile PUT: keep what is valid, fall
    back or drop what is not, never fail. The strict half, for the PATCH
    route, is _validate_dashboard_widgets_patch below."""
    settings = settings if isinstance(settings, dict) else {}
    result: dict = {}
    single = _cover_limit(settings.get("cover_limit")) or DEFAULT_COVER_LIMIT
    for widget in COVER_WIDGETS:
        entry = settings.get(widget) if isinstance(settings.get(widget), dict) else {}
        result[widget] = {"cover_limit": _cover_limit(entry.get("cover_limit")) or single}
        if widget in MONTHS_WIDGETS:
            months = _widget_months(entry.get("months"))
            if months is not None:
                result[widget]["months"] = months
    result["cover_limit"] = result[COVER_WIDGETS[0]]["cover_limit"]
    entry = settings.get(PLAYLISTS_WIDGET) if isinstance(settings.get(PLAYLISTS_WIDGET), dict) else {}
    result[PLAYLISTS_WIDGET] = {"limit": _playlists_limit(entry.get("limit")) or DEFAULT_PLAYLISTS_WIDGET_LIMIT}
    return result


def _playlists_limit(value):
    """The stored form of the Playlists widget's row count, or None if it is
    not one. bool is excluded: True == 1 in Python."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    lo, hi = PLAYLISTS_WIDGET_LIMIT_RANGE
    return value if lo <= value <= hi else None


def _cover_limit(value):
    """The stored form of a cover count, or None if it is not one of the
    allowed values. bool is excluded: True == 1 in Python."""
    if isinstance(value, bool) or value not in DASHBOARD_COVER_LIMITS:
        return None
    return value


def _normalize_dashboard_widgets(parsed) -> dict:
    """Sanitizes an already-parsed dashboard_widgets value (dict, None, or
    any other JSON-decoded shape), tolerating garbage by falling back to
    "nothing disabled, no per-widget settings, no saved order" — same shape
    a brand-new user gets."""
    default: dict = {"disabled": [], "order": [], "settings": _sanitize_widget_settings(None)}
    if not isinstance(parsed, dict):
        return default
    disabled = parsed.get("disabled")
    order = parsed.get("order")
    settings = _sanitize_widget_settings(parsed.get("settings"))
    return {
        "disabled": disabled if isinstance(disabled, list) else [],
        # #263: an empty/missing/malformed order isn't sanitized further
        # here — the widget catalog it's ordering lives client-side, so
        # "does this id still exist" and "append anything missing" are the
        # frontend's job (widgetOrder()), not this function's.
        "order": order if isinstance(order, list) else [],
        "settings": settings,
    }


def _dashboard_widgets_dict(raw: str | None) -> dict:
    """Parses the users.dashboard_widgets JSON column, tolerating NULL/
    missing/malformed text (a fresh column, or a hand-edited DB)."""
    if not raw:
        return _normalize_dashboard_widgets(None)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    return _normalize_dashboard_widgets(parsed)


def _validate_dashboard_widgets_patch(body: dict) -> dict:
    """The strict half: a PATCH names exactly what it wants changed, and
    anything malformed is a 400 with the field named, never silently
    corrected. Only the keys present in the request come back. Widget ids
    in `disabled` and `order` are not checked against the catalog, on
    purpose -- the catalog lives client-side and an unknown id is the
    client's to carry, not the server's to reject (see the order comment
    in _normalize_dashboard_widgets)."""
    patch: dict = {}
    for key in ("disabled", "order"):
        if key in body:
            ids = body[key]
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                abort(400, description=_("Widget ids must be a list of strings"))
            patch[key] = ids
    if "settings" in body:
        settings = body["settings"]
        if not isinstance(settings, dict):
            abort(400, description=_("Widget settings must be an object"))
        clean: dict = {}
        for key, value in settings.items():
            if key == "cover_limit":
                # The single value an app built before the split sends.
                if _cover_limit(value) is None:
                    abort(400, description=_("Cover limit is not one of the allowed values"))
                clean[key] = value
            elif key in COVER_WIDGETS:
                if not isinstance(value, dict) or not value:
                    abort(400, description=_("Unknown widget setting"))
                entry: dict = {}
                for field, v in value.items():
                    if field == "cover_limit":
                        if _cover_limit(v) is None:
                            abort(400, description=_("Cover limit is not one of the allowed values"))
                        entry[field] = v
                    elif field == "months" and key in MONTHS_WIDGETS:
                        if _widget_months(v) is None:
                            abort(400, description=_("Months must be a whole number from 1 to 24"))
                        entry[field] = v
                    else:
                        abort(400, description=_("Unknown widget setting"))
                clean[key] = entry
            elif key == PLAYLISTS_WIDGET:
                if not isinstance(value, dict) or set(value) != {"limit"} or _playlists_limit(value["limit"]) is None:
                    abort(400, description=_("Unknown widget setting"))
                clean[key] = value
            else:
                abort(400, description=_("Unknown widget setting"))
        patch["settings"] = clean
    return patch


def _apply_dashboard_widgets_patch(conn, user_id: int, body, device_id: int | None = None) -> dict:
    """Applies a PATCH to the account's browser set, or with [device_id] to
    that paired device's own set -- the same validation, merge and
    admin-only union either way."""
    if not isinstance(body, dict):
        abort(400, description=_("Widget settings must be an object"))
    patch = _validate_dashboard_widgets_patch(body)
    if device_id is None:
        widgets = _stored_dashboard_widgets(conn, user_id)
    else:
        widgets = _device_dashboard_widgets(conn, device_id, user_id, commit=False)
    if "disabled" in patch:
        widgets["disabled"] = patch["disabled"]
    if "order" in patch:
        widgets["order"] = patch["order"]
    if "settings" in patch:
        # Field by field within each widget, so a months change keeps that
        # widget's cover count and the other way round. The single value an
        # older app sends sets all three counts.
        merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in widgets["settings"].items()}
        changes = patch["settings"]
        if "cover_limit" in changes:
            for widget in COVER_WIDGETS:
                merged.setdefault(widget, {})["cover_limit"] = changes["cover_limit"]
        for widget, fields in changes.items():
            if isinstance(fields, dict):
                merged.setdefault(widget, {}).update(fields)
        widgets["settings"] = merged
    if not _is_admin(conn, user_id):
        widgets["disabled"] = sorted(set(widgets["disabled"]) | ADMIN_ONLY_WIDGETS)
    widgets = _normalize_dashboard_widgets(widgets)
    if device_id is None:
        conn.execute("UPDATE users SET dashboard_widgets = ? WHERE id = ?",
                     (json.dumps(widgets), user_id))
    else:
        conn.execute("UPDATE devices SET dashboard_widgets = ? WHERE id = ?",
                     (json.dumps(widgets), device_id))
    conn.commit()
    return widgets


def _stored_dashboard_widgets(conn, user_id: int) -> dict:
    row = conn.execute("SELECT dashboard_widgets FROM users WHERE id = ?", (user_id,)).fetchone()
    return _dashboard_widgets_dict(row["dashboard_widgets"])


def _device_dashboard_widgets(conn, device_id: int, user_id: int, *, commit: bool = True) -> dict:
    """A paired device's own Home widget set. A device that has never had
    one -- a new pairing, or an upgrade -- gets a copy of the account's
    current browser set, stored on this first read, so nobody's Home
    rearranges itself and a later browser change no longer moves it."""
    row = conn.execute("SELECT dashboard_widgets FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row["dashboard_widgets"] is not None:
        return _dashboard_widgets_dict(row["dashboard_widgets"])
    widgets = _stored_dashboard_widgets(conn, user_id)
    conn.execute("UPDATE devices SET dashboard_widgets = ? WHERE id = ? AND dashboard_widgets IS NULL",
                 (json.dumps(widgets), device_id))
    if commit:
        conn.commit()
    return widgets


def _spotify_experimental_enabled(conn) -> bool:
    """#398: gated separately from _spotify_oauth_client's configured-or-not
    check. Credentials can remain set (an admin toggling this off to pause
    the feature shouldn't have to also discard client_id/secret) while the
    feature itself stays off -- so "configured" and "enabled" are two
    different questions, each with its own guard below."""
    return db.get_config(conn, "experimental_spotify_enabled") == "1"


def _is_valid_url(url: str) -> bool:
    """#509: a malformed URL in any of the admin config's provider fields
    (subsonic_url, jellyfin_url, emby_url, plex_url, lms_url,
    mirror_subsonic_url, mirror_jellyfin_url, mirror_emby_url, mirror_plex_url, lidarr_url,
    lastfm_api_base, listenbrainz_api_base) used to save happily and only
    surface later, at request time, as a connection error indistinguishable
    from "the target is genuinely unreachable" — reading as "the server is
    down" rather than "you typed the URL wrong" (a missing colon in
    "http//host" being the concrete case that prompted this). This is a
    cheap well-formedness check, not a reachability check — that's what
    each provider's own reconnect()/status() already does separately, right
    after this passes. scheme must be http/https (no bare hostnames, no
    other schemes) and a host must be present."""
    parsed = urlsplit(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _invalid_url_message(field_label: str) -> str:
    """#512 review: the eleven call sites below used to share one fully
    generic message ("Enter a valid http:// or https:// URL.") with no
    indication of WHICH field was wrong — fine the moment it's typed (the
    admin is looking right at the field that just changed), genuinely
    unhelpful on an already-saved value from before this validation
    existed: any save of ANY unrelated setting sends the whole adminConfig
    object, re-triggers this check against a field the admin isn't even
    touching, and 400s with a message that gives no hint which of up to
    eleven fields to go fix. `field_label` is a stable, English identifier
    (not itself translated — same "translated prefix + appended untranslated
    detail" split mirror.py's own _set_error already uses for detail that
    can't be a locale-aware sentence fragment), appended after the
    translated sentence rather than interpolated inside it, so it can't
    land mid-word for a language whose grammar orders things differently."""
    return f"{_('Enter a valid http:// or https:// URL.')} ({field_label})"


# Held by api_admin_provider_switch from its last "is a sync running?"
# check until it has queued its own first sync, and taken (without waiting)
# by the scheduled sync below. A scheduled sync queued inside that window
# would list the old provider while the switch removes its playlists. One
# process runs both the routes and the job workers, so a lock is enough.
_PROVIDER_SWITCH_LOCK = threading.Lock()


def _trusted_proxy() -> str:
    """#383, follow-up to #382: waitress's own proxy-trust
    gate (see the serve() call in __main__) decides which peers ProxyFix
    ever sees X-Forwarded-For from at all — and by extension, which peers
    _client_ip() above can trust. Default "*" (any peer) is correct for the
    shipped docker-compose.yaml (only Traefik can reach this container's
    port), but is a deployment guarantee, not a code one: a directly
    reachable port makes "*" trust an attacker's own header just as readily,
    silently reviving the rate-limiter bypass #382 fixed.

    `... or "*"` rather than get()'s own default: waitress's str_iftruthy
    coercion treats an empty string the same as unset — turns it into
    trusted_proxy=None, matching NO peer rather than every peer. .env.example
    ships this key uncommented, so TROBAR_TRUSTED_PROXY= (blanked rather than
    deleted) is a realistic edit, and it's the worse failure mode of the two:
    no peer trusted means X-Forwarded-For gets stripped entirely, ProxyFix
    falls back to the raw socket peer (the proxy itself), and every real
    user behind it collapses into one shared rate-limit bucket keyed on the
    proxy's own address — one mistyped password can lock out the whole
    household. `get(..., "*")` alone doesn't catch this; the empty string
    would sail through as a "set" value.

    Extracted to its own function purely so the env-var read is
    unit-testable without invoking waitress itself."""
    return os.environ.get("TROBAR_TRUSTED_PROXY") or "*"


# --- Directly-exposed-deployment signal (#389) ----------------------------
# #383/#394 established that TROBAR_TRUSTED_PROXY="*" only defeats the rate
# limiter if this container's port is reachable by more than the one reverse
# proxy it's meant for — but nothing about a single request can tell "no
# proxy, safely isolated" from "no proxy, silently exposed" apart: an
# attacker's spoofed X-Forwarded-For looks no different from a real one.
#
# The signal that actually works: a single reverse proxy always presents as
# the SAME raw socket peer across requests (its own address); a directly
# reachable port sees a different peer per real client. werkzeug's ProxyFix
# keeps that raw, pre-rewrite peer in environ["werkzeug.proxy_fix.orig"]
# unconditionally on every request (verified against the installed
# werkzeug's ProxyFix.__call__ — populated whether or not X-Forwarded-For
# was even present, not just when a rewrite happened).
#
# Timestamped rather than a bare set: a long-running instance's one real
# proxy can still get a new address over time (container recreate, #394's
# own Docker-IP-instability point) without ever being reachable by anyone
# else. Counting only peers seen in the last week keeps that from slowly
# crossing the threshold on its own; the cap is a memory bound, not a
# tuning knob (only whether the count clears the threshold matters).
_EXPOSURE_LOCK = threading.Lock()


_exposure_peers: dict[str, float] = {}


_EXPOSURE_WINDOW_S = 7 * 24 * 3600


def _exposure_peer_count() -> int:
    """Distinct raw peers seen in the last _EXPOSURE_WINDOW_S — the number
    the Library-health panel shows. A getter rather than reading
    _exposure_peers directly so callers don't need the lock or the window
    logic duplicated."""
    cutoff = time.time() - _EXPOSURE_WINDOW_S
    with _EXPOSURE_LOCK:
        return sum(1 for seen in _exposure_peers.values() if seen >= cutoff)


def _active_provider_id(conn) -> str:
    """Same fallback logic as _active_provider, as the string key instead
    of the module — for persisting which provider synced a playlist."""
    pid = db.get_config(conn, "provider")
    return pid if pid in _PROVIDERS else "roon"


# Unbound here: main.py binds it with init_app(app) before anything is
# registered on it (see the note there).
oauth = OAuth()


def _end_other_sessions(conn, user_id: int) -> None:
    """Every session of this user ends, except the current request's own if
    it is that user's (a password change keeps the person making it signed
    in). Does not commit."""
    conn.execute("UPDATE users SET session_generation = session_generation + 1 WHERE id = ?", (user_id,))
    if session.get("local_user_id") == user_id:
        session["session_generation"] = conn.execute(
            "SELECT session_generation FROM users WHERE id = ?", (user_id,)).fetchone()[0]


def require_admin(conn) -> None:
    if not _is_admin(conn, get_current_user_id(conn)):
        abort(403, description=_("Admin access required"))


def _admin_counts(conn) -> dict:
    """What the Administration widget shows: how many accounts, how many
    delegations in force. Counts only -- the full listings stay behind the
    admin routes and never reach a phone."""
    users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    delegations = conn.execute("SELECT COUNT(*) AS n FROM device_delegations").fetchone()["n"]
    return {"users": users, "delegations": delegations}


def _music_root_is_readonly(root: Path | None = None) -> bool | None:
    """True if the music library is a read-only mount, False if writable, None
    if the path doesn't exist yet. Defaults to the effective MUSIC_ROOT but
    accepts an explicit path (used by the setup wizard to check a just-entered
    folder). Uses the mount flag (ST_RDONLY), which reflects docker's ':ro'
    where file-mode bits wouldn't. Trobar never writes to the
    library, so writable = worth flagging (a bug/compromise could alter files)."""
    try:
        root = root or db.get_music_root()
        if not root.exists():
            return None
        return bool(os.statvfs(root).f_flag & os.ST_RDONLY)
    except OSError:
        return None


def _config_field_error(field: str, message: str) -> NoReturn:
    """A 400 from PUT /api/admin/config that names the setting it rejects,
    by its adminConfig key, so the page can open the section holding that
    field and focus it. `error` is the same translated message as before."""
    resp = jsonify({"error": message, "field": field})
    resp.status_code = 400
    abort(resp)


def _effective_folder(body: dict, key: str, env_var: str, current: Path | None) -> Path | None:
    """What a folder setting will actually RESOLVE TO once this PUT is
    applied — which is the only value worth validating.

    Three cases, and the third is the one that is easy to miss:

    - the field is absent: `current`, the effective value already (both
      getters are env-aware);
    - the field carries a path: that path, normalized;
    - the field is present but EMPTY: not None, but whatever `env_var`
      falls back to. Clearing the setting clears the *override*, and the
      environment default underneath it becomes effective again. Validating
      the cleared field as "unset" would let a save that re-exposes an
      overlapping environment default straight through.

    The written value is still None for that last case — the store keeps
    the override, not the resolved value. So validation and persistence
    deliberately look at different things here.

    Needed at all because the two folder settings are validated against
    each other and the admin form PUTs the whole config object at once (see
    _invalid_url_message's docstring for the same shape biting the URL
    checks): comparing a folder against the other's *stored* value would
    pass a save that changes both into an overlapping pair, and would 400 a
    save that fixes an overlap by moving the other one."""
    if key not in body:
        return current
    raw = (body.get(key) or "").strip()
    if raw:
        return Path(os.path.normpath(raw))
    env = os.environ.get(env_var)
    return Path(env) if env else None


def _folders_overlap(a: Path, b: Path) -> bool:
    """True when one folder is the other, or contains it.

    Lexical (normpath + Path.parents), never resolve() — same reasoning as
    mirror._safe_path()'s: an admin-typed ".." must collapse before the
    comparison, but a Docker bind-mounted (symlinked) folder must still
    compare as the path the admin typed. Both sides are normalized here
    rather than at the call sites, since one side is usually a stored
    value this request did not write."""
    a = Path(os.path.normpath(str(a)))
    b = Path(os.path.normpath(str(b)))
    return a == b or a in b.parents or b in a.parents


def _failed_writes(rows: list, prefix: str) -> tuple[int, str | None]:
    """How many of these playlists' last write to one sink failed, and the
    most recent failure's message (the playlist written last first)."""
    failed = [r for r in rows if r.get(f"{prefix}_enabled")
              and (r.get(f"{prefix}_last_error_code") or r.get(f"{prefix}_last_error"))]
    failed.sort(key=lambda r: r.get(f"{prefix}_last_written_at") or "", reverse=True)
    return len(failed), (failed[0].get(f"{prefix}_last_error") if failed else None)


def _require_playlist_visible(conn, user_id: int, sel_type: str, target: str) -> None:
    """#28: blocks creating/toggling a selection against an owned-and-
    unshared playlist someone else set private — the actual enforcement
    half of the feature, not just hiding it from GET /api/provider/
    playlists. Without this, a household member could still sync a
    playlist their sibling just marked private by POSTing the id
    directly, making "unshare" a purely cosmetic UI toggle. No-op for
    every other selection type, and for a WELL-FORMED playlist id that
    doesn't resolve to a row at all (matches this app's existing lax
    behavior — selections aren't validated against the target's
    existence elsewhere either).

    #434 review: a MALFORMED target ('1_0', not a plain integer under any
    parser) is a different case and must NOT fall into that same lax
    path. sync_state._device_playlists() still resolves a selection's
    target with SQLite's own CAST(? AS INTEGER) (a leading-integer-
    prefix parse) when building a device's .m3u8 files -- so letting a
    malformed target sail through this gate (because
    sync_state.parse_target_id() calls it unparseable and this function
    used to treat "unparseable" the same as "no such row", i.e. allow)
    meant a selection could be created here and then resolved to a
    REAL, possibly-private playlist downstream by that CAST. Rejecting
    outright is what actually closes the gap: a target that cannot name
    any playlist under the strict parser must never become a stored
    selection/basket-item for another reader to reinterpret with a
    looser one."""
    if sel_type != "playlist":
        return
    playlist_id = sync_state.parse_target_id(target)
    if playlist_id is None:
        abort(400, description=_("Invalid playlist."))
    row = conn.execute(
        "SELECT owner_user_id, shared FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None or row["owner_user_id"] is None or row["shared"]:
        return
    if row["owner_user_id"] == user_id or _is_admin(conn, user_id):
        return
    abort(403, description=_("This playlist is private."))


def _revoke_non_owner_playlist_selections(conn, playlist_id: int, owner_user_id: int) -> None:
    """#73: _require_playlist_visible() above only gates *new* selection
    creation — nothing previously re-checked `shared` once a selection
    already existed, so a device that added a playlist while it was still
    shared kept pulling its tracks on every future sync indefinitely,
    even after the owner marked it private. Called right after a PATCH
    flips `shared` 1->0: deletes every selection targeting this playlist
    not created by its owner or by an admin (an admin's own visibility
    was never gated by `shared` in the first place — see GET /api/
    provider/playlists) — delete_selection() already tells every
    affected device to remove the files, the exact same mechanism as an
    explicit user deletion elsewhere in this app. is_admin is checked
    live (current status), same as every other admin check here, not
    whatever it was at selection-creation time."""
    rows = conn.execute(
        "SELECT s.id FROM selections s JOIN users u ON u.id = s.created_by_user_id "
        "WHERE s.type = 'playlist' AND s.target = ? AND s.created_by_user_id != ? AND u.is_admin = 0",
        (str(playlist_id), owner_user_id),
    ).fetchall()
    for row in rows:
        sync_state.delete_selection(conn, row["id"])


def _start_playlist_sync():
    """Start a provider playlist sync in the background: 202, or 409 while
    one runs. Shared by both prefixes; the caller has authenticated."""
    with db.connection() as conn:
        provider_id = _active_provider_id(conn)
        provider = _PROVIDERS[provider_id]
    # #138: kick the sync off in the background and return immediately (202)
    # instead of blocking the request for the minutes a large sync takes — the
    # UI polls .../sync/status and reloads the list as playlists commit (#133).
    result = playlist_sync.start_sync(provider, provider_id)
    if result.get("already_running"):
        # #129: mirror the library-scan endpoint — one sync at a time.
        return jsonify({"error": _("A playlist sync is already running.")}), 409
    return jsonify(result), 202


def _mirror_coverage(conn, playlist_ids) -> dict:
    """{playlist_id: {sink: {"on_target", "total"}}} from each server sink's
    last write (mirror_runs), for the mirror icon's tooltip."""
    out: dict = {}
    for (pid, sink), run in mirror_runs.latest(conn, playlist_ids).items():
        out.setdefault(pid, {})[sink] = {"on_target": run["on_target"], "total": run["total"]}
    return out


def _visible_playlists(conn, user_id: int) -> list:
    """The playlists this user may see, attributed for them: #28 and #81 as the
    browser route's docstring describes. Shared by the browser route and the
    App API, so the two can never disagree about who sees what."""
    admin = _is_admin(conn, user_id)
    rows = conn.execute(
        "SELECT p.id, p.title, p.source_provider, p.inferred_origin_provider, p.last_synced_at, "
        "p.owner_user_id, u.username AS owner_username, p.shared, "
        "(p.owner_user_id = :uid) AS is_own, "
        "sd.name AS source_device_name, sd.device_type AS source_device_type, "
        "sd.icon AS source_device_icon, "
        "p.mirror_enabled, p.mirror_last_error, p.mirror_last_error_code, "
        "p.subsonic_mirror_enabled, p.subsonic_mirror_last_error, "
        "p.subsonic_mirror_last_error_code, "
        "p.jellyfin_mirror_enabled, p.jellyfin_mirror_last_error, "
        "p.jellyfin_mirror_last_error_code, "
        "p.emby_mirror_enabled, p.emby_mirror_last_error, "
        "p.emby_mirror_last_error_code, "
        "p.music_assistant_mirror_enabled, p.music_assistant_mirror_last_error, "
        "p.music_assistant_mirror_last_error_code, "
        "p.plex_mirror_enabled, p.plex_mirror_last_error, p.plex_mirror_last_error_code, "
        "p.lidarr_request_enabled, p.lidarr_request_last_run_at, "
        "p.lidarr_request_last_count, p.lidarr_request_last_error, "
        "p.lidarr_request_last_error_code, "
        "p.golden_source_id, "
        "g.owner_user_id AS golden_owner_user_id, g.shared AS golden_shared, "
        "gu.username AS golden_owner_username, "
        "COUNT(t.id) AS track_count, "
        "SUM(CASE WHEN t.matched_track_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_count, "
        # #200: a correlated subquery, not a third LEFT JOIN — joining
        # both playlist_tracks and unresolved_playlist_tracks here would
        # fan out (each unresolved row multiplying every playlist_tracks
        # row it's grouped alongside), corrupting track_count/
        # matched_count above too.
        "(SELECT COUNT(*) FROM unresolved_playlist_tracks u "
        " WHERE u.playlist_id = p.id AND u.excluded = 0 AND u.accepted = 0) AS unresolved_count, "
        # #494: same correlated-subquery reasoning as unresolved_count
        # just above — this is the "does this playlist have anything
        # Lidarr-requestable at all" eligibility flag (album IS NOT NULL
        # is always false for every Roon/iTunes unresolved row, so those
        # playlists naturally get 0 here without a provider-specific
        # special case).
        # An album looked up in MusicBrainz (inferred_album) counts as much
        # as the source's own.
        "(SELECT COUNT(*) FROM unresolved_playlist_tracks u "
        " WHERE u.playlist_id = p.id AND u.excluded = 0 AND u.accepted = 0 "
        " AND COALESCE(NULLIF(u.album, ''), u.inferred_album, '') != '') AS unresolved_with_album_count "
        "FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id "
        "LEFT JOIN users u ON u.id = p.owner_user_id "
        "LEFT JOIN playlists g ON g.id = p.golden_source_id "
        "LEFT JOIN users gu ON gu.id = g.owner_user_id "
        # A playlist made on a device names it in its key, "<device id>:<path>".
        "LEFT JOIN devices sd ON p.source_provider = 'device' AND sd.id = "
        " CAST(substr(p.source_playlist_id, 1, instr(p.source_playlist_id, ':') - 1) AS INTEGER) "
        "WHERE :admin OR p.owner_user_id IS NULL OR p.shared = 1 OR p.owner_user_id = :uid "
        "GROUP BY p.id ORDER BY p.title",
        {"uid": user_id, "admin": admin},
    ).fetchall()

    # #410: lets the row disable "Mirror to…" (rather than let it
    # succeed and only fail — silently, per-row — on the next write)
    # when there's nothing configured to mirror into. One query outside
    # the loop; the value is the same for every row.
    mirror_folder_configured = db.get_mirror_folder() is not None
    # #189: same reasoning, for the Subsonic/Jellyfin/Emby sinks' own connections.
    subsonic_mirror_configured = db.get_mirror_subsonic_config() is not None
    jellyfin_mirror_configured = db.get_mirror_jellyfin_config() is not None
    emby_mirror_configured = db.get_mirror_emby_config() is not None
    music_assistant_mirror_configured = db.get_mirror_music_assistant_config() is not None
    plex_mirror_configured = db.get_mirror_plex_config() is not None
    # #494: same reasoning, for "Request missing albums" — not a
    # mirror target, but the same "disable rather than let it fail
    # silently later" posture.
    lidarr_request_configured = db.get_lidarr_config() is not None
    # #509: get_lidarr_config() needs FIVE values (url, api_key, root
    # folder, quality profile, metadata profile) — get_lidarr_connection()
    # needs only the first two, which is also what Administration's own
    # "Connected" indicator reflects. Without this split, "not
    # configured" collapsed two genuinely different situations into one
    # hint: no Lidarr connection at all, vs. connected fine but the
    # three profile fields were never chosen — the second one is
    # invisible from Administration (which shows green "Connected" the
    # moment url+api_key work), so the per-playlist hint was sending
    # the admin to look at a screen that agreed with nothing was wrong.
    lidarr_request_connected = db.get_lidarr_connection() is not None

    result = []
    coverage = _mirror_coverage(conn, [r["id"] for r in rows])
    for r in rows:
        d = dict(r)
        d["mirror_coverage"] = coverage.get(r["id"], {})
        # #449: same SQLite-integer-vs-JSON-boolean coercion as
        # is_own/is_pinned in _device_rows_for_user() -- mirror_enabled
        # already got this treatment, is_own/shared hadn't. The App
        # API reads these too (a non-JS consumer), and "booleans are
        # booleans" throughout the API is the rule either way.
        d["is_own"] = bool(d["is_own"])
        d["shared"] = bool(d["shared"])
        d["mirror_enabled"] = bool(d["mirror_enabled"])
        d["mirror_folder_configured"] = mirror_folder_configured
        d["subsonic_mirror_enabled"] = bool(d["subsonic_mirror_enabled"])
        d["subsonic_mirror_configured"] = subsonic_mirror_configured
        d["jellyfin_mirror_enabled"] = bool(d["jellyfin_mirror_enabled"])
        d["jellyfin_mirror_configured"] = jellyfin_mirror_configured
        d["emby_mirror_enabled"] = bool(d["emby_mirror_enabled"])
        d["emby_mirror_configured"] = emby_mirror_configured
        d["music_assistant_mirror_enabled"] = bool(d["music_assistant_mirror_enabled"])
        d["music_assistant_mirror_configured"] = music_assistant_mirror_configured
        d["plex_mirror_enabled"] = bool(d["plex_mirror_enabled"])
        d["plex_mirror_configured"] = plex_mirror_configured
        d["lidarr_request_enabled"] = bool(d["lidarr_request_enabled"])
        d["lidarr_request_configured"] = lidarr_request_configured
        d["lidarr_request_connected"] = lidarr_request_connected
        # #494: exactly the eligibility flag the disabled-button hint
        # needs client-side — popped rather than left as a raw count,
        # since the UI only ever needs "is there anything at all", not
        # the number itself.
        d["lidarr_request_has_albums"] = d.pop("unresolved_with_album_count") > 0
        golden_owner = d.pop("golden_owner_user_id")
        golden_shared = d.pop("golden_shared")
        # golden_owner is non-NULL only when the row is dual-source AND
        # its golden Tidal row still exists (the LEFT JOIN gives NULL
        # once that row is gone — ON DELETE SET NULL also clears the ref
        # on the next sync).
        if d["golden_source_id"] is not None and golden_owner is not None:
            viewer_sees_golden = admin or golden_shared == 1 or golden_owner == user_id
            if viewer_sees_golden:
                continue  # suppress the Roon duplicate; viewer sees the Tidal golden copy
            # else keep + expose golden_owner_username for the "shared by X" badge
        else:
            d["golden_owner_username"] = None
        d.pop("golden_source_id", None)
        result.append(d)
    return result


def _set_playlist_shared(conn, user_id: int, playlist_id: int):
    """The `shared` toggle, owner or admin only; shared by both prefixes."""
    row = conn.execute("SELECT owner_user_id FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if row is None:
        abort(404, description=_("Playlist not found"))
    if row["owner_user_id"] is None:
        abort(400, description=_("This playlist has no owner to change sharing for."))
    if row["owner_user_id"] != user_id and not _is_admin(conn, user_id):
        abort(403, description=_("Unauthorized access to this playlist"))
    body = request.get_json(force=True)
    if "shared" not in body:
        abort(400, description=_("Nothing to update."))
    new_shared = 1 if body["shared"] else 0
    conn.execute("UPDATE playlists SET shared = ? WHERE id = ?", (new_shared, playlist_id))
    conn.commit()
    if new_shared == 0:
        # #73: retroactive, not just forward-looking — see the
        # helper's own docstring.
        _revoke_non_owner_playlist_selections(conn, playlist_id, row["owner_user_id"])
    return jsonify({"status": "ok"})


def _update_playlist(conn, user_id: int, playlist_id: int):
    """The PATCH body, shared by the browser and the App API: `title`
    renames a playlist made in Trobar, `shared` goes to _set_playlist_shared."""
    body = request.get_json(force=True, silent=True) or {}
    if "title" in body:
        _trobar_playlist_for_edit(conn, user_id, playlist_id)
        playlist_sync.rename_trobar_playlist(conn, playlist_id, _playlist_title(body))
        if "shared" not in body:
            return jsonify({"status": "ok"})
    return _set_playlist_shared(conn, user_id, playlist_id)


_PLAYLIST_TITLE_MAX = 200


def _playlist_title(body: dict) -> str:
    """The `title` of a request body, trimmed: 1 to _PLAYLIST_TITLE_MAX
    characters, or a 400."""
    title = body.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title or len(title) > _PLAYLIST_TITLE_MAX:
        abort(400, description=_("A playlist needs a name of at most %(n)d characters.",
                                 n=_PLAYLIST_TITLE_MAX))
    return title


def _playlist_for_owner(conn, user_id: int, playlist_id: int):
    """The playlist's row for its owner or an admin: 404 when there is no such
    playlist, 403 for someone else's."""
    row = conn.execute("SELECT owner_user_id, source_provider, title, rule FROM playlists WHERE id = ?",
                       (playlist_id,)).fetchone()
    if row is None:
        abort(404, description=_("Playlist not found"))
    if row["owner_user_id"] != user_id and not _is_admin(conn, user_id):
        abort(403, description=_("Unauthorized access to this playlist"))
    return row


def _trobar_playlist_for_edit(conn, user_id: int, playlist_id: int):
    """A playlist made in Trobar that this user may edit: its owner, or an
    admin. 404 when there is no such playlist, 403 for someone else's,
    400 for a playlist from a provider, which isn't edited here."""
    row = _playlist_for_owner(conn, user_id, playlist_id)
    if row["source_provider"] != playlist_sync.TROBAR_SOURCE:
        abort(400, description=_("Only a playlist made in Trobar can be edited here."))
    return row


def _rule_playlist_for_edit(conn, user_id: int, playlist_id: int):
    """A rule playlist this user may edit, with the same answers as
    _trobar_playlist_for_edit; any other playlist has no rule, a 400."""
    row = _playlist_for_owner(conn, user_id, playlist_id)
    if row["source_provider"] != rule_playlists.RULE_SOURCE:
        abort(400, description=_("This playlist has no rule."))
    return row


def _track_ids(body: dict) -> list[int]:
    """`track_ids` of a request body: a list of library track ids that all
    exist, at most playlist_sync.TROBAR_TRACKS_MAX of them, or a 400."""
    ids = body.get("track_ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        abort(400, description=_("track_ids must be a list of track ids."))
    return ids


_PLAYLIST_ALBUMS_MAX = 200


def _album_track_ids(conn, body: dict) -> list[int]:
    """`albums` of a request body, [{"artist", "album"}, ...] as the Library
    lists them, as their library track ids: each album in disc and track
    order, the albums in the order given. An album with no tracks adds
    nothing. A malformed list is a 400."""
    albums = body.get("albums")
    if (not isinstance(albums, list) or len(albums) > _PLAYLIST_ALBUMS_MAX
            or not all(isinstance(a, dict) and isinstance(a.get("artist"), str)
                       and isinstance(a.get("album"), str) for a in albums)):
        abort(400, description=_("albums must be a list of at most %(n)d artist and album pairs.",
                                 n=_PLAYLIST_ALBUMS_MAX))
    ids: list[int] = []
    for a in albums:
        ids.extend(r["id"] for r in conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND album = ? AND (album_artist = ? OR artist = ?) "
            "ORDER BY disc_no IS NULL, disc_no, track_no IS NULL, track_no, relative_path",
            (a["album"], a["artist"], a["artist"])))
    return ids


def _create_playlist(conn, user_id: int):
    """The create body, shared by the browser and the App API."""
    title = _playlist_title(request.get_json(force=True, silent=True) or {})
    playlist_id = playlist_sync.create_trobar_playlist(conn, user_id, title)
    return jsonify({"id": playlist_id, "title": title}), 201


def _playlist_items(body: dict) -> list:
    """`items` of a request body, [{"track_id": n} or {"entry_id": n}, ...],
    as set_trobar_playlist_tracks takes them; or the plain `track_ids`
    form. A 400 when malformed."""
    if "items" not in body:
        return list(_track_ids(body))
    items = body.get("items")
    out: list = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or len(item) != 1:
                break
            key, value = next(iter(item.items()))
            if key not in ("track_id", "entry_id") or not isinstance(value, int) or isinstance(value, bool):
                break
            out.append(value if key == "track_id" else {"entry_id": value})
        else:
            return out
    abort(400, description=_("items must be a list of track_id or entry_id objects."))


def _write_playlist_tracks(conn, user_id: int, playlist_id: int, entries_of):
    """The PUT and POST bodies of the tracks route, shared by the browser
    and the App API. entries_of(conn, playlist_id) lists the entries for
    the response, in the caller's own shape."""
    _trobar_playlist_for_edit(conn, user_id, playlist_id)
    body = request.get_json(force=True, silent=True) or {}
    if request.method == "POST":
        new = _album_track_ids(conn, body) if "albums" in body and "track_ids" not in body else _track_ids(body)
        added = len(new)
        items: list = [{"entry_id": r["id"]} for r in conn.execute(
            "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (playlist_id,))] + new
    else:
        items = _playlist_items(body)
        added = sum(1 for i in items if isinstance(i, int))
    if len(items) > playlist_sync.TROBAR_TRACKS_MAX:
        abort(400, description=_("A playlist can hold at most %(n)d entries.",
                                 n=playlist_sync.TROBAR_TRACKS_MAX))
    unknown = playlist_sync.unknown_track_ids(conn, [i for i in items if isinstance(i, int)])
    if unknown:
        return jsonify({"error": _("Some tracks aren't in the library."), "unknown": unknown}), 400
    foreign = playlist_sync.foreign_entry_ids(
        conn, playlist_id, [i["entry_id"] for i in items if isinstance(i, dict)])
    if foreign:
        return jsonify({"error": _("Some entries aren't in this playlist."), "unknown_entries": foreign}), 400
    count = playlist_sync.set_trobar_playlist_tracks(conn, playlist_id, items)
    return jsonify({"status": "ok", "tracks": count, "added": added,
                    "entries": entries_of(conn, playlist_id)})


def _delete_playlist(conn, user_id: int, playlist_id: int, also_uploads: bool = False):
    """The delete body, shared by the browser and the App API: a playlist made
    in Trobar, by its owner or an admin. With `also_uploads`, the browser's
    case since #287, a playlist uploaded as a file or made from a rule too,
    under the same rule; the App API deletes only what it can make."""
    if also_uploads:
        row = _playlist_for_owner(conn, user_id, playlist_id)
        if row["source_provider"] not in (playlist_sync.TROBAR_SOURCE, playlist_sync.UPLOAD_SOURCE,
                                          rule_playlists.RULE_SOURCE):
            abort(400, description=_("Only a playlist made in Trobar or uploaded as a file can be deleted here."))
    else:
        _trobar_playlist_for_edit(conn, user_id, playlist_id)
    playlist_sync.delete_trobar_playlist(conn, playlist_id)
    return jsonify({"status": "ok"})


def _subscription_row(conn, sub_id: int, user_id: int):
    """One subscription the caller is allowed to act on, or abort.

    Owner or admin, the same rule the `shared` toggle above uses. A
    subscription is not a shareable object in its own right -- what it
    produces is a playlist, and that playlist's own sharing toggle is where
    a household member gets access. So there is no "shared subscription"
    case to consider here, only ownership."""
    row = conn.execute(
        "SELECT id, owner_user_id, provider, external_id, url, title "
        "FROM playlist_subscriptions WHERE id = ?", (sub_id,)
    ).fetchone()
    if row is None:
        abort(404, description=_("Playlist subscription not found"))
    if row["owner_user_id"] != user_id and not _is_admin(conn, user_id):
        abort(403, description=_("Unauthorized access to this playlist subscription"))
    return row


def _subscription_json(row) -> dict:
    return {
        "id": row["id"], "url": row["url"], "provider": row["provider"],
        "title": row["title"], "last_synced_at": row["last_synced_at"],
        "last_error": row["last_error"], "last_error_at": row["last_error_at"],
        "track_count": row["last_track_count"], "matched_count": row["last_matched_count"],
        # The most tracks the source can return, or None when it returns
        # them all. The page says a playlist at the limit may be longer.
        "track_limit": spotify_public_client.LIMIT
        if row["provider"] == spotify_public_client.PROVIDER_ID else None,
    }


def _own_subscriptions(conn, user_id: int):
    """This user's own subscriptions; shared by both prefixes."""
    rows = conn.execute(
        "SELECT * FROM playlist_subscriptions WHERE owner_user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    return jsonify({"subscriptions": [_subscription_json(r) for r in rows]})


def _add_subscription(conn, user_id: int):
    """Subscribe by URL and import at once; shared by both prefixes."""
    body = request.get_json(force=True)
    raw_url = (body.get("url") or "").strip()
    # Spotify first: its parser takes only links, while YouTube Music's
    # also takes a bare id, which would otherwise swallow anything.
    provider = spotify_public_client.PROVIDER_ID
    external_id = spotify_public_client.parse_playlist_url(raw_url)
    if external_id is None:
        provider = ytmusic_client.PROVIDER_ID
        external_id = ytmusic_client.parse_playlist_url(raw_url)
    if external_id is None:
        abort(400, description=_(
            "That doesn't look like a YouTube Music or Spotify playlist link. "
            "Copy the playlist's Share link, or the address bar while it's open."))
    existing = conn.execute(
        "SELECT id FROM playlist_subscriptions "
        "WHERE owner_user_id = ? AND provider = ? AND external_id = ?",
        (user_id, provider, external_id),
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            "INSERT INTO playlist_subscriptions (owner_user_id, provider, external_id, url) "
            "VALUES (?, ?, ?, ?)",
            (user_id, provider, external_id, raw_url),
        )
        conn.commit()
        sub_id = cur.lastrowid
    else:
        sub_id = existing["id"]
        conn.execute(
            "UPDATE playlist_subscriptions SET url = ? WHERE id = ?", (raw_url, sub_id))
        conn.commit()

    sub = conn.execute(
        "SELECT id, owner_user_id, provider, external_id, title "
        "FROM playlist_subscriptions WHERE id = ?", (sub_id,)
    ).fetchone()
    outcome = playlist_sync.sync_one_subscription(conn, sub)
    row = conn.execute(
        "SELECT * FROM playlist_subscriptions WHERE id = ?", (sub_id,)).fetchone()
    return jsonify({"status": outcome["status"], "subscription": _subscription_json(row)})


def _refresh_subscription(conn, user_id: int, sub_id: int):
    """Re-fetch one subscription, owner or admin; shared by both prefixes."""
    _subscription_row(conn, sub_id, user_id)
    sub = conn.execute(
        "SELECT id, owner_user_id, provider, external_id, title "
        "FROM playlist_subscriptions WHERE id = ?", (sub_id,)
    ).fetchone()
    outcome = playlist_sync.sync_one_subscription(conn, sub)
    row = conn.execute(
        "SELECT * FROM playlist_subscriptions WHERE id = ?", (sub_id,)).fetchone()
    return jsonify({"status": outcome["status"], "subscription": _subscription_json(row)})


def _delete_subscription(conn, user_id: int, sub_id: int):
    """Unsubscribe and remove its playlist, owner or admin; shared by both prefixes."""
    _subscription_row(conn, sub_id, user_id)
    playlist_sync.delete_subscription(conn, sub_id)
    return jsonify({"status": "ok"})


def _require_playlist_visible_by_id(conn, user_id: int, playlist_id: int) -> None:
    """#200: same #28 visibility rule as GET /api/provider/playlists'
    per-row filtering (admin, unowned, shared, or the owner) — but for a
    single playlist id rather than filtering a list, since the unresolved-
    tracks review is a per-playlist action, not something reachable only
    via that list response. Aborts 404/403; a private, someone-else-owned
    playlist's review surface is exactly as invisible as the playlist
    itself is in that list."""
    row = conn.execute(
        "SELECT owner_user_id, shared FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None:
        abort(404, description=_("Playlist not found"))
    if row["owner_user_id"] is None or row["shared"] or row["owner_user_id"] == user_id:
        return
    if _is_admin(conn, user_id):
        return
    abort(403, description=_("Unauthorized access to this playlist"))


def _unresolved_tracks_of(conn, user_id: int, playlist_id: int):
    """A visible playlist's unresolved entries; shared by both prefixes."""
    _require_playlist_visible_by_id(conn, user_id, playlist_id)
    return jsonify(sync_state.list_unresolved_playlist_tracks(conn, playlist_id))


def _exclude_unresolved_tracks(conn, user_id: int, playlist_id: int):
    """Exclude or un-exclude unresolved entries of a visible playlist; shared by both prefixes."""
    _require_playlist_visible_by_id(conn, user_id, playlist_id)
    body = request.get_json(force=True)
    ids = body.get("ids")
    if not isinstance(ids, list):
        abort(400, description=_("ids must be a list of unresolved-track row ids."))
    excluded = bool(body.get("excluded", True))
    count = sync_state.set_unresolved_playlist_tracks_excluded(conn, playlist_id, ids, excluded)
    return jsonify({"unresolved_count": count})


def _set_lidarr_requests(conn, user_id: int, playlist_id: int):
    """The per-playlist Lidarr request toggle, for anyone who can see the
    playlist; shared by both prefixes."""
    _require_playlist_visible_by_id(conn, user_id, playlist_id)
    body = request.get_json(force=True)
    if "enabled" not in body:
        abort(400, description=_("Nothing to update."))
    enabled = bool(body["enabled"])
    conn.execute(
        "UPDATE playlists SET lidarr_request_enabled = ? WHERE id = ?",
        (1 if enabled else 0, playlist_id),
    )
    conn.commit()
    # Queued, not run here: a run is one Lidarr round trip per missing
    # album, and the client is waiting on this response.
    queued = lidarr_requests.enqueue(conn, playlist_id) if enabled else False

    row = conn.execute(
        "SELECT lidarr_request_enabled, lidarr_request_last_run_at, "
        "lidarr_request_last_count, lidarr_request_last_error, "
        "lidarr_request_last_error_code FROM playlists WHERE id = ?",
        (playlist_id,),
    ).fetchone()
    return jsonify({
        **dict(row),
        "lidarr_request_enabled": bool(row["lidarr_request_enabled"]),
        "lidarr_request_queued": queued,
    })


# #352: never 'autofit' — that type is only ever created internally
# (autofit.refresh_autofit), resolved by selection id against
# autofit_tracks rather than by a target string, so it isn't meaningful
# for a client to ask for by type/target through either route below.
#
# 'track' is fully handled (_resolve_selection_track_ids, and list_basket
# resolves it to a title). The Android app's playlist screen stages one.
VALID_SELECTION_TYPES = {"artist", "album", "playlist", "track"}


def _selections_for(conn, user_id: int) -> list[dict]:
    # Own selections, plus any selection currently linked to a
    # device this user can see (owns or has pinned) — lets a delegate
    # manage sync content on a device they've been granted, even if
    # someone else originally created the selection targeting it.
    if _is_admin(conn, user_id):
        rows = conn.execute(
            "SELECT s.id, s.type, s.target, s.created_at, s.created_by_user_id, "
            "u.username AS created_by_username, GROUP_CONCAT(sd.device_id) AS device_ids "
            "FROM selections s JOIN users u ON u.id = s.created_by_user_id "
            "LEFT JOIN selection_devices sd ON sd.selection_id = s.id "
            "GROUP BY s.id ORDER BY s.created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT s.id, s.type, s.target, s.created_at, s.created_by_user_id, "
            "u.username AS created_by_username, GROUP_CONCAT(sd.device_id) AS device_ids "
            "FROM selections s JOIN users u ON u.id = s.created_by_user_id "
            "LEFT JOIN selection_devices sd ON sd.selection_id = s.id "
            "WHERE (s.created_by_user_id = :uid OR EXISTS ("
            "  SELECT 1 FROM selection_devices sd2 JOIN devices d ON d.id = sd2.device_id "
            "  WHERE sd2.selection_id = s.id AND ("
            "    d.owner_user_id = :uid OR d.id IN (SELECT device_id FROM device_pins WHERE user_id = :uid)"
            "  )"
            ")) "
            # #73: defense-in-depth alongside the PATCH-time revocation
            # above — a playlist-type selection never surfaces its
            # (still-meaningful, since it pairs with a real playlist
            # id) id/type here once the target is owned-and-unshared
            # by someone else. #434 review: this CAST(? AS INTEGER)
            # is NOT the same parser as _require_playlist_visible()'s
            # (sync_state.parse_target_id(), strict ASCII-digits-
            # only) any more, so this no longer literally "mirrors"
            # that check for a malformed target -- it still fails
            # CLOSED in that case (CAST takes a leading-digit prefix,
            # so a malformed target can only ever hide a selection
            # here, never wrongly reveal one), which is a UX oddity
            # at worst, not a leak. Left as CAST deliberately: this
            # is a read-side filter over already-existing rows, and
            # since #434 made the write side reject any malformed
            # target outright, no NEW selection can ever be created
            # with one for this query to have to handle going
            # forward.
            "AND NOT (s.type = 'playlist' AND EXISTS ("
            "  SELECT 1 FROM playlists p WHERE p.id = CAST(s.target AS INTEGER) "
            "  AND p.owner_user_id IS NOT NULL AND p.shared = 0 AND p.owner_user_id != :uid"
            ")) "
            "GROUP BY s.id ORDER BY s.created_at DESC",
            {"uid": user_id},
        ).fetchall()
    return _with_appears_on(conn, [dict(r) for r in rows])


def _with_appears_on(conn, selections: list[dict]) -> list[dict]:
    """An album selection whose artist isn't the album's album artist is
    that artist's tracks on someone else's album (#240, "Also appears on",
    and every per-performer selection from before the Library grouped by
    album artist). It carries appears_on = {album_artist, tracks, of} so the
    Selections table can say so; None for a whole album, and for the rest.
    A whole album carries album_tracks instead, its track count, so the row
    can say "whole album, N tracks"; None for everything else.

    One query for all of them: tracks has no index on album, so a query per
    selection is a scan of the library per selection."""
    targets = [s["target"].partition("||") for s in selections if s["type"] == "album"]
    whole: dict[tuple[str, str], int] = {}
    performed: dict[tuple[str, str], tuple[str, int]] = {}
    if targets:
        # By the raw album_artist, in its order, so a performer on two album
        # artists' albums of one name resolves to the first, always the same.
        for r in conn.execute(
                f"SELECT album, artist, {_ALBUM_ARTIST} AS by_artist, COUNT(*) AS n FROM tracks "
                "WHERE deleted_at IS NULL AND album IN (SELECT value FROM json_each(?)) "
                "GROUP BY album, artist, tracks.album_artist ORDER BY tracks.album_artist",
                (json.dumps(sorted({album for _, _, album in targets})),)):
            whole[(r["album"], r["by_artist"])] = whole.get((r["album"], r["by_artist"]), 0) + r["n"]
            performed.setdefault((r["album"], r["artist"]), (r["by_artist"], r["n"]))
    for sel in selections:
        sel["appears_on"] = None
        sel["album_tracks"] = None
        if sel["type"] != "album":
            continue
        artist, _, album = sel["target"].partition("||")
        if (album, artist) in whole:
            sel["album_tracks"] = whole[(album, artist)]
        elif (album, artist) in performed:
            by_artist, tracks = performed[(album, artist)]
            sel["appears_on"] = {"album_artist": by_artist, "tracks": tracks, "of": whole[(album, by_artist)]}
    return selections


# #303/#501: the cross-surface staging basket. Items accumulate here from
# any surface, staged against the device(s) chosen in the picker at add
# time, until a POST /api/basket/fan-out sends a device's own section —
# the exact same sync_state.create_selection() call the pre-existing
# single-item device picker always made, just per-device now instead of
# one flat pass over the whole basket.
def _basket_add(conn, user_id: int, body: dict) -> int:
    if body.get("type") not in VALID_SELECTION_TYPES:
        abort(400, description=_("Unsupported selection type."))
    device_ids = [int(d) for d in body.get("device_ids", [])]
    # #501: staging now IS choosing a destination — same "no
    # meaningful no-op" refusal api_basket_fan_out already made.
    if not device_ids:
        abort(400, description=_("Choose at least one device."))
    for device_id in device_ids:
        _require_device_access(conn, user_id, device_id)
    _require_playlist_visible(conn, user_id, body["type"], body["target"])
    return sync_state.add_basket_item(conn, user_id, body["type"], body["target"], device_ids)


def _basket_fan_out(conn, user_id: int, device_ids: list[int]) -> dict:
    # #351: there's no meaningful "fan out to nowhere" — the UI already
    # disables Confirm until at least one device is checked, but the API
    # itself didn't refuse it, so a direct/buggy call could clear the
    # whole basket while creating nothing.
    if not device_ids:
        abort(400, description=_("Choose at least one device."))
    # #349: fanning out to a delegated device is intentional, not an
    # accident of what this guard happens to permit — _require_device_
    # access() already allows "owner, admin, or anyone the owner has
    # granted delegation over" (the pinned-device picker only ever
    # shows devices this check would pass), and that's the whole
    # feature. No separate "is this a delegated fan-out" branch needed.
    for device_id in device_ids:
        _require_device_access(conn, user_id, device_id)
    requested_devices = set(device_ids)

    all_items = sync_state.list_basket(conn, user_id)
    # Only items that touch at least one of THIS call's devices are
    # part of it at all — an item staged solely for some other device
    # the caller didn't check here is irrelevant to this fan-out.
    relevant_items = [
        item for item in all_items if requested_devices & set(item["device_ids"])
    ]
    # #352: a basket item can only predate type validation now (a
    # hand-edited DB, or a row added before this check existed) — skip
    # it rather than fail the whole fan-out over one bad row, and report
    # how many were skipped so it isn't silently dropped.
    #
    # #471: a malformed playlist target (e.g. 'target=1_0', pre-dating
    # #434's write-side rejection -- #424 is exactly why such rows
    # exist) is the SAME category of legacy-bad-row, and gets the same
    # treatment here, not a hard 400 for the whole fan-out.
    # _require_playlist_visible() below still aborts on one -- rightly,
    # since #434's fix depends on it never reaching that far for a
    # freshly-created row -- so it has to be filtered out before that
    # loop runs, not left for that loop to reject.
    valid_items = [
        item for item in relevant_items
        if item["type"] in VALID_SELECTION_TYPES
        and (item["type"] != "playlist" or sync_state.parse_target_id(item["target"]) is not None)
    ]
    skipped = len(relevant_items) - len(valid_items)
    # #349: evaluated against the ACTOR (user_id), never the target
    # device's owner — deliberate. The person whose privacy is at stake
    # is the one choosing to send it; requiring the destination owner to
    # also see the playlist would break delegated fan-out for exactly
    # the playlists most likely to be curated for someone else, the
    # actor's own private ones. The destination owner can still see the
    # resulting selection once it lands (they own the device), so they
    # may learn the playlist's name — an inherent, accepted consequence
    # of the actor choosing to put it there, not a gap to close.
    for item in valid_items:
        _require_playlist_visible(conn, user_id, item["type"], item["target"])
    # #351: one transaction for the whole fan-out (every device's
    # section in this call together) — a mid-loop failure must not
    # leave some items converted to real selections while the basket
    # still holds all of them (the natural retry would then recreate
    # the ones that already succeeded).
    try:
        for item in valid_items:
            item_devices = sorted(requested_devices & set(item["device_ids"]))
            sync_state.create_selection(
                conn, item["type"], item["target"], user_id, item_devices, commit=False)
            for device_id in item_devices:
                sync_state.unstage_basket_item_device(
                    conn, user_id, item["id"], device_id, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ({"status": "ok", "count": len(valid_items), "skipped": skipped})


def _basket_set_last_destination(conn, user_id: int, surface: str, device_ids: list[int]) -> dict:
    row = conn.execute(
        "SELECT basket_last_destinations FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    current = _basket_last_destinations_dict(row["basket_last_destinations"])
    current[surface] = device_ids
    conn.execute(
        "UPDATE users SET basket_last_destinations = ? WHERE id = ?",
        (json.dumps(current), user_id),
    )
    conn.commit()
    return ({"status": "ok"})


def _widget_catalog(catalog=DASHBOARD_WIDGET_CATALOG) -> list[dict]:
    return [{"id": wid, "admin_only": admin_only} for wid, admin_only in catalog]


def _playlists_to_sync(conn, user_id: int, device_id: int, limit: int) -> list[dict]:
    """The playlists this user may see with the most library tracks not yet
    on the device, most first, none with nothing left. An excluded track
    stays off the device, so it isn't waiting."""
    visible = {r["id"]: r for r in _visible_playlists(conn, user_id)}
    if not visible:
        return []
    marks = ",".join("?" * len(visible))
    rows = conn.execute(
        "SELECT pt.playlist_id, COUNT(DISTINCT t.id) AS to_sync FROM playlist_tracks pt "
        "JOIN tracks t ON t.id = pt.matched_track_id AND t.deleted_at IS NULL "
        "LEFT JOIN device_track_state s ON s.track_id = t.id AND s.device_id = ? "
        "AND s.status IN ('downloaded', 'excluded') "
        f"WHERE s.track_id IS NULL AND pt.playlist_id IN ({marks}) GROUP BY pt.playlist_id",
        (device_id, *visible)).fetchall()
    ranked = sorted(rows, key=lambda r: (-r["to_sync"], visible[r["playlist_id"]]["title"].casefold()))
    return [{"id": r["playlist_id"], "title": visible[r["playlist_id"]]["title"],
             "track_count": visible[r["playlist_id"]]["track_count"],
             "matched_count": visible[r["playlist_id"]]["matched_count"] or 0,
             "to_sync": r["to_sync"]} for r in ranked[:limit]]


#: #283: period only ever reached the Last.fm calls in both routes below —
#: the ListenBrainz calls always used their own half_yearly default
#: regardless of ?period, silently putting the two sources on different
#: time windows for any non-default period. ListenBrainz has no exact
#: equivalent vocabulary, so this maps the closest range for each Last.fm
#: period (all_time is the closest analog to "overall"; ListenBrainz has no
#: "quarter"-vs-"3month" distinction worth drawing).
_LASTFM_PERIOD_TO_LISTENBRAINZ_RANGE = {
    "overall": "all_time", "7day": "week", "1month": "month",
    "3month": "quarter", "6month": "half_yearly", "12month": "year",
}


#: Last.fm's own `period` enum for user.getTopAlbums, validated against
#: rather than trusting it degrades to [] on garbage (an assumption the old
#: code inherited from top_albums rather than stating). Derived
#: from the mapping above rather than listed separately — keeping one
#: source of truth means a period added to one can't be forgotten in the
#: other and silently KeyError the ListenBrainz lookup below.
_LASTFM_PERIODS = tuple(_LASTFM_PERIOD_TO_LISTENBRAINZ_RANGE)


def _validated_period(raw: str) -> str:
    return raw if raw in _LASTFM_PERIODS else "6month"


def _scrobble_sources(conn, user_id: int) -> tuple:
    """The user's listening-history accounts and the keys/bases their
    clients need: (user row, Last.fm API key, Last.fm base, ListenBrainz
    base). Per-user key first, then the admin-configured app-wide default
    (lastfm.top_albums also has its own LASTFM_API_KEY env var as a last
    resort, for deployments that haven't set either yet)."""
    user = conn.execute(
        "SELECT lastfm_username, lastfm_api_key, listenbrainz_username, maloja_url FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    api_key = user["lastfm_api_key"] or db.get_config(conn, "lastfm_api_key_default") or ""
    lastfm_base = db.get_config(conn, "lastfm_api_base") or ""
    listenbrainz_base = db.get_config(conn, "listenbrainz_api_base") or ""
    return user, api_key, lastfm_base, listenbrainz_base


_SCROBBLE_SOURCES = ("lastfm", "listenbrainz", "maloja", "device_plays")


def _add_from(combined: list, sources: dict, name: str, result) -> None:
    """Merge one history call's result. None means the service didn't
    answer: the source is marked "error" (and stays so if another of its
    calls did answer), instead of passing for "nothing played"."""
    if result is None:
        sources[name] = "error"
        return
    combined += result
    if sources[name] != "error":
        sources[name] = "ok"


def _suggestions_for(conn, user_id: int, period: str) -> tuple[list[dict], dict]:
    """The merged suggestion feed, and each history source's state:
    "ok", "error" (it didn't answer), or "unconfigured"."""
    user, api_key, lastfm_base, listenbrainz_base = _scrobble_sources(conn, user_id)
    sources = dict.fromkeys(_SCROBBLE_SOURCES, "unconfigured")
    user_device_ids = {row["id"] for row in _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))}

    # Fetch enough candidates per source that filtering-to-one-source in
    # the UI can still show a full 30 of it, not just whatever survived
    # from a mixed-default-sized pool (follow-up).
    combined = suggestions.recently_added(conn, user_device_ids, limit=30)
    if user["lastfm_username"]:
        _add_from(combined, sources, "lastfm", lastfm.suggestions(
            conn, user["lastfm_username"], api_key, period, limit=200, user_device_ids=user_device_ids,
            api_base=lastfm_base,
        ))
        _add_from(combined, sources, "lastfm", lastfm.recently_played_suggestions(
            conn, user["lastfm_username"], api_key, limit=200, user_device_ids=user_device_ids,
            api_base=lastfm_base,
        ))
    # Both services can be configured at once — the feed just gains more
    # sources and the dedup below keeps one copy per album.
    if user["listenbrainz_username"]:
        _add_from(combined, sources, "listenbrainz", listenbrainz.suggestions(
            conn, user["listenbrainz_username"],
            _LASTFM_PERIOD_TO_LISTENBRAINZ_RANGE[period], limit=200, user_device_ids=user_device_ids,
            api_base=listenbrainz_base,
        ))
        _add_from(combined, sources, "listenbrainz", listenbrainz.recently_played_suggestions(
            conn, user["listenbrainz_username"], limit=100, user_device_ids=user_device_ids,
            api_base=listenbrainz_base,
        ))
    # Maloja uses Last.fm's period names itself (maloja._from) and names its
    # suggestions as the library does, so the dedup below catches them.
    if user["maloja_url"]:
        _add_from(combined, sources, "maloja", maloja.suggestions(
            conn, user["maloja_url"], period, limit=200, user_device_ids=user_device_ids))
        _add_from(combined, sources, "maloja", maloja.recently_played_suggestions(
            conn, user["maloja_url"], limit=100, user_device_ids=user_device_ids))
    if device_plays.is_source(conn, user_id):
        _add_from(combined, sources, "device_plays", device_plays.suggestions(
            conn, user_id, period, limit=200, user_device_ids=user_device_ids))
        _add_from(combined, sources, "device_plays", device_plays.recently_played_suggestions(
            conn, user_id, limit=100, user_device_ids=user_device_ids))

    seen = set()
    deduped = []
    for s in combined:
        key = (s["artist"].lower(), s["album"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped, sources


def _most_played_limit(raw) -> int:
    try:
        return max(1, min(int(raw if raw is not None else 8), 50))
    except (TypeError, ValueError):
        return 8


def _most_played_for(conn, user_id: int, period: str, limit: int) -> tuple[list[dict], dict]:
    """The merged most-played chart, and each history source's state, as
    _suggestions_for() reports them."""
    user, api_key, lastfm_base, listenbrainz_base = _scrobble_sources(conn, user_id)
    sources = dict.fromkeys(_SCROBBLE_SOURCES, "unconfigured")
    combined: list = []
    if user["lastfm_username"]:
        _add_from(combined, sources, "lastfm", lastfm.most_played(
            user["lastfm_username"], api_key, period, limit=50, api_base=lastfm_base))
    if user["listenbrainz_username"]:
        _add_from(combined, sources, "listenbrainz", listenbrainz.most_played(
            user["listenbrainz_username"], _LASTFM_PERIOD_TO_LISTENBRAINZ_RANGE[period],
            limit=50, api_base=listenbrainz_base,
        ))
    if user["maloja_url"]:
        _add_from(combined, sources, "maloja", maloja.most_played(conn, user["maloja_url"], period, limit=50))
    if device_plays.is_source(conn, user_id):
        _add_from(combined, sources, "device_plays", device_plays.most_played(conn, user_id, period, limit=50))

    # Both services can be configured at once — same "more sources, dedup
    # keeps one copy" reasoning as /api/suggestions, re-sorted by
    # playcount since two providers' raw lists can't be merged in order.
    seen = set()
    deduped = []
    for s in sorted(combined, key=lambda s: -s["playcount"]):
        key = (s["artist"].lower(), s["album"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped[:limit], sources


def _meta_file(name: str) -> Path | None:
    here = Path(__file__).resolve().parent
    for candidate in (here / name, here.parent / name):
        if candidate.is_file():
            return candidate
    return None
