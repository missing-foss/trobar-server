#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trobar — Flask web app + device-facing sync API."""

import concurrent.futures
import difflib
import hashlib
import json
import logging
import os
import posixpath
import secrets
import shutil
import signal
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, NoReturn
from urllib.parse import quote, urlsplit

import click
from flask import Flask, jsonify, request, render_template, send_file, abort, Response, session, redirect, url_for
from flask_babel import Babel, gettext as _, get_locale
from flask_compress import Compress
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth

import album_lookup
import artist_images
import covers
import db
import emby_client
import filesystem_client
import fingerprint
import history_cache
import jellyfin_client
import jobs
import lastfm
import library_quiz
import lidarr_client
import lidarr_requests
import listenbrainz
import maloja
import lms_client
import mbid_backfill
import music_assistant_client
import mirror
import mirror_emby
import mirror_jellyfin
import mirror_music_assistant
import mirror_plex
import mirror_runs
import mirror_subsonic
import play_forward
import playlist_sync
import provider_config
import plex_client
import provenance
import roon_client
import scanner
import settings
import subsonic_client
import suggestions
import sync_state
import spotify_client
import spotify_public_client
import tidal_client
import transcode
import url_guard
import ytmusic_client

import device_api
import devices
import library
import ops
import user_profile
import admin
import app_api
import playlists
import selections
import integrations
import auth
import tidal_share_routes
from web_common import (
    APP_API_VERSION,
    DASHBOARD_WIDGET_CATALOG,
    VALID_SELECTION_TYPES,
    _ALBUM_ARTIST,
    _DEVICE_PLAYLIST_ENTRIES_MAX,
    _EXPOSURE_LOCK,
    _EXPOSURE_WINDOW_S,
    _PROVIDERS,
    _PROVIDER_SWITCH_LOCK,
    _RL_LOCK,
    _RankingUnavailable,
    _active_provider,
    _active_provider_id,
    _add_subscription,
    _admin_counts,
    _app_authenticates,
    _apply_dashboard_widgets_patch,
    _artist_image_config,
    _artist_image_response,
    _authenticated_device,
    _basket_add,
    _basket_fan_out,
    _basket_last_destinations_dict,
    _basket_set_last_destination,
    _client_ip,
    _config_int,
    _contained,
    _cover_response,
    _create_playlist,
    _delete_playlist,
    _delete_subscription,
    _device_dashboard_widgets,
    _device_list,
    _device_rows_for_user,
    _device_usage,
    _end_other_sessions,
    _exclude_unresolved_tracks,
    _exposure_peers,
    _has_delegation,
    _invalid_url_message,
    _is_admin,
    _is_valid_url,
    _library_cover_row,
    _library_similar_artists,
    _library_stats,
    _meta_file,
    _mirror_coverage,
    _most_played_for,
    _most_played_limit,
    _music_root_is_readonly,
    _own_subscriptions,
    _provision_user,
    _ranked_album_keys_for_user,
    _rate_limited,
    _recently_added_for,
    _recently_released_for,
    _record_failure,
    _refresh_autofit_after_limit_change,
    _refresh_subscription,
    _require_device_access,
    _require_playlist_visible,
    _require_playlist_visible_by_id,
    _rl_failures,
    _selections_for,
    _session_user_id,
    _set_lidarr_requests,
    _spotify_experimental_enabled,
    _start_playlist_sync,
    _suggestions_for,
    _trusted_proxy,
    _unresolved_tracks_of,
    _update_playlist,
    _validated_device_name,
    _validated_device_type,
    _validated_max_size_bytes,
    _validated_period,
    _validated_source_of_truth,
    _validated_transcode_format,
    _visible_playlists,
    _widget_catalog,
    _write_playlist_tracks,
    get_current_user_id,
    oauth,
    require_admin,
)

# #297: wire job types to their handlers at import, in ONE place. Registration
# lives here rather than in the owning modules so jobs.py never imports them
# (circular) and they never import jobs.py just to register — the queue stays
# ignorant of every feature, and the full set of job types is readable in a
# single spot. Registering at import (harmless — it only populates a dict) while
# the worker itself starts only in __main__ is deliberate: the test suite
# imports this module and must NOT acquire a background worker, but a handler
# that isn't registered would make a queued job look like a wiring bug.
# #301: LOG_LEVEL, applied to TROBAR'S OWN LOGGERS ONLY — never the root logger.
#
# Every module here does `logging.getLogger(__name__)`, and since they're
# top-level modules (not a package) there's no shared parent to configure, hence
# the explicit list. test_logging.py asserts this list matches the modules that
# actually define a logger, so it can't silently rot when one is added.
#
# An ALLOWLIST rather than "root at LOG_LEVEL, third-party pinned back", because
# DEBUG on the root logger leaks credentials. Measured: urllib3 logs full request
# URLs at DEBUG, and subsonic_client puts auth material in the query string
# (`u`, `t`, `s` — a salted-md5 token with its salt, offline-brute-forceable):
#   DEBUG:urllib3.connectionpool:... "GET /rest/ping?u=alice&p=SECRET&v=1.16.1"
# A denylist would need to enumerate every library that might do this (waitress
# logs request lines, authlib handles token exchanges, roonapi is unaudited);
# an allowlist makes anything we haven't thought of safe by default. Verified
# that our loggers still emit at DEBUG while urllib3/waitress stay silent.
#
# Header-borne secrets (X-Plex-Token, X-Emby-Token, Jellyfin api_key, OAuth
# bearers) are NOT logged by urllib3 — that needs
# http.client.HTTPConnection.debuglevel, which nothing here sets. Don't set it.
_APP_LOGGERS = (
    "main",  # also Flask's app.logger, since the app is named after this module
    "album_artist", "album_lookup", "artist_image_keys", "artist_images", "audiodb_client",
    "autofit", "basket_store", "covers", "credential_store", "db", "device_store", "admin", "app_api", "auth",
    "device_api", "device_plays", "devices",
    "integrations", "library", "ops", "playlists", "selections", "user_profile", "web_common",
    "emby_client", "filesystem_client", "fingerprint", "history_cache", "identity",
    "itunes_library", "jellyfin_client", "jobs", "lastfm", "library_quiz", "lidarr_client",
    "lidarr_requests", "listenbrainz", "lms_client", "maloja", "matching", "mbid_backfill",
    "play_forward",
    "mediabrowser_client",
    "mirror", "mirror_emby", "mirror_jellyfin", "mirror_mediabrowser", "mirror_music_assistant",
    "mirror_plex", "mirror_runs", "mirror_sinks", "mirror_subsonic", "music_assistant_client",
    "playlist_sync",
    "plex_client",
    "provenance", "provider_config", "roon_client", "rule_playlists", "scanner", "selection_store", "settings",
    "spotify_client", "spotify_public_client", "subsonic_client", "suggestions", "sync_state",
    "tidal_client", "tidal_share_routes", "tidal_shares", "token_store", "track_merge", "transcode", "unresolved_store", "url_guard",
    "ytmusic_client",
)


def _configure_logging() -> None:
    """Root stays at WARNING and gets the handler; our own loggers get
    LOG_LEVEL. A child logger's records are filtered by ITS level and then
    handled by root's handler regardless of root's level, so this yields our
    DEBUG without anyone else's.

    An unrecognised LOG_LEVEL warns and falls back to WARNING rather than
    raising — a typo in an env var must not stop the server booting, same
    posture as the other startup checks."""
    requested = os.environ.get("LOG_LEVEL", "WARNING").strip().upper()
    resolved = logging.getLevelNamesMapping().get(requested)
    level = logging.WARNING if resolved is None else resolved
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        force=True,
    )
    for name in _APP_LOGGERS:
        logging.getLogger(name).setLevel(level)
    if resolved is None:
        logging.getLogger("main").warning(
            "LOG_LEVEL=%r isn't a known level (DEBUG/INFO/WARNING/ERROR/CRITICAL) "
            "— using WARNING.", requested)


_configure_logging()

# #297 step 3 / #356: LANES. The rule is NOT "does this job decode audio" —
# two of the four types below decode audio in LANE_SHORT, and (since #334)
# fingerprint.JOB_TYPE decodes none at all despite being LANE_LONG. The real
# split is whether a single execution RUNS TO COMPLETION internally (loops
# until its whole backlog is drained, no fixed upper bound — a full library
# scan; provenance.ensure_library_fingerprints, which explicitly loops until
# nothing pending remains; fingerprint.JOB_TYPE's AcoustID/MusicBrainz
# lookup, which does the same over the network) versus processes ONE capped
# batch (each job's own _BATCH_LIMIT, currently 100 everywhere) and returns,
# relying on being re-triggered for the rest. A capped batch has a bounded
# worst case even when every item in it is an audio decode — see
# jobs._LANE_BY_TYPE.
def _run_library_scan(payload, report):
    """scanner.run_job, then _sync_after_scan. Wired here because the
    follow-up needs the playlist side, which scanner.py doesn't know about.
    The scan has succeeded once run_job returns: a failure in the follow-up
    is logged, never raised, so the scan's counts stay its outcome."""
    result = scanner.run_job(payload, report)
    try:
        _sync_after_scan(result)
    except Exception:
        logging.getLogger("main").exception("scan finished but asking for a playlist sync failed")
    return result


jobs.register(scanner.JOB_TYPE, _run_library_scan, lane=jobs.LANE_LONG,
              on_retry=scanner.retry_payload)
jobs.register(fingerprint.JOB_TYPE, fingerprint.run_job, lane=jobs.LANE_LONG)
# Network-bound like the fingerprint lookup, and on the same lane for the
# same service: one job at a time per lane keeps the two from querying
# MusicBrainz at once. It drains every pending gap in one run.
jobs.register(album_lookup.JOB_TYPE, album_lookup.run_job, lane=jobs.LANE_LONG)
# One playlist's Lidarr requests: a round trip per missing album, so no fixed
# upper bound on how long it takes.
jobs.register(lidarr_requests.JOB_TYPE, lidarr_requests.run_job, lane=jobs.LANE_LONG)
# A one-time re-read of every library file's tags for its MusicBrainz
# recording id; runs to completion over the whole library.
jobs.register(mbid_backfill.JOB_TYPE, mbid_backfill.run_job, lane=jobs.LANE_LONG)
jobs.register(play_forward.JOB_TYPE, play_forward.run_job, lane=jobs.LANE_LONG)
# #356: both of these decode audio (rematch's per-candidate re-verification,
# device_fingerprints' per-track compute) — genuinely SHORT anyway, because
# both are one _BATCH_LIMIT=100 batch per execution, not a drain-the-backlog
# loop. Moving either LONG would queue it behind hours-long scans/backfills,
# which is worse for what these exist to do: make a device's OWN sync useful
# again soon, not eventually.
jobs.register(provenance.JOB_TYPE_REMATCH, provenance.rematch_device)
jobs.register(provenance.JOB_TYPE_DEVICE_FINGERPRINTS, provenance.run_device_fingerprints_job)
def _library_fingerprints_then_lookup(payload, report):
    """#334: the keyless fingerprint pass, then queue the AcoustID lookup.

    The lookup CONSUMES what this produces (it needs tracks.fingerprint and no
    longer decodes audio itself), so the producer tells it when there is something
    to consume. That is what makes the two order-independent: run the lookup first
    and it finds nothing and exits; this pass then computes fingerprints and queues
    it again. Before #334 the ordering was a sentence in a release note, and it was
    ignored within hours of being written.

    Wired here rather than inside provenance.py so that module needs no import of
    fingerprint or jobs — main.py is where cross-module wiring lives (see
    jobs.register's docstring).

    Only when something was actually computed: an empty pass means the library is
    already fingerprinted, and the lookup was queued alongside this job by
    scanner._queue_post_scan_jobs anyway."""
    result = provenance.ensure_library_fingerprints(payload, report)
    if result.get("computed"):
        with db.connection() as conn:
            jobs.enqueue(conn, fingerprint.JOB_TYPE, dedupe_key=fingerprint.JOB_TYPE)
    return result


jobs.register(provenance.JOB_TYPE_LIBRARY_FINGERPRINTS,
              _library_fingerprints_then_lookup, lane=jobs.LANE_LONG)

# #362: scheduled rescanning, off by default (scan_interval_hours=0). Wired
# here rather than at scanner.py's import time for the same reason every
# other registration on this page is — main.py is where cross-module wiring
# lives, so a module can be imported (by tests, by the setup CLI) without
# silently acquiring background behaviour.
jobs.on_idle(scanner.maybe_schedule_rescan)
jobs.on_idle(mbid_backfill.maybe_queue)


def _run_playlist_sync(payload, report):
    """#297 step 3: playlist_sync.py's own job payload only ever carries
    `provider_id` (a JSON-serializable string) — the live provider MODULE has
    to be resolved back from it, and only main.py's `_PROVIDERS` dict can do
    that (defined further down this file; fine to reference here since this
    function isn't called until well after the whole module has imported).
    Wired here rather than inside playlist_sync.py for the same reason
    _library_fingerprints_then_lookup lives here and not in provenance.py —
    see jobs.register's docstring. `report` gets the count of playlists
    synced so far, with no total: a sync has no single natural denominator
    the way a file walk does (multiple merge passes — primary provider,
    filesystem, Roon profiles, Tidal, Spotify — each a different size). Its
    call before each playlist is also where an admin's cancel stops the sync.

    The provider is the one active when the job RUNS, not the
    `provider_id` the payload recorded when it was queued: a sync queued
    just before a library-source switch and run after it would otherwise
    list the old provider and write back the playlists the switch removed,
    which nothing would ever clean up (each sync cleans only its own
    sources). The payload keeps the id for the jobs view."""
    with db.connection() as conn:
        provider_id = _active_provider_id(conn)
    return playlist_sync.sync_playlists(_PROVIDERS[provider_id], provider_id, report)


jobs.register(playlist_sync.JOB_TYPE, _run_playlist_sync)


# Set when a scan changed the library and a playlist sync should follow
# (_sync_after_scan); cleared once a sync that starts after the scan is
# queued. In app_config, so it survives a restart.
_SYNC_AFTER_SCAN_PENDING = "playlist_sync_after_scan_pending"


def _sync_after_scan(result: dict) -> None:
    """After a scan that added, updated or removed tracks, ask for one
    playlist sync, so playlists, device playlists and mirrors pick up the
    change. A scan that changed nothing asks for nothing. Off with the
    playlist_sync_after_scan setting (on unless set to "0"), and nothing
    before setup has finished.

    Asked for through a pending flag that _maybe_schedule_playlist_sync
    acts on, not by queueing directly: a sync that is already RUNNING may
    have read the tracks before this scan wrote them, and the dedupe key
    would turn a direct start_sync into a no-op. The hook waits for the
    running one to end, then queues another. One already QUEUED will read
    the new tracks, and counts as the sync asked for."""
    if not any(result.get(k) for k in ("added", "updated", "removed")):
        return
    with db.connection() as conn:
        if not db.get_config(conn, "setup_completed") \
                or db.get_config(conn, "playlist_sync_after_scan") == "0":
            return
        db.set_config(conn, _SYNC_AFTER_SCAN_PENDING, "1")
        conn.commit()
    _maybe_schedule_playlist_sync()


def _maybe_schedule_playlist_sync() -> None:
    """Queue a playlist sync when one is asked for: the schedule says one is
    due (playlist_sync.sync_due), or a scan left the pending flag set
    (_sync_after_scan). Registered with jobs.on_idle, like the scheduled
    rescan. Nothing before setup has finished: the provider isn't chosen
    yet. Skipped, not waited for, while a provider switch holds the lock;
    the next idle cycle tries again.

    While a sync is running, nothing is queued and the flag stays set; a
    queued sync clears it. The schedule needs no such care: the running
    sync's end restarts its interval anyway."""
    if not _PROVIDER_SWITCH_LOCK.acquire(blocking=False):
        return
    try:
        with db.connection() as conn:
            if not db.get_config(conn, "setup_completed"):
                return
            pending = db.get_config(conn, _SYNC_AFTER_SCAN_PENDING) == "1"
            state = playlist_sync.sync_job_state(conn)
            if state == "queued" and pending:
                db.set_config(conn, _SYNC_AFTER_SCAN_PENDING, None)
                conn.commit()
                return
            if state is not None or not (pending or playlist_sync.sync_due(conn)):
                return
            provider_id = _active_provider_id(conn)
        result = playlist_sync.start_sync(_PROVIDERS[provider_id], provider_id)
        if result.get("status") == "started" and pending:
            with db.connection() as conn:
                db.set_config(conn, _SYNC_AFTER_SCAN_PENDING, None)
                conn.commit()
    finally:
        _PROVIDER_SWITCH_LOCK.release()


jobs.on_idle(_maybe_schedule_playlist_sync)

app = Flask(__name__)
# Behind a TLS-terminating reverse proxy (Traefik), the request reaches Flask
# over plain HTTP on the container port, so url_for(_external=True) would build
# http:// URLs. That breaks OIDC: the redirect_uri must be the real https URL
# to match what's registered at the IdP. ProxyFix makes Flask honour
# the proxy's X-Forwarded-Proto/Host so external URLs are correct. Safe here
# because the app port is never published directly — only the trusted proxy
# (on the `proxy` docker network) can reach it, so these headers can't be
# spoofed by an outside client.
#
# x_for=1: trust exactly one hop of X-Forwarded-For, matching the deployment
# model (docs/operations/networking.md — one reverse proxy, app never exposed
# directly). ProxyFix then rewrites environ["REMOTE_ADDR"] from the
# RIGHT-most XFF entry (the one the trusted proxy itself appended), which is
# what makes it safe to trust: the proxy appends rather than replaces, so
# anything to its left is whatever the client claimed and must be ignored.
# Read via request.remote_addr (see _client_ip below), never by re-parsing
# X-Forwarded-For by hand — a hand-rolled `.split(",")[0]` picks the
# LEFT-most (attacker-supplied) entry instead and was exactly how this app
# shipped for a while (#382): the brute-force backoff below
# keyed on a value the client could rotate every request, for a bucket that
# never accumulated a single failure.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)  # type: ignore[method-assign]
# Doesn't help the audio files themselves (FLAC is already losslessly
# compressed — gzipping it again saves ~nothing) but /api/device/changes
# can return a large JSON body listing thousands of pending tracks on a
# big sync, and JSON text compresses very well.
app.config["COMPRESS_MIMETYPES"] = ["application/json"]
app.config["COMPRESS_MIN_SIZE"] = 500
Compress(app)

# Session-cookie hardening. The app is served over HTTPS behind
# Traefik, so mark the session cookie Secure (override with
# SESSION_COOKIE_SECURE=0 for plain-http local dev, else login breaks there)
# and SameSite=Lax. HttpOnly is Flask's default but we assert it. SameSite=Lax
# is also the app's primary CSRF posture — browsers won't attach the cookie to
# cross-site POSTs — backed up by _reject_cross_site_mutations() below.
app.config["SESSION_COOKIE_HTTPONLY"] = True
# Sliding: a session is permanent (see _start_session) and Flask re-signs its
# cookie on every request, so it lasts SESSION_LIFETIME_DAYS since the last
# visit. Flask checks the age server-side when it loads the cookie.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    days=max(1, int(os.environ.get("SESSION_LIFETIME_DAYS", "30") or 30)))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("SESSION_COOKIE_SECURE", "1").lower() not in ("0", "false", "no")
)

# #92: cap the request body. Flask's default is unbounded, and JSON endpoints
# read the whole body into memory (request.get_json), so an oversized body is a
# cheap memory-pressure DoS. 4 MB comfortably clears the 2 MB avatar upload —
# the only large legitimate body — so anything bigger gets an automatic 413.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024


@app.after_request
def _security_headers(resp):
    """#92: defense-in-depth response headers on every response. A full
    Content-Security-Policy is deferred (Alpine.js needs `unsafe-eval`, so a
    strict CSP needs its own build); HSTS is best set at the TLS-terminating
    reverse proxy, not here."""
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Drop the "Werkzeug/x.y Python/z" version banner (CVE-matching aid). Under
    # waitress in production this is also set via serve(ident=...); this covers
    # the dev-server path and is harmless (same value) under both.
    resp.headers["Server"] = "trobar"
    return resp


_EXPOSURE_PEER_CAP = 200


def _raw_peer() -> str | None:
    """The socket peer waitress actually saw, before ProxyFix's
    X-Forwarded-For rewrite. None outside a request context (tests)."""
    return request.environ.get("werkzeug.proxy_fix.orig", {}).get("REMOTE_ADDR")


@app.before_request
def _record_exposure_sample() -> None:
    """Only meaningful when trusted_proxy is "*": a pinned value already
    gates who ProxyFix ever sees X-Forwarded-For from (waitress itself
    strips the header from anyone else), so a directly-exposed port can't
    exploit it regardless of what this samples. Skipped in `forward` mode
    too — there the ForwardAuth gate is the real security boundary, and a
    reachable port is a separate question this signal isn't measuring."""
    if _trusted_proxy() != "*" or settings.AUTH_MODE == "forward":
        return
    peer = _raw_peer()
    if not peer:
        return
    now = time.time()
    with _EXPOSURE_LOCK:
        if len(_exposure_peers) < _EXPOSURE_PEER_CAP or peer in _exposure_peers:
            _exposure_peers[peer] = now
        cutoff = now - _EXPOSURE_WINDOW_S
        for stale in [p for p, seen in _exposure_peers.items() if seen < cutoff]:
            del _exposure_peers[stale]


@app.before_request
def _reject_cross_site_mutations():
    """CSRF defense-in-depth: reject state-changing requests that
    carry a cross-origin Origin header. The web UI is same-origin so its Origin
    matches request.host; a cross-site attacker page sends its own Origin and is
    blocked. Non-browser callers (curl) and the device Bearer API send no Origin
    and ride no ambient cookie, so they're unaffected. Paired with SameSite=Lax."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    # Device API authenticates by Bearer token (no session cookie to abuse);
    # the OIDC handshake legitimately crosses origins (IdP → app callback).
    # Trailing slash matters (#99): "/api/device/" is the Bearer token API;
    # "/api/devices*" are the session-cookie-authenticated web endpoints and
    # must keep the Origin check (a bare "/api/device" prefix exempted them).
    # The App API (/api/app/) is Bearer-authenticated too -- the same device
    # token, acting as the device's owner -- so the same reasoning exempts it.
    if request.path.startswith("/api/device/") or request.path.startswith("/api/app/") \
            or request.path.startswith("/oidc/"):
        return None
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        abort(403, description=_("Cross-site request blocked"))
    return None


def _get_locale() -> str:
    lang = request.cookies.get("lang")
    if lang in ("en", "fr"):
        return lang
    return request.accept_languages.best_match(["en", "fr"]) or "en"


babel = Babel(app, locale_selector=_get_locale)
app.jinja_env.globals["get_locale"] = get_locale

# DB-first (app_config 'music_root', set by the setup wizard),
# env var only seeds the initial default — see db.get_music_root(). Read
# fresh at each call site below rather than cached here, so a change in the
# wizard takes effect immediately.


# Bound to the app here rather than at creation: the registry itself lives
# in web_common, since the admin routes reset its Tidal and Spotify clients.
# OAuth(app) is exactly OAuth() then init_app(app). Nothing may call
# oauth.register() before this line: until init_app has run, register()
# returns a lazy proxy rather than a client.
oauth.init_app(app)
if settings.AUTH_MODE == "oidc":
    # server_metadata_url points Authlib at the IdP's discovery document; it
    # handles endpoint discovery, JWKS fetch/caching, and full ID-token
    # validation (signature + standard claims + nonce) on token exchange.
    oauth.register(
        name="oidc",
        client_id=settings.OIDC_CLIENT_ID,
        client_secret=settings.OIDC_CLIENT_SECRET,
        server_metadata_url=f"{settings.OIDC_ISSUER.rstrip('/')}/.well-known/openid-configuration",
        # PKCE (S256) on top of the confidential-client secret — defense in
        # depth, recommended by OAuth 2.1 even when a client secret is present.
        client_kwargs={"scope": settings.OIDC_SCOPES, "code_challenge_method": "S256"},
    )


def _forward_auth_warnings() -> list[str]:
    """What the server says at startup about trusting more than a forward-mode
    deployment may realise. Printed by __main__; a function so it is tested."""
    if settings.AUTH_MODE != "forward":
        return []
    warnings = []
    if not settings.FORWARD_AUTH_SECRET:
        warnings.append(
            "AUTH_MODE=forward with no FORWARD_AUTH_SECRET set — the app trusts "
            "identity headers with no verification that a trusted proxy set them. Ensure the app "
            "port is NEVER exposed directly and your proxy strips client-supplied X-authentik-* "
            "headers. Set FORWARD_AUTH_SECRET (injected by your proxy) to fail-closed instead.")
    if settings.DEV_SERVER:
        warnings.append(
            "AUTH_MODE=forward with TROBAR_DEV_SERVER on — a request without the identity "
            "header is let in as DEV_USER instead of refused. Local development only: never "
            "run this on an instance anyone else can reach.")
    return warnings


_SECRET_KEY_FILE = db.DATA_DIR / "flask_secret_key"


def _load_or_create_secret_key() -> bytes:
    if _SECRET_KEY_FILE.exists():
        return _SECRET_KEY_FILE.read_bytes()
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    _SECRET_KEY_FILE.write_bytes(key)
    # Whoever holds this key can forge session cookies — keep it owner-only
    # at rest. Best-effort (see db.get_conn).
    try:
        os.chmod(db.DATA_DIR, 0o700)
        os.chmod(_SECRET_KEY_FILE, 0o600)
    except OSError:
        pass
    return key


app.secret_key = _load_or_create_secret_key()


@app.before_request
def _require_session_when_app_authenticates():
    """Bites when the app itself is responsible for authentication — AUTH_MODE
    local or oidc, and on the emergency port regardless of mode — in all these
    cases nothing upstream of Flask has verified who's asking, so a session is
    mandatory. `forward` mode via a ForwardAuth proxy is unchanged (the proxy
    already gated the request before Flask saw it). The device API and #446's
    read-only integration API each authenticate by their own Bearer token
    regardless of any of this, never by session."""
    if not _app_authenticates():
        return None
    # #446: the /api/integrations/ prefix is exempted wholesale, not this
    # one route by name -- a future route added under it is login-exempt by
    # default, so forgetting to call _authenticated_integration_token() there
    # would leave it fully UNauthenticated rather than merely over-permissioned.
    # Same shape as the existing /api/device/ exemption, not a new risk,
    # but worth remembering before adding a second route under this prefix.
    # /api/app/ is the third Bearer-authenticated prefix (the phone's own
    # screens, acting as the device's owner). Same wholesale exemption, same
    # standing warning: a route added under it without calling
    # _authenticated_app_user() is unauthenticated, not merely over-permissioned.
    if request.path.startswith("/api/device/") or request.path.startswith("/api/integrations/") \
            or request.path.startswith("/api/app/") \
            or request.path.startswith("/static") \
            or request.path.startswith("/set-language/") \
            or request.path.startswith("/oidc/") \
            or request.path in ("/login", "/logout", "/api/enrollment/redeem", "/healthz"):
        # #163: redeem is the one enrollment path a session-less device hits (it
        # has no token yet); the grant that authorizes it is its credential.
        # /api/enrollment/grant stays session-gated (it's minted by a web user).
        # /healthz answers only whether the database does (see ops.py).
        return None
    with db.connection() as conn:
        if _session_user_id(conn) is not None:
            return None
    if request.path.startswith("/api/"):
        abort(401, description=_("Login required"))
    return redirect(url_for("auth.login"))


@app.before_request
def _require_setup_wizard():
    """AUTH_MODE=local only. oidc/forward modes provision the admin
    externally (verified ID-token claim / proxy header + ADMIN_USERNAME
    self-promotion, see _provision_user) and have no equivalent "first admin
    account" step to walk through, so the wizard doesn't apply there at all.
    Only ever redirects actual page loads
    (not /api/* calls) for a logged-in admin whose session predates — or
    survived closing the tab during — an unfinished wizard: completion is a
    persisted app_config flag, not tied to how many users happen to exist."""
    if settings.AUTH_MODE != "local" or request.path.startswith("/api/") or request.path.startswith("/static") \
            or request.path.startswith("/set-language/") \
            or request.path in ("/login", "/logout", "/setup", "/healthz"):
        # /healthz too: this gate opens the database first, so with it down
        # it would answer 500 for /healthz instead of /healthz's own 503.
        return None
    with db.connection() as conn:
        user_id = _session_user_id(conn)
        if user_id is None:
            return None  # the other before_request hook handles getting them to /login first
        if not _is_admin(conn, user_id) or db.get_config(conn, "setup_completed"):
            return None
    return redirect(url_for("auth.setup_wizard"))


@app.errorhandler(HTTPException)
def api_error_json(e: HTTPException):
    """Every /api/* route is fetch()-driven from the web UI (or the Android
    client, for /api/device/*) — none of them have an HTML error page to
    fall back on usefully. Widened from device-only to all of /api/ after
    noticing the new admin/account endpoints' descriptive abort() messages
    ("This username already exists" etc.) were silently never reaching
    the frontend, which saw an HTML error page and fell back to a bare
    'HTTP 400'."""
    if request.path.startswith("/api/"):
        return jsonify({"error": e.description or e.name}), e.code
    return e

# ---------------------------------------------------------------------------
# Current-user resolution (web UI only — trusts Authentik's ForwardAuth
# headers, since that router is gated by authentik@file in Traefik; the
# device API never calls this, it authenticates by per-device Bearer token)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

def _build_js_i18n() -> dict:
    """Strings needed by index.html's Alpine app() for client-side dynamic
    content (relative-time, confirm/alert dialogs, catch-block wrappers) —
    resolved through the same gettext() catalog as every server-rendered
    string, so client and server never disagree on language for a request."""
    return {
        "locale": str(get_locale()),
        "tabs": {
            "library": _("Library"),
            "playlists": _("Playlists"),
            "selections": _("Sync"),
            "suggestions": _("Suggestions"),
            "devices": _("Devices"),
        },
        "themeOptions": {
            "auto": _("System"),
            "light": _("Light"),
            "dark": _("Dark"),
        },
        # The brand themes' names are proper names (brand/BRAND.md) and stay
        # as they are in every language; their one-line character translates.
        "brandOptions": {
            "troubadour": _("Default · cream"),
            "nuech": _("Night · dark only"),
            "garriga": _("Sage · olive"),
            "peira": _("Stone · terracotta"),
            "contraste": _("High contrast · dark only"),
        },
        "deviceType": {
            "phone": _("Phone"), "tablet": _("Tablet"),
            "watch": _("Smartwatch"), "dap": _("Dedicated audio player"),
            # #218: relabelled from "SD card / USB storage" — a local-folder
            # target (device_type 'folder', below) used to have nowhere to
            # go but this bucket, so the old wording overclaimed what it
            # covered. The stored value is unchanged, existing devices are
            # unaffected.
            "sdcard": _("Removable storage"),
            "folder": _("Local folder"),
        },
        # Home dashboard widget catalog labels — keyed by the ids of
        # DASHBOARD_WIDGET_CATALOG; a test holds the two together.
        "widgets": {
            "library": _("Library"),
            "devices": _("Devices"),
            "suggestions": _("Suggestions"),
            "recently_added": _("Recently added"),
            "recently_released": _("Recently released"),
            "most_played": _("Most played"),
            "administration": _("Administration"),
        },
        # #263: accessible labels for the Preferences widget-reorder buttons
        # — dynamic (which widget), so can't be a static Jinja _() call in
        # the template like most of this file's strings.
        "moveWidgetUp": _("Move {name} up"),
        "moveWidgetDown": _("Move {name} down"),
        # #265: the storage-breakdown bar's aria-label, spelling out what the
        # segments are for anyone not reading the colors (screen reader, or
        # just someone who wants the numbers) — same role="img" + text-label
        # pattern as the Library widget's codec/decade charts.
        # Music already in the sync folder that Trobar did not put there.
        # "Max music size" is the limit on that folder, so this counts
        # against it -- named for what it is from the user's side rather
        # than as "foreign", which is our word for it and not theirs.
        "storageForeignShare": _("{gb} GB already in the folder"),
        "foreignBytesUnmeasured": _(
            "This device hasn't reported what else is in its music folder yet, so "
            "anything already there isn't counted against the limit."),
        "folderOverLimit": _(
            "The music folder holds {used} GB, over its {max} GB limit — including "
            "music Trobar didn't put there."),
        "storageManualShare": _("{gb} GB manual picks"),
        "storageAutofitShare": _("{gb} GB auto-fit (up to {pct}%)"),
        "storageFreeShare": _("{gb} GB headroom"),
        "providerState": {
            "paired": _("Connected"),
            "pending_approval": _("Awaiting approval"),
            "disconnected": _("Disconnected"),
        },
        # Administration > Configuration's collapsible sections: the names
        # (for "unsaved changes in …"), the one-line summary each header
        # shows, and the warnings that stay on a header even when its
        # section is collapsed.
        "configSectionNames": {
            "library": _("Library"),
            "sources": _("Playlist sources"),
            "playlists": _("Playlists"),
            "listening": _("Listening"),
            "devices": _("Devices"),
            "maintenance": _("Maintenance"),
            "security": _("Security"),
        },
        # Change library source dialog.
        "switchSteps": {
            "choose": _("Choose"),
            "connect": _("Connect"),
            "review": _("Review and confirm"),
            "done": _("Done"),
        },
        "switchChooseIntro": _("Where should Trobar read your library's playlists from? {current}, the current source, isn't listed."),
        "switchNeeds": _("Needs: {needs}"),
        # The providers docs table's "What you get / What it needs".
        "switchProviders": {
            "filesystem": {"gets": _("Library browsing and sync, zero dependencies"),
                           "needs": _("nothing, just the music folder")},
            "roon": {"gets": _("Playlists, live pairing status"), "needs": _("a Roon Core on the LAN")},
            "jellyfin": {"gets": _("Playlists"), "needs": _("server URL, API key and username")},
            "emby": {"gets": _("Playlists"), "needs": _("server URL, API key and username")},
            "plex": {"gets": _("Playlists"), "needs": _("server URL and token")},
            "lms": {"gets": _("Playlists"), "needs": _("server URL (and user and password if secured)")},
            "subsonic": {"gets": _("Playlists"), "needs": _("server URL, user and password")},
            "music_assistant": {"gets": _("Playlists, and a mirror target"),
                                "needs": _("server URL and a long-lived token")},
        },
        "switchField": {
            "host": _("Roon host"),
            "port": _("Port"),
            "url": _("Server URL"),
            "username": _("Username"),
            "password": _("Password"),
            "api_key": _("API key"),
            "token": _("Token"),
            "usernameOptional": _("Username (optional)"),
            "passwordOptional": _("Password (optional)"),
        },
        "switchConnectIntro": _("Enter {provider}'s connection details and test them."),
        "switchFilesystemNothing": _("Nothing to connect: Filesystem reads playlists from the music folder Trobar already uses."),
        "switchSetupGuide": _("{provider} setup guide"),
        "switchTest": _("Test connection"),
        "switchTesting": _("Testing…"),
        "switchTestFirst": _("Test the connection to continue."),
        "switchTestOk": _("✓ Connected"),
        "switchTestReachable": _("✓ Roon Core reachable. After the switch, approve Trobar in Roon (Settings, Extensions)."),
        "switchTestFailed": _("Couldn't connect with these details."),
        "switchReviewIntro": _("Nothing has changed yet. This is what switching from {from} to {to} does, counted on this server just now. Only {from}'s own playlists are affected."),
        "switchRemovedHead": {
            "one": _("Removed: {n} playlist from {from}"),
            "other": _("Removed: {n} playlists from {from}"),
        },
        "switchRemovedNone": _("Removed: no playlists, {from} has none of its own"),
        "switchCarriedHead": {
            "one": _("Carried over: {n} of {from}'s {of} playlists"),
            "other": _("Carried over: {n} of {from}'s {of} playlists"),
        },
        "switchCarriedWhat": _("{to} has a playlist with the same name, owner and most of the same tracks. Each keeps its device selections, mirroring, shared/private choice and excluded gaps. Its tracks are refreshed from {to} on the first sync."),
        "switchRemovedNotThere": {
            "one": _("Removed: {n} playlist {to} doesn't have"),
            "other": _("Removed: {n} playlists {to} doesn't have"),
        },
        "switchCarryNone": _("{to} has no playlist with the same name, owner and most of the same tracks."),
        "switchCarryRoon": _("None can be carried over: Roon can't list its playlists until Trobar is approved in Roon, after the switch."),
        "switchCarryFailed": _("None can be carried over: {to} couldn't list its playlists just now. Go back and test again to retry."),
        "switchCarryListed": _("None can be carried over: {to}'s playlists are already in Trobar as playlists of their own."),
        "switchDoneCarried": {
            "one": _("{n} playlist was carried over, with its settings."),
            "other": _("{n} playlists were carried over, with their settings."),
        },
        "switchRemovedNoneMerged": _("Removed: no playlists. The .m3u folder's playlists stay: they are read whichever source is active."),
        "switchRemovedMirrored": {
            "one": _("Mirroring was on for {n} of them; its mirrored copies are removed too."),
            "other": _("Mirroring was on for {n} of them; their mirrored copies are removed too."),
        },
        "switchDeviceHas": {
            "one": _("{name} has {n} of them selected."),
            "other": _("{name} has {n} of them selected."),
        },
        "switchDevicesConsequence": _("Those selections are removed with the playlists. On each device's next sync, Trobar removes those playlist files, and any tracks no other selection keeps on the device."),
        "switchImages": _("Also the artist-image cache: images are fetched again from {to}."),
        "switchUntouchedPlaylists": {
            "one": _("{n} playlist from other sources, with its settings: {sources}."),
            "other": _("{n} playlists from other sources, with all their settings: {sources}."),
        },
        "switchKeptFrom": _("{n} from {source}"),
        "switchSources": {
            "filesystem": _("the .m3u folder and iTunes"),
            "music_assistant": _("Music Assistant"),
            "tidal": _("linked Tidal accounts"),
            "spotify": _("linked Spotify accounts"),
            "ytmusic": _("YouTube Music subscriptions"),
            "spotify_public": _("Spotify playlist links"),
            "other": _("no recorded source"),
        },
        "switchUntouchedRest": _("The library, users and devices; artist, album and track selections; listening history; every other setting on this page."),
        "switchNothingSaved": _("Nothing is saved until the last step."),
        "switchConfirm": _("Switch to {provider}"),
        "switchSwitching": _("Switching…"),
        "switchDoneHead": _("✓ Switched to {provider}"),
        "switchDoneRemoved": {
            "one": _("{n} playlist from the old source was removed."),
            "other": _("{n} playlists from the old source were removed."),
        },
        "switchDoneDevices": {
            "one": _("{n} device had playlist selections removed; its next sync drops those files."),
            "other": _("{n} devices had playlist selections removed; their next sync drops those files."),
        },
        "switchDoneRoon": _("Approve Trobar in Roon (Settings, Extensions) so it can read your playlists."),
        "switchSyncRunning": _("First playlist sync from {provider}: running…"),
        "switchSyncDone": {
            "one": _("First playlist sync: {n} playlist."),
            "other": _("First playlist sync: {n} playlists."),
        },
        "playlistSourceFailed": _("{provider} couldn't be synced for {user}: {reason}. Its playlists are unchanged."),
        "switchSyncPrimaryFailed": _("First playlist sync: {provider} couldn't list its playlists yet; the other sources synced ({n})."),
        "switchSyncFailed": _("The first playlist sync failed: {error}. Run it again from Playlists once the connection works."),
        "switchSyncNotStarted": _("The first playlist sync didn't start: another one is running. Its result appears in Playlists."),
        "switchDoneHint": _("Open Playlists to check them, or select playlists for a device again from its page."),
        "configUnsavedChange": _("Unsaved change"),
        # Administration > Users headers.
        "usersSumAccounts": {"one": _("{n} user"), "other": _("{n} users")},
        "usersSumAdmins": {"one": _("{n} admin"), "other": _("{n} admins")},
        "usersSumBreakGlassOk": _("break-glass ✓"),
        "usersWarnNoBreakGlass": _("No break-glass account"),
        "usersSumDelegations": {"one": _("{n} delegation"), "other": _("{n} delegations")},
        "usersSumNoDelegations": _("none"),
        "userManages": _("manages: {names}"),
        "userManagedBy": _("managed by: {names}"),
        "configUnsavedIn": _("Unsaved changes in: {sections}"),
        "cfgSumLibrary": _("{provider} · {state}"),
        "cfgSumRescanEvery": _("rescan every {n} h"),
        "cfgSumRescanOff": _("automatic rescan off"),
        "cfgSumPlaylistSyncEvery": _("playlist sync every {n} h"),
        "cfgSumPlaylistSyncOff": _("automatic playlist sync off"),
        "cfgSumSources": _("{n} of {total} set up"),
        "cfgSumAcoustidOn": _("AcoustID matching on"),
        "cfgSumAcoustidOff": _("AcoustID matching off"),
        "cfgSumMirrorTargets": {
            "one": _("mirroring to {n} target"),
            "other": _("mirroring to {n} targets"),
        },
        "cfgSumMirrorNone": _("no mirror target"),
        "cfgSumLidarrOn": _("Lidarr connected"),
        "cfgSumLidarrOff": _("no Lidarr"),
        "cfgSumLastfmKey": _("default Last.fm key set"),
        "cfgSumLastfmNoKey": _("no default Last.fm key"),
        "cfgSumListenStandard": _("standard Last.fm and ListenBrainz servers"),
        "cfgSumListenCustom": _("custom listening-history servers"),
        "cfgSumTranscode": _("{n} transcodes at a time, priority {nice}"),
        "cfgSumTranscodeCache": _("{n} GB cache"),
        "cfgSumTranscodeNoCache": _("no cache"),
        "cfgSumJobs": _("job history kept {n} days"),
        "cfgSumSignIn": _("sign-in: {mode}"),
        "cfgWarnFieldToFix": _("A field to fix"),
        "cfgWarnWritable": _("Library mounted writable"),
        "cfgWarnProviderDown": _("Library source disconnected"),
        "cfgWarnProviderPending": _("Library source awaiting approval"),
        "cfgWarnMaDown": _("Music Assistant unreachable"),
        "cfgWarnMirrorDown": _("{target} mirror target unreachable"),
        "cfgWarnLidarrDown": _("Lidarr unreachable"),
        "cfgWarnLidarrIncomplete": _("Lidarr: profiles not chosen"),
        "cfgWarnBreakGlass": _("Break-glass password not set"),
        # #509: Lidarr-specific — the only provider config in Administration
        # with a post-pairing step (root folder + quality/metadata
        # profiles). "Connected" alone used to be shown as soon as
        # url/api_key worked, even with none of the three chosen, which put
        # Administration and the per-playlist request button in flat
        # contradiction (green here, "not set up" there). Distinct text and
        # colour (amber, not the plain green providerState.paired gets) so
        # it reads as its own state, not a variant of "done".
        "lidarrConnectedIncomplete": _("Connected — choose a root folder and profiles below"),
        # #509 item 3: the admin config form's live pre-save check — fires
        # on blur once a field group looks complete, shown next to the
        # group being tested rather than replacing that provider's own
        # (last-SAVED) status line above, since the two mean different
        # things: this one is about what's currently typed, not yet saved.
        "connectionCheckChecking": _("Checking…"),
        "connectionCheckOk": _("✓ Reachable"),
        "connectionCheckFailed": _("Could not connect — check the URL and credentials."),
        # Same message as the Flask-side abort() in _is_valid_url()'s
        # caller — kept as one string in two catalogs (Python's gettext
        # and this JS one) rather than a shared constant, matching how
        # every other message already duplicated between a save-time
        # abort() and a JS-side hint in this file is handled.
        "invalidUrlMessage": _("Enter a valid http:// or https:// URL."),
        "lastfmOk": _("Connected"),
        "lastfmError": _("Connection error"),
        "selectAlbums": {"one": _("Select {n} album"), "other": _("Select {n} albums")},
        "sortByName": _("Sort: name"),
        "sortByAlbumCount": _("Sort: album count"),
        "scanning": _("Scanning…"),
        "rescan": _("Rescan"),
        "confirmFullRescan": _("A full rescan re-reads every file and can take a while. Continue?"),
        "albumCount": {"one": _("{n} album"), "other": _("{n} albums")},
        "reissuedSuffix": _("reissued {y}"),
        "listView": _("List view"),
        "coverView": _("Cover view"),
        "trackCount": {"one": _("{n} track"), "other": _("{n} tracks")},
        "discLabel": _("Disc {n}"),
        "appearsOnCount": _("appears on {n}"),
        "appearsOnHint": _("Albums by other artists with {artist} on them. Selecting one syncs only {artist}'s tracks on it."),
        "appearsOnLabel": _("{artist} on {album}"),
        "appearsOnRow": {"one": _("{count} of {n} track"), "other": _("{count} of {n} tracks")},
        "wholeAlbumRow": {"one": _("whole album, {n} track"), "other": _("whole album, {n} tracks")},
        "selectedBy": _("Selected by {name}"),
        "confirmRemoveFromDevice": {
            "one": _("Remove {item} from {device}? Its {n} track is deleted from {device} at its next sync, unless another selection still needs it."),
            "other": _("Remove {item} from {device}? Its {n} tracks are deleted from {device} at its next sync, unless another selection still needs them.")},
        "matrixAddFailed": _("Couldn't add {item} to {device}: {error}. Nothing changed."),
        "matrixRemoveFailed": _("Couldn't remove {item} from {device}: {error}"),
        "matrixForgetFailed": _("Couldn't forget {item}: {error}"),
        "libraryDuration": _("{duration} of music"),
        # #187: "%" kept clear of a following letter so Babel doesn't misread it
        # as a printf conversion (e.g. "% of" → %o), which breaks .po compilation.
        "libraryFormatShare": _("tracks {tracks}%, storage {storage}%"),
        "durationDaysHours": _("{days} days, {hours} hours"),
        "durationHours": _("{hours} hours"),
        "syncing": _("Syncing…"),
        "refreshPlaylists": _("Refresh playlists"),
        # Playlists made in Trobar: created, filled from the Library, edited.
        "newPlaylist": _("New playlist"),
        "newPlaylistEllipsis": _("New playlist…"),
        "editPlaylist": _("Edit"),
        "playlistName": _("Playlist name"),
        "playlistSourceTrobar": _("Made in Trobar"),
        "deletePlaylist": _("Delete…"),
        "playlistEditorDone": _("Done"),
        "playlistEditorHint": _("Drag a track by its handle, or use the arrows. Add tracks from the Library with “Add to playlist…”."),
        "playlistEditorEmpty": _("No tracks yet. Add some from the Library with “Add to playlist…”."),
        "moveTrackUp": _("Move {title} up"),
        "moveTrackDown": _("Move {title} down"),
        "removeTrack": _("Remove {title}"),
        # #287: plain words for what a delete does, for every playlist delete.
        "confirmDeletePlaylist": _("Delete the playlist “{title}”? Devices it was synced to remove it at their next sync, except devices set to keep their content. Copies Trobar made of it on other music servers or in the playlist folder are deleted too. The songs stay in your library."),
        "playlistActions": _("Actions"),
        "playlistActionsFor": _("Actions for “{title}”"),
        "playlistEditFailed": _("The playlist couldn't be saved."),
        "playChoiceFailed": _("What the watch plays couldn't be saved."),
        "notInLibrary": _("not in the library"),
        "addToPlaylist": _("Add to playlist…"),
        "addAlbumTo": {"one": _("Add the {n} track to"), "other": _("Add the {n} tracks to")},
        "addTrackToPlaylist": _("+ Playlist"),
        "addTrackTo": _("Add this track to"),
        "addTrackToPlaylistLabel": _("Add “{title}” to a playlist"),
        "addedToPlaylist": {"one": _("Added {n} track to “{title}”."),
                            "other": _("Added {n} tracks to “{title}”.")},
        # Uploading a .m3u/.m3u8 as a playlist, and downloading any playlist
        # as one.
        "uploadPlaylist": _("Upload playlist…"),
        "playlistUploaded": {"one": _("“{title}” uploaded: {matched} of {n} track matched."),
                             "other": _("“{title}” uploaded: {matched} of {n} tracks matched.")},
        "playlistUploadReplaced": {"one": _("“{title}” replaced: {matched} of {n} track matched."),
                                   "other": _("“{title}” replaced: {matched} of {n} tracks matched.")},
        "playlistUploadUnmatched": {"one": _("The {n} other is in its unresolved list."),
                                    "other": _("The {n} others are in its unresolved list.")},
        "playlistUploadNoneMatched": _("They are all in its unresolved list."),
        "playlistUploadFailed": _("The upload failed."),
        "playlistSourceUpload": _("Uploaded file"),
        "historySourceDevices": _("My devices"),
        "credentialSaved": _("Saved, encrypted"),
        "lastfmConnectedAs": _("Connected as {name}"),
        "credentialUnreadable": _("This can't be read any more: enter it again."),
        "playSendMalojaUrl": _("Add your Maloja URL in Scrobbler accounts."),
        "playSendMalojaKey": _("Add your Maloja API key in Scrobbler accounts."),
        "playSendLbUser": _("Add your ListenBrainz username in Scrobbler accounts."),
        "playSendLbToken": _("Add your ListenBrainz user token in Scrobbler accounts."),
        "playSendLfmApp": _("An admin needs to set up Last.fm first."),
        "playSendLfmConnect": _("Connect Last.fm in Scrobbler accounts."),
        "playSendUnreadable": _("Enter it again in Scrobbler accounts."),
        "playSendRefused": _("It refused the credential: check it in Scrobbler accounts."),
        "playSendUnreachable": _("It didn't answer. Plays go when it's back, with the next ones."),
        "playSendRejected": {"one": _("It rejected {n} play, which won't be sent."),
                             "other": _("It rejected {n} plays, which won't be sent.")},
        "devicePlaysNone": _("No plays stored."),
        "devicePlaysStored": {"one": _("{n} play stored."), "other": _("{n} plays stored.")},
        "devicePlaysDeleteTitle": {"one": _("Delete {n} play?"), "other": _("Delete {n} plays?")},
        "newRulePlaylist": _("New rule playlist…"),
        "editRule": _("Edit rule…"),
        "playlistSourceRule": _("Rule playlist"),
        "ruleNewTitle": _("New rule playlist"),
        "ruleEditTitle": _("Edit rule playlist"),
        "ruleFieldLabel": _("Field"),
        "ruleTestLabel": _("Test"),
        "ruleValueLabel": _("Value"),
        "ruleValueToLabel": _("To"),
        "ruleMatchLabel": _("All or any"),
        "ruleAll": _("all"),
        "ruleAny": _("any"),
        "ruleYes": _("yes"),
        "ruleNo": _("no"),
        "ruleAnd": _("and"),
        "ruleMinutes": _("min"),
        "ruleDays": _("days"),
        "ruleRemove": _("Remove this condition"),
        "ruleRemoveGroup": _("Remove this group"),
        "ruleAddCondition": _("+ Add a condition"),
        "ruleAddGroup": _("+ Add a group"),
        "ruleNoLimit": _("no limit"),
        "ruleCreate": _("Create"),
        "ruleSave": _("Save"),
        "ruleFieldYear": _("Year"),
        "ruleFieldFormat": _("File type"),
        "ruleFieldLossless": _("Lossless"),
        "ruleFieldAdded": _("Added"),
        "ruleFieldArtist": _("Artist"),
        "ruleFieldAlbum": _("Album"),
        "ruleFieldLength": _("Length"),
        "ruleOpBetween": _("between"),
        "ruleOpIs": _("is"),
        "ruleOpFrom": _("from"),
        "ruleOpUntil": _("until"),
        "ruleOpOneOf": _("is one of"),
        "ruleOpSince": _("since"),
        "ruleOpInLast": _("in the last"),
        "ruleOpContains": _("contains"),
        "ruleOpAtLeast": _("at least"),
        "ruleOpAtMost": _("at most"),
        "ruleFinish": _("Finish the conditions to see how many tracks match."),
        "ruleInvalid": _("One of the values in this rule can't be used."),
        "ruleNoMatch": _("No tracks match this rule yet."),
        "ruleNoMatchHint": _("You can still save it: it fills when matching tracks are scanned."),
        "ruleKeepsAdded": _("{n} match; the limit keeps the most recently added."),
        "ruleKeepsArtist": _("{n} match; the limit keeps the first by artist."),
        "ruleKeepsRandom": _("{n} match; the limit keeps a shuffled selection."),
        "ruleWouldHold": {"one": _("Would hold {n} track"), "other": _("Would hold {n} tracks")},
        "downloadPlaylist": _("Download…"),
        "downloadPlaylistM3u8": _("Download .m3u8"),
        "downloadIncludeUnmatched": _("Include unmatched entries, as comments"),
        "dismiss": _("Dismiss"),
        # #411: shown only while the hide-zero-match filter is on, so a
        # filtered-down list doesn't just look short.
        "playlistsShownCount": _("{shown} of {total} shown"),
        "playlistAvailability": _("{matched}/{total} tracks available locally ({pct}%)"),
        # Public-playlist URL subscriptions. The two "nothing matched"
        # strings are separate on purpose: a playlist that fetched fine and
        # matched nothing is a different problem from one we could not
        # fetch, and the pasted URL looks identical in both cases.
        "subscriptionAddFailed": _("Couldn't add that playlist."),
        "subscriptionUnavailable": _("Unavailable — deleted, made private, or the link is wrong."),
        "subscriptionFetchFailed": _("Couldn't reach YouTube Music. Nothing already imported was changed."),
        "subscriptionFetchFailedSpotify": _("Couldn't read this playlist from Spotify. Nothing already imported was changed."),
        # Spotify's embed page lists a playlist's first 100 tracks and gives
        # no total, so a playlist at the limit may be longer. Said on the row
        # so the missing tail doesn't read as tracks that failed to match.
        "subscriptionAtLimit": _("Spotify shares only the first {limit} tracks of a playlist, so this one may be longer."),
        "subscriptionNoMatches": _("Fetched, but none of its tracks are in your library."),
        "subscriptionMatchCount": _("{matched} of {total} tracks in your library"),
        "subscriptionRemoveConfirm": _("Remove \"{title}\" and the playlist it created?"),
        "sending": _("Sending…"),
        "chooseImage": _("Choose an image"),
        "saving": _("Saving…"),
        "disconnect": _("Disconnect"),
        "tidalConnectedAs": _("Connected as {name}"),
        # The header's avatar button, and its count of Tidal invitations.
        "profileButton": _("Profile"),
        "profileWithInvitations": {"one": _("Profile, {n} invitation"), "other": _("Profile, {n} invitations")},
        "invitationsWaiting": {"one": _("{n} invitation waiting"), "other": _("{n} invitations waiting")},
        # Tidal playlist shares (Playlists → Share with…, and Profile).
        "tidalShareTitle": _("Share “{title}”"),
        "tidalShareLinked": _("Tidal linked"),
        "tidalShareMustReconnect": _("needs to reconnect Tidal"),
        "tidalShareRecipientReconnect": _("{name} has to reconnect Tidal before the copy can appear. The invitation waits, and the copy follows once they do."),
        "tidalShareSendInvitation": _("Send invitation"),
        "tidalShareNow": _("Share now"),
        "tidalShareInvited": _("Invitation sent to {name}. They answer it on their Profile."),
        "tidalShareSharedNow": _("Shared with {name}. The copy appears in their Tidal account at the next playlist sync."),
        "tidalSharePending": _("Waiting for an answer"),
        "tidalShareNewInvitation": _("New invitation"),
        "tidalShareSyncing": _("Syncing"),
        "tidalShareNeedsAttention": _("Needs attention"),
        "tidalShareEnded": _("Ended"),
        "tidalShareWith": _("with {name}"),
        "tidalShareFrom": _("From {name}"),
        "tidalShareOneWayShort": _("one-way"),
        "tidalShareTwoWayShort": _("two-way"),
        "tidalShareTracks": {"one": _("{n} track"), "other": _("{n} tracks")},
        "tidalShareInvitedWhen": _("invited {time}"),
        "tidalShareEndedWhen": _("ended {time}"),
        "tidalShareSyncedWhen": _("last synced {time}"),
        "tidalShareNotSyncedYet": _("not synced yet"),
        "tidalShareInviteOneWay": _("If you accept, Trobar creates “{title}” in your Tidal account and keeps it the same as {name}'s on each playlist sync. Changes you make to it are replaced; rename it if you like, the name stays yours."),
        "tidalShareInviteTwoWay": _("If you accept, Trobar creates “{title}” in your Tidal account and keeps it in step with {name}'s on each playlist sync, both ways: what either of you adds or removes reaches the other."),
        "tidalShareUnavailableYou": {"one": _("{n} track isn't available in your region, so your copy doesn't have it."), "other": _("{n} tracks aren't available in your region, so your copy doesn't have them.")},
        "tidalShareStopKeeps": _("Stopping keeps your copy as your own playlist, and {name}'s as theirs."),
        "tidalShareTwoWayFor": _("Two-way: {name}'s changes come back to your playlist"),
        "tidalShareDeleteQueued": _("Your original will be deleted at the next playlist sync, once Trobar has checked {name}'s copy still has every track."),
        "tidalShareDeleteTitle": _("Delete your original “{title}”?"),
        "tidalShareDeleteCopyStays": _("{name}'s copy stays in their Tidal account as their own playlist. Yours is deleted from your Tidal account at the next playlist sync."),
        "tidalShareErrNotLinked": _("Waiting for Tidal to be linked on both sides, yours and {name}'s."),
        "tidalShareErrReconnect": _("Waiting for a Tidal reconnect before anything can be written."),
        "tidalShareErrSourceDeleted": _("The original playlist was deleted in Tidal, so the share ended."),
        "tidalShareErrCopyDeleted": _("The copy was deleted in Tidal, so the share ended."),
        "tidalShareErrNotOwned": _("Two-way needs a playlist you own in Tidal; you only follow this one. Nothing is written until you switch it to one-way."),
        "tidalShareErrNotOwnedRecipient": _("Paused: two-way needs a playlist {name} owns in Tidal, and they only follow this one."),
        "tidalShareErrRetry": _("The last sync didn't come out as expected. Trobar tries again at the next one."),
        "tidalShareKeptLacks": _("Your original was kept: {name}'s copy lacks some of its tracks (by position in your playlist: {positions}). Remove those from your playlist in Tidal and ask again, or keep the original."),
        "tidalShareKeptCopyMissing": _("Your original was kept: {name}'s copy no longer exists in Tidal."),
        "tidalShareKeptNotOwned": _("Your original was kept: you only follow that playlist in Tidal, so it isn't yours to delete."),
        "tidalShareKeptStillShared": _("Your original was kept: it's still shared with someone else."),
        "spotifyConnectedAs": _("Connected as {name}"),
        "unknownTracksNote": _("{count} on device, not in library"),
        "unknownTracksReviewHint": _("Review these tracks"),
        "unknownTracksReviewTitle": _("On device, not in your library"),
        "unknownTracksReviewHelp": _(
            "These are on the device but match no track in your library — added "
            "outside Trobar, or kept after a library change. Adopt to acknowledge "
            "one as device-owned: the server records it and leaves it alone (it "
            "isn't added to your library)."),
        "unknownTrackAdopt": _("Adopt"),
        "unknownTrackAdopted": _("Adopted"),
        "unknownTracksAdoptAll": _("Adopt all"),
        "unknownTracksError": _("Couldn't load or save — try again."),
        "unresolvedTracksNote": _("{count} not matched locally"),
        "unresolvedTracksReviewHint": _("Review these tracks"),
        "unresolvedTracksReviewTitle": _("Not matched to your library"),
        "unresolvedTracksReviewHelp": _(
            "These tracks in this playlist don't match anything in your library — "
            "streamed but never downloaded, a spelling mismatch, or genuinely "
            "missing. Exclude to acknowledge one as expected: it stops being "
            "flagged here (this doesn't change what syncs to a device)."),
        "unresolvedTrackExclude": _("Exclude"),
        "unresolvedTrackExcluded": _("Excluded"),
        "unresolvedTracksExcludeAll": _("Exclude all"),
        # A playlist made on a device: a match from the file name is only
        # a suggestion until accepted.
        "guessesHelp": _("Matched only by the file's name on the card. Not sent to a device or a mirror until you accept it."),
        "guessedFromFileName": _("Guessed from the file name: {artist} – {title}"),
        "acceptGuess": _("Accept"),
        "acceptGuessLabel": _("Accept {artist} – {title}"),
        "excludeGuessLabel": _("Exclude {artist} – {title}"),
        "guessAccepted": _("Accepted: matched from now on"),
        "guessUndo": _("Undo"),
        "unresolvedTracksError": _("Couldn't load or save — try again."),
        # After an album the source didn't give, found in MusicBrainz instead.
        "unresolvedAlbumInferred": _("{album} (from MusicBrainz)"),
        # #507: the four per-sink mirror buttons collapsed into one button
        # that opens a picker — these three are the picker's own generic
        # strings; the per-sink to/on/off/hint strings below are unchanged
        # and now render as picker rows instead of buttons on the row.
        "mirrorPickerButton": _("Mirror…"),
        "mirrorPickerNotConfiguredHint": _("Mirroring isn't set up yet — ask an admin to configure a mirror target in Administration."),
        # The identity-row badge's title/aria-label (#507 item 5) — a
        # composed hand+provider icon needs a text equivalent, same
        # "wasn't legible without it" reasoning as #410's error-line fix
        # right below the button that used to carry this.
        "mirroredToHint": _("Mirrored to {provider}"),
        # Coverage of a mirror's last write (mirror_runs): the admin line,
        # its Show list and the playlist row's icon tooltip.
        "mirrorOnTarget": _("{on} / {total} on {sink}"),
        "mirrorNotOnTarget": {"one": _("{n} not on {sink}"), "other": _("{n} not on {sink}")},
        "mirrorRefusedBy": {"one": _("{n} refused by {sink}"), "other": _("{n} refused by {sink}")},
        "mirrorShowMissing": _("Show"),
        "mirrorHideMissing": _("Hide"),
        "mirrorRecentWrites": _("Last writes: {list} on {sink}."),
        "mirrorReasonNotOn": _("Not on {sink}"),
        "mirrorReasonRefused": _("Refused after the write"),
        "mirrorMissingCap": _("Showing {shown} of {n}: each write keeps the first {max}."),
        "mirrorMissingFailed": _("The list could not be loaded."),
        "mirrorShortChip": {
            "one": _("{n} playlist short on a target"),
            "other": _("{n} playlists short on a target"),
        },
        "mirrorAllOnTarget": _("all {n} tracks on {provider}"),
        "mirrorSomeOnTarget": {"one": _("{on} of {n} track on {provider}"),
                               "other": _("{on} of {n} tracks on {provider}")},
        "mirrorTo": _("Mirror to…"),
        "mirroring": _("Mirroring"),
        "mirrorOnHint": _("Kept in sync as a local .m3u file. Click to stop mirroring."),
        "mirrorOffHint": _("Mirror this playlist to a local .m3u file, kept in sync as your library grows."),
        "mirrorFolderUnset": _("No mirror folder configured — set one in Administration > Configuration."),
        "mirrorLastWritten": _("Last written {when}"),
        # Admin > Playlist mirrors: each target by name, what it is (in the
        # nothing-configured explanation), its state, and the two sections'
        # header summaries and warnings.
        "mirrorSinkNames": {
            "filesystem": _(".m3u folder"),
            "subsonic": "Subsonic",
            "jellyfin": "Jellyfin",
            "emby": "Emby",
            "music_assistant": "Music Assistant",
            "plex": "Plex",
        },
        "mirrorSinkWhat": {
            "filesystem": _("for players that read playlist files"),
            "server": _("a playlist on that server"),
        },
        "mirrorStateReady": _("Ready"),
        "mirrorStateConnected": _("Connected"),
        "mirrorStateUnreachable": _("Unreachable"),
        "mirrorStateNotWritable": _("Can't write to the folder"),
        "mirrorStateWriteFailed": {
            "one": _("{n} playlist failed its last write"),
            "other": _("{n} playlists failed their last write"),
        },
        "mirrorStateLidarrIncomplete": _("Needs attention: root folder and profiles not chosen"),
        "mirrorStateRequestFailed": {
            "one": _("{n} playlist's last request failed"),
            "other": _("{n} playlists' last request failed"),
        },
        "mirrorSumTargets": {
            "one": _("{n} mirror target"),
            "other": _("{n} mirror targets"),
        },
        "mirrorSumNoTarget": _("No mirror target"),
        "mirrorNoTargetChip": _("No mirror target set up"),
        "mirrorAlsoAvailable": _("Also available: {names}."),
        "mirrorSumPlaylists": {
            "one": _("{n} playlist"),
            "other": _("{n} playlists"),
        },
        "mirrorFailedChip": {
            "one": _("{n} playlist with a failed write"),
            "other": _("{n} playlists with a failed write"),
        },
        # #428: mirror.py stores a machine-readable mirror_last_error_code
        # alongside mirror_last_error now — a background job has no user
        # locale to translate into, and the same row is read by users with
        # different languages. unset_folder needs no prefix here, since
        # it's fully covered by mirrorFolderUnset above with no detail to
        # append. The other four interpolate an OS exception or a
        # filename, which can never itself be translated (an OSError
        # stringifies in the C library's locale, not the user's) — only
        # the prefix is, the client appends the untranslated detail after it.
        "mirrorErrorNotWritable": _("Mirror folder isn't writable:"),
        "mirrorErrorBadFilename": _("Computed filename is invalid — not writing it:"),
        "mirrorErrorMarkerUnsafe": _("A file already exists that wasn't created by Trobar — not overwriting it:"),
        "mirrorErrorWriteFailed": _("Failed to write mirror file:"),
        # #189: the second sink — a Subsonic/Navidrome server as a write
        # target. Same shape as every filesystem-mirror key above, kept
        # separate rather than parameterized since the two sinks' hint
        # copy genuinely differs (a folder vs. a server) not just in name.
        "subsonicMirrorTo": _("Mirror to Subsonic…"),
        "subsonicMirroring": _("Mirroring to Subsonic"),
        "subsonicMirrorOnHint": _("Kept in sync with your Subsonic/Navidrome mirror-target server. Click to stop mirroring."),
        "subsonicMirrorOffHint": _("Mirror this playlist to your Subsonic/Navidrome mirror-target server, kept in sync as your library grows."),
        "subsonicMirrorUrlUnset": _("No Subsonic mirror target configured — set one in Administration > Configuration."),
        "subsonicMirrorErrorUnreachable": _("Could not reach the Subsonic mirror target:"),
        "subsonicMirrorErrorWriteFailed": _("Failed to write the Subsonic mirror playlist:"),
        # #189 review: none of this playlist's locally-matched tracks were
        # found on the target -- the strong signal of a broken match or a
        # target pointed at the wrong library/account, not an ordinary
        # partial mirror (which stays silent, same as every other sink).
        # #189 review: no detail ever follows this one (see mirror_subsonic.py's
        # no_target_matches branch) -- a trailing colon with nothing after it
        # read as broken. Self-contained, colon-free, same shape as
        # mirrorFolderUnset above for the same reason.
        "subsonicMirrorErrorNoTargetMatches": _("None of this playlist's tracks were found on the Subsonic mirror target."),
        # #189: the third sink — a Jellyfin server as a write target. Same
        # shape as the Subsonic sink's own keys just above; no_target_matches
        # is self-contained/colon-free from the start here, learning from
        # the review that caught the Subsonic one appending a detail that
        # never actually follows it.
        "jellyfinMirrorTo": _("Mirror to Jellyfin…"),
        "jellyfinMirroring": _("Mirroring to Jellyfin"),
        "jellyfinMirrorOnHint": _("Kept in sync with your Jellyfin mirror-target server. Click to stop mirroring."),
        "jellyfinMirrorOffHint": _("Mirror this playlist to your Jellyfin mirror-target server, kept in sync as your library grows."),
        "jellyfinMirrorUrlUnset": _("No Jellyfin mirror target configured — set one in Administration > Configuration."),
        "jellyfinMirrorErrorUnreachable": _("Could not reach the Jellyfin mirror target:"),
        "jellyfinMirrorErrorWriteFailed": _("Failed to write the Jellyfin mirror playlist:"),
        "jellyfinMirrorErrorNoTargetMatches": _("None of this playlist's tracks were found on the Jellyfin mirror target."),
        # #189: the fourth and (per the RFC) final sink — an Emby server as
        # a write target. Same shape as the Jellyfin sink's own keys above.
        "embyMirrorTo": _("Mirror to Emby…"),
        "embyMirroring": _("Mirroring to Emby"),
        "embyMirrorOnHint": _("Kept in sync with your Emby mirror-target server. Click to stop mirroring."),
        "embyMirrorOffHint": _("Mirror this playlist to your Emby mirror-target server, kept in sync as your library grows."),
        "embyMirrorUrlUnset": _("No Emby mirror target configured — set one in Administration > Configuration."),
        "embyMirrorErrorUnreachable": _("Could not reach the Emby mirror target:"),
        "embyMirrorErrorWriteFailed": _("Failed to write the Emby mirror playlist:"),
        "embyMirrorErrorNoTargetMatches": _("None of this playlist's tracks were found on the Emby mirror target."),
        "maMirrorTo": _("Mirror to Music Assistant…"),
        "maMirroring": _("Mirroring to Music Assistant"),
        "maMirrorOnHint": _("Kept in sync with a copy in Music Assistant. Click to stop mirroring."),
        "maMirrorOffHint": _("Mirror this playlist to Music Assistant, kept in sync as your library grows."),
        "maMirrorUrlUnset": _("Music Assistant isn't connected: set it up in Administration > Configuration."),
        "maMirrorErrorUnreachable": _("Could not reach Music Assistant:"),
        "maMirrorErrorWriteFailed": _("Failed to write the Music Assistant copy:"),
        "maMirrorErrorNoTargetMatches": _("None of this playlist's tracks were found in Music Assistant's library."),
        "maMirrorErrorReadback": _("Music Assistant's copy doesn't hold what was written:"),
        "plexMirrorTo": _("Mirror to Plex…"),
        "plexMirroring": _("Mirroring to Plex"),
        "plexMirrorOnHint": _("Kept in sync with your Plex mirror-target server. Click to stop mirroring."),
        "plexMirrorOffHint": _("Mirror this playlist to your Plex mirror-target server, kept in sync as your library grows."),
        "plexMirrorUrlUnset": _("No Plex mirror target configured — set one in Administration > Configuration."),
        "plexMirrorErrorUnreachable": _("Could not reach the Plex mirror target:"),
        "plexMirrorErrorWriteFailed": _("Failed to write the Plex mirror playlist:"),
        "plexMirrorErrorNoTargetMatches": _("None of this playlist's tracks were found on the Plex mirror target."),
        "plexMirrorErrorReadback": _("Plex's copy doesn't hold what was written:"),
        # "Request missing albums" — not a mirror sink (Lidarr isn't a
        # copy destination, it's asked to acquire what's missing), and,
        # unlike the sinks, not shown at all until Lidarr is configured.
        # Shown, an off button is disabled only when there is nothing to
        # ask for, with a hint for each case: no gaps at all, or gaps with
        # no album, neither from their provider nor found in MusicBrainz.
        # The not-configured hint names a run's unset_target error.
        "lidarrRequestTo": _("Request missing albums…"),
        "lidarrRequesting": _("Requesting missing albums"),
        "lidarrRequestOnHint": _("Missing albums are requested from Lidarr as new gaps appear. Click to stop."),
        "lidarrRequestOffHint": _("Request this playlist's missing albums from your configured Lidarr instance, monitor-only — Lidarr's own scheduled search finds them later."),
        "lidarrRequestNotConfiguredHint": _("Lidarr requests aren't set up yet — ask an admin to configure a Lidarr connection in Administration."),
        "lidarrRequestNoAlbumDataHint": _("This playlist's source doesn't provide album information for unresolved tracks, so nothing here can be requested."),
        # A playlist with no gaps at all is not short of album data.
        "lidarrRequestNothingMissingHint": _("Nothing is missing from this playlist, so there is nothing to request."),
        "lidarrRequestLastRun": _("Requested {n} albums, last run {when}"),
        # lidarr_request_last_error_code is one of 'unset_target' (reuses
        # lidarrRequestNotConfiguredHint above, same as mirrorFolderUnset's
        # own reuse), 'partial', or 'failed' — a run touches potentially
        # several albums, so this is deliberately just "at least one album
        # in the last run" granularity (#494's own settled decision:
        # minimal feedback by design, not a per-album error list), with the
        # untranslated reason code from lidarr_requests.py appended after a
        # translated prefix, same shape as the mirror sinks' own error keys.
        "lidarrRequestErrorPartial": _("An album was added to Lidarr but couldn't be put on the wanted list:"),
        "lidarrRequestErrorFailed": _("Failed to request an album from Lidarr:"),
        # #297 step 2: background-jobs admin panel.
        "jobsRunning": _("Running"),
        "jobsQueued": _("Queued"),
        "jobsFailed": _("Failed"),
        "jobsDone": _("Completed"),
        "jobsNone": _("No background jobs yet."),
        "jobsAllClear": _("Nothing running or waiting."),
        "jobsRetry": _("Retry"),
        "jobsCancel": _("Cancel"),
        "jobsCancelling": _("Cancelling…"),
        "jobsCancelled": _("Cancelled"),
        "jobsRetrying": _("Retrying…"),
        "jobsAttempts": _("attempt {n} of {max}"),
        "jobsStarted": _("started {when}"),
        "jobsFinished": _("finished {when}"),
        # #297 step 3: live progress for a running job.
        "jobsProgress": _("{done} of {total} files"),
        # #360: fingerprint_backfill processes tracks, not files — its rows
        # already have a fingerprint by the time this job sees them, it's only
        # doing the AcoustID/MusicBrainz lookup, so "files" would describe
        # work this job doesn't do.
        "jobsProgressTracks": _("{done} of {total} tracks"),
        "jobsProgressAlbums": _("{done} of {total} albums"),
        "jobsPlaylistsSynced": {"one": _("{n} playlist synced"), "other": _("{n} playlists synced")},
        "jobsCounting": _("counting files…"),
        # #357: the Library tab's scan progressbar needs an accessible name
        # (WCAG's "Name From: author" requirement for role=progressbar) —
        # unlike the admin panel, where each job row already has a visible
        # type label to point aria-labelledby at, there's only ever one scan
        # here, so a fixed translated label is simpler than inventing one.
        "libraryScanProgressLabel": _("Library scan progress"),
        # #332: the delete-user dialog. Counts are pluralised the same way
        # relativeTime's are (an {one,other} pair resolved by Intl.PluralRules),
        # because "1 devices" in a blocking message reads as a bug in itself.
        "deleteUserTitle": _("Delete \u201c{username}\u201d?"),
        "deleteUserBlocked": _("This account can't be deleted yet — it still owns:"),
        "deleteUserBlockedHint": _(
            "Delete those first. Ownership can't be transferred from here: delegation lets "
            "someone else manage a device without owning it, and a playlist's owner only "
            "changes by re-syncing it."),
        "deleteUserConfirm": _("This permanently deletes the account."),
        "deleteUserAlsoRemoved": _(
            "Their Last.fm, ListenBrainz, Tidal and Spotify connections go with it, along "
            "with any delegations and pending device enrolments."),
        "deleteAccount": _("Delete account"),
        "deleting": _("Deleting…"),
        "blockerDevices": {"one": _("{n} device"), "other": _("{n} devices")},
        "blockerSelections": {"one": _("{n} selection"), "other": _("{n} selections")},
        "blockerPlaylists": {"one": _("{n} playlist"), "other": _("{n} playlists")},
        # #333: last_error shown against a job that is NOT failed — it describes a
        # previous attempt, not the current state.
        "jobsPreviousError": _("previous attempt: {error}"),
        "jobsCheckedCount": _("{n} checked"),
        "jobsQueuedAt": _("queued {when}"),
        "jobsError": _("Couldn't load or update — try again."),
        "shared": _("Shared"),
        "private": _("Private"),
        "playlistSharedHint": _("Visible to everyone in the household. Click to make it private."),
        "playlistPrivateHint": _("Only visible to you (and the admin). Click to share it."),
        "inferredOriginHint": _("Its tracks closely match a {provider} playlist — likely imported into Roon from there."),
        "sharedByOwner": _("shared by {name}"),
        "unknownProvider": _("Unknown source"),
        # A playlist made on one of the user's devices: the device's name.
        "fromDevice": _("From {name}"),
        "syncToDevice": _("Sync {item} to {device}"),
        "roonProfileForUser": _("Roon profile for {name}"),
        "save": _("Save"),
        "loading": _("Loading…"),
        "refresh": _("Refresh"),
        "roonProfileSuggested": _("suggested: {name}"),
        # #262: same per-user mapping idea, generalized to Jellyfin/Emby —
        # a single shared key rather than roonProfileSuggested's own,
        # since the hint text itself doesn't vary by provider.
        "jellyfinUserForUser": _("Jellyfin account for {name}"),
        "embyUserForUser": _("Emby account for {name}"),
        "userMappingSuggested": _("suggested: {name}"),
        "setPassword": _("Set password"),
        "changePassword": _("Change password"),
        "lastSeen": _("Seen {time}"),
        # #229: distinct from lastSeen — see sync_state.sync_status()'s own
        # comment for why last_seen_at alone can't be trusted for this.
        "syncStatusSynced": _("Synced {time}"),
        "syncStatusSyncing": {"one": _("Syncing… {n} track left"), "other": _("Syncing… {n} tracks left")},
        "syncStatusNeverSynced": _("Nothing synced yet"),
        # #309: announced once when the wizard hands the user over, since
        # the first scan now runs in the background instead of blocking.
        "firstScanStarted": _("Scanning your library now — browse away, it fills in as it goes."),
        "firstScanNotStarted": _("Setup saved, but the library scan didn't start. Use Rescan on the Library tab."),
        "syncCompleteToast": _("{name} finished syncing"),
        # #416: fan-out used to say nothing beyond the basket icon vanishing
        # — indistinguishable from "Clear". count/skipped come straight from
        # POST /api/basket/fan-out's own response.
        "basketFanOutToast": {
            "one": _("{n} item queued to {devices}"),
            "other": _("{n} items queued to {devices}"),
        },
        # #416 part 2: a basket item whose type predates #352's validation
        # (or a hand-edited DB) is skipped during fan-out and still cleared
        # with the rest -- the backend already reported this in `skipped`,
        # the frontend just used to throw it away.
        "basketFanOutSkipped": _("{n} skipped (unrecognized type)"),
        # #416 part 3: the single-item buttons need a dynamic label (JS
        # t(), not the static Jinja {{ _() }} the rest of the template
        # uses) since which one shows depends on reactive
        # $store.basket.has() state, not the page's initial render.
        #
        # #501: "Select" and "Add to basket" collapse into one button
        # everywhere — clicking it always opens the device picker now.
        # inBasket ("In basket") is still exactly the right word for the
        # staged state (it's still literally the basket). syncTo below is
        # the not-yet-staged label on a row; addToBasket is the album dialog's.
        "inBasket": _("In basket"),
        "addToBasket": _("Add to basket"),
        "syncTo": _("Sync to…"),
        # The Library artist-header button keeps its own more specific
        # not-yet-staged label (this syncs the WHOLE artist, not one
        # album) instead of the generic syncTo above; inBasket covers its
        # staged state the same way.
        "syncEntireArtist": _("Sync entire artist"),
        # #501: the device picker's two actions, replacing the old single
        # Confirm. addAndSendNowWithCount's {n} is what THIS click would
        # actually send — items already staged for the checked device(s)
        # plus the new one(s) — shown only once that count is more than
        # just the new item(s), so a quick single add into an empty
        # section still reads as the plain, unqualified action. This is
        # the fix for the edge case the issue itself calls out: a device's
        # section already holding several staged items must not silently
        # ride along on a quick unrelated add without being visible first.
        "addAndKeepBrowsing": _("Add & keep browsing"),
        "addAndSendNow": _("Add & send now"),
        "addAndSendNowWithCount": {
            "one": _("Add & send now ({n} item)"),
            "other": _("Add & send now ({n} items)"),
        },
        "creating": _("Creating…"),
        # #233: repurposed from the old standalone "+ New device" button —
        # same action (createDevice()), now the submit button inside the
        # guided Add Device modal's direct-token branch, so the "+" no
        # longer fits.
        "newDevice": _("Create device"),
        "addDevice": _("+ Add device"),
        "generatePairingCode": _("Generate pairing code"),
        "enrollHintMobile": _("Scan the QR code shown next in the Trobar app, or type the code in by hand."),
        "enrollHintWatch": _("Garmin can't scan a QR code — open Garmin Connect Mobile's settings for this app and enter the code shown next."),
        "newLocalAccount": _("+ Local account"),
        "newDelegation": _("+ Delegate"),
        "usageNoLimit": _("{used} GB used"),
        "usageWithLimit": _("{used} / {max} GB"),
        "overLimitSuffix": _(" — over limit"),
        "realFreeSpace": _("Actual free space on device: {free} GB"),
        "limitExceedsCapacitySuffix": _(" — the configured limit exceeds physical capacity!"),
        "usedOfTotal": _("{used} / {total} GB"),
        "pickerSendSize": _("{gb} GB"),
        "pickerLegendUsed": _("Used"),
        "pickerLegendThisSend": _("This send"),
        "pickerLegendRoomLeft": _("Room left"),
        "pickerLegendDoesntFit": _("Doesn't fit"),
        "pickerUsedOfCap": _("{used} GB used of {cap} GB ({why})"),
        "pickerCapLimit": _("its limit"),
        "pickerCapFree": _("the device's free space"),
        "pickerCapFreeReserve": _("the device's free space, keeping {reserve} GB free"),
        "pickerThisSend": _("this send {gb} GB"),
        "pickerThisSendLeft": _("this send {gb} GB · {left} GB left"),
        "pickerThisSendNeeds": _("this send needs {gb} GB"),
        "pickerOverLimit": _("Over by {gb} GB (its limit). You can still send it; the limit is what the Devices tab warns about, and nothing refuses it."),
        "pickerDoesntFitFree": _("Doesn't fit: needs {gb} GB more than the device's free space. Free some space on the device or send less."),
        "pickerDoesntFitFreeReserve": _("Doesn't fit: needs {gb} GB more than the device can take while keeping {reserve} GB free. Free some space on the device or send less."),
        "never": _("never"),
        "justNow": _("just now"),
        "minutesAgo": {"one": _("{n} minute ago"), "other": _("{n} minutes ago")},
        "hoursAgo": {"one": _("{n} hour ago"), "other": _("{n} hours ago")},
        "yesterday": _("yesterday"),
        "daysAgo": {"one": _("{n} day ago"), "other": _("{n} days ago")},
        "weeksAgo": {"one": _("{n} week ago"), "other": _("{n} weeks ago")},
        "monthsAgo": {"one": _("{n} month ago"), "other": _("{n} months ago")},
        "yearsAgo": {"one": _("{n} year ago"), "other": _("{n} years ago")},
        "historyDownSome": {"one": _("{sources} didn't answer, so its most-played albums are missing below."),
                            "other": _("{sources} didn't answer, so their most-played albums are missing below.")},
        "historyDownAll": _("{sources} didn't answer"),
        "mostPlayedDown": _("{sources} didn't answer, so the chart can't be drawn right now."),
        "period_7day": _("Last 7 days"),
        "period_1month": _("Last month"),
        "period_3month": _("Last 3 months"),
        "period_6month": _("Last 6 months"),
        "period_12month": _("Last year"),
        "period_overall": _("All time"),
        "scanResultText": _("Last scan: {added} added, {updated} updated, {removed} removed ({unchanged} unchanged)."),
        "scanSkippedDirs": {"one": _("{n} folder couldn't be read; its tracks were kept."),
                            "other": _("{n} folders couldn't be read; their tracks were kept.")},
        "scanMoved": {"one": _("{n} file moved and kept its track."),
                      "other": _("{n} files moved and kept their tracks.")},
        "scanUnreadableFiles": {"one": _("{n} file couldn't be read; its track was kept."),
                                "other": _("{n} files couldn't be read; their tracks were kept.")},
        "scanVanishedFiles": {"one": _("{n} file disappeared during the scan."),
                              "other": _("{n} files disappeared during the scan.")},
        "scanFromFolder": {"one": _("{n} file had no usable tags and was named from its folder."),
                           "other": _("{n} files had no usable tags and were named from their folders.")},
        "confirmDeleteAdminUser": _('Permanently delete the account "{username}"? Fails if this account still owns devices, selections, or playlists — delete or reassign those first.'),
        "resetPassword": _("Reset password"),
        "resetPasswordFor": _('Reset password for "{username}"'),
        "genericFailure": _("Failed ({error})"),
        # The messages of api() and showApiError().
        "networkError": _("Can't reach the server. Check the connection and try again."),
        "requestFailedStatus": _("Something went wrong (HTTP {status})."),
        "unexpectedResponse": _("The server sent an unexpected response."),
        "confirmRevokeDelegation": _("{grantee} will no longer be able to manage {target}'s devices. Continue?"),
        "confirmUnpinDevice": _('Remove "{name}" from your list? You will no longer manage it until you add it again.'),
        "confirmClearBasket": {"one": _("Clear the basket? Its {n} item is removed; nothing on your devices changes."),
                               "other": _("Clear the basket? Its {n} items are removed; nothing on your devices changes.")},
        "confirmRemoveAvatar": _("Remove your profile picture?"),
        "confirmAutofitOff": _('Turn off auto-fill for "{name}"? The albums it picked are removed from the device at its next sync, unless another selection includes them.'),
        "confirmAutofitOffKept": _('Turn off auto-fill for "{name}"? Its albums stay on the device while Keep device content is on, and turning auto-fill back on picks them afresh.'),
        "confirmKeepContentOff": _('Let the server manage "{name}" again? Tracks on it that no selection includes are removed at its next sync.'),
        "confirmKeepContentOffCount": {
            "one": _('Let the server manage "{name}" again? This will remove {n} track from it at its next sync: no selection includes it.'),
            "other": _('Let the server manage "{name}" again? This will remove {n} tracks from it at its next sync: no selection includes them.')},
        "confirmCancelJob": _("Cancel this {type} job?"),
        "confirmStopJob": _("Stop this running {type} job? It stops at its next safe point."),
        "confirmRegenerateToken": _('Get a config file for "{name}", for a desktop app too old to read pairing codes? A new token is issued: the current one stops working immediately, and whatever is paired with it must be paired again.'),
        "addMobileDevice": _("Add a mobile device"),
        "enrollHelp": _("In the Trobar app, scan this QR or enter the code below. It works once and expires after 20 minutes."),
        "repairDeviceTitle": _('Re-pair "{name}"'),
        "repairDeviceHelp": _("In the Trobar app, scan this QR or enter the code below. The app reconnects to this device and keeps its music, settings and history. It works once and expires after 20 minutes."),
        "pairDeviceTitle": _('Pair "{name}"'),
        # The desktop app: no QR scanner, so the dialog also shows the URL.
        "pairDesktopHelp": _("In the Trobar desktop app, open the card or folder and enter this server's URL and the code below. The device keeps its name, music and settings. The code works once and expires after 20 minutes."),
        "confirmRepairDevice": _('Re-pair "{name}" with a fresh app install? The app keeps this device\'s music, settings and history. Whatever is currently paired stops working and must be re-paired too.'),
        "repairFailed": _("Could not create a re-pairing code ({error})"),
        "confirmDeleteDevice": _('Permanently delete "{name}"? Its assigned selections will be unassigned (not deleted) and its token will stop working immediately.'),
        "confirmTranscodeChange": _('Change the format for "{name}"? Every synced track gets a new file name, so the next sync re-downloads everything and removes the old files.'),
        "transferTitle": _("This device replaces…"),
        "transferHelp": _("Pick the old device this one is taking over from. Its selections and settings move over, and the old device is deleted. By default its synced tracks are re-downloaded fresh — check below if this device already has them."),
        "transferButton": _("Transfer"),
        "transferAssumePresentLabel": _("This device already has these files (e.g. a cloned card)"),
        "transferTypeMismatchWarning": _("Different device type — the inherited storage limit and auto-fit settings may not make sense here."),
        "confirmTransferDevice": _('"{new}" will take over everything "{old}" currently syncs (tracks, selections, and settings), and "{old}" will be permanently deleted. Continue?'),
        "apiTokenMeta": _("Created {created} — last used {used}"),
        "confirmRevokeApiToken": _('Revoke "{name}"? Anything using this token will stop working immediately.'),
        "saveFailedCheckHostPort": _("Save failed ({error}) — check the host/port."),
        "saveFailedRetry": _("Save failed ({error}) — try again."),
        "scanRefreshFailed": _("Refresh after scan failed ({error}) — reload the page."),
        "createFailedRetry": _("Creation failed ({error}) — try again."),
        "addedTime": _("Added {time}"),
        "listenedRecently": _("Recently played"),
        "playCount": {"one": _("{n} play"), "other": _("{n} plays")},
        "playlistFallback": _("Playlist #{id}"),
        "autofitSummary": _("{albums} albums · {gb} GB of most-played"),
        "autofitNoLastfm": _("Set up Last.fm on the owner's profile to rank most-played"),
        "autofitBudgetFull": _("Manual selections already fill the storage limit"),
        "autofitEmpty": _("No matching albums found in the library yet"),
        "autofitFillPreview": _("≈ {gb} GB · ≈ {tracks} tracks (estimate)"),
        "autofitFillPercentLabel": _("Fill percentage for {name}"),
        "healthUnmatched": _("Playlist tracks with no local match"),
        "healthUnknown": _("Tracks tagged Unknown Artist/Album"),
        "healthDuplicates": _("Possible duplicate tracks"),
        # #364: population A (fingerprint pass gave up) is a normal problem
        # category — genuinely actionable, so it reads the same as the three
        # above. Population B is deliberately NOT here: it's rendered as its
        # own informational block below, not a chart/pill category, so it
        # can't be mistaken for a problem when it's often just an unindexed
        # recording.
        "healthFingerprintFailed": _("Tracks the fingerprint pass gave up on"),
        "healthUnidentified": _("Not found in AcoustID/MusicBrainz"),
        "healthUnidentifiedHint": _(
            "Fingerprinted successfully, but no match in either database — often just "
            "means the recording isn't indexed there (live recordings, bootlegs, "
            "self-released or local music, much classical). Not necessarily a problem."),
        # #365: DATA_DIR-on-a-network-filesystem alert. Composed from the raw
        # filesystem type (not the console warning text, which is untranslated
        # and styled for stdout) so it goes through the same catalog as
        # everything else in this panel.
        "healthNetworkDataDir": _(
            "DATA_DIR is on a network filesystem ({type}). SQLite needs working file "
            "locking, which network shares don't reliably provide — this can corrupt "
            "your database and lose your selections, device pairings and playlists. "
            "Move DATA_DIR to local disk; a network-mounted music library is fine, "
            "Trobar only reads it."),
        "healthLastScanned": _("Library last scanned {time}"),
        # #389: distinct-raw-peer-count alert, same "compose the translated
        # message from a raw value" pattern as healthNetworkDataDir above.
        "healthExposureWarning": _(
            "This instance has been reached directly by {count} different addresses "
            "in the last week while TROBAR_TRUSTED_PROXY is left at its default "
            "(\"*\"). If this container's port is only reachable through your "
            "reverse proxy, this just means the proxy's own address changed — "
            "otherwise, the per-IP brute-force rate limiter may not be protecting "
            "what you think it is, since anyone who can reach this port can claim "
            "to be any address. See Networking & Reverse Proxy in the docs."),
        # #389 (review feedback): the always-shown neutral counterpart —
        # otherwise a signal that only ever speaks up past its threshold
        # is indistinguishable from one that's silently never seeing
        # anything, on a deployment where it can't work at all.
        "healthExposureCount": {
            "one": _("{n} distinct address has reached this instance directly in the last week"),
            "other": _("{n} distinct addresses have reached this instance directly in the last week"),
        },
        # #362: "make the next scheduled run visible" was explicit in the
        # issue — an enabled schedule nobody can see is the same invisible-
        # background-mechanism problem #297 set out to fix.
        "nextScheduledScan": _("Next scheduled scan: {when}"),
        "nextScheduledPlaylistSync": _("Next scheduled playlist sync: {when}"),
        "showLicense": _("Show license"),
        "hideLicense": _("Hide license"),
        "showNotices": _("Show third-party notices"),
        "hideNotices": _("Hide third-party notices"),
        "updateChecking": _("Checking…"),
        "updateAvailable": _("Update available: {tag} — see the releases page."),
        "libraryNoMatch": _("No artist or album matches “{q}”"),
        "updateUpToDate": _("You are running the latest release ({tag})."),
        "updateCheckFailed": _("Could not reach GitHub ({error}) — try again later."),
        "aboutLoadFailed": _("Could not load the text ({error})."),
        # #508: the "duel the bard" Easter egg — kept to two strings (win/
        # lose) with a shared {winner, winner_count, loser, loser_count}
        # param shape rather than separately templating what was picked,
        # per the issue's own "keep the string count deliberately small".
        "quizCorrect": _("Correct — {winner} has {winner_count} albums, {loser} has {loser_count} albums."),
        "quizIncorrect": _("Not quite — {winner} actually has {winner_count} albums, {loser} has {loser_count} albums."),
    }


@app.route("/")
def index():
    with db.connection() as conn:
        # follow-up: bumped whenever the artist-image cache is
        # cleared (key/provider change) — rides into artistImageUrl() as a
        # cache-busting param so a plain page reload shows the re-fetched
        # images instead of the browser's day-old copies.
        epoch = db.get_config(conn, "artist_images_epoch") or "0"
        # The same for album covers: bumped when a scan drops cached covers.
        covers_epoch = db.get_config(conn, "covers_epoch") or "0"
    return render_template("index.html", js_i18n=_build_js_i18n(),
                           artist_images_epoch=epoch, covers_epoch=covers_epoch,
                           thanks_card_syncs=user_profile.THANKS_CARD_SYNCS,
                           show_logout=_app_authenticates())


# --- About -------------------------------------------------------
# VERSION/LICENSE/THIRD_PARTY_NOTICES.md sit next to main.py in the image
# (Dockerfile copies them); in a dev checkout they're one level up, at the
# repo root. Update checks are client-side and user-initiated only (the
# browser calls the GitHub releases API on button press) — the server never
# phones home.


@app.route("/api/about")
def api_about():
    version_file = _meta_file("VERSION")
    version = version_file.read_text(encoding="utf-8").strip() if version_file else "unknown"
    return jsonify({"version": version})


@app.route("/about/<doc>")
def about_doc(doc):
    names = {"license": "LICENSE", "notices": "THIRD_PARTY_NOTICES.md"}
    if doc not in names:
        abort(404)
    path = _meta_file(names[doc])
    if path is None:
        abort(404)
    return path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain; charset=utf-8"}


def _safe_referrer() -> str:
    r"""The referrer, but only if it points back into this app — otherwise the
    home page. Prevents an open redirect from a crafted Referer.

    Two conditions, and the second is the one a host check alone does not
    give you: the referrer's host must be ours (or absent), AND the path must
    be a single absolute path rather than something the browser re-reads as
    an authority. `//evil.example` and `/\evil.example` both satisfy "starts
    with a slash" while being resolved as protocol-relative URLs to another
    origin — so the host we validated is not the host the browser arrives at,
    and validating it proved nothing. Every rejected shape is named in
    SafeReferrerTests rather than left implied by this paragraph."""
    ref = request.referrer
    if ref:
        u = urlsplit(ref)
        if not u.netloc or u.netloc == request.host:
            path = u.path or "/"
            if path.startswith("/") and not path.startswith(("//", "/\\")):
                return path + (("?" + u.query) if u.query else "")
    return url_for("index")


@app.route("/set-language/<lang>")
def set_language(lang):
    if lang not in ("en", "fr"):
        abort(404)
    resp = redirect(_safe_referrer())
    resp.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


# The library reads below are shared by the session routes and the App API
# routes (/api/app/library/*): one implementation, two front doors that differ
# only in who authenticates. None of these queries is per-user -- the library
# is the household's -- except similar-artists, which reads the acting user's
# Last.fm key.


@app.route("/api/dashboard/catalog")
def api_dashboard_catalog():
    """The widget catalog the web client renders from -- see
    DASHBOARD_WIDGET_CATALOG for why it is served rather than embedded."""
    return jsonify(_widget_catalog())


@app.route("/api/lastfm/status")
def api_lastfm_status():
    """Header status-dot backing — separate from /api/suggestions since that
    endpoint can legitimately return [] for reasons that have nothing to do
    with whether the Last.fm connection itself is healthy (nothing currently
    qualifies vs. the account/key actually being broken)."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        user = conn.execute(
            "SELECT lastfm_username, lastfm_api_key FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user["lastfm_username"]:
            return jsonify({"configured": False, "ok": False})
        api_key = user["lastfm_api_key"] or db.get_config(conn, "lastfm_api_key_default") or ""
        api_base = db.get_config(conn, "lastfm_api_base") or ""
        return jsonify({"configured": True, "ok": lastfm.check_connection(user["lastfm_username"], api_key, api_base)})


@app.route("/api/maloja/status")
def api_maloja_status():
    """Maloja counterpart of /api/lastfm/status, for the header badge:
    configured when the Profile has a URL, ok when a Maloja answers there."""
    with db.connection() as conn:
        user = conn.execute(
            "SELECT maloja_url FROM users WHERE id = ?", (get_current_user_id(conn),)
        ).fetchone()
    if not user["maloja_url"]:
        return jsonify({"configured": False, "ok": False})
    return jsonify({"configured": True, "ok": maloja.check_connection(user["maloja_url"])})


@app.route("/api/listenbrainz/status")
def api_listenbrainz_status():
    """ListenBrainz counterpart of /api/lastfm/status — same
    configured/ok split for the header badge, no API key involved."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        user = conn.execute(
            "SELECT listenbrainz_username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user["listenbrainz_username"]:
            return jsonify({"configured": False, "ok": False})
        api_base = db.get_config(conn, "listenbrainz_api_base") or ""
        return jsonify({"configured": True, "ok": listenbrainz.check_connection(user["listenbrainz_username"], api_base)})


@app.route("/api/suggestions")
def api_suggestions():
    """Merged suggestion feed (#30): recently-added-to-library
    (local scan, no Last.fm needed) first, then Last.fm top-played and
    recently-played if the user has a Last.fm username configured. Deduped
    across sources by (artist, album) — priority order above decides which
    source's copy (and thus which provider's art) wins for an album that
    shows up in more than one."""
    period = _validated_period(request.args.get("period", "6month"))
    with db.connection() as conn:
        # A bare list by default; with_sources=1 (the web page) adds each
        # history source's state, as the App API always does. fresh=1 (the
        # page's Refresh and Retry) asks the services again.
        if request.args.get("fresh") == "1":
            history_cache.clear()
        items, sources = _suggestions_for(conn, get_current_user_id(conn), period)
        if request.args.get("with_sources") == "1":
            return jsonify({"items": items, "sources": sources})
        return jsonify(items)


@app.route("/api/suggestions/most-played")
def api_most_played():
    """#267: a ranked, un-shuffled most-played chart — the user's actual
    Last.fm/ListenBrainz listening, not filtered to the local library or
    what's not-yet-synced like /api/suggestions is (a different intent:
    "what do I listen to" vs. "what should I sync"). Same per-user
    config as /api/suggestions; [] if neither service is set up."""
    period = _validated_period(request.args.get("period", "6month"))
    limit = _most_played_limit(request.args.get("limit"))
    with db.connection() as conn:
        if request.args.get("fresh") == "1":
            history_cache.clear()
        items, sources = _most_played_for(conn, get_current_user_id(conn), period, limit)
        if request.args.get("with_sources") == "1":
            return jsonify({"items": items, "sources": sources})
        return jsonify(items)


# ---------------------------------------------------------------------------
# First-run setup wizard — AUTH_MODE=local only, see
# _require_setup_wizard above for why Authentik mode doesn't need this.
# ---------------------------------------------------------------------------


def _network_data_dir_warning() -> str | None:
    """#299: the startup warning text when DATA_DIR is on a network filesystem,
    or None when it isn't. Returns the string rather than printing it so the
    wording and the trigger are both testable — the MUSIC_ROOT warning above is
    inline in __main__ and consequently isn't.

    DATA_DIR on NFS/SMB can CORRUPT the database: SQLite needs working POSIX
    advisory locking, those shares don't reliably provide it (SQLite's own
    corruption guide, §2.1), and WAL — which get_conn enables — makes it worse
    rather than better there. Worth warning about at startup rather than leaving
    to the docs because the failure is late, silent and unrecoverable: it
    surfaces as corruption under concurrent access long after the mistake, and
    takes selections, device pairings and playlists with it.

    Warns, never refuses — same posture as _music_root_is_readonly's warning, so
    a misdetected filesystem can't stop someone's server from booting. The
    message names the detected type (credibility), says what breaks (stakes),
    and answers the underlying want rather than only forbidding something: people
    put DATA_DIR on a NAS because they want the data backed up, so it points at
    backing up TO the NAS instead."""
    fs_type = db.data_dir_network_fs()
    if not fs_type:
        return None
    return (
        f"DATA_DIR ({db.DATA_DIR}) is on a network filesystem "
        f"({fs_type}). SQLite needs working file locking, which network shares don't "
        "reliably provide — this CAN CORRUPT your database and lose your selections, "
        "device pairings and playlists. Move DATA_DIR to local disk on the machine "
        "running the server. A network-mounted MUSIC_ROOT is fine (Trobar only reads "
        "it); if you want this data on your NAS, back it up there rather than running "
        "it from there. See docs/getting-started/installation.md."
    )


# App API: library browsing. Same reads as the session routes, behind the
# device token acting as the owner. Authentication comes first in every one
# of these, before any early return: the prefix is exempt from the login
# gate, so a route that answers anything at all before authenticating has
# answered an anonymous caller.


# App API: the staging loop -- basket, its fan-out into selections, the
# device list the picker offers, and per-device usage. Shared functions with
# the session routes; the acting user is the device's owner throughout,
# including for _require_device_access, so a phone can stage to and send to
# exactly the devices its owner could from a browser and no others.


# App API: the home dashboard -- the widget catalog, the acting user's
# widget preferences (read, and the same narrow PATCH the browser has), and
# the data behind each widget. The devices and library-artists widgets read
# the routes above. The two feeds that need a listening history say so
# explicitly (`configured`) rather than answering with an empty list that
# could equally mean "nothing to suggest".


# --- /api/app/playlists*: the playlist section for the phone (#108) -------
# Everything the browser's playlist section does except mirroring, behind
# the device token, acting as the device's owner. Every handler below is the
# browser route's own body (the shared _functions above), so #28 privacy,
# #81 attribution and the owner-or-admin checks exist once. No route here
# turns a mirror on or off, and the list leaves the mirror fields out;
# deleting a playlist deletes its mirrors, as in the browser. Level 3 adds
# authoring a playlist made in Trobar: create, rename, set or append its
# entries, delete.


@app.cli.command("create-admin")
@click.argument("username")
@click.option("--password", default=None, help="Password (prompted securely if omitted).")
def create_admin_command(username: str, password: str | None) -> None:
    """Create an admin account, or grant an existing user admin + reset their
    password. Headless equivalent of the /login bootstrap (the very first
    AUTH_MODE=local login, when zero users exist yet, creates the admin) —
    for recovering a lost local password, or provisioning/scripting without
    a browser, without reaching for a raw DB edit. Note: in oidc/forward
    mode, admin status is instead granted automatically on login to whoever
    matches ADMIN_USERNAME (see _provision_user) — this command only sets
    is_admin + a *local* password, which is meaningful there purely as the
    break-glass fallback (api_account_password), not as how admin is
    normally granted in those modes. Run inside the container:
    `flask --app main.py create-admin <username>`.
    """
    if not password:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < 8:
        raise click.ClickException("Password must be at least 8 characters")

    db.init_db()
    with db.connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, is_admin, password_hash) VALUES (?, 1, ?)",
                (username, generate_password_hash(password)),
            )
            click.echo(f"Created admin account {username!r}.")
        else:
            # No request here, so no current session to keep: all of them end.
            conn.execute(
                "UPDATE users SET is_admin = 1, password_hash = ?, "
                "session_generation = session_generation + 1 WHERE id = ?",
                (generate_password_hash(password), row["id"]),
            )
            click.echo(f"Updated {username!r}: now admin, password reset.")
        conn.commit()


# Startup security warnings. They were print()s, shown whatever LOG_LEVEL
# said, and must stay that way: LOG_LEVEL=ERROR quiets Trobar's own logging,
# not the warning that a directly-exposed port lets anyone spoof the forward-
# auth header. So this logger is deliberately NOT in _APP_LOGGERS: it keeps
# root's fixed WARNING level and root's handler.
_STARTUP_LOG = logging.getLogger("trobar.startup")


def _log_startup_warnings() -> None:
    # Trobar only ever reads the music library — it has no code path
    # that writes there. Mount it read-only so a bug or a compromise can't
    # modify or delete the user's files. Warn (don't refuse — a false positive
    # shouldn't take the app down) if the effective MUSIC_ROOT isn't a
    # read-only mount. Also surfaced to the admin UI via /api/admin/config.
    if _music_root_is_readonly() is False:
        _STARTUP_LOG.warning(
            "MUSIC_ROOT (%s) is not mounted read-only. "
            "Trobar never writes to your music library; mount it read-only (append ':ro' to "
            "the volume in docker-compose, e.g. '/path/to/music:/music:ro') so a bug or "
            "compromise can't alter or delete it. See SECURITY.md.", db.get_music_root()
        )

    data_dir_warning = _network_data_dir_warning()
    if data_dir_warning:
        _STARTUP_LOG.warning("%s", data_dir_warning)

    # forward mode trusts identity headers. Without a proxy-injected
    # shared secret to confirm the request actually came through the trusted
    # proxy, a directly-exposed port lets anyone spoof X-authentik-username —
    # warn loudly so a self-hoster can't miss it.
    for warning in _forward_auth_warnings():
        _STARTUP_LOG.warning("%s", warning)

    if settings.EMERGENCY_PORT and settings.AUTH_MODE != "forward":
        _STARTUP_LOG.warning(
            "EMERGENCY_PORT=%s set but AUTH_MODE=%r — "
            "ignoring it. It only exists to bypass a ForwardAuth proxy gate (forward mode); "
            "in local/oidc mode there's no such gate in front of Flask to bypass.",
            settings.EMERGENCY_PORT, settings.AUTH_MODE
        )


# Blueprints (#253): routes that have moved out of this file.
app.register_blueprint(device_api.bp)
app.register_blueprint(devices.bp)
app.register_blueprint(library.bp)
app.register_blueprint(ops.bp)
app.register_blueprint(user_profile.bp)
app.register_blueprint(admin.bp)
app.register_blueprint(app_api.bp)
app.register_blueprint(playlists.bp)
app.register_blueprint(selections.bp)
app.register_blueprint(integrations.bp)
app.register_blueprint(auth.bp)
app.register_blueprint(tidal_share_routes.bp)


def _on_sigterm(_signum, _frame) -> NoReturn:
    """`docker stop` sends SIGTERM to PID 1, which is this process. Without a
    handler the kernel ignores it there, so every stop waited out docker's
    timeout and ended in SIGKILL. Running jobs are asked to stop at their next
    safe point and queued again (jobs.shutdown); then the server exits."""
    if not jobs.shutdown(timeout=5.0):
        logging.getLogger("main").warning(
            "a background job was still running at shutdown; it will be queued again at the next start")
    raise SystemExit(0)


if __name__ == "__main__":
    db.init_db()
    # Only the active provider's ensure_started() runs — avoids an unwanted
    # Roon pairing attempt against a real Roon Core (or any provider
    # connection attempt at all) for a deployment actually configured for
    # the other one. This happened once before by accident when this call
    # was unconditional (a container restart alone triggered a pairing
    # request against production Roon).
    with db.connection() as _startup_conn:
        _active_provider(_startup_conn).ensure_started()

    # MUSIC_ROOT, DATA_DIR, forward auth, EMERGENCY_PORT: shown whatever
    # LOG_LEVEL says (see _STARTUP_LOG).
    _log_startup_warnings()

    if settings.EMERGENCY_PORT and settings.AUTH_MODE == "forward":
        # Same Flask app, second internal listener — see EMERGENCY_PORT /
        # _is_emergency_request() above for why this exists and how it's
        # kept safe. Runs in a background thread; the main thread still
        # blocks on the normal port below, same as before this existed.
        import threading
        from werkzeug.serving import make_server
        emergency_server = make_server("0.0.0.0", settings.EMERGENCY_PORT, app, threaded=True)
        threading.Thread(target=emergency_server.serve_forever, daemon=True).start()
        # A listener that bypasses the proxy gate is something the operator
        # must see, whatever LOG_LEVEL says (see _STARTUP_LOG).
        _STARTUP_LOG.warning(
            "emergency port %s listening (bypasses the proxy gate)", settings.EMERGENCY_PORT)

    # #297: start the single background job worker. Here, in __main__, and not
    # at import: the test suite imports this module (hundreds of tests) and the
    # `python3 -m scanner` CLI pulls it in transitively — in both cases a worker
    # would spin up against whatever db.DB_PATH happened to be set to and start
    # claiming jobs out from under them. Idempotent, so the TROBAR_DEV_SERVER
    # branch below reloading the app can't produce two.
    #
    # This also runs the boot-time reaper (see jobs.requeue_interrupted): work
    # that was mid-flight when the process last died gets requeued rather than
    # silently lost, which nothing did before this existed.
    if jobs.start_worker():
        logging.getLogger("main").info("background job worker started")
    signal.signal(signal.SIGTERM, _on_sigterm)

    if settings.DEV_SERVER:
        # Flask's Werkzeug dev server — LOCAL DEVELOPMENT ONLY (opt-in), for
        # its reloader/debugger. threaded=True so two devices syncing
        # concurrently aren't serialised behind one another's file downloads.
        app.run(host="0.0.0.0", port=5000, threaded=True)
    else:
        # #82: production serving via waitress instead of the Werkzeug dev
        # server (not built to be efficient/stable/secure, per its own docs).
        # Kept deliberately SINGLE-PROCESS, multithreaded: the scan lock
        # (scanner._SCAN_LOCK), transcode-concurrency condition
        # (transcode._cond), and per-IP rate-limit counters are all in-process
        # state that multiple worker processes would each hold separately —
        # breaking the "scan already running" 409, the transcode cap, and the
        # brute-force backoff. waitress is called in-process here (not a
        # separate entrypoint) so this whole __main__ bootstrap — including the
        # EMERGENCY_PORT listener above — still runs. `ident="trobar"` drops the
        # server version banner at the WSGI layer (#92). channel_timeout caps
        # slow-client connections; it must exceed the longest legitimate
        # response, i.e. a full-album MP3 transcode stream (#82's endpoint
        # note), hence a generous 1h rather than the 120s default.
        # trusted_proxy (#113): waitress has its OWN proxy-trust gate in front
        # of ProxyFix and defaults to clear_untrusted_proxy_headers=True — so
        # without this it STRIPS X-Forwarded-Proto/Host before ProxyFix (line
        # ~52) can read them, and url_for(_external=True) builds http:// URLs
        # behind the TLS proxy — breaking the OIDC/Tidal redirect_uri (must be
        # https:// to match what's registered at the IdP). The old app.run()
        # dev server had no such gate, so this regressed only on the #82
        # switch. See _trusted_proxy()'s own docstring for what this value
        # means for the brute-force rate limiter (#383/#382), and
        # docs/operations/networking.md for the operator-facing version.
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000, threads=8,
              ident="trobar", channel_timeout=3600,
              trusted_proxy=_trusted_proxy(),
              trusted_proxy_headers="x-forwarded-for x-forwarded-host x-forwarded-proto")
