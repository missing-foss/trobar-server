<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Lyrion Music Server (LMS)

The LMS provider gives Trobar playlists from a self-hosted **Lyrion Music
Server** (formerly Logitech Media Server / Squeezebox Server). Its playlist
entries are matched against your local library by artist/album/title, the
same as every other provider.

## LMS under another name

Several appliance distributions and turnkey music servers **run Lyrion Music
Server itself**, under their own branding. If you have one of these, this is
the provider to use — there is nothing extra to install, and nothing about
Trobar to configure differently. Point it at the LMS instance the appliance
already exposes.

- **Daphile** — built on Squeezebox Server, LMS's earlier name.
- **piCorePlayer** — LMS is an optional component, installed from
  piCorePlayer's own web interface. A piCorePlayer box set up purely as a
  player has no LMS on it; one with the LMS component installed does.
- **Max2Play** — ships an installer for the server on its images.
- **Innuos** — innuOS runs LMS internally, behind its own Sense interface.

!!! note
    Trobar speaks to one thing: **JSON-RPC over HTTP at
    `<server>/jsonrpc.js`**, conventionally port 9000 — the same interface
    LMS's own web UI is served from. Anything exposing a stock LMS HTTP
    interface works; an appliance that wraps LMS in its own front end may
    restrict or alter it, and that is the case to check first if a connection
    that should work does not.

    The four above were confirmed against each project's or vendor's own
    documentation as running LMS. They have **not** been tested against
    Trobar, and the list is not exhaustive — any LMS install works whatever
    the box is called. Innuos documents reaching its internal LMS on port
    9000; the others do not state a port, so use whatever their own LMS
    settings show.

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
