#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for covers.py (#525) — embedded album-art extraction and its on-disk
cache, neither of which had any coverage before this file.

Why it matters that these are real files, not mocks: `extract_cover` wraps its
whole tinytag call in `except Exception: return None`. That is the right
runtime behaviour (an unreadable file must not break a browse) but it means a
tinytag regression cannot surface as an error — it surfaces as album art
quietly not appearing. Mocking tinytag here would test the mock and leave the
real failure mode uncovered, so every fixture below is a genuine MP3 with
hand-built ID3v2.3 frames, matching test_scanner.py's deliberate
no-mutagen approach.

Every branch of extract_cover turns out to be reachable with a real file —
including the two odd ones, which are built rather than mocked:

  - an APIC frame declaring an EMPTY mime string makes tinytag report
    `mime_type is None`, which is what exercises the `or "image/jpeg"` default;
  - an APIC frame carrying ZERO bytes of picture data yields an image object
    with `data == b""`, which is what exercises the `not cover.data` guard.

    python3 -m unittest test_covers -v      # from app/
"""
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="trobar-test-covers-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import covers  # noqa: E402

# A 1x1 red PNG — small enough to inline, real enough that the bytes coming
# back out can be compared to the bytes that went in.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
    "7753de0000000c4944415408d763f8cf00000301010018dd8db00000000049454e44ae426082"
)

# One silent MPEG-1 Layer III frame, repeated — same constant and same reason
# as test_scanner.py's: just enough real audio that tinytag will parse the
# file at all. Only the ID3 frames prepended to it matter here.
_MP3_SILENCE_FRAME = bytes.fromhex(
    "fffb900400000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000"
)


def _syncsafe(n: int) -> bytes:
    return bytes([(n >> (7 * i)) & 0x7f for i in (3, 2, 1, 0)])


def _frame(frame_id: bytes, payload: bytes) -> bytes:
    """An ID3v2.3 frame. Note the size is a plain big-endian uint32 here —
    v2.3 only made the *tag* header size syncsafe, not the frame sizes (that
    changed in v2.4). Getting this wrong makes tinytag skip the frame
    silently, which would look like "no cover" rather than a broken fixture."""
    return frame_id + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def _text_frame(frame_id: bytes, text: str) -> bytes:
    return _frame(frame_id, b"\x00" + text.encode("latin-1"))  # 0x00 = ISO-8859-1


def _apic_frame(data: bytes = _PNG, mime: str = "image/png",
                description: str = "", picture_type: int = 3) -> bytes:
    """An ID3v2.3 APIC (attached picture) frame. Layout is: encoding byte,
    null-terminated mime string, picture-type byte (3 = front cover),
    null-terminated description, then the raw picture bytes."""
    payload = (b"\x00"
               + mime.encode("latin-1") + b"\x00"
               + bytes([picture_type])
               + description.encode("latin-1") + b"\x00"
               + data)
    return _frame(b"APIC", payload)


def _make_mp3(path: Path, *frames: bytes) -> Path:
    """A tiny real MP3 carrying whatever ID3v2.3 frames are passed."""
    body = b"".join(frames)
    header = b"ID3" + bytes([3, 0]) + b"\x00" + _syncsafe(len(body))
    path.write_bytes(header + body + _MP3_SILENCE_FRAME * 5)
    return path


def _cover(result: tuple[bytes, str] | None) -> tuple[bytes, str]:
    """Narrow the Optional so mypy (check_untyped_defs is on) can see through
    to the tuple, and fail with a readable message rather than a bare
    TypeError when a call that should have found art returned None."""
    assert result is not None, "expected a cover, got None"
    return result


class _TempDirTestCase(unittest.TestCase):
    """Every test gets its own scratch dir AND its own cover cache. covers.py
    computes CACHE_DIR once at import from db.DATA_DIR, so it is repointed per
    test rather than relying on import order — that also keeps the cache tests
    from leaking state into each other."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trobar-test-covers-case-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_cache_dir = covers.CACHE_DIR
        covers.CACHE_DIR = self.tmp / "album_covers"
        self.addCleanup(self._restore_cache_dir)

    def _restore_cache_dir(self):
        covers.CACHE_DIR = self._orig_cache_dir


class ExtractCoverTests(_TempDirTestCase):
    """The tinytag-facing half: what comes back out of a real file."""

    def test_returns_the_embedded_picture_bytes_and_its_mime_type(self):
        path = _make_mp3(self.tmp / "art.mp3",
                         _text_frame(b"TPE1", "Test Artist"),
                         _text_frame(b"TALB", "Test Album"),
                         _apic_frame())
        data, content_type = _cover(covers.extract_cover(path))
        # Byte-for-byte, not just "some bytes" — a truncated or re-encoded
        # picture would still be truthy and would still pass a length check.
        self.assertEqual(data, _PNG)
        self.assertEqual(content_type, "image/png")

    def test_honours_the_declared_mime_type_rather_than_sniffing_content(self):
        # The picture really is a PNG; the frame claims JPEG. Whatever the
        # frame says is what gets served as the Content-Type, so this pins
        # that the declared value wins.
        path = _make_mp3(self.tmp / "claims-jpeg.mp3",
                         _apic_frame(mime="image/jpeg"))
        self.assertEqual(_cover(covers.extract_cover(path))[1], "image/jpeg")

    def test_returns_none_when_the_file_has_no_embedded_art(self):
        path = _make_mp3(self.tmp / "no-art.mp3",
                         _text_frame(b"TPE1", "Test Artist"),
                         _text_frame(b"TALB", "Test Album"))
        self.assertIsNone(covers.extract_cover(path))

    def test_returns_none_when_the_picture_frame_carries_no_data(self):
        # An APIC frame with a valid mime but zero picture bytes. tinytag
        # hands back an image object with data == b"", so this is the
        # `not cover.data` guard — without it, callers would get an empty
        # body with a 200 and a broken <img> instead of a clean 404.
        path = _make_mp3(self.tmp / "empty-art.mp3", _apic_frame(data=b""))
        self.assertIsNone(covers.extract_cover(path))

    def test_defaults_to_jpeg_when_the_picture_declares_no_mime_type(self):
        # An empty mime string in the frame makes tinytag report mime_type
        # as None, which is what the `or "image/jpeg"` fallback exists for.
        path = _make_mp3(self.tmp / "no-mime.mp3", _apic_frame(mime=""))
        data, content_type = _cover(covers.extract_cover(path))
        self.assertEqual(data, _PNG)
        self.assertEqual(content_type, "image/jpeg")

    def test_returns_none_when_the_file_is_missing(self):
        # The realistic production trigger for the `except`: an NFS path that
        # vanished between the DB row being read and the art being fetched.
        # tinytag raises FileNotFoundError; a browse must not 500 over it.
        self.assertIsNone(covers.extract_cover(self.tmp / "gone.mp3"))

    def test_returns_none_for_a_format_tinytag_does_not_support(self):
        path = self.tmp / "sleeve.txt"
        path.write_bytes(b"not audio")
        # tinytag raises UnsupportedFormatError here, not a parse error.
        self.assertIsNone(covers.extract_cover(path))

    def test_returns_none_for_a_truncated_file_rather_than_raising(self):
        path = self.tmp / "truncated.mp3"
        path.write_bytes(b"ID3" + bytes([3, 0]) + b"\x00")
        self.assertIsNone(covers.extract_cover(path))


class GetCoverCacheTests(_TempDirTestCase):
    """The caching half. The point of this cache is that a hit never touches
    the source file, so the assertions delete the source and re-ask."""

    def test_extracts_and_caches_on_the_first_call(self):
        source = _make_mp3(self.tmp / "track.mp3", _apic_frame())
        data, content_type = _cover(covers.get_cover("Artist", "Album", source))
        self.assertEqual(data, _PNG)
        self.assertEqual(content_type, "image/png")
        cached = list(covers.CACHE_DIR.iterdir())
        self.assertEqual(sorted(p.suffix for p in cached), [".img", ".type"])

    def test_a_cache_hit_never_reads_the_source_file(self):
        source = _make_mp3(self.tmp / "track.mp3", _apic_frame())
        covers.get_cover("Artist", "Album", source)
        source.unlink()  # if the second call touches the source, it cannot succeed
        data, content_type = _cover(covers.get_cover("Artist", "Album", source))
        self.assertEqual(data, _PNG)
        self.assertEqual(content_type, "image/png")

    def test_an_album_with_no_art_is_negative_cached(self):
        source = _make_mp3(self.tmp / "track.mp3", _text_frame(b"TPE1", "Test Artist"))
        self.assertIsNone(covers.get_cover("Artist", "Album", source))
        markers = [p.suffix for p in covers.CACHE_DIR.iterdir()]
        self.assertEqual(markers, [".none"])
        # And the marker is what answers the second call — proven by removing
        # the source, which would otherwise be re-read.
        source.unlink()
        self.assertIsNone(covers.get_cover("Artist", "Album", source))

    def test_a_missing_source_is_not_negative_cached(self):
        # A transient absence must not poison the cache: if it did, an album
        # whose files were briefly unreachable would stay coverless until the
        # next rescan even after they came back.
        missing = self.tmp / "not-here.mp3"
        self.assertIsNone(covers.get_cover("Artist", "Album", missing))
        self.assertFalse(covers.CACHE_DIR.exists()
                         and any(covers.CACHE_DIR.iterdir()))

    def test_art_reappears_once_a_previously_missing_source_exists(self):
        # The consequence of the rule above, stated as behaviour rather than
        # as an absent file.
        source = self.tmp / "track.mp3"
        self.assertIsNone(covers.get_cover("Artist", "Album", source))
        _make_mp3(source, _apic_frame())
        self.assertEqual(_cover(covers.get_cover("Artist", "Album", source))[0], _PNG)

    def test_albums_whose_keys_concatenate_alike_do_not_collide(self):
        # The ␟ delimiter exists so ("a", "bc") and ("ab", "c") are different
        # cache entries. Without it both would hash "abc" and the second album
        # would serve the first one's art.
        first = _make_mp3(self.tmp / "first.mp3", _apic_frame())
        second = _make_mp3(self.tmp / "second.mp3", _apic_frame(mime="image/jpeg"))
        self.assertEqual(_cover(covers.get_cover("a", "bc", first))[1], "image/png")
        self.assertEqual(_cover(covers.get_cover("ab", "c", second))[1], "image/jpeg")


class InvalidateTests(_TempDirTestCase):
    """Called by the scanner when an album's tracks change, so replaced art
    actually propagates instead of serving the old picture forever."""

    def test_invalidate_drops_a_cached_cover(self):
        source = _make_mp3(self.tmp / "track.mp3", _apic_frame())
        covers.get_cover("Artist", "Album", source)
        covers.invalidate("Artist", "Album")
        self.assertEqual(list(covers.CACHE_DIR.iterdir()), [])

    def test_invalidate_drops_a_negative_marker_too(self):
        # Art added to a previously coverless album must become visible; if
        # the .none marker survived, it never would.
        source = _make_mp3(self.tmp / "track.mp3", _text_frame(b"TPE1", "A"))
        covers.get_cover("Artist", "Album", source)
        covers.invalidate("Artist", "Album")
        self.assertEqual(list(covers.CACHE_DIR.iterdir()), [])
        _make_mp3(source, _apic_frame())
        self.assertEqual(_cover(covers.get_cover("Artist", "Album", source))[0], _PNG)

    def test_invalidate_leaves_other_albums_alone(self):
        source = _make_mp3(self.tmp / "track.mp3", _apic_frame())
        covers.get_cover("Artist", "Kept", source)
        covers.get_cover("Artist", "Dropped", source)
        covers.invalidate("Artist", "Dropped")
        source.unlink()
        self.assertEqual(_cover(covers.get_cover("Artist", "Kept", source))[0], _PNG)

    def test_invalidate_is_a_no_op_for_an_uncached_album(self):
        covers.invalidate("Never", "Cached")  # must not raise


class ClearAllTests(_TempDirTestCase):
    """Called by a forced full rescan."""

    def test_clear_all_wipes_every_cached_album(self):
        source = _make_mp3(self.tmp / "track.mp3", _apic_frame())
        covers.get_cover("Artist", "One", source)
        covers.get_cover("Artist", "Two", source)
        covers.clear_all()
        self.assertFalse(covers.CACHE_DIR.exists())

    def test_clear_all_is_a_no_op_when_nothing_was_cached(self):
        covers.clear_all()  # must not raise even though CACHE_DIR never existed
        self.assertFalse(covers.CACHE_DIR.exists())


if __name__ == "__main__":
    unittest.main()
