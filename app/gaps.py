#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Gaps view: every unresolved track of the playlists a user can see,
in one place, grouped by album, so the same missing album is found and
dealt with once rather than playlist by playlist.

A user sees the gaps of exactly the playlists the playlist list shows them
(web_common._visible_playlists): unowned, shared, their own, or every one
for an admin. Excluding and requesting from Lidarr act only on those.

An album is the source's own, or else the one MusicBrainz inferred
(album_lookup.py). Tracks with neither form one group with no album: they
can be excluded but not requested, since Lidarr is asked for albums.

Requesting goes through lidarr_requests, the same job and the same dedupe
table as a playlist's "Request missing albums", and each album shows what
Lidarr did with it, read from that table."""

import logging
import sqlite3

import lidarr_requests
import matching

_log = logging.getLogger(__name__)


def _album_of(row) -> tuple[str | None, str | None]:
    """(album, where it came from): 'source', 'musicbrainz', or (None, None)."""
    if row["album"]:
        return row["album"], "source"
    if row["inferred_album"]:
        return row["inferred_album"], "musicbrainz"
    return None, None


def _key(artist: str, album: str | None) -> tuple[str, str] | None:
    return None if album is None else (matching.normalize(artist), matching.normalize(album))


def _rows(conn: sqlite3.Connection, playlist_ids: list[int], show_excluded: bool) -> list:
    """The open unresolved rows of these playlists (and the excluded ones,
    when asked). An accepted file-name guess is matched, not a gap."""
    if not playlist_ids:
        return []
    marks = ",".join("?" * len(playlist_ids))
    return conn.execute(
        "SELECT id, playlist_id, artist, title, album, inferred_album, excluded "
        f"FROM unresolved_playlist_tracks WHERE playlist_id IN ({marks}) AND accepted = 0 "
        + ("" if show_excluded else "AND excluded = 0 ")
        + "ORDER BY artist, album, title",
        playlist_ids,
    ).fetchall()


def _lidarr_states(conn: sqlite3.Connection) -> dict:
    """What Lidarr did with each album ever asked for, by normalized pair."""
    return {
        (r["normalized_artist"], r["normalized_album"]):
            {"status": r["status"], "error": r["error"], "at": r["requested_at"]}
        for r in conn.execute(
            "SELECT normalized_artist, normalized_album, status, error, requested_at "
            "FROM lidarr_requested_albums").fetchall()
    }


def list_gaps(conn: sqlite3.Connection, playlists: list[dict], owner: str | None = None,
              source: str | None = None, show_excluded: bool = False) -> dict:
    """The gaps of `playlists` (the caller's visible ones, as
    _visible_playlists returns them), grouped by album. `owner` keeps
    playlists of that owner's username ("" for the unowned ones); `source`
    keeps one source provider. `open_count` counts open gaps in every
    visible playlist, whatever the filters, for the tab's pill."""
    by_id = {p["id"]: p for p in playlists}
    all_open = _rows(conn, list(by_id), show_excluded=False)
    kept = [p for p in playlists
            if (owner is None or (p.get("owner_username") or "") == owner)
            and (source is None or _source_of(p) == source)]
    rows = _rows(conn, [p["id"] for p in kept], show_excluded)
    lidarr = _lidarr_states(conn)

    groups: dict = {}
    for r in rows:
        album, album_from = _album_of(r)
        key = _key(r["artist"], album)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "artist": r["artist"] if album is not None else None,
                "album": album, "album_from": album_from,
                "tracks": [], "playlists": {},
                "lidarr": lidarr.get(key) if key is not None else None,
            }
        p = by_id[r["playlist_id"]]
        g["tracks"].append({"id": r["id"], "artist": r["artist"], "title": r["title"],
                            "playlist_id": r["playlist_id"], "excluded": bool(r["excluded"])})
        g["playlists"][p["id"]] = {"id": p["id"], "title": p["title"],
                                  "owner": p.get("owner_username"), "shared": bool(p.get("shared"))}

    out = []
    for key, g in groups.items():
        g["playlists"] = sorted(g["playlists"].values(), key=lambda p: (p["title"] or "").casefold())
        g["open_count"] = sum(1 for t in g["tracks"] if not t["excluded"])
        # An album whose tracks are all excluded isn't an open gap: the
        # server would drop it from a request, so it isn't offered.
        g["requestable"] = key is not None and g["open_count"] > 0
        out.append(g)
    # Albums by artist then album; the group without an album last.
    out.sort(key=lambda g: (g["album"] is None, (g["artist"] or "").casefold(),
                            (g["album"] or "").casefold()))
    return {"groups": out, "open_count": len(all_open),
            "owners": sorted({p.get("owner_username") or "" for p in playlists}),
            "sources": sorted({_source_of(p) for p in playlists if _source_of(p)})}


def _source_of(p: dict) -> str | None:
    return p.get("source_provider") or p.get("inferred_origin_provider")


def set_excluded(conn: sqlite3.Connection, playlists: list[dict], ids: list[int],
                 excluded: bool) -> int:
    """Excludes (or un-excludes) the given unresolved rows, only those in
    `playlists` (the caller's visible ones); the rest are ignored. Returns
    how many rows it changed. Does not commit."""
    visible = [p["id"] for p in playlists]
    ids = list(dict.fromkeys(ids))
    if not ids or not visible:
        return 0
    cur = conn.execute(
        "UPDATE unresolved_playlist_tracks SET excluded = ? "
        f"WHERE id IN ({','.join('?' * len(ids))}) "
        f"AND playlist_id IN ({','.join('?' * len(visible))}) AND excluded != ?",
        (1 if excluded else 0, *ids, *visible, 1 if excluded else 0),
    )
    return cur.rowcount


def requestable_albums(conn: sqlite3.Connection, playlists: list[dict],
                       wanted: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The (artist, album) pairs of `wanted` that are open, requestable gaps
    of `playlists`, in the spelling the gaps carry. A pair that isn't (from
    a playlist the caller can't see, excluded, or made up) is dropped, so
    this can't be used to ask Lidarr for anything else."""
    wanted_keys = {_key(a, b) for a, b in wanted if a and b}
    found: dict = {}
    for r in _rows(conn, [p["id"] for p in playlists], show_excluded=False):
        album, _ = _album_of(r)
        key = _key(r["artist"], album)
        if key in wanted_keys and key not in found:
            found[key] = (r["artist"], album)
    return list(found.values())


def request_albums(conn: sqlite3.Connection, playlists: list[dict],
                   wanted: list[tuple[str, str]]) -> dict:
    """Queues a Lidarr request for the wanted albums that are open gaps of
    the caller's playlists. Returns how many were queued and whether a job
    was."""
    albums = requestable_albums(conn, playlists, wanted)
    queued = lidarr_requests.enqueue_albums(conn, albums)
    return {"albums": len(albums), "queued": queued}
