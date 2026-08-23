<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# iTunes / Apple Music

Import playlists from an exported iTunes/Apple Music `Library.xml` — a
static, local playlist source layered on top of the
[Filesystem provider](filesystem.md). It works no matter which provider is
active (Roon, Jellyfin, or any other), the same as the `.m3u`/`.m3u8`
discovery it rides alongside: this isn't a provider of its own, it's an
extra playlist source the Filesystem provider owns and merges in.

## Connecting

1. Export your library: in Music.app (macOS) or iTunes (Windows), **File →
 Library → Export Library**. This produces a `Library.xml` file wherever
   you save it.
2. Make sure that file is reachable inside the container (put it somewhere
 under your `MUSIC_ROOT` mount, or add a separate volume mount for it).
3. Set **Administration → iTunes/Apple Music Library.xml path** to that
 file's path (e.g. `/music/Library.xml`) — editable live, no restart.

## What gets imported

Every user-created playlist in the library, including **smart playlists**
(the exported file already holds their resolved track list, so no rule
evaluation is needed on Trobar's side). The built-in per-media-type views
iTunes always creates — Library, Music, Movies, TV Shows, Podcasts,
Audiobooks, Purchased, Genius — are excluded; they aren't something you
curated.

Tracks are matched against your local library the same way every other
provider's playlists are: by the on-disk path recorded in `Library.xml`
when it resolves under `MUSIC_ROOT`, tolerant of a differing mount prefix
(the library was very likely exported from a different machine than the
one running Trobar) — falling back to artist/album/title otherwise.

## The one real limitation: it's a snapshot, not a live sync

Since macOS Catalina, Music.app no longer keeps `Library.xml` updated on
its own — it's a point-in-time export, not something Trobar can watch for
changes. If you add or edit playlists, **re-export and overwrite the same
file** — Trobar re-reads it fresh on every sync (the same way it re-reads
`.m3u` files), so the next sync picks up the change automatically. No
re-import step, no restart.
