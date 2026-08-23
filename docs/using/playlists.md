<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Playlists

Playlists come from your active library provider (Roon / Jellyfin / Emby /
Plex / LMS / Subsonic) and from each member's personal [Tidal](../providers/tidal.md) /
[Spotify](../providers/spotify.md) accounts, merged into one pool. Filesystem
alone has no provider playlists — see
[Providers overview](../providers/index.md).

## How they sync

Select a playlist for a device like any other selection. It lands on the device
as an ordered **`.m3u8`** file at the sync-folder root — same name and order as
the source, listing only the tracks that are **actually on the device**. Entries
that resolve to files you don't have are skipped (Trobar only syncs files you
have).

`.m3u8` is UTF-8; Poweramp and most player apps read it directly. A few very old
DAP firmwares only read legacy `.m3u` — see
[Troubleshooting → Playlists on old players](../troubleshooting.md#playlists-on-old-players).

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

## Mirroring to a local folder, a Subsonic/Navidrome server, a Jellyfin server, or an Emby server

Four independent mirror sinks are available on any playlist you can see,
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
  music library).
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
- **Emby** does the same against an Emby server, matched and
  connected the same way as the Jellyfin sink. One Emby-specific quirk:
  Emby reliably reverts the mirror's descriptive comment a few seconds
  after any write that adds tracks (an internal metadata refresh it
  schedules itself, outside Trobar's control) — the playlist name and its
  track list are unaffected, only that one comment field.

This is the full set of sinks in the cross-provider mirroring RFC — writing
back to Plex or a streaming provider isn't implemented yet.

- The filesystem mirror is identified by a distinctive name suffix and an
 internal marker line, so Trobar never overwrites or deletes a `.m3u` file
  you placed in that folder yourself — only files carrying its own marker.
  The Subsonic, Jellyfin, and Emby mirrors have no equivalent file-clobbering
  risk to guard against: every write either creates a fresh remote playlist
  or replaces one by the remote id Trobar itself stored on a previous write.
- An admin sets the mirror folder, or a Subsonic/Jellyfin/Emby mirror-target's
  URL/credentials, once each in **Administration > Configuration**; after
  that, mirroring a playlist to any sink is available to any household
  member, not just the admin. Clearing a mirror target's fields and saving
  disconnects it — every playlist mirroring to it starts failing gracefully
  with a clear "not configured" message rather than continuing to write
  against stale credentials.
- Renaming a playlist in Trobar renames its Subsonic, Jellyfin, or Emby
  mirror to match on the next sync; the filesystem mirror instead gets a
  fresh file under the new name (the old one is removed) since the
  marker/filename scheme is how that sink tracks identity.
- **Administration > Playlist mirrors** lists every currently-mirrored
  playlist, per sink, with its coverage and, if a write ever fails (folder
  not writable, a naming conflict with a non-Trobar file, the target server
  unreachable, or none of the playlist's tracks resolving on the target),
  the reason why.

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
lists Lidarr-enabled playlists alongside the mirror sinks, for the same
at-a-glance visibility.
