#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedded cover-art extraction — pulled from whatever audio file is on disk
(FLAC/MP3/M4A/OGG/…), no external API and no dependency on a provider being
paired (library metadata is filesystem-first by design, see scanner.py).

tinytag exposes one uniform image accessor across every format —
`.images.any` returns the first embedded picture with its mime type — which
replaces the three format-specific branches (FLAC pictures / ID3 APIC / MP4
covr) the mutagen version needed."""

import hashlib
import shutil
from pathlib import Path

from tinytag import TinyTag

import db

# On-disk cache of extracted album covers. Without it,
# /api/library/cover did a live NFS open + full tag/picture parse on every
# thumbnail, every browse, for every user — browsing a 20-album artist fired 20
# NFS reads each time. Mirrors artist_images.py's proven cache: keyed by a hash
# of artist‖album, with a content-type sidecar. A hit never touches the
# filesystem. Invalidated by the scanner when an album's tracks change or on a
# full rescan (see scanner.py), so replaced art still propagates.
CACHE_DIR = db.DATA_DIR / "album_covers"
# U+241F (SYMBOL FOR UNIT SEPARATOR) can't occur in artist/album text, so it's a
# safe key delimiter — "a"‖"bc" and "ab"‖"c" never collide.
_KEY_SEP = "␟"


def extract_cover(path: Path) -> tuple[bytes, str] | None:
    try:
        cover = TinyTag.get(path, image=True).images.any
    except Exception:
        return None
    if cover is None or not cover.data:
        return None
    return cover.data, cover.mime_type or "image/jpeg"


def _cache_base(artist: str, album: str) -> Path:
    digest = hashlib.sha256(f"{artist}{_KEY_SEP}{album}".encode("utf-8")).hexdigest()
    return CACHE_DIR / digest


def get_cover(artist: str, album: str, source_path: Path) -> tuple[bytes, str] | None:
    """Disk-cached album cover. Serves from cache when present (no NFS read);
    otherwise extracts from `source_path` (a track in the album) and caches the
    result — including a negative marker when the album has no embedded art, so
    coverless albums don't re-hit the filesystem on every browse either. Returns
    None (→ 404) when there's no cover."""
    base = _cache_base(artist, album)
    img_path = base.with_suffix(".img")
    type_path = base.with_suffix(".type")
    none_path = base.with_suffix(".none")

    if img_path.exists() and type_path.exists():
        return img_path.read_bytes(), type_path.read_text(encoding="utf-8")
    if none_path.exists():
        return None

    extracted = extract_cover(source_path)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if extracted is None:
        # Negative-cache only when the source really exists (an album with no
        # embedded art), never when the file is missing — a transient absence
        # shouldn't poison the cache; a rescan re-reads regardless.
        if source_path.exists():
            none_path.touch()
        return None
    data, content_type = extracted
    img_path.write_bytes(data)
    type_path.write_text(content_type, encoding="utf-8")
    return data, content_type


def invalidate(artist: str, album: str) -> None:
    """Drop one album's cached cover (art was added/changed/removed)."""
    base = _cache_base(artist, album)
    for suffix in (".img", ".type", ".none"):
        base.with_suffix(suffix).unlink(missing_ok=True)


def clear_all() -> None:
    """Wipe the whole cover cache — used by a full (forced) rescan."""
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
