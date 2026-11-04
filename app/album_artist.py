# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The album artist of each track: what a library groups an album under.

A track's own `artist` is the performer; on a compilation every track has
a different one, and grouping by it shatters the album into one-track
"albums". The album artist is, per album (the tracks sharing a folder and
an album name):

- the file's `albumartist` tag when it has one;
- else the tag the album's other tracks carry (the most common one), so a
  half-tagged album stays one album;
- else, when the album's tracks name more than one artist, "Various
  Artists";
- else the track's own artist.

It depends on the whole album, so it's stored (tracks.album_artist) and
recomputed per album whenever one of its tracks changes; the raw tag is
kept beside it (tracks.album_artist_tag), because an incremental scan
doesn't re-read unchanged files. Until every track of an album has had its
tag read (tracks.album_artist_read, 0 on a library from before this), the
album isn't guessed to be a compilation: each track keeps its own artist
(or its tag), and the scan that reads the missing tags decides."""

import logging
import posixpath
from collections import Counter

_log = logging.getLogger(__name__)

VARIOUS_ARTISTS = "Various Artists"
# Above this many albums touched, refresh() reads the library once instead of
# album by album.
_WHOLE_LIBRARY_ABOVE = 500


def album_key(relative_path: str, album: str) -> tuple[str, str]:
    """The album a track belongs to: its folder and its album name."""
    return posixpath.dirname(relative_path), album.lower()


def effective(rows) -> dict:
    """{track id: album artist} for the tracks of one album. Each row has
    id, artist, album_artist_tag and album_artist_read."""
    if any(not r["album_artist_read"] for r in rows):
        return {r["id"]: r["album_artist_tag"] or r["artist"] for r in rows}
    tags = [r["album_artist_tag"] for r in rows if r["album_artist_tag"]]
    if tags:
        common = Counter(tags).most_common(1)[0][0]
        return {r["id"]: r["album_artist_tag"] or common for r in rows}
    if len({r["artist"] for r in rows}) > 1:
        return {r["id"]: VARIOUS_ARTISTS for r in rows}
    return {r["id"]: r["artist"] for r in rows}


def refresh(conn, keys=None) -> int:
    """Recompute tracks.album_artist for the albums in `keys` (album_key
    tuples), or for the whole library when None. Live tracks only decide an
    album; a deleted one keeps what it had. Returns the rows written. Does
    not commit."""
    groups: dict = {}
    if keys is not None and len(keys) > _WHOLE_LIBRARY_ABOVE:
        keys = None  # a forced rescan touches every album: one pass is cheaper
    if keys is None:
        rows = conn.execute(
            "SELECT id, relative_path, album, artist, album_artist_tag, album_artist_read FROM tracks "
            "WHERE deleted_at IS NULL").fetchall()
    else:
        wanted = set(keys)
        rows = []
        for folder in {f for f, _album in wanted}:
            if not folder:  # files at the library's root: no prefix to range on
                rows += conn.execute(
                    "SELECT id, relative_path, album, artist, album_artist_tag, album_artist_read FROM tracks "
                    "WHERE deleted_at IS NULL AND relative_path NOT LIKE '%/%'").fetchall()
                continue
            # A folder's tracks as a range on the indexed relative_path:
            # "folder/" up to "folder0", '0' sorting right after '/'.
            rows += conn.execute(
                "SELECT id, relative_path, album, artist, album_artist_tag, album_artist_read FROM tracks "
                "WHERE deleted_at IS NULL AND relative_path >= ? AND relative_path < ?",
                (folder + "/", folder + "0")).fetchall()
        rows = [r for r in rows if album_key(r["relative_path"], r["album"]) in wanted]
    for r in rows:
        groups.setdefault(album_key(r["relative_path"], r["album"]), {})[r["id"]] = r
    updates = []
    for members in groups.values():
        for track_id, value in effective(list(members.values())).items():
            updates.append((value, track_id))
    conn.executemany("UPDATE tracks SET album_artist = ? WHERE id = ?", updates)
    return len(updates)
