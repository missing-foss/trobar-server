#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Artist images — unlike covers.py's album art, there's no "artist photo"
audio tag, so the active provider's own image-fetching API is the primary
source. filesystem_client is always tried as a fallback when the active
provider misses — a folder/cover/poster/artist/thumb image sitting
directly in the artist's own folder (the real convention Jaikoz/Kodi/Jellyfin
all converge on) can supply a picture the active service's own API doesn't
have. Provider round-trips aren't free, so fetched images are cached to disk
indefinitely (no TTL/refresh) — a cache hit never touches the provider (or
the filesystem fallback) at all, and once an image is cached it keeps working
even if the provider is later offline or unreachable. Cache is cleared on
provider switch (see main.py's admin config handler) since a cached image is
tied to whichever provider (or the filesystem fallback) supplied it, not to
the artist name alone.

A miss everywhere is remembered for a day (MISS_TTL_S), as a .none marker
beside the cache, like covers.py's: without it a long artist list asks every
source again, for every artist without a picture, on every view. A day, not
forever as for covers, because these sources are outside services: an
artist with no picture today may get one when a provider is next reachable.
Clearing the cache clears the markers with it."""

import hashlib
import io
import json
import time
from pathlib import Path
import logging

from PIL import Image

import audiodb_client
import db
import filesystem_client

_log = logging.getLogger(__name__)

CACHE_DIR = db.DATA_DIR / "artist_images"
MISS_TTL_S = 24 * 60 * 60


def _cache_path(artist: str) -> Path:
    digest = hashlib.sha256(artist.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.img"


def get_artist_image(artist: str, provider,
                     audiodb_api_key: str | None = None) -> tuple[bytes, str] | None:
    """Returns (bytes, content_type), from disk cache if present, else
    fetched and cached. None if unavailable everywhere. Source order
: TheAudioDB first when the admin configured a key (the
    cleanly-licensed source), then the active provider, then the
    filesystem fallback. `provider` is the caller-supplied
    active provider module — see main.py's _active_provider(). A .src
    sidecar records where each cached image came from (license hygiene —
    required for attribution-carrying sources, useful for all)."""
    cache_path = _cache_path(artist)
    meta_path = cache_path.with_suffix(".type")
    if cache_path.exists() and meta_path.exists():
        return cache_path.read_bytes(), meta_path.read_text(encoding="utf-8")
    miss_path = cache_path.with_suffix(".none")
    try:
        if time.time() - miss_path.stat().st_mtime < MISS_TTL_S:
            return None
    except FileNotFoundError:
        pass

    fetched = None
    source: dict = {}
    if audiodb_api_key:
        found = audiodb_client.get_artist_image(artist, audiodb_api_key)
        if found is not None:
            data, content_type, image_url = found
            fetched = (data, content_type)
            source = {"source": "theaudiodb", "url": image_url}
    if fetched is None:
        fetched = provider.get_artist_image(artist)
        source = {"source": getattr(provider, "__name__", "provider")}
    if fetched is None and provider is not filesystem_client:
        fetched = filesystem_client.get_artist_image(artist)
        source = {"source": "filesystem"}
    if fetched is None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        miss_path.touch()  # a fresh mtime when an expired marker is renewed
        return None
    data, content_type = fetched
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    miss_path.unlink(missing_ok=True)
    cache_path.write_bytes(data)
    meta_path.write_text(content_type, encoding="utf-8")
    cache_path.with_suffix(".src").write_text(
        json.dumps(source), encoding="utf-8")
    return data, content_type


SMALL_MAX_PX = 512


def downscale(data: bytes, content_type: str, max_px: int = SMALL_MAX_PX) -> tuple[bytes, str]:
    """the `small` device variant: longest side capped at `max_px`
    (SMALL_MAX_PX unless asked), re-encoded JPEG. Never upscales; on any
    decode problem the original passes through untouched (a picture is
    decorative, never worth failing a request over)."""
    try:
        # Image.Image, not the narrower ImageFile.open() returns — .convert()
        # below produces a plain Image, not still an ImageFile.
        img: Image.Image = Image.open(io.BytesIO(data))
        if max(img.size) <= max_px:
            return data, content_type
        img.thumbnail((max_px, max_px))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, content_type
