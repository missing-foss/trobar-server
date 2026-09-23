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

These credentials are read-only: Trobar uses them to read playlists.

Trobar *can* write to a Jellyfin server, as a **mirror target** — a separate
connection you configure yourself. Mirroring creates and updates playlists on
that server and can delete items from them. It is off until you enable it, and
never touches a playlist it did not create. See
[Playlists](../using/playlists.md#mirroring-to-a-local-folder-a-subsonicnavidrome-server-a-jellyfin-server-or-an-emby-server).

## Notes

- **Give the Jellyfin library Trobar reads or mirrors into a music collection
  type.** In a library with no collection type, **10.11.11** classifies an
  `.ogg` file as video: it is absent from the audio walk the mirror index
  builds, so the track is silently left out of a mirrored playlist, and read
  back out of a playlist it arrives carrying its folder's name and no artist,
  which artist/album/title matching cannot match either. A `.flac` beside it is
  unaffected. **12.0.0** classifies the same file as audio on both paths. In a
  library typed as music the two versions agree and there is nothing to do —
  this only bites an untyped library on 10.11.

- Looking for **Emby**? It's API-near-identical to Jellyfin (Jellyfin is a
  2018 fork of Emby's server) and has its own provider — see [Emby](emby.md).
  Use that one only for an actual Emby server: the Emby provider authenticates
  with a header that **Jellyfin 12.0** ships turned off, so an Emby connection
  pointed at a default 12.0 server does not authenticate. A Jellyfin
  administrator can turn that legacy mechanism back on, so the outcome depends
  on a server setting; the advice is the same either way. This provider is
  unaffected — its own `Authorization: MediaBrowser Token="…"` authenticates
  on 10.11.11 and 12.0.0 alike.
