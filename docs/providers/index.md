<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Providers overview

A **provider** is the optional layer that gives Trobar playlists and library
niceties on top of the core filesystem sync. The core — browse your library,
pick artists/albums, sync them to devices — never depends on a provider being
online, or existing at all. Choose one when you set up (see
[First Run](../getting-started/first-run.md)).

## The two kinds of source

Trobar distinguishes two ways music reaches a playlist:

- **Library provider** — one active at a time (Filesystem, Roon, Jellyfin,
  Emby, Plex, Lyrion Music Server, a Subsonic-compatible server, or Music
  Assistant). This is
  the shared, admin-configured source of truth for playlists and, for Roon,
  live pairing status.
- **Personal streaming accounts** — each household member links their **own**
  account (Tidal, Spotify) from their own Profile, and their playlists merge
  into the shared pool alongside the library provider's. The admin only
  registers the OAuth app once; see each provider's page.

Whatever the source, playlist entries are matched against the files in **your**
library by artist/album/title — Trobar only ever syncs files you actually have.
Tracks that exist only on a streaming service are flagged and skipped by design.

## Choosing a library provider

| Provider | What you get | What it needs |
|---|---|---|
| [Filesystem](filesystem.md) | Library browsing + sync, zero dependencies | Nothing — just the music folder |
| [Roon](roon.md) | Playlists, live pairing status | A Roon Core on the LAN |
| [Jellyfin](jellyfin.md) | Playlists | Server URL + API key + username |
| [Emby](emby.md) | Playlists | Server URL + API key + username |
| [Plex](plex.md) | Playlists; also a mirror target | Server URL + token |
| [Lyrion Music Server](lms.md) | Playlists | Server URL (+ user/password if secured) |
| [Subsonic ecosystem](subsonic.md) | Playlists | Server URL + user + password |
| [Music Assistant](music-assistant.md) | Playlists, artist images where it has them; also a mirror target | Server URL + long-lived token |

Switching the active provider later goes through **Change library source…**
in [Administration](../administration.md#changing-the-library-source). A
playlist the new provider also has (same name and owner, most of the same
tracks) is carried over with its settings; the old provider's other playlists
and the artist-image cache are removed. The counts are shown before you
confirm. Playlists from every other source keep their
rows and settings. A device with one of the removed playlists selected loses
that selection, and its next sync removes the playlist's file and any tracks
no other selection keeps on it. Your library, users, devices, and artist,
album and track selections are untouched.

## Local import sources

- [iTunes / Apple Music](itunes.md) — import playlists from an exported
 `Library.xml`. Not a library provider itself (nothing to activate) — an
  extra playlist source the Filesystem provider merges in, working no
  matter which provider is active.

## Other playlist sources

- [Music Assistant](music-assistant.md) — playlists made in Music Assistant,
  added alongside the active provider when it isn't the provider itself. An
  admin enters its URL and a long-lived token once; the same connection
  serves it as a library provider and as a mirror target.

## Personal streaming accounts

- [Tidal](tidal.md) — per-user OAuth linking; playlists with source attribution.
- [Spotify](spotify.md) — per-user OAuth linking (**validation pending** — see
  the page).

## Public playlists by link

- [YouTube Music](youtube-music.md) — paste the link to a public playlist and
  its tracks are matched against your library. No account and no credentials,
  for you or for the server. Built on an **unofficial** interface, which the
  page explains before anything else — read it before relying on this.
- [Spotify](spotify-links.md) — the same for a public Spotify playlist, up to
  its first 100 tracks. Read from Spotify's embed player page, which is not
  an API either; the page says what that means.

### Services with no direct integration (Roon only)

Two services Roon supports as backends — **KKBOX** and **Qobuz** — have no
direct Trobar integration, for different reasons. Their playlists still sync
through the [Roon](roon.md#streaming-backends-tidal-qobuz-kkbox) provider like
any other Roon content; you just don't get a direct link or a source-attribution
badge. The reasoning is documented on the Roon page.

## Looking for a service not listed here?

See [Providers we don't support](other.md) — Deezer, YouTube Music, Apple
Music, Qobuz, and KKBOX, with the reason for each and, where one exists,
what would change it.
