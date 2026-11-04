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
- **Username** and **Password** — a Subsonic account on that server. These
  credentials are read-only: Trobar uses them to read playlists.

Trobar *can* write to a Subsonic server, as a **mirror target** — a separate
connection you configure yourself. Mirroring creates and updates playlists on
that server. It is off until you enable it, and never touches a playlist it did
not create. See
[Playlists](../using/playlists.md#mirroring).

!!! tip
    If your server exposes more than one API dialect, use its **Subsonic**
    endpoint/credentials here. Native (non-Subsonic) APIs aren't used by this
    provider.

## How Trobar signs in, and what that risks

The Subsonic API signs each request with an MD5 hash of your password and a
fresh random salt, and that is the sign-in every server in this family shares.
Trobar can't replace it. MD5 is weak, and this matters in three ways:

- The hash and salt travel in each request's URL. Over plain `http://`, anyone
  who can see the traffic can reuse them, or try to work out the password from
  them offline. Server access logs can hold them too.
- The server has to be able to check the hash, so most Subsonic servers keep your
  password in a form they can read.
- Trobar needs the password itself to make the hash, so it keeps it in plain text
  in its database, like other provider credentials. See
  [Backups](../operations/backups.md).

So:

- Use an `https://` address if Trobar reaches the server over anything but a
  network you trust.
- Give Trobar its own account on the server, with a password you use nowhere else.
  If that account leaks, nothing else does.
- Give that account only the access it needs.
