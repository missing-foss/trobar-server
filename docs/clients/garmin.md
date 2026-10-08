<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Garmin watch app

For Garmin smartwatches with onboard music storage. Source and releases:
[trobar-garmin](https://github.com/missing-foss/trobar-garmin). Unlike the
Android and Desktop clients, this one doesn't have its own screen — it plugs
into the watch's **native Music player** (the same place the Spotify/Deezer
tiles live), supplying pairing, sync, and content.

## Status

**Not yet on the Connect IQ Store** — install for now means sideloading a
signed build from [Releases](https://github.com/missing-foss/trobar-garmin/releases)
(tags `vX.Y.Z`). Only one device is supported today: fēnix 5 Plus
(must be a Music-capable variant — Connect IQ's Audio Content Provider
category, which this app uses, only runs on watches with onboard music
storage).

Own a different Music-capable watch? Open a
[**Request watch model support**](https://github.com/missing-foss/trobar-garmin/issues/new?template=watch_model_request.yml)
issue — see the trobar-garmin README's
["Requesting another watch model"](https://github.com/missing-foss/trobar-garmin#requesting-another-watch-model)
section for what's needed (a tester willing to sideload a build and
confirm it works is what actually gets a model supported, not just added).

## Install

1. Download `trobar-garmin.prg` from the
   [latest release](https://github.com/missing-foss/trobar-garmin/releases/latest).
2. Connect the watch to your computer over USB — it mounts as a mass-storage
   device.
3. Copy the `.prg` file into the `GARMIN/APPS` folder on the watch.
4. Disconnect. The watch picks up new apps in that folder on its own.

This is Garmin's standard non-Store sideload path, not something specific to
Trobar — see
[Garmin's own developer docs](https://developer.garmin.com/connect-iq/connect-iq-basics/)
if a step doesn't match what you see (device firmware varies).

!!! tip "Linux: use gMTP"
 Plain CLI tools (`gio copy`, `jmtpfs`, `mtp-sendfile`) can fail with a
 `libmtp` "could not send object info" error on some setups. **gMTP**
 (a dedicated MTP client) is the reliable way to copy the `.prg` file
    over on Linux.

## Pair

There's no keyboard on the watch. The app reads two settings, the **server
URL** and a short **pairing code**, and the watch exchanges the code for its
own device credentials the next time it syncs over its own Wi-Fi.

**Garmin Connect Mobile can't set them for a sideloaded app.** It shows an
app's settings only when the app was installed from the Connect IQ Store,
and Trobar isn't there yet. Until it is, the settings reach the watch as a
**settings file** copied over USB.

!!! warning "Not yet confirmed on a watch"
    The settings-file route below follows how the Connect IQ simulator
    stores an app's settings. It hasn't been confirmed on a fēnix yet; this
    note goes once it has.

You need the Connect IQ SDK's simulator, running **the same build** you
sideloaded.

1. **Get everything ready first.** The pairing code is valid for **one
   hour** and works once. It's used at the watch's next sync, not when you
   copy the file.
2. Start the app in the simulator on `fenix5plus`.
3. In the trobar-server web UI, create the device: **Profile → Devices → Add
   mobile device (QR / code)** (see
   [Clients overview → device tokens](index.md#device-tokens)). Note the
   server URL and the code; the QR code isn't used here.
4. In the simulator, open **File → Edit Persistent Storage → Edit
   Application.Properties data**, set `serverUrl` and `enrollCode`, and send
   them to the simulator.
5. Find the `.SET` file the simulator wrote: it's named after the app's
   `.PRG`, in a `GARMIN/APPS/SETTINGS` folder under the simulator's
   temporary directory.
6. Connect the watch over USB and copy that file into `GARMIN/APPS/SETTINGS/`,
   **under the same base name as the Trobar `.prg` on the watch**. Check
   whether the `.prg` is in `GARMIN/APPS/` or `GARMIN/APPS/MEDIA/`; the
   simulator keeps an audio app in `MEDIA/`. Disconnect.
7. Let the watch sync on its own Wi-Fi within the hour (see
   [Wi-Fi requirements](#wi-fi-requirements)).

Open Trobar in the watch's Music app to see the outcome. **"Paired: *device
name*"** means it worked. **"Pairing failed: Invalid or expired code"** means
the settings arrived but the code had expired or was already used: repeat
with a fresh code. **"Pairing failed: Network error"** means the watch
couldn't reach the server during its sync: check the server URL and the
watch's Wi-Fi. If it still says **"Enter server + code in the Garmin
Connect app"**, the watch either didn't pick up the settings file or can't
use the server URL in it (it must start with `http://` or `https://`):
check the file's name and folder, and the URL.

### Pairing again

A paired watch ignores a new code. To pair it again (to another server, or
after its device was deleted in the web UI), open Trobar in the Music app,
press **Menu** (hold **Up**), choose **Forget pairing**, then follow the
steps above with a new code.

Once the app is on the Connect IQ Store, steps 2 and 4–6 become one: enter
the server URL and code in the app's **Settings** in Garmin Connect Mobile.

## Wi-Fi requirements

Both pairing and sync happen over the watch's **own** Wi-Fi (not the phone's),
so if either one silently never triggers, check the access point before
anything else:

- **2.4 GHz only, channels 1–11.** Every Music-capable fēnix (5 Plus through
  8) has 2.4 GHz-only Wi-Fi — 5 GHz networks aren't visible to the watch at
  all. Channel 12/13 (common in Europe) isn't supported either.
- **WPA2-PSK only.** WPA3-only or mixed WPA2/WPA3 mode often fails; 802.1x
  enterprise isn't supported. Force WPA2-PSK on the network the watch uses.
- **No band-steering.** If your 2.4 GHz and 5 GHz bands share one SSID, split
  them — the watch chokes on this regularly.
- **No hidden SSIDs**, and a password of at least 8 characters.

## Transcode format

The watch has no FLAC or other lossless decoding, so a `watch`-type device
that doesn't specify a format is automatically created on the lowest MP3
tier rather than left on "original" — there's nothing to set before your
first sync. You can still raise the tier yourself in the web UI (device →
Edit) if you want better quality at the cost of more transcoding. See
[Devices & Storage](../using/devices.md#per-device-options).

!!! warning "The watch's sync screen can get stuck at 0% — this is expected"
    On some real-device tests, the watch's own native "Syncing…" screen has
    stayed at 0% and never dismissed itself, **even when the sync actually
    completed successfully**. This has been confirmed (via debug
    instrumentation) to be a Connect IQ platform limitation, not a Trobar
    bug: the app calls the documented completion signal correctly every
    time, but the watch's own progress UI doesn't always honour it.

    If this happens: cancel the stuck screen (Back) and check the device's
    entry in the trobar-server web UI (or wait a few seconds and check
    again) — the sync has almost certainly already finished correctly.
    Don't assume it failed just because the screen didn't close on its own.

## What syncs

Any combination of selections can be assigned, the same as other devices —
see the same [selections matrix](../using/library-selections.md) used
elsewhere. The watch plays them as a **single queue**, not separate browsable
playlists: assigned playlists come first, in their own order, then any
remaining tracks (album/artist/track selections) sorted by path.

## Playback

Once synced, the music shows up as a source inside the watch's own Music app
— Trobar doesn't add a second player UI on top, it plugs into the same
screens Spotify/Deezer use:

1. Open the Music app and select **Trobar** from the source list. You'll see
   a status screen ("Paired: *device name*", or an error if something's
   wrong). If you've already synced something, you'll also see a
   **"Press Select to play"** hint. If it's missing, there's nothing on the
   watch yet — assign something in the web UI and let it sync first.
2. Press **Select** (the upper-right button) to start playback. This hands
   off to the watch's native player — from here it's the same controls as
   any other music source: Select play/pauses, the **Down** button skips to
   the next track, and holding the **Up** button opens more controls
   (including switching to a different music source). Back from any of
   these screens returns you to Trobar's own status screen.
3. Connecting Bluetooth headphones while a source is active can also
   auto-resume playback on its own, without going through the source list
   at all.

## Replacing this watch

The watch can't hold enough storage to recover a server database loss on its
own, but a broken or replaced watch is still recoverable server-side — see
[Device loss, replacement & migration](../using/device-recovery.md) for both
that limitation and the transfer that does cover a watch swap.
