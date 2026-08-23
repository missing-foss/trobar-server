<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Emby

The Emby provider gives Trobar playlists from your Emby server. Its playlist
entries are matched against your local library by artist/album/title, the
same as every other provider.

Emby and Jellyfin share the same lineage (Jellyfin is a 2018 fork of Emby's
server) and a near-identical API — this provider is a separate client from
Jellyfin's, and the two behave the same way from Trobar's side apart from how
they authenticate.

**Connecting to a Jellyfin server? Use the [Jellyfin](jellyfin.md) provider,
not this one.** Pointing the Emby provider at a Jellyfin server has
historically worked, because the two APIs are so close. It stops
authenticating on **Jellyfin 12.0**, which retires the Emby-style
`X-Emby-Token` header this client sends. The Jellyfin provider authenticates
the way Jellyfin keeps, and is unaffected. This is a change on Jellyfin's
side, not a Trobar deprecation — nothing changes for **Emby** servers, which
don't retire that header.

## Connecting

Enter the connection details in the setup wizard, or later under
[Administration → Provider connection](../administration.md#provider-connection)
(editable live, no restart):

- **Server URL** — your Emby base URL (e.g. `https://emby.example.com`).
- **API key** — created in Emby under **Settings → API Keys**.
- **Username** — the Emby user whose playlists Trobar reads.

Trobar reads playlists only; it never writes to Emby.

!!! note
    Emby is closed-source (Jellyfin is the FOSS fork) — that has no bearing
    on Trobar, which is only ever a client of your own server's documented
    API, the same relationship it has with Plex.
