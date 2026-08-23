<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Environment Variables

The consolidated list of every variable Trobar's `.env.example` sets up for
you — almost all of them read directly by the app itself; a couple (marked
below) are compose-only and never touch `os.environ` at all. The **source of
truth** is
[`.env.example`](https://github.com/missing-foss/trobar-server/blob/main/.env.example)
in the repo — copy it to `.env` and edit. The table here mirrors it for
reference.

## Required

| Variable | Default | Purpose |
|---|---|---|
| `MUSIC_ROOT` | `/music` | Path, inside the container, to your music library (mounted read-only) |
| `DATA_DIR` | `/data` | SQLite database, avatars, and caches — mount as a persistent volume ([Backups](../operations/backups.md)) |
| `AUTH_MODE` | `local` | `local` \| `oidc` \| `forward` — see [Authentication Modes](../getting-started/authentication.md) |
| `ADMIN_USERNAME` | *(unset)* | Username promoted to admin; in `local` mode also locks first-run admin creation to it |

## Auth: OIDC (only when `AUTH_MODE=oidc`)

| Variable | Default | Purpose |
|---|---|---|
| `OIDC_ISSUER` | *(unset)* | IdP issuer URL (discovery at `{issuer}/.well-known/openid-configuration`) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | *(unset)* | Credentials from the app registered in your IdP |
| `OIDC_SCOPES` | `openid profile email` | Requested scopes |
| `OIDC_USERNAME_CLAIM` | `preferred_username` | ID-token claim mapped to the app username |
| `OIDC_LOGOUT` | `false` | If true, logout also ends the SSO session at the IdP |

## Auth: forward (only when `AUTH_MODE=forward`)

| Variable | Default | Purpose |
|---|---|---|
| `FORWARD_AUTH_SECRET` | *(unset)* | Shared secret the proxy injects as `X-Forward-Auth-Secret`; when set, requests without it are rejected (fail-closed). Strongly recommended. |
| `EMERGENCY_PORT` | `0` (disabled) | Second, un-proxied listener for break-glass local login during an SSO outage. **Never expose to the internet.** `forward` mode only. |

## Integrations (optional)

| Variable | Default | Purpose |
|---|---|---|
| `ROON_HOST` / `ROON_PORT` | *(unset)* / `9330` | Roon Core address — can also be set later in the admin UI ([Roon](../providers/roon.md)) |
| `LASTFM_API_KEY` | *(unset)* | App-wide fallback Last.fm key; each user can set their own in their profile ([Suggestions](../using/suggestions.md)) |
| `MIRROR_ROOT` | *(unset)* | Container path for playlist mirroring's `.m3u` output — only meaningful with a matching writable volume mount; the `app_config` mirror folder set in Administration overrides this, same override-over-env-var relationship `MUSIC_ROOT` has ([Playlists — Mirroring](../using/playlists.md#mirroring-to-a-local-folder-a-subsonicnavidrome-server-a-jellyfin-server-or-an-emby-server)) |

## Deployment (optional)

| Variable | Default | Purpose |
|---|---|---|
| `TZ` | `UTC` | Container timezone — affects log timestamps and anything the server formats itself. **It does not change the times shown in the web UI**: those are stored in UTC and rendered in *your browser's* timezone, so they are correct for whoever is looking, including someone travelling. |
| `TROBAR_TRUSTED_PROXY` | `*` | Peer(s) waitress trusts to set `X-Forwarded-For`/`-Host`/`-Proto`. **Security-relevant**: also what the per-IP brute-force rate limiter trusts for "who is this request really from" — see [Networking & Reverse Proxy](../operations/networking.md#trusted-proxy-and-rate-limiting) before pinning this away from the default |
| `LOG_LEVEL` | `WARNING` | How much Trobar logs: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Raise it to diagnose a provider problem — `INFO` adds things like Tidal/Spotify retry breadcrumbs. An unrecognised value warns and falls back to `WARNING`. |

!!! tip "`LOG_LEVEL=DEBUG` is safe to leave on while you diagnose"
    It applies to **Trobar's own** logging only, never to the libraries
    underneath. That's deliberate: HTTP libraries log full request URLs at
    debug level, and some music servers carry authentication in the query
    string — so a blanket debug setting would write credentials into your logs.
    Trobar's doesn't.

    If you need that wire-level detail for a provider bug, it's better raised
    as an issue than switched on in a running install.

!!! note "Listening-history API bases"
 `LASTFM_API_BASE` (default `http://ws.audioscrobbler.com/2.0/`) and
 `LISTENBRAINZ_API_BASE` (default `https://api.listenbrainz.org`), if set,
    provide the default when the equivalent Administration fields are left
    blank — see
    [Suggestions](../using/suggestions.md#pointing-at-self-hosted-services).

## Advanced / development (optional)

Not in `.env.example` — these are for local development and troubleshooting,
not a typical household deployment.

| Variable | Default | Purpose |
|---|---|---|
| `SESSION_COOKIE_SECURE` | `1` (secure) | Set to `0`/`false`/`no` to drop the session cookie's `Secure` flag for plain-HTTP local development — over real HTTPS, leave this at its default or login breaks. See [SECURITY.md](https://github.com/missing-foss/trobar-server/blob/main/SECURITY.md). |
| `DEV_USER` | `dev` | `forward` mode only: username used when the proxy's identity header is absent — a local-dev convenience, never seen behind a real ForwardAuth proxy that always sets it. |
| `TROBAR_DEV_SERVER` | *(unset, off)* | Set to `1`/`true`/`yes` to run Flask's Werkzeug development server (reloader/debugger) instead of waitress. **Local development only** — Werkzeug's dev server isn't built to be efficient, stable, or secure. |
