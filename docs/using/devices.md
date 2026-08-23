<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Devices & Storage

Each user manages their own devices in the **Devices** panel (the admin sees
every device). A device is created in the web UI, which issues its one-time
token — see [Clients overview](../clients/index.md#device-tokens).

## Per-device options

- **Type** — phone, tablet, watch, DAP, or SD card / USB storage. Mostly
  cosmetic; SD card / USB storage is what the [desktop app](../clients/desktop.md)
  targets.
- **Storage limit** — a cap in GB. The usage bar counts what is **actually** on
  the device (real reported sizes, including transcoded ones).
- **Auto-fit** — optionally fills the device with the owner's Last.fm
  most-played albums, whole albums at a time, frozen until refreshed. A **fill
  percentage** (default 100%, i.e. fill it all) caps auto-fit's own share of
  the device's storage limit, leaving headroom for podcasts, audiobooks,
  photos, or just breathing room — it's a percentage of the storage limit
  itself, not of whatever's left after your manual selections, so adding a
  manual selection later doesn't quietly move the reserved space. Dragging the
  slider shows a live, approximate GB and track-count estimate before you
  commit — approximate because auto-fit packs whole albums (the real result
  lands under the target, not on it) and the track count is an average over
  your library, not the exact ranked pick a refresh would make.
- **Transcode format** — Originals (default) or MP3 320/256/192/128 kbit/s,
  available for every device type. Changing it triggers a **confirmed full
  re-sync** under the new file names.
- **Artist pictures** — off (default), small (~512px, downscaled by the server —
  good for DAP screens and card space), or full size. Clients write an
 `artist.jpg` into each artist folder on the device, never overwriting one you
  placed yourself.

Storage budgets stay honest either way: the server counts the real transcoded
sizes reported by clients, and estimates MP3 sizes from track durations for
not-yet-synced music.

## Replacing a device

**"Replaces…"** on a device reassigns everything an old device held —
synced tracks, selections, and settings — onto it, then deletes the old
device. Pair the replacement first, then use this action on it and pick the
device it's taking over from. See
[Device loss, replacement & migration](device-recovery.md#client-replaced-server-intact)
for the full picture, including what happens across device types and who's
allowed to do it.

## Transcoding

When a device's format is MP3 320/256/192/128 kbit/s, the **server** transcodes
on demand for **every** device type — Android and desktop alike. At
`GET /api/device/file/<id>`, when the format is set and the source is lossless
(FLAC/WAV/AIFF), the track is transcoded to a temporary file server-side,
streamed once the transcode finishes, then deleted — nothing is
*persistently* cached, and the client just downloads whatever it's served.
The stream can't be range-resumed — if the connection drops the client
restarts that one track on its next sync. (The desktop client ships no
ffmpeg and does no transcoding of its own.)

Two admin settings bound the server's CPU cost (both take effect on the next
request, no restart):

- **Concurrent transcodes** — how many tracks may transcode at once (default 1).
  Requests beyond the limit wait for a slot rather than being rejected.
- **Encoder priority (nice level, 0–19)** — the `nice` level ffmpeg runs at
  (default 10); higher means lower priority, so transcoding backs off under load
  rather than competing with everything else running alongside Trobar.

Already-lossy files are copied as-is, never re-encoded.

## Monitoring devices externally

Everything on this page — per-device storage, sync status, transcode
settings — is also available read-only via a per-user API token, for
building a Home Assistant dashboard, a Grafana panel, or an uptime check
without opening the web UI. See [Integration API](../reference/integration-api.md).
