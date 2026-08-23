<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Trobar

A self-hosted, multi-user music library sync: pick artists, albums, or
playlists and keep them offline on Android phones, tablets, watches, dedicated
audio players (DAPs), or any mass-storage device — an SD card, a USB drive, a
laptop's own folder. Files are copied as-is by default — byte-for-byte, no
quality loss — with optional per-device MP3 transcoding for space-limited cards.

## Why Trobar

- **Filesystem-first.** Your library lives on disk in whatever folder structure
  you already have. A [provider](providers/index.md) (Roon, Jellyfin, Emby,
  Plex, LMS, or any Subsonic server) is an optional layer for playlists — the
  core sync never depends on one being online, or existing at all.
- **Your files, untouched.** What's in your library is what lands on the device,
  byte-for-byte — unless you *ask* for MP3 transcoding on a device, in which case
  the server does the conversion on the fly, never altering
  your originals.
- **Multi-user by design.** Every household member gets their own account,
  devices, and selections, with optional [delegation](using/delegation.md).
- **Works with or without SSO.** Local accounts, OpenID Connect, or a ForwardAuth
  proxy — your choice. See [Authentication Modes](getting-started/authentication.md).
- **No telemetry, no CDN.** Nothing phones home; every web asset is served from
  your own instance. See [Security](operations/security.md).

The name is Occitan for "to find" — the verb the troubadours got theirs from.
The full story is in [Why "Trobar"](project/why-trobar.md).

## Documentation map

- **[Getting Started](getting-started/installation.md)** — install & deploy,
  first-run wizard, and authentication modes.
- **[Providers](providers/index.md)** — Roon, Jellyfin, Emby, Plex, LMS, the
  Subsonic ecosystem, Filesystem, and personal Tidal / Spotify accounts.
- **[Using Trobar](using/library-selections.md)** — library & selections,
  suggestions, playlists, devices & storage, delegation.
- **[Clients](clients/index.md)** — the Android, desktop, and Garmin apps.
- **[Administration](administration.md)** — the admin-panel reference.
- **[Operations](operations/networking.md)** — networking, security, backups,
  upgrading.
- **[Troubleshooting](troubleshooting.md)** — the sharp edges and their fixes.
- **[Reference](reference/environment.md)** — every environment variable, the
  architecture/sync protocol, the read-only [Integration
  API](reference/integration-api.md), and the [Home Assistant
  integration](reference/home-assistant.md).

Source and issues live on
[GitHub](https://github.com/missing-foss/trobar-server).
