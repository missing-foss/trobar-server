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
    endpoint/credentials here. Playlists are read and written through the
    Subsonic API. Navidrome's own API is used for two things only, both
    for members' own playlists (below): listing the server's users, and
    giving a mirror copy to its owner.

## Members' own playlists (Navidrome)

On Navidrome, each member's playlists can be theirs in Trobar too, private
ones staying private, and their mirror copies can go into their own Navidrome
account.

- **The account Trobar uses must be a Navidrome admin**, here and for the
  mirror target. An admin account lists every user's playlists, private ones
  included, each with its owner. When you enter the connection details,
  Administration says whether the account is an admin. With an ordinary
  account, only the shared pass runs: the account's own playlists and other
  users' public ones, as household playlists.
- **Each member gives their Navidrome username** on Profile → Streaming
  accounts, with their Navidrome password. The password proves the username
  is theirs: Trobar signs in with it once to check it, then keeps only the
  username. The password is never stored or logged. One Navidrome user can
  belong to one member only, and the accounts Trobar itself uses can't be
  claimed: their playlists are the household's. A member can remove theirs on
  Profile, and an administrator can clear it under Administration → Users.
- **What a sync does with each playlist:**
    - a member's own is theirs, private to them until they share it, like
      any playlist they own;
    - one owned by the account Trobar uses, or public on Navidrome, is a
      household playlist, as before;
    - **a private one whose owner isn't a member yet is visible to
      administrators only**, until its owner gives their username.
- **Mirror copies** of a member's playlist are written as before, then given
  to the member's Navidrome account, so they appear in their own library.
  Trobar goes on updating them through the mirror account. If the member
  removes their username, the copies stay where they are.

Other Subsonic servers don't say who owns a playlist, so on them only the
shared pass runs.

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
