#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#285: playlist mirroring MVP — the buildable first slice of #189's
cross-provider mirroring RFC. Writes a Trobar-managed `.m3u` file into a
separately-configured folder (never MUSIC_ROOT itself — Trobar never
writes to the library, and filesystem_client.py's own `.m3u` discovery
walks the whole of MUSIC_ROOT with no exclusion mechanism, so a mirror
written there would be picked back up as a new source playlist on the
next sync) for any playlist with `mirror_enabled=1`.

Scenario A only: one-way, golden-wins, full idempotent rewrite (never a
delta). Managed-copy identity is a distinctive visible filename suffix
plus a hidden machine-readable marker line — the marker (M3U_MARKER,
reused from sync_state.py's own device-`.m3u` convention, same purpose)
is what makes "never clobber a user's own playlist file" safe: every
write and delete here is gated on that single check.
"""

import logging
import os
from pathlib import Path

from werkzeug.utils import secure_filename

import db
import sync_state

_log = logging.getLogger(__name__)

# ASCII, already werkzeug.secure_filename()-stable (verified directly —
# secure_filename() leaves "..._Trobar_.m3u" byte-for-byte unchanged for
# any normal title) so a well-formed playlist name round-trips through
# _compute_filename() below without losing its distinctive marker to
# sanitization. The angle-bracket " ⟨Trobar⟩" form from the original
# design doesn't survive secure_filename() (it strips non-ASCII and
# collapses spaces/brackets to '_'), which is why this is plainer.
MIRROR_SUFFIX = "_Trobar_"


def _safe_path(folder: Path, filename: str) -> Path | None:
    """Joins `folder` and `filename`, then verifies the result is
    genuinely a DIRECT CHILD of `folder` — not merely some descendant.
    `filename` always comes from _compute_filename()'s return value
    (already passed through werkzeug.secure_filename(), which strips
    every directory-separator character — see MIRROR_SUFFIX's own
    comment), so this can't actually contain '/' today, but this helper
    doesn't assume that about its argument: a plain `startswith(base +
    sep)` containment check (tried first, see the CodeQL note below)
    would silently accept 'sub/dir/x.m3u' as long as it resolved
    somewhere under `folder`, which is a real regression surface if a
    future caller ever passes this something less strict than
    secure_filename()'s output. Checking that the joined path's PARENT is
    exactly `folder` closes that gap outright, structurally, rather than
    relying on every caller to keep sanitizing upstream.

    Deliberately os.path (normpath/join), not pathlib resolve()+
    relative_to() — this is the exact shape from CodeQL's own
    py/path-injection query-help "Recommendation" example. Two earlier
    attempts (a pathlib resolve()+relative_to() equivalent, then
    os.path.basename() alone) were both re-checked against the real
    SARIF taint flow after pushing and neither actually cleared the
    alert; this shape plus secure_filename() upstream is what did.

    `base_str` is normpath()'d too (#294): `folder` comes straight from
    the admin-configured `mirror_folder` (str(Path(...)), never resolved),
    so an admin-typed path containing `..` (e.g. `/srv/../srv/mirror`)
    would otherwise never equal `full_str`'s own normalized dirname and
    every write would fail. Not resolve() — that shape didn't clear
    CodeQL, and staying unresolved is also what keeps a Docker
    bind-mounted (symlinked) mirror folder working.
    Returns None (never touch anything) if containment fails."""
    base_str = os.path.normpath(str(folder))
    full_str = os.path.normpath(os.path.join(base_str, filename))
    if os.path.dirname(full_str) != base_str:
        return None
    return Path(full_str)


def _is_marker_safe(path: Path) -> bool:
    """True if `path` doesn't exist, or its second line is M3U_MARKER —
    the single check gating every write AND delete this module performs.
    A read/decode failure is treated as NOT safe (conservative: refuse to
    touch a file we can't positively identify as our own)."""
    if not path.exists():
        return True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.readline()  # #EXTM3U
            return f.readline().rstrip("\n") == sync_state.M3U_MARKER
    except OSError:
        return False


def _compute_filename(conn, playlist_id: int, title: str) -> str:
    """Same collision-disambiguation-by-id trick as
    sync_state._device_playlists() — append the id if another playlist's
    title sanitizes to the same name. Reaches into sync_state's own
    filename-sanitization helper (fs_segment) for a first, human-readable
    pass, then werkzeug.secure_filename() — CodeQL's py/path-injection
    query specifically recognizes this one (confirmed empirically: neither
    a pathlib resolve()+relative_to() check nor a raw os.path.normpath()+
    startswith() check, both tried first, actually registered when
    re-checked against the real SARIF taint flow). secure_filename() is
    applied BEFORE the collision check below, not after, so the comparison
    is against the same representation that's actually stored in
    mirror_filename from a previous write.

    #294: secure_filename() NFKD-normalizes then drops every non-ASCII
    codepoint, so a title in a script with no Latin characters at all
    (CJK, Cyrillic, Greek, Arabic, ...) sanitizes to nothing — every such
    playlist would otherwise collide onto the same bare-suffix filename.
    Falling back to the playlist's own id (already globally unique) keeps
    the mirror distinguishable in a file manager instead."""
    segment = sync_state.fs_segment(title)
    if not secure_filename(segment):
        segment = f"playlist-{playlist_id}"
    base = secure_filename(f"{segment}{MIRROR_SUFFIX}.m3u")
    clash = conn.execute(
        "SELECT 1 FROM playlists WHERE id != ? AND mirror_filename = ?",
        (playlist_id, base),
    ).fetchone()
    if clash is None:
        return base
    return secure_filename(f"{segment}{MIRROR_SUFFIX} ({playlist_id}).m3u")


def _set_error(conn, playlist_id: int, code: str, detail: str | None = None) -> None:
    """#428: `code` is one of the five failure modes below, always
    English-language-independent (a client renders it via i18n). `detail`
    is everything that can't itself be translated -- an OS exception's
    text (arrives in the C library's locale, not the user's) or a
    computed/conflicting filename -- appended to the translated prefix
    client-side, not baked into an English sentence here. unset_folder is
    the one code with no detail: it's fully translatable on its own."""
    conn.execute(
        "UPDATE playlists SET mirror_last_error_code = ?, mirror_last_error = ? WHERE id = ?",
        (code, detail, playlist_id),
    )


def delete_mirror(conn, playlist_id: int) -> None:
    """Marker-checked delete of this playlist's stored mirror_filename, if
    any. Called when mirror_enabled flips to 0, and from
    playlist_sync.py's stale-playlist cleanup (BEFORE the playlist row
    itself is deleted, since this needs mirror_filename first) so a
    removed golden source doesn't leave an orphaned mirror file behind.
    Does not commit — same convention as sync_state.record_unresolved_
    playlist_tracks, the caller's own trailing commit covers it."""
    row = conn.execute(
        "SELECT mirror_filename FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None or not row["mirror_filename"]:
        return
    folder = db.get_mirror_folder()
    if folder is None:
        return
    path = _safe_path(folder, row["mirror_filename"])
    if path is None:
        _log.warning("refusing to delete %r — resolves outside the mirror folder",
                     row["mirror_filename"])
    elif _is_marker_safe(path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _log.warning("failed to delete mirror file %s", path, exc_info=True)
    else:
        _log.warning("refusing to delete non-Trobar-marked file %s", path)
    conn.execute(
        "UPDATE playlists SET mirror_filename = NULL, mirror_last_written_at = NULL "
        "WHERE id = ?", (playlist_id,),
    )


def write_mirror(conn, playlist_id: int) -> None:
    """Idempotent full rewrite of this playlist's mirror file. No-ops
    (touches nothing) if the playlist isn't mirror_enabled or no
    mirror_folder is configured. Never raises — every failure mode is
    instead persisted to mirror_last_error for the admin overview. Does
    not commit — same convention as sync_state.record_unresolved_
    playlist_tracks, the caller's own trailing commit covers it (this
    runs inline in playlist_sync.py's per-playlist commit and must not
    itself abort or fragment that transaction)."""
    row = conn.execute(
        "SELECT title, mirror_enabled, mirror_filename FROM playlists WHERE id = ?",
        (playlist_id,),
    ).fetchone()
    if row is None or not row["mirror_enabled"]:
        return

    folder = db.get_mirror_folder()
    if folder is None:
        _set_error(conn, playlist_id, "unset_folder")
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _set_error(conn, playlist_id, "not_writable", str(exc))
        return

    new_filename = _compute_filename(conn, playlist_id, row["title"])
    old_filename = row["mirror_filename"]
    if old_filename and old_filename != new_filename:
        old_path = _safe_path(folder, old_filename)
        if old_path is not None:
            if _is_marker_safe(old_path):
                old_path.unlink(missing_ok=True)
            else:
                _log.warning("refusing to delete non-Trobar-marked file %s", old_path)

    target = _safe_path(folder, new_filename)
    if target is None:
        _set_error(conn, playlist_id, "bad_filename", new_filename)
        return
    if not _is_marker_safe(target):
        _set_error(conn, playlist_id, "marker_unsafe", new_filename)
        return

    music_root = db.get_music_root()
    entries = conn.execute(
        "SELECT t.artist, t.title, t.duration, t.relative_path "
        "FROM playlist_tracks pt JOIN tracks t ON t.id = pt.matched_track_id "
        "WHERE pt.playlist_id = ? AND t.deleted_at IS NULL ORDER BY pt.position",
        (playlist_id,),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()[0]

    lines = [
        "#EXTM3U",
        sync_state.M3U_MARKER,
        f"#PLAYLIST:{row['title']}",
        f"# Trobar mirror — {len(entries)} of {total} present, grows with your library",
    ]
    for e in entries:
        duration = int(e["duration"]) if e["duration"] else -1
        lines.append(f"#EXTINF:{duration},{e['artist']} - {e['title']}")
        lines.append(str(music_root / e["relative_path"]))

    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        _set_error(conn, playlist_id, "write_failed", str(exc))
        return

    conn.execute(
        "UPDATE playlists SET mirror_filename = ?, mirror_last_written_at = datetime('now'), "
        "mirror_last_error = NULL, mirror_last_error_code = NULL WHERE id = ?",
        (new_filename, playlist_id),
    )
