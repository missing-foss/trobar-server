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
covr) the mutagen version needed.

When a track has no embedded picture, an image beside it serves instead
(folder_cover): the names other players write and read, such as cover.jpg
and folder.jpg."""

import hashlib
import logging
import re
import shutil
from pathlib import Path

from tinytag import TinyTag

import artist_images
import db

_log = logging.getLogger(__name__)

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


# The names an album's picture goes by beside its files, in the order they are
# preferred, matched without regard to case.
_FOLDER_IMAGE_BASENAMES = ("cover", "folder", "front", "album")
_FOLDER_IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
}
# A disc folder inside an album (CD1, Disc 2): its album's picture is usually
# one level up.
_DISC_FOLDER = re.compile(r"^(cd|disc|disk)\s*\d+$", re.IGNORECASE)


def folder_cover(source_path: Path) -> tuple[bytes, str] | None:
    """An album picture beside `source_path`: in its folder, then in the
    album folder above it when that folder is a disc folder. The music root
    itself is never searched: in a flat library one picture there would
    become every album's cover."""
    try:
        root = db.get_music_root().resolve()
    except Exception:
        root = None
    folders = [source_path.parent]
    if _DISC_FOLDER.match(source_path.parent.name):
        folders.append(source_path.parent.parent)
    for folder in folders:
        try:
            if root is not None and folder.resolve() == root:
                continue
            by_lower_name = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
        except OSError:
            continue
        for basename in _FOLDER_IMAGE_BASENAMES:
            for ext, mime in _FOLDER_IMAGE_EXTENSIONS.items():
                candidate = by_lower_name.get(basename + ext)
                if candidate is None:
                    continue
                try:
                    data = candidate.read_bytes()
                except OSError:
                    continue
                if data:
                    return data, mime
    return None


def _cache_base(artist: str, album: str) -> Path:
    digest = hashlib.sha256(f"{artist}{_KEY_SEP}{album}".encode("utf-8")).hexdigest()
    return CACHE_DIR / digest


def get_cover(artist: str, album: str, source_path: Path) -> tuple[bytes, str] | None:
    """Disk-cached album cover. Serves from cache when present (no NFS read);
    otherwise extracts from `source_path` (a track in the album), else takes a
    picture beside it (folder_cover), and caches the result — including a
    negative marker when the album has neither, so coverless albums don't
    re-hit the filesystem on every browse either. Returns None (→ 404) when
    there's no cover."""
    base = _cache_base(artist, album)
    img_path = base.with_suffix(".img")
    type_path = base.with_suffix(".type")
    none_path = base.with_suffix(".none")

    if img_path.exists() and type_path.exists():
        return img_path.read_bytes(), type_path.read_text(encoding="utf-8")
    if none_path.exists():
        return None

    extracted = extract_cover(source_path) or folder_cover(source_path)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if extracted is None:
        # Negative-cache only when the source really exists (an album with no
        # art, embedded or beside it), never when the file is missing — a transient absence
        # shouldn't poison the cache; a rescan re-reads regardless.
        if source_path.exists():
            none_path.touch()
        return None
    data, content_type = extracted
    img_path.write_bytes(data)
    type_path.write_text(content_type, encoding="utf-8")
    return data, content_type


def get_small_cover(artist: str, album: str, source_path: Path) -> tuple[bytes, str] | None:
    """The cover at most artist_images.SMALL_MAX_PX on its longer side, for
    the page's small thumbnails: a full-size cover is often several times
    the size of a 40 px slot. Made from get_cover's result and cached beside
    it, so a hit reads neither the source nor the full cover."""
    base = _cache_base(artist, album)
    small_img, small_type = base.with_suffix(".simg"), base.with_suffix(".stype")
    if small_img.exists() and small_type.exists():
        return small_img.read_bytes(), small_type.read_text(encoding="utf-8")
    cover = get_cover(artist, album, source_path)
    if cover is None:
        return None
    data, content_type = artist_images.downscale(*cover)
    small_img.write_bytes(data)
    small_type.write_text(content_type, encoding="utf-8")
    return data, content_type


def invalidate(artist: str, album: str) -> None:
    """Drop one album's cached cover (art was added/changed/removed)."""
    base = _cache_base(artist, album)
    for suffix in (".img", ".type", ".none", ".simg", ".stype"):
        base.with_suffix(suffix).unlink(missing_ok=True)


def clear_all() -> None:
    """Wipe the whole cover cache — used by a full (forced) rescan."""
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
