#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for mirror_subsonic.py's playlist-mirroring second sink (#189):
enabled-gating, the tag-based target lookup, the collapsed-match guard, the
stale-remote-id recreate path, the write/delete DB bookkeeping, and the
admin-error-surfacing failure modes. subsonic_client's own request mechanics
(pagination, create-or-replace semantics, ...) are covered directly in
test_subsonic_client.py — mocked at that module boundary here, same as
test_playlist_sync.py mocks mirror_subsonic itself one layer up. Same
DATA_DIR/DB_PATH tmp-dir override pattern as test_mirror.py.

    python3 -m unittest test_mirror_subsonic -v      # from app/
"""
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import db
import matching
import mirror_subsonic
import subsonic_client


def _key(artist: str, album: str, title: str) -> tuple[str, str, str]:
    return (matching.normalize(artist), matching.normalize(album), matching.normalize(title))


class _MirrorSubsonicTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-mirror-subsonic-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig_data_dir, self._orig_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore_db_globals)
        self.conn = db.get_conn()
        self._track_seq = 0
        # write_mirror() checks db.get_mirror_subsonic_config() directly
        # (not through the mocked subsonic_client) to distinguish "unset"
        # from "configured but unreachable" — every test needs a real
        # config on record unless it's specifically testing the unset case.
        db.set_config(self.conn, "mirror_subsonic_url", "http://mirror.example.com")
        db.set_config(self.conn, "mirror_subsonic_username", "trobar")
        db.set_config(self.conn, "mirror_subsonic_password", "secret")
        self.conn.commit()

    def _restore_db_globals(self):
        db.DATA_DIR, db.DB_PATH = self._orig_data_dir, self._orig_db_path

    def _clear_mirror_target_config(self) -> None:
        db.set_config(self.conn, "mirror_subsonic_url", None)
        db.set_config(self.conn, "mirror_subsonic_username", None)
        db.set_config(self.conn, "mirror_subsonic_password", None)
        self.conn.commit()

    def _make_playlist(self, title: str, subsonic_mirror_enabled: bool = True,
                        remote_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO playlists (title, subsonic_mirror_enabled, subsonic_mirror_remote_id) "
            "VALUES (?, ?, ?)",
            (title, 1 if subsonic_mirror_enabled else 0, remote_id),
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
            "SELECT subsonic_mirror_enabled, subsonic_mirror_remote_id, "
            "subsonic_mirror_last_written_at, subsonic_mirror_last_error, "
            "subsonic_mirror_last_error_code FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()


class NoOpTests(_MirrorSubsonicTestBase):
    def test_disabled_playlist_is_a_no_op(self):
        pid = self._make_playlist("Chill", subsonic_mirror_enabled=False)
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            mirror_subsonic.write_mirror(self.conn, pid)
        client.mirror_build_tag_index.assert_not_called()
        self.assertIsNone(self._row(pid)["subsonic_mirror_remote_id"])

    def test_no_mirror_target_configured_sets_unset_target_and_never_calls_the_client(self):
        # #189 review: distinct from "configured but dead" below -- checked
        # directly against db.get_mirror_subsonic_config(), before ever
        # reaching subsonic_client, so a missing config can't even look
        # like a network failure.
        self._clear_mirror_target_config()
        pid = self._make_playlist("Chill")
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            mirror_subsonic.write_mirror(self.conn, pid)
            client.mirror_build_tag_index.assert_not_called()
        row = self._row(pid)
        self.assertIsNone(row["subsonic_mirror_remote_id"])
        self.assertEqual(row["subsonic_mirror_last_error_code"], "unset_target")

    def test_configured_but_unreachable_target_sets_an_error(self):
        pid = self._make_playlist("Chill")
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = None
            mirror_subsonic.write_mirror(self.conn, pid)
        row = self._row(pid)
        self.assertIsNone(row["subsonic_mirror_remote_id"])
        self.assertEqual(row["subsonic_mirror_last_error_code"], "unreachable")


class WriteContentTests(_MirrorSubsonicTestBase):
    def test_successful_write_stores_the_new_remote_id_and_clears_errors(self):
        pid = self._make_playlist("Road Trip")
        t1 = self._make_track("Artist A", "Album", "Song A")
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist A", "Album", "Song A"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "99"}
            mirror_subsonic.write_mirror(self.conn, pid)
            client.mirror_create_or_replace_playlist.assert_called_once_with(
                "Road Trip", ["s1"], None)
            client.mirror_set_playlist_metadata.assert_called_once_with(
                "99", "Road Trip", mock.ANY)

        row = self._row(pid)
        self.assertEqual(row["subsonic_mirror_remote_id"], "99")
        self.assertIsNotNone(row["subsonic_mirror_last_written_at"])
        self.assertIsNone(row["subsonic_mirror_last_error"])
        self.assertIsNone(row["subsonic_mirror_last_error_code"])

    def test_only_target_indexed_tracks_are_included(self):
        pid = self._make_playlist("Partial")
        t1 = self._make_track("Artist", "Album", "Indexed")
        t2 = self._make_track("Artist", "Album", "Not Indexed")
        self._add_playlist_track(pid, 0, t1)
        self._add_playlist_track(pid, 1, t2)

        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Indexed"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_subsonic.write_mirror(self.conn, pid)
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

        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Indexed"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_subsonic.write_mirror(self.conn, pid)
            song_ids = client.mirror_create_or_replace_playlist.call_args.args[1]
        self.assertEqual(song_ids, ["s1"])

    def test_write_failure_sets_an_error_and_leaves_remote_id_untouched(self):
        pid = self._make_playlist("Chill", remote_id="1")
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "error", "reason": "boom", "code": None}
            mirror_subsonic.write_mirror(self.conn, pid)
        row = self._row(pid)
        self.assertEqual(row["subsonic_mirror_remote_id"], "1")  # unchanged
        self.assertEqual(row["subsonic_mirror_last_error_code"], "write_failed")
        self.assertEqual(row["subsonic_mirror_last_error"], "boom")


class CollapsedMatchGuardTests(_MirrorSubsonicTestBase):
    """#189 review: write_mirror() used to happily report a clean write
    over a playlist that matched NONE of its locally-resolved tracks on
    the target — the strong signal of a broken join key or a target
    pointed at the wrong library/account."""

    def test_zero_target_matches_with_locally_matched_tracks_sets_an_error(self):
        pid = self._make_playlist("Chill")
        t1 = self._make_track("Artist", "Album", "Song")
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {}  # target has nothing matching
            mirror_subsonic.write_mirror(self.conn, pid)
            client.mirror_create_or_replace_playlist.assert_not_called()

        row = self._row(pid)
        self.assertEqual(row["subsonic_mirror_last_error_code"], "no_target_matches")
        self.assertIsNone(row["subsonic_mirror_remote_id"])

    def test_a_playlist_with_nothing_matched_locally_still_writes_an_empty_target_playlist(self):
        # Not the same case as above: zero LOCAL matches is the ordinary
        # "nothing resolved yet" state every sink already writes an empty
        # copy for (mirror.py's filesystem sink does the same) — only a
        # nonzero local match count that collapses to zero TARGET matches
        # is the failure signal.
        pid = self._make_playlist("Empty So Far")
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_subsonic.write_mirror(self.conn, pid)
            client.mirror_create_or_replace_playlist.assert_called_once_with(
                "Empty So Far", [], None)
        self.assertIsNone(self._row(pid)["subsonic_mirror_last_error_code"])


class UntaggedFileDivergenceTests(_MirrorSubsonicTestBase):
    def test_diverging_unknown_tag_conventions_are_a_legitimate_drop(self):
        # scanner._read_tags() falls back to "Unknown Artist"/"Unknown
        # Album" for a file with no tags at all; Navidrome's own fallback
        # spells it "[Unknown Artist]"/"[Unknown Album]". Both sides agree
        # on title (the filename stem) and disagree on artist/album, so
        # this is a legitimate drop for THIS one track — not a bug to
        # special-case in the matcher — but with only one track in the
        # playlist, dropping its only entry is indistinguishable from the
        # collapsed-match failure case, so it correctly surfaces as one.
        pid = self._make_playlist("Chill")
        t1 = self._make_track("Unknown Artist", "Unknown Album", "03 - No Tags At All")
        self._add_playlist_track(pid, 0, t1)
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("[Unknown Artist]", "[Unknown Album]", "03 - No Tags At All"):
                    [{"id": "s1", "track_no": None}],
            }
            mirror_subsonic.write_mirror(self.conn, pid)
            client.mirror_create_or_replace_playlist.assert_not_called()
        self.assertEqual(self._row(pid)["subsonic_mirror_last_error_code"], "no_target_matches")

    def test_an_untagged_track_among_matched_ones_is_just_a_partial_drop(self):
        # Same tag divergence, but alongside a track that DOES match — the
        # playlist still writes, just without the one untagged entry. Only
        # a TOTAL collapse (the case above) is treated as an error.
        pid = self._make_playlist("Chill")
        t1 = self._make_track("Artist", "Album", "Song")
        t2 = self._make_track("Unknown Artist", "Unknown Album", "03 - No Tags At All")
        self._add_playlist_track(pid, 0, t1)
        self._add_playlist_track(pid, 1, t2)
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [{"id": "s1", "track_no": None}],
                _key("[Unknown Artist]", "[Unknown Album]", "03 - No Tags At All"):
                    [{"id": "s2", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_subsonic.write_mirror(self.conn, pid)
            song_ids = client.mirror_create_or_replace_playlist.call_args.args[1]
        self.assertEqual(song_ids, ["s1"])
        self.assertIsNone(self._row(pid)["subsonic_mirror_last_error_code"])


class TrackNoTiebreakTests(_MirrorSubsonicTestBase):
    def test_track_no_disambiguates_between_two_target_candidates(self):
        pid = self._make_playlist("Chill")
        t1 = self._make_track("Artist", "Album", "Song", track_no=2)
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [
                    {"id": "wrong-copy", "track_no": 1},
                    {"id": "right-copy", "track_no": 2},
                ],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_subsonic.write_mirror(self.conn, pid)
            song_ids = client.mirror_create_or_replace_playlist.call_args.args[1]
        self.assertEqual(song_ids, ["right-copy"])

    def test_falls_back_to_the_lowest_id_when_track_no_does_not_disambiguate(self):
        pid = self._make_playlist("Chill")
        t1 = self._make_track("Artist", "Album", "Song", track_no=None)
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [
                    {"id": "s2", "track_no": 1},
                    {"id": "s1", "track_no": 1},
                ],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_subsonic.write_mirror(self.conn, pid)
            song_ids = client.mirror_create_or_replace_playlist.call_args.args[1]
        self.assertEqual(song_ids, ["s1"])  # deterministic, not list order


class StaleRemoteIdTests(_MirrorSubsonicTestBase):
    """#189 review: a remote id whose target-side playlist was deleted
    must not wedge the mirror forever — same "never a stuck row" posture
    delete_mirror() already has for its own unlink failure."""

    def test_data_not_found_clears_the_stale_id_and_recreates(self):
        pid = self._make_playlist("Chill", remote_id="stale-id")
        t1 = self._make_track("Artist", "Album", "Song")
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.ERROR_DATA_NOT_FOUND = subsonic_client.ERROR_DATA_NOT_FOUND
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.side_effect = [
                {"status": "error", "reason": "data not found",
                 "code": subsonic_client.ERROR_DATA_NOT_FOUND},
                {"status": "ok", "remote_id": "new-id"},
            ]
            mirror_subsonic.write_mirror(self.conn, pid)
            calls = client.mirror_create_or_replace_playlist.call_args_list
            self.assertEqual(calls[0].args, ("Chill", ["s1"], "stale-id"))
            self.assertEqual(calls[1].args, ("Chill", ["s1"], None))

        row = self._row(pid)
        self.assertEqual(row["subsonic_mirror_remote_id"], "new-id")
        self.assertIsNone(row["subsonic_mirror_last_error_code"])

    def test_a_different_error_code_does_not_trigger_a_retry(self):
        pid = self._make_playlist("Chill", remote_id="stale-id")
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.ERROR_DATA_NOT_FOUND = subsonic_client.ERROR_DATA_NOT_FOUND
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "error", "reason": "wrong username or password", "code": 40}
            mirror_subsonic.write_mirror(self.conn, pid)
        client.mirror_create_or_replace_playlist.assert_called_once()
        row = self._row(pid)
        self.assertEqual(row["subsonic_mirror_remote_id"], "stale-id")  # untouched
        self.assertEqual(row["subsonic_mirror_last_error_code"], "write_failed")


class TagIndexCacheTests(_MirrorSubsonicTestBase):
    """#189 review: N mirrored playlists used to mean N full target
    library walks per sync — playlist_sync.py now builds the index once
    and passes the same cache dict to every write_mirror() call."""

    def test_a_shared_cache_builds_the_index_only_once(self):
        pid1 = self._make_playlist("One")
        pid2 = self._make_playlist("Two")
        cache: dict = {}
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_subsonic.write_mirror(self.conn, pid1, tag_index_cache=cache)
            mirror_subsonic.write_mirror(self.conn, pid2, tag_index_cache=cache)
        client.mirror_build_tag_index.assert_called_once()

    def test_no_cache_builds_a_fresh_index_every_call(self):
        pid1 = self._make_playlist("One")
        pid2 = self._make_playlist("Two")
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {}
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "1"}
            mirror_subsonic.write_mirror(self.conn, pid1)
            mirror_subsonic.write_mirror(self.conn, pid2)
        self.assertEqual(client.mirror_build_tag_index.call_count, 2)

    def test_a_cached_failed_build_is_not_retried_within_the_same_run(self):
        pid1 = self._make_playlist("One")
        pid2 = self._make_playlist("Two")
        cache: dict = {}
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = None
            mirror_subsonic.write_mirror(self.conn, pid1, tag_index_cache=cache)
            mirror_subsonic.write_mirror(self.conn, pid2, tag_index_cache=cache)
        client.mirror_build_tag_index.assert_called_once()
        self.assertEqual(self._row(pid1)["subsonic_mirror_last_error_code"], "unreachable")
        self.assertEqual(self._row(pid2)["subsonic_mirror_last_error_code"], "unreachable")


class IdempotentRewriteTests(_MirrorSubsonicTestBase):
    def test_second_write_passes_the_stored_remote_id(self):
        pid = self._make_playlist("Stable")
        t1 = self._make_track("Artist", "Album", "Song")
        self._add_playlist_track(pid, 0, t1)

        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_build_tag_index.return_value = {
                _key("Artist", "Album", "Song"): [{"id": "s1", "track_no": None}],
            }
            client.mirror_create_or_replace_playlist.return_value = {
                "status": "ok", "remote_id": "77"}
            mirror_subsonic.write_mirror(self.conn, pid)
            first_call_remote_id = client.mirror_create_or_replace_playlist.call_args.args[2]

            mirror_subsonic.write_mirror(self.conn, pid)
            second_call_remote_id = client.mirror_create_or_replace_playlist.call_args.args[2]

        self.assertIsNone(first_call_remote_id)
        self.assertEqual(second_call_remote_id, "77")


class DeleteMirrorTests(_MirrorSubsonicTestBase):
    def test_delete_with_no_remote_id_is_a_clean_no_op(self):
        pid = self._make_playlist("Never Written", remote_id=None)
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            mirror_subsonic.delete_mirror(self.conn, pid)  # must not raise
        client.mirror_delete_playlist.assert_not_called()
        self.assertIsNone(self._row(pid)["subsonic_mirror_remote_id"])

    def test_delete_removes_the_remote_playlist_and_clears_remote_id(self):
        pid = self._make_playlist("Chill", remote_id="42")
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_delete_playlist.return_value = True
            mirror_subsonic.delete_mirror(self.conn, pid)
        client.mirror_delete_playlist.assert_called_once_with("42")
        row = self._row(pid)
        self.assertIsNone(row["subsonic_mirror_remote_id"])
        self.assertIsNone(row["subsonic_mirror_last_written_at"])

    def test_delete_clears_remote_id_even_when_the_remote_call_fails(self):
        # Same posture as mirror.delete_mirror's own unlink-failure case:
        # losing track of a possibly-orphaned remote playlist is the
        # lesser problem next to a stuck row that can never write a fresh
        # one again.
        pid = self._make_playlist("Chill", remote_id="42")
        with mock.patch("mirror_subsonic.subsonic_client") as client:
            client.mirror_delete_playlist.return_value = False
            mirror_subsonic.delete_mirror(self.conn, pid)
        self.assertIsNone(self._row(pid)["subsonic_mirror_remote_id"])


if __name__ == "__main__":
    unittest.main()
