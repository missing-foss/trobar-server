#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for mirror_jellyfin.py's playlist-mirroring third sink (#189):
enabled-gating, the tag-based target lookup, the collapsed-match guard, the
stale-remote-id recreate path, the write/delete DB bookkeeping, and the
admin-error-surfacing failure modes. jellyfin_client's own request mechanics
(pagination, replace semantics, ...) are covered directly in
test_jellyfin_client.py — mocked at that module boundary here, same as
test_mirror_subsonic.py mocks subsonic_client one layer up. Same
DATA_DIR/DB_PATH tmp-dir override pattern as test_mirror_subsonic.py.

    python3 -m unittest test_mirror_jellyfin -v      # from app/
"""
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import db
import matching
import mirror_jellyfin


def _key(artist: str, album: str, title: str) -> tuple[str, str, str]:
    return (matching.normalize(artist), matching.normalize(album), matching.normalize(title))


class _MirrorJellyfinTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-mirror-jellyfin-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore_db_globals)
        self.conn = db.get_conn()
        self._track_seq = 0
        # write_mirror() checks db.get_mirror_jellyfin_config() directly
        # (not through the mocked jellyfin_client) to distinguish "unset"
        # from "configured but unreachable" — every test needs a real
        # config on record unless it's specifically testing the unset case.
        db.set_config(self.conn, "mirror_jellyfin_url", "http://mirror.example.com")
        db.set_config(self.conn, "mirror_jellyfin_api_key", "key")
        db.set_config(self.conn, "mirror_jellyfin_username", "trobar")
        db.set_config(self.conn, "mirror_jellyfin_user_id", "u1")
        self.conn.commit()

    def _restore_db_globals(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def _clear_mirror_target_config(self) -> None:
        db.set_config(self.conn, "mirror_jellyfin_url", None)
        db.set_config(self.conn, "mirror_jellyfin_api_key", None)
        db.set_config(self.conn, "mirror_jellyfin_username", None)
        db.set_config(self.conn, "mirror_jellyfin_user_id", None)
        self.conn.commit()

    def _make_playlist(self, title: str, jellyfin_mirror_enabled: bool = True,
                        remote_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO playlists (title, jellyfin_mirror_enabled, jellyfin_mirror_remote_id) "
            "VALUES (?, ?, ?)",
            (title, 1 if jellyfin_mirror_enabled else 0, remote_id),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _make_track(self, artist: str, album: str, title: str, track_no: int | None = None) -> int:
        self._track_seq += 1
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime, duration) "
            "VALUES (?, ?, ?, ?, ?, 1, 0.0, 180.0)",
            (f"track-{self._track_seq}.flac", artist, album, title, track_no),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _add_playlist_track(self, playlist_id: int, position: int, track_id: int | None,
                             artist: str = "A", title: str = "T") -> None:
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (playlist_id, position, artist, title, track_id),
        )
        self.conn.commit()

    def _row(self, playlist_id: int):
        return self.conn.execute(
            "SELECT jellyfin_mirror_enabled, jellyfin_mirror_remote_id, "
            "jellyfin_mirror_last_written_at, jellyfin_mirror_last_error, "
            "jellyfin_mirror_last_error_code FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()


class NoOpTests(_MirrorJellyfinTestBase):
    def test_disabled_playlist_is_a_no_op(self):
        pid = self._make_playlist("Chill", jellyfin_mirror_enabled=False)
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            mirror_jellyfin.write_mirror(self.conn, pid)
        client.mirror_build_tag_index.assert_not_called()
        self.assertIsNone(self._row(pid)["jellyfin_mirror_remote_id"])

    def test_no_mirror_target_configured_sets_unset_target_and_never_calls_the_client(self):
        # #189 review analog to the Subsonic sink: distinct from
        # "configured but dead" below -- checked directly against
        # db.get_mirror_jellyfin_config(), before ever reaching
        # jellyfin_client, so a missing config can't even look like a
        # network failure.
        self._clear_mirror_target_config()
        pid = self._make_playlist("Chill")
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            mirror_jellyfin.write_mirror(self.conn, pid)
            client.mirror_build_tag_index.assert_not_called()
        row = self._row(pid)
        self.assertIsNone(row["jellyfin_mirror_remote_id"])
        self.assertEqual(row["jellyfin_mirror_last_error_code"], "unset_target")

    def test_configured_but_unreachable_target_sets_an_error(self):
        pid = self._make_playlist("Chill")
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = None
            mirror_jellyfin.write_mirror(self.conn, pid)
        row = self._row(pid)
        self.assertIsNone(row["jellyfin_mirror_remote_id"])
        self.assertEqual(row["jellyfin_mirror_last_error_code"], "unreachable")


class WriteContentTests(_MirrorJellyfinTestBase):
    def test_successful_write_stores_the_new_remote_id_and_clears_errors(self):
        pid = self._make_playlist("Road Trip")
        t1 = self._make_track("Artist A", "Album", "Song A")
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist A", "Album", "Song A"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "99"}
            mirror_jellyfin.write_mirror(self.conn, pid)
            client.mirror_create_or_replace_playlist.assert_called_once_with(
                "Road Trip", ["s1"], None)
            client.mirror_set_playlist_metadata.assert_called_once_with(
                "99", "Road Trip", mock.ANY)

        row = self._row(pid)
        self.assertEqual(row["jellyfin_mirror_remote_id"], "99")
        self.assertIsNotNone(row["jellyfin_mirror_last_written_at"])
        self.assertIsNone(row["jellyfin_mirror_last_error"])
        self.assertIsNone(row["jellyfin_mirror_last_error_code"])

    def test_only_target_indexed_tracks_are_included(self):
        pid = self._make_playlist("Partial")
        t1 = self._make_track("Artist", "Album", "Indexed")
        t2 = self._make_track("Artist", "Album", "Not Indexed")
        self._add_playlist_track(pid, 0, t1)
        self._add_playlist_track(pid, 1, t2)

        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Indexed"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_jellyfin.write_mirror(self.conn, pid)
            song_ids = client.mirror_create_or_replace_playlist.call_args.args[1]
            self.assertEqual(song_ids, ["s1"])
            comment = client.mirror_set_playlist_metadata.call_args.args[2]
        self.assertIn("1 of 2 present", comment)

    def test_unmatched_local_tracks_are_excluded_before_the_target_lookup(self):
        # A playlist_tracks row with matched_track_id NULL never reaches
        # the target-tag comparison at all (the JOIN drops it) — same
        # "unresolved locally" case mirror.py already excludes.
        pid = self._make_playlist("Partial")
        t1 = self._make_track("Artist", "Album", "Indexed")
        self._add_playlist_track(pid, 0, t1)
        self._add_playlist_track(pid, 1, None, "B", "Never matched")

        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Indexed"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_jellyfin.write_mirror(self.conn, pid)
            song_ids = client.mirror_create_or_replace_playlist.call_args.args[1]
        self.assertEqual(song_ids, ["s1"])

    def test_write_failure_sets_an_error_and_leaves_remote_id_untouched(self):
        pid = self._make_playlist("Chill", remote_id="1")
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "error", "reason": "boom", "code": None}
            mirror_jellyfin.write_mirror(self.conn, pid)
        row = self._row(pid)
        self.assertEqual(row["jellyfin_mirror_remote_id"], "1")  # unchanged
        self.assertEqual(row["jellyfin_mirror_last_error_code"], "write_failed")
        self.assertEqual(row["jellyfin_mirror_last_error"], "boom")


class CollapsedMatchGuardTests(_MirrorJellyfinTestBase):
    """#189 review: write_mirror() must not report a clean write over a
    playlist that matched NONE of its locally-resolved tracks on the
    target — the strong signal of a broken join key or a target pointed
    at the wrong library/account."""

    def test_zero_target_matches_with_locally_matched_tracks_sets_an_error(self):
        pid = self._make_playlist("Chill")
        t1 = self._make_track("Artist", "Album", "Song")
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {}  # target has nothing matching
            mirror_jellyfin.write_mirror(self.conn, pid)
            client.mirror_create_or_replace_playlist.assert_not_called()

        row = self._row(pid)
        self.assertEqual(row["jellyfin_mirror_last_error_code"], "no_target_matches")
        self.assertIsNone(row["jellyfin_mirror_remote_id"])

    def test_a_playlist_with_nothing_matched_locally_still_writes_an_empty_target_playlist(self):
        # Not the same case as above: zero LOCAL matches is the ordinary
        # "nothing resolved yet" state every sink already writes an empty
        # copy for — only a nonzero local match count that collapses to
        # zero TARGET matches is the failure signal.
        pid = self._make_playlist("Empty So Far")
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_jellyfin.write_mirror(self.conn, pid)
            client.mirror_create_or_replace_playlist.assert_called_once_with(
                "Empty So Far", [], None)
        self.assertIsNone(self._row(pid)["jellyfin_mirror_last_error_code"])


class TrackNoTiebreakTests(_MirrorJellyfinTestBase):
    def test_track_no_disambiguates_between_two_target_candidates(self):
        pid = self._make_playlist("Chill")
        t1 = self._make_track("Artist", "Album", "Song", track_no=2)
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [
                    {"id": "wrong-copy", "track_no": 1},
                    {"id": "right-copy", "track_no": 2},
                ],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_jellyfin.write_mirror(self.conn, pid)
            song_ids = client.mirror_create_or_replace_playlist.call_args.args[1]
        self.assertEqual(song_ids, ["right-copy"])

    def test_falls_back_to_the_lowest_id_when_track_no_does_not_disambiguate(self):
        pid = self._make_playlist("Chill")
        t1 = self._make_track("Artist", "Album", "Song", track_no=None)
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [
                    {"id": "s2", "track_no": 1},
                    {"id": "s1", "track_no": 1},
                ],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_jellyfin.write_mirror(self.conn, pid)
            song_ids = client.mirror_create_or_replace_playlist.call_args.args[1]
        self.assertEqual(song_ids, ["s1"])  # deterministic, not list order


class StaleRemoteIdTests(_MirrorJellyfinTestBase):
    """#189 review analog to the Subsonic sink: a remote id whose
    target-side playlist was deleted must not wedge the mirror forever —
    same "never a stuck row" posture delete_mirror() already has for its
    own delete-call failure. Jellyfin's stale signal is a universal HTTP
    404 (mirror_jellyfin._ERROR_NOT_FOUND), not a client-exported
    constant the way Subsonic's protocol-specific numeric code is."""

    def test_not_found_clears_the_stale_id_and_recreates(self):
        pid = self._make_playlist("Chill", remote_id="stale-id")
        t1 = self._make_track("Artist", "Album", "Song")
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.side_effect = [
                {"status": "error", "reason": "playlist not found", "code": 404},
                {"status": "ok", "remote_id": "new-id"},
            ]
            mirror_jellyfin.write_mirror(self.conn, pid)
            calls = client.mirror_create_or_replace_playlist.call_args_list
            self.assertEqual(calls[0].args, ("Chill", ["s1"], "stale-id"))
            self.assertEqual(calls[1].args, ("Chill", ["s1"], None))

        row = self._row(pid)
        self.assertEqual(row["jellyfin_mirror_remote_id"], "new-id")
        self.assertIsNone(row["jellyfin_mirror_last_error_code"])

    def test_a_different_error_code_does_not_trigger_a_retry(self):
        pid = self._make_playlist("Chill", remote_id="stale-id")
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "error", "reason": "failed to add items", "code": 500}
            mirror_jellyfin.write_mirror(self.conn, pid)
        client.mirror_create_or_replace_playlist.assert_called_once()
        row = self._row(pid)
        self.assertEqual(row["jellyfin_mirror_remote_id"], "stale-id")  # untouched
        self.assertEqual(row["jellyfin_mirror_last_error_code"], "write_failed")

    def test_a_missing_remote_id_never_retries_regardless_of_code(self):
        # result.get("code") == 404 alone isn't enough to trigger a retry
        # -- there must have been a remote_id to invalidate in the first
        # place (write_mirror()'s own guard: `... and row["jellyfin_
        # mirror_remote_id"] is not None`), otherwise a fresh create that
        # somehow got a 404 back would loop.
        pid = self._make_playlist("Chill", remote_id=None)
        t1 = self._make_track("Artist", "Album", "Song")
        self._add_playlist_track(pid, 0, t1)
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "error", "reason": "create failed", "code": 404}
            mirror_jellyfin.write_mirror(self.conn, pid)
        client.mirror_create_or_replace_playlist.assert_called_once()
        row = self._row(pid)
        self.assertIsNone(row["jellyfin_mirror_remote_id"])
        self.assertEqual(row["jellyfin_mirror_last_error_code"], "write_failed")


class TagIndexCacheTests(_MirrorJellyfinTestBase):
    """#189 review analog to the Subsonic sink: N mirrored playlists used
    to mean N full target library walks per sync — playlist_sync.py now
    builds the index once and passes the same cache dict to every
    write_mirror() call."""

    def test_a_shared_cache_builds_the_index_only_once(self):
        pid1 = self._make_playlist("One")
        pid2 = self._make_playlist("Two")
        cache: dict = {}
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_jellyfin.write_mirror(self.conn, pid1, tag_index_cache=cache)
            mirror_jellyfin.write_mirror(self.conn, pid2, tag_index_cache=cache)
        client.mirror_build_tag_index.assert_called_once()

    def test_no_cache_builds_a_fresh_index_every_call(self):
        pid1 = self._make_playlist("One")
        pid2 = self._make_playlist("Two")
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_jellyfin.write_mirror(self.conn, pid1)
            mirror_jellyfin.write_mirror(self.conn, pid2)
        self.assertEqual(client.mirror_build_tag_index.call_count, 2)

    def test_a_cached_failed_build_is_not_retried_within_the_same_run(self):
        pid1 = self._make_playlist("One")
        pid2 = self._make_playlist("Two")
        cache: dict = {}
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = None
            mirror_jellyfin.write_mirror(self.conn, pid1, tag_index_cache=cache)
            mirror_jellyfin.write_mirror(self.conn, pid2, tag_index_cache=cache)
        client.mirror_build_tag_index.assert_called_once()
        self.assertEqual(self._row(pid1)["jellyfin_mirror_last_error_code"], "unreachable")
        self.assertEqual(self._row(pid2)["jellyfin_mirror_last_error_code"], "unreachable")


class IdempotentRewriteTests(_MirrorJellyfinTestBase):
    def test_second_write_passes_the_stored_remote_id(self):
        pid = self._make_playlist("Stable")
        t1 = self._make_track("Artist", "Album", "Song")
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "77"}
            mirror_jellyfin.write_mirror(self.conn, pid)
            first_call_remote_id = client.mirror_create_or_replace_playlist.call_args.args[2]

            mirror_jellyfin.write_mirror(self.conn, pid)
            second_call_remote_id = client.mirror_create_or_replace_playlist.call_args.args[2]

        self.assertIsNone(first_call_remote_id)
        self.assertEqual(second_call_remote_id, "77")


class DeleteMirrorTests(_MirrorJellyfinTestBase):
    def test_delete_with_no_remote_id_is_a_clean_no_op(self):
        pid = self._make_playlist("Never Written", remote_id=None)
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            mirror_jellyfin.delete_mirror(self.conn, pid)  # must not raise
        client.mirror_delete_playlist.assert_not_called()
        self.assertIsNone(self._row(pid)["jellyfin_mirror_remote_id"])

    def test_delete_removes_the_remote_playlist_and_clears_remote_id(self):
        pid = self._make_playlist("Chill", remote_id="42")
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_delete_playlist.return_value = True
            mirror_jellyfin.delete_mirror(self.conn, pid)
        client.mirror_delete_playlist.assert_called_once_with("42")
        row = self._row(pid)
        self.assertIsNone(row["jellyfin_mirror_remote_id"])
        self.assertIsNone(row["jellyfin_mirror_last_written_at"])

    def test_delete_clears_remote_id_even_when_the_remote_call_fails(self):
        # Same posture as mirror.delete_mirror's own unlink-failure case:
        # losing track of a possibly-orphaned remote playlist is the
        # lesser problem next to a stuck row that can never write a fresh
        # one again.
        pid = self._make_playlist("Chill", remote_id="42")
        with mock.patch("mirror_jellyfin.jellyfin_client") as client:
            client.mirror_delete_playlist.return_value = False
            mirror_jellyfin.delete_mirror(self.conn, pid)
        self.assertIsNone(self._row(pid)["jellyfin_mirror_remote_id"])


if __name__ == "__main__":
    unittest.main()
