<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Troubleshooting

Sharp edges hit in real use, with what actually fixes them.

## Roon pairing

**The extension never shows up in Roon Settings -> Extensions.** Trobar and
the Core must see each other on the LAN (same L2 network or routed multicast
for discovery; a host-networked or macvlan container helps if your compose
network isolates UDP discovery). Check the container can reach the Core's
host/port at all.

**Pairing keeps flapping / drops as soon as it connects.** Roon allows one
connection per extension identity. If something else on your network runs a
second copy of Trobar (a test instance, an old container still running),
the two fight over the pairing and both lose. Stop the duplicate.

!!! warning "Never point two Trobar deployments at one Core"
    One Core supports one Trobar. Worth stating as a rule rather than only as
    a symptom, because flapping pairing doesn't look like a duplicate-instance
    problem — and both instances lose, so neither one works to tell you.

**Playlist tracks are "missing".** Playlist entries are matched against the
files in your library by artist/album/title tags. Tracks that only exist on
a streaming service (TIDAL/Qobuz inside Roon) are flagged and never synced —
that is by design; Trobar only handles files you have.

## `.nomedia` and MediaStore

!!! warning "It hides all synced audio, not just the artist pictures"
 The Android app's "Hide from gallery" toggle writes a `.nomedia` marker at
    the sync-folder root, and Android applies it **recursively to everything
    under it, audio included** — any app that discovers media through
    MediaStore (most galleries, some players) stops seeing the synced music.
    If your player went blank after enabling it, that is why: turn it off and
    let the device rescan.

Player apps that scan folders themselves (Poweramp in folder mode, most file
managers) are unaffected.

Some gallery apps also keep their own cache and need a force-close (or a
reboot) before they notice a rescan.

## Desktop app

**The Linux tarball won't start (`GLIBC_2.3x not found`).** The prebuilt
binary needs a distribution roughly as new as Ubuntu 24.04 (glibc 2.39).
On anything older, build from source — `flutter build linux` in `desktop/`.

**Files it says are "not managed by Trobar".** The post-sync sweep lists
files on the card that no synced track accounts for — typically leftovers
from a transcode-format change, but your own hand-copied files show up too.
"Keep" is always safe; Trobar never deletes anything unmarked on its own.

## Playlists on old players

Playlist files are written as UTF-8 `.m3u8`. A few very old DAP firmwares
only read `.m3u` (often in a legacy codepage) and may ignore them or mangle
accented names — if your player supports Rockbox, that reads `.m3u8` fine.

## Setup wizard

**The last step shows a fetch/network error.** Reload the page. Your setup is
saved and the library scan is running — the wizard disappears once you reload,
and you'll find the scan already in progress on the Library tab.

This happened on older versions because the wizard waited for the whole first
scan to finish before responding, which a reverse proxy would time out (nginx
defaults to a 60-second read timeout). The scan itself always completed; only
the reply was lost. The wizard now starts the scan in the background and hands
you straight to the library, and it recovers by itself if the request does fail.

## Library scanning

**A full/forced rescan is slow.** Every file's tags are re-read; over
network storage budget minutes per 10k tracks. Normal rescans are
incremental and cheap — forced ones only happen when you ask for one.

**Tracks show as "Unknown Artist/Album".** Tag-reading falls back to the
`Artist/Album/Track` folder convention when tags are missing or malformed;
files outside both conventions land in Unknown. Fix the tags and rescan —
the Library health panel counts these for you.

## Building your own APK

The Android app is signed with the maintainer's key on Releases. If you
build your own, set `TROBAR_KEYSTORE`, `TROBAR_KEYSTORE_PASSWORD`,
`TROBAR_KEY_ALIAS` (defaults to `trobar`), and optionally
`TROBAR_KEY_PASSWORD` before `./gradlew assembleRelease`.

!!! danger "Keep using the same keystore forever"

    Android refuses to update an app whose signature changed, so switching
    keys means uninstall + re-pair on **every** device — there is no migration
    path for an app that's already installed. Back the keystore up somewhere
    you won't lose it, and don't put it in CI.

## "Database is locked" (HTTP 500)

Should not happen since scanning commits in small batches, but if you drive
the SQLite database from outside the app (manual scripts against
`DATA_DIR/music-sync.db`), keep your transactions short — the app's API
waits up to 30 seconds for a competing writer, then gives up.
