#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Filesystem-only provider — no external server at all. MUSIC_ROOT (see
db.get_music_root — DB-editable via the setup wizard,, env var only
seeds the initial default) is the same root scanner.py already walks to
build the `tracks` catalog, so this provider's "connection" is just "is that
directory there," always paired if so.

Two things an external provider (Roon/Subsonic/Jellyfin) normally supplies
have real filesystem conventions of their own, confirmed against how actual
music-organizer tools (Jaikoz/SongKong, Kodi, Jellyfin itself) lay files out
on disk rather than invented here:

- Artist images: Jaikoz's own "save artwork to filesystem" feature, Kodi, and
  Jellyfin all converge on a single image file placed directly in the
  artist's own folder (not nested under an album), named some variant of
  folder/cover/poster/artist/thumb. Checked live against this app's own
  library (`MUSIC_ROOT`) — none of these exist yet anywhere in it, so this
  starts empty, but populates itself the moment such a file appears (from
  Jaikoz or dropped in by hand), no re-scan or config needed since it reads
  straight off disk on every request (see artist_images.py's own on-disk
  cache in front of this, though — a cache entry written before an image
  file existed won't refresh itself; same caveat as any other provider).
- Playlists: `.m3u`/`.m3u8` is the universal *format* (EXTM3U + optional
  "#EXTINF:duration,Artist - Title" + a path per line), but unlike artist
  images there's no agreed-on *location* convention across tools (Kodi keeps
  them entirely outside the music tree; Jaikoz doesn't manage playlists at
  all) — so this just walks MUSIC_ROOT itself for any such file, wherever it
  is. Entries may be relative (to the playlist file's own folder) or
  absolute; either way the resolved string is handed to matching.py's
  existing match_playlist_track_by_path(), whose trailing-segment comparison
  already tolerates a differing mount prefix (built for Subsonic/Jellyfin,
  works the same way here).

An optional second playlist source lives alongside the .m3u discovery
above: an admin-configured path to an exported iTunes/Apple Music
Library.xml (#171) — see itunes_library.py for the parsing and its own
docstring for why this is folded into the filesystem provider rather than
a new active-provider. `_ITUNES_ID_PREFIX` disambiguates its ids from
.m3u-file-path ids in the shared list_playlists()/get_playlist_tracks()
namespace (the two happen to never collide in practice — a Playlist
Persistent ID is 16 hex digits, not a path — but the prefix makes that
guaranteed rather than assumed).
"""

import os
from pathlib import Path

import db
import itunes_library

_ITUNES_ID_PREFIX = "itunes:"

_PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")

_ARTIST_IMAGE_BASENAMES = ("folder", "cover", "poster", "artist", "thumb")
_ARTIST_IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
}


def ensure_started() -> None:
    """Nothing to start — kept for interface parity with the other provider
    clients so main.py's startup dispatch needs no special case."""
    pass


def status() -> dict:
    root = db.get_music_root()
    if root.is_dir():
        return {"state": "paired", "root": str(root), "provider": "filesystem"}
    return {"state": "disconnected", "root": str(root), "provider": "filesystem"}


def retry_pairing() -> dict:
    """Nothing to retry beyond re-checking the mount — kept as its own
    function purely for interface parity with the other providers'
    retry_pairing, so main.py's dispatch never needs a provider-specific
    branch."""
    return status()


def reconnect() -> dict:
    """No credentials to persist here — the music root itself is configured
    via the setup wizard's own dedicated step (POST /api/setup/music-root),
    not this per-provider hook. Kept for interface parity with the other
    clients; nothing currently calls this since there's no other admin-
    editable config for this provider."""
    return status()


def _iter_playlist_files(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if Path(fname).suffix.lower() in _PLAYLIST_EXTENSIONS:
                yield Path(dirpath) / fname


def _itunes_library_path() -> Path | None:
    conn = db.get_conn()
    try:
        raw = db.get_config(conn, "itunes_library_path")
    finally:
        conn.close()
    return Path(raw) if raw else None


def list_playlists() -> dict:
    """Returns {"status": "ok", "playlists": [{"id", "title"}, ...]} — the
    common shape every provider client returns (#75). The title is the
    file's path relative to the music root, extension stripped — which is
    already unique per file, so it doubles as the stable `id` here (the two
    are identical for this provider). Nested playlists show their folder
    for context.

    When an iTunes Library.xml path is configured (#171), its user-created
    playlists are appended, each `id` namespaced with `_ITUNES_ID_PREFIX`."""
    root = db.get_music_root()
    if not root.is_dir():
        return {"status": "error", "reason": "not_paired"}
    titles = [str(f.relative_to(root).with_suffix("")) for f in _iter_playlist_files(root)]
    playlists = [{"id": t, "title": t} for t in titles]

    xml_path = _itunes_library_path()
    if xml_path is not None:
        playlists += [
            {"id": _ITUNES_ID_PREFIX + p["id"], "title": p["title"]}
            for p in itunes_library.parse_library(xml_path, root)
        ]
    return {"status": "ok", "playlists": playlists}


def _find_playlist_file(root: Path, title: str) -> Path | None:
    for ext in _PLAYLIST_EXTENSIONS:
        candidate = root / f"{title}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _parse_m3u(path: Path) -> list[dict]:
    """Tolerant EXTM3U parse: an "#EXTINF:duration,Artist - Title" line
    supplies artist/title for the very next path line when present, but many
    tools export bare path-per-line files with no EXTINF at all — handled
    the same way, just with artist/title left for the caller to derive from
    the filename instead."""
    encoding = "utf-8-sig" if path.suffix.lower() == ".m3u8" else "utf-8"
    try:
        text = path.read_text(encoding=encoding, errors="replace")
    except OSError:
        return []

    entries = []
    pending_artist = pending_title = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith("#EXTINF:"):
            _, _, label = line.partition(",")
            if " - " in label:
                pending_artist, _, pending_title = label.partition(" - ")
            else:
                pending_artist, pending_title = None, (label or None)
            continue
        if line.startswith("#"):
            continue
        entries.append({"path": line, "artist": pending_artist, "title": pending_title})
        pending_artist = pending_title = None
    return entries


def _resolve_entry_path(root: Path, playlist_path: Path, raw: str) -> str:
    """Absolute entries are passed through as-is — matching.py's segment
    comparison tolerates whatever prefix the authoring tool used. Relative
    entries are resolved against the playlist file's own folder and, when
    that stays under the music root (the overwhelmingly common case),
    converted to the same root-relative form `tracks.relative_path` already
    uses — an exact match rather than just a trailing-segment one."""
    raw = raw.replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return raw
    resolved = os.path.normpath(str(playlist_path.parent / raw))
    try:
        return str(Path(resolved).relative_to(root))
    except ValueError:
        return resolved


def _itunes_playlist_by_id(root: Path, playlist_title: str, target_id: str) -> dict:
    xml_path = _itunes_library_path()
    if xml_path is None:
        return {"status": "error", "reason": "not_paired"}
    match = next((p for p in itunes_library.parse_library(xml_path, root) if p["id"] == target_id), None)
    if match is None:
        return {"status": "not_found", "failed_segment": playlist_title}
    return {"status": "ok", "playlist": playlist_title, "tracks": match["tracks"]}


def _itunes_playlist_by_title(root: Path, playlist_title: str) -> dict | None:
    xml_path = _itunes_library_path()
    if xml_path is None:
        return None
    match = next((p for p in itunes_library.parse_library(xml_path, root) if p["title"] == playlist_title), None)
    if match is None:
        return None
    return {"status": "ok", "playlist": playlist_title, "tracks": match["tracks"]}


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist", "path", "album"} — "album" is always None for an .m3u-sourced
    playlist (no such field in that format; #171's iTunes-sourced ones do
    carry it), matching playlist_sync.py's already-optional handling of it
    for the other providers. `source_playlist_id` (== the title for an
    .m3u-sourced playlist, see list_playlists) is accepted for call-shape
    uniformity and preferred when given; the title is the file path either
    way.

    An id carrying `_ITUNES_ID_PREFIX` (#171) routes to the configured
    Library.xml instead — checked first since it's an unambiguous marker
    when present, so the more common .m3u lookup below never has to parse
    the (potentially large) XML file to rule it out."""
    root = db.get_music_root()

    if source_playlist_id is not None and source_playlist_id.startswith(_ITUNES_ID_PREFIX):
        return _itunes_playlist_by_id(root, playlist_title, source_playlist_id[len(_ITUNES_ID_PREFIX):])

    path = _find_playlist_file(root, source_playlist_id or playlist_title)
    if path is not None:
        tracks = []
        for i, entry in enumerate(_parse_m3u(path)):
            resolved_path = _resolve_entry_path(root, path, entry["path"])
            tracks.append({
                "position": i,
                "title": entry["title"] or Path(entry["path"]).stem,
                "artist": entry["artist"] or "",
                "path": resolved_path,
                "album": None,
            })
        return {"status": "ok", "playlist": playlist_title, "tracks": tracks}

    if source_playlist_id is None:
        found = _itunes_playlist_by_title(root, playlist_title)
        if found is not None:
            return found

    return {"status": "not_found", "failed_segment": playlist_title}


def get_artist_image(artist_name: str) -> tuple[bytes, str] | None:
    """Returns (bytes, content_type) or None if not configured / not found —
    a miss here just means no picture, never a sync failure (same contract as
    the other providers' get_artist_image). Looks up the artist's actual
    top-level folder via the tracks table (rather than assuming the tag value
    matches the folder name byte-for-byte) so a library where they differ
    still resolves correctly."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT relative_path FROM tracks WHERE deleted_at IS NULL AND artist = ? LIMIT 1",
            (artist_name,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    parts = Path(row["relative_path"]).parts
    if len(parts) < 2:
        return None  # flat layout, no artist-level folder to look in
    artist_dir = db.get_music_root() / parts[0]
    if not artist_dir.is_dir():
        return None

    by_lower_name = {p.name.lower(): p for p in artist_dir.iterdir() if p.is_file()}
    for basename in _ARTIST_IMAGE_BASENAMES:
        for ext, mime in _ARTIST_IMAGE_EXTENSIONS.items():
            candidate = by_lower_name.get(f"{basename}{ext}")
            if candidate is not None:
                return candidate.read_bytes(), mime
    return None
