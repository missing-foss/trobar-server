<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Clients overview

Trobar has three official clients — the [Android app](android.md), the
cross-platform [Desktop app](desktop.md), and the [Garmin watch app](garmin.md)
— plus the sync API itself, which any device targets.

## Device tokens

Every device gets its own API token. The token is shown **exactly once**:

- created in the web UI (**Profile → Devices → create a device**), as a **QR
 code** for phones or a downloadable **`trobar-device.json`** for the
  desktop app; or
- created via **Profile → Devices → Add mobile device (QR / code)**, which
  also mints a short, human-typeable enrollment code alongside the QR — the
  path the Garmin watch app uses, since it can't scan a QR code and instead
  has the server URL and code typed into it via Garmin Connect Mobile.

Lost it? "Regenerate + QR" issues a fresh one — the old token stops working
immediately.

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
