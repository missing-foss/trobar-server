<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Spotify (public playlists by link)

Paste the link to a **public** Spotify playlist and Trobar reads its track
list and matches it against your own library. It works like
[YouTube Music links](youtube-music.md), in the same box on the same page.
No account, no sign-in and no credentials of any kind, for you or for
whoever runs the server.

This is separate from [linking a Spotify account](spotify.md). A linked
account brings in **your own** playlists, in full. A link brings in **any
public playlist**, up to its first 100 tracks, and needs no account.

!!! warning "Read this before you rely on it"
    Spotify's API doesn't serve this. Its playlist endpoint returns the
    tracks only to the playlist's owner or a collaborator, whatever token
    asks. Trobar reads the playlist from Spotify's **embed player page**
    instead: the one websites put in a frame to show a playlist. That page
    is not an API. It is undocumented, and Spotify can change it at any
    time without notice.

    When that happens, the link shows an error and nothing already imported
    is changed (see [When it breaks](#when-it-breaks)). Trobar can't make
    the page reliable.

## Adding a playlist

1. Open **Playlists**.
2. Paste the link into **Add a public playlist by link** and press **Add**.
3. The import runs there and then, and the result appears under the link.

As with YouTube Music links, the playlist is yours and **private by
default**. Flip its Shared/Private pill if you want the rest of the
household to see it.

These link shapes work:

- the playlist's Share link, `https://open.spotify.com/playlist/…`, with or
  without its `?si=…` part;
- the same with a language segment, such as `…/intl-fr/playlist/…`;
- an embed link, `…/embed/playlist/…`;
- a `spotify:playlist:…` URI.

A `spotify.link/…` short link is **not** accepted: it only says where it
leads by redirecting. Open it, then copy the full link from the address bar
or from the playlist's Share menu.

## What you should expect it to find

- **At most 100 tracks.** The embed page lists a playlist's first 100
  tracks, and gives no total. A playlist that comes back with 100 says
  so under its link: *"Spotify shares only the first 100 tracks of a
  playlist, so this one may be longer."* Tracks past the 100th are never
  read, so they don't show as unmatched either.
- **Artist and title, no album.** Matching uses artist and title only, as
  for YouTube Music. With several artists credited, they come through
  joined with commas.
- Editorial playlists (Spotify's own) and playlists made by users both work.

The count under each link works as it does for YouTube Music: *"0 of 50
tracks in your library"* means the playlist's tracks aren't in your
library, not that the import failed. Tracks that miss land in the usual
[unresolved-tracks review](../using/playlists.md).

## What it does not do

- **It never writes to Spotify** and never signs in.
- **It downloads nothing.** Trobar reads artist/title pairs and matches
  them against files you already have.
- **It does not do private playlists.** Those would need an account; use a
  [linked account](spotify.md) for your own.
- **It sends nothing about you.** The only request is for the playlist's
  embed page, made when you add a link and on each sync afterwards.

## When it breaks

A playlist that has been deleted, made private, or whose link was wrong
shows as **Unavailable**. Anything else, including the page changing shape,
shows as *"Couldn't read this playlist from Spotify"*. Either way:

- **Nothing already imported is changed.** The playlist keeps its tracks,
  stays on any device it was synced to, and keeps its selections.
- The error stays on the link until a fetch succeeds. **Refresh** retries
  at once.
- **Remove** is the only thing that deletes the playlist.

## For the person running the server

Nothing to configure, and no dependency beyond what the server already
uses. With no Spotify links added, nothing is contacted. Each link costs one
request per sync.

Verified in October 2026 against Spotify's live embed page.
