<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Library & Selections

The core of Trobar: browse your library, pick what you want, and sync it to any
combination of devices.

## Browsing

The **Library** tab lists your music by artist and album, built from the scan of
`MUSIC_ROOT`. Trobar reads tags first and falls back to the
`Artist/Album/Track` folder convention when tags are missing or malformed;
anything that fits neither lands under **Unknown Artist/Album**. The Library
health panel counts those so you can fix the tags and rescan.

## Selections

A **selection** is your choice of an artist, album, or playlist targeted at one
or more devices. Batch-select multiple albums and sync them to any combination
of devices at once — the server then computes each device's missing tracks and
the [clients](../clients/index.md) download the diff.

Selections are **per user**: every household member has their own, and they
survive a provider switch, a rescan, and device token regeneration.

## Scanning

- **Incremental rescans** (the Library tab's refresh button) pick up new albums
  and only re-read tags for **changed** files — cheap, run them freely.
- **A full/forced rescan** re-reads every file's tags; over network storage
  that's budget minutes per 10k tracks. It only happens when you ask for one.
- Scans run in the **background** and the UI polls for progress, so a large scan
  doesn't block the request.
- **Nothing rescans the library on its own by default.** New files sit unseen
  — and unfingerprinted, so they can't benefit from [recovery-by-fingerprint](
  ../administration.md) either — until someone clicks Rescan (or the first-run
  setup wizard runs it once). If that doesn't fit a set-and-forget deployment,
  an admin can turn on automatic rescanning in **Administration → Configuration
  → Automatic library rescan**: set an interval in hours and Trobar enqueues an
  incremental (never forced) rescan once that many hours have passed since the
  *previous* scan finished. It's off (0) until set.

See also [Suggestions](suggestions.md) for filling devices from listening
history, and [Devices & Storage](devices.md) for per-device limits and auto-fit.
