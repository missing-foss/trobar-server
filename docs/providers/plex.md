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

Trobar reads playlists only; it never writes to Plex.
