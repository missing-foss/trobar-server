#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playlist mirroring's Plex sink, beside mirror.py (filesystem),
mirror_subsonic.py, mirror_jellyfin.py, mirror_emby.py and
mirror_music_assistant.py. Same contract: one-way, golden-wins, a full
idempotent rewrite of every playlist with `plex_mirror_enabled = 1`.

The target is its own connection (db.get_mirror_plex_config), and a Plex
playlist belongs to the account of the token used, so that is where the
copies land. Only a playlist this module created is ever written to or
deleted: its key is stored as `plex_mirror_remote_id`, and its name carries
mirror.MIRROR_SUFFIX, so the read side (plex_client.list_playlists) never
takes it for a source.

What Plex does, measured live (see plex_client's mirror section), and how
this module follows it:
- A key Plex doesn't have is dropped with a 200, so every write is
  confirmed by reading the copy back; a mismatch is `readback_mismatch`,
  never a clean write.
- A playlist holds a track at most once, so a Trobar playlist that repeats
  a track is mirrored with its first occurrence only.
- There is no replace: a rewrite clears the list, then adds the new one.
  When the copy already holds the new list, under the right name and
  summary, nothing is written, so a rerun changes nothing.
- A Trobar track is found in Plex by the last folders and the file name of
  its path (the music roots differ), else by artist, album and title.
  Tracks Plex lacks are left out, as the other sinks do.
- A copy that can't be read is only given up on when Plex says it is gone
  (a 404, or its key now names something else); a passing error leaves the
  stored key alone, so a copy is never orphaned and duplicated.
"""

import logging

import db
import matching
import plex_client

_log = logging.getLogger(__name__)


def _set_error(conn, playlist_id: int, code: str, detail: str | None = None) -> None:
    conn.execute(
        "UPDATE playlists SET plex_mirror_last_error = ?, plex_mirror_last_error_code = ? WHERE id = ?",
        (detail, code, playlist_id),
    )


def _get_index(index_cache: dict | None):
    """The target's track index, built once per sync run: `index_cache` is
    a dict the caller owns and passes to every write_mirror() of one run, as
    for the other server sinks. None (a one-off write) builds a fresh one. A
    failed build is cached too, so one unreachable verdict serves the run."""
    if index_cache is None:
        return plex_client.mirror_build_index()
    if "index" not in index_cache:
        index_cache["index"] = plex_client.mirror_build_index()
    return index_cache["index"]


def _pick(candidates: list[dict], track_no: int | None) -> dict:
    """One of several target tracks for the same key (Plex holds a track
    twice, a FLAC and an MP3 copy): the track number decides, then the
    lowest key, so a rerun picks the same one."""
    if track_no is not None:
        for c in candidates:
            if c["track_no"] == track_no:
                return c
    return min(candidates, key=lambda c: int(c["key"]) if c["key"].isdigit() else c["key"])


def _resolve(index: dict, track) -> str | None:
    by_path = index["by_tail"].get(plex_client.tail_key(track["relative_path"] or ""))
    if by_path:
        return _pick(by_path, track["track_no"])["key"]
    key = (matching.normalize(track["artist"] or ""), matching.normalize(track["album"] or ""),
           matching.normalize(track["title"] or ""))
    tagged = index["by_tags"].get(key)
    return _pick(tagged, track["track_no"])["key"] if tagged else None


def _mismatch(expected: list[str], written: list[str] | None) -> str:
    """What the read-back shows, for plex_mirror_last_error."""
    if written is None:
        return "the playlist could not be read back"
    missing = len(set(expected) - set(written))
    if missing:
        return f"{missing} of {len(expected)} tracks missing after the write"
    if len(written) != len(expected):
        return f"{len(written)} entries where {len(expected)} were written"
    return "the tracks are there, in a different order"


def delete_mirror(conn, playlist_id: int) -> None:
    """Deletes this playlist's Plex copy, if it has one. Called when
    mirroring is switched off and from playlist_sync's removal path, before
    the row goes. The stored key is cleared either way, as the other sinks
    do: a lost copy is the lesser problem next to a row that can never
    write a fresh one. Does not commit."""
    row = conn.execute(
        "SELECT plex_mirror_remote_id, plex_mirror_machine FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None or not row["plex_mirror_remote_id"]:
        return
    if row["plex_mirror_machine"] != plex_client.mirror_machine():
        # The copy is on another server than the target now configured (or
        # the target doesn't answer): the key can't be trusted there.
        _log.warning("not deleting the Plex mirror %s of playlist %s: not on the current target",
                     row["plex_mirror_remote_id"], playlist_id)
    elif not plex_client.mirror_delete(row["plex_mirror_remote_id"]):
        _log.warning("failed to delete the Plex mirror %s of playlist %s",
                     row["plex_mirror_remote_id"], playlist_id)
    conn.execute(
        "UPDATE playlists SET plex_mirror_remote_id = NULL, plex_mirror_machine = NULL, "
        "plex_mirror_last_written_at = NULL WHERE id = ?", (playlist_id,),
    )


def write_mirror(conn, playlist_id: int, index_cache: dict | None = None) -> None:
    """Idempotent full rewrite of this playlist's Plex copy. Does nothing
    unless the playlist has mirroring to Plex on. Never raises: every
    failure is stored in plex_mirror_last_error (code and detail), as the
    other sinks do. Does not commit."""
    row = conn.execute(
        "SELECT title, plex_mirror_enabled, plex_mirror_remote_id, plex_mirror_machine "
        "FROM playlists WHERE id = ?", (playlist_id,),
    ).fetchone()
    if row is None or not row["plex_mirror_enabled"]:
        return
    if db.get_mirror_plex_config() is None:
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
    total = conn.execute(
        "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()[0]
    keys: list[str] = []
    for track in tracks:
        found = _resolve(index, track)
        if found is not None and found not in keys:
            keys.append(found)
    # Something matched locally but nothing on the target: a target pointed
    # at another library, not a partial mirror. Reported, not written.
    if tracks and not keys:
        _set_error(conn, playlist_id, "no_target_matches")
        return

    name = plex_client.mirror_name(row["title"])
    summary = f"Trobar mirror — {len(keys)} of {total} present, grows with your library"
    remote_id = row["plex_mirror_remote_id"]
    if remote_id and row["plex_mirror_machine"] != index["machine"]:
        # The copy is on another server (the mirror target was changed):
        # the key means nothing here, and might name someone else's
        # playlist, so it is never used. A fresh copy is made.
        conn.execute("UPDATE playlists SET plex_mirror_remote_id = NULL, plex_mirror_machine = NULL "
                     "WHERE id = ?", (playlist_id,))
        remote_id = None
    current = plex_client.mirror_read(remote_id) if remote_id else None
    if remote_id and current is None:
        exists = plex_client.mirror_exists(remote_id)
        if exists is None:
            _set_error(conn, playlist_id, "unreachable")
            return
        if exists:
            _set_error(conn, playlist_id, "write_failed", "the copy could not be read")
            return
        # Gone on the target: write a fresh copy.
        conn.execute("UPDATE playlists SET plex_mirror_remote_id = NULL, plex_mirror_machine = NULL "
                     "WHERE id = ?", (playlist_id,))
        remote_id = None

    if remote_id is None:
        remote_id = plex_client.mirror_create(row["title"], index["machine"], keys)
        if remote_id is None:
            _set_error(conn, playlist_id, "write_failed", "the playlist could not be created")
            return
        conn.execute("UPDATE playlists SET plex_mirror_remote_id = ?, plex_mirror_machine = ? WHERE id = ?",
                     (remote_id, index["machine"], playlist_id))
        current = {"title": name, "summary": "", "keys": None}
    assert current is not None

    if current["keys"] != keys:
        if current["keys"] is not None and not plex_client.mirror_replace(remote_id, index["machine"], keys):
            _set_error(conn, playlist_id, "write_failed", "the playlist's tracks could not be replaced")
            return
        written = plex_client.mirror_read(remote_id)
        if written is None or written["keys"] != keys:
            _set_error(conn, playlist_id, "readback_mismatch",
                       _mismatch(keys, written["keys"] if written else None))
            return
    if (current["title"], current["summary"]) != (name, summary) \
            and not plex_client.mirror_set_metadata(remote_id, row["title"], summary):
        _set_error(conn, playlist_id, "write_failed", "the playlist could not be renamed")
        return

    conn.execute(
        "UPDATE playlists SET plex_mirror_last_written_at = datetime('now'), "
        "plex_mirror_last_error = NULL, plex_mirror_last_error_code = NULL WHERE id = ?",
        (playlist_id,),
    )
