# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unresolved playlist tracks: the entries of a playlist that matched no
library track, kept so they can be reviewed, excluded or accepted.

A playlist sync records them; the Playlists panel lists them, excludes the
ones the user doesn't want and accepts a suggested file-name match for
others. Split out of sync_state, which re-exports these names for now.
"""
import json
import logging
import sqlite3

_log = logging.getLogger(__name__)


def record_unresolved_playlist_tracks(conn: sqlite3.Connection, playlist_id: int,
                                      unresolved: list) -> None:
    """#200: replace this playlist's unresolved-track review rows with the
    current sync's misses (called from playlist_sync._sync_one_playlist,
    once per playlist, with a list of {"position", "artist", "title",
    "album", "isrc"} dicts for entries identity.py's resolver couldn't
    match). Preserves `excluded`/`first_seen_at` for any entry that
    persists across the resync — same "acknowledgment survives a resync"
    shape as _record_device_unknown_tracks above, needed for the same
    reason: playlist_tracks itself is fully DELETE+recreated every sync
    (see _sync_one_playlist), so there's nowhere else to keep an "I've seen
    this, stop flagging it" choice.

    Identity is (artist, title, album), normalized NULL->'' — see
    idx_unresolved_playlist_tracks_identity in db.py for why the
    normalization matters. A playlist entry has no better stable id: most
    providers give no path, and `position` shifts on reorder/insert.

    The preserve-set DELETE below uses json_each() with ONE bound
    parameter, not an OR-chain with one term per row: a naive
    "(artist=? AND title=? AND album=?) OR (...) OR ..." expression tree
    hits SQLite's SQLITE_MAX_EXPR_DEPTH (default 1000) once a single
    playlist has >=1000 unresolved tracks — not a theoretical edge case,
    since "provider connected before the first library scan" or a large
    #189 golden playlist synced against a partially-covering library both
    put every entry in that state. json_extract(), not the shorter ->>
    operator, for compatibility with older SQLite builds (->> needs
    >=3.38)."""
    seen: set[tuple[str, str, str]] = set()
    deduped: list[tuple] = []
    for e in unresolved:
        artist, title, album = e.get("artist") or "", e.get("title") or "", e.get("album") or ""
        key = (artist, title, album)
        if key in seen:
            continue  # two genuinely-identical misses in one playlist -> one review row
        seen.add(key)
        deduped.append((e.get("position", 0), artist, title, album, e.get("isrc"),
                        e.get("suggested_track_id"), e.get("source_path")))

    if deduped:
        keep_json = json.dumps([[d[1], d[2], d[3]] for d in deduped])
        conn.execute(
            "DELETE FROM unresolved_playlist_tracks WHERE playlist_id = ? "
            "AND (artist, title, album) NOT IN ("
            "  SELECT json_extract(je.value, '$[0]'), json_extract(je.value, '$[1]'), "
            "         json_extract(je.value, '$[2]') FROM json_each(?) je"
            ")",
            (playlist_id, keep_json),
        )
    else:
        conn.execute("DELETE FROM unresolved_playlist_tracks WHERE playlist_id = ?", (playlist_id,))

    # A suggestion that changes (a different library track now fits the
    # file name best) is a new question: its acceptance doesn't carry over.
    for pos, artist, title, album, isrc, suggested, source_path in deduped:
        conn.execute(
            "INSERT INTO unresolved_playlist_tracks "
            "(playlist_id, position, artist, title, album, isrc, suggested_track_id, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(playlist_id, artist, title, album) "
            "DO UPDATE SET position = excluded.position, isrc = excluded.isrc, "
            "accepted = CASE WHEN suggested_track_id IS excluded.suggested_track_id THEN accepted ELSE 0 END, "
            "suggested_track_id = excluded.suggested_track_id, source_path = excluded.source_path",
            (playlist_id, pos, artist, title, album, isrc, suggested, source_path),
        )


def accepted_suggestion(conn: sqlite3.Connection, playlist_id: int, artist: str, title: str,
                        album: str | None, suggested_track_id: int) -> bool:
    """Whether this playlist's owner accepted `suggested_track_id` for the
    entry (artist, title, album): a suggestion is matched only once it is.
    Same identity, and same NULL->'' normalisation, as
    record_unresolved_playlist_tracks."""
    row = conn.execute(
        "SELECT 1 FROM unresolved_playlist_tracks WHERE playlist_id = ? AND artist = ? AND title = ? "
        "AND album = ? AND suggested_track_id = ? AND accepted = 1",
        (playlist_id, artist or "", title or "", album or "", suggested_track_id),
    ).fetchone()
    return row is not None


def list_unresolved_playlist_tracks(conn: sqlite3.Connection, playlist_id: int) -> list:
    """#200: this playlist's unresolved entries for the web review list —
    un-excluded first, then by artist/title/album."""
    rows = conn.execute(
        "SELECT u.id, u.artist, u.title, u.album, u.inferred_album, u.isrc, u.excluded, u.first_seen_at, "
        "u.accepted, u.source_path, t.id AS suggested_id, t.artist AS suggested_artist, "
        "t.title AS suggested_title, t.album AS suggested_album "
        "FROM unresolved_playlist_tracks u LEFT JOIN tracks t "
        "ON t.id = u.suggested_track_id AND t.deleted_at IS NULL "
        "WHERE u.playlist_id = ? "
        "ORDER BY u.excluded, u.artist, u.title, u.album",
        (playlist_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in ("id", "artist", "title", "album", "inferred_album", "isrc",
                               "first_seen_at", "source_path")}
        d["excluded"] = bool(r["excluded"])
        d["accepted"] = bool(r["accepted"])
        # A guess from the file name, when there is one: the library track
        # accepting it would match. None for an entry that matched nothing.
        d["suggested"] = ({"track_id": r["suggested_id"], "artist": r["suggested_artist"],
                           "title": r["suggested_title"], "album": r["suggested_album"]}
                          if r["suggested_id"] is not None else None)
        out.append(d)
    return out


def set_unresolved_playlist_tracks_accepted(conn: sqlite3.Connection, playlist_id: int,
                                            ids: list, accepted: bool) -> list:
    """Accept (or un-accept) the file-name guesses of the given review rows:
    the playlist entry at each row's position is matched to its suggestion,
    or back to unmatched. Rows without a suggestion, or whose suggested
    track has left the library, are skipped. Returns the ids changed. Does
    not commit; the caller rewrites the playlist's mirrors."""
    changed = []
    for row in conn.execute(
        "SELECT u.id, u.position, u.suggested_track_id FROM unresolved_playlist_tracks u "
        "JOIN tracks t ON t.id = u.suggested_track_id AND t.deleted_at IS NULL "
        f"WHERE u.playlist_id = ? AND u.id IN ({','.join('?' * len(ids))})",
        (playlist_id, *ids),
    ).fetchall() if ids else []:
        conn.execute("UPDATE unresolved_playlist_tracks SET accepted = ? WHERE id = ?",
                     (1 if accepted else 0, row["id"]))
        conn.execute("UPDATE playlist_tracks SET matched_track_id = ? WHERE playlist_id = ? AND position = ?",
                     (row["suggested_track_id"] if accepted else None, playlist_id, row["position"]))
        changed.append(row["id"])
    return changed


def set_unresolved_playlist_tracks_excluded(conn: sqlite3.Connection, playlist_id: int,
                                            ids: list, excluded: bool) -> int:
    """#200: mark the given unresolved_playlist_tracks row ids excluded
    (acknowledged as "not actually a gap" — e.g. a provider-only track that
    will never exist locally, streamed but never downloaded — so it stops
    being flagged) or un-excluded. Returns the resulting non-excluded count
    for this playlist."""
    seen = list(dict.fromkeys(ids))
    if seen:
        placeholders = ",".join("?" * len(seen))
        conn.execute(
            f"UPDATE unresolved_playlist_tracks SET excluded = ? "
            f"WHERE playlist_id = ? AND id IN ({placeholders})",
            (1 if excluded else 0, playlist_id, *seen),
        )
    count = conn.execute(
        "SELECT COUNT(*) FROM unresolved_playlist_tracks WHERE playlist_id = ? AND excluded = 0 AND accepted = 0",
        (playlist_id,),
    ).fetchone()[0]
    conn.commit()
    return count
