# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A few minutes' memory of what the listening-history services answered.

The Suggestions tab, the Most played widget and auto-fit each ask Last.fm,
ListenBrainz and Maloja for the same charts, up to six sequential calls of
up to 15 s each per page load. Their raw answers are kept here for
TTL_SECONDS, keyed by the call and its arguments. Only the raw answers: what
the feeds build from them also depends on the library and on what devices
hold, which changes from one minute to the next, so that part is computed
fresh every time.

An outage (None) is never kept, so the next request tries the service
again. clear() forgets everything; Refresh and Retry in the page call it, so
they always ask the services."""

import copy
import functools
import logging
import threading
import time

_log = logging.getLogger(__name__)

TTL_SECONDS = 300
# Enough for every user's few calls; the oldest goes first past it.
_MAX_ENTRIES = 512

_lock = threading.Lock()
_entries: dict[tuple, tuple[float, object]] = {}


def clear() -> None:
    with _lock:
        _entries.clear()


def cached(name: str):
    """Decorate a client's raw fetch (one returning None on failure)."""
    def wrap(fetch):
        @functools.wraps(fetch)
        def call(*args, **kwargs):
            key = (name, args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with _lock:
                hit = _entries.get(key)
                if hit is not None and now - hit[0] < TTL_SECONDS:
                    return copy.deepcopy(hit[1])
            value = fetch(*args, **kwargs)
            if value is not None:
                with _lock:
                    if len(_entries) >= _MAX_ENTRIES:
                        del _entries[min(_entries, key=lambda k: _entries[k][0])]
                    _entries[key] = (now, copy.deepcopy(value))
            return value
        return call
    return wrap
