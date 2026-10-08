<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Kodi

Kodi isn't a library provider for Trobar. It's a place to send playlists.
Trobar's **filesystem mirror** writes `.m3u` files, one folder per user, and
each Kodi profile can read its playlists from a folder of your choosing.
Point each profile at its user's folder and that person's mirrored playlists
show up under **Music → Playlists** in their profile, and nobody else's. No
add-on, no remote access to Kodi, no credentials: it works whether or not
Kodi is running.

Verified against Kodi **21.2** (Omega), with the music on an SMB share.

## How Trobar lays out the mirror folder

Every mirrored playlist goes into its owner's folder under the mirror folder:

```
<mirror folder>/
  alice/
    music/Road_Trip_Trobar_.m3u
    mixed/
  bob/
    music/...
    mixed/
  _shared/
    music/...
    mixed/
```

- **The folder name** comes from the username, made safe for a file system;
  a name with nothing usable left becomes `user-<id>`. It is fixed the first
  time Trobar writes for that user.
- **`_shared/`** holds the playlists nobody owns (provider-wide, shared ones).
  Any profile can be pointed at it instead.
- **`music/` and `mixed/`** are the two subfolders Kodi reads. Trobar writes
  into `music/` and creates an empty `mixed/`, because Kodi refuses the
  folder ("Error 2: share not available") if either is missing.
- **File names** end in `_Trobar_`, and Kodi shows playlists by file name, so
  the suffix is visible in Kodi. It is how Trobar recognises its own files.

## Setting Kodi's Playlists folder

Each Kodi profile has a **Playlists folder** setting (`system.playlistspath`),
where it reads and saves playlists. **It isn't in Kodi's settings screens**,
not even at the Expert level: Kodi files it as an internal setting (Kodi 21,
22 and its development branch alike). Set it per profile, one of two ways:

- **Edit the profile's `guisettings.xml` while Kodi is stopped.** The master
  profile's is `userdata/guisettings.xml`; another profile's is under
  `userdata/profiles/<name>/`. Set the line
  `<setting id="system.playlistspath">smb://nas/Music/UserPlaylists/alice/</setting>`
  (your own path, ending in `/`). Kodi rewrites this file when it exits, so
  an edit made while it runs is lost.
- **Or over JSON-RPC, while that profile is loaded**, if that profile has
  Kodi's web interface on (Settings → Services → Control): call
  `Settings.SetSettingValue` with
  `{"setting": "system.playlistspath", "value": "smb://nas/Music/UserPlaylists/alice/"}`.
  The value is kept when Kodi exits cleanly.

An `smb://` path is what was verified. Kodi takes `nfs://` and local paths in
the same setting, but they haven't been tested here.

## Paths Kodi can open

A mirror lists each track, and Kodi has to be able to open those paths.
There are two ways.

### Recommended: the mirror folder inside the music share

Put the mirror folder inside the share that holds your music, e.g.
`Music/UserPlaylists/`, and tell Trobar where it sits. The playlists then
list tracks relative to their own folder
(`../../../Artist/Album/track.flac`), which Kodi resolves against the
playlist file, on any machine and at any mount point.

1. **Mount the same share twice.** Trobar never writes to your library, so the
   music stays read-only and the mirror folder gets a writable mount of its
   own. With the standard `docker-compose.yaml`, in `.env`:

    ```
    MUSIC_VOLUME=/mnt/nas/Music
    MIRROR_VOLUME=/mnt/nas/Music/UserPlaylists
    ```

    The music is mounted read-only at `/music`, the mirror folder writable at
    `/mirrors`.
2. **In Trobar**, under Administration → Configuration → Playlist mirroring,
   set **Location inside the music share** to `UserPlaylists`. Trobar checks
   it by writing a test file through `/mirrors` and looking for it under
   `/music/UserPlaylists`; if the two mounts aren't the same folder, it says
   so and saves nothing.
3. **In Kodi**, point each profile's Playlists folder at its user's folder,
   e.g. `smb://nas/Music/UserPlaylists/alice/`.

Trobar's playlist discovery skips the files it wrote itself, so a mirror
inside the music share never comes back as a source playlist. A playlist
someone saves from Kodi into the same folder is an ordinary `.m3u`, and
Trobar picks it up like any other.

### Without a location: absolute paths

With the location left empty, mirrors list each track by its path as Trobar
sees it (`/music/Artist/Album/track.flac` in the standard Docker setup).
Kodi can't open that as such, but its `userdata/advancedsettings.xml` can
rewrite the prefix to the share:

```xml
<advancedsettings version="1.0">
  <pathsubstitution>
    <substitute>
      <from>/music/</from>
      <to>smb://nas/Music/</to>
    </substitute>
  </pathsubstitution>
</advancedsettings>
```

Restart Kodi after creating or editing that file. Then point each profile's
Playlists folder at its user's folder as above.

## Upgrading from the flat mirror folder

Before the per-user layout, Trobar wrote every mirror straight into the
mirror folder. On the first sync after upgrading, each mirror is rewritten
into its owner's folder and the old file is removed (only if it carries
Trobar's marker; any other file is left alone). A player or Kodi profile that
read the flat folder has to be pointed at a user's folder, or at `_shared/`.
