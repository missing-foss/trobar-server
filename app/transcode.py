# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Server-side MP3 transcode, used by GET /api/device/file/<id>.

The ffmpeg flags below mirror trobar-desktop's local transcoder
(lib/transcoder.dart) exactly, so a track transcoded here is equivalent to
one a desktop client would have produced: same codec/bitrate, cover art
copied not re-encoded, ID3v2.3 (not the ffmpeg default v2.4) for old-DAP
hardware compatibility. Keep the two in sync if that preset ever changes.

Output goes to a real temp file, not stdout/pipe:1 (trobar-server#223).
Confirmed directly (byte-level, via ffprobe) that piping mp3 output with
an ID3v2 tag to a non-seekable destination corrupts the tag: ffmpeg writes
the tag's syncsafe size field as a placeholder and normally seeks back to
patch in the real size once all frames (including the APIC cover-art
frame) are written — it can't do that seek on a pipe, so the size field
is left at 0. Every reader (ffprobe, the Garmin watch's own metadata
parser, etc.) sees a header claiming a zero-length tag and treats
everything after it as raw audio, silently discarding both the cover art
and every text tag even though ffmpeg exits 0 and the frame bytes are
technically still present in the file, just unreachable. Writing to a
seekable file and only reading it back once ffmpeg has exited (so the
patched-up header is already on disk) fixes this — at the cost of no
longer being able to start sending bytes to the client until the whole
track has finished transcoding.

Concurrency and CPU priority are admin-configurable (Administration >
Transcoding, app_config keys transcode_concurrency/transcode_nice_level)
rather than hardcoded — transcoding is CPU-heavy and must never crowd out
everything else running alongside this container. This is resource
hygiene, not crash prevention: a request blocks until a slot frees up
rather than being rejected.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading

_log = logging.getLogger(__name__)

BITRATES = {"mp3_320": "320k", "mp3_256": "256k", "mp3_192": "192k", "mp3_128": "128k"}

_cond = threading.Condition()
_active = 0


class TranscodeStartError(Exception):
    """ffmpeg (or nice) could not even be started."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def acquire_slot(limit: int) -> None:
    """Blocks until fewer than `limit` transcodes are running. `limit` is
    read fresh by the caller on every call (not fixed at process start),
    so an admin's change to the concurrency setting takes effect on the
    next request — no restart, no resizing a fixed-size Semaphore."""
    global _active
    with _cond:
        while _active >= max(1, limit):
            _cond.wait()
        _active += 1


def release_slot() -> None:
    global _active
    with _cond:
        _active -= 1
        _cond.notify()


def start(abs_path, transcode_format: str, nice_level: int) -> tuple[subprocess.Popen, str, str]:
    """Starts ffmpeg transcoding abs_path to transcode_format into a fresh
    temp file (see module docstring for why not stdout/pipe:1) and returns
    (proc, out_path, err_path). stderr is captured to its own temp file,
    not a pipe: a review of an earlier version of this fix caught that a
    PIPE nobody reads is a deadlock waiting to happen — ffmpeg blocks
    writing to a full ~64KB pipe buffer, iter_output()'s proc.wait() never
    returns, the concurrency slot is never released, and with the default
    transcode_concurrency=1 that one hang wedges every future transcode. A
    file can't fill up like that, and unlike the old discard-it-unread
    PIPE, it's actually readable afterwards for diagnosing a failure (see
    iter_output). Caller must already hold a concurrency slot
    (acquire_slot) and is responsible for releasing it, and for removing
    out_path/err_path, once the process is fully drained or killed (see
    iter_output — it does all of this)."""
    fd, out_path = tempfile.mkstemp(suffix=".mp3", prefix="trobar-transcode-")
    os.close(fd)
    err_fd, err_path = tempfile.mkstemp(suffix=".log", prefix="trobar-transcode-")
    cmd = [
        "nice", "-n", str(nice_level),
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(abs_path),
        # First audio stream + the cover art if there is one. Explicit
        # maps: a plain `-map 0` would abort on any exotic extra stream.
        "-map", "0:a:0", "-map", "0:v:0?",
        "-c:a", "libmp3lame", "-b:a", BITRATES[transcode_format],
        "-c:v", "copy",
        "-map_metadata", "0",
        "-id3v2_version", "3",
        "-f", "mp3", out_path,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err_fd)
    except OSError as e:
        os.remove(out_path)
        os.remove(err_path)
        raise TranscodeStartError(str(e)) from e
    finally:
        os.close(err_fd)
    return proc, out_path, err_path


def iter_output(proc: subprocess.Popen, out_path: str, err_path: str):
    """Waits for ffmpeg to finish writing out_path, then streams the
    finished file back in chunks (a Flask Response generator). Nothing
    can be safely read before ffmpeg exits — the ID3v2 tag on disk is
    only correct once ffmpeg has seeked back and patched its size field
    in, right at the end (see module docstring). One consequence: unlike
    the old stdout-pipe version, a client disconnecting *during* the wait
    has no way to interrupt it (there's no yield point to receive that
    signal until ffmpeg is already done) — the transcode just runs to
    completion on an abandoned connection instead of being killed early.
    Rare and not a correctness/deadlock risk (the slot still frees itself
    right after), just a minor CPU-efficiency cost, accepted as part of
    this fix. Always cleans up: kills the process if it's somehow still
    running when we're done (belt-and-suspenders — proc.wait() above
    means it never should be), removes both temp files, and releases the
    concurrency slot."""
    try:
        proc.wait()
        if proc.returncode == 0:
            with open(out_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        else:
            stderr = _read_stderr(err_path)
            _log.warning("transcode of %s failed (exit %d): %s", out_path, proc.returncode, stderr)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for path in (out_path, err_path):
            try:
                os.remove(path)
            except OSError:
                pass
        release_slot()


def _read_stderr(path: str) -> str:
    """Best-effort read of ffmpeg's captured stderr — the caller removes
    the file right after regardless (see iter_output's finally), this just
    folds its content into a single log line first."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return "(could not read stderr log)"
