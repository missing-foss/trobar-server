<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# First Run & Setup Wizard

The first visit as the admin opens a short setup wizard. In `AUTH_MODE=local`
the very first login attempt creates the admin account — which is why you set
`ADMIN_USERNAME` **before first launch**, so a stranger who reaches a fresh
instance first can't claim it (see [Authentication Modes](authentication.md)).

## The wizard steps

### 1. Confirm the music root

The wizard verifies the folder is readable and warns if it is mounted
writable (it should not be — Trobar treats your library as read-only truth).

### 2. Pick a provider

A provider is where playlists and library niceties come from. One provider is
active at a time.

| Provider | What you get | What it needs |
|---|---|---|
| Filesystem | Library browsing + sync only, zero dependencies | Nothing — just the music folder |
| Roon | Playlists, live pairing status | A Roon Core on the LAN |
| Jellyfin | Playlists | Server URL + API key + username |
| Emby | Playlists | Server URL + API key + username |
| Plex | Playlists | Server URL + token |
| Lyrion Music Server | Playlists | Server URL (+ user/password if secured) |
| Subsonic | Playlists | Server URL + user + password (Navidrome, Gonic, Airsonic, …) |

Each has its own page under [Providers](../providers/index.md), including the
[Roon pairing dance](../providers/roon.md). Switching provider later (from the
[Administration](../administration.md) panel) clears playlists and the
artist-image cache — selections, devices, and users survive.

!!! note "Artist images are separate"
    Artist pictures are configured independently of the provider: set a free
    [TheAudioDB](https://www.theaudiodb.com) API key in Administration and
    pictures come from there. Without a key, the active provider and any image
    sitting in the artist's own folder act as fallbacks. See
    [API keys](../administration.md#api-keys).

### 3. Listening history (optional)

An optional app-wide default Last.fm API key, used as a fallback for any user
who hasn't connected their own account. Leave it blank to skip — every user
can still connect their own Last.fm or ListenBrainz account later from their
profile (ListenBrainz needs no key at all), and this can be set later too. See
[Suggestions](../using/suggestions.md).

Clicking **Finish** completes setup and starts the first library scan in the
background, then takes you straight to the library — there's no separate
"scan in progress" screen to wait on. Plan for a few minutes per 10k tracks on
network storage; incremental rescans afterwards only touch changed files. More
on scanning in [Library & Selections](../using/library-selections.md).

## Next

- [Authentication Modes](authentication.md) — pick and finish wiring up auth.
- [Providers](../providers/index.md) — connect Roon/Jellyfin/Emby/Plex/LMS/
  Subsonic, or stay on Filesystem.
- [Clients](../clients/android.md) — pair your first device.
