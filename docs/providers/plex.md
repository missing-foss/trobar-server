<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Plex

The Plex provider gives Trobar playlists from your Plex Media Server. Its
playlist entries are matched against your local library by artist/album/title,
the same as every other provider. Unlike Jellyfin/Subsonic, there's no
separate username to set — the token itself is already scoped to the Plex
account it belongs to.

## Connecting

Enter the connection details in the setup wizard, or later under
[Administration → Provider connection](../administration.md#provider-connection)
(editable live, no restart):

- **Server URL** — your Plex Media Server's base URL (e.g.
 `http://192.168.1.10:32400`).
- **Token** — an `X-Plex-Token` for the server, found via
  [Plex's own instructions](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

Trobar's own copies, from [mirroring into Plex](#mirroring-into-plex), are
never read back as source playlists: they're the ones whose name ends in
`_Trobar_`.

## Mirroring into Plex

Any Trobar playlist can be mirrored into a Plex Media Server as a Plex music
playlist, kept up to date on every playlist sync like the
[other mirror targets](../using/playlists.md#mirroring).

- **Set it up** under Administration → Configuration → Playlist mirroring:
  **Plex mirror-target URL** and **Plex token**. It is its own connection,
  separate from the Plex provider's even when both point at the same server.
- **Whose playlists.** A Plex playlist belongs to the account whose token is
  used, so that is where the copies land. A Plex Home managed user's token
  would put them in that user's playlists; that case hasn't been tested
  here, since only plex.tv issues those tokens.
- **Which tracks.** A track is found in Plex by its last two folders and its
  file name (the two music roots differ: `/music/…` in Plex's container,
  something else in Trobar's), then by artist, album and title. Tracks Plex
  doesn't have are left out of the copy. If none of a playlist's tracks are
  in Plex, nothing is written and the playlist shows why.
- **What Plex does with it.** A Plex playlist holds a track at most once, so
  a Trobar playlist that repeats a track is mirrored with its first
  occurrence. Plex accepts a track it doesn't have without an error, so
  every write is checked by reading the copy back; a difference shows as an
  error on the playlist, never as a clean write.
- **Name and summary.** The copy is named after the playlist, with
  `_Trobar_` at the end, and its summary says how many of its tracks are
  present. Renaming the playlist renames the copy.
- **Removed in Plex.** A copy deleted in Plex is recreated on the next sync.
  A copy Plex can't read for a moment (an error, or no answer) is left
  alone, and nothing is written until it answers again.
- **Stopping.** Turning the mirror off deletes the copy.

Verified against Plex Media Server **1.43.4**.
