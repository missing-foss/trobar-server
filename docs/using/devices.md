<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Devices & Storage

Each user manages their own devices on the **Devices** tab, the last one in
the main navigation. An admin sees every device there; someone with
[delegated management](delegation.md) also
sees the devices they have pinned, under **Available delegated devices** →
**+ Add to my list**. **+ Add device** creates one and shows a pairing code
for its app — see [Clients overview](../clients/index.md#device-tokens).

Each device's card shows its type, when it was last seen and its sync state,
with these actions:

- **Edit** — its name, type, icon, transcode format and artist pictures.
- **Re-pair in app** — a new pairing code for this same device, keeping its
  selections and settings (a reinstalled app, a reformatted card).
- **Replaces…** — see [Replacing a device](#replacing-a-device) below.
- **Delete** — removes the device after asking: its token stops working at
  once, and its selections stay but no longer go to it.

## Per-device options

- **Type** — phone, tablet, smartwatch, dedicated audio player (DAP),
  removable storage, or local folder. It decides how the device pairs: a
  phone, tablet or watch with its app, the others with the
  [desktop app](../clients/desktop.md). The optional **icon** is only a
  picture.
- **Storage limit** — a cap in GB on the device's **music folder**, not on
  Trobar's own share of it. The usage bar counts what is **actually** on the
  device (real reported sizes, including transcoded ones), and music that was
  already in that folder before Trobar ever synced to it counts too — it
  appears as a separate grey slice, and Trobar fills only the space left over.

    That last part needs the app to have told the server what else is in the
    folder, which it does on sync. Until it has, the panel says so rather than
    drawing an empty folder: a device that has never reported cannot be
    distinguished from an empty one otherwise, and only one of those means the
    limit is being enforced against everything there. An older app that never
    reports it keeps the previous behaviour — the limit applies to Trobar's
    own files alone.
- **Auto-fit** — optionally fills the device with the owner's Last.fm
  most-played albums, whole albums at a time, frozen until refreshed. A
  compilation Last.fm ranks under "Various Artists" (its album artist) is
  fitted whole; one ranked under a single performer fits that performer's
  tracks on it. A **fill
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
- **Keep device content** — when on, the server never removes tracks from the
  device, so what is on it survives even a lost server database; it still
  adds what you select. The Android and desktop apps turn it on themselves
  when they are paired on a device that already holds music, and send the
  server the list of files there. Files that match no track in the library
  then show on the card as **on device, not in library** (added by hand, or
  kept after leaving the library). Click that note to review them; **Adopt**
  marks one as the device's own, so it stops being counted. Switching it off
  asks first and says how many tracks on the device no selection includes:
  those are removed from it at its next sync.
- **Play** (watches only) — what the watch plays: **Everything**, every
  playlist on it as one queue followed by the rest of its music, or one of
  the playlists selected for it. It shows once at least one playlist is
  selected for the watch, and goes back to Everything if that playlist is
  unselected.

**On the device**, music lands as `Album artist/Album/NN - Title.ext` (with
`CDn-` before the number on a disc-tagged album): a compilation goes under
**Various Artists**, and a guest track sits with the rest of its album. A
track already on a device keeps the name it was delivered under, so changing
how files are named never moves or re-downloads what a device already holds.

Storage budgets stay honest either way: the server counts the real transcoded
sizes reported by clients, and estimates MP3 sizes from track durations for
not-yet-synced music.

## Replacing a device

**"Replaces…"** on a device reassigns everything an old device held —
synced tracks, selections, settings and the playlists made on it
([Playlists made on a device](playlists.md#playlists-made-on-a-device)) —
onto it, then deletes the old device. Pair the replacement first, then use this action on it and pick the
device it's taking over from. See
[Device loss, replacement & migration](device-recovery.md#client-replaced-server-intact)
for the full picture, including what happens across device types and who's
allowed to do it.

## Transcoding

When a device's format is MP3 320/256/192/128 kbit/s, the **server** transcodes
on demand for **every** device type — Android and desktop alike. At
`GET /api/device/file/<id>`, when the format is set and the source is lossless
(FLAC, WAV, AIFF, or ALAC in an `.m4a`; see
[Supported formats](library-selections.md#supported-formats)), the track is
transcoded to a temporary file server-side,
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
