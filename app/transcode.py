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
everything else running alongside this container. A request waits for a
free slot for up to SLOT_WAIT_SECONDS and is then answered 503 with
Retry-After, and an encode that runs past ENCODE_TIMEOUT_SECONDS is
killed: neither a busy server nor a hung ffmpeg may hold a web thread or a
slot for ever.

Finished transcodes are kept on disk under DATA_DIR/transcode, one file per
track, source modification time and format, so a re-sync, a retry or a
second device with the same format is served the file instead of encoding
the same track again. The cache is capped (Administration > Transcoding,
app_config key transcode_cache_gb, DEFAULT_CACHE_GB unless set; 0 keeps
nothing), and the least recently served files go first. A file whose source
changes gets a new name, so a stale encode is never served; the old one
ages out like any other.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_log = logging.getLogger(__name__)

BITRATES = {"mp3_320": "320k", "mp3_256": "256k", "mp3_192": "192k", "mp3_128": "128k"}

_cond = threading.Condition()
_active = 0


DEFAULT_CACHE_GB = 2
_CACHE_DIR_NAME = "transcode"
_TEMP_PREFIX = "trobar-transcode-"
_evict_lock = threading.Lock()


class TranscodeStartError(Exception):
    """ffmpeg (or nice) could not even be started."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# How long a request waits for a free transcode slot before it is answered
# 503 with Retry-After, and how long one encode may run before it is killed.
# Each waiting request holds one of the server's few web threads, and one
# hung ffmpeg would otherwise hold its slot, and every device waiting on it,
# for ever. ffmpeg encodes MP3 many times faster than real time, so ten
# minutes is far beyond any track.
SLOT_WAIT_SECONDS = 60
ENCODE_TIMEOUT_SECONDS = 600


def acquire_slot(limit: int, timeout: float | None = None) -> bool:
    """Waits until fewer than `limit` transcodes are running and takes a
    slot; returns False, holding none, if `timeout` seconds pass first
    (None waits for ever). `limit` is read fresh by the caller on every call
    (not fixed at process start), so an admin's change to the concurrency
    setting takes effect on the next request — no restart, no resizing a
    fixed-size Semaphore."""
    global _active
    with _cond:
        if not _cond.wait_for(lambda: _active < max(1, limit), timeout=timeout):
            return False
        _active += 1
        return True


def release_slot() -> None:
    global _active
    with _cond:
        _active -= 1
        _cond.notify()


def cache_dir(data_dir) -> Path:
    return Path(data_dir) / _CACHE_DIR_NAME


def cache_path(data_dir, track_id: int, source_mtime_ns: int, transcode_format: str) -> Path:
    """Where this track's transcode in this format is kept. The source's
    modification time is part of the name, so a re-tagged or replaced file
    is encoded afresh instead of being served its old transcode."""
    return cache_dir(data_dir) / f"{track_id}-{source_mtime_ns}-{transcode_format}.mp3"


def cache_hit(path: Path) -> bool:
    """True when `path` holds a finished transcode. Marks it used now, which
    is what eviction orders by: in its access time, not its modification
    time, which send_file builds the ETag and Last-Modified from."""
    try:
        os.utime(path, ns=(time.time_ns(), path.stat().st_mtime_ns))
    except OSError:
        return False
    return True


def keep(out_path: str, err_path: str, final_path: Path, cap_bytes: int) -> bool:
    """Moves a finished transcode (wait_for() returned True) into the cache,
    then removes the stderr log, releases the slot and evicts down to
    `cap_bytes`. False, with everything left as it was, when the move fails:
    the caller then streams it with iter_file() as if there were no cache.
    out_path must be in final_path's directory (see start's out_dir), so the
    move is a rename and a reader never sees half a file."""
    try:
        os.replace(out_path, final_path)
    except OSError:
        _log.warning("could not keep the transcode %s", final_path, exc_info=True)
        return False
    _cleanup(err_path)
    evict(final_path.parent, cap_bytes, spare=final_path)
    return True


def evict(directory: Path, cap_bytes: int, spare: Path | None = None) -> None:
    """Removes the least recently used transcodes until the rest fit in
    `cap_bytes` (0 removes them all). `spare`, the file about to be served,
    is never removed, even when it alone is over the cap. Also removes
    encodes a crash left half written, once they are older than any encode
    may run; a newer one may still be in progress and is left alone."""
    with _evict_lock:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        kept = []
        stale_before = time.time() - 2 * ENCODE_TIMEOUT_SECONDS
        for entry in entries:
            try:
                stat = entry.stat()
            except OSError:
                continue
            if entry.name.startswith(_TEMP_PREFIX):
                if stat.st_mtime < stale_before:
                    _remove(entry.path)
            elif entry.name.endswith(".mp3") and (spare is None or entry.path != str(spare)):
                kept.append((stat.st_atime, stat.st_size, entry.path))
        total = sum(size for _atime, size, _path in kept)
        if spare is not None:
            try:
                total += spare.stat().st_size
            except OSError:
                pass
        for _atime, size, path in sorted(kept):
            if total <= cap_bytes:
                break
            _remove(path)
            total -= size


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def start(abs_path, transcode_format: str, nice_level: int,
          out_dir: Path | None = None) -> tuple[subprocess.Popen, str, str]:
    """Starts ffmpeg transcoding abs_path to transcode_format into a fresh
    temp file (see module docstring for why not stdout/pipe:1) and returns
    (proc, out_path, err_path). stderr is captured to its own temp file,
    not a pipe: a review of an earlier version of this fix caught that a
    PIPE nobody reads is a deadlock waiting to happen — ffmpeg blocks
    writing to a full ~64KB pipe buffer, wait_for()'s proc.wait() never
    returns, the concurrency slot is never released, and with the default
    transcode_concurrency=1 that one hang wedges every future transcode. A
    file can't fill up like that, and unlike the old discard-it-unread
    PIPE, it's actually readable afterwards for diagnosing a failure (see
    wait_for). Caller must already hold a concurrency slot
    (acquire_slot) and is responsible for releasing it, and for removing
    out_path/err_path, once the process is fully drained or killed (see
    wait_for and iter_file — they do all of this). With `out_dir`, the
    output is written there instead of the system temp directory, so keep()
    can move it into the cache with a rename."""
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
    fd, out_path = tempfile.mkstemp(suffix=".mp3", prefix=_TEMP_PREFIX, dir=out_dir)
    os.close(fd)
    err_fd, err_path = tempfile.mkstemp(suffix=".log", prefix=_TEMP_PREFIX)
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


def wait_for(proc: subprocess.Popen, out_path: str, err_path: str,
             timeout: float = ENCODE_TIMEOUT_SECONDS) -> bool:
    """Waits for ffmpeg to finish writing out_path. True when it exited 0:
    the caller then streams the file with iter_file(), which cleans up.
    False when it failed or ran past `timeout` (then it is killed): the
    reason is logged, both temp files are removed and the slot released, so
    the caller answers with an error before sending a byte. A failed encode
    used to be streamed as an empty 200, which every client took for a
    successful download."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        _log.warning("transcode to %s killed after %d s", out_path, timeout)
        _cleanup(out_path, err_path)
        return False
    if proc.returncode != 0:
        _log.warning("transcode of %s failed (exit %d): %s", out_path, proc.returncode, _read_stderr(err_path))
        _cleanup(out_path, err_path)
        return False
    return True


class iter_file:
    """Streams a finished transcode (after wait_for() returned True) in
    chunks, as a Flask Response body, then removes the temp files and
    releases the slot, however the response ends. A class rather than a
    generator: the WSGI server always calls close(), even when the client
    leaves before the first chunk, and an unstarted generator's finally
    would never run."""

    def __init__(self, out_path: str, err_path: str):
        self._out_path = out_path
        self._err_path = err_path
        self._closed = False
        try:
            self._file = open(out_path, "rb")
        except OSError:
            self.close()
            raise

    def __iter__(self):
        try:
            while True:
                chunk = self._file.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        file = getattr(self, "_file", None)
        if file is not None:
            file.close()
        _cleanup(self._out_path, self._err_path)


def _cleanup(*paths: str) -> None:
    for path in paths:
        _remove(path)
    release_slot()


def _read_stderr(path: str) -> str:
    """Best-effort read of ffmpeg's captured stderr — the caller removes
    the file right after regardless (see _cleanup), this just
    folds its content into a single log line first."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return "(could not read stderr log)"
