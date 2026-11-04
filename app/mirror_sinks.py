# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The six playlist mirror sinks, as one list the rest of the app loops over.

Every sink module follows the same contract (MirrorBackend): write_mirror()
rewrites one playlist's copy and is a no-op unless that sink's own enabled
flag is on, delete_mirror() removes it and forgets the stored id or file,
and neither raises; a failure is recorded on the playlist row.

Sinks talk to servers and shares, so no write transaction may be open while
they run: every other writer would wait for those calls. write_all() and
delete_all() commit what the caller has written before the first sink and
after each one, and a sink that writes and then calls out again commits first.
So a playlist's own change is kept even if a sink then fails or the process
dies, and a mirror left behind is put right by the next sync.

With no transaction to keep them apart, two writers on one playlist would each
read its row, call out, and store what they found: a mirror switched off
mid-write would keep its copy, and two writes would make two. So everything
that reads a playlist's mirror state and acts on it holds playlist_lock() for
as long as it runs. Take it with nothing uncommitted: a sink holding it may be
about to write, and would wait on a transaction whose owner is waiting for the
lock. The server is one process, so a lock in memory reaches every writer.

playlist_sync and the mirror toggle reach the sinks through SINKS rather
than naming each one, so they cannot drift apart again.

The registry is not in mirror.py, which is itself the filesystem sink and
one of the six.

A sink's functions are looked up on its module at call time, so a test
that patches `mirror_subsonic.write_mirror` still reaches every caller.

Five sinks build an index of the target's library on their first write and
can share it across one sync run: new_caches() makes one dict per such sink,
and write_all() hands each its own under the keyword that sink names it
(`tag_index_cache` or `index_cache`). The filesystem sink takes none.
"""
import logging
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol

import mirror
import mirror_emby
import mirror_jellyfin
import mirror_music_assistant
import mirror_plex
import mirror_subsonic

_log = logging.getLogger(__name__)


class MirrorBackend(Protocol):
    def write_mirror(self, conn: sqlite3.Connection, playlist_id: int) -> None: ...

    def delete_mirror(self, conn: sqlite3.Connection, playlist_id: int) -> None: ...


@dataclass(frozen=True)
class MirrorSink:
    # The sink's name in the toggle route, the admin page and mirror_runs.
    key: str
    backend: MirrorBackend
    # The playlists column that turns this sink on for one playlist.
    enabled_column: str
    # write_mirror()'s keyword for the per-run index cache; None takes none.
    cache_keyword: str | None


# In the order every caller has always written and deleted them.
SINKS: tuple[MirrorSink, ...] = (
    MirrorSink("filesystem", mirror, "mirror_enabled", None),
    MirrorSink("subsonic", mirror_subsonic, "subsonic_mirror_enabled", "tag_index_cache"),
    MirrorSink("jellyfin", mirror_jellyfin, "jellyfin_mirror_enabled", "tag_index_cache"),
    MirrorSink("emby", mirror_emby, "emby_mirror_enabled", "tag_index_cache"),
    MirrorSink("music_assistant", mirror_music_assistant, "music_assistant_mirror_enabled",
               "index_cache"),
    MirrorSink("plex", mirror_plex, "plex_mirror_enabled", "index_cache"),
)
BY_KEY = {s.key: s for s in SINKS}


_locks: defaultdict[int, threading.RLock] = defaultdict(threading.RLock)
_locks_guard = threading.Lock()


def playlist_lock(playlist_id: int) -> threading.RLock:
    """The lock on one playlist's mirrors. See the module docstring."""
    with _locks_guard:
        return _locks[playlist_id]


def new_caches() -> dict[str, dict]:
    """One empty index cache per sink that takes one, for one sync run."""
    return {s.key: {} for s in SINKS if s.cache_keyword is not None}


def write_all(conn: sqlite3.Connection, playlist_id: int,
              caches: dict[str, dict] | None = None, *, with_caches: bool = False) -> None:
    """Every sink's write_mirror() for one playlist, in order. With
    `with_caches`, each sink that takes a cache gets its own from `caches`
    (None when there is none), as a sync passes them; without, the call is
    the bare (conn, playlist_id) an edit made here has always used. Commits."""
    conn.commit()
    with playlist_lock(playlist_id):
        for s in SINKS:
            if with_caches and s.cache_keyword is not None:
                cache = caches.get(s.key) if caches is not None else None
                s.backend.write_mirror(conn, playlist_id, **{s.cache_keyword: cache})
            else:
                s.backend.write_mirror(conn, playlist_id)
            conn.commit()


def delete_all(conn: sqlite3.Connection, playlist_id: int) -> None:
    """Every sink's delete_mirror() for one playlist, in order. Commits."""
    conn.commit()
    with playlist_lock(playlist_id):
        for s in SINKS:
            s.backend.delete_mirror(conn, playlist_id)
            conn.commit()
