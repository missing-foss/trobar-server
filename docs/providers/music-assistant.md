<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Music Assistant

[Music Assistant](https://www.music-assistant.io/) works with Trobar in three
ways, over one connection:

- **As the library provider.** Choose it at first run or through
  [Change library source](../administration.md#changing-the-library-source).
  Its playlists sync and match like any provider's, and Trobar uses the
  artist images Music Assistant's metadata has, where it has some.
- **As an extra playlist source**, while another provider is active, the way
  `.m3u` files and linked Tidal accounts are. If you run Roon or Jellyfin as
  your library and Music Assistant for playback, you get both sets of
  playlists. When Music Assistant *is* the provider, its playlists are of
  course listed only once.
- **As a mirror target.** Any Trobar playlist can be mirrored into Music
  Assistant: see [Mirroring](#mirroring-into-music-assistant) below.

## Connecting

Music Assistant 2.x has its own user accounts, so Trobar needs a token.

1. In Music Assistant, open your user profile and, under **Long-lived access
   tokens**, choose **Create new token**. Name it so you can recognise it
   later, e.g. "Trobar", and copy it. **Which account:** reading works with
   any account, and so does mirroring, because Music Assistant's playlists
   are shared by all its users. Only one thing needs an admin account's
   token: deleting a mirrored copy when you stop mirroring a playlist. With
   a user account's token, Trobar empties that copy instead, and you can
   delete it in Music Assistant yourself.
2. In Trobar, go to **Administration → Configuration → Music Assistant**
   (under Playlist sources) and enter:
    - **Music Assistant URL** — its web address, including the port (8095 by
      default), e.g. `http://192.168.1.10:8095`.
    - **Long-lived token** — the token from step 1.
3. Save. The next playlist sync includes Music Assistant's playlists.

To make Music Assistant the library provider, use **Change library source**
on the Library source card, which asks for the same URL and token. At first
run, pick it in the setup wizard.

!!! note "The token expires after a year"
    A long-lived token is valid for one year and is not renewed by use.
    (Music Assistant 2.9.9's own profile page says ten years, but the server
    issues it with a one-year expiry.) When it expires, the card shows the
    connection as disconnected and Music Assistant's playlists stop
    updating. Create a new token and save it.

To disconnect, clear both fields and save. Playlists already synced from Music
Assistant stay in Trobar until Music Assistant is connected again and no
longer lists them.

## Which playlists appear

Only playlists that someone made. Music Assistant also lists playlists it
generates itself (random tracks, infinite mix, recently played, all
favourites); those are left out, and so are Trobar's own mirrored copies.

## How tracks are matched

- **Tracks from Music Assistant's local files.** Music Assistant knows each
  file's path inside its music folder. When Music Assistant reads the same
  music folder as Trobar (a common setup), the track is matched by that
  path, exactly. A different mount point for the same files still matches.
- **Everything else** (a streaming provider's track, a URL) has no file path
  and is matched by artist and title, as for the other providers. A track you
  don't have in your library is flagged and skipped, by design.
- Entries that aren't tracks (radio stations, podcast episodes, audiobooks)
  are left out, not counted as missing.

## Mirroring into Music Assistant

Turn it on per playlist with **Mirror… → Music Assistant**, as for the other
mirror targets. Trobar then keeps a copy of the playlist in Music Assistant,
rewritten on every playlist sync:

- The copy is named after the playlist with `_Trobar_` at the end, the same
  marker the `.m3u` mirror uses. Trobar only ever writes to, empties or
  deletes a playlist it created; a playlist you made in Music Assistant is
  never touched. Its copies are never read back into Trobar as playlists.
- Tracks are found in Music Assistant's library by their file path, also when
  its music folder is mounted at a different depth, and otherwise by artist,
  album and title. A track Music Assistant doesn't have is left out of the
  copy, as on the other servers.
- Music Assistant keeps a track **at most once** per playlist. A Trobar
  playlist that repeats a track is mirrored with its first occurrence.
- Music Assistant adds tracks in the background and doesn't say which it
  refused, so every write is checked by reading the copy back. If it doesn't
  hold what was written, the playlist shows a mirror error ("doesn't hold
  what was written") rather than a clean write. When the copy already holds
  the right tracks, nothing is written.
- If you delete the copy in Music Assistant, the next sync creates a new one.
- Renaming a playlist in Trobar doesn't rename its copy: Music Assistant
  only lets an admin rename. Turn mirroring off and on again to recreate the
  copy under the new name.

## Versions

Verified against Music Assistant **2.9.9**, for all three roles: reading
playlists, and creating, rewriting, emptying and deleting mirrored copies,
with an admin and with a user account's token. Earlier versions have not
been tested. Artist images were not observed on the test library (its files
carry no album-artist tags), so how often Music Assistant has one for an
artist depends on your library and its metadata.
