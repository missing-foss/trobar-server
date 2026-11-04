# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playlist mirroring to a Jellyfin or Emby server: one implementation for
both, since the two sinks were the same code under two names.

Same contract as the other sinks: one-way, golden wins, a full idempotent
rewrite of every playlist with `<name>_mirror_enabled = 1`. Every write
either creates a fresh remote playlist or replaces one by the remote id this
sink stored on a previous write (`<name>_mirror_remote_id`), so there is no
shared namespace a stray write could land in, and no marker machinery like
the filesystem sink's.

What differs between the two servers is the request mechanics, which live
in each server's own client module (its mirror_*() functions), and the
names: `<name>` is the column prefix ("jellyfin" or "emby") on playlists and
users, and the run's key in mirror_runs. mirror_jellyfin.py and
mirror_emby.py each bind one MediaBrowserMirror to their client and export
its write_mirror and delete_mirror.
"""
import logging
from collections.abc import Callable
from types import ModuleType

import matching
import mirror_runs

_log = logging.getLogger(__name__)

# "That playlistId doesn't exist", as both clients report it. Jellyfin's own
# playlist-scoped calls return a clean 404 ("Playlist not found") for a stale
# or nonexistent id (confirmed live). Emby's replace endpoint returns a bare
# 500 instead, so emby_client.mirror_create_or_replace_playlist() checks the
# id exists up front and reports this 404 itself (also confirmed live; see
# emby_client.py's docstring). A universal HTTP code, unlike Subsonic's
# protocol-specific numeric error.code, so no named export from either
# client is needed for this.
_ERROR_NOT_FOUND = 404


class MediaBrowserMirror:
    """One server's mirror sink. `client` and `config` are called on every
    use, not captured: each twin passes lambdas over its own module's names,
    so a test that patches mirror_jellyfin.jellyfin_client (or
    mirror_emby.emby_client) still reaches the calls made here."""

    def __init__(self, name: str, label: str, client: Callable[[], ModuleType],
                 config: Callable[[], tuple | None]) -> None:
        assert name in ("jellyfin", "emby"), name  # a column prefix: never from input
        self.name = name
        self.label = label
        self.client = client
        self.config = config

    def _set_error(self, conn, playlist_id: int, code: str, detail: str | None = None) -> None:
        conn.execute(
            f"UPDATE playlists SET {self.name}_mirror_last_error = ?, "
            f"{self.name}_mirror_last_error_code = ? WHERE id = ?",
            (detail, code, playlist_id),
        )


    def _mirror_account(self) -> str | None:
        """The configured mirror account's user id."""
        config = self.config()
        return config[2] if config else None


    def _same_server(self, cache: dict | None) -> bool | None:
        """Whether the mirror target is the library server, asked once per sync
        run: `cache` is the run's tag-index cache dict (None for a one-off
        write, which asks afresh)."""
        if cache is None:
            return self.client().mirror_target_is_library_server()
        if "same_server" not in cache:
            cache["same_server"] = self.client().mirror_target_is_library_server()
        return cache["same_server"]


    def _target_account(self, conn, row, cache: dict | None) -> str | None:
        """The account this playlist's copy belongs in: its owner's mapped
        account (users.<name>_user_id) when the owner has one and the mirror
        target is the library server the mapping was made on; otherwise the
        configured mirror account, as for an unowned playlist. When it can't be
        told whether the servers match (one didn't answer), an existing copy
        stays in the account it is in rather than being moved on a passing
        error."""
        if row["owner_user_id"] is None:
            return self._mirror_account()
        same = self._same_server(cache)
        if same is None:
            return row[f"{self.name}_mirror_owner_id"] or self._mirror_account()
        if same:
            mapped = conn.execute(f"SELECT {self.name}_user_id FROM users WHERE id = ?",
                                  (row["owner_user_id"],)).fetchone()
            if mapped is not None and mapped[0]:
                return mapped[0]
        return self._mirror_account()


    def _get_tag_index(self, tag_index_cache: dict | None):
        """Builds the target's tag index, or returns the one already built this
        sync run. `tag_index_cache` is a plain dict the CALLER owns and passes
        to every self.write_mirror() call across one sync pass (playlist_sync.py) —
        without it, N mirrored playlists meant N full-library walks against the
        target per sync. A one-off caller (the playlist mirror-toggle route)
        passes None and gets a fresh build every time, which is correct there:
        there's only one playlist to write.

        The failed-build case (None) is cached too, keyed the same way — every
        playlist in this run shares one target-unreachable verdict instead of
        each independently re-discovering it."""
        if tag_index_cache is None:
            return self.client().mirror_build_tag_index()
        if "index" not in tag_index_cache:
            tag_index_cache["index"] = self.client().mirror_build_tag_index()
        return tag_index_cache["index"]


    def _select_song_id(self, candidates: list[dict], track_no: int | None) -> str:
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


    def delete_mirror(self, conn, playlist_id: int) -> None:
        """Delete this playlist's remote mirror playlist, if any. Called when
        <name>_mirror_enabled flips to 0, and from playlist_sync.py's
        stale-playlist cleanup (BEFORE the playlist row itself is deleted,
        since this needs <name>_mirror_remote_id first). mirror_sinks commits
        around it.

        Clears the stored remote_id regardless of whether the remote delete
        call itself succeeded (logging a warning on failure) — same choice
        mirror.delete_mirror makes for a filesystem unlink failure: losing
        track of a possibly-orphaned remote playlist is the lesser problem
        next to a stuck row that can never write a fresh one again."""
        row = conn.execute(
            f"SELECT {self.name}_mirror_remote_id FROM playlists WHERE id = ?", (playlist_id,)
        ).fetchone()
        if row is None or not row[f"{self.name}_mirror_remote_id"]:
            return
        if not self.client().mirror_delete_playlist(row[f"{self.name}_mirror_remote_id"]):
            _log.warning(
                "failed to delete remote %s mirror playlist %s for playlist %s",
                self.label, row[f"{self.name}_mirror_remote_id"], playlist_id,
            )
        conn.execute(
            f"UPDATE playlists SET {self.name}_mirror_remote_id = NULL, {self.name}_mirror_owner_id = NULL, "
            f"{self.name}_mirror_last_written_at = NULL WHERE id = ?", (playlist_id,),
        )


    def write_mirror(self, conn, playlist_id: int, tag_index_cache: dict | None = None) -> None:
        """Idempotent full rewrite of this playlist's mirror on the server. No-ops
        (touches nothing) if the playlist isn't <name>_mirror_enabled or no
        mirror-target connection is configured. Never raises — every failure
        mode is instead persisted to <name>_mirror_last_error for the admin
        overview, same contract as mirror.write_mirror()/mirror_subsonic.write_
        mirror(). mirror_sinks commits around it.

        `tag_index_cache`: see self._get_tag_index()'s docstring — pass the same
        dict across every playlist in one sync run to build the target index
        only once."""
        row = conn.execute(
            f"SELECT title, owner_user_id, {self.name}_mirror_enabled, {self.name}_mirror_remote_id, {self.name}_mirror_owner_id "
            "FROM playlists WHERE id = ?", (playlist_id,),
        ).fetchone()
        if row is None or not row[f"{self.name}_mirror_enabled"]:
            return

        # Distinguish "nothing configured" from "configured but unreachable" —
        # mirror_build_tag_index() collapses both to None, so this check has
        # to happen up front here instead. Same split mirror.py/mirror_subsonic.py
        # already make (unset_folder/unset_target vs a real failure).
        if self.config() is None:
            self._set_error(conn, playlist_id, "unset_target")
            return

        tag_index = self._get_tag_index(tag_index_cache)
        if tag_index is None:
            self._set_error(conn, playlist_id, "unreachable")
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
        for e in entries:
            key = (matching.normalize(e["artist"]), matching.normalize(e["album"]),
                   matching.normalize(e["title"]))
            candidates = tag_index.get(key)
            if candidates:
                song_ids.append(self._select_song_id(candidates, e["track_no"]))
            else:
                lacked.append(e)

        # self.write_mirror() must not report a clean write over a playlist that
        # resolved NONE of its locally-matched tracks on the target — the
        # smoking-gun signal of a broken join key or a target pointed at the
        # wrong library/account, not a legitimate "golden ∩ local ∩
        # target-indexed" partial mirror (that model still allows SOME tracks
        # to drop, just not literally all of them when there was something to
        # match in the first place). `entries` empty (nothing matched locally
        # yet) is the ordinary case and still writes an empty target playlist,
        # same as the other two sinks would.
        if entries and not song_ids:
            mirror_runs.record(conn, playlist_id, self.name, len(entries), 0, lacked)
            self._set_error(conn, playlist_id, "no_target_matches")
            return

        # The account the copy belongs in (_target_account). A copy written
        # into another one (the mapping changed, or the mirror account did)
        # moves: the old copy is deleted by its id, which the admin key can do
        # whoever owns it, and a fresh one is made in the right account. A copy
        # from before this was tracked counts as being in the mirror account.
        target = self._target_account(conn, row, tag_index_cache)
        remote_id = row[f"{self.name}_mirror_remote_id"]
        if remote_id is not None and (row[f"{self.name}_mirror_owner_id"] or self._mirror_account()) != target:
            if not self.client().mirror_delete_playlist(remote_id):
                self._set_error(conn, playlist_id, "write_failed", "the copy in the previous account could not be removed")
                return
            conn.execute(f"UPDATE playlists SET {self.name}_mirror_remote_id = NULL, {self.name}_mirror_owner_id = NULL "
                         "WHERE id = ?", (playlist_id,))
            conn.commit()  # before calling out again
            remote_id = None
        result = self.client().mirror_create_or_replace_playlist(row["title"], song_ids, remote_id, target)
        if result["status"] != "ok" and result.get("code") == _ERROR_NOT_FOUND \
                and remote_id is not None:
            # The remote playlist Trobar remembers was deleted target-side —
            # clear the stale id up front (so even if this retry also fails for
            # an unrelated reason, the next write tries a fresh create rather
            # than repeating "not found" forever) and retry once as a create.
            # Same "never a stuck row" posture self.delete_mirror() already takes
            # for its own unlink failure.
            conn.execute(
                f"UPDATE playlists SET {self.name}_mirror_remote_id = NULL WHERE id = ?", (playlist_id,))
            conn.commit()  # before calling out again
            result = self.client().mirror_create_or_replace_playlist(row["title"], song_ids, None, target)
        if result["status"] != "ok":
            self._set_error(conn, playlist_id, "write_failed", result.get("reason"))
            return

        remote_id = result["remote_id"]
        self.client().mirror_set_playlist_metadata(
            remote_id, row["title"],
            f"Trobar mirror — {len(song_ids)} of {total} present, grows with your library",
        )

        conn.execute(
            f"UPDATE playlists SET {self.name}_mirror_remote_id = ?, {self.name}_mirror_owner_id = ?, "
            f"{self.name}_mirror_last_written_at = datetime('now'), "
            f"{self.name}_mirror_last_error = NULL, {self.name}_mirror_last_error_code = NULL "
            "WHERE id = ?",
            (remote_id, target, playlist_id),
        )
        mirror_runs.record(conn, playlist_id, self.name, len(entries), len(song_ids), lacked)

