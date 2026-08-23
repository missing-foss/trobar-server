<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Filesystem

The Filesystem provider is the zero-dependency default: it needs nothing beyond
your music folder. Pick it when you don't run Roon/Jellyfin/Emby/Plex/Subsonic,
or when you just want library browsing and sync without a live server.

## What you get

- Full **library browsing** by artist and album, batch selection, and sync to
  any device — the whole core of Trobar.
- **Suggestions** still work (they come from Last.fm/ListenBrainz and your
  library, not from a provider) — see [Suggestions](../using/suggestions.md).
- **Local playlists**, always — any `.m3u`/`.m3u8` file anywhere under your
  music folder is picked up automatically, whichever provider is active. Entries
  are matched against your library by artist/album/title, or by exact path when
  the playlist file carries one.
- **iTunes/Apple Music libraries** → see [iTunes / Apple Music](itunes.md) —
  an optional import source layered on this provider, also works whichever
  provider is active.

## What you don't get

- **Server-side provider playlists** — those come only from Roon, Jellyfin,
  Emby, Plex, or a Subsonic server. You can still get playlists onto devices via
  a personal [Tidal](tidal.md) or [Spotify](spotify.md) account, or the local
  sources above, all of which layer on independently of the active provider.

## Folder conventions

Trobar reads tags first and falls back to the `Artist/Album/Track` folder layout
when tags are missing or malformed. Files that fit neither land under "Unknown
Artist/Album" — the Library health panel counts these so you can fix the tags
and rescan. More in [Library & Selections](../using/library-selections.md).
