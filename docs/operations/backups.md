<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Backups & DATA_DIR

`DATA_DIR` is **everything Trobar keeps**: the SQLite database (users, devices,
selections, playlists, provider config), the cover/artist-image caches, user
avatars, the session secret key, and the Roon pairing token. Your **music
library is not in here** — Trobar treats `MUSIC_ROOT` as read-only truth that
you back up separately.

## What to back up

Back up **`DATA_DIR`**. Because it holds plaintext provider credentials and the
session key, treat it like a password store — **back it up encrypted**, and
restrict who can read it.

!!! danger "Back it up to the NAS — don't run it from there"

    A network share is a fine **destination** for these backups and a dangerous
 **home** for `DATA_DIR` itself. SQLite needs working file locking, which
    NFS, SMB/CIFS and similar shares don't provide reliably; running the live
    database from one can corrupt it.

 So: keep `DATA_DIR` on local disk on the machine running the server, and
    copy the snapshots produced below onto your NAS. That gets you the durability
    you wanted without risking the database. See
    [Installation](../getting-started/installation.md#the-two-mounts-that-matter).

## How

The safest snapshot is with the app stopped, so SQLite isn't mid-write:

```bash
docker compose down
tar czf trobar-data-$(date +%F).tar.gz -C ./data .   # bind-mount default
docker compose up -d
```

For a named volume, copy it out with a throwaway container:

```bash
docker run --rm -v <volume>:/data -v "$PWD":/backup alpine \
  tar czf /backup/trobar-data-$(date +%F).tar.gz -C /data .
```

## Restore

Stop the stack, replace the contents of `DATA_DIR` with the backup, make sure
everything is owned by uid `10001` (see
[Upgrading](upgrading.md)), and start again.

!!! note "The caches rebuild themselves"
    Losing only the cover/artist-image caches is harmless — they re-fetch. The
    database is the part that matters; a device that still has its files can even
    re-seed the server after a database loss (the client uploads its manifest and
    the server re-matches it), but a proper backup is the intended safety net. See
    [Device loss, replacement & migration](../using/device-recovery.md) for the
    per-client details of that recovery path, and for what happens when it's the
    *device* that's lost or replaced instead.
