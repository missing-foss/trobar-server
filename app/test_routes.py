#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Route-layer tests for main.py's playlist privacy enforcement — #41's
flagged "silent-regression hurts users" surface: the enforcement added by
#68 (ownership + opt-in sharing), #73/#74 (retroactive unshare), verified
until now only by hand.

    python3 -m unittest test_routes -v      # from app/

This is the Flask-test-client harness #41's scoping note calls the actual
new infrastructure. DATA_DIR is pointed at a throwaway temp dir *before*
importing main (its module-level secret-key load writes there); each test
gets its own fresh SQLite file via db.init_db(). Auth is a real session
cookie (AUTH_MODE defaults to local), set through the test client's
session_transaction — the same session key get_current_user_id() reads.
"""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# main freezes DATA_DIR-derived paths at import time (its _SECRET_KEY_FILE
# = db.DATA_DIR / "flask_secret_key", written on first import). db.DATA_DIR
# itself is frozen when db is first imported — which another test module in
# the same `unittest` run may already have done with the default /data
# (unwritable). So set the env var AND overwrite db's already-frozen
# globals before importing main, making this robust to test collection
# order rather than relying on being imported first.
_TMP = tempfile.mkdtemp(prefix="trobar-test-routes-")
os.environ["DATA_DIR"] = _TMP

import db          # noqa: E402
db.DATA_DIR = Path(_TMP)
db.DB_PATH = Path(_TMP) / "music-sync.db"

import jobs             # noqa: E402
import library_quiz     # noqa: E402
import main             # noqa: E402
import playlist_sync    # noqa: E402
import scanner          # noqa: E402
import sync_state       # noqa: E402


def tearDownModule():
    # Remove the whole throwaway dir, not just per-test .db files —
    # tearDown unlinks the .db but WAL-mode leaves .db-wal/.db-shm
    # sidecars, and the module-level flask_secret_key lives here too.
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(client, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess["local_user_id"] = user_id


class _RouteTestBase(unittest.TestCase):
    def setUp(self):
        # Fresh DB file per test — patch the module global get_conn() reads
        # at call time, so every route handler picks it up.
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()

        self.conn = db.get_conn()
        self.admin = self._make_user("admin", is_admin=True)
        self.owner = self._make_user("owner")
        self.other = self._make_user("bob")

    def tearDown(self):
        self.conn.close()
        self._db_path.unlink(missing_ok=True)

    def _make_user(self, username: str, is_admin: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO users (username, is_admin) VALUES (?, ?)", (username, 1 if is_admin else 0))
        self.conn.commit()
        return sync_state._new_id(cur)

    def _make_playlist(self, title: str, owner_user_id=None, shared: int = 1) -> int:
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, owner_user_id, shared, last_synced_at) "
            "VALUES (?, 'roon', ?, ?, datetime('now'))",
            (title, owner_user_id, shared))
        self.conn.commit()
        return sync_state._new_id(cur)


class ProviderPlaylistVisibilityTests(_RouteTestBase):
    """GET /api/provider/playlists — an owned-and-unshared playlist is
    invisible to everyone but its owner and the admin; unowned or shared
    playlists are visible to all."""

    def setUp(self):
        super().setUp()
        self.private = self._make_playlist("Private", owner_user_id=self.owner, shared=0)
        self.shared = self._make_playlist("Shared", owner_user_id=self.owner, shared=1)
        self.unowned = self._make_playlist("Unowned", owner_user_id=None, shared=1)

    def _titles_visible_to(self, user_id):
        _login(self.client, user_id)
        resp = self.client.get("/api/provider/playlists")
        self.assertEqual(resp.status_code, 200)
        return {p["title"] for p in resp.get_json()}

    def test_non_owner_cannot_see_private_playlist(self):
        self.assertEqual(self._titles_visible_to(self.other), {"Shared", "Unowned"})

    def test_owner_sees_own_private_playlist(self):
        self.assertEqual(self._titles_visible_to(self.owner), {"Private", "Shared", "Unowned"})

    def test_admin_sees_every_playlist(self):
        self.assertEqual(self._titles_visible_to(self.admin), {"Private", "Shared", "Unowned"})

    def test_is_own_and_shared_are_real_json_booleans_not_0_1(self):
        # #449's sweep: same SQLite-integer-vs-JSON-boolean gap as
        # is_own/is_pinned on the device list, here for is_own/shared.
        # assertIs, not assertTrue -- pins the type, not just truthiness.
        _login(self.client, self.owner)
        rows = {p["title"]: p for p in self.client.get("/api/provider/playlists").get_json()}
        self.assertIs(rows["Private"]["is_own"], True)
        self.assertIs(rows["Private"]["shared"], False)
        self.assertIs(rows["Unowned"]["is_own"], False)
        self.assertIs(rows["Unowned"]["shared"], True)


class GoldenSourcePerViewerTests(_RouteTestBase):
    """#81: a dual-source playlist (a household Roon row + the owner's own
    private Tidal row, linked via golden_source_id) is attributed per
    viewer. Owner sees the Tidal golden copy (Roon dup suppressed);
    non-owner sees the Roon row badged 'shared by <owner>'; a Tidal-only
    private playlist stays hidden from non-owners."""

    def setUp(self):
        super().setUp()
        # owner's own Tidal playlist, private (#28)
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, last_synced_at) "
            "VALUES ('Road Trip', 'tidal', 'tid1', ?, 0, datetime('now'))", (self.owner,))
        self.tidal_id = sync_state._new_id(cur)
        # the same playlist reached via the shared Roon connection — unowned,
        # household-visible, linked to the Tidal golden copy
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, owner_user_id, inferred_origin_provider, "
            "golden_source_id, last_synced_at) "
            "VALUES ('Road Trip', 'roon', NULL, 'tidal', ?, datetime('now'))", (self.tidal_id,))
        self.roon_id = sync_state._new_id(cur)
        # a genuinely Tidal-only private playlist (no Roon counterpart)
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, last_synced_at) "
            "VALUES ('Solo', 'tidal', 'tid2', ?, 0, datetime('now'))", (self.owner,))
        self.solo_id = sync_state._new_id(cur)
        self.conn.commit()

    def _rows_for(self, user_id):
        _login(self.client, user_id)
        resp = self.client.get("/api/provider/playlists")
        self.assertEqual(resp.status_code, 200)
        return {p["id"]: p for p in resp.get_json()}

    def test_owner_sees_tidal_copy_roon_dup_suppressed(self):
        rows = self._rows_for(self.owner)
        self.assertIn(self.tidal_id, rows)          # their golden Tidal copy
        self.assertNotIn(self.roon_id, rows)        # Roon dup suppressed
        self.assertEqual(rows[self.tidal_id]["source_provider"], "tidal")

    def test_non_owner_sees_roon_row_badged_shared_by_owner(self):
        rows = self._rows_for(self.other)
        self.assertIn(self.roon_id, rows)           # reaches them via Roon
        self.assertNotIn(self.tidal_id, rows)       # owner's Tidal copy stays private
        self.assertEqual(rows[self.roon_id]["golden_owner_username"], "owner")
        # ...and the Tidal-only private playlist is not visible to them either
        self.assertNotIn(self.solo_id, rows)

    def test_admin_sees_the_golden_copy(self):
        rows = self._rows_for(self.admin)
        # admin can see the golden Tidal row, so the Roon dup is suppressed
        self.assertIn(self.tidal_id, rows)
        self.assertNotIn(self.roon_id, rows)

    def test_shared_tidal_copy_also_suppresses_roon_dup_for_everyone(self):
        # If the owner un-privates their Tidal copy, a non-owner can now see
        # it directly — so the Roon dup is suppressed for them too (no
        # double row). Handled by the same "can the viewer see the golden
        # row" rule.
        self.conn.execute("UPDATE playlists SET shared=1 WHERE id=?", (self.tidal_id,))
        self.conn.commit()
        rows = self._rows_for(self.other)
        self.assertIn(self.tidal_id, rows)
        self.assertNotIn(self.roon_id, rows)


class PlaylistPatchAuthTests(_RouteTestBase):
    """PATCH /api/provider/playlists/<id> — the shared toggle's auth matrix."""

    def setUp(self):
        super().setUp()
        self.owned = self._make_playlist("Owned", owner_user_id=self.owner, shared=1)
        self.unowned = self._make_playlist("Unowned", owner_user_id=None, shared=1)

    def _patch(self, user_id, playlist_id, body):
        _login(self.client, user_id)
        return self.client.patch(f"/api/provider/playlists/{playlist_id}", json=body)

    def test_owner_can_toggle_shared(self):
        resp = self._patch(self.owner, self.owned, {"shared": False})
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute("SELECT shared FROM playlists WHERE id=?", (self.owned,)).fetchone()
        self.assertEqual(row["shared"], 0)

    def test_admin_can_toggle_shared(self):
        resp = self._patch(self.admin, self.owned, {"shared": False})
        self.assertEqual(resp.status_code, 200)

    def test_non_owner_gets_403(self):
        resp = self._patch(self.other, self.owned, {"shared": False})
        self.assertEqual(resp.status_code, 403)
        row = self.conn.execute("SELECT shared FROM playlists WHERE id=?", (self.owned,)).fetchone()
        self.assertEqual(row["shared"], 1)  # unchanged

    def test_unowned_playlist_gets_400(self):
        resp = self._patch(self.owner, self.unowned, {"shared": False})
        self.assertEqual(resp.status_code, 400)

    def test_missing_playlist_gets_404(self):
        resp = self._patch(self.owner, 99999, {"shared": False})
        self.assertEqual(resp.status_code, 404)

    def test_missing_shared_key_gets_400(self):
        resp = self._patch(self.owner, self.owned, {"something_else": True})
        self.assertEqual(resp.status_code, 400)


class PlaylistUnresolvedTracksTests(_RouteTestBase):
    """#200: GET/POST .../unresolved-tracks — same #28 visibility rule as
    the playlist itself (private+someone-else-owned is a 403/404, not just
    absent from the list), and a working exclude/un-exclude round trip."""

    def setUp(self):
        super().setUp()
        self.owned = self._make_playlist("Owned", owner_user_id=self.owner, shared=1)
        self.private = self._make_playlist("Private", owner_user_id=self.owner, shared=0)
        self.unowned = self._make_playlist("Unowned", owner_user_id=None, shared=1)
        self._add_unresolved(self.owned, "Artist A", "Song A")
        self._add_unresolved(self.private, "Artist B", "Song B")

    def _add_unresolved(self, playlist_id: int, artist: str, title: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album) "
            "VALUES (?, 0, ?, ?, '')",
            (playlist_id, artist, title),
        )
        self.conn.commit()
        return sync_state._new_id(cur)

    def _get(self, user_id, playlist_id):
        _login(self.client, user_id)
        return self.client.get(f"/api/provider/playlists/{playlist_id}/unresolved-tracks")

    def _exclude(self, user_id, playlist_id, ids, excluded=True):
        _login(self.client, user_id)
        return self.client.post(
            f"/api/provider/playlists/{playlist_id}/unresolved-tracks/exclude",
            json={"ids": ids, "excluded": excluded},
        )

    def test_owner_can_list_their_own_playlists_unresolved_tracks(self):
        resp = self._get(self.owner, self.owned)
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["artist"], "Artist A")
        self.assertFalse(rows[0]["excluded"])

    def test_anyone_can_list_a_shared_playlists_unresolved_tracks(self):
        resp = self._get(self.other, self.owned)
        self.assertEqual(resp.status_code, 200)

    def test_non_owner_cannot_list_a_private_playlists_unresolved_tracks(self):
        resp = self._get(self.other, self.private)
        self.assertEqual(resp.status_code, 403)

    def test_owner_can_list_their_own_private_playlists_unresolved_tracks(self):
        resp = self._get(self.owner, self.private)
        self.assertEqual(resp.status_code, 200)

    def test_admin_can_list_any_playlists_unresolved_tracks(self):
        resp = self._get(self.admin, self.private)
        self.assertEqual(resp.status_code, 200)

    def test_missing_playlist_gets_404(self):
        resp = self._get(self.owner, 99999)
        self.assertEqual(resp.status_code, 404)

    def test_exclude_round_trip(self):
        row_id = self._get(self.owner, self.owned).get_json()[0]["id"]
        resp = self._exclude(self.owner, self.owned, [row_id], True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["unresolved_count"], 0)
        rows = self._get(self.owner, self.owned).get_json()
        self.assertTrue(rows[0]["excluded"])

        resp = self._exclude(self.owner, self.owned, [row_id], False)
        self.assertEqual(resp.get_json()["unresolved_count"], 1)

    def test_non_owner_cannot_exclude_on_a_private_playlist(self):
        row_id = self._get(self.owner, self.private).get_json()[0]["id"]
        resp = self._exclude(self.other, self.private, [row_id], True)
        self.assertEqual(resp.status_code, 403)

    def test_ids_must_be_a_list(self):
        _login(self.client, self.owner)
        resp = self.client.post(
            f"/api/provider/playlists/{self.owned}/unresolved-tracks/exclude",
            json={"ids": "not-a-list"},
        )
        self.assertEqual(resp.status_code, 400)


class PlaylistListUnresolvedCountTests(_RouteTestBase):
    """#200: GET /api/provider/playlists' unresolved_count field — added
    via a correlated subquery specifically to avoid the fan-out bug a third
    LEFT JOIN would cause alongside the existing playlist_tracks one (see
    the route's own comment)."""

    def setUp(self):
        super().setUp()
        self.playlist = self._make_playlist("Mix", owner_user_id=None, shared=1)
        # Two real playlist_tracks rows (to make a fan-out bug visible if
        # one crept back in) plus two unresolved rows, one excluded.
        for i in range(2):
            self.conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, position, artist, title) "
                "VALUES (?, ?, 'Artist', 'Title')", (self.playlist, i),
            )
        self.conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album, excluded) "
            "VALUES (?, 0, 'A', 'Song A', '', 0)", (self.playlist,),
        )
        self.conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album, excluded) "
            "VALUES (?, 1, 'B', 'Song B', '', 1)", (self.playlist,),
        )
        self.conn.commit()

    def test_unresolved_count_excludes_acknowledged_rows_and_track_count_is_unaffected(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/provider/playlists")
        self.assertEqual(resp.status_code, 200)
        row = next(p for p in resp.get_json() if p["id"] == self.playlist)
        self.assertEqual(row["unresolved_count"], 1)
        self.assertEqual(row["track_count"], 2)  # not fanned out by the join above


class PlaylistListMirrorFolderConfiguredTests(_RouteTestBase):
    """#410: GET /api/provider/playlists' mirror_folder_configured field —
    lets the row disable "Mirror to…" instead of letting it succeed and
    only fail (per-row, silently) on the next write. One system-wide value
    attached to every row, not a per-playlist setting."""

    def setUp(self):
        super().setUp()
        self.playlist = self._make_playlist("Mix", owner_user_id=None, shared=1)

    def test_true_when_a_mirror_folder_is_set(self):
        mirror_dir = Path(tempfile.mkdtemp(prefix="trobar-test-mirror-configured-", dir=_TMP))
        db.set_config(self.conn, "mirror_folder", str(mirror_dir))
        self.conn.commit()
        _login(self.client, self.owner)
        resp = self.client.get("/api/provider/playlists")
        row = next(p for p in resp.get_json() if p["id"] == self.playlist)
        self.assertTrue(row["mirror_folder_configured"])

    def test_false_when_no_mirror_folder_is_set(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/provider/playlists")
        row = next(p for p in resp.get_json() if p["id"] == self.playlist)
        self.assertFalse(row["mirror_folder_configured"])


class PlaylistListLidarrConnectedTests(_RouteTestBase):
    """#509: GET /api/provider/playlists' lidarr_request_connected field --
    split out from lidarr_request_configured because "not configured" used
    to collapse two different situations (no connection at all, vs.
    connected but the three profile fields never chosen) into the same
    disabled-button hint, which read as "not set up" even while
    Administration was showing green "Connected" for the second case."""

    def setUp(self):
        super().setUp()
        self.playlist = self._make_playlist("Mix", owner_user_id=None, shared=1)

    def test_neither_flag_set_with_nothing_configured(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/provider/playlists")
        row = next(p for p in resp.get_json() if p["id"] == self.playlist)
        self.assertFalse(row["lidarr_request_connected"])
        self.assertFalse(row["lidarr_request_configured"])

    def test_connected_but_not_configured_when_profile_fields_are_unset(self):
        # The exact state the issue was filed over: url+api_key work, but
        # none of root folder / quality profile / metadata profile were
        # ever chosen.
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        self.conn.commit()
        _login(self.client, self.owner)
        resp = self.client.get("/api/provider/playlists")
        row = next(p for p in resp.get_json() if p["id"] == self.playlist)
        self.assertTrue(row["lidarr_request_connected"])
        self.assertFalse(row["lidarr_request_configured"])

    def test_both_true_once_fully_configured(self):
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        db.set_config(self.conn, "lidarr_root_folder_path", "/music")
        db.set_config(self.conn, "lidarr_quality_profile_id", "1")
        db.set_config(self.conn, "lidarr_metadata_profile_id", "2")
        self.conn.commit()
        _login(self.client, self.owner)
        resp = self.client.get("/api/provider/playlists")
        row = next(p for p in resp.get_json() if p["id"] == self.playlist)
        self.assertTrue(row["lidarr_request_connected"])
        self.assertTrue(row["lidarr_request_configured"])


class PlaylistMirrorToggleTests(_RouteTestBase):
    """#285: POST .../mirror — same #28 visibility rule as the
    unresolved-tracks routes (any user who can see the playlist, not
    owner/admin-gated), and a real write/delete round trip against a real
    tmp mirror folder (this harness uses a real file-backed DB, so
    mirror.write_mirror's own db.get_conn() calls resolve correctly —
    unlike test_playlist_sync.py's in-memory harness)."""

    def setUp(self):
        super().setUp()
        self._mirror_dir = Path(tempfile.mkdtemp(prefix="trobar-test-mirror-route-", dir=_TMP))
        db.set_config(self.conn, "mirror_folder", str(self._mirror_dir))
        db.set_config(self.conn, "music_root", str(Path(_TMP) / "no-such-music"))
        self.conn.commit()
        self.owned = self._make_playlist("Owned", owner_user_id=self.owner, shared=1)
        self.private = self._make_playlist("Private", owner_user_id=self.owner, shared=0)

    def _toggle(self, user_id, playlist_id, enabled=True):
        _login(self.client, user_id)
        return self.client.post(
            f"/api/provider/playlists/{playlist_id}/mirror", json={"enabled": enabled}
        )

    def test_any_user_can_enable_mirroring_on_a_shared_playlist(self):
        resp = self._toggle(self.other, self.owned, True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["mirror_enabled"])

    def test_non_owner_cannot_toggle_a_private_playlist(self):
        resp = self._toggle(self.other, self.private, True)
        self.assertEqual(resp.status_code, 403)

    def test_owner_can_toggle_their_own_private_playlist(self):
        resp = self._toggle(self.owner, self.private, True)
        self.assertEqual(resp.status_code, 200)

    def test_missing_playlist_gets_404(self):
        resp = self._toggle(self.owner, 99999, True)
        self.assertEqual(resp.status_code, 404)

    def test_enabling_writes_a_real_file_and_disabling_deletes_it(self):
        resp = self._toggle(self.owner, self.owned, True)
        data = resp.get_json()
        self.assertIsNotNone(data["mirror_filename"])
        path = self._mirror_dir / data["mirror_filename"]
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("Generated by Trobar", content)

        resp = self._toggle(self.owner, self.owned, False)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["mirror_enabled"])
        self.assertFalse(path.exists())

    def test_missing_enabled_key_gets_400(self):
        _login(self.client, self.owner)
        resp = self.client.post(f"/api/provider/playlists/{self.owned}/mirror", json={})
        self.assertEqual(resp.status_code, 400)

    def test_a_real_write_failure_surfaces_its_code_through_the_route(self):
        # #428: the toggle response is the client's own live source for
        # mirror_last_error_code (rendered immediately, not waiting for a
        # background job or a reload) -- pin that it actually carries the
        # new column, not just the old free-text one.
        self._mirror_dir.rmdir()
        self._mirror_dir.write_text("not a directory", encoding="utf-8")
        resp = self._toggle(self.owner, self.owned, True)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["mirror_last_error_code"], "not_writable")
        self.assertIsNotNone(data["mirror_last_error"])


class PlaylistLidarrRequestsToggleTests(_RouteTestBase):
    """#494: POST .../lidarr-requests — same #28 visibility rule as
    PlaylistMirrorToggleTests above (any user who can see the playlist),
    but a distinct route since this isn't a mirror sink. lidarr_client is
    mocked at the module boundary main.py imports it through, same as
    AdminConfigMirrorJellyfinTests mocks requests.request one layer down."""

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "lidarr_url", "http://lidarr.local")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        db.set_config(self.conn, "lidarr_root_folder_path", "/music")
        db.set_config(self.conn, "lidarr_quality_profile_id", "1")
        db.set_config(self.conn, "lidarr_metadata_profile_id", "2")
        self.conn.commit()
        self.owned = self._make_playlist("Owned", owner_user_id=self.owner, shared=1)
        self.private = self._make_playlist("Private", owner_user_id=self.owner, shared=0)

    def _toggle(self, user_id, playlist_id, enabled=True):
        _login(self.client, user_id)
        return self.client.post(
            f"/api/provider/playlists/{playlist_id}/lidarr-requests", json={"enabled": enabled}
        )

    def test_any_user_can_enable_requests_on_a_shared_playlist(self):
        with mock.patch("lidarr_requests.lidarr_client"):
            resp = self._toggle(self.other, self.owned, True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["lidarr_request_enabled"])

    def test_non_owner_cannot_toggle_a_private_playlist(self):
        resp = self._toggle(self.other, self.private, True)
        self.assertEqual(resp.status_code, 403)

    def test_owner_can_toggle_their_own_private_playlist(self):
        with mock.patch("lidarr_requests.lidarr_client"):
            resp = self._toggle(self.owner, self.private, True)
        self.assertEqual(resp.status_code, 200)

    def test_missing_playlist_gets_404(self):
        resp = self._toggle(self.owner, 99999, True)
        self.assertEqual(resp.status_code, 404)

    def test_missing_enabled_key_gets_400(self):
        _login(self.client, self.owner)
        resp = self.client.post(f"/api/provider/playlists/{self.owned}/lidarr-requests", json={})
        self.assertEqual(resp.status_code, 400)

    def test_enabling_runs_a_real_mocked_client_round_trip(self):
        # #494's own "instant feedback, don't wait for the next sync"
        # contract — enabling calls run_for_playlist() immediately, whose
        # outcome is already visible in THIS response, not just after a
        # later reload.
        self.conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album) "
            "VALUES (?, 0, 'Artist', 'Track', 'Album')", (self.owned,))
        self.conn.commit()
        with mock.patch("lidarr_requests.lidarr_client") as client:
            client.lookup_album.return_value = {
                "status": "ok",
                "candidates": [{"foreignAlbumId": "fa1", "artist": {
                    "artistName": "Artist", "foreignArtistId": "far1"}}],
            }
            client.add_and_monitor_album.return_value = {"status": "ok", "artist_id": 1, "album_id": 2}
            resp = self._toggle(self.owner, self.owned, True)
        data = resp.get_json()
        self.assertEqual(data["lidarr_request_last_count"], 1)
        self.assertIsNotNone(data["lidarr_request_last_run_at"])

    def test_disabling_does_not_call_the_client(self):
        with mock.patch("lidarr_requests.lidarr_client") as client:
            self._toggle(self.owner, self.owned, True)
            client.reset_mock()
            resp = self._toggle(self.owner, self.owned, False)
        client.lookup_album.assert_not_called()
        self.assertFalse(resp.get_json()["lidarr_request_enabled"])


class AdminConfigLidarrTests(_RouteTestBase):
    """#494: PUT /api/admin/config's two independent Lidarr blocks —
    url/api_key (same explicit-clear-on-blank shape as the mirror targets)
    and the three profile fields (a genuinely separate save moment, since
    they can't be chosen until the connection above is live)."""

    def _mock_response(self, json_body, status_code=200):
        resp = mock.Mock()
        resp.status_code = status_code
        resp.content = b"x"
        resp.json.return_value = json_body
        return resp

    def test_setting_url_and_api_key_persists_and_pings_status(self):
        _login(self.client, self.admin)
        with mock.patch("requests.request", side_effect=[
            self._mock_response({"version": "3.1.0.4875"}),  # reconnect's own status() ping
            self._mock_response({"version": "3.1.0.4875"}),  # the route's own status() for its GET-shaped response
        ]) as req:
            resp = self.client.put("/api/admin/config", json={
                "lidarr_url": "http://lidarr.example.com", "lidarr_api_key": "key1",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "lidarr_url"), "http://lidarr.example.com")
        self.assertEqual(db.get_config(self.conn, "lidarr_api_key"), "key1")
        self.assertEqual(req.call_count, 2)
        self.assertEqual(resp.get_json()["lidarr_state"], "paired")

    def test_blanking_url_and_api_key_also_clears_the_three_profile_fields(self):
        # The three fields are meaningless -- or worse, wrong -- against a
        # different Lidarr instance, so a reconnect-from-scratch wipes them too.
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        db.set_config(self.conn, "lidarr_root_folder_path", "/music")
        db.set_config(self.conn, "lidarr_quality_profile_id", "1")
        db.set_config(self.conn, "lidarr_metadata_profile_id", "2")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("requests.request") as req:
            resp = self.client.put("/api/admin/config", json={"lidarr_url": "", "lidarr_api_key": ""})
        self.assertEqual(resp.status_code, 200)
        req.assert_not_called()
        self.assertIsNone(db.get_config(self.conn, "lidarr_url"))
        self.assertIsNone(db.get_config(self.conn, "lidarr_api_key"))
        self.assertIsNone(db.get_config(self.conn, "lidarr_root_folder_path"))
        self.assertIsNone(db.get_config(self.conn, "lidarr_quality_profile_id"))
        self.assertIsNone(db.get_config(self.conn, "lidarr_metadata_profile_id"))

    def test_a_payload_that_never_mentions_url_leaves_it_untouched(self):
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"job_retention_days": 14})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "lidarr_url"), "http://lidarr.example.com")

    def test_setting_the_three_profile_fields_persists_them(self):
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("requests.request", return_value=self._mock_response({"version": "x"})):
            resp = self.client.put("/api/admin/config", json={
                "lidarr_root_folder_path": "/music",
                "lidarr_quality_profile_id": 3,
                "lidarr_metadata_profile_id": 4,
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "lidarr_root_folder_path"), "/music")
        self.assertEqual(db.get_config(self.conn, "lidarr_quality_profile_id"), "3")
        self.assertEqual(db.get_config(self.conn, "lidarr_metadata_profile_id"), "4")

    def test_a_non_integer_profile_id_gets_400(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={
            "lidarr_root_folder_path": "/music",
            "lidarr_quality_profile_id": "not-a-number",
            "lidarr_metadata_profile_id": 4,
        })
        self.assertEqual(resp.status_code, 400)

    def test_get_reports_disconnected_state_when_unconfigured(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.get_json()["lidarr_state"], "disconnected")


class AdminLidarrOptionsRouteTests(_RouteTestBase):
    """#494: GET /api/admin/lidarr-options — admin-only, live three-list
    fetch, same shape as AdminUserProviderMappingRouteTests' jellyfin-users
    route below, generalized to three lists."""

    def test_non_admin_gets_403(self):
        _login(self.client, self.other)
        resp = self.client.get("/api/admin/lidarr-options")
        self.assertEqual(resp.status_code, 403)

    def test_not_configured_shape(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/lidarr-options")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "error", "reason": "not_configured", "code": None})

    def test_admin_gets_all_three_lists_combined(self):
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("lidarr_client.list_root_folders", return_value={
            "status": "ok", "root_folders": [{"path": "/music", "free_space": 1}]}), \
            mock.patch("lidarr_client.list_quality_profiles", return_value={
                "status": "ok", "quality_profiles": [{"id": 1, "name": "Lossless"}]}), \
            mock.patch("lidarr_client.list_metadata_profiles", return_value={
                "status": "ok", "metadata_profiles": [{"id": 2, "name": "Standard"}]}):
            resp = self.client.get("/api/admin/lidarr-options")
        self.assertEqual(resp.get_json(), {
            "status": "ok",
            "root_folders": [{"path": "/music", "free_space": 1}],
            "quality_profiles": [{"id": 1, "name": "Lossless"}],
            "metadata_profiles": [{"id": 2, "name": "Standard"}],
        })

    def test_a_failure_partway_through_stops_and_surfaces_that_error(self):
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("lidarr_client.list_root_folders", return_value={
            "status": "ok", "root_folders": []}), \
            mock.patch("lidarr_client.list_quality_profiles", return_value={
                "status": "error", "reason": "unreachable", "code": 500}) as quality, \
            mock.patch("lidarr_client.list_metadata_profiles") as metadata:
            resp = self.client.get("/api/admin/lidarr-options")
        self.assertEqual(resp.get_json(), {"status": "error", "reason": "unreachable", "code": 500})
        quality.assert_called_once()
        metadata.assert_not_called()


class AdminMirrorsRouteTests(_RouteTestBase):
    """#285: GET /api/admin/mirrors — admin-only overview."""

    def setUp(self):
        super().setUp()
        self._mirror_dir = Path(tempfile.mkdtemp(prefix="trobar-test-mirror-admin-", dir=_TMP))
        db.set_config(self.conn, "mirror_folder", str(self._mirror_dir))
        db.set_config(self.conn, "music_root", str(Path(_TMP) / "no-such-music"))
        self.conn.commit()

    def test_non_admin_gets_403(self):
        _login(self.client, self.other)
        resp = self.client.get("/api/admin/mirrors")
        self.assertEqual(resp.status_code, 403)

    def test_admin_sees_mirrored_playlists_only(self):
        mirrored = self._make_playlist("Mirrored", owner_user_id=None, shared=1)
        self._make_playlist("Not Mirrored", owner_user_id=None, shared=1)
        self.conn.execute("UPDATE playlists SET mirror_enabled = 1 WHERE id = ?", (mirrored,))
        self.conn.commit()

        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/mirrors")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["mirror_folder"], str(self._mirror_dir))
        titles = [p["title"] for p in data["playlists"]]
        self.assertEqual(titles, ["Mirrored"])

    def test_error_code_is_included_alongside_the_detail(self):
        # #428: the client needs both to render a translated message --
        # pin that this endpoint's SELECT wasn't missed when the column
        # was added (the toggle route's own SELECT is covered separately
        # in PlaylistMirrorToggleTests).
        mirrored = self._make_playlist("Mirrored", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET mirror_enabled = 1, mirror_last_error_code = 'not_writable', "
            "mirror_last_error = 'boom' WHERE id = ?", (mirrored,))
        self.conn.commit()

        _login(self.client, self.admin)
        data = self.client.get("/api/admin/mirrors").get_json()
        row = next(p for p in data["playlists"] if p["id"] == mirrored)
        self.assertEqual(row["mirror_last_error_code"], "not_writable")
        self.assertEqual(row["mirror_last_error"], "boom")

    def test_a_playlist_with_only_lidarr_requests_enabled_is_included(self):
        # #494: not a mirror sink, but included here too — the WHERE
        # clause's own OR was extended, not just the SELECT list.
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        self.conn.commit()
        requesting = self._make_playlist("Requesting", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET lidarr_request_enabled = 1, lidarr_request_last_count = 3, "
            "lidarr_request_last_run_at = datetime('now') WHERE id = ?", (requesting,))
        self.conn.commit()

        _login(self.client, self.admin)
        data = self.client.get("/api/admin/mirrors").get_json()
        self.assertEqual(data["lidarr_url"], "http://lidarr.example.com")
        row = next(p for p in data["playlists"] if p["id"] == requesting)
        self.assertTrue(row["lidarr_request_enabled"])
        self.assertEqual(row["lidarr_request_last_count"], 3)


class RateLimitTrustedProxyTests(_RouteTestBase):
    """#382: the brute-force backoff must key on the
    trusted, proxy-appended hop of X-Forwarded-For — the RIGHT-most
    entry, which ProxyFix(x_for=1) rewrites into request.remote_addr —
    not whatever a client claims on the left. Before the fix, an
    attacker who sent a fake leftmost IP that changed every request
    never accumulated a single failure in any one bucket."""

    def setUp(self):
        super().setUp()
        # Module-level global shared by every test in the process; clear
        # both directions so this test can't leak into (or be polluted by)
        # any other test that happens to share a bucket key.
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)

    def _failed_login(self, xff=None):
        headers = {"X-Forwarded-For": xff} if xff else {}
        return self.client.post(
            "/login", data={"username": "no-such-user", "password": "wrong"}, headers=headers)

    def test_a_spoofed_rotating_leftmost_ip_does_not_evade_the_limit(self):
        # Same trusted right-most hop every time (as a real single-proxy
        # deployment would send), attacker-controlled left prefix rotates
        # every request — this is exactly #382's exploit.
        for i in range(10):
            resp = self._failed_login(xff=f"203.0.113.{i}, 198.51.100.9")
            self.assertNotEqual(resp.status_code, 429, f"attempt {i} was already limited")
        limited = self._failed_login(xff="203.0.113.99, 198.51.100.9")
        self.assertEqual(limited.status_code, 429,
                          "rotating the spoofed leftmost IP must not evade rate limiting")

    def test_two_real_clients_behind_the_same_proxy_are_limited_independently(self):
        # The fix must not overcorrect into treating every request as one
        # bucket — two different trusted (right-most) hops are two different
        # real clients and must not share a limit.
        for _ in range(10):
            self._failed_login(xff="1.2.3.4, 198.51.100.9")
        other_client_resp = self._failed_login(xff="1.2.3.4, 198.51.100.10")
        self.assertNotEqual(other_client_resp.status_code, 429,
                             "a different trusted client IP must not inherit another's limit")

    def test_direct_connections_with_no_proxy_still_rate_limit_on_remote_addr(self):
        # Local/dev access with no reverse proxy in front at all — ProxyFix
        # falls back to the real socket peer when there's no XFF to trust.
        for i in range(10):
            resp = self._failed_login(xff=None)
            self.assertNotEqual(resp.status_code, 429, f"attempt {i} was already limited")
        self.assertEqual(self._failed_login(xff=None).status_code, 429)


class TrustedProxyConfigTests(unittest.TestCase):
    """#383: waitress's trusted_proxy is a deployment guarantee the rate
    limiter's fix (#382) rests on — "*" trusts any peer's own
    X-Forwarded-For, which is only safe because the shipped compose makes
    this port unreachable except from Traefik. TROBAR_TRUSTED_PROXY lets an
    operator on a different topology pin it instead."""

    def test_defaults_to_wildcard_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self.assertEqual(main._trusted_proxy(), "*")

    def test_honours_an_explicit_value(self):
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": "172.20.0.5"}):
            self.assertEqual(main._trusted_proxy(), "172.20.0.5")

    def test_a_blanked_value_falls_back_to_wildcard_not_empty_string(self):
        # .env.example ships this key uncommented, so TROBAR_TRUSTED_PROXY=
        # (blanked rather than deleted) is a realistic edit — and the worse
        # of the two failure modes: waitress's str_iftruthy coercion turns
        # an empty string into trusted_proxy=None (trust NO peer), not "*"
        # (trust every peer). A plain os.environ.get(key, "*") would let ""
        # sail through as though it had been meaningfully set; this must not.
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": ""}):
            self.assertEqual(main._trusted_proxy(), "*")


class ExposureWarningTests(unittest.TestCase):
    """#389: the distinct-raw-peer-count signal for a deployment that looks
    directly exposed with trusted_proxy left at "*". Pure-logic tests
    manipulate main._exposure_peers directly (same idiom
    RateLimitTrustedProxyTests uses for _rl_failures) rather than driving
    real requests — RecordExposureSampleTests below covers the actual
    werkzeug integration end to end."""

    def setUp(self):
        main._exposure_peers.clear()
        self.addCleanup(main._exposure_peers.clear)

    def _seed(self, n, seen=None):
        seen = time.time() if seen is None else seen
        for i in range(n):
            main._exposure_peers[f"203.0.113.{i}"] = seen

    def test_none_below_threshold(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self._seed(main._EXPOSURE_WARN_THRESHOLD)
            self.assertIsNone(main._exposure_warning())

    def test_warns_above_threshold(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self._seed(main._EXPOSURE_WARN_THRESHOLD + 1)
            self.assertEqual(main._exposure_warning(), main._EXPOSURE_WARN_THRESHOLD + 1)

    def test_none_when_trusted_proxy_is_pinned_even_with_many_peers(self):
        # The pin itself is the acknowledgement — a directly-exposed port
        # can't exploit "*" trust that was never granted in the first place.
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": "172.20.0.5"}):
            self._seed(main._EXPOSURE_WARN_THRESHOLD + 10)
            self.assertIsNone(main._exposure_warning())

    def test_none_in_forward_mode_even_with_many_peers(self):
        # forward mode's real boundary is the ForwardAuth gate; a reachable
        # port is a separate question this signal isn't measuring.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self._seed(main._EXPOSURE_WARN_THRESHOLD + 10)
            with mock.patch.object(main, "AUTH_MODE", "forward"):
                self.assertIsNone(main._exposure_warning())

    def test_stale_peers_outside_the_window_are_not_counted(self):
        # A long-running instance's one real proxy can still get a new
        # address over time (container recreate) without ever being
        # reachable by anyone else — only counting recent peers is what
        # keeps that from slowly crossing the threshold on its own.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            long_ago = time.time() - main._EXPOSURE_WINDOW_S - 3600
            self._seed(main._EXPOSURE_WARN_THRESHOLD + 5, seen=long_ago)
            self.assertIsNone(main._exposure_warning())

    def test_status_shows_the_count_below_threshold(self):
        # Review feedback on #389: unlike _exposure_warning, this always
        # reports the live count while the mechanism is active — a signal
        # that only ever speaks up past its threshold is indistinguishable
        # from one that's silently never seeing anything.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self._seed(2)
            self.assertEqual(main._exposure_status(), 2)

    def test_status_is_none_not_zero_when_trusted_proxy_is_pinned(self):
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": "172.20.0.5"}):
            self.assertIsNone(main._exposure_status())

    def test_status_is_none_not_zero_in_forward_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            with mock.patch.object(main, "AUTH_MODE", "forward"):
                self.assertIsNone(main._exposure_status())


class RecordExposureSampleTests(_RouteTestBase):
    """End-to-end: a real request through the full WSGI stack (ProxyFix
    included) must actually populate _exposure_peers from the raw socket
    peer, not just from a hand-constructed dict — proves the
    werkzeug.proxy_fix.orig assumption holds against the installed
    werkzeug, not just that _exposure_warning's own arithmetic is right."""

    def setUp(self):
        super().setUp()
        main._exposure_peers.clear()
        self.addCleanup(main._exposure_peers.clear)

    def test_a_request_records_its_raw_remote_addr(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self.client.get("/login", environ_overrides={"REMOTE_ADDR": "198.51.100.42"})
        self.assertIn("198.51.100.42", main._exposure_peers)

    def test_the_same_peer_repeated_does_not_grow_the_count(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            for _ in range(5):
                self.client.get("/login", environ_overrides={"REMOTE_ADDR": "198.51.100.42"})
        self.assertEqual(main._exposure_peer_count(), 1)

    def test_different_peers_each_count(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            for i in range(3):
                self.client.get("/login", environ_overrides={"REMOTE_ADDR": f"198.51.100.{i}"})
        self.assertEqual(main._exposure_peer_count(), 3)

    def test_a_spoofed_x_forwarded_for_does_not_change_the_raw_peer_recorded(self):
        # The whole point: the header is attacker-controlled, the raw socket
        # peer isn't. A request claiming a different address every time via
        # XFF must still record as the SAME raw peer if it's really the same
        # connecting socket.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            for i in range(5):
                self.client.get(
                    "/login", environ_overrides={"REMOTE_ADDR": "198.51.100.42"},
                    headers={"X-Forwarded-For": f"203.0.113.{i}"})
        self.assertEqual(main._exposure_peer_count(), 1)

    def test_nothing_recorded_when_trusted_proxy_is_pinned(self):
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": "172.20.0.5"}):
            self.client.get("/login", environ_overrides={"REMOTE_ADDR": "198.51.100.42"})
        self.assertEqual(main._exposure_peer_count(), 0)

    def test_nothing_recorded_in_forward_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            with mock.patch.object(main, "AUTH_MODE", "forward"):
                self.client.get("/login", environ_overrides={"REMOTE_ADDR": "198.51.100.42"})
        self.assertEqual(main._exposure_peer_count(), 0)


class AdminHealthRouteTests(_RouteTestBase):
    """#364/#365: GET /api/admin/health — admin-only. Covers the two new
    fingerprint populations and the two non-category signals (DATA_DIR
    network-fs, last scan time) added alongside the pre-existing categories."""

    def _add_track(self, relative_path, **cols) -> int:
        fields = {
            "relative_path": relative_path, "artist": "A", "album": "B", "title": "C",
            "size": 1, "mtime": 0.0,
        }
        fields.update(cols)
        names = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        cur = self.conn.execute(
            f"INSERT INTO tracks ({names}) VALUES ({placeholders})", tuple(fields.values()))
        self.conn.commit()
        return sync_state._new_id(cur)

    def test_non_admin_gets_403(self):
        _login(self.client, self.other)
        resp = self.client.get("/api/admin/health")
        self.assertEqual(resp.status_code, 403)

    def test_fingerprint_failed_population(self):
        # #364 population A: fingerprint IS NULL AND fingerprint_failed_at IS
        # NOT NULL — the pass gave up on this file, and it's still selected
        # again by every future pass (that's the bug this surfaces).
        self._add_track("bad.flac", fingerprint=None, fingerprint_failed_at="2026-01-01 00:00:00")
        self._add_track("good.flac", fingerprint="FP", duration=180.0)

        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["fingerprint_failed"]["count"], 1)
        self.assertEqual(data["fingerprint_failed"]["items"][0]["relative_path"], "bad.flac")

    def test_unidentified_fingerprints_population(self):
        # #364 population B: fingerprinted and checked, but no ISRC — stamped
        # once by fingerprint.py and never retried. Deliberately not counted
        # as fingerprint_failed (different predicate, different meaning).
        self._add_track(
            "unmatched.flac", fingerprint="FP", duration=180.0,
            fingerprint_checked_at="2026-01-01 00:00:00", acoustid_isrc=None)
        self._add_track(
            "matched.flac", fingerprint="FP", duration=180.0,
            fingerprint_checked_at="2026-01-01 00:00:00", acoustid_isrc="US-ABC-26-00001")

        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/health")
        data = resp.get_json()
        self.assertEqual(data["unidentified_fingerprints"]["count"], 1)

    def test_unidentified_fingerprints_checks_acoustid_isrc_not_tag_isrc(self):
        # #408: tracks.isrc (scanner-populated from the file's own embedded
        # tags) and tracks.acoustid_isrc (fingerprint.py's AcoustID/
        # MusicBrainz backfill) are independent columns — embedded ISRC tags
        # are rare regardless of whether AcoustID found a match, so this
        # must key on acoustid_isrc or nearly every identified track gets
        # miscounted as "not found."
        self._add_track(
            "no_tag_but_identified.flac", fingerprint="FP", duration=180.0,
            fingerprint_checked_at="2026-01-01 00:00:00",
            isrc=None, acoustid_isrc="US-ABC-26-00001")

        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/health")
        data = resp.get_json()
        self.assertEqual(data["unidentified_fingerprints"]["count"], 0)

    def test_no_network_data_dir_by_default(self):
        # The test harness's DATA_DIR is a plain tempdir — never a network fs.
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/health")
        self.assertIsNone(resp.get_json()["data_dir_network_fs"])

    def test_data_dir_network_fs_is_surfaced(self):
        _login(self.client, self.admin)
        with mock.patch.object(db, "data_dir_network_fs", return_value="nfs4"):
            resp = self.client.get("/api/admin/health")
        self.assertEqual(resp.get_json()["data_dir_network_fs"], "nfs4")

    def test_exposure_warning_is_none_by_default(self):
        # A fresh test run's single request from the test client itself is
        # nowhere near the threshold.
        main._exposure_peers.clear()
        self.addCleanup(main._exposure_peers.clear)
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/health")
        self.assertIsNone(resp.get_json()["exposure_warning"])

    def test_exposure_warning_is_surfaced_past_the_threshold(self):
        main._exposure_peers.clear()
        self.addCleanup(main._exposure_peers.clear)
        for i in range(main._EXPOSURE_WARN_THRESHOLD + 1):
            main._exposure_peers[f"203.0.113.{i}"] = time.time()
        _login(self.client, self.admin)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            resp = self.client.get("/api/admin/health")
        # +1 for the admin's own request the assertion above didn't seed.
        self.assertEqual(
            resp.get_json()["exposure_warning"], main._EXPOSURE_WARN_THRESHOLD + 2)

    def test_exposure_peer_count_is_shown_even_below_threshold(self):
        # Review feedback on #389: a signal that only ever speaks up past
        # its threshold is indistinguishable from one that's silently
        # broken. This field is always present (while the mechanism is
        # active) so an operator can tell "quiet and correct" apart from
        # "quiet because it can't see anything here."
        main._exposure_peers.clear()
        self.addCleanup(main._exposure_peers.clear)
        _login(self.client, self.admin)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            resp = self.client.get("/api/admin/health")
        # The admin's own request is the one and only sample so far.
        self.assertEqual(resp.get_json()["exposure_peer_count"], 1)

    def test_exposure_peer_count_is_none_when_trusted_proxy_is_pinned(self):
        main._exposure_peers.clear()
        self.addCleanup(main._exposure_peers.clear)
        _login(self.client, self.admin)
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": "172.20.0.5"}):
            resp = self.client.get("/api/admin/health")
        self.assertIsNone(resp.get_json()["exposure_peer_count"])

    def test_last_scan_finished_at_is_none_before_any_scan(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/health")
        self.assertIsNone(resp.get_json()["last_scan_finished_at"])

    def test_last_scan_finished_at_reflects_the_most_recent_done_scan(self):
        self.conn.execute(
            "INSERT INTO jobs (type, state, finished_at) VALUES (?, 'done', '2026-01-01 00:00:00')",
            (scanner.JOB_TYPE,))
        self.conn.execute(
            "INSERT INTO jobs (type, state, finished_at) VALUES (?, 'done', '2026-06-15 12:00:00')",
            (scanner.JOB_TYPE,))
        # A currently-running scan must not win over the last COMPLETED one.
        self.conn.execute("INSERT INTO jobs (type, state) VALUES (?, 'running')", (scanner.JOB_TYPE,))
        self.conn.commit()

        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/health")
        self.assertEqual(resp.get_json()["last_scan_finished_at"], "2026-06-15 12:00:00")


class AdminConfigMirrorFolderValidationTests(_RouteTestBase):
    """#285: PUT /api/admin/config rejects a mirror_folder equal to or
    nested inside MUSIC_ROOT — the concrete fix for the self-import
    feedback loop filesystem_client.py's .m3u discovery would otherwise
    cause (it walks the whole of MUSIC_ROOT with no exclusion mechanism)."""

    def setUp(self):
        super().setUp()
        self._music_root = Path(tempfile.mkdtemp(prefix="trobar-test-music-root-", dir=_TMP))
        db.set_config(self.conn, "music_root", str(self._music_root))
        self.conn.commit()

    def test_rejects_mirror_folder_equal_to_music_root(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"mirror_folder": str(self._music_root)})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_mirror_folder_nested_inside_music_root(self):
        _login(self.client, self.admin)
        resp = self.client.put(
            "/api/admin/config", json={"mirror_folder": str(self._music_root / "mirrors")}
        )
        self.assertEqual(resp.status_code, 400)

    def test_accepts_a_separate_mirror_folder(self):
        separate = Path(_TMP) / "a-separate-mirror-dir"
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"mirror_folder": str(separate)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "mirror_folder"), str(separate))

    def test_rejects_a_dotdot_path_that_resolves_into_music_root(self):
        # #294: an unnormalized candidate compared lexically against
        # music_root (Path equality/`.parents` don't collapse '..') let a
        # path like MUSIC_ROOT/sub/.. — which IS MUSIC_ROOT — sail through
        # the containment check.
        evasive = str(self._music_root / "sub" / "..")
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"mirror_folder": evasive})
        self.assertEqual(resp.status_code, 400)

    def test_dotdot_in_an_accepted_mirror_folder_is_normalized_before_storing(self):
        # mirror.py's _safe_path() compares this stored value against a
        # normalized join, so storing it un-normalized broke every write
        # when the admin-typed path contained '..' — normalize at save
        # time too, not just defensively in _safe_path().
        separate = Path(_TMP) / "a-separate-mirror-dir"
        raw = str(separate / "a" / ".." / "b")
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"mirror_folder": raw})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "mirror_folder"), str(separate / "b"))


class AdminConfigUrlValidationTests(_RouteTestBase):
    """#509: PUT /api/admin/config used to save a malformed URL (missing
    colon, no scheme, no host) happily in any of eleven provider fields —
    the bad value only surfaced later, at request time, as a connection
    error indistinguishable from "the target is genuinely unreachable".
    _is_valid_url() (scheme http/https + a host present) now gates every
    one of them before the corresponding reconnect()/set_config call, so a
    typo fails fast with a clear message instead. One test per field,
    looped rather than duplicated eleven times almost-identically, since
    that's what actually proves the issue's own enumerated list is fully
    covered rather than trusting the shared helper by inference."""

    # Each entry: the malformed payload that must 400 (and, for fields with
    # required companions, the extra keys that make the block's own
    # all-fields-truthy gate fire at all -- without those, the malformed
    # URL alone wouldn't even reach _is_valid_url(), since the reconnect
    # block it lives in is a no-op until every required field is present),
    # plus the field label _invalid_url_message() must name in the error.
    # #512 review: a save of ANY unrelated setting resends the WHOLE
    # adminConfig object, so an already-saved bad URL from before this
    # validation existed re-triggers on every future save regardless of
    # what's actually being changed -- a fully generic message left up to
    # eleven candidates to check by hand. Pinning the label here is what
    # proves that fix, not just that a 400 happens at all.
    _MALFORMED_URL_PAYLOADS = [
        ({"subsonic_url": "http//bad", "subsonic_username": "u", "subsonic_password": "p"},
         "Subsonic URL"),
        ({"jellyfin_url": "http//bad", "jellyfin_api_key": "k", "jellyfin_username": "u"},
         "Jellyfin URL"),
        ({"emby_url": "http//bad", "emby_api_key": "k", "emby_username": "u"}, "Emby URL"),
        ({"plex_url": "http//bad", "plex_token": "t"}, "Plex URL"),
        ({"lms_url": "http//bad"}, "LMS URL"),
        ({"mirror_subsonic_url": "http//bad", "mirror_subsonic_username": "u",
          "mirror_subsonic_password": "p"}, "Subsonic mirror-target URL"),
        ({"mirror_jellyfin_url": "http//bad", "mirror_jellyfin_api_key": "k",
          "mirror_jellyfin_username": "u"}, "Jellyfin mirror-target URL"),
        ({"mirror_emby_url": "http//bad", "mirror_emby_api_key": "k",
          "mirror_emby_username": "u"}, "Emby mirror-target URL"),
        ({"lidarr_url": "http//bad", "lidarr_api_key": "k"}, "Lidarr URL"),
        ({"lastfm_api_base": "http//bad"}, "Last.fm API base URL"),
        ({"listenbrainz_api_base": "http//bad"}, "ListenBrainz API base URL"),
    ]

    def test_every_enumerated_field_rejects_a_malformed_url_and_names_itself(self):
        _login(self.client, self.admin)
        for payload, field_label in self._MALFORMED_URL_PAYLOADS:
            with self.subTest(payload=payload):
                resp = self.client.put("/api/admin/config", json=payload)
                self.assertEqual(resp.status_code, 400, payload)
                error = resp.get_json()["error"]
                self.assertIn("valid", error.lower())
                self.assertIn(field_label, error, payload)

    def test_a_url_with_no_scheme_at_all_is_rejected(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"lastfm_api_base": "example.com"})
        self.assertEqual(resp.status_code, 400)

    def test_a_non_http_scheme_is_rejected(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"lastfm_api_base": "ftp://example.com"})
        self.assertEqual(resp.status_code, 400)

    def test_blank_optional_url_fields_are_still_allowed_unset(self):
        # #509 doesn't touch the existing "blank means opt out" contract for
        # lastfm_api_base/listenbrainz_api_base -- only non-blank values get
        # the well-formedness check.
        _login(self.client, self.admin)
        resp = self.client.put(
            "/api/admin/config", json={"lastfm_api_base": "", "listenbrainz_api_base": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(db.get_config(self.conn, "lastfm_api_base"))

    def test_a_well_formed_standalone_url_field_still_saves(self):
        _login(self.client, self.admin)
        resp = self.client.put(
            "/api/admin/config", json={"lastfm_api_base": "https://libre.fm/2.0/"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "lastfm_api_base"), "https://libre.fm/2.0/")

    def test_a_well_formed_connect_tuple_url_still_reconnects(self):
        # lidarr_url as the representative connect-tuple case (unlike
        # lastfm_api_base above, this one drives an actual reconnect() call)
        # -- proves the new check sits BEFORE reconnect, not instead of it.
        resp_mock = mock.Mock()
        resp_mock.status_code = 200
        resp_mock.content = b"x"
        resp_mock.json.return_value = {"version": "3.1.0.4875"}
        _login(self.client, self.admin)
        with mock.patch("requests.request", return_value=resp_mock) as req:
            resp = self.client.put("/api/admin/config", json={
                "lidarr_url": "http://lidarr.example.com:8686", "lidarr_api_key": "key1",
            })
        self.assertEqual(resp.status_code, 200)
        req.assert_called()
        self.assertEqual(db.get_config(self.conn, "lidarr_url"), "http://lidarr.example.com:8686")


class AdminConfigTestConnectionRouteTests(_RouteTestBase):
    """#509 item 3: POST /api/admin/config/test-connection — the admin
    config form's live pre-save check, the non-persisting counterpart to
    PUT /api/admin/config's bulk save. Dispatches to each provider's own
    (mocked here) test_connection(); this class's own job is the
    dispatch/validation logic around that call, not re-testing what each
    client module's test_connection() itself does (see e.g.
    test_subsonic_client.TestConnectionTests for that)."""

    def test_non_admin_gets_403(self):
        _login(self.client, self.other)
        resp = self.client.post("/api/admin/config/test-connection", json={
            "provider": "subsonic", "url": "http://nav.local", "username": "u", "password": "p",
        })
        self.assertEqual(resp.status_code, 403)

    def test_unknown_provider_gets_400(self):
        _login(self.client, self.admin)
        resp = self.client.post("/api/admin/config/test-connection", json={"provider": "not-a-real-provider"})
        self.assertEqual(resp.status_code, 400)

    def test_a_missing_required_field_gets_400(self):
        _login(self.client, self.admin)
        resp = self.client.post("/api/admin/config/test-connection", json={
            "provider": "subsonic", "url": "http://nav.local", "username": "u",
            # password omitted
        })
        self.assertEqual(resp.status_code, 400)

    def test_a_malformed_url_short_circuits_before_calling_the_client(self):
        _login(self.client, self.admin)
        with mock.patch("subsonic_client.test_connection") as test_connection:
            resp = self.client.post("/api/admin/config/test-connection", json={
                "provider": "subsonic", "url": "http//nav.local", "username": "u", "password": "p",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"state": "invalid_url"})
        test_connection.assert_not_called()

    def test_a_good_response_round_trips(self):
        _login(self.client, self.admin)
        with mock.patch("subsonic_client.test_connection",
                         return_value={"state": "paired", "url": "http://nav.local", "provider": "subsonic"}):
            resp = self.client.post("/api/admin/config/test-connection", json={
                "provider": "subsonic", "url": "http://nav.local", "username": "u", "password": "p",
            })
        self.assertEqual(resp.get_json()["state"], "paired")

    def test_never_touches_the_database(self):
        # The property the whole feature rests on -- see this route's own
        # docstring.
        with mock.patch("subsonic_client.test_connection",
                         return_value={"state": "paired", "url": "http://nav.local", "provider": "subsonic"}):
            _login(self.client, self.admin)
            self.client.post("/api/admin/config/test-connection", json={
                "provider": "subsonic", "url": "http://nav.local", "username": "u", "password": "p",
            })
        self.assertIsNone(db.get_config(self.conn, "subsonic_url"))

    def test_mirror_subsonic_dispatches_to_the_same_client_function_as_subsonic(self):
        # "Is this server reachable with these creds" doesn't depend on
        # which config namespace the answer will end up persisted under —
        # see _TEST_CONNECTION_PROVIDERS' own comment.
        _login(self.client, self.admin)
        with mock.patch("subsonic_client.test_connection",
                         return_value={"state": "paired", "url": "http://nav.local", "provider": "subsonic"}) as tc:
            resp = self.client.post("/api/admin/config/test-connection", json={
                "provider": "mirror_subsonic", "url": "http://nav.local", "username": "u", "password": "p",
            })
        self.assertEqual(resp.get_json()["state"], "paired")
        tc.assert_called_once_with("http://nav.local", "u", "p")

    def test_lms_only_requires_the_url_field(self):
        # LMS's own "Authorize" setting is off by default -- username/
        # password stay optional here too, unlike every other provider.
        _login(self.client, self.admin)
        with mock.patch("lms_client.test_connection",
                         return_value={"state": "paired", "url": "http://lms.local", "provider": "lms"}) as tc:
            resp = self.client.post("/api/admin/config/test-connection", json={
                "provider": "lms", "url": "http://lms.local",
            })
        self.assertEqual(resp.get_json()["state"], "paired")
        tc.assert_called_once_with("http://lms.local", "", "")

    def test_lms_still_requires_the_url_itself(self):
        _login(self.client, self.admin)
        resp = self.client.post("/api/admin/config/test-connection", json={"provider": "lms"})
        self.assertEqual(resp.status_code, 400)


class AdminConfigMirrorSubsonicTests(_RouteTestBase):
    """#189 review: PUT /api/admin/config's Subsonic mirror-TARGET triple —
    the explicit clear path (this write target has no "switch away"
    mechanism the way an active-provider connection does) and the
    mirror_subsonic_state field GET now exposes."""

    def _mock_response(self, json_body):
        resp = mock.Mock()
        resp.json.return_value = json_body
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        return resp

    def test_setting_all_three_persists_and_pings(self):
        _login(self.client, self.admin)
        with mock.patch("requests.get", return_value=self._mock_response(
                {"subsonic-response": {"status": "ok"}})) as get:
            resp = self.client.put("/api/admin/config", json={
                "mirror_subsonic_url": "http://nav.example.com",
                "mirror_subsonic_username": "trobar",
                "mirror_subsonic_password": "secret",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "mirror_subsonic_url"), "http://nav.example.com")
        # Two pings: one inside mirror_reconnect() itself (to report
        # whether the new credentials work), one from the route building
        # its own GET-shaped response afterward.
        self.assertEqual(get.call_count, 2)
        self.assertEqual(resp.get_json()["mirror_subsonic_state"], "paired")

    def test_blanking_all_three_clears_the_stored_config(self):
        db.set_config(self.conn, "mirror_subsonic_url", "http://nav.example.com")
        db.set_config(self.conn, "mirror_subsonic_username", "trobar")
        db.set_config(self.conn, "mirror_subsonic_password", "secret")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("requests.get") as get:
            resp = self.client.put("/api/admin/config", json={
                "mirror_subsonic_url": "", "mirror_subsonic_username": "", "mirror_subsonic_password": "",
            })
        self.assertEqual(resp.status_code, 200)
        get.assert_not_called()  # nothing to ping once cleared
        self.assertIsNone(db.get_config(self.conn, "mirror_subsonic_url"))
        self.assertIsNone(db.get_config(self.conn, "mirror_subsonic_username"))
        self.assertIsNone(db.get_config(self.conn, "mirror_subsonic_password"))

    def test_a_payload_that_never_mentions_the_triple_leaves_it_untouched(self):
        # The clear path is gated on the key's presence specifically so an
        # unrelated partial update (job_retention_days here) can't wipe a
        # configured mirror target as a side effect.
        db.set_config(self.conn, "mirror_subsonic_url", "http://nav.example.com")
        db.set_config(self.conn, "mirror_subsonic_username", "trobar")
        db.set_config(self.conn, "mirror_subsonic_password", "secret")
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"job_retention_days": 14})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "mirror_subsonic_url"), "http://nav.example.com")

    def test_get_reports_disconnected_state_when_unconfigured(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.get_json()["mirror_subsonic_state"], "disconnected")


class AdminConfigMirrorJellyfinTests(_RouteTestBase):
    """#189: PUT /api/admin/config's Jellyfin mirror-TARGET triple — same
    shape as AdminConfigMirrorSubsonicTests above (the explicit clear path,
    the mirror_jellyfin_state field GET now exposes), adapted for
    Jellyfin's user-id-resolution step inside mirror_reconnect()."""

    def _mock_response(self, json_body, status_code=200):
        resp = mock.Mock()
        resp.status_code = status_code
        resp.content = b"x"
        resp.json.return_value = json_body
        return resp

    def test_setting_all_three_persists_resolves_the_user_id_and_pings(self):
        _login(self.client, self.admin)
        with mock.patch("requests.request", side_effect=[
            self._mock_response([{"Name": "trobar", "Id": "u1"}]),  # GET /Users in reconnect
            self._mock_response({"Id": "u1"}),                      # GET /Users/u1 in mirror_status
            self._mock_response({"Id": "u1"}),                      # GET /Users/u1 for the route's own GET response
        ]) as req:
            resp = self.client.put("/api/admin/config", json={
                "mirror_jellyfin_url": "http://jf.example.com",
                "mirror_jellyfin_api_key": "key",
                "mirror_jellyfin_username": "trobar",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "mirror_jellyfin_url"), "http://jf.example.com")
        self.assertEqual(db.get_config(self.conn, "mirror_jellyfin_user_id"), "u1")
        self.assertEqual(req.call_count, 3)
        self.assertEqual(resp.get_json()["mirror_jellyfin_state"], "paired")

    def test_blanking_all_three_clears_the_stored_config(self):
        db.set_config(self.conn, "mirror_jellyfin_url", "http://jf.example.com")
        db.set_config(self.conn, "mirror_jellyfin_api_key", "key")
        db.set_config(self.conn, "mirror_jellyfin_username", "trobar")
        db.set_config(self.conn, "mirror_jellyfin_user_id", "u1")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("requests.request") as req:
            resp = self.client.put("/api/admin/config", json={
                "mirror_jellyfin_url": "", "mirror_jellyfin_api_key": "", "mirror_jellyfin_username": "",
            })
        self.assertEqual(resp.status_code, 200)
        req.assert_not_called()  # nothing to ping once cleared
        self.assertIsNone(db.get_config(self.conn, "mirror_jellyfin_url"))
        self.assertIsNone(db.get_config(self.conn, "mirror_jellyfin_api_key"))
        self.assertIsNone(db.get_config(self.conn, "mirror_jellyfin_username"))
        self.assertIsNone(db.get_config(self.conn, "mirror_jellyfin_user_id"))

    def test_a_payload_that_never_mentions_the_triple_leaves_it_untouched(self):
        # Same reason as the Subsonic sink's equivalent test: the clear
        # path is gated on the key's presence specifically so an
        # unrelated partial update can't wipe a configured mirror target
        # as a side effect.
        db.set_config(self.conn, "mirror_jellyfin_url", "http://jf.example.com")
        db.set_config(self.conn, "mirror_jellyfin_api_key", "key")
        db.set_config(self.conn, "mirror_jellyfin_username", "trobar")
        db.set_config(self.conn, "mirror_jellyfin_user_id", "u1")
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"job_retention_days": 14})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "mirror_jellyfin_url"), "http://jf.example.com")

    def test_get_reports_disconnected_state_when_unconfigured(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.get_json()["mirror_jellyfin_state"], "disconnected")


class AdminConfigMirrorEmbyTests(_RouteTestBase):
    """#189: PUT /api/admin/config's Emby mirror-TARGET triple — same shape
    as AdminConfigMirrorJellyfinTests above, adapted for one real
    difference: emby_client's mirror_reconnect()/mirror_status() (unlike
    jellyfin_client's) still go through _get() -> requests.get() directly,
    not the mirror-target write path's requests.request() — see
    emby_client.py's own _get() docstring for why that split exists."""

    def _mock_response(self, json_body, status_code=200):
        resp = mock.Mock()
        resp.status_code = status_code
        resp.content = b"x"
        resp.json.return_value = json_body
        resp.raise_for_status.return_value = None
        return resp

    def test_setting_all_three_persists_resolves_the_user_id_and_pings(self):
        _login(self.client, self.admin)
        with mock.patch("requests.get", side_effect=[
            self._mock_response([{"Name": "admin", "Id": "u1"}]),  # GET /Users in reconnect
            self._mock_response({"Id": "u1"}),                      # GET /Users/u1 in mirror_status
            self._mock_response({"Id": "u1"}),                      # GET /Users/u1 for the route's own GET response
        ]) as get:
            resp = self.client.put("/api/admin/config", json={
                "mirror_emby_url": "http://emby.example.com",
                "mirror_emby_api_key": "key",
                "mirror_emby_username": "admin",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "mirror_emby_url"), "http://emby.example.com")
        self.assertEqual(db.get_config(self.conn, "mirror_emby_user_id"), "u1")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(resp.get_json()["mirror_emby_state"], "paired")

    def test_blanking_all_three_clears_the_stored_config(self):
        db.set_config(self.conn, "mirror_emby_url", "http://emby.example.com")
        db.set_config(self.conn, "mirror_emby_api_key", "key")
        db.set_config(self.conn, "mirror_emby_username", "admin")
        db.set_config(self.conn, "mirror_emby_user_id", "u1")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("requests.get") as get:
            resp = self.client.put("/api/admin/config", json={
                "mirror_emby_url": "", "mirror_emby_api_key": "", "mirror_emby_username": "",
            })
        self.assertEqual(resp.status_code, 200)
        get.assert_not_called()  # nothing to ping once cleared
        self.assertIsNone(db.get_config(self.conn, "mirror_emby_url"))
        self.assertIsNone(db.get_config(self.conn, "mirror_emby_api_key"))
        self.assertIsNone(db.get_config(self.conn, "mirror_emby_username"))
        self.assertIsNone(db.get_config(self.conn, "mirror_emby_user_id"))

    def test_a_payload_that_never_mentions_the_triple_leaves_it_untouched(self):
        # Same reason as the Subsonic/Jellyfin sinks' equivalent tests: the
        # clear path is gated on the key's presence specifically so an
        # unrelated partial update can't wipe a configured mirror target
        # as a side effect.
        db.set_config(self.conn, "mirror_emby_url", "http://emby.example.com")
        db.set_config(self.conn, "mirror_emby_api_key", "key")
        db.set_config(self.conn, "mirror_emby_username", "admin")
        db.set_config(self.conn, "mirror_emby_user_id", "u1")
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"job_retention_days": 14})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "mirror_emby_url"), "http://emby.example.com")

    def test_get_reports_disconnected_state_when_unconfigured(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.get_json()["mirror_emby_state"], "disconnected")


class AdminUserProviderMappingRouteTests(_RouteTestBase):
    """#262: GET /api/admin/jellyfin-users + PUT .../users/<id>/jellyfin-user
    (and the Emby equivalents) — the per-Trobar-user account mapping, same
    admin-only/fuzzy-suggestion shape as the pre-existing (and, it turns
    out, itself untested) Roon profile mapping. Both providers share one
    implementation (main.py's _admin_provider_user_mapping()), so these
    tests exercise it through both route pairs rather than duplicating a
    third time for an internal helper."""

    def test_non_admin_gets_403(self):
        _login(self.client, self.other)
        resp = self.client.get("/api/admin/jellyfin-users")
        self.assertEqual(resp.status_code, 403)

    def test_lists_target_users_and_trobar_users_with_a_suggestion(self):
        # "bob" is close enough to a target-server "bob" account to
        # suggest it; "owner" has no close match.
        with mock.patch.object(
            main.jellyfin_client, "list_users",
            return_value={"status": "ok", "users": [{"id": "jf1", "name": "bob"}, {"id": "jf2", "name": "carol"}]},
        ):
            _login(self.client, self.admin)
            resp = self.client.get("/api/admin/jellyfin-users")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["target_users"], [{"id": "jf1", "name": "bob"}, {"id": "jf2", "name": "carol"}])
        by_username = {u["username"]: u for u in data["users"]}
        self.assertEqual(by_username["bob"]["suggested_id"], "jf1")
        self.assertEqual(by_username["bob"]["suggested_name"], "bob")
        self.assertIsNone(by_username["owner"]["suggested_id"])

    def test_an_already_mapped_user_gets_no_suggestion(self):
        # #262 review-analog to the Roon mapping: don't suggest over an
        # existing choice, even if a closer name match exists.
        self.conn.execute("UPDATE users SET jellyfin_user_id = 'jf9' WHERE id = ?", (self.other,))
        self.conn.commit()
        with mock.patch.object(
            main.jellyfin_client, "list_users",
            return_value={"status": "ok", "users": [{"id": "jf1", "name": "bob"}]},
        ):
            _login(self.client, self.admin)
            resp = self.client.get("/api/admin/jellyfin-users")
        by_username = {u["username"]: u for u in resp.get_json()["users"]}
        self.assertEqual(by_username["bob"]["mapped_id"], "jf9")
        self.assertIsNone(by_username["bob"]["suggested_id"])

    def test_propagates_a_not_paired_target_server(self):
        with mock.patch.object(main.jellyfin_client, "list_users",
                                return_value={"status": "error", "reason": "not_paired"}):
            _login(self.client, self.admin)
            resp = self.client.get("/api/admin/jellyfin-users")
        self.assertEqual(resp.get_json(), {"status": "error", "reason": "not_paired"})

    def test_put_persists_the_mapping(self):
        _login(self.client, self.admin)
        resp = self.client.put(f"/api/admin/users/{self.other}/jellyfin-user", json={"jellyfin_user_id": "jf1"})
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute("SELECT jellyfin_user_id FROM users WHERE id = ?", (self.other,)).fetchone()
        self.assertEqual(row["jellyfin_user_id"], "jf1")

    def test_put_blank_clears_the_mapping(self):
        self.conn.execute("UPDATE users SET jellyfin_user_id = 'jf1' WHERE id = ?", (self.other,))
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.put(f"/api/admin/users/{self.other}/jellyfin-user", json={"jellyfin_user_id": ""})
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute("SELECT jellyfin_user_id FROM users WHERE id = ?", (self.other,)).fetchone()
        self.assertIsNone(row["jellyfin_user_id"])

    def test_put_non_admin_gets_403(self):
        _login(self.client, self.other)
        resp = self.client.put(f"/api/admin/users/{self.other}/jellyfin-user", json={"jellyfin_user_id": "jf1"})
        self.assertEqual(resp.status_code, 403)

    def test_emby_routes_use_the_emby_column_and_client(self):
        # Not a full duplicate of the Jellyfin tests above — just confirms
        # the shared helper is actually wired to the right client/column
        # for the OTHER provider, since that's the one thing a shared
        # implementation could get backwards.
        with mock.patch.object(
            main.emby_client, "list_users",
            return_value={"status": "ok", "users": [{"id": "eb1", "name": "bob"}]},
        ):
            _login(self.client, self.admin)
            resp = self.client.get("/api/admin/emby-users")
        by_username = {u["username"]: u for u in resp.get_json()["users"]}
        self.assertEqual(by_username["bob"]["suggested_id"], "eb1")

        resp = self.client.put(f"/api/admin/users/{self.other}/emby-user", json={"emby_user_id": "eb1"})
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute("SELECT emby_user_id, jellyfin_user_id FROM users WHERE id = ?", (self.other,)).fetchone()
        self.assertEqual(row["emby_user_id"], "eb1")
        self.assertIsNone(row["jellyfin_user_id"])  # the two mappings are independent


class AdminConfigJobRetentionValidationTests(_RouteTestBase):
    """#361: PUT /api/admin/config's job_retention_days — the only knob
    #361 settled on (the per-type collapse and failed-jobs handling are
    fixed behaviour in jobs._prune_finished, not configurable)."""

    def test_defaults_to_seven_when_never_set(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.get_json()["job_retention_days"], 7)

    def test_accepts_a_valid_value(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"job_retention_days": 14})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "job_retention_days"), "14")

    def test_rejects_zero_and_negative_values(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"job_retention_days": 0})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_a_non_numeric_value(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"job_retention_days": "soon"})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_an_absurdly_large_value(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"job_retention_days": 999999})
        self.assertEqual(resp.status_code, 400)


class AdminConfigScanIntervalValidationTests(_RouteTestBase):
    """#362: PUT /api/admin/config's scan_interval_hours — 0 means off, the
    issue's explicit "defaulting to off" decision, so 0 is a valid value
    (unlike job_retention_days, which has no off state)."""

    def test_defaults_to_off(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.get_json()["scan_interval_hours"], 0)

    def test_zero_is_accepted_as_off(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"scan_interval_hours": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "scan_interval_hours"), "0")

    def test_accepts_a_valid_interval(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"scan_interval_hours": 24})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "scan_interval_hours"), "24")

    def test_rejects_a_negative_value(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"scan_interval_hours": -1})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_a_non_numeric_value(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"scan_interval_hours": "soon"})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_more_than_a_year(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"scan_interval_hours": 8761})
        self.assertEqual(resp.status_code, 400)

    def test_next_scheduled_scan_at_is_none_when_off(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertIsNone(resp.get_json()["next_scheduled_scan_at"])

    def test_next_scheduled_scan_at_is_populated_once_enabled_and_scanned(self):
        self.conn.execute(
            "INSERT INTO jobs (type, state, finished_at) VALUES (?, 'done', datetime('now'))",
            (scanner.JOB_TYPE,))
        self.conn.commit()
        _login(self.client, self.admin)
        self.client.put("/api/admin/config", json={"scan_interval_hours": 24})
        resp = self.client.get("/api/admin/config")
        self.assertIsNotNone(resp.get_json()["next_scheduled_scan_at"])


class AdminConfigSpotifyExperimentalToggleTests(_RouteTestBase):
    """#398: the admin-facing side of the experimental toggle — GET reports
    it, PUT sets it independently of the credential fields, and it defaults
    off (db.init_db() in setUp seeds it with no credentials configured)."""

    def test_get_reports_off_by_default(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertFalse(resp.get_json()["experimental_spotify_enabled"])

    def test_put_enables_it(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"experimental_spotify_enabled": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["experimental_spotify_enabled"])
        self.assertEqual(db.get_config(self.conn, "experimental_spotify_enabled"), "1")

    def test_put_disables_it_again(self):
        db.set_config(self.conn, "experimental_spotify_enabled", "1")
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"experimental_spotify_enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["experimental_spotify_enabled"])

    def test_toggling_it_off_does_not_touch_stored_credentials(self):
        # #398's decision: turning the feature off pauses it, it doesn't
        # discard spotify_client_id/secret -- re-enabling needs no re-entry.
        db.set_config(self.conn, "spotify_client_id", "cid")
        db.set_config(self.conn, "spotify_client_secret", "csec")
        db.set_config(self.conn, "experimental_spotify_enabled", "1")
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"experimental_spotify_enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "spotify_client_id"), "cid")
        self.assertEqual(db.get_config(self.conn, "spotify_client_secret"), "csec")


class AdminJobsRouteTests(_RouteTestBase):
    """#297 step 2: the admin background-jobs panel — the user-visible payoff
    of the queue. Before it, a failed scan or backfill left nothing but a log
    line the admin would never read."""

    def _add_job(self, job_type="demo", state="queued", attempts=0, last_error=None,
                 dedupe_key=None):
        cur = self.conn.execute(
            "INSERT INTO jobs (type, state, attempts, last_error, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_type, state, attempts, last_error, dedupe_key))
        self.conn.commit()
        return sync_state._new_id(cur)

    def test_retry_409s_instead_of_500ing_when_the_work_is_already_queued(self):
        # The dedupe index covers ('queued','running'), so flipping a failed
        # job back to queued while another with the same key is pending would
        # violate it — an unhandled IntegrityError, i.e. a 500. Reachable on the
        # real job type: the backfill exhausts its retries, the next library
        # scan queues a fresh one, and the failed row is still in the panel.
        failed = self._add_job(job_type="fingerprint_backfill", state="failed", attempts=3,
                               dedupe_key="fingerprint_backfill")
        self._add_job(job_type="fingerprint_backfill", state="queued",
                      dedupe_key="fingerprint_backfill")
        _login(self.client, self.admin)
        resp = self.client.post(f"/api/admin/jobs/{failed}/retry",
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already queued", resp.get_json()["error"])
        # the failed row is left alone rather than half-updated
        self.assertEqual(
            self.conn.execute("SELECT state FROM jobs WHERE id = ?", (failed,)).fetchone()[0],
            "failed")

    def test_retry_still_works_when_no_competing_job_holds_the_key(self):
        # The guard must not block the ordinary case it's protecting.
        failed = self._add_job(job_type="fingerprint_backfill", state="failed", attempts=3,
                               dedupe_key="fingerprint_backfill")
        _login(self.client, self.admin)
        resp = self.client.post(f"/api/admin/jobs/{failed}/retry",
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.conn.execute("SELECT state FROM jobs WHERE id = ?", (failed,)).fetchone()[0],
            "queued")

    def test_retry_of_a_keyless_job_skips_the_dedupe_check(self):
        # A NULL dedupe_key never collides (the index is partial on NOT NULL),
        # so those jobs must retry unimpeded even with others queued.
        failed = self._add_job(state="failed", attempts=3, dedupe_key=None)
        self._add_job(state="queued", dedupe_key=None)
        _login(self.client, self.admin)
        resp = self.client.post(f"/api/admin/jobs/{failed}/retry",
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200)

    def test_overview_returns_counts_and_recent_jobs(self):
        self._add_job(state="queued")
        self._add_job(state="failed", attempts=3, last_error="boom")
        _login(self.client, self.admin)
        body = self.client.get("/api/admin/jobs").get_json()
        self.assertEqual(body["counts"]["queued"], 1)
        self.assertEqual(body["counts"]["failed"], 1)
        self.assertEqual(len(body["jobs"]), 2)
        self.assertEqual(body["jobs"][0]["state"], "failed")  # newest first

    def test_overview_is_admin_only(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/api/admin/jobs").status_code, 403)

    def test_overview_requires_a_session(self):
        self.assertIn(self.client.get("/api/admin/jobs").status_code, (302, 401, 403))

    def test_retry_requeues_a_failed_job_and_resets_its_budget(self):
        job_id = self._add_job(state="failed", attempts=3, last_error="boom")
        _login(self.client, self.admin)
        resp = self.client.post(f"/api/admin/jobs/{job_id}/retry",
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute(
            "SELECT state, attempts, run_after FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["state"], "queued")
        # Budget reset, so a fix gets a real chance rather than one leftover try.
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["run_after"])

    def test_retry_refuses_a_running_job(self):
        # 'Retrying' a running job would produce a second concurrent copy of
        # work already in flight — exactly what the dedupe index prevents.
        job_id = self._add_job(state="running", attempts=1)
        _login(self.client, self.admin)
        resp = self.client.post(f"/api/admin/jobs/{job_id}/retry",
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            self.conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0],
            "running")

    def test_retry_404s_on_an_unknown_job(self):
        _login(self.client, self.admin)
        resp = self.client.post("/api/admin/jobs/99999/retry",
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 404)

    def test_retry_is_admin_only(self):
        job_id = self._add_job(state="failed")
        _login(self.client, self.owner)
        resp = self.client.post(f"/api/admin/jobs/{job_id}/retry",
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 403)

    def test_cancel_removes_a_queued_job(self):
        job_id = self._add_job(state="queued")
        _login(self.client, self.admin)
        resp = self.client.delete(f"/api/admin/jobs/{job_id}",
                                  headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def test_cancel_refuses_a_running_job_with_a_clear_reason(self):
        # A running handler is mid-decode or mid-HTTP; it can't be interrupted
        # safely without cooperative handlers, so this refuses rather than
        # pretending to cancel and silently doing nothing.
        job_id = self._add_job(state="running", attempts=1)
        _login(self.client, self.admin)
        resp = self.client.delete(f"/api/admin/jobs/{job_id}",
                                  headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already started", resp.get_json()["error"])
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def test_cancel_is_admin_only(self):
        job_id = self._add_job(state="queued")
        _login(self.client, self.owner)
        resp = self.client.delete(f"/api/admin/jobs/{job_id}",
                                  headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone())


class SetupCompleteRouteTests(_RouteTestBase):
    """#309: POST /api/setup/complete starts the first scan in the BACKGROUND.

    It used to run the synchronous scan, so the wizard's last step blocked for a
    whole first index — which behind a reverse proxy outlives the read timeout
    and shows a fetch failure even though everything worked. Reproduced in the
    wild behind Traefik."""

    def setUp(self):
        super().setUp()
        self._root = Path(tempfile.mkdtemp(prefix="trobar-test-setup-root-", dir=_TMP))
        db.set_config(self.conn, "music_root", str(self._root))
        self.conn.commit()

    def _completed(self):
        # A fresh connection: the route commits on its own.
        conn = db.get_conn()
        try:
            return bool(db.get_config(conn, "setup_completed"))
        finally:
            conn.close()

    def _post(self):
        return self.client.post("/api/setup/complete",
                                headers={"Origin": "http://localhost"})

    def test_it_starts_the_scan_in_the_background_not_synchronously(self):
        # The actual bug: blocking on scan_library is what the proxy timed out on.
        _login(self.client, self.admin)
        with mock.patch.object(main.scanner, "start_scan",
                               return_value={"status": "started"}) as start, \
             mock.patch.object(main.scanner, "scan_library") as sync_scan:
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"scan_started": True})
        start.assert_called_once()
        sync_scan.assert_not_called()
        self.assertTrue(self._completed())

    def test_an_unreadable_music_root_is_rejected_BEFORE_setup_is_committed(self):
        # The ordering that makes "stay in the wizard" possible at all. If
        # setup_completed were committed first, a reload would bounce the user
        # to an empty main UI with no route back to fix the path.
        db.set_config(self.conn, "music_root", str(self._root / "does-not-exist"))
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch.object(main.scanner, "start_scan") as start:
            resp = self._post()
        self.assertEqual(resp.status_code, 400)
        start.assert_not_called()
        self.assertFalse(self._completed(), "setup must NOT be marked done")
        self.assertIn("music folder", resp.get_json()["error"])

    def test_an_already_running_scan_counts_as_success(self):
        # A scan being underway is the desired end state; the wizard shouldn't
        # refuse to finish over a technicality.
        _login(self.client, self.admin)
        with mock.patch.object(main.scanner, "start_scan",
                               return_value={"status": "error", "already_running": True}):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["scan_started"])
        self.assertTrue(self._completed())

    def test_a_scan_that_cannot_start_still_completes_setup(self):
        # Past the commit nothing may trap the user: report it, but let them in.
        # The Library tab's Rescan is right there.
        _login(self.client, self.admin)
        with mock.patch.object(main.scanner, "start_scan",
                               side_effect=RuntimeError("no threads")):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["scan_started"])
        self.assertTrue(self._completed())

    def test_it_is_admin_only(self):
        _login(self.client, self.owner)
        with mock.patch.object(main.scanner, "start_scan") as start:
            resp = self._post()
        self.assertEqual(resp.status_code, 403)
        start.assert_not_called()
        self.assertFalse(self._completed())


class SelectionCreationEnforcementTests(_RouteTestBase):
    """POST /api/selections and /api/selections/toggle-device —
    _require_playlist_visible() blocks selecting an owned-and-unshared
    playlist someone else set private (the enforcement half of #28, not
    just hiding it from the list)."""

    def setUp(self):
        super().setUp()
        self.private = self._make_playlist("Private", owner_user_id=self.owner, shared=0)
        self.shared = self._make_playlist("Shared", owner_user_id=self.owner, shared=1)
        # each user needs a device to attach a selection to
        self.owner_device, _ = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.other_device, _ = sync_state.create_device(self.conn, self.other, "bob-dev")
        self.conn.commit()

    def _post_selection(self, user_id, target, device_id):
        _login(self.client, user_id)
        return self.client.post("/api/selections", json={
            "type": "playlist", "target": str(target), "device_ids": [device_id]})

    def test_non_owner_cannot_select_private_playlist(self):
        resp = self._post_selection(self.other, self.private, self.other_device)
        self.assertEqual(resp.status_code, 403)
        # The abort must fire BEFORE create_selection — assert no row leaked
        # through, pinning the ordering in api_selections, not just the code.
        self.conn.close()
        self.conn = db.get_conn()
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM selections WHERE type='playlist' AND target=?",
            (str(self.private),)).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_owner_can_select_own_private_playlist(self):
        resp = self._post_selection(self.owner, self.private, self.owner_device)
        self.assertEqual(resp.status_code, 200)

    def test_anyone_can_select_a_shared_playlist(self):
        resp = self._post_selection(self.other, self.shared, self.other_device)
        self.assertEqual(resp.status_code, 200)

    def test_toggle_device_also_enforces_on_check(self):
        _login(self.client, self.other)
        resp = self.client.post("/api/selections/toggle-device", json={
            "type": "playlist", "target": str(self.private),
            "device_id": self.other_device, "checked": True})
        self.assertEqual(resp.status_code, 403)

    def test_toggle_device_unchecking_is_exempt_from_the_visibility_gate(self):
        # #68's deliberate design (mirrored in _require_playlist_visible's
        # own docstring): unchecking only ever REMOVES access, so the
        # visibility gate is intentionally NOT applied to checked=False.
        # A non-owner de-selecting an owned-and-unshared playlist must not
        # 403 — a future "tighten this" refactor adding the check to the
        # uncheck direction would silently break a non-owner's ability to
        # remove a playlist they're dropping, and this pins it as intended.
        _login(self.client, self.other)
        resp = self.client.post("/api/selections/toggle-device", json={
            "type": "playlist", "target": str(self.private),
            "device_id": self.other_device, "checked": False})
        self.assertEqual(resp.status_code, 200)


class BasketTargetParsingConsistencyTests(_RouteTestBase):
    """#434: _require_playlist_visible() (the visibility gate, in main.py)
    and sync_state.list_basket() (the reader) used to parse a basket
    target with two different parsers -- SQLite's CAST(? AS INTEGER)
    (leading-integer prefix) in the gate, a bare int() (PEP-515
    underscores, whitespace) in the reader. A target like '1_0' could
    therefore be authorized against one real playlist and served back as a
    completely different one -- the object checked was not the object
    returned.

    #434 review (PR #469): the first fix (share sync_state.parse_target_
    id()'s strict parse in both places) wasn't sufficient on its own --
    treating an unparseable target as "no such playlist" made the GATE
    permissive, but sync_state._device_playlists() still resolves a
    selection's target with the SAME raw CAST(? AS INTEGER) when writing
    a device's .m3u8, so a malformed target let through here could still
    be reinterpreted as a REAL (possibly private) playlist downstream.
    The actual fix: reject a malformed target outright at the one shared
    write-side gate, so it can never become a stored selection/basket-item
    for any reader, strict or CAST-based, to resolve differently."""

    def setUp(self):
        super().setUp()
        self.private = self._make_playlist("Private", owner_user_id=self.owner, shared=0)
        self.other_device, _ = sync_state.create_device(self.conn, self.other, "bob-basket-dev")
        self.conn.commit()

    def test_underscore_separated_target_is_rejected_not_stored(self):
        # Exactly the shape that used to diverge: CAST would read only the
        # leading digits (this real, private playlist's id) while int()
        # would read the whole string as a different number entirely.
        # Neither parser can resolve it under the strict rules, so it must
        # never be written at all -- not stored-but-missing, rejected.
        target = f"{self.private}_0"
        _login(self.client, self.other)
        resp = self.client.post("/api/basket", json={
            "type": "playlist", "target": target, "device_ids": [self.other_device]})
        self.assertEqual(resp.status_code, 400)

        basket = self.client.get("/api/basket").get_json()
        self.assertFalse(any(i["target"] == target for i in basket))

    def test_selections_post_also_rejects_a_malformed_target(self):
        # Same shared gate, the other call site -- POST /api/selections
        # must refuse it too, and create nothing.
        device, _ = sync_state.create_device(self.conn, self.other, "bob-dev")
        self.conn.commit()
        target = f"{self.private}_0"
        _login(self.client, self.other)
        resp = self.client.post("/api/selections", json={
            "type": "playlist", "target": target, "device_ids": [device]})
        self.assertEqual(resp.status_code, 400)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM selections WHERE target=?", (target,)).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_well_formed_nonexistent_target_stays_lax(self):
        # The OTHER case the fix must not break: a well-formed id that
        # simply doesn't exist (e.g. a deleted playlist) is not malformed,
        # and this app's existing behaviour already tolerates it -- still
        # accepted, still resolves to a "missing" item, not rejected.
        target = "999999"
        _login(self.client, self.other)
        resp = self.client.post("/api/basket", json={
            "type": "playlist", "target": target, "device_ids": [self.other_device]})
        self.assertEqual(resp.status_code, 200)
        basket = self.client.get("/api/basket").get_json()
        item = next(i for i in basket if i["target"] == target)
        self.assertTrue(item["missing"])


class SelectionTypeValidationTests(_RouteTestBase):
    """#352: `type` is validated at the API boundary for both routes that
    persist one — an unrecognised type used to be accepted silently and
    just resolve to zero tracks at sync time, with nothing telling the
    caller anything was wrong."""

    def setUp(self):
        super().setUp()
        self.device, _ = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.conn.commit()
        _login(self.client, self.owner)

    def test_post_selections_rejects_an_unknown_type(self):
        resp = self.client.post("/api/selections", json={
            "type": "banana", "target": "x", "device_ids": [self.device]})
        self.assertEqual(resp.status_code, 400)
        n = self.conn.execute("SELECT COUNT(*) AS n FROM selections").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_post_selections_rejects_autofit(self):
        # autofit is only ever created internally (sync_state.refresh_autofit)
        # and resolved by selection id against autofit_tracks, not by a
        # target string — never legitimate through this route.
        resp = self.client.post("/api/selections", json={
            "type": "autofit", "target": "x", "device_ids": [self.device]})
        self.assertEqual(resp.status_code, 400)

    def test_post_selections_accepts_every_valid_type(self):
        for sel_type, target in (("artist", "A"), ("album", "A||B"), ("track", "1")):
            resp = self.client.post("/api/selections", json={
                "type": sel_type, "target": target, "device_ids": [self.device]})
            self.assertEqual(resp.status_code, 200, sel_type)

    def test_post_basket_rejects_an_unknown_type(self):
        resp = self.client.post("/api/basket", json={"type": "banana", "target": "x"})
        self.assertEqual(resp.status_code, 400)
        n = self.conn.execute("SELECT COUNT(*) AS n FROM basket_items").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_post_basket_rejects_a_non_numeric_playlist_target(self):
        # #434 review (PR #469): POST no longer accepts this at all --
        # a malformed playlist target must never become a stored row,
        # since a downstream reader still using SQLite's CAST(? AS
        # INTEGER) (sync_state._device_playlists) could reinterpret it as
        # a real, possibly-private playlist.
        resp = self.client.post("/api/basket", json={
            "type": "playlist", "target": "not-a-number", "device_ids": [self.device]})
        self.assertEqual(resp.status_code, 400)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM basket_items WHERE target='not-a-number'").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_get_basket_survives_a_non_numeric_playlist_target_from_before_this_gate_existed(self):
        # #424's original scenario, still real: a malformed playlist
        # target can still exist from a hand-edited DB or a row written
        # before this validation was added -- inserted directly here
        # since POST itself refuses it now (previous test). GET must
        # still load the whole basket, not 500 on this one row.
        self.conn.execute(
            "INSERT INTO basket_items (user_id, type, target) VALUES (?, 'playlist', 'not-a-number')",
            (self.owner,))
        self.conn.commit()
        resp = self.client.get("/api/basket")
        self.assertEqual(resp.status_code, 200)
        items = resp.get_json()
        item = next(i for i in items if i["target"] == "not-a-number")
        self.assertTrue(item["missing"])


class BasketFanOutTests(_RouteTestBase):
    """#351: POST /api/basket/fan-out is atomic (a mid-loop failure must not
    leave some items converted to real selections while the basket still
    holds all of them — the natural retry would then recreate the ones that
    already succeeded), and refuses an empty device list instead of
    silently clearing accumulated picks with nothing to show for it."""

    def setUp(self):
        super().setUp()
        self.device, _ = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.conn.commit()
        _login(self.client, self.owner)

    def _add_to_basket(self, item_type, target):
        resp = self.client.post("/api/basket", json={
            "type": item_type, "target": target, "device_ids": [self.device]})
        self.assertEqual(resp.status_code, 200)

    def _basket_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM basket_items WHERE user_id = ?", (self.owner,)
        ).fetchone()["n"]

    def _selection_count(self):
        return self.conn.execute("SELECT COUNT(*) AS n FROM selections").fetchone()["n"]

    def test_empty_device_ids_is_rejected_and_the_basket_is_untouched(self):
        self._add_to_basket("artist", "A")
        resp = self.client.post("/api/basket/fan-out", json={"device_ids": []})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._basket_count(), 1)
        self.assertEqual(self._selection_count(), 0)

    def test_a_mid_loop_failure_leaves_the_basket_intact_and_creates_nothing(self):
        self._add_to_basket("artist", "A")
        self._add_to_basket("artist", "B")
        real_create_selection = sync_state.create_selection
        calls = []

        def flaky(conn, sel_type, target, user_id, device_ids, **kwargs):
            calls.append(target)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real_create_selection(conn, sel_type, target, user_id, device_ids, **kwargs)

        with mock.patch.object(sync_state, "create_selection", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self.client.post("/api/basket/fan-out", json={"device_ids": [self.device]})
        # Item A's real create_selection call ran and would have committed
        # on its own outside a transaction — the whole point of #351 is that
        # it doesn't survive item B's failure.
        self.assertEqual(self._basket_count(), 2)
        self.assertEqual(self._selection_count(), 0)

    def test_a_successful_fan_out_creates_selections_and_clears_the_basket(self):
        self._add_to_basket("artist", "A")
        self._add_to_basket("artist", "B")
        resp = self.client.post("/api/basket/fan-out", json={"device_ids": [self.device]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["count"], 2)
        self.assertEqual(self._basket_count(), 0)
        self.assertEqual(self._selection_count(), 2)

    def _insert_legacy_row(self, item_type, target):
        # Simulates a basket_items row from before #501 (or #434, or type
        # validation existed at all) -- inserted directly with its own
        # basket_item_devices link, since the API itself no longer accepts
        # a row shaped like this. Without the device link this row
        # wouldn't even be part of ANY fan-out's relevant_items -- linking
        # it to self.device is what makes it "this device's section has
        # one bad row in it", the actual case being tested.
        cur = self.conn.execute(
            "INSERT INTO basket_items (user_id, type, target) VALUES (?, ?, ?)",
            (self.owner, item_type, target))
        item_id = sync_state._new_id(cur)
        self.conn.execute(
            "INSERT INTO basket_item_devices (basket_item_id, device_id) VALUES (?, ?)",
            (item_id, self.device))
        self.conn.commit()

    def test_a_legacy_invalid_type_row_is_skipped_not_fatal(self):
        self._add_to_basket("artist", "A")
        self._insert_legacy_row("banana", "x")
        resp = self.client.post("/api/basket/fan-out", json={"device_ids": [self.device]})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["skipped"], 1)

    def test_a_legacy_malformed_playlist_target_is_skipped_not_fatal(self):
        # #471: the same category of legacy-bad-row as the type case above,
        # inserted directly since POST /api/basket now rejects it outright
        # (#434) -- simulates a row from before that gate existed (#424 is
        # exactly why these exist). Must not 400 the whole fan-out over one
        # bad row, same as an unknown type doesn't.
        self._add_to_basket("artist", "A")
        self._insert_legacy_row("playlist", "1_0")
        resp = self.client.post("/api/basket/fan-out", json={"device_ids": [self.device]})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["skipped"], 1)


class PerDeviceBasketTests(_RouteTestBase):
    """#501: staging is per-device now -- an item can be linked to several
    devices, and a fan-out to N devices must only ever touch each item's
    OWN links, never apply an item to a device it was never staged for
    just because that device also appeared in the same request."""

    def setUp(self):
        super().setUp()
        self.device_a, _ = sync_state.create_device(self.conn, self.owner, "phone")
        self.device_b, _ = sync_state.create_device(self.conn, self.owner, "tablet")
        self.conn.commit()
        _login(self.client, self.owner)

    def _stage(self, item_type, target, device_ids):
        resp = self.client.post("/api/basket", json={
            "type": item_type, "target": target, "device_ids": device_ids})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["id"]

    def _device_ids_for(self, item_id):
        item = next(i for i in self.client.get("/api/basket").get_json() if i["id"] == item_id)
        return item["device_ids"]

    def _selections_for(self, sel_type, target):
        return self.conn.execute(
            "SELECT sd.device_id FROM selections s "
            "JOIN selection_devices sd ON sd.selection_id = s.id "
            "WHERE s.type = ? AND s.target = ?", (sel_type, target),
        ).fetchall()

    def test_post_basket_requires_at_least_one_device(self):
        resp = self.client.post("/api/basket", json={"type": "artist", "target": "A", "device_ids": []})
        self.assertEqual(resp.status_code, 400)
        n = self.conn.execute("SELECT COUNT(*) AS n FROM basket_items").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_post_basket_requires_access_to_every_named_device(self):
        other_device, _ = sync_state.create_device(self.conn, self.other, "not-mine")
        self.conn.commit()
        resp = self.client.post("/api/basket", json={
            "type": "artist", "target": "A", "device_ids": [other_device]})
        self.assertEqual(resp.status_code, 403)

    def test_get_basket_reports_each_items_device_ids(self):
        item_id = self._stage("artist", "A", [self.device_a, self.device_b])
        self.assertEqual(sorted(self._device_ids_for(item_id)), sorted([self.device_a, self.device_b]))

    def test_cross_device_isolation_an_item_staged_for_one_device_never_reaches_another(self):
        # The correctness-critical case: X is staged only for A, Y for both
        # A and B. Fanning out to [A, B] in one call must send X to A
        # only, never to B just because B was also in this request.
        self._stage("artist", "X", [self.device_a])
        self._stage("artist", "Y", [self.device_a, self.device_b])
        resp = self.client.post(
            "/api/basket/fan-out", json={"device_ids": [self.device_a, self.device_b]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["count"], 2)

        x_devices = {row["device_id"] for row in self._selections_for("artist", "X")}
        y_devices = {row["device_id"] for row in self._selections_for("artist", "Y")}
        self.assertEqual(x_devices, {self.device_a})
        self.assertEqual(y_devices, {self.device_a, self.device_b})

    def test_partial_unlink_sending_one_device_leaves_the_item_staged_for_the_other(self):
        item_id = self._stage("artist", "A", [self.device_a, self.device_b])
        resp = self.client.post("/api/basket/fan-out", json={"device_ids": [self.device_a]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["count"], 1)

        # Still in the basket, still staged for device_b -- device_a's
        # link (and only that one) was consumed by the send above.
        self.assertEqual(self._device_ids_for(item_id), [self.device_b])
        devices = {row["device_id"] for row in self._selections_for("artist", "A")}
        self.assertEqual(devices, {self.device_a})

    def test_full_removal_sending_an_items_last_device_removes_it_from_the_basket(self):
        item_id = self._stage("artist", "A", [self.device_a])
        resp = self.client.post("/api/basket/fan-out", json={"device_ids": [self.device_a]})
        self.assertEqual(resp.status_code, 200)
        basket = self.client.get("/api/basket").get_json()
        self.assertFalse(any(i["id"] == item_id for i in basket))

    def test_a_device_not_in_the_request_is_untouched_even_if_it_has_pending_items(self):
        item_id = self._stage("artist", "A", [self.device_b])
        resp = self.client.post("/api/basket/fan-out", json={"device_ids": [self.device_a]})
        self.assertEqual(resp.status_code, 200)
        # Nothing relevant to device_a was in the basket -- a clean no-op,
        # and device_b's own untouched section still holds the item.
        self.assertEqual(resp.get_json(), {"status": "ok", "count": 0, "skipped": 0})
        self.assertEqual(self._device_ids_for(item_id), [self.device_b])

    def test_delete_item_device_unstages_just_that_device(self):
        item_id = self._stage("artist", "A", [self.device_a, self.device_b])
        resp = self.client.delete(f"/api/basket/{item_id}/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._device_ids_for(item_id), [self.device_b])

    def test_delete_item_device_removes_the_item_when_it_was_the_last_device(self):
        item_id = self._stage("artist", "A", [self.device_a])
        resp = self.client.delete(f"/api/basket/{item_id}/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 200)
        basket = self.client.get("/api/basket").get_json()
        self.assertFalse(any(i["id"] == item_id for i in basket))

    def test_delete_item_device_is_scoped_to_the_owning_user(self):
        item_id = self._stage("artist", "A", [self.device_a])
        _login(self.client, self.other)
        resp = self.client.delete(f"/api/basket/{item_id}/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 200)  # silent no-op, matches DELETE /api/basket/<id>
        _login(self.client, self.owner)
        self.assertEqual(self._device_ids_for(item_id), [self.device_a])


class BasketFanOutDelegationTests(_RouteTestBase):
    """#349 (decided 2026-07-28): a basket CAN fan out to a delegated
    device -- this needed no new code, since api_basket_fan_out()'s
    existing _require_device_access() call already permits "owner, admin,
    or anyone the owner has granted delegation over". Pins the two
    decided invariants so a future refactor of either guard can't quietly
    break them: the fan-out itself succeeds, and _require_playlist_visible
    is evaluated against the ACTOR, never the destination device's owner —
    so a delegate's own private playlist is a valid fan-out source even
    though the receiving device belongs to someone else entirely."""

    def setUp(self):
        super().setUp()
        # self.other is delegated over self.owner -- self.other can act on
        # self.owner's devices, matching #349's Alice-delegated-over-Bob
        # scenario (self.other is "Alice", self.owner is "Bob" here).
        self.conn.execute(
            "INSERT INTO device_delegations (grantee_user_id, target_user_id) VALUES (?, ?)",
            (self.other, self.owner))
        self.conn.commit()
        self.target_device, _ = sync_state.create_device(
            self.conn, self.owner, "Owner's DAP", "sdcard")
        # Owned by the DELEGATE (self.other), private -- the exact case
        # #349 reasons about: the actor's own unshared playlist, not the
        # destination device owner's.
        self.private_playlist = self._make_playlist(
            "Delegate's Private Mix", owner_user_id=self.other, shared=0)

    def test_delegate_fans_out_own_private_playlist_to_delegated_device(self):
        _login(self.client, self.other)
        add_resp = self.client.post("/api/basket", json={
            "type": "playlist", "target": str(self.private_playlist),
            "device_ids": [self.target_device]})
        self.assertEqual(add_resp.status_code, 200)

        resp = self.client.post(
            "/api/basket/fan-out", json={"device_ids": [self.target_device]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["count"], 1)

        selection = self.conn.execute(
            "SELECT s.id FROM selections s "
            "JOIN selection_devices sd ON sd.selection_id = s.id "
            "WHERE sd.device_id = ? AND s.type = 'playlist' AND s.target = ?",
            (self.target_device, str(self.private_playlist)),
        ).fetchone()
        self.assertIsNotNone(selection)


class RetroactiveUnshareTests(_RouteTestBase):
    """#73/#74 — unsharing a playlist retroactively revokes non-owner
    selections (and hides them from GET /api/selections), not just blocks
    new ones."""

    def setUp(self):
        super().setUp()
        self.private = self._make_playlist("P", owner_user_id=self.owner, shared=1)  # starts shared
        self.owner_device, _ = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.other_device, _ = sync_state.create_device(self.conn, self.other, "bob-dev")
        self.admin_device, _ = sync_state.create_device(self.conn, self.admin, "admin-dev")
        self.conn.commit()
        # Everyone selects it while it's still shared (allowed).
        for uid, dev in ((self.owner, self.owner_device), (self.other, self.other_device),
                         (self.admin, self.admin_device)):
            sync_state.create_selection(self.conn, "playlist", str(self.private), uid, [dev])

    def _selections_created_by(self, user_id):
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM selections WHERE type='playlist' AND target=? AND created_by_user_id=?",
            (str(self.private), user_id))}

    def test_unshare_revokes_only_the_non_owner_selection(self):
        before_other = self._selections_created_by(self.other)
        _login(self.client, self.owner)
        resp = self.client.patch(f"/api/provider/playlists/{self.private}", json={"shared": False})
        self.assertEqual(resp.status_code, 200)
        # Reopen — the PATCH ran on its own connection.
        self.conn.close()
        self.conn = db.get_conn()
        # bob's selection is gone; owner's and admin's survive.
        self.assertEqual(self._selections_created_by(self.other), set())
        self.assertTrue(before_other)  # sanity: it existed before
        self.assertTrue(self._selections_created_by(self.owner))
        self.assertTrue(self._selections_created_by(self.admin))

    def test_unshared_playlist_selection_hidden_from_non_owner_list(self):
        _login(self.client, self.owner)
        self.client.patch(f"/api/provider/playlists/{self.private}", json={"shared": False})
        # Even if a stale selection somehow remained, GET /api/selections
        # must not surface a now-private playlist to a non-owner. Manufacture
        # one directly to test the filter independently of the revocation.
        self.conn.close()
        self.conn = db.get_conn()
        cur = self.conn.execute(
            "INSERT INTO selections (type, target, created_by_user_id) VALUES ('playlist', ?, ?)",
            (str(self.private), self.other))
        stale = sync_state._new_id(cur)
        self.conn.execute(
            "INSERT INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
            (stale, self.other_device))
        self.conn.commit()

        _login(self.client, self.other)
        resp = self.client.get("/api/selections")
        self.assertEqual(resp.status_code, 200)
        targets = {(s["type"], s["target"]) for s in resp.get_json()}
        self.assertNotIn(("playlist", str(self.private)), targets)

        # ...but the admin still sees everything.
        _login(self.client, self.admin)
        admin_targets = {(s["type"], s["target"]) for s in self.client.get("/api/selections").get_json()}
        self.assertIn(("playlist", str(self.private)), admin_targets)


class RevokeHelperTests(_RouteTestBase):
    """_revoke_non_owner_playlist_selections() as a conn-only unit — the
    core of #74, exercised directly."""

    def setUp(self):
        super().setUp()
        self.pl = self._make_playlist("P", owner_user_id=self.owner, shared=1)
        self.owner_device, _ = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.other_device, _ = sync_state.create_device(self.conn, self.other, "bob-dev")
        self.admin_device, _ = sync_state.create_device(self.conn, self.admin, "admin-dev")
        self.conn.commit()
        self.owner_sel = sync_state.create_selection(
            self.conn, "playlist", str(self.pl), self.owner, [self.owner_device])
        self.other_sel = sync_state.create_selection(
            self.conn, "playlist", str(self.pl), self.other, [self.other_device])
        self.admin_sel = sync_state.create_selection(
            self.conn, "playlist", str(self.pl), self.admin, [self.admin_device])

    def _exists(self, sel_id):
        return self.conn.execute("SELECT 1 FROM selections WHERE id=?", (sel_id,)).fetchone() is not None

    def test_revokes_non_owner_keeps_owner_and_admin(self):
        main._revoke_non_owner_playlist_selections(self.conn, self.pl, self.owner)
        self.conn.commit()
        self.assertFalse(self._exists(self.other_sel))  # non-owner, revoked
        self.assertTrue(self._exists(self.owner_sel))   # owner, kept
        self.assertTrue(self._exists(self.admin_sel))   # admin, kept


class FirstRunBootstrapTests(unittest.TestCase):
    """#96: the first-run admin claim in local mode must honour
    ADMIN_USERNAME so a stranger can't POST /login on a fresh instance and
    seize the sole admin account. Its own harness (no seeded users — the
    bootstrap branch only fires when the users table is empty)."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _admins(self):
        conn = db.get_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT username, is_admin FROM users").fetchall()]
        finally:
            conn.close()

    def test_wrong_username_rejected_when_admin_username_set(self):
        from unittest import mock
        with mock.patch.object(main, "ADMIN_USERNAME", "owner"):
            resp = self.client.post("/login", data={"username": "attacker", "password": "pw123456"})
        self.assertEqual(resp.status_code, 200)  # re-rendered form, not a redirect
        self.assertNotIn("/", resp.headers.get("Location", ""))
        self.assertEqual(self._admins(), [])  # no account created at all

    def test_matching_username_bootstraps_admin_when_admin_username_set(self):
        from unittest import mock
        with mock.patch.object(main, "ADMIN_USERNAME", "owner"):
            resp = self.client.post("/login", data={"username": "owner", "password": "pw123456"})
        self.assertEqual(resp.status_code, 302)  # redirected into the app
        self.assertEqual(self._admins(), [{"username": "owner", "is_admin": 1}])

    def test_any_username_bootstraps_when_admin_username_unset(self):
        # Unchanged legacy behaviour when ADMIN_USERNAME is not configured —
        # the documented "don't expose before first login" caveat covers it.
        from unittest import mock
        with mock.patch.object(main, "ADMIN_USERNAME", ""):
            resp = self.client.post("/login", data={"username": "whoever", "password": "pw123456"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._admins(), [{"username": "whoever", "is_admin": 1}])

    def test_wrong_username_rejected_even_after_repeated_attempts_no_account(self):
        # The rejection path records a failure (rate-limited like a login) and
        # never creates a row — a would-be squatter can't accrete an account.
        from unittest import mock
        with mock.patch.object(main, "ADMIN_USERNAME", "owner"):
            for _ in range(3):
                self.client.post("/login", data={"username": "attacker", "password": "pw123456"})
        self.assertEqual(self._admins(), [])


class DeviceEndpointTests(_RouteTestBase):
    """#97 (honour transcode_format on create), #99 (CSRF Origin check must
    still cover the plural /api/devices web API), #100 (input validation:
    missing name -> 400 not 500; max_size_bytes must be a non-negative int)."""

    def setUp(self):
        super().setUp()
        _login(self.client, self.owner)

    def _post(self, payload, origin=None):
        headers = {"Origin": origin} if origin else {}
        return self.client.post("/api/devices", json=payload, headers=headers)

    # --- #100: input validation ---
    def test_missing_name_is_400_not_500(self):
        self.assertEqual(self._post({}).status_code, 400)

    def test_blank_name_is_400(self):
        self.assertEqual(self._post({"name": "   "}).status_code, 400)

    def test_negative_max_size_is_400(self):
        self.assertEqual(self._post({"name": "x", "max_size_bytes": -9999}).status_code, 400)

    def test_non_integer_max_size_is_400(self):
        self.assertEqual(self._post({"name": "x", "max_size_bytes": "lots"}).status_code, 400)

    def test_boolean_max_size_is_400(self):
        # bool is an int subclass — must not slip through as 0/1.
        self.assertEqual(self._post({"name": "x", "max_size_bytes": True}).status_code, 400)

    def test_is_own_and_is_pinned_are_real_json_booleans_not_0_1(self):
        # #449: _device_rows_for_user() is shared with the read-only
        # integration API — coerced once there so both endpoints agree,
        # rather than only the caller that happened to notice. assertIs
        # (not assertTrue) so a regression back to SQLite's 0/1 ints would
        # actually fail this, not just silently pass on truthiness.
        self._post({"name": "Phone"})
        row = self.client.get("/api/devices").get_json()[0]
        self.assertIs(row["is_own"], True)
        self.assertIs(row["is_pinned"], False)

    def test_valid_create_with_limit_ok(self):
        self.assertEqual(self._post({"name": "Phone", "max_size_bytes": 1000}).status_code, 200)

    def test_null_max_size_ok(self):
        self.assertEqual(self._post({"name": "Phone", "max_size_bytes": None}).status_code, 200)

    def test_patch_negative_max_size_is_400(self):
        did = self._post({"name": "Phone"}).get_json()["id"]
        r = self.client.patch(f"/api/devices/{did}", json={"max_size_bytes": -1})
        self.assertEqual(r.status_code, 400)

    def test_absurdly_large_max_size_is_400_not_500(self):
        # Above signed 64-bit: would OverflowError on INSERT (a 500) without
        # the upper-bound guard.
        r = self._post({"name": "x", "max_size_bytes": 2**63})
        self.assertEqual(r.status_code, 400)

    def test_max_size_at_the_limit_is_ok(self):
        r = self._post({"name": "Big", "max_size_bytes": 2**63 - 1})
        self.assertEqual(r.status_code, 200)

    # --- #97: transcode_format / artist_images honoured on create ---
    def test_transcode_format_honoured_on_create(self):
        r = self._post({"name": "DAP", "device_type": "dap", "transcode_format": "mp3_320"})
        self.assertEqual(r.status_code, 200)
        did = r.get_json()["id"]
        row = self.conn.execute("SELECT transcode_format FROM devices WHERE id=?", (did,)).fetchone()
        self.assertEqual(row["transcode_format"], "mp3_320")

    def test_artist_images_honoured_on_create(self):
        did = self._post({"name": "DAP", "artist_images": "full"}).get_json()["id"]
        row = self.conn.execute("SELECT artist_images FROM devices WHERE id=?", (did,)).fetchone()
        self.assertEqual(row["artist_images"], "full")

    def test_invalid_transcode_format_on_create_is_400(self):
        self.assertEqual(self._post({"name": "DAP", "transcode_format": "flacsupreme"}).status_code, 400)

    # --- #99: CSRF Origin check now covers /api/devices* ---
    def test_cross_origin_post_to_devices_is_blocked(self):
        self.assertEqual(self._post({"name": "x"}, origin="https://evil.example").status_code, 403)

    def test_cross_origin_regenerate_token_is_blocked(self):
        did = self._post({"name": "x"}).get_json()["id"]
        r = self.client.post(f"/api/devices/{did}/regenerate-token",
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_post_to_devices_ok(self):
        self.assertEqual(self._post({"name": "x"}, origin="http://localhost").status_code, 200)

    def test_bearer_device_api_still_exempt_from_origin_check(self):
        # A cross-origin POST to the Bearer token API must NOT be CSRF-blocked
        # (it carries no session cookie) — it fails auth (401) instead of 403.
        r = self.client.post("/api/device/ack", headers={"Origin": "https://evil.example"}, json={})
        self.assertNotEqual(r.status_code, 403)


class DeviceTransferRouteTests(_RouteTestBase):
    """#440: POST /api/devices/<new>/transfer-from — "this device replaces
    that one". Mechanics (settings/track-state/selection reassignment) are
    covered in test_sync_state.TransferDeviceTests; this is the route-level
    permission surface, which is the part that's actually security-
    sensitive (two devices, possibly two owners)."""

    def setUp(self):
        super().setUp()
        _login(self.client, self.owner)

    def _transfer(self, new_device_id, from_device_id):
        return self.client.post(
            f"/api/devices/{new_device_id}/transfer-from", json={"from_device_id": from_device_id})

    def test_same_owner_transfer_succeeds_and_deletes_old_device(self):
        old, _ = sync_state.create_device(self.conn, self.owner, "Old", "phone", max_size_bytes=5000)
        new, _ = sync_state.create_device(self.conn, self.owner, "New", "phone")
        resp = self._transfer(new, old)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.conn.execute("SELECT id FROM devices WHERE id=?", (old,)).fetchone())
        new_row = self.conn.execute("SELECT max_size_bytes FROM devices WHERE id=?", (new,)).fetchone()
        self.assertEqual(new_row["max_size_bytes"], 5000)

    def test_assume_present_defaults_to_false_over_the_route(self):
        # #442 review: omitting assume_present entirely (what the request
        # body actually looks like before the UI checkbox is ever touched)
        # must resolve to the safe default, not an unset/None that skips
        # the downgrade -- bool(body.get("assume_present", False)) is what
        # makes that true; this pins it at the HTTP layer, not just unit-
        # tests sync_state.transfer_device's own default parameter.
        old, _ = sync_state.create_device(self.conn, self.owner, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.owner, "New", "phone")
        sync_state.create_selection(self.conn, "artist", "A", self.owner, [old])
        t1 = sync_state._new_id(self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('a/1.flac', 'A', 'H', '1', 1000, 0)"))
        self.conn.execute(
            "INSERT OR REPLACE INTO device_track_state (device_id, track_id, status) VALUES (?, ?, 'downloaded')",
            (old, t1))
        self.conn.commit()

        resp = self.client.post(f"/api/devices/{new}/transfer-from", json={"from_device_id": old})

        self.assertEqual(resp.status_code, 200)
        status = self.conn.execute(
            "SELECT status FROM device_track_state WHERE device_id=? AND track_id=?", (new, t1)).fetchone()
        self.assertEqual(status["status"], "pending")

    def test_assume_present_true_is_honoured_over_the_route(self):
        old, _ = sync_state.create_device(self.conn, self.owner, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.owner, "New", "phone")
        sync_state.create_selection(self.conn, "artist", "A", self.owner, [old])
        t1 = sync_state._new_id(self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('a/1.flac', 'A', 'H', '1', 1000, 0)"))
        self.conn.execute(
            "INSERT OR REPLACE INTO device_track_state (device_id, track_id, status) VALUES (?, ?, 'downloaded')",
            (old, t1))
        self.conn.commit()

        resp = self.client.post(
            f"/api/devices/{new}/transfer-from", json={"from_device_id": old, "assume_present": True})

        self.assertEqual(resp.status_code, 200)
        status = self.conn.execute(
            "SELECT status FROM device_track_state WHERE device_id=? AND track_id=?", (new, t1)).fetchone()
        self.assertEqual(status["status"], "downloaded")

    def test_device_cannot_replace_itself(self):
        did, _ = sync_state.create_device(self.conn, self.owner, "Solo", "phone")
        self.assertEqual(self._transfer(did, did).status_code, 400)

    def test_transfer_from_a_nonexistent_device_is_404(self):
        new, _ = sync_state.create_device(self.conn, self.owner, "New", "phone")
        self.assertEqual(self._transfer(new, 999999).status_code, 404)

    def test_transfer_onto_a_nonexistent_device_is_404(self):
        old, _ = sync_state.create_device(self.conn, self.owner, "Old", "phone")
        self.assertEqual(self._transfer(999999, old).status_code, 404)

    def test_a_device_the_caller_does_not_manage_is_403(self):
        # Neither device belongs to (or is delegated to) the logged-in owner.
        old, _ = sync_state.create_device(self.conn, self.other, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.other, "New", "phone")
        self.assertEqual(self._transfer(new, old).status_code, 403)

    def test_cross_owner_transfer_without_admin_is_403_even_with_delegation(self):
        # #440's own callout: a delegate has legitimate access to both the
        # other person's OLD device (via delegation) and their OWN device
        # (as its owner) — which would otherwise let them siphon someone
        # else's synced content onto their own hardware. Plain per-device
        # _require_device_access alone would let this through; the route
        # must additionally require admin once the two devices' owners differ.
        self.conn.execute(
            "INSERT INTO device_delegations (grantee_user_id, target_user_id) VALUES (?, ?)",
            (self.owner, self.other))
        self.conn.commit()
        old, _ = sync_state.create_device(self.conn, self.other, "Other's Old Phone", "phone")
        new, _ = sync_state.create_device(self.conn, self.owner, "My Phone", "phone")
        resp = self._transfer(new, old)
        self.assertEqual(resp.status_code, 403)
        # And nothing was actually transferred.
        self.assertIsNotNone(self.conn.execute("SELECT id FROM devices WHERE id=?", (old,)).fetchone())

    def test_cross_owner_transfer_by_admin_succeeds(self):
        old, _ = sync_state.create_device(self.conn, self.other, "Other's Old Phone", "phone")
        new, _ = sync_state.create_device(self.conn, self.owner, "My Phone", "phone")
        _login(self.client, self.admin)
        resp = self._transfer(new, old)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.conn.execute("SELECT id FROM devices WHERE id=?", (old,)).fetchone())

    def test_cross_origin_transfer_is_blocked(self):
        old, _ = sync_state.create_device(self.conn, self.owner, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.owner, "New", "phone")
        resp = self.client.post(
            f"/api/devices/{new}/transfer-from", json={"from_device_id": old},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)
        # Origin-blocked, not actually applied.
        self.assertIsNotNone(self.conn.execute("SELECT id FROM devices WHERE id=?", (old,)).fetchone())


class IntegrationTokenRouteTests(_RouteTestBase):
    """#446/#474: session-authenticated management of integration tokens —
    create/list/revoke. Admin-only, unlike device management or the
    earlier api_tokens/action_tokens split (both of which were per-user,
    mintable by anyone) -- see db.py's integration_tokens comment for why
    that changed. Scoped to the admin who created a given token (an admin
    still only sees/revokes their own), same DELETE-with-WHERE ownership
    pattern as before."""

    def setUp(self):
        super().setUp()
        self.admin2 = self._make_user("admin2", is_admin=True)
        _login(self.client, self.admin)

    def test_create_returns_id_name_and_the_raw_token_once(self):
        resp = self.client.post("/api/integration-tokens", json={"name": "Home Assistant"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["name"], "Home Assistant")
        self.assertIn("token", body)
        self.assertIsInstance(body["id"], int)

    def test_blank_name_is_400(self):
        resp = self.client.post("/api/integration-tokens", json={"name": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_list_never_includes_the_raw_token(self):
        self.client.post("/api/integration-tokens", json={"name": "Home Assistant"})
        resp = self.client.get("/api/integration-tokens")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Home Assistant")
        self.assertNotIn("token", rows[0])
        self.assertNotIn("token_hash", rows[0])

    def test_list_only_shows_the_caller_owns_tokens(self):
        self.client.post("/api/integration-tokens", json={"name": "Mine"})
        _login(self.client, self.admin2)
        self.client.post("/api/integration-tokens", json={"name": "Admin2's"})

        _login(self.client, self.admin)
        rows = self.client.get("/api/integration-tokens").get_json()

        self.assertEqual([r["name"] for r in rows], ["Mine"])

    def test_delete_revokes_it(self):
        token_id = self.client.post(
            "/api/integration-tokens", json={"name": "Home Assistant"}).get_json()["id"]

        resp = self.client.delete(f"/api/integration-tokens/{token_id}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/integration-tokens").get_json(), [])

    def test_delete_of_a_nonexistent_token_is_404(self):
        resp = self.client.delete("/api/integration-tokens/999999")
        self.assertEqual(resp.status_code, 404)

    def test_delete_cannot_reach_another_admins_token(self):
        token_id = self.client.post(
            "/api/integration-tokens", json={"name": "Mine"}).get_json()["id"]
        _login(self.client, self.admin2)

        resp = self.client.delete(f"/api/integration-tokens/{token_id}")

        self.assertEqual(resp.status_code, 404)
        _login(self.client, self.admin)
        self.assertEqual(len(self.client.get("/api/integration-tokens").get_json()), 1)

    def test_cross_origin_create_is_blocked(self):
        resp = self.client.post("/api/integration-tokens", json={"name": "x"},
                                 headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)

    def test_non_admin_cannot_create(self):
        # #474's revision: minting is what's gated now, not the credential
        # type -- a logged-in non-admin must not be able to reach this at
        # all, the same 403 as any other admin-only route in this file.
        _login(self.client, self.owner)
        resp = self.client.post("/api/integration-tokens", json={"name": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_non_admin_cannot_list(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/integration-tokens")
        self.assertEqual(resp.status_code, 403)

    def test_non_admin_cannot_delete(self):
        token_id = self.client.post(
            "/api/integration-tokens", json={"name": "Mine"}).get_json()["id"]
        _login(self.client, self.owner)

        resp = self.client.delete(f"/api/integration-tokens/{token_id}")

        self.assertEqual(resp.status_code, 403)


class ApiTokensToIntegrationTokensMigrationTests(unittest.TestCase):
    """#474 revision, caught in review: a bare `ALTER TABLE ... RENAME` is
    not enough. v2.8.0/2.8.1 shipped api_tokens with NO admin gate on
    minting -- any logged-in user could create one, under an explicit
    read-only promise. integration_tokens' whole safety property is "an
    admin minted it," so a row that predates the gate must not survive
    the rename with action capability it was never granted. Builds the
    exact pre-migration v2.8.x shape by hand -- users + a bare api_tokens
    table, no ORM, no db.init_db() yet -- so db.init_db() (called in
    setUp, after the fixture) exercises the real upgrade path a restart
    would take, not behaviour asserted on a database that was always
    integration_tokens."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)

        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username TEXT UNIQUE NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
            "password_hash TEXT)"
        )
        # The exact v2.8.0/2.8.1 shape (db.py's api_tokens, pre-#474-revision).
        conn.execute(
            "CREATE TABLE api_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "name TEXT NOT NULL, token_hash TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), last_used_at TEXT)"
        )
        self._admin_id = conn.execute(
            "INSERT INTO users (username, is_admin) VALUES ('admin', 1)").lastrowid
        self._member_id = conn.execute(
            "INSERT INTO users (username, is_admin) VALUES ('member', 0)").lastrowid
        self._admin_hash = sync_state.hash_token("admin-raw-token")
        self._member_hash = sync_state.hash_token("member-raw-token")
        conn.execute(
            "INSERT INTO api_tokens (owner_user_id, name, token_hash) VALUES (?, 'Admin HA', ?)",
            (self._admin_id, self._admin_hash))
        conn.execute(
            "INSERT INTO api_tokens (owner_user_id, name, token_hash) VALUES (?, 'Members Grafana', ?)",
            (self._member_id, self._member_hash))
        conn.commit()
        conn.close()

        # The real upgrade path: same call a server restart makes.
        db.DB_PATH = self._db_path
        db.init_db()
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()
        self.conn = db.get_conn()

    def tearDown(self):
        self.conn.close()
        self._db_path.unlink(missing_ok=True)

    def test_the_non_admin_owned_row_is_revoked_not_carried_over(self):
        rows = self.conn.execute(
            "SELECT * FROM integration_tokens WHERE owner_user_id = ?", (self._member_id,)
        ).fetchall()
        self.assertEqual(rows, [])

    def test_the_admin_owned_row_survives_with_its_hash_and_name_intact(self):
        row = self.conn.execute(
            "SELECT * FROM integration_tokens WHERE owner_user_id = ?", (self._admin_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["token_hash"], self._admin_hash)
        self.assertEqual(row["name"], "Admin HA")

    def test_the_revoked_non_admin_token_401s_at_the_actions_route(self):
        resp = self.client.post(
            "/api/integrations/actions/scan",
            headers={"Authorization": "Bearer member-raw-token"})
        self.assertEqual(resp.status_code, 401)

    def test_the_revoked_non_admin_token_401s_at_devices_and_server_too(self):
        # The promise this token was minted under was read-only -- so even
        # the routes it USED to be able to reach must also reject it now,
        # not just the new action route.
        for path in ("/api/integrations/devices", "/api/integrations/server"):
            resp = self.client.get(
                path, headers={"Authorization": "Bearer member-raw-token"})
            self.assertEqual(resp.status_code, 401, path)

    def test_the_surviving_admin_token_still_authenticates_the_actions_route(self):
        with mock.patch.object(scanner, "start_scan", return_value={"status": "started"}):
            resp = self.client.post(
                "/api/integrations/actions/scan",
                headers={"Authorization": "Bearer admin-raw-token"})
        self.assertEqual(resp.status_code, 202)

    def test_a_second_migration_run_is_a_no_op(self):
        # init_db() runs on every server start, not just the first one
        # after upgrading -- the migration must not try to rename an
        # already-renamed table, and must not re-run (and re-revoke
        # anything) on the second and subsequent boots.
        db.init_db()
        row = self.conn.execute(
            "SELECT * FROM integration_tokens WHERE owner_user_id = ?", (self._admin_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["token_hash"], self._admin_hash)


class IntegrationDevicesRouteTests(_RouteTestBase):
    """#446: GET /api/integrations/devices — the read-only, API-token-
    authenticated counterpart to GET /api/devices."""

    # #455: shown as the assertion message on test_device_shape_is_locked's
    # failures below — the only place this instruction reliably reaches
    # whoever's change tripped it.
    _CONTRACT_MESSAGE = (
        "/api/integrations/devices is a published contract (#446) consumed "
        "by trobar-ha and by anything built from "
        "docs/reference/integration-api.md. If this change is intentional: "
        "update that page, refresh the payload reference in trobar-ha#2, "
        "and open a trobar-ha issue before merging."
    )

    def setUp(self):
        super().setUp()
        # Module-level global shared by every test in the process — one
        # test here deliberately exhausts the "integration_token:"+ip
        # bucket, which would otherwise leak into (429-block) every other
        # test in this class, AND into IntegrationServerRouteTests /
        # IntegrationMirrorsRouteTests / IntegrationActionsScanRouteTests,
        # since #474's revision has all four routes share this one bucket
        # now (one credential, one authenticator). Same isolation as
        # RateLimitTrustedProxyTests above.
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)

    def _token_for(self, user_id):
        token_id, raw = sync_state.create_integration_token(self.conn, user_id, "Test token")
        return raw

    def test_a_valid_token_returns_the_owners_devices(self):
        sync_state.create_device(self.conn, self.admin, "Phone", "phone")
        token = self._token_for(self.admin)

        resp = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(resp.status_code, 200)
        names = [d["name"] for d in resp.get_json()]
        self.assertEqual(names, ["Phone"])

    # test_it_never_returns_another_users_devices removed by #479: it
    # tested a non-admin-owned token's scoping, a scenario the fix below
    # makes unreachable -- such a token now 401s before ever reaching
    # _device_rows_for_user (see IntegrationTokenDemotionTests), a
    # stronger guarantee than "authenticates but sees nothing". The
    # "does an admin token see every device" side is already covered by
    # test_an_admins_token_sees_every_device_same_as_an_admin_session
    # below.

    def test_is_own_and_is_pinned_are_real_json_booleans_not_0_1(self):
        # #449: assertTrue/assertFalse would pass on the SQLite ints too
        # (1 is truthy, 0 is falsy) and wouldn't catch a regression back to
        # them — assertIs against the literal True/False is what actually
        # pins the JSON *type*, not just its truthiness.
        sync_state.create_device(self.conn, self.admin, "Phone", "phone")
        token = self._token_for(self.admin)

        row = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {token}"}).get_json()[0]

        self.assertIs(row["is_own"], True)
        self.assertIs(row["is_pinned"], False)

    def test_an_admins_token_sees_every_device_same_as_an_admin_session(self):
        sync_state.create_device(self.conn, self.owner, "Owner's phone", "phone")
        sync_state.create_device(self.conn, self.other, "Bob's phone", "phone")
        token = self._token_for(self.admin)

        resp = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {token}"})

        names = {d["name"] for d in resp.get_json()}
        self.assertEqual(names, {"Owner's phone", "Bob's phone"})

    def test_response_includes_sync_status_and_autofit(self):
        sync_state.create_device(self.conn, self.admin, "Phone", "phone")
        token = self._token_for(self.admin)

        row = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {token}"}).get_json()[0]

        self.assertIn("sync_status", row)
        self.assertIn("autofit", row)

    def test_device_shape_is_locked(self):
        # #455: none of the tests above lock the *shape* -- they check
        # behaviours (who sees what, that booleans are booleans) without
        # asserting the exact key set, so an added, renamed, or removed
        # field passes every one of them silently. #449 (is_own/is_pinned
        # as 0/1) was caught by a maintainer pasting a real response into a
        # review, not by any test -- this is what would have caught it
        # mechanically instead, and at the moment of the change rather than
        # whenever someone next happens to look.
        sync_state.create_device(self.conn, self.admin, "Phone", "phone")
        token = self._token_for(self.admin)

        row = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {token}"}).get_json()[0]

        self.assertEqual(
            set(row.keys()),
            {
                "id", "name", "device_type", "owner_user_id", "owner_username",
                "is_own", "is_pinned", "max_size_bytes", "reported_free_bytes",
                "reported_total_bytes", "free_bytes_reported_at", "created_at",
                "last_seen_at", "source_of_truth", "transcode_format",
                "artist_images", "unknown_track_count", "autofit", "sync_status",
            },
            self._CONTRACT_MESSAGE,
        )
        self.assertEqual(
            set(row["sync_status"].keys()),
            {"pending_count", "last_synced_at"},
            self._CONTRACT_MESSAGE,
        )
        # Autofit disabled (the default here) is the short shape. The
        # enabled shape (period/albums/tracks/bytes added) is pinned at the
        # function level by AutofitStatusTests
        # .test_reports_period_albums_tracks_and_bytes_after_a_refresh in
        # test_sync_state.py -- not through this route, but autofit_status()
        # is what this route calls verbatim (main.py), so that coverage
        # carries over; no need to duplicate the fixture setup here for the
        # enabled case too.
        self.assertEqual(
            set(row["autofit"].keys()),
            {"enabled", "percent"},
            self._CONTRACT_MESSAGE,
        )

    def test_no_bearer_header_is_401(self):
        resp = self.client.get("/api/integrations/devices")
        self.assertEqual(resp.status_code, 401)

    def test_an_invalid_token_is_401_not_500(self):
        resp = self.client.get(
            "/api/integrations/devices", headers={"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(resp.status_code, 401)

    def test_a_revoked_token_stops_authenticating(self):
        token_id, raw = sync_state.create_integration_token(self.conn, self.admin, "Test token")
        sync_state.revoke_integration_token(self.conn, self.admin, token_id)

        resp = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {raw}"})

        self.assertEqual(resp.status_code, 401)

    def test_a_device_bearer_token_cannot_authenticate_here(self):
        # #446's whole point: this is a DIFFERENT credential from a device
        # token, not a wider grant an existing token happens to also satisfy.
        _, device_token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")

        resp = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {device_token}"})

        self.assertEqual(resp.status_code, 401)

    def test_reachable_with_no_session_at_all_in_local_auth_mode(self):
        # #446: the login-required before_request gate must exempt this
        # route the same way it already exempts /api/device/* — otherwise
        # a Bearer-only caller 401s before ever reaching the token check.
        # This test never calls _login(); AUTH_MODE defaults to 'local' in
        # this test harness (see _RouteTestBase), so without the exemption
        # this would 401 with "Login required" regardless of the header.
        sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        token = self._token_for(self.admin)

        resp = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(resp.status_code, 200)

    def test_repeated_bad_tokens_are_rate_limited_separate_from_login(self):
        # Shared "integration_token:"+ip bucket (see setUp's comment).
        for _ in range(30):
            self.client.get("/api/integrations/devices",
                             headers={"Authorization": "Bearer wrong"})
        resp = self.client.get("/api/integrations/devices",
                                headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status_code, 429)

        # The login endpoint's own limiter must be unaffected — proves the
        # two failure counters use distinct buckets, not a shared one.
        login_resp = self.client.post(
            "/login", data={"username": "someone", "password": "wrong"})
        self.assertNotEqual(login_resp.status_code, 429)


class IntegrationServerRouteTests(_RouteTestBase):
    """#475: GET /api/integrations/server — the read-only, API-token-
    authenticated server-metrics sibling of /api/integrations/devices, for
    trobar-ha#25's "server" device. Instance-wide, not scoped per caller —
    every logged-in user's own dashboard already shows the same track
    count and total size, so a token seeing them is not a new disclosure."""

    _CONTRACT_MESSAGE = (
        "/api/integrations/server is a published contract (#475) consumed "
        "by trobar-ha and by anything built from "
        "docs/reference/integration-api.md. If this change is intentional: "
        "update that page, refresh the payload reference in trobar-ha#2, "
        "and open a trobar-ha issue before merging."
    )

    def setUp(self):
        super().setUp()
        # Same isolation as IntegrationDevicesRouteTests above -- the
        # "integration_token:"+ip rate-limit bucket is module-level/shared,
        # and shared across all four /api/integrations/* routes.
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)

    def _token_for(self, user_id):
        token_id, raw = sync_state.create_integration_token(self.conn, user_id, "Test token")
        return raw

    def _make_track(self, relative_path, size=1000, deleted=False):
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, deleted_at) "
            "VALUES (?, 'A', 'B', 'T', ?, 0, ?)",
            (relative_path, size, "2026-01-01" if deleted else None),
        )
        self.conn.commit()

    def test_a_valid_token_gets_200(self):
        token = self._token_for(self.admin)
        resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)

    def test_no_bearer_header_is_401(self):
        resp = self.client.get("/api/integrations/server")
        self.assertEqual(resp.status_code, 401)

    def test_an_invalid_token_is_401_not_500(self):
        resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(resp.status_code, 401)

    def test_a_revoked_token_stops_authenticating(self):
        token_id, raw = sync_state.create_integration_token(self.conn, self.admin, "Test token")
        sync_state.revoke_integration_token(self.conn, self.admin, token_id)
        resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {raw}"})
        self.assertEqual(resp.status_code, 401)

    def test_a_device_bearer_token_cannot_authenticate_here(self):
        # Same distinct-credential guarantee as #446's own route -- a
        # device token must not double as an integration token.
        _, device_token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {device_token}"})
        self.assertEqual(resp.status_code, 401)

    def test_reachable_with_no_session_at_all_in_local_auth_mode(self):
        # The #475 prefix trap this issue explicitly warned about: a route
        # added under /api/integrations/ is login-exempt by default (the
        # before_request gate exempts the whole prefix), so the ONLY thing
        # standing between this route and being fully unauthenticated is
        # _authenticated_integration_token() actually being called. This
        # test never logs in at all -- if that call were missing or
        # short-circuited, this would still return 200 rather than 401.
        token = self._token_for(self.admin)
        resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)

    def test_track_count_and_total_bytes_reflect_the_whole_library(self):
        # Instance-wide, not per-owner -- unlike /devices, there's no
        # per-user scoping question here (see the class docstring).
        self._make_track("a.flac", size=1000)
        self._make_track("b.flac", size=2000)
        self._make_track("deleted.flac", size=9999, deleted=True)
        token = self._token_for(self.admin)

        body = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"}).get_json()

        self.assertEqual(body["track_count"], 2)
        self.assertEqual(body["total_bytes"], 3000)

    def test_an_empty_library_reports_zero_not_null(self):
        # SUM() over zero rows is SQL NULL, not 0 -- COALESCE is what turns
        # a brand-new install's library into a real number a client can do
        # arithmetic on without a null-check first.
        token = self._token_for(self.admin)
        body = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"}).get_json()
        self.assertEqual(body["track_count"], 0)
        self.assertEqual(body["total_bytes"], 0)

    def test_scan_running_and_last_scan_at_reflect_real_scan_status(self):
        with mock.patch.object(scanner, "scan_status", return_value={
            "running": True, "last_result": None, "progress": None, "last_scan_at": None,
        }):
            token = self._token_for(self.admin)
            body = self.client.get(
                "/api/integrations/server", headers={"Authorization": f"Bearer {token}"}).get_json()
        self.assertTrue(body["scan_running"])
        self.assertIsNone(body["last_scan_at"])

    def test_no_online_field(self):
        # #475's own explicit decision: a response arriving IS the signal;
        # an always-true field would be noise, not information.
        token = self._token_for(self.admin)
        body = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"}).get_json()
        self.assertNotIn("online", body)

    def test_shape_is_locked(self):
        # Same discipline as #455's lock on /api/integrations/devices --
        # an added/renamed/removed field must fail loudly here rather than
        # silently drift from what trobar-ha#2's payload reference and
        # docs/reference/integration-api.md say this endpoint returns.
        token = self._token_for(self.admin)
        body = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"}).get_json()
        self.assertEqual(
            set(body.keys()),
            {"version", "track_count", "total_bytes", "scan_running", "last_scan_at"},
            self._CONTRACT_MESSAGE,
        )


class IntegrationMirrorsRouteTests(_RouteTestBase):
    """#498: GET /api/integrations/mirrors — the monitoring surface #189's
    three added sinks (Subsonic, Jellyfin, Emby) never got alongside the
    pre-existing filesystem one. Same integration-token auth as /devices
    and /server; this class leans on those for the general auth-mechanics
    coverage and focuses on the mirror-specific aggregation."""

    _CONTRACT_MESSAGE = (
        "/api/integrations/mirrors is a published contract (#498) consumed "
        "by trobar-ha and by anything built from "
        "docs/reference/integration-api.md. If this change is intentional: "
        "update that page, refresh the payload reference in trobar-ha#2, "
        "and open a trobar-ha issue before merging."
    )

    def setUp(self):
        super().setUp()
        # Same shared "integration_token:"+ip bucket as the other three
        # /api/integrations/* routes.
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)

    def _token_for(self, user_id):
        token_id, raw = sync_state.create_integration_token(self.conn, user_id, "Test token")
        return raw

    def _get(self, token):
        return self.client.get(
            "/api/integrations/mirrors", headers={"Authorization": f"Bearer {token}"})

    def test_a_valid_token_gets_200(self):
        token = self._token_for(self.admin)
        self.assertEqual(self._get(token).status_code, 200)

    def test_no_bearer_header_is_401(self):
        resp = self.client.get("/api/integrations/mirrors")
        self.assertEqual(resp.status_code, 401)

    def test_an_invalid_token_is_401_not_500(self):
        resp = self.client.get(
            "/api/integrations/mirrors", headers={"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(resp.status_code, 401)

    def test_a_revoked_token_stops_authenticating(self):
        token_id, raw = sync_state.create_integration_token(self.conn, self.admin, "Test token")
        sync_state.revoke_integration_token(self.conn, self.admin, token_id)
        self.assertEqual(self._get(raw).status_code, 401)

    def test_a_device_bearer_token_cannot_authenticate_here(self):
        _, device_token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        self.assertEqual(self._get(device_token).status_code, 401)

    def test_reachable_with_no_session_at_all_in_local_auth_mode(self):
        # Same #446 prefix-trap guard as the other three /api/integrations/*
        # routes -- this route is login-exempt by default, so the only
        # thing standing between it and being fully unauthenticated is
        # _authenticated_integration_token() actually being called. This
        # test never logs in at all.
        token = self._token_for(self.admin)
        self.assertEqual(self._get(token).status_code, 200)

    def test_shape_is_locked(self):
        # Same discipline as #455's lock on /api/integrations/devices --
        # an added/renamed/removed field must fail loudly here rather than
        # silently drift from what trobar-ha#2's payload reference and
        # docs/reference/integration-api.md say this endpoint returns.
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(
            set(body.keys()),
            {"mirrors_failing", "by_sink", "failing", "failing_truncated"},
            self._CONTRACT_MESSAGE,
        )

    def test_an_empty_install_reports_all_zero(self):
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["mirrors_failing"], 0)
        self.assertEqual(body["failing"], [])
        self.assertFalse(body["failing_truncated"])
        self.assertEqual(
            body["by_sink"],
            {sink: {"enabled": 0, "failing": 0}
             for sink in ("filesystem", "subsonic", "jellyfin", "emby")},
        )

    def test_an_enabled_healthy_mirror_counts_as_enabled_not_failing(self):
        p = self._make_playlist("OK", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET subsonic_mirror_enabled = 1 WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["by_sink"]["subsonic"], {"enabled": 1, "failing": 0})
        self.assertEqual(body["mirrors_failing"], 0)
        self.assertEqual(body["failing"], [])

    def test_a_failing_mirror_is_counted_and_listed(self):
        p = self._make_playlist("Road Trip", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET subsonic_mirror_enabled = 1, "
            "subsonic_mirror_last_error_code = 'unreachable', "
            "subsonic_mirror_last_written_at = '2026-07-30 09:14:02' WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["mirrors_failing"], 1)
        self.assertEqual(body["by_sink"]["subsonic"], {"enabled": 1, "failing": 1})
        self.assertEqual(body["failing"], [{
            "playlist_id": p,
            "title": "Road Trip",
            "sink": "subsonic",
            "error_code": "unreachable",
            "last_written_at": "2026-07-30 09:14:02",
        }])

    def test_unset_target_counts_as_failing(self):
        # #498's own settled decision: clearing a mirror target while
        # playlists are still enabled against it is exactly the silent-
        # drift case this endpoint exists to surface, so it counts -- no
        # per-code allowlist that would need unset_target added to it.
        p = self._make_playlist("Drifted", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET jellyfin_mirror_enabled = 1, "
            "jellyfin_mirror_last_error_code = 'unset_target' WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["mirrors_failing"], 1)
        self.assertEqual(body["by_sink"]["jellyfin"]["failing"], 1)

    def test_a_playlist_mirrored_to_two_sinks_counts_once_in_each(self):
        # by_sink counts playlist x sink PAIRS, not playlists -- the
        # property that makes by_sink.*.enabled/failing sum to
        # mirrors_failing / the total enabled count.
        p = self._make_playlist("Dual", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET subsonic_mirror_enabled = 1, "
            "subsonic_mirror_last_error_code = 'unreachable', "
            "jellyfin_mirror_enabled = 1 WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["by_sink"]["subsonic"], {"enabled": 1, "failing": 1})
        self.assertEqual(body["by_sink"]["jellyfin"], {"enabled": 1, "failing": 0})
        self.assertEqual(body["mirrors_failing"], 1)
        self.assertEqual(len(body["failing"]), 1)
        self.assertEqual(body["failing"][0]["sink"], "subsonic")

    def test_a_healthy_sink_does_not_inflate_another_sinks_failure_count(self):
        # Cross-sink isolation -- the mirror-monitoring equivalent of
        # #501's cross-device isolation property.
        p = self._make_playlist("Cross", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET emby_mirror_enabled = 1, "
            "emby_mirror_last_error_code = 'write_failed', "
            "mirror_enabled = 1 WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["by_sink"]["filesystem"], {"enabled": 1, "failing": 0})
        self.assertEqual(body["by_sink"]["emby"], {"enabled": 1, "failing": 1})
        self.assertEqual(body["mirrors_failing"], 1)

    def test_a_disabled_sink_with_a_leftover_error_code_is_not_counted(self):
        # Disabling a sink doesn't clear its last_error_code column (same
        # as /api/admin/mirrors' own display) -- only *_mirror_enabled
        # gates whether it counts here.
        p = self._make_playlist("Turned Off", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET subsonic_mirror_enabled = 0, "
            "subsonic_mirror_last_error_code = 'unreachable' WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["by_sink"]["subsonic"], {"enabled": 0, "failing": 0})
        self.assertEqual(body["mirrors_failing"], 0)
        self.assertEqual(body["failing"], [])

    def test_the_failing_worklist_is_capped_but_the_counts_stay_exact(self):
        for i in range(main._MIRRORS_FAILING_LIMIT + 5):
            p = self._make_playlist(f"Failing {i:03d}", owner_user_id=None, shared=1)
            self.conn.execute(
                "UPDATE playlists SET subsonic_mirror_enabled = 1, "
                "subsonic_mirror_last_error_code = 'unreachable' WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["mirrors_failing"], main._MIRRORS_FAILING_LIMIT + 5)
        self.assertEqual(body["by_sink"]["subsonic"]["failing"], main._MIRRORS_FAILING_LIMIT + 5)
        self.assertEqual(len(body["failing"]), main._MIRRORS_FAILING_LIMIT)
        self.assertTrue(body["failing_truncated"])

    def test_error_code_is_the_raw_code_not_a_rendered_message(self):
        p = self._make_playlist("Raw Code", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET mirror_enabled = 1, "
            "mirror_last_error_code = 'not_writable', "
            "mirror_last_error = 'Permission denied: /mnt/mirror' WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        entry = body["failing"][0]
        self.assertEqual(entry["error_code"], "not_writable")
        self.assertNotIn("error", entry)
        self.assertNotIn("mirror_last_error", entry)


class IntegrationActionsScanRouteTests(_RouteTestBase):
    """#474: POST /api/integrations/actions/scan — the integration-token-
    authenticated counterpart to POST /api/library/scan, so an integration
    can trigger a rescan and not just observe one. Same 202/409 response
    shape as /api/library/scan. As of #474's revision this shares its
    credential and authenticator with the read-only devices/server
    routes -- see db.py's integration_tokens comment for why (the trust
    boundary moved to who may mint one, not which table it lives in)."""

    def setUp(self):
        super().setUp()
        # Same isolation as IntegrationServerRouteTests -- the
        # "integration_token:"+ip rate-limit bucket is module-level/shared,
        # and shared across all four /api/integrations/* routes.
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)

    def _token_for(self, user_id):
        token_id, raw = sync_state.create_integration_token(self.conn, user_id, "Test token")
        return raw

    def test_a_valid_token_backgrounds_the_scan_and_returns_202(self):
        token = self._token_for(self.admin)
        with mock.patch.object(scanner, "start_scan",
                               return_value={"status": "started"}) as start:
            resp = self.client.post(
                "/api/integrations/actions/scan",
                headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 202)
        start.assert_called_once()

    def test_already_running_is_409(self):
        token = self._token_for(self.admin)
        with mock.patch.object(scanner, "start_scan",
                               return_value={"already_running": True}):
            resp = self.client.post(
                "/api/integrations/actions/scan",
                headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 409)

    def test_no_bearer_header_is_401(self):
        resp = self.client.post("/api/integrations/actions/scan")
        self.assertEqual(resp.status_code, 401)

    def test_an_invalid_token_is_401_not_500(self):
        resp = self.client.post(
            "/api/integrations/actions/scan",
            headers={"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(resp.status_code, 401)

    def test_a_revoked_token_stops_authenticating(self):
        token_id, raw = sync_state.create_integration_token(self.conn, self.admin, "Test token")
        sync_state.revoke_integration_token(self.conn, self.admin, token_id)
        resp = self.client.post(
            "/api/integrations/actions/scan",
            headers={"Authorization": f"Bearer {raw}"})
        self.assertEqual(resp.status_code, 401)

    def test_a_device_bearer_token_cannot_authenticate_here(self):
        _, device_token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        resp = self.client.post(
            "/api/integrations/actions/scan",
            headers={"Authorization": f"Bearer {device_token}"})
        self.assertEqual(resp.status_code, 401)

    def test_reachable_with_no_session_at_all_in_local_auth_mode(self):
        # The same /api/integrations/ prefix trap #475 pinned a test for:
        # the whole prefix is login-exempt by the before_request gate, so
        # the ONLY thing standing between this route and being fully
        # unauthenticated is _authenticated_integration_token() actually
        # being called. This test never logs in at all -- if that call were
        # missing or short-circuited, this would still 202 a real scan
        # rather than 401.
        token = self._token_for(self.admin)
        with mock.patch.object(scanner, "start_scan",
                               return_value={"status": "started"}) as start:
            resp = self.client.post(
                "/api/integrations/actions/scan",
                headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 202)
        start.assert_called_once()

    def test_force_flag_is_passed_through(self):
        token = self._token_for(self.admin)
        with mock.patch.object(scanner, "start_scan",
                               return_value={"status": "started"}) as start:
            self.client.post(
                "/api/integrations/actions/scan", json={"force": True},
                headers={"Authorization": f"Bearer {token}"})
        start.assert_called_once()
        self.assertTrue(start.call_args.kwargs.get("force"))

    def test_omitted_force_defaults_to_false(self):
        token = self._token_for(self.admin)
        with mock.patch.object(scanner, "start_scan",
                               return_value={"status": "started"}) as start:
            self.client.post(
                "/api/integrations/actions/scan",
                headers={"Authorization": f"Bearer {token}"})
        self.assertFalse(start.call_args.kwargs.get("force"))

    def test_the_same_token_also_authenticates_devices_server_and_mirrors(self):
        # #474's revision, demonstrated rather than just asserted: one
        # mint, one secret, and it works against all four
        # /api/integrations/* routes -- there is no longer a second,
        # differently-scoped credential to separately mint or paste.
        token = self._token_for(self.admin)

        with mock.patch.object(scanner, "start_scan", return_value={"status": "started"}):
            scan_resp = self.client.post(
                "/api/integrations/actions/scan",
                headers={"Authorization": f"Bearer {token}"})
        devices_resp = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {token}"})
        server_resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"})
        mirrors_resp = self.client.get(
            "/api/integrations/mirrors", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(scan_resp.status_code, 202)
        self.assertEqual(devices_resp.status_code, 200)
        self.assertEqual(server_resp.status_code, 200)
        self.assertEqual(mirrors_resp.status_code, 200)


class IntegrationTokenDemotionTests(_RouteTestBase):
    """#479: require_admin() at api_integration_tokens is a mint-time
    gate -- checked once, never again. _authenticated_integration_token
    now re-verifies admin status on every use too, so a token can't
    outlive the trust it was minted under. There's no admin-demotion
    route in the app today (confirmed while fixing this: _provision_user
    only ever sets is_admin FROM 0 TO 1), so these simulate it directly
    at the DB layer -- the same way a future demotion feature, or manual
    intervention, would leave a user's admin flag."""

    def setUp(self):
        super().setUp()
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)

    def _token_for(self, user_id):
        token_id, raw = sync_state.create_integration_token(self.conn, user_id, "Test token")
        return raw

    def _demote(self, user_id):
        self.conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_id,))
        self.conn.commit()

    def test_a_demoted_owners_token_401s_on_devices(self):
        token = self._token_for(self.admin)
        self._demote(self.admin)

        resp = self.client.get(
            "/api/integrations/devices", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(resp.status_code, 401)

    def test_a_demoted_owners_token_401s_on_server(self):
        token = self._token_for(self.admin)
        self._demote(self.admin)

        resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(resp.status_code, 401)

    def test_a_demoted_owners_token_401s_on_mirrors(self):
        token = self._token_for(self.admin)
        self._demote(self.admin)

        resp = self.client.get(
            "/api/integrations/mirrors", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(resp.status_code, 401)

    def test_a_demoted_owners_token_401s_on_actions_scan(self):
        token = self._token_for(self.admin)
        self._demote(self.admin)

        resp = self.client.post(
            "/api/integrations/actions/scan", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(resp.status_code, 401)

    def test_a_token_whose_owner_stays_admin_keeps_working(self):
        # Guards against over-correcting into revoking everything --
        # this is the "someone else's is_admin changed" case, not "any
        # token stops working."
        token = self._token_for(self.admin)
        other_admin = self._make_user("other-admin", is_admin=True)
        self._demote(other_admin)

        resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(resp.status_code, 200)

    def test_deleting_the_owner_still_revokes_via_cascade(self):
        # Regression cover for the path that already worked
        # (integration_tokens.owner_user_id REFERENCES users(id) ON
        # DELETE CASCADE) -- #479 flagged this as the reason the gap was
        # easy to miss: the obvious revocation case (delete the user)
        # was never broken, only demotion (change the flag, keep the row).
        token = self._token_for(self.admin)
        self.conn.execute("DELETE FROM users WHERE id = ?", (self.admin,))
        self.conn.commit()

        resp = self.client.get(
            "/api/integrations/server", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(resp.status_code, 401)


class AutofitApiTests(_RouteTestBase):
    """#217: the fill-percentage cap — persisted via POST .../autofit; the
    percent-independent preview basis read via GET .../autofit/preview."""

    def setUp(self):
        super().setUp()
        _login(self.client, self.owner)
        self.device, _ = sync_state.create_device(
            self.conn, self.owner, "phone", max_size_bytes=10_000)

    def test_posting_a_percent_persists_it(self):
        resp = self.client.post(f"/api/devices/{self.device}/autofit", json={"percent": 60})
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute(
            "SELECT autofit_percent FROM devices WHERE id = ?", (self.device,)).fetchone()
        self.assertEqual(row["autofit_percent"], 60)

    def test_omitting_percent_leaves_it_unchanged(self):
        self.client.post(f"/api/devices/{self.device}/autofit", json={"percent": 60})
        self.client.post(f"/api/devices/{self.device}/autofit", json={})
        row = self.conn.execute(
            "SELECT autofit_percent FROM devices WHERE id = ?", (self.device,)).fetchone()
        self.assertEqual(row["autofit_percent"], 60)

    def test_zero_percent_is_400(self):
        # 0 would mean "fill nothing", a confusing back door to disabling.
        resp = self.client.post(f"/api/devices/{self.device}/autofit", json={"percent": 0})
        self.assertEqual(resp.status_code, 400)

    def test_percent_over_100_is_400(self):
        resp = self.client.post(f"/api/devices/{self.device}/autofit", json={"percent": 101})
        self.assertEqual(resp.status_code, 400)

    def test_non_integer_percent_is_400(self):
        resp = self.client.post(f"/api/devices/{self.device}/autofit", json={"percent": "lots"})
        self.assertEqual(resp.status_code, 400)

    def test_boolean_percent_is_400(self):
        resp = self.client.post(f"/api/devices/{self.device}/autofit", json={"percent": True})
        self.assertEqual(resp.status_code, 400)

    def test_preview_returns_the_basis(self):
        resp = self.client.get(f"/api/devices/{self.device}/autofit/preview")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["max_size_bytes"], 10_000)

    def test_preview_nonexistent_device_is_404(self):
        resp = self.client.get("/api/devices/999999/autofit/preview")
        self.assertEqual(resp.status_code, 404)

    def test_preview_denied_for_a_non_owner_non_admin(self):
        _login(self.client, self.other)
        resp = self.client.get(f"/api/devices/{self.device}/autofit/preview")
        self.assertEqual(resp.status_code, 403)


class DeviceFileAuthorizationTests(_RouteTestBase):
    """#110: GET /api/device/file/<id> authorizes by device_track_state
    membership — a valid device token can only download the tracks the server
    actually offered THAT device, not any track id in the library."""

    def setUp(self):
        super().setUp()
        self._music = Path(tempfile.mkdtemp(dir=_TMP))
        db.set_config(self.conn, "music_root", str(self._music))
        self.conn.commit()
        self.device_id, self.token = sync_state.create_device(self.conn, self.owner, "phone")

    def _make_track_file(self, rel: str) -> int:
        p = self._music / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"FLACDATA")
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES (?, 'A', 'Al', 'T', 8, 0)", (rel,))
        self.conn.commit()
        return sync_state._new_id(cur)

    def _offer(self, track_id: int, status: str, device_id=None):
        self.conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))", (device_id or self.device_id, track_id, status))
        self.conn.commit()

    def _get(self, track_id: int):
        resp = self.client.get(f"/api/device/file/{track_id}",
                               headers={"Authorization": f"Bearer {self.token}"})
        resp.close()  # release send_file's file handle (avoids a ResourceWarning)
        return resp

    def test_offered_pending_track_is_served(self):
        tid = self._make_track_file("a/x.flac")
        self._offer(tid, "pending")
        self.assertEqual(self._get(tid).status_code, 200)

    def test_offered_downloaded_track_is_served(self):
        # re-download / integrity retry of an already-downloaded track
        tid = self._make_track_file("a/y.flac")
        self._offer(tid, "downloaded")
        self.assertEqual(self._get(tid).status_code, 200)

    def test_track_never_offered_is_404(self):
        tid = self._make_track_file("a/z.flac")  # real file + real track, just not offered
        self.assertEqual(self._get(tid).status_code, 404)

    def test_removed_track_is_404(self):
        tid = self._make_track_file("a/w.flac")
        self._offer(tid, "removed")
        self.assertEqual(self._get(tid).status_code, 404)

    def test_excluded_track_is_404(self):
        tid = self._make_track_file("a/e.flac")
        self._offer(tid, "excluded")
        self.assertEqual(self._get(tid).status_code, 404)

    def test_another_devices_track_is_404(self):
        # Offered to a DIFFERENT device — must not be reachable with our token.
        other_id, _tok = sync_state.create_device(self.conn, self.other, "other-phone")
        tid = self._make_track_file("a/v.flac")
        self._offer(tid, "pending", device_id=other_id)
        self.assertEqual(self._get(tid).status_code, 404)


class HttpHardeningTests(unittest.TestCase):
    """#92: security response headers, suppressed Server banner, and a request
    body cap. Uses a minimal DB (GET /login counts users) but no seeded rows."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def test_security_headers_present(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")

    def test_server_banner_suppressed(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.headers.get("Server"), "trobar")
        self.assertNotIn("Werkzeug", resp.headers.get("Server", ""))

    def test_headers_present_on_api_responses_too(self):
        # after_request runs for every response, including JSON error paths.
        resp = self.client.get("/api/admin/users")  # 401 (unauthenticated)
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")

    def test_max_content_length_configured(self):
        self.assertEqual(main.app.config["MAX_CONTENT_LENGTH"], 4 * 1024 * 1024)

    def test_oversized_body_rejected_with_413(self):
        big = b"x" * (4 * 1024 * 1024 + 1)
        resp = self.client.post("/login", data={"username": "a", "password": big})
        self.assertEqual(resp.status_code, 413)


class PlaylistSyncConcurrencyRouteTests(_RouteTestBase):
    """#129: POST /api/provider/playlists/sync returns 409 when a sync is
    already in flight, mirroring the library-scan endpoint's guard."""

    def test_returns_409_when_a_sync_is_already_running(self):
        _login(self.client, self.admin)
        # #297 step 3: simulate an in-flight sync by occupying the queue's
        # dedupe slot directly (no need to actually run it — start_sync's
        # enqueue call is what the route checks), then hit the route.
        jobs.enqueue(self.conn, playlist_sync.JOB_TYPE, dedupe_key=playlist_sync.JOB_DEDUPE)
        resp = self.client.post("/api/provider/playlists/sync")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("error", resp.get_json())


class PlaylistSyncBackgroundRouteTests(_RouteTestBase):
    """#138: the POST backgrounds the sync (202) and a status endpoint reports
    running/last_result for the UI to poll."""

    def test_post_backgrounds_the_sync_and_returns_202(self):
        _login(self.client, self.admin)
        # Mock start_sync so no real background thread runs during the test.
        with mock.patch.object(playlist_sync, "start_sync",
                               return_value={"status": "started"}) as start:
            resp = self.client.post("/api/provider/playlists/sync")
        self.assertEqual(resp.status_code, 202)
        start.assert_called_once()

    def test_status_endpoint_reports_running_and_last_result(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/provider/playlists/sync/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("running", body)
        self.assertIn("last_result", body)


class SpotifyLinkRouteTests(_RouteTestBase):
    """#10 Part B: the per-user Spotify link — profile status field, disconnect
    clearing the stored tokens, and the not-configured connect guard.

    #398: the experimental toggle is a SEPARATE gate from "are credentials
    configured" — db.init_db() (called in setUp) seeds
    experimental_spotify_enabled to '0' here, since no credentials are set
    at that point, so every test below starts from the flag OFF unless it
    explicitly turns it on."""

    def _enable(self):
        db.set_config(self.conn, "experimental_spotify_enabled", "1")
        self.conn.commit()

    def test_profile_reports_spotify_disconnected_by_default(self):
        _login(self.client, self.owner)
        body = self.client.get("/api/profile").get_json()
        self.assertIn("spotify_connected", body)
        self.assertFalse(body["spotify_connected"])

    def test_profile_reports_experimental_flag_off_by_default(self):
        _login(self.client, self.owner)
        body = self.client.get("/api/profile").get_json()
        self.assertIn("spotify_experimental_enabled", body)
        self.assertFalse(body["spotify_experimental_enabled"])

    def test_profile_reports_experimental_flag_once_enabled(self):
        self._enable()
        _login(self.client, self.owner)
        body = self.client.get("/api/profile").get_json()
        self.assertTrue(body["spotify_experimental_enabled"])

    def test_disconnect_clears_the_link(self):
        self._enable()
        self.conn.execute(
            "UPDATE users SET spotify_refresh_token=?, spotify_user_id=?, spotify_display_name=? WHERE id=?",
            ("rt", "sp-user", "Alice", self.owner))
        self.conn.commit()
        _login(self.client, self.owner)
        resp = self.client.delete("/api/profile/spotify")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["spotify_connected"])
        row = self.conn.execute(
            "SELECT spotify_refresh_token, spotify_user_id, spotify_display_name "
            "FROM users WHERE id=?", (self.owner,)).fetchone()
        self.assertEqual(tuple(row), (None, None, None))

    def test_disconnect_refuses_when_the_flag_is_off(self):
        # #398: the whole surface is gated, not just connect/callback — a
        # stale bookmark/direct call to DELETE must not work either, since
        # the Profile button it comes from is hidden in this state.
        self.conn.execute(
            "UPDATE users SET spotify_refresh_token=?, spotify_user_id=?, spotify_display_name=? WHERE id=?",
            ("rt", "sp-user", "Alice", self.owner))
        self.conn.commit()
        _login(self.client, self.owner)
        resp = self.client.delete("/api/profile/spotify")
        self.assertEqual(resp.status_code, 400)
        # And the link survives -- refusing must not have side-effected it.
        row = self.conn.execute(
            "SELECT spotify_refresh_token FROM users WHERE id=?", (self.owner,)).fetchone()
        self.assertEqual(row["spotify_refresh_token"], "rt")

    def test_connect_400s_when_flag_off_even_if_configured(self):
        # Credentials alone are not enough -- the experimental toggle is a
        # second, independent gate on top of _spotify_oauth_client.
        db.set_config(self.conn, "spotify_client_id", "cid")
        db.set_config(self.conn, "spotify_client_secret", "csec")
        self.conn.commit()
        _login(self.client, self.owner)
        resp = self.client.get("/profile/spotify/connect")
        self.assertEqual(resp.status_code, 400)

    def test_connect_400s_when_enabled_but_not_configured(self):
        # The flag alone isn't enough either -- still needs real credentials.
        self._enable()
        _login(self.client, self.owner)
        resp = self.client.get("/profile/spotify/connect")
        self.assertEqual(resp.status_code, 400)

    def test_callback_redirects_without_linking_when_flag_is_off(self):
        # Simulates the URL being hit directly (or the flag flipping off
        # mid-flow) -- must not complete a link, but also must not show an
        # error page, since this is Spotify's own redirect target.
        db.set_config(self.conn, "spotify_client_id", "cid")
        db.set_config(self.conn, "spotify_client_secret", "csec")
        self.conn.commit()
        _login(self.client, self.owner)
        resp = self.client.get("/profile/spotify/callback", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        row = self.conn.execute(
            "SELECT spotify_refresh_token FROM users WHERE id=?", (self.owner,)).fetchone()
        self.assertIsNone(row["spotify_refresh_token"])


class LibraryScanBackgroundRouteTests(_RouteTestBase):
    """#140: POST /api/library/scan backgrounds the scan (202) and a status
    endpoint reports running/last_result for the UI to poll."""

    def test_post_backgrounds_the_scan_and_returns_202(self):
        _login(self.client, self.admin)
        # Mock start_scan so no real background walk runs during the test.
        with mock.patch.object(scanner, "start_scan",
                               return_value={"status": "started"}) as start:
            resp = self.client.post("/api/library/scan")
        self.assertEqual(resp.status_code, 202)
        start.assert_called_once()

    def test_status_endpoint_reports_running_and_last_result(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/library/scan/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("running", body)
        self.assertIn("last_result", body)


class DeviceSourceOfTruthRouteTests(_RouteTestBase):
    """#63: source_of_truth is settable from both surfaces (web session PATCH
    and device Bearer-token PATCH) against the one DB field, and surfaced in the
    device list."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "owner-dev")

    def test_web_patch_sets_source_of_truth(self):
        _login(self.client, self.owner)
        resp = self.client.patch(f"/api/devices/{self.device}", json={"source_of_truth": "device"})
        self.assertEqual(resp.status_code, 200)
        sot = self.conn.execute(
            "SELECT source_of_truth FROM devices WHERE id=?", (self.device,)).fetchone()[0]
        self.assertEqual(sot, "device")

    def test_web_patch_rejects_invalid_value(self):
        _login(self.client, self.owner)
        resp = self.client.patch(f"/api/devices/{self.device}", json={"source_of_truth": "bogus"})
        self.assertEqual(resp.status_code, 400)

    def test_device_token_patch_sets_source_of_truth(self):
        resp = self.client.patch(
            "/api/device/source-of-truth", json={"source_of_truth": "device"},
            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 200)
        sot = self.conn.execute(
            "SELECT source_of_truth FROM devices WHERE id=?", (self.device,)).fetchone()[0]
        self.assertEqual(sot, "device")

    def test_device_list_includes_source_of_truth(self):
        _login(self.client, self.owner)
        rows = self.client.get("/api/devices").get_json()
        self.assertTrue(rows and all("source_of_truth" in d for d in rows))

    def test_device_info_returns_source_of_truth(self):
        # #63: the device-facing /api/device/info exposes it so the client can
        # reflect the current value (no client-local drift).
        resp = self.client.get(
            "/api/device/info", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["source_of_truth"], "server")


class DeviceManifestRouteTests(_RouteTestBase):
    """#63: POST /api/device/manifest (device Bearer token) marks the device's
    already-held tracks 'downloaded' and reports unmatched paths."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('rt/1.flac', 'Ar', 'Al', 'Ti', 1, 0)")
        self.conn.commit()

    def test_manifest_marks_downloaded_and_counts_unmatched(self):
        # The device uploads the device_path() form it downloaded (Ar/Al/Ti.flac),
        # not the catalog relative_path ('rt/1.flac') it never sees.
        resp = self.client.post(
            "/api/device/manifest", json={"paths": ["Ar/Al/Ti.flac", "ghost/x.flac"]},
            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"matched": 1, "unmatched": 1})
        # the matched track is now 'downloaded' for this device
        state = self.conn.execute(
            "SELECT dts.status FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND t.relative_path = 'rt/1.flac'", (self.device,)).fetchone()
        self.assertEqual(state["status"], "downloaded")

    def test_manifest_rejects_non_list_paths(self):
        resp = self.client.post(
            "/api/device/manifest", json={"paths": "nope"},
            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 400)


class DeviceFingerprintsRouteTests(_RouteTestBase):
    """#239: GET /api/device/fingerprints (device Bearer) serves the
    server-computed fingerprint for each track this device holds, so the
    client can keep a local provenance DB. Clients never compute
    fingerprints — they only store what this returns."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.conn.commit()

    def _auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _add_held_track(self, relative_path, fingerprint=None, status="downloaded",
                        device_id=None, track_no=None, fingerprint_seq=None):
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime, "
            "fingerprint, fingerprint_seq) VALUES (?, 'Ar', 'Al', 'Ti', ?, 1, 0, ?, ?)",
            (relative_path, track_no, fingerprint, fingerprint_seq))
        track_id = sync_state._new_id(cur)
        self.conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, ?)",
            (device_id if device_id is not None else self.device, track_id, status))
        self.conn.commit()
        return track_id

    def test_returns_fingerprint_and_device_path(self):
        track = self._add_held_track("catalog/deep/1.flac", fingerprint="AQAAFP", fingerprint_seq=1)
        resp = self.client.get("/api/device/fingerprints", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["entries"], [
            # the device_path() wire form, NOT the catalog relative_path
            {"track_id": track, "fingerprint": "AQAAFP", "fingerprint_seq": 1, "path": "Ar/Al/Ti.flac"},
        ])
        self.assertIsNone(body["next_after"])
        self.assertEqual(body["pending"], 0)

    def test_a_track_without_a_fingerprint_is_omitted_but_counted_pending(self):
        self._add_held_track("has.flac", fingerprint="FP")
        self._add_held_track("none.flac", fingerprint=None)
        body = self.client.get("/api/device/fingerprints", headers=self._auth()).get_json()
        self.assertEqual(len(body["entries"]), 1)
        # `pending` is how the client knows to come back rather than treat a
        # short page as "I have everything".
        self.assertEqual(body["pending"], 1)

    def test_transcoding_device_gets_the_source_fingerprint_with_a_transcoded_path(self):
        # The locked #239 decision: ship the SOURCE audio's fingerprint even
        # though the device holds an MP3, because recovery compares it against
        # files in the server's own source filesystem. Only the PATH reflects
        # the transcode.
        self.conn.execute("UPDATE devices SET transcode_format = 'mp3_320' WHERE id = ?",
                          (self.device,))
        self.conn.commit()
        self._add_held_track("catalog/x.flac", fingerprint="SOURCEFP")
        body = self.client.get("/api/device/fingerprints", headers=self._auth()).get_json()
        self.assertEqual(body["entries"][0]["fingerprint"], "SOURCEFP")
        self.assertEqual(body["entries"][0]["path"], "Ar/Al/Ti.mp3")

    def test_pagination_walks_with_the_cursor(self):
        ids = [self._add_held_track(f"t{i}.flac", fingerprint=f"FP{i}", track_no=i)
               for i in range(5)]
        first = self.client.get("/api/device/fingerprints?limit=2",
                                headers=self._auth()).get_json()
        self.assertEqual([e["track_id"] for e in first["entries"]], ids[:2])
        self.assertEqual(first["next_after"], ids[1])

        second = self.client.get(
            f"/api/device/fingerprints?limit=2&after={first['next_after']}",
            headers=self._auth()).get_json()
        self.assertEqual([e["track_id"] for e in second["entries"]], ids[2:4])

        last = self.client.get(
            f"/api/device/fingerprints?limit=2&after={second['next_after']}",
            headers=self._auth()).get_json()
        self.assertEqual([e["track_id"] for e in last["entries"]], ids[4:])
        # a short page ends the walk
        self.assertIsNone(last["next_after"])

    def test_limit_is_capped(self):
        self._add_held_track("t.flac", fingerprint="FP")
        resp = self.client.get("/api/device/fingerprints?limit=99999", headers=self._auth())
        self.assertEqual(resp.status_code, 200)  # capped, not rejected

    def test_rejects_a_non_numeric_or_negative_cursor(self):
        for query in ("?after=abc", "?limit=nope", "?after=-1", "?limit=0",
                      "?computed_after=abc", "?computed_after=-1"):
            resp = self.client.get(f"/api/device/fingerprints{query}", headers=self._auth())
            self.assertEqual(resp.status_code, 400, query)

    def test_computed_after_filters_out_unchanged_entries(self):
        # #439: the orthogonal filter — a device that already has the low-seq
        # track shouldn't see it again once it asks for only what's newer.
        old = self._add_held_track("old.flac", fingerprint="OLD", fingerprint_seq=1)
        new = self._add_held_track("new.flac", fingerprint="NEW", fingerprint_seq=2)
        body = self.client.get(
            "/api/device/fingerprints?computed_after=1", headers=self._auth()).get_json()
        self.assertEqual([e["track_id"] for e in body["entries"]], [new])
        self.assertNotIn(old, [e["track_id"] for e in body["entries"]])

    def test_computed_after_zero_still_excludes_never_computed_entries(self):
        # A track predating this feature (fingerprint set, fingerprint_seq
        # still NULL from before the migration) must not silently vanish
        # from an incremental walk just because it can't satisfy > 0 --
        # it's still a real fingerprint the device needs, it's only that
        # NOTHING is known about when it was computed.
        never_seq = self._add_held_track("legacy.flac", fingerprint="LEGACY", fingerprint_seq=None)
        seqd = self._add_held_track("new.flac", fingerprint="NEW", fingerprint_seq=1)
        body = self.client.get(
            "/api/device/fingerprints?computed_after=0", headers=self._auth()).get_json()
        ids = [e["track_id"] for e in body["entries"]]
        self.assertIn(seqd, ids)
        self.assertNotIn(never_seq, ids)

    def test_entries_include_fingerprint_seq_for_the_client_to_track(self):
        self._add_held_track("t.flac", fingerprint="FP", fingerprint_seq=7)
        body = self.client.get("/api/device/fingerprints", headers=self._auth()).get_json()
        self.assertEqual(body["entries"][0]["fingerprint_seq"], 7)

    def test_computed_after_combines_with_the_track_id_cursor(self):
        # Both filters apply together -- track_id ordering/pagination is
        # unaffected by the presence of the seq filter.
        skip_by_after = self._add_held_track("a.flac", fingerprint="A", fingerprint_seq=5)
        skip_by_seq = self._add_held_track("b.flac", fingerprint="B", fingerprint_seq=1)
        keep = self._add_held_track("c.flac", fingerprint="C", fingerprint_seq=5)
        body = self.client.get(
            f"/api/device/fingerprints?after={skip_by_after}&computed_after=2",
            headers=self._auth()).get_json()
        self.assertEqual([e["track_id"] for e in body["entries"]], [keep])

    def test_never_leaks_another_devices_tracks(self):
        other, _ = sync_state.create_device(self.conn, self.other, "bob-dev")
        self.conn.commit()
        mine = self._add_held_track("mine.flac", fingerprint="MINE")
        self._add_held_track("theirs.flac", fingerprint="THEIRS", device_id=other)
        body = self.client.get("/api/device/fingerprints", headers=self._auth()).get_json()
        self.assertEqual([e["track_id"] for e in body["entries"]], [mine])

    def test_requires_a_device_token(self):
        self.assertEqual(self.client.get("/api/device/fingerprints").status_code, 401)


class DeviceChangesTriggersFingerprintsTests(_RouteTestBase):
    """#239: device sync is the trigger for computing fingerprints (the locked
    decision — compute on first device sync of a track). Mocked at the
    start_ensure_fingerprints boundary: that function's own backgrounding is
    covered in test_provenance, and what matters here is that /changes fires it
    for the right device without doing the work inline."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.conn.commit()

    def test_changes_triggers_a_fingerprint_pass_for_this_device(self):
        with mock.patch.object(main.provenance, "start_ensure_fingerprints") as start:
            resp = self.client.get(
                "/api/device/changes", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 200)
        start.assert_called_once_with(self.device)

    def test_changes_still_returns_its_payload_if_the_trigger_raises(self):
        # Provenance is a side-benefit; it must never be able to break sync.
        with mock.patch.object(main.provenance, "start_ensure_fingerprints",
                               side_effect=RuntimeError("boom")):
            resp = self.client.get(
                "/api/device/changes", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("to_download", resp.get_json())


class DeviceProvenancePushRouteTests(_RouteTestBase):
    """#239 PR 2: POST /api/device/provenance — a device pushes back what it
    holds, and the server queues a fingerprint rematch. Stores and returns;
    matching is a background job because verifying costs an audio decode."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.conn.commit()

    def _auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _post(self, entries):
        return self.client.post("/api/device/provenance",
                                json={"entries": entries}, headers=self._auth())

    def _jobs(self):
        return self.conn.execute(
            "SELECT type, state, payload, dedupe_key FROM jobs ORDER BY id").fetchall()

    def test_a_push_is_stored_and_queues_one_rematch(self):
        resp = self._post([
            {"track_id": 7, "fingerprint": "FPA", "path": "Ar/Al/01 - A.flac"},
            {"track_id": 8, "fingerprint": "FPB", "path": "Ar/Al/02 - B.flac"},
        ])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"received": 2, "stored": 2, "pending": 2})
        rows = self.conn.execute(
            "SELECT path, fingerprint, claimed_track_id, state FROM device_provenance "
            "WHERE device_id = ? ORDER BY path", (self.device,)).fetchall()
        self.assertEqual([r["fingerprint"] for r in rows], ["FPA", "FPB"])
        self.assertEqual([r["state"] for r in rows], ["pending", "pending"])
        jobs_rows = self._jobs()
        self.assertEqual(len(jobs_rows), 1)
        self.assertEqual(jobs_rows[0]["type"], "provenance_rematch")
        self.assertIn(str(self.device), jobs_rows[0]["dedupe_key"])

    def test_several_pages_collapse_to_one_queued_job(self):
        # A large device pushes in pages; that must not queue N rematches.
        for i in range(3):
            self._post([{"fingerprint": f"FP{i}", "path": f"p{i}.flac"}])
        self.assertEqual(len(self._jobs()), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM device_provenance WHERE device_id = ?",
                              (self.device,)).fetchone()[0], 3)

    def test_the_push_never_matches_inline(self):
        # Verifying costs an audio decode; a device's sync must not wait for it.
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, fingerprint) "
            "VALUES ('c/x.flac', 'Ar', 'Al', 'Ti', 1, 0, 'FPZ')")
        self.conn.commit()
        self._post([{"fingerprint": "FPZ", "path": "dev/x.flac"}])
        self.assertEqual(
            self.conn.execute("SELECT state FROM device_provenance WHERE device_id = ?",
                              (self.device,)).fetchone()["state"], "pending")
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM device_track_state WHERE device_id = ?",
                              (self.device,)).fetchone())

    def test_track_id_is_optional(self):
        resp = self._post([{"fingerprint": "FPA", "path": "a.flac"}])
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(
            self.conn.execute("SELECT claimed_track_id FROM device_provenance "
                              "WHERE device_id = ?", (self.device,)).fetchone()[0])

    def test_rejects_a_non_list_entries(self):
        resp = self.client.post("/api/device/provenance",
                                json={"entries": "nope"}, headers=self._auth())
        self.assertEqual(resp.status_code, 400)

    def test_rejects_an_oversized_page(self):
        entries = [{"fingerprint": "F", "path": f"p{i}"} for i in range(main._PROVENANCE_PUSH_MAX + 1)]
        resp = self._post(entries)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("smaller pages", resp.get_json()["error"])

    def test_rejects_malformed_entries_without_storing_a_partial_page(self):
        # Validation happens up front so one bad entry can't leave half a page
        # committed — a half-stored push would silently under-recover.
        for bad in ([{"fingerprint": "F"}],                      # no path
                    [{"path": "p"}],                             # no fingerprint
                    [{"fingerprint": "", "path": "p"}],          # empty fingerprint
                    [{"fingerprint": "F", "path": ""}],          # empty path
                    ["not-a-dict"],
                    [{"fingerprint": "F", "path": "p", "track_id": "seven"}]):
            resp = self._post([{"fingerprint": "OK", "path": "good.flac"}] + bad)
            self.assertEqual(resp.status_code, 400, bad)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM device_provenance").fetchone()[0], 0)

    def test_requires_a_device_token(self):
        resp = self.client.post("/api/device/provenance", json={"entries": []})
        self.assertEqual(resp.status_code, 401)

    def test_another_devices_push_is_scoped_separately(self):
        other, other_token = sync_state.create_device(self.conn, self.other, "bob-dev")
        self.conn.commit()
        self._post([{"fingerprint": "FPA", "path": "shared-name.flac"}])
        self.client.post("/api/device/provenance",
                         json={"entries": [{"fingerprint": "FPB", "path": "shared-name.flac"}]},
                         headers={"Authorization": f"Bearer {other_token}"})
        rows = self.conn.execute(
            "SELECT device_id, fingerprint FROM device_provenance ORDER BY device_id").fetchall()
        self.assertEqual(len(rows), 2)  # same path, different devices — both kept
        self.assertEqual({r["fingerprint"] for r in rows}, {"FPA", "FPB"})


class DeviceChangesContinuesRematchTests(_RouteTestBase):
    """#239 PR 2: the rematch handler can't re-enqueue itself (it holds its own
    dedupe_key while running), and its batch cap means one push rarely finishes.
    A recovering device syncs repeatedly, so /api/device/changes continues it."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "owner-dev")
        self.conn.commit()
        # /api/device/changes also spawns the fingerprint-computation thread
        # (PR 1). Left real, that daemon thread outlives the test and opens the
        # per-test temp DB after tearDown has deleted it — harmless today but it
        # logs "no such table" and is a latent flake. Mocked here because this
        # class is about the REMATCH continuation, not that pass;
        # DeviceChangesTriggersFingerprintsTests covers the trigger itself.
        patcher = mock.patch.object(main.provenance, "start_ensure_fingerprints")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _changes(self):
        return self.client.get("/api/device/changes",
                               headers={"Authorization": f"Bearer {self.token}"})

    def test_a_sync_queues_a_rematch_while_rows_are_pending(self):
        self.conn.execute(
            "INSERT INTO device_provenance (device_id, path, fingerprint) VALUES (?, 'p', 'F')",
            (self.device,))
        self.conn.commit()
        self.assertEqual(self._changes().status_code, 200)
        types = [r["type"] for r in self.conn.execute("SELECT type FROM jobs")]
        self.assertIn("provenance_rematch", types)

    def test_a_sync_queues_nothing_when_no_recovery_is_underway(self):
        # The overwhelmingly common case — this must add no queue churn.
        self.assertEqual(self._changes().status_code, 200)
        types = [r["type"] for r in self.conn.execute("SELECT type FROM jobs")]
        self.assertNotIn("provenance_rematch", types)

    def test_a_sync_still_succeeds_if_the_continuation_fails(self):
        # Provenance is a side-benefit; it must never break a device's sync.
        with mock.patch.object(main.provenance, "pushed_pending_count",
                               side_effect=RuntimeError("boom")):
            resp = self._changes()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("to_download", resp.get_json())


class DeviceUnknownTracksRouteTests(_RouteTestBase):
    """#161: the owner reviews + adopts a device's unknown extras via the
    session-authed /api/devices/<id>/unknown-tracks routes."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "owner-dev")
        # seed two unknown extras (neither matches any library track)
        self.client.post(
            "/api/device/manifest",
            json={"paths": ["Ghosts/Nowhere/03 - Drift.mp3", "loose.flac"]},
            headers={"Authorization": f"Bearer {self.token}"})

    def test_list_returns_parsed_extras_for_the_owner(self):
        # no session → not allowed through
        self.assertNotEqual(
            self.client.get(f"/api/devices/{self.device}/unknown-tracks").status_code, 200)
        _login(self.client, self.owner)
        resp = self.client.get(f"/api/devices/{self.device}/unknown-tracks")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertEqual({r["path"] for r in rows}, {"Ghosts/Nowhere/03 - Drift.mp3", "loose.flac"})
        drift = next(r for r in rows if r["path"] == "Ghosts/Nowhere/03 - Drift.mp3")
        self.assertEqual((drift["artist"], drift["album"], drift["title"]), ("Ghosts", "Nowhere", "Drift"))
        self.assertFalse(drift["adopted"])

    def test_adopt_reduces_the_flagged_count(self):
        _login(self.client, self.owner)
        resp = self.client.post(
            f"/api/devices/{self.device}/unknown-tracks/adopt",
            json={"paths": ["loose.flac"], "adopted": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"unknown_track_count": 1})
        rows = self.client.get(f"/api/devices/{self.device}/unknown-tracks").get_json()
        self.assertTrue(next(r for r in rows if r["path"] == "loose.flac")["adopted"])

    def test_adopt_rejects_non_list(self):
        _login(self.client, self.owner)
        resp = self.client.post(
            f"/api/devices/{self.device}/unknown-tracks/adopt", json={"paths": "nope"})
        self.assertEqual(resp.status_code, 400)

    def test_a_different_user_is_denied(self):
        _login(self.client, self._make_user("intruder"))
        self.assertEqual(
            self.client.get(f"/api/devices/{self.device}/unknown-tracks").status_code, 403)


class EnrollmentRouteTests(_RouteTestBase):
    """#163: web session mints an enrollment grant; a device (no session)
    redeems it to create itself + get a token."""

    def test_grant_mints_a_code_for_the_session_user(self):
        _login(self.client, self.owner)
        resp = self.client.post("/api/enrollment/grant")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["code"]), 8)

    def test_redeem_creates_device_owned_by_grant_user(self):
        _login(self.client, self.owner)
        code = self.client.post("/api/enrollment/grant").get_json()["code"]
        resp = self.client.post(
            "/api/enrollment/redeem", json={"code": code, "name": "Pixel"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("token", resp.get_json())
        owner = self.conn.execute(
            "SELECT owner_user_id FROM devices WHERE name='Pixel'").fetchone()["owner_user_id"]
        self.assertEqual(owner, self.owner)

    def test_redeem_invalid_code_is_400(self):
        resp = self.client.post(
            "/api/enrollment/redeem", json={"code": "NOPENOPE", "name": "X"})
        self.assertEqual(resp.status_code, 400)


class AdminUserOwnedCountsTests(_RouteTestBase):
    """#332: the users list carries per-user owned counts, so the delete dialog can
    name WHICH ownership blocks a deletion before anything is attempted.

    The old flow was a native confirm() with a fixed string, then a 400 listing
    three categories without saying which one, how many, or which items."""

    def test_the_users_list_reports_owned_counts(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/users")
        self.assertEqual(resp.status_code, 200)
        by_id = {u["id"]: u for u in resp.get_json()}
        for key in ("owned_devices", "owned_selections", "owned_playlists"):
            self.assertIn(key, by_id[self.owner], key)

    def test_counts_are_per_user_and_not_multiplied_together(self):
        # Correlated subqueries, not joins: three one-to-many counts joined would
        # multiply each other (2 devices x 3 selections -> 6 of each).
        self.conn.execute(
            "INSERT INTO devices (owner_user_id, name, api_token_hash) VALUES (?, 'd1', 'h1')",
            (self.owner,))
        self.conn.execute(
            "INSERT INTO devices (owner_user_id, name, api_token_hash) VALUES (?, 'd2', 'h2')",
            (self.owner,))
        for n in range(3):
            self.conn.execute(
                "INSERT INTO selections (type, target, created_by_user_id) "
                "VALUES ('album', ?, ?)", (f"a{n}", self.owner))
        self.conn.commit()
        _login(self.client, self.admin)
        me = {u["id"]: u for u in self.client.get("/api/admin/users").get_json()}[self.owner]
        self.assertEqual(me["owned_devices"], 2)
        self.assertEqual(me["owned_selections"], 3)
        self.assertEqual(me["owned_playlists"], 0)

    def test_deleting_an_owner_is_refused_without_implying_reassignment(self):
        self.conn.execute(
            "INSERT INTO devices (owner_user_id, name, api_token_hash) VALUES (?, 'd', 'h')",
            (self.owner,))
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.delete(f"/api/admin/users/{self.owner}")
        self.assertEqual(resp.status_code, 400)
        msg = resp.get_json()["error"]
        # "or reassign" promised a route that mostly does not exist (#70).
        self.assertNotIn("reassign", msg.lower())
        self.assertIn("delete those first", msg.lower())

    def test_an_unencumbered_user_can_still_be_deleted(self):
        # The guard must not have become a blanket refusal.
        self.conn.execute("INSERT INTO users (username) VALUES ('spare')")
        self.conn.commit()
        spare = self.conn.execute(
            "SELECT id FROM users WHERE username='spare'").fetchone()["id"]
        _login(self.client, self.admin)
        self.assertEqual(self.client.delete(f"/api/admin/users/{spare}").status_code, 200)


class AdminUserPasswordResetTests(_RouteTestBase):
    """#237: admin-only password reset — the recovery counterpart to a
    user's own change-password flow, so a forgotten local password never
    has to mean starting over with a new account."""

    def test_admin_can_reset_another_users_password(self):
        _login(self.client, self.admin)
        resp = self.client.put(
            f"/api/admin/users/{self.owner}/password", json={"password": "new-password-123"})
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (self.owner,)).fetchone()
        self.assertIsNotNone(row["password_hash"])

    def test_admin_can_reset_another_admins_password(self):
        # Deliberately allowed (see the route's own docstring) — the
        # alternative is an unrecoverable lockout on a forgotten admin
        # password.
        other_admin = self._make_user("second-admin", is_admin=True)
        _login(self.client, self.admin)
        resp = self.client.put(
            f"/api/admin/users/{other_admin}/password", json={"password": "new-password-123"})
        self.assertEqual(resp.status_code, 200)

    def test_password_under_8_chars_rejected(self):
        _login(self.client, self.admin)
        resp = self.client.put(
            f"/api/admin/users/{self.owner}/password", json={"password": "short"})
        self.assertEqual(resp.status_code, 400)

    def test_nonexistent_user_is_404(self):
        _login(self.client, self.admin)
        resp = self.client.put(
            "/api/admin/users/999999/password", json={"password": "new-password-123"})
        self.assertEqual(resp.status_code, 404)

    def test_non_admin_is_forbidden(self):
        _login(self.client, self.owner)
        resp = self.client.put(
            f"/api/admin/users/{self.other}/password", json={"password": "new-password-123"})
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_is_401(self):
        resp = self.client.put(
            f"/api/admin/users/{self.owner}/password", json={"password": "new-password-123"})
        self.assertEqual(resp.status_code, 401)


class AdminUserDeleteLockoutGuardrailTests(_RouteTestBase):
    """#237: two ways a delete could remove the last working login —
    deleting the last admin, or (under OIDC specifically) the sole
    remaining local-password account — both blocked with a clear error
    rather than silently permitted."""

    def test_admin_cannot_delete_own_account_even_as_sole_admin(self):
        # This is what actually keeps the admin count from ever reaching
        # zero in a real request: actor and target can never differ while
        # the admin count is <= 1, since both would have to be admins,
        # pushing the count to >= 2. So the practical, reachable guard
        # against "delete the last admin" is the pre-existing own-account
        # check, not the count check below — this test documents that.
        _login(self.client, self.admin)  # the fixture's only admin
        resp = self.client.delete(f"/api/admin/users/{self.admin}")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("own account", resp.get_json()["error"])

    def test_last_admin_count_guard_fires_in_isolation(self):
        # The count guard itself can't be reached via a normal request (see
        # the note above) — it's defense-in-depth for a future refactor
        # where the two admin-identity reads in the route (the initial
        # `admin_id` and require_admin's own check) could diverge, e.g. a
        # race. Exercise it directly by decoupling those two reads: the
        # first (compared against target_user_id) returns a phantom id, the
        # second (require_admin's) returns the real admin, so the route
        # still authorizes the caller but its own-account check no longer
        # matches.
        _login(self.client, self.admin)
        with mock.patch.object(main, "get_current_user_id", side_effect=[999999, self.admin]):
            resp = self.client.delete(f"/api/admin/users/{self.admin}")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("last admin", resp.get_json()["error"])

    def test_deleting_sole_local_password_account_under_oidc_is_blocked(self):
        # self.admin has no password_hash by default (_make_user doesn't set
        # one) — give it one, matching a real break-glass account.
        self.conn.execute(
            "UPDATE users SET password_hash = 'x' WHERE id = ?", (self.admin,))
        self.conn.commit()
        second_admin = self._make_user("second-admin", is_admin=True)
        _login(self.client, second_admin)
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.delete(f"/api/admin/users/{self.admin}")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("local password", resp.get_json()["error"])

    def test_same_scenario_is_not_blocked_under_local_auth_mode(self):
        # AUTH_MODE=local has no IdP to be locked out of, so this guard is
        # OIDC-specific — confirmed by asserting the *other* guard (a
        # non-admin target has no last-admin concern) doesn't accidentally
        # fire here either.
        self.conn.execute(
            "UPDATE users SET password_hash = 'x' WHERE id = ?", (self.owner,))
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "local"):
            resp = self.client.delete(f"/api/admin/users/{self.owner}")
        self.assertEqual(resp.status_code, 200)

    def test_deleting_a_non_sole_local_password_account_under_oidc_is_fine(self):
        self.conn.execute(
            "UPDATE users SET password_hash = 'x' WHERE id IN (?, ?)", (self.admin, self.owner))
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.delete(f"/api/admin/users/{self.owner}")
        self.assertEqual(resp.status_code, 200)  # self.admin still has one


class OidcBreakGlassPasswordTests(_RouteTestBase):
    """#235 part 2: under AUTH_MODE=oidc, a local password only works — and
    can only be set — for admin accounts. Every surface that could create or
    honour a non-admin's local password there (login, self-service change,
    admin-managed create/reset) treats it as if it doesn't exist, so
    break-glass really is admin-only rather than a standing per-user IdP
    bypass. forward mode is untouched throughout: its own break-glass
    already requires the separate emergency port, unlike oidc's plain
    /login."""

    def _set_password(self, user_id: int, password: str) -> None:
        self.conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (main.generate_password_hash(password), user_id))
        self.conn.commit()

    # --- /login ---

    def test_login_honours_admin_password_under_oidc(self):
        self._set_password(self.admin, "adminpass123")
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.post("/login", data={"username": "admin", "password": "adminpass123"})
        self.assertEqual(resp.status_code, 302)

    def test_login_rejects_correct_non_admin_password_under_oidc(self):
        self._set_password(self.owner, "ownerpass123")
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.post("/login", data={"username": "owner", "password": "ownerpass123"})
        self.assertEqual(resp.status_code, 200)  # re-rendered login form, not a redirect

    def test_login_honours_non_admin_password_under_local_mode(self):
        # Unchanged legacy behaviour — the oidc restriction is mode-specific.
        self._set_password(self.owner, "ownerpass123")
        with mock.patch.object(main, "AUTH_MODE", "local"):
            resp = self.client.post("/login", data={"username": "owner", "password": "ownerpass123"})
        self.assertEqual(resp.status_code, 302)

    # --- self-service POST /api/account/password ---

    def test_self_service_password_change_blocked_for_non_admin_under_oidc(self):
        _login(self.client, self.owner)
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.post("/api/account/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 400)

    def test_self_service_password_change_allowed_for_admin_under_oidc(self):
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.post("/api/account/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    def test_self_service_password_change_allowed_for_non_admin_under_local_mode(self):
        _login(self.client, self.owner)
        with mock.patch.object(main, "AUTH_MODE", "local"):
            resp = self.client.post("/api/account/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    # --- admin-managed create POST /api/admin/users ---

    def test_creating_local_account_blocked_under_oidc(self):
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.post(
                "/api/admin/users", json={"username": "newuser", "password": "newpassword1"})
        self.assertEqual(resp.status_code, 400)

    def test_creating_local_account_allowed_under_local_mode(self):
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "local"):
            resp = self.client.post(
                "/api/admin/users", json={"username": "newuser", "password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    # --- admin-managed reset PUT /api/admin/users/<id>/password ---

    def test_admin_reset_blocked_for_non_admin_target_under_oidc(self):
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.put(
                f"/api/admin/users/{self.owner}/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 400)

    def test_admin_reset_allowed_for_admin_target_under_oidc(self):
        other_admin = self._make_user("second-admin", is_admin=True)
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.put(
                f"/api/admin/users/{other_admin}/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    def test_admin_reset_allowed_for_non_admin_target_under_local_mode(self):
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "local"):
            resp = self.client.put(
                f"/api/admin/users/{self.owner}/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    def test_admin_reset_404_for_nonexistent_user_under_oidc(self):
        _login(self.client, self.admin)
        with mock.patch.object(main, "AUTH_MODE", "oidc"):
            resp = self.client.put(
                "/api/admin/users/999999/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 404)


class BreakGlassSetFlagTests(_RouteTestBase):
    """#246: /api/admin/config's break_glass_set flag — whether ANY admin
    has a local password, driving the Administration break-glass card's
    warning state. Not oidc-gated itself (the frontend only shows the card
    under oidc, but the flag's truthfulness doesn't depend on that)."""

    def _get_flag(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["break_glass_set"]

    def test_false_when_no_admin_has_a_local_password(self):
        # self.admin has no password_hash by default (_make_user doesn't set one).
        self.assertFalse(self._get_flag())

    def test_true_once_the_admin_sets_one(self):
        self.conn.execute(
            "UPDATE users SET password_hash = 'x' WHERE id = ?", (self.admin,))
        self.conn.commit()
        self.assertTrue(self._get_flag())

    def test_a_non_admins_password_does_not_count(self):
        self.conn.execute(
            "UPDATE users SET password_hash = 'x' WHERE id = ?", (self.owner,))
        self.conn.commit()
        self.assertFalse(self._get_flag())

    def test_true_if_any_admin_has_one_not_just_the_caller(self):
        other_admin = self._make_user("second-admin", is_admin=True)
        self.conn.execute(
            "UPDATE users SET password_hash = 'x' WHERE id = ?", (other_admin,))
        self.conn.commit()
        self.assertTrue(self._get_flag())


class DashboardWidgetsCoverLimitTests(unittest.TestCase):
    """#269: dashboard_widgets.settings.cover_limit is clamped to the
    curated grid-tidy set server-side too — settings arrives over the API,
    so a client bypassing the dropdown (or a stale/hand-edited row) can't
    push an arbitrary count into the template's cover grid."""

    def test_default_when_absent(self):
        result = main._normalize_dashboard_widgets({"disabled": []})
        self.assertEqual(result["settings"]["cover_limit"], 15)

    def test_default_for_non_dict_input(self):
        result = main._normalize_dashboard_widgets(None)
        self.assertEqual(result["settings"]["cover_limit"], 15)

    def test_valid_values_pass_through(self):
        for n in (15, 30, 45, 60):
            with self.subTest(n=n):
                result = main._normalize_dashboard_widgets({"settings": {"cover_limit": n}})
                self.assertEqual(result["settings"]["cover_limit"], n)

    def test_out_of_range_value_clamped_to_default(self):
        result = main._normalize_dashboard_widgets({"settings": {"cover_limit": 999}})
        self.assertEqual(result["settings"]["cover_limit"], 15)

    def test_non_numeric_value_clamped_to_default(self):
        result = main._normalize_dashboard_widgets({"settings": {"cover_limit": "lots"}})
        self.assertEqual(result["settings"]["cover_limit"], 15)

    def test_other_settings_keys_preserved(self):
        # Per-widget settings (e.g. recently_added's "months") share the
        # same dict — validating cover_limit must not drop them.
        result = main._normalize_dashboard_widgets(
            {"settings": {"recently_added": {"months": 6}, "cover_limit": 30}})
        self.assertEqual(result["settings"]["recently_added"], {"months": 6})
        self.assertEqual(result["settings"]["cover_limit"], 30)


class DashboardWidgetsOrderTests(unittest.TestCase):
    """#263: dashboard_widgets.order — the server only checks "is this a
    list", since the widget catalog it orders lives client-side (a stale
    or unknown id in the list is the frontend's problem to fall back on,
    not something the server can validate without duplicating the catalog)."""

    def test_default_empty_when_absent(self):
        result = main._normalize_dashboard_widgets({"disabled": []})
        self.assertEqual(result["order"], [])

    def test_default_empty_for_non_dict_input(self):
        result = main._normalize_dashboard_widgets(None)
        self.assertEqual(result["order"], [])

    def test_valid_list_passes_through(self):
        result = main._normalize_dashboard_widgets({"order": ["devices", "library"]})
        self.assertEqual(result["order"], ["devices", "library"])

    def test_non_list_value_falls_back_to_empty(self):
        result = main._normalize_dashboard_widgets({"order": "devices,library"})
        self.assertEqual(result["order"], [])

    def test_order_does_not_disturb_disabled_or_settings(self):
        result = main._normalize_dashboard_widgets(
            {"disabled": ["administration"], "order": ["devices"], "settings": {"cover_limit": 30}})
        self.assertEqual(result["disabled"], ["administration"])
        self.assertEqual(result["order"], ["devices"])
        self.assertEqual(result["settings"]["cover_limit"], 30)


class ProfileCoverLimitRouteTests(_RouteTestBase):
    """Same clamp, exercised through the real PUT /api/profile route — a
    request crafted directly against the API (not through the UI's curated
    dropdown) is the actual threat model the server-side clamp guards."""

    def _put_cover_limit(self, value):
        _login(self.client, self.owner)
        resp = self.client.put(
            "/api/profile",
            json={"dashboard_widgets": {"disabled": [], "settings": {"cover_limit": value}}},
        )
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["dashboard_widgets"]["settings"]["cover_limit"]

    def test_valid_value_round_trips(self):
        self.assertEqual(self._put_cover_limit(45), 45)

    def test_out_of_range_value_is_clamped(self):
        self.assertEqual(self._put_cover_limit(999999), 15)


class ProfileWidgetOrderRouteTests(_RouteTestBase):
    """#263: dashboard_widgets.order round-trips through the real route,
    same as cover_limit above."""

    def test_order_round_trips(self):
        _login(self.client, self.owner)
        resp = self.client.put(
            "/api/profile",
            json={"dashboard_widgets": {"disabled": [], "order": ["recently_added", "library"]}},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.get_json()["dashboard_widgets"]["order"], ["recently_added", "library"])


class ProfileHideZeroMatchPlaylistsRouteTests(_RouteTestBase):
    """#411: hide_zero_match_playlists round-trips through the real route,
    same as cover_limit/order above — and defaults to off (today's
    behaviour, every playlist shown) for an account that's never set it."""

    def test_defaults_to_false(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/profile")
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.get_json()["hide_zero_match_playlists"], False)

    def test_round_trips_true(self):
        _login(self.client, self.owner)
        resp = self.client.put("/api/profile", json={"hide_zero_match_playlists": True})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.get_json()["hide_zero_match_playlists"], True)

    def test_round_trips_false_after_true(self):
        _login(self.client, self.owner)
        self.client.put("/api/profile", json={"hide_zero_match_playlists": True})
        resp = self.client.put("/api/profile", json={"hide_zero_match_playlists": False})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.get_json()["hide_zero_match_playlists"], False)


class MostPlayedRouteTests(_RouteTestBase):
    """#267: /api/suggestions/most-played — merges both configured
    sources, dedupes, sorts by playcount, and clamps a garbage ?limit
    rather than 500ing on int()."""

    def _mock_response(self, json_body):
        resp = mock.Mock()
        resp.json.return_value = json_body
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        return resp

    def test_empty_when_neither_service_configured(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/suggestions/most-played")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_merges_and_sorts_both_sources_by_playcount(self):
        self.conn.execute(
            "UPDATE users SET lastfm_username = 'alice', lastfm_api_key = 'k', "
            "listenbrainz_username = 'alice' WHERE id = ?", (self.owner,))
        self.conn.commit()
        lastfm_body = {"topalbums": {"album": [
            {"artist": {"name": "A"}, "name": "Low playcount", "playcount": "5"},
        ]}}
        lb_body = {"payload": {"release_groups": [
            {"artist_name": "B", "release_group_name": "High playcount", "listen_count": 99},
        ]}}
        _login(self.client, self.owner)
        with mock.patch("requests.get", side_effect=[
            self._mock_response(lastfm_body), self._mock_response(lb_body)]):
            resp = self.client.get("/api/suggestions/most-played")
        self.assertEqual(resp.status_code, 200)
        albums = [r["album"] for r in resp.get_json()]
        self.assertEqual(albums, ["High playcount", "Low playcount"])

    def test_garbage_limit_falls_back_to_default_instead_of_500ing(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/suggestions/most-played?limit=not-a-number")
        self.assertEqual(resp.status_code, 200)

    def _configure_both_services(self):
        self.conn.execute(
            "UPDATE users SET lastfm_username = 'alice', lastfm_api_key = 'k', "
            "listenbrainz_username = 'alice' WHERE id = ?", (self.owner,))
        self.conn.commit()

    def test_garbage_period_falls_back_to_default_instead_of_500ing(self):
        self._configure_both_services()
        _login(self.client, self.owner)
        with mock.patch("requests.get", return_value=self._mock_response(
                {"topalbums": {"album": []}})):
            resp = self.client.get("/api/suggestions/most-played?period=not-a-real-period")
        self.assertEqual(resp.status_code, 200)

    def test_non_default_period_reaches_both_providers_correctly_mapped(self):
        # #283: ?period used to only ever reach the Last.fm call — ListenBrainz
        # always got its own half_yearly default regardless. Confirm both
        # requests actually carry the right value for a non-default period.
        self._configure_both_services()
        _login(self.client, self.owner)
        with mock.patch("requests.get", return_value=self._mock_response(
                {"topalbums": {"album": []}, "payload": {"release_groups": []}})) as get:
            resp = self.client.get("/api/suggestions/most-played?period=12month")
        self.assertEqual(resp.status_code, 200)
        lastfm_call, listenbrainz_call = get.call_args_list
        self.assertEqual(lastfm_call.kwargs["params"]["period"], "12month")
        self.assertEqual(listenbrainz_call.kwargs["params"]["range"], "year")


class SuggestionsPeriodMappingRouteTests(_RouteTestBase):
    """#283: the same period validation/ListenBrainz-mapping fix, exercised
    through the older /api/suggestions route — it had the identical bug
    (ListenBrainz suggestions always used half_yearly regardless of
    ?period), just never filed as its own issue since #283 was written
    against the newer /api/suggestions/most-played route."""

    def _mock_response(self, json_body):
        resp = mock.Mock()
        resp.json.return_value = json_body
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        return resp

    def test_non_default_period_reaches_listenbrainz_mapped(self):
        # api_suggestions() also calls recently_played_suggestions() (a
        # separate, range-less /listens endpoint) when listenbrainz_username
        # is set — search every call rather than assume call order, so this
        # doesn't depend on which of the two the route happens to make last.
        self.conn.execute(
            "UPDATE users SET listenbrainz_username = 'alice' WHERE id = ?", (self.owner,))
        self.conn.commit()
        _login(self.client, self.owner)
        with mock.patch("requests.get", return_value=self._mock_response(
                {"payload": {"release_groups": [], "listens": []}})) as get:
            resp = self.client.get("/api/suggestions?period=1month")
        self.assertEqual(resp.status_code, 200)
        ranges = [c.kwargs["params"]["range"] for c in get.call_args_list if "range" in c.kwargs.get("params", {})]
        self.assertEqual(ranges, ["month"])


class LibraryQuizPairRouteTests(_RouteTestBase):
    """#508: thin route-layer coverage for api_library_quiz_pair() — the
    actual pair-selection logic has its own thorough, DB-free unit tests in
    test_library_quiz.py. This just checks the SQL rows thread through
    library_quiz.py's dict-shaped inputs correctly and the JSON contract
    holds end to end."""

    def _make_track(self, relative_path, artist, album):
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES (?, ?, ?, 'T', 1000, 0)",
            (relative_path, artist, album),
        )
        self.conn.commit()

    def test_not_enough_eligible_artists_reports_unavailable(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/library/quiz-pair")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"available": False})

    def test_a_real_gap_pair_is_returned_with_album_counts(self):
        for i in range(library_quiz.MIN_ALBUMS):
            self._make_track(f"small/{i}.flac", "Small Artist", f"Album {i}")
        for i in range(library_quiz.MIN_ALBUMS + 10):
            self._make_track(f"big/{i}.flac", "Big Artist", f"Album {i}")
        _login(self.client, self.owner)
        resp = self.client.get("/api/library/quiz-pair")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["available"])
        names = {body["a"]["artist"], body["b"]["artist"]}
        self.assertEqual(names, {"Small Artist", "Big Artist"})
        counts = {body["a"]["artist"]: body["a"]["album_count"],
                  body["b"]["artist"]: body["b"]["album_count"]}
        self.assertEqual(counts["Small Artist"], library_quiz.MIN_ALBUMS)
        self.assertEqual(counts["Big Artist"], library_quiz.MIN_ALBUMS + 10)

    def test_various_artists_never_appears_in_a_pair(self):
        # "Various Artists" would otherwise win every round on raw album
        # count alone (it's the phantom mega-artist every compilation's
        # per-track artist tag assembles into — see library_quiz.py).
        for i in range(50):
            self._make_track(f"va/{i}.flac", "Various Artists", f"Comp {i}")
        for i in range(library_quiz.MIN_ALBUMS):
            self._make_track(f"a/{i}.flac", "Artist A", f"Album {i}")
        for i in range(library_quiz.MIN_ALBUMS + 5):
            self._make_track(f"b/{i}.flac", "Artist B", f"Album {i}")
        _login(self.client, self.owner)
        resp = self.client.get("/api/library/quiz-pair")
        body = resp.get_json()
        self.assertTrue(body["available"])
        names = {body["a"]["artist"], body["b"]["artist"]}
        self.assertNotIn("Various Artists", names)

    def test_deleted_tracks_are_excluded_from_album_counts(self):
        for i in range(library_quiz.MIN_ALBUMS):
            self._make_track(f"a/{i}.flac", "Artist A", f"Album {i}")
        for i in range(library_quiz.MIN_ALBUMS + 5):
            self._make_track(f"b/{i}.flac", "Artist B", f"Album {i}")
        self.conn.execute(
            "UPDATE tracks SET deleted_at = '2026-01-01' WHERE artist = 'Artist B'")
        self.conn.commit()
        _login(self.client, self.owner)
        resp = self.client.get("/api/library/quiz-pair")
        body = resp.get_json()
        # Artist B's albums are all soft-deleted, so it drops below
        # MIN_ALBUMS and can't appear at all — not enough real candidates left.
        self.assertEqual(body, {"available": False})


if __name__ == "__main__":
    unittest.main()
