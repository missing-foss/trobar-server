#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#189: playlist mirroring's fourth and (per the RFC) final sink — an Emby
server, alongside app/mirror.py's filesystem sink, app/mirror_subsonic.py's
Subsonic sink, and app/mirror_jellyfin.py's Jellyfin sink. Same contract,
same "one-way, golden-wins, full idempotent rewrite" semantics, for any
playlist with `emby_mirror_enabled = 1`.

Unlike the filesystem sink, there's no "don't clobber a file we don't own"
concern to guard (no marker/safe-path machinery here): every write either
creates a fresh remote playlist (a title, no remote_id yet) or replaces one
by the remote id THIS module itself stored on a previous write
(`emby_mirror_remote_id`) — there's no ambient shared namespace a stray
write could land in by accident the way an unmarked filesystem path could.

The actual request mechanics (tag-based song lookup, the GET/DELETE/POST
dance a "replace" decomposes into, best-effort name+comment) live in
emby_client.py's mirror_*() functions, confirmed live against a real Emby
4.9.5 instance — including three real divergences from the Jellyfin sink
despite the shared API lineage (see emby_client.py's module docstring). Those
it shares with Jellyfin are defined in mediabrowser_client.py.

The sink itself is mirror_mediabrowser.MediaBrowserMirror, shared with
mirror_jellyfin.py: this module binds it to emby_client and the emby_
columns, and exports its write_mirror and delete_mirror.
"""

import logging

import db
import emby_client
from mirror_mediabrowser import MediaBrowserMirror, _ERROR_NOT_FOUND  # noqa: F401

# Looked up on each call, not captured, so patching emby_client here reaches it.
_mirror = MediaBrowserMirror(
    "emby", "Emby", lambda: emby_client, lambda: db.get_mirror_emby_config())

_log = logging.getLogger(__name__)

write_mirror = _mirror.write_mirror
delete_mirror = _mirror.delete_mirror
