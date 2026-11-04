<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Changelog

What changed for someone running Trobar, by version, in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) shape. A change that
needs something from you when you upgrade links to its section in
[Upgrading](docs/operations/upgrading.md), which has the steps.

A release's date is its tag's. Every release tag needs a section here: the
release workflow refuses a tag without one.

The oldest entry here is 2.4.0, and not every version since has one. Earlier
history is in the git log.

## [Unreleased]

## [2.17.0]

### Security

- The pinned Python dependencies are up to date. This clears the published
  advisories that 2.16.0's pins carry in Authlib, Brotli, certifi, joserfc,
  Pillow, urllib3 and Werkzeug.
- Forward auth refuses a request without the identity header, and warns at
  start-up when it runs on the development server's fallback.
- Pairing a provider is admin-only.
- Changing a password asks for the current one. Logout, a password change or a
  reset end a session; sessions otherwise slide over a set lifetime.
- Last.fm uses HTTPS by default, and its API key stays out of the log.
- Rate-limit entries are dropped once their window has passed.
- OIDC login now requires the identity provider to return the nonce. A provider that doesn't will refuse logins. Standard providers, Authentik included, are not affected.
  See [Upgrading](docs/operations/upgrading.md#oidc-login-requires-the-nonce-2170).

### Added

- Rule playlists: a playlist filled from a rule over your library, refreshed on
  every playlist sync.
- Playlists made in Trobar: create, fill from the Library, reorder, rename and
  delete, and add a single track from an album's track list.
- Upload a `.m3u` or `.m3u8` as a playlist, and download any playlist as `.m3u8`.
- Share a Tidal playlist with another member, one-way or two-way, once an admin
  allows it. Its invitations show as a badge in the header.
- Choose which selected playlist a watch plays.
- Playlist sync on a schedule, and after a library scan that changed tracks.
- Each playlist mirror shows what it holds, run by run.
- Administration can run the setup wizard again.
- Cancel a running job. Jobs stop cleanly when the server shuts down.
- `/healthz`, a cheap health check.
- A backup recipe for a running server.
- AIFF files are scanned.
- Album covers: a picture beside the files counts when none is embedded, and
  thumbnails have a small cached variant.
- Finished transcodes are kept in a capped disk cache.
- Sync to… shows, per device, what it holds, this send, and its room.
- App API level 3: make playlists from the phone.
- Devices can report plays, kept only for someone who turned that on. While
  it's on, they feed that person's Suggestions and Most played. While it's off,
  they stay stored but aren't used. The switch is under Profile, with a way to
  delete what is stored.
- Plays you keep can go on to your own Maloja, ListenBrainz or Last.fm, each its own
  switch: new plays only, once each. Their credentials are stored encrypted.
- App API level 4: see what is selected for a device from the phone, and remove it.
- The App API serves a Playlists widget for the app's Home: what has most left to sync to a device.
- Clicking an album opens it: a larger cover, the year and the track list, with the album or one track to add to the basket or a playlist. A checkbox on the cover now selects several albums, and the list view's Tracks button is gone.
- App API level 5: an album's tracks.
- App API level 6: the library by album artist, with what an artist appears on.
- A playlist added by a public link carries a link badge in the playlist list, and each link in the "Add a public playlist by link" panel shows its provider's icon.

### Changed

- Pairing codes last 20 minutes, not an hour.
  See [Upgrading](docs/operations/upgrading.md#pairing-codes-last-20-minutes-2170).
- Playlists match by MusicBrainz recording id, after a one-time tag re-read.
  See [Upgrading](docs/operations/upgrading.md#playlists-match-by-musicbrainz-recording-id-a-one-time-tag-re-read-2170).
- The first library scan reads every file once.
  See [Upgrading](docs/operations/upgrading.md#the-first-library-scan-reads-every-file-once-2170).
- ALAC in `.m4a` is now transcoded.
  See [Upgrading](docs/operations/upgrading.md#alac-in-m4a-is-now-transcoded-2170).
- New downloads go into album-artist folders.
  See [Upgrading](docs/operations/upgrading.md#new-downloads-go-into-album-artist-folders-2170).
- An album selection includes the album's guest tracks.
  See [Upgrading](docs/operations/upgrading.md#an-album-selection-includes-the-albums-guest-tracks-2170).
- The Library groups albums by album artist, and an artist selection
  includes theirs.
  See [Upgrading](docs/operations/upgrading.md#the-library-groups-albums-by-album-artist-an-artist-selection-includes-theirs-2170).
- Playlists have one Actions menu per row. Devices has a tab of its own.
- Five actions that throw something away now ask first, and switching off Keep
  device content says how many tracks will go.
- Matching uses the album to break ties, takes ISRCs from Tidal and Spotify,
  and compares normalised titles.
- Device-made playlists: a match by file name is a suggestion to accept.
- The selections table locks boxes, confirms an untick and offers Forget.
- Suggestions and Most played say when a history source didn't answer, and those
  services' answers are kept for five minutes.
- The image holds no unit tests or build-only inputs.
- Accessibility: named form controls, one dialog behaviour, a visible focus ring,
  a skip link, and larger touch targets.

### Fixed

- A library scan no longer holds up logins and syncs while it reads files, and
  writing a playlist's mirrors no longer holds them up while it reaches the
  target.
- A library scan refuses to remove most of the library, and keeps what it
  couldn't read. A moved file keeps its track, and a file vanishing mid-scan no
  longer stops the scan.
- A rescan reaches the devices, and a playlist's changes reach the devices that
  select it.
- A failed transcode no longer sends an empty success.
- An unreachable identity provider no longer fails sign-in or logout.
- Setup wizard: a rejected setting stops the step.
- Web UI: failed calls report an error instead of passing silently.
- Basket: an older answer can no longer bring sent items back.
- Log out works: the menu sent a request the server refuses. It's hidden where
  a proxy owns the session.
- An artist's count of albums they appear on now matches the list when two albums share a name.
- Scheduled and after-scan playlist syncs now run on an install older than the setup wizard, which never recorded setup as done. Such an install can't go back to an older build without restoring its backup.
  See [Upgrading](docs/operations/upgrading.md#going-back-to-2160-needs-the-backup-2170).
- The Playlists tab no longer sits empty for seconds after a page load on a large library: the selection list reads its album counts in one query, and the playlists no longer wait for it.
- A playlist from a linked Tidal or Spotify account no longer also appears as Music Assistant's copy of it.
- Switching away from Roon stops the connection to it at once, with no restart, and saving settings no longer reconnects it.
- Tidal playlists are listed from the address Tidal now serves them at. Tidal retired the old one, which made each Tidal sync fail at that first step.

## [2.16.0]

### Added

- Mirror targets: Plex, Music Assistant, and Jellyfin and Emby mirrors that write
  each member's copy into their own account.
- Playlists made on a device are read back into Trobar and sent to the mirrors.
- Public Spotify playlists by link, an unofficial source capped at 100 tracks.
- Music Assistant as a library provider and an extra playlist source.
- Maloja as a listening-history source.
- Lidarr: request a playlist gap's missing album.
- App API level 2, with the playlist section.
- Home: each paired device keeps its own widget set, and devices can have an icon.
- The Occitan rosette and five themes.

### Changed

- Filesystem mirrors move into per-user folders.
  See [Upgrading](docs/operations/upgrading.md#filesystem-mirrors-move-into-per-user-folders-2160).
- Pairing shows a short code for every device.
  See [Upgrading](docs/operations/upgrading.md#pairing-shows-a-short-code-for-every-device-2160).
- Administration: named, collapsible sections, Users and Delegations in one tab,
  a Playlist mirrors tab, and a guided switch of library provider.

### Fixed

- Library search finds album titles, and tags with typographic hyphens or accents.
- In dark mode, the sync toast's text is readable.
- Roon: the Artists menu is found reliably.
- The web page no longer runs its startup twice per load.

## [2.15.0]

### Security

- Fixed an open redirect in the referrer guard, and contained stored-path joins.

### Added

- The App API: a prefix authenticated by a device's token, acting as its owner,
  with library, basket, selection, device and dashboard routes.
- Public YouTube Music playlists by link, no account.
- Read one folder outside the music root as a playlist source.
- The integration API exposes what else is in a device's music folder.

### Changed

- What is already in a sync folder counts against a device's size limit.
- A reinstalled app can re-pair with the device it already had.
- The Emby provider warns that it stops authenticating against Jellyfin 12.0.

### Fixed

- `.m3u` entries with accents, written in a Windows codepage or another Unicode
  form, resolve.
- Deleting a device no longer leaves its basket items without device links.
- Deleted devices are dropped from the picker's remembered destinations.

## [2.14.0]

### Added

- The artist album-count quiz, "duel the bard".

### Changed

- Dependency updates, including cryptography and tinytag.

## [2.13.0]

### Changed

- The four mirror buttons collapsed into one picker.
  See [Upgrading](docs/operations/upgrading.md#the-four-mirror-buttons-collapsed-into-one-picker-2130).

## [2.12.0]

### Added

- Mirror health is visible to integrations.
  See [Upgrading](docs/operations/upgrading.md#new-mirror-health-is-visible-to-integrations-2120).

### Changed

- The device picker replaced the per-row buttons.
  See [Upgrading](docs/operations/upgrading.md#the-device-picker-replaced-the-per-row-buttons-2120).
- Anything staged in your basket is cleared by this upgrade.
  See [Upgrading](docs/operations/upgrading.md#anything-staged-in-your-basket-is-cleared-by-this-upgrade-2120).

## [2.11.0]

### Added

- Request missing albums from Lidarr.
  See [Upgrading](docs/operations/upgrading.md#new-request-missing-albums-from-lidarr-2110).

## [2.10.0]

### Added

- Mirror playlists to Subsonic/Navidrome, Jellyfin, or Emby.
  See [Upgrading](docs/operations/upgrading.md#new-mirror-playlists-to-subsonicnavidrome-jellyfin-or-emby-2100).

### Changed

- Newly-synced playlists from a personal account start private, not shared.
  See [Upgrading](docs/operations/upgrading.md#newly-synced-playlists-from-a-personal-account-now-start-private-not-shared-2100).

### Security

- An integration token's owner is re-checked on every use, not just at
  minting.
  See [Upgrading](docs/operations/upgrading.md#an-integration-tokens-owner-is-re-checked-on-every-use-not-just-at-minting-2100).

## [2.9.0]

### Security

- Integration tokens minted by non-admins are revoked.
  See [Upgrading](docs/operations/upgrading.md#integration-tokens-minted-by-non-admins-are-revoked-290).

## [2.4.0]

### Changed

- Fingerprints need a library scan before device recovery can match against
  them.
  See [Upgrading](docs/operations/upgrading.md#fingerprints-need-a-scan-24x).
