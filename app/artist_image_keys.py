# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The artist name → image key map a provider client builds once per
process, then answers every artist-image lookup from.

Building it means walking every artist the provider has (one or more
requests), which is too slow to repeat for each picture, and the artist list
rarely changes while the server runs. Subsonic, Jellyfin, Emby and Roon each
kept the same global map and lock for it; this is that pattern once.
"""
import logging
import threading
from collections.abc import Callable

_log = logging.getLogger(__name__)


class ArtistImageKeys:
    """Built on first use by the client's own builder, under a lock, so two
    first lookups at once make one walk, not two; every lookup after that
    is a dict get. clear() forgets the map, so the next lookup builds it
    again."""

    def __init__(self) -> None:
        self._map: dict[str, str] | None = None
        self._lock = threading.Lock()

    def get(self, build: Callable[[], dict[str, str]]) -> dict[str, str]:
        with self._lock:
            if self._map is None:
                self._map = build()
            return self._map

    def clear(self) -> None:
        with self._lock:
            self._map = None
