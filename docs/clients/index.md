<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Clients overview

Trobar has three official clients — the [Android app](android.md), the
cross-platform [Desktop app](desktop.md), and the [Garmin watch app](garmin.md)
— plus the sync API itself, which any device targets.

## Device tokens

Every device gets its own API token, which the client receives by redeeming
a short **pairing code**: 8 characters, shown with a QR code, valid for an
hour and only once. The token itself is never shown.

- **A phone, tablet or watch:** **Profile → Devices → Add device**, choose
  the type, then **Generate pairing code**. The app scans the QR code or
  takes the typed code, and creates its device. The Garmin watch can't scan:
  the server URL and code reach it as app settings, see
  [Garmin → Pair](garmin.md#pair).
- **A DAP, removable storage or a local folder:** **Add device**, enter a
  name (and a limit if you want one), then **Create device**. The dialog shows
  the server URL and a code for the [desktop app](desktop.md#pair-a-target).
  The device keeps the name and limit you entered.
- **Pairing again** (a reinstalled app, a reformatted card, another
  computer's copy): **Re-pair in app** on the device issues a new code for
  that same device. Its selections, settings and history stay, and whatever
  was paired before stops working once the code is redeemed.

Desktop builds that predate pairing codes read only a raw token. For those,
the code dialog of a DAP, card or folder has **Older desktop app? Get a config
file instead**, which issues a new token (the old one stops working
immediately) and offers it as a QR code and a downloadable
`trobar-device.json`.

## The sync model

All clients follow the same server-driven model:

1. The server computes what each device is **missing**.
2. The client downloads the diff and **acknowledges** each track with the real
   byte count written.
3. On every sync the client **verifies** the files the server believes are on
   the device still exist — with a re-download / leave-deleted choice when they
   don't.

Files are written **atomically** (a half-copied track never sits under its real
name), and playlist selections arrive as `.m3u8` files at the sync-folder root,
listing only what's actually on the device.

Pick your client:

- **[Android app](android.md)** — phones, tablets, watches, Android DAPs.
- **[Desktop app](desktop.md)** — anything that mounts as storage (SD cards, USB
  drives) or a folder.
- **[Garmin watch app](garmin.md)** — Garmin smartwatches with onboard music
  storage, played back through the watch's own native Music player.
