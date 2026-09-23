<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# YouTube Music (public playlists by link)

Paste the link to a **public** YouTube Music playlist and Trobar reads its
track list and matches it against your own library, like any other playlist
source. No account, no sign-in, no credentials of any kind — for you or for
whoever runs the server.

It is not a library provider: there is nothing to activate, nothing to switch
to, and it does not take the place of Roon/Jellyfin/Subsonic or whatever else
you use. Each link you add becomes one more playlist in the same pool.

!!! warning "Read this before you turn it on"
    This feature is built on `ytmusicapi`, which talks to YouTube Music's
    **internal, unofficial endpoints**. Those are reverse-engineered rather
    than published, and Google has never promised they will keep working or
    keep their shape. Nothing about reading a public playlist without signing
    in changes that.

    In practice this means the feature can stop working, at any time, without
    warning and without anything being wrong on your side. Trobar is built to
    fail visibly rather than quietly when that happens — see
    [When it breaks](#when-it-breaks) — but it cannot make an unofficial
    endpoint reliable.

## Adding a playlist

1. Open **Playlists**.
2. Paste the link into **Add a public playlist by link** and press **Add**.
3. The import runs there and then, and the result appears under the link.

Any household member can add their own. What comes back is a playlist owned
by you and **private by default**, exactly like a personally-linked Tidal or
Spotify playlist — flip its Shared/Private pill if you want the rest of the
household to see it. Nothing you add is visible to anyone else, and no one
else's links are visible to you.

Any of these link shapes work: the playlist's Share link, the address bar
while it is playing (`…/watch?v=…&list=…`), the `www`/`m`/`youtu.be`
variants, or the bare playlist id. Re-pasting a different link to the same
playlist refreshes the one you already have rather than adding a second copy.

## What you should expect it to find

Track **artist and title** come through cleanly on song-based playlists,
which are the two fields the matcher uses. **Album is almost always absent**
on user-curated playlists, which costs nothing for matching but means there
is no album to disambiguate two recordings of the same song.

Quality is a property of the playlist, not of Trobar, and **you cannot tell
in advance from the link** — the same URL shape covers both:

- A playlist of songs — matches about as well as any other playlist source.
- A playlist of hour-long "mix" videos — imports perfectly and matches
  essentially nothing, because the entries are 90-minute uploads whose
  "artist" is the uploader.

Two more ways a playlist can be misleading rather than empty: re-uploader
channels put the channel name in the artist field, and live or alternate
takes can match your studio recording **confidently and wrongly**. If a
playlist matters to you, look at what it actually resolved to rather than
trusting the count.

This is why the count is shown under every link. **"0 of 79 tracks in your
library"** is the feature telling you that playlist is not usable — not that
you own none of the music. Tracks that miss land in the usual per-playlist
[unresolved-tracks review](../using/playlists.md), same as every other
source.

## What it does not do

- **It never writes to YouTube Music.** There is no write path in the code
  at all — no liking, no adding, no playlist creation.
- **It downloads nothing.** Trobar reads a list of artist/title pairs and
  matches them against files you already have. Music that is only on
  YouTube stays only on YouTube.
- **It does not do private playlists.** Public links only. A private one
  would need an account, which is the whole cost this feature exists to
  avoid.
- **It sends nothing about you.** No account, no identifier, no listening
  history — the only outbound request is "give me this public playlist",
  made when you add a link and on each sync afterwards.

## When it breaks

A playlist that has been deleted, made private, or whose link was wrong
shows as **Unavailable**. A refusal from YouTube's side shows as a fetch
failure. In both cases:

- **Nothing already imported is changed.** Tracks stay in the playlist,
  the playlist stays on any device you synced it to, and your selections
  are untouched. A source that can refuse for its own reasons must never
  be able to delete your things by doing so.
- The error stays visible on the link until a fetch succeeds. Use
  **Refresh** to retry immediately rather than waiting for the next sync.
- **Remove** is the only thing that deletes the playlist, because that is
  you asking for it. Devices are told to remove the files, the same as
  deleting any other playlist.

## Why only YouTube Music

The obvious question is why this is not "paste any playlist link". The
answer inverts what you would expect, and it is worth stating so nobody
spends an afternoon proposing it:

| Service | Public playlist by link, no account? |
|---|---|
| YouTube Music | **Works** — via the unofficial endpoints above. |
| Spotify | **Not possible.** Its Get Playlist endpoint returns the track list only for playlists the authenticated user owns or collaborates on. No token of any kind reads a stranger's public playlist. |
| Tidal | **Not today.** Playlist endpoints need a user token whether or not the playlist is public. TIDAL has said it is considering opening public-playlist endpoints to the client-credentials flow; if that ships, it becomes the first *sanctioned* backend for this feature. |

So the one service where this works has no sanctioned API, and the two with
sanctioned APIs specifically forbid it. That is why this page is named for
YouTube Music rather than dressed up as a general link importer.

## For the person running the server

Nothing to configure. The feature has no settings, no credentials and no
admin panel entry: a subscription is the whole of its configuration, and it
is per-user.

With no links added anywhere, **nothing is contacted** — the dependency is
not even imported until the first fetch. If `ytmusicapi` is missing from an
install, adding a link reports a failure and everything else keeps working.
