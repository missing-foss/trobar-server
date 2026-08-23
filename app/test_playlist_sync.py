#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests that _sync_one_playlist must set owner_user_id
for the two per-user sync paths (Roon profile, direct Tidal) and — the
easy way to silently break someone's privacy choice — must never touch
`shared` on re-sync, or every future sync would reset a user's own
"make this private" choice back to the shared default.

    python3 -m unittest test_playlist_sync -v      # from app/

Uses an in-memory sqlite DB built from db.SCHEMA + db._run_migrations()
(owner_user_id/shared are migration-added columns, not in the base
CREATE TABLE) — no Flask, no fixtures on disk. A minimal stub stands in
for a provider module (only get_playlist_tracks() is ever called by
_sync_one_playlist).
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import jobs
import playlist_sync
import spotify_client
import sync_state
import tidal_client


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)
    db._migrate_playlists_composite_key(conn)  # #75 composite unique indexes
    return conn


def _make_user(conn: sqlite3.Connection, username: str = "alice") -> int:
    cur = conn.execute("INSERT INTO users (username) VALUES (?)", (username,))
    conn.commit()
    return sync_state._new_id(cur)


class _StubProvider:
    """Only get_playlist_tracks() is called by _sync_one_playlist — real
    provider modules (roon_client, tidal_client) have a much wider surface
    this test has no need to stand in for."""

    def __init__(self, tracks):
        self._tracks = tracks

    def get_playlist_tracks(self, title, source_playlist_id=None, **_kwargs):
        return {"status": "ok", "tracks": self._tracks}


_ONE_TRACK = [{"position": 0, "artist": "Artist A", "title": "Song A", "album": None}]


class SyncOnePlaylistOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.user_id = _make_user(self.conn)
        self.provider = _StubProvider(_ONE_TRACK)

    def test_new_playlist_with_owner_is_stored_owned_and_private_by_default(self):
        # #496: an owned playlist starts private — the owner opts in to
        # share it, rather than the sync publishing it to the household
        # on their behalf.
        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "roon", "My Mix", owner_user_id=self.user_id,
        )
        row = self.conn.execute(
            "SELECT owner_user_id, shared FROM playlists WHERE title = 'My Mix'"
        ).fetchone()
        self.assertEqual(row["owner_user_id"], self.user_id)
        self.assertEqual(row["shared"], 0)

    def test_new_playlist_without_owner_stays_unowned_and_shared(self):
        # Unowned (the household's single configured provider account's own
        # listing) is unaffected by #496 — it belongs to the household by
        # definition and stays shared=1.
        playlist_sync._sync_one_playlist(self.conn, self.provider, "subsonic", "Shared Mix")
        row = self.conn.execute(
            "SELECT owner_user_id, shared FROM playlists WHERE title = 'Shared Mix'"
        ).fetchone()
        self.assertIsNone(row["owner_user_id"])
        self.assertEqual(row["shared"], 1)

    def test_resync_never_resets_an_owners_privacy_choice(self):
        # First sync creates it private (shared=0 default, #496), the
        # owner explicitly shares it, then a normal re-sync must leave
        # that choice alone — the exact bug this issue would otherwise
        # reintroduce every single sync, just in the opposite direction
        # from before #496 (silently reverting a share back to private
        # instead of a privatize back to shared).
        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "roon", "My Mix", owner_user_id=self.user_id,
        )
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'My Mix'").fetchone()["id"]
        self.conn.execute("UPDATE playlists SET shared = 1 WHERE id = ?", (playlist_id,))
        self.conn.commit()

        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "roon", "My Mix", owner_user_id=self.user_id,
        )
        row = self.conn.execute("SELECT shared FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        self.assertEqual(row["shared"], 1)

    def test_ownership_updates_on_resync_if_the_source_changes(self):
        # e.g. a Roon profile mapping gets removed — the next sync that
        # picks this title up via the primary (unowned) pass should
        # reflect that, same as source_provider already does.
        other_user = _make_user(self.conn, "bob")
        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "roon", "My Mix", owner_user_id=self.user_id,
        )
        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "roon", "My Mix", owner_user_id=other_user,
        )
        row = self.conn.execute("SELECT owner_user_id FROM playlists WHERE title = 'My Mix'").fetchone()
        self.assertEqual(row["owner_user_id"], other_user)

    def test_ownership_transfer_resets_shared_to_private_default(self):
        # #70/#496: owner A explicitly shares their playlist, then it's
        # reassigned to owner B on a later re-sync (profile mapping / Tidal
        # link changes hands). B must NOT silently inherit A's sharing
        # choice — `shared` resets to the same private default a freshly
        # owned playlist gets, so B gets a private playlist they can share
        # themselves if they want.
        other_user = _make_user(self.conn, "bob")
        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "roon", "My Mix", owner_user_id=self.user_id,
        )
        pid = self.conn.execute("SELECT id FROM playlists WHERE title = 'My Mix'").fetchone()["id"]
        self.conn.execute("UPDATE playlists SET shared = 1 WHERE id = ?", (pid,))  # A explicitly shared it
        self.conn.commit()

        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "roon", "My Mix", owner_user_id=other_user,
        )
        row = self.conn.execute("SELECT owner_user_id, shared FROM playlists WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["owner_user_id"], other_user)
        self.assertEqual(row["shared"], 0)  # reset to private, not inherited as shared


class SyncOnePlaylistUnresolvedTracksTests(unittest.TestCase):
    """#200: _sync_one_playlist now resolves each track via identity.py
    (matching.py underneath, unchanged) and records anything it misses
    into unresolved_playlist_tracks for the review surface."""

    def setUp(self):
        self.conn = _make_conn()

    def test_matched_track_is_not_recorded_as_unresolved(self):
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('Artist A/Song A.flac', 'Artist A', '', 'Song A', 1, 0.0)"
        )
        self.conn.commit()
        provider = _StubProvider([{"position": 0, "artist": "Artist A", "title": "Song A", "album": None}])
        playlist_sync._sync_one_playlist(self.conn, provider, "roon", "Mix")
        rows = self.conn.execute("SELECT * FROM unresolved_playlist_tracks").fetchall()
        self.assertEqual(rows, [])

    def test_unmatched_track_is_recorded_as_unresolved(self):
        provider = _StubProvider(_ONE_TRACK)  # Artist A / Song A, no local track exists
        playlist_sync._sync_one_playlist(self.conn, provider, "roon", "Mix")
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        rows = sync_state.list_unresolved_playlist_tracks(self.conn, playlist_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["artist"], "Artist A")
        self.assertEqual(rows[0]["title"], "Song A")

    def test_excluded_flag_survives_a_resync_with_the_same_miss(self):
        provider = _StubProvider(_ONE_TRACK)
        playlist_sync._sync_one_playlist(self.conn, provider, "roon", "Mix")
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        row_id = sync_state.list_unresolved_playlist_tracks(self.conn, playlist_id)[0]["id"]
        sync_state.set_unresolved_playlist_tracks_excluded(self.conn, playlist_id, [row_id], True)

        playlist_sync._sync_one_playlist(self.conn, provider, "roon", "Mix")
        rows = sync_state.list_unresolved_playlist_tracks(self.conn, playlist_id)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["excluded"])

    def test_a_track_that_starts_matching_is_removed_from_the_review_list(self):
        provider = _StubProvider(_ONE_TRACK)
        playlist_sync._sync_one_playlist(self.conn, provider, "roon", "Mix")
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        self.assertEqual(len(sync_state.list_unresolved_playlist_tracks(self.conn, playlist_id)), 1)

        # The library gains a matching track before the next sync.
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('Artist A/Song A.flac', 'Artist A', '', 'Song A', 1, 0.0)"
        )
        self.conn.commit()
        playlist_sync._sync_one_playlist(self.conn, provider, "roon", "Mix")
        self.assertEqual(sync_state.list_unresolved_playlist_tracks(self.conn, playlist_id), [])


class SyncOnePlaylistMirrorTests(unittest.TestCase):
    """#285: _sync_one_playlist calls mirror.write_mirror once
    matched_track_id is fresh for every track. Mocked here (mirror.py's
    own real-filesystem behavior is covered by test_mirror.py) since this
    harness's in-memory conn isn't reachable via db.get_conn(), which
    mirror.write_mirror uses internally once past its mirror_enabled
    early-return — mocking keeps this test about the WIRING, not
    mirror.py's internals."""

    def setUp(self):
        self.conn = _make_conn()
        self.provider = _StubProvider(_ONE_TRACK)

    def test_write_mirror_is_called_with_the_new_playlists_id(self):
        with mock.patch.object(playlist_sync.mirror, "write_mirror") as write_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        write_mock.assert_called_once_with(self.conn, playlist_id)

    def test_write_mirror_is_called_on_every_sync_regardless_of_mirror_enabled(self):
        # write_mirror itself decides whether mirror_enabled=1 (it no-ops
        # otherwise) — _sync_one_playlist always calls it unconditionally,
        # same shape as record_unresolved_playlist_tracks above.
        with mock.patch.object(playlist_sync.mirror, "write_mirror") as write_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        self.assertEqual(write_mock.call_count, 2)


class SyncOnePlaylistMirrorSubsonicTests(unittest.TestCase):
    """#189: the second, independent sink — same wiring shape as
    SyncOnePlaylistMirrorTests above, mocked here for the same reason
    (mirror_subsonic.py's own behavior is covered by
    test_mirror_subsonic.py)."""

    def setUp(self):
        self.conn = _make_conn()
        self.provider = _StubProvider(_ONE_TRACK)

    def test_write_mirror_is_called_with_the_new_playlists_id(self):
        with mock.patch.object(playlist_sync.mirror_subsonic, "write_mirror") as write_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        write_mock.assert_called_once_with(self.conn, playlist_id, tag_index_cache=None)

    def test_write_mirror_is_called_on_every_sync_regardless_of_mirror_enabled(self):
        with mock.patch.object(playlist_sync.mirror_subsonic, "write_mirror") as write_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        self.assertEqual(write_mock.call_count, 2)

    def test_one_sinks_mock_does_not_suppress_the_other(self):
        # Both sinks are called from the same call site, unconditionally
        # -- mocking one must not accidentally swallow the other.
        with mock.patch.object(playlist_sync.mirror, "write_mirror") as fs_mock, \
             mock.patch.object(playlist_sync.mirror_subsonic, "write_mirror") as subsonic_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        fs_mock.assert_called_once()
        subsonic_mock.assert_called_once()


class SyncOnePlaylistMirrorJellyfinTests(unittest.TestCase):
    """#189: the third, independent sink — same wiring shape as
    SyncOnePlaylistMirrorSubsonicTests above, mocked here for the same
    reason (mirror_jellyfin.py's own behavior is covered by
    test_mirror_jellyfin.py)."""

    def setUp(self):
        self.conn = _make_conn()
        self.provider = _StubProvider(_ONE_TRACK)

    def test_write_mirror_is_called_with_the_new_playlists_id(self):
        with mock.patch.object(playlist_sync.mirror_jellyfin, "write_mirror") as write_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        write_mock.assert_called_once_with(self.conn, playlist_id, tag_index_cache=None)

    def test_write_mirror_is_called_on_every_sync_regardless_of_mirror_enabled(self):
        with mock.patch.object(playlist_sync.mirror_jellyfin, "write_mirror") as write_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        self.assertEqual(write_mock.call_count, 2)

    def test_none_of_the_four_sinks_mocks_suppresses_another(self):
        # All four sinks are called from the same call site,
        # unconditionally -- mocking any one must not accidentally
        # swallow the others.
        with mock.patch.object(playlist_sync.mirror, "write_mirror") as fs_mock, \
             mock.patch.object(playlist_sync.mirror_subsonic, "write_mirror") as subsonic_mock, \
             mock.patch.object(playlist_sync.mirror_jellyfin, "write_mirror") as jellyfin_mock, \
             mock.patch.object(playlist_sync.mirror_emby, "write_mirror") as emby_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        fs_mock.assert_called_once()
        subsonic_mock.assert_called_once()
        jellyfin_mock.assert_called_once()
        emby_mock.assert_called_once()


class SyncOnePlaylistMirrorEmbyTests(unittest.TestCase):
    """#189: the fourth and (per the RFC) final sink — same wiring shape as
    SyncOnePlaylistMirrorJellyfinTests above, mocked here for the same
    reason (mirror_emby.py's own behavior is covered by
    test_mirror_emby.py)."""

    def setUp(self):
        self.conn = _make_conn()
        self.provider = _StubProvider(_ONE_TRACK)

    def test_write_mirror_is_called_with_the_new_playlists_id(self):
        with mock.patch.object(playlist_sync.mirror_emby, "write_mirror") as write_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        write_mock.assert_called_once_with(self.conn, playlist_id, tag_index_cache=None)

    def test_write_mirror_is_called_on_every_sync_regardless_of_mirror_enabled(self):
        with mock.patch.object(playlist_sync.mirror_emby, "write_mirror") as write_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        self.assertEqual(write_mock.call_count, 2)


class SyncOnePlaylistLidarrRequestsTests(unittest.TestCase):
    """#494: not a mirror sink, but called from the same shared call site
    right after the four sinks above — same wiring-only shape (its own
    behavior is covered by test_lidarr_requests.py)."""

    def setUp(self):
        self.conn = _make_conn()
        self.provider = _StubProvider(_ONE_TRACK)

    def test_run_for_playlist_is_called_with_the_new_playlists_id(self):
        with mock.patch.object(playlist_sync.lidarr_requests, "run_for_playlist") as run_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        playlist_id = self.conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        run_mock.assert_called_once_with(self.conn, playlist_id)

    def test_run_for_playlist_is_called_on_every_sync_regardless_of_enabled(self):
        with mock.patch.object(playlist_sync.lidarr_requests, "run_for_playlist") as run_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        self.assertEqual(run_mock.call_count, 2)

    def test_none_of_the_five_calls_mocks_suppresses_another(self):
        with mock.patch.object(playlist_sync.mirror, "write_mirror") as fs_mock, \
             mock.patch.object(playlist_sync.mirror_subsonic, "write_mirror") as subsonic_mock, \
             mock.patch.object(playlist_sync.mirror_jellyfin, "write_mirror") as jellyfin_mock, \
             mock.patch.object(playlist_sync.mirror_emby, "write_mirror") as emby_mock, \
             mock.patch.object(playlist_sync.lidarr_requests, "run_for_playlist") as lidarr_mock:
            playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Mix")
        fs_mock.assert_called_once()
        subsonic_mock.assert_called_once()
        jellyfin_mock.assert_called_once()
        emby_mock.assert_called_once()
        lidarr_mock.assert_called_once()


class StaleCleanupMirrorTests(unittest.TestCase):
    """#285: the stale-playlist cleanup in _sync_playlists deletes a
    removed playlist's mirror file (marker-checked, via mirror.py) BEFORE
    the row itself is deleted. Uses a temp-file DB, same as
    UpgradeSyncIntegrationTests above — sync_playlists() opens its own
    db.get_conn(), it doesn't accept one."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="trobar-test-mirror-cleanup-"))
        self._prev_db_path, self._prev_data_dir = db.DB_PATH, db.DATA_DIR
        db.DATA_DIR = self._tmp
        db.DB_PATH = self._tmp / "test.db"
        db.init_db()
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self._tmp / "no-such-music"))
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH, db.DATA_DIR = self._prev_db_path, self._prev_data_dir
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def test_stale_removal_calls_delete_mirror_before_the_row_is_gone(self):
        provider = _FullStubProvider(
            [{"id": "src1", "title": "Mix"}],
            [{"position": 0, "artist": "A", "title": "T", "path": None, "album": None}],
        )
        playlist_sync.sync_playlists(provider, "subsonic")
        conn = db.get_conn()
        playlist_id = conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        conn.close()

        seen_row_still_present = []

        def _record(c, pid):
            row = c.execute("SELECT id FROM playlists WHERE id = ?", (pid,)).fetchone()
            seen_row_still_present.append(row is not None)

        # Second sync: the provider no longer lists "Mix" at all -> stale.
        gone_provider = _FullStubProvider([], [])
        with mock.patch.object(playlist_sync.mirror, "delete_mirror", side_effect=_record) as delete_mock:
            result = playlist_sync.sync_playlists(gone_provider, "subsonic")

        self.assertEqual(result["removed"], 1)
        delete_mock.assert_called_once_with(mock.ANY, playlist_id)
        self.assertEqual(seen_row_still_present, [True])
        conn = db.get_conn()
        self.assertIsNone(conn.execute("SELECT id FROM playlists WHERE id = ?", (playlist_id,)).fetchone())
        conn.close()

    def test_stale_removal_calls_delete_mirror_subsonic_before_the_row_is_gone(self):
        # #189: the second sink's cleanup gets the same BEFORE-the-row-goes
        # ordering guarantee as mirror.delete_mirror above.
        provider = _FullStubProvider(
            [{"id": "src1", "title": "Mix"}],
            [{"position": 0, "artist": "A", "title": "T", "path": None, "album": None}],
        )
        playlist_sync.sync_playlists(provider, "subsonic")
        conn = db.get_conn()
        playlist_id = conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        conn.close()

        seen_row_still_present = []

        def _record(c, pid):
            row = c.execute("SELECT id FROM playlists WHERE id = ?", (pid,)).fetchone()
            seen_row_still_present.append(row is not None)

        gone_provider = _FullStubProvider([], [])
        with mock.patch.object(playlist_sync.mirror_subsonic, "delete_mirror", side_effect=_record) as delete_mock:
            result = playlist_sync.sync_playlists(gone_provider, "subsonic")

        self.assertEqual(result["removed"], 1)
        delete_mock.assert_called_once_with(mock.ANY, playlist_id)
        self.assertEqual(seen_row_still_present, [True])

    def test_stale_removal_calls_delete_mirror_jellyfin_before_the_row_is_gone(self):
        # #189: the third sink's cleanup gets the same BEFORE-the-row-goes
        # ordering guarantee as mirror.delete_mirror above.
        provider = _FullStubProvider(
            [{"id": "src1", "title": "Mix"}],
            [{"position": 0, "artist": "A", "title": "T", "path": None, "album": None}],
        )
        playlist_sync.sync_playlists(provider, "subsonic")
        conn = db.get_conn()
        playlist_id = conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        conn.close()

        seen_row_still_present = []

        def _record(c, pid):
            row = c.execute("SELECT id FROM playlists WHERE id = ?", (pid,)).fetchone()
            seen_row_still_present.append(row is not None)

        gone_provider = _FullStubProvider([], [])
        with mock.patch.object(playlist_sync.mirror_jellyfin, "delete_mirror", side_effect=_record) as delete_mock:
            result = playlist_sync.sync_playlists(gone_provider, "subsonic")

        self.assertEqual(result["removed"], 1)
        delete_mock.assert_called_once_with(mock.ANY, playlist_id)
        self.assertEqual(seen_row_still_present, [True])

    def test_stale_removal_calls_delete_mirror_emby_before_the_row_is_gone(self):
        # #189: the fourth (and per the RFC, final) sink's cleanup gets the
        # same BEFORE-the-row-goes ordering guarantee as mirror.delete_mirror
        # above.
        provider = _FullStubProvider(
            [{"id": "src1", "title": "Mix"}],
            [{"position": 0, "artist": "A", "title": "T", "path": None, "album": None}],
        )
        playlist_sync.sync_playlists(provider, "subsonic")
        conn = db.get_conn()
        playlist_id = conn.execute("SELECT id FROM playlists WHERE title = 'Mix'").fetchone()["id"]
        conn.close()

        seen_row_still_present = []

        def _record(c, pid):
            row = c.execute("SELECT id FROM playlists WHERE id = ?", (pid,)).fetchone()
            seen_row_still_present.append(row is not None)

        gone_provider = _FullStubProvider([], [])
        with mock.patch.object(playlist_sync.mirror_emby, "delete_mirror", side_effect=_record) as delete_mock:
            result = playlist_sync.sync_playlists(gone_provider, "subsonic")

        self.assertEqual(result["removed"], 1)
        delete_mock.assert_called_once_with(mock.ANY, playlist_id)
        self.assertEqual(seen_row_still_present, [True])


def _make_track(conn: sqlite3.Connection, relative_path: str) -> int:
    cur = conn.execute(
        "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
        "VALUES (?, 'Artist', 'Album', 'Title', 1000, 0)",
        (relative_path,),
    )
    conn.commit()
    return sync_state._new_id(cur)


def _make_playlist_with_tracks(conn: sqlite3.Connection, title: str, source_provider: str | None, track_ids) -> int:
    cur = conn.execute(
        "INSERT INTO playlists (title, source_provider, last_synced_at) VALUES (?, ?, datetime('now'))",
        (title, source_provider),
    )
    playlist_id = sync_state._new_id(cur)
    for i, track_id in enumerate(track_ids):
        conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, ?, 'Artist', 'Title', ?)",
            (playlist_id, i, track_id),
        )
    conn.commit()
    return playlist_id


class InferRoonPlaylistOriginsTests(unittest.TestCase):
    """#26: Roon's own API can't tell a native playlist from a
    Tidal-imported one — this diffs resolved track sets instead of
    relying on title at all. See _infer_roon_playlist_origins()'s own
    docstring for the actual scope this lands at (different-titled pairs
    only, not the literal #23 same-title-on-Roon's-own-side case, which
    this title-keyed sync model can't represent as two rows in the first
    place) — test_same_title_roon_and_tidal_entries_cannot_both_exist
    below documents that limitation directly."""

    def setUp(self):
        self.conn = _make_conn()
        self.tracks = [_make_track(self.conn, f"artist/album/{i:02d}.flac") for i in range(10)]

    def test_substantial_overlap_marks_roon_playlist_as_tidal_origin(self):
        _make_playlist_with_tracks(self.conn, "Roon Party", "roon", self.tracks[0:5])
        _make_playlist_with_tracks(self.conn, "My Tidal Mix", "tidal", self.tracks[0:5])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE title = 'Roon Party'"
        ).fetchone()
        self.assertEqual(row["inferred_origin_provider"], "tidal")

    def test_overlap_marks_roon_playlist_as_spotify_origin(self):
        # #147: the inference now attributes any directly-linked provider,
        # not just Tidal.
        roon_id = _make_playlist_with_tracks(self.conn, "Roon Party", "roon", self.tracks[0:5])
        sp_id = _make_playlist_with_tracks(self.conn, "My Spotify Mix", "spotify", self.tracks[0:5])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        row = self.conn.execute(
            "SELECT inferred_origin_provider, golden_source_id FROM playlists WHERE id = ?",
            (roon_id,)).fetchone()
        self.assertEqual(row["inferred_origin_provider"], "spotify")
        self.assertEqual(row["golden_source_id"], sp_id)  # #81 link to the matched row

    def test_best_overlap_wins_across_two_providers(self):
        # #147 tie-break: Roon [0..5) overlaps a Tidal row at 3/5 (0.6, the
        # floor) and a Spotify row at 5/5 — the higher ratio (Spotify) wins.
        roon_id = _make_playlist_with_tracks(self.conn, "Roon Party", "roon", self.tracks[0:5])
        _make_playlist_with_tracks(self.conn, "Tidal Party", "tidal", self.tracks[0:3] + self.tracks[7:9])
        _make_playlist_with_tracks(self.conn, "Spotify Party", "spotify", self.tracks[0:5])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE id = ?", (roon_id,)).fetchone()
        self.assertEqual(row["inferred_origin_provider"], "spotify")

    def test_no_overlap_leaves_it_unset(self):
        _make_playlist_with_tracks(self.conn, "Roon Party", "roon", self.tracks[0:5])
        _make_playlist_with_tracks(self.conn, "My Tidal Mix", "tidal", self.tracks[5:10])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE title = 'Roon Party'"
        ).fetchone()
        self.assertIsNone(row["inferred_origin_provider"])

    def test_small_overlap_below_threshold_is_not_a_match(self):
        # 2 shared tracks out of 5 — below both the absolute (3) and
        # ratio (0.6) thresholds; two unrelated playlists sharing a
        # couple of generic/popular tracks shouldn't read as "imported".
        _make_playlist_with_tracks(self.conn, "Roon Party", "roon", self.tracks[0:5])
        _make_playlist_with_tracks(self.conn, "My Tidal Mix", "tidal", self.tracks[3:5] + self.tracks[5:8])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE title = 'Roon Party'"
        ).fetchone()
        self.assertIsNone(row["inferred_origin_provider"])

    def test_title_is_never_consulted_two_differently_titled_roon_rows(self):
        # NOT the literal #23 "two Roon playlists both named 'Party'"
        # scenario — that can't be represented as two rows at all here
        # (playlists.title is UNIQUE, and every provider client in this
        # codebase, e.g. roon_client.get_playlist_tracks(title), is
        # title-keyed, so two same-titled entries from any source
        # collapse into one row before this function ever runs — see
        # this function's own docstring). What this actually proves: the
        # comparison is purely track-based, with title having zero
        # influence on the result — two Roon rows with different,
        # unrelated titles, only one of which actually matches the
        # linked Tidal playlist's tracks.
        native_id = _make_playlist_with_tracks(self.conn, "Roon Only Mix", "roon", self.tracks[5:9])
        imported_id = _make_playlist_with_tracks(self.conn, "Some Roon Title", "roon", self.tracks[0:5])
        _make_playlist_with_tracks(self.conn, "My Tidal Party", "tidal", self.tracks[0:5])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        native_row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE id = ?", (native_id,)
        ).fetchone()
        imported_row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE id = ?", (imported_id,)
        ).fetchone()
        self.assertIsNone(native_row["inferred_origin_provider"])
        self.assertEqual(imported_row["inferred_origin_provider"], "tidal")

    def test_same_title_roon_and_tidal_entries_now_coexist(self):
        # #75 reversed the old limitation this test used to document: a Roon
        # "Party" and a Tidal "Party" now coexist as two separate rows
        # (different source_provider => different composite key), instead of
        # the second overwriting the first. So the inference DOES have two
        # rows to compare when the titles match across providers.
        provider = _StubProvider(_ONE_TRACK)
        playlist_sync._sync_one_playlist(self.conn, provider, "roon", "Party")
        playlist_sync._sync_one_playlist(self.conn, provider, "tidal", "Party")
        rows = {r["source_provider"] for r in self.conn.execute(
            "SELECT source_provider FROM playlists WHERE title = 'Party'")}
        self.assertEqual(rows, {"roon", "tidal"})

    def test_small_tidal_playlist_fully_inside_a_large_roon_playlist_is_not_a_match(self):
        # The min()-denominator bug flagged in review: a small Tidal
        # playlist whose tracks are a subset of a much larger Roon
        # playlist isn't evidence the *large* Roon playlist was
        # "imported from" the small one — most of the large playlist's
        # own tracks are unexplained by it. Ratio must be normalized by
        # the Roon side specifically, not min(roon, tidal).
        roon_id = _make_playlist_with_tracks(self.conn, "Roon Everything", "roon", self.tracks)  # 10 tracks
        _make_playlist_with_tracks(self.conn, "Tidal Faves", "tidal", self.tracks[0:3])  # fully contained
        playlist_sync._infer_roon_playlist_origins(self.conn)
        row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE id = ?", (roon_id,)
        ).fetchone()
        self.assertIsNone(row["inferred_origin_provider"])

    def test_recompute_clears_a_stale_inference(self):
        # e.g. Tidal got disconnected since the last sync, or the
        # playlist's tracks genuinely diverged — a prior match must not
        # linger forever.
        roon_id = _make_playlist_with_tracks(self.conn, "Roon Party", "roon", self.tracks[0:5])
        self.conn.execute(
            "UPDATE playlists SET inferred_origin_provider = 'tidal' WHERE id = ?", (roon_id,)
        )
        self.conn.commit()
        playlist_sync._infer_roon_playlist_origins(self.conn)
        row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE id = ?", (roon_id,)
        ).fetchone()
        self.assertIsNone(row["inferred_origin_provider"])

    def test_no_tidal_playlists_is_a_clean_no_op(self):
        _make_playlist_with_tracks(self.conn, "Roon Party", "roon", self.tracks[0:5])
        playlist_sync._infer_roon_playlist_origins(self.conn)  # must not raise
        row = self.conn.execute(
            "SELECT inferred_origin_provider FROM playlists WHERE title = 'Roon Party'"
        ).fetchone()
        self.assertIsNone(row["inferred_origin_provider"])

    # --- #81 golden-source link (golden_source_id) ---

    def test_track_overlap_records_golden_source_id(self):
        roon_id = _make_playlist_with_tracks(self.conn, "Roon Mix", "roon", self.tracks[0:5])
        tidal_id = _make_playlist_with_tracks(self.conn, "Tidal Mix", "tidal", self.tracks[0:5])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT golden_source_id FROM playlists WHERE id=?", (roon_id,)).fetchone()[0],
            tidal_id)

    def test_exact_title_match_links_even_with_no_track_data(self):
        # #81: title alone is strong evidence — a Roon "Party" and a Tidal
        # "Party" with no locally-resolved tracks still link (nothing to
        # contradict the title).
        roon_id = _make_playlist_with_tracks(self.conn, "Party", "roon", [])
        tidal_id = _make_playlist_with_tracks(self.conn, "Party", "tidal", [])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT golden_source_id FROM playlists WHERE id=?", (roon_id,)).fetchone()[0],
            tidal_id)

    def test_same_title_but_contradicting_tracks_is_not_linked(self):
        # Two genuinely different playlists that merely share a name (#23):
        # both have enough resolved tracks to judge, and they don't overlap
        # → NOT the same playlist, no golden link.
        roon_id = _make_playlist_with_tracks(self.conn, "Party", "roon", self.tracks[0:5])
        _make_playlist_with_tracks(self.conn, "Party", "tidal", self.tracks[5:10])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        row = self.conn.execute(
            "SELECT golden_source_id, inferred_origin_provider FROM playlists WHERE id=?", (roon_id,)).fetchone()
        self.assertIsNone(row["golden_source_id"])
        self.assertIsNone(row["inferred_origin_provider"])

    def test_tidal_only_playlist_leaves_no_golden_link_anywhere(self):
        # A Tidal-only playlist (no Roon counterpart) must not cause any
        # Roon row to be linked to it — the correctness crux (#81): it stays
        # private, never surfaced via a golden badge.
        roon_id = _make_playlist_with_tracks(self.conn, "Roon Only", "roon", self.tracks[0:5])
        _make_playlist_with_tracks(self.conn, "Private Tidal", "tidal", self.tracks[5:10])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        self.assertIsNone(
            self.conn.execute("SELECT golden_source_id FROM playlists WHERE id=?", (roon_id,)).fetchone()[0])

    def test_golden_link_cleared_when_tidal_row_disconnects(self):
        roon_id = _make_playlist_with_tracks(self.conn, "Mix", "roon", self.tracks[0:5])
        tidal_id = _make_playlist_with_tracks(self.conn, "Mix", "tidal", self.tracks[0:5])
        playlist_sync._infer_roon_playlist_origins(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT golden_source_id FROM playlists WHERE id=?", (roon_id,)).fetchone()[0],
            tidal_id)
        # Tidal disconnected → its row gone; recompute clears the stale link.
        self.conn.execute("DELETE FROM playlists WHERE id=?", (tidal_id,))
        self.conn.commit()
        playlist_sync._infer_roon_playlist_origins(self.conn)
        self.assertIsNone(
            self.conn.execute("SELECT golden_source_id FROM playlists WHERE id=?", (roon_id,)).fetchone()[0])


class CompositeKeyUpsertTests(unittest.TestCase):
    """#75: _sync_one_playlist keys on (source_provider, source_playlist_id)
    for id-exposing providers, (source_provider, title) for Roon."""

    def setUp(self):
        self.conn = _make_conn()
        self.provider = _StubProvider(_ONE_TRACK)

    def _count(self, where: str, params) -> int:
        return self.conn.execute(f"SELECT COUNT(*) AS n FROM playlists WHERE {where}", params).fetchone()["n"]

    def test_two_same_titled_id_provider_playlists_coexist(self):
        # Two Subsonic playlists both named "Party", distinct ids — the
        # exact same-provider collision that used to collapse to one row.
        playlist_sync._sync_one_playlist(self.conn, self.provider, "subsonic", "Party", source_playlist_id="id1")
        playlist_sync._sync_one_playlist(self.conn, self.provider, "subsonic", "Party", source_playlist_id="id2")
        ids = {r["source_playlist_id"] for r in self.conn.execute(
            "SELECT source_playlist_id FROM playlists WHERE source_provider='subsonic' AND title='Party'")}
        self.assertEqual(ids, {"id1", "id2"})

    def test_legacy_null_id_row_is_adopted_in_place_on_first_upgrade_sync(self):
        # #85: a pre-#75 row has source_playlist_id NULL (ids were never
        # stored). The first post-upgrade sync must ADOPT it (same row id,
        # id stamped on), not insert a new row + strand the old one — which
        # would revoke its selections and reset its shared flag downstream.
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared) "
            "VALUES ('Legacy', 'subsonic', NULL, NULL, 0)")  # NULL id, marked private
        legacy_id = playlist_sync.sync_state._new_id(cur)
        self.conn.commit()

        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "subsonic", "Legacy", source_playlist_id="realid")

        rows = self.conn.execute(
            "SELECT id, source_playlist_id, shared FROM playlists WHERE source_provider='subsonic'").fetchall()
        self.assertEqual(len(rows), 1)                      # adopted, not duplicated
        self.assertEqual(rows[0]["id"], legacy_id)          # SAME row id — selections/ownership survive
        self.assertEqual(rows[0]["source_playlist_id"], "realid")  # id now stamped on
        self.assertEqual(rows[0]["shared"], 0)              # private choice preserved

    def test_legacy_owned_private_tidal_row_keeps_privacy_on_adoption(self):
        # The privacy-sensitive #85 case: a Tidal playlist is *owned* and
        # can be genuinely private. Adopting it under the same owner must
        # not trip the #70 owner-change shared-reset — the private flag has
        # to survive the upgrade.
        owner = _make_user(self.conn, "owner")
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared) "
            "VALUES ('My Mix', 'tidal', NULL, ?, 0)", (owner,))
        legacy_id = playlist_sync.sync_state._new_id(cur)
        self.conn.commit()

        playlist_sync._sync_one_playlist(
            self.conn, self.provider, "tidal", "My Mix", source_playlist_id="tid1", owner_user_id=owner)

        row = self.conn.execute(
            "SELECT id, source_playlist_id, owner_user_id, shared FROM playlists WHERE source_provider='tidal'"
        ).fetchone()
        self.assertEqual(row["id"], legacy_id)
        self.assertEqual(row["source_playlist_id"], "tid1")
        self.assertEqual(row["owner_user_id"], owner)
        self.assertEqual(row["shared"], 0)  # still private — not reset

    def test_same_id_resyncs_the_same_row(self):
        playlist_sync._sync_one_playlist(self.conn, self.provider, "subsonic", "Party", source_playlist_id="id1")
        playlist_sync._sync_one_playlist(self.conn, self.provider, "subsonic", "Party", source_playlist_id="id1")
        self.assertEqual(self._count("source_provider='subsonic' AND source_playlist_id='id1'", ()), 1)

    def test_id_keyed_rename_updates_title_in_place(self):
        # Provider renamed the playlist but kept its id — the title follows
        # the id, no orphan second row (a real win over title-keying).
        playlist_sync._sync_one_playlist(self.conn, self.provider, "subsonic", "Old Name", source_playlist_id="id1")
        pid = self.conn.execute("SELECT id FROM playlists WHERE source_playlist_id='id1'").fetchone()["id"]
        playlist_sync._sync_one_playlist(self.conn, self.provider, "subsonic", "New Name", source_playlist_id="id1")
        rows = self.conn.execute("SELECT id, title FROM playlists WHERE source_provider='subsonic'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], pid)          # same row, id preserved
        self.assertEqual(rows[0]["title"], "New Name")

    def test_roon_two_same_title_still_collapse(self):
        # Roon has no stable id, so it stays title-keyed and two same-titled
        # Roon playlists still collapse — the honest #75 limitation.
        playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Party")
        playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Party")
        self.assertEqual(self._count("source_provider='roon' AND title='Party'", ()), 1)

    def test_cross_provider_same_title_coexists(self):
        playlist_sync._sync_one_playlist(self.conn, self.provider, "roon", "Party")
        playlist_sync._sync_one_playlist(self.conn, self.provider, "subsonic", "Party", source_playlist_id="s1")
        self.assertEqual(self._count("title='Party'", ()), 2)


class CompositeKeyMigrationTests(unittest.TestCase):
    """#75: _migrate_playlists_composite_key rebuilds a pre-#75 playlists
    table (global title UNIQUE, no source_playlist_id) into the new shape
    without losing data or renumbering ids."""

    def _old_shape_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # The exact pre-#75 shape: title UNIQUE, migration columns present,
        # no source_playlist_id, plus the FK'd children that reference it.
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);"
            "CREATE TABLE playlists (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT UNIQUE NOT NULL,"
            " source_provider TEXT, owner_user_id INTEGER, shared INTEGER NOT NULL DEFAULT 1,"
            " inferred_origin_provider TEXT, last_synced_at TEXT);"
            "CREATE TABLE playlist_tracks (id INTEGER PRIMARY KEY, "
            " playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE, position INT);"
            "CREATE TABLE selections (id INTEGER PRIMARY KEY, type TEXT, target TEXT, created_by_user_id INTEGER);"
        )
        conn.execute("INSERT INTO playlists (id, title, source_provider, shared) VALUES (5, 'Party', 'roon', 0)")
        conn.execute("INSERT INTO playlists (id, title, source_provider, shared) VALUES (9, 'Chill', 'subsonic', 1)")
        conn.execute("INSERT INTO playlist_tracks (playlist_id, position) VALUES (5, 0)")
        conn.execute("INSERT INTO selections (type, target, created_by_user_id) VALUES ('playlist', '9', 1)")
        # _run_migrations adds all newer columns before the rebuild runs.
        conn.execute("ALTER TABLE playlists ADD COLUMN source_playlist_id TEXT")
        conn.execute("ALTER TABLE playlists ADD COLUMN golden_source_id INTEGER "
                     "REFERENCES playlists(id) ON DELETE SET NULL")
        conn.commit()
        return conn

    def test_rebuild_drops_unique_preserves_ids_and_children(self):
        conn = self._old_shape_conn()
        self.assertIn("UNIQUE", conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='playlists'").fetchone()["sql"].upper())

        db._migrate_playlists_composite_key(conn)

        # UNIQUE gone; ids + shared preserved exactly.
        self.assertNotIn("UNIQUE", conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='playlists'").fetchone()["sql"].upper())
        rows = {r["id"]: (r["title"], r["shared"]) for r in conn.execute("SELECT id, title, shared FROM playlists")}
        self.assertEqual(rows, {5: ("Party", 0), 9: ("Chill", 1)})
        # FK child + id-as-string selection target still point at the right ids.
        self.assertEqual(conn.execute("SELECT playlist_id FROM playlist_tracks").fetchone()["playlist_id"], 5)
        self.assertEqual(conn.execute("SELECT target FROM selections").fetchone()["target"], "9")

    def test_rebuild_is_idempotent(self):
        conn = self._old_shape_conn()
        db._migrate_playlists_composite_key(conn)
        db._migrate_playlists_composite_key(conn)  # second run must be a clean no-op
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM playlists").fetchone()["n"], 2)

    def test_composite_uniqueness_is_enforced_after_migration(self):
        conn = self._old_shape_conn()
        db._migrate_playlists_composite_key(conn)
        # (source_provider, title) unique for id-NULL (Roon) rows.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO playlists (title, source_provider, source_playlist_id) VALUES ('Party','roon',NULL)")

    def test_survives_preexisting_fk_orphans(self):
        # Regression: a real DB can carry a stray orphan (a playlist_tracks
        # row whose playlist_id no longer exists, or an unrelated dangling
        # FK). The migration must NOT abort on those — it preserves ids, so
        # it can't create new orphans, and bricking startup over cruft it
        # didn't cause would be a nasty upgrade failure. (An earlier draft's
        # global PRAGMA foreign_key_check did exactly that against the dev DB.)
        conn = self._old_shape_conn()
        conn.execute("PRAGMA foreign_keys=OFF")  # how orphans arise in reality
        conn.execute("INSERT INTO playlist_tracks (playlist_id, position) VALUES (999, 0)")  # orphan
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        db._migrate_playlists_composite_key(conn)  # must not raise
        # Real rows still intact; the orphan is left as-is (not this
        # migration's job to clean up), same as before.
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM playlists").fetchone()["n"], 2)
        self.assertEqual(
            conn.execute("SELECT playlist_id FROM playlist_tracks WHERE playlist_id=5").fetchone()["playlist_id"], 5)


class _FullStubProvider:
    """Also implements list_playlists() (unlike _StubProvider), so it can
    drive the real sync_playlists() loop end to end."""

    def __init__(self, playlists, tracks):
        self._playlists = playlists  # [{"id", "title"}, ...]
        self._tracks = tracks

    def list_playlists(self):
        return {"status": "ok", "playlists": self._playlists}

    def get_playlist_tracks(self, title, source_playlist_id=None, **_kwargs):
        return {"status": "ok", "tracks": self._tracks}


class UpgradeSyncIntegrationTests(unittest.TestCase):
    """#85 end to end: the FULL sync_playlists() run (including its
    stale-row cleanup) must not revoke a legacy id-provider playlist's
    selection or reset its privacy on the first post-upgrade sync. Uses a
    temp-file DB because sync_playlists() opens its own db.get_conn()."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="trobar-test-upgrade-"))
        self._prev_db_path, self._prev_data_dir = db.DB_PATH, db.DATA_DIR
        db.DATA_DIR = self._tmp
        db.DB_PATH = self._tmp / "test.db"
        db.init_db()
        # Point the filesystem provider at a nonexistent root so its merge
        # is a clean no-op (not_paired) and doesn't interfere.
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self._tmp / "no-such-music"))
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH, db.DATA_DIR = self._prev_db_path, self._prev_data_dir
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def test_first_post_upgrade_sync_keeps_selection_and_private_flag(self):
        conn = db.get_conn()
        user_id = _make_user(conn, "alice")
        device_id, _tok = sync_state.create_device(conn, user_id, "phone")
        track_id = _make_track(conn, "artist/a.flac")
        # A legacy Subsonic playlist: source_playlist_id NULL (pre-#75),
        # marked private, with a track and a device selection attached.
        cur = conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, shared, last_synced_at) "
            "VALUES ('Legacy', 'subsonic', NULL, 0, datetime('now'))")
        legacy_pid = sync_state._new_id(cur)
        conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, 0, 'A', 'T', ?)", (legacy_pid, track_id))
        conn.commit()
        sel_id = sync_state.create_selection(conn, "playlist", str(legacy_pid), user_id, [device_id])
        conn.close()

        provider = _FullStubProvider(
            [{"id": "realid", "title": "Legacy"}],
            [{"position": 0, "artist": "A", "title": "T", "path": None, "album": None}])
        result = playlist_sync.sync_playlists(provider, "subsonic")

        self.assertEqual(result["removed"], 0)  # nothing stale-deleted
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT id, source_playlist_id, shared FROM playlists WHERE source_provider='subsonic'").fetchall()
            self.assertEqual(len(rows), 1)                        # adopted, not duplicated
            self.assertEqual(rows[0]["id"], legacy_pid)           # same row
            self.assertEqual(rows[0]["source_playlist_id"], "realid")
            self.assertEqual(rows[0]["shared"], 0)                # privacy preserved
            # the selection was NOT revoked
            self.assertIsNotNone(conn.execute("SELECT 1 FROM selections WHERE id=?", (sel_id,)).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM selection_devices WHERE selection_id=? AND device_id=?",
                (sel_id, device_id)).fetchone())
        finally:
            conn.close()

    def test_ghost_reclaimed_by_a_real_sync_but_selected_ghost_survives(self):
        # #93 end to end: a full sync_playlists() run reclaims an orphaned
        # NULL-source_provider ghost the provider stale-scan can't see, while a
        # ghost still backing a selection is preserved (its selection intact).
        conn = db.get_conn()
        user_id = _make_user(conn, "alice")
        device_id, _tok = sync_state.create_device(conn, user_id, "phone")
        orphan = _make_playlist_with_tracks(conn, "Orphan", None, [])
        selected = _make_playlist_with_tracks(conn, "Selected", None, [])
        sel_id = sync_state.create_selection(conn, "playlist", str(selected), user_id, [device_id])
        conn.commit()
        conn.close()

        result = playlist_sync.sync_playlists(_FullStubProvider([], []), "subsonic")
        self.assertEqual(result["removed"], 1)  # only the orphaned ghost

        conn = db.get_conn()
        try:
            self.assertIsNone(conn.execute("SELECT 1 FROM playlists WHERE id=?", (orphan,)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM playlists WHERE id=?", (selected,)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM selections WHERE id=?", (sel_id,)).fetchone())
        finally:
            conn.close()


class GhostPlaylistCleanupTests(unittest.TestCase):
    """#93: _cleanup_ghost_playlists() reclaims legacy NULL-source_provider
    rows the provider stale-scan can't see — but selection-safe (#85): a ghost
    still backing a playlist selection is preserved, never revoked."""

    def setUp(self):
        self.conn = _make_conn()

    def tearDown(self):
        self.conn.close()

    def _row_exists(self, pid):
        return self.conn.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone() is not None

    def test_orphaned_ghost_is_removed(self):
        tid = _make_track(self.conn, "artist/a.flac")
        ghost = _make_playlist_with_tracks(self.conn, "Ghost", None, [tid])
        removed = playlist_sync._cleanup_ghost_playlists(self.conn)
        self.assertEqual(removed, 1)
        self.assertFalse(self._row_exists(ghost))
        # playlist_tracks cascade-deleted with it
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id=?", (ghost,)).fetchone())

    def test_ghost_backing_a_selection_is_preserved(self):
        user_id = _make_user(self.conn, "alice")
        device_id, _tok = sync_state.create_device(self.conn, user_id, "phone")
        tid = _make_track(self.conn, "artist/a.flac")
        ghost = _make_playlist_with_tracks(self.conn, "Ghost", None, [tid])
        sel_id = sync_state.create_selection(self.conn, "playlist", str(ghost), user_id, [device_id])
        removed = playlist_sync._cleanup_ghost_playlists(self.conn)
        self.assertEqual(removed, 0)
        self.assertTrue(self._row_exists(ghost))                      # row kept
        self.assertIsNotNone(self.conn.execute(                        # selection NOT revoked
            "SELECT 1 FROM selections WHERE id=?", (sel_id,)).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id=?", (ghost,)).fetchone())

    def test_provider_backed_rows_are_untouched(self):
        keep = _make_playlist_with_tracks(self.conn, "Real", "subsonic", [])
        removed = playlist_sync._cleanup_ghost_playlists(self.conn)
        self.assertEqual(removed, 0)
        self.assertTrue(self._row_exists(keep))

    def test_mixed_only_orphaned_ghosts_go(self):
        user_id = _make_user(self.conn, "alice")
        device_id, _tok = sync_state.create_device(self.conn, user_id, "phone")
        orphan = _make_playlist_with_tracks(self.conn, "Orphan", None, [])
        selected = _make_playlist_with_tracks(self.conn, "Selected", None, [])
        sync_state.create_selection(self.conn, "playlist", str(selected), user_id, [device_id])
        real = _make_playlist_with_tracks(self.conn, "Real", "roon", [])
        removed = playlist_sync._cleanup_ghost_playlists(self.conn)
        self.assertEqual(removed, 1)
        self.assertFalse(self._row_exists(orphan))
        self.assertTrue(self._row_exists(selected))
        self.assertTrue(self._row_exists(real))


class TidalPerOwnerCleanupTests(unittest.TestCase):
    """#71: the stale-cleanup pass filters Tidal rows by owner_user_id, so
    one linked user's failed fetch only protects THAT user's rows — other
    users' successfully-synced Tidal playlists are still cleaned on the same
    run (replacing #67's all-or-nothing tidal_all_ok gate). Full-run test
    against a temp-file DB, real cleanup pass, mocked tidal_client."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="trobar-test-tidal71-"))
        self._prev_db_path, self._prev_data_dir = db.DB_PATH, db.DATA_DIR
        db.DATA_DIR = self._tmp
        db.DB_PATH = self._tmp / "test.db"
        db.init_db()
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self._tmp / "no-such-music"))
        db.set_config(conn, "tidal_client_id", "cid")
        db.set_config(conn, "tidal_client_secret", "csecret")
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH, db.DATA_DIR = self._prev_db_path, self._prev_data_dir
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def _seed_tidal_row(self, conn, title, src_id, owner_id):
        cur = conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, last_synced_at) "
            "VALUES (?, 'tidal', ?, ?, datetime('now'))", (title, src_id, owner_id))
        return sync_state._new_id(cur)

    def test_failed_users_rows_survive_while_healthy_users_stale_rows_are_cleaned(self):
        from unittest import mock

        conn = db.get_conn()
        alice = _make_user(conn, "alice")
        bob = _make_user(conn, "bob")
        conn.execute("UPDATE users SET tidal_refresh_token='alice-rt', tidal_user_id='alice-tid' WHERE id=?", (alice,))
        conn.execute("UPDATE users SET tidal_refresh_token='bob-rt', tidal_user_id='bob-tid' WHERE id=?", (bob,))
        # Both users have a pre-existing Tidal playlist that will NOT be
        # relisted this run — so both are "stale" candidates.
        alice_pid = self._seed_tidal_row(conn, "Alice Old", "a-old", alice)
        bob_pid = self._seed_tidal_row(conn, "Bob Old", "b-old", bob)
        conn.commit()
        conn.close()

        def fake_refresh(client_id, client_secret, refresh_token):
            if refresh_token == "alice-rt":
                raise tidal_client.TidalTransientError("network down")
            return ("bob-access", "bob-rt")

        def fake_list(access_token, tidal_user_id):
            # Bob's fetch succeeds but lists nothing — his old row is
            # genuinely stale this run.
            return {"status": "ok", "playlists": []}

        provider = _FullStubProvider([], [])
        with mock.patch.object(tidal_client, "refresh_access_token", side_effect=fake_refresh), \
                mock.patch.object(tidal_client, "list_playlists", side_effect=fake_list):
            result = playlist_sync.sync_playlists(provider, "subsonic")

        self.assertEqual(result["removed"], 1)  # only Bob's stale row
        conn = db.get_conn()
        try:
            # Alice's row survives — her fetch failed, so its absence from
            # this run's listing is not evidence it's gone.
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM playlists WHERE id=?", (alice_pid,)).fetchone())
            # Bob's row is cleaned — his fetch succeeded and didn't relist it.
            self.assertIsNone(
                conn.execute("SELECT 1 FROM playlists WHERE id=?", (bob_pid,)).fetchone())
        finally:
            conn.close()

    def test_auth_revocation_protects_that_users_rows_too(self):
        from unittest import mock

        conn = db.get_conn()
        alice = _make_user(conn, "alice")
        conn.execute("UPDATE users SET tidal_refresh_token='alice-rt', tidal_user_id='alice-tid' WHERE id=?", (alice,))
        alice_pid = self._seed_tidal_row(conn, "Alice Old", "a-old", alice)
        conn.commit()
        conn.close()

        def fake_refresh(client_id, client_secret, refresh_token):
            raise tidal_client.TidalAuthError("revoked")

        provider = _FullStubProvider([], [])
        with mock.patch.object(tidal_client, "refresh_access_token", side_effect=fake_refresh):
            result = playlist_sync.sync_playlists(provider, "subsonic")

        self.assertEqual(result["removed"], 0)  # revoked != gone; row protected
        conn = db.get_conn()
        try:
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM playlists WHERE id=?", (alice_pid,)).fetchone())
            # The stale link was cleared so the UI prompts a reconnect.
            self.assertIsNone(
                conn.execute("SELECT tidal_refresh_token FROM users WHERE id=?", (alice,)).fetchone()[0])
        finally:
            conn.close()


class _FailingPrimaryProvider:
    """A primary provider whose listing fails (e.g. not paired yet after a
    restart) — used to prove the independent secondary merges still run (#128)."""

    def list_playlists(self):
        return {"status": "error", "reason": "not_paired"}

    def get_playlist_tracks(self, title, source_playlist_id=None, **_kwargs):
        return {"status": "error", "reason": "not_paired"}


class PrimaryListingFailureTests(unittest.TestCase):
    """#128: a primary-provider listing failure must not short-circuit the
    independent secondary merges (Tidal here), nor delete the primary's own
    existing playlists (they weren't authoritatively re-listed). Full run
    against a temp-file DB, mocked tidal_client."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="trobar-test-128-"))
        self._prev_db_path, self._prev_data_dir = db.DB_PATH, db.DATA_DIR
        db.DATA_DIR = self._tmp
        db.DB_PATH = self._tmp / "test.db"
        db.init_db()
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self._tmp / "no-such-music"))
        db.set_config(conn, "tidal_client_id", "cid")
        db.set_config(conn, "tidal_client_secret", "csecret")
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH, db.DATA_DIR = self._prev_db_path, self._prev_data_dir
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def test_primary_failure_still_syncs_tidal_and_preserves_primary_rows(self):
        from unittest import mock

        conn = db.get_conn()
        alice = _make_user(conn, "alice")
        conn.execute("UPDATE users SET tidal_refresh_token='alice-rt', tidal_user_id='alice-tid' WHERE id=?", (alice,))
        # A pre-existing playlist from the (now-failing) primary provider.
        cur = conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, last_synced_at) "
            "VALUES ('Primary Old', 'subsonic', 's-old', datetime('now'))")
        subsonic_pid = sync_state._new_id(cur)
        conn.commit()
        conn.close()

        def fake_refresh(cid, csecret, rt):
            return ("alice-access", "alice-rt")

        def fake_list(access_token, tidal_user_id):
            return {"status": "ok", "playlists": [{"id": "t-1", "title": "Alice Tidal"}]}

        def fake_tracks(title, source_playlist_id=None, **_kwargs):
            return {"status": "ok", "tracks": [
                {"position": 0, "artist": "The Cardigans", "title": "Lovefool", "album": None}]}

        provider = _FailingPrimaryProvider()
        with mock.patch.object(tidal_client, "refresh_access_token", side_effect=fake_refresh), \
                mock.patch.object(tidal_client, "list_playlists", side_effect=fake_list), \
                mock.patch.object(tidal_client, "get_playlist_tracks", side_effect=fake_tracks):
            result = playlist_sync.sync_playlists(provider, "subsonic")

        # Partial result: overall ok, Tidal synced, primary failure surfaced.
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["playlists"], 1)          # the Tidal one
        self.assertEqual(result["primary_status"], "error")
        self.assertEqual(result["primary_provider"], "subsonic")
        self.assertEqual(result["primary_error"], "not_paired")
        self.assertEqual(result["removed"], 0)            # primary rows untouched

        conn = db.get_conn()
        try:
            # Tidal playlist actually landed despite the primary failing.
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM playlists WHERE source_provider='tidal' AND title='Alice Tidal'"
            ).fetchone())
            # The primary provider's pre-existing row was NOT deleted — we
            # couldn't list it, so its absence isn't evidence it's gone.
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM playlists WHERE id=?", (subsonic_pid,)).fetchone())
        finally:
            conn.close()

    def test_primary_success_omits_the_partial_result_fields(self):
        # The normal path is unchanged: no primary_status/error keys, and the
        # primary's own playlists sync as before.
        provider = _FullStubProvider(
            [{"id": "p1", "title": "OK"}],
            [{"position": 0, "artist": "A", "title": "T", "album": None}])
        result = playlist_sync.sync_playlists(provider, "subsonic")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["playlists"], 1)
        self.assertNotIn("primary_status", result)
        self.assertNotIn("primary_error", result)

    def test_primary_failure_with_no_secondary_providers_is_a_safe_noop(self):
        # #128 review: when the primary fails AND nothing else contributes
        # (no filesystem playlists, no linked Tidal user), provider_ids is
        # empty, so the stale-cleanup query renders as `source_provider IN ()`.
        # That must be a safe no-op — not a crash, and must not delete the
        # primary's own pre-existing rows.
        conn = db.get_conn()
        cur = conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, last_synced_at) "
            "VALUES ('Primary Old', 'subsonic', 's-old', datetime('now'))")
        subsonic_pid = sync_state._new_id(cur)
        conn.commit()
        conn.close()

        # No Tidal users are linked (setUp sets creds but no user rows), so
        # provider_ids stays empty on a failed primary.
        result = playlist_sync.sync_playlists(_FailingPrimaryProvider(), "subsonic")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["primary_status"], "error")
        self.assertEqual(result["playlists"], 0)
        self.assertEqual(result["removed"], 0)
        conn = db.get_conn()
        try:
            # The IN () cleanup neither crashed nor deleted the primary row.
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM playlists WHERE id=?", (subsonic_pid,)).fetchone())
        finally:
            conn.close()


class _ExplodingProvider:
    """If a job never actually gets run (or a dedupe/queue guard fails and it
    runs twice), these blow up — proving sync_playlists() was never entered."""

    def list_playlists(self):
        raise AssertionError("sync body ran when it shouldn't have")

    def get_playlist_tracks(self, *a, **k):
        raise AssertionError("sync body ran when it shouldn't have")


class WriteLockReleaseTests(unittest.TestCase):
    """#133: the sync must not hold its DB write transaction across the network
    fetches between playlists (that long-held write lock, worsened by #136's
    retry backoff, collides with the library scan and device writes). Verified
    by checking the connection isn't in a write transaction when each playlist
    is fetched."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="trobar-test-133-"))
        self._prev_db_path, self._prev_data_dir = db.DB_PATH, db.DATA_DIR
        db.DATA_DIR = self._tmp
        db.DB_PATH = self._tmp / "test.db"
        db.init_db()
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self._tmp / "no-such-music"))
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH, db.DATA_DIR = self._prev_db_path, self._prev_data_dir
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def test_no_open_write_transaction_while_fetching_each_playlist(self):
        from unittest import mock

        conn = db.get_conn()
        in_txn_at_fetch = []

        class _Provider:
            def list_playlists(self):
                return {"status": "ok", "playlists": [
                    {"id": "p1", "title": "One"}, {"id": "p2", "title": "Two"}]}

            def get_playlist_tracks(self, title, source_playlist_id=None, **_k):
                # Sampled as the very first thing _sync_one_playlist does, so it
                # reflects whether the PREVIOUS playlist's writes are still
                # uncommitted (holding the write lock).
                in_txn_at_fetch.append(conn.in_transaction)
                return {"status": "ok", "tracks": [
                    {"position": 0, "artist": "A", "title": "T", "album": None}]}

        # sync_playlists opens its own connection via db.get_conn(); hand it the
        # one we can inspect. The filesystem merge is stubbed to a no-op so it
        # doesn't open+close (our shared) connection out from under the run.
        with mock.patch.object(db, "get_conn", return_value=conn), \
                mock.patch.object(playlist_sync.filesystem_client, "list_playlists",
                                  return_value={"status": "error"}):
            result = playlist_sync.sync_playlists(_Provider(), "subsonic")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["playlists"], 2)
        # Neither fetch happens inside an open write transaction: the first
        # because nothing's written yet, the second because playlist 1 was
        # committed before fetching playlist 2. Pre-#133 the second was True.
        self.assertEqual(in_txn_at_fetch, [False, False])


class BackgroundSyncTests(unittest.TestCase):
    """#297 step 3: the sync is a JOB now — start_sync enqueues, and the
    worker runs the handler main.py wires up (_run_playlist_sync). These are
    the same properties the old lock+thread version guaranteed, re-pinned
    against the queue:

      - start_sync returns immediately and does NOT sync inline;
      - #129: a second trigger is refused while one is pending (dedupe, not a
        lock — this absorbs the old SyncConcurrencyGuardTests, whose premise
        was a lock around sync_playlists() that no longer exists);
      - sync_status reports running/last_result for the UI to poll;
      - #141: a poll during a run never reports the PREVIOUS sync's counts.

    A real (temporary) DB is needed, since the queue is a table."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="trobar-test-138-"))
        self._prev_db_path, self._prev_data_dir = db.DB_PATH, db.DATA_DIR
        db.DATA_DIR = self._tmp
        db.DB_PATH = self._tmp / "test.db"
        db.init_db()
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self._tmp / "no-such-music"))
        conn.commit()
        conn.close()
        # Wire the handler explicitly, resolving provider_id -> whatever stub
        # THIS test constructed. Production resolves it via main.py's
        # _PROVIDERS (real provider client modules), which this module
        # deliberately doesn't import — same shape as test_scanner.py's
        # BackgroundScanTests; test_main_wires_the_sync_into_the_short_lane
        # below is what checks the production wiring matches.
        self._providers: dict[str, object] = {}
        def _test_handler(payload, report):
            return playlist_sync.sync_playlists(
                self._providers[payload["provider_id"]], payload["provider_id"])
        jobs.register(playlist_sync.JOB_TYPE, _test_handler)
        self.addCleanup(jobs._HANDLERS.pop, playlist_sync.JOB_TYPE, None)
        self.addCleanup(jobs._LANE_BY_TYPE.pop, playlist_sync.JOB_TYPE, None)

    def tearDown(self):
        db.DB_PATH, db.DATA_DIR = self._prev_db_path, self._prev_data_dir
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def _run_queued(self):
        """Run the queued sync the way the worker would, in this thread."""
        return jobs.run_one(jobs.LANE_SHORT)

    def _start(self, provider, provider_id):
        self._providers[provider_id] = provider
        return playlist_sync.start_sync(provider, provider_id)

    def test_start_sync_queues_and_does_not_sync_inline(self):
        started = self._start(_ExplodingProvider(), "roon")
        self.assertEqual(started["status"], "started")
        self.assertIsInstance(started["job_id"], int)
        self.assertTrue(playlist_sync.sync_status()["running"])
        # the whole point of the 202: nothing ran yet, so the exploding
        # provider hasn't actually exploded — _run_queued() would raise if it
        # had, and this test never calls it.

    def test_the_worker_runs_it_and_records_the_result(self):
        provider = _FullStubProvider(
            [{"id": "p1", "title": "OK"}],
            [{"position": 0, "artist": "A", "title": "T", "album": None}])
        self._start(provider, "subsonic")
        self.assertTrue(self._run_queued())
        st = playlist_sync.sync_status()
        self.assertFalse(st["running"])
        self.assertEqual(st["last_result"]["status"], "ok")
        self.assertEqual(st["last_result"]["playlists"], 1)
        conn = db.get_conn()
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM playlists WHERE title='OK'").fetchone())
        finally:
            conn.close()

    def test_a_second_trigger_is_refused_while_one_is_pending(self):
        first = self._start(_FullStubProvider([], []), "subsonic")
        second = self._start(_ExplodingProvider(), "roon")
        self.assertEqual(first["status"], "started")
        self.assertEqual(second["status"], "error")
        self.assertTrue(second["already_running"])

    def test_a_poll_during_a_run_does_not_report_the_previous_runs_counts(self):
        # #141: run one sync to completion, then start a second and poll
        # mid-flight (before running it) — sync_status() must not show the
        # FIRST run's counts while the second is still queued.
        self._start(_FullStubProvider(
            [{"id": "p1", "title": "Old"}],
            [{"position": 0, "artist": "A", "title": "T", "album": None}]), "subsonic")
        self.assertTrue(self._run_queued())
        self.assertEqual(playlist_sync.sync_status()["last_result"]["playlists"], 1)

        self._start(_FullStubProvider([], []), "subsonic")
        st = playlist_sync.sync_status()
        self.assertTrue(st["running"])
        self.assertIsNone(st["last_result"])  # the stale 1-playlist result is gone

    def test_main_wires_the_sync_into_the_short_lane(self):
        # The lane is only correct in production if main.py says so; setUp
        # above wires it locally, so without this a mis-wire there would go
        # unnoticed. Short, not long: a sync is seconds-to-minutes, not the
        # hours a scan/fingerprint backfill can take (jobs._LANE_BY_TYPE).
        import main  # noqa: F401 — imported for its registration side effects
        self.assertEqual(jobs._LANE_BY_TYPE.get(playlist_sync.JOB_TYPE), jobs.LANE_SHORT)


class SpotifyPerOwnerCleanupTests(unittest.TestCase):
    """#10 Part B: the Spotify merge block mirrors Tidal — a linked account's
    playlists sync with owner attribution, and a user whose fetch fails is
    protected from stale-cleanup (only THAT user's rows), same as #71 for
    Tidal. Full run against a temp-file DB, mocked spotify_client."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="trobar-test-spotify-"))
        self._prev_db_path, self._prev_data_dir = db.DB_PATH, db.DATA_DIR
        db.DATA_DIR = self._tmp
        db.DB_PATH = self._tmp / "test.db"
        db.init_db()
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self._tmp / "no-such-music"))
        db.set_config(conn, "spotify_client_id", "cid")
        db.set_config(conn, "spotify_client_secret", "csec")
        # #398: credentials alone no longer turn the sync block on -- these
        # tests are exercising a working, linked Spotify setup, so the
        # experimental toggle must explicitly be on too.
        db.set_config(conn, "experimental_spotify_enabled", "1")
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH, db.DATA_DIR = self._prev_db_path, self._prev_data_dir
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def _seed_row(self, conn, title, src_id, owner_id):
        cur = conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, last_synced_at) "
            "VALUES (?, 'spotify', ?, ?, datetime('now'))", (title, src_id, owner_id))
        return sync_state._new_id(cur)

    def test_links_sync_and_a_failed_users_rows_are_protected(self):
        from unittest import mock

        conn = db.get_conn()
        alice = _make_user(conn, "alice")
        bob = _make_user(conn, "bob")
        conn.execute("UPDATE users SET spotify_refresh_token='alice-rt', spotify_user_id='alice-sp' WHERE id=?", (alice,))
        conn.execute("UPDATE users SET spotify_refresh_token='bob-rt', spotify_user_id='bob-sp' WHERE id=?", (bob,))
        alice_old = self._seed_row(conn, "Alice Old", "a-old", alice)
        bob_old = self._seed_row(conn, "Bob Old", "b-old", bob)
        conn.commit()
        conn.close()

        def fake_refresh(cid, csec, rt):
            if rt == "bob-rt":
                raise spotify_client.SpotifyTransientError("network down")
            return ("alice-access", "alice-rt")

        def fake_list(access_token):
            return {"status": "ok", "playlists": [{"id": "a-new", "title": "Alice New"}]}

        def fake_tracks(title, source_playlist_id=None, **k):
            return {"status": "ok", "tracks": [
                {"position": 0, "artist": "A", "title": "T", "album": None}]}

        provider = _FullStubProvider([], [])
        with mock.patch.object(spotify_client, "refresh_access_token", side_effect=fake_refresh), \
                mock.patch.object(spotify_client, "list_playlists", side_effect=fake_list), \
                mock.patch.object(spotify_client, "get_playlist_tracks", side_effect=fake_tracks):
            playlist_sync.sync_playlists(provider, "subsonic")

        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT owner_user_id FROM playlists WHERE source_provider='spotify' AND title='Alice New'"
            ).fetchone()
            self.assertIsNotNone(row)               # Alice's new playlist synced
            self.assertEqual(row["owner_user_id"], alice)  # owned by her
            # Alice synced ok and didn't relist her old row -> cleaned.
            self.assertIsNone(conn.execute("SELECT 1 FROM playlists WHERE id=?", (alice_old,)).fetchone())
            # Bob's fetch failed -> his old row is protected, not deleted.
            self.assertIsNotNone(conn.execute("SELECT 1 FROM playlists WHERE id=?", (bob_old,)).fetchone())
        finally:
            conn.close()

    def test_auth_revocation_clears_the_link_and_protects_rows(self):
        from unittest import mock

        conn = db.get_conn()
        alice = _make_user(conn, "alice")
        conn.execute("UPDATE users SET spotify_refresh_token='alice-rt', spotify_user_id='alice-sp' WHERE id=?", (alice,))
        alice_old = self._seed_row(conn, "Alice Old", "a-old", alice)
        conn.commit()
        conn.close()

        def fake_refresh(cid, csec, rt):
            raise spotify_client.SpotifyAuthError("revoked")

        provider = _FullStubProvider([], [])
        with mock.patch.object(spotify_client, "refresh_access_token", side_effect=fake_refresh):
            playlist_sync.sync_playlists(provider, "subsonic")

        conn = db.get_conn()
        try:
            # revoked != gone: the row survives, but the stale link is cleared.
            self.assertIsNotNone(conn.execute("SELECT 1 FROM playlists WHERE id=?", (alice_old,)).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT spotify_refresh_token FROM users WHERE id=?", (alice,)).fetchone()[0])
        finally:
            conn.close()

    def test_sync_is_skipped_when_the_experimental_flag_is_off(self):
        # #398: turning the toggle off must stop a still-linked user's
        # account from silently continuing to sync in the background --
        # credentials and a valid link alone are no longer enough.
        from unittest import mock

        conn = db.get_conn()
        db.set_config(conn, "experimental_spotify_enabled", "0")
        alice = _make_user(conn, "alice")
        conn.execute(
            "UPDATE users SET spotify_refresh_token='alice-rt', spotify_user_id='alice-sp' WHERE id=?",
            (alice,))
        conn.commit()
        conn.close()

        def fake_refresh(cid, csec, rt):
            self.fail("refresh_access_token must not be called while the flag is off")

        provider = _FullStubProvider([], [])
        with mock.patch.object(spotify_client, "refresh_access_token", side_effect=fake_refresh):
            playlist_sync.sync_playlists(provider, "subsonic")

        # The link itself is untouched -- "off" pauses the feature, it
        # doesn't disconnect anyone.
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT spotify_refresh_token FROM users WHERE id=?", (alice,)).fetchone()
            self.assertEqual(row["spotify_refresh_token"], "alice-rt")
        finally:
            conn.close()


class PerUserProviderMappingTests(unittest.TestCase):
    """#262: Trobar users individually mapped to their own Jellyfin/Emby
    account get their own playlists merged in too, same "still lands in
    the one shared pool, attributed via owner_user_id" shape as the
    existing Roon-profile mapping (that block, in turn, has no dedicated
    test of its own — this is new coverage for the pattern, not a port of
    an existing one). Uses a temp-file DB, same reason as
    StaleCleanupMirrorTests above: sync_playlists() opens its own
    db.get_conn(), it doesn't accept one. `provider is jellyfin_client`/
    `emby_client` is an identity check against the real imported module,
    so the mapped/default listings are distinguished by mocking the real
    client's own functions rather than a generic stub provider."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="trobar-test-user-mapping-"))
        self._prev_db_path, self._prev_data_dir = db.DB_PATH, db.DATA_DIR
        db.DATA_DIR = self._tmp
        db.DB_PATH = self._tmp / "test.db"
        db.init_db()
        self.conn = db.get_conn()

    def tearDown(self):
        self.conn.close()
        db.DB_PATH, db.DATA_DIR = self._prev_db_path, self._prev_data_dir
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def _make_user(self, username: str, **mapping) -> int:
        columns = ", ".join(mapping.keys())
        placeholders = ", ".join("?" for _ in mapping)
        cur = self.conn.execute(
            f"INSERT INTO users (username{', ' + columns if columns else ''}) "
            f"VALUES (?{', ' + placeholders if placeholders else ''})",
            (username, *mapping.values()),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def test_mapped_users_are_ignored_when_a_different_provider_is_active(self):
        self._make_user("alice", jellyfin_user_id="jf-alice", emby_user_id="emby-alice")
        with mock.patch.object(playlist_sync.roon_client, "list_playlists",
                                return_value={"status": "ok", "playlists": []}), \
             mock.patch.object(playlist_sync.jellyfin_client, "list_playlists") as jf_list, \
             mock.patch.object(playlist_sync.emby_client, "list_playlists") as emby_list:
            playlist_sync.sync_playlists(playlist_sync.roon_client, "roon")
        jf_list.assert_not_called()
        emby_list.assert_not_called()

    def test_jellyfin_mapped_user_playlist_is_synced_with_ownership(self):
        user_id = self._make_user("alice", jellyfin_user_id="jf-alice")

        def _list_playlists(user_id=None):
            if user_id == "jf-alice":
                return {"status": "ok", "playlists": [{"id": "p1", "title": "Alice Mix"}]}
            return {"status": "ok", "playlists": []}  # the default account's own listing

        def _get_tracks(title, source_playlist_id=None, user_id=None):
            return {"status": "ok", "playlist": title, "tracks": []}

        with mock.patch.object(playlist_sync.jellyfin_client, "list_playlists", side_effect=_list_playlists), \
             mock.patch.object(playlist_sync.jellyfin_client, "get_playlist_tracks",
                                side_effect=_get_tracks) as tracks_mock:
            playlist_sync.sync_playlists(playlist_sync.jellyfin_client, "jellyfin")

        row = self.conn.execute(
            "SELECT owner_user_id, source_provider, shared FROM playlists WHERE title = 'Alice Mix'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["owner_user_id"], user_id)
        self.assertEqual(row["source_provider"], "jellyfin")
        # #496: this is the exact scenario the issue was filed about — a
        # mapped user's playlist must not publish to the household by
        # default just because an admin picked their account.
        self.assertEqual(row["shared"], 0)
        tracks_mock.assert_called_once_with("Alice Mix", "p1", user_id="jf-alice")

    def test_emby_mapped_user_playlist_is_synced_with_ownership(self):
        # Same mechanism, Emby's own client — not just a copy-paste
        # assumption, since #490 found real behavioral divergences
        # between these two despite the shared MediaBrowser lineage.
        user_id = self._make_user("alice", emby_user_id="emby-alice")

        def _list_playlists(user_id=None):
            if user_id == "emby-alice":
                return {"status": "ok", "playlists": [{"id": "p1", "title": "Alice Mix"}]}
            return {"status": "ok", "playlists": []}

        def _get_tracks(title, source_playlist_id=None, user_id=None):
            return {"status": "ok", "playlist": title, "tracks": []}

        with mock.patch.object(playlist_sync.emby_client, "list_playlists", side_effect=_list_playlists), \
             mock.patch.object(playlist_sync.emby_client, "get_playlist_tracks",
                                side_effect=_get_tracks) as tracks_mock:
            playlist_sync.sync_playlists(playlist_sync.emby_client, "emby")

        row = self.conn.execute(
            "SELECT owner_user_id, source_provider, shared FROM playlists WHERE title = 'Alice Mix'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["owner_user_id"], user_id)
        self.assertEqual(row["source_provider"], "emby")
        self.assertEqual(row["shared"], 0)  # #496, same scenario as the Jellyfin test above
        tracks_mock.assert_called_once_with("Alice Mix", "p1", user_id="emby-alice")

    def test_a_playlist_already_synced_by_the_default_account_is_not_reprocessed(self):
        # Same key precedence as the Roon block: a playlist the mapped
        # user can also see, already synced by the default listing, is
        # skipped by this pass rather than double-processed (first
        # synced this run wins, so it stays unowned).
        user_id = self._make_user("alice", jellyfin_user_id="jf-alice")

        def _list_playlists(user_id=None):
            # Both the default account and alice's mapped account see the
            # exact same playlist item (same stable src_id).
            return {"status": "ok", "playlists": [{"id": "shared1", "title": "Shared Mix"}]}

        def _get_tracks(title, source_playlist_id=None, user_id=None):
            return {"status": "ok", "playlist": title, "tracks": []}

        with mock.patch.object(playlist_sync.jellyfin_client, "list_playlists", side_effect=_list_playlists), \
             mock.patch.object(playlist_sync.jellyfin_client, "get_playlist_tracks",
                                side_effect=_get_tracks) as tracks_mock:
            playlist_sync.sync_playlists(playlist_sync.jellyfin_client, "jellyfin")

        rows = self.conn.execute("SELECT owner_user_id FROM playlists WHERE title = 'Shared Mix'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["owner_user_id"])  # default pass's row, not alice's
        tracks_mock.assert_called_once()  # not called a second time for alice's pass
        self.assertNotEqual(user_id, None)  # (alice's id exists; just never attached)

    def test_a_mapped_user_error_is_skipped_not_raised(self):
        self._make_user("alice", jellyfin_user_id="jf-alice")

        def _list_playlists(user_id=None):
            if user_id == "jf-alice":
                return {"status": "error", "reason": "not_paired"}
            return {"status": "ok", "playlists": []}

        with mock.patch.object(playlist_sync.jellyfin_client, "list_playlists", side_effect=_list_playlists):
            result = playlist_sync.sync_playlists(playlist_sync.jellyfin_client, "jellyfin")  # must not raise
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
