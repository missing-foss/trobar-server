#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#494: "Request missing albums" — pushes a playlist's unresolved tracks to
a Lidarr instance as wanted albums, whenever a playlist's
lidarr_request_enabled flag is set. Same overall shape as the #189 mirror
sinks (mirror.py/mirror_subsonic.py/mirror_jellyfin.py/mirror_emby.py) —
called unconditionally per playlist from playlist_sync.py, no-ops
internally when disabled or unconfigured, never raises — but the VERB is
different: this doesn't copy the playlist anywhere, it asks Lidarr to
start watching for albums Trobar's library doesn't have yet.

Dedup is the headline design constraint (#494's own review settled this):
the same missing album can show up as a gap in several different
playlists, and must only ever be requested from Lidarr ONCE across the
whole install — not once per playlist. lidarr_requested_albums (db.py's
SCHEMA) is the cross-playlist record that makes that true: a row exists
there, forever, for every (normalized artist, normalized album) pair this
module has ever attempted, whether it succeeded, partially succeeded, or
failed outright. A pair with an existing row is never retried — the
alternative (retrying 'failed'/'partial' pairs on every later sync) would
repeatedly hammer Lidarr/MusicBrainz for data that, in the overwhelmingly
common case, isn't going to change (a genuine MusicBrainz mismatch, a
Lidarr-side config problem) — an admin who notices a stuck row fixes it by
hand in Lidarr's own UI rather than this module trying forever."""

import db
import lidarr_client
import matching


def _pick_candidate(candidates: list[dict], artist: str) -> dict | None:
    """Filter lidarr_client.lookup_album()'s raw, unranked candidates for
    an exact matching.normalize()'d artist-name match — never trust
    candidates[0] (see lidarr_client's own module docstring: #494's live
    testing found the top-ranked hit wrong 4 times out of 5 — tribute
    albums, lullaby-cover collections, and soundfont remixes routinely
    outrank the real release). Returns None if nothing matches, which the
    caller records as a permanent 'failed' outcome, same as any other
    resolution miss."""
    target = matching.normalize(artist)
    for candidate in candidates:
        candidate_artist = (candidate.get("artist") or {}).get("artistName", "")
        if matching.normalize(candidate_artist) == target:
            return candidate
    return None


def _already_attempted(conn, artist: str, album: str) -> bool:
    """True if lidarr_requested_albums already has a row for this
    (normalized artist, normalized album) pair, regardless of whether that
    attempt succeeded, partially succeeded, or failed — see this module's
    own docstring for why a row of any status means "don't ask again"."""
    return conn.execute(
        "SELECT 1 FROM lidarr_requested_albums WHERE normalized_artist = ? AND normalized_album = ?",
        (matching.normalize(artist), matching.normalize(album)),
    ).fetchone() is not None


def _record_outcome(
    conn, artist: str, album: str, status: str,
    lidarr_artist_id: int | None, lidarr_album_id: int | None, error: str | None,
) -> None:
    """A row is written exactly once per (normalized artist, normalized
    album) pair, ever — OR IGNORE rather than a plain INSERT specifically
    to stay safe if the same pair is ever attempted from two overlapping
    calls (the per-playlist toggle route runs this immediately on enable,
    outside the sequential single-threaded sync loop that's the only place
    this dedup is otherwise provably race-free — see the toggle route's
    own docstring)."""
    conn.execute(
        "INSERT OR IGNORE INTO lidarr_requested_albums "
        "(normalized_artist, normalized_album, artist, album, status, "
        "lidarr_artist_id, lidarr_album_id, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (matching.normalize(artist), matching.normalize(album), artist, album,
         status, lidarr_artist_id, lidarr_album_id, error),
    )


def _attempt_one(artist: str, album: str) -> tuple[str, str | None, int | None, int | None]:
    """Looks up and requests exactly one album from Lidarr. Returns
    (status, error_reason, lidarr_artist_id, lidarr_album_id) where status
    is 'requested' | 'partial' | 'failed' — see lidarr_client.
    add_and_monitor_album's own docstring for what 'partial' means."""
    lookup = lidarr_client.lookup_album(f"{artist} {album}")
    if lookup["status"] != "ok":
        return "failed", lookup.get("reason"), None, None

    candidate = _pick_candidate(lookup["candidates"], artist)
    if candidate is None:
        return "failed", "no_artist_match", None, None

    foreign_album_id = candidate.get("foreignAlbumId")
    foreign_artist_id = (candidate.get("artist") or {}).get("foreignArtistId")
    if not foreign_album_id or not foreign_artist_id:
        return "failed", "candidate_missing_ids", None, None

    result = lidarr_client.add_and_monitor_album(foreign_album_id, foreign_artist_id)
    if result["status"] == "ok":
        return "requested", None, result["artist_id"], result["album_id"]
    status = "partial" if result.get("stage") == "monitor" else "failed"
    return status, result.get("reason"), result.get("artist_id"), result.get("album_id")


def run_for_playlist(conn, playlist_id: int) -> None:
    """No-op (touches nothing at all) unless lidarr_request_enabled. If
    enabled but db.get_lidarr_config() isn't set, records 'unset_target'
    on lidarr_request_last_error_code and returns without touching
    lidarr_request_last_run_at — same "not yet configured is not a run"
    posture as mirror_subsonic.write_mirror's own unset_target check.
    Never raises otherwise — every per-album failure is caught and
    recorded via _record_outcome, not propagated.

    Eligible unresolved rows: excluded = 0 AND album IS NOT NULL AND
    album != '' — this single condition is both the #200 exclusion rule
    and the no-album-data eligibility rule (#494 item 9): Roon and
    iTunes/Apple Music unresolved rows always have album IS NULL, so
    they're naturally never selected here, not specially cased.

    Does not commit — runs inline in playlist_sync.py's per-playlist
    commit, same convention as every mirror sink's write_mirror().

    Always updates (once configured): lidarr_request_last_run_at (even at
    zero eligible/new rows — a legitimate outcome, not an error),
    lidarr_request_last_count (= rows that reached 'requested' this run
    specifically — a 'partial' or 'failed' outcome does not count),
    lidarr_request_last_error/_code (= the LAST failure this run, cleared
    to NULL on a run with none)."""
    row = conn.execute(
        "SELECT lidarr_request_enabled FROM playlists WHERE id = ?", (playlist_id,),
    ).fetchone()
    if row is None or not row["lidarr_request_enabled"]:
        return

    if db.get_lidarr_config() is None:
        conn.execute(
            "UPDATE playlists SET lidarr_request_last_error_code = 'unset_target', "
            "lidarr_request_last_error = NULL WHERE id = ?",
            (playlist_id,),
        )
        return

    eligible = conn.execute(
        "SELECT artist, album FROM unresolved_playlist_tracks "
        "WHERE playlist_id = ? AND excluded = 0 AND album IS NOT NULL AND album != ''",
        (playlist_id,),
    ).fetchall()

    requested_count = 0
    last_error: str | None = None
    last_error_code: str | None = None
    for entry in eligible:
        artist, album = entry["artist"], entry["album"]
        if _already_attempted(conn, artist, album):
            continue
        status, error, lidarr_artist_id, lidarr_album_id = _attempt_one(artist, album)
        _record_outcome(conn, artist, album, status, lidarr_artist_id, lidarr_album_id, error)
        if status == "requested":
            requested_count += 1
        else:
            last_error_code = status  # 'partial' or 'failed'
            last_error = error

    conn.execute(
        "UPDATE playlists SET lidarr_request_last_run_at = datetime('now'), "
        "lidarr_request_last_count = ?, lidarr_request_last_error = ?, "
        "lidarr_request_last_error_code = ? WHERE id = ?",
        (requested_count, last_error, last_error_code, playlist_id),
    )
