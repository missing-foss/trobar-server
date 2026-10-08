#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playlist mirroring's Music Assistant sink, beside mirror.py (filesystem),
mirror_subsonic.py, mirror_jellyfin.py and mirror_emby.py. Same contract:
one-way, golden-wins, a full idempotent rewrite of every playlist with
`music_assistant_mirror_enabled = 1`.

The target is Music Assistant's one connection (db.get_mirror_music_
assistant_config), whose playlists are server-wide. Only a playlist this
module created is ever written to or deleted: its id is stored as
`music_assistant_mirror_remote_id`, and its name carries mirror.MIRROR_SUFFIX
(music_assistant_client.mirror_name), so a playlist someone made in Music
Assistant is never touched, and the sink's copies are never read back in as
source playlists (music_assistant_client.is_mirror_copy).

What Music Assistant does, measured live (see music_assistant_client's
docstring) and how this module follows it:
- Adding tracks is a background task that reports success even when items
  were refused, so every write is confirmed by reading the playlist back and
  comparing it with what was meant. A mismatch is an error
  (`readback_mismatch`), never a clean write.
- A playlist holds a track at most once, so a Trobar playlist that repeats
  a track is mirrored with its first occurrence only, and the read-back is
  compared with that list.
- There is no one-shot replace: a rewrite removes every entry, then adds the
  new list. When the read-back already equals the new list, nothing is
  written, so a rerun changes nothing.
- A Trobar track is found in Music Assistant by its path (exact, then the
  same trailing folders at a different depth), else by artist, album and
  title. Tracks Music Assistant doesn't have are left out, as the other
  sinks do (golden, within the local library, within the target's).
- Music Assistant can't rename a playlist without an admin token, so a
  mirror keeps the name it was created with; a retitled playlist's copy
  keeps the old title until it is recreated (toggle mirroring off and on).
"""

import logging

import db
import matching
import music_assistant_client

_log = logging.getLogger(__name__)

# Trailing path segments compared when the two music folders sit at
# different depths: artist folder, album folder, file.
_TAIL_SEGMENTS = 3


def _set_error(conn, playlist_id: int, code: str, detail: str | None = None) -> None:
    conn.execute(
        "UPDATE playlists SET music_assistant_mirror_last_error = ?, "
        "music_assistant_mirror_last_error_code = ? WHERE id = ?",
        (detail, code, playlist_id),
    )


def _tail(path: str) -> tuple[str, ...]:
    return tuple(p.casefold() for p in path.replace("\\", "/").split("/") if p)[-_TAIL_SEGMENTS:]


def _get_index(index_cache: dict | None):
    """The target's track index (music_assistant_client.mirror_build_index),
    built once per sync run: `index_cache` is a dict the caller owns and
    passes to every write_mirror() of one run, as the other server sinks'
    tag-index caches are. None (a one-off write) builds a fresh one. A
    failed build is cached too, so one unreachable verdict serves the run."""
    if index_cache is None:
        index = music_assistant_client.mirror_build_index()
    else:
        if "index" not in index_cache:
            index_cache["index"] = music_assistant_client.mirror_build_index()
        index = index_cache["index"]
    if index is not None and "by_tail" not in index:
        by_tail: dict[tuple[str, ...], list[dict]] = {}
        for entry in index["by_path"].values():
            by_tail.setdefault(_tail(entry["path"]), []).append(entry)
        index["by_tail"] = by_tail
    return index


def _pick(candidates: list[dict], track_no: int | None) -> dict:
    """One of several target tracks for the same key (the target holds a
    track twice, e.g. a FLAC and an MP3 copy): the track number decides,
    then the lowest URI, so a rerun picks the same one."""
    if track_no is not None:
        for c in candidates:
            if c["track_no"] == track_no:
                return c
    return min(candidates, key=lambda c: c["uri"])


def _resolve(index: dict, track) -> dict | None:
    path = track["relative_path"] or ""
    exact = index["by_path"].get(path)
    if exact is not None:
        return exact
    same_tail = index["by_tail"].get(_tail(path))
    if same_tail:
        return _pick(same_tail, track["track_no"])
    key = (matching.normalize(track["artist"] or ""), matching.normalize(track["album"] or ""),
           matching.normalize(track["title"] or ""))
    tagged = index["by_tags"].get(key)
    if tagged:
        return _pick(tagged, track["track_no"])
    return None


def _mismatch(expected: list[str], written: list[str] | None) -> str:
    """What the read-back shows, for music_assistant_mirror_last_error."""
    if written is None:
        return "the playlist could not be read back"
    missing = len(set(expected) - set(written))
    if missing:
        return f"{missing} of {len(expected)} tracks missing after the write"
    if len(written) != len(expected):
        return f"{len(written)} entries where {len(expected)} were written"
    return "the tracks are there, in a different order"


def delete_mirror(conn, playlist_id: int) -> None:
    """Deletes this playlist's Music Assistant copy, if it has one. Called
    when mirroring is switched off and from playlist_sync's removal path,
    before the row goes. With a non-admin token Music Assistant refuses the
    delete, so the copy is emptied instead and stays as an empty playlist,
    still named as Trobar's. The stored id is cleared either way, as the
    other sinks do: a lost copy is the lesser problem next to a row that
    can never write a fresh one. Does not commit."""
    row = conn.execute(
        "SELECT music_assistant_mirror_remote_id FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None or not row["music_assistant_mirror_remote_id"]:
        return
    outcome = music_assistant_client.mirror_delete(row["music_assistant_mirror_remote_id"])
    if outcome != "deleted":
        _log.warning("Music Assistant mirror %s of playlist %s: %s, not deleted",
                     row["music_assistant_mirror_remote_id"], playlist_id, outcome)
    conn.execute(
        "UPDATE playlists SET music_assistant_mirror_remote_id = NULL, "
        "music_assistant_mirror_last_written_at = NULL WHERE id = ?", (playlist_id,),
    )


def write_mirror(conn, playlist_id: int, index_cache: dict | None = None) -> None:
    """Idempotent full rewrite of this playlist's Music Assistant copy. Does
    nothing unless the playlist has mirroring to Music Assistant on. Never
    raises: every failure is stored in music_assistant_mirror_last_error
    (code and detail) for the playlist row and the admin overview, as the
    other sinks do. Does not commit."""
    row = conn.execute(
        "SELECT title, music_assistant_mirror_enabled, music_assistant_mirror_remote_id "
        "FROM playlists WHERE id = ?", (playlist_id,),
    ).fetchone()
    if row is None or not row["music_assistant_mirror_enabled"]:
        return
    if db.get_mirror_music_assistant_config() is None:
        _set_error(conn, playlist_id, "unset_target")
        return
    index = _get_index(index_cache)
    if index is None:
        _set_error(conn, playlist_id, "unreachable")
        return

    tracks = conn.execute(
        "SELECT t.relative_path, t.artist, t.album, t.title, t.track_no FROM playlist_tracks pt "
        "JOIN tracks t ON t.id = pt.matched_track_id "
        "WHERE pt.playlist_id = ? AND t.deleted_at IS NULL ORDER BY pt.position",
        (playlist_id,),
    ).fetchall()
    wanted: list[dict] = []
    seen: set[str] = set()
    for track in tracks:
        found = _resolve(index, track)
        if found is not None and found["uri"] not in seen:
            seen.add(found["uri"])
            wanted.append(found)
    # Something matched locally but nothing on the target: a target pointed
    # at another library, not a partial mirror. Reported, not written.
    if tracks and not wanted:
        _set_error(conn, playlist_id, "no_target_matches")
        return

    remote_id = row["music_assistant_mirror_remote_id"]
    current = music_assistant_client.mirror_read(remote_id) if remote_id else None
    if remote_id and current is None:
        # The copy couldn't be read. A deleted playlist and a passing server
        # error both answer 500, so only a complete listing without it means
        # it is gone; otherwise keep the id, or the copy would be orphaned
        # (still named as Trobar's, so never read back in, and not
        # deletable with a user token) and a duplicate created.
        listed = music_assistant_client.mirror_listed(remote_id)
        if listed is None:
            _set_error(conn, playlist_id, "unreachable")
            return
        if listed:
            _set_error(conn, playlist_id, "write_failed", "the copy could not be read")
            return
        conn.execute("UPDATE playlists SET music_assistant_mirror_remote_id = NULL WHERE id = ?",
                     (playlist_id,))
        remote_id = None
    if remote_id is None:
        remote_id = music_assistant_client.mirror_create(row["title"])
        if remote_id is None:
            _set_error(conn, playlist_id, "write_failed", "create_playlist failed")
            return
        conn.execute("UPDATE playlists SET music_assistant_mirror_remote_id = ? WHERE id = ?",
                     (remote_id, playlist_id))
        current = []

    expected = [w["path"] for w in wanted]
    if current != expected:
        if not music_assistant_client.mirror_replace(remote_id, len(current or []), [w["uri"] for w in wanted]):
            _set_error(conn, playlist_id, "write_failed", "a playlist update task did not finish")
            return
        written = music_assistant_client.mirror_read(remote_id)
        if written != expected:
            _set_error(conn, playlist_id, "readback_mismatch", _mismatch(expected, written))
            return

    conn.execute(
        "UPDATE playlists SET music_assistant_mirror_last_written_at = datetime('now'), "
        "music_assistant_mirror_last_error = NULL, music_assistant_mirror_last_error_code = NULL "
        "WHERE id = ?", (playlist_id,),
    )
