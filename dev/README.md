<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Trobar — local dev environment

A one-command local stack for developing/testing Trobar, with **no external
accounts and no production dependencies**. It runs on **rootless Docker**, so
nothing needs root or the docker group.

## What you get

| Service | URL | Purpose |
|---|---|---|
| **trobar** | http://localhost:5000 | the app, in `local` auth mode |
| **navidrome** | http://localhost:4533 | Subsonic-API server (test the Subsonic provider) |
| **jellyfin** | http://localhost:8096 | Jellyfin (test the Jellyfin provider) |
| **lidarr** | http://localhost:8686 | Lidarr (test #494's "request missing albums") |
| lastfm-mock | (internal) | fake Last.fm — Suggestions / similar-artists / auto-fit with no key |
| music-seed | (one-shot) | generates the test library, then exits |

The **test library** is generated on first `up`: a handful of invented artists
and albums (`dev/testlib.json`) rendered as real, tagged, silent FLACs with
embedded cover art — copyright-free, a few KB each. The same manifest drives the
Last.fm mock, so Suggestions and the similar-artists strip reference music that
actually exists locally.

## Run it

```bash
cd dev
docker compose up --build          # first run also builds images + seeds music
```

Then open **http://localhost:5000** and complete the first-run wizard:
1. create the admin account (username defaults to `admin`),
2. music path — leave it at `/music`,
3. provider — pick **Filesystem** (works immediately, no setup),
4. finish → it scans the test library.

Everything filesystem-based (browse, select, per-device sync state, cover
caching, storage-budget auto-fit) works right away. Suggestions and
similar-artists work too, via the mock — set a Last.fm username (any value) in
your profile; the mock ignores the key.

Stop with `docker compose down`. Wipe everything (including the seeded music and
all app data) with `docker compose down -v`.

## Testing the Subsonic / Jellyfin providers (optional)

These need a one-time account setup in each server, then wiring into Trobar's
**Administration** panel (Profil → Administration → Library source):

- **Navidrome (Subsonic):** open http://localhost:4533, create the admin user.
  In Trobar's admin panel choose **Subsonic**, URL `http://navidrome:4533`, and
  that username/password.
- **Jellyfin:** open http://localhost:8096, complete the setup wizard, add
  `/music` as a **Music** library, then Dashboard → API Keys → mint one. In
  Trobar choose **Jellyfin**, URL `http://jellyfin:8096`, that API key, and your
  Jellyfin username.

(Trobar reaches them by service name on the compose network — `navidrome` /
`jellyfin` — not `localhost`.)

## Testing Lidarr album requests (#494, optional)

Lidarr is an *acquisition* target, not a library source or a mirror — it's
never seeded from `/music`, and the whole point is requesting albums Trobar's
library doesn't have.

- Open http://localhost:8686, complete the setup wizard, then Settings →
  General → Security → mint an API key. Add a root folder (any writable
  path under `/config` works — #494's requests are monitor-only, nothing
  actually needs to download).
- In Trobar's admin panel, connect **Lidarr** with URL `http://lidarr:8686`
  and that API key, click "Refresh options", then pick the root
  folder/quality profile/metadata profile you just created.
- On the Playlists tab, toggle "Request missing albums…" on a playlist with
  unresolved gaps that have album metadata (Subsonic/Jellyfin/Emby-sourced
  playlists — Roon/iTunes playlists never have album data on unresolved
  rows, so the button stays disabled there by design).

## Notes

- **Rootless Docker:** all volumes live under the running user's
  `~/.local/share/docker`; nothing touches a system Docker or a production stack.
- **Last.fm mock:** implements only the four methods the app calls
  (`user.getInfo`, `user.getTopAlbums`, `artist.getSimilar`,
  `user.getRecentTracks`). Point real Last.fm back by unsetting
  `LASTFM_API_BASE` on the `trobar` service. To test against *live* Last.fm,
  set a real `LASTFM_API_KEY` and remove the `LASTFM_API_BASE` override.
- **Regenerating the library:** edit `dev/testlib.json` and
  `docker compose up --build --force-recreate music-seed` (or `down -v` for a
  clean slate). The seeder is idempotent — it only writes files that don't exist.
- The `trobar` image here is built with `AUTH_MODE=local` and
  `SESSION_COOKIE_SECURE=0` (plain-http dev); production uses the root
  `docker-compose.yaml` instead.
