<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Jellyfin

The Jellyfin provider gives Trobar playlists from your Jellyfin server. Its
playlist entries are matched against your local library by artist/album/title,
the same as every other provider.

## Connecting

Enter the connection details in the setup wizard, or later under
[Administration → Provider connection](../administration.md#provider-connection)
(editable live, no restart):

- **Server URL** — your Jellyfin base URL (e.g. `https://jellyfin.example.com`).
- **API key** — created in Jellyfin under **Dashboard → API Keys**.
- **Username** — the Jellyfin user whose playlists Trobar reads.

Trobar reads playlists only; it never writes to Jellyfin.

## Notes

- Looking for **Emby**? It's API-near-identical to Jellyfin (Jellyfin is a
  2018 fork of Emby's server) and has its own provider — see [Emby](emby.md).
  Use that one only for an actual Emby server: the Emby provider authenticates
  with a header that **Jellyfin 12.0** retires, so an Emby connection pointed
  at a Jellyfin server stops working after that upgrade. This provider is
  unaffected.
