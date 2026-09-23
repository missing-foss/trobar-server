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
historically worked, because the two APIs are so close. Against a default
**Jellyfin 12.0** server it stops authenticating: the Emby-style
`X-Emby-Token` header this client sends is one of the legacy authorization
mechanisms 12.0 ships turned off, and the request comes back `401`. Measured
on 10.11.11, where that header authenticates, and on 12.0.0, where it does
not.

The header is disabled, not removed. A Jellyfin administrator can turn the
legacy mechanism back on server-side (`EnableLegacyAuthorization`), after
which this provider authenticates against that server again — so a 12.0
server may or may not accept it, depending on a setting Trobar cannot see.
The advice is the same either way: use the Jellyfin provider for a Jellyfin
server. It authenticates the way Jellyfin keeps, which is unchanged on both
versions. This is a change on Jellyfin's side, not a Trobar deprecation —
nothing changes for **Emby** servers.

## Connecting

Enter the connection details in the setup wizard, or later under
[Administration → Provider connection](../administration.md#provider-connection)
(editable live, no restart):

- **Server URL** — your Emby base URL (e.g. `https://emby.example.com`).
- **API key** — created in Emby under **Settings → API Keys**.
- **Username** — the Emby user whose playlists Trobar reads.

These credentials are read-only: Trobar uses them to read playlists.

Trobar *can* write to an Emby server, as a **mirror target** — a separate
connection you configure yourself. Mirroring creates and updates playlists on
that server and can delete items from them. It is off until you enable it, and
never touches a playlist it did not create. See
[Playlists](../using/playlists.md#mirroring-to-a-local-folder-a-subsonicnavidrome-server-a-jellyfin-server-or-an-emby-server).

!!! note
    Emby is closed-source (Jellyfin is the FOSS fork) — that has no bearing
    on Trobar, which is only ever a client of your own server's documented
    API, the same relationship it has with Plex.
