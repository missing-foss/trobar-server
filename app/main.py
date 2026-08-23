#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trobar — Flask web app + device-facing sync API."""

import difflib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import click
from flask import Flask, jsonify, request, render_template, send_file, abort, Response, session, redirect, url_for
from flask_babel import Babel, gettext as _, get_locale
from flask_compress import Compress
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth

import artist_images
import covers
import db
import emby_client
import filesystem_client
import fingerprint
import jellyfin_client
import jobs
import lastfm
import library_quiz
import lidarr_client
import lidarr_requests
import listenbrainz
import lms_client
import mirror
import mirror_emby
import mirror_jellyfin
import mirror_subsonic
import playlist_sync
import plex_client
import provenance
import roon_client
import scanner
import subsonic_client
import suggestions
import sync_state
import spotify_client
import tidal_client
import transcode

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
    "db", "fingerprint", "jobs", "mirror", "mirror_emby", "mirror_jellyfin", "mirror_subsonic",
    "playlist_sync", "provenance", "scanner", "spotify_client", "tidal_client", "transcode",
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
jobs.register(scanner.JOB_TYPE, scanner.run_job, lane=jobs.LANE_LONG)
jobs.register(fingerprint.JOB_TYPE, fingerprint.run_job, lane=jobs.LANE_LONG)
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
        conn = db.get_conn()
        try:
            jobs.enqueue(conn, fingerprint.JOB_TYPE, dedupe_key=fingerprint.JOB_TYPE)
        finally:
            conn.close()
    return result


jobs.register(provenance.JOB_TYPE_LIBRARY_FINGERPRINTS,
              _library_fingerprints_then_lookup, lane=jobs.LANE_LONG)

# #362: scheduled rescanning, off by default (scan_interval_hours=0). Wired
# here rather than at scanner.py's import time for the same reason every
# other registration on this page is — main.py is where cross-module wiring
# lives, so a module can be imported (by tests, by the setup CLI) without
# silently acquiring background behaviour.
jobs.on_idle(scanner.maybe_schedule_rescan)


def _run_playlist_sync(payload, _report):
    """#297 step 3: playlist_sync.py's own job payload only ever carries
    `provider_id` (a JSON-serializable string) — the live provider MODULE has
    to be resolved back from it, and only main.py's `_PROVIDERS` dict can do
    that (defined further down this file; fine to reference here since this
    function isn't called until well after the whole module has imported).
    Wired here rather than inside playlist_sync.py for the same reason
    _library_fingerprints_then_lookup lives here and not in provenance.py —
    see jobs.register's docstring. No progress reporting: a sync has no
    single natural denominator the way a file walk does (multiple merge
    passes — primary provider, filesystem, Roon profiles, Tidal, Spotify —
    each a different size), so `report` is accepted and ignored, exactly the
    case jobs.register's docstring names as normal."""
    provider_id = payload["provider_id"]
    provider = _PROVIDERS.get(provider_id, roon_client)
    return playlist_sync.sync_playlists(provider, provider_id)


jobs.register(playlist_sync.JOB_TYPE, _run_playlist_sync)

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

# --- Brute-force backoff -----------------------------------------
# Tiny in-memory per-IP failure counter for the two credential-verifying paths
# (password /login and device Bearer auth). Strong tokens/PBKDF hashes already
# make guessing impractical; this just adds cheap per-IP backoff. In-process
# only (single worker today); resets on restart — fine for the threat model.
_RL_LOCK = threading.Lock()
_rl_failures: dict[str, deque] = defaultdict(deque)


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
_EXPOSURE_PEER_CAP = 200
_EXPOSURE_WARN_THRESHOLD = 5


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
    if _trusted_proxy() != "*" or AUTH_MODE == "forward":
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


def _exposure_peer_count() -> int:
    """Distinct raw peers seen in the last _EXPOSURE_WINDOW_S — the number
    the Library-health panel shows. A getter rather than reading
    _exposure_peers directly so callers don't need the lock or the window
    logic duplicated."""
    cutoff = time.time() - _EXPOSURE_WINDOW_S
    with _EXPOSURE_LOCK:
        return sum(1 for seen in _exposure_peers.values() if seen >= cutoff)


def _exposure_warning() -> int | None:
    """#389: Library-health signal for a deployment that looks directly
    exposed with TROBAR_TRUSTED_PROXY left at "*". Returns the distinct
    peer count (for the UI to compose its own translated message from,
    same pattern as _network_data_dir_warning/data_dir_network_fs), or
    None when there's nothing to say: trusted_proxy is pinned (the pin
    itself is the acknowledgement), forward mode (different boundary),
    or too few distinct peers seen yet to mean anything."""
    if _trusted_proxy() != "*" or AUTH_MODE == "forward":
        return None
    count = _exposure_peer_count()
    return count if count > _EXPOSURE_WARN_THRESHOLD else None


def _exposure_status() -> int | None:
    """#389 (review feedback): the always-shown counterpart to
    _exposure_warning, same reasoning as #365's "library last scanned N
    days ago" — a signal that only ever speaks up past its threshold is
    indistinguishable from a signal that's silently broken (e.g. if a
    given deployment's Docker networking masks real client source IPs,
    _exposure_peer_count never grows and the warning simply never fires,
    which looks identical to "nothing to report"). Showing the live count
    even below threshold lets an operator confirm the mechanism is active
    and has correctly found nothing, rather than guessing. None (not 0)
    when the mechanism isn't active at all — trusted_proxy pinned or
    forward mode — so "not applicable" is never confused with "quiet"."""
    if _trusted_proxy() != "*" or AUTH_MODE == "forward":
        return None
    return _exposure_peer_count()


def _rate_limited(bucket: str, max_failures: int, window_s: int) -> bool:
    """True if `bucket` has already logged >= max_failures within window_s."""
    now = time.time()
    with _RL_LOCK:
        dq = _rl_failures[bucket]
        while dq and dq[0] < now - window_s:
            dq.popleft()
        return len(dq) >= max_failures


def _record_failure(bucket: str) -> None:
    with _RL_LOCK:
        _rl_failures[bucket].append(time.time())


def _clear_failures(bucket: str) -> None:
    with _RL_LOCK:
        _rl_failures.pop(bucket, None)


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
    if request.path.startswith("/api/device/") or request.path.startswith("/oidc/"):
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

# "one or the other" — never more than one simultaneously.
# roon_client/subsonic_client/jellyfin_client/emby_client/plex_client/
# lms_client/filesystem_client expose the same function names/shapes
# (status, reconnect, list_playlists, get_playlist_tracks, get_artist_image,
# ensure_started) so every call site just dispatches through this rather
# than branching per route.
_PROVIDERS = {
    "roon": roon_client, "subsonic": subsonic_client, "jellyfin": jellyfin_client,
    "emby": emby_client, "plex": plex_client, "lms": lms_client,
    "filesystem": filesystem_client,
}


def _active_provider(conn):
    return _PROVIDERS.get(db.get_config(conn, "provider") or "roon", roon_client)


def _active_provider_id(conn) -> str:
    """Same fallback logic as _active_provider, as the string key instead
    of the module — for persisting which provider synced a playlist."""
    pid = db.get_config(conn, "provider")
    return pid if pid in _PROVIDERS else "roon"

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


def _gravatar_url(email: str) -> str:
    digest = hashlib.md5((email or "").strip().lower().encode("utf-8")).hexdigest()
    # d=404 so a not-found gravatar 404s instead of returning a placeholder
    # image — lets the frontend fall back to its own default via @error.
    return f"https://www.gravatar.com/avatar/{digest}?d=404&s=80"

# The one household member allowed to see/change app-wide config (Roon
# host/port, default Last.fm key) — everyone else in this app has equal
# access to everything else (devices, selections, profile), no other admin
# distinction exists. Set via env var rather than hardcoded so a future
# deployment of this app elsewhere just sets their own value, no code change.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")

# Three auth modes:
#   "local"   — every user authenticates via username+password against this
#               app directly. Fail-closed default: a fresh/misconfigured
# install lands here, not on header-trust.
#   "oidc"    — the app is an OpenID Connect client: it redirects users to
#               their IdP and cryptographically verifies the returned ID token
#               (signature via JWKS + iss/aud/exp/nonce). Works with any OIDC
#               provider (Authentik, Authelia, Keycloak, …), no reverse-proxy
#               auth layer required.
#   "forward" — trust identity headers (X-authentik-username) set by an
#               upstream ForwardAuth proxy. The app does NO auth of its own in
#               this mode, so the proxy MUST gate every request and the app
#               port must never be exposed directly. "authentik" is kept as a
#               backwards-compatible alias for this mode (this was the old
#               default name).
AUTH_MODE = os.environ.get("AUTH_MODE", "local")
if AUTH_MODE == "authentik":
    AUTH_MODE = "forward"

# OIDC client config — only consulted when AUTH_MODE=="oidc".
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email")
# Which ID-token claim carries the username the app keys users on. Default
# preferred_username; some IdPs/setups prefer email or a custom claim.
OIDC_USERNAME_CLAIM = os.environ.get("OIDC_USERNAME_CLAIM", "preferred_username")
# RP-initiated logout: when truthy, /logout also redirects to the IdP's
# end-session endpoint so the SSO session is cleared too, not just this app's.
OIDC_LOGOUT = os.environ.get("OIDC_LOGOUT", "").lower() in ("1", "true", "yes")

# forward-mode hardening: when set, the trusted proxy must inject a
# matching X-Forward-Auth-Secret header or the request is rejected — a
# fail-closed guard against someone hitting a directly-exposed port and
# spoofing X-authentik-username. When unset, headers are trusted as before
# (with a loud startup warning) so an existing proxy-gated deployment keeps
# working without a proxy-config change.
FORWARD_AUTH_SECRET = os.environ.get("FORWARD_AUTH_SECRET", "")

oauth = OAuth(app)
if AUTH_MODE == "oidc":
    # server_metadata_url points Authlib at the IdP's discovery document; it
    # handles endpoint discovery, JWKS fetch/caching, and full ID-token
    # validation (signature + standard claims + nonce) on token exchange.
    oauth.register(
        name="oidc",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{OIDC_ISSUER.rstrip('/')}/.well-known/openid-configuration",
        # PKCE (S256) on top of the confidential-client secret — defense in
        # depth, recommended by OAuth 2.1 even when a client secret is present.
        client_kwargs={"scope": OIDC_SCOPES, "code_challenge_method": "S256"},
    )

# Second internal port, NOT proxied by the reverse proxy (see
# docker-compose.yaml — port-published directly to the LAN, never via the
# public domain/proxy path at all).
# This is what makes the admin's break-glass login actually survive a
# ForwardAuth-proxy/SSO outage: the proxy's gate sits in front of Flask
# entirely, so a session cookie alone does nothing if the proxy never lets the
# request through in the first place — this port bypasses the proxy. 0
# (default) disables it — nothing changes for a deployment that hasn't set it.
# Only meaningful when AUTH_MODE=="forward" — see the __main__ block, which
# refuses to start this listener otherwise, since in local/oidc mode there's
# no proxy gate in front of Flask to bypass in the first place (and a local
# login is directly reachable anyway).
EMERGENCY_PORT = int(os.environ.get("EMERGENCY_PORT", "0"))


def _is_emergency_request() -> bool:
    # SERVER_PORT reflects the actual listening socket a connection arrived
    # on (verified directly — it's not derived from the client-sent Host
    # header and can't be spoofed by one), unlike everything else on the
    # request that a client controls. Traefik always connects to Flask's one
    # normal port regardless of what external hostname/port the original
    # client used, so this is a reliable way to tell "came in via the
    # un-proxied emergency port" from "came in via Traefik."
    return EMERGENCY_PORT != 0 and request.environ.get("SERVER_PORT") == str(EMERGENCY_PORT)


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
    if AUTH_MODE not in ("local", "oidc") and not _is_emergency_request():
        return None
    # #446: the /api/integrations/ prefix is exempted wholesale, not this
    # one route by name -- a future route added under it is login-exempt by
    # default, so forgetting to call _authenticated_integration_token() there
    # would leave it fully UNauthenticated rather than merely over-permissioned.
    # Same shape as the existing /api/device/ exemption, not a new risk,
    # but worth remembering before adding a second route under this prefix.
    if request.path.startswith("/api/device/") or request.path.startswith("/api/integrations/") \
            or request.path.startswith("/static") \
            or request.path.startswith("/set-language/") \
            or request.path.startswith("/oidc/") \
            or request.path in ("/login", "/logout", "/api/enrollment/redeem"):
        # #163: redeem is the one enrollment path a session-less device hits (it
        # has no token yet); the grant that authorizes it is its credential.
        # /api/enrollment/grant stays session-gated (it's minted by a web user).
        return None
    if session.get("local_user_id") is not None:
        return None
    if request.path.startswith("/api/"):
        abort(401, description=_("Login required"))
    return redirect(url_for("login"))


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
    if AUTH_MODE != "local" or request.path.startswith("/api/") or request.path.startswith("/static") \
            or request.path.startswith("/set-language/") \
            or request.path in ("/login", "/logout", "/setup"):
        return None
    user_id = session.get("local_user_id")
    if user_id is None:
        return None  # the other before_request hook handles getting them to /login first
    conn = db.get_conn()
    try:
        if not _is_admin(conn, user_id) or db.get_config(conn, "setup_completed"):
            return None
    finally:
        conn.close()
    return redirect(url_for("setup_wizard"))


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
    if username and username == ADMIN_USERNAME:
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ? AND is_admin = 0", (user_id,))
        conn.commit()
    return user_id


def get_current_user_id(conn) -> int:
    # A local session (set by /login's local-password path, or the OIDC
    # callback) always wins. In local/oidc mode it's the *only* accepted
    # identity — headers are never trusted.
    local_user_id = session.get("local_user_id")
    if local_user_id is not None:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (local_user_id,)).fetchone()
        if row is not None:
            return row["id"]
        session.pop("local_user_id", None)  # stale — e.g. that user got deleted

    if AUTH_MODE in ("local", "oidc") or _is_emergency_request():
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
    if FORWARD_AUTH_SECRET and request.headers.get("X-Forward-Auth-Secret") != FORWARD_AUTH_SECRET:
        abort(401, description=_("Login required"))
    username = request.headers.get("X-authentik-username") or os.environ.get("DEV_USER", "dev")
    email = request.headers.get("X-authentik-email", "")
    return _provision_user(conn, username, email)


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = db.get_conn()
    try:
        user_count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        bootstrap = AUTH_MODE == "local" and user_count == 0
        error = None
        if request.method == "POST":
            ip = _client_ip()
            if _rate_limited("login:" + ip, max_failures=10, window_s=300):
                return render_template(
                    "login.html", bootstrap=bootstrap,
                    error=_("Too many failed attempts. Please wait a few minutes and try again."),
                    auth_mode=AUTH_MODE,
                ), 429
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not username or not password:
                error = _("Username and password are required.")
            elif bootstrap and ADMIN_USERNAME and username != ADMIN_USERNAME:
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
                session["local_user_id"] = cur.lastrowid
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
                    and (AUTH_MODE != "oidc" or row["is_admin"])
                ):
                    _clear_failures("login:" + ip)
                    session["local_user_id"] = row["id"]
                    return redirect(url_for("index"))
                _record_failure("login:" + ip)
                error = _("Invalid credentials.")
        return render_template("login.html", bootstrap=bootstrap, error=error, auth_mode=AUTH_MODE)
    finally:
        conn.close()


@app.route("/oidc/login")
def oidc_login():
    """Kick off the OIDC authorization-code flow — Authlib builds the authorize
    URL with state + nonce + PKCE (stashed in the session) and redirects to the
    IdP. No-op guard for the wrong mode so the route can't be abused to start a
    flow that /oidc/callback would then reject anyway."""
    if AUTH_MODE != "oidc":
        return redirect(url_for("login"))
    return oauth.oidc.authorize_redirect(url_for("oidc_callback", _external=True))


@app.route("/oidc/callback")
def oidc_callback():
    """IdP redirect target. authorize_access_token() exchanges the code and
    fully validates the returned ID token (signature via JWKS, iss/aud/exp and
    the nonce) — an invalid/tampered/expired token raises here and never
    establishes a session. The verified username claim is then provisioned the
    same way the forward-header path is, and the local session is set."""
    if AUTH_MODE != "oidc":
        return redirect(url_for("login"))
    try:
        token = oauth.oidc.authorize_access_token()
    except Exception:
        return render_template("login.html", bootstrap=False,
                               error=_("Sign-in failed. Please try again."), auth_mode=AUTH_MODE), 401
    claims = token.get("userinfo") or {}
    username = (claims.get(OIDC_USERNAME_CLAIM) or "").strip()
    if not username:
        return render_template("login.html", bootstrap=False,
                               error=_("Your identity provider did not return a username."), auth_mode=AUTH_MODE), 401
    email = claims.get("email", "") or ""
    conn = db.get_conn()
    try:
        session["local_user_id"] = _provision_user(conn, username, email)
    finally:
        conn.close()
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("local_user_id", None)
    # RP-initiated logout (opt-in): also clear the SSO session at the IdP so a
    # re-login actually re-prompts, rather than silently re-using the still-open
    # IdP session. Falls back to a plain redirect if the IdP's discovery doc has
    # no end_session_endpoint.
    if AUTH_MODE == "oidc" and OIDC_LOGOUT:
        metadata = oauth.oidc.load_server_metadata()
        end_session = metadata.get("end_session_endpoint")
        if end_session:
            return redirect(f"{end_session}?post_logout_redirect_uri={url_for('index', _external=True)}")
    return redirect(url_for("login") if AUTH_MODE in ("local", "oidc") else url_for("index"))


def _is_admin(conn, user_id: int) -> bool:
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


def require_admin(conn) -> None:
    if not _is_admin(conn, get_current_user_id(conn)):
        abort(403, description=_("Admin access required"))


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
        },
        "themeOptions": {
            "auto": _("System"),
            "light": _("Light"),
            "dark": _("Dark"),
        },
        "deviceType": {
            "phone": _("Phone"), "android": _("Phone"), "tablet": _("Tablet"),
            "watch": _("Smartwatch"), "dap": _("Dedicated audio player"),
            # #218: relabelled from "SD card / USB storage" — a local-folder
            # target (device_type 'folder', below) used to have nowhere to
            # go but this bucket, so the old wording overclaimed what it
            # covered. The stored value is unchanged, existing devices are
            # unaffected.
            "sdcard": _("Removable storage"),
            "folder": _("Local folder"),
        },
        # Home dashboard widget catalog labels — id must match
        # dashboardWidgetCatalog in index.html and ADMIN_ONLY_WIDGETS above.
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
        "storageManualShare": _("{gb} GB manual picks"),
        "storageAutofitShare": _("{gb} GB auto-fit (up to {pct}%)"),
        "storageFreeShare": _("{gb} GB headroom"),
        "providerState": {
            "paired": _("Connected"),
            "pending_approval": _("Awaiting approval"),
            "disconnected": _("Disconnected"),
        },
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
        "libraryDuration": _("{duration} of music"),
        # #187: "%" kept clear of a following letter so Babel doesn't misread it
        # as a printf conversion (e.g. "% of" → %o), which breaks .po compilation.
        "libraryFormatShare": _("tracks {tracks}%, storage {storage}%"),
        "durationDaysHours": _("{days} days, {hours} hours"),
        "durationHours": _("{hours} hours"),
        "syncing": _("Syncing…"),
        "refreshPlaylists": _("Refresh playlists"),
        # #411: shown only while the hide-zero-match filter is on, so a
        # filtered-down list doesn't just look short.
        "playlistsShownCount": _("{shown} of {total} shown"),
        "playlistAvailability": _("{matched}/{total} tracks available locally ({pct}%)"),
        "sending": _("Sending…"),
        "chooseImage": _("Choose an image"),
        "saving": _("Saving…"),
        "disconnect": _("Disconnect"),
        "tidalConnectedAs": _("Connected as {name}"),
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
        "unresolvedTracksError": _("Couldn't load or save — try again."),
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
        "mirrorTo": _("Mirror to…"),
        "mirroring": _("Mirroring"),
        "mirrorOnHint": _("Kept in sync as a local .m3u file. Click to stop mirroring."),
        "mirrorOffHint": _("Mirror this playlist to a local .m3u file, kept in sync as your library grows."),
        "mirrorFolderIs": _("Mirror folder: {folder}"),
        "mirrorFolderUnset": _("No mirror folder configured — set one in Administration > Configuration."),
        "mirrorLastWritten": _("Last written {when}"),
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
        "subsonicMirrorUrlIs": _("Subsonic mirror target: {url}"),
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
        "jellyfinMirrorUrlIs": _("Jellyfin mirror target: {url}"),
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
        "embyMirrorUrlIs": _("Emby mirror target: {url}"),
        "embyMirrorUrlUnset": _("No Emby mirror target configured — set one in Administration > Configuration."),
        "embyMirrorErrorUnreachable": _("Could not reach the Emby mirror target:"),
        "embyMirrorErrorWriteFailed": _("Failed to write the Emby mirror playlist:"),
        "embyMirrorErrorNoTargetMatches": _("None of this playlist's tracks were found on the Emby mirror target."),
        # #494: "Request missing albums" — not a mirror sink (Lidarr isn't
        # a copy destination, it's asked to acquire what's missing), but
        # the button follows the same shown-but-disabled shape as the four
        # sinks above. Two distinct disabled reasons need two distinct
        # hints: not configured (server-side gate, same as the mirror
        # buttons) vs. this playlist's provider giving no album data at
        # all on its unresolved rows (every Roon/iTunes playlist).
        "lidarrRequestTo": _("Request missing albums…"),
        "lidarrRequesting": _("Requesting missing albums"),
        "lidarrRequestOnHint": _("Missing albums are requested from Lidarr as new gaps appear. Click to stop."),
        "lidarrRequestOffHint": _("Request this playlist's missing albums from your configured Lidarr instance, monitor-only — Lidarr's own scheduled search finds them later."),
        "lidarrRequestNotConfiguredHint": _("Lidarr requests aren't set up yet — ask an admin to configure a Lidarr connection in Administration."),
        # #509: distinct from the hint above — the connection itself works
        # (Administration shows green "Connected"), but the root folder and
        # quality/metadata profiles were never chosen, so there's still
        # nowhere to request an album TO. Splitting this out means the hint
        # actually matches what Administration would show if the admin went
        # to go look, instead of both states reading as "not set up" while
        # one of them is showing green.
        "lidarrRequestSetupIncompleteHint": _("Lidarr is connected, but the root folder and profiles haven't been chosen yet — ask an admin to finish setup in Administration."),
        "lidarrRequestNoAlbumDataHint": _("This playlist's source doesn't provide album information for unresolved tracks, so nothing here can be requested."),
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
        # Admin overview panel's own top summary line, same
        # UrlIs/UrlUnset pair shape as the mirror sinks above.
        "lidarrRequestUrlIs": _("Lidarr target: {url}"),
        "lidarrRequestUrlUnset": _("No Lidarr connection configured — set one in Administration > Configuration."),
        # #297 step 2: background-jobs admin panel.
        "jobsRunning": _("Running"),
        "jobsQueued": _("Queued"),
        "jobsFailed": _("Failed"),
        "jobsDone": _("Completed"),
        "jobsNone": _("No background jobs yet."),
        "jobsAllClear": _("Nothing running or waiting."),
        "jobsRetry": _("Retry"),
        "jobsCancel": _("Cancel"),
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
        "jobsRunningCantCancel": _("Already started — it can't be cancelled."),
        "jobsError": _("Couldn't load or update — try again."),
        "shared": _("Shared"),
        "private": _("Private"),
        "playlistSharedHint": _("Visible to everyone in the household. Click to make it private."),
        "playlistPrivateHint": _("Only visible to you (and the admin). Click to share it."),
        "inferredOriginHint": _("Its tracks closely match a {provider} playlist — likely imported into Roon from there."),
        "sharedByOwner": _("shared by {name}"),
        "unknownProvider": _("Unknown source"),
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
        # staged state (it's still literally the basket); the old
        # dedicated addToBasket ("Add to basket") string is retired along
        # with the button that used it, replaced by syncTo below for the
        # not-yet-staged state.
        "inBasket": _("In basket"),
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
        "never": _("never"),
        "justNow": _("just now"),
        "minutesAgo": {"one": _("{n} minute ago"), "other": _("{n} minutes ago")},
        "hoursAgo": {"one": _("{n} hour ago"), "other": _("{n} hours ago")},
        "yesterday": _("yesterday"),
        "daysAgo": {"one": _("{n} day ago"), "other": _("{n} days ago")},
        "weeksAgo": {"one": _("{n} week ago"), "other": _("{n} weeks ago")},
        "monthsAgo": {"one": _("{n} month ago"), "other": _("{n} months ago")},
        "yearsAgo": {"one": _("{n} year ago"), "other": _("{n} years ago")},
        "scanResultText": _("Last scan: {added} added, {updated} updated, {removed} removed ({unchanged} unchanged)."),
        "confirmDeleteAdminUser": _('Permanently delete the account "{username}"? Fails if this account still owns devices, selections, or playlists — delete or reassign those first.'),
        "resetPassword": _("Reset password"),
        "resetPasswordFor": _('Reset password for "{username}"'),
        "genericFailure": _("Failed ({error})"),
        "confirmRevokeDelegation": _("{grantee} will no longer be able to manage {target}'s devices. Continue?"),
        "confirmUnpinDevice": _('Remove "{name}" from your list? You will no longer manage it until you add it again.'),
        "confirmRegenerateToken": _('Regenerate the token for "{name}"? The old token will stop working immediately — the already-paired device will need to rescan the new QR code.'),
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
        "noDevice": _("no device"),
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
        "showLicense": _("Show license"),
        "hideLicense": _("Hide license"),
        "showNotices": _("Show third-party notices"),
        "hideNotices": _("Hide third-party notices"),
        "updateChecking": _("Checking…"),
        "updateAvailable": _("Update available: {tag} — see the releases page."),
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
    conn = db.get_conn()
    try:
        # follow-up: bumped whenever the artist-image cache is
        # cleared (key/provider change) — rides into artistImageUrl() as a
        # cache-busting param so a plain page reload shows the re-fetched
        # images instead of the browser's day-old copies.
        epoch = db.get_config(conn, "artist_images_epoch") or "0"
    finally:
        conn.close()
    return render_template("index.html", js_i18n=_build_js_i18n(),
                           artist_images_epoch=epoch)


# --- About -------------------------------------------------------
# VERSION/LICENSE/THIRD_PARTY_NOTICES.md sit next to main.py in the image
# (Dockerfile copies them); in a dev checkout they're one level up, at the
# repo root. Update checks are client-side and user-initiated only (the
# browser calls the GitHub releases API on button press) — the server never
# phones home.

def _meta_file(name: str) -> Path | None:
    here = Path(__file__).resolve().parent
    for candidate in (here / name, here.parent / name):
        if candidate.is_file():
            return candidate
    return None


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
    """The referrer, but only if it points back into this app — otherwise the
    home page. Prevents an open redirect from a crafted Referer."""
    ref = request.referrer
    if ref:
        u = urlsplit(ref)
        if not u.netloc or u.netloc == request.host:
            path = u.path or "/"
            if path.startswith("/"):
                return path + (("?" + u.query) if u.query else "")
    return url_for("index")


@app.route("/set-language/<lang>")
def set_language(lang):
    if lang not in ("en", "fr"):
        abort(404)
    resp = redirect(_safe_referrer())
    resp.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@app.route("/api/library/artists")
def api_library_artists():
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT artist, COUNT(*) AS track_count, "
            "COUNT(DISTINCT album) AS album_count FROM tracks "
            "WHERE deleted_at IS NULL GROUP BY artist ORDER BY artist"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/library/albums")
def api_library_albums():
    artist = request.args.get("artist", "")
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT album, MAX(year) AS year, MAX(reissue_year) AS reissue_year, "
            "COUNT(*) AS track_count FROM tracks "
            "WHERE deleted_at IS NULL AND artist = ? GROUP BY album "
            "ORDER BY year IS NULL, year DESC, album",
            (artist,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/library/stats")
def api_library_stats():
    """Real numbers for the home dashboard's Library widget — codec split,
    storage per codec, total duration, decade breakdown. One full pass over
    tracks (relative_path/size/duration/year only — cheap columns, no tag
    blobs), aggregated in Python: sqlite has no clean "extension of a path"
    built-in, and this is a snapshot computed on dashboard load, not a hot
    path worth a fancier query."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT relative_path, size, duration, year FROM tracks WHERE deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()

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

    return jsonify({
        "total_tracks": len(rows),
        "total_duration_seconds": int(total_duration),
        "by_codec": by_codec,
        "by_decade": dict(sorted(by_decade.items())),
    })


@app.route("/api/library/similar-artists")
def api_library_similar_artists():
    """Artists similar to `artist` (Last.fm artist.getSimilar) that are actually
    in the local library — so they're selectable/syncable, not
    external discovery. Returns up to 8 library-cased names, most-similar first.
    [] if Last.fm isn't configured or nothing similar is in the library."""
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify([])
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        row = conn.execute("SELECT lastfm_api_key FROM users WHERE id = ?", (user_id,)).fetchone()
        api_key = (row["lastfm_api_key"] if row else None) or db.get_config(conn, "lastfm_api_key_default") or ""
        similar = lastfm.similar_artists(artist, api_key, limit=40, api_base=db.get_config(conn, "lastfm_api_base") or "")
        if not similar:
            return jsonify([])
        library = {r["artist"].lower(): r["artist"] for r in conn.execute(
            "SELECT DISTINCT artist FROM tracks WHERE deleted_at IS NULL"
        )}
        out, seen = [], set()
        for name in similar:
            if name.lower() == artist.lower():
                continue  # skip the artist itself
            exact = library.get(name.lower())
            if exact and exact not in seen:
                seen.add(exact)
                out.append(exact)
            if len(out) >= 8:
                break
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/library/cover")
def api_library_cover():
    artist = request.args.get("artist", "")
    album = request.args.get("album", "")
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT relative_path FROM tracks WHERE deleted_at IS NULL "
            "AND artist = ? AND album = ? LIMIT 1",
            (artist, album),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)
    # Disk-cached: only the first browse of an album touches NFS.
    cover = covers.get_cover(artist, album, db.get_music_root() / row["relative_path"])
    if cover is None:
        abort(404)
    data, mime = cover
    return Response(data, mimetype=mime, headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/library/artist-image")
def api_library_artist_image():
    artist = request.args.get("artist", "")
    if not artist:
        abort(404)
    conn = db.get_conn()
    try:
        provider = _active_provider(conn)
        audiodb_key = db.get_config(conn, "audiodb_api_key")
    finally:
        conn.close()
    found = artist_images.get_artist_image(artist, provider, audiodb_key)
    if found is None:
        abort(404)
    data, content_type = found
    return Response(data, mimetype=content_type, headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/library/quiz-pair")
def api_library_quiz_pair():
    """#508: the About tab's "duel the bard" Easter egg — "which artist has
    more albums?", scoped to the local library. Pair selection is a pure
    function in library_quiz.py (unit-testable without a browser or a DB);
    this route is just the read plus the JSON shape.

    Artist images are NOT pre-checked for availability here — artist_images.py
    caches on a hit only (no negative cache), so there's no cheap way to know
    a miss without actually fetching, and doing that here would add real
    network cost to a for-fun endpoint. The client already handles a missing
    artist image gracefully everywhere else (artistImageUrl() + the
    established @error/@load pattern degrades to a neutral placeholder), so
    the quiz cards reuse that instead of adding server-side prefetch."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT artist, COUNT(DISTINCT album) AS album_count FROM tracks "
            "WHERE deleted_at IS NULL GROUP BY artist"
        ).fetchall()
    finally:
        conn.close()
    candidates = library_quiz.eligible_candidates([dict(r) for r in rows])
    pair = library_quiz.pick_pair(candidates)
    if pair is None:
        return jsonify({"available": False})
    a, b = pair
    return jsonify({"available": True, "a": a, "b": b})


@app.route("/api/library/scan", methods=["POST"])
def api_library_scan():
    force = request.args.get("force") == "1"
    # #140: background the scan and return immediately (202) — a full scan takes
    # tens of minutes; the UI polls .../scan/status and shows counts when done.
    result = scanner.start_scan(db.get_music_root(), force=force)
    if result.get("already_running"):
        return jsonify({"error": _("A library scan is already running.")}), 409
    return jsonify(result), 202


@app.route("/api/library/scan/status")
def api_library_scan_status():
    # #140: running/idle + the last completed scan's counts, polled by the UI.
    return jsonify(scanner.scan_status())


@app.route("/api/provider/status")
def api_provider_status():
    conn = db.get_conn()
    try:
        return jsonify(_active_provider(conn).status())
    finally:
        conn.close()


@app.route("/api/provider/pair", methods=["POST"])
def api_provider_pair():
    conn = db.get_conn()
    try:
        return jsonify(_active_provider(conn).retry_pairing())
    finally:
        conn.close()


@app.route("/api/provider/playlists/sync", methods=["POST"])
def api_provider_playlists_sync():
    conn = db.get_conn()
    try:
        provider_id = _active_provider_id(conn)
        provider = _PROVIDERS[provider_id]
    finally:
        conn.close()
    # #138: kick the sync off in the background and return immediately (202)
    # instead of blocking the request for the minutes a large sync takes — the
    # UI polls .../sync/status and reloads the list as playlists commit (#133).
    result = playlist_sync.start_sync(provider, provider_id)
    if result.get("already_running"):
        # #129: mirror the library-scan endpoint — one sync at a time.
        return jsonify({"error": _("A playlist sync is already running.")}), 409
    return jsonify(result), 202


@app.route("/api/provider/playlists/sync/status")
def api_provider_playlists_sync_status():
    # #138: running/idle + the last completed run's counts, polled by the UI.
    return jsonify(playlist_sync.sync_status())


@app.route("/api/provider/playlists")
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
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        admin = _is_admin(conn, user_id)
        rows = conn.execute(
            "SELECT p.id, p.title, p.source_provider, p.inferred_origin_provider, p.last_synced_at, "
            "p.owner_user_id, u.username AS owner_username, p.shared, "
            "(p.owner_user_id = :uid) AS is_own, "
            "p.mirror_enabled, p.mirror_last_error, p.mirror_last_error_code, "
            "p.subsonic_mirror_enabled, p.subsonic_mirror_last_error, "
            "p.subsonic_mirror_last_error_code, "
            "p.jellyfin_mirror_enabled, p.jellyfin_mirror_last_error, "
            "p.jellyfin_mirror_last_error_code, "
            "p.emby_mirror_enabled, p.emby_mirror_last_error, "
            "p.emby_mirror_last_error_code, "
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
            " WHERE u.playlist_id = p.id AND u.excluded = 0) AS unresolved_count, "
            # #494: same correlated-subquery reasoning as unresolved_count
            # just above — this is the "does this playlist have anything
            # Lidarr-requestable at all" eligibility flag (album IS NOT NULL
            # is always false for every Roon/iTunes unresolved row, so those
            # playlists naturally get 0 here without a provider-specific
            # special case).
            "(SELECT COUNT(*) FROM unresolved_playlist_tracks u "
            " WHERE u.playlist_id = p.id AND u.excluded = 0 "
            " AND u.album IS NOT NULL AND u.album != '') AS unresolved_with_album_count "
            "FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id "
            "LEFT JOIN users u ON u.id = p.owner_user_id "
            "LEFT JOIN playlists g ON g.id = p.golden_source_id "
            "LEFT JOIN users gu ON gu.id = g.owner_user_id "
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
        for r in rows:
            d = dict(r)
            # #449: same SQLite-integer-vs-JSON-boolean coercion as
            # is_own/is_pinned in _device_rows_for_user() -- mirror_enabled
            # already got this treatment, is_own/shared hadn't. This
            # endpoint is session-only today (JS truthiness covers 0/1
            # fine), but "booleans are booleans" throughout the API is the
            # rule now, not just where a non-JS consumer happens to exist.
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
        return jsonify(result)
    finally:
        conn.close()


@app.route("/api/provider/playlists/<int:playlist_id>", methods=["PATCH"])
def api_provider_playlist_update(playlist_id: int):
    """The `shared` toggle — owner or admin only (#28's own proposed
    design). Nothing else about a playlist is user-editable here; it's
    provider-synced data, not something Trobar itself lets you rename/
    reorder."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
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
    finally:
        conn.close()


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


@app.route("/api/provider/playlists/<int:playlist_id>/unresolved-tracks")
def api_playlist_unresolved_tracks(playlist_id: int):
    """#200: this playlist's unresolved entries (identity.py's resolver
    missed every tier) for the web review list — same visibility as the
    playlist itself."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_playlist_visible_by_id(conn, user_id, playlist_id)
        return jsonify(sync_state.list_unresolved_playlist_tracks(conn, playlist_id))
    finally:
        conn.close()


@app.route("/api/provider/playlists/<int:playlist_id>/unresolved-tracks/exclude", methods=["POST"])
def api_playlist_unresolved_tracks_exclude(playlist_id: int):
    """#200: {"ids": [...], "excluded": true|false} — acknowledge the given
    unresolved_playlist_tracks rows as "not actually a gap" (e.g. a
    provider-only track that's streamed but never downloaded, so it will
    never resolve locally) so they stop being flagged, or un-exclude them.
    Returns the resulting non-excluded count for this playlist. Excluding
    doesn't change matching itself — it's purely a review-list
    acknowledgment, same relationship set_device_unknown_adopted's
    `adopted` has to that other review surface."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_playlist_visible_by_id(conn, user_id, playlist_id)
        body = request.get_json(force=True)
        ids = body.get("ids")
        if not isinstance(ids, list):
            abort(400, description=_("ids must be a list of unresolved-track row ids."))
        excluded = bool(body.get("excluded", True))
        count = sync_state.set_unresolved_playlist_tracks_excluded(conn, playlist_id, ids, excluded)
        return jsonify({"unresolved_count": count})
    finally:
        conn.close()


@app.route("/api/provider/playlists/<int:playlist_id>/mirror", methods=["POST"])
def api_playlist_mirror_toggle(playlist_id: int):
    """#285/#189: {"enabled": true|false, "sink": "filesystem"|"subsonic"|
    "jellyfin"|"emby"} — toggle this playlist's mirror to one sink. `sink`
    defaults to "filesystem" (the shape this route originally had, before
    #189 added more sinks) so an old cached frontend mid-deploy keeps
    working. Same #28 visibility as the unresolved-tracks routes (any user
    who can see the playlist, not owner/admin-gated — the issue explicitly
    calls this "not admin-only"). Writes/deletes the mirror immediately
    (the sink module's own write_mirror/delete_mirror), rather than
    waiting for the next scheduled sync, so the toggle has visible effect
    right away. Returns the refreshed row for EVERY sink (not just the one
    toggled) so the UI can update its whole mirror-status display without
    a separate reload."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_playlist_visible_by_id(conn, user_id, playlist_id)
        body = request.get_json(force=True)
        if "enabled" not in body:
            abort(400, description=_("Nothing to update."))
        enabled = bool(body["enabled"])
        sink = body.get("sink", "filesystem")
        if sink not in ("filesystem", "subsonic", "jellyfin", "emby"):
            abort(400, description=_("Unknown mirror sink."))

        if sink == "filesystem":
            conn.execute(
                "UPDATE playlists SET mirror_enabled = ? WHERE id = ?",
                (1 if enabled else 0, playlist_id),
            )
            (mirror.write_mirror if enabled else mirror.delete_mirror)(conn, playlist_id)
        elif sink == "subsonic":
            conn.execute(
                "UPDATE playlists SET subsonic_mirror_enabled = ? WHERE id = ?",
                (1 if enabled else 0, playlist_id),
            )
            (mirror_subsonic.write_mirror if enabled else mirror_subsonic.delete_mirror)(
                conn, playlist_id)
        elif sink == "jellyfin":
            conn.execute(
                "UPDATE playlists SET jellyfin_mirror_enabled = ? WHERE id = ?",
                (1 if enabled else 0, playlist_id),
            )
            (mirror_jellyfin.write_mirror if enabled else mirror_jellyfin.delete_mirror)(
                conn, playlist_id)
        else:
            conn.execute(
                "UPDATE playlists SET emby_mirror_enabled = ? WHERE id = ?",
                (1 if enabled else 0, playlist_id),
            )
            (mirror_emby.write_mirror if enabled else mirror_emby.delete_mirror)(
                conn, playlist_id)
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
            "emby_mirror_last_error_code "
            "FROM playlists WHERE id = ?", (playlist_id,),
        ).fetchone()
        return jsonify({
            **dict(row),
            "mirror_enabled": bool(row["mirror_enabled"]),
            "subsonic_mirror_enabled": bool(row["subsonic_mirror_enabled"]),
            "jellyfin_mirror_enabled": bool(row["jellyfin_mirror_enabled"]),
            "emby_mirror_enabled": bool(row["emby_mirror_enabled"]),
        })
    finally:
        conn.close()


@app.route("/api/provider/playlists/<int:playlist_id>/lidarr-requests", methods=["POST"])
def api_playlist_lidarr_requests_toggle(playlist_id: int):
    """#494: {"enabled": true|false}. A separate route from
    api_playlist_mirror_toggle above, deliberately — this isn't a mirror
    (nothing is copied anywhere), and that route's own `sink` values
    enumerate mirror DESTINATIONS; a "lidarr" sink there would misdescribe
    the feature. Same #28 visibility as the mirror toggle (any user who
    can see the playlist, not owner/admin-gated). Runs
    lidarr_requests.run_for_playlist() immediately on enable — same
    "instant feedback, don't wait for the next scheduled sync" contract as
    the mirror toggle. Nothing to do on disable: by settled
    design, un-ticking stops FUTURE requests only and never un-monitors or
    removes anything Lidarr already has — there is no delete_mirror()
    equivalent to call here."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
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
        if enabled:
            lidarr_requests.run_for_playlist(conn, playlist_id)
            conn.commit()

        row = conn.execute(
            "SELECT lidarr_request_enabled, lidarr_request_last_run_at, "
            "lidarr_request_last_count, lidarr_request_last_error, "
            "lidarr_request_last_error_code FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()
        return jsonify({
            **dict(row),
            "lidarr_request_enabled": bool(row["lidarr_request_enabled"]),
        })
    finally:
        conn.close()


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
        "SELECT d.id, d.name, d.device_type, d.max_size_bytes, d.transcode_format, d.artist_images, "
        "d.source_of_truth, d.unknown_track_count, "
        "d.reported_free_bytes, "
        "d.reported_total_bytes, d.free_bytes_reported_at, d.created_at, d.last_seen_at, "
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


# SQLite stores INTEGER as signed 64-bit; a larger value raises OverflowError
# on INSERT (a 500). Reject it up front so bad input never 500s (#100).
_MAX_SIZE_BYTES_LIMIT = 2**63 - 1


def _validated_max_size_bytes(value):
    """A storage cap is either null (no limit) or a non-negative integer number
    of bytes that fits a signed 64-bit column (#100). Rejects negatives,
    non-integers, booleans, strings, and absurdly-large values — an unvalidated
    value flows into the usage/autofit math and would blow up there or on INSERT
    later. Returns the int or None."""
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 0 or value > _MAX_SIZE_BYTES_LIMIT):
        abort(400, description=_("Storage limit must be a whole number of bytes, or empty for no limit."))
    return value


def _validated_autofit_percent(value):
    """#217: a whole number 1-100 — 0 would mean "fill nothing", which is
    just disabling auto-fit through a confusing back door, so it's excluded
    rather than silently accepted."""
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 100):
        abort(400, description=_("Fill percentage must be a whole number between 1 and 100."))
    return value


def _validated_transcode_format(value):
    """None (originals) or one of the supported MP3 formats (#97)."""
    fmt = value or None
    if fmt is not None and fmt not in sync_state.TRANSCODE_FORMATS:
        abort(400, description=_("Unsupported transcode format."))
    return fmt


def _validated_artist_images(value):
    imgs = value or None
    if imgs not in (None, "small", "full"):
        abort(400, description=_("Unsupported artist image setting."))
    return imgs


def _validated_source_of_truth(value):
    if value not in ("server", "device"):
        abort(400, description=_("source_of_truth must be 'server' or 'device'."))
    return value


@app.route("/api/devices", methods=["GET", "POST"])
def api_devices():
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        if request.method == "POST":
            body = request.get_json(force=True)
            name = _validated_device_name(body)
            # #97: honour transcode_format/artist_images at create time (PATCH
            # already did) so an API client can pair + configure in one call,
            # instead of silently dropping them and creating a plain device.
            device_id, raw_token = sync_state.create_device(
                conn, user_id, name, body.get("device_type", "phone"),
                _validated_max_size_bytes(body.get("max_size_bytes")),
                transcode_format=_validated_transcode_format(body.get("transcode_format")),
                artist_images=_validated_artist_images(body.get("artist_images")),
            )
            return jsonify({"id": device_id, "name": name, "token": raw_token})

        rows = _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))
        out = []
        for r in rows:
            d = dict(r)
            d["autofit"] = sync_state.autofit_status(conn, d["id"])
            d["sync_status"] = sync_state.sync_status(conn, d["id"])
            out.append(d)
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/integrations/devices")
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
    conn = db.get_conn()
    try:
        user_id = _authenticated_integration_token(conn)
        rows = _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))
        out = []
        for r in rows:
            d = dict(r)
            d["autofit"] = sync_state.autofit_status(conn, d["id"])
            d["sync_status"] = sync_state.sync_status(conn, d["id"])
            out.append(d)
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/integrations/server")
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
    conn = db.get_conn()
    try:
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
    finally:
        conn.close()


# #498: (enabled_column, error_code_column, last_written_column) per sink.
# "filesystem" keeps its historical unprefixed `mirror_*` columns (it
# predates the other three sinks, added); subsonic/jellyfin/emby
# each got their own `{sink}_mirror_*` columns when added. A dict (not a
# tuple of tuples) so both api_integrations_mirrors and its response's
# `by_sink` object iterate in the same order — filesystem, subsonic,
# jellyfin, emby — matching #189's own introduction order and every other
# sink listing in this file (api_playlist_mirror_toggle, api_admin_mirrors).
_MIRROR_SINK_COLUMNS = {
    "filesystem": ("mirror_enabled", "mirror_last_error_code", "mirror_last_written_at"),
    "subsonic": ("subsonic_mirror_enabled", "subsonic_mirror_last_error_code",
                 "subsonic_mirror_last_written_at"),
    "jellyfin": ("jellyfin_mirror_enabled", "jellyfin_mirror_last_error_code",
                 "jellyfin_mirror_last_written_at"),
    "emby": ("emby_mirror_enabled", "emby_mirror_last_error_code", "emby_mirror_last_written_at"),
}

# #498: unlike Health's 200 (opened occasionally, in a browser), this
# endpoint is meant to be polled unattended every few minutes — a single
# dead mirror target can fail every playlist mirrored to it, so the cap
# needs to stay well under a size that would make a routine poll heavy.
# mirrors_failing/by_sink are computed from the FULL set regardless of this
# cap and stay exact; only the `failing` worklist itself is capped.
_MIRRORS_FAILING_LIMIT = 50


@app.route("/api/integrations/mirrors")
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
    conn = db.get_conn()
    try:
        _authenticated_integration_token(conn)
        rows = conn.execute(
            "SELECT id, title, "
            "mirror_enabled, mirror_last_error_code, mirror_last_written_at, "
            "subsonic_mirror_enabled, subsonic_mirror_last_error_code, "
            "subsonic_mirror_last_written_at, "
            "jellyfin_mirror_enabled, jellyfin_mirror_last_error_code, "
            "jellyfin_mirror_last_written_at, "
            "emby_mirror_enabled, emby_mirror_last_error_code, emby_mirror_last_written_at "
            "FROM playlists "
            "WHERE mirror_enabled = 1 OR subsonic_mirror_enabled = 1 "
            "OR jellyfin_mirror_enabled = 1 OR emby_mirror_enabled = 1 "
            "ORDER BY title"
        ).fetchall()

        by_sink = {sink: {"enabled": 0, "failing": 0} for sink in _MIRROR_SINK_COLUMNS}
        failing = []
        for row in rows:
            for sink, (enabled_col, error_col, written_col) in _MIRROR_SINK_COLUMNS.items():
                if not row[enabled_col]:
                    continue
                by_sink[sink]["enabled"] += 1
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
        })
    finally:
        conn.close()


@app.route("/api/integrations/actions/scan", methods=["POST"])
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
    conn = db.get_conn()
    try:
        _authenticated_integration_token(conn)
    finally:
        conn.close()
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    result = scanner.start_scan(db.get_music_root(), force=force)
    if result.get("already_running"):
        return jsonify({"error": _("A library scan is already running.")}), 409
    return jsonify(result), 202


@app.route("/api/integration-tokens", methods=["GET", "POST"])
def api_integration_tokens():
    """#446/#474: manage integration tokens — admin-only, unlike every
    other per-user credential in this file (device tokens, the old
    api_tokens/action_tokens split). That's the actual safety property
    now: db.py's integration_tokens comment and #474's PR discussion cover
    why gating who may mint one replaced the earlier read-only/action
    table split. require_admin() on both GET and POST, not just POST — a
    non-admin's own list is always empty under this model, so there is
    nothing for them to legitimately see here either."""
    conn = db.get_conn()
    try:
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
    finally:
        conn.close()


@app.route("/api/integration-tokens/<int:token_id>", methods=["DELETE"])
def api_integration_token_delete(token_id: int):
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        require_admin(conn)
        if not sync_state.revoke_integration_token(conn, user_id, token_id):
            abort(404, description=_("Integration token not found."))
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/enrollment/grant", methods=["POST"])
def api_enrollment_grant():
    """#163: an authenticated web session mints a short-lived enrollment code
    for the current user; the web UI shows it as a QR + human code for a mobile
    client to redeem (see /api/enrollment/redeem). Keeps auth in the browser —
    the app never holds user credentials (#162)."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        code = sync_state.create_enrollment_grant(conn, user_id)
        return jsonify({"code": code, "expires_in": sync_state.ENROLLMENT_TTL_SECONDS})
    finally:
        conn.close()


@app.route("/api/enrollment/redeem", methods=["POST"])
def api_enrollment_redeem():
    """#163: a mobile client (no session, no device token yet) redeems an
    enrollment code to create its own device and receive a device Bearer token
    (the same token used for all /api/device/* calls thereafter). Rate-limited
    per IP like the other device endpoints."""
    ip = _client_ip()
    if _rate_limited("enroll:" + ip, max_failures=30, window_s=300):
        abort(429, description=_("Too many attempts. Please wait a few minutes."))
    conn = db.get_conn()
    try:
        body = request.get_json(force=True)
        code = (body.get("code") or "").strip().upper()
        name = _validated_device_name(body)
        result = sync_state.redeem_enrollment_grant(
            conn, code, name, body.get("device_type", "phone"),
            _validated_max_size_bytes(body.get("max_size_bytes")),
            transcode_format=_validated_transcode_format(body.get("transcode_format")),
        )
        if result is None:
            _record_failure("enroll:" + ip)
            abort(400, description=_("Invalid or expired enrollment code."))
        device_id, raw_token = result
        return jsonify({"id": device_id, "name": name, "token": raw_token})
    finally:
        conn.close()


@app.route("/api/devices/delegatable")
def api_devices_delegatable():
    """The pool a delegate picks from to pin a device into their own
    Appareils list: devices owned by someone who's granted them delegation,
    not already pinned. See."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        rows = conn.execute(
            "SELECT d.id, d.name, d.device_type, u.username AS owner_username "
            "FROM devices d JOIN users u ON u.id = d.owner_user_id "
            "WHERE d.owner_user_id IN (SELECT target_user_id FROM device_delegations WHERE grantee_user_id = ?) "
            "AND d.id NOT IN (SELECT device_id FROM device_pins WHERE user_id = ?) "
            "ORDER BY u.username, d.name",
            (user_id, user_id),
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/devices/<int:device_id>/pin", methods=["POST", "DELETE"])
def api_device_pin(device_id: int):
    conn = db.get_conn()
    try:
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
    finally:
        conn.close()


@app.route("/api/devices/<int:device_id>", methods=["PATCH", "DELETE"])
def api_device_update(device_id: int):
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        if request.method == "DELETE":
            # selection_devices and device_track_state both have ON DELETE
            # CASCADE on device_id — removing the device alone is enough to
            # clean those up. Selections themselves are left untouched even
            # if this was their only assigned device (just shows "no
            # device" in the Selections tab — still reassignable later).
            conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
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
            params.append(body["device_type"])
        if "max_size_bytes" in body:
            updates.append("max_size_bytes = ?")
            params.append(_validated_max_size_bytes(body.get("max_size_bytes")))
        if "artist_images" in body:
            updates.append("artist_images = ?")
            params.append(_validated_artist_images(body.get("artist_images")))
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
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/devices/<int:device_id>/regenerate-token", methods=["POST"])
def api_device_regenerate_token(device_id: int):
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        token = sync_state.regenerate_token(conn, device_id)
        name = conn.execute("SELECT name FROM devices WHERE id = ?", (device_id,)).fetchone()["name"]
        return jsonify({"name": name, "token": token})
    finally:
        conn.close()


@app.route("/api/devices/<int:device_id>/transfer-from", methods=["POST"])
def api_device_transfer(device_id: int):
    """#440: "device_id replaces from_device_id" -- reassigns
    device_track_state/selections/settings from the old device onto this
    one, then deletes the old device. See sync_state.transfer_device for
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
    conn = db.get_conn()
    try:
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
        # sync_state.transfer_device's docstring for why the default matters.
        assume_present = bool(body.get("assume_present", False))
        summary = sync_state.transfer_device(conn, from_device_id, device_id, assume_present=assume_present)
        return jsonify({"status": "ok", **summary})
    finally:
        conn.close()


@app.route("/api/devices/<int:device_id>/unknown-tracks")
def api_device_unknown_tracks(device_id: int):
    """#161: the device's "unknown" extras — manifest paths that match no live
    library track (side-loaded, or kept past a library deletion) — for the web
    review list. Owner/admin/delegate only (session)."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        return jsonify(sync_state.list_device_unknown_tracks(conn, device_id))
    finally:
        conn.close()


@app.route("/api/devices/<int:device_id>/unknown-tracks/adopt", methods=["POST"])
def api_device_unknown_tracks_adopt(device_id: int):
    """#161: {"paths": [...], "adopted": true|false} — acknowledge the given
    device paths as device-owned extras (recorded, never managed by the server)
    so they stop being flagged, or un-adopt them. Returns the resulting
    unknown_track_count (non-adopted)."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        body = request.get_json(force=True)
        paths = body.get("paths")
        if not isinstance(paths, list):
            abort(400, description=_("paths must be a list of device paths."))
        adopted = bool(body.get("adopted", True))
        count = sync_state.set_device_unknown_adopted(conn, device_id, paths, adopted)
        return jsonify({"unknown_track_count": count})
    finally:
        conn.close()


@app.route("/api/devices/<int:device_id>/usage")
def api_device_usage(device_id: int):
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
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
            "SELECT max_size_bytes, reported_free_bytes, reported_total_bytes, free_bytes_reported_at "
            "FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        max_size = device["max_size_bytes"]
        free_bytes = device["reported_free_bytes"]
        total_bytes = device["reported_total_bytes"]
        device_used_bytes = (total_bytes - free_bytes) if (total_bytes is not None and free_bytes is not None) else None
        return jsonify({
            "used_bytes": used_bytes, "max_size_bytes": max_size,
            "track_count": len(track_ids),
            "over_limit": max_size is not None and used_bytes > max_size,
            "reported_free_bytes": free_bytes,
            "reported_total_bytes": total_bytes,
            # whole-device storage usage — distinct from used_bytes, which is
            # only Trobar's own share of it (the device may have other
            # apps/files using the rest).
            "device_used_bytes": device_used_bytes,
            "free_bytes_reported_at": device["free_bytes_reported_at"],
            # the limit set in the web UI claiming more space than the
            # device's own storage actually has free right now (independent
            # of how much is already used by this sync's own files).
            "limit_exceeds_physical_capacity": (
                max_size is not None and free_bytes is not None and max_size > (used_bytes + free_bytes)
            ),
        })
    finally:
        conn.close()


def _ranked_album_keys_for_user(conn, user_id: int, period: str):
    """(artist_lower, album_lower) tuples in descending Last.fm play order for a
    user — the ranking auto-fit greedily fills from. None if the user
    has no Last.fm username configured (auto-fit can't rank without it)."""
    row = conn.execute(
        "SELECT lastfm_username, lastfm_api_key FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None or not row["lastfm_username"]:
        return None
    api_key = row["lastfm_api_key"] or db.get_config(conn, "lastfm_api_key_default") or ""
    # A generous limit so a large device has enough candidate albums to fill.
    albums = lastfm.top_albums(row["lastfm_username"], api_key, period=period, limit=500,
                                api_base=db.get_config(conn, "lastfm_api_base") or "")
    keys = []
    for a in albums:
        name = (a.get("name") or "").strip()
        artist = ((a.get("artist") or {}).get("name") or "").strip()
        if name and artist:
            keys.append((artist.lower(), name.lower()))
    return keys


@app.route("/api/devices/<int:device_id>/autofit", methods=["POST", "DELETE"])
def api_device_autofit(device_id: int):
    """Enable/refresh (POST) or disable (DELETE) storage-budget auto-fit for a
    device. Ranks the device owner's most-played albums (Last.fm) and
    fits whole albums into the device's remaining budget; on-demand only, so the
    synced set is stable until the next refresh. #217: an optional "percent"
    in the POST body persists a new fill-percentage cap before refreshing —
    omit it to refresh at whatever's already set."""
    conn = db.get_conn()
    try:
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
        ranked = _ranked_album_keys_for_user(conn, device["owner_user_id"], period)
        # dict(...) rather than sync_state.AutofitSummary directly: the
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
    finally:
        conn.close()


@app.route("/api/devices/<int:device_id>/autofit/preview")
def api_device_autofit_preview(device_id: int):
    """#217: the percent-independent basis (max_size_bytes/manual_bytes/
    avg_track_bytes) for the live GB/track-count preview in the Devices
    panel — fetched once when the auto-fit mini-panel opens, not per slider
    drag; the frontend does the cheap percent arithmetic itself from there.
    No Last.fm call, no write."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_device_access(conn, user_id, device_id)
        result = sync_state.autofit_fill_basis(conn, device_id)
        if result is None:
            abort(404)
        return jsonify(result)
    finally:
        conn.close()


# #352: never 'autofit' — that type is only ever created internally
# (sync_state.refresh_autofit), resolved by selection id against
# autofit_tracks rather than by a target string, so it isn't meaningful
# for a client to ask for by type/target through either route below.
#
# #438: 'track' IS valid and fully handled (_resolve_selection_track_ids,
# and list_basket resolves it to a title), but deliberately has no UI
# affordance — nothing calls basket.add('track', …). It's reachable only
# via the API. Not an oversight; don't "fix" it by adding a button.
VALID_SELECTION_TYPES = {"artist", "album", "playlist", "track"}


@app.route("/api/selections", methods=["GET", "POST"])
def api_selections():
    conn = db.get_conn()
    try:
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
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/selections/<int:selection_id>", methods=["DELETE"])
def api_selection_delete(selection_id: int):
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        _require_selection_access(conn, user_id, selection_id)
        sync_state.delete_selection(conn, selection_id)
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/selections/toggle-device", methods=["POST"])
def api_selections_toggle_device():
    """Used by the Selections matrix views (albums/artists/playlists) — one
    checkbox cell, one call. Creates the selection on first check; an
    unchecked-to-zero selection is left in place (device-less, removable
    from the flat list) rather than auto-deleted, so an accidental click is
    easy to undo."""
    conn = db.get_conn()
    try:
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
        return jsonify({"status": "ok", "selection_id": selection_id})
    finally:
        conn.close()


def _basket_last_destinations_dict(raw: str | None) -> dict:
    """Parses the users.basket_last_destinations JSON column, tolerating
    NULL/missing/malformed text (a fresh column, or a hand-edited DB) and
    dropping any entry that isn't a surface name mapped to a list of ints —
    same tolerate-garbage shape as _dashboard_widgets_dict."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result = {}
    for surface, device_ids in parsed.items():
        if isinstance(surface, str) and isinstance(device_ids, list) \
                and all(isinstance(d, int) for d in device_ids):
            result[surface] = device_ids
    return result


# #303/#501: the cross-surface staging basket. Items accumulate here from
# any surface, staged against the device(s) chosen in the picker at add
# time, until a POST /api/basket/fan-out sends a device's own section —
# the exact same sync_state.create_selection() call the pre-existing
# single-item device picker always made, just per-device now instead of
# one flat pass over the whole basket.
@app.route("/api/basket", methods=["GET", "POST", "DELETE"])
def api_basket():
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        if request.method == "POST":
            body = request.get_json(force=True)
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
            item_id = sync_state.add_basket_item(
                conn, user_id, body["type"], body["target"], device_ids)
            return jsonify({"id": item_id})
        if request.method == "DELETE":
            sync_state.clear_basket(conn, user_id)
            return jsonify({"status": "ok"})
        return jsonify(sync_state.list_basket(conn, user_id))
    finally:
        conn.close()


@app.route("/api/basket/<int:item_id>", methods=["DELETE"])
def api_basket_item_delete(item_id: int):
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        sync_state.remove_basket_item(conn, user_id, item_id)
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/basket/<int:item_id>/devices/<int:device_id>", methods=["DELETE"])
def api_basket_item_device_delete(item_id: int, device_id: int):
    """#501: the per-device basket panel's own per-section × — unstages
    this item from just THIS device's section, leaving it staged for any
    others. Ownership is enforced inside sync_state.unstage_basket_item_
    device itself (scoped to user_id in the same query), matching
    api_basket_item_delete's own style, so a bad item_id or one belonging
    to someone else is silently a no-op rather than a 404/403 — same
    tolerance the whole-item DELETE above already has."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        sync_state.unstage_basket_item_device(conn, user_id, item_id, device_id)
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/basket/fan-out", methods=["POST"])
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
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True)
        device_ids = [int(d) for d in body.get("device_ids", [])]
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
        return jsonify({"status": "ok", "count": len(valid_items), "skipped": skipped})
    finally:
        conn.close()


@app.route("/api/basket/last-destination", methods=["PATCH"])
def api_basket_last_destination():
    """Persists "device_ids last chosen for a pick from this surface" —
    the device picker's cheap smart-default (#303). Called by both the
    pre-existing single-item picker and the basket's own fan-out step
    ('basket' is just another surface name)."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        body = request.get_json(force=True)
        surface = body["surface"]
        device_ids = [int(d) for d in body.get("device_ids", [])]
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
        return jsonify({"status": "ok"})
    finally:
        conn.close()


# Widget ids a non-admin must never see as enabled, kept in sync by hand
# with the frontend catalog's adminOnly flags (templates/index.html,
# dashboardWidgetCatalog). Belt-and-suspenders, not a real privilege
# boundary — the Administration card is only ever a shortcut/preview, the
# actual admin routes are already separately gated by require_admin/
# is_admin elsewhere, so this only prevents a UI inconsistency.
ADMIN_ONLY_WIDGETS = {"administration"}

# #269: the home cover grid is grid-cols-3 sm:grid-cols-4 md:grid-cols-5 —
# only multiples of 15 fill complete rows at both the 3-col mobile and 5-col
# desktop widths (60 also being clean at the 4-col tablet width, as LCM(3,4,5)).
# A curated set, not a free integer, so a user can't pick a count that leaves
# a ragged row — and the server clamps to it too, since settings.cover_limit
# arrives over the API and a client isn't the only way to set it.
DASHBOARD_COVER_LIMITS = (15, 30, 45, 60)
DEFAULT_COVER_LIMIT = 15


def _normalize_dashboard_widgets(parsed) -> dict:
    """Sanitizes an already-parsed dashboard_widgets value (dict, None, or
    any other JSON-decoded shape), tolerating garbage by falling back to
    "nothing disabled, no per-widget settings, no saved order" — same shape
    a brand-new user gets."""
    default: dict = {"disabled": [], "order": [], "settings": {"cover_limit": DEFAULT_COVER_LIMIT}}
    if not isinstance(parsed, dict):
        return default
    disabled = parsed.get("disabled")
    order = parsed.get("order")
    settings = parsed.get("settings")
    settings = dict(settings) if isinstance(settings, dict) else {}
    if settings.get("cover_limit") not in DASHBOARD_COVER_LIMITS:
        settings["cover_limit"] = DEFAULT_COVER_LIMIT
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


def _profile_dict(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        "SELECT username, lastfm_username, lastfm_api_key, listenbrainz_username, "
        "cover_view_mode, show_reissue_year, dashboard_widgets, basket_last_destinations, "
        "hide_zero_match_playlists, "
        "is_admin, email, avatar_path, tidal_refresh_token, tidal_display_name, "
        "spotify_refresh_token, spotify_display_name FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    result = dict(row)
    # A real JSON boolean, not the raw 0/1 SQLite stores — same treatment
    # as mirror_enabled/mirror_folder_configured elsewhere in this file.
    result["hide_zero_match_playlists"] = bool(result["hide_zero_match_playlists"])
    result["dashboard_widgets"] = _dashboard_widgets_dict(result["dashboard_widgets"])
    result["basket_last_destinations"] = _basket_last_destinations_dict(
        result["basket_last_destinations"])
    avatar_path = result.pop("avatar_path")
    email = result.pop("email")
    # The refresh token itself is never sent to the frontend — connected
    # status + display name is all the UI needs (see /profile/tidal/* below).
    result["tidal_connected"] = result.pop("tidal_refresh_token") is not None
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
    is_local_session = session.get("local_user_id") is not None
    result["is_local_session"] = is_local_session
    # #235: the web UI needs this to decide whether/where to show a local-
    # password form at all (Profile → Account under local/forward, moved to
    # Administration and admin-only under oidc, replaced by an "account is
    # managed externally" notice for non-admins under oidc) — not a secret,
    # just the deployment's auth mode and (when relevant) its IdP's own URL.
    result["auth_mode"] = AUTH_MODE
    result["oidc_issuer"] = OIDC_ISSUER if AUTH_MODE == "oidc" and OIDC_ISSUER else None
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


@app.route("/api/profile", methods=["GET", "PUT"])
def api_profile():
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        if request.method == "PUT":
            body = request.get_json(force=True)
            widgets = _normalize_dashboard_widgets(body.get("dashboard_widgets"))
            if not _is_admin(conn, user_id):
                # A non-admin's own request can only ever ask to hide an
                # admin-only widget further, never un-hide one — see
                # ADMIN_ONLY_WIDGETS.
                widgets["disabled"] = sorted(set(widgets["disabled"]) | ADMIN_ONLY_WIDGETS)
            conn.execute(
                "UPDATE users SET lastfm_username = ?, lastfm_api_key = ?, listenbrainz_username = ?, "
                "cover_view_mode = ?, show_reissue_year = ?, dashboard_widgets = ?, "
                "hide_zero_match_playlists = ? WHERE id = ?",
                (body.get("lastfm_username") or None, body.get("lastfm_api_key") or None,
                 body.get("listenbrainz_username") or None,
                 body.get("cover_view_mode", "list"), 1 if body.get("show_reissue_year") else 0,
                 json.dumps(widgets), 1 if body.get("hide_zero_match_playlists") else 0, user_id),
            )
            conn.commit()
        return jsonify(_profile_dict(conn, user_id))
    finally:
        conn.close()


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
    if "tidal" not in oauth._clients:
        oauth.register(
            name="tidal",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url=tidal_client.AUTHORIZE_URL,
            access_token_url=tidal_client.TOKEN_URL,
            # PKCE (S256) alongside the client secret — same defense-in-depth
            # posture as the OIDC registration above.
            client_kwargs={"scope": tidal_client.SCOPES, "code_challenge_method": "S256"},
        )
    return oauth.tidal


@app.route("/profile/tidal/connect")
def tidal_connect():
    """Kicks off the per-user Tidal link (#21) — see tidal_client.py's
    module docstring for why this is a per-user OAuth flow rather than an
    admin-configured shared connection like Roon/Subsonic/Jellyfin. Requires
    an existing Trobar session; the linked account then belongs to whichever
    Trobar user was logged in when they clicked "Connect"."""
    conn = db.get_conn()
    try:
        get_current_user_id(conn)  # 401s here if not logged in, before redirecting anywhere
        client = _tidal_oauth_client(conn)
        if client is None:
            abort(400, description=_("Tidal isn't configured yet — ask an admin to set it up in Administration."))
        return client.authorize_redirect(url_for("tidal_callback", _external=True))
    finally:
        conn.close()


@app.route("/profile/tidal/callback")
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
    conn = db.get_conn()
    try:
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
        if not access_token or not refresh_token:
            return redirect(url_for("index") + "?tidal_error=1#/profile")
        info = tidal_client.get_current_user(access_token)
        if info["status"] != "ok":
            return redirect(url_for("index") + "?tidal_error=1#/profile")
        conn.execute(
            "UPDATE users SET tidal_refresh_token = ?, tidal_user_id = ?, tidal_display_name = ? WHERE id = ?",
            (refresh_token, info["user_id"], info["display_name"], user_id),
        )
        conn.commit()
        return redirect(url_for("index") + "#/profile")
    finally:
        conn.close()


@app.route("/api/profile/tidal", methods=["DELETE"])
def api_profile_tidal_disconnect():
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        conn.execute(
            "UPDATE users SET tidal_refresh_token = NULL, tidal_user_id = NULL, tidal_display_name = NULL "
            "WHERE id = ?",
            (user_id,),
        )
        conn.commit()
        return jsonify(_profile_dict(conn, user_id))
    finally:
        conn.close()


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


def _spotify_experimental_enabled(conn) -> bool:
    """#398: gated separately from _spotify_oauth_client's configured-or-not
    check. Credentials can remain set (an admin toggling this off to pause
    the feature shouldn't have to also discard client_id/secret) while the
    feature itself stays off -- so "configured" and "enabled" are two
    different questions, each with its own guard below."""
    return db.get_config(conn, "experimental_spotify_enabled") == "1"


@app.route("/profile/spotify/connect")
def spotify_connect():
    """Kicks off the per-user Spotify link (#10 Part B) — see
    spotify_client.py for why this is a per-user OAuth flow rather than an
    admin-configured shared connection. Requires an existing Trobar session;
    the linked account belongs to whichever Trobar user clicked "Connect"."""
    conn = db.get_conn()
    try:
        get_current_user_id(conn)  # 401s here if not logged in, before redirecting anywhere
        # #398: refuse even if credentials happen to be configured -- a
        # bookmarked/typed URL must not be a back door around the toggle
        # the Profile UI hides this behind.
        if not _spotify_experimental_enabled(conn):
            abort(400, description=_("Spotify is an experimental feature and isn't enabled on this server."))
        client = _spotify_oauth_client(conn)
        if client is None:
            abort(400, description=_("Spotify isn't configured yet — ask an admin to set it up in Administration."))
        return client.authorize_redirect(url_for("spotify_callback", _external=True))
    finally:
        conn.close()


@app.route("/profile/spotify/callback")
def spotify_callback():
    """Spotify's redirect target. Exchanges the code, fetches the linked
    account's own id/display name once (get_current_user), and stores both on
    whichever Trobar user's session started the flow. Error redirects put the
    query param BEFORE the `#` (`?spotify_error=1#/profile`) — the frontend's
    hash-router splits the fragment on `/`, so anything after `#/profile` would
    corrupt that parse."""
    conn = db.get_conn()
    try:
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
    finally:
        conn.close()


@app.route("/api/profile/spotify", methods=["DELETE"])
def api_profile_spotify_disconnect():
    conn = db.get_conn()
    try:
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
    finally:
        conn.close()


@app.route("/api/profile/avatar", methods=["POST", "DELETE"])
def api_profile_avatar():
    """Available regardless of session type (follow-up) — Authentik
    itself has no self-service avatar upload in this version at all (its
    `avatar` API field is read-only), so this is the only way an Authentik-
    authenticated household member gets a custom picture without an admin
    hand-editing their Authentik attributes. An upload here always takes
    priority over the Gravatar fallback; see _profile_dict."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        row = conn.execute("SELECT avatar_path FROM users WHERE id = ?", (user_id,)).fetchone()

        if request.method == "DELETE":
            if row["avatar_path"]:
                (AVATAR_DIR / row["avatar_path"]).unlink(missing_ok=True)
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
            (AVATAR_DIR / row["avatar_path"]).unlink(missing_ok=True)
        filename = f"{user_id}.{ext}"
        (AVATAR_DIR / filename).write_bytes(data)
        conn.execute("UPDATE users SET avatar_path = ? WHERE id = ?", (filename, user_id))
        conn.commit()
        return jsonify(_profile_dict(conn, user_id))
    finally:
        conn.close()


@app.route("/api/profile/avatar-image")
def api_profile_avatar_image():
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        row = conn.execute("SELECT avatar_path FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row["avatar_path"]:
        abort(404)
    path = AVATAR_DIR / row["avatar_path"]
    if not path.exists():
        abort(404)
    content_type = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(path.suffix.lstrip("."))
    return send_file(path, mimetype=content_type)


@app.route("/api/lastfm/status")
def api_lastfm_status():
    """Header status-dot backing — separate from /api/suggestions since that
    endpoint can legitimately return [] for reasons that have nothing to do
    with whether the Last.fm connection itself is healthy (nothing currently
    qualifies vs. the account/key actually being broken)."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        user = conn.execute(
            "SELECT lastfm_username, lastfm_api_key FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user["lastfm_username"]:
            return jsonify({"configured": False, "ok": False})
        api_key = user["lastfm_api_key"] or db.get_config(conn, "lastfm_api_key_default") or ""
        api_base = db.get_config(conn, "lastfm_api_base") or ""
        return jsonify({"configured": True, "ok": lastfm.check_connection(user["lastfm_username"], api_key, api_base)})
    finally:
        conn.close()


@app.route("/api/listenbrainz/status")
def api_listenbrainz_status():
    """ListenBrainz counterpart of /api/lastfm/status — same
    configured/ok split for the header badge, no API key involved."""
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        user = conn.execute(
            "SELECT listenbrainz_username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user["listenbrainz_username"]:
            return jsonify({"configured": False, "ok": False})
        api_base = db.get_config(conn, "listenbrainz_api_base") or ""
        return jsonify({"configured": True, "ok": listenbrainz.check_connection(user["listenbrainz_username"], api_base)})
    finally:
        conn.close()


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


@app.route("/api/suggestions")
def api_suggestions():
    """Merged suggestion feed (#30): recently-added-to-library
    (local scan, no Last.fm needed) first, then Last.fm top-played and
    recently-played if the user has a Last.fm username configured. Deduped
    across sources by (artist, album) — priority order above decides which
    source's copy (and thus which provider's art) wins for an album that
    shows up in more than one."""
    period = _validated_period(request.args.get("period", "6month"))
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        user = conn.execute(
            "SELECT lastfm_username, lastfm_api_key, listenbrainz_username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        # Per-user key first, then the admin-configured app-wide default
        # (lastfm.top_albums also has its own LASTFM_API_KEY env var as a
        # last resort, for deployments that haven't set either yet).
        api_key = user["lastfm_api_key"] or db.get_config(conn, "lastfm_api_key_default") or ""
        lastfm_base = db.get_config(conn, "lastfm_api_base") or ""
        listenbrainz_base = db.get_config(conn, "listenbrainz_api_base") or ""
        user_device_ids = {row["id"] for row in _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))}

        # Fetch enough candidates per source that filtering-to-one-source in
        # the UI can still show a full 30 of it, not just whatever survived
        # from a mixed-default-sized pool (follow-up).
        combined = suggestions.recently_added(conn, user_device_ids, limit=30)
        if user["lastfm_username"]:
            combined += lastfm.suggestions(
                conn, user["lastfm_username"], api_key, period, limit=200, user_device_ids=user_device_ids,
                api_base=lastfm_base,
            )
            combined += lastfm.recently_played_suggestions(
                conn, user["lastfm_username"], api_key, limit=200, user_device_ids=user_device_ids,
                api_base=lastfm_base,
            )
        # Both services can be configured at once — the feed just gains more
        # sources and the dedup below keeps one copy per album.
        if user["listenbrainz_username"]:
            combined += listenbrainz.suggestions(
                conn, user["listenbrainz_username"],
                _LASTFM_PERIOD_TO_LISTENBRAINZ_RANGE[period], limit=200, user_device_ids=user_device_ids,
                api_base=listenbrainz_base,
            )
            combined += listenbrainz.recently_played_suggestions(
                conn, user["listenbrainz_username"], limit=100, user_device_ids=user_device_ids,
                api_base=listenbrainz_base,
            )

        seen = set()
        deduped = []
        for s in combined:
            key = (s["artist"].lower(), s["album"].lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(s)
        return jsonify(deduped)
    finally:
        conn.close()


@app.route("/api/suggestions/most-played")
def api_most_played():
    """#267: a ranked, un-shuffled most-played chart — the user's actual
    Last.fm/ListenBrainz listening, not filtered to the local library or
    what's not-yet-synced like /api/suggestions is (a different intent:
    "what do I listen to" vs. "what should I sync"). Same per-user
    config as /api/suggestions; [] if neither service is set up."""
    period = _validated_period(request.args.get("period", "6month"))
    try:
        limit = max(1, min(int(request.args.get("limit", 8)), 50))
    except (TypeError, ValueError):
        limit = 8
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        user = conn.execute(
            "SELECT lastfm_username, lastfm_api_key, listenbrainz_username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        api_key = user["lastfm_api_key"] or db.get_config(conn, "lastfm_api_key_default") or ""
        lastfm_base = db.get_config(conn, "lastfm_api_base") or ""
        listenbrainz_base = db.get_config(conn, "listenbrainz_api_base") or ""

        combined = []
        if user["lastfm_username"]:
            combined += lastfm.most_played(user["lastfm_username"], api_key, period, limit=50, api_base=lastfm_base)
        if user["listenbrainz_username"]:
            combined += listenbrainz.most_played(
                user["listenbrainz_username"], _LASTFM_PERIOD_TO_LISTENBRAINZ_RANGE[period],
                limit=50, api_base=listenbrainz_base,
            )

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
        return jsonify(deduped[:limit])
    finally:
        conn.close()


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


@app.route("/api/library/recently-added")
def api_library_recently_added():
    """Home dashboard's Recently Added widget — distinct from the
    Suggestions tab's own fixed top-N recently-added section (see
    suggestions.recently_added_widget's docstring)."""
    months = request.args.get("months", 3, type=int)
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        user_device_ids = {row["id"] for row in _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))}
        out = suggestions.recently_added_widget(conn, _months_ago_iso(months), user_device_ids)
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/library/recently-released")
def api_library_recently_released():
    """Home dashboard's Recently Released widget — new to the world
    (tag-derived release_date), independent of when the scanner saw the
    file (that's Recently Added, above)."""
    months = request.args.get("months", 3, type=int)
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        user_device_ids = {row["id"] for row in _device_rows_for_user(conn, user_id, _is_admin(conn, user_id))}
        out = suggestions.recently_released_widget(conn, _months_ago_iso(months), user_device_ids)
        return jsonify(out)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# First-run setup wizard — AUTH_MODE=local only, see
# _require_setup_wizard above for why Authentik mode doesn't need this.
# ---------------------------------------------------------------------------

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


@app.route("/setup")
def setup_wizard():
    conn = db.get_conn()
    try:
        if AUTH_MODE != "local" or not _is_admin(conn, get_current_user_id(conn)):
            return redirect(url_for("index"))
    finally:
        conn.close()
    return render_template("setup.html", js_i18n=_build_setup_js_i18n())


@app.route("/api/setup/status")
def api_setup_status():
    conn = db.get_conn()
    try:
        require_admin(conn)
        return jsonify({
            "completed": bool(db.get_config(conn, "setup_completed")),
            "music_root": str(db.get_music_root()),
            "music_root_writable": _music_root_is_readonly() is False,
        })
    finally:
        conn.close()


@app.route("/api/setup/music-root", methods=["POST"])
def api_setup_music_root():
    conn = db.get_conn()
    try:
        require_admin(conn)
        body = request.get_json(force=True)
        path_str = (body.get("path") or "").strip()
        if not path_str or not Path(path_str).is_dir():
            return jsonify({"error": _("This folder doesn't exist or isn't readable by the server.")}), 400
        db.set_config(conn, "music_root", path_str)
        conn.commit()
        # flag a writable mount so the wizard can warn right here.
        return jsonify({
            "music_root": path_str,
            "music_root_writable": _music_root_is_readonly(Path(path_str)) is False,
        })
    finally:
        conn.close()


@app.route("/api/setup/complete", methods=["POST"])
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
    conn = db.get_conn()
    try:
        require_admin(conn)
        root = db.get_music_root()
        if not root.is_dir():
            # Not committed, so the wizard can legitimately stay put.
            return jsonify({"error": _(
                "Can't read the music folder %(path)s — check the path and the "
                "container's volume mount, then try again.", path=str(root))}), 400
        db.set_config(conn, "setup_completed", "1")
        conn.commit()
    finally:
        conn.close()

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
        app.logger.exception("could not start the first library scan")
    return jsonify({"scan_started": scan_started})


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
        f"[main] WARNING: DATA_DIR ({db.DATA_DIR}) is on a network filesystem "
        f"({fs_type}). SQLite needs working file locking, which network shares don't "
        "reliably provide — this CAN CORRUPT your database and lose your selections, "
        "device pairings and playlists. Move DATA_DIR to local disk on the machine "
        "running the server. A network-mounted MUSIC_ROOT is fine (Trobar only reads "
        "it); if you want this data on your NAS, back it up there rather than running "
        "it from there. See docs/getting-started/installation.md."
    )


def _is_valid_url(url: str) -> bool:
    """#509: a malformed URL in any of the admin config's provider fields
    (subsonic_url, jellyfin_url, emby_url, plex_url, lms_url,
    mirror_subsonic_url, mirror_jellyfin_url, mirror_emby_url, lidarr_url,
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


@app.route("/api/admin/config", methods=["GET", "PUT"])
def api_admin_config():
    """App-wide settings (active provider + its connection, default Last.fm
    key) — restricted to ADMIN_USERNAME, unlike everything else in this app
    which any logged-in household member can see/change. Provider-agnostic
    key names (roon_host/roon_port, subsonic_url/subsonic_username/
    subsonic_password, jellyfin_url/jellyfin_api_key/jellyfin_username,
    emby_url/emby_api_key/emby_username, plex_url/plex_token,
    lms_url/lms_username/lms_password) — "one or the other",
    never more than one active simultaneously.
    filesystem_client's music root itself is set via the setup wizard's own
    dedicated /api/setup/music-root step, not here — its status is still
    echoed back here so the admin UI can show the root path and paired
    state. It does have one editable field through this endpoint though:
    itunes_library_path (#171), an optional import source layered on top of
    the filesystem provider rather than a config of its own."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        if request.method == "PUT":
            body = request.get_json(force=True)

            new_provider = body.get("provider")
            if new_provider in _PROVIDERS:
                old_provider = db.get_config(conn, "provider") or "roon"
                if new_provider != old_provider:
                    # Clean slate on switch — the previous provider's
                    # playlist rows would otherwise look like stale/wrong
                    # data under the new provider (title-keyed table is now
                    # shared), and cached artist images are tied to
                    # whichever provider supplied them, not to the artist
                    # name alone.
                    conn.execute("DELETE FROM playlist_tracks")
                    conn.execute("DELETE FROM playlists")
                    shutil.rmtree(artist_images.CACHE_DIR, ignore_errors=True)
                    db.set_config(conn, "artist_images_epoch", str(int(time.time())))
                db.set_config(conn, "provider", new_provider)
                conn.commit()

            host = (body.get("roon_host") or "").strip()
            port = body.get("roon_port")
            if host and port:
                roon_client.reconnect(host, int(port))

            subsonic_url = (body.get("subsonic_url") or "").strip()
            subsonic_username = (body.get("subsonic_username") or "").strip()
            subsonic_password = body.get("subsonic_password")
            if subsonic_url and subsonic_username and subsonic_password:
                if not _is_valid_url(subsonic_url):
                    abort(400, description=_invalid_url_message("Subsonic URL"))
                subsonic_client.reconnect(subsonic_url, subsonic_username, subsonic_password)

            jellyfin_url = (body.get("jellyfin_url") or "").strip()
            jellyfin_api_key = (body.get("jellyfin_api_key") or "").strip()
            jellyfin_username = (body.get("jellyfin_username") or "").strip()
            if jellyfin_url and jellyfin_api_key and jellyfin_username:
                if not _is_valid_url(jellyfin_url):
                    abort(400, description=_invalid_url_message("Jellyfin URL"))
                jellyfin_client.reconnect(jellyfin_url, jellyfin_api_key, jellyfin_username)

            # #168: Emby — Jellyfin's upstream, same config shape (url +
            # api key + a username resolved to an internal userId).
            emby_url = (body.get("emby_url") or "").strip()
            emby_api_key = (body.get("emby_api_key") or "").strip()
            emby_username = (body.get("emby_username") or "").strip()
            if emby_url and emby_api_key and emby_username:
                if not _is_valid_url(emby_url):
                    abort(400, description=_invalid_url_message("Emby URL"))
                emby_client.reconnect(emby_url, emby_api_key, emby_username)

            # #158: no username, unlike Jellyfin/Emby above — the token is
            # already scoped to one Plex account.
            plex_url = (body.get("plex_url") or "").strip()
            plex_token = (body.get("plex_token") or "").strip()
            if plex_url and plex_token:
                if not _is_valid_url(plex_url):
                    abort(400, description=_invalid_url_message("Plex URL"))
                plex_client.reconnect(plex_url, plex_token)

            # #172: username/password optional — only sent as Basic Auth
            # when LMS's own "Authorize" setting is on (off by default).
            # reconnect() persists blank username/password fine, so this
            # only requires the URL, unlike every other server-provider
            # above.
            lms_url = (body.get("lms_url") or "").strip()
            if lms_url:
                if not _is_valid_url(lms_url):
                    abort(400, description=_invalid_url_message("LMS URL"))
                lms_client.reconnect(lms_url, (body.get("lms_username") or "").strip(),
                                      body.get("lms_password") or "")

            if "lastfm_api_key_default" in body:
                db.set_config(conn, "lastfm_api_key_default", body.get("lastfm_api_key_default") or None)
                conn.commit()

            # TheAudioDB key makes the cleanly-licensed source
            # the primary for artist images; clearing it falls back to the
            # provider-first behaviour. Cache cleared on change so already-
            # cached provider images get re-fetched from the better source.
            if "audiodb_api_key" in body:
                old_key = db.get_config(conn, "audiodb_api_key")
                new_key = body.get("audiodb_api_key") or None
                if new_key != old_key:
                    shutil.rmtree(artist_images.CACHE_DIR, ignore_errors=True)
                    db.set_config(conn, "artist_images_epoch", str(int(time.time())))
                db.set_config(conn, "audiodb_api_key", new_key)
                conn.commit()

            # #200: opt-in AcoustID key for fingerprint.py's local-library
            # ISRC backfill (scan-triggered, see scanner.start_scan). Blank
            # = skip fingerprinting entirely, same "opt-in, blank = skip"
            # shape as the other provider keys here — no cache to
            # invalidate on change, unlike audiodb_api_key above.
            if "acoustid_api_key" in body:
                db.set_config(conn, "acoustid_api_key", (body.get("acoustid_api_key") or "").strip() or None)
                conn.commit()

            # Point suggestions/listening-history reads at a self-hosted
            # alternative (Libre.fm, self-hosted ListenBrainz) instead of
            # the real services — blank means "use the default" (the
            # LASTFM_API_BASE/LISTENBRAINZ_API_BASE env var if set, else the
            # real service). See lastfm.API_BASE / listenbrainz.API_BASE.
            if "lastfm_api_base" in body:
                lastfm_api_base = (body.get("lastfm_api_base") or "").strip()
                if lastfm_api_base and not _is_valid_url(lastfm_api_base):
                    abort(400, description=_invalid_url_message("Last.fm API base URL"))
                db.set_config(conn, "lastfm_api_base", lastfm_api_base or None)
                conn.commit()
            if "listenbrainz_api_base" in body:
                listenbrainz_api_base = (body.get("listenbrainz_api_base") or "").strip()
                if listenbrainz_api_base and not _is_valid_url(listenbrainz_api_base):
                    abort(400, description=_invalid_url_message("ListenBrainz API base URL"))
                db.set_config(conn, "listenbrainz_api_base", listenbrainz_api_base or None)
                conn.commit()

            # #21's direct Tidal provider: one admin-registered OAuth app
            # (developer.tidal.com), each Trobar user then links their own
            # Tidal account against it (Profile > "Streaming accounts").
            # Evict the cached Authlib client on change — see
            # _tidal_oauth_client's docstring for why re-register() alone
            # wouldn't pick this up without a restart.
            if "tidal_client_id" in body or "tidal_client_secret" in body:
                old_id = db.get_config(conn, "tidal_client_id")
                old_secret = db.get_config(conn, "tidal_client_secret")
                new_id = (body.get("tidal_client_id", old_id) or "").strip() or None
                new_secret = (body.get("tidal_client_secret", old_secret) or "").strip() or None
                if (new_id, new_secret) != (old_id, old_secret):
                    oauth._clients.pop("tidal", None)
                db.set_config(conn, "tidal_client_id", new_id)
                db.set_config(conn, "tidal_client_secret", new_secret)
                conn.commit()

            if "spotify_client_id" in body or "spotify_client_secret" in body:
                old_id = db.get_config(conn, "spotify_client_id")
                old_secret = db.get_config(conn, "spotify_client_secret")
                new_id = (body.get("spotify_client_id", old_id) or "").strip() or None
                new_secret = (body.get("spotify_client_secret", old_secret) or "").strip() or None
                if (new_id, new_secret) != (old_id, old_secret):
                    oauth._clients.pop("spotify", None)  # #10: pick up new creds without a restart
                db.set_config(conn, "spotify_client_id", new_id)
                db.set_config(conn, "spotify_client_secret", new_secret)
                conn.commit()

            # #398: an explicit admin decision, independent of whether
            # credentials happen to be set -- toggling this off pauses the
            # feature (routes refuse, Profile hides the block) without
            # discarding spotify_client_id/secret, so re-enabling later
            # needs no re-entry.
            if "experimental_spotify_enabled" in body:
                db.set_config(
                    conn, "experimental_spotify_enabled",
                    "1" if body.get("experimental_spotify_enabled") else "0")
                conn.commit()

            # How many tracks may transcode at once, and how much CPU
            # priority ffmpeg gets — resource hygiene, not crash
            # prevention, so the admin decides rather than a hardcoded
            # constant (see transcode.py).
            if "transcode_concurrency" in body:
                try:
                    val = max(1, int(body.get("transcode_concurrency")))
                except (TypeError, ValueError):
                    return jsonify({"error": _("Concurrency must be a whole number of 1 or more.")}), 400
                db.set_config(conn, "transcode_concurrency", str(val))
                conn.commit()

            if "transcode_nice_level" in body:
                try:
                    val = int(body.get("transcode_nice_level"))
                except (TypeError, ValueError):
                    return jsonify({"error": _("Nice level must be a whole number.")}), 400
                if not (0 <= val <= 19):
                    return jsonify({"error": _("Nice level must be between 0 and 19.")}), 400
                db.set_config(conn, "transcode_nice_level", str(val))
                conn.commit()

            # #361: how long finished background jobs stay in full before
            # collapsing to one row per type/outcome — see jobs._prune_finished.
            if "job_retention_days" in body:
                try:
                    val = int(body.get("job_retention_days"))
                except (TypeError, ValueError):
                    return jsonify({"error": _("Retention must be a whole number of days.")}), 400
                if not (1 <= val <= 3650):
                    return jsonify({"error": _("Retention must be between 1 and 3650 days.")}), 400
                db.set_config(conn, "job_retention_days", str(val))
                conn.commit()

            # #362: 0 (the default) means off — a fresh install behaves
            # exactly as it did before this existed, per the issue's explicit
            # "defaulting to off" decision. See scanner.maybe_schedule_rescan.
            if "scan_interval_hours" in body:
                try:
                    val = int(body.get("scan_interval_hours"))
                except (TypeError, ValueError):
                    return jsonify({"error": _("Scan interval must be a whole number of hours.")}), 400
                if not (0 <= val <= 8760):  # 8760h = 1 year; 0 = off
                    return jsonify({"error": _("Scan interval must be between 0 (off) and 8760 hours.")}), 400
                db.set_config(conn, "scan_interval_hours", str(val))
                conn.commit()

            # #171: an optional path to an exported iTunes/Apple Music
            # Library.xml, merged into filesystem_client's own playlist
            # listing alongside its .m3u discovery — see
            # filesystem_client._itunes_library_path. Not gated on the
            # active provider being filesystem (same as every other
            # field here), since it only takes effect while it is.
            if "itunes_library_path" in body:
                db.set_config(conn, "itunes_library_path", (body.get("itunes_library_path") or "").strip() or None)
                conn.commit()

            # #285: opt-in playlist-mirroring output folder. Must be
            # outside MUSIC_ROOT — Trobar never writes to the library, and
            # filesystem_client.py's own .m3u discovery walks the whole of
            # MUSIC_ROOT with no exclusion mechanism, so a mirror written
            # inside it would be picked back up as a new source playlist
            # on the very next sync.
            if "mirror_folder" in body:
                new_folder = (body.get("mirror_folder") or "").strip()
                if new_folder:
                    # #294: normalize before storing (and before the
                    # MUSIC_ROOT containment check below) so an admin-typed
                    # ".." can't evade either check — mirror.py's own
                    # _safe_path() compares against this stored value
                    # unresolved, so a raw ".." saved here broke every
                    # write, and lexical Path.parents comparisons below
                    # don't collapse ".." either.
                    new_folder = os.path.normpath(new_folder)
                    music_root = db.get_music_root()
                    candidate = Path(new_folder)
                    if candidate == music_root or music_root in candidate.parents:
                        abort(400, description=_(
                            "The mirror folder can't be inside your music library "
                            "(MUSIC_ROOT) — choose a separate folder."))
                db.set_config(conn, "mirror_folder", new_folder or None)
                conn.commit()

            # #189: the Subsonic mirror-TARGET connection — a distinct
            # write destination from subsonic_url/username/password above
            # (that triple is the active read-source; see
            # db.get_mirror_subsonic_config()'s docstring). Same
            # all-three-or-nothing shape as every other provider block
            # here.
            mirror_subsonic_url = (body.get("mirror_subsonic_url") or "").strip()
            mirror_subsonic_username = (body.get("mirror_subsonic_username") or "").strip()
            mirror_subsonic_password = body.get("mirror_subsonic_password") or ""
            if mirror_subsonic_url and mirror_subsonic_username and mirror_subsonic_password:
                if not _is_valid_url(mirror_subsonic_url):
                    abort(400, description=_invalid_url_message("Subsonic mirror-target URL"))
                subsonic_client.mirror_reconnect(
                    mirror_subsonic_url, mirror_subsonic_username, mirror_subsonic_password)
            elif "mirror_subsonic_url" in body and not mirror_subsonic_url \
                    and not mirror_subsonic_username and not mirror_subsonic_password:
                # #189 review: unlike every other provider-connection
                # triple in this route, this one has no "switch away"
                # mechanism -- a read-source's unused credentials just go
                # inert when a different provider is picked, but this
                # write target stays authoritative for every
                # subsonic_mirror_enabled playlist until explicitly
                # cleared. Gated on the key's presence (unlike the connect
                # branch above) so a partial payload that never mentions
                # this triple can't accidentally wipe it.
                db.set_config(conn, "mirror_subsonic_url", None)
                db.set_config(conn, "mirror_subsonic_username", None)
                db.set_config(conn, "mirror_subsonic_password", None)
                conn.commit()

            # #189: the Jellyfin mirror-TARGET connection — same shape and
            # same reasoning as the Subsonic block just above (a distinct
            # write destination from jellyfin_url/api_key/username below,
            # explicit clear path since this sink has no "switch away"
            # mechanism).
            mirror_jellyfin_url = (body.get("mirror_jellyfin_url") or "").strip()
            mirror_jellyfin_api_key = (body.get("mirror_jellyfin_api_key") or "").strip()
            mirror_jellyfin_username = (body.get("mirror_jellyfin_username") or "").strip()
            if mirror_jellyfin_url and mirror_jellyfin_api_key and mirror_jellyfin_username:
                if not _is_valid_url(mirror_jellyfin_url):
                    abort(400, description=_invalid_url_message("Jellyfin mirror-target URL"))
                jellyfin_client.mirror_reconnect(
                    mirror_jellyfin_url, mirror_jellyfin_api_key, mirror_jellyfin_username)
            elif "mirror_jellyfin_url" in body and not mirror_jellyfin_url \
                    and not mirror_jellyfin_api_key and not mirror_jellyfin_username:
                db.set_config(conn, "mirror_jellyfin_url", None)
                db.set_config(conn, "mirror_jellyfin_api_key", None)
                db.set_config(conn, "mirror_jellyfin_username", None)
                db.set_config(conn, "mirror_jellyfin_user_id", None)
                conn.commit()

            # #189: the Emby mirror-TARGET connection — same shape and same
            # reasoning as the Subsonic/Jellyfin blocks just above (a
            # distinct write destination from emby_url/api_key/username
            # below, explicit clear path since this sink has no "switch
            # away" mechanism).
            mirror_emby_url = (body.get("mirror_emby_url") or "").strip()
            mirror_emby_api_key = (body.get("mirror_emby_api_key") or "").strip()
            mirror_emby_username = (body.get("mirror_emby_username") or "").strip()
            if mirror_emby_url and mirror_emby_api_key and mirror_emby_username:
                if not _is_valid_url(mirror_emby_url):
                    abort(400, description=_invalid_url_message("Emby mirror-target URL"))
                emby_client.mirror_reconnect(
                    mirror_emby_url, mirror_emby_api_key, mirror_emby_username)
            elif "mirror_emby_url" in body and not mirror_emby_url \
                    and not mirror_emby_api_key and not mirror_emby_username:
                db.set_config(conn, "mirror_emby_url", None)
                db.set_config(conn, "mirror_emby_api_key", None)
                db.set_config(conn, "mirror_emby_username", None)
                db.set_config(conn, "mirror_emby_user_id", None)
                conn.commit()

            # #494: the Lidarr connection for "Request missing albums" —
            # not a mirror target (nothing is copied to it), but the same
            # explicit-clear-on-blank shape. Two INDEPENDENT gated blocks,
            # not one: url+api_key must be saved and live BEFORE the admin
            # can even see what root folders/quality profiles/metadata
            # profiles exist to choose from (GET /api/admin/lidarr-options
            # needs a working connection) — there is no single "all fields
            # at once" moment here the way there is for every other
            # provider's connection.
            lidarr_url = (body.get("lidarr_url") or "").strip()
            lidarr_api_key = (body.get("lidarr_api_key") or "").strip()
            if lidarr_url and lidarr_api_key:
                if not _is_valid_url(lidarr_url):
                    abort(400, description=_invalid_url_message("Lidarr URL"))
                lidarr_client.reconnect(lidarr_url, lidarr_api_key)
            elif "lidarr_url" in body and not lidarr_url and not lidarr_api_key:
                db.set_config(conn, "lidarr_url", None)
                db.set_config(conn, "lidarr_api_key", None)
                # Clearing the connection also invalidates the three
                # profile fields below — they're ids/paths specific to
                # THIS Lidarr instance and would mean nothing (or worse,
                # something wrong) against a different one.
                db.set_config(conn, "lidarr_root_folder_path", None)
                db.set_config(conn, "lidarr_quality_profile_id", None)
                db.set_config(conn, "lidarr_metadata_profile_id", None)
                conn.commit()

            lidarr_root_folder_path = (body.get("lidarr_root_folder_path") or "").strip()
            lidarr_quality_profile_id = body.get("lidarr_quality_profile_id")
            lidarr_metadata_profile_id = body.get("lidarr_metadata_profile_id")
            if lidarr_root_folder_path and lidarr_quality_profile_id and lidarr_metadata_profile_id:
                try:
                    quality_id = int(lidarr_quality_profile_id)
                    metadata_id = int(lidarr_metadata_profile_id)
                except (TypeError, ValueError):
                    abort(400, description=_("Invalid Lidarr profile selection."))
                db.set_config(conn, "lidarr_root_folder_path", lidarr_root_folder_path)
                db.set_config(conn, "lidarr_quality_profile_id", str(quality_id))
                db.set_config(conn, "lidarr_metadata_profile_id", str(metadata_id))
                conn.commit()

        roon_status = roon_client.status()
        subsonic_status = subsonic_client.status()
        subsonic_mirror_status = subsonic_client.mirror_status()
        jellyfin_status = jellyfin_client.status()
        jellyfin_mirror_status = jellyfin_client.mirror_status()
        emby_status = emby_client.status()
        emby_mirror_status = emby_client.mirror_status()
        lidarr_status = lidarr_client.status()
        plex_status = plex_client.status()
        lms_status = lms_client.status()
        filesystem_status = filesystem_client.status()
        return jsonify({
            "provider": db.get_config(conn, "provider") or "roon",
            "roon_host": roon_status["host"],
            "roon_port": roon_status["port"],
            "subsonic_url": subsonic_status["url"],
            "subsonic_username": db.get_config(conn, "subsonic_username") or "",
            "subsonic_password": db.get_config(conn, "subsonic_password") or "",
            "jellyfin_url": jellyfin_status["url"],
            "jellyfin_api_key": db.get_config(conn, "jellyfin_api_key") or "",
            "jellyfin_username": db.get_config(conn, "jellyfin_username") or "",
            "emby_url": emby_status["url"],
            "emby_api_key": db.get_config(conn, "emby_api_key") or "",
            "emby_username": db.get_config(conn, "emby_username") or "",
            "plex_url": plex_status["url"],
            "plex_token": db.get_config(conn, "plex_token") or "",
            "lms_url": lms_status["url"],
            "lms_username": db.get_config(conn, "lms_username") or "",
            "lms_password": db.get_config(conn, "lms_password") or "",
            "filesystem_root": filesystem_status["root"],
            "itunes_library_path": db.get_config(conn, "itunes_library_path") or "",
            "mirror_folder": str(db.get_mirror_folder() or "") or "",
            # #189: echoed back the same way every other provider's
            # connection is (subsonic_url/username/password above) — the
            # admin config form can't stay pre-filled across a reload
            # otherwise.
            "mirror_subsonic_url": subsonic_mirror_status["url"],
            "mirror_subsonic_username": db.get_config(conn, "mirror_subsonic_username") or "",
            "mirror_subsonic_password": db.get_config(conn, "mirror_subsonic_password") or "",
            # #189 review: mirror_status() was computed above but the
            # state itself never left this function -- the admin had no
            # signal the target credentials actually work until a
            # per-playlist error surfaced later. "paired"/"disconnected",
            # same values as every other provider's own status.
            "mirror_subsonic_state": subsonic_mirror_status["state"],
            # #189: same reasoning as the Subsonic mirror-target trio above.
            "mirror_jellyfin_url": jellyfin_mirror_status["url"],
            "mirror_jellyfin_api_key": db.get_config(conn, "mirror_jellyfin_api_key") or "",
            "mirror_jellyfin_username": db.get_config(conn, "mirror_jellyfin_username") or "",
            "mirror_jellyfin_state": jellyfin_mirror_status["state"],
            # #189: same reasoning, for the fourth sink.
            "mirror_emby_url": emby_mirror_status["url"],
            "mirror_emby_api_key": db.get_config(conn, "mirror_emby_api_key") or "",
            "mirror_emby_username": db.get_config(conn, "mirror_emby_username") or "",
            "mirror_emby_state": emby_mirror_status["state"],
            # #494: not a mirror target — echoed the same way regardless,
            # same reasoning (the admin config form can't stay pre-filled
            # across a reload otherwise).
            "lidarr_url": lidarr_status["url"],
            "lidarr_api_key": db.get_config(conn, "lidarr_api_key") or "",
            "lidarr_state": lidarr_status["state"],
            "lidarr_root_folder_path": db.get_config(conn, "lidarr_root_folder_path") or "",
            "lidarr_quality_profile_id": db.get_config(conn, "lidarr_quality_profile_id") or "",
            "lidarr_metadata_profile_id": db.get_config(conn, "lidarr_metadata_profile_id") or "",
            "tidal_client_id": db.get_config(conn, "tidal_client_id") or "",
            "tidal_client_secret": db.get_config(conn, "tidal_client_secret") or "",
            "spotify_client_id": db.get_config(conn, "spotify_client_id") or "",
            "spotify_client_secret": db.get_config(conn, "spotify_client_secret") or "",
            "experimental_spotify_enabled": db.get_config(conn, "experimental_spotify_enabled") == "1",
            "lastfm_api_key_default": db.get_config(conn, "lastfm_api_key_default") or "",
            "audiodb_api_key": db.get_config(conn, "audiodb_api_key") or "",
            "acoustid_api_key": db.get_config(conn, "acoustid_api_key") or "",
            "lastfm_api_base": db.get_config(conn, "lastfm_api_base") or "",
            "listenbrainz_api_base": db.get_config(conn, "listenbrainz_api_base") or "",
            "transcode_concurrency": _config_int(conn, "transcode_concurrency", 1),
            "transcode_nice_level": _config_int(conn, "transcode_nice_level", 10),
            "job_retention_days": _config_int(conn, "job_retention_days", jobs.DEFAULT_JOB_RETENTION_DAYS),
            "scan_interval_hours": _config_int(conn, "scan_interval_hours", 0),
            # #362: "make the next scheduled run visible" — None when
            # scheduling is off, or when it's on but nothing has scanned yet
            # (due immediately, so there's no future timestamp to show).
            "next_scheduled_scan_at": scanner.next_scheduled_scan_at(conn),
            "auth_mode": AUTH_MODE,  # read-only display — changing it is an env var + restart, not a live toggle
            # surfaces a UI warning banner when the music library is
            # mounted writable (None/unknown is treated as not-writable so we
            # don't cry wolf before setup is done).
            "music_root_writable": _music_root_is_readonly() is False,
            # #246: under oidc, an admin's local password is the only login
            # that survives the IdP being unreachable — this drives the
            # break-glass card's warning state if none is set at all (a
            # yes/no signal, never the hash itself).
            "break_glass_set": conn.execute(
                "SELECT EXISTS(SELECT 1 FROM users WHERE is_admin = 1 AND password_hash IS NOT NULL) AS v"
            ).fetchone()["v"] == 1,
        })
    finally:
        conn.close()


# #509 item 3: (client module, test_connection's attribute NAME, field
# names, required field names) per provider key. Deliberately the module +
# an attribute name string, not a direct function reference — a direct
# reference would bind to the ORIGINAL function object at import time,
# which a test's mock.patch("subsonic_client.test_connection", ...) can
# never reach afterward (it patches the module's attribute, not whatever
# already captured the old value). getattr() at call time below always
# resolves through the module's CURRENT attribute instead.
#
# field names are positional args to test_connection, in order.
# required_field_names is a SUBSET of field_names — usually all of them,
# except LMS, whose username/password stay optional (its own "Authorize"
# setting is off by default, same as reconnect()'s own contract). One
# dispatch table rather than nine near-identical routes — same shape as
# _MIRROR_SINK_COLUMNS (#498) and _admin_provider_user_mapping (#262). A
# provider and its #189 mirror-target counterpart share the same
# test_connection function: "is this server reachable with these creds"
# doesn't depend on which config namespace the answer will end up
# persisted under, only PUT /api/admin/config's own per-block handling
# (above) does that.
_TEST_CONNECTION_PROVIDERS: dict[str, tuple[Any, str, tuple[str, ...], tuple[str, ...]]] = {
    "subsonic": (subsonic_client, "test_connection", ("url", "username", "password"),
                 ("url", "username", "password")),
    "mirror_subsonic": (subsonic_client, "test_connection", ("url", "username", "password"),
                        ("url", "username", "password")),
    "jellyfin": (jellyfin_client, "test_connection", ("url", "api_key", "username"),
                 ("url", "api_key", "username")),
    "mirror_jellyfin": (jellyfin_client, "test_connection", ("url", "api_key", "username"),
                        ("url", "api_key", "username")),
    "emby": (emby_client, "test_connection", ("url", "api_key", "username"),
             ("url", "api_key", "username")),
    "mirror_emby": (emby_client, "test_connection", ("url", "api_key", "username"),
                    ("url", "api_key", "username")),
    "plex": (plex_client, "test_connection", ("url", "token"), ("url", "token")),
    "lms": (lms_client, "test_connection", ("url", "username", "password"), ("url",)),
    "lidarr": (lidarr_client, "test_connection", ("url", "api_key"), ("url", "api_key")),
}


@app.route("/api/admin/config/test-connection", methods=["POST"])
def api_admin_config_test_connection():
    """#509 item 3: verify credentials AS TYPED, without saving them — the
    non-persisting counterpart to PUT /api/admin/config's bulk save (which
    sends the whole adminConfig object in one request; wiring a live check
    to THAT endpoint on every field blur would silently persist every
    other half-typed field on the page too). Reuses each provider's
    existing test_connection() (a thin wrapper over the same low-level
    ping/request helper status()/reconnect() already use, just against
    explicit values instead of the stored config) — this route's own job
    is only the provider dispatch and the pre-flight URL check, not any
    new connection logic.

    Response is the SAME {"state": ...} shape status()/test_connection()
    already return, plus one more state this route adds itself:
    "invalid_url" — a malformed URL short-circuits before any network
    call, reusing _is_valid_url() from #509's own earlier fix, same
    well-formedness check PUT /api/admin/config applies at save time."""
    conn = db.get_conn()
    try:
        require_admin(conn)
    finally:
        conn.close()
    body = request.get_json(force=True)
    provider = body.get("provider")
    entry = _TEST_CONNECTION_PROVIDERS.get(provider)
    if entry is None:
        abort(400, description=_("Unknown provider."))
    module, fn_name, field_names, required_field_names = entry
    values = {f: (body.get(f) or "") for f in field_names}
    if any(not values[f].strip() for f in required_field_names):
        abort(400, description=_("All fields are required to test the connection."))
    url = values["url"].strip()
    if not _is_valid_url(url):
        return jsonify({"state": "invalid_url"})
    args = [values[f].strip() if f != "password" else values[f] for f in field_names]
    args[0] = url
    return jsonify(getattr(module, fn_name)(*args))


_HEALTH_ITEM_LIMIT = 200  # cap the per-category worklist so the payload stays small


@app.route("/api/admin/health")
def api_admin_health():
    """Admin-only library-health worklist: things silently falling
    through — playlist entries that matched no local track, tracks left with the
    scanner's Unknown-Artist/Album fallback, probable duplicate files, tracks
    the fingerprint pass gave up on, and tracks AcoustID/MusicBrainz couldn't
    identify (#364). Each category reports a full count plus a capped sample
    list to act on. Also carries two non-category signals (#365): a DATA_DIR
    network-filesystem alert and the last completed scan's timestamp."""
    conn = db.get_conn()
    try:
        require_admin(conn)

        unmatched_count = conn.execute(
            "SELECT COUNT(*) AS n FROM playlist_tracks WHERE matched_track_id IS NULL"
        ).fetchone()["n"]
        unmatched = [dict(r) for r in conn.execute(
            "SELECT p.title AS playlist, pt.artist, pt.title AS title, pt.album "
            "FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id "
            "WHERE pt.matched_track_id IS NULL ORDER BY p.title, pt.position LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        )]

        unknown_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE deleted_at IS NULL "
            "AND (artist = 'Unknown Artist' OR album = 'Unknown Album')"
        ).fetchone()["n"]
        unknown = [dict(r) for r in conn.execute(
            "SELECT artist, album, title, relative_path FROM tracks "
            "WHERE deleted_at IS NULL AND (artist = 'Unknown Artist' OR album = 'Unknown Album') "
            "ORDER BY relative_path LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        )]

        # Probable duplicates: the same (artist, album, title, disc, track) on
        # more than one path — e.g. two rips or a FLAC+MP3 of the same track.
        # Case-insensitive grouping; paths joined so the admin can see which
        # files collide.
        dup_rows = conn.execute(
            "SELECT artist, album, title, track_no, COUNT(*) AS n, "
            "GROUP_CONCAT(relative_path, '\n') AS paths FROM tracks "
            "WHERE deleted_at IS NULL "
            "GROUP BY LOWER(artist), LOWER(album), LOWER(title), disc_no, track_no "
            "HAVING n > 1 ORDER BY n DESC, artist, album, title LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        ).fetchall()
        dup_total = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM tracks WHERE deleted_at IS NULL "
            "GROUP BY LOWER(artist), LOWER(album), LOWER(title), disc_no, track_no HAVING COUNT(*) > 1)"
        ).fetchone()["n"]
        duplicates = [
            {"artist": r["artist"], "album": r["album"], "title": r["title"],
             "track_no": r["track_no"], "count": r["n"], "paths": (r["paths"] or "").split("\n")}
            for r in dup_rows
        ]

        # #364 population A: the fingerprint pass gave up on this file
        # (undecodable, vanished, a permissions problem) and _library_track_
        # rows keeps re-selecting it every pass — genuinely actionable, so it
        # renders like every other problem category (chart included).
        fingerprint_failed_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE deleted_at IS NULL "
            "AND fingerprint IS NULL AND fingerprint_failed_at IS NOT NULL"
        ).fetchone()["n"]
        fingerprint_failed = [dict(r) for r in conn.execute(
            "SELECT artist, album, title, relative_path FROM tracks "
            "WHERE deleted_at IS NULL AND fingerprint IS NULL AND fingerprint_failed_at IS NOT NULL "
            "ORDER BY relative_path LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        )]

        # #364 population B: fingerprinted and looked up fine, but AcoustID/
        # MusicBrainz had no match — stamped once by fingerprint.py and never
        # retried. Mostly NOT actionable (live recordings, bootlegs,
        # self-released and local music, much classical genuinely aren't in
        # either database), so this is informational rather than a problem:
        # excluded from healthStatsChart even though it's listed here like
        # every other category.
        #
        # #408: acoustid_isrc, not isrc — tracks.isrc is scanner-populated
        # from the file's own embedded tags (identity.py's tier 1), a
        # completely independent column from fingerprint.py's AcoustID/
        # MusicBrainz backfill (tier 2). Embedded ISRC tags are rare
        # regardless of AcoustID outcome, so checking isrc here counted
        # nearly every successfully-identified track as "not found."
        unidentified_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE deleted_at IS NULL "
            "AND fingerprint IS NOT NULL AND fingerprint_checked_at IS NOT NULL "
            "AND acoustid_isrc IS NULL"
        ).fetchone()["n"]
        unidentified = [dict(r) for r in conn.execute(
            "SELECT artist, album, title, relative_path FROM tracks "
            "WHERE deleted_at IS NULL AND fingerprint IS NOT NULL "
            "AND fingerprint_checked_at IS NOT NULL AND acoustid_isrc IS NULL "
            "ORDER BY relative_path LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        )]

        # #365: last completed library_scan, so the panel can answer "why
        # isn't my new album showing up" before it becomes a support question.
        # None if a scan has never finished on this instance.
        last_scan_row = conn.execute(
            "SELECT finished_at FROM jobs WHERE type = ? AND state = 'done' "
            "ORDER BY finished_at DESC LIMIT 1",
            (scanner.JOB_TYPE,),
        ).fetchone()

        return jsonify({
            "unmatched_playlist_tracks": {"count": unmatched_count, "items": unmatched},
            "unknown_tags": {"count": unknown_count, "items": unknown},
            "duplicates": {"count": dup_total, "items": duplicates},
            "fingerprint_failed": {"count": fingerprint_failed_count, "items": fingerprint_failed},
            "unidentified_fingerprints": {"count": unidentified_count, "items": unidentified},
            "item_limit": _HEALTH_ITEM_LIMIT,
            # #365: a genuine alert, not a count — rendered distinctly from
            # the categories above. The fs type string, not the pre-formatted
            # console warning (_network_data_dir_warning): that text is
            # untranslated and console-styled ("[main] WARNING: ..."), so the
            # UI composes its own translated message from the type alone.
            # None when DATA_DIR is on local disk.
            "data_dir_network_fs": db.data_dir_network_fs(),
            "last_scan_finished_at": last_scan_row["finished_at"] if last_scan_row else None,
            # #389: same "genuine alert, not a count" treatment as
            # data_dir_network_fs above — a distinct-peer count (see
            # _exposure_warning), not a boolean, so the UI can name the
            # number in its own translated message. None whenever there's
            # nothing to say (pinned trusted_proxy, forward mode, or too
            # few distinct peers seen yet).
            "exposure_warning": _exposure_warning(),
            # #389 (review feedback): always-shown counterpart so "quiet
            # because nothing's wrong" and "quiet because the mechanism
            # can't see anything on this deployment's network" aren't the
            # same rendering. None only when the mechanism isn't active at
            # all (pinned trusted_proxy, forward mode).
            "exposure_peer_count": _exposure_status(),
        })
    finally:
        conn.close()


@app.route("/api/admin/mirrors")
def api_admin_mirrors():
    """#285/#189: admin-only read-only overview of every playlist with ANY
    sink enabled — title, per-sink status (filename or remote id,
    last-written timestamp, last error), and coverage (matched/total,
    the same numbers each sink's own write_mirror() writes into its
    comment/marker line). Small and unpaginated, unlike Health's capped
    worklists — this lists a status, not a problem set expected to grow
    large."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        rows = conn.execute(
            "SELECT p.id, p.title, "
            "p.mirror_enabled, p.mirror_filename, p.mirror_last_written_at, "
            "p.mirror_last_error, p.mirror_last_error_code, "
            "p.subsonic_mirror_enabled, p.subsonic_mirror_remote_id, "
            "p.subsonic_mirror_last_written_at, p.subsonic_mirror_last_error, "
            "p.subsonic_mirror_last_error_code, "
            "p.jellyfin_mirror_enabled, p.jellyfin_mirror_remote_id, "
            "p.jellyfin_mirror_last_written_at, p.jellyfin_mirror_last_error, "
            "p.jellyfin_mirror_last_error_code, "
            "p.emby_mirror_enabled, p.emby_mirror_remote_id, "
            "p.emby_mirror_last_written_at, p.emby_mirror_last_error, "
            "p.emby_mirror_last_error_code, "
            # #494: not a mirror sink, but shown here for the same
            # full-admin-panel-visibility reason as the four above —
            # there's no single remote id (a run can request several
            # albums), so last_run_at/last_count stand in for
            # last_written_at/remote_id below.
            "p.lidarr_request_enabled, p.lidarr_request_last_run_at, "
            "p.lidarr_request_last_count, p.lidarr_request_last_error, "
            "p.lidarr_request_last_error_code, "
            "COUNT(pt.id) AS total, "
            "SUM(CASE WHEN pt.matched_track_id IS NOT NULL THEN 1 ELSE 0 END) AS matched "
            "FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
            "WHERE p.mirror_enabled = 1 OR p.subsonic_mirror_enabled = 1 "
            "OR p.jellyfin_mirror_enabled = 1 OR p.emby_mirror_enabled = 1 "
            "OR p.lidarr_request_enabled = 1 "
            "GROUP BY p.id ORDER BY p.title"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["mirror_enabled"] = bool(d["mirror_enabled"])
            d["subsonic_mirror_enabled"] = bool(d["subsonic_mirror_enabled"])
            d["jellyfin_mirror_enabled"] = bool(d["jellyfin_mirror_enabled"])
            d["emby_mirror_enabled"] = bool(d["emby_mirror_enabled"])
            d["lidarr_request_enabled"] = bool(d["lidarr_request_enabled"])
            out.append(d)
        subsonic_config = db.get_mirror_subsonic_config()
        jellyfin_config = db.get_mirror_jellyfin_config()
        emby_config = db.get_mirror_emby_config()
        lidarr_connection = db.get_lidarr_connection()
        return jsonify({
            "mirror_folder": str(db.get_mirror_folder() or "") or None,
            "subsonic_mirror_url": subsonic_config[0] if subsonic_config else None,
            "jellyfin_mirror_url": jellyfin_config[0] if jellyfin_config else None,
            "emby_mirror_url": emby_config[0] if emby_config else None,
            "lidarr_url": lidarr_connection[0] if lidarr_connection else None,
            "playlists": out,
        })
    finally:
        conn.close()


@app.route("/api/admin/jobs")
def api_admin_jobs():
    """#297 step 2: what background work is running, queued, or failed.

    This is the user-visible payoff of the job queue, and the reason it was
    worth building rather than being a pure refactor: before it, a failed
    library scan or fingerprint backfill left nothing but a log line the
    admin would never read. For self-hosted software the admin IS the user,
    so this is a feature surface, not internal diagnostics."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        return jsonify({
            "counts": jobs.status(conn),
            "jobs": jobs.recent(conn, limit=_JOBS_PAGE_LIMIT),
        })
    finally:
        conn.close()


@app.route("/api/admin/jobs/<int:job_id>/retry", methods=["POST"])
def api_admin_job_retry(job_id: int):
    """#297: put a failed job back in the queue, resetting its attempt
    budget so a fix (a corrected config, a provider that's back up) gets a
    real chance rather than one leftover attempt.

    Only `failed` jobs — retrying a queued one is a no-op, and 'retrying' a
    running one would produce a second concurrent copy of work already in
    flight, which is exactly what the dedupe index exists to prevent."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        row = conn.execute(
            "SELECT state, dedupe_key FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            abort(404, description=_("No such job."))
        if row["state"] != "failed":
            abort(400, description=_("Only a failed job can be retried."))
        # The dedupe index covers state IN ('queued','running'), so flipping
        # this row back to 'queued' would violate it if another job with the
        # same key is already pending — an unhandled IntegrityError, i.e. a 500.
        #
        # Reachable on the only job type that exists today, and not as an edge
        # case: the backfill exhausts its retries, the NEXT library scan queues
        # a fresh one (by design), and the failed row is still sitting in the
        # panel inviting a click.
        #
        # The collision means the retry is unnecessary — the work is already
        # queued — so this is reassurance, not an error to repair. Checked
        # explicitly rather than catching IntegrityError, purely because it
        # affords a message that says what's actually true.
        if row["dedupe_key"] is not None:
            clash = conn.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key = ? AND state IN ('queued', 'running')",
                (row["dedupe_key"],),
            ).fetchone()
            if clash is not None:
                abort(409, description=_("This job is already queued to run again."))
        conn.execute(
            "UPDATE jobs SET state = 'queued', attempts = 0, run_after = NULL, "
            "started_at = NULL, finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        jobs.wake()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/admin/jobs/<int:job_id>", methods=["DELETE"])
def api_admin_job_cancel(job_id: int):
    """#297: drop a job that hasn't started yet.

    QUEUED ONLY, deliberately. A running job cannot be stopped from here —
    the worker is inside a handler doing real audio decode or provider HTTP,
    and interrupting that safely needs the handlers themselves to check for
    cancellation, which they don't. Promising a cancel button that silently
    does nothing to the job you actually want stopped would be worse than
    not offering one, so this refuses instead."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            abort(404, description=_("No such job."))
        if row["state"] == "running":
            abort(409, description=_(
                "This job has already started and can't be cancelled — wait for it to finish."))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/account/password", methods=["POST"])
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
    conn = db.get_conn()
    try:
        user_id = get_current_user_id(conn)
        if AUTH_MODE == "oidc" and not _is_admin(conn, user_id):
            abort(400, description=_(
                "Local passwords are admin-only while AUTH_MODE=oidc — your account is managed by "
                "the identity provider."))
        body = request.get_json(force=True)
        password = body.get("password") or ""
        if len(password) < 8:
            abort(400, description=_("Password must be at least 8 characters"))
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/admin/users", methods=["GET", "POST"])
def api_admin_users():
    """Admin-only: full visibility over every account (Authentik-provisioned
    or local), and the ability to provision new local-only accounts (needed
    in AUTH_MODE=local, where there's no SSO auto-provisioning new users the
    way Authentik headers do)."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        if request.method == "POST":
            # #235: this always creates a non-admin row (no is_admin flag
            # below) — exactly the "non-admin local password under oidc"
            # the break-glass model forbids, and it's a local-mode-only tool
            # to begin with (oidc auto-provisions accounts from IdP headers,
            # it doesn't need this).
            if AUTH_MODE == "oidc":
                abort(400, description=_(
                    "New local accounts can't be created while AUTH_MODE=oidc — accounts are "
                    "provisioned automatically from the identity provider."))
            body = request.get_json(force=True)
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            if not username or len(password) < 8:
                abort(400, description=_("Username required, password at least 8 characters"))
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password)),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                abort(400, description=_("This username already exists"))
        # #332: owned counts come with the list, so the delete dialog can say
        # WHICH ownership blocks a deletion before anything is attempted. The old
        # flow was a native confirm() with a fixed string, then a 400 naming three
        # categories without saying which one, how many, or which items — a dead
        # end the admin had to go hunting from.
        #
        # Correlated subqueries rather than joins: three independent one-to-many
        # counts would otherwise multiply each other, and a user list is small.
        rows = conn.execute(
            "SELECT id, username, email, is_admin, "
            "(password_hash IS NOT NULL) AS has_local_password, "
            "(SELECT COUNT(*) FROM devices WHERE owner_user_id = users.id) AS owned_devices, "
            "(SELECT COUNT(*) FROM selections WHERE created_by_user_id = users.id) "
            "  AS owned_selections, "
            "(SELECT COUNT(*) FROM playlists WHERE owner_user_id = users.id) AS owned_playlists "
            "FROM users ORDER BY username"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/admin/users/<int:target_user_id>", methods=["DELETE"])
def api_admin_user_delete(target_user_id: int):
    conn = db.get_conn()
    try:
        admin_id = get_current_user_id(conn)
        require_admin(conn)
        if target_user_id == admin_id:
            abort(400, description=_("Cannot delete your own account"))
        row = conn.execute(
            "SELECT is_admin, (password_hash IS NOT NULL) AS has_local_password "
            "FROM users WHERE id = ?", (target_user_id,)
        ).fetchone()
        if row is None:
            abort(404, description=_("User not found"))
        # #237: two lockout guardrails, same reasoning as "don't delete your
        # own account" above — both are about never reaching a state with
        # no working login left, not about protecting any particular user.
        if row["is_admin"]:
            admin_count = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1").fetchone()["n"]
            if admin_count <= 1:
                abort(400, description=_("Cannot delete the last admin account"))
        if AUTH_MODE == "oidc" and row["has_local_password"]:
            # Under OIDC, a local password is exceptional — everyone else
            # authenticates via the IdP. Deleting the sole remaining one
            # removes the only login that survives an IdP outage, which
            # local-only mode doesn't have the same stakes for (there's no
            # IdP to go down), so this check is OIDC-specific.
            local_pw_count = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE password_hash IS NOT NULL").fetchone()["n"]
            if local_pw_count <= 1:
                abort(400, description=_(
                    "This is the only account with a local password — deleting it would lock "
                    "everyone out if the identity provider ever becomes unreachable. Give another "
                    "admin a local password first if you still want to remove this one."))
        try:
            conn.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
            conn.commit()
        except sqlite3.IntegrityError:
            # devices.owner_user_id / selections.created_by_user_id are
            # NOT NULL foreign keys with no cascade or reassignment, and
            # #68 added playlists.owner_user_id (also a restricted FK) — so
            # this account still owns a device, a selection, or a synced
            # playlist. Delegation lets other users *manage* their devices,
            # it doesn't transfer ownership, so it doesn't help here; a
            # playlist's ownership only transfers by re-syncing it under a
            # different mapping (#70). Fail cleanly rather than a raw 500.
            # #332: "or reassign" implied a route that mostly does not exist —
            # delegation lets others MANAGE a device without transferring
            # ownership, and a playlist's owner only changes by re-syncing under a
            # different mapping (#70). The dialog now names the actual counts
            # before anything is attempted; this stays as the server-side backstop
            # for a stale list or a direct API call.
            abort(400, description=_(
                "This account still owns devices, selections or playlists. Delete those "
                "first — ownership can't be transferred from here."))
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/admin/users/<int:target_user_id>/password", methods=["PUT"])
def api_admin_user_reset_password(target_user_id: int):
    """Admin-only password reset (#237) — the admin-recovery counterpart to
    a user's own change-password self-service. Deliberately allows
    resetting another admin's (or the break-glass account's own) password:
    refusing that would mean a forgotten admin password is an unrecoverable
    lockout with no in-app path out, which is worse than trusting an admin
    (who could already delete the account, or any other, outright) with
    this too. #235: under oidc, resetting a non-admin's password is refused
    instead — an admin minting one would otherwise recreate exactly the
    non-admin break-glass bypass that mode is meant to rule out."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        row = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if row is None:
            abort(404, description=_("User not found"))
        if AUTH_MODE == "oidc" and not row["is_admin"]:
            abort(400, description=_(
                "Local passwords are admin-only while AUTH_MODE=oidc — this account is managed by "
                "the identity provider."))
        body = request.get_json(force=True)
        password = body.get("password") or ""
        if len(password) < 8:
            abort(400, description=_("Password must be at least 8 characters"))
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), target_user_id),
        )
        if cur.rowcount == 0:
            abort(404, description=_("User not found"))
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/admin/roon-profiles")
def api_admin_roon_profiles():
    """Admin-only: live Roon profiles + every Trobar user, with a fuzzy-
    matched suggestion per unmapped user (same difflib cutoff as
    matching.py's playlist-track matching, not reinvented here) — for
    Administration > Configuration > Roon's mapping UI. Not gated on the
    active provider actually being Roon: harmless either way (just
    reflects whatever roon_client.list_profiles() can currently reach),
    the frontend only shows this section when Roon is configured."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        result = roon_client.list_profiles()
        users_rows = conn.execute(
            "SELECT id, username, roon_profile FROM users ORDER BY username"
        ).fetchall()
    finally:
        conn.close()

    if result["status"] != "ok":
        return jsonify(result)

    profile_names = [p["title"] for p in result["profiles"]]
    users_out = []
    for row in users_rows:
        suggestion = None
        if not row["roon_profile"] and profile_names:
            close = difflib.get_close_matches(row["username"], profile_names, n=1, cutoff=0.85)
            suggestion = close[0] if close else None
        users_out.append({
            "id": row["id"], "username": row["username"],
            "roon_profile": row["roon_profile"], "suggested": suggestion,
        })
    return jsonify({"status": "ok", "profiles": result["profiles"], "users": users_out})


@app.route("/api/admin/users/<int:target_user_id>/roon-profile", methods=["PUT"])
def api_admin_user_roon_profile(target_user_id: int):
    conn = db.get_conn()
    try:
        require_admin(conn)
        body = request.get_json(force=True)
        profile = (body.get("roon_profile") or "").strip() or None
        conn.execute("UPDATE users SET roon_profile = ? WHERE id = ?", (profile, target_user_id))
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


def _admin_provider_user_mapping(conn, client, column: str) -> dict:
    """#262: shared implementation behind the Jellyfin/Emby per-Trobar-user
    mapping routes below — same shape as api_admin_roon_profiles() above,
    generalized since Jellyfin/Emby's own list_users()/mapping column
    naming is identical in structure (unlike Roon, which maps to a
    profile NAME rather than an opaque id — so here the fuzzy-matched
    suggestion has to resolve back through a name->id lookup to land on
    something actually storable in `column`, rather than being directly
    usable as-is the way Roon's profile-name suggestion is)."""
    result = client.list_users()
    users_rows = conn.execute(
        f"SELECT id, username, {column} FROM users ORDER BY username"
    ).fetchall()

    if result["status"] != "ok":
        return result

    target_users = result["users"]
    ids_by_name = {u["name"]: u["id"] for u in target_users}
    users_out = []
    for row in users_rows:
        suggested_id = suggested_name = None
        if not row[column] and ids_by_name:
            close = difflib.get_close_matches(row["username"], list(ids_by_name.keys()), n=1, cutoff=0.85)
            if close:
                suggested_name = close[0]
                suggested_id = ids_by_name[suggested_name]
        users_out.append({
            "id": row["id"], "username": row["username"], "mapped_id": row[column],
            "suggested_id": suggested_id, "suggested_name": suggested_name,
        })
    return {"status": "ok", "target_users": target_users, "users": users_out}


@app.route("/api/admin/jellyfin-users")
def api_admin_jellyfin_users():
    """#262: live Jellyfin server users + every Trobar user, with a
    fuzzy-matched suggestion per unmapped user — for Administration >
    Configuration > Jellyfin's mapping UI. Not gated on the active
    provider actually being Jellyfin: harmless either way, the frontend
    only shows this section when Jellyfin is configured. See
    _admin_provider_user_mapping()'s own docstring for the shape."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        return jsonify(_admin_provider_user_mapping(conn, jellyfin_client, "jellyfin_user_id"))
    finally:
        conn.close()


@app.route("/api/admin/users/<int:target_user_id>/jellyfin-user", methods=["PUT"])
def api_admin_user_jellyfin_user(target_user_id: int):
    conn = db.get_conn()
    try:
        require_admin(conn)
        body = request.get_json(force=True)
        mapped_id = (body.get("jellyfin_user_id") or "").strip() or None
        conn.execute("UPDATE users SET jellyfin_user_id = ? WHERE id = ?", (mapped_id, target_user_id))
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/admin/emby-users")
def api_admin_emby_users():
    """#262: live Emby server users + every Trobar user, with a
    fuzzy-matched suggestion per unmapped user — for Administration >
    Configuration > Emby's mapping UI. Not gated on the active provider
    actually being Emby: harmless either way, the frontend only shows
    this section when Emby is configured. See
    _admin_provider_user_mapping()'s own docstring for the shape."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        return jsonify(_admin_provider_user_mapping(conn, emby_client, "emby_user_id"))
    finally:
        conn.close()


@app.route("/api/admin/users/<int:target_user_id>/emby-user", methods=["PUT"])
def api_admin_user_emby_user(target_user_id: int):
    conn = db.get_conn()
    try:
        require_admin(conn)
        body = request.get_json(force=True)
        mapped_id = (body.get("emby_user_id") or "").strip() or None
        conn.execute("UPDATE users SET emby_user_id = ? WHERE id = ?", (mapped_id, target_user_id))
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/admin/lidarr-options")
def api_admin_lidarr_options():
    """#494: live root-folder/quality-profile/metadata-profile lists from
    the already-saved Lidarr connection (lidarr_url/lidarr_api_key must be
    persisted first via PUT /api/admin/config — see that route's own
    two-phase docstring; nothing here can succeed before that). Same
    "admin-only, live-fetch-from-external-server-into-a-dropdown" shape as
    GET /api/admin/jellyfin-users, generalized to three lists instead of
    one Trobar-user mapping."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        root_folders = lidarr_client.list_root_folders()
        if root_folders["status"] != "ok":
            return jsonify(root_folders)
        quality_profiles = lidarr_client.list_quality_profiles()
        if quality_profiles["status"] != "ok":
            return jsonify(quality_profiles)
        metadata_profiles = lidarr_client.list_metadata_profiles()
        if metadata_profiles["status"] != "ok":
            return jsonify(metadata_profiles)
        return jsonify({
            "status": "ok",
            "root_folders": root_folders["root_folders"],
            "quality_profiles": quality_profiles["quality_profiles"],
            "metadata_profiles": metadata_profiles["metadata_profiles"],
        })
    finally:
        conn.close()


@app.route("/api/admin/delegations", methods=["GET", "POST"])
def api_admin_delegations():
    """Admin-only: grant/list delegation of full device-management rights
    between two non-admin users (e.g. mum -> kid1's devices).
    Delegation covers every device the target user owns, current and future;
    the grantee only actually *sees* one in their own Appareils list once
    they pin it (GET /api/devices/delegatable + POST .../pin)."""
    conn = db.get_conn()
    try:
        require_admin(conn)
        if request.method == "POST":
            body = request.get_json(force=True)
            grantee_id = int(body["grantee_user_id"])
            target_id = int(body["target_user_id"])
            if grantee_id == target_id:
                abort(400, description=_("A user cannot delegate to themselves"))
            for uid in (grantee_id, target_id):
                if conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None:
                    abort(400, description=_("User not found"))
            try:
                conn.execute(
                    "INSERT INTO device_delegations (grantee_user_id, target_user_id) VALUES (?, ?)",
                    (grantee_id, target_id),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                abort(400, description=_("This delegation already exists"))
        rows = conn.execute(
            "SELECT dd.id, dd.grantee_user_id, gu.username AS grantee_username, "
            "dd.target_user_id, tu.username AS target_username, dd.granted_at "
            "FROM device_delegations dd "
            "JOIN users gu ON gu.id = dd.grantee_user_id "
            "JOIN users tu ON tu.id = dd.target_user_id "
            "ORDER BY tu.username, gu.username"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/admin/delegations/<int:delegation_id>", methods=["DELETE"])
def api_admin_delegation_delete(delegation_id: int):
    conn = db.get_conn()
    try:
        require_admin(conn)
        row = conn.execute(
            "SELECT grantee_user_id, target_user_id FROM device_delegations WHERE id = ?",
            (delegation_id,),
        ).fetchone()
        if row is not None:
            conn.execute("DELETE FROM device_delegations WHERE id = ?", (delegation_id,))
            # Revoking removes management rights immediately (checks derive
            # live from this table) — also drop any pins the (former)
            # grantee held on the target's devices, so a now-inaccessible
            # device doesn't linger, unreachable, in their Appareils list.
            conn.execute(
                "DELETE FROM device_pins WHERE user_id = ? AND device_id IN "
                "(SELECT id FROM devices WHERE owner_user_id = ?)",
                (row["grantee_user_id"], row["target_user_id"]),
            )
            conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Device-facing API — in forward auth mode this path must be exempted from
# the ForwardAuth gate at the proxy (native clients can't follow an HTML
# login redirect). Every endpoint here authenticates the per-device Bearer
# token itself.
# ---------------------------------------------------------------------------

# #239: /api/device/fingerprints page sizes. A chromaprint fingerprint is
# ~2.2 KB of text (measured: ~18.5 chars per second of audio, capped by
# pyacoustid at 120 seconds' worth), so 200 entries is roughly a 450 KB page
# before gzip — the reason this is paginated at all, and the reason the cap
# exists rather than trusting a client-supplied limit.
_FINGERPRINT_PAGE_DEFAULT = 200
_FINGERPRINT_PAGE_MAX = 500

# #239 PR 2: entries accepted per provenance push. Same reasoning as the page
# sizes above, in the other direction — a fingerprint is ~2.2 KB, so an
# uncapped push from a 5,000-track device would be a ~12 MB request body.
# Clients push in pages; the response's `pending` tells them there's more.
_PROVENANCE_PUSH_MAX = 500

# #297 step 2: how many recent jobs the admin overview lists. Unpaginated on
# purpose — jobs.py prunes finished rows to a retention cap, so the table
# can't grow without bound and this is a status view, not a worklist to page
# through. Kept below that cap so the panel always shows the newest ones.
_JOBS_PAGE_LIMIT = 50


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


def _config_int(conn, key: str, default: int) -> int:
    raw = db.get_config(conn, key)
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


@app.route("/api/device/info")
def api_device_info():
    """Lets the client show its own server-side identity (name as set in
    the web UI) — the app only stores server_url+token from pairing, never
    its own name."""
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        return jsonify({"name": device["name"], "device_type": device["device_type"],
                         "max_size_bytes": device["max_size_bytes"],
                         "transcode_format": device["transcode_format"],
                         "artist_images": device["artist_images"],
                         # #63: so the client can show/reflect the current
                         # source_of_truth (single field, no client-local drift).
                         "source_of_truth": device["source_of_truth"]})
    finally:
        conn.close()


@app.route("/api/device/storage", methods=["POST"])
def api_device_storage():
    """Client reports free + total space of its actual storage volume
    (StatFs-derived on Android, will be shutil.disk_usage() of the mounted
    volume for the future desktop/SD-card CLI client) — surfaced in the web
    UI's Profil > Appareils so the limit set there can be sanity-checked
    against what's physically available, not just trusted blindly, and so
    the device's *overall* storage consumption (not just Trobar's own
    share of it) is visible too."""
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        conn.execute(
            "UPDATE devices SET reported_free_bytes = ?, reported_total_bytes = ?, "
            "free_bytes_reported_at = datetime('now') WHERE id = ?",
            (body.get("free_bytes"), body.get("total_bytes"), device["id"]),
        )
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/device/limit", methods=["PATCH"])
def api_device_limit():
    """Lets the device itself set its own allocation, mirroring what
    PATCH /api/devices/<id> already lets the web UI do — added because the
    Android app's Settings screen authenticates by device Bearer token, not
    an Authentik session, so it can't call the web-facing devices route."""
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        conn.execute(
            "UPDATE devices SET max_size_bytes = ? WHERE id = ?",
            (body.get("max_size_bytes"), device["id"]),
        )
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/device/source-of-truth", methods=["PATCH"])
def api_device_source_of_truth():
    """#63: lets the device set its own source_of_truth, mirroring what
    PATCH /api/devices/<id> lets the web UI do (device Bearer token, not an
    Authentik session). 'device' stops the server pruning this device's tracks
    (so it survives a server-DB loss); 'server' is the default conform-to-server
    behavior. Flipping to 'server' re-prunes on the recompute below."""
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        sot = _validated_source_of_truth(body.get("source_of_truth"))
        conn.execute(
            "UPDATE devices SET source_of_truth = ? WHERE id = ?", (sot, device["id"]))
        conn.commit()
        sync_state.recompute_device_state(conn, device["id"])
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/device/manifest", methods=["POST"])
def api_device_manifest():
    """#63 recovery: a re-paired device POSTs {"paths": [...]} — the relative
    paths it already holds — and the server marks those tracks 'downloaded' so
    they aren't re-fetched after a server-DB loss (pair with
    source_of_truth='device', set before the first sync). Device Bearer token.
    Returns {"matched": n, "unmatched": n}."""
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        paths = body.get("paths")
        if not isinstance(paths, list):
            abort(400, description=_("paths must be a list of relative paths."))
        return jsonify(sync_state.record_device_manifest(conn, device["id"], paths))
    finally:
        conn.close()


@app.route("/api/device/changes")
def api_device_changes():
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        changes = sync_state.get_changes(conn, device["id"])
    finally:
        conn.close()
    # #239: device sync is the trigger for computing the fingerprints this
    # device's provenance DB will store (the locked design decision: reuse
    # #200's cache when a track already has one, compute on first device sync
    # when it doesn't). Fire-and-forget AFTER the response is built and the
    # connection released — a real audio decode must never run inline in a
    # request holding a DB connection, and this is a client-polled hot path.
    # It no-ops harmlessly when there's nothing left to compute.
    #
    # Swallowed deliberately: provenance is a side-benefit of syncing and must
    # never be able to break the sync itself. start_ensure_fingerprints only
    # enqueues a job row, but that write CAN fail (a DB hiccup) — and that's
    # exactly when you'd least want every device's sync to start 500ing. The
    # pass is retried on the device's next sync anyway.
    try:
        provenance.start_ensure_fingerprints(device["id"])
    except Exception:
        app.logger.exception("could not start the device fingerprint pass")

    # #239 PR 2: continue any in-flight provenance rematch. The rematch handler
    # can't re-enqueue itself — it still holds its own dedupe_key while running,
    # so the follow-up would be refused — and its batch cap means one push
    # rarely finishes the work. A recovering device syncs repeatedly, so
    # continuing here drains it with no client involvement. No-ops (dedupe, or
    # nothing pending) in the overwhelmingly common case of no recovery underway.
    # Swallowed for the same reason as above: this must never break a sync.
    try:
        conn = db.get_conn()
        try:
            if provenance.pushed_pending_count(conn, device["id"]):
                if provenance.library_fingerprints_pending(conn):
                    jobs.enqueue(conn, provenance.JOB_TYPE_LIBRARY_FINGERPRINTS,
                                 dedupe_key=provenance.JOB_TYPE_LIBRARY_FINGERPRINTS)
                jobs.enqueue(conn, provenance.JOB_TYPE_REMATCH,
                             payload={"device_id": device["id"]},
                             dedupe_key=f"{provenance.JOB_TYPE_REMATCH}:{device['id']}")
        finally:
            conn.close()
    except Exception:
        app.logger.exception("could not continue the provenance rematch")
    return jsonify(changes)


@app.route("/api/device/fingerprints")
def api_device_fingerprints():
    """#239: the server-computed fingerprint for each track this device holds
    or is about to hold, so the client can keep a local provenance DB —
    "these files came from Trobar, and here's the identity the server itself
    assigned them". Clients never compute fingerprints; they only store what
    this returns. Device Bearer token.

    Cursor-paginated on ascending track_id (`?after=`, `?limit=`) because a
    fingerprint is ~2.2 KB of text and a large DAP holds thousands of tracks —
    measured, and the reason this is its own endpoint rather than extra fields
    on /api/device/changes (which lists everything the device holds and is
    polled, so it would have grown by megabytes per poll).

    `path` is the same _device_path wire form /api/device/changes and
    /api/device/manifest already speak, so the client can key its provenance
    rows by the path it actually wrote. `fingerprint` is always the SOURCE
    audio's, even on a transcoding device — see provenance.py's docstring for
    why that's the only form that rematches on recovery. `pending` counts this
    device's tracks whose fingerprint isn't computed yet, so a client knows to
    come back rather than treat a short page as complete.

    #439: `?computed_after=` is a SECOND, orthogonal filter on top of the
    `after`/`next_after` track_id cursor above, not a replacement for it —
    that cursor still drives the initial full walk. Once a client has walked
    everything once, it can remember the highest `fingerprint_seq` it saw
    across every entry (each one is included below for exactly this) and
    pass it back as `computed_after` on every later sync, getting only
    tracks whose fingerprint is NEW or CHANGED since then — no more
    re-fetching megabytes of unchanged data every sync (the entire point:
    and measured). fingerprint_seq is a strictly-increasing
    counter, not a timestamp (db.py explains why: a wall-clock second can
    tie many rows together during a bulk backfill, which a `>` filter can't
    safely resolve; a counter can't ever tie). track_id ordering is
    UNCHANGED even when this filter is active — fingerprint_seq is never
    the sort key, only a WHERE predicate, so the existing cursor's
    resume-a-cut-off-page guarantee still holds exactly as before."""
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        try:
            after = int(request.args.get("after", 0))
            limit = int(request.args.get("limit", _FINGERPRINT_PAGE_DEFAULT))
            computed_after_raw = request.args.get("computed_after")
            computed_after = int(computed_after_raw) if computed_after_raw is not None else None
        except (TypeError, ValueError):
            abort(400, description=_("after, limit, and computed_after must be whole numbers."))
        if after < 0 or limit < 1 or (computed_after is not None and computed_after < 0):
            abort(400, description=_("after must be 0 or more and limit at least 1."))
        limit = min(limit, _FINGERPRINT_PAGE_MAX)

        fmt = device["transcode_format"]
        query = (
            "SELECT t.id, t.artist, t.album, t.title, t.track_no, t.disc_no, "
            "t.relative_path, t.fingerprint, t.fingerprint_seq FROM device_track_state dts "
            "JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status IN ('pending', 'downloaded') "
            "AND t.deleted_at IS NULL AND t.fingerprint IS NOT NULL AND t.id > ? "
        )
        params = [device["id"], after]
        if computed_after is not None:
            query += "AND t.fingerprint_seq > ? "
            params.append(computed_after)
        query += "ORDER BY t.id LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        entries = [
            {"track_id": row["id"], "fingerprint": row["fingerprint"],
             "fingerprint_seq": row["fingerprint_seq"],
             "path": sync_state.device_path(row, fmt)}
            for row in rows
        ]
        return jsonify({
            "entries": entries,
            # Only a full page can have more behind it; a short page is the
            # end of the cursor walk (though `pending` may still be > 0 —
            # those tracks have no fingerprint YET, so they aren't in this
            # result set at all and a later call will pick them up).
            "next_after": entries[-1]["track_id"] if len(rows) == limit else None,
            "pending": provenance.pending_count(conn, device["id"]),
        })
    finally:
        conn.close()


@app.route("/api/device/provenance", methods=["POST"])
def api_device_provenance():
    """#239 PR 2: a device pushes back the provenance DB it built from
    /api/device/fingerprints, and the server rematches it BY FINGERPRINT
    against its own library. Device Bearer token.

    Body: {"entries": [{"track_id": N, "fingerprint": "...", "path": "..."}]}

    Why this exists alongside /api/device/manifest: the manifest matches on a
    byte-exact device_path() comparison, which only works while the layout still
    agrees. device_path() is built from tags, track_no/disc_no, fs_segment()'s
    sanitisation and the transcode extension, so any drift there makes Trobar
    fail to recognise files it wrote itself — the "listed as unknown to adopt"
    symptom in #161. Audio content doesn't drift when a tag does. Both are
    accepted; provenance alone is sufficient and strictly better.

    Stores and returns immediately — matching happens in a background job,
    because verifying each entry costs an audio decode and this must not block a
    device's sync. `pending` is how the client knows there's more to do."""
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        entries = body.get("entries")
        if not isinstance(entries, list):
            abort(400, description=_("entries must be a list."))
        if len(entries) > _PROVENANCE_PUSH_MAX:
            abort(400, description=_(
                "Too many entries in one request — push them in smaller pages."))
        # Validated up front, so one malformed entry can't leave half a page
        # stored. path/fingerprint are what the rematch needs; track_id is
        # optional and only ever kept for diagnostics.
        for entry in entries:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("path"), str) or not entry["path"]
                    or not isinstance(entry.get("fingerprint"), str)
                    or not entry["fingerprint"]):
                abort(400, description=_(
                    "Each entry needs a non-empty path and fingerprint."))
            if entry.get("track_id") is not None and not isinstance(entry["track_id"], int):
                abort(400, description=_("track_id must be a whole number when given."))

        stored = provenance.store_pushed_provenance(conn, device["id"], entries)
        conn.commit()
        pending = provenance.pushed_pending_count(conn, device["id"])
        # One job however many pages were pushed — the dedupe key collapses them
        # while one is queued or running, and it frees once that finishes.
        # A pushed fingerprint can only match a track the server has itself
        # fingerprinted, and after a DB loss NOTHING in the library has been —
        # device-scoped fingerprinting (PR 1) covers only tracks a device
        # already syncs, which is empty in exactly that situation. So drive a
        # library-wide pass too; the rematch defers rows until it finishes.
        if provenance.library_fingerprints_pending(conn):
            jobs.enqueue(conn, provenance.JOB_TYPE_LIBRARY_FINGERPRINTS,
                         dedupe_key=provenance.JOB_TYPE_LIBRARY_FINGERPRINTS)
        jobs.enqueue(conn, provenance.JOB_TYPE_REMATCH, payload={"device_id": device["id"]},
                     dedupe_key=f"{provenance.JOB_TYPE_REMATCH}:{device['id']}")
        return jsonify({"received": len(entries), "stored": stored, "pending": pending})
    finally:
        conn.close()


@app.route("/api/device/file/<int:track_id>")
def api_device_file(track_id: int):
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        # #110: authorize, not just authenticate. The Bearer token proves which
        # device is asking; this proves the track is one the server actually
        # offered THAT device (its own device_track_state set — an O(1) PK
        # lookup, exactly what get_changes() told it to fetch), not any track_id
        # in the library. So a stolen/copied device token (a plaintext
        # credential on the card, trobar-desktop#12) reaches only that device's
        # selections instead of the whole catalog. 'downloaded' is included for
        # re-download/integrity retries; 'removed'/absent are excluded. Checked
        # before the transcode branch below so that path inherits it too. 404
        # (not 403) so it doesn't confirm which track ids exist.
        allowed = conn.execute(
            "SELECT 1 FROM device_track_state WHERE device_id = ? AND track_id = ? "
            "AND status IN ('pending', 'downloaded')",
            (device["id"], track_id),
        ).fetchone()
        if allowed is None:
            abort(404, description=_("Track not found (removed from the library?)"))
        row = conn.execute(
            "SELECT relative_path FROM tracks WHERE id = ? AND deleted_at IS NULL", (track_id,)
        ).fetchone()
        if row is None:
            abort(404, description=_("Track not found (removed from the library?)"))
        abs_path = db.get_music_root() / row["relative_path"]
        if not abs_path.is_file():
            abort(404, description=_("File not found on the server"))

        fmt = device["transcode_format"]
        if not sync_state.wants_transcode(row, fmt):
            return send_file(abs_path, conditional=True)

        if not transcode.ffmpeg_available():
            abort(500, description=_("Transcoding is unavailable on the server right now"))
        limit = _config_int(conn, "transcode_concurrency", 1)
        nice_level = _config_int(conn, "transcode_nice_level", 10)
        transcode.acquire_slot(limit)
        try:
            proc, out_path, err_path = transcode.start(abs_path, fmt, nice_level)
        except transcode.TranscodeStartError:
            transcode.release_slot()
            abort(500, description=_("Transcoding failed to start"))
        # The transcode finishes (into a temp file, trobar-server#223)
        # before any bytes reach the client, so its final size would be
        # knowable here — but Range/resume on it still isn't offered,
        # unchanged from before: Accept-Ranges: none tells well-behaved
        # clients not to bother, and a dropped connection just means the
        # client restarts the track from scratch on its next sync, same as
        # any other failed download.
        return Response(transcode.iter_output(proc, out_path, err_path), mimetype="audio/mpeg",
                         headers={"Accept-Ranges": "none"})
    finally:
        conn.close()


@app.route("/api/device/artist-image")
def api_device_artist_image():
    conn = db.get_conn()
    try:
        _authenticated_device(conn)
        provider = _active_provider(conn)
        audiodb_key = db.get_config(conn, "audiodb_api_key")
    finally:
        conn.close()
    artist = request.args.get("artist", "")
    if not artist:
        abort(404)
    found = artist_images.get_artist_image(artist, provider, audiodb_key)
    if found is None:
        abort(404, description=_("No artist image available"))
    data, content_type = found
    if request.args.get("size") == "small": # DAP-friendly variant
        data, content_type = artist_images.downscale(data, content_type)
    return Response(data, mimetype=content_type, headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/device/ack", methods=["POST"])
def api_device_ack():
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        raw_bytes = body.get("bytes_on_device")
        sync_state.ack(conn, device["id"], int(body["track_id"]), body["status"],
                       int(raw_bytes) if raw_bytes is not None else None)
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@app.route("/api/device/missing-tracks", methods=["POST"])
def api_device_missing_tracks():
    """the client spot-checks its own "should be downloaded"
    tracks against what's actually on disk (this endpoint never does that
    itself, it only records the client's — or its own standing preference's
    — decision): `redownload` flips those track ids back to `pending`,
    `exclude` marks them so they stop being silently re-queued without
    touching the selection that still nominally requires them."""
    conn = db.get_conn()
    try:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        redownload_ids = [int(t) for t in body.get("redownload", [])]
        exclude_ids = [int(t) for t in body.get("exclude", [])]
        sync_state.resolve_missing_tracks(conn, device["id"], redownload_ids, exclude_ids)
        return jsonify({"status": "ok"})
    finally:
        conn.close()


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
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, is_admin, password_hash) VALUES (?, 1, ?)",
                (username, generate_password_hash(password)),
            )
            click.echo(f"Created admin account {username!r}.")
        else:
            conn.execute(
                "UPDATE users SET is_admin = 1, password_hash = ? WHERE id = ?",
                (generate_password_hash(password), row["id"]),
            )
            click.echo(f"Updated {username!r}: now admin, password reset.")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    db.init_db()
    # Only the active provider's ensure_started() runs — avoids an unwanted
    # Roon pairing attempt against a real Roon Core (or any provider
    # connection attempt at all) for a deployment actually configured for
    # the other one. This happened once before by accident when this call
    # was unconditional (a container restart alone triggered a pairing
    # request against production Roon).
    _startup_conn = db.get_conn()
    try:
        _active_provider(_startup_conn).ensure_started()
    finally:
        _startup_conn.close()

    # Trobar only ever reads the music library — it has no code path
    # that writes there. Mount it read-only so a bug or a compromise can't
    # modify or delete the user's files. Warn (don't refuse — a false positive
    # shouldn't take the app down) if the effective MUSIC_ROOT isn't a
    # read-only mount. Also surfaced to the admin UI via /api/admin/config.
    if _music_root_is_readonly() is False:
        print(
            f"[main] WARNING: MUSIC_ROOT ({db.get_music_root()}) is not mounted read-only. "
            "Trobar never writes to your music library; mount it read-only (append ':ro' to "
            "the volume in docker-compose, e.g. '/path/to/music:/music:ro') so a bug or "
            "compromise can't alter or delete it. See SECURITY.md."
        )

    _data_dir_warning = _network_data_dir_warning()
    if _data_dir_warning:
        print(_data_dir_warning)

    # forward mode trusts identity headers. Without a proxy-injected
    # shared secret to confirm the request actually came through the trusted
    # proxy, a directly-exposed port lets anyone spoof X-authentik-username —
    # warn loudly so a self-hoster can't miss it.
    if AUTH_MODE == "forward" and not FORWARD_AUTH_SECRET:
        print(
            "[main] WARNING: AUTH_MODE=forward with no FORWARD_AUTH_SECRET set — the app trusts "
            "identity headers with no verification that a trusted proxy set them. Ensure the app "
            "port is NEVER exposed directly and your proxy strips client-supplied X-authentik-* "
            "headers. Set FORWARD_AUTH_SECRET (injected by your proxy) to fail-closed instead."
        )

    if EMERGENCY_PORT and AUTH_MODE != "forward":
        print(
            f"[main] EMERGENCY_PORT={EMERGENCY_PORT} set but AUTH_MODE={AUTH_MODE!r} — "
            "ignoring it. It only exists to bypass a ForwardAuth proxy gate (forward mode); "
            "in local/oidc mode there's no such gate in front of Flask to bypass."
        )
    elif EMERGENCY_PORT:
        # Same Flask app, second internal listener — see EMERGENCY_PORT /
        # _is_emergency_request() above for why this exists and how it's
        # kept safe. Runs in a background thread; the main thread still
        # blocks on the normal port below, same as before this existed.
        import threading
        from werkzeug.serving import make_server
        emergency_server = make_server("0.0.0.0", EMERGENCY_PORT, app, threaded=True)
        threading.Thread(target=emergency_server.serve_forever, daemon=True).start()
        print(f"[main] emergency port {EMERGENCY_PORT} listening (bypasses the proxy gate)")

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
        print("[main] background job worker started")

    if os.environ.get("TROBAR_DEV_SERVER", "").lower() in ("1", "true", "yes"):
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
