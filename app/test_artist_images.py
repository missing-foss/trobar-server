#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for artist_images.py (#390) — the source-precedence chain, the disk
cache, and the `small` downscale.

Same reasoning as test_covers.py (#525): this module pairs an indefinite disk
cache with a broad `except Exception` around image decoding, so a regression
here shows up as a missing or mangled picture rather than as an error. The
precedence rule (TheAudioDB when a key is configured → active provider →
filesystem fallback) is licensing-relevant, not just cosmetic — TheAudioDB is
the cleanly-licensed source and is meant to win — so each step is asserted
both for what it returns AND for what it records in the .src sidecar.

Cache assertions are written as "the provider is never consulted again"
rather than "a file exists", since serving without a provider round-trip is
the entire point of the cache.

    python3 -m unittest test_artist_images -v
"""
import io
import json
import os
import shutil
import tempfile
import types
import unittest
import unittest.mock as mock
from pathlib import Path

from PIL import Image

_TMP = tempfile.mkdtemp(prefix="trobar-test-artist-images-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import artist_images  # noqa: E402


def _png(size=(64, 64), mode="RGB", colour=(255, 0, 0)) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, colour if mode != "L" else 128).save(out, format="PNG")
    return out.getvalue()


def _fake_provider(name="fake_provider", result=None):
    """A stand-in for main.py's _active_provider() return value, which is a
    real module — so this is a real module too. It matters: artist_images
    records `provider.__name__` in the .src sidecar, and a Mock would hand
    back an auto-created attribute that json.dumps cannot serialise."""
    module = types.ModuleType(name)
    module.get_artist_image = mock.Mock(return_value=result)  # type: ignore[attr-defined]
    return module


class _ArtistImagesTestCase(unittest.TestCase):
    """Per-test cache dir. CACHE_DIR is computed once at import from
    db.DATA_DIR, so it is repointed here rather than depending on import
    order, and so cached state cannot leak between tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trobar-test-artist-images-case-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = artist_images.CACHE_DIR
        artist_images.CACHE_DIR = self.tmp / "artist_images"
        self.addCleanup(self._restore)

    def _restore(self):
        artist_images.CACHE_DIR = self._orig

    def _src_sidecar(self, artist="Portishead"):
        path = artist_images._cache_path(artist).with_suffix(".src")
        return json.loads(path.read_text(encoding="utf-8"))


class SourcePrecedenceTests(_ArtistImagesTestCase):

    def test_theaudiodb_wins_when_a_key_is_configured(self):
        provider = _fake_provider(result=(b"provider-bytes", "image/png"))
        with mock.patch.object(artist_images.audiodb_client, "get_artist_image",
                               return_value=(b"adb-bytes", "image/jpeg",
                                             "https://example.invalid/a.jpg")) as adb:
            result = artist_images.get_artist_image("Portishead", provider, "KEY")
        self.assertEqual(result, (b"adb-bytes", "image/jpeg"))
        adb.assert_called_once_with("Portishead", "KEY")
        # The licensing-relevant half: the provider must not even be asked.
        provider.get_artist_image.assert_not_called()
        self.assertEqual(self._src_sidecar(),
                         {"source": "theaudiodb", "url": "https://example.invalid/a.jpg"})

    def test_theaudiodb_is_not_consulted_without_a_key(self):
        provider = _fake_provider(result=(b"provider-bytes", "image/png"))
        with mock.patch.object(artist_images.audiodb_client,
                               "get_artist_image") as adb:
            result = artist_images.get_artist_image("Portishead", provider, None)
        adb.assert_not_called()
        self.assertEqual(result, (b"provider-bytes", "image/png"))

    def test_falls_through_to_the_provider_when_theaudiodb_misses(self):
        provider = _fake_provider(name="jellyfin_client",
                                  result=(b"provider-bytes", "image/png"))
        with mock.patch.object(artist_images.audiodb_client, "get_artist_image",
                               return_value=None):
            result = artist_images.get_artist_image("Portishead", provider, "KEY")
        self.assertEqual(result, (b"provider-bytes", "image/png"))
        self.assertEqual(self._src_sidecar(), {"source": "jellyfin_client"})

    def test_falls_through_to_the_filesystem_when_the_provider_misses(self):
        provider = _fake_provider(result=None)
        with mock.patch.object(artist_images.filesystem_client, "get_artist_image",
                               return_value=(b"fs-bytes", "image/jpeg")) as fs:
            result = artist_images.get_artist_image("Portishead", provider, None)
        self.assertEqual(result, (b"fs-bytes", "image/jpeg"))
        fs.assert_called_once_with("Portishead")
        self.assertEqual(self._src_sidecar(), {"source": "filesystem"})

    def test_does_not_ask_the_filesystem_twice_when_it_is_the_active_provider(self):
        # filesystem_client as the ACTIVE provider already had its turn; the
        # fallback is explicitly skipped so it isn't queried twice for the
        # same miss.
        with mock.patch.object(artist_images.filesystem_client, "get_artist_image",
                               return_value=None) as fs:
            result = artist_images.get_artist_image(
                "Portishead", artist_images.filesystem_client, None)
        self.assertIsNone(result)
        self.assertEqual(fs.call_count, 1)

    def test_returns_none_and_caches_nothing_when_every_source_misses(self):
        provider = _fake_provider(result=None)
        with mock.patch.object(artist_images.filesystem_client, "get_artist_image",
                               return_value=None):
            result = artist_images.get_artist_image("Nobody", provider, None)
        self.assertIsNone(result)
        # No negative caching here (unlike covers.py) — an artist with no
        # picture today may get one when the provider is next reachable.
        self.assertFalse(artist_images.CACHE_DIR.exists())


class CacheTests(_ArtistImagesTestCase):

    def test_caches_the_image_its_content_type_and_a_source_sidecar(self):
        provider = _fake_provider(name="plex_client",
                                  result=(b"provider-bytes", "image/png"))
        artist_images.get_artist_image("Portishead", provider, None)
        base = artist_images._cache_path("Portishead")
        self.assertEqual(base.read_bytes(), b"provider-bytes")
        self.assertEqual(base.with_suffix(".type").read_text(encoding="utf-8"),
                         "image/png")
        self.assertEqual(self._src_sidecar(), {"source": "plex_client"})

    def test_a_cache_hit_never_touches_the_provider(self):
        provider = _fake_provider(result=(b"provider-bytes", "image/png"))
        artist_images.get_artist_image("Portishead", provider, None)
        provider.get_artist_image.reset_mock()
        result = artist_images.get_artist_image("Portishead", provider, None)
        self.assertEqual(result, (b"provider-bytes", "image/png"))
        provider.get_artist_image.assert_not_called()

    def test_a_cache_hit_never_touches_theaudiodb_either(self):
        # The cache is what keeps a rate-limited third-party key from being
        # spent on every browse.
        provider = _fake_provider(result=None)
        with mock.patch.object(artist_images.audiodb_client, "get_artist_image",
                               return_value=(b"adb", "image/jpeg", "u")) as adb:
            artist_images.get_artist_image("Portishead", provider, "KEY")
            adb.reset_mock()
            result = artist_images.get_artist_image("Portishead", provider, "KEY")
        self.assertEqual(result, (b"adb", "image/jpeg"))
        adb.assert_not_called()

    def test_different_artists_get_different_cache_entries(self):
        first = _fake_provider(result=(b"one", "image/png"))
        second = _fake_provider(result=(b"two", "image/jpeg"))
        artist_images.get_artist_image("Artist One", first, None)
        artist_images.get_artist_image("Artist Two", second, None)
        self.assertEqual(artist_images.get_artist_image("Artist One", first, None),
                         (b"one", "image/png"))
        self.assertEqual(artist_images.get_artist_image("Artist Two", second, None),
                         (b"two", "image/jpeg"))

    def test_a_half_written_cache_entry_is_refetched(self):
        # Both the image and its .type sidecar must be present for a hit —
        # a crash between the two writes must not serve a typeless image.
        provider = _fake_provider(result=(b"provider-bytes", "image/png"))
        artist_images.get_artist_image("Portishead", provider, None)
        artist_images._cache_path("Portishead").with_suffix(".type").unlink()
        provider.get_artist_image.reset_mock()
        result = artist_images.get_artist_image("Portishead", provider, None)
        self.assertEqual(result, (b"provider-bytes", "image/png"))
        provider.get_artist_image.assert_called_once()


class DownscaleTests(unittest.TestCase):
    """The `small` device variant. A picture is decorative, so every failure
    path here returns the original rather than raising."""

    def test_downscales_a_large_image_and_re_encodes_it_as_jpeg(self):
        data, content_type = artist_images.downscale(_png(size=(1400, 900)), "image/png")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(max(Image.open(io.BytesIO(data)).size),
                         artist_images.SMALL_MAX_PX)

    def test_preserves_aspect_ratio(self):
        data, _ = artist_images.downscale(_png(size=(1000, 500)), "image/png")
        width, height = Image.open(io.BytesIO(data)).size
        self.assertEqual((width, height), (512, 256))

    def test_never_upscales_a_small_image(self):
        original = _png(size=(100, 100))
        data, content_type = artist_images.downscale(original, "image/png")
        # Returned byte-identical, not re-encoded: a 100px picture must not
        # be blown up to 512 or silently converted to JPEG.
        self.assertEqual(data, original)
        self.assertEqual(content_type, "image/png")

    def test_an_image_exactly_at_the_cap_passes_through_untouched(self):
        original = _png(size=(artist_images.SMALL_MAX_PX, artist_images.SMALL_MAX_PX))
        data, content_type = artist_images.downscale(original, "image/png")
        self.assertEqual(data, original)
        self.assertEqual(content_type, "image/png")

    def test_converts_a_transparent_image_before_writing_jpeg(self):
        # JPEG cannot hold an alpha channel; without the convert() this
        # raises inside save() and the whole downscale silently degrades to
        # returning the oversized original.
        data, content_type = artist_images.downscale(
            _png(size=(1200, 1200), mode="RGBA", colour=(255, 0, 0, 128)), "image/png")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(data)).mode, "RGB")
        self.assertEqual(max(Image.open(io.BytesIO(data)).size),
                         artist_images.SMALL_MAX_PX)

    def test_converts_a_palette_image_before_writing_jpeg(self):
        data, content_type = artist_images.downscale(
            _png(size=(1200, 1200), mode="P"), "image/png")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(data)).mode, "RGB")

    def test_keeps_greyscale_as_is_rather_than_converting(self):
        # "L" is in the allowed set — JPEG stores greyscale natively, so
        # converting to RGB would triple the bytes for no gain.
        data, content_type = artist_images.downscale(
            _png(size=(1200, 1200), mode="L"), "image/png")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(data)).mode, "L")

    def test_undecodable_bytes_pass_through_untouched(self):
        data, content_type = artist_images.downscale(b"not an image", "image/png")
        self.assertEqual(data, b"not an image")
        self.assertEqual(content_type, "image/png")

    def test_empty_bytes_pass_through_untouched(self):
        data, content_type = artist_images.downscale(b"", "image/jpeg")
        self.assertEqual(data, b"")
        self.assertEqual(content_type, "image/jpeg")


if __name__ == "__main__":
    unittest.main()
