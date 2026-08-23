<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Security

This document describes Trobar's intended deployment, its threat model, the
hardening that is in place, and the access boundaries that are **intentional**
(so a self-hoster can make an informed decision rather than discover them by
surprise). It complements the auth-mode documentation in the
[README](README.md#auth-modes).

## Reporting a vulnerability

Preferred: GitHub's **private vulnerability reporting** — "Report a
vulnerability" under the repository's Security tab (enabled on all Trobar
repositories). Or email **missing_foss@etik.com** with details and, if
possible, a way to reproduce. Please don't open a public issue for anything
exploitable until it's been addressed.

## Intended deployment & threat model

Trobar is a **self-hosted, household-scale** application. It is designed to run:

- **behind a TLS-terminating reverse proxy** (Traefik, Caddy, nginx, …). The app
  speaks plain HTTP on its container port and relies on the proxy for HTTPS; it
 honours `X-Forwarded-Proto`/`-Host` from that proxy (and reads
 `X-Forwarded-For` for per-IP rate limiting). **The container port must not be
  published directly** — only the proxy should reach it. This isn't just a
  best practice: the per-IP brute-force backoff (login, device pairing,
 device tokens) trusts `X-Forwarded-For` from any peer by default
 (`TROBAR_TRUSTED_PROXY=*`), which is correct and sufficient exactly because
  the shipped compose makes that port unreachable any other way. If it's
  ever reachable directly, that same trust lets an attacker supply their own
 `X-Forwarded-For` and rotate it every request, silently defeating the rate
 limiter. `TROBAR_TRUSTED_PROXY` can be pinned to a narrower value, but an
 incorrect pin is *worse* than leaving it at `*` — see
  [Networking & Reverse Proxy](docs/operations/networking.md#trusted-proxy-and-rate-limiting)
  before changing it.
- among **mutually-trusting users**. Everyone with an account is a member of one
  household sharing one music library. The trust boundary is "authenticated vs.
 not", plus a single **admin** (`ADMIN_USERNAME`) for app-wide configuration.
  It is *not* built to host mutually-distrusting tenants isolated from each
  other's library (see [Access boundaries](#access-boundaries-intentional)).

Out of scope for the threat model: a malicious authenticated household member
attacking other members; a malicious admin; an attacker with filesystem access
to `DATA_DIR` or the host.

## Hardening in place

- **Authentication.** Three modes (`local`, `oidc`, `forward`) — see the
 [README](README.md#auth-modes). In `local`/`oidc` the app authenticates every
 request itself and **never trusts identity headers**; `forward` mode trusts a
  ForwardAuth proxy and must sit behind one (optionally locked with
 `FORWARD_AUTH_SECRET`). Default is fail-closed `local`.
- **First-run admin claim.** In `local` mode the first `POST /login` on a
  fresh instance (zero users) creates the sole **admin** account. Set
 `ADMIN_USERNAME` and that bootstrap is only honoured for that exact
  username — anyone else who reaches the instance first is rejected, so they
 can't claim admin and lock out the operator. **If you leave `ADMIN_USERNAME`
 unset, the first request to reach `/login` — whoever it is — becomes admin;
  do not expose a local-mode instance until you have completed first login.**
 (`oidc`/`forward` modes provision the admin externally and are unaffected.)
- **Session cookies** are set `Secure`, `SameSite=Lax`, `HttpOnly`
 explicitly. `Secure` can be disabled for plain-HTTP local development with
 `SESSION_COOKIE_SECURE=0`.
- **Brute-force backoff.** A per-IP failure counter throttles password login
 (10 failures / 5 min) and device Bearer-token auth (30 / 5 min) with `429`s.
  Passwords are stored as Werkzeug PBKDF hashes; device tokens are
 `secrets.token_urlsafe(32)` stored only as SHA-256.
- **CSRF.** `SameSite=Lax` plus an `Origin`-header check that rejects
 state-changing requests carrying a cross-origin `Origin`. The device Bearer
  API (no ambient cookie) and the OIDC handshake are exempt.
- **Android client.** The pairing Bearer token is stored **Keystore-encrypted
  at rest** (AES-256-GCM, non-exportable key — hardware-backed where available),
  so it can't be recovered from a raw dump of app storage. Transport is HTTPS
 only (`usesCleartextTraffic=false`) with standard certificate validation (no
 custom trust manager), and `allowBackup=false` keeps the token out of
 `adb backup` / cloud auto-backup.

## Secrets at rest (`DATA_DIR`)

`DATA_DIR` (default `/data`, a mounted volume) holds everything sensitive:

- the SQLite database — **password hashes** and **plaintext provider
  credentials** (Subsonic password, Jellyfin API key, per-user Last.fm keys,
  Roon host/port, per-user Tidal OAuth refresh tokens — unlike the
  others, a Tidal refresh token is a credential to that specific
  household member's own Tidal account, not a shared admin-configured one);
- `flask_secret_key` — the session-signing key (whoever holds it can forge
  session cookies);
- `roon_token.json` — the Roon Core pairing token;
- `avatars/` — uploaded profile images.

**Provider credentials are stored in cleartext by design.** The app needs the
usable secret to authenticate to each provider on every sync, and this is a
single-file self-hosted database — encrypting at rest would only move the key
problem elsewhere on the same host without raising the bar against the actual
threat (someone who can read `DATA_DIR`). So **file permissions are the security
boundary**, not encryption:

- Trobar sets `DATA_DIR` to `0o700` and the secret-key / token / database files
 to `0o600` on creation (best-effort — some bind-mounted filesystems ignore
 `chmod`).
- **You should:** treat `DATA_DIR` like a password store — keep it on a volume
  only the container's user can read, and **back it up encrypted**. Do not commit
 it or bake it into an image (the `Dockerfile` copies only `app/`, and
 `.gitignore` covers `data/`, `*.db`, and `roon_token.json`).

## Access boundaries (intentional)

These follow from the household-trust model above. They are deliberate; tighten
them yourself if your deployment needs stricter isolation.

- **Mount the music library read-only.** Trobar only ever *reads* your library
 (it's a sync source) — there is no code path that writes to `MUSIC_ROOT`. Mount
 it read-only (`:ro` in docker-compose, as the reference compose does) so a bug
  or a compromise can never modify or delete your files. Trobar logs a warning at
 startup if `MUSIC_ROOT` is not a read-only mount.
- **The admin is fully trusted.** `ADMIN_USERNAME` can point the library root
 (`POST /api/setup/music-root`) at *any* directory the container can read, edit
  provider config, and manage users. There is no allowed-root allowlist — an
  admin already has broad capability by definition. If you want to constrain it,
  run the container with a narrowly-scoped mount so nothing else is reachable.
- **A paired device can only read tracks it was actually offered.**
 `GET /api/device/file/<track_id>` checks the requesting device's own
 `device_track_state` (status `pending`/`downloaded`) before serving a track,
  and returns 404 for anything outside that set — a stolen/copied device token
 (a plaintext credential on a card by design) reaches only
  that one device's own selections, not the whole library. This is
  defense-in-depth for the lost/stolen-card case, not a change to the
  household-trust model: everyone in the household still ultimately controls
  what's selected for their own devices via the shared selections UI. (Path
  traversal is not possible: file paths come from the scanner-populated
 database, never from client input, and are resolved under `MUSIC_ROOT`.)
- **Library scans** (`POST /api/library/scan`) are triggerable by any
 authenticated user, but a lock makes a concurrent trigger a fast `409` no-op,
  so repeated requests can't pile up multiple multi-minute NFS walks.

## Serving in production

Trobar serves via the **waitress** production WSGI server (`python main.py`
runs it in-process) — not Flask's Werkzeug development server, which is not
built to be efficient/stable/secure. It runs as a **single multithreaded
process on purpose**: the scan lock, the transcode-concurrency limit, and the
per-IP brute-force counters are all in-process state, so a multi-process worker
model (e.g. `gunicorn -w 2`) would silently break each of them. For local
development you can opt back into the Werkzeug server (reloader/debugger) with
`TROBAR_DEV_SERVER=1`.

This still expects the intended deployment — a household instance behind a
trusted, TLS-terminating reverse proxy. Keep that proxy (and, in `forward`
mode, the ForwardAuth gate) in front; never publish the app (or
`EMERGENCY_PORT`) directly to the internet. Response security headers
(`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`), a suppressed
`Server` banner, and a 4 MB request-body cap are set by the app itself;
**HSTS** and a full **Content-Security-Policy** are best terminated at the proxy
(a strict in-app CSP is deferred — Alpine.js needs `unsafe-eval`).

The container runs as an **unprivileged user, uid 10001** (not root) and
carries a `HEALTHCHECK`. The image's `/data` is owned by that uid, so a **named
volume inherits it**; if you **bind-mount** `DATA_DIR` from the host, `chown` the
host directory to uid `10001` (or run the container with a matching `--user`)
so the app can write its database, secret key, and avatars.
