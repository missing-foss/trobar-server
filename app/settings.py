#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings the auth code reads, from the environment, read once at start.

Read them as `settings.AUTH_MODE`, never `from settings import AUTH_MODE`: that
form makes a second name with its own value, and a test that patches
settings.AUTH_MODE would then not reach the code reading the copy.
test_settings.py fails on it.
"""

import logging
import os

_log = logging.getLogger(__name__)

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

# Local development only: Werkzeug's dev server instead of waitress, and in
# forward mode a request without the identity header signs in as DEV_USER.
DEV_SERVER = os.environ.get("TROBAR_DEV_SERVER", "").lower() in ("1", "true", "yes")
if AUTH_MODE == "authentik":
    AUTH_MODE = "forward"

# forward-mode hardening: when set, the trusted proxy must inject a
# matching X-Forward-Auth-Secret header or the request is rejected — a
# fail-closed guard against someone hitting a directly-exposed port and
# spoofing X-authentik-username. When unset, headers are trusted as before
# (with a loud startup warning) so an existing proxy-gated deployment keeps
# working without a proxy-config change.
FORWARD_AUTH_SECRET = os.environ.get("FORWARD_AUTH_SECRET", "")

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
