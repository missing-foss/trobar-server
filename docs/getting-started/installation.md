<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Installation & Deployment

Trobar runs as a single Docker container — Flask + SQLite — alongside your
existing music library, which is mounted **read-only**. It serves plain HTTP on
its container port and expects to sit behind a TLS-terminating reverse proxy.

## Quick start

```bash
git clone https://github.com/missing-foss/trobar-server
cd trobar-server
cp .env.example .env   # set MUSIC_VOLUME, ADMIN_USERNAME at minimum
docker compose up -d --build
```

The container serves via the [waitress](https://pypi.org/project/waitress/)
WSGI server and runs as an **unprivileged user (uid 10001)**, not root.

## The two mounts that matter

- **`MUSIC_ROOT`** — your music library, mounted **read-only**. Trobar never
  writes to it. Any mount protocol works (local disk, NFS, SMB): the app only
  does plain filesystem reads.
- **`DATA_DIR`** — the SQLite database, the cover/artist-image caches, and user
  avatars. Make this a **persistent volume**, back it up, and treat it like a
  password store — it holds provider credentials and the session key. See
  [Backups & DATA_DIR](../operations/backups.md).

!!! danger "`DATA_DIR` must be on local disk — not a network share"

 Note the contrast with `MUSIC_ROOT` above. Trobar's database is SQLite,
    which needs working file locking. **NFS, SMB/CIFS and similar network
    shares don't provide it reliably, and can corrupt the database** — losing
    your selections, device pairings and playlist state.

    This is not a theoretical risk. It doesn't fail at startup; it fails later,
    under concurrent access, as corruption rather than a clear error.

 A NAS-mounted `MUSIC_ROOT` is completely fine — Trobar only ever reads it,
 so no locking is involved. It's `DATA_DIR` specifically that must live on
    the machine running the server.

    **If you want your data on the NAS, back it up there rather than running it
    from there.** See [Backups & DATA_DIR](../operations/backups.md).

 Trobar prints a startup warning if it detects `DATA_DIR` on a network
    filesystem, but it can't catch every case — so place it deliberately.

### DATA_DIR ownership

Because the container runs as uid `10001`, that user must be able to write
`DATA_DIR`:

- A **fresh named volume** inherits the right ownership automatically.
- A **bind mount** from a host path (the reference compose defaults to
 `./data`) must be `chown`ed to uid `10001` first, or the app can't write
 there. Upgrading from a pre-10001 (root) image needs a one-time `chown` — see
  [Upgrading](../operations/upgrading.md).

## Required environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MUSIC_ROOT` | `/music` | Path, inside the container, to your music library (mounted read-only) |
| `DATA_DIR` | `/data` | Where the SQLite database, avatars, and caches live — mount this as a persistent volume |
| `AUTH_MODE` | `local` | How users authenticate — see [Authentication Modes](authentication.md) |
| `ADMIN_USERNAME` | *(unset)* | The username promoted to admin. In `local` mode it also **locks first-run admin creation to this username** — set it **before first launch**. |

The full list of every variable, required and optional, is on the
[Environment Variables](../reference/environment.md) reference page.

## Reverse proxy

Run Trobar behind a TLS-terminating reverse proxy (Traefik, Caddy, nginx, …).
The `docker-compose.yaml` in this repo binds the app only to `127.0.0.1:5000`
and expects your own reverse-proxy config in front of it — it doesn't ship
configuration for any specific proxy; any proxy works. **The container port
must never be exposed directly to the internet.** Full details, including
split-horizon DNS for LAN clients, are on
[Networking & Reverse Proxy](../operations/networking.md).

## Next

- [First Run & Setup Wizard](first-run.md) — claim the admin account and pick a
  provider.
- [Authentication Modes](authentication.md) — `local`, `oidc`, or `forward`.
