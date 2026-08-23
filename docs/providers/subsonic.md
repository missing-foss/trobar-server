<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Subsonic ecosystem

The Subsonic provider speaks the Subsonic API, so it works with any
Subsonic-compatible server for playlists. That covers a wide FOSS ecosystem:

- **Navidrome**
- **Airsonic** / Airsonic-Advanced
- **Gonic**
- **Ampache** (Subsonic API)
- **Funkwhale** (Subsonic API)
- **Koel** (Subsonic API)

Any server implementing the Subsonic API should work; the ones above are the
common FOSS choices. Playlist entries are matched against your local library by
artist/album/title.

!!! note
    Subsonic API compatibility varies by server — some implement only a
    subset. Trobar calls five endpoints, and they matter differently:

 - **`ping.view`** — the connectivity/pairing check. A server that
      doesn't support this fails immediately at setup, before you ever get
      to playlists — you'll know right away, not partway through an import.
 - **`getPlaylists` / `getPlaylist`** — the actual playlist data this
      provider exists for.
 - **`getArtists` / `getCoverArt`** — artist images, which degrade
      gracefully (no crash, just no image) if unsupported.

    Navidrome, Gonic, and Ampache are known-good on all five. Funkwhale
 implements only a Subsonic subset — confirm `ping.view` and the
    playlist endpoints actually work there before relying on it.

## Connecting

Enter the connection details in the setup wizard, or later under
[Administration → Provider connection](../administration.md#provider-connection)
(editable live, no restart):

- **Server URL** — your server's base URL.
- **Username** and **Password** — a Subsonic account on that server. Trobar
  reads playlists only.

!!! tip
    If your server exposes more than one API dialect, use its **Subsonic**
    endpoint/credentials here. Native (non-Subsonic) APIs aren't used by this
    provider.
