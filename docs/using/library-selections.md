<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Library & Selections

The core of Trobar: browse your library, pick what you want, and sync it to any
combination of devices.

## Browsing

The **Library** tab lists your music by album artist and album, built from the scan of
`MUSIC_ROOT`. Trobar reads tags first and falls back to the
`Artist/Album/Track` folder convention when tags are missing or malformed;
anything that fits neither lands under **Unknown Artist/Album**. The Library
health panel counts those so you can fix the tags and rescan. A compilation is one album
under Various Artists (or its `albumartist` tag), and an artist's page lists the
albums by others they play on under **Also appears on**: selecting one syncs only
that artist's tracks on it. Clicking an album, in either view of an artist's
page or among the home page's covers, opens it: a larger cover, the year and
the track list, by disc when the album has more than one, with a track's own
artist shown when it isn't the album's (a guest, a compilation). From there
**Add to basket** sends the album through the device picker, and **Add to
playlist…** and **+ Playlist** add the album or one track. Beside the time of
the last scan, it also says how many of the files that scan read were named from
their folders, as a hint that their tags are worth fixing.

Album covers come from the art embedded in the audio files; an image file
beside them, such as `folder.jpg` or `cover.jpg`, isn't read.

After a scan, the Library tab says what changed: tracks added, updated and
removed, and, when it happened, files that moved and kept their tracks, files or
folders that couldn't be read (their tracks are kept), files that disappeared
during the scan, and files named from their folders.

## Selections

A **selection** is your choice of an artist, album, or playlist targeted at one
or more devices. The server then works out each device's missing tracks and
the [clients](../clients/index.md) download the difference.

### Sending music to devices

Everything goes through the **Sync to…** dialog:

- **An artist:** **Sync entire artist** on the artist's page.
- **An album:** **Sync to…** on its row in the Library, or in an artist's
  **Also appears on** list.
- **Several albums:** tick them in the Library's list view, or tick the
  checkbox on their covers in the cover view, on the Suggestions tab, or in the
  home page's Suggestions, Recently added and Recently released widgets; then
  **Select N albums**.
- **A playlist:** **Sync to…** on its row in Playlists.

The dialog lists your devices, including any you manage for someone else;
an admin's lists every device in the household.
Tick the ones the music should go to, then:

- **Add & keep browsing** puts it in your **Basket** for those devices and
  sends nothing yet;
- **Add & send now** adds it and sends, at once, everything in the Basket for
  the ticked devices. When there is more than what you just picked, the
  button says how many items will go.

The dialog remembers the devices you last chose, separately for the Library,
Playlists, Suggestions, Recently added and Recently released.

The **Basket** button at the top of the page (shown once something is in it)
opens the list of what is waiting, one section per device. **Send** sends that
device's section and leaves the others; **×** takes an item off that device
only; **Clear** empties the whole Basket. Nothing in the Basket reaches a
device until it is sent. The Basket is kept on the server, so it is still
there after a reload or in another browser. An item already in the Basket
shows **In basket** where its Sync button was.

Selections are **per user**: every household member has their own. Artist,
album and track selections survive a provider switch, a rescan, and device
token regeneration. A selected playlist from the old provider stays selected
if the switch carries it over to the new provider, and is removed with it
otherwise.

### The Selections tab

The **Selections** tab shows every album, artist and playlist selected for a
device you can see, one row each, with a box per device:

- **Tick** a box to send it to that device.
- **Untick** one of yours and Trobar asks first, saying how many tracks are
  deleted from the device at its next sync. A track another selection of that
  device still needs stays.
- **A locked box** is kept ticked by someone else's selection (an admin and a
  delegate see those): hover it to see who. Only they can untick it.
- **A change that fails** says so under the row's name, and the boxes show what
  is true.
- **Forget** appears on a row of yours that is ticked on no device: it removes
  the selection from the list.
- **An artist's part of someone else's album** (selected from Also appears on)
  reads "Artist on Album · 2 of 14 tracks", so it isn't mistaken for the whole
  album, which reads "Album · whole album, 14 tracks".

## Scanning

- **Rescan** (on the Library tab) picks up new albums and only re-reads tags
  for **changed** files — cheap, run it freely.
- **Full rescan** (admins only, beside it) re-reads every file's tags, which
  is what picks up a changed tag on an unchanged file; over network storage
  budget minutes per 10k tracks. It only happens when you ask for one.
- Scans run in the **background** and the UI polls for progress, so a large scan
  doesn't block the request.
- **A rescan reaches the devices.** A new album under a selected artist goes
  to every device that selects it, and a file deleted from disk is removed
  from them, on each device's next sync. A deleted track that a selected
  playlist still points at stays on the device until the playlist is matched
  again, and one in an auto-fit set until auto-fit is next refreshed.
- **Moving files doesn't re-download them.** A file that moved to another
  folder with its size and tags unchanged (a renamed folder, a reorganised
  library) keeps its track, so devices keep their copy. Two identical copies
  moved at once, or a file changed as it moved, first count as one removed
  and one added. Once the library's fingerprints are computed, a removed
  track whose audio matches exactly one new track is merged into it:
  playlists and selections follow, and a device that would store it under
  the same name keeps its copy.
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

### Supported formats

The scan reads these files into the library; anything else in the music
folder is ignored. On a device set to an MP3 format, lossless files are
transcoded and the rest are copied as they are.

| Extension | Format | On a device set to MP3 |
|---|---|---|
| `.flac` | FLAC | transcoded |
| `.wav` | WAV | transcoded |
| `.aiff`, `.aif` | AIFF | transcoded |
| `.m4a` | ALAC | transcoded |
| `.m4a` | AAC | copied as is |
| `.mp3`, `.aac`, `.ogg`, `.opus` | lossy | copied as is |

An `.m4a` file can hold either: the scan reads which, and only ALAC is
transcoded. Re-encoding an already-lossy AAC file would only lose quality.
