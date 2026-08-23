<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Device loss, replacement & migration

"I'm replacing my phone" / "my watch broke" / "my server died" all have
different answers depending on which side lost its state — the **server**
(its database) or the **client** (the device itself) — and the mechanism
differs by client type too. This page pulls all of that together in one
place. It documents what's actually true **today**; anything still open or
unbuilt is marked as such rather than described as if it already worked.

This is a different concern from [Backups & DATA_DIR](../operations/backups.md),
which covers protecting the server's own database in the first place, and
from [Devices & Storage](devices.md), which covers day-to-day device
management rather than what happens when a device is lost or swapped.

## Quick reference

| Scenario | Android | Desktop (SD card / folder) | Garmin |
|---|---|---|---|
| **Server database lost**, the device still has its files | Automatic — see [below](#server-database-lost-client-intact) | Automatic, same mechanism | Not covered — [known limitation](#server-database-lost-client-intact) |
| **Device lost, stolen, or broken** — a new one takes its place | [Device-to-device transfer](#client-replaced-server-intact) in the web UI | Same | Same — the one case Garmin *is* covered for |
| **Voluntary upgrade**, old device still in hand | [Copy the folder across first](#voluntary-phone-upgrade-both-devices-in-hand), then pair fresh | Just move the card — nothing to do | No local storage to copy — use device-to-device transfer instead |
| **Moving the card**, or replacing the computer running the desktop app | N/A | [Nothing needed](#sd-card-usb-storage-desktop) — the card carries its own identity | N/A |

## Server database lost, client intact

The case [provenance](https://github.com/missing-foss/trobar-server/issues/239)
was built for: the server's database is gone (restored from an old backup,
or never backed up at all), but a device still physically holds the music
files. Rather than re-downloading everything from scratch, the client pushes
back what it already has — a chromaprint **audio fingerprint** plus its path,
per track — and the server rematches **by audio content**, not by file path.
That's what lets it survive a re-tag or re-encode that a path-based match
wouldn't.

- **Android** — stored in an app-private SQLite database, pushed automatically
  on every sync. Being app-private, it doesn't survive an app reinstall or a
  phone swap on its own; see [device-to-device transfer](#client-replaced-server-intact)
  or [voluntary phone upgrade](#voluntary-phone-upgrade-both-devices-in-hand)
  for those cases instead.
- **Desktop** — stored in `.trobar/provenance.json`, written **onto the
 card/folder itself**, alongside pairing (`.trobar/device.json`) and
  last-sync state. Because it lives on the card, it survives a card move
  between computers for free — see
  [SD card / USB storage](#sd-card-usb-storage-desktop) below.
- **Garmin — not covered.** Connect IQ's storage budget for the app is only
  ~128 KB total, and a single fingerprint is ~2.2 KB — the watch's *entire*
  storage budget holds roughly **58 fingerprints**, and it isn't empty to
 begin with (pairing, `CONTENT_MAP`, play order and sync status already
  live there). A watch syncing 200 tracks would need ~440 KB; it doesn't
  fit, and no tuning makes it fit. The watch is excluded from provenance
  for this reason — decided and recorded on

 —
  so a watch's content matches the server by path only, and a database loss
  on the server side means the watch's synced tracks re-download from
  scratch, same as a brand-new device.

## Client replaced, server intact

The mirror-image case: the device itself is gone or being retired — lost,
stolen, broken, or just upgraded — but the server's database is intact and
still knows exactly what that device was supposed to hold. **Device-to-device
transfer** reassigns that state to the replacement, server-side, with no
cooperation needed from either device:

1. Pair the new device normally first (it needs to already exist before you
   can transfer anything onto it).
2. On the new device's card in **Profile → Devices**, click **"Replaces…"**
   and pick the old device from the list.
3. Confirm. The new device inherits the old one's assigned selections and
   per-device settings (transcode format, storage limit, auto-fit, artist
   pictures), and the old device is deleted — its token stops working
   immediately.

By default, the tracks the old device held are marked to **re-download
fresh** on the new device, rather than assumed already present — the safe
choice, since the ordinary case here is a genuinely blank replacement. If the
new device really is already stocked with the same files (a cloned SD card,
a restored backup), check **"This device already has these files"** before
confirming to skip the redundant re-fetch.

This works for **any device type**, including a straight swap across types
(e.g. replacing a DAP with a phone) — the web UI flags a cross-type transfer
with a warning, since an inherited storage limit or auto-fit setting sized
for very different hardware will likely need a second look, but it doesn't
block it.

This is the **only** mechanism that reaches every client type, including
Garmin — the watch can't hold provenance ([above](#server-database-lost-client-intact)),
so a broken or replaced watch has no client-side recovery path at all, only
this server-side one. It's also the only mechanism for genuine loss or theft:
the physical files are gone either way, but the household doesn't have to
re-configure what should sync to the replacement from scratch.

Only the device's owner or an admin can perform a transfer, and touching two
devices with different owners (an admin re-homing devices between household
members) requires admin either way — one person managing their own delegated
device can't use this to redirect someone else's synced content onto their
own hardware.

!!! note "Complementary, not overlapping, with the DB-loss case above"
    A household hitting both at once — a fresh server database *and* a
    replaced device — still needs both mechanisms: provenance recovers
    identity when the *server* forgot; transfer recovers configuration when
    the *client* was replaced.

## Voluntary phone upgrade, both devices in hand

The case with nothing to physically move — most modern phones have no SD
card. Android already has a manifest-recovery step that fires on **every
fresh enrollment**, not just a reinstall on the same phone: it walks whatever
folder you pick during pairing, matches what's already there against the
library, and tells the server so it skips re-downloading anything already
present.

So the workflow already works today, it's just not obvious from the app:

1. Copy the old phone's Trobar folder onto the new one however you like —
   USB, Nearby Share, a cloud drive, anything.
2. Pair the new phone and pick that same folder as the sync target during
   setup.
3. The app recognises what's already there and only fetches what's missing.

The folder-picker screen now says this directly ("Already have this music on
another device? Copy it into the folder you pick here…"), so this isn't a
secret — just be aware it's a **manual copy you do yourself**, not something
the app automates between two phones. Combine it with
[device-to-device transfer](#client-replaced-server-intact) afterwards if
you also want the new phone to inherit the old one's selections and
settings, not just skip re-downloading files.

## SD card / USB storage (desktop)

The easy case, worth stating plainly so it doesn't get lost among the harder
ones above: the **card is the portable unit by design**. Pairing, last-sync
outcome, and provenance all live in `.trobar/` on the card itself, not in the
desktop app's own storage (see [Desktop app → Pair a target](../clients/desktop.md#pair-a-target)).
So:

- **Moving the card** between computers needs nothing beyond installing the
  desktop app and opening the card — it's recognised and synced as the same
  device automatically.
- **Replacing the computer** running the desktop app is the same thing —
  the app itself holds no per-card state to lose.

There's genuinely nothing to back up or restore here, which is why an
app-side backup/restore feature for the desktop client was closed as
unneeded .

A **dying or already-dead card** is a different situation — there's nothing
left to move. That's the [device-to-device transfer](#client-replaced-server-intact)
case instead: pair the new card fresh, then use "Replaces…" from the old
card's entry to carry over its selections and settings; the files themselves
need a full re-sync onto the new card, same as any other replaced device
where "already has these files" is left unchecked.
