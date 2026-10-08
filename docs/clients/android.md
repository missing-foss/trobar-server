<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Android app

For phones, tablets, and Android-based DAPs. Source and releases:
[trobar-android](https://github.com/missing-foss/trobar-android).

## Install

Add the repository to [Obtainium](https://github.com/ImranR98/Obtainium) to get
updates automatically, or grab the APK from
[Releases](https://github.com/missing-foss/trobar-android/releases) (tags
`vX.Y.Z`). Building your own APK: see
[Troubleshooting → Building your own APK](../troubleshooting.md#building-your-own-apk).

## Pair

1. Create the device in the web UI (type: phone / tablet / watch / DAP) — see
   [Clients overview → device tokens](index.md#device-tokens).
2. Scan the QR with the app's pairing screen.
3. Pick the sync folder — any folder the system file picker offers, including SD
   cards.

Sync then runs in the background on a schedule, or on demand with **Sync now**
in Settings.

## Browse, stage and send

The app has four tabs — **Home**, **Library**, **Playlists** and
**Settings**, shown as icons — and a basket. Away from home, wanting new music on the phone:

1. **Choose the destination**: the device button at the top of Home,
   Library and Playlists says which device staging is for. It starts as this phone; pick
   another from its list, and the phone keeps the choice.
2. **Library**: browse artists and albums, and double-tap an album to add
   it to the basket for that device. Its cover flies to the basket, and the
   basket's count goes up once the server has it. A single tap does
   nothing. **Sync entire artist** adds a whole artist the same way. The
   album grids on Home take the same double-tap. An album already in the
   basket for that device is not added twice; the app says so instead.
3. **Basket**: review what is staged, grouped by device, and send each
   device's section. Sending turns it into selections on the server — the
   same thing the web UI's basket does. When the section is for this phone,
   the sync starts straight away. If the current connection is one your
   **Sync via** setting rules out (by default, a metered one such as mobile
   data), the app says so instead, and the sync waits for a connection the
   setting allows.
4. The tracks arrive once the sync runs.

A line above the tab bar says what the sync is doing, on every tab: running
and how far along, waiting and why, just finished, or failed. It also says
when the server cannot be reached or no longer accepts this phone. Tapping
it opens Settings, where the sync card, under this phone's name, holds the
detail: progress, the last sync, the last error, and **Sync now**.

Home shows the same widgets as the web dashboard — library statistics,
devices, suggestions, recently added and released, most played, and the
administration counts for an admin — with the same choice of which to show,
in which order, and the same per-widget settings; a change made on the
phone shows in the browser and the other way round, because the
preferences live on the server.

**Playlists** lists the playlists your account can see, as the web UI
does: most available first, with how many of their tracks the library
holds, whose they are when they are someone else's, and when they last
synced. **Refresh playlists** reads them again from your music services
and follows the sync while it runs. Everything the web UI's playlist
section does is here, except mirroring and the filter that hides playlists
with no matches (the app lists every playlist):

- **Stage one** by double-tapping it, as an album; a single tap opens it.
  Double-tapping an open playlist's summary stages it too.
- **Open one** to see its tracks, with the ones your library has no match
  for marked. **Review** lists those; **Exclude** one you expect to be
  missing and it stops being flagged (what syncs to a device does not
  change). **Exclude all** asks first.
- **Request missing albums** from Lidarr, per playlist, where an admin has
  set Lidarr up; the switch says why when it cannot be turned on, and once
  on, how its last run went.
- **Share** a playlist of yours with the household, or make it private
  again, where you may change it. Making it private asks first: anyone
  else who chose it, other than an admin, loses it — it leaves their
  selections, and its tracks are removed from their devices at their next
  sync.
- **Subscriptions**, from the link button at the top of the tab: add a
  public YouTube Music playlist by its link, refresh one, or remove one
  (which removes the playlist it made). Sharing a link to Trobar from
  another app — **Add to Trobar playlists** in the share sheet — opens
  this with the link filled in; nothing is added until you tap **Add**.

The basket is shared with the browser: something staged on the phone can
be sent from a laptop later, and vice versa.

**Server support.** These screens talk to the server's
[App API](../reference/app-api.md), which a server offers from the release
that carries it onwards; the contract page says which one. The app checks
for it each time it starts and whenever Settings opens. On a server without
it, Home and Library show *Update the server to use this*; syncing keeps
working as before. The Playlists tab needs level 2 of the App API, which a
later release brings; on a server that offers only level 1, that tab alone
shows the same message.

## Settings worth knowing

- **Sync via** — Wi-Fi only / + mobile data / + roaming. Devices without a
  cellular radio don't show this and always use Wi-Fi.
- **Locally deleted files** — what happens when you delete synced music on the
  device by hand: ask each time (default), always re-download, or leave deleted
  (the server stops re-queuing those tracks).
- **Hide from gallery** — writes a `.nomedia` marker so artist pictures stop
 appearing in your photo gallery. **Read the warning first:** `.nomedia` is
  recursive and hides the *audio* from MediaStore-based apps too — see
  [Troubleshooting → .nomedia and MediaStore](../troubleshooting.md#nomedia-and-mediastore).

## Artist pictures

A **device-level** setting (web UI → device → Modify: off / small / full size).
When enabled, the app writes an `artist.jpg` into each artist folder after
sync — never overwriting a picture you placed yourself. See
[Devices & Storage](../using/devices.md#per-device-options).

## Playlists

Playlist selections arrive as `.m3u8` files at the sync-folder root — same name
and order as the source playlist, listing only what is actually on the device.
Poweramp and most player apps pick them up. See [Playlists](../using/playlists.md).
Playlists you make yourself in the sync folder go the other way, into Trobar:
[Playlists made on a device](../using/playlists.md#playlists-made-on-a-device).

## Replacing this phone

Upgrading to a new phone, or recovering from a server database loss, doesn't
mean starting from zero — see
[Device loss, replacement & migration](../using/device-recovery.md) for the
manual-copy workflow for a voluntary upgrade, and the server-side transfer for
a lost, stolen, or broken device.
