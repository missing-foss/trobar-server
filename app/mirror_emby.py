#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#189: playlist mirroring's fourth and (per the RFC) final sink — an Emby
server, alongside app/mirror.py's filesystem sink, app/mirror_subsonic.py's
Subsonic sink, and app/mirror_jellyfin.py's Jellyfin sink. Same contract,
same "one-way, golden-wins, full idempotent rewrite" semantics, for any
playlist with `emby_mirror_enabled = 1`.

Unlike the filesystem sink, there's no "don't clobber a file we don't own"
concern to guard (no marker/safe-path machinery here): every write either
creates a fresh remote playlist (a title, no remote_id yet) or replaces one
by the remote id THIS module itself stored on a previous write
(`emby_mirror_remote_id`) — there's no ambient shared namespace a stray
write could land in by accident the way an unmarked filesystem path could.

The actual request mechanics (tag-based song lookup, the GET/DELETE/POST
dance a "replace" decomposes into, best-effort name+comment) live in
emby_client.py's mirror_*() functions, confirmed live against a real Emby
4.9.5 instance — including three real divergences from the Jellyfin sink
despite the shared API lineage (see emby_client.py's module docstring)."""

import logging

import db
import emby_client
import matching

_log = logging.getLogger(__name__)

# Emby's own HTTP status for "that item doesn't exist" (confirmed live via
# emby_client.mirror_create_or_replace_playlist()'s own up-front existence
# check — the replace call's OWN endpoint returns a bare 500 for a stale
# id, not a 404, which is why that check exists at all; see emby_client.py's
# docstring). A universal HTTP code, so no named export from emby_client is
# needed for this, same reasoning as mirror_jellyfin.py's own constant.
_ERROR_NOT_FOUND = 404


def _set_error(conn, playlist_id: int, code: str, detail: str | None = None) -> None:
    conn.execute(
        "UPDATE playlists SET emby_mirror_last_error = ?, "
        "emby_mirror_last_error_code = ? WHERE id = ?",
        (detail, code, playlist_id),
    )


def _get_tag_index(tag_index_cache: dict | None):
    """Builds the target's tag index, or returns the one already built this
    sync run. `tag_index_cache` is a plain dict the CALLER owns and passes
    to every write_mirror() call across one sync pass (playlist_sync.py) —
    without it, N mirrored playlists meant N full-library walks against the
    target per sync. A one-off caller (the playlist mirror-toggle route)
    passes None and gets a fresh build every time, which is correct there:
    there's only one playlist to write.

    The failed-build case (None) is cached too, keyed the same way — every
    playlist in this run shares one target-unreachable verdict instead of
    each independently re-discovering it."""
    if tag_index_cache is None:
        return emby_client.mirror_build_tag_index()
    if "index" not in tag_index_cache:
        tag_index_cache["index"] = emby_client.mirror_build_tag_index()
    return tag_index_cache["index"]


def _select_song_id(candidates: list[dict], track_no: int | None) -> str:
    """A tag key can legitimately map to more than one target song (the
    target library holds the same album twice, e.g. a FLAC and an MP3
    copy) — track_no is a tiebreaker here, not part of the index's own key
    (see mirror_build_tag_index()'s docstring for why: a hard track_no in
    the key would turn any track-number disagreement into a silent drop).
    Falls back to the lowest song id when track_no doesn't disambiguate
    either, so a repeat write against unchanged data picks the same one
    rather than flapping between candidates on list order."""
    if len(candidates) == 1:
        return candidates[0]["id"]
    if track_no is not None:
        for c in candidates:
            if c["track_no"] == track_no:
                return c["id"]
    return min(candidates, key=lambda c: c["id"])["id"]


def delete_mirror(conn, playlist_id: int) -> None:
    """Delete this playlist's remote mirror playlist, if any. Called when
    emby_mirror_enabled flips to 0, and from playlist_sync.py's
    stale-playlist cleanup (BEFORE the playlist row itself is deleted,
    since this needs emby_mirror_remote_id first). Does not commit — same
    convention as mirror.delete_mirror, the caller's own trailing commit
    covers it.

    Clears the stored remote_id regardless of whether the remote delete
    call itself succeeded (logging a warning on failure) — same choice
    mirror.delete_mirror makes for a filesystem unlink failure: losing
    track of a possibly-orphaned remote playlist is the lesser problem
    next to a stuck row that can never write a fresh one again."""
    row = conn.execute(
        "SELECT emby_mirror_remote_id FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None or not row["emby_mirror_remote_id"]:
        return
    if not emby_client.mirror_delete_playlist(row["emby_mirror_remote_id"]):
        _log.warning(
            "failed to delete remote Emby mirror playlist %s for playlist %s",
            row["emby_mirror_remote_id"], playlist_id,
        )
    conn.execute(
        "UPDATE playlists SET emby_mirror_remote_id = NULL, "
        "emby_mirror_last_written_at = NULL WHERE id = ?", (playlist_id,),
    )


def write_mirror(conn, playlist_id: int, tag_index_cache: dict | None = None) -> None:
    """Idempotent full rewrite of this playlist's Emby mirror. No-ops
    (touches nothing) if the playlist isn't emby_mirror_enabled or no
    mirror-target connection is configured. Never raises — every failure
    mode is instead persisted to emby_mirror_last_error for the admin
    overview, same contract as the other three sinks. Does not commit —
    runs inline in playlist_sync.py's per-playlist commit.

    `tag_index_cache`: see _get_tag_index()'s docstring — pass the same
    dict across every playlist in one sync run to build the target index
    only once."""
    row = conn.execute(
        "SELECT title, emby_mirror_enabled, emby_mirror_remote_id "
        "FROM playlists WHERE id = ?", (playlist_id,),
    ).fetchone()
    if row is None or not row["emby_mirror_enabled"]:
        return

    # Distinguish "nothing configured" from "configured but unreachable" —
    # mirror_build_tag_index() collapses both to None, so this check has
    # to happen up front here instead. Same split every other sink already
    # makes (unset_folder/unset_target vs a real failure).
    if db.get_mirror_emby_config() is None:
        _set_error(conn, playlist_id, "unset_target")
        return

    tag_index = _get_tag_index(tag_index_cache)
    if tag_index is None:
        _set_error(conn, playlist_id, "unreachable")
        return

    entries = conn.execute(
        "SELECT t.artist, t.album, t.title, t.track_no FROM playlist_tracks pt "
        "JOIN tracks t ON t.id = pt.matched_track_id "
        "WHERE pt.playlist_id = ? AND t.deleted_at IS NULL ORDER BY pt.position",
        (playlist_id,),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()[0]

    # Tracks not indexed on the target are dropped, not treated as an
    # error — this is the "golden ∩ local ∩ target-indexed" model #189
    # settled on: a target with a genuinely divergent library is out of
    # scope for v1, not a failure of this write.
    song_ids = []
    for e in entries:
        key = (matching.normalize(e["artist"]), matching.normalize(e["album"]),
               matching.normalize(e["title"]))
        candidates = tag_index.get(key)
        if candidates:
            song_ids.append(_select_song_id(candidates, e["track_no"]))

    # write_mirror() must not report a clean write over a playlist that
    # resolved NONE of its locally-matched tracks on the target — the
    # smoking-gun signal of a broken join key or a target pointed at the
    # wrong library/account, not a legitimate "golden ∩ local ∩
    # target-indexed" partial mirror (that model still allows SOME tracks
    # to drop, just not literally all of them when there was something to
    # match in the first place). `entries` empty (nothing matched locally
    # yet) is the ordinary case and still writes an empty target playlist,
    # same as the other sinks would.
    if entries and not song_ids:
        _set_error(conn, playlist_id, "no_target_matches")
        return

    result = emby_client.mirror_create_or_replace_playlist(
        row["title"], song_ids, row["emby_mirror_remote_id"])
    if result["status"] != "ok" and result.get("code") == _ERROR_NOT_FOUND \
            and row["emby_mirror_remote_id"] is not None:
        # The remote playlist Trobar remembers was deleted target-side —
        # clear the stale id up front (so even if this retry also fails for
        # an unrelated reason, the next write tries a fresh create rather
        # than repeating "not found" forever) and retry once as a create.
        # Same "never a stuck row" posture delete_mirror() already takes
        # for its own unlink failure.
        conn.execute(
            "UPDATE playlists SET emby_mirror_remote_id = NULL WHERE id = ?", (playlist_id,))
        result = emby_client.mirror_create_or_replace_playlist(row["title"], song_ids, None)
    if result["status"] != "ok":
        _set_error(conn, playlist_id, "write_failed", result.get("reason"))
        return

    remote_id = result["remote_id"]
    emby_client.mirror_set_playlist_metadata(
        remote_id, row["title"],
        f"Trobar mirror — {len(song_ids)} of {total} present, grows with your library",
    )

    conn.execute(
        "UPDATE playlists SET emby_mirror_remote_id = ?, "
        "emby_mirror_last_written_at = datetime('now'), "
        "emby_mirror_last_error = NULL, emby_mirror_last_error_code = NULL "
        "WHERE id = ?",
        (remote_id, playlist_id),
    )
