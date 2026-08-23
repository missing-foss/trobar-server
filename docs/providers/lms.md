<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Lyrion Music Server (LMS)

The LMS provider gives Trobar playlists from a self-hosted **Lyrion Music
Server** (formerly Logitech Media Server / Squeezebox Server). Its playlist
entries are matched against your local library by artist/album/title, the
same as every other provider.

## Connecting

Enter the connection details in the setup wizard, or later under
[Administration → Provider connection](../administration.md#provider-connection)
(editable live, no restart):

- **Server URL** — your LMS base URL, including its web/JSON-RPC port (e.g.
 `http://192.168.1.10:9000`).
- **Username** and **Password** — only needed if you've turned on LMS's own
  **Settings → Security → "Authorize"** option. Leave both blank otherwise,
  the default for a typical home/LAN setup.

Trobar reads playlists only; it never writes to LMS.
