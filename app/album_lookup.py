#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The album a playlist gap belongs to, from MusicBrainz, for gaps whose
source gave none.

"Request missing albums" asks Lidarr for albums, so a gap without an album
name could never be requested. This fills in
`unresolved_playlist_tracks.inferred_album` for such gaps; the source's own
`album` stays untouched (it is part of the row's identity across resyncs),
and every reader that wants "the album" takes the source's first and the
looked-up one otherwise.

Two lookups, both MusicBrainz recording searches, because a search result
carries each release's status, date and release-group types in one
response (the ISRC resource can't include release groups):

1. by ISRC, when the gap has one -- exact by construction;
2. by artist + title otherwise, keeping only recordings whose title and
   credited artist match the gap's exactly after matching.normalize()
   (the gap's title may drop its trailing "(...)" tags, as in the library
   matcher; MusicBrainz's may not). A search's top hit is not trusted, for
   the same reason lidarr_requests filters Lidarr's own album lookup.

A recording is usually on several releases -- the album, a single,
compilations, live sets -- so the album is the release group of the
earliest Official release whose release group is an Album with no
secondary type. No such release leaves the album empty: a wrong album
would be requested from Lidarr, an empty one only isn't.

Answers are cached in musicbrainz_album_lookups by lookup key, found or
not, and never asked again: the same gap reappears on every sync, and in
several playlists. A failed request (network, HTTP error) is not an answer
and is not cached.

Runs only while Lidarr requests are configured: without Lidarr nothing
reads the result."""

import logging
import time

import requests

import db
import jobs
import lidarr_requests
import matching

_log = logging.getLogger(__name__)

#: Queued after every playlist sync and whenever the Lidarr settings are
#: saved; registered in main.py on the long lane, the same lane as the
#: fingerprint backfill -- jobs in one lane run one at a time, so the two
#: never query MusicBrainz at once and its 1 request/second policy holds
#: across both.
JOB_TYPE = "musicbrainz_album_lookup"

_SEARCH_URL = "https://musicbrainz.org/ws/2/recording"
_RATE_LIMIT_SECONDS = 1.0
# MusicBrainz blocks generic User-Agents; same identifying string as
# fingerprint.py's lookups.
_USER_AGENT = "Trobar-Server (+https://github.com/missing-foss/trobar-server; missing_foss@etik.com)"
# A popular song has hundreds of recordings (live versions are recordings
# of their own); the studio one is not always on the first page of 25.
_SEARCH_LIMIT = 100
# Lucene query syntax: these must be escaped inside a quoted term.
_LUCENE_SPECIAL = '\\+-&|!(){}[]^"~*?:/'


def _quote(value: str) -> str:
    escaped = "".join("\\" + c if c in _LUCENE_SPECIAL else c for c in value)
    return f'"{escaped}"'


def lookup_key(artist: str, title: str, isrc: str | None) -> str | None:
    """The cache key for a gap: its ISRC when it has one, else its
    normalized artist and title. None when neither route can run."""
    if isrc:
        return "isrc:" + isrc.strip().upper()
    if artist and title:
        return "at:" + matching.normalize(artist) + "\x1f" + matching.normalize(title)
    return None


def _search(query: str) -> list | None:
    """The recordings a search returns, or None when the request failed."""
    time.sleep(_RATE_LIMIT_SECONDS)  # MusicBrainz's own 1 request/second policy
    params: dict[str, str | int] = {"query": query, "fmt": "json", "limit": _SEARCH_LIMIT}
    try:
        resp = requests.get(
            _SEARCH_URL,
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("recordings") or []
    except Exception:
        _log.warning("MusicBrainz recording search failed for %r", query, exc_info=True)
        return None


def _credited_artist(recording: dict) -> str:
    return "".join(
        (credit.get("name") or "") + (credit.get("joinphrase") or "")
        for credit in recording.get("artist-credit") or []
    )


def _matches(recording: dict, artist: str, title: str) -> bool:
    """Exact title and artist match, normalized. The artist may be the full
    credit ("A feat. B") or any one credited name; a credits-list artist
    ("Songwriter, Performer") matches on its last segment, as in the
    library matcher."""
    # The gap's title as given or without its trailing "(...)" tags (a
    # streaming service's "(2009 Remaster)"), against MusicBrainz's title
    # as it is: stripping that side too would let "Song (Karaoke Version)"
    # pass for "Song".
    wanted = {matching.normalize(title), matching.normalize(matching._strip_annotations(title))}
    if matching.normalize(recording.get("title") or "") not in wanted:
        return False
    credited = {matching.normalize(_credited_artist(recording))}
    credited |= {matching.normalize(c.get("name") or "") for c in recording.get("artist-credit") or []}
    return any(matching.normalize(a) in credited for a in matching._artist_candidates(artist))


def pick_album(recordings: list) -> str | None:
    """The release-group title of the earliest Official release whose
    release group is an Album with no secondary type, across the given
    recordings; None when there is none. Undated releases sort last."""
    best: tuple[str, str] | None = None
    for recording in recordings:
        for release in recording.get("releases") or []:
            group = release.get("release-group") or {}
            if release.get("status") != "Official":
                continue
            if group.get("primary-type") != "Album" or group.get("secondary-types"):
                continue
            title = group.get("title") or release.get("title")
            if not title:
                continue
            # "9999" sorts an undated release after every dated one; ISO
            # dates of any precision (YYYY, YYYY-MM, YYYY-MM-DD) compare
            # correctly as strings.
            date = release.get("date") or "9999"
            if best is None or date < best[0]:
                best = (date, title)
    return best[1] if best else None


def find_album(artist: str, title: str, isrc: str | None) -> tuple[bool, str | None]:
    """(answered, album). answered is False when the request failed, so the
    caller doesn't cache a non-answer."""
    if isrc:
        recordings = _search("isrc:" + _quote(isrc.strip().upper()))
        if recordings is None:
            return False, None
        album = pick_album(recordings)
        if album or not (artist and title):
            return True, album
    if not (artist and title):
        return True, None
    # Narrowed to recordings on an official album: a popular song has
    # hundreds of live recordings, enough to push the studio one past the
    # first page. pick_album still applies the full rule.
    query = ("recording:" + _quote(matching._strip_annotations(title))
             + " AND artist:" + _quote(matching._artist_candidates(artist)[-1])
             + " AND status:official AND primarytype:album")
    recordings = _search(query)
    if recordings is None:
        return False, None
    return True, pick_album([r for r in recordings if _matches(r, artist, title)])


def _pending(conn) -> list:
    """Gaps that could be requested if they had an album: not excluded, no
    album from the source, none looked up yet."""
    return conn.execute(
        "SELECT id, playlist_id, artist, title, isrc FROM unresolved_playlist_tracks "
        "WHERE excluded = 0 AND (album IS NULL OR album = '') AND inferred_album IS NULL"
    ).fetchall()


def run_job(_payload: dict | None = None, report=None) -> dict:
    """Job handler. Looks up every pending gap's album, once per lookup key,
    and fills in inferred_album wherever an answer names one; then runs the
    Lidarr requests of every playlist that gained one and has them on, so
    they don't wait for the next sync. Returns counts for the jobs panel."""
    if db.get_lidarr_config() is None:
        return {"skipped": "lidarr_not_configured"}
    conn = db.get_conn()
    try:
        by_key: dict[str, list] = {}
        for row in _pending(conn):
            key = lookup_key(row["artist"] or "", row["title"] or "", row["isrc"])
            if key is not None:
                by_key.setdefault(key, []).append(row)
        cached = {
            r["lookup_key"]: r["album"]
            for r in conn.execute("SELECT lookup_key, album FROM musicbrainz_album_lookups")
        }
        asked = 0
        filled_playlists: set[int] = set()
        total = len(by_key)
        for done, (key, rows) in enumerate(by_key.items()):
            if report:
                report(done, total)
            if key in cached:
                album = cached[key]
            else:
                first = rows[0]
                answered, album = find_album(first["artist"] or "", first["title"] or "", first["isrc"])
                asked += 1
                if not answered:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO musicbrainz_album_lookups (lookup_key, album) VALUES (?, ?)",
                    (key, album),
                )
            if album:
                conn.executemany(
                    "UPDATE unresolved_playlist_tracks SET inferred_album = ? WHERE id = ?",
                    [(album, r["id"]) for r in rows],
                )
                filled_playlists.update(r["playlist_id"] for r in rows)
            conn.commit()
        for playlist_id in sorted(filled_playlists):
            lidarr_requests.run_for_playlist(conn, playlist_id)
            conn.commit()
        return {"gaps": sum(len(r) for r in by_key.values()), "asked": asked,
                "playlists_filled": len(filled_playlists)}
    finally:
        conn.close()


def enqueue(conn) -> None:
    """Queue a lookup run unless one is already queued or running, and only
    while Lidarr requests are configured."""
    if db.get_lidarr_config(conn) is None:
        return
    jobs.enqueue(conn, JOB_TYPE, dedupe_key=JOB_TYPE)


def enqueue_if_pending(conn, playlist_id: int) -> None:
    """enqueue(), when this playlist has a gap waiting for a lookup."""
    pending = conn.execute(
        "SELECT 1 FROM unresolved_playlist_tracks WHERE playlist_id = ? AND excluded = 0 "
        "AND (album IS NULL OR album = '') AND inferred_album IS NULL LIMIT 1",
        (playlist_id,),
    ).fetchone()
    if pending:
        enqueue(conn)
