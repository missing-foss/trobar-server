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

Sync then runs in the background on a schedule, or on demand from the status
screen.

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

## Replacing this phone

Upgrading to a new phone, or recovering from a server database loss, doesn't
mean starting from zero — see
[Device loss, replacement & migration](../using/device-recovery.md) for the
manual-copy workflow for a voluntary upgrade, and the server-side transfer for
a lost, stolen, or broken device.
