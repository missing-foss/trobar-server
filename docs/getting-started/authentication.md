<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Authentication Modes

Trobar authenticates in one of three modes, set by `AUTH_MODE`. In every mode,
`ADMIN_USERNAME` names the account that is promoted to admin.

| Mode | What it does |
|---|---|
| `local` *(default)* | Every user has a username/password stored by the app itself. Simplest way to run it — no external auth system needed. Fail-closed default. |
| `oidc` | **Recommended for SSO.** The app is an OpenID Connect client: it redirects users to your identity provider and cryptographically verifies the returned ID token (signature + standard claims). Works with any OIDC provider — Authentik, Authelia, Keycloak, Zitadel, … — configured directly, no ForwardAuth middleware. |
| `forward` | Trust `X-authentik-*`-style identity headers set by a ForwardAuth reverse proxy. **The app performs no authentication of its own in this mode.** (`authentik` is accepted as an alias.) |

Prefer `oidc` over `forward` unless you specifically run a blanket ForwardAuth
proxy: it's verified in-app (nothing to spoof) and needs no per-app proxy
configuration.

## `local`

Nothing else to configure. The first visit creates the admin account (locked
to `ADMIN_USERNAME`); other household accounts are created in the
[Administration](../administration.md#users) panel. Don't expose the instance
publicly until first login is done.

## `oidc`

Set `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and `OIDC_CLIENT_SECRET`, and register the
app in your IdP with redirect URI `https://<your-host>/oidc/callback`. Users are
auto-provisioned on first login, taking the username from `OIDC_USERNAME_CLAIM`
(default `preferred_username`). The `/login` page still offers a local password
as a **break-glass fallback** for when the IdP is down — under `oidc` mode this
is an admin-only operational control, set from Administration → Configuration,
not from a regular user's Profile.

| Variable | Default | Purpose |
|---|---|---|
| `OIDC_ISSUER` | *(unset)* | Your IdP's issuer URL (discovery at `{issuer}/.well-known/openid-configuration`) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | *(unset)* | Credentials from the app you register in your IdP |
| `OIDC_SCOPES` | `openid profile email` | Requested scopes |
| `OIDC_USERNAME_CLAIM` | `preferred_username` | Which ID-token claim maps to the app username |
| `OIDC_LOGOUT` | `false` | If true, logout also ends the SSO session at the IdP (RP-initiated logout) |

## `forward`

!!! danger "The proxy must gate every route"

    The app performs no authentication of its own in this mode — it trusts the
    identity headers it is handed. If the app port is reachable directly,
    anyone can set that header themselves and **become admin**.

 Set `FORWARD_AUTH_SECRET` — a shared secret your proxy injects as
 `X-Forward-Auth-Secret` — so a stray direct request fails closed.

### `EMERGENCY_PORT` (forward mode only)

In `forward` mode the proxy fronts everything — so if your SSO stack is down,
even a valid local session cookie is useless, because the proxy never lets the
request through. `EMERGENCY_PORT` opens a second listener the proxy does **not**
front, so the admin's break-glass local password still works during an IdP
outage:

!!! warning "Publish it on a LAN/VPN address only"
 e.g. `192.168.1.10:5001:5001` — never to the internet. This listener is
    deliberately the one your proxy does **not** front, so it sits outside
    whatever gating the proxy provides.

- Identity headers are *never* trusted on this port, regardless of mode.
- It refuses to start unless `AUTH_MODE=forward` — in `local`/`oidc` the normal
  port already serves local logins, and an extra listener would be pure attack
  surface.

If you run `local` or `oidc`, ignore this option entirely.

## See also

- [Security & Threat Model](../operations/security.md) — how these modes fit the
  intended deployment.
- [Environment Variables](../reference/environment.md) — the consolidated table.
