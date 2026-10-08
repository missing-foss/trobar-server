#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#285: playlist mirroring MVP — the buildable first slice of #189's
cross-provider mirroring RFC. Writes a Trobar-managed `.m3u` file into a
separately-configured folder (never MUSIC_ROOT itself: Trobar never
writes to the library) for any playlist with `mirror_enabled=1`. The
folder may still be visible inside MUSIC_ROOT through another mount of the
same share; filesystem_client.py's `.m3u` discovery skips files carrying
the marker below, so a mirror is never read back as a source playlist.

Scenario A only: one-way, golden-wins, full idempotent rewrite (never a
delta). Managed-copy identity is a distinctive visible filename suffix
plus a hidden machine-readable marker line — the marker (M3U_MARKER,
reused from sync_state.py's own device-`.m3u` convention, same purpose)
is what makes "never clobber a user's own playlist file" safe: every
write and delete here is gated on that single check.

Per-user layout: each playlist goes to <mirror folder>/<owner's folder>/
music/, beside an empty mixed/ (user_folder_name(); SHARED_FOLDER for
unowned playlists), so one Kodi profile can read one user's playlists by
pointing its Playlists folder (`system.playlistspath`) at that user's
folder. With the mirror folder's place in the music share configured
(db.get_mirror_share_location()), entries are relative to the file
(entry_path()). Verified on Kodi 21.2 over SMB, with this writer's own
output: two profiles, each pointed at its user's folder, each list only
their own playlist under Music -> Playlists and play its track through the
relative entry. Kodi needs both music/ and mixed/ ("share not available"
otherwise) and creates neither. The setting is internal in Kodi (level 4,
absent from its settings screens), set per profile in guisettings.xml or
over JSON-RPC: docs/providers/kodi.md.
"""

import logging
import os
import tempfile
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


def _compute_filename(conn, playlist_id: int, title: str, subfolder: str = "") -> str:
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
        (playlist_id, subfolder + base),
    ).fetchone()
    if clash is None:
        return base
    return secure_filename(f"{segment}{MIRROR_SUFFIX} ({playlist_id}).m3u")


# Kodi reads a playlists folder's music/ and mixed/ subfolders, and shows
# "share not available" if either is missing; it doesn't create them.
_KODI_SUBFOLDERS = ("music", "mixed")


def _stored_path(folder: Path, stored: str) -> Path | None:
    """The file a stored mirror_filename names: "<user>/music/<file>" since
    the per-user layout, a bare "<file>" for a mirror written before it.
    Each segment is joined through _safe_path(), so every step must be a
    direct child of the one before; None if any isn't."""
    path: Path | None = folder
    for segment in stored.split("/"):
        path = _safe_path(path, segment) if path is not None else None
    return path


# Unowned (provider-wide, shared) playlists go here, beside the per-user
# folders. The leading underscore keeps it out of what secure_filename()
# can derive from a username: it strips leading underscores.
SHARED_FOLDER = "_shared"


def user_folder_name(conn, user_id: int | None) -> str:
    """The folder, under the mirror root, for playlists owned by `user_id`
    (SHARED_FOLDER for None). Derived from the username once, through
    secure_filename() as titles are, and stored on the user, so later
    writes reuse it unchanged. A username that sanitizes to nothing, or
    to a name another user already holds, gets its id instead or added.
    Does not commit."""
    if user_id is None:
        return SHARED_FOLDER
    row = conn.execute("SELECT username, mirror_folder_name FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return SHARED_FOLDER
    if row["mirror_folder_name"]:
        return row["mirror_folder_name"]
    name = secure_filename(row["username"] or "") or f"user-{user_id}"
    taken = conn.execute("SELECT 1 FROM users WHERE id != ? AND lower(mirror_folder_name) = lower(?)",
                         (user_id, name)).fetchone()
    if taken is not None:
        name = f"{name}-{user_id}"
    conn.execute("UPDATE users SET mirror_folder_name = ? WHERE id = ?", (name, user_id))
    return name


def entry_path(music_root: Path, relative_path: str, location: str | None) -> str:
    """One m3u entry for a track. Without a location: the absolute path
    under MUSIC_ROOT, as always. With one (the mirror root's place inside
    the music share, e.g. "UserPlaylists"): a path relative to the playlist
    file at <root>/<user>/music/, so it climbs the location's own depth
    plus those two folders. Kodi resolves such an entry against the
    playlist file's folder, on any machine that mounts the share."""
    if not location:
        return str(music_root / relative_path)
    depth = len(Path(location).parts) + 2
    return "../" * depth + Path(relative_path).as_posix()


def seen_in_library(mirror_folder: Path, music_root: Path, location: str) -> bool:
    """True if `mirror_folder` and MUSIC_ROOT/`location` are the same folder,
    reached through two mounts of one share. Checked with a file rather
    than by comparing paths or inodes, which differ between mount types: a
    uniquely named probe is written through the mirror mount and looked
    for under the library, then removed. False when it can't be written."""
    try:
        mirror_folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=mirror_folder, prefix=".trobar-probe-") as probe:
            return (music_root / location / Path(probe.name).name).is_file()
    except OSError:
        return False


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
    path = _stored_path(folder, row["mirror_filename"])
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
        "SELECT title, owner_user_id, mirror_enabled, mirror_filename FROM playlists WHERE id = ?",
        (playlist_id,),
    ).fetchone()
    if row is None or not row["mirror_enabled"]:
        return

    folder = db.get_mirror_folder()
    if folder is None:
        _set_error(conn, playlist_id, "unset_folder")
        return
    # The owner's folder, holding Kodi's two subfolders; unowned playlists
    # go to SHARED_FOLDER.
    user_dir = _safe_path(folder, user_folder_name(conn, row["owner_user_id"]))
    music_dir = _safe_path(user_dir, "music") if user_dir is not None else None
    if user_dir is None or music_dir is None:
        _set_error(conn, playlist_id, "bad_filename", str(user_dir))
        return
    try:
        for sub in _KODI_SUBFOLDERS:
            (user_dir / sub).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _set_error(conn, playlist_id, "not_writable", str(exc))
        return

    prefix = f"{user_dir.name}/music/"
    file_name = _compute_filename(conn, playlist_id, row["title"], prefix)
    new_filename = prefix + file_name
    old_filename = row["mirror_filename"]
    if old_filename and old_filename != new_filename:
        # A rename, or a flat file from before the per-user layout.
        old_path = _stored_path(folder, old_filename)
        if old_path is not None:
            if _is_marker_safe(old_path):
                old_path.unlink(missing_ok=True)
            else:
                _log.warning("refusing to delete non-Trobar-marked file %s", old_path)

    target = _safe_path(music_dir, file_name)
    if target is None:
        _set_error(conn, playlist_id, "bad_filename", file_name)
        return
    if not _is_marker_safe(target):
        _set_error(conn, playlist_id, "marker_unsafe", new_filename)
        return

    music_root = db.get_music_root()
    location = db.get_mirror_share_location()
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
        lines.append(entry_path(music_root, e["relative_path"], location))

    content = "\n".join(lines) + "\n"
    try:
        # A rerun with nothing changed leaves the file alone, modification
        # time included, so a player watching the folder has nothing to
        # re-read; mirror_last_written_at keeps naming the last real write.
        if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == content:
            conn.execute(
                "UPDATE playlists SET mirror_filename = ?, mirror_last_error = NULL, "
                "mirror_last_error_code = NULL WHERE id = ?",
                (new_filename, playlist_id),
            )
            return
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        _set_error(conn, playlist_id, "write_failed", str(exc))
        return

    conn.execute(
        "UPDATE playlists SET mirror_filename = ?, mirror_last_written_at = datetime('now'), "
        "mirror_last_error = NULL, mirror_last_error_code = NULL WHERE id = ?",
        (new_filename, playlist_id),
    )
