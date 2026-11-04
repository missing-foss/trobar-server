# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test-suite guard: finds SQLite write transactions held across slow work.

A write transaction left open while a file is read, a socket connects, a
subprocess starts or the thread sleeps shuts out every other writer for that
long, and one that a second connection then tries to write beside waits on
itself. Nothing else in the code stops that being written.

The guard runs only under dev/run_unittests.py and nothing in the app imports
it. It wraps sqlite3.connect so each connection is tracked by its thread, and
watches Python's audit events for the slow work. An offence is recorded under
the first frame in app code that isn't a test. Only connections app code
opened are watched, so a test's own fixtures don't count.

What it can't see: anything a test has mocked (a mocked HTTP client opens no
socket, a patched tag reader opens no file), os.stat, which raises no audit
event, and a connection a test opened and handed to the code under test. The
time limit below catches some of that, and no more."""

import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: A transaction open longer than this is an offence whatever it was doing.
LIMIT_SECONDS = 2.0

_APP = str(Path(__file__).resolve().parent.parent / "app") + "/"
_EVENTS = {
    "open": "file",
    "os.listdir": "file",
    "os.scandir": "file",
    "socket.connect": "network",
    "socket.getaddrinfo": "network",
    "subprocess.Popen": "subprocess",
    "time.sleep": "sleep",
}
#: A statement that takes the write lock. A read transaction takes none, so it
#: isn't an offence, however long it stays open.
_WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE",
           "CREATE", "DROP", "ALTER")
#: Python importing a module is not the code under test reading a file.
_MODULE_SUFFIXES = (".py", ".pyc", ".so")

_local = threading.local()
_real_connect = sqlite3.connect
_found: set[str] = set()
_capture: list[set[str]] = []
_installed = False


def _app_frame():
    """The innermost frame in app code that isn't a test or db.py, whose job
    is to open connections for everyone else; None if the stack has none."""
    frame = sys._getframe(1)
    while frame is not None:
        path = frame.f_code.co_filename
        name = Path(path).name
        if path.startswith(_APP) and not name.startswith("test_") and name != "db.py":
            return frame
        frame = frame.f_back
    return None


def _record(kind: str) -> None:
    frame = _app_frame()
    if frame is not None:
        key = f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_qualname} {kind}"
        (_capture[-1] if _capture else _found).add(key)


class _TrackedConnection(sqlite3.Connection):
    """Notes when a transaction opened, for the time limit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._guard_opened = None
        self._guard_writing = False
        self._guard_app = _app_frame() is not None
        if self._guard_app:
            if not hasattr(_local, "connections"):
                _local.connections = []
            _local.connections.append(self)

    def _guard_before(self, sql: str) -> None:
        """A write here with another of this thread's connections holding the
        lock waits on itself until the busy timeout."""
        if self._guard_app and sql.lstrip().upper().startswith(_WRITES):
            self._guard_writing = True
            if any(c is not self and c.writing() for c in _local.connections):
                _record("second-writer")

    def _guard_note(self) -> None:
        if self.in_transaction and self._guard_opened is None:
            self._guard_opened = time.monotonic()

    def writing(self) -> bool:
        """True while this connection holds the write lock."""
        return self._guard_writing and self.in_transaction

    def _guard_end(self) -> None:
        opened, self._guard_opened = self._guard_opened, None
        writing, self._guard_writing = self._guard_writing, False
        if self._guard_app and writing and opened is not None and time.monotonic() - opened > LIMIT_SECONDS:
            _record("held-too-long")

    def execute(self, *args):
        self._guard_before(args[0])
        cursor = super().execute(*args)
        self._guard_note()
        return cursor

    def executemany(self, *args):
        self._guard_before(args[0])
        cursor = super().executemany(*args)
        self._guard_note()
        return cursor

    def executescript(self, *args):
        self._guard_before(args[0])
        cursor = super().executescript(*args)
        self._guard_note()
        return cursor

    def commit(self):
        self._guard_end()
        super().commit()

    def rollback(self):
        self._guard_end()
        super().rollback()

    def close(self):
        self._guard_end()
        connections = getattr(_local, "connections", None)
        if connections is not None and self in connections:
            connections.remove(self)
        super().close()


def _connect(*args, **kwargs):
    kwargs.setdefault("factory", _TrackedConnection)
    return _real_connect(*args, **kwargs)


def _audit(event: str, args: tuple) -> None:
    kind = _EVENTS.get(event)
    if kind is None or getattr(_local, "busy", False):
        return
    connections = getattr(_local, "connections", None)
    if not connections:
        return
    _local.busy = True
    try:
        if event == "open" and isinstance(args[0], (str, bytes)) \
                and str(args[0]).endswith(_MODULE_SUFFIXES):
            return
        if any(c.writing() for c in connections):
            _record(kind)
    except sqlite3.ProgrammingError:
        pass  # a connection closed behind our back holds nothing
    finally:
        _local.busy = False


def install() -> None:
    """Start watching. An audit hook can't be removed; with no tracked
    connection in a transaction it costs a dictionary lookup."""
    global _installed
    if not _installed:
        _installed = True
        sqlite3.connect = _connect
        sys.addaudithook(_audit)


@contextmanager
def capture() -> Iterator[set[str]]:
    """What the guard finds inside the block, kept apart from the run's own
    list: the guard's tests offend on purpose."""
    install()
    found: set[str] = set()
    _capture.append(found)
    try:
        yield found
    finally:
        _capture.pop()


def found() -> set[str]:
    """Every offence so far, as `file.py:function kind`."""
    return set(_found)
