<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Playlists

Playlists come from your active library provider (Roon / Jellyfin / Emby /
Plex / LMS / Subsonic) and from each member's personal [Tidal](../providers/tidal.md) /
[Spotify](../providers/spotify.md) accounts, merged into one pool. Filesystem
alone has no provider playlists — see
[Providers overview](../providers/index.md). Any household member can also
paste the link to a public
[YouTube Music](../providers/youtube-music.md) or
[Spotify](../providers/spotify-links.md) playlist, with no account at all.

Two local sources merge in on top of that, whichever provider is active:
`.m3u`/`.m3u8` files under your music folder (always), and, when an admin
configures them, an [extra playlist folder](../providers/filesystem.md#playlists-your-player-keeps-somewhere-else)
outside it or an exported [iTunes/Apple Music](../providers/itunes.md)
library.

## How they sync

Select a playlist for a device like any other selection. It lands on the device
as an ordered **`.m3u8`** file at the sync-folder root — same name and order as
the source, listing only the tracks that are **actually on the device**. Entries
that resolve to files you don't have are skipped (Trobar only syncs files you
have).

`.m3u8` is UTF-8; Poweramp and most player apps read it directly. A few very old
DAP firmwares only read legacy `.m3u` — see
[Troubleshooting → Playlists on old players](../troubleshooting.md#playlists-on-old-players).

## Playlists made on a device

A playlist someone makes on a device, such as a DAP with an SD card or the
phone's own player, comes back into Trobar on the device's next sync. It
becomes a playlist of the device's owner, and from there it can be mirrored
anywhere that person's playlists go (Kodi, Jellyfin and the other sinks), as a
backup and so it's visible everywhere.

- **What's read.** The Android and desktop apps read every `.m3u` and `.m3u8`
  file in the sync folder, in any subfolder, except Trobar's own: the
  playlists Trobar sends to the device, which carry its marker line. Each
  file is sent to the server on every sync, together with the rest. In the
  playlist list, it shows the device's icon, with "From <device>" as its
  tooltip.
- **How entries are matched.** Players write entries relative to the playlist
  file, relative to the sync folder, or as full paths from their own storage
  (`/mnt/sdcard/…`). All three work for tracks Trobar put on the device. An
  entry that matches nothing in your library lands in the device's
  **unknown tracks**, like any other file on it Trobar didn't put there.
- **Private to start with**, like any playlist that has an owner. Share it
  with the usual toggle.
- **The device's copy wins.** It's the source: every sync rewrites the
  playlist's mirrors from it. An edit made in a mirror (in Kodi, in Jellyfin)
  is overwritten, as it is for every mirrored playlist.
- **Removing it** from the device removes it from Trobar on the next sync,
  along with its mirrors; devices it was sent to are told. Deleting the
  device in Trobar removes its playlists the same way. **Replacing** the
  device moves them to the new one.
- **Limits.** One sync sends at most 1000 playlists, of at most 20000
  entries each. Past that, the server refuses the whole upload: nothing is
  lost, but that device's playlists stop updating, and its sync shows the
  error, until it's back under the limits.
- **A playlist of yours is never overwritten.** If one of your files has the
  same name as a playlist Trobar sends to that device, yours stays and
  Trobar's isn't written there. Yours isn't read into Trobar either, since
  that name is Trobar's on that device; rename one of the two.

## Ownership & sharing

A playlist has an owner — and a sharing toggle — when it arrives through
something personal to one household member: **your own personally-linked
streaming account** (Tidal or Spotify), or a Trobar user an admin has mapped
to their own account on the active provider (Roon profile, or a Jellyfin/Emby
login — Administration > Configuration). Everything else — the household's
single configured provider account's own listing, and anything discovered
from the filesystem — has no owner and is always visible to the whole
household; there's no toggle to show for it.

For a playlist that *does* have an owner, sharing is **opt-in, not opt-out**:
new ones default to **Private**, visible only to you and the admin. This is
deliberate — the alternative publishes whatever you (or whoever's account was
just mapped) had linked, private streaming-service playlists included, to
everyone in the household before you've had a chance to notice or object.
Click the pill to make yours **Shared** once you're ready for the rest of the
household to see and sync it. Enforcement is real, not just a hidden row: a
household member can't sync a private playlist even by targeting it directly,
not only by browsing to it. A playlist that already existed before this
default changed keeps whatever sharing state it already had — this only
applies going forward, and to an ownership change (e.g. a mapping reassigned
to a different household member).

!!! note "Private means private from your housemates, not from the admin"
    The admin can see and sync every playlist regardless of its sharing
    setting — the same "admin is fully trusted" position that applies
    throughout Trobar (see [Security & Threat Model](../operations/security.md)).

### The "shared by *name*" badge is a different thing

A flat, non-clickable label — attribution, not a privacy control, and
unrelated to the Shared/Private pill above. It can appear on a **Roon**
playlist when the same playlist is *also* reachable through another
household member's directly-linked Tidal or Spotify account (their
"golden-source" copy):

- If you can already see their linked-account copy (it's shared, or you're
  the owner, or you're admin), the Roon duplicate is hidden entirely — you
  just see their copy instead.
- If you can't, you keep seeing the Roon row, labelled **"shared by *name*"**
  so its origin is clear.

That badge means *"this reaches you via the household's shared Roon
connection, and originates with *name*'s linked account"* — not *"*name*
shared this playlist with you"*. The Roon copy itself has no owner and is
never gated by the sharing toggle, regardless of the badge.

## Mirroring

A playlist can be mirrored to a local folder, a Subsonic/Navidrome server, a
Jellyfin server, an Emby server, Music Assistant or a Plex server. Six
independent mirror sinks are available on any playlist you can see,
each kept in sync automatically on every future playlist sync: as more of the
playlist's tracks show up in your library, the mirror grows to include them,
always listing exactly the currently-resolved subset in the original order.
Enabling one has no effect on the others — mirror to any combination.

A single **"Mirror…"** button on the playlist row opens a picker listing
only the sinks an admin has actually configured — nothing to pick from an
unconfigured target. A sink already mirroring shows greyed with a
checkmark; clicking it again turns it off. Once a sink is on, its icon
appears next to the playlist's title (a hand holding that sink's logo,
distinct from the plain provider icon that marks where the playlist came
*from*); the icon turns red and its tooltip names the problem if that
sink's last write failed — the picker itself, and **Administration >
Playlist mirrors**, have the full detail.

- **Filesystem** writes a Trobar-managed `.m3u` file in a folder the admin
  configures separately (a distinct, writable mount — never your read-only
  music library), in the playlist owner's own subfolder:
  `<user>/music/`, or `_shared/music/` for a playlist nobody owns. Each
  Kodi profile can read one user's folder: see [Kodi](../providers/kodi.md).
- **Subsonic** creates (and keeps replacing) a playlist on a
  Subsonic/Navidrome server the admin configures as a mirror target — a
  separate connection from the one used to *read* playlists if Subsonic is
  your active provider, even when it happens to point at the same server.
  Tracks are matched onto the target by artist/album/title, so this works
  best when the target server indexes the same music library Trobar does;
  a track it doesn't have is silently dropped from that copy rather than
  erroring.
- **Jellyfin** does the same against a Jellyfin server — its own
  independent mirror-target connection, again separate from an active
  Jellyfin provider connection even when both point at the same server.
  Matched the same way (artist/album/title), with the same silent-drop
  behavior for tracks the target doesn't have.

    One thing here depends on the **server's version**, not on anything
    Trobar chooses. If a source playlist lists the same track twice,
    Jellyfin 10.11 collapses it to a single entry, while Jellyfin 12.0
    keeps both — it dropped the constraint that made a repeat impossible.
    Measured on 10.11.11 and 12.0.0. So the same source playlist can
    produce a mirror with a different track count on two different
    servers, and neither is a fault: 12.0 reproduces what your playlist
    actually says.
- **Emby** does the same against an Emby server, matched and
  connected the same way as the Jellyfin sink. One Emby-specific quirk:
  Emby reliably reverts the mirror's descriptive comment a few seconds
  after any write that adds tracks (an internal metadata refresh it
  schedules itself, outside Trobar's control) — the playlist name and its
  track list are unaffected, only that one comment field.
- **Whose account, on Jellyfin and Emby.** A playlist that belongs to a
  household member whom an admin has mapped to their own Jellyfin or Emby
  account (Administration > Configuration) is mirrored into **that
  member's account**, so each person finds their own playlists there,
  as Kodi profiles do, and only they see it (on Jellyfin, a member's copy is
  made private; Emby keeps a user's playlists to that user anyway). That needs the mirror target to be **the same
  server as the library connection**, because the mapping is made on the
  library server; Trobar compares the two servers' ids once per sync. On a
  different server, and for a member without a mapping or a playlist with
  no owner, the copy goes into the mirror target's own account, as before.
  If the account a copy belongs in changes (a member is mapped, unmapped or
  remapped, or the mirror account is changed), the copy is moved: the old
  one is deleted and a new one written in the right account. A copy is
  never written into an account other than the one it was made in.

- **Music Assistant** keeps a copy in Music Assistant, through its one
  connection (the same one it is read through), since its playlists are
  shared by all its users. The copy's name ends in `_Trobar_`. Tracks are
  found by file path, then by artist/album/title. Music Assistant keeps a
  track at most once, so a repeated track is mirrored once; and it doesn't
  report refused tracks, so every write is confirmed by reading the copy
  back. Details, and which account's token to use, on the
  [Music Assistant](../providers/music-assistant.md#mirroring-into-music-assistant)
  page.

- **Plex** keeps a copy on a Plex Media Server the admin configures as a
  mirror target (its own URL and token, separate from a Plex provider
  connection even when both are the same server). The copy belongs to the
  token's Plex account, and its name ends in `_Trobar_`. Tracks are found by
  their folders and file name, then by artist/album/title. Plex keeps a track
  at most once, so a repeated track is mirrored once; and it accepts tracks
  it doesn't have without saying so, so every write is confirmed by reading
  the copy back. See [Plex](../providers/plex.md#mirroring-into-plex).

Writing back to a streaming provider isn't implemented.

- The filesystem mirror is identified by a distinctive name suffix and an
 internal marker line, so Trobar never overwrites or deletes a `.m3u` file
  you placed in that folder yourself — only files carrying its own marker.
  The Subsonic, Jellyfin, Emby, Music Assistant and Plex mirrors have no
  equivalent file-clobbering risk to guard against: every write either
  creates a fresh remote playlist or replaces one by the remote id Trobar
  itself stored on a previous write.
- An admin sets the mirror folder, or a Subsonic/Jellyfin/Emby/Plex mirror-target's
  URL/credentials, once each in **Administration > Configuration**; after
  that, mirroring a playlist to any sink is available to any household
  member, not just the admin. Clearing a mirror target's fields and saving
  disconnects it — every playlist mirroring to it starts failing gracefully
  with a clear "not configured" message rather than continuing to write
  against stale credentials.
- Renaming a playlist in Trobar renames its Subsonic, Jellyfin, Emby or Plex
  mirror to match on the next sync (not its Music Assistant copy, which only
  an admin can rename: turn that mirror off and on to recreate it); the filesystem mirror instead gets a
  fresh file under the new name (the old one is removed) since the
  marker/filename scheme is how that sink tracks identity.
- **Administration > Playlist mirrors** has two collapsible sections.
  **Targets** lists only the targets that are set up, each with its state:
  ✓ ready or connected; a warning when it is reachable but a playlist's last
  write to it failed; an error when the server can't be reached or the
  folder can't be written. Each line links to that target's settings.
  Lidarr sits apart, under **Requests**, since it isn't a mirror. With no
  mirror target set up, the section says so and links to set one up.
  **Playlists** lists every mirrored playlist with its coverage, one line
  per sink, each naming the sink and, if its last write failed (folder not
  writable, a naming conflict with a non-Trobar file, the target server
  unreachable, or none of the playlist's tracks resolving on the target),
  the reason. Collapsed, each section's header keeps a summary and its
  warnings. With nothing set up at all, the tab explains what mirroring is
  instead.

## Requesting missing albums from Lidarr

A playlist can have gaps: tracks that don't resolve to anything in your
library at all, not just tracks the current device's storage budget left
out. Where the gap is an entire missing **album**, and you run
[Lidarr](https://lidarr.audio/), "Request missing albums…" asks Lidarr to
start watching for it — one opt-in toggle per playlist, next to the Mirror…
button.

This is a request, not a mirror: nothing is copied anywhere, and nothing is
searched for immediately. Enabling it puts each missing album on Lidarr's
**wanted list, monitor-only** — Lidarr's own scheduled search is what
actually finds a release later, on its own timeline. Turning the toggle back
off stops *future* gaps from being requested; it never un-monitors or
removes anything already asked for. Every household member who can see the
playlist can toggle it — same visibility rule as the Shared/Private pill
above and the mirror sinks.

A few things shape when this can help:

- **Same album, requested once, ever — not once per playlist.** If the same
  missing album shows up as a gap in two different playlists, it's still
  only asked from Lidarr the first time either one is enabled. A later sync
  of the other playlist recognizes it's already been requested and does
  nothing further.
- **The button is disabled, with a hint explaining why, whenever nothing
  could happen if you clicked it** — either Lidarr isn't connected yet (ask
  an admin), or this specific playlist's source gives no album information
  on its unresolved tracks at all. Roon and iTunes/Apple Music playlists
  always fall in the second group; the artist/title Trobar has for an
  unresolved Roon or iTunes track doesn't come with an album name to look up.
- **A request that partially fails is not retried.** Lidarr's own API needs
  two calls to fully request an album; if the first succeeds but the second
  doesn't, that album is left exactly as a full lookup failure would be —
  recorded once, not retried automatically. An admin who notices one stuck
  can always finish it by hand directly in Lidarr.
- **Feedback is deliberately minimal**: a small "requested N albums, last
  run HH:MM" line, or an error if the last run hit one. There's no live
  polling of Lidarr's own state — the gap count on the playlist itself
  dropping over time, as Lidarr finds and Trobar's next scan picks up new
  files, is the real signal that a request worked.

An admin connects Lidarr once, in **Administration > Configuration**: a URL
and API key, then — once that pair is confirmed live — a root folder,
quality profile, and metadata profile chosen from Lidarr's own lists (a
"Refresh options" button fetches them). All three profile fields are
required before any playlist's toggle becomes usable. Clearing the URL/API
key and saving disconnects Lidarr entirely and also clears the three profile
choices, since they're specific to that one Lidarr instance and would be
wrong pointed at a different one. **Administration > Playlist mirrors**
shows Lidarr's state under **Requests** (just a check when all is well), and
lists Lidarr-enabled playlists alongside the mirrored ones.
