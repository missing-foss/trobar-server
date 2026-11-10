<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Tidal (public playlists by link)

Paste the link to a **public** Tidal playlist and Trobar reads its track
list and matches it against your own library. It works like
[Spotify links](spotify-links.md) and [YouTube Music links](youtube-music.md),
in the same box on the same page. You need no Tidal account and don't sign
in.

This is separate from [linking a Tidal account](tidal.md). A linked account
brings in **your own** playlists. A link brings in **any public playlist**,
in full.

## Adding a playlist

1. Open **Playlists**.
2. Paste the link into **Add a public playlist by link** and press **Add**.
3. The import runs there and then, and the result appears under the link.

As with the other links, the playlist is yours and **private by default**.
Flip its Shared/Private pill if you want the rest of the household to see it.

These link shapes work:

- `https://tidal.com/browse/playlist/…`, the playlist's Share link;
- `https://tidal.com/playlist/…`;
- `https://listen.tidal.com/playlist/…`, the web player's address bar.

If the server's Tidal app isn't set up, the box says so instead of
importing: see [For the person running the server](#for-the-person-running-the-server).

## What you should expect it to find

- **The whole playlist.** There is no track limit.
- **Exact matches where your files carry an ISRC.** Tidal gives each track's
  ISRC (the recording's international code), so a track whose file has the
  same code matches on it before artist and title are compared. The rest
  match on artist and title, as for the other links.
- Public and unlisted playlists both work. A private one shows as
  **Unavailable**.

The count under each link works as it does for the other links: *"0 of 50
tracks in your library"* means the playlist's tracks aren't in your library,
not that the import failed. Tracks that miss land in the usual
[unresolved-tracks review](../using/playlists.md).

## What it does not do

- **It never writes to Tidal** and never signs anyone in.
- **It downloads nothing.** Trobar reads the track list and matches it
  against files you already have.
- **It sends nothing about you.** The requests name the playlist and carry
  the server's own Tidal app credentials, never a member's.

## When it breaks

A playlist that has been deleted, made private, or whose link was wrong
shows as **Unavailable**. Anything else shows as *"Couldn't read this
playlist from Tidal"*. Either way:

- **Nothing already imported is changed.** The playlist keeps its tracks,
  stays on any device it was synced to, and keeps its selections.
- The error stays on the link until a fetch succeeds. **Refresh** retries
  at once.
- **Remove** is the only thing that deletes the playlist.

## For the person running the server

These links read with the **Tidal app the server already registers** for
linked Tidal accounts: its client ID and secret, entered once under
Administration (see [Register the OAuth app](tidal.md#admin-register-the-oauth-app-once)).
Nothing else to configure, and no member needs to link an account. Without
that app, Tidal links are refused with a message saying so; the other link
kinds still work.

Tidal returns a playlist 20 tracks at a time, and after a few quick requests
asks the server to wait a few seconds. Trobar waits and carries on, so a
long playlist takes a little longer to import, not fewer tracks. If Tidal
keeps refusing, the fetch fails and the playlist stays as it was. Each link
costs about two requests per 20 tracks per sync.

Verified in November 2026 against Tidal's live API.
