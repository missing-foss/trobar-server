<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Desktop app

For anything that mounts as storage or is a folder: DAP SD cards, USB drives, a
phone in file-transfer mode, an external disk — or a folder on the computer
itself (an offline copy on a laptop works exactly the same way). Source and
releases: [trobar-desktop](https://github.com/missing-foss/trobar-desktop).

## Install

- **Linux x64**: download the tarball from
  [Releases](https://github.com/missing-foss/trobar-desktop/releases) (tags
 `vX.Y.Z`), extract, and run `./install.sh` (user-install, no root) or
 run `./trobar_desktop` directly.
- **Windows / macOS**: download the zip from Releases. These builds are
  **unsigned**, so the OS shows an "unidentified developer" warning you'll need
  to allow past.
- **Older Linux, or building yourself**: `flutter build linux|macos|windows` in
  the [trobar-desktop](https://github.com/missing-foss/trobar-desktop) repo.

## Pair a target

1. Create the device in the web UI (type: SD card / USB storage) and download
 its `trobar-device.json`.
2. In the app: **Open folder…** → pick the mounted card → **Load
   trobar-device.json…**.

The pairing is written **onto the card** (`.trobar/device.json`), so the card
carries its own identity — plug it into any computer running Trobar and it's
recognised and synced as the same device. The app stores nothing per-card.

## Sync

One button, or automatic (Settings): **sync on card detect**, and/or a
**periodic re-sync interval** (off/15/30/60/360 min) while a card is open.
Both are opt-in and off by default, and only act while the app itself is
open — there's no background daemon. Files are written atomically, each
track is acknowledged with the real byte count written, folders left empty
by removals are pruned, and playlist `.m3u8` files are maintained at the
card root exactly like on Android.

## Transcoding

If the device's format (web UI → device → Modify) is MP3 320/256/192/128 kbit/s,
the **server** transcodes lossless sources (FLAC/WAV/AIFF) to MP3, to a
temporary file server-side, then streams it and deletes it — the desktop app
just downloads whatever it's served, exactly as it does an original file. It
does **no** transcoding of its own; the release build ships no ffmpeg. Tags
and embedded cover art carry over
(ID3v2.3, for older DAP compatibility); already-lossy files are copied as-is,
never re-encoded. The same server-side path applies to every device type — see
[Devices & Storage → Transcoding](../using/devices.md#transcoding).

Changing the format later re-syncs the whole device under the new file names
(the web UI warns first), and the app's **orphan sweep** then offers to remove
the leftover old-format files. It only ever deletes files it can prove Trobar
wrote — anything you copied onto the card yourself is never touched.

## Artist pictures

Same device-level setting as Android (off / small / full). `small` serves ~512px
versions — a sensible default for DAP screens and card space. Hand-placed
`artist.jpg` files are never overwritten, and the orphan sweep never touches
them.

## Moving or replacing a card

Because pairing and sync state both live on the card itself, moving it
between computers or replacing the computer running the app needs nothing
extra — see
[Device loss, replacement & migration → SD card / USB storage](../using/device-recovery.md#sd-card-usb-storage-desktop).
A dying card that needs replacing outright is a different case, covered in
the same page.
