<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Privacy

Trobar is software you run yourself. The server runs on your own machine,
and the apps (the Garmin watch app, the Android app, the desktop app and the
Home Assistant integration) talk to **your** server, at the address you give
them. The Trobar project runs no server of its own: nothing any part of
Trobar does sends data to the project, and none of it contains analytics,
telemetry, advertising or crash reporting.

This page says what each part sends, where it goes, and what is kept. It
covers the server and every client, Garmin first.

## Garmin watch app

**Where it connects:** only to the server address you enter in the app's
settings. The app contacts no other address. It accepts a plain `http://`
address too; use `https://` so the traffic is encrypted.

**How:** the watch downloads during a music sync, over the watch's own
Wi-Fi. Your phone doesn't need to be nearby for that. The server address and
the pairing code are typed into the app's settings in Garmin Connect on your
phone, which passes them to the watch. Garmin Connect and the watch's
operating system are Garmin's, and what Garmin does with data that passes
through them is covered by Garmin's own privacy policy.

**What it sends to your server:**

| When | What |
|---|---|
| Pairing | the pairing code, the watch model as its name, and the type "watch" |
| Each sync | the device's access token, to ask what has changed |
| Each track downloaded | the access token, to fetch the file |
| After each track | the track's id and whether it was downloaded or removed |

It sends nothing about what you play: no play history, no listening times.

**What it keeps on the watch:** the server address and pairing code (as app
settings); the access token, device id and device name; the result of the
last pairing attempt; the list of downloaded tracks and their play order;
and the time and result of the last sync. Unpairing deletes the token,
device id and name, but leaves the music, the track list, the play order and
the last sync's details. The music itself goes into the watch's own media
storage.

**Permissions:** network access, and storing media on the watch.

## Android app

**Where it connects:**

- **Your server.** Release builds only connect over HTTPS.
- **GitHub**, only when you tap the update check in About. The check reads
  the latest release number; it sends nothing about you or your library.
- **Album art from listening services.** Some dashboard cards show album art
  that your server got from Last.fm or ListenBrainz. The app loads those
  images directly from where that art is hosted (Last.fm's image servers, or
  the Cover Art Archive for ListenBrainz), without sending your Trobar
  credentials.

Links in the About screen (source code, documentation, support on Ko-fi)
open in your browser; the app doesn't fetch them itself.

**When:** when you use the app, and also in the background: a sync runs
every six hours, with the network and charging conditions you choose in
Settings.

**What it sends to your server:**

- at pairing: the pairing code, a device name (your phone's model unless you
  change it), the device type, and the storage limit and audio format you
  chose;
- after pairing, with every request: its access token and your app language;
- the free and total space of the storage it syncs to, and how much of the
  music there didn't come from Trobar;
- the storage limit and the "which side is right" setting, when you change
  them;
- the paths of the music files already in the sync folder, once, on the
  first sync after pairing, so the server can recognise them instead of
  downloading them again;
- the playlists (.m3u and .m3u8 files) you keep in the sync folder, so they
  can be matched to the library;
- for each track: whether it was downloaded or removed and how many bytes
  are on the device; separately, which file holds which track (track id,
  audio fingerprint and path), so the files can be recognised again later;
- what you decide about tracks that went missing from the device;
- with artist pictures turned on for the device (a setting in Trobar's web
  page), the name of every top-level folder in the sync folder, including folders Trobar didn't create, to ask for that
  artist's picture;
- what you do in the app itself: browsing the library, choosing albums and
  playlists, the playlist links you share with it (your server then fetches
  those playlists).

**What it keeps on the phone**, in the app's private storage: the server
address, the access token (encrypted with a key held by Android's keystore),
your settings, the time and result of the last sync, a small database of the
files it wrote (track id, fingerprint, path), and a cache of cover images.
Android's cloud backup is turned off for the app. The music, playlists and
artist pictures go into the folder you picked.

**Permissions:** network access and network state; the camera, only to scan
the pairing QR code; notifications, to show sync progress; and running a
sync in the foreground. The background-sync library Android apps use for
scheduled work may add its own permissions to run after a restart. The app
needs no storage permission: you grant access to one folder through
Android's folder picker.

## Desktop app

**Where it connects:**

- **Your server.**
- **GitHub**, only when you click the update check in About, as in the
  Android app.

**What it sends to your server**, for the memory card it syncs:

- at pairing: the pairing code, a device name (the card's name unless you
  change it), the type "sdcard", and your app language;
- after pairing, with every request: the card's access token;
- the card's free and total space, and the storage limit when you change it;
- the "which side is right" setting, when you change it;
- the paths of the music files already on the card, on the first sync after
  pairing;
- the card's .m3u and .m3u8 playlists;
- for each track: whether it was downloaded or removed and how many bytes
  are on the card; separately, which file holds which track (track id,
  audio fingerprint and path);
- what you decide about tracks that went missing from the card;
- with artist pictures turned on for the card (a setting in Trobar's web
  page), the names of the card's top-level folders, to ask for those artists' pictures.

**What it keeps:**

- **on the card**, in a `.trobar` folder: the server address and the card's
  access token, in plain text, so the card can be synced from any computer;
  the result of the last sync (an error message there can include a file
  path); and the list of which file holds which track (track ids,
  fingerprints and paths). Anyone holding the card can read all of this.
  Removing the device in Trobar's web page revokes the token.
- **on the computer**, in your user's settings folder: language, theme,
  automatic-sync settings, and the folders you picked by hand. No token.

The app writes no log files.

**Permissions:** on macOS, network access only. It finds cards by looking
for its `.trobar` folder in mounted drives, and uses the system's own disk
tools to tell what kind of drive each one is and how much space it has.

## Home Assistant integration

**Where it connects:** only to the server address you configure, using an
access token an administrator creates in Trobar.

**What it sends to your server:** requests for the status of the server, its
devices and its playlist mirrors, every five minutes; and a library scan when
you press the scan button.

**What it shows in Home Assistant:**

- for every device in the household, not only yours: its name, type and
  owner's username, whether it is yours, its free and total space, pending
  tracks, how many files on it Trobar doesn't know, and its last sync;
- for the server: its version, library size and last scan;
- when playlist mirrors fail: for each failing playlist, its title, where it
  was being copied to, the error and when it was last written. This list
  covers the whole server, so it includes other users' playlist titles.

No track titles and no listening history.

**What Home Assistant keeps:** the server address and token, in Home
Assistant's own configuration storage. Home Assistant's recorder stores the
history of these entities, owner usernames and playlist titles included,
unless you exclude them in its recorder settings.

## The server

### What it keeps

Everything is in `DATA_DIR` on your machine (see
[Backups & DATA_DIR](operations/backups.md)):

- **accounts:** username; email address, for users who sign in through an
  identity provider; a password hash, when Trobar does the sign-in; an
  uploaded profile picture; your settings; your Last.fm and ListenBrainz
  usernames and Last.fm API key, and your Maloja address, if you entered
  them. Your Maloja API key and ListenBrainz user token, if you entered them,
  and your Last.fm session and account name, if you connected it, are stored
  encrypted, the name aside (see below);
- **linked accounts:** when you link Spotify or Tidal, that account's id,
  display name and access, and for Tidal which permissions the link was
  granted; when you map your account to a Jellyfin, Emby or
  Roon user, that user's id or profile;
- **devices:** name, type, owner, a hash of the device's token (not the token
  itself), storage figures the device reported, and when it last synced;
- **access for apps:** hashes of pairing codes and of the tokens
  administrators create for integrations such as Home Assistant, and a list
  of signed-out sessions;
- **your music choices:** selections, the basket, playlists, the public
  playlist links you added, and how playlist tracks matched your library;
- **Tidal shares:** for each playlist shared between two members, who
  shared it with whom, its title, the Tidal ids of the playlist and of the
  copy, its track ids as of the last sync, the tracks Tidal refused on
  either side, the last error, whether and when the owner asked for
  their original to be deleted after the share ended, and, if that was
  refused, which tracks of the original the copy lacked;
- **sync state:** which tracks each device has, audio fingerprints of
  tracks, and for files a device had that weren't from Trobar, their path,
  artist, album and title;
- **a sync count:** how many device syncs have finished without an error,
  one number for the whole server, with nothing about which device or
  account they came from. It decides when administrators see a thank-you
  card, and whether each administrator dismissed it;
- **connections you set up:** provider addresses, keys and passwords, the
  Roon pairing token, and the access granted when you link a Spotify or
  Tidal account. These are stored in plain text, so protect `DATA_DIR` like
  a password store. The Maloja API key, ListenBrainz user token and Last.fm
  session are the exception: they are encrypted, with a key kept in a file of its own. That
  protects a leaked database or snapshot. It protects against someone who can
  read all of `DATA_DIR` only if an administrator keeps that key elsewhere;
- **caches** of cover art and artist pictures, and MP3 copies of tracks
  transcoded for devices (see
  [Administration](administration.md#devices)).

**IP addresses** are never written to disk or to the logs. The server holds
some in memory only, so a restart clears them:

- to slow down repeated failed attempts, it keeps the address that failed
  to sign in, pair a device or use a device or integration token, with the
  times of those failures. An address is kept until five minutes have
  passed since its last failure. It is then removed the next time that
  address is checked, or by a sweep that runs at most once a minute
  whenever the server checks any address (on a sign-in, a pairing, a
  password change, or a device or integration token). An address that
  fails nothing isn't kept;
- with its default proxy settings, and unless it runs behind an
  authenticating proxy, it remembers the addresses
  that connected to it directly over the last seven days, up to 200 of
  them, to warn the administrator when the server looks reachable from the
  internet without a proxy in front.

By default the server keeps no listening history of its own. Each person can
turn on keeping plays reported by their devices. The server then stores, for
each play, the track, its artist, title and album, the device and the time.
This is off until you turn it on, and it applies to you alone. While it is on,
your plays feed your own Suggestions and Most played. Turning it off stops
storing new plays and stops using the stored ones: they stay stored but aren't
used, and count again if you turn it back on. You can delete what is stored at
any time, and removing your account deletes it.

When you connect Last.fm, ListenBrainz or Maloja, the server reads your history
from there when a page needs it, and holds the answer in memory for five
minutes.

If you keep plays, you can also turn on sending them to your own Maloja,
ListenBrainz or Last.fm. Each is a separate switch, off by default. Plays already sent stay
there if you delete them here.

### What it contacts

On its own, the server contacts nothing outside your network. Everything
below happens only when you, or your administrator, set it up:

| Service | When | What it is sent |
|---|---|---|
| Your music server (Roon, Jellyfin, Plex…), Lidarr, Music Assistant, Kodi, Maloja | once you configure it | what that connection needs |
| Your identity provider | when sign-in through it is set up | the sign-in exchange, to learn who you are |
| Last.fm | once a username or an API key is set | the API key; your username, to read your history and charts; artist names, to find similar artists. By default these requests use HTTPS, and the API key is kept out of the server's log; if you turn on sending, your session and each play: artist, title, album and time |
| ListenBrainz | once a username is entered | the username, to read listening history and charts; if you turn on sending, your user token and each play: artist, title, album and time |
| Spotify, Tidal | once a user links an account | requests on that account, to read its playlists |
| Tidal | only for a playlist shared between two linked members, while the share is active, and once after it ends if its owner asks for their original to be deleted | requests on the recipient's account, to create and update the copy; for a two-way share, on the owner's account, to add or remove tracks; and for that deletion, on both, to check the copy holds every track before deleting the owner's playlist |
| Spotify, YouTube Music | once a user adds a public playlist link | the playlist's address, to read it |
| AcoustID, then MusicBrainz | only with an AcoustID key | each track's audio fingerprint to AcoustID, and the recording it matched to MusicBrainz, to find the track's ISRC |
| MusicBrainz | only while Lidarr requests are set up | the artist and title (or ISRC) of playlist tracks missing from your library |
| TheAudioDB | only with a TheAudioDB key | artist names, for artist pictures |

### What your browser contacts

The web page loads everything from your server, with three exceptions:

- **suggestion covers** load from where that art is hosted: Last.fm's image
  servers for Last.fm suggestions, the Cover Art Archive for ListenBrainz
  ones;
- **Gravatar**, for users signed in by an authenticating proxy who haven't
  uploaded a profile picture: the browser asks Gravatar for a picture
  matching an MD5 hash of their email address (of an empty address, if they
  have none);
- **GitHub**, only when someone clicks the update check in About.

## Questions

Trobar is open source: every statement here can be checked in the code.
Questions and corrections are welcome as an
[issue](https://github.com/missing-foss/trobar-server/issues).
