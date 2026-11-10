#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#189: playlist mirroring's second sink — a Subsonic/Navidrome API server,
alongside app/mirror.py's filesystem sink. Same contract, same "one-way,
golden-wins, full idempotent rewrite" semantics, for any playlist with
`subsonic_mirror_enabled = 1`.

Unlike the filesystem sink, there's no "don't clobber a file we don't own"
concern to guard (no marker/safe-path machinery here): every write either
creates a fresh remote playlist (a title, no remote_id yet) or replaces one
by the remote id THIS module itself stored on a previous write
(`subsonic_mirror_remote_id`) — there's no ambient shared namespace a stray
write could land in by accident the way an unmarked filesystem path could.

The actual request mechanics (tag-based song lookup, create-or-replace in
one call, best-effort comment) live in subsonic_client.py's mirror_*()
functions, confirmed live against a real Navidrome instance (#189's design
round, and a review round that replaced the original path-based join key
with this tag-based one after live-testing found the target's own `path`
field is a lossy tag-derived synthesis, not the real file path).
"""

import logging

import db
import matching
import mirror_copies
import mirror_runs
import subsonic_client

_log = logging.getLogger(__name__)


def _set_error(conn, playlist_id: int, code: str, detail: str | None = None) -> None:
    conn.execute(
        "UPDATE playlists SET subsonic_mirror_last_error = ?, "
        "subsonic_mirror_last_error_code = ? WHERE id = ?",
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
        return subsonic_client.mirror_build_tag_index()
    if "index" not in tag_index_cache:
        tag_index_cache["index"] = subsonic_client.mirror_build_tag_index()
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
    subsonic_mirror_enabled flips to 0, and from playlist_sync.py's
    stale-playlist cleanup (BEFORE the playlist row itself is deleted,
    since this needs subsonic_mirror_remote_id first). mirror_sinks commits
    around it.

    Clears the stored remote_id regardless of whether the remote delete
    call itself succeeded (logging a warning on failure) — same choice
    mirror.delete_mirror makes for a filesystem unlink failure: losing
    track of a possibly-orphaned remote playlist is the lesser problem
    next to a stuck row that can never write a fresh one again."""
    row = conn.execute(
        "SELECT subsonic_mirror_remote_id FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None or not row["subsonic_mirror_remote_id"]:
        return
    if not subsonic_client.mirror_delete_playlist(row["subsonic_mirror_remote_id"]):
        _log.warning(
            "failed to delete remote Subsonic mirror playlist %s for playlist %s",
            row["subsonic_mirror_remote_id"], playlist_id,
        )
    conn.execute(
        "UPDATE playlists SET subsonic_mirror_remote_id = NULL, "
        "subsonic_mirror_last_written_at = NULL WHERE id = ?", (playlist_id,),
    )
    mirror_copies.forget(conn, playlist_id, "subsonic")


def _hand_over(remote_id: str, held_by: str | None, wanted: str | None,
               cache: dict | None) -> str | None:
    """Puts the copy in its owner's Navidrome account (their username from
    Profile) when that user exists on the mirror server, through the mirror
    account, which must be a Navidrome admin. Returns whose account it is in
    now (None: the mirror account's). A copy whose owner gave no username,
    or one the server doesn't know, stays where it is: moving it back would
    take it out of someone's library without their asking. A failed move is
    tried again on the next write."""
    if not wanted or (held_by or "").casefold() == wanted.casefold():
        return held_by
    config = db.get_mirror_subsonic_config()
    if config is None:
        return held_by
    if cache is None or "navidrome_users" not in cache:
        users = subsonic_client.navidrome_usernames(*config)
        if cache is not None:
            cache["navidrome_users"] = users
    else:
        users = cache["navidrome_users"]
    if not users or wanted.casefold() not in users:
        return held_by
    if subsonic_client.navidrome_give_playlist(*config, remote_id, wanted):
        return wanted
    _log.warning("Couldn't give Subsonic mirror copy %s to %s; trying again next write", remote_id, wanted)
    return held_by


def write_mirror(conn, playlist_id: int, tag_index_cache: dict | None = None) -> None:
    """Idempotent full rewrite of this playlist's Subsonic mirror. No-ops
    (touches nothing) if the playlist isn't subsonic_mirror_enabled or no
    mirror-target connection is configured. Never raises — every failure
    mode is instead persisted to subsonic_mirror_last_error for the admin
    overview, same contract as mirror.write_mirror(). mirror_sinks commits around it.

    `tag_index_cache`: see _get_tag_index()'s docstring — pass the same
    dict across every playlist in one sync run to build the target index
    only once."""
    row = conn.execute(
        "SELECT p.title, p.subsonic_mirror_enabled, p.subsonic_mirror_remote_id, "
        "p.subsonic_mirror_owner, u.subsonic_username AS owner_subsonic_username "
        "FROM playlists p LEFT JOIN users u ON u.id = p.owner_user_id WHERE p.id = ?", (playlist_id,),
    ).fetchone()
    if row is None or not row["subsonic_mirror_enabled"]:
        return

    # #189 review: distinguish "nothing configured" from "configured but
    # unreachable" -- mirror_build_tag_index() collapses both to None, so
    # this check has to happen up front here instead. Same split mirror.py
    # already has (unset_folder vs a real write failure).
    if db.get_mirror_subsonic_config() is None:
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
    lacked = []
    placed: dict = {}  # target song id -> the local track placed there
    for e in entries:
        key = (matching.normalize(e["artist"]), matching.normalize(e["album"]),
               matching.normalize(e["title"]))
        candidates = tag_index.get(key)
        if candidates:
            song_id = _select_song_id(candidates, e["track_no"])
            song_ids.append(song_id)
            placed.setdefault(str(song_id), e)
        else:
            lacked.append(e)

    # A review round found write_mirror() would otherwise happily report a
    # clean write over a playlist that resolved NONE of its locally-matched
    # tracks on the target — the smoking-gun signal of a broken join key or
    # a target pointed at the wrong library/account, not a legitimate
    # "golden ∩ local ∩ target-indexed" partial mirror (that model still
    # allows SOME tracks to drop, just not literally all of them when there
    # was something to match in the first place). `entries` empty (nothing
    # matched locally yet) is the ordinary case and still writes an empty
    # target playlist, same as mirror.py's filesystem sink would.
    if entries and not song_ids:
        mirror_runs.record(conn, playlist_id, "subsonic", len(entries), 0, lacked)
        _set_error(conn, playlist_id, "no_target_matches")
        return

    # The copy as it stands, for what this write overwrites. One read per
    # write; a copy that can't be read is written all the same.
    current = None
    if row["subsonic_mirror_remote_id"] is not None:
        current = subsonic_client.mirror_read_items(row["subsonic_mirror_remote_id"])
        if not isinstance(current, list):
            current = None
    result = subsonic_client.mirror_create_or_replace_playlist(
        row["title"], song_ids, row["subsonic_mirror_remote_id"])
    if result["status"] != "ok" and result.get("code") == subsonic_client.ERROR_DATA_NOT_FOUND \
            and row["subsonic_mirror_remote_id"] is not None:
        # The remote playlist Trobar remembers was deleted target-side —
        # clear the stale id up front (so even if this retry also fails for
        # an unrelated reason, the next write tries a fresh create rather
        # than repeating "data not found" forever) and retry once as a
        # create. Same "never a stuck row" posture delete_mirror() already
        # takes for its own unlink failure.
        conn.execute(
            "UPDATE playlists SET subsonic_mirror_remote_id = NULL WHERE id = ?", (playlist_id,))
        conn.commit()  # before calling out again
        current = None  # a fresh copy overwrites nothing
        result = subsonic_client.mirror_create_or_replace_playlist(row["title"], song_ids, None)
    if result["status"] != "ok":
        _set_error(conn, playlist_id, "write_failed", result.get("reason"))
        return

    remote_id = result["remote_id"]
    discarded, restored = mirror_copies.overwritten(conn, playlist_id, "subsonic", current, song_ids)
    subsonic_client.mirror_set_playlist_metadata(
        remote_id, row["title"],
        f"Trobar mirror — {len(song_ids)} of {total} present, grows with your library",
    )
    # A fresh copy is in the mirror account; an old one is where we put it.
    held_by = row["subsonic_mirror_owner"] if row["subsonic_mirror_remote_id"] == remote_id else None
    held_by = _hand_over(remote_id, held_by, row["owner_subsonic_username"], tag_index_cache)

    conn.execute(
        "UPDATE playlists SET subsonic_mirror_remote_id = ?, subsonic_mirror_owner = ?, "
        "subsonic_mirror_last_written_at = datetime('now'), "
        "subsonic_mirror_last_error = NULL, subsonic_mirror_last_error_code = NULL "
        "WHERE id = ?",
        (remote_id, held_by, playlist_id),
    )
    # Nothing reads the copy back after the write, so the next baseline is
    # what was written; stored after the last call out.
    mirror_copies.remember(conn, playlist_id, "subsonic", [
        {"id": sid, "artist": placed[sid]["artist"], "title": placed[sid]["title"]}
        for sid in dict.fromkeys(str(i) for i in song_ids)])
    mirror_runs.record(conn, playlist_id, "subsonic", len(entries), len(song_ids), lacked,
                       discarded=discarded, restored=restored)
