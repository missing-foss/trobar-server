<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Backups & DATA_DIR

`DATA_DIR` is **everything Trobar keeps**: the SQLite database (users, devices,
selections, playlists, provider config), the cover/artist-image caches, the
transcode cache, user avatars, the session secret key, and the Roon pairing
token. Your **music
library is not in here** — Trobar treats `MUSIC_ROOT` as read-only truth that
you back up separately.

## What to back up

Back up **`DATA_DIR`**. Because it holds plaintext provider credentials and the
session key, treat it like a password store — **back it up encrypted**, and
restrict who can read it.

**The credential key.** The Maloja API key, ListenBrainz user token and Last.fm
session that send plays, the Last.fm shared secret, and each person's Tidal
refresh token are encrypted with a key in `credential_key`. A database snapshot doesn't
include that file, so a leaked snapshot doesn't give anyone those values, and a
copy of all of `DATA_DIR` does include it. To keep it out, set
`TROBAR_CREDENTIAL_KEY_FILE` to a path outside `DATA_DIR` and back that file up
separately. If the key is lost or changes, nothing breaks: those values read
as gone, their owners see a request to enter them again (for Tidal, Connect
again on their Profile), and plays, playlists and every other setting stay as
they were. A key file that is damaged is different:

- the play-sending credentials read as gone, and saving one fails until you
  delete that file; Trobar makes a new key the next time one is saved;
- Tidal links are kept. Tidal syncs for those people stop until the key works
  again, and linking Tidal is refused meanwhile. A token Tidal renews during a
  sync in that time is stored unencrypted, until a start with a working key
  encrypts it.
  Restore the key from a backup if you have one: deleting the damaged file
  makes it a lost key, and everyone connects Tidal again.

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

### While Trobar keeps running

The database runs in SQLite's WAL mode: recent writes sit in
`music-sync.db-wal` until SQLite folds them into `music-sync.db`. So a plain
copy or `tar` of a running `DATA_DIR` is **not** a safe backup. It can catch
the two files at different moments, and a database restored from that can be
missing writes or be corrupt.

Let SQLite take the copy instead. Its backup API produces a consistent snapshot
while the server keeps reading and writing. The image has no `sqlite3` command,
so use its Python:

```bash
# 1. A consistent copy of the database, next to the live one.
docker compose exec trobar python -c "import sqlite3; dst = sqlite3.connect('/data/music-sync.db.snapshot'); sqlite3.connect('/data/music-sync.db').backup(dst); dst.close()"

# 2. Everything else, leaving out the live database files.
sudo tar czf trobar-data-$(date +%F).tar.gz -C ./data \
  --exclude=./music-sync.db --exclude=./music-sync.db-wal --exclude=./music-sync.db-shm .

# 3. Remove the snapshot from DATA_DIR again.
docker compose exec trobar rm /data/music-sync.db.snapshot
```

`sudo`, because the files belong to the container's uid `10001` and some
(the session key) are readable by that user only. A plain `tar` stops at
them.

The transcode cache, `transcode/`, can be left out with
`--exclude=./transcode`: it holds only copies the server makes again as
devices sync, up to the size set in
[Administration](../administration.md#devices).

For a named volume, run step 2 in a throwaway container:

```bash
docker run --rm -v <volume>:/data -v "$PWD":/backup alpine \
  tar czf /backup/trobar-data-$(date +%F).tar.gz -C /data \
  --exclude=./music-sync.db --exclude=./music-sync.db-wal --exclude=./music-sync.db-shm .
```

The archive holds the database as `music-sync.db.snapshot`; [Restore](#restore)
says what to do with it.

### With the server stopped

Stopping the stack first also gives a consistent copy, with no snapshot step:

```bash
docker compose down
sudo tar czf trobar-data-$(date +%F).tar.gz -C ./data .   # bind-mount default
docker compose up -d
```

For a named volume:

```bash
docker run --rm -v <volume>:/data -v "$PWD":/backup alpine \
  tar czf /backup/trobar-data-$(date +%F).tar.gz -C /data .
```

## Restore

Stop the stack, replace the contents of `DATA_DIR` with the backup, make sure
everything is owned by uid `10001` (see
[Upgrading](upgrading.md)), and start again.

If the backup was taken while Trobar was running, rename
`music-sync.db.snapshot` to `music-sync.db`. Make sure no `music-sync.db-wal`
or `music-sync.db-shm` is left in `DATA_DIR` from before: SQLite would apply
a leftover `-wal` to the restored database.

!!! note "The caches rebuild themselves"
    Losing only the cover/artist-image caches is harmless — they re-fetch. The
    database is the part that matters; a device that still has its files can even
    re-seed the server after a database loss (the client uploads its manifest and
    the server re-matches it), but a proper backup is the intended safety net. See
    [Device loss, replacement & migration](../using/device-recovery.md) for the
    per-client details of that recovery path, and for what happens when it's the
    *device* that's lost or replaced instead.
