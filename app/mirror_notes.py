#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mirror notes Emby drops, written again once it has dropped them.

Emby runs its own metadata refresh a few seconds after a playlist's items
change, and that refresh resets the playlist's Overview, where the mirror's note
("Trobar mirror — N of M present, …") goes. Locking the field doesn't stop it.
Measured on a real Emby 4.10.1.0: a note written once was gone at 3 s on every
try; the same note written again 6 s after the item change was still there at
8, 20 and 60 s.

So a mirror run notes each Emby note it writes, and once the run is over, one
timer writes them all again DELAY_SECONDS after the run's last one: one wait per
run, not per playlist, and nobody waits on it. Only the latest write to a
playlist, from whichever run, is written again, so a slower run never puts an
older title and note back. A sync is one run (see
playlist_sync.sync_playlists); a write outside a run, such as an edit made in a
request, is a run of its own. The timer calls the server's API and nothing
else, so it never touches the database, and like the first write it never
raises: the note is cosmetic.
"""

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator

_log = logging.getLogger(__name__)

# After the run's last Emby write. Measured: Emby's refresh had reset the note
# by 3 s, and a note written at 6 s stayed.
DELAY_SECONDS = 6.0

_local = threading.local()
_timers: list[threading.Timer] = []
_timers_lock = threading.Lock()
# The latest write's number for each (sink, remote id), across runs. A sync
# runs in one thread and an edit in another: without this, the run whose
# timer fires last would put its older title and note back over a newer one.
# A rewrite runs only while its write is still the latest for its playlist.
_latest: dict[tuple[str, str], int] = {}


class _Run:
    def __init__(self) -> None:
        # (sink name, remote id) -> (that write's number, the call that writes
        # the note again); a playlist written twice in one run is written
        # again once, with its last note.
        self.writes: dict[tuple[str, str], tuple[int, Callable[[], None]]] = {}
        self.last_at = 0.0


@contextlib.contextmanager
def run() -> Iterator[None]:
    """One mirror run: the notes it writes are written again once, after it."""
    outer = getattr(_local, "run", None)
    if outer is not None:  # already inside one: it is the run
        yield
        return
    current = _local.run = _Run()
    try:
        yield
    finally:
        _local.run = None
        _schedule(current)


def note_written(sink: str, remote_id: str, write_again: Callable[[], None]) -> None:
    """A note was just written to `remote_id` on `sink`; `write_again` writes
    it once more. Outside a run, it is a run of its own."""
    key = (sink, str(remote_id))
    with _timers_lock:
        number = _latest.get(key, 0) + 1
        _latest[key] = number
    current = getattr(_local, "run", None)
    if current is None:
        single = _Run()
        single.writes[key] = (number, write_again)
        single.last_at = time.monotonic()
        _schedule(single)
        return
    current.writes[key] = (number, write_again)
    current.last_at = time.monotonic()


def _schedule(finished: _Run) -> None:
    if not finished.writes:
        return
    delay = max(0.0, finished.last_at + DELAY_SECONDS - time.monotonic())
    timer = threading.Timer(delay, _write_again, args=(list(finished.writes.items()),))
    timer.daemon = True
    with _timers_lock:
        _timers[:] = [t for t in _timers if t.is_alive()]
        _timers.append(timer)
    timer.start()


def _write_again(writes: list[tuple[tuple[str, str], tuple[int, Callable[[], None]]]]) -> None:
    for key, (number, write) in writes:
        with _timers_lock:
            if _latest.get(key) != number:
                continue  # a newer write to this playlist rewrites it instead
        try:
            write()
        except Exception:  # noqa: BLE001 -- cosmetic, as the first write
            _log.warning("A mirror note could not be written again", exc_info=True)


def wait_for_pending(timeout: float = 30.0) -> bool:
    """Waits for every scheduled rewrite to finish; True if they all did. For
    tests."""
    deadline = time.monotonic() + timeout
    with _timers_lock:
        pending = list(_timers)
    for timer in pending:
        timer.join(max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in pending)
