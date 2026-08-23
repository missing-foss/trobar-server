#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""iTunes/Apple Music Library.xml playlist import (#171) — a static,
zero-gate local playlist source in the same family as filesystem_client's
own .m3u discovery, folded directly into that provider (see its own
imports of this module) rather than a new active-provider: Library.xml is
explicitly published by Apple for third-party read access, no API/auth/
ToS gate. Since macOS Catalina, Apple Music.app no longer auto-generates
it — the user must File > Library > Export Library to produce one. That
makes this inherently a static snapshot, stale until re-exported, not a
live sync — an honest limitation the issue itself accepts rather than
tries to work around.

Parsed fresh on every playlist read (same as filesystem_client's own
.m3u discovery — no separate "import" action or file-change detection
needed): a re-exported file at the same configured path is picked up on
the very next sync automatically.

plistlib (stdlib) transparently handles both the XML and binary plist
formats Library.xml can be saved as — no external dependency, matching
the issue's "no API, no auth, no ToS gate" framing.

Playlists carry a Track ID array; each track's own Location is a file://
URL (not a bare path) — decoded and, when it falls under MUSIC_ROOT,
converted to the same root-relative form tracks.relative_path uses,
mirroring filesystem_client._resolve_entry_path's handling of absolute
.m3u entries.

Built-in library views (the "Library"/"Music"/"Movies"/"TV Shows"/
"Podcasts"/"Audiobooks"/"Purchased"/"Genius" entries iTunes itself
always creates, marked with a "Master" or "Distinguished Kind" key) are
excluded — they're per-media-type views, not something a user curated.
Smart playlists are NOT excluded: "Playlist Items" already holds the
snapshot Apple resolved at export time regardless of whether the
playlist is smart or static, so no rule evaluation is needed here.
"""

import plistlib
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _location_to_path(location: str) -> str | None:
    """Library.xml's Location is a file:// URL (macOS:
    file:///Users/...; Windows iTunes: file://localhost/C:/...) —
    decoded to a plain filesystem path, or None for anything that isn't
    a local file URL (a track streamed from Apple Music/missing from
    disk has no Location at all, or a non-file scheme)."""
    if not location:
        return None
    parts = urlsplit(location)
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    # urlsplit turns "file://localhost/C:/Users/..." into a path of
    # "/C:/Users/..." — strip the leading slash in front of a drive letter.
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def _resolve_path(root: Path, raw_path: str) -> str:
    """Mirrors filesystem_client._resolve_entry_path's absolute-path
    handling: converted to the same root-relative form
    tracks.relative_path uses when it falls under MUSIC_ROOT (an exact
    match), left as the raw absolute path otherwise — matching.py's
    trailing-segment comparison tolerates a differing mount prefix (the
    library was very likely exported from a different machine/OS than
    the one running Trobar)."""
    try:
        return str(Path(raw_path).relative_to(root))
    except ValueError:
        return raw_path


def _is_user_playlist(entry: dict) -> bool:
    """Excludes the built-in per-media-type views iTunes always creates
    — not something a user curated. Smart playlists are deliberately NOT
    excluded here (see module docstring)."""
    return not entry.get("Master") and "Distinguished Kind" not in entry


def parse_library(xml_path: Path, music_root: Path) -> list[dict]:
    """Returns [{"id", "title", "tracks": [{"position", "title", "artist",
    "path", "album"}, ...]}, ...] — the same common playlist/track shape
    every provider client returns, so filesystem_client can merge these
    straight into its own list_playlists()/get_playlist_tracks() output.
    Returns [] if the file is missing, unreadable, or not a valid plist —
    a bad or stale-pointing config here degrades to "no iTunes
    playlists" rather than breaking the rest of the sync, same contract
    as every other optional data source in this codebase (e.g.
    get_artist_image's "a miss just means no picture")."""
    try:
        with xml_path.open("rb") as f:
            library = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return []

    tracks_by_id = library.get("Tracks") or {}

    playlists = []
    for entry in library.get("Playlists") or []:
        if not _is_user_playlist(entry):
            continue
        title = entry.get("Name")
        playlist_id = entry.get("Playlist Persistent ID")
        if not title or not playlist_id:
            continue

        tracks: list[dict] = []
        for item in entry.get("Playlist Items") or []:
            track = tracks_by_id.get(str(item.get("Track ID")))
            if track is None:
                continue
            path = _location_to_path(track.get("Location") or "")
            if path is None:
                continue
            tracks.append({
                "position": len(tracks),
                "title": track.get("Name") or Path(path).stem,
                "artist": track.get("Artist") or "",
                "path": _resolve_path(music_root, path),
                "album": track.get("Album"),
            })
        playlists.append({"id": playlist_id, "title": title, "tracks": tracks})
    return playlists
