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
  the playlist file carries one. One folder **outside** your music library can
  be scanned the same way — see [below](#playlists-your-player-keeps-somewhere-else).
- **iTunes/Apple Music libraries** → see [iTunes / Apple Music](itunes.md) —
  an optional import source layered on this provider, also works whichever
  provider is active.

## What you don't get

- **Server-side provider playlists** — those come only from Roon, Jellyfin,
  Emby, Plex, or a Subsonic server. You can still get playlists onto devices via
  a personal [Tidal](tidal.md) or [Spotify](spotify.md) account, or the local
  sources above, all of which layer on independently of the active provider.

## Playlists your player keeps somewhere else

Discovery walks your music folder, which is where most players that export
`.m3u` will put a playlist if you point them at it. Some don't put it there
at all: a player may keep its playlists in a directory of its own, next to
its settings and database rather than inside your library. The files are
usually ordinary `.m3u` — it is only the *location* that Trobar can't see.

Set **Administration → Extra playlist folder** to that directory and it is
walked exactly as your music folder is: `.m3u`/`.m3u8` at any depth, entries
matched against your library the same way. It's editable live, no restart,
and it takes effect whichever provider is active.

A few things worth knowing:

- **It is read-only.** Trobar never writes there, and never touches the
  files it finds. That's the difference between this and the mirror output
  folder ([Mirroring](../using/playlists.md#mirroring-to-a-local-folder-a-subsonicnavidrome-server-a-jellyfin-server-or-an-emby-server)),
  which is the write side.
- **It must not overlap your music folder or the mirror folder** — Trobar
  refuses the setting if it does, in either direction. Overlapping either
  one means the same playlist gets imported twice, once through each root.
- **Entries that aren't full paths are read as relative to your music
  folder**, not to the playlist file — there's no music next to the
  playlist file, so that's the only reading that can mean anything.
- **One folder**, not a list. If two players each keep their own, mount
  both underneath a single folder and point this at that.
- **A folder that isn't there is not an error** — a volume you forgot to
  mount costs you those playlists, and changes nothing else.

## Folder conventions

Trobar reads tags first and falls back to the `Artist/Album/Track` folder layout
when tags are missing or malformed. Files that fit neither land under "Unknown
Artist/Album" — the Library health panel counts these so you can fix the tags
and rescan. More in [Library & Selections](../using/library-selections.md).
