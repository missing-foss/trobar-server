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
import contextlib
import io
import json
from datetime import timedelta
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import time
import threading
import unittest
import urllib.parse
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

import admin            # noqa: E402
import app_api          # noqa: E402
import auth             # noqa: E402
import credential_store  # noqa: E402
import play_forward     # noqa: E402
import tidal_client     # noqa: E402
import device_api       # noqa: E402
import devices          # noqa: E402
import filesystem_client  # noqa: E402
import history_cache    # noqa: E402
import integrations     # noqa: E402
import jobs             # noqa: E402
import library_quiz     # noqa: E402
import ops               # noqa: E402
import lidarr_requests  # noqa: E402
import main             # noqa: E402
import mirror_sinks     # noqa: E402
import playlist_sync    # noqa: E402
import playlists        # noqa: E402
import scanner          # noqa: E402
import settings         # noqa: E402
import sync_state       # noqa: E402
import user_profile     # noqa: E402
import web_common       # noqa: E402


_NO_HISTORY_CACHE = mock.patch.object(history_cache, "TTL_SECONDS", 0)


def setUpModule():
    # unittest calls this every time it enters the module, and
    # tearDownModule every time it leaves; a selection that interleaves
    # modules (`test_routes.A test_maloja test_routes.B`) comes back after
    # the dir was removed. _TMP is made once, at import.
    # Every test answers for the services itself: a cached answer from an
    # earlier test must not stand in for its mock (history_cache).
    _NO_HISTORY_CACHE.start()
    os.makedirs(_TMP, exist_ok=True)


def tearDownModule():
    # Remove the whole throwaway dir, not just per-test .db files —
    # tearDown unlinks the .db but WAL-mode leaves .db-wal/.db-shm
    # sidecars, and the module-level flask_secret_key lives here too.
    _NO_HISTORY_CACHE.stop()
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


class RulePlaylistRouteTests(_RouteTestBase):
    """The rule playlist routes: live count, create, read, edit, and who may."""

    RULE = {"match": "all", "conditions": [{"field": "artist", "op": "contains", "value": "air"}]}

    def setUp(self):
        super().setUp()
        for path, artist in (("a/1.flac", "Air"), ("a/2.flac", "Air"), ("b/1.flac", "Beck")):
            self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                "VALUES (?, ?, 'x', ?, 1, 0)", (path, artist, path))
        self.conn.commit()
        _login(self.client, self.owner)

    def _create(self, title="Air", rule=None):
        return self.client.post("/api/provider/playlists/rule", json={"title": title, "rule": rule or self.RULE})

    def _entries(self, playlist_id):
        return self.conn.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?",
                                 (playlist_id,)).fetchone()[0]

    def test_routes_need_a_session(self):
        anonymous = main.app.test_client()
        for resp in (anonymous.post("/api/provider/playlists/rule/count", json={"rule": self.RULE}),
                     anonymous.post("/api/provider/playlists/rule", json={"title": "x", "rule": self.RULE}),
                     anonymous.get("/api/provider/playlists/1/rule")):
            self.assertEqual(resp.status_code, 401)

    def test_count_says_what_matches_and_what_the_limit_keeps(self):
        resp = self.client.post("/api/provider/playlists/rule/count", json={"rule": {**self.RULE, "limit": 1}})
        self.assertEqual(resp.get_json(), {"matches": 2, "tracks": 1})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0], 0)

    def test_create_fills_a_private_playlist_of_the_caller(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201)
        row = self.conn.execute("SELECT * FROM playlists WHERE id = ?", (resp.get_json()["id"],)).fetchone()
        self.assertEqual((row["source_provider"], row["owner_user_id"], row["shared"]), ("rule", self.owner, 0))
        self.assertEqual(self._entries(row["id"]), 2)

    def test_a_field_or_test_that_is_not_text_is_a_400(self):
        for condition in ({"field": [], "op": "is", "value": 1}, {"field": {}, "op": "is", "value": 1},
                          {"field": "year", "op": [], "value": 1}, {"field": "duration", "op": {}, "value": 1}):
            resp = self.client.post("/api/provider/playlists/rule/count",
                                    json={"rule": {"match": "all", "conditions": [condition]}})
            self.assertEqual(resp.status_code, 400, condition)

    def test_two_rule_playlists_can_share_a_name(self):
        self.assertEqual(self._create().status_code, 201)
        _login(self.client, self.other)
        self.assertEqual(self._create().status_code, 201)

    def test_a_bad_rule_or_title_creates_nothing(self):
        for body in ({"title": "x"}, {"title": "x", "rule": {"match": "all", "conditions": []}},
                     {"title": "", "rule": self.RULE}):
            self.assertEqual(self.client.post("/api/provider/playlists/rule", json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/provider/playlists/rule", json=[1]).status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0], 0)

    def test_get_returns_the_rule_for_editing(self):
        playlist_id = self._create().get_json()["id"]
        got = self.client.get(f"/api/provider/playlists/{playlist_id}/rule").get_json()
        self.assertEqual(got["title"], "Air")
        self.assertEqual(got["rule"]["conditions"], self.RULE["conditions"])

    def test_put_replaces_title_and_rule_and_refills(self):
        playlist_id = self._create().get_json()["id"]
        rule = {"match": "all", "conditions": [{"field": "artist", "op": "is", "value": "Beck"}]}
        resp = self.client.put(f"/api/provider/playlists/{playlist_id}/rule", json={"title": "Beck", "rule": rule})
        self.assertEqual(resp.get_json(), {"status": "ok", "tracks": 1})
        self.assertEqual(self._entries(playlist_id), 1)
        got = self.client.get(f"/api/provider/playlists/{playlist_id}/rule").get_json()
        self.assertEqual((got["title"], got["rule"]["conditions"]), ("Beck", rule["conditions"]))

    def test_a_bad_put_changes_nothing(self):
        playlist_id = self._create().get_json()["id"]
        resp = self.client.put(f"/api/provider/playlists/{playlist_id}/rule", json={"title": "x", "rule": {}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.get(f"/api/provider/playlists/{playlist_id}/rule").get_json()["title"], "Air")
        self.assertEqual(self._entries(playlist_id), 2)

    def test_only_the_owner_or_an_admin_reads_or_edits_a_rule(self):
        playlist_id = self._create().get_json()["id"]
        url = f"/api/provider/playlists/{playlist_id}/rule"
        _login(self.client, self.other)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.put(url, json={"title": "x", "rule": self.RULE}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/provider/playlists/{playlist_id}").status_code, 403)
        _login(self.client, self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_rule_routes_refuse_a_playlist_without_a_rule(self):
        playlist_id = self._make_playlist("Other", self.owner)
        self.assertEqual(self.client.get(f"/api/provider/playlists/{playlist_id}/rule").status_code, 400)
        self.assertEqual(self.client.get("/api/provider/playlists/99999/rule").status_code, 404)

    def test_tracks_are_not_written_by_hand_but_the_playlist_can_be_deleted(self):
        playlist_id = self._create().get_json()["id"]
        resp = self.client.put(f"/api/provider/playlists/{playlist_id}/tracks", json={"track_ids": []})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._entries(playlist_id), 2)
        self.assertEqual(self.client.delete(f"/api/provider/playlists/{playlist_id}").status_code, 200)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)).fetchone())


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


class PlaylistInferredAlbumTests(_RouteTestBase):
    """A gap's album looked up in MusicBrainz (inferred_album) makes the
    playlist requestable from Lidarr, and the gaps review gets it apart
    from the source's own album."""

    def setUp(self):
        super().setUp()
        self.playlist = self._make_playlist("Mix", owner_user_id=None, shared=1)
        cur = self.conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album) "
            "VALUES (?, 0, 'Band', 'Song', '')", (self.playlist,))
        self.gap = cur.lastrowid
        self.conn.commit()

    def _row(self):
        _login(self.client, self.owner)
        resp = self.client.get("/api/provider/playlists")
        return next(p for p in resp.get_json() if p["id"] == self.playlist)

    def test_a_gap_without_any_album_makes_nothing_requestable(self):
        self.assertFalse(self._row()["lidarr_request_has_albums"])

    def test_a_looked_up_album_makes_the_gap_requestable(self):
        self.conn.execute("UPDATE unresolved_playlist_tracks SET inferred_album = 'Found' WHERE id = ?",
                          (self.gap,))
        self.conn.commit()
        self.assertTrue(self._row()["lidarr_request_has_albums"])

    def test_the_review_lists_the_looked_up_album_apart_from_the_source_one(self):
        self.conn.execute("UPDATE unresolved_playlist_tracks SET inferred_album = 'Found' WHERE id = ?",
                          (self.gap,))
        self.conn.commit()
        _login(self.client, self.owner)
        rows = self.client.get(f"/api/provider/playlists/{self.playlist}/unresolved-tracks").get_json()
        self.assertEqual((rows[0]["album"], rows[0]["inferred_album"]), ("", "Found"))


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

    def test_a_toggle_commits_before_the_sink_runs(self):
        other = sqlite3.connect(db.DB_PATH, timeout=0)
        self.addCleanup(other.close)

        def write_elsewhere(_conn, _playlist_id):
            other.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('w', '1')")
            other.commit()
        for enabled, name in ((True, "write_mirror"), (False, "delete_mirror")):
            with self.subTest(enabled=enabled), \
                    mock.patch(f"mirror.{name}", side_effect=write_elsewhere) as sink:
                self.assertEqual(self._toggle(self.owner, self.owned, enabled).status_code, 200)
                sink.assert_called_once()

    def test_a_toggle_waits_for_a_write_in_progress(self):
        lock = mirror_sinks.playlist_lock(self.owned)
        lock.acquire()
        self.addCleanup(lambda: lock.release() if held else None)
        held = True
        responses = []
        thread = threading.Thread(target=lambda: responses.append(
            self._toggle(self.owner, self.owned, True)))
        thread.start()
        thread.join(0.2)
        self.assertTrue(thread.is_alive(), "the toggle did not wait for the lock")
        held = False
        lock.release()
        thread.join(10)
        self.assertEqual(responses[0].status_code, 200)

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

    def test_an_unknown_or_non_string_sink_gets_400(self):
        _login(self.client, self.owner)
        for sink in ("dropbox", "mirror", ["plex"], {"plex": 1}, 7):
            with self.subTest(sink=sink):
                resp = self.client.post(f"/api/provider/playlists/{self.owned}/mirror",
                                        json={"enabled": True, "sink": sink})
                self.assertEqual(resp.status_code, 400)

    def test_each_sink_flips_its_own_column_and_calls_its_own_module(self):
        # Written out here, not read from mirror_sinks: the registry is what
        # this checks.
        expected = {
            "filesystem": (main.mirror, "mirror_enabled"),
            "subsonic": (main.mirror_subsonic, "subsonic_mirror_enabled"),
            "jellyfin": (main.mirror_jellyfin, "jellyfin_mirror_enabled"),
            "emby": (main.mirror_emby, "emby_mirror_enabled"),
            "music_assistant": (main.mirror_music_assistant, "music_assistant_mirror_enabled"),
            "plex": (main.mirror_plex, "plex_mirror_enabled"),
        }
        flags = [col for _module, col in expected.values()]
        _login(self.client, self.owner)
        for sink, (module, column) in expected.items():
            for enabled in (True, False):
                with self.subTest(sink=sink, enabled=enabled), \
                        mock.patch.object(module, "write_mirror") as write, \
                        mock.patch.object(module, "delete_mirror") as delete:
                    resp = self.client.post(f"/api/provider/playlists/{self.owned}/mirror",
                                            json={"enabled": enabled, "sink": sink})
                    self.assertEqual(resp.status_code, 200)
                    self.assertIs(resp.get_json()[column], enabled)
                    called, idle = (write, delete) if enabled else (delete, write)
                    self.assertEqual(called.call_count, 1)
                    self.assertEqual(called.call_args.args[1], self.owned)
                    idle.assert_not_called()
                    row = self.conn.execute(
                        f"SELECT {', '.join(flags)} FROM playlists WHERE id = ?", (self.owned,)).fetchone()
                    on = {col for col in flags if row[col]}
                    self.assertEqual(on, {column} if enabled else set())

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

    def test_enabling_queues_the_requests_and_answers_at_once(self):
        """The response doesn't wait on Lidarr: a run is one round trip per
        missing album. It says the run is queued; the queued job does the
        round trip, and its outcome shows on the playlist afterwards."""
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
            client.lookup_album.assert_not_called()
            self.assertTrue(data["lidarr_request_queued"])
            self.assertIsNone(data["lidarr_request_last_run_at"])
            job = self.conn.execute(
                "SELECT payload FROM jobs WHERE type = ?", (lidarr_requests.JOB_TYPE,)).fetchone()
            lidarr_requests.run_job(json.loads(job["payload"]))
        row = self.conn.execute(
            "SELECT lidarr_request_last_count, lidarr_request_last_run_at FROM playlists WHERE id = ?",
            (self.owned,)).fetchone()
        self.assertEqual(row["lidarr_request_last_count"], 1)
        self.assertIsNotNone(row["lidarr_request_last_run_at"])

    def test_disabling_does_not_call_the_client(self):
        with mock.patch("lidarr_requests.lidarr_client") as client:
            self._toggle(self.owner, self.owned, True)
            client.reset_mock()
            resp = self._toggle(self.owner, self.owned, False)
        client.lookup_album.assert_not_called()
        self.assertFalse(resp.get_json()["lidarr_request_enabled"])
        self.assertFalse(resp.get_json()["lidarr_request_queued"])


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

    def _requesting_playlist(self):
        requesting = self._make_playlist("Requesting", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET lidarr_request_enabled = 1, lidarr_request_last_count = 3, "
            "lidarr_request_last_run_at = datetime('now') WHERE id = ?", (requesting,))
        self.conn.commit()
        return requesting

    def test_a_playlist_with_only_lidarr_requests_enabled_is_included(self):
        # #494: not a mirror sink, but included here too — the WHERE
        # clause's own OR was extended, not just the SELECT list.
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        db.set_config(self.conn, "lidarr_root_folder_path", "/music")
        db.set_config(self.conn, "lidarr_quality_profile_id", "1")
        db.set_config(self.conn, "lidarr_metadata_profile_id", "2")
        self.conn.commit()
        requesting = self._requesting_playlist()

        _login(self.client, self.admin)
        data = self.client.get("/api/admin/mirrors").get_json()
        self.assertEqual(data["lidarr_url"], "http://lidarr.example.com")
        self.assertTrue(data["lidarr_request_configured"])
        row = next(p for p in data["playlists"] if p["id"] == requesting)
        self.assertTrue(row["lidarr_request_enabled"])
        self.assertEqual(row["lidarr_request_last_count"], 3)

    def test_a_stale_lidarr_flag_lists_nothing_while_lidarr_is_not_configured(self):
        # Connected, but the three profile fields unset: requests can't
        # run, so a playlist whose flag is still on has nothing to report.
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example.com")
        db.set_config(self.conn, "lidarr_api_key", "key1")
        self.conn.commit()
        self._requesting_playlist()

        _login(self.client, self.admin)
        data = self.client.get("/api/admin/mirrors").get_json()
        self.assertFalse(data["lidarr_request_configured"])
        self.assertEqual(data["playlists"], [])


class AdminMirrorTargetsTests(_RouteTestBase):
    """GET /api/admin/mirrors' `targets`: every sink listed, configured or
    not, and a state only for the configured ones: ok, warning (reachable
    but a write failed, or Lidarr without its profiles) or error
    (unreachable, or a folder that can't be written). Probes are mocked."""

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "music_root", str(Path(_TMP) / "no-such-music"))
        self.conn.commit()
        _login(self.client, self.admin)

    def _targets(self):
        data = self.client.get("/api/admin/mirrors").get_json()
        return {t["key"]: t for t in data["targets"]}

    def _configure_subsonic(self):
        for key, value in (("mirror_subsonic_url", "http://navidrome.example"),
                           ("mirror_subsonic_username", "u"), ("mirror_subsonic_password", "p")):
            db.set_config(self.conn, key, value)
        self.conn.commit()

    def _configure_lidarr(self, profiles=True):
        db.set_config(self.conn, "lidarr_url", "http://lidarr.example")
        db.set_config(self.conn, "lidarr_api_key", "k")
        if profiles:
            db.set_config(self.conn, "lidarr_root_folder_path", "/music")
            db.set_config(self.conn, "lidarr_quality_profile_id", "1")
            db.set_config(self.conn, "lidarr_metadata_profile_id", "2")
        self.conn.commit()

    def test_nothing_configured_lists_every_target_without_a_state(self):
        targets = self._targets()
        self.assertEqual(list(targets), ["filesystem", "subsonic", "jellyfin", "emby", "music_assistant", "plex", "lidarr"])
        for t in targets.values():
            self.assertFalse(t["configured"])
            self.assertIsNone(t["state"])
        self.assertEqual(targets["lidarr"]["kind"], "request")

    def test_a_writable_folder_is_ok(self):
        folder = Path(tempfile.mkdtemp(prefix="trobar-test-mirror-target-", dir=_TMP))
        db.set_config(self.conn, "mirror_folder", str(folder))
        self.conn.commit()
        t = self._targets()["filesystem"]
        self.assertEqual((t["configured"], t["state"], t["where"]), (True, "ok", str(folder)))

    def test_a_folder_that_does_not_exist_is_an_error(self):
        db.set_config(self.conn, "mirror_folder", str(Path(_TMP) / "no-such-mirror-folder"))
        self.conn.commit()
        t = self._targets()["filesystem"]
        self.assertEqual((t["state"], t["detail_code"]), ("error", "not_writable"))

    def test_an_unreachable_server_is_an_error(self):
        self._configure_subsonic()
        with mock.patch.object(main.subsonic_client, "mirror_status",
                               return_value={"state": "disconnected", "url": "http://navidrome.example"}):
            t = self._targets()["subsonic"]
        self.assertEqual((t["state"], t["detail_code"], t["where"]), ("error", "unreachable", "http://navidrome.example"))

    def test_a_reachable_server_with_a_failed_write_is_a_warning_naming_it(self):
        self._configure_subsonic()
        playlist = self._make_playlist("Road Trip", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET subsonic_mirror_enabled = 1, subsonic_mirror_last_error_code = 'write_failed', "
            "subsonic_mirror_last_error = 'boom' WHERE id = ?", (playlist,))
        self.conn.commit()
        with mock.patch.object(main.subsonic_client, "mirror_status",
                               return_value={"state": "paired", "url": "http://navidrome.example"}):
            t = self._targets()["subsonic"]
        self.assertEqual((t["state"], t["detail_code"], t["count"], t["detail"]), ("warning", "write_failed", 1, "boom"))

    def test_lidarr_connected_without_its_profiles_is_a_warning(self):
        self._configure_lidarr(profiles=False)
        with mock.patch.object(main.lidarr_client, "status", return_value={"state": "paired", "url": "x"}):
            t = self._targets()["lidarr"]
        self.assertEqual((t["state"], t["detail_code"]), ("warning", "incomplete"))

    def test_lidarr_set_up_and_reachable_is_ok(self):
        self._configure_lidarr()
        with mock.patch.object(main.lidarr_client, "status", return_value={"state": "paired", "url": "x"}):
            t = self._targets()["lidarr"]
        self.assertEqual(t["state"], "ok")

    def test_lidarr_unreachable_is_an_error(self):
        self._configure_lidarr()
        with mock.patch.object(main.lidarr_client, "status", return_value={"state": "disconnected", "url": "x"}):
            t = self._targets()["lidarr"]
        self.assertEqual((t["state"], t["detail_code"]), ("error", "unreachable"))


class RateLimitForgetsAddressesTests(unittest.TestCase):
    """The limiter holds an address no longer than its window: checking
    creates nothing, and a bucket whose failures have all aged out goes,
    whether or not that address is ever checked again."""

    def setUp(self):
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)
        for name, value in (("_rl_max_window_s", 0), ("_rl_last_sweep", 0.0)):
            # create=True: these tests also run against a limiter without
            # the sweep, and fail there on behaviour, not on setUp. The
            # limiter and the state it rebinds live in web_common (#253).
            state = mock.patch.object(web_common, name, value, create=True)
            state.start()
            self.addCleanup(state.stop)
        self.now = 1_000_000.0
        patcher = mock.patch.object(main.time, "time", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_checking_an_address_that_failed_nothing_keeps_nothing(self):
        self.assertFalse(main._rate_limited("device:192.0.2.1", 30, 300))
        self.assertNotIn("device:192.0.2.1", main._rl_failures)

    def test_a_checked_bucket_goes_once_its_failures_age_out(self):
        main._rate_limited("login:192.0.2.2", 10, 300)
        main._record_failure("login:192.0.2.2")
        self.now += 301
        web_common._rl_last_sweep = self.now  # the bucket's own check, not the sweep
        self.assertFalse(main._rate_limited("login:192.0.2.2", 10, 300))
        self.assertNotIn("login:192.0.2.2", main._rl_failures)

    def test_a_bucket_never_checked_again_is_swept(self):
        main._rate_limited("login:192.0.2.3", 10, 300)
        main._record_failure("login:192.0.2.3")
        self.now += 301
        main._rate_limited("login:192.0.2.4", 10, 300)
        self.assertNotIn("login:192.0.2.3", main._rl_failures)

    def test_failures_inside_the_window_still_count(self):
        for _ in range(10):
            main._rate_limited("login:192.0.2.5", 10, 300)
            main._record_failure("login:192.0.2.5")
        self.now += 299
        main._rate_limited("login:192.0.2.6", 10, 300)
        self.assertTrue(main._rate_limited("login:192.0.2.5", 10, 300))

    def test_the_sweep_keeps_a_bucket_whose_newest_failure_is_live(self):
        # One failure aged out, one still inside the window; the sweep runs on
        # another address's check. Judging the bucket by its oldest failure
        # would drop it and reset a live count.
        main._rate_limited("login:192.0.2.7", 10, 300)
        main._record_failure("login:192.0.2.7")
        self.now += 290
        main._record_failure("login:192.0.2.7")
        self.now += 11
        main._rate_limited("login:192.0.2.8", 10, 300)
        self.assertEqual(len(main._rl_failures.get("login:192.0.2.7", ())), 2)
        main._rate_limited("login:192.0.2.7", 10, 300)
        self.assertEqual(len(main._rl_failures["login:192.0.2.7"]), 1)


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
            self._seed(admin._EXPOSURE_WARN_THRESHOLD)
            self.assertIsNone(admin._exposure_warning())

    def test_warns_above_threshold(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self._seed(admin._EXPOSURE_WARN_THRESHOLD + 1)
            self.assertEqual(admin._exposure_warning(), admin._EXPOSURE_WARN_THRESHOLD + 1)

    def test_none_when_trusted_proxy_is_pinned_even_with_many_peers(self):
        # The pin itself is the acknowledgement — a directly-exposed port
        # can't exploit "*" trust that was never granted in the first place.
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": "172.20.0.5"}):
            self._seed(admin._EXPOSURE_WARN_THRESHOLD + 10)
            self.assertIsNone(admin._exposure_warning())

    def test_none_in_forward_mode_even_with_many_peers(self):
        # forward mode's real boundary is the ForwardAuth gate; a reachable
        # port is a separate question this signal isn't measuring.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self._seed(admin._EXPOSURE_WARN_THRESHOLD + 10)
            with mock.patch.object(settings, "AUTH_MODE", "forward"):
                self.assertIsNone(admin._exposure_warning())

    def test_stale_peers_outside_the_window_are_not_counted(self):
        # A long-running instance's one real proxy can still get a new
        # address over time (container recreate) without ever being
        # reachable by anyone else — only counting recent peers is what
        # keeps that from slowly crossing the threshold on its own.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            long_ago = time.time() - main._EXPOSURE_WINDOW_S - 3600
            self._seed(admin._EXPOSURE_WARN_THRESHOLD + 5, seen=long_ago)
            self.assertIsNone(admin._exposure_warning())

    def test_status_shows_the_count_below_threshold(self):
        # Review feedback on #389: unlike _exposure_warning, this always
        # reports the live count while the mechanism is active — a signal
        # that only ever speaks up past its threshold is indistinguishable
        # from one that's silently never seeing anything.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            self._seed(2)
            self.assertEqual(admin._exposure_status(), 2)

    def test_status_is_none_not_zero_when_trusted_proxy_is_pinned(self):
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": "172.20.0.5"}):
            self.assertIsNone(admin._exposure_status())

    def test_status_is_none_not_zero_in_forward_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            with mock.patch.object(settings, "AUTH_MODE", "forward"):
                self.assertIsNone(admin._exposure_status())


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
        self.assertEqual(web_common._exposure_peer_count(), 1)

    def test_different_peers_each_count(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            for i in range(3):
                self.client.get("/login", environ_overrides={"REMOTE_ADDR": f"198.51.100.{i}"})
        self.assertEqual(web_common._exposure_peer_count(), 3)

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
        self.assertEqual(web_common._exposure_peer_count(), 1)

    def test_nothing_recorded_when_trusted_proxy_is_pinned(self):
        with mock.patch.dict(os.environ, {"TROBAR_TRUSTED_PROXY": "172.20.0.5"}):
            self.client.get("/login", environ_overrides={"REMOTE_ADDR": "198.51.100.42"})
        self.assertEqual(web_common._exposure_peer_count(), 0)

    def test_nothing_recorded_in_forward_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            with mock.patch.object(settings, "AUTH_MODE", "forward"):
                self.client.get("/login", environ_overrides={"REMOTE_ADDR": "198.51.100.42"})
        self.assertEqual(web_common._exposure_peer_count(), 0)


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
        for i in range(admin._EXPOSURE_WARN_THRESHOLD + 1):
            main._exposure_peers[f"203.0.113.{i}"] = time.time()
        _login(self.client, self.admin)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TROBAR_TRUSTED_PROXY", None)
            resp = self.client.get("/api/admin/health")
        # +1 for the admin's own request the assertion above didn't seed.
        self.assertEqual(
            resp.get_json()["exposure_warning"], admin._EXPOSURE_WARN_THRESHOLD + 2)

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

    def _last_scan_from_folder(self, result):
        self.conn.execute(
            "INSERT INTO jobs (type, state, finished_at, result) VALUES (?, 'done', '2026-06-15 12:00:00', ?)",
            (scanner.JOB_TYPE, result))
        self.conn.commit()
        _login(self.client, self.admin)
        return self.client.get("/api/admin/health").get_json()["last_scan_from_folder"]

    def test_last_scan_from_folder_is_the_last_scans_count(self):
        self.assertEqual(self._last_scan_from_folder('{"added": 3, "from_folder": 2}'), 2)

    def test_last_scan_from_folder_is_none_for_a_result_without_it(self):
        # A scan from before the count, or one whose result isn't a dict.
        for result in (None, '{"added": 3}', '"text"', "not json"):
            with self.subTest(result=result):
                self.conn.execute("DELETE FROM jobs")
                self.assertIsNone(self._last_scan_from_folder(result))


class AdminConfigMirrorShareLocationTests(_RouteTestBase):
    """Where the mirror folder sits inside the music share. Two mounts of
    one share are simulated by a link under MUSIC_ROOT to the mirror
    folder, which is elsewhere, as /music and /mirrors are in compose."""

    def setUp(self):
        super().setUp()
        self._music_root = Path(tempfile.mkdtemp(prefix="trobar-test-music-root-", dir=_TMP))
        self._mirrors = Path(tempfile.mkdtemp(prefix="trobar-test-mirrors-", dir=_TMP))
        (self._music_root / "Lists").mkdir()
        (self._music_root / "Lists" / "Trobar").symlink_to(self._mirrors)
        (self._music_root / "Elsewhere").mkdir()
        db.set_config(self.conn, "music_root", str(self._music_root))
        db.set_config(self.conn, "mirror_folder", str(self._mirrors))
        self.conn.commit()
        _login(self.client, self.admin)

    def _put(self, **body):
        return self.client.put("/api/admin/config", json=body)

    def _refused(self, resp):
        self.assertEqual((resp.status_code, resp.get_json()["field"]), (400, "mirror_share_location"))
        self.assertIsNone(db.get_mirror_share_location())

    def test_the_mirror_folder_seen_from_the_library_is_accepted(self):
        self.assertEqual(self._put(mirror_share_location=" /Lists/Trobar/ ").status_code, 200)
        self.assertEqual(db.get_mirror_share_location(), "Lists/Trobar")
        self.assertEqual(self.client.get("/api/admin/config").get_json()["mirror_share_location"], "Lists/Trobar")
        self.assertEqual(list(self._mirrors.iterdir()), [], "the probe file is removed")

    def test_another_folder_of_the_library_is_refused(self):
        resp = self._put(mirror_share_location="Elsewhere")
        self._refused(resp)
        self.assertIn("test file", resp.get_json()["error"])
        self._refused(self._put(mirror_share_location="Missing"))

    def test_dotdot_and_backslashes_are_refused(self):
        # Each of these does reach the mirror folder, so only the rule refuses them.
        (self._music_root / "Lists\\Trobar").symlink_to(self._mirrors)
        for location in ("Lists/../Lists/Trobar", "Lists\\Trobar"):
            with self.subTest(location=location):
                self._refused(self._put(mirror_share_location=location))

    def test_no_mirror_folder_is_refused(self):
        db.set_config(self.conn, "mirror_folder", None)
        self.conn.commit()
        with mock.patch.dict(os.environ, {"MIRROR_ROOT": ""}):
            resp = self._put(mirror_share_location="Lists/Trobar")
        self._refused(resp)
        self.assertIn("mirror output folder first", resp.get_json()["error"])

    def test_empty_clears_it(self):
        self._put(mirror_share_location="Lists/Trobar")
        self.assertEqual(self._put(mirror_share_location="").status_code, 200)
        self.assertIsNone(db.get_mirror_share_location())

    def test_moving_the_mirror_folder_rechecks_a_stored_location(self):
        self._put(mirror_share_location="Lists/Trobar")
        other = Path(tempfile.mkdtemp(prefix="trobar-test-other-mirrors-", dir=_TMP))
        resp = self._put(mirror_folder=str(other))
        self.assertEqual((resp.status_code, resp.get_json()["field"]), (400, "mirror_share_location"))
        self.assertEqual(db.get_config(self.conn, "mirror_folder"), str(self._mirrors))

    def test_an_unrelated_save_does_not_probe(self):
        self._put(mirror_share_location="Lists/Trobar")
        with mock.patch.object(main.mirror, "seen_in_library") as probe:
            self.assertEqual(self._put(extra_playlist_folder="").status_code, 200)
        probe.assert_not_called()

class AdminConfigMirrorFolderValidationTests(_RouteTestBase):
    """#285: PUT /api/admin/config rejects a mirror_folder equal to or
    nested inside MUSIC_ROOT: Trobar never writes into the library's own
    path. (A mirror folder that is also visible under MUSIC_ROOT through a
    second mount is fine: filesystem_client.py skips Trobar's marked files.)"""

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


class AdminConfigExtraPlaylistFolderValidationTests(_RouteTestBase):
    """PUT /api/admin/config rejects an extra playlist folder that overlaps
    either MUSIC_ROOT or the mirror output folder.

    Both overlaps import the same playlist twice: the second root is walked
    for .m3u exactly the way MUSIC_ROOT is, so a file under both roots is
    listed under both ids, and a mirror written inside it comes straight
    back as a source playlist — the self-import loop the mirror_folder rule
    above already exists to prevent, reachable again through a second root.

    Overlap is rejected in EITHER direction here, unlike the mirror rule,
    because this root is READ: a folder containing MUSIC_ROOT would walk
    the music library too, where a mirror folder containing MUSIC_ROOT
    would still write its files outside it."""

    def setUp(self):
        super().setUp()
        self._music_root = Path(tempfile.mkdtemp(prefix="trobar-test-music-root-", dir=_TMP))
        db.set_config(self.conn, "music_root", str(self._music_root))
        self.conn.commit()
        _login(self.client, self.admin)

    def _put(self, **fields):
        return self.client.put("/api/admin/config", json=fields)

    def test_accepts_a_separate_folder(self):
        separate = Path(_TMP) / "a-separate-playlist-dir"
        resp = self._put(extra_playlist_folder=str(separate))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "extra_playlist_folder"), str(separate))

    def test_clearing_the_field_unsets_it(self):
        db.set_config(self.conn, "extra_playlist_folder", str(Path(_TMP) / "x"))
        self.conn.commit()
        self.assertEqual(self._put(extra_playlist_folder="").status_code, 200)
        self.assertIsNone(db.get_config(self.conn, "extra_playlist_folder"))

    def test_rejects_a_folder_equal_to_music_root(self):
        self.assertEqual(self._put(extra_playlist_folder=str(self._music_root)).status_code, 400)

    def test_rejects_a_folder_inside_music_root(self):
        self.assertEqual(
            self._put(extra_playlist_folder=str(self._music_root / "playlists")).status_code, 400)

    def test_rejects_a_folder_containing_music_root(self):
        self.assertEqual(
            self._put(extra_playlist_folder=str(self._music_root.parent)).status_code, 400)

    def test_rejects_a_dotdot_path_that_resolves_into_music_root(self):
        evasive = str(self._music_root / "sub" / "..")
        self.assertEqual(self._put(extra_playlist_folder=evasive).status_code, 400)

    def test_dotdot_in_an_accepted_folder_is_normalized_before_storing(self):
        separate = Path(_TMP) / "a-separate-playlist-dir"
        resp = self._put(extra_playlist_folder=str(separate / "a" / ".." / "b"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "extra_playlist_folder"), str(separate / "b"))

    def test_rejects_a_folder_overlapping_the_stored_mirror_folder(self):
        mirror = Path(_TMP) / "a-mirror-dir"
        db.set_config(self.conn, "mirror_folder", str(mirror))
        self.conn.commit()
        self.assertEqual(self._put(extra_playlist_folder=str(mirror / "sub")).status_code, 400)

    def test_rejects_a_mirror_folder_overlapping_the_stored_extra_folder(self):
        """The symmetric twin: the pair is refused whichever field the
        admin is editing."""
        extra = Path(_TMP) / "a-separate-playlist-dir"
        db.set_config(self.conn, "extra_playlist_folder", str(extra))
        self.conn.commit()
        self.assertEqual(self._put(mirror_folder=str(extra / "sub")).status_code, 400)

    def test_one_put_setting_both_to_an_overlapping_pair_is_rejected(self):
        """The admin form PUTs the whole config object, so both fields
        arrive together — the check has to read the value this request is
        about to store, not the one already stored."""
        base = Path(_TMP) / "both-at-once"
        resp = self._put(extra_playlist_folder=str(base), mirror_folder=str(base / "out"))
        self.assertEqual(resp.status_code, 400)

    def test_one_put_moving_the_mirror_out_of_the_way_is_accepted(self):
        """The other half of that: a save that FIXES an overlap by moving
        the other folder must not be refused against the stale stored
        value."""
        extra = Path(_TMP) / "moving-extra"
        db.set_config(self.conn, "mirror_folder", str(extra / "out"))
        self.conn.commit()
        resp = self._put(extra_playlist_folder=str(extra),
                         mirror_folder=str(Path(_TMP) / "moved-mirror"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "extra_playlist_folder"), str(extra))

    def test_a_refused_save_writes_neither_folder(self):
        """The review defect on this branch, as a route-level test.

        Both fields commit together now. Before that they committed one at
        a time, so a PUT whose mirror was refused had already written the
        extra folder -- and that write had only ever been checked against
        the mirror value in the BODY, the one that just got rejected. What
        stayed in the store was the mirror as it already was, next to an
        extra folder nothing had ever compared it to: an overlapping pair,
        arrived at THROUGH the checks rather than around them, while the
        admin saw a 400 and believed nothing had been saved."""
        mirror = Path(_TMP) / "probe-mirror-dir"
        db.set_config(self.conn, "mirror_folder", str(mirror))
        self.conn.commit()

        resp = self._put(extra_playlist_folder=str(mirror / "sub"),
                         mirror_folder=str(self._music_root / "inside"))

        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(db.get_config(self.conn, "extra_playlist_folder"))
        self.assertEqual(db.get_config(self.conn, "mirror_folder"), str(mirror))

    def test_a_refused_save_does_not_write_the_field_that_was_fine(self):
        """The general form of the above: a rejection leaves this endpoint
        as it found it, including the half that would have been accepted on
        its own."""
        separate = Path(_TMP) / "a-separate-playlist-dir"
        resp = self._put(extra_playlist_folder=str(separate),
                         mirror_folder=str(self._music_root / "inside"))
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(db.get_config(self.conn, "extra_playlist_folder"))

    def test_clearing_a_folder_is_validated_against_the_environment_default(self):
        """Clearing a field clears the OVERRIDE, and the environment
        default underneath becomes effective again -- so an empty value is
        not "unset" for validation purposes. Reachable with the shipped
        compose file, which sets MIRROR_ROOT."""
        env_mirror = Path(_TMP) / "env-mirror-parent" / "out"
        with mock.patch.dict(os.environ, {"MIRROR_ROOT": str(env_mirror)}):
            resp = self._put(extra_playlist_folder=str(Path(_TMP) / "env-mirror-parent"),
                             mirror_folder="")
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(db.get_config(self.conn, "extra_playlist_folder"))

    def test_clearing_a_folder_is_accepted_with_no_environment_default(self):
        """The control for the test above -- otherwise it would pass just
        as well against a rule that refused every cleared field."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MIRROR_ROOT", None)
            resp = self._put(extra_playlist_folder=str(Path(_TMP) / "env-mirror-parent"),
                             mirror_folder="")
        self.assertEqual(resp.status_code, 200)

    def test_an_unrelated_save_is_not_refused_by_a_pair_it_is_not_touching(self):
        """The admin form PUTs every field at once, so a rule that fired on
        stored state alone would 400 saves of settings nobody was editing.
        With neither folder field in the body this does nothing at all."""
        overlapping = Path(_TMP) / "already-overlapping"
        db.set_config(self.conn, "extra_playlist_folder", str(overlapping))
        db.set_config(self.conn, "mirror_folder", str(overlapping / "out"))
        self.conn.commit()
        self.assertEqual(self._put(itunes_library_path="/music/Library.xml").status_code, 200)

    def test_the_effective_value_is_echoed_back(self):
        separate = Path(_TMP) / "a-separate-playlist-dir"
        self._put(extra_playlist_folder=str(separate))
        data = self.client.get("/api/admin/config").get_json()
        self.assertEqual(data["extra_playlist_folder"], str(separate))


class DevicePlaysRouteTests(_RouteTestBase):
    """POST /api/device/plays and the person's own switch for keeping them."""

    def setUp(self):
        super().setUp()
        self.device_id, self.token = sync_state.create_device(self.conn, self.owner, "watch")
        self.track = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('a/1.flac', 'Air', 'x', 'y', 1, 0)").lastrowid
        self.conn.commit()
        self.when = int(time.time()) - 60
        _login(self.client, self.owner)

    def _report(self, body=None, token=None):
        body = {"plays": [{"track_id": self.track, "played_at": self.when}]} if body is None else body
        return self.client.post("/api/device/plays", json=body,
                                headers={"Authorization": f"Bearer {token or self.token}"})

    def _enable(self, on=True):
        return self.client.put("/api/profile/device-plays", json={"enabled": on})

    def test_stored_plays_are_used_only_while_the_switch_is_on(self):
        url = "/api/suggestions/most-played?with_sources=1"
        self.assertEqual(self.client.get(url).get_json()["sources"]["device_plays"], "unconfigured")
        self._enable()
        self._report()
        body = self.client.get(url).get_json()
        self.assertEqual(body["sources"]["device_plays"], "ok")
        self.assertEqual([(i["artist"], i["album"], i["playcount"]) for i in body["items"]], [("Air", "x", 1)])
        # One copy per album, and "recently added" comes first: out of the way
        # here, as each source is what is being looked at.
        with mock.patch("web_common.suggestions.recently_added", return_value=[]):
            self.assertEqual([i["source"] for i in self.client.get("/api/suggestions").get_json()], ["device_plays"])
        self._enable(False)
        body = self.client.get(url).get_json()
        self.assertEqual((body["items"], body["sources"]["device_plays"]), ([], "unconfigured"))
        with mock.patch("web_common.suggestions.recently_added", return_value=[]):
            self.assertEqual(self.client.get("/api/suggestions").get_json(), [])
        profile = self.client.get("/api/profile").get_json()
        self.assertEqual(profile["device_plays_count"], 1, "off keeps what is stored")
        self._enable()
        self.assertEqual(len(self.client.get(url).get_json()["items"]), 1)

    def test_a_play_outside_the_period_is_still_a_recent_one(self):
        self._enable()
        old = self.when - 40 * 86400
        self.assertEqual(self._report({"plays": [{"track_id": self.track, "played_at": old}]}).get_json()["stored"], 1)
        with mock.patch("web_common.suggestions.recently_added", return_value=[]):
            got = self.client.get("/api/suggestions?period=7day").get_json()
        self.assertEqual([i["source"] for i in got], ["device_plays-recent"])

    def test_an_album_on_every_device_is_not_suggested(self):
        self._enable()
        self._report()
        sync_state.create_selection(self.conn, "album", "Air||x", self.owner, [self.device_id])
        self.conn.commit()
        with mock.patch("web_common.suggestions.recently_added", return_value=[]):
            self.assertEqual(self.client.get("/api/suggestions").get_json(), [])

    def test_plays_count_as_configured_on_the_app_api(self):
        with db.connection() as conn:
            self.assertFalse(app_api._scrobble_configured(conn, self.owner))
        self._enable()
        with db.connection() as conn:
            self.assertTrue(app_api._scrobble_configured(conn, self.owner))
        self._report()
        self._enable(False)
        with db.connection() as conn:
            self.assertFalse(app_api._scrobble_configured(conn, self.owner), "off: stored plays aren't a source")

    def test_a_device_needs_its_token(self):
        self.assertEqual(self._report(token="nope").status_code, 401)
        self.assertEqual(self.client.post("/api/device/plays", json={"plays": []}).status_code, 401)

    def test_nothing_is_stored_until_its_owner_turns_it_on(self):
        self.assertFalse(self.client.get("/api/profile").get_json()["device_plays_enabled"])
        self.assertEqual(self._report().get_json()["enabled"], False)
        self.assertEqual(self.client.get("/api/profile").get_json()["device_plays_count"], 0)

    def test_on_stores_a_play_for_the_devices_owner(self):
        self.assertTrue(self._enable().get_json()["device_plays_enabled"])
        self.assertEqual(self._report().get_json()["stored"], 1)
        self.assertEqual(self.client.get("/api/profile").get_json()["device_plays_count"], 1)
        _login(self.client, self.admin)
        self.assertEqual(self.client.get("/api/profile").get_json()["device_plays_count"], 0)

    def test_the_switch_is_the_callers_own(self):
        _login(self.client, self.admin)
        self._enable()
        self.assertEqual(self._report().get_json()["enabled"], False)

    def test_a_malformed_body_is_a_400(self):
        self._enable()
        bodies: list[object] = [[], {}, {"plays": "x"}, {"plays": [{"track_id": 1}]},
                                {"plays": [{"track_id": 10 ** 30, "played_at": self.when}]}]
        for body in bodies:
            self.assertEqual(self._report(body).status_code, 400, body)
        self.assertEqual(self.client.put("/api/profile/device-plays", json={"enabled": 1}).status_code, 400)
        self.assertEqual(self.client.put("/api/profile/device-plays", json=[]).status_code, 400)

    def test_delete_clears_the_plays_and_keeps_the_switch(self):
        self._enable()
        self._report()
        profile = self.client.delete("/api/profile/device-plays").get_json()
        self.assertEqual((profile["device_plays_count"], profile["device_plays_enabled"]), (0, True))

    def test_removing_a_device_keeps_its_plays(self):
        self._enable()
        self._report()
        self.assertEqual(self.conn.execute("SELECT device_id FROM device_plays").fetchone()[0], self.device_id)
        self.assertLess(self.client.delete(f"/api/devices/{self.device_id}").status_code, 300)
        self.assertIsNone(self.conn.execute("SELECT device_id FROM device_plays").fetchone()[0])


class SendCredentialRouteTests(_RouteTestBase):
    """PUT /api/profile/send-credentials: stored encrypted, never returned."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.key_file = Path(tmp.name) / "key"
        env = mock.patch.dict(os.environ, {"TROBAR_CREDENTIAL_KEY_FILE": str(self.key_file)})
        env.start()
        self.addCleanup(env.stop)
        _login(self.client, self.owner)

    def _put(self, **body):
        return self.client.put("/api/profile/send-credentials", json=body)

    def _column(self, column="maloja_api_key_enc", user=None):
        return self.conn.execute(f"SELECT {column} FROM users WHERE id = ?", (user or self.owner,)).fetchone()[0]

    def test_needs_a_session(self):
        self.assertEqual(main.app.test_client().put("/api/profile/send-credentials", json={}).status_code, 401)

    def test_stored_encrypted_and_never_returned(self):
        resp = self._put(maloja_api_key="hunter2-value")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("hunter2-value", resp.get_data(as_text=True))
        self.assertNotIn("hunter2-value", self._column())
        self.assertNotIn("maloja_api_key_enc", resp.get_json())
        self.assertEqual(resp.get_json()["maloja_api_key_state"], "set")
        self.assertEqual(resp.get_json()["listenbrainz_token_state"], "none")
        self.assertNotIn("hunter2-value", self.client.get("/api/profile").get_data(as_text=True))

    def test_a_field_left_out_stays_and_an_empty_one_goes(self):
        self._put(maloja_api_key="a", listenbrainz_token="b")
        before = self._column("listenbrainz_token_enc")
        profile = self._put(maloja_api_key="").get_json()
        self.assertEqual((profile["maloja_api_key_state"], profile["listenbrainz_token_state"]), ("none", "set"))
        self.assertIsNone(self._column())
        self.assertEqual(self._column("listenbrainz_token_enc"), before)

    def test_a_pasted_value_is_trimmed_and_blanks_count_as_empty(self):
        self._put(maloja_api_key="  a\n")
        self.assertEqual(credential_store.decrypt(self._column(), self.owner, "maloja_api_key"), "a")
        self._put(maloja_api_key="   ")
        self.assertIsNone(self._column())

    def test_it_is_the_callers_own(self):
        self._put(maloja_api_key="a")
        self.assertIsNone(self._column(user=self.other))

    def test_a_whole_profile_put_leaves_them_alone(self):
        self._put(maloja_api_key="a")
        before = self._column()
        profile = self.client.get("/api/profile").get_json()
        self.assertEqual(self.client.put("/api/profile", json=profile).status_code, 200)
        self.assertEqual(self._column(), before)

    def test_a_bad_value_changes_nothing_at_all(self):
        self._put(maloja_api_key="a")
        before = self._column()
        self.assertEqual(self._put(maloja_api_key="", listenbrainz_token=123).status_code, 400)
        self.assertEqual(self._column(), before)

    def test_a_lost_key_says_enter_it_again_and_breaks_nothing(self):
        self._put(maloja_api_key="a")
        self.key_file.unlink()
        resp = self.client.get("/api/profile")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["maloja_api_key_state"], "unreadable")
        self.assertEqual(self._put(maloja_api_key="again").get_json()["maloja_api_key_state"], "set")

    def test_a_bad_body_is_a_400(self):
        self.assertEqual(self.client.put("/api/profile/send-credentials", json=[]).status_code, 400)
        self.assertEqual(self._put(maloja_api_key=1).status_code, 400)
        self.assertEqual(self._put(maloja_api_key="x" * 501).status_code, 400)

    def test_a_damaged_key_is_a_503_and_stores_nothing(self):
        self.key_file.write_bytes(b"short")
        self.assertEqual(self._put(maloja_api_key="a").status_code, 503)
        self.assertIsNone(self._column())


class PlaySendRouteTests(_RouteTestBase):
    """PUT /api/profile/play-sends, and plays queueing a send."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"TROBAR_CREDENTIAL_KEY_FILE": str(Path(tmp.name) / "key")})
        env.start()
        self.addCleanup(env.stop)
        _login(self.client, self.owner)
        self.conn.execute("UPDATE users SET maloja_url = 'http://8.8.8.8' WHERE id = ?", (self.owner,))
        self.conn.commit()

    def _put(self, **body):
        return self.client.put("/api/profile/play-sends", json=body)

    def test_needs_a_session(self):
        self.assertEqual(main.app.test_client().put("/api/profile/play-sends", json={}).status_code, 401)

    def test_on_needs_its_credential(self):
        self.assertEqual(self._put(target="maloja", enabled=True).status_code, 409)
        self.client.put("/api/profile/send-credentials", json={"maloja_api_key": "k"})
        profile = self._put(target="maloja", enabled=True).get_json()
        self.assertEqual(profile["play_sends"]["maloja"]["on"], True)

    def test_a_bad_body_is_a_400(self):
        for body in ({"target": "spotify", "enabled": True}, {"target": "maloja", "enabled": 1}, {}):
            self.assertEqual(self.client.put("/api/profile/play-sends", json=body).status_code, 400, body)
        self.assertEqual(self.client.put("/api/profile/play-sends", json=[]).status_code, 400)

    def test_removing_the_credential_turns_the_switch_off(self):
        self.client.put("/api/profile/send-credentials", json={"maloja_api_key": "k"})
        self._put(target="maloja", enabled=True)
        profile = self.client.put("/api/profile/send-credentials", json={"maloja_api_key": ""}).get_json()
        self.assertEqual(profile["play_sends"]["maloja"], {"on": False, "ready": False, "error": None, "rejected": 0})

    def test_stored_plays_queue_a_send_only_with_a_target_on(self):
        device_id, token = sync_state.create_device(self.conn, self.owner, "watch")
        track = self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                                  "VALUES ('a/1.flac', 'Air', 'x', 'y', 1, 0)").lastrowid
        self.conn.commit()
        self.client.put("/api/profile/device-plays", json={"enabled": True})
        self.client.put("/api/profile/send-credentials", json={"maloja_api_key": "k"})
        self._put(target="maloja", enabled=True)
        when = int(time.time()) - 60
        with mock.patch("device_api.play_forward.enqueue_for") as enqueue:
            for _ in range(2):  # the second is a retry: nothing new, nothing queued
                self.client.post("/api/device/plays", json={"plays": [{"track_id": track, "played_at": when}]},
                                 headers={"Authorization": f"Bearer {token}"})
        enqueue.assert_called_once_with(mock.ANY, self.owner)


class LastfmLinkRouteTests(_RouteTestBase):
    """The Last.fm link, and the admin's API account."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"TROBAR_CREDENTIAL_KEY_FILE": str(Path(tmp.name) / "key")})
        env.start()
        self.addCleanup(env.stop)
        _login(self.client, self.owner)

    def _app(self):
        db.set_config(self.conn, "lastfm_app_api_key", "app-key")
        db.set_config(self.conn, play_forward.APP_SECRET_CONFIG,
                      credential_store.encrypt("app-secret", 0, play_forward.APP_SECRET_CONFIG))
        self.conn.commit()

    def _start(self):
        resp = self.client.get("/profile/lastfm/connect")
        self.assertEqual(resp.status_code, 302)
        with self.client.session_transaction() as sess:
            return resp, sess["lastfm_link_state"]

    def test_connect_needs_the_admins_account(self):
        self.assertEqual(self.client.get("/profile/lastfm/connect").status_code, 400)

    def test_connect_sends_the_person_to_lastfm_with_a_state(self):
        self._app()
        resp, state = self._start()
        self.assertIn("https://www.last.fm/api/auth/?api_key=app-key", resp.location)
        self.assertIn(state, resp.location)

    def test_callback_stores_the_session_encrypted(self):
        self._app()
        _, state = self._start()
        with mock.patch("auth.lastfm.get_session", return_value={"name": "air", "key": "the-session"}) as get:
            resp = self.client.get(f"/profile/lastfm/callback?state={state}&token=tok")
        self.assertEqual(get.call_args.args[:3], ("app-key", "app-secret", "tok"))
        self.assertNotIn("lastfm_error", resp.location)
        row = self.conn.execute("SELECT lastfm_session_enc, lastfm_session_name FROM users WHERE id = ?",
                                (self.owner,)).fetchone()
        self.assertEqual(row["lastfm_session_name"], "air")
        self.assertNotIn("the-session", row["lastfm_session_enc"])
        profile = self.client.get("/api/profile").get_json()
        self.assertEqual((profile["lastfm_session_state"], profile["play_sends"]["lastfm"]["ready"]), ("set", True))
        self.assertNotIn("lastfm_session_enc", profile)

    def test_a_forged_or_replayed_callback_stores_nothing(self):
        self._app()
        _, state = self._start()
        with mock.patch("auth.lastfm.get_session") as get:
            resp = self.client.get("/profile/lastfm/callback?state=wrong&token=tok")
            again = self.client.get(f"/profile/lastfm/callback?state={state}&token=tok")
        get.assert_not_called()
        self.assertIn("lastfm_error=1", resp.location)
        self.assertIn("lastfm_error=1", again.location)

    def test_a_callback_needs_a_session(self):
        self._app()
        with mock.patch("auth.lastfm.get_session") as get:
            resp = main.app.test_client().get("/profile/lastfm/callback?state=x&token=t")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.location)
        get.assert_not_called()

    def test_a_refused_token_stores_nothing(self):
        self._app()
        _, state = self._start()
        with mock.patch("auth.lastfm.get_session", return_value=None):
            resp = self.client.get(f"/profile/lastfm/callback?state={state}&token=tok")
        self.assertIn("lastfm_error=1", resp.location)
        self.assertIsNone(self.conn.execute("SELECT lastfm_session_enc FROM users WHERE id = ?",
                                            (self.owner,)).fetchone()[0])

    def test_disconnect_forgets_the_session_and_turns_the_switch_off(self):
        self._app()
        self.conn.execute("UPDATE users SET lastfm_session_enc = ?, lastfm_session_name = 'air' WHERE id = ?",
                          (credential_store.encrypt("s", self.owner, "lastfm_session"), self.owner))
        self.conn.commit()
        self.assertEqual(self.client.put("/api/profile/play-sends", json={"target": "lastfm", "enabled": True}).status_code, 200)
        profile = self.client.delete("/api/profile/lastfm-session").get_json()
        self.assertEqual((profile["lastfm_session_state"], profile["lastfm_session_name"]), ("none", None))
        self.assertFalse(profile["play_sends"]["lastfm"]["on"])

    def test_the_admins_secret_is_write_only_and_encrypted(self):
        _login(self.client, self.admin)
        self.assertEqual(self.client.put("/api/admin/config", json={
            "lastfm_app_api_key": "k", "lastfm_app_secret": "the-secret"}).status_code, 200)
        stored = db.get_config(self.conn, play_forward.APP_SECRET_CONFIG)
        self.assertNotIn("the-secret", stored or "")
        self.assertTrue(stored)
        config = self.client.get("/api/admin/config")
        self.assertNotIn("the-secret", config.get_data(as_text=True))
        self.assertTrue(config.get_json()["lastfm_app_secret_set"])
        self.assertNotIn(stored, config.get_data(as_text=True))
        self.client.put("/api/admin/config", json={"lastfm_app_secret": ""})
        self.assertTrue(self.client.get("/api/admin/config").get_json()["lastfm_app_secret_set"])
        self.assertEqual(play_forward.lastfm_app(self.conn), {"key": "k", "secret": "the-secret"})
        self.client.put("/api/admin/config", json={"lastfm_app_secret": None})
        self.assertFalse(self.client.get("/api/admin/config").get_json()["lastfm_app_secret_set"])

    def test_only_an_admin_sets_it(self):
        self.assertEqual(self.client.put("/api/admin/config", json={"lastfm_app_secret": "x"}).status_code, 403)


class DeviceStorageReserveTests(_RouteTestBase):
    """POST /api/device/storage's optional `reserve_bytes`: the space the
    device keeps free on its volume, which its own space check won't let a
    sync eat into. Absent is not zero, as for `foreign_bytes`."""

    def setUp(self):
        super().setUp()
        self.device_id, self.token = sync_state.create_device(self.conn, self.owner, "phone")
        self.conn.commit()

    def _report(self, **body):
        return self.client.post("/api/device/storage", json=body,
                                headers={"Authorization": f"Bearer {self.token}"})

    def test_a_reserve_is_stored_beside_the_report(self):
        self._report(free_bytes=1, total_bytes=2, reserve_bytes=1_000_000_000)
        self.assertEqual(self._reserve(), 1_000_000_000)

    def test_a_report_without_a_reserve_leaves_it_as_it_was(self):
        # Quietly: an older client sends none on every report, and that is
        # not a client bug to warn about.
        with self.assertNoLogs(level="WARNING"):
            self._report(free_bytes=1, total_bytes=2)
        self.assertIsNone(self._reserve())
        self._report(free_bytes=1, total_bytes=2, reserve_bytes=5)
        self._report(free_bytes=3, total_bytes=4)
        self.assertEqual(self._reserve(), 5)

    def test_an_unusable_reserve_is_ignored_without_losing_the_rest(self):
        for bad in (-1, "5", None, True, 1.5):
            with self.subTest(bad=bad):
                resp = self._report(free_bytes=7, total_bytes=8, reserve_bytes=bad)
                self.assertEqual(resp.status_code, 200)
                self.assertIsNone(self._reserve())
                self.assertEqual(self.conn.execute(
                    "SELECT reported_free_bytes FROM devices WHERE id = ?", (self.device_id,)).fetchone()[0], 7)

    def _reserve(self):
        return self.conn.execute(
            "SELECT reported_reserve_bytes FROM devices WHERE id = ?", (self.device_id,)).fetchone()[0]


class DeviceChangesEstimatedSizeTests(_RouteTestBase):
    """GET /api/device/changes: each download carries `estimated_size`, what
    it will take on the device, for the device's space check before it
    downloads. A transcoded file's size is only known once written, so it
    gets the estimate the usage figures use; anything else, its size."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "phone")
        self.tracks = {}
        for name, path, size, duration, lossless in (
                ("flac", "a/1.flac", 5_000_000, 60.0, 1), ("mp3", "a/2.mp3", 1_000_000, 60.0, 0)):
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, duration, lossless) "
                "VALUES (?, 'A', 'Al', ?, ?, 0, ?, ?)", (path, name, size, duration, lossless))
            self.tracks[name] = sync_state._new_id(cur)
            sync_state.create_selection(self.conn, "track", str(self.tracks[name]), self.owner, [self.device])
        self.conn.commit()
        patcher = mock.patch.object(main.provenance, "start_ensure_fingerprints")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _estimates(self):
        body = self.client.get("/api/device/changes",
                               headers={"Authorization": f"Bearer {self.token}"}).get_json()
        return {t["track_id"]: (t["transcode"], t["estimated_size"]) for t in body["to_download"]}

    def test_without_transcoding_the_estimate_is_the_size(self):
        self.assertEqual(self._estimates(), {
            self.tracks["flac"]: (False, 5_000_000), self.tracks["mp3"]: (False, 1_000_000)})

    def test_a_transcoded_track_is_estimated_as_the_usage_figures_estimate_it(self):
        self.conn.execute("UPDATE devices SET transcode_format = 'mp3_320' WHERE id = ?", (self.device,))
        self.conn.commit()
        # 60 s at 320 kbit/s (40,000 bytes a second), plus the slack; the mp3
        # is copied as it is.
        self.assertEqual(self._estimates(), {
            self.tracks["flac"]: (True, 60 * 40_000 + sync_state._EST_SLACK_BYTES),
            self.tracks["mp3"]: (False, 1_000_000)})


class DeviceStorageForeignBytesTests(_RouteTestBase):
    """POST /api/device/storage's optional `foreign_bytes` -- the bytes of
    music already in the sync folder that Trobar did not put there.

    Everything here is about one rule: ABSENT IS NOT ZERO. A client too old
    to measure the folder sends nothing, and every device enrolled before
    the client could measure it has NULL. Storing zero for either would
    state a measurement nobody made, and the budget would then silently go
    on believing the folder was empty -- which is the defect this feature
    exists to fix, reintroduced from the other end."""

    def setUp(self):
        super().setUp()
        self.device_id, self.token = sync_state.create_device(
            self.conn, self.owner, "phone", max_size_bytes=5000)
        self.conn.commit()

    def _report(self, **body):
        return self.client.post("/api/device/storage", json=body,
                                headers={"Authorization": f"Bearer {self.token}"})

    def _row(self):
        return self.conn.execute(
            "SELECT reported_foreign_bytes AS b, foreign_bytes_reported_at AS at, "
            "reported_free_bytes AS free FROM devices WHERE id = ?",
            (self.device_id,)).fetchone()

    def test_a_reported_figure_is_stored_and_timestamped(self):
        self.assertEqual(self._report(free_bytes=1, total_bytes=2, foreign_bytes=4096).status_code, 200)
        row = self._row()
        self.assertEqual(row["b"], 4096)
        self.assertIsNotNone(row["at"])

    def test_zero_is_a_real_measurement_and_is_stored(self):
        self.assertEqual(self._report(free_bytes=1, total_bytes=2, foreign_bytes=0).status_code, 200)
        row = self._row()
        self.assertEqual(row["b"], 0)
        self.assertIsNotNone(row["at"])

    def test_an_old_client_omitting_the_field_leaves_it_unset(self):
        """The upgrade path: a client that never learned to measure the
        folder must not be recorded as having measured it empty."""
        self.assertEqual(self._report(free_bytes=1, total_bytes=2).status_code, 200)
        row = self._row()
        self.assertIsNone(row["b"])
        self.assertIsNone(row["at"])

    def test_a_later_report_without_the_field_does_not_erase_the_last_one(self):
        """A client that stops sending it -- a downgrade, or a build where
        the walk failed -- leaves a stale number that says how stale it is.
        Erasing it would throw away the only measurement anyone has."""
        self._report(free_bytes=1, total_bytes=2, foreign_bytes=4096)
        self._report(free_bytes=9, total_bytes=9)
        row = self._row()
        self.assertEqual(row["b"], 4096)
        self.assertEqual(row["free"], 9)  # the rest of the report still landed

    def test_an_unusable_value_is_ignored_without_losing_the_rest(self):
        """Negative, boolean and non-numeric are client bugs. Storing one
        would quietly shrink or inflate the budget for as long as nobody
        looked; 400ing the request would lose the free/total figures over a
        field that is allowed to be missing entirely."""
        for bad in (-1, "4096", None, True, 1.5):
            with self.subTest(bad=bad):
                self.conn.execute(
                    "UPDATE devices SET reported_foreign_bytes = NULL, "
                    "foreign_bytes_reported_at = NULL WHERE id = ?", (self.device_id,))
                self.conn.commit()
                resp = self._report(free_bytes=7, total_bytes=8, foreign_bytes=bad)
                self.assertEqual(resp.status_code, 200)
                row = self._row()
                self.assertIsNone(row["b"])
                self.assertEqual(row["free"], 7)

    def test_usage_reports_null_until_a_device_has_measured(self):
        _login(self.client, self.owner)
        data = self.client.get(f"/api/devices/{self.device_id}/usage").get_json()
        self.assertIsNone(data["reported_foreign_bytes"])
        self.assertIsNone(data["folder_used_bytes"])
        # Not merely falsy: the UI has to tell "unmeasured" from "empty".
        self.assertFalse(data["folder_over_limit"])

    def test_usage_reports_the_folder_total_once_measured(self):
        self._report(free_bytes=1, total_bytes=2, foreign_bytes=6000)
        _login(self.client, self.owner)
        data = self.client.get(f"/api/devices/{self.device_id}/usage").get_json()
        self.assertEqual(data["reported_foreign_bytes"], 6000)
        self.assertEqual(data["folder_used_bytes"], 6000)  # nothing synced yet
        # 6000 in a folder limited to 5000, with Trobar holding none of it.
        self.assertTrue(data["folder_over_limit"])
        self.assertFalse(data["over_limit"])


def _ok(title, tracks):
    return {"status": "ok", "playlist": title, "tracks": [
        {"position": i, "title": t, "artist": a, "album": None, "path": None}
        for i, (a, t) in enumerate(tracks)]}


class PlaylistSubscriptionTests(_RouteTestBase):
    """Public-playlist URL subscriptions: /api/playlist-subscriptions.

    Every test here stubs ytmusic_client.get_playlist_tracks. Nothing
    reaches YouTube, which is also the acceptance criterion these routes
    have to meet — a Trobar with no subscriptions makes no external call at
    all, and one with subscriptions makes exactly the calls its owner asked
    for."""

    URL = "https://music.youtube.com/playlist?list=PLabc123"

    def _add(self, url=None, response=None):
        response = response if response is not None else _ok("Road Trip", [("Bon Jovi", "Livin' On A Prayer")])
        with mock.patch.object(main.ytmusic_client, "get_playlist_tracks", return_value=response):
            return self.client.post("/api/playlist-subscriptions", json={"url": url or self.URL})

    def _subscriptions(self):
        return self.conn.execute("SELECT * FROM playlist_subscriptions ORDER BY id").fetchall()

    def _playlists(self):
        return self.conn.execute(
            "SELECT * FROM playlists WHERE source_provider = 'ytmusic' ORDER BY id").fetchall()

    def test_adding_a_url_imports_it_immediately(self):
        _login(self.client, self.owner)
        resp = self._add()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["subscription"]["title"], "Road Trip")
        self.assertEqual(body["subscription"]["track_count"], 1)
        self.assertEqual(len(self._playlists()), 1)

    def test_the_playlist_it_creates_is_owned_and_private(self):
        """#496's default, reached through a new route: a subscription is
        personal, so what it produces starts visible to its owner alone."""
        _login(self.client, self.owner)
        self._add()
        row = self._playlists()[0]
        self.assertEqual(row["owner_user_id"], self.owner)
        self.assertEqual(row["shared"], 0)

    def test_a_link_that_is_not_a_playlist_is_rejected_before_any_fetch(self):
        _login(self.client, self.owner)
        with mock.patch.object(main.ytmusic_client, "get_playlist_tracks") as fetch:
            resp = self.client.post(
                "/api/playlist-subscriptions", json={"url": "https://example.test/nope"})
        self.assertEqual(resp.status_code, 400)
        fetch.assert_not_called()
        self.assertEqual(self._subscriptions(), [])

    def test_re_adding_the_same_playlist_by_another_url_refreshes_it(self):
        """The same playlist has several URL spellings. Pasting a second
        one means "this playlist", not "a second playlist"."""
        _login(self.client, self.owner)
        self._add()
        self._add(url="https://www.youtube.com/watch?v=abc&list=PLabc123")
        self.assertEqual(len(self._subscriptions()), 1)
        self.assertEqual(len(self._playlists()), 1)

    def test_two_users_subscribing_to_one_playlist_get_one_each(self):
        """Keyed on the subscription, not the external id. Sharing one row
        would hand ownership to whoever synced last and reset the other's
        sharing choice with it."""
        _login(self.client, self.owner)
        self._add()
        _login(self.client, self.other)
        self._add()
        rows = self._playlists()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["owner_user_id"] for r in rows}, {self.owner, self.other})

    def test_a_playlist_that_matches_nothing_reports_its_counts(self):
        """The mix-video case. It imports perfectly and matches nothing,
        and its URL is indistinguishable from a good playlist's — so the
        pair of numbers is the only thing that tells a user which they
        pasted."""
        _login(self.client, self.owner)
        resp = self._add(response=_ok("Jazz house mix", [
            ("Max Shkiv", "90 minute jazz house mix"),
            ("Max Shkiv", "2 hour lounge mix"),
        ]))
        sub = resp.get_json()["subscription"]
        self.assertEqual((sub["track_count"], sub["matched_count"]), (2, 0))

    def test_an_unavailable_playlist_is_recorded_not_raised(self):
        _login(self.client, self.owner)
        resp = self._add(response={"status": "unavailable"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "unavailable")
        self.assertEqual(self._subscriptions()[0]["last_error"], "unavailable")
        self.assertEqual(self._playlists(), [])

    def test_a_later_failure_never_removes_the_playlist_already_imported(self):
        """#71's lesson, on the source most exposed to it: the endpoint is
        unofficial and will refuse sometimes. A playlist somebody has
        already synced onto a device must survive that."""
        _login(self.client, self.owner)
        self._add()
        playlist_id = self._playlists()[0]["id"]
        sub_id = self._subscriptions()[0]["id"]

        with mock.patch.object(main.ytmusic_client, "get_playlist_tracks",
                               return_value={"status": "unavailable"}):
            resp = self.client.post(f"/api/playlist-subscriptions/{sub_id}/refresh")
        self.assertEqual(resp.get_json()["status"], "unavailable")

        rows = self._playlists()
        self.assertEqual([r["id"] for r in rows], [playlist_id])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM playlist_tracks WHERE playlist_id = ?",
                (playlist_id,)).fetchone()["c"],
            1)
        self.assertEqual(self._subscriptions()[0]["last_error"], "unavailable")

    def test_a_successful_refresh_clears_the_error(self):
        _login(self.client, self.owner)
        self._add(response={"status": "unavailable"})
        sub_id = self._subscriptions()[0]["id"]
        with mock.patch.object(main.ytmusic_client, "get_playlist_tracks",
                               return_value=_ok("Back again", [("A", "B")])):
            self.client.post(f"/api/playlist-subscriptions/{sub_id}/refresh")
        row = self._subscriptions()[0]
        self.assertIsNone(row["last_error"])
        self.assertEqual(row["title"], "Back again")

    def test_a_rename_at_the_source_follows_through(self):
        _login(self.client, self.owner)
        self._add()
        sub_id = self._subscriptions()[0]["id"]
        with mock.patch.object(main.ytmusic_client, "get_playlist_tracks",
                               return_value=_ok("Renamed", [("A", "B")])):
            self.client.post(f"/api/playlist-subscriptions/{sub_id}/refresh")
        self.assertEqual(self._playlists()[0]["title"], "Renamed")

    def test_removing_a_subscription_removes_its_playlist(self):
        """Deliberately unlike a failed fetch: this is the user asking for
        it to go."""
        _login(self.client, self.owner)
        self._add()
        sub_id = self._subscriptions()[0]["id"]
        resp = self.client.delete(f"/api/playlist-subscriptions/{sub_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._subscriptions(), [])
        self.assertEqual(self._playlists(), [])

    def test_listing_shows_only_your_own(self):
        _login(self.client, self.owner)
        self._add()
        _login(self.client, self.other)
        self.assertEqual(
            self.client.get("/api/playlist-subscriptions").get_json()["subscriptions"], [])

    def test_the_admin_cannot_list_someone_elses_either(self):
        """An admin can already see every playlist a subscription produces.
        Listing other people's subscribed URLs here would only add a second,
        less obvious place for one household member's tastes to leak."""
        _login(self.client, self.owner)
        self._add()
        _login(self.client, self.admin)
        self.assertEqual(
            self.client.get("/api/playlist-subscriptions").get_json()["subscriptions"], [])

    def test_another_user_cannot_refresh_or_remove_yours(self):
        _login(self.client, self.owner)
        self._add()
        sub_id = self._subscriptions()[0]["id"]
        _login(self.client, self.other)
        self.assertEqual(
            self.client.post(f"/api/playlist-subscriptions/{sub_id}/refresh").status_code, 403)
        self.assertEqual(
            self.client.delete(f"/api/playlist-subscriptions/{sub_id}").status_code, 403)
        self.assertEqual(len(self._subscriptions()), 1)

    def test_the_admin_can_remove_one(self):
        _login(self.client, self.owner)
        self._add()
        sub_id = self._subscriptions()[0]["id"]
        _login(self.client, self.admin)
        self.assertEqual(
            self.client.delete(f"/api/playlist-subscriptions/{sub_id}").status_code, 200)


class PlaylistSubscriptionSyncTests(_RouteTestBase):
    """The subscription merge inside a full sync run, which is a different
    path from the routes above: the prune pass runs there and is what could
    delete a playlist behind a failing fetch."""

    def _subscribe(self, owner_user_id, external_id="PLabc123"):
        cur = self.conn.execute(
            "INSERT INTO playlist_subscriptions (owner_user_id, provider, external_id, url) "
            "VALUES (?, 'ytmusic', ?, ?)",
            (owner_user_id, external_id, f"https://music.youtube.com/playlist?list={external_id}"))
        self.conn.commit()
        return sync_state._new_id(cur)

    def _sync(self, response=None, side_effect=None):
        """A full sync run with a do-nothing active provider, so what is
        under test is the subscription merge and the prune pass around it
        rather than any particular provider's listing."""
        provider = mock.Mock()
        provider.list_playlists.return_value = {"status": "ok", "playlists": []}
        with mock.patch.object(playlist_sync.ytmusic_client, "get_playlist_tracks",
                               return_value=response, side_effect=side_effect):
            return playlist_sync.sync_playlists(provider, "subsonic")

    def test_a_subscription_is_merged_in_alongside_the_active_provider(self):
        self._subscribe(self.owner)
        result = self._sync(_ok("Road Trip", [("Bon Jovi", "Livin' On A Prayer")]))
        self.assertEqual(result["status"], "ok")
        rows = self.conn.execute(
            "SELECT title, owner_user_id FROM playlists WHERE source_provider = 'ytmusic'").fetchall()
        self.assertEqual([(r["title"], r["owner_user_id"]) for r in rows], [("Road Trip", self.owner)])

    def test_with_no_subscriptions_the_client_is_never_called(self):
        """Opt-in, measured rather than asserted in prose: zero external
        calls when nobody has subscribed to anything."""
        provider = mock.Mock()
        provider.list_playlists.return_value = {"status": "ok", "playlists": []}
        with mock.patch.object(playlist_sync.ytmusic_client, "get_playlist_tracks") as fetch:
            playlist_sync.sync_playlists(provider, "subsonic")
        fetch.assert_not_called()

    def test_the_prune_pass_spares_a_playlist_whose_fetch_failed(self):
        """Without the explicit protection this is exactly how the playlist
        disappears: its key is not in listed_keys this run, and 'ytmusic'
        is in provider_ids, so the stale pass would take it."""
        self._subscribe(self.owner)
        self._sync(_ok("Road Trip", [("Bon Jovi", "Livin' On A Prayer")]))
        before = self.conn.execute(
            "SELECT id FROM playlists WHERE source_provider = 'ytmusic'").fetchone()["id"]

        result = self._sync({"status": "unavailable"})
        after = self.conn.execute(
            "SELECT id FROM playlists WHERE source_provider = 'ytmusic'").fetchall()
        self.assertEqual([r["id"] for r in after], [before])
        self.assertEqual(result["removed"], 0)

    def test_one_subscription_failing_does_not_take_another_users_with_it(self):
        self._subscribe(self.owner, "PLone")
        self._subscribe(self.other, "PLtwo")
        calls = []

        def per_subscription(title, external_id):
            calls.append(external_id)
            if external_id == "PLone":
                return {"status": "unavailable"}
            return _ok("Theirs", [("A", "B")])

        # Seed both, then fail only one.
        self._sync(_ok("Seeded", [("A", "B")]))
        self._sync(side_effect=per_subscription)
        rows = self.conn.execute(
            "SELECT title FROM playlists WHERE source_provider = 'ytmusic' ORDER BY id").fetchall()
        self.assertEqual([r["title"] for r in rows], ["Seeded", "Theirs"])
        self.assertEqual(sorted(calls), ["PLone", "PLtwo"])


class SpotifyLinkSubscriptionTests(_RouteTestBase):
    """Public Spotify playlists by link, through the same routes and sync
    as YouTube Music's. Both clients are stubbed: nothing reaches either
    service."""

    PID = "0aBcDeFgHiJkLmNoPqRsTu"
    URL = f"https://open.spotify.com/playlist/{PID}?si=abc"

    def _add(self, url=None, response=None):
        response = response if response is not None else _ok("Mix", [("Bon Jovi", "Livin' On A Prayer")])
        with mock.patch.object(main.spotify_public_client, "get_playlist_tracks",
                               return_value=response) as spotify, \
                mock.patch.object(main.ytmusic_client, "get_playlist_tracks",
                                  return_value=_ok("YT", [("A", "B")])) as yt:
            resp = self.client.post("/api/playlist-subscriptions", json={"url": url or self.URL})
        return resp, spotify, yt

    def _subscriptions(self):
        return self.conn.execute("SELECT * FROM playlist_subscriptions ORDER BY id").fetchall()

    def test_a_spotify_link_is_fetched_from_spotify(self):
        _login(self.client, self.owner)
        resp, spotify, yt = self._add()
        self.assertEqual(resp.status_code, 200)
        sub = resp.get_json()["subscription"]
        self.assertEqual((sub["provider"], sub["title"], sub["track_count"]),
                         ("spotify_public", "Mix", 1))
        self.assertEqual(sub["track_limit"], 100)
        self.assertEqual(spotify.call_args.args[1], self.PID)
        yt.assert_not_called()
        row = self.conn.execute(
            "SELECT owner_user_id FROM playlists WHERE source_provider = 'spotify_public'").fetchall()
        self.assertEqual([r["owner_user_id"] for r in row], [self.owner])

    def test_a_youtube_subscription_has_no_track_limit(self):
        _login(self.client, self.owner)
        resp, spotify, yt = self._add(url="https://music.youtube.com/playlist?list=PLabc123")
        sub = resp.get_json()["subscription"]
        self.assertEqual((sub["provider"], sub["track_limit"]), ("ytmusic", None))
        spotify.assert_not_called()

    def test_a_bare_id_still_goes_to_youtube_music(self):
        _login(self.client, self.owner)
        resp, spotify, yt = self._add(url=self.PID)
        self.assertEqual(resp.get_json()["subscription"]["provider"], "ytmusic")
        spotify.assert_not_called()
        self.assertEqual(yt.call_args.args[1], self.PID)

    def test_a_short_link_is_rejected_before_any_fetch(self):
        _login(self.client, self.owner)
        resp, spotify, yt = self._add(url="https://spotify.link/AbCdEf")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Spotify", resp.get_json()["error"])
        spotify.assert_not_called()
        yt.assert_not_called()
        self.assertEqual(self._subscriptions(), [])

    def test_two_spellings_of_one_playlist_are_one_subscription(self):
        _login(self.client, self.owner)
        self._add()
        self._add(url=f"spotify:playlist:{self.PID}")
        self.assertEqual(len(self._subscriptions()), 1)

    def test_an_unavailable_playlist_is_recorded(self):
        _login(self.client, self.owner)
        resp, _, _ = self._add(response={"status": "unavailable"})
        self.assertEqual(resp.get_json()["status"], "unavailable")
        self.assertEqual(self._subscriptions()[0]["last_error"], "unavailable")

    def _subscribe(self):
        self.conn.execute(
            "INSERT INTO playlist_subscriptions (owner_user_id, provider, external_id, url) "
            "VALUES (?, 'spotify_public', ?, ?)", (self.owner, self.PID, self.URL))
        self.conn.commit()

    def _sync(self, response):
        provider = mock.Mock()
        provider.list_playlists.return_value = {"status": "ok", "playlists": []}
        with mock.patch.object(playlist_sync.spotify_public_client, "get_playlist_tracks",
                               return_value=response):
            return playlist_sync.sync_playlists(provider, "subsonic")

    def test_a_failed_fetch_keeps_the_playlist(self):
        self._subscribe()
        self._sync(_ok("Mix", [("A", "B")]))
        result = self._sync({"status": "error", "reason": "unexpected_response"})
        rows = self.conn.execute(
            "SELECT title FROM playlists WHERE source_provider = 'spotify_public'").fetchall()
        self.assertEqual([r["title"] for r in rows], ["Mix"])
        self.assertEqual(result["removed"], 0)

    def test_a_linked_spotify_accounts_playlist_is_not_pruned_by_a_link(self):
        """Why the link source has its own id. Were it "spotify", a sync
        with a Spotify link and no account linked this run would count
        itself authoritative for every "spotify" row, and prune them."""
        self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, "
            "shared, last_synced_at) VALUES ('From my account', 'spotify', 'acct1', ?, 0, "
            "datetime('now'))", (self.other,))
        self.conn.commit()
        self._subscribe()
        self._sync(_ok("Mix", [("A", "B")]))
        rows = self.conn.execute(
            "SELECT title FROM playlists WHERE source_provider = 'spotify'").fetchall()
        self.assertEqual([r["title"] for r in rows], ["From my account"])


class MirrorCopySelectionCarryTests(_RouteTestBase):
    """A Jellyfin copy that older versions read back as a source left a
    duplicate row. The listing now skips copies, so the next sync prunes
    that row once, moving its device selections to the original first."""

    COPY = "6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b"
    PROVIDER = "jellyfin"

    def setUp(self):
        super().setUp()
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
            "VALUES ('a/b/1.flac', 'Artist A', 'Album', 'Song A', 1, 1, 0.0)")
        self.track = sync_state._new_id(cur)
        self.original = self._row("Road Trip", "filesystem", None, remote_id=self.COPY)
        # The copy's id spelled differently from the stored one: no dashes,
        # upper case.
        self.duplicate = self._row("Road Trip", self.PROVIDER, self.COPY.replace("-", "").upper())
        cur = self.conn.execute(
            "INSERT INTO devices (owner_user_id, name, api_token_hash) VALUES (?, 'd', 'h')", (self.owner,))
        self.device = sync_state._new_id(cur)
        self.conn.commit()

    def _row(self, title, source, source_id, remote_id=None):
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, "
            f"last_synced_at, {self.PROVIDER}_mirror_remote_id) VALUES (?, ?, ?, ?, 0, datetime('now'), ?)",
            (title, source, source_id, self.owner, remote_id))
        pid = sync_state._new_id(cur)
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, 0, 'Artist A', 'Song A', ?)", (pid, self.track))
        return pid

    def _select(self, playlist_id):
        sync_state.create_selection(self.conn, "playlist", str(playlist_id), self.owner, [self.device])
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded' WHERE device_id = ?",
                          (self.device,))
        self.conn.commit()

    def _sync(self):
        provider = mock.Mock()
        provider.list_playlists.return_value = {"status": "ok", "playlists": []}
        return playlist_sync.sync_playlists(provider, self.PROVIDER)

    def _selections(self):
        return [(r["target"], r["device_id"]) for r in self.conn.execute(
            "SELECT s.target, sd.device_id FROM selections s "
            "JOIN selection_devices sd ON sd.selection_id = s.id WHERE s.type = 'playlist'")]

    def _state(self):
        return self.conn.execute("SELECT status FROM device_track_state WHERE device_id = ? AND track_id = ?",
                                 (self.device, self.track)).fetchone()["status"]

    def test_the_duplicates_selection_moves_to_the_original(self):
        self._select(self.duplicate)
        result = self._sync()
        self.assertEqual(result["removed"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM playlists WHERE id = ?", (self.duplicate,)).fetchone())
        self.assertEqual(self._selections(), [(str(self.original), self.device)])
        self.assertEqual(self._state(), "downloaded")  # nothing for the device to delete

    def test_a_device_already_given_the_original_is_not_given_it_twice(self):
        self._select(self.original)
        self._select(self.duplicate)
        self._sync()
        self.assertEqual(self._selections(), [(str(self.original), self.device)])
        self.assertEqual(self._state(), "downloaded")

    def test_a_pruned_playlist_that_is_not_a_copy_still_takes_its_selection(self):
        other = self._row("Gone", self.PROVIDER, "ffffffffffffffffffffffffffffffff")
        self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (self.original,))
        self.conn.commit()
        self._select(other)
        self._sync()
        self.assertNotIn(str(other), [t for t, _ in self._selections()])
        self.assertEqual(self._state(), "removed")


    def _select_as(self, user_id, playlist_id):
        cur = self.conn.execute(
            "INSERT INTO devices (owner_user_id, name, api_token_hash) VALUES (?, 'theirs', 'h2')", (user_id,))
        device = sync_state._new_id(cur)
        sync_state.create_selection(self.conn, "playlist", str(playlist_id), user_id, [device])
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded' WHERE device_id = ?", (device,))
        self.conn.commit()
        return device

    def _theirs(self, user_id):
        return [r["target"] for r in self.conn.execute(
            "SELECT target FROM selections WHERE type = 'playlist' AND created_by_user_id = ?", (user_id,))]

    def test_a_selection_is_not_carried_onto_a_private_playlist_its_creator_cant_see(self):
        """The duplicate came back through the default pass, so it's
        unowned and anyone could select it; the original is the owner's
        private playlist."""
        self.conn.execute("UPDATE playlists SET owner_user_id = NULL WHERE id = ?", (self.duplicate,))
        self.conn.commit()
        device = self._select_as(self.other, self.duplicate)
        self._sync()
        self.assertEqual(self._theirs(self.other), [])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM device_track_state WHERE device_id = ?", (device,)).fetchone()["status"],
            "removed")

    def test_a_selection_is_carried_when_its_creator_can_see_the_original(self):
        for label, setup, creator in (
                ("shared", "UPDATE playlists SET shared = 1 WHERE id = ?", self.other),
                ("unowned", "UPDATE playlists SET owner_user_id = NULL WHERE id = ?", self.other),
                ("admin", None, self.admin)):
            with self.subTest(label):
                if setup:
                    self.conn.execute(setup, (self.original,))
                self.conn.commit()
                duplicate = self._row("Road Trip", self.PROVIDER, self.COPY.replace("-", ""))
                self.conn.commit()
                self._select_as(creator, duplicate)
                self._sync()
                self.assertEqual(self._theirs(creator), [str(self.original)])
                self.conn.execute("DELETE FROM selections WHERE created_by_user_id = ?", (creator,))
                self.conn.execute("UPDATE playlists SET owner_user_id = ?, shared = 0 WHERE id = ?",
                                  (self.owner, self.original))
                self.conn.commit()


class EmbyMirrorCopySelectionCarryTests(MirrorCopySelectionCarryTests):
    PROVIDER = "emby"


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
        ({"mirror_plex_url": "http//bad", "mirror_plex_token": "t"}, "Plex mirror-target URL"),
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

    def test_a_rejected_setting_is_named_by_the_field_the_page_binds(self):
        # The configuration page opens the section holding `field` and
        # focuses its input, found by its x-model binding: a field the
        # template doesn't bind would leave the admin with no pointer.
        template = (Path(__file__).parent / "templates" / "index.html").read_text()
        bad_numbers = [
            ({"transcode_concurrency": "many"}, "transcode_concurrency"),
            ({"transcode_nice_level": 99}, "transcode_nice_level"),
            ({"transcode_cache_gb": "lots"}, "transcode_cache_gb"),
            ({"transcode_cache_gb": -1}, "transcode_cache_gb"),
            ({"job_retention_days": 0}, "job_retention_days"),
            ({"scan_interval_hours": -1}, "scan_interval_hours"),
        ]
        cases = [(payload, next(iter(payload))) for payload, _label in self._MALFORMED_URL_PAYLOADS]
        _login(self.client, self.admin)
        for payload, field in cases + bad_numbers:
            with self.subTest(payload=payload):
                resp = self.client.put("/api/admin/config", json=payload)
                self.assertEqual(resp.status_code, 400, payload)
                self.assertEqual(resp.get_json()["field"], field)
                self.assertRegex(template, rf'x-model(\.number)?="adminConfig\.{field}"')

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


class AdminConfigMusicAssistantTests(_RouteTestBase):
    """PUT /api/admin/config's Music Assistant connection: URL and token
    saved together, both blank disconnects, and its state in the GET."""

    def _count(self, n=9):
        resp = mock.Mock()
        resp.status_code = 200
        resp.json.return_value = n
        resp.raise_for_status.return_value = None
        return resp

    def test_saving_url_and_token_persists_and_reports_paired(self):
        _login(self.client, self.admin)
        with mock.patch("requests.post", return_value=self._count()) as post:
            resp = self.client.put("/api/admin/config", json={
                "music_assistant_url": "http://ma.example.invalid:8095", "music_assistant_token": " tok ",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "music_assistant_url"), "http://ma.example.invalid:8095")
        self.assertEqual(db.get_config(self.conn, "music_assistant_token"), "tok")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})
        body = resp.get_json()
        self.assertEqual(body["music_assistant_state"], "paired")
        self.assertEqual(body["music_assistant_url"], "http://ma.example.invalid:8095")

    def test_a_malformed_url_is_refused_and_nothing_saved(self):
        _login(self.client, self.admin)
        with mock.patch("requests.post") as post:
            resp = self.client.put("/api/admin/config", json={
                "music_assistant_url": "http//ma.example.invalid", "music_assistant_token": "tok",
            })
        self.assertEqual(resp.status_code, 400)
        post.assert_not_called()
        self.assertIsNone(db.get_config(self.conn, "music_assistant_url"))

    def test_blanking_both_disconnects(self):
        db.set_config(self.conn, "music_assistant_url", "http://ma.example.invalid:8095")
        db.set_config(self.conn, "music_assistant_token", "tok")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("requests.post") as post:
            resp = self.client.put("/api/admin/config", json={"music_assistant_url": "", "music_assistant_token": ""})
        self.assertEqual(resp.status_code, 200)
        post.assert_not_called()
        self.assertIsNone(db.get_config(self.conn, "music_assistant_url"))
        self.assertIsNone(db.get_config(self.conn, "music_assistant_token"))
        self.assertEqual(resp.get_json()["music_assistant_state"], "disconnected")

    def test_a_payload_that_never_mentions_it_leaves_it_untouched(self):
        db.set_config(self.conn, "music_assistant_url", "http://ma.example.invalid:8095")
        db.set_config(self.conn, "music_assistant_token", "tok")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("requests.post", return_value=self._count()):
            resp = self.client.put("/api/admin/config", json={"job_retention_days": 14})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "music_assistant_token"), "tok")

    def test_test_connection_dispatches_url_and_token(self):
        _login(self.client, self.admin)
        with mock.patch("music_assistant_client.test_connection",
                        return_value={"state": "paired", "url": "http://ma.example.invalid:8095",
                                      "provider": "music_assistant"}) as tc:
            resp = self.client.post("/api/admin/config/test-connection", json={
                "provider": "music_assistant", "url": "http://ma.example.invalid:8095", "token": "tok",
            })
        self.assertEqual(resp.get_json()["state"], "paired")
        tc.assert_called_once_with("http://ma.example.invalid:8095", "tok")
        self.assertIsNone(db.get_config(self.conn, "music_assistant_url"))

    def test_test_connection_requires_the_token(self):
        _login(self.client, self.admin)
        resp = self.client.post("/api/admin/config/test-connection", json={
            "provider": "music_assistant", "url": "http://ma.example.invalid:8095",
        })
        self.assertEqual(resp.status_code, 400)


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


class ProviderSwitchTests(_RouteTestBase):
    """Changing the library source: the general config PUT no longer does
    it once setup is complete; POST /api/admin/provider/switch does, after
    re-testing the connection, and removes only the old provider's
    playlists, through the path that tells devices."""

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "provider", "jellyfin")
        db.set_config(self.conn, "setup_completed", "1")
        self.conn.commit()
        self.tracks = [self._track(i) for i in range(3)]
        self.kids = self._playlist("Kids", "jellyfin", self.tracks[:2])
        self.focus = self._playlist("Focus", "jellyfin", self.tracks[2:], mirror=True)
        self.tidal = self._playlist("Discover", "tidal", self.tracks[2:])
        self.m3u = self._playlist("Road", "filesystem", self.tracks[2:])
        self.device, _ = sync_state.create_device(self.conn, self.owner, "Garmin")
        sync_state.create_selection(self.conn, "playlist", str(self.kids), self.owner, [self.device])
        # The second Kids track is also selected on its own: it must stay.
        sync_state.create_selection(self.conn, "track", str(self.tracks[1]), self.owner, [self.device])
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded' WHERE device_id = ?",
                          (self.device,))
        self.conn.commit()
        _login(self.client, self.admin)

    def _track(self, i: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
            "VALUES (?, 'A', 'B', ?, ?, 1000, 0)", (f"A/B/{i}.flac", f"T{i}", i + 1))
        self.conn.commit()
        return sync_state._new_id(cur)

    def _playlist(self, title: str, source: str, track_ids, mirror: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, mirror_enabled, "
            "last_synced_at) VALUES (?, ?, ?, ?, datetime('now'))",
            (title, source, f"{source}-{title}", 1 if mirror else 0))
        pid = sync_state._new_id(cur)
        for pos, tid in enumerate(track_ids):
            self.conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
                "VALUES (?, ?, 'A', 'x', ?)", (pid, pos, tid))
        self.conn.commit()
        return pid

    def _titles(self) -> set:
        conn = db.get_conn()
        try:
            return {r["title"] for r in conn.execute("SELECT title FROM playlists")}
        finally:
            conn.close()

    def _provider(self) -> str | None:
        conn = db.get_conn()
        try:
            return db.get_config(conn, "provider")
        finally:
            conn.close()

    _SUBSONIC = {"provider": "subsonic", "url": "http://nav.local", "username": "u", "password": "p"}

    # The target's playlists as its client lists them: {id: (title, [(artist, title), ...])}.
    def _target(self, client, playlists=None, listing_ok=True, per_user=None):
        playlists = playlists or {}
        per_user = per_user or {}

        def list_playlists(user_id=None):
            if not listing_ok:
                return {"status": "error", "reason": "unreachable"}
            ids = per_user.get(user_id, []) if user_id else [i for i in playlists if i not in
                                                            {x for v in per_user.values() for x in v}]
            return {"status": "ok", "playlists": [{"id": i, "title": playlists[i][0]} for i in ids]}

        def get_playlist_tracks(title, source_playlist_id=None, **_kwargs):
            entries = playlists[source_playlist_id][1]
            # An entry is (artist, title), or (artist, title, mbid) for a
            # source that gives a MusicBrainz recording id.
            return {"status": "ok", "tracks": [
                {"position": n, "artist": e[0], "title": e[1], "path": None, "mbid": e[2] if len(e) > 2 else None}
                for n, e in enumerate(entries)]}

        return _Patches(client, list_playlists, get_playlist_tracks)

    def _switch(self, body=None, state="paired", target=None, carry=None):
        body = {**(body or self._SUBSONIC), **({"carry": carry} if carry is not None else {})}
        with mock.patch.object(main.subsonic_client, "test_connection",
                               return_value={"state": state}) as test, \
             mock.patch.object(main.subsonic_client, "reconnect") as reconnect, \
             (target or self._target(main.subsonic_client)), \
             mock.patch.object(main.playlist_sync, "start_sync",
                               return_value={"status": "started", "job_id": 7}) as start:
            resp = self.client.post("/api/admin/provider/switch", json=body)
        return resp, test, reconnect, start

    def _preview(self, body=None, target=None):
        with (target or self._target(main.subsonic_client)):
            return self.client.post("/api/admin/provider/switch/preview", json=body or self._SUBSONIC)

    def _set_provider(self, value):
        db.set_config(self.conn, "provider", value)
        self.conn.commit()

    def test_a_switch_away_from_roon_stops_it(self):
        self._set_provider("roon")
        with mock.patch.object(main.roon_client, "stop") as stop:
            resp, *_ = self._switch()
        self.assertEqual(resp.status_code, 200)
        stop.assert_called_once()

    def test_a_switch_between_other_sources_leaves_roon_alone(self):
        self._set_provider("filesystem")
        with mock.patch.object(main.roon_client, "stop") as stop:
            self._switch()
        stop.assert_not_called()

    def test_a_save_connects_roon_only_while_it_is_the_source(self):
        address = {"roon_host": "core.invalid", "roon_port": 9330}
        for provider, called in (("subsonic", "store_address"), ("roon", "reconnect")):
            with self.subTest(provider), \
                    mock.patch.object(main.roon_client, "reconnect") as reconnect, \
                    mock.patch.object(main.roon_client, "store_address") as store:
                self._set_provider(provider)
                self.client.put("/api/admin/config", json=address)
                ({"store_address": store, "reconnect": reconnect}[called]).assert_called_once_with("core.invalid", 9330)
                ({"store_address": reconnect, "reconnect": store}[called]).assert_not_called()

    def test_the_config_put_refuses_a_different_provider_once_setup_is_done(self):
        resp = self.client.put("/api/admin/config", json={"provider": "subsonic"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["field"], "provider")
        self.assertEqual(self._provider(), "jellyfin")
        self.assertEqual(self._titles(), {"Kids", "Focus", "Discover", "Road"})

    def test_the_config_put_accepts_the_current_provider(self):
        resp = self.client.put("/api/admin/config", json={"provider": "jellyfin"})
        self.assertEqual(resp.status_code, 200)

    def test_first_run_setup_still_picks_the_provider_through_the_put(self):
        db.set_config(self.conn, "setup_completed", None)
        self.conn.commit()
        resp = self.client.put("/api/admin/config", json={"provider": "filesystem"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._provider(), "filesystem")

    def test_the_preview_counts_what_a_switch_removes_and_changes_nothing(self):
        resp = self._preview()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "from": "jellyfin",
            "carried": {"count": 0, "titles": [], "ids": [], "status": "ok"},
            "removed": {"count": 2, "titles": ["Focus", "Kids"], "mirrored": 1},
            "devices": [{"name": "Garmin", "playlists": 1}],
            "kept": {"tidal": 1, "filesystem": 1},
        })
        self.assertEqual(self._titles(), {"Kids", "Focus", "Discover", "Road"})

    def test_the_preview_counts_a_playlist_mirrored_to_any_one_sink(self):
        columns = ("mirror_enabled", "subsonic_mirror_enabled", "jellyfin_mirror_enabled",
                   "emby_mirror_enabled", "music_assistant_mirror_enabled", "plex_mirror_enabled")
        self.conn.execute(f"UPDATE playlists SET {' = 0, '.join(columns)} = 0")
        self.conn.commit()
        self.assertEqual(self._preview().get_json()["removed"]["mirrored"], 0)
        for column in columns:
            with self.subTest(column=column):
                self.conn.execute(f"UPDATE playlists SET {column} = 1 WHERE title = 'Kids'")
                self.conn.commit()
                self.assertEqual(self._preview().get_json()["removed"]["mirrored"], 1)
                self.conn.execute(f"UPDATE playlists SET {column} = 0")
                self.conn.commit()

    def test_the_preview_refuses_the_active_provider_and_an_unknown_one(self):
        self.assertEqual(self._preview({**self._SUBSONIC, "provider": "jellyfin"}).status_code, 409)
        self.assertEqual(self._preview({**self._SUBSONIC, "provider": "winamp"}).status_code, 400)

    def test_a_switch_removes_only_the_old_providers_playlists(self):
        resp, _test, reconnect, start = self._switch()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(body["provider"], "subsonic")
        self.assertEqual(body["removed"]["count"], 2)
        self.assertEqual(body["sync"], {"status": "started", "job_id": 7})
        reconnect.assert_called_once_with("http://nav.local", "u", "p")
        start.assert_called_once_with(main.subsonic_client, "subsonic")
        self.assertEqual(self._provider(), "subsonic")
        self.assertEqual(self._titles(), {"Discover", "Road"})

    def test_a_device_with_a_removed_playlist_selected_is_told_at_once(self):
        # Before this, the switch deleted every playlist row and left the
        # selection pointing at nothing: the playlist file vanished from the
        # device's next sync, its tracks stayed until something else
        # recomputed the device, and the dead selection was never removed.
        self._switch()
        conn = db.get_conn()
        try:
            sels = [r["type"] for r in conn.execute("SELECT type FROM selections")]
            changes = sync_state.get_changes(conn, self.device)
        finally:
            conn.close()
        self.assertEqual(sels, ["track"])
        self.assertEqual(changes["playlists"], [])
        self.assertEqual([d["track_id"] for d in changes["to_delete"]], [self.tracks[0]])
        self.assertEqual([d["track_id"] for d in changes["downloaded"]], [self.tracks[1]])

    def test_a_failed_connection_test_changes_nothing(self):
        resp, _test, reconnect, start = self._switch(state="disconnected")
        self.assertEqual(resp.status_code, 400)
        reconnect.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self._provider(), "jellyfin")
        self.assertEqual(self._titles(), {"Kids", "Focus", "Discover", "Road"})

    def test_a_switch_is_refused_while_a_playlist_sync_runs(self):
        with mock.patch.object(main.playlist_sync, "sync_status", return_value={"running": True}):
            resp, test, _reconnect, _start = self._switch()
        self.assertEqual(resp.status_code, 409)
        test.assert_not_called()
        self.assertEqual(self._provider(), "jellyfin")

    def test_a_sync_queued_during_the_test_or_the_planning_stops_the_switch(self):
        with mock.patch.object(main.playlist_sync, "sync_status",
                               side_effect=[{"running": False}, {"running": True}]):
            resp, test, reconnect, start = self._switch()
        self.assertEqual(resp.status_code, 409)
        test.assert_called_once()
        reconnect.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self._provider(), "jellyfin")
        self.assertEqual(self._titles(), {"Kids", "Focus", "Discover", "Road"})

    def test_a_scheduled_sync_cannot_queue_while_the_switch_runs(self):
        # An idle tick in the middle of the switch, after its last check,
        # must not queue a sync; the switch's own sync is the only one.
        held = []
        real_rows = admin._switch_provider_rows

        def rows_then_idle_tick(*args):
            held.append(main._PROVIDER_SWITCH_LOCK.locked())
            real_rows(*args)
            main._maybe_schedule_playlist_sync()

        with mock.patch.object(admin, "_switch_provider_rows", side_effect=rows_then_idle_tick):
            resp, _test, _reconnect, start = self._switch()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(held, [True])
        start.assert_called_once_with(main.subsonic_client, "subsonic")
        self.assertFalse(main._PROVIDER_SWITCH_LOCK.locked())

    def test_a_refused_switch_releases_the_lock(self):
        # A lock left held would stop every scheduled sync, silently.
        with mock.patch.object(main.playlist_sync, "sync_status",
                               side_effect=[{"running": False}, {"running": True}]):
            resp, *_ = self._switch()
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(main._PROVIDER_SWITCH_LOCK.locked())

    def test_a_sync_job_runs_against_the_provider_active_when_it_starts(self):
        # Queued under Jellyfin, run after a switch to Subsonic.
        db.set_config(self.conn, "provider", "subsonic")
        self.conn.commit()
        with mock.patch.object(main.playlist_sync, "sync_playlists", return_value={}) as sync:
            main._run_playlist_sync({"provider_id": "jellyfin"}, None)
        sync.assert_called_once_with(main.subsonic_client, "subsonic", None)

    def test_missing_fields_are_refused_before_any_test(self):
        resp, test, _reconnect, _start = self._switch({"provider": "subsonic", "url": "http://nav.local"})
        self.assertEqual(resp.status_code, 400)
        test.assert_not_called()

    def test_switching_away_from_filesystem_keeps_the_m3u_playlists(self):
        db.set_config(self.conn, "provider", "filesystem")
        self.conn.commit()
        resp, *_ = self._switch()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["removed"]["count"], 0)
        self.assertEqual(self._titles(), {"Kids", "Focus", "Discover", "Road"})

    def test_music_assistant_can_be_switched_to_like_any_provider(self):
        self._playlist("Probe mix", "music_assistant", [])
        body = {"provider": "music_assistant", "url": "http://ma.local:8095", "token": "t"}
        with mock.patch.object(main.music_assistant_client, "test_connection",
                               return_value={"state": "paired"}), \
             mock.patch.object(main.music_assistant_client, "reconnect") as reconnect, \
             mock.patch.object(main.playlist_sync, "start_sync", return_value={"status": "started"}) as start:
            resp = self.client.post("/api/admin/provider/switch", json=body)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        reconnect.assert_called_once_with("http://ma.local:8095", "t")
        start.assert_called_once_with(main.music_assistant_client, "music_assistant")
        self.assertEqual(self._provider(), "music_assistant")
        # Jellyfin's own playlists go; Music Assistant's were already there and stay.
        self.assertEqual(self._titles(), {"Discover", "Road", "Probe mix"})

    def test_switching_away_from_music_assistant_keeps_its_playlists(self):
        db.set_config(self.conn, "provider", "music_assistant")
        self.conn.commit()
        self._playlist("Probe mix", "music_assistant", [])
        resp, *_ = self._switch()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["removed"]["count"], 0)
        self.assertIn("Probe mix", self._titles())

    def test_first_run_setup_can_pick_music_assistant(self):
        db.set_config(self.conn, "setup_completed", None)
        self.conn.commit()
        resp = self.client.put("/api/admin/config", json={"provider": "music_assistant"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._provider(), "music_assistant")

    def test_the_connect_step_test_for_roon_reports_reachable_as_ok(self):
        with mock.patch.object(main.roon_client, "test_connection",
                               return_value={"state": "reachable"}) as test:
            resp = self.client.post("/api/admin/provider/switch/test",
                                    json={"provider": "roon", "host": "core.local", "port": "9330"})
        self.assertEqual(resp.get_json()["ok"], True)
        test.assert_called_once_with("core.local", 9330)

    def test_the_connect_test_also_takes_the_active_source(self):
        # The setup wizard, run again, tests the current source's settings as
        # typed. Testing changes nothing, so it isn't refused like a switch.
        with mock.patch.object(main.jellyfin_client, "test_connection",
                               return_value={"state": "paired"}) as test:
            resp = self.client.post("/api/admin/provider/switch/test", json={
                "provider": "jellyfin", "url": "http://jf.local:8096", "api_key": "k", "username": "u"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["ok"], True)
        test.assert_called_once()
        self.assertEqual(self._preview({**self._SUBSONIC, "provider": "jellyfin"}).status_code, 409,
                         "previewing a switch to it is still refused")

    def test_the_switch_endpoints_are_admin_only(self):
        _login(self.client, self.other)
        self.assertEqual(self._preview().status_code, 403)
        self.assertEqual(self._switch()[0].status_code, 403)
        self.assertEqual(self._provider(), "jellyfin")


    # --- Carrying playlists over to their namesakes ---

    def _songs(self, names):
        """Library tracks named by `names`, and their (artist, title) as a provider lists them."""
        ids = []
        for n in names:
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
                "VALUES (?, 'Artist', 'Album', ?, 1, 1000, 0)", (f"Artist/Album/{n}.flac", n))
            ids.append(sync_state._new_id(cur))
        self.conn.commit()
        return ids, [("Artist", n) for n in names]

    def _party(self, owner=None):
        """A Jellyfin playlist with four matched tracks, mirrored, selected on the device."""
        ids, entries = self._songs(["Song 1", "Song 2", "Song 3", "Song 4"])
        pid = self._playlist("Party Mix", "jellyfin", ids, mirror=True)
        self.conn.execute("UPDATE playlists SET inferred_origin_provider = 'tidal', golden_source_id = ? "
                          "WHERE id = ?", (self.tidal, pid))
        if owner is not None:
            self.conn.execute("UPDATE playlists SET owner_user_id = ? WHERE id = ?", (owner, pid))
            self.conn.commit()
        sync_state.create_selection(self.conn, "playlist", str(pid), self.owner, [self.device])
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded' WHERE device_id = ?",
                          (self.device,))
        self.conn.commit()
        return pid, ids, entries

    def _row(self, pid):
        conn = db.get_conn()
        try:
            return conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
        finally:
            conn.close()

    def test_a_namesake_with_the_same_tracks_is_carried_over_with_its_id_and_settings(self):
        pid, ids, entries = self._party()
        target = lambda: self._target(main.subsonic_client, {"s-party": (" party  mix", entries)})
        preview = self._preview(target=target()).get_json()
        self.assertEqual(preview["carried"], {"count": 1, "titles": [" party  mix"], "ids": [pid], "status": "ok"})
        self.assertEqual(preview["removed"]["titles"], ["Focus", "Kids"])

        resp, *_ = self._switch(target=target(), carry=preview["carried"]["ids"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["carried"]["count"], 1)
        row = self._row(pid)
        self.assertEqual((row["source_provider"], row["source_playlist_id"], row["mirror_enabled"]),
                         ("subsonic", "s-party", 1))
        # A Roon-only origin hint doesn't survive the move.
        self.assertEqual((row["inferred_origin_provider"], row["golden_source_id"]), (None, None))
        conn = db.get_conn()
        try:
            sel = conn.execute("SELECT 1 FROM selections WHERE type = 'playlist' AND target = ?",
                               (str(pid),)).fetchone()
            changes = sync_state.get_changes(conn, self.device)
        finally:
            conn.close()
        self.assertIsNotNone(sel)
        # The device keeps the playlist: nothing of it is queued for deletion.
        self.assertFalse({d["track_id"] for d in changes["to_delete"]} & set(ids))

    def test_a_namesake_whose_songs_match_only_by_recording_id_is_carried_over(self):
        pid, ids, _entries = self._party()
        mbids = [f"8f3471b5-7e6a-48da-86a9-c1c07a0f47a{n}" for n in range(4)]
        for track_id, mbid in zip(ids, mbids):
            self.conn.execute("UPDATE tracks SET mbid = ? WHERE id = ?", (mbid, track_id))
        self.conn.commit()
        renamed = [("Someone Else", f"Retitled {n}", mbid) for n, mbid in enumerate(mbids)]
        target = self._target(main.subsonic_client, {"s-party": ("Party Mix", renamed)})
        preview = self._preview(target=target).get_json()
        self.assertEqual(preview["carried"]["ids"], [pid])

    def test_the_preview_matches_what_the_switch_does(self):
        _pid, _ids, entries = self._party()
        # Three of the four tracks: carried over (3 shared, 100% of the smaller).
        target = lambda: self._target(main.subsonic_client, {"s-party": ("Party Mix", entries[:3])})
        preview = self._preview(target=target()).get_json()
        done = self._switch(target=target(), carry=preview["carried"]["ids"])[0].get_json()
        for key in ("carried", "removed", "devices", "kept"):
            self.assertEqual(done[key], preview[key], key)

    def test_a_target_that_answers_differently_at_the_switch_changes_nothing(self):
        # The review showed Party Mix carried over; at the switch the target's
        # listing fails, so the plan would remove it instead.
        pid, _ids, entries = self._party()
        preview = self._preview(target=self._target(
            main.subsonic_client, {"s-party": ("Party Mix", entries)})).get_json()
        self.assertEqual(preview["carried"]["ids"], [pid])
        resp, _test, reconnect, start = self._switch(
            target=self._target(main.subsonic_client, listing_ok=False), carry=preview["carried"]["ids"])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "plan_changed")
        reconnect.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self._provider(), "jellyfin")
        self.assertIsNotNone(self._row(pid))
        conn = db.get_conn()
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM selections WHERE type = 'playlist' AND target = ?", (str(pid),)).fetchone())
        finally:
            conn.close()

    def test_a_carry_over_the_review_did_not_show_changes_nothing(self):
        # Confirmed with nothing carried over; the target now has the namesake.
        pid, _ids, entries = self._party()
        resp, *_ = self._switch(target=self._target(
            main.subsonic_client, {"s-party": ("Party Mix", entries)}), carry=[])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self._row(pid)["source_provider"], "jellyfin")

    def test_a_namesake_with_too_little_overlap_is_not_carried_over(self):
        pid, _ids, entries = self._party()
        _other, others = self._songs(["Other 1", "Other 2", "Other 3"])
        # Two shared tracks out of five: under the three-track minimum.
        target = self._target(main.subsonic_client, {"s-party": ("Party Mix", entries[:2] + others)})
        resp, *_ = self._switch(target=target)
        self.assertEqual(resp.get_json()["carried"]["count"], 0)
        self.assertIsNone(self._row(pid))

    def test_three_shared_tracks_are_not_enough_under_sixty_percent(self):
        ids, entries = self._songs([f"Long {n}" for n in range(8)])
        pid = self._playlist("Long Drive", "jellyfin", ids)
        _other, others = self._songs([f"Else {n}" for n in range(5)])
        # Three shared of eight on the smaller side: 37.5%.
        target = self._target(main.subsonic_client, {"s-long": ("Long Drive", entries[:3] + others)})
        resp, *_ = self._switch(target=target)
        self.assertEqual(resp.get_json()["carried"]["count"], 0)
        self.assertIsNone(self._row(pid))

    def test_two_small_playlists_sharing_everything_still_need_three_tracks(self):
        # 2 of 2 shared passes the 60% rule; only the 3-track minimum stops a
        # different two-song "Favourites" being adopted.
        ids, entries = self._songs(["Fav 1", "Fav 2"])
        pid = self._playlist("Favourites", "jellyfin", ids)
        target = lambda: self._target(main.subsonic_client, {"s-fav": ("Favourites", entries)})
        preview = self._preview(target=target()).get_json()
        self.assertEqual(preview["carried"]["count"], 0)
        resp, *_ = self._switch(target=target(), carry=preview["carried"]["ids"])
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self._row(pid))

    def test_a_target_playlist_that_already_has_a_row_is_not_paired(self):
        # Re-keying onto it would collide with the unique (provider, id) index.
        pid, _ids, entries = self._party()
        stale = self._playlist("Other", "subsonic", [])
        self.conn.execute("UPDATE playlists SET source_playlist_id = 's-party' WHERE id = ?", (stale,))
        self.conn.commit()
        target = self._target(main.subsonic_client, {"s-party": ("Party Mix", entries)})
        preview = self._preview(target=target).get_json()
        self.assertEqual(preview["carried"]["count"], 0)
        resp, *_ = self._switch(target=self._target(main.subsonic_client, {"s-party": ("Party Mix", entries)}),
                                carry=preview["carried"]["ids"])
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertIsNone(self._row(pid))

    def test_duplicate_namesakes_are_left_unmatched(self):
        pid, _ids, entries = self._party()
        target = self._target(main.subsonic_client, {
            "s-a": ("Party Mix", entries), "s-b": ("party mix", entries)})
        resp, *_ = self._switch(target=target)
        self.assertEqual(resp.get_json()["carried"]["count"], 0)
        self.assertIsNone(self._row(pid))

    def test_a_namesake_of_another_owner_is_not_carried_over(self):
        # The old playlist is the owner's own; Subsonic's listing has no owner.
        pid, _ids, entries = self._party(owner=self.owner)
        target = self._target(main.subsonic_client, {"s-party": ("Party Mix", entries)})
        resp, *_ = self._switch(target=target)
        self.assertEqual(resp.get_json()["carried"]["count"], 0)
        self.assertIsNone(self._row(pid))

    def test_a_users_own_playlist_is_carried_over_to_that_users_namesake(self):
        pid, _ids, entries = self._party(owner=self.owner)
        self.conn.execute("UPDATE users SET emby_user_id = 'emby-owner' WHERE id = ?", (self.owner,))
        self.conn.commit()
        body = {"provider": "emby", "url": "http://emby.local", "api_key": "k", "username": "u"}
        target = lambda: self._target(main.emby_client, {"e-party": ("Party Mix", entries)},
                                      per_user={"emby-owner": ["e-party"]})
        with mock.patch.object(main.emby_client, "_resolve_user_id", return_value="emby-admin"):
            preview = self._preview(body, target=target()).get_json()
        self.assertEqual(preview["carried"]["count"], 1)
        with mock.patch.object(main.emby_client, "test_connection", return_value={"state": "paired"}), \
             mock.patch.object(main.emby_client, "_resolve_user_id", return_value="emby-admin"), \
             mock.patch.object(main.emby_client, "reconnect"), target(), \
             mock.patch.object(main.playlist_sync, "start_sync", return_value={"status": "started"}):
            resp = self.client.post("/api/admin/provider/switch",
                                    json={**body, "carry": preview["carried"]["ids"]})
        self.assertEqual(resp.get_json()["carried"]["count"], 1)
        self.assertEqual(self._row(pid)["source_playlist_id"], "e-party")

    def test_the_preview_reads_the_target_with_the_typed_connection_only(self):
        seen = []

        def list_playlists(user_id=None):
            seen.append(main.subsonic_client._current_config())
            return {"status": "ok", "playlists": []}

        self._party()
        with mock.patch.object(main.subsonic_client, "list_playlists", side_effect=list_playlists):
            self.client.post("/api/admin/provider/switch/preview", json=self._SUBSONIC)
        self.assertEqual(seen, [("http://nav.local", "u", "p")])
        # Outside the request, the stored (empty) connection again.
        self.assertEqual(main.subsonic_client._current_config(), ("", "", ""))

    def test_an_unreadable_target_carries_nothing_over_and_says_why(self):
        pid, _ids, _entries = self._party()
        preview = self._preview(target=self._target(main.subsonic_client, listing_ok=False)).get_json()
        self.assertEqual(preview["carried"], {"count": 0, "titles": [], "ids": [], "status": "listing_failed"})
        self.assertEqual(preview["removed"]["count"], 3)
        resp, *_ = self._switch(target=self._target(main.subsonic_client, listing_ok=False))
        self.assertIsNone(self._row(pid))

    def test_roon_and_filesystem_targets_carry_nothing_over(self):
        self._party()
        preview = self._preview({"provider": "filesystem"}).get_json()
        self.assertEqual(preview["carried"]["status"], "already_listed")
        preview = self._preview({"provider": "roon", "host": "core.local", "port": "9330"}).get_json()
        self.assertEqual(preview["carried"]["status"], "not_listable")
        # Music Assistant, a provider now, is listed under every provider too.
        preview = self._preview({"provider": "music_assistant", "url": "http://ma.local:8095",
                                 "token": "t"}).get_json()
        self.assertEqual(preview["carried"]["status"], "already_listed")


class _Patches:
    """Patches a client's list_playlists and get_playlist_tracks together."""

    def __init__(self, client, list_playlists, get_playlist_tracks):
        self._patches = [mock.patch.object(client, "list_playlists", side_effect=list_playlists),
                         mock.patch.object(client, "get_playlist_tracks", side_effect=get_playlist_tracks)]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


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


class AdminConfigMirrorPlexTests(_RouteTestBase):
    """The Plex mirror target: its own URL and token, saved together,
    cleared together, echoed back with its state, and the sink toggle."""

    def _answer(self, status=200):
        resp = mock.Mock()
        resp.status_code = status
        resp.content = b'{"MediaContainer": {}}'
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"MediaContainer": {}}
        return resp

    def test_url_and_token_are_saved_and_the_state_reported(self):
        _login(self.client, self.admin)
        with mock.patch("requests.request", return_value=self._answer()):
            resp = self.client.put("/api/admin/config", json={
                "mirror_plex_url": " http://plex.example.com:32400 ", "mirror_plex_token": "tok"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_mirror_plex_config(), ("http://plex.example.com:32400", "tok"))
        body = resp.get_json()
        self.assertEqual((body["mirror_plex_url"], body["mirror_plex_token"], body["mirror_plex_state"]),
                         ("http://plex.example.com:32400", "tok", "paired"))

    def test_blanking_both_clears_it(self):
        db.set_config(self.conn, "mirror_plex_url", "http://plex.example.com:32400")
        db.set_config(self.conn, "mirror_plex_token", "tok")
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch("requests.request") as request:
            resp = self.client.put("/api/admin/config", json={"mirror_plex_url": "", "mirror_plex_token": ""})
        self.assertEqual(resp.status_code, 200)
        request.assert_not_called()
        self.assertIsNone(db.get_mirror_plex_config())

    def test_one_field_alone_changes_nothing(self):
        _login(self.client, self.admin)
        self.client.put("/api/admin/config", json={"mirror_plex_url": "http://plex.example.com:32400"})
        self.assertIsNone(db.get_config(self.conn, "mirror_plex_url"))

    def test_the_sink_toggle_accepts_plex(self):
        cur = self.conn.execute("INSERT INTO playlists (title) VALUES ('Mix')")
        pid = cur.lastrowid
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.post(f"/api/provider/playlists/{pid}/mirror", json={"enabled": True, "sink": "plex"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["plex_mirror_enabled"])
        self.assertEqual(body["plex_mirror_last_error_code"], "unset_target")
        listed = [p for p in self.client.get("/api/provider/playlists").get_json() if p["id"] == pid][0]
        self.assertEqual((listed["plex_mirror_enabled"], listed["plex_mirror_configured"]), (True, False))

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



class AdminConfigPlaylistSyncIntervalTests(_RouteTestBase):
    """PUT/GET /api/admin/config's playlist_sync_interval_hours, the same
    0-to-a-year range as scan_interval_hours, 0 being off."""

    def test_a_fresh_install_reports_six_hours(self):
        _login(self.client, self.admin)
        resp = self.client.get("/api/admin/config")
        self.assertEqual(resp.get_json()["playlist_sync_interval_hours"], 6)

    def test_zero_is_accepted_as_off(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"playlist_sync_interval_hours": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "playlist_sync_interval_hours"), "0")

    def test_accepts_a_valid_interval(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"playlist_sync_interval_hours": 24})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "playlist_sync_interval_hours"), "24")

    def test_rejects_bad_values_and_names_the_field(self):
        _login(self.client, self.admin)
        for bad in (-1, 8761, "soon"):
            resp = self.client.put("/api/admin/config", json={"playlist_sync_interval_hours": bad})
            self.assertEqual(resp.status_code, 400, bad)
            self.assertEqual(resp.get_json()["field"], "playlist_sync_interval_hours")
        self.assertEqual(db.get_config(self.conn, "playlist_sync_interval_hours"), "6")

    def test_next_sync_is_none_until_something_has_synced(self):
        _login(self.client, self.admin)
        self.assertIsNone(self.client.get("/api/admin/config").get_json()["next_scheduled_playlist_sync_at"])
        self.conn.execute("INSERT INTO jobs (type, state, finished_at) VALUES (?, 'done', datetime('now'))",
                          (main.playlist_sync.JOB_TYPE,))
        self.conn.commit()
        self.assertIsNotNone(self.client.get("/api/admin/config").get_json()["next_scheduled_playlist_sync_at"])

    def test_next_sync_is_none_when_off(self):
        self.conn.execute("INSERT INTO jobs (type, state, finished_at) VALUES (?, 'done', datetime('now'))",
                          (main.playlist_sync.JOB_TYPE,))
        db.set_config(self.conn, "playlist_sync_interval_hours", "0")
        self.conn.commit()
        _login(self.client, self.admin)
        self.assertIsNone(self.client.get("/api/admin/config").get_json()["next_scheduled_playlist_sync_at"])


class ScheduledPlaylistSyncHookTests(_RouteTestBase):
    """main._maybe_schedule_playlist_sync, the jobs.on_idle hook."""

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "provider", "jellyfin")
        db.set_config(self.conn, "setup_completed", "1")
        self.conn.commit()

    def _queued(self):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM jobs WHERE type = ? AND state = 'queued'",
            (main.playlist_sync.JOB_TYPE,))]

    def test_an_install_older_than_the_wizard_runs_its_schedule_once_migrated(self):
        self.conn.execute("DELETE FROM app_config WHERE key = 'setup_completed'")
        self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                          "VALUES ('a.flac', 'A', 'B', 'T', 1, 0)")
        self.conn.commit()
        main._maybe_schedule_playlist_sync()
        self.assertEqual(self._queued(), [], "the fault this fixes")
        db._migrate_setup_completed_for_old_installs(self.conn)
        self.conn.commit()
        main._maybe_schedule_playlist_sync()
        self.assertEqual(self._queued(), [{"provider_id": "jellyfin"}])

    def test_due_queues_a_sync_of_the_active_provider(self):
        main._maybe_schedule_playlist_sync()
        self.assertEqual(self._queued(), [{"provider_id": "jellyfin"}])

    def test_nothing_before_setup_has_finished(self):
        db.set_config(self.conn, "setup_completed", None)
        self.conn.commit()
        main._maybe_schedule_playlist_sync()
        self.assertEqual(self._queued(), [])

    def test_nothing_when_off(self):
        db.set_config(self.conn, "playlist_sync_interval_hours", "0")
        self.conn.commit()
        main._maybe_schedule_playlist_sync()
        self.assertEqual(self._queued(), [])

    def test_nothing_when_not_yet_due(self):
        self.conn.execute("INSERT INTO jobs (type, state, finished_at) VALUES (?, 'done', datetime('now'))",
                          (main.playlist_sync.JOB_TYPE,))
        self.conn.commit()
        main._maybe_schedule_playlist_sync()
        self.assertEqual(self._queued(), [])

    def test_does_not_stack_on_a_queued_sync(self):
        main._maybe_schedule_playlist_sync()
        main._maybe_schedule_playlist_sync()
        self.assertEqual(len(self._queued()), 1)

    def test_skipped_while_a_provider_switch_holds_the_lock(self):
        with main._PROVIDER_SWITCH_LOCK:
            main._maybe_schedule_playlist_sync()
        self.assertEqual(self._queued(), [])
        self.assertFalse(main._PROVIDER_SWITCH_LOCK.locked())

    def test_is_registered_as_an_idle_hook(self):
        self.assertIn(main._maybe_schedule_playlist_sync, main.jobs._idle_hooks)



class SyncAfterScanTests(_RouteTestBase):
    """main._sync_after_scan and the pending flag _maybe_schedule_playlist_sync
    acts on: one playlist sync after a scan that changed the library."""

    CHANGED = {"added": 1, "updated": 0, "removed": 0, "unchanged": 10}

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "provider", "jellyfin")
        db.set_config(self.conn, "setup_completed", "1")
        # The schedule is off, so every sync here comes from the scan.
        db.set_config(self.conn, "playlist_sync_interval_hours", "0")
        self.conn.commit()

    def _jobs(self, state):
        return self.conn.execute("SELECT COUNT(*) FROM jobs WHERE type = ? AND state = ?",
                                 (main.playlist_sync.JOB_TYPE, state)).fetchone()[0]

    def _pending(self):
        return db.get_config(self.conn, main._SYNC_AFTER_SCAN_PENDING)

    def _add_sync(self, state):
        self.conn.execute("INSERT INTO jobs (type, state, dedupe_key) VALUES (?, ?, ?)",
                          (main.playlist_sync.JOB_TYPE, state, main.playlist_sync.JOB_DEDUPE))
        self.conn.commit()

    def test_a_scan_that_changed_tracks_queues_one_sync(self):
        main._sync_after_scan(self.CHANGED)
        self.assertEqual(self._jobs("queued"), 1)
        self.assertIsNone(self._pending())

    def test_removed_or_updated_tracks_count_as_a_change(self):
        for k in ("updated", "removed"):
            self.conn.execute("DELETE FROM jobs")
            self.conn.commit()
            main._sync_after_scan({"added": 0, "updated": 0, "removed": 0, k: 2})
            self.assertEqual(self._jobs("queued"), 1, k)

    def test_a_scan_that_changed_nothing_asks_for_nothing(self):
        main._sync_after_scan({"added": 0, "updated": 0, "removed": 0, "unchanged": 10})
        self.assertEqual(self._jobs("queued"), 0)
        self.assertIsNone(self._pending())

    def test_nothing_when_turned_off(self):
        db.set_config(self.conn, "playlist_sync_after_scan", "0")
        self.conn.commit()
        main._sync_after_scan(self.CHANGED)
        self.assertEqual(self._jobs("queued"), 0)
        self.assertIsNone(self._pending())

    def test_nothing_before_setup_has_finished(self):
        db.set_config(self.conn, "setup_completed", None)
        self.conn.commit()
        main._sync_after_scan(self.CHANGED)
        self.assertEqual(self._jobs("queued"), 0)

    def test_an_already_queued_sync_counts_as_the_one_asked_for(self):
        self._add_sync("queued")
        main._sync_after_scan(self.CHANGED)
        self.assertEqual(self._jobs("queued"), 1)
        self.assertIsNone(self._pending())

    def test_a_running_sync_is_followed_by_another_once_it_ends(self):
        # It may have read the tracks before the scan wrote them.
        self._add_sync("running")
        main._sync_after_scan(self.CHANGED)
        self.assertEqual(self._jobs("queued"), 0)
        self.assertEqual(self._pending(), "1")
        main._maybe_schedule_playlist_sync()  # still running: keep waiting
        self.assertEqual(self._jobs("queued"), 0)
        self.conn.execute("UPDATE jobs SET state = 'done', finished_at = datetime('now'), "
                          "dedupe_key = NULL")
        self.conn.commit()
        main._maybe_schedule_playlist_sync()
        self.assertEqual(self._jobs("queued"), 1)
        self.assertIsNone(self._pending())

    def test_the_flag_waits_out_a_provider_switch(self):
        with main._PROVIDER_SWITCH_LOCK:
            main._sync_after_scan(self.CHANGED)
        self.assertEqual(self._jobs("queued"), 0)
        self.assertEqual(self._pending(), "1")
        main._maybe_schedule_playlist_sync()
        self.assertEqual(self._jobs("queued"), 1)

    def test_the_scan_job_asks_after_running_and_keeps_its_result(self):
        self.assertIs(main.jobs._HANDLERS[main.scanner.JOB_TYPE], main._run_library_scan)
        with mock.patch.object(main.scanner, "run_job", return_value=self.CHANGED), \
             mock.patch.object(main, "_sync_after_scan") as after:
            self.assertEqual(main._run_library_scan({}, None), self.CHANGED)
        after.assert_called_once_with(self.CHANGED)

    def test_a_failure_after_the_scan_never_fails_the_scan(self):
        with mock.patch.object(main.scanner, "run_job", return_value=self.CHANGED), \
             mock.patch.object(main, "_sync_after_scan", side_effect=RuntimeError("db gone")):
            self.assertEqual(main._run_library_scan({}, None), self.CHANGED)

    def test_the_setting_is_on_by_default_and_can_be_turned_off(self):
        _login(self.client, self.admin)
        self.assertTrue(self.client.get("/api/admin/config").get_json()["playlist_sync_after_scan"])
        resp = self.client.put("/api/admin/config", json={"playlist_sync_after_scan": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(self.conn, "playlist_sync_after_scan"), "0")
        self.assertFalse(self.client.get("/api/admin/config").get_json()["playlist_sync_after_scan"])



class MirrorCoverageRouteTests(_RouteTestBase):
    """Mirror coverage (mirror_runs) in the admin overview, its Show list,
    and the playlist rows' tooltip counts."""

    def setUp(self):
        super().setUp()
        self.pid = self._make_playlist("Road Trip")
        self.conn.execute("UPDATE playlists SET jellyfin_mirror_enabled = 1 WHERE id = ?", (self.pid,))
        main.mirror_runs.record(self.conn, self.pid, "jellyfin", 64, 38)
        main.mirror_runs.record(self.conn, self.pid, "jellyfin", 64, 40,
                                [{"artist": "Cinder & Ash", "title": "Smoulder"}])
        self.conn.commit()

    def test_the_admin_overview_carries_each_sinks_coverage_and_history(self):
        _login(self.client, self.admin)
        rows = self.client.get("/api/admin/mirrors").get_json()["playlists"]
        row = next(r for r in rows if r["id"] == self.pid)
        self.assertEqual(set(row["coverage"]), {"jellyfin"})
        cov = row["coverage"]["jellyfin"]
        self.assertEqual((cov["on_target"], cov["total"], cov["lacked"], cov["history"]), (40, 64, 1, [38, 40]))

    def test_show_lists_the_missing_tracks_with_their_reason(self):
        _login(self.client, self.admin)
        resp = self.client.get(f"/api/admin/mirrors/{self.pid}/jellyfin/missing")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["missing"], [{"artist": "Cinder & Ash", "title": "Smoulder", "reason": "not_on_target"}])
        self.assertEqual(body["list_max"], main.mirror_runs.LIST_MAX)

    def test_show_is_admin_only_and_404s_without_a_run(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.get(f"/api/admin/mirrors/{self.pid}/jellyfin/missing").status_code, 403)
        _login(self.client, self.admin)
        self.assertEqual(self.client.get(f"/api/admin/mirrors/{self.pid}/plex/missing").status_code, 404)
        self.assertEqual(self.client.get(f"/api/admin/mirrors/{self.pid}/filesystem/missing").status_code, 404)

    def test_the_playlist_rows_carry_the_tooltip_counts(self):
        _login(self.client, self.owner)
        rows = self.client.get("/api/provider/playlists").get_json()
        rows = rows["playlists"] if isinstance(rows, dict) else rows
        row = next(r for r in rows if r["id"] == self.pid)
        self.assertEqual(row["mirror_coverage"], {"jellyfin": {"on_target": 40, "total": 64}})



class PlaylistUploadExportTests(_RouteTestBase):
    """Uploading an .m3u/.m3u8 as a playlist, and downloading any visible
    playlist as an .m3u8."""

    def setUp(self):
        super().setUp()
        self.songs = [self._track("Cinder & Ash", "Emberwake", "Smoulder", "Cinder & Ash/Emberwake/01 Smoulder.flac", 241.6),
                      self._track("Glass Meridian", "Latitude", "Two Suns", "Glass Meridian/Latitude/01 Two Suns.flac", 199)]

    def _track(self, artist, album, title, path, duration):
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, duration, size, mtime) "
            "VALUES (?, ?, ?, ?, ?, 1000, 0)", (path, artist, album, title, duration))
        self.conn.commit()
        return sync_state._new_id(cur)

    _FILE = ("#EXTM3U\n#EXTINF:241,Cinder & Ash - Smoulder\n/music/Cinder & Ash/Emberwake/01 Smoulder.flac\n"
             "#EXTINF:10,Nobody - Nothing\n/music/Nobody/None/01 Nothing.flac\n"
             "#EXTINF:199,Glass Meridian - Two Suns\n/music/Glass Meridian/Latitude/01 Two Suns.flac\n")

    def _upload(self, data: bytes, name="Road Trip.m3u8"):
        return self.client.post("/api/provider/playlists/upload",
                                data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")

    def _row(self, pid):
        return self.conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()

    def test_the_uploader_deletes_an_upload_and_its_device_selection(self):
        # #287: an upload had no way out but uploading a file of the same name.
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        device, _ = sync_state.create_device(self.conn, self.owner, "Phone")
        sync_state.create_selection(self.conn, "playlist", str(pid), self.owner, [device])
        self.conn.commit()
        self.assertEqual(self.client.delete(f"/api/provider/playlists/{pid}").status_code, 200)
        self.assertIsNone(self._row(pid))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM selections WHERE type = 'playlist' AND target = ?", (str(pid),)).fetchone()[0], 0)

    def test_only_the_uploader_or_an_admin_deletes_an_upload(self):
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        _login(self.client, self.other)
        self.assertEqual(self.client.delete(f"/api/provider/playlists/{pid}").status_code, 403)
        self.assertIsNotNone(self._row(pid))
        _login(self.client, self.admin)
        self.assertEqual(self.client.delete(f"/api/provider/playlists/{pid}").status_code, 200)
        self.assertIsNone(self._row(pid))

    def test_the_app_api_still_deletes_only_playlists_made_in_trobar(self):
        # The browser's change; the App API's delete is left as it was.
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        _, token = sync_state.create_device(self.conn, self.owner, "Owner's phone", "phone")
        self.conn.commit()
        resp = self.client.delete(f"/api/app/playlists/{pid}", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 400)
        self.assertIsNotNone(self._row(pid))

    def test_an_upload_becomes_a_private_playlist_of_the_uploader(self):
        _login(self.client, self.owner)
        resp = self._upload(self._FILE.encode())
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual((body["title"], body["tracks"], body["matched"], body["replaced"]),
                         ("Road Trip", 3, 2, False))
        row = self._row(body["playlist_id"])
        self.assertEqual((row["title"], row["source_provider"], row["owner_user_id"], row["shared"]),
                         ("Road Trip", "upload", self.owner, 0))
        matched = [r["matched_track_id"] for r in self.conn.execute(
            "SELECT matched_track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
            (body["playlist_id"],))]
        self.assertEqual(matched, [self.songs[0], None, self.songs[1]])
        _login(self.client, self.other)
        rows = self.client.get("/api/provider/playlists").get_json()
        rows = rows["playlists"] if isinstance(rows, dict) else rows
        self.assertNotIn(body["playlist_id"], [r["id"] for r in rows])

    def test_the_unmatched_entry_goes_to_the_unresolved_review(self):
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        resp = self.client.get(f"/api/provider/playlists/{pid}/unresolved-tracks").get_json()
        items = resp["tracks"] if isinstance(resp, dict) and "tracks" in resp else resp
        self.assertIn("Nothing", json.dumps(items))

    def test_the_same_file_name_again_replaces_its_entries(self):
        _login(self.client, self.owner)
        first = self._upload(self._FILE.encode()).get_json()
        second = self._upload(b"/music/Glass Meridian/Latitude/01 Two Suns.flac\n").get_json()
        self.assertEqual((second["playlist_id"], second["replaced"], second["tracks"]), (first["playlist_id"], True, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM playlists WHERE source_provider = 'upload'").fetchone()[0], 1)

    def test_two_users_uploading_the_same_name_get_their_own_playlists(self):
        _login(self.client, self.owner)
        a = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        _login(self.client, self.other)
        b = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        self.assertNotEqual(a, b)
        self.assertEqual((self._row(a)["owner_user_id"], self._row(b)["owner_user_id"]), (self.owner, self.other))

    def test_bad_uploads_are_refused(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.post("/api/provider/playlists/upload", data={},
                                          content_type="multipart/form-data").status_code, 400)
        self.assertEqual(self._upload(self._FILE.encode(), "notes.txt").status_code, 400)
        self.assertEqual(self._upload(b"#EXTM3U\n# nothing here\n").status_code, 400)
        too_many = ("/music/x.flac\n" * (main._DEVICE_PLAYLIST_ENTRIES_MAX + 1)).encode()
        self.assertEqual(self._upload(too_many).status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0], 0)

    def test_a_path_in_the_file_name_is_dropped(self):
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode(), "../../etc/Road Trip.m3u").get_json()["playlist_id"]
        self.assertEqual(self._row(pid)["title"], "Road Trip")

    def test_the_export_lists_matched_tracks_in_order_relative_to_the_library(self):
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        resp = self.client.get(f"/api/provider/playlists/{pid}/export.m3u8")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "audio/x-mpegurl")
        self.assertIn('filename="Road Trip.m3u8"', resp.headers["Content-Disposition"])
        self.assertEqual(resp.get_data(as_text=True),
                         "#EXTM3U\n"
                         "#EXTINF:241,Cinder & Ash - Smoulder\nCinder & Ash/Emberwake/01 Smoulder.flac\n"
                         "#EXTINF:199,Glass Meridian - Two Suns\nGlass Meridian/Latitude/01 Two Suns.flac\n")

    def test_the_export_can_keep_unresolved_entries_as_comments(self):
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        text = self.client.get(f"/api/provider/playlists/{pid}/export.m3u8?unresolved=1").get_data(as_text=True)
        self.assertEqual(text.splitlines()[3], "# Nobody - Nothing")

    def test_a_track_since_removed_is_left_out_like_an_unmatched_one(self):
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE id = ?", (self.songs[1],))
        self.conn.commit()
        text = self.client.get(f"/api/provider/playlists/{pid}/export.m3u8").get_data(as_text=True)
        self.assertNotIn("Two Suns", text)

    def test_a_line_break_in_a_tag_cannot_start_a_line(self):
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        self.conn.execute("UPDATE tracks SET title = ? WHERE id = ?", ("Smoulder\n/etc/passwd", self.songs[0]))
        self.conn.commit()
        lines = self.client.get(f"/api/provider/playlists/{pid}/export.m3u8").get_data(as_text=True).splitlines()
        self.assertNotIn("/etc/passwd", lines)
        self.assertIn("#EXTINF:241,Cinder & Ash - Smoulder /etc/passwd", lines)

    def test_only_a_visible_playlist_can_be_downloaded(self):
        _login(self.client, self.owner)
        pid = self._upload(self._FILE.encode()).get_json()["playlist_id"]
        _login(self.client, self.other)
        self.assertIn(self.client.get(f"/api/provider/playlists/{pid}/export.m3u8").status_code, (403, 404))
        _login(self.client, self.admin)
        self.assertEqual(self.client.get(f"/api/provider/playlists/{pid}/export.m3u8").status_code, 200)

    def test_any_visible_playlist_exports_not_only_uploads(self):
        shared = self._make_playlist("Kitchen", owner_user_id=None, shared=1)
        self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
                          "VALUES (?, 0, 'a', 't', ?)", (shared, self.songs[1]))
        self.conn.commit()
        _login(self.client, self.other)
        text = self.client.get(f"/api/provider/playlists/{shared}/export.m3u8").get_data(as_text=True)
        self.assertIn("Glass Meridian/Latitude/01 Two Suns.flac", text)


class TrobarPlaylistTests(_RouteTestBase):
    """Playlists made in Trobar: create, fill from the library, reorder,
    rename, delete; owner or admin, and only on a playlist made here."""

    def setUp(self):
        super().setUp()
        self.t = [self._track(f"Song {i}") for i in range(3)]

    def _track(self, title):
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES (?, 'Artist', 'Album', ?, 1000, 0)", (f"Artist/Album/{title}.flac", title))
        self.conn.commit()
        return sync_state._new_id(cur)

    def _create(self, title="Road Trip"):
        resp = self.client.post("/api/provider/playlists", json={"title": title})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()["id"]

    def _tracks_of(self, pid):
        return [r["matched_track_id"] for r in self.conn.execute(
            "SELECT matched_track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (pid,))]

    def _row(self, pid):
        return self.conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()

    def test_a_new_playlist_is_empty_private_and_owned_by_its_maker(self):
        _login(self.client, self.owner)
        pid = self._create("  Road Trip  ")
        row = self._row(pid)
        self.assertEqual((row["title"], row["source_provider"], row["owner_user_id"], row["shared"]),
                         ("Road Trip", "trobar", self.owner, 0))
        self.assertEqual(self._tracks_of(pid), [])
        _login(self.client, self.other)
        rows = self.client.get("/api/provider/playlists").get_json()
        rows = rows["playlists"] if isinstance(rows, dict) else rows
        self.assertNotIn(pid, [r["id"] for r in rows])

    def test_a_name_is_required_and_bounded(self):
        _login(self.client, self.owner)
        for body in ({}, {"title": "   "}, {"title": 7}, {"title": "x" * (web_common._PLAYLIST_TITLE_MAX + 1)}):
            self.assertEqual(self.client.post("/api/provider/playlists", json=body).status_code, 400, body)

    def test_put_sets_the_tracks_in_order_and_post_appends(self):
        _login(self.client, self.owner)
        pid = self._create()
        resp = self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[2], self.t[0], self.t[2]]})
        body = resp.get_json()
        self.assertEqual((body["status"], body["tracks"], body["added"]), ("ok", 3, 3))
        self.assertEqual([e["track_id"] for e in body["entries"]], [self.t[2], self.t[0], self.t[2]],
                         "a write returns the entries as they now are")
        self.assertEqual(self._tracks_of(pid), [self.t[2], self.t[0], self.t[2]])
        resp = self.client.post(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[1]]})
        self.assertEqual((resp.get_json()["tracks"], resp.get_json()["added"]), (4, 1))
        self.assertEqual(self._tracks_of(pid), [self.t[2], self.t[0], self.t[2], self.t[1]])
        self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[1]]})
        self.assertEqual(self._tracks_of(pid), [self.t[1]], "a PUT also removes")
        entry = self.conn.execute("SELECT artist, title FROM playlist_tracks WHERE playlist_id = ?", (pid,)).fetchone()
        self.assertEqual((entry["artist"], entry["title"]), ("Artist", "Song 1"))

    def test_only_library_tracks_can_be_added(self):
        _login(self.client, self.owner)
        pid = self._create()
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE id = ?", (self.t[1],))
        self.conn.commit()
        resp = self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0], self.t[1], 999999]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["unknown"], [self.t[1], 999999])
        self.assertEqual(self._tracks_of(pid), [], "nothing is written")
        for body in ({}, {"track_ids": "1"}, {"track_ids": [True]}, {"track_ids": ["1"]}):
            self.assertEqual(self.client.put(f"/api/provider/playlists/{pid}/tracks", json=body).status_code, 400, body)

    def test_the_track_count_is_capped(self):
        _login(self.client, self.owner)
        pid = self._create()
        with mock.patch.object(main.playlist_sync, "TROBAR_TRACKS_MAX", 2):
            resp = self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": self.t})
        self.assertEqual(resp.status_code, 400)

    def test_an_edit_rewrites_the_mirrors(self):
        _login(self.client, self.owner)
        pid = self._create()
        with mock.patch.object(main.playlist_sync, "write_playlist_mirrors") as write:
            self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0]]})
            self.client.patch(f"/api/provider/playlists/{pid}", json={"title": "Renamed"})
        self.assertEqual([c.args[1] for c in write.call_args_list], [pid, pid])

    def test_rename_and_sharing_in_one_patch(self):
        _login(self.client, self.owner)
        pid = self._create()
        resp = self.client.patch(f"/api/provider/playlists/{pid}", json={"title": "Renamed", "shared": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((self._row(pid)["title"], self._row(pid)["shared"]), ("Renamed", 1))

    def test_only_the_owner_or_an_admin_may_edit(self):
        _login(self.client, self.owner)
        pid = self._create()
        _login(self.client, self.other)
        self.assertEqual(self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0]]}).status_code, 403)
        self.assertEqual(self.client.patch(f"/api/provider/playlists/{pid}", json={"title": "Mine"}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/provider/playlists/{pid}").status_code, 403)
        _login(self.client, self.admin)
        self.assertEqual(self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0]]}).status_code, 200)
        self.assertEqual(self.client.put("/api/provider/playlists/999999/tracks", json={"track_ids": []}).status_code, 404)

    def test_a_providers_playlist_is_not_edited_here(self):
        pid = self._make_playlist("From Roon", owner_user_id=self.owner)
        _login(self.client, self.owner)
        self.assertEqual(self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0]]}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/provider/playlists/{pid}", json={"title": "Renamed"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/provider/playlists/{pid}").status_code, 400)
        self.assertEqual(self._row(pid)["title"], "From Roon")
        self.assertEqual(self.client.patch(f"/api/provider/playlists/{pid}", json={"shared": False}).status_code, 200,
                         "the sharing toggle still works on any owned playlist")

    def _album_track(self, album, title, disc, no):
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, disc_no, track_no) "
            "VALUES (?, 'Glass Meridian', ?, ?, 1000, 0, ?, ?)", (f"GM/{album}/{title}.flac", album, title, disc, no))
        self.conn.commit()
        return sync_state._new_id(cur)

    def test_post_appends_whole_albums_in_their_own_order(self):
        b2 = self._album_track("Latitude", "Northbound", 1, 3)
        a1 = self._album_track("Latitude", "Two Suns", 1, 1)
        c1 = self._album_track("Latitude", "Bonus", 2, 1)
        a2 = self._album_track("Latitude", "Latitude", 1, 2)
        other = self._album_track("Other", "Elsewhere", 1, 1)
        _login(self.client, self.owner)
        pid = self._create()
        self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0]]})
        resp = self.client.post(f"/api/provider/playlists/{pid}/tracks", json={"albums": [
            {"artist": "Glass Meridian", "album": "Latitude"}, {"artist": "Glass Meridian", "album": "Other"}]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual((resp.get_json()["added"], resp.get_json()["tracks"]), (5, 6))
        self.assertEqual(self._tracks_of(pid), [self.t[0], a1, a2, b2, c1, other])

    def test_albums_are_for_appending_and_must_be_well_formed(self):
        _login(self.client, self.owner)
        pid = self._create()
        for body in ({"albums": "Latitude"}, {"albums": [{"artist": "A"}]}, {"albums": [["A", "B"]]},
                     {"albums": [{"artist": "A", "album": "B"}] * (web_common._PLAYLIST_ALBUMS_MAX + 1)}):
            self.assertEqual(self.client.post(f"/api/provider/playlists/{pid}/tracks", json=body).status_code, 400, body)
        self.assertEqual(self.client.put(f"/api/provider/playlists/{pid}/tracks",
                                         json={"albums": [{"artist": "A", "album": "B"}]}).status_code, 400)
        resp = self.client.post(f"/api/provider/playlists/{pid}/tracks",
                                json={"albums": [{"artist": "Nobody", "album": "Nothing"}]})
        self.assertEqual((resp.status_code, resp.get_json()["added"]), (200, 0))

    def _remove_from_library(self, track_id):
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE id = ?", (track_id,))
        self.conn.commit()

    def test_appending_after_a_track_left_the_library_keeps_it_in_place(self):
        _login(self.client, self.owner)
        pid = self._create()
        self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0], self.t[1]]})
        self._remove_from_library(self.t[0])
        resp = self.client.post(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[2]]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        entries = resp.get_json()["entries"]
        self.assertEqual([(e["track_id"], e["title"]) for e in entries],
                         [(None, "Song 0"), (self.t[1], "Song 1"), (self.t[2], "Song 2")])
        self.assertEqual(self._tracks_of(pid), [self.t[0], self.t[1], self.t[2]],
                         "the removed track's link is kept, so it comes back if the file does")

    def test_an_editor_reorder_after_a_track_left_the_library_works(self):
        _login(self.client, self.owner)
        pid = self._create()
        self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0], self.t[1]]})
        self._remove_from_library(self.t[0])
        entries = self.client.get(f"/api/provider/playlists/{pid}/tracks").get_json()["tracks"]
        self.assertEqual([e["track_id"] for e in entries], [None, self.t[1]])
        items = [{"track_id": e["track_id"]} if e["track_id"] else {"entry_id": e["entry_id"]} for e in entries]
        resp = self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"items": items[::-1]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual([(e["track_id"], e["title"]) for e in resp.get_json()["entries"]],
                         [(self.t[1], "Song 1"), (None, "Song 0")])
        row = self.conn.execute("SELECT artist, title, matched_track_id FROM playlist_tracks "
                                "WHERE playlist_id = ? AND position = 1", (pid,)).fetchone()
        self.assertEqual(tuple(row), ("Artist", "Song 0", self.t[0]))

    def test_an_appended_playlist_keeps_an_entry_that_never_matched(self):
        _login(self.client, self.owner)
        pid = self._create()
        self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
                          "VALUES (?, 0, 'Nobody', 'Nothing', NULL)", (pid,))
        self.conn.commit()
        self.client.post(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[1]]})
        rows = self.conn.execute("SELECT title, matched_track_id FROM playlist_tracks WHERE playlist_id = ? "
                                 "ORDER BY position", (pid,)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("Nothing", None), ("Song 1", self.t[1])])

    def test_items_must_be_well_formed_and_entries_from_this_playlist(self):
        _login(self.client, self.owner)
        pid = self._create()
        other = self._create("Other")
        self.client.put(f"/api/provider/playlists/{other}/tracks", json={"track_ids": [self.t[0]]})
        foreign = self.client.get(f"/api/provider/playlists/{other}/tracks").get_json()["tracks"][0]["entry_id"]
        resp = self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"items": [{"entry_id": foreign}]})
        self.assertEqual((resp.status_code, resp.get_json().get("unknown_entries")), (400, [foreign]))
        for items in ("x", [3], [{"track_id": "3"}], [{"track_id": 3, "entry_id": 4}], [{"other": 3}],
                      [{"track_id": True}]):
            self.assertEqual(self.client.put(f"/api/provider/playlists/{pid}/tracks",
                                             json={"items": items}).status_code, 400, items)

    def test_get_lists_the_tracks_in_order_with_their_tags(self):
        _login(self.client, self.owner)
        pid = self._create()
        self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[2], self.t[0]]})
        tracks = self.client.get(f"/api/provider/playlists/{pid}/tracks").get_json()["tracks"]
        self.assertEqual([(t["track_id"], t["title"], t["artist"], t["album"]) for t in tracks],
                         [(self.t[2], "Song 2", "Artist", "Album"), (self.t[0], "Song 0", "Artist", "Album")])

    def test_get_gives_an_unmatched_entry_its_own_tags_and_no_id(self):
        pid = self._make_playlist("From Roon", owner_user_id=self.owner)
        self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
                          "VALUES (?, 0, 'Nobody', 'Nothing', NULL), (?, 1, 'x', 'y', ?)", (pid, pid, self.t[1]))
        self.conn.commit()
        _login(self.client, self.owner)
        tracks = self.client.get(f"/api/provider/playlists/{pid}/tracks").get_json()["tracks"]
        self.assertEqual([(t["track_id"], t["artist"], t["title"]) for t in tracks],
                         [(None, "Nobody", "Nothing"), (self.t[1], "Artist", "Song 1")])

    def test_get_refuses_a_playlist_the_user_cannot_see(self):
        _login(self.client, self.owner)
        pid = self._create()
        _login(self.client, self.other)
        self.assertIn(self.client.get(f"/api/provider/playlists/{pid}/tracks").status_code, (403, 404))

    def test_delete_removes_it_and_its_device_selection(self):
        _login(self.client, self.owner)
        pid = self._create()
        self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0]]})
        device, _ = sync_state.create_device(self.conn, self.owner, "Phone")
        sync_state.create_selection(self.conn, "playlist", str(pid), self.owner, [device])
        self.conn.commit()
        self.assertEqual(self.client.delete(f"/api/provider/playlists/{pid}").status_code, 200)
        self.assertIsNone(self._row(pid))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM selections WHERE type = 'playlist'").fetchone()[0], 0)

    def test_a_provider_sync_leaves_playlists_made_here_alone(self):
        _login(self.client, self.owner)
        pid = self._create()
        self.client.put(f"/api/provider/playlists/{pid}/tracks", json={"track_ids": [self.t[0]]})

        class Empty:
            def list_playlists(self):
                return {"status": "ok", "playlists": []}

            def get_playlist_tracks(self, *_a, **_k):
                return {"status": "ok", "tracks": []}

        main.playlist_sync.sync_playlists(Empty(), "jellyfin")
        self.assertIsNotNone(self._row(pid))
        self.assertEqual(self._tracks_of(pid), [self.t[0]])


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

    def test_an_admin_retry_runs_a_failed_full_rescan_full_again(self):
        # Only the automatic retries are made incremental (scanner.retry_payload);
        # an admin retrying a failed full rescan asked for a full one.
        failed = self._add_job(job_type="library_scan", state="failed", attempts=3)
        self.conn.execute("UPDATE jobs SET payload = ? WHERE id = ?",
                          ('{"force": true, "confirm_mass_removal": false}', failed))
        self.conn.commit()
        _login(self.client, self.admin)
        resp = self.client.post(f"/api/admin/jobs/{failed}/retry",
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT payload FROM jobs WHERE id = ?", (failed,)).fetchone()[0])["force"], True)

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

    def test_cancel_asks_a_running_job_to_stop_and_keeps_its_row(self):
        # The handler stops itself at its next report; the row has to stay
        # until it does, or the worker would finish a job that no longer exists.
        job_id = self._add_job(state="running", attempts=1)
        self.addCleanup(jobs._cancel_requests.pop, job_id, None)
        _login(self.client, self.admin)
        resp = self.client.delete(f"/api/admin/jobs/{job_id}",
                                  headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json(), {"status": "cancelling"})
        self.assertEqual(jobs._cancel_requests.get(job_id), jobs.CANCEL_ADMIN)
        self.assertEqual(
            self.conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0],
            "running")
        listed = self.client.get("/api/admin/jobs").get_json()["jobs"]
        self.assertTrue(next(j for j in listed if j["id"] == job_id)["cancelling"])

    def test_cancel_of_a_queued_job_asks_nothing_to_stop(self):
        job_id = self._add_job(state="queued")
        _login(self.client, self.admin)
        self.client.delete(f"/api/admin/jobs/{job_id}", headers={"Origin": "http://localhost"})
        self.assertNotIn(job_id, jobs._cancel_requests)

    def test_cancel_is_admin_only(self):
        job_id = self._add_job(state="queued")
        _login(self.client, self.owner)
        resp = self.client.delete(f"/api/admin/jobs/{job_id}",
                                  headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone())


class SigtermTests(unittest.TestCase):
    def test_sigterm_stops_the_jobs_then_exits_cleanly(self):
        with mock.patch.object(main.jobs, "shutdown", return_value=True) as shutdown:
            with self.assertRaises(SystemExit) as raised:
                main._on_sigterm(15, None)
        shutdown.assert_called_once_with(timeout=5.0)
        self.assertEqual(raised.exception.code, 0)

    def test_a_job_still_running_is_logged_and_the_server_still_exits(self):
        with mock.patch.object(main.jobs, "shutdown", return_value=False), \
                self.assertLogs("main", level="WARNING") as logs, \
                self.assertRaises(SystemExit):
            main._on_sigterm(15, None)
        self.assertIn("queued again at the next start", logs.output[0])


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


    def _music_root(self):
        conn = db.get_conn()
        try:
            return db.get_config(conn, "music_root")
        finally:
            conn.close()

    def test_a_rerun_checks_the_music_folder_without_saving_it(self):
        other = Path(tempfile.mkdtemp(prefix="trobar-test-setup-other-", dir=_TMP))
        _login(self.client, self.admin)
        resp = self.client.post("/api/setup/music-root", json={"path": str(other), "check_only": True},
                                headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["music_root"], str(other))
        self.assertEqual(self._music_root(), str(self._root), "unchanged until the wizard finishes")
        missing = self.client.post("/api/setup/music-root", json={"path": str(other / "nope"), "check_only": True},
                                   headers={"Origin": "http://localhost"})
        self.assertEqual(missing.status_code, 400)
        self.client.post("/api/setup/music-root", json={"path": str(other)}, headers={"Origin": "http://localhost"})
        self.assertEqual(self._music_root(), str(other), "saved without check_only")


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


class SelectionsTableRouteTests(_RouteTestBase):
    """#239: the Selections table's confirmation counts what unticking would
    delete, and unticking a box someone else's selection keeps says so."""

    def setUp(self):
        super().setUp()
        self.device, _ = sync_state.create_device(self.conn, self.owner, "phone")
        self.ids = [sync_state._new_id(self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES (?, 'Glass Meridian', 'Latitude', ?, 1, 0)", (f"lat/{n}.flac", f"Song {n}")))
            for n in (1, 2, 3)]
        self.other_track = sync_state._new_id(self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('x/1.flac', 'Glass Meridian', 'Longitude', 'Elsewhere', 1, 0)"))
        self.conn.commit()

    def _toggle(self, user, checked, target="Glass Meridian||Latitude"):
        _login(self.client, user)
        return self.client.post("/api/selections/toggle-device", json={
            "type": "album", "target": target, "device_id": self.device, "checked": checked})

    def _count(self, user, target="Glass Meridian||Latitude"):
        _login(self.client, user)
        return self.client.post("/api/selections/removal-count", json={
            "type": "album", "target": target, "device_id": self.device}).get_json()["tracks"]

    def test_the_count_is_what_nothing_else_on_the_device_still_needs(self):
        self._toggle(self.owner, True)
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded'")
        self.conn.commit()
        self.assertEqual(self._count(self.owner), 3)
        # An artist selection on the same device needs them too: nothing goes.
        sync_state.create_selection(self.conn, "artist", "Glass Meridian", self.owner, [self.device])
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded'")
        self.conn.commit()
        self.assertEqual(self._count(self.owner), 0)

    def test_only_tracks_on_the_device_count(self):
        self._toggle(self.owner, True)  # still pending: nothing to delete yet
        self.assertEqual(self._count(self.owner), 0)

    def test_no_selection_of_ones_own_counts_nothing(self):
        self.assertEqual(self._count(self.owner), 0)

    def _owners_links(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM selection_devices sd JOIN selections s ON s.id = sd.selection_id "
            "WHERE s.created_by_user_id = ? AND sd.device_id = ?", (self.owner, self.device)).fetchone()[0]

    def test_unticking_a_box_another_persons_selection_keeps_says_who(self):
        self._toggle(self.owner, True)
        sync_state.create_selection(self.conn, "album", "Glass Meridian||Latitude", self.admin, [self.device])
        self.assertEqual(self._owners_links(), 1)
        resp = self._toggle(self.owner, False)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["selected_by"], ["admin"])
        # The caller's own part is still removed: otherwise their link would
        # keep the tracks on the device once the other person unticks theirs.
        self.assertEqual(self._owners_links(), 0)

    def test_unticking_ones_own_box_is_fine(self):
        self._toggle(self.owner, True)
        self.assertEqual(self._toggle(self.owner, False).status_code, 200)

    def test_the_count_needs_access_to_the_device(self):
        _login(self.client, self.other)
        resp = self.client.post("/api/selections/removal-count", json={
            "type": "album", "target": "Glass Meridian||Latitude", "device_id": self.device})
        self.assertEqual(resp.status_code, 403)


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


class RememberedDestinationPruningTests(_RouteTestBase):
    """Deleting a device must not leave its id behind in any user's
    remembered picker destinations.

    users.basket_last_destinations is a JSON column, so nothing cascades
    it. A leftover id is worse than useless: the picker only renders
    checkboxes for devices that still exist, so the dead id cannot be
    seen or unchecked, but it is still pre-selected on every pick from
    that surface -- and _require_device_access then 404s the whole
    add/send with "Device not found"."""

    def setUp(self):
        super().setUp()
        self.device_a, _ = sync_state.create_device(self.conn, self.owner, "phone")
        self.device_b, _ = sync_state.create_device(self.conn, self.owner, "tablet")
        self.conn.commit()
        _login(self.client, self.owner)

    def _remember(self, surface, device_ids):
        resp = self.client.patch("/api/basket/last-destination", json={
            "surface": surface, "device_ids": device_ids})
        self.assertEqual(resp.status_code, 200)

    def _remembered(self, user_id=None):
        """Read the column directly rather than via GET /api/profile: the
        point of these tests is what is left in the DB, and a read-time
        filter added later must not be able to make them pass while the
        stored value is still poisoned."""
        row = self.conn.execute(
            "SELECT basket_last_destinations FROM users WHERE id = ?",
            (self.owner if user_id is None else user_id,),
        ).fetchone()
        raw = row["basket_last_destinations"]
        return json.loads(raw) if raw else {}

    def test_remembering_a_destination_round_trips(self):
        # Guards the tests below: if the PATCH stored nothing, every
        # "the id is gone" assertion would pass against a device that was
        # never remembered in the first place.
        self._remember("library", [self.device_a, self.device_b])
        self.assertEqual(self._remembered(), {"library": [self.device_a, self.device_b]})

    def test_deleting_a_device_prunes_it_and_leaves_its_neighbour(self):
        self._remember("library", [self.device_a, self.device_b])
        resp = self.client.delete(f"/api/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._remembered(), {"library": [self.device_b]})

    def test_the_reported_failure_a_send_after_the_delete_no_longer_carries_a_dead_id(self):
        # End to end over the API, in the order the report describes:
        # remember, delete, then stage from that surface with exactly the
        # ids the picker would pre-select. Before the fix the remembered
        # list still held the deleted id and this POST 404'd.
        self._remember("library", [self.device_a, self.device_b])
        self.client.delete(f"/api/devices/{self.device_a}")
        preselected = self._remembered()["library"]
        resp = self.client.post("/api/basket", json={
            "type": "artist", "target": "A", "device_ids": preselected})
        self.assertEqual(resp.status_code, 200)

    def test_every_surface_is_pruned_not_just_one(self):
        self._remember("library", [self.device_a])
        self._remember("suggestions", [self.device_a, self.device_b])
        self.client.delete(f"/api/devices/{self.device_a}")
        self.assertEqual(
            self._remembered(), {"library": [], "suggestions": [self.device_b]})

    def test_a_surface_that_never_named_the_device_is_untouched(self):
        self._remember("library", [self.device_b])
        self.client.delete(f"/api/devices/{self.device_a}")
        self.assertEqual(self._remembered(), {"library": [self.device_b]})

    def test_another_users_memory_of_the_device_is_pruned_too(self):
        # An admin deleting someone else's device: the pruning cannot be
        # scoped to the acting user, or the owner keeps the dead id and
        # hits the failure the deleting admin never sees.
        self._remember("library", [self.device_a])
        _login(self.client, self.admin)
        resp = self.client.delete(f"/api/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._remembered(self.owner), {"library": []})

    def test_a_malformed_column_does_not_break_the_delete(self):
        # Same tolerate-garbage contract _basket_last_destinations_dict
        # already has: a hand-edited DB must not make a device
        # undeletable.
        self.conn.execute(
            "UPDATE users SET basket_last_destinations = ? WHERE id = ?",
            ("not json at all", self.owner))
        self.conn.commit()
        resp = self.client.delete(f"/api/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 200)
        gone = self.conn.execute(
            "SELECT 1 FROM devices WHERE id = ?", (self.device_a,)).fetchone()
        self.assertIsNone(gone)

    def test_a_failed_delete_leaves_the_memory_alone(self):
        # The prune shares the delete's transaction, so a delete that
        # never happens must not prune either. self.other manages neither
        # device, so this 403s before anything is written.
        self._remember("library", [self.device_a])
        _login(self.client, self.other)
        resp = self.client.delete(f"/api/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._remembered(self.owner), {"library": [self.device_a]})


class ContainedPathTests(unittest.TestCase):
    """#56/#57/#58: every join of a stored relative path onto a root now goes
    through main._contained(), which returns None instead of a path that
    escapes the root.

    No live defect prompted this -- both writers are server-generated. The
    guard exists so the read and delete sides stop depending on that staying
    true of every future writer, which is the property app/mirror.py already
    has for its own writes and these sites did not."""

    ROOT = Path("/srv/music")

    def test_an_ordinary_relative_path_is_joined(self):
        # The control. Without it, every rejection below would pass against a
        # function that returned None unconditionally.
        self.assertEqual(main._contained(self.ROOT, "Artist/Album/01.flac"),
                         Path("/srv/music/Artist/Album/01.flac"))

    def test_a_bare_filename_is_joined(self):
        self.assertEqual(main._contained(self.ROOT, "7.png"), Path("/srv/music/7.png"))

    def test_the_root_itself_is_allowed(self):
        self.assertEqual(main._contained(self.ROOT, "."), self.ROOT)

    def test_a_parent_traversal_is_refused(self):
        self.assertIsNone(main._contained(self.ROOT, "../etc/passwd"))

    def test_a_traversal_buried_mid_path_is_refused(self):
        self.assertIsNone(main._contained(self.ROOT, "Artist/../../etc/passwd"))

    def test_an_absolute_path_is_refused(self):
        # os.path.join() discards the base when the second argument is
        # absolute; the prefix test is what catches the result.
        self.assertIsNone(main._contained(self.ROOT, "/etc/passwd"))

    def test_a_sibling_directory_sharing_a_prefix_is_refused(self):
        # /srv/music-private starts with the base STRING but is not inside
        # the base DIRECTORY -- the separator in the comparison is what
        # distinguishes them, and dropping it would silently allow this.
        self.assertIsNone(main._contained(self.ROOT, "../music-private/x"))


class SafeReferrerTests(_RouteTestBase):
    """#60: /set-language redirects to the referrer, so the referrer is
    attacker-influenced input and the redirect is an open-redirect sink.

    _safe_referrer() guards it by requiring the referrer's host to be ours
    and the path to start with a slash. That is necessary and was not
    sufficient: a path beginning `//` or `/\\` starts with a slash, passes
    the host check, and is then resolved by the browser as a protocol-
    relative URL to a DIFFERENT origin -- so the host that was validated is
    not the host the user lands on.

    The guard had no tests at all before this class, which is why the gap
    survived. Each rejected shape is named below rather than covered by one
    example, so a future rewrite has to keep rejecting all of them."""

    ORIGIN = "http://localhost"

    def _location(self, referrer):
        resp = self.client.get("/set-language/en",
                               headers={"Referer": referrer} if referrer else {})
        self.assertEqual(resp.status_code, 302)
        return resp.headers["Location"]

    def test_a_same_host_path_is_kept(self):
        # The control. If this failed, every assertion below would pass
        # against a guard that simply refuses everything.
        self.assertEqual(self._location(f"{self.ORIGIN}/library"), "/library")

    def test_a_same_host_path_keeps_its_query(self):
        self.assertEqual(self._location(f"{self.ORIGIN}/a?b=c"), "/a?b=c")

    def test_a_foreign_host_falls_back_home(self):
        self.assertEqual(self._location("http://evil.example/x"), "/")

    def test_a_protocol_relative_referrer_falls_back_home(self):
        self.assertEqual(self._location("//evil.example/x"), "/")

    def test_a_double_slash_path_on_our_own_host_falls_back_home(self):
        # The reported defect. urlsplit() gives path "//evil.example", which
        # starts with "/" and so passed the old check -- and Location:
        # //evil.example is protocol-relative, i.e. another origin.
        self.assertEqual(self._location(f"{self.ORIGIN}//evil.example"), "/")

    def test_a_slash_backslash_path_on_our_own_host_falls_back_home(self):
        # Same shape via the separator several browsers normalise to "/".
        self.assertEqual(self._location(f"{self.ORIGIN}/\\evil.example"), "/")

    def test_no_referrer_falls_back_home(self):
        self.assertEqual(self._location(None), "/")

    def test_the_emitted_location_never_carries_a_foreign_authority(self):
        # The property, stated once independently of the shapes above: after
        # this route, the browser must stay on this origin. A new bypass that
        # nobody thought to enumerate should fail here even if it slips past
        # every named case.
        hostile = [
            "http://evil.example/x", "//evil.example/x",
            f"{self.ORIGIN}//evil.example", f"{self.ORIGIN}/\\evil.example",
            f"{self.ORIGIN}//evil.example/path?q=1",
        ]
        for referrer in hostile:
            with self.subTest(referrer=referrer):
                loc = self._location(referrer)
                self.assertFalse(loc.startswith("//"), loc)
                self.assertFalse(loc.startswith("/\\"), loc)
                self.assertNotIn("evil.example", loc)


class DeviceDeleteBasketOrphanTests(_RouteTestBase):
    """#22: deleting a device must not leave basket_items rows with no
    device links.

    basket_item_devices cascades on device_id, so the link rows go and the
    item does not. app/db.py's SCHEMA comment on that table states the
    no-zero-links invariant as something maintained everywhere a
    basket_items row is touched -- but the cascade is not a place a row is
    touched by application code, so it was the one path that broke it. The
    result is invisible rather than loud: the basket panel renders per
    device, so an item with no devices has nowhere to appear, cannot be
    cleared, and accumulates one per delete."""

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

    def _item_row_exists(self, item_id):
        """Reads basket_items directly rather than through GET /api/basket.

        The whole defect is a row that the API cannot show: it renders per
        device, so an orphaned item is already absent from that response
        while still sitting in the table. Asserting over the endpoint would
        pass against the unfixed code."""
        return self.conn.execute(
            "SELECT 1 FROM basket_items WHERE id = ?", (item_id,)
        ).fetchone() is not None

    def _link_count(self, item_id):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM basket_item_devices WHERE basket_item_id = ?",
            (item_id,),
        ).fetchone()["n"]

    def test_staging_creates_the_row_this_suite_asserts_about(self):
        # Guards the two tests below. Both assert a row is absent after a
        # delete; if staging never wrote one, or wrote it somewhere
        # _item_row_exists does not look, they would pass against code that
        # prunes nothing at all.
        item_id = self._stage("artist", "A", [self.device_a])
        self.assertTrue(self._item_row_exists(item_id))
        self.assertEqual(self._link_count(item_id), 1)

    def test_deleting_the_items_only_device_removes_the_item_row(self):
        item_id = self._stage("artist", "A", [self.device_a])
        resp = self.client.delete(f"/api/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._link_count(item_id), 0)
        self.assertFalse(self._item_row_exists(item_id))

    def test_an_item_staged_for_a_second_device_survives(self):
        # The other half of the invariant: prune what the delete orphaned,
        # and nothing else. An item that still has somewhere to render must
        # keep its row and its remaining link.
        item_id = self._stage("artist", "A", [self.device_a, self.device_b])
        resp = self.client.delete(f"/api/devices/{self.device_a}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self._item_row_exists(item_id))
        self.assertEqual(self._link_count(item_id), 1)

    def test_an_unrelated_users_orphan_is_left_alone(self):
        # Scoped to what this delete orphaned, not a global sweep: a
        # link-less row produced by any other cause stays put, so a later
        # producer of this state surfaces instead of being quietly mopped up
        # by every device deletion.
        stray = self.conn.execute(
            "INSERT INTO basket_items (user_id, type, target) VALUES (?, 'artist', 'Z')",
            (self.owner,),
        ).lastrowid
        self.conn.commit()
        item_id = self._stage("artist", "A", [self.device_a])
        self.client.delete(f"/api/devices/{self.device_a}")
        self.assertFalse(self._item_row_exists(item_id))
        self.assertTrue(self._item_row_exists(stray))


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
        web_common._revoke_non_owner_playlist_selections(self.conn, self.pl, self.owner)
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
        with mock.patch.object(settings, "ADMIN_USERNAME", "owner"):
            resp = self.client.post("/login", data={"username": "attacker", "password": "pw123456"})
        self.assertEqual(resp.status_code, 200)  # re-rendered form, not a redirect
        self.assertNotIn("/", resp.headers.get("Location", ""))
        self.assertEqual(self._admins(), [])  # no account created at all

    def test_matching_username_bootstraps_admin_when_admin_username_set(self):
        from unittest import mock
        with mock.patch.object(settings, "ADMIN_USERNAME", "owner"):
            resp = self.client.post("/login", data={"username": "owner", "password": "pw123456"})
        self.assertEqual(resp.status_code, 302)  # redirected into the app
        self.assertEqual(self._admins(), [{"username": "owner", "is_admin": 1}])

    def test_any_username_bootstraps_when_admin_username_unset(self):
        # Unchanged legacy behaviour when ADMIN_USERNAME is not configured —
        # the documented "don't expose before first login" caveat covers it.
        from unittest import mock
        with mock.patch.object(settings, "ADMIN_USERNAME", ""):
            resp = self.client.post("/login", data={"username": "whoever", "password": "pw123456"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._admins(), [{"username": "whoever", "is_admin": 1}])

    def test_wrong_username_rejected_even_after_repeated_attempts_no_account(self):
        # The rejection path records a failure (rate-limited like a login) and
        # never creates a row — a would-be squatter can't accrete an account.
        from unittest import mock
        with mock.patch.object(settings, "ADMIN_USERNAME", "owner"):
            for _ in range(3):
                self.client.post("/login", data={"username": "attacker", "password": "pw123456"})
        self.assertEqual(self._admins(), [])


class DevicePlayPlaylistRouteTests(_RouteTestBase):
    """The device's Play choice through PATCH /api/devices/<id>: null for
    Everything or a playlist selected for the device; listed on the web's
    device list and kept out of the published integration shape."""

    def setUp(self):
        super().setUp()
        self.watch, _ = sync_state.create_device(self.conn, self.owner, "Watch", "watch")
        self.chosen = playlist_sync.create_trobar_playlist(self.conn, self.owner, "Run")
        self.unselected = playlist_sync.create_trobar_playlist(self.conn, self.owner, "Walk")
        sync_state.create_selection(self.conn, "playlist", str(self.chosen), self.owner, [self.watch])
        _login(self.client, self.owner)

    def _patch(self, value):
        return self.client.patch(f"/api/devices/{self.watch}", json={"play_playlist_id": value})

    def _listed(self):
        return next(d for d in self.client.get("/api/devices").get_json() if d["id"] == self.watch)

    def test_a_selected_playlist_is_stored_and_null_clears_it(self):
        self.assertIsNone(self._listed()["play_playlist_id"])
        self.assertEqual(self._patch(self.chosen).status_code, 200)
        self.assertEqual(self._listed()["play_playlist_id"], self.chosen)
        self.assertEqual(self._patch(None).status_code, 200)
        self.assertIsNone(self._listed()["play_playlist_id"])

    def test_anything_but_a_selected_playlist_is_refused(self):
        for bad in (self.unselected, 999999, True, str(self.chosen), 1.5):
            with self.subTest(value=bad):
                self.assertEqual(self._patch(bad).status_code, 400)
        self.assertIsNone(self._listed()["play_playlist_id"])

    def test_a_playlist_selected_only_for_another_device_is_refused(self):
        phone, _ = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        sync_state.create_selection(self.conn, "playlist", str(self.unselected), self.owner, [phone])
        self.assertEqual(self._patch(self.unselected).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/devices/{phone}",
                                           json={"play_playlist_id": self.unselected}).status_code, 200)

    def test_the_integration_api_shape_is_unchanged(self):
        self._patch(self.chosen)
        _token_id, token = sync_state.create_integration_token(self.conn, self.admin, "Test token")
        rows = self.client.get("/api/integrations/devices", headers={"Authorization": f"Bearer {token}"}).get_json()
        watch = next(d for d in rows if d["id"] == self.watch)  # the control: the device is listed
        self.assertNotIn("play_playlist_id", watch)


class DeviceIconTests(_RouteTestBase):
    """A device's icon: an optional picture from a fixed set, apart from its
    type. Null follows the type; it is served wherever the type is, except
    the published integration API, whose shape is locked."""

    def setUp(self):
        super().setUp()
        self.phone, self.token = sync_state.create_device(self.conn, self.owner, "Phone", "dap")
        _login(self.client, self.owner)

    def _patch(self, body):
        return self.client.patch(f"/api/devices/{self.phone}", json=body)

    def _listed(self, path, **kw):
        return next(d for d in self.client.get(path, **kw).get_json() if d["id"] == self.phone)

    def test_a_new_device_has_no_icon_and_follows_its_type(self):
        self.assertIsNone(self._listed("/api/devices")["icon"])

    def test_an_icon_from_the_set_is_stored_and_served_beside_the_type(self):
        self.assertEqual(self._patch({"icon": "clickwheel"}).status_code, 200)
        listed = self._listed("/api/devices")
        self.assertEqual((listed["icon"], listed["device_type"]), ("clickwheel", "dap"))  # the type is untouched
        app_headers = {"Authorization": f"Bearer {self.token}"}
        self.assertEqual(self._listed("/api/app/devices", headers=app_headers)["icon"], "clickwheel")
        self.assertEqual(self.client.get("/api/device/info", headers=app_headers).get_json()["icon"], "clickwheel")

    def test_null_clears_it_and_an_icon_outside_the_set_is_refused(self):
        self._patch({"icon": "cassette"})
        self.assertEqual(self._patch({"icon": None}).status_code, 200)
        self.assertIsNone(self._listed("/api/devices")["icon"])
        for bad in ("player", "", "PHONE", 3, True):
            with self.subTest(icon=bad):
                self.assertEqual(self._patch({"icon": bad}).status_code, 400)
        self.assertIsNone(self._listed("/api/devices")["icon"])

    def test_a_patch_without_an_icon_leaves_it_alone(self):
        self._patch({"icon": "cassette"})
        self._patch({"name": "Kitchen"})
        self.assertEqual(self._listed("/api/devices")["icon"], "cassette")

    def test_the_set_matches_the_glyphs_the_web_ships(self):
        # Every id the server accepts has a glyph in icons.js, so no stored
        # icon ever draws as a blank.
        icons_js = (Path(__file__).parent / "static" / "js" / "icons.js").read_text()
        for icon in web_common.DEVICE_ICONS:
            with self.subTest(icon=icon):
                self.assertIn(f'"device-{icon}":', icons_js)


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

    def test_create_takes_only_a_known_device_type(self):
        for bad in ("android", "toaster"):
            with self.subTest(bad=bad):
                self.assertEqual(self._post({"name": "x", "device_type": bad}).status_code, 400)
        did = self._post({"name": "Phone"}).get_json()["id"]
        row = self.conn.execute("SELECT device_type FROM devices WHERE id = ?", (did,)).fetchone()
        self.assertEqual(row["device_type"], "phone")

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


class DeviceRepairCodeRouteTests(_RouteTestBase):
    """POST /api/devices/<id>/repair-code — mint an enrollment code that
    re-attaches a reinstalled app to THIS device instead of creating a new
    one, then redeem it through the wizard's ordinary endpoint."""

    def setUp(self):
        super().setUp()
        self.device_id, self.old_token = sync_state.create_device(
            self.conn, self.owner, "Pixel", "phone")
        self.conn.commit()
        _login(self.client, self.owner)

    def _mint(self, device_id=None):
        return self.client.post(
            f"/api/devices/{device_id or self.device_id}/repair-code")

    def test_the_reported_failure_a_reinstalled_app_gets_its_device_back(self):
        # End to end over the API, in the order a user hits it: mint a code in
        # the web UI, redeem it from a fresh install exactly as the wizard
        # does. Before this existed there was no request that could do it --
        # redeem always created a new device.
        mint = self._mint()
        self.assertEqual(mint.status_code, 200)
        code = mint.get_json()["code"]

        redeem = self.client.post("/api/enrollment/redeem",
                                  json={"code": code, "name": "Pixel"})
        self.assertEqual(redeem.status_code, 200)
        self.assertEqual(redeem.get_json()["id"], self.device_id)

        count = self.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        self.assertEqual(count, 1)

    def test_the_response_carries_the_devices_existing_name(self):
        # The app displays what it gets back; it must see the device's real
        # name, not the placeholder it sent.
        code = self._mint().get_json()["code"]
        body = self.client.post("/api/enrollment/redeem",
                                json={"code": code, "name": "Some new phone"}).get_json()
        self.assertEqual(body["name"], "Pixel")

    def test_redeeming_with_an_unknown_device_type_is_refused_and_keeps_the_code(self):
        code = self._mint().get_json()["code"]
        refused = self.client.post("/api/enrollment/redeem",
                                   json={"code": code, "name": "Pixel", "device_type": "android"})
        self.assertEqual(refused.status_code, 400)
        redeem = self.client.post("/api/enrollment/redeem", json={"code": code, "name": "Pixel"})
        self.assertEqual(redeem.status_code, 200)

    def test_a_card_devices_code_is_8_characters_and_keeps_the_device(self):
        """The web UI's pairing action for every device, and its new-device
        flow for a DAP, card or folder: an 8-character code bound to that
        device. The desktop app redeems it with its own name and type; the
        device keeps the ones it has, its limit included."""
        card_id, _token = sync_state.create_device(
            self.conn, self.owner, "Kitchen card", "sdcard", 8_000_000_000)
        self.conn.commit()
        code = self._mint(card_id).get_json()["code"]
        self.assertRegex(code, r"^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{8}$")

        body = self.client.post("/api/enrollment/redeem", json={
            "code": code, "name": "card", "device_type": "sdcard"}).get_json()
        self.assertEqual((body["id"], body["name"]), (card_id, "Kitchen card"))
        row = self.conn.execute(
            "SELECT device_type, max_size_bytes FROM devices WHERE id = ?", (card_id,)).fetchone()
        self.assertEqual((row["device_type"], row["max_size_bytes"]), ("sdcard", 8_000_000_000))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0], 2)

    def test_a_stranger_cannot_mint_a_repair_code(self):
        # 403 rather than 404: the device exists, this user just may not
        # manage it. Minting is the whole attack surface here -- a code is a
        # bearer credential for someone else's device.
        _login(self.client, self.other)
        self.assertEqual(self._mint().status_code, 403)

    def test_minting_for_a_device_that_does_not_exist_is_404(self):
        self.assertEqual(self._mint(device_id=999999).status_code, 404)

    def test_an_admin_may_mint_but_ownership_does_not_move(self):
        # An admin helping someone re-pair must not silently acquire the
        # device: the grant is bound to the OWNER, not to whoever minted it.
        _login(self.client, self.admin)
        code = self._mint().get_json()["code"]
        self.client.post("/api/enrollment/redeem", json={"code": code, "name": "Pixel"})
        row = self.conn.execute(
            "SELECT owner_user_id FROM devices WHERE id=?", (self.device_id,)).fetchone()
        self.assertEqual(row["owner_user_id"], self.owner)

    def test_cross_origin_mint_is_blocked(self):
        # Same CSRF Origin check every other /api/devices* mutation carries.
        r = self.client.post(f"/api/devices/{self.device_id}/repair-code",
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_minting_requires_a_session(self):
        self.assertEqual(self.client.post("/logout").status_code, 302)
        self.assertNotEqual(self._mint().status_code, 200)


class LogoutTests(_RouteTestBase):
    """/logout is POST-only (a link elsewhere can't sign anyone out), so the
    menu's Log out has to be a form that POSTs, and it only exists where the
    app holds a session to end."""

    _FORM = '<form method="post" action="/logout">'

    def test_post_ends_the_session_and_goes_to_login(self):
        _login(self.client, self.owner)
        resp = self.client.post("/logout")
        self.assertEqual((resp.status_code, resp.headers["Location"]), (302, "/login"))
        self.assertEqual(self.client.get("/api/profile").status_code, 401)

    def test_get_is_refused(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertEqual(self.client.get("/api/profile").status_code, 200)

    def test_a_cross_site_post_is_refused(self):
        _login(self.client, self.owner)
        resp = self.client.post("/logout", headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.get("/api/profile").status_code, 200)

    def test_the_menu_posts_in_local_and_oidc_mode(self):
        _login(self.client, self.admin)
        self.conn.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('setup_completed', '1')")
        self.conn.commit()
        for mode in ("local", "oidc"):
            with self.subTest(mode=mode), mock.patch.object(settings, "AUTH_MODE", mode):
                html = self.client.get("/").get_data(as_text=True)
                self.assertIn(self._FORM, html)
                self.assertNotIn('href="/logout"', html)

    def test_no_log_out_in_forward_mode(self):
        with mock.patch.object(settings, "AUTH_MODE", "forward"):
            html = self.client.get("/").get_data(as_text=True)
        self.assertIn("avatarMenuOpen", html, "the menu itself still renders")
        self.assertNotIn(self._FORM, html)

    def test_forward_mode_on_the_emergency_port_can_log_out(self):
        _login(self.client, self.admin)
        port = {"SERVER_PORT": "25001"}
        with mock.patch.object(settings, "AUTH_MODE", "forward"), mock.patch.object(settings, "EMERGENCY_PORT", 25001):
            self.assertIn(self._FORM, self.client.get("/", environ_overrides=port).get_data(as_text=True))
            resp = self.client.post("/logout", environ_overrides=port)
        self.assertEqual(resp.headers["Location"], "/login")


class ForwardAuthNoHeaderTests(_RouteTestBase):
    """Forward mode trusts the proxy's identity header. A request without it
    (a misconfigured proxy, or one that reached the port directly) is
    refused, not signed in as DEV_USER, except on a local dev server."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(settings, "AUTH_MODE", "forward"),
                        mock.patch.object(settings, "FORWARD_AUTH_SECRET", "")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _users(self):
        return {r["username"] for r in self.conn.execute("SELECT username FROM users")}

    def test_no_header_is_refused_and_logged(self):
        before = self._users()
        with self.assertLogs("main", level="WARNING") as log:
            resp = self.client.get("/api/profile")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("X-authentik-username", log.output[0])
        self.assertEqual(self._users(), before, "no account was created")

    def test_the_header_still_signs_in(self):
        resp = self.client.get("/api/profile", headers={"X-authentik-username": "bob"})
        self.assertEqual((resp.status_code, resp.get_json()["username"]), (200, "bob"))

    def test_a_dev_server_signs_in_as_dev_user(self):
        with mock.patch.object(settings, "DEV_SERVER", True), \
                mock.patch.dict(os.environ, {"DEV_USER": "devperson"}):
            resp = self.client.get("/api/profile")
        self.assertEqual((resp.status_code, resp.get_json()["username"]), (200, "devperson"))


class ForwardAuthStartupWarningTests(unittest.TestCase):
    """The startup warnings for a forward-mode deployment trusting more than
    it may realise."""

    def _warnings(self, mode="forward", secret="", dev=False):
        with mock.patch.object(settings, "AUTH_MODE", mode), \
                mock.patch.object(settings, "FORWARD_AUTH_SECRET", secret), \
                mock.patch.object(settings, "DEV_SERVER", dev):
            return main._forward_auth_warnings()

    def test_none_for_a_secured_forward_deployment_or_another_mode(self):
        self.assertEqual(self._warnings(secret="s3cret"), [])
        self.assertEqual(self._warnings(mode="local", dev=True), [])

    def test_forward_without_a_secret(self):
        [warning] = self._warnings()
        self.assertIn("no FORWARD_AUTH_SECRET", warning)

    def test_forward_with_the_dev_server(self):
        [warning] = self._warnings(secret="s3cret", dev=True)
        self.assertIn("TROBAR_DEV_SERVER", warning)
        self.assertIn("DEV_USER", warning)
        self.assertEqual(len(self._warnings(dev=True)), 2)


class OidcUnreachableTests(_RouteTestBase):
    """An identity provider that's down: sign-in says so and points at the
    local-password form; logout finishes locally."""

    def setUp(self):
        super().setUp()
        self.oauth = mock.MagicMock()
        down = ConnectionError("connection refused")
        self.oauth.oidc.authorize_redirect.side_effect = down
        self.oauth.oidc.load_server_metadata.side_effect = down
        for patcher in (mock.patch.object(settings, "AUTH_MODE", "oidc"),
                        mock.patch.object(settings, "OIDC_LOGOUT", True),
                        mock.patch.object(auth, "oauth", self.oauth)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sso_sign_in_shows_the_local_password_fallback(self):
        with self.assertLogs("main", level="WARNING") as log:
            resp = self.client.get("/oidc/login")
        self.assertEqual(resp.status_code, 503)
        page = resp.get_data(as_text=True)
        self.assertIn("SSO is unreachable right now. An administrator can sign in with a local password.", page)
        # Under oidc only an admin's local password works: the notice mustn't
        # send everyone to it.
        self.assertNotIn("Sign in with a local password", page)
        self.assertIn('name="password"', page, "the local-password form is on the page")
        self.assertIn("ConnectionError", log.output[0])

    def test_logout_finishes_locally(self):
        _login(self.client, self.owner)
        with self.assertLogs("main", level="WARNING"):
            resp = self.client.post("/logout")
        self.assertEqual((resp.status_code, resp.headers["Location"]), (302, "/login"))
        self.assertEqual(self.client.get("/api/profile").status_code, 401)


class SessionLifetimeTests(_RouteTestBase):
    """Sessions slide over a configurable lifetime, and a password change or
    an admin reset ends the user's other sessions."""

    def _client_logged_in_as(self, user_id, password="correct horse"):
        self.conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                          (main.generate_password_hash(password), user_id))
        self.conn.commit()
        username = self.conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        client = main.app.test_client()
        resp = client.post("/login", data={"username": username, "password": password})
        self.assertEqual(resp.status_code, 302, "signed in")
        return client, resp

    def test_a_login_is_a_permanent_session_whose_cookie_is_renewed(self):
        client, resp = self._client_logged_in_as(self.owner)
        self.assertIn("Expires=", resp.headers["Set-Cookie"])
        again = client.get("/api/profile")
        self.assertEqual(again.status_code, 200)
        self.assertIn("Expires=", again.headers.get("Set-Cookie", ""), "re-signed on every request: sliding")
        self.assertEqual(main.app.config["PERMANENT_SESSION_LIFETIME"].days, 30)

    def test_a_logged_out_session_stays_out_when_an_old_cookie_comes_back(self):
        # A request in flight at logout returns a re-signed copy of the
        # signed-in cookie after the logout's own: replaying it must not work.
        client, _ = self._client_logged_in_as(self.owner)
        stale = client.get_cookie("session").value
        self.assertEqual(client.post("/logout").status_code, 302)
        client.set_cookie("session", stale)
        self.assertEqual(client.get("/api/profile").status_code, 401)
        # Another session of the same user is unaffected.
        other, _ = self._client_logged_in_as(self.owner)
        self.assertEqual(other.get("/api/profile").status_code, 200)

    def test_a_session_older_than_the_lifetime_is_over(self):
        client, _ = self._client_logged_in_as(self.owner)
        with mock.patch.dict(main.app.config, {"PERMANENT_SESSION_LIFETIME": timedelta(seconds=1)}):
            time.sleep(2.1)
            self.assertEqual(client.get("/api/profile").status_code, 401)

    def test_changing_my_password_ends_my_other_sessions_and_keeps_this_one(self):
        here, _ = self._client_logged_in_as(self.owner)
        elsewhere, _ = self._client_logged_in_as(self.owner)
        resp = here.post("/api/account/password",
                         json={"password": "a new long password", "current_password": "correct horse"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(here.get("/api/profile").status_code, 200)
        self.assertEqual(elsewhere.get("/api/profile").status_code, 401)

    def test_an_admin_reset_ends_the_users_sessions(self):
        user, _ = self._client_logged_in_as(self.owner)
        admin, _ = self._client_logged_in_as(self.admin, "admin password!")
        resp = admin.put(f"/api/admin/users/{self.owner}/password", json={"password": "reset by admin"},
                         headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200)
        # A page first, while the stale cookie is still there: pages are guarded
        # only by the login gate, and the API call below clears the session.
        page = user.get("/")
        self.assertEqual((page.status_code, page.headers.get("Location")), (302, "/login"))
        self.assertEqual(user.get("/api/profile").status_code, 401)
        self.assertEqual(admin.get("/api/profile").status_code, 200, "the admin's own session is untouched")

    def test_the_create_admin_command_ends_the_accounts_sessions(self):
        user, _ = self._client_logged_in_as(self.owner)
        username = self.conn.execute("SELECT username FROM users WHERE id = ?", (self.owner,)).fetchone()[0]
        result = main.app.test_cli_runner().invoke(args=["create-admin", username, "--password", "cli password!"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(user.get("/api/profile").status_code, 401)


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


class DeviceListSnapshotTests(_RouteTestBase):
    """A device deleted while the list is being built (in another tab, or by
    another user) must not fail the whole list: the rows and each device's
    statuses are read from one snapshot."""

    def setUp(self):
        super().setUp()
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)
        _phone, self.phone_token = sync_state.create_device(self.conn, self.admin, "Phone", "phone")
        self.gone, _ = sync_state.create_device(self.conn, self.admin, "Player", "dap")
        # All three lists reach it through web_common._device_list.
        real = web_common._device_rows_for_user

        def rows_then_delete(*args, **kwargs):
            rows = real(*args, **kwargs)
            other = db.get_conn()
            try:
                other.execute("DELETE FROM devices WHERE id = ?", (self.gone,))
                other.commit()
            finally:
                other.close()
            return rows

        patcher = mock.patch.object(web_common, "_device_rows_for_user", side_effect=rows_then_delete)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _check(self, resp):
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        self.assertEqual(sorted(d["name"] for d in resp.get_json()), ["Phone", "Player"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM devices WHERE id = ?", (self.gone,)).fetchone())

    def test_the_web_list_survives_a_device_deleted_mid_list(self):
        _login(self.client, self.admin)
        self._check(self.client.get("/api/devices"))

    def test_the_app_list_survives_a_device_deleted_mid_list(self):
        self._check(self.client.get("/api/app/devices",
                                    headers={"Authorization": f"Bearer {self.phone_token}"}))

    def test_the_integrations_list_survives_a_device_deleted_mid_list(self):
        _token_id, token = sync_state.create_integration_token(self.conn, self.admin, "Test token")
        self._check(self.client.get("/api/integrations/devices",
                                    headers={"Authorization": f"Bearer {token}"}))


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
                "is_own", "is_pinned", "max_size_bytes",
                "reported_foreign_bytes", "foreign_bytes_reported_at",
                "reported_free_bytes",
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
            {"mirrors_failing", "by_sink", "failing", "failing_truncated",
             "mirrors_short", "short", "short_truncated"},
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
            {sink: {"enabled": 0, "failing": 0, "short": 0}
             for sink in ("filesystem", "subsonic", "jellyfin", "emby", "music_assistant", "plex")},
        )
        self.assertEqual((body["mirrors_short"], body["short"], body["short_truncated"]), (0, [], False))

    def test_a_pair_short_on_its_target_is_counted_and_listed(self):
        pid = self._make_playlist("Road Trip")
        self.conn.execute("UPDATE playlists SET jellyfin_mirror_enabled = 1, plex_mirror_enabled = 1 "
                          "WHERE id = ?", (pid,))
        main.mirror_runs.record(self.conn, pid, "jellyfin", 64, 40, [{"artist": "A", "title": "T"}] * 24)
        main.mirror_runs.record(self.conn, pid, "plex", 64, 64)
        self.conn.commit()
        body = self._get(self._token_for(self.admin)).get_json()
        self.assertEqual(body["mirrors_short"], 1)
        self.assertEqual(body["by_sink"]["jellyfin"]["short"], 1)
        self.assertEqual(body["by_sink"]["plex"]["short"], 0)
        self.assertEqual(sum(v["short"] for v in body["by_sink"].values()), body["mirrors_short"])
        self.assertEqual(set(body["short"][0].keys()),
                         {"playlist_id", "title", "sink", "on_target", "total", "lacked", "refused", "run_at"},
                         self._CONTRACT_MESSAGE)
        self.assertEqual((body["short"][0]["sink"], body["short"][0]["on_target"], body["short"][0]["total"],
                          body["short"][0]["lacked"]), ("jellyfin", 40, 64, 24))
        self.assertEqual(body["mirrors_failing"], 0, "short is not failing")

    def test_a_disabled_mirrors_old_runs_are_not_short(self):
        pid = self._make_playlist("Road Trip")
        main.mirror_runs.record(self.conn, pid, "jellyfin", 64, 40, [{"artist": "A", "title": "T"}] * 24)
        self.conn.commit()
        body = self._get(self._token_for(self.admin)).get_json()
        self.assertEqual(body["mirrors_short"], 0)
        self.assertEqual(body["by_sink"]["jellyfin"], {"enabled": 0, "failing": 0, "short": 0})
        self.assertEqual(body["short"], [])

    def test_a_disabled_sink_is_skipped_on_a_playlist_with_another_enabled(self):
        # The row query takes every playlist with ANY sink enabled, so a
        # disabled sink on such a playlist is only kept out by the per-sink
        # enabled check: with its leftover error and old runs, it must
        # count nowhere, while the enabled sink beside it counts as usual.
        pid = self._make_playlist("Road Trip")
        self.conn.execute(
            "UPDATE playlists SET jellyfin_mirror_enabled = 1, plex_mirror_enabled = 0, "
            "plex_mirror_last_error_code = 'unreachable' WHERE id = ?", (pid,))
        main.mirror_runs.record(self.conn, pid, "plex", 64, 40, [{"artist": "A", "title": "T"}] * 24)
        main.mirror_runs.record(self.conn, pid, "jellyfin", 64, 60, [{"artist": "A", "title": "T"}] * 4)
        self.conn.commit()
        body = self._get(self._token_for(self.admin)).get_json()
        self.assertEqual(body["by_sink"]["plex"], {"enabled": 0, "failing": 0, "short": 0})
        self.assertEqual(body["by_sink"]["jellyfin"], {"enabled": 1, "failing": 0, "short": 1})
        self.assertEqual((body["mirrors_failing"], body["mirrors_short"]), (0, 1))
        self.assertEqual([e["sink"] for e in body["short"]], ["jellyfin"])

    def test_an_enabled_healthy_mirror_counts_as_enabled_not_failing(self):
        p = self._make_playlist("OK", owner_user_id=None, shared=1)
        self.conn.execute(
            "UPDATE playlists SET subsonic_mirror_enabled = 1 WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["by_sink"]["subsonic"], {"enabled": 1, "failing": 0, "short": 0})
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
        self.assertEqual(body["by_sink"]["subsonic"], {"enabled": 1, "failing": 1, "short": 0})
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
        self.assertEqual(body["by_sink"]["subsonic"], {"enabled": 1, "failing": 1, "short": 0})
        self.assertEqual(body["by_sink"]["jellyfin"], {"enabled": 1, "failing": 0, "short": 0})
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
        self.assertEqual(body["by_sink"]["filesystem"], {"enabled": 1, "failing": 0, "short": 0})
        self.assertEqual(body["by_sink"]["emby"], {"enabled": 1, "failing": 1, "short": 0})
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
        self.assertEqual(body["by_sink"]["subsonic"], {"enabled": 0, "failing": 0, "short": 0})
        self.assertEqual(body["mirrors_failing"], 0)
        self.assertEqual(body["failing"], [])

    def test_the_failing_worklist_is_capped_but_the_counts_stay_exact(self):
        for i in range(integrations._MIRRORS_FAILING_LIMIT + 5):
            p = self._make_playlist(f"Failing {i:03d}", owner_user_id=None, shared=1)
            self.conn.execute(
                "UPDATE playlists SET subsonic_mirror_enabled = 1, "
                "subsonic_mirror_last_error_code = 'unreachable' WHERE id = ?", (p,))
        self.conn.commit()
        token = self._token_for(self.admin)
        body = self._get(token).get_json()
        self.assertEqual(body["mirrors_failing"], integrations._MIRRORS_FAILING_LIMIT + 5)
        self.assertEqual(body["by_sink"]["subsonic"]["failing"], integrations._MIRRORS_FAILING_LIMIT + 5)
        self.assertEqual(len(body["failing"]), integrations._MIRRORS_FAILING_LIMIT)
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


class DeviceLimitTests(_RouteTestBase):
    """A device's size limit: validated on both routes, 0 meaning no limit, and
    a change refreshing the frozen auto-fit set."""

    def setUp(self):
        super().setUp()
        _login(self.client, self.owner)
        self.device, self.token = sync_state.create_device(
            self.conn, self.owner, "phone", max_size_bytes=20_000)
        for artist, album in (("Glass Meridian", "Latitude"), ("Aphelion", "Event Horizon")):
            for n in (1, 2):
                self.conn.execute(
                    "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                    "VALUES (?, ?, ?, ?, 3000, 0)", (f"{artist}/{album}/{n}.mp3", artist, album, f"T{n}"))
        self.conn.commit()
        ranked = [("glass meridian", "latitude"), ("aphelion", "event horizon")]
        # Both names: the autofit routes in devices call devices', the limit
        # refresh in web_common calls its own.
        for module in (devices, web_common):
            patcher = mock.patch.object(module, "_ranked_album_keys_for_user", return_value=ranked)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _limit(self):
        return self.conn.execute(
            "SELECT max_size_bytes FROM devices WHERE id = ?", (self.device,)).fetchone()[0]

    def _autofit_track_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM autofit_tracks a JOIN selections s ON s.id = a.selection_id "
            "JOIN selection_devices sd ON sd.selection_id = s.id WHERE sd.device_id = ?",
            (self.device,)).fetchone()[0]

    def _device_patch(self, body):
        return self.client.patch("/api/device/limit", json=body,
                                 headers={"Authorization": f"Bearer {self.token}"})

    def test_lowering_the_limit_on_the_web_refreshes_autofit(self):
        self.client.post(f"/api/devices/{self.device}/autofit", json={})
        self.assertEqual(self._autofit_track_count(), 4)
        resp = self.client.patch(f"/api/devices/{self.device}", json={"max_size_bytes": 7_000})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._autofit_track_count(), 2, "only the first album fits now")
        # ...and the device is told: the second album's tracks leave it.
        states = dict(self.conn.execute(
            "SELECT status, COUNT(*) FROM device_track_state WHERE device_id = ? GROUP BY status",
            (self.device,)).fetchall())
        self.assertEqual(states, {"pending": 2, "removed": 2})

    def test_lowering_the_limit_from_the_device_refreshes_autofit(self):
        self.client.post(f"/api/devices/{self.device}/autofit", json={})
        self.assertEqual(self._device_patch({"max_size_bytes": 7_000}).status_code, 200)
        self.assertEqual(self._autofit_track_count(), 2)

    def _held(self):
        return (self._autofit_track_count(), dict(self.conn.execute(
            "SELECT status, COUNT(*) FROM device_track_state WHERE device_id = ? GROUP BY status",
            (self.device,)).fetchall()))

    def _autofit_all_downloaded(self):
        self.client.post(f"/api/devices/{self.device}/autofit", json={})
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded' WHERE device_id = ?",
                          (self.device,))
        self.conn.commit()
        self.assertEqual(self._held(), (4, {"downloaded": 4}))

    def test_a_limit_change_during_a_lastfm_outage_keeps_autofit(self):
        # lastfm.top_albums gives [] on any failure: the set must not be
        # refreshed from nothing, which would remove every auto-fit track.
        self._autofit_all_downloaded()
        # The state is checked inside each block, so it is what fails first;
        # the warning is checked as the block closes.
        with mock.patch.object(web_common, "_ranked_album_keys_for_user", return_value=[]):
            with self.assertLogs("main", level="WARNING"):
                self.assertEqual(self._device_patch({"max_size_bytes": 30_000}).status_code, 200)
                self.assertEqual(self._held(), (4, {"downloaded": 4}))
            with self.assertLogs("main", level="WARNING"):
                self.assertEqual(self.client.patch(f"/api/devices/{self.device}",
                                                   json={"max_size_bytes": 40_000}).status_code, 200)
                self.assertEqual(self._held(), (4, {"downloaded": 4}))

    def test_resending_the_same_limit_from_the_device_leaves_autofit_alone(self):
        self._autofit_all_downloaded()
        # A ranking that would now fit only the first album: not consulted.
        with mock.patch.object(web_common, "_ranked_album_keys_for_user",
                               return_value=[("glass meridian", "latitude")]):
            self.assertEqual(self._device_patch({"max_size_bytes": 20_000}).status_code, 200)
        self.assertEqual(self._held(), (4, {"downloaded": 4}))

    def test_the_device_route_validates_the_limit(self):
        for bad in (-1, "10", True, 1.5):
            with self.subTest(bad=bad):
                self.assertEqual(self._device_patch({"max_size_bytes": bad}).status_code, 400)
        self.assertEqual(self._limit(), 20_000)

    def test_zero_means_no_limit_on_both_routes(self):
        self.assertEqual(self._device_patch({"max_size_bytes": 0}).status_code, 200)
        self.assertIsNone(self._limit())
        self.client.patch(f"/api/devices/{self.device}", json={"max_size_bytes": 5_000})
        self.client.patch(f"/api/devices/{self.device}", json={"max_size_bytes": 0})
        self.assertIsNone(self._limit())

    def test_a_zero_stored_before_is_no_limit_in_usage(self):
        self.conn.execute("UPDATE devices SET max_size_bytes = 0 WHERE id = ?", (self.device,))
        self.conn.commit()
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.owner, [self.device])
        usage = self.client.get(f"/api/devices/{self.device}/usage").get_json()
        self.assertEqual((usage["max_size_bytes"], usage["over_limit"]), (None, False))

    def test_device_type_is_one_of_the_known_types(self):
        resp = self.client.patch(f"/api/devices/{self.device}", json={"device_type": "toaster"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.patch(f"/api/devices/{self.device}",
                                           json={"device_type": "dap"}).status_code, 200)

    def test_android_is_not_a_device_type_any_more(self):
        # It silently became "phone" at the next start, so the choice never stuck.
        resp = self.client.patch(f"/api/devices/{self.device}", json={"device_type": "android"})
        self.assertEqual(resp.status_code, 400)

    def test_the_known_types_are_the_ones_the_ui_labels(self):
        self.assertEqual(set(web_common.DEVICE_TYPES), set(main._build_js_i18n()["deviceType"]))


class HistorySourceOutageTests(_RouteTestBase):
    """A history service that doesn't answer is reported as "error", not as
    "nothing played", and an auto-fit refresh during a Last.fm outage keeps
    the set it has."""

    def setUp(self):
        super().setUp()
        _login(self.client, self.owner)
        self.conn.execute("UPDATE users SET lastfm_username = 'alice', lastfm_api_key = 'k' WHERE id = ?",
                          (self.owner,))
        self.conn.commit()
        self.device, self.token = sync_state.create_device(
            self.conn, self.owner, "phone", max_size_bytes=20_000)

    def _app_get(self, path):
        return self.client.get(path, headers={"Authorization": f"Bearer {self.token}"}).get_json()

    def test_sources_say_which_service_did_not_answer(self):
        with mock.patch.object(main.lastfm, "suggestions", return_value=None), \
                mock.patch.object(main.lastfm, "recently_played_suggestions", return_value=[]), \
                mock.patch.object(main.lastfm, "most_played", return_value=None):
            sug = self._app_get("/api/app/suggestions")
            top = self._app_get("/api/app/suggestions/most-played")
            web = self.client.get("/api/suggestions").get_json()
        expected = {"lastfm": "error", "listenbrainz": "unconfigured", "maloja": "unconfigured",
                             "device_plays": "unconfigured"}
        self.assertEqual((sug["sources"], top["sources"]), (expected, expected))
        self.assertIsInstance(web, list, "the web route keeps its bare list")

    def test_sources_say_ok_when_the_service_answered_with_nothing(self):
        with mock.patch.object(main.lastfm, "most_played", return_value=[]):
            top = self._app_get("/api/app/suggestions/most-played")
        self.assertEqual((top["items"], top["sources"]["lastfm"]), ([], "ok"))

    def _autofit_all_downloaded(self):
        for n, (artist, album) in enumerate((("Glass Meridian", "Latitude"), ("Aphelion", "Event Horizon"))):
            self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                "VALUES (?, ?, ?, 'T', 3000, 0)", (f"{n}.mp3", artist, album))
        self.conn.commit()
        albums = [{"name": "Latitude", "artist": {"name": "Glass Meridian"}},
                  {"name": "Event Horizon", "artist": {"name": "Aphelion"}}]
        with mock.patch.object(main.lastfm, "top_albums", return_value=albums):
            self.client.post(f"/api/devices/{self.device}/autofit", json={})
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded' WHERE device_id = ?",
                          (self.device,))
        self.conn.commit()

    def _held(self):
        return dict(self.conn.execute(
            "SELECT status, COUNT(*) FROM device_track_state WHERE device_id = ? GROUP BY status",
            (self.device,)).fetchall())

    def test_an_autofit_refresh_during_a_lastfm_outage_keeps_the_set(self):
        self._autofit_all_downloaded()
        self.assertEqual(self._held(), {"downloaded": 2})
        with mock.patch.object(main.lastfm, "top_albums", return_value=None):
            with self.assertLogs("main", level="WARNING"):
                resp = self.client.post(f"/api/devices/{self.device}/autofit", json={})
                self.assertEqual(self._held(), {"downloaded": 2})
        body = resp.get_json()
        self.assertEqual((resp.status_code, body["reason"], body["tracks"]), (200, "lastfm_unavailable", 2))

    def test_a_limit_change_during_a_lastfm_outage_keeps_the_set(self):
        self._autofit_all_downloaded()
        with mock.patch.object(main.lastfm, "top_albums", return_value=None):
            with self.assertLogs("main", level="WARNING"):
                self.client.patch(f"/api/devices/{self.device}", json={"max_size_bytes": 4_000})
                self.assertEqual(self._held(), {"downloaded": 2})


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


class DeviceFileTranscodeTests(_RouteTestBase):
    """GET /api/device/file/<id> for a device that transcodes: a failed or
    hung encode is an error, never an empty 200 a client would keep, and a
    busy server says so instead of holding the request."""

    def setUp(self):
        super().setUp()
        self._music = Path(tempfile.mkdtemp(dir=_TMP))
        db.set_config(self.conn, "music_root", str(self._music))
        self.device_id, self.token = sync_state.create_device(self.conn, self.owner, "dap")
        self.conn.execute("UPDATE devices SET transcode_format = 'mp3_320' WHERE id = ?", (self.device_id,))
        (self._music / "a").mkdir()
        (self._music / "a" / "x.flac").write_bytes(b"FLACDATA")
        cur = self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                                "VALUES ('a/x.flac', 'A', 'Al', 'T', 8, 0)")
        self.track = sync_state._new_id(cur)
        self.conn.execute("INSERT INTO device_track_state (device_id, track_id, status, updated_at) "
                          "VALUES (?, ?, 'pending', datetime('now'))", (self.device_id, self.track))
        self.conn.commit()
        main.transcode._active = 0
        self.cache = main.transcode.cache_dir(db.DATA_DIR)
        shutil.rmtree(self.cache, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.cache, True)

    def _encode(self, returncode, content=b"ID3mp3bytes", out_dir=None):
        fd, out_path = tempfile.mkstemp(dir=out_dir or _TMP, suffix=".mp3")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        fd, err_path = tempfile.mkstemp(dir=_TMP, suffix=".log")
        os.close(fd)
        proc = mock.Mock()
        proc.wait.return_value = None
        proc.returncode = returncode
        return proc, out_path, err_path

    def _get(self):
        resp = self.client.get(f"/api/device/file/{self.track}", headers={"Authorization": f"Bearer {self.token}"})
        data = resp.get_data()
        resp.close()
        return resp, data

    def test_a_finished_encode_is_sent_with_its_length(self):
        # Streamed from its temp file: with no cache, nothing is kept.
        db.set_config(self.conn, "transcode_cache_gb", "0")
        self.conn.commit()
        started = self._encode(0)
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", return_value=started):
            resp, data = self._get()
        self.assertEqual((resp.status_code, data, resp.headers["Content-Length"]), (200, b"ID3mp3bytes", "11"))
        self.assertEqual(main.transcode._active, 0)

    def test_a_failed_encode_is_an_error_not_an_empty_track(self):
        started = self._encode(1, b"partial")
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", return_value=started), \
                self.assertLogs("transcode", level="WARNING"):
            resp, data = self._get()
        self.assertEqual(resp.status_code, 500)
        self.assertNotEqual(data, b"partial")
        self.assertEqual(main.transcode._active, 0)
        self.assertFalse(os.path.exists(started[1]))

    def _start(self, content=b"ID3mp3bytes"):
        """A stand-in for transcode.start that writes where the real one
        would: into out_dir when the route passes one."""
        def start(abs_path, fmt, nice_level, out_dir=None):
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
            return self._encode(0, content, out_dir=out_dir)
        return mock.Mock(side_effect=start)

    def _cached_files(self):
        return sorted(p.name for p in self.cache.glob("*.mp3")) if self.cache.exists() else []

    def test_a_second_fetch_is_served_from_the_cache_without_encoding(self):
        start = self._start()
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", start):
            first, first_data = self._get()
            second, second_data = self._get()
        self.assertEqual(start.call_count, 1)
        self.assertEqual((first.status_code, first_data), (200, b"ID3mp3bytes"))
        self.assertEqual((second.status_code, second_data), (200, b"ID3mp3bytes"))
        self.assertEqual(second.mimetype, "audio/mpeg")
        mtime = (self._music / "a" / "x.flac").stat().st_mtime_ns
        self.assertEqual(self._cached_files(), [f"{self.track}-{mtime}-mp3_320.mp3"])
        self.assertEqual(main.transcode._active, 0)

    def test_a_cached_transcode_can_be_fetched_in_ranges(self):
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", self._start()):
            self._get()
        resp = self.client.get(f"/api/device/file/{self.track}",
                               headers={"Authorization": f"Bearer {self.token}", "Range": "bytes=3-5"})
        self.assertEqual((resp.status_code, resp.get_data()), (206, b"mp3"))
        resp.close()

    def test_a_changed_source_is_encoded_again(self):
        start = self._start()
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", start):
            self._get()
            source = self._music / "a" / "x.flac"
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            self._get()
        self.assertEqual(start.call_count, 2)
        self.assertEqual(len(self._cached_files()), 2)

    def test_the_cap_is_counted_in_gigabytes(self):
        # Two cached tracks are far inside the 2 GB default: neither is evicted.
        self.cache.mkdir(parents=True, exist_ok=True)
        other = self.cache / "999-1-mp3_320.mp3"
        other.write_bytes(b"x" * 4096)
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", self._start()):
            self._get()
        self.assertTrue(other.exists())
        self.assertEqual(len(self._cached_files()), 2)

    def test_a_cache_of_zero_keeps_nothing(self):
        db.set_config(self.conn, "transcode_cache_gb", "0")
        self.conn.commit()
        start = self._start()
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", start):
            resp, data = self._get()
            self._get()
        self.assertEqual((resp.status_code, data, resp.headers["Accept-Ranges"]), (200, b"ID3mp3bytes", "none"))
        self.assertEqual(start.call_count, 2)
        self.assertIsNone(start.call_args.kwargs["out_dir"])
        self.assertEqual(self._cached_files(), [])

    def test_an_encode_finished_while_waiting_for_the_slot_is_served(self):
        mtime = (self._music / "a" / "x.flac").stat().st_mtime_ns
        cached = main.transcode.cache_path(db.DATA_DIR, self.track, mtime, "mp3_320")

        def acquire(limit, timeout=None):
            # Another request encodes the track while this one waits.
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b"ID3fromtheother")
            main.transcode._active += 1
            return True

        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "acquire_slot", side_effect=acquire), \
                mock.patch.object(main.transcode, "start") as start:
            resp, data = self._get()
        start.assert_not_called()
        self.assertEqual((resp.status_code, data), (200, b"ID3fromtheother"))
        self.assertEqual(main.transcode._active, 0)

    def test_a_transcode_the_cache_cannot_take_is_still_sent(self):
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", self._start()), \
                mock.patch.object(main.transcode.os, "replace", side_effect=OSError("disk full")), \
                self.assertLogs("transcode", level="WARNING"):
            resp, data = self._get()
        self.assertEqual((resp.status_code, data), (200, b"ID3mp3bytes"))
        self.assertEqual(main.transcode._active, 0)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_saving_a_smaller_cache_frees_the_space_now(self):
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "start", self._start()):
            self._get()
        self.assertEqual(len(self._cached_files()), 1)
        _login(self.client, self.admin)
        resp = self.client.put("/api/admin/config", json={"transcode_cache_gb": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["transcode_cache_gb"], 0)
        self.assertEqual(self._cached_files(), [])

    def test_the_cache_defaults_to_two_gb(self):
        _login(self.client, self.admin)
        self.assertEqual(self.client.get("/api/admin/config").get_json()["transcode_cache_gb"], 2)

    def test_a_busy_server_answers_503_with_retry_after(self):
        with mock.patch.object(main.transcode, "ffmpeg_available", return_value=True), \
                mock.patch.object(main.transcode, "acquire_slot", return_value=False) as acquire, \
                mock.patch.object(main.transcode, "start") as start:
            resp, _data = self._get()
        self.assertEqual((resp.status_code, resp.headers["Retry-After"]), (503, str(main.transcode.SLOT_WAIT_SECONDS)))
        self.assertEqual(acquire.call_args.kwargs["timeout"], main.transcode.SLOT_WAIT_SECONDS)
        start.assert_not_called()


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

    def test_only_an_admin_can_confirm_a_mass_removal(self):
        with mock.patch.object(scanner, "start_scan", return_value={"status": "started"}) as start:
            _login(self.client, self.owner)
            self.assertEqual(self.client.post("/api/library/scan?confirm_mass_removal=1").status_code, 403)
            start.assert_not_called()
            _login(self.client, self.admin)
            self.assertEqual(self.client.post("/api/library/scan?confirm_mass_removal=1").status_code, 202)
        self.assertIs(start.call_args.kwargs["confirm_mass_removal"], True)

    def test_only_an_admin_can_run_a_full_rescan(self):
        with mock.patch.object(scanner, "start_scan", return_value={"status": "started"}) as start:
            _login(self.client, self.owner)
            resp = self.client.post("/api/library/scan?force=1")
            self.assertEqual(resp.status_code, 403)
            self.assertIn("full rescan", resp.get_json()["error"])
            start.assert_not_called()
            self.assertEqual(self.client.post("/api/library/scan").status_code, 202)
            self.assertIs(start.call_args.kwargs["force"], False)
            _login(self.client, self.admin)
            self.assertEqual(self.client.post("/api/library/scan?force=1").status_code, 202)
        self.assertIs(start.call_args.kwargs["force"], True)

    def test_the_scan_job_is_registered_to_retry_incremental(self):
        self.assertIs(jobs._ON_RETRY[scanner.JOB_TYPE], scanner.retry_payload)

    def test_a_refused_scan_reports_why(self):
        _login(self.client, self.admin)
        self.conn.execute(
            "INSERT INTO jobs (type, state, payload, last_error, finished_at) VALUES (?, 'failed', '{}', ?, datetime('now'))",
            (scanner.JOB_TYPE, "MassRemovalRefused: The scan found no audio files under /music"))
        self.conn.commit()
        last = self.client.get("/api/library/scan/status").get_json()["last_result"]
        self.assertEqual(last["status"], "error")
        self.assertIn("found no audio files", last["reason"])

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


class DevicePrunePreviewRouteTests(_RouteTestBase):
    """GET /api/devices/<id>/prune-preview: how many tracks switching Keep
    device content off removes from the device, for the question the switch
    asks. Only tracks on the device and outside every selection count."""

    def setUp(self):
        super().setUp()
        self.device, _token = sync_state.create_device(self.conn, self.owner, "kept-dev")
        self.conn.execute("UPDATE devices SET source_of_truth = 'device' WHERE id = ?", (self.device,))
        self.tracks = {}
        for name in ("kept", "extra1", "extra2", "pending", "excluded"):
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                "VALUES (?, 'A', 'Al', ?, 1, 0)", (f"a/{name}.flac", name))
            self.tracks[name] = sync_state._new_id(cur)
        # One track a selection still asks for; the rest are on the device or
        # pending or excluded with no selection behind them.
        sync_state.create_selection(self.conn, "track", str(self.tracks["kept"]), self.owner, [self.device])
        for name, status in (("kept", "downloaded"), ("extra1", "downloaded"), ("extra2", "downloaded"),
                             ("pending", "pending"), ("excluded", "excluded")):
            self.conn.execute(
                "INSERT OR REPLACE INTO device_track_state (device_id, track_id, status, updated_at) "
                "VALUES (?, ?, ?, datetime('now'))", (self.device, self.tracks[name], status))
        self.conn.commit()

    def test_counts_the_tracks_on_the_device_that_no_selection_includes(self):
        _login(self.client, self.owner)
        resp = self.client.get(f"/api/devices/{self.device}/prune-preview")
        self.assertEqual((resp.status_code, resp.get_json()), (200, {"tracks": 2}))

    def test_the_count_is_what_switching_it_off_then_removes(self):
        _login(self.client, self.owner)
        before = self.client.get(f"/api/devices/{self.device}/prune-preview").get_json()["tracks"]
        self.client.patch(f"/api/devices/{self.device}", json={"source_of_truth": "server"})
        removed_from_device = self.conn.execute(
            "SELECT COUNT(*) FROM device_track_state WHERE device_id = ? AND status = 'removed' "
            "AND track_id IN (?, ?, ?)",
            (self.device, self.tracks["kept"], self.tracks["extra1"], self.tracks["extra2"])).fetchone()[0]
        self.assertEqual(before, removed_from_device)

    def test_it_writes_nothing(self):
        _login(self.client, self.owner)
        snapshot = self.conn.execute(
            "SELECT track_id, status FROM device_track_state WHERE device_id = ? ORDER BY track_id",
            (self.device,)).fetchall()
        self.client.get(f"/api/devices/{self.device}/prune-preview")
        self.assertEqual(self.conn.execute(
            "SELECT track_id, status FROM device_track_state WHERE device_id = ? ORDER BY track_id",
            (self.device,)).fetchall(), snapshot)

    def test_only_someone_who_may_manage_the_device_can_ask(self):
        _login(self.client, self.other)
        self.assertEqual(self.client.get(f"/api/devices/{self.device}/prune-preview").status_code, 403)
        self.assertEqual(self.client.get("/api/devices/999999/prune-preview").status_code, 404)


class DeviceSendProjectionRouteTests(_RouteTestBase):
    """GET /api/devices/send-projection: what sending an item adds to each
    device, for the Sync to… picker's bars. Only the item's tracks the device
    neither requires, holds nor excludes count as new."""

    def setUp(self):
        super().setUp()
        self.device, _token = sync_state.create_device(self.conn, self.owner, "phone")
        self.conn.execute("UPDATE devices SET max_size_bytes = 10000, reported_free_bytes = 2000 WHERE id = ?",
                          (self.device,))
        self.tracks = {}
        for name, size in (("held", 100), ("new", 300), ("excluded", 400), ("queued", 500), ("elsewhere", 700)):
            album = "Other" if name == "elsewhere" else "Al"
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                "VALUES (?, 'A', ?, ?, ?, 0)", (f"a/{name}.flac", album, name, size))
            self.tracks[name] = sync_state._new_id(cur)
        # Already asked for: held (on the device, smaller there than its file)
        # and queued (not downloaded yet). The excluded one was asked for and
        # left deleted on purpose.
        for name in ("held", "queued", "excluded"):
            sync_state.create_selection(self.conn, "track", str(self.tracks[name]), self.owner, [self.device])
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded', bytes_on_device = 80 "
                          "WHERE device_id = ? AND track_id = ?", (self.device, self.tracks["held"]))
        self.conn.execute("UPDATE device_track_state SET status = 'excluded' "
                          "WHERE device_id = ? AND track_id = ?", (self.device, self.tracks["excluded"]))
        self.conn.commit()

    def _get(self, **params):
        return self.client.get("/api/devices/send-projection", query_string=params)

    def test_only_what_the_device_lacks_counts_as_new(self):
        _login(self.client, self.owner)
        resp = self._get(type="album", target="A||Al")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # The album is four tracks; only "new" is new to the device.
        self.assertEqual((body["tracks"], body["bytes"]), (4, 100 + 300 + 400 + 500))
        self.assertEqual(body["devices"], {str(self.device): {
            "new_tracks": 1, "new_bytes": 300,
            # held as reported by the device, queued by its file; not the excluded one.
            "used_bytes": 80 + 500, "on_device_bytes": 80,
            "max_size_bytes": 10000, "reported_free_bytes": 2000, "reserve_bytes": None,
        }})

    def test_the_reserve_the_device_reported_comes_with_it(self):
        self.conn.execute("UPDATE devices SET reported_reserve_bytes = 1500 WHERE id = ?", (self.device,))
        self.conn.commit()
        _login(self.client, self.owner)
        projected = self._get(type="album", target="A||Al").get_json()["devices"][str(self.device)]
        self.assertEqual(projected["reserve_bytes"], 1500)

    def test_held_or_excluded_without_a_selection_is_not_new(self):
        # Kept on a device whose content is kept, and left deleted on purpose:
        # neither is required by a selection, and sending neither adds bytes.
        for name, status in (("kept", "downloaded"), ("refused", "excluded")):
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                "VALUES (?, 'A', 'Al', ?, 1000, 0)", (f"a/{name}.flac", name))
            self.conn.execute("INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, ?)",
                              (self.device, sync_state._new_id(cur), status))
        self.conn.commit()
        _login(self.client, self.owner)
        projected = self._get(type="album", target="A||Al").get_json()["devices"][str(self.device)]
        self.assertEqual((projected["new_tracks"], projected["new_bytes"]), (1, 300))

    def test_a_device_deleted_meanwhile_does_not_fail_the_projection(self):
        # Another tab or user deletes a device while the projection reads
        # them: it is read from one snapshot, so it answers, not 404s.
        doomed, _token = sync_state.create_device(self.conn, self.owner, "old phone")
        self.conn.commit()
        real_usage = web_common._device_usage
        deleted: list[int] = []

        def usage_then_delete(conn, user_id, device_id):
            if not deleted:
                other = db.get_conn()
                other.execute("DELETE FROM devices WHERE id = ?", (doomed,))
                other.commit()
                other.close()
                deleted.append(doomed)
            return real_usage(conn, user_id, device_id)

        _login(self.client, self.owner)
        with mock.patch.object(web_common, "_device_usage", side_effect=usage_then_delete):
            resp = self._get(type="album", target="A||Al")
        self.assertEqual(deleted, [doomed])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.get_json()["devices"]), {str(self.device), str(doomed)})

    def test_several_targets_are_measured_together(self):
        _login(self.client, self.owner)
        body = self._get(type="album", target=["A||Al", "A||Other"]).get_json()
        self.assertEqual(body["devices"][str(self.device)]["new_bytes"], 300 + 700)

    def test_new_bytes_are_what_sending_it_then_adds_to_usage(self):
        _login(self.client, self.owner)
        projected = self._get(type="album", target="A||Al").get_json()["devices"][str(self.device)]
        sync_state.create_selection(self.conn, "album", "A||Al", self.owner, [self.device])
        after = self.client.get(f"/api/devices/{self.device}/usage").get_json()["used_bytes"]
        self.assertEqual(after, projected["used_bytes"] + projected["new_bytes"])

    def test_it_writes_nothing(self):
        _login(self.client, self.owner)
        query = "SELECT device_id, track_id, status FROM device_track_state ORDER BY 1, 2"
        snapshot = self.conn.execute(query).fetchall()
        self._get(type="album", target="A||Al")
        self.assertEqual(self.conn.execute(query).fetchall(), snapshot)

    def test_each_user_sees_only_the_devices_they_may_manage(self):
        _login(self.client, self.other)
        self.assertEqual(self._get(type="album", target="A||Al").get_json()["devices"], {})
        _login(self.client, self.admin)
        self.assertIn(str(self.device), self._get(type="album", target="A||Al").get_json()["devices"])

    def test_a_private_playlist_is_not_measured_for_someone_else(self):
        private = self._make_playlist("mine", owner_user_id=self.owner, shared=0)
        _login(self.client, self.other)
        self.assertEqual(self._get(type="playlist", target=str(private)).status_code, 403)
        _login(self.client, self.owner)
        self.assertEqual(self._get(type="playlist", target=str(private)).status_code, 200)

    def test_bad_requests_are_refused(self):
        _login(self.client, self.owner)
        self.assertEqual(self._get(type="autofit", target="1").status_code, 400)
        self.assertEqual(self._get(type="album", target=["x||y"] * 1001).status_code, 400)
        self.assertEqual(self._get(type="album").get_json()["tracks"], 0)


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



class DevicePlaylistsRouteTests(_RouteTestBase):
    """POST /api/device/playlists: playlists a person made on a device,
    uploaded by the client that syncs it, become playlists of the device's
    owner and go out to the mirrors like any other."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "Ana's DAP")
        for rel, artist, album, title, no in (
                ("lib/a.flac", "Cinder & Ash", "Emberwake", "Smoulder", 1),
                ("lib/b.flac", "Glass Meridian", "Latitude", "Two Suns", 2)):
            self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
                              "VALUES (?, ?, ?, ?, ?, 1, 0)", (rel, artist, album, title, no))
        self.conn.commit()
        index = sync_state.device_path_index(self.conn, self.device)
        self.on_card = {row["relative_path"]: p for p, row in index.items()}

    def _upload(self, playlists, token=None):
        return self.client.post("/api/device/playlists", json={"playlists": playlists},
                                headers={"Authorization": f"Bearer {token or self.token}"})

    def _rows(self):
        return self.conn.execute(
            "SELECT id, title, source_provider, source_playlist_id, owner_user_id, shared "
            "FROM playlists WHERE source_provider = 'device' ORDER BY title").fetchall()

    def _matched(self, playlist_id):
        return [r["relative_path"] for r in self.conn.execute(
            "SELECT t.relative_path FROM playlist_tracks pt LEFT JOIN tracks t ON t.id = pt.matched_track_id "
            "WHERE pt.playlist_id = ? ORDER BY pt.position", (playlist_id,))]

    def test_a_card_playlist_becomes_a_private_playlist_of_the_devices_owner(self):
        a = self.on_card["lib/a.flac"]
        resp = self._upload([{"path": "Road Trip.m3u8", "entries": [a]}])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"playlists": 1, "tracks": 1, "matched": 1, "unknown": 0})
        [row] = self._rows()
        self.assertEqual((row["title"], row["owner_user_id"], row["shared"], row["source_playlist_id"]),
                         ("Road Trip", self.owner, 0, f"{self.device}:Road Trip.m3u8"))
        self.assertEqual(self._matched(row["id"]), ["lib/a.flac"])

    def test_the_three_ways_players_write_entries_resolve(self):
        a, b = self.on_card["lib/a.flac"], self.on_card["lib/b.flac"]
        self._upload([{"path": "Playlists/Mix.m3u", "entries": [
            "../" + a,                         # relative to the playlist's folder
            b,                                 # relative to the sync root
            "/mnt/sdcard/Music/" + a,          # absolute, from the player's mount
            "E:\\Music\\" + b.replace("/", "\\"),  # a Windows-made playlist
        ]}])
        [row] = self._rows()
        self.assertEqual(self._matched(row["id"]), ["lib/a.flac", "lib/b.flac", "lib/a.flac", "lib/b.flac"])

    def test_short_entries_resolve_against_the_playlists_folder(self):
        # A playlist saved inside an album folder lists bare file names; one
        # in an artist folder lists Album/file. Neither has three segments.
        artist, album, name = self.on_card["lib/a.flac"].split("/")
        self._upload([{"path": f"{artist}/{album}/album.m3u", "entries": [name]},
                      {"path": f"{artist}/best.m3u", "entries": [f"{album}/{name}", f"./{album}/{name}"]},
                      {"path": "top.m3u", "entries": [name]}])  # at the root, a bare name is nothing
        matched = {r["title"]: self._matched(r["id"]) for r in self._rows()}
        self.assertEqual(matched, {"album": ["lib/a.flac"], "best": ["lib/a.flac", "lib/a.flac"], "top": [None]})

    def test_an_entry_matching_no_track_goes_to_the_devices_unknown_tracks(self):
        resp = self._upload([{"path": "Mix.m3u", "entries": ["Side Loaded/Bootleg/01 - Live.mp3"]}])
        self.assertEqual(resp.get_json()["unknown"], 1)
        unknown = self.conn.execute("SELECT path, artist, title FROM device_unknown_tracks WHERE device_id = ?",
                                    (self.device,)).fetchall()
        self.assertEqual([tuple(r) for r in unknown], [("Side Loaded/Bootleg/01 - Live.mp3", "Side Loaded", "Live")])
        self.assertEqual(self.conn.execute("SELECT unknown_track_count FROM devices WHERE id = ?",
                                           (self.device,)).fetchone()[0], 1)
        [row] = self._rows()
        self.assertEqual(self._matched(row["id"]), [None])

    # A file Trobar didn't put on the card, named the way Trobar names them.
    _COPIED = "Music/Glass Meridian/Latitude/07 - Two Suns.mp3"

    def _b(self):
        return self.conn.execute("SELECT id FROM tracks WHERE relative_path = 'lib/b.flac'").fetchone()[0]

    def _review(self, playlist_id):
        return self.client.get(f"/api/provider/playlists/{playlist_id}/unresolved-tracks").get_json()

    def _unresolved_count(self, playlist_id):
        rows = self.client.get("/api/provider/playlists").get_json()
        rows = rows["playlists"] if isinstance(rows, dict) else rows
        return next(r for r in rows if r["id"] == playlist_id)["unresolved_count"]

    def _accept(self, playlist_id, ids, accepted=True):
        return self.client.post(f"/api/provider/playlists/{playlist_id}/unresolved-tracks/accept",
                                json={"ids": ids, "accepted": accepted})

    def test_a_file_named_like_a_library_track_is_only_a_suggestion(self):
        self._upload([{"path": "Mix.m3u", "entries": [self._COPIED]}])
        [row] = self._rows()
        self.assertEqual(self._matched(row["id"]), [None], "never matched, nor sent anywhere, until accepted")
        _login(self.client, self.owner)
        [entry] = self._review(row["id"])
        self.assertEqual((entry["suggested"]["track_id"], entry["source_path"], entry["accepted"]),
                         (self._b(), self._COPIED, False))
        self.assertEqual((entry["suggested"]["artist"], entry["suggested"]["title"]), ("Glass Meridian", "Two Suns"))
        self.assertEqual(self._unresolved_count(row["id"]), 1)

    def test_a_card_path_under_a_mount_point_still_gives_its_artist(self):
        self._upload([{"path": "Mix.m3u", "entries": ["/mnt/sdcard/" + self._COPIED]}])
        [row] = self._rows()
        _login(self.client, self.owner)
        self.assertEqual(self._review(row["id"])[0]["suggested"]["track_id"], self._b())

    def test_accepting_matches_it_at_once_and_after_the_next_upload_and_undo_takes_it_back(self):
        self._upload([{"path": "Mix.m3u", "entries": [self._COPIED]}])
        [row] = self._rows()
        _login(self.client, self.owner)
        [entry] = self._review(row["id"])
        resp = self._accept(row["id"], [entry["id"]])
        self.assertEqual(resp.get_json(), {"changed": [entry["id"]], "unresolved_count": 0})
        self.assertEqual(self._matched(row["id"]), ["lib/b.flac"])
        self._upload([{"path": "Mix.m3u", "entries": [self._COPIED]}])
        self.assertEqual(self._matched(row["id"]), ["lib/b.flac"], "the acceptance survives a re-upload")
        self.assertEqual(self._review(row["id"])[0]["accepted"], True)
        self.assertEqual(self._unresolved_count(row["id"]), 0)
        self._accept(row["id"], [entry["id"]], accepted=False)
        self.assertEqual(self._matched(row["id"]), [None])
        self.assertEqual(self._unresolved_count(row["id"]), 1)

    def test_accepting_sends_the_track_to_a_device_selecting_the_playlist(self):
        self._upload([{"path": "Mix.m3u", "entries": [self._COPIED]}])
        [row] = self._rows()
        sync_state.create_selection(self.conn, "playlist", str(row["id"]), self.owner, [self.device])
        self.conn.commit()
        _login(self.client, self.owner)
        self._accept(row["id"], [self._review(row["id"])[0]["id"]])
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT track_id, status FROM device_track_state WHERE device_id = ?", (self.device,))],
            [(self._b(), "pending")])

    def test_accepting_rewrites_the_playlists_mirrors(self):
        self._upload([{"path": "Mix.m3u", "entries": [self._COPIED]}])
        [row] = self._rows()
        _login(self.client, self.owner)
        with mock.patch.object(main.playlist_sync, "write_playlist_mirrors") as write:
            self._accept(row["id"], [self._review(row["id"])[0]["id"]])
        write.assert_called_once()
        self.assertEqual(write.call_args.args[1], row["id"])

    def test_a_name_that_fits_nothing_has_no_suggestion(self):
        self._upload([{"path": "Mix.m3u", "entries": ["Side Loaded/Bootleg/01 - Live.mp3"]}])
        [row] = self._rows()
        _login(self.client, self.owner)
        [entry] = self._review(row["id"])
        self.assertIsNone(entry["suggested"])
        self.assertEqual(self._accept(row["id"], [entry["id"]]).get_json()["changed"], [])

    def test_a_suggestion_that_changes_must_be_accepted_again(self):
        self._upload([{"path": "Mix.m3u", "entries": [self._COPIED]}])
        [row] = self._rows()
        _login(self.client, self.owner)
        self._accept(row["id"], [self._review(row["id"])[0]["id"]])
        # The accepted track leaves the library; another copy of the song is
        # what the file name now fits.
        self.conn.execute("UPDATE tracks SET deleted_at = 'now' WHERE relative_path = 'lib/b.flac'")
        self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
                          "VALUES ('lib/c.flac', 'Glass Meridian', 'Latitude (Deluxe)', 'Two Suns', 2, 1, 0)")
        self.conn.commit()
        self._upload([{"path": "Mix.m3u", "entries": [self._COPIED]}])
        self.assertEqual(self._matched(row["id"]), [None])
        [entry] = self._review(row["id"])
        self.assertEqual((entry["accepted"], entry["suggested"]["album"]), (False, "Latitude (Deluxe)"))

    def test_only_the_playlists_owner_or_an_admin_may_accept(self):
        self._upload([{"path": "Mix.m3u", "entries": [self._COPIED]}])
        [row] = self._rows()
        self.conn.execute("UPDATE playlists SET shared = 1 WHERE id = ?", (row["id"],))
        self.conn.commit()
        _login(self.client, self.other)
        [entry] = self._review(row["id"])
        self.assertEqual(self._accept(row["id"], [entry["id"]]).status_code, 403)
        self.assertEqual(self._matched(row["id"]), [None])
        _login(self.client, self.owner)
        self.assertEqual(self._accept(row["id"], ["x"]).status_code, 400)

    def test_a_reupload_updates_and_a_file_gone_from_the_card_is_removed(self):
        a, b = self.on_card["lib/a.flac"], self.on_card["lib/b.flac"]
        self._upload([{"path": "One.m3u", "entries": [a]}, {"path": "Two.m3u", "entries": [a]}])
        one = {r["title"]: r["id"] for r in self._rows()}["One"]
        self._upload([{"path": "One.m3u", "entries": [b, a]}])
        self.assertEqual([r["title"] for r in self._rows()], ["One"])
        self.assertEqual(self._rows()[0]["id"], one, "same row, updated in place")
        self.assertEqual(self._matched(one), ["lib/b.flac", "lib/a.flac"])

    def test_another_devices_playlists_are_left_alone(self):
        other, other_token = sync_state.create_device(self.conn, self.owner, "Phone")
        a = self.on_card["lib/a.flac"]
        self._upload([{"path": "Mine.m3u", "entries": [a]}])
        self._upload([], token=other_token)
        self.assertEqual([r["title"] for r in self._rows()], ["Mine"])

    def test_a_playlist_trobar_wrote_to_the_card_is_never_read_back(self):
        # Trobar -> card -> Trobar: a playlist selected onto this device is
        # written to its root; uploading that file is ignored.
        a = self.on_card["lib/a.flac"]
        self._upload([{"path": "Mine.m3u", "entries": [a]}])
        [mine] = self._rows()
        sync_state.create_selection(self.conn, "playlist", str(mine["id"]), self.owner, [self.device])
        [written] = sync_state.device_playlist_filenames(self.conn, self.device)
        resp = self._upload([{"path": "Mine.m3u", "entries": [a]}, {"path": written, "entries": [a]}])
        self.assertEqual(resp.get_json()["playlists"], 1)
        self.assertEqual([r["title"] for r in self._rows()], ["Mine"])

    def test_its_mirror_is_written_and_never_read_back_as_a_source(self):
        # card -> Trobar -> mirror folder (a Kodi folder) -> filesystem reader.
        mirror_dir = Path(tempfile.mkdtemp(prefix="trobar-test-device-mirror-", dir=_TMP))
        db.set_config(self.conn, "mirror_folder", str(mirror_dir))
        db.set_config(self.conn, "music_root", str(mirror_dir))  # the mirror seen inside the library
        self.conn.commit()
        a = self.on_card["lib/a.flac"]
        self._upload([{"path": "Road Trip.m3u8", "entries": [a]}])
        [row] = self._rows()
        self.conn.execute("UPDATE playlists SET mirror_enabled = 1 WHERE id = ?", (row["id"],))
        self.conn.commit()
        self._upload([{"path": "Road Trip.m3u8", "entries": [a]}])
        written = list(mirror_dir.rglob("*_Trobar_.m3u"))
        self.assertEqual([p.relative_to(mirror_dir).parts[0] for p in written], ["owner"])
        self.assertEqual(filesystem_client.list_playlists()["playlists"], [])
        # Removing the file from the card removes the row and its mirror.
        self._upload([])
        self.assertEqual(self._rows(), [])
        self.assertEqual(list(mirror_dir.rglob("*.m3u")), [])

    def test_the_list_names_the_device_a_playlist_came_from(self):
        a = self.on_card["lib/a.flac"]
        self.conn.execute("UPDATE devices SET device_type = 'dap' WHERE id = ?", (self.device,))
        self.conn.commit()
        self._upload([{"path": "Road Trip.m3u8", "entries": [a]}])
        _login(self.client, self.owner)
        [row] = [p for p in self.client.get("/api/provider/playlists").get_json() if p["source_provider"] == "device"]
        self.assertEqual((row["source_device_name"], row["source_device_type"]), ("Ana's DAP", "dap"))

    def test_deleting_the_device_removes_its_playlists_and_their_mirrors(self):
        mirror_dir = Path(tempfile.mkdtemp(prefix="trobar-test-device-delete-", dir=_TMP))
        db.set_config(self.conn, "mirror_folder", str(mirror_dir))
        self.conn.commit()
        self._upload([{"path": "Road Trip.m3u8", "entries": [self.on_card["lib/a.flac"]]}])
        [row] = self._rows()
        self.conn.execute("UPDATE playlists SET mirror_enabled = 1 WHERE id = ?", (row["id"],))
        self.conn.commit()
        self._upload([{"path": "Road Trip.m3u8", "entries": [self.on_card["lib/a.flac"]]}])
        self.assertEqual(len(list(mirror_dir.rglob("*_Trobar_.m3u"))), 1)
        _login(self.client, self.owner)
        self.assertEqual(self.client.delete(f"/api/devices/{self.device}").status_code, 200)
        self.assertEqual(self._rows(), [])
        self.assertEqual(list(mirror_dir.rglob("*.m3u")), [])

    def test_replacing_the_device_moves_its_playlists_to_the_new_one(self):
        self._upload([{"path": "Road Trip.m3u8", "entries": [self.on_card["lib/a.flac"]]}])
        [before] = self._rows()
        new, new_token = sync_state.create_device(self.conn, self.owner, "Ana's new DAP")
        sync_state.transfer_device(self.conn, self.device, new)
        self.conn.commit()
        [after] = self._rows()
        self.assertEqual((after["id"], after["source_playlist_id"]), (before["id"], f"{new}:Road Trip.m3u8"))
        # The new device's next upload keeps the same row.
        self._upload([{"path": "Road Trip.m3u8", "entries": [self.on_card["lib/a.flac"]]}], token=new_token)
        self.assertEqual([r["id"] for r in self._rows()], [before["id"]])

    def test_bad_uploads_are_refused(self):
        a = self.on_card["lib/a.flac"]
        bodies: tuple[dict, ...] = ({"playlists": "x"}, {"nope": []},
                     {"playlists": [{"path": "../x.m3u", "entries": [a]}]},
                     {"playlists": [{"path": "/abs.m3u", "entries": [a]}]},
                     {"playlists": [{"path": ".trobar/x.m3u", "entries": [a]}]},
                     {"playlists": [{"path": "song.flac", "entries": [a]}]},
                     {"playlists": [{"path": "x.m3u", "entries": "a"}]},
                     {"playlists": [{"path": "x.m3u", "entries": [1]}]})
        for body in bodies:
            with self.subTest(body=body):
                resp = self.client.post("/api/device/playlists", json=body,
                                        headers={"Authorization": f"Bearer {self.token}"})
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._rows(), [])

    def test_the_caps_bound_one_upload(self):
        # The only bound on the work one device token can ask for.
        one = {"path": "x.m3u", "entries": [self.on_card["lib/a.flac"]]}
        at_cap = [dict(one, path=f"p{i}.m3u") for i in range(device_api._DEVICE_PLAYLISTS_MAX)]
        self.assertEqual(self._upload(at_cap).status_code, 200)
        over = at_cap + [dict(one, path="one-more.m3u")]
        self.assertEqual(self._upload(over).status_code, 400)
        entries = ["x/y/z.mp3"] * main._DEVICE_PLAYLIST_ENTRIES_MAX
        self.assertEqual(self._upload([{"path": "long.m3u", "entries": entries}]).status_code, 200)
        self.assertEqual(self._upload([{"path": "long.m3u", "entries": entries + ["x/y/z.mp3"]}]).status_code, 400)

    def test_a_device_token_is_required(self):
        resp = self.client.post("/api/device/playlists", json={"playlists": []})
        self.assertEqual(resp.status_code, 401)
        _login(self.client, self.owner)
        self.assertEqual(self.client.post("/api/device/playlists", json={"playlists": []}).status_code, 401)

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

    def test_a_delivered_track_is_named_as_it_is_on_the_device(self):
        # The album-artist layout names new deliveries; a file already on the
        # device keeps its delivered name, and that is the one the client stores.
        track = self._add_held_track("catalog/1.flac", fingerprint="FP", fingerprint_seq=1)
        self.conn.execute("UPDATE tracks SET album_artist = 'Various Artists' WHERE id = ?", (track,))
        self.conn.execute("UPDATE device_track_state SET delivered_path = 'Ar/Al/Ti.flac' WHERE track_id = ?",
                          (track,))
        self.conn.commit()
        body = self.client.get("/api/device/fingerprints", headers=self._auth()).get_json()
        self.assertEqual([e["path"] for e in body["entries"]], ["Ar/Al/Ti.flac"])

    def test_a_track_queued_again_is_named_by_its_new_delivery(self):
        # pending with an old delivered_path (a format change, a re-download):
        # the client is about to write the new name, so that is its path.
        track = self._add_held_track("catalog/1.flac", fingerprint="FP", fingerprint_seq=1, status="pending")
        self.conn.execute("UPDATE tracks SET album_artist = 'Various Artists' WHERE id = ?", (track,))
        self.conn.execute("UPDATE device_track_state SET delivered_path = 'Ar/Al/Ti.flac' WHERE track_id = ?",
                          (track,))
        self.conn.commit()
        body = self.client.get("/api/device/fingerprints", headers=self._auth()).get_json()
        self.assertEqual([e["path"] for e in body["entries"]], ["Various Artists/Al/Ti.flac"])

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
        entries = [{"fingerprint": "F", "path": f"p{i}"} for i in range(device_api._PROVENANCE_PUSH_MAX + 1)]
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
        # One mock under both names, so the sequence holds: the route (in
        # admin) asks first, require_admin (in web_common) second.
        ids = mock.Mock(side_effect=[999999, self.admin])
        with mock.patch.object(admin, "get_current_user_id", ids), \
                mock.patch.object(web_common, "get_current_user_id", ids):
            resp = self.client.delete(f"/api/admin/users/{self.admin}")
        self.assertEqual(ids.call_count, 2)
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
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
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
        with mock.patch.object(settings, "AUTH_MODE", "local"):
            resp = self.client.delete(f"/api/admin/users/{self.owner}")
        self.assertEqual(resp.status_code, 200)

    def test_deleting_a_non_sole_local_password_account_under_oidc_is_fine(self):
        self.conn.execute(
            "UPDATE users SET password_hash = 'x' WHERE id IN (?, ?)", (self.admin, self.owner))
        self.conn.commit()
        _login(self.client, self.admin)
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
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

    def setUp(self):
        super().setUp()
        # The failure counts are module-level: one test's wrong guesses
        # mustn't count toward the next one's limit.
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)

    def _set_password(self, user_id: int, password: str) -> None:
        self.conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (main.generate_password_hash(password), user_id))
        self.conn.commit()

    # --- /login ---

    def test_login_honours_admin_password_under_oidc(self):
        self._set_password(self.admin, "adminpass123")
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.post("/login", data={"username": "admin", "password": "adminpass123"})
        self.assertEqual(resp.status_code, 302)

    def test_login_rejects_correct_non_admin_password_under_oidc(self):
        self._set_password(self.owner, "ownerpass123")
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.post("/login", data={"username": "owner", "password": "ownerpass123"})
        self.assertEqual(resp.status_code, 200)  # re-rendered login form, not a redirect

    def test_login_honours_non_admin_password_under_local_mode(self):
        # Unchanged legacy behaviour — the oidc restriction is mode-specific.
        self._set_password(self.owner, "ownerpass123")
        with mock.patch.object(settings, "AUTH_MODE", "local"):
            resp = self.client.post("/login", data={"username": "owner", "password": "ownerpass123"})
        self.assertEqual(resp.status_code, 302)

    # --- self-service POST /api/account/password ---

    def test_self_service_password_change_blocked_for_non_admin_under_oidc(self):
        _login(self.client, self.owner)
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.post("/api/account/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 400)

    def test_self_service_password_change_allowed_for_admin_under_oidc(self):
        _login(self.client, self.admin)
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.post("/api/account/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    def test_self_service_password_change_allowed_for_non_admin_under_local_mode(self):
        _login(self.client, self.owner)
        with mock.patch.object(settings, "AUTH_MODE", "local"):
            resp = self.client.post("/api/account/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    def _change_own_password(self, current):
        body = {"password": "a brand new one"}
        if current is not None:
            body["current_password"] = current
        return self.client.post("/api/account/password", json=body)

    def _password_is(self, user_id, password):
        row = self.conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        return main.check_password_hash(row["password_hash"], password)

    def test_changing_a_password_asks_for_the_current_one(self):
        self._set_password(self.owner, "ownerpass123")
        _login(self.client, self.owner)
        for current in (None, "", "not it at all"):
            with self.subTest(current=current):
                resp = self._change_own_password(current)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["field"], "current_password")
                self.assertTrue(self._password_is(self.owner, "ownerpass123"), "unchanged")
        self.assertEqual(self._change_own_password("ownerpass123").status_code, 200)
        self.assertTrue(self._password_is(self.owner, "a brand new one"))

    def test_a_first_password_needs_no_current_one(self):
        _login(self.client, self.owner)
        self.assertEqual(self._change_own_password(None).status_code, 200)
        self.assertTrue(self._password_is(self.owner, "a brand new one"))

    def test_wrong_current_passwords_are_throttled_per_account(self):
        self._set_password(self.owner, "ownerpass123")
        _login(self.client, self.owner)
        for _ in range(10):
            self.assertEqual(self._change_own_password("guess").status_code, 400)
        # Past the limit even the right one is refused for now.
        self.assertEqual(self._change_own_password("ownerpass123").status_code, 429)
        self.assertTrue(self._password_is(self.owner, "ownerpass123"))

    def test_the_right_current_password_resets_the_count(self):
        self._set_password(self.owner, "ownerpass123")
        _login(self.client, self.owner)
        for _ in range(9):
            self.assertEqual(self._change_own_password("guess").status_code, 400)
        self.assertEqual(self._change_own_password("ownerpass123").status_code, 200)
        # Ten more wrong ones are all answered, not refused: the count began again.
        for _ in range(10):
            self.assertEqual(self._change_own_password("guess").status_code, 400)

    def test_the_profile_says_whether_there_is_a_local_password(self):
        _login(self.client, self.owner)
        self.assertIs(self.client.get("/api/profile").get_json()["has_local_password"], False)
        self._set_password(self.owner, "ownerpass123")
        self.assertIs(self.client.get("/api/profile").get_json()["has_local_password"], True)

    # --- admin-managed create POST /api/admin/users ---

    def test_creating_local_account_blocked_under_oidc(self):
        _login(self.client, self.admin)
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.post(
                "/api/admin/users", json={"username": "newuser", "password": "newpassword1"})
        self.assertEqual(resp.status_code, 400)

    def test_creating_local_account_allowed_under_local_mode(self):
        _login(self.client, self.admin)
        with mock.patch.object(settings, "AUTH_MODE", "local"):
            resp = self.client.post(
                "/api/admin/users", json={"username": "newuser", "password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    # --- admin-managed reset PUT /api/admin/users/<id>/password ---

    def test_admin_reset_blocked_for_non_admin_target_under_oidc(self):
        _login(self.client, self.admin)
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.put(
                f"/api/admin/users/{self.owner}/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 400)

    def test_admin_reset_allowed_for_admin_target_under_oidc(self):
        other_admin = self._make_user("second-admin", is_admin=True)
        _login(self.client, self.admin)
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.put(
                f"/api/admin/users/{other_admin}/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    def test_admin_reset_allowed_for_non_admin_target_under_local_mode(self):
        _login(self.client, self.admin)
        with mock.patch.object(settings, "AUTH_MODE", "local"):
            resp = self.client.put(
                f"/api/admin/users/{self.owner}/password", json={"password": "newpassword1"})
        self.assertEqual(resp.status_code, 200)

    def test_admin_reset_404_for_nonexistent_user_under_oidc(self):
        _login(self.client, self.admin)
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
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
        result = web_common._normalize_dashboard_widgets({"disabled": []})
        self.assertEqual(result["settings"]["cover_limit"], 15)

    def test_default_for_non_dict_input(self):
        result = web_common._normalize_dashboard_widgets(None)
        self.assertEqual(result["settings"]["cover_limit"], 15)

    def test_valid_values_pass_through(self):
        for n in (15, 30, 45, 60):
            with self.subTest(n=n):
                result = web_common._normalize_dashboard_widgets({"settings": {"cover_limit": n}})
                self.assertEqual(result["settings"]["cover_limit"], n)

    def test_out_of_range_value_clamped_to_default(self):
        result = web_common._normalize_dashboard_widgets({"settings": {"cover_limit": 999}})
        self.assertEqual(result["settings"]["cover_limit"], 15)

    def test_non_numeric_value_clamped_to_default(self):
        result = web_common._normalize_dashboard_widgets({"settings": {"cover_limit": "lots"}})
        self.assertEqual(result["settings"]["cover_limit"], 15)

    def test_other_settings_keys_preserved(self):
        # Per-widget settings (e.g. recently_added's "months") share the
        # same dict — validating cover_limit must not drop them.
        result = web_common._normalize_dashboard_widgets(
            {"settings": {"recently_added": {"months": 6}, "cover_limit": 30}})
        self.assertEqual(result["settings"]["recently_added"], {"months": 6, "cover_limit": 30})  # its count from the single value
        self.assertEqual(result["settings"]["cover_limit"], 30)


class DashboardWidgetsPerWidgetCoverLimitTests(_RouteTestBase):
    """Each cover widget has its own cover count. A set stored with the old
    single value reads as that value for all three; the single value stays
    in every answer, as Suggestions' count, for an app built before the
    split; and a PATCH of the single value, which such an app sends, sets
    all three."""

    def _patch(self, body):
        _login(self.client, self.owner)
        return self.client.patch("/api/profile/dashboard-widgets", json=body)

    def test_an_older_set_reads_as_its_single_value_for_each_cover_widget(self):
        result = web_common._normalize_dashboard_widgets({"settings": {"cover_limit": 45}})
        for widget in ("suggestions", "recently_added", "recently_released"):
            self.assertEqual(result["settings"][widget]["cover_limit"], 45)
        self.assertEqual(result["settings"]["cover_limit"], 45)

    def test_a_widgets_own_count_changes_that_widget_only(self):
        body = self._patch({"settings": {"recently_added": {"cover_limit": 60}}}).get_json()
        self.assertEqual(body["settings"]["recently_added"]["cover_limit"], 60)
        self.assertEqual(body["settings"]["suggestions"]["cover_limit"], 15)
        self.assertEqual(body["settings"]["recently_released"]["cover_limit"], 15)
        # The single value an older app reads is Suggestions' count.
        self.assertEqual(body["settings"]["cover_limit"], 15)
        body = self._patch({"settings": {"suggestions": {"cover_limit": 30}}}).get_json()
        self.assertEqual(body["settings"]["cover_limit"], 30)

    def test_the_single_value_an_older_app_sends_sets_all_three(self):
        self._patch({"settings": {"recently_added": {"cover_limit": 60}}})
        body = self._patch({"settings": {"cover_limit": 45}}).get_json()
        for widget in ("suggestions", "recently_added", "recently_released"):
            self.assertEqual(body["settings"][widget]["cover_limit"], 45)

    def test_a_months_change_keeps_the_widgets_count_and_the_other_way_round(self):
        self._patch({"settings": {"recently_added": {"cover_limit": 60}}})
        body = self._patch({"settings": {"recently_added": {"months": 9}}}).get_json()
        self.assertEqual(body["settings"]["recently_added"], {"cover_limit": 60, "months": 9})
        body = self._patch({"settings": {"recently_added": {"cover_limit": 30}}}).get_json()
        self.assertEqual(body["settings"]["recently_added"], {"cover_limit": 30, "months": 9})

    def test_a_malformed_per_widget_setting_is_refused(self):
        bad_settings: list[dict] = [
            {"recently_added": {"cover_limit": 7}},
            {"recently_added": {"cover_limit": True}},
            {"suggestions": {"months": 3}},  # Suggestions has no months
            {"suggestions": {"colour": "red"}},
            {"suggestions": {}},
            {"suggestions": 30},
        ]
        for bad in bad_settings:
            with self.subTest(settings=bad):
                self.assertEqual(self._patch({"settings": bad}).status_code, 400)

    def test_a_device_set_has_the_same_per_widget_counts(self):
        _phone, token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        headers = {"Authorization": f"Bearer {token}"}
        body = self.client.patch("/api/app/dashboard/widgets", headers=headers,
                                 json={"settings": {"recently_released": {"cover_limit": 30}}}).get_json()
        self.assertEqual(body["settings"]["recently_released"]["cover_limit"], 30)
        self.assertEqual(body["settings"]["suggestions"]["cover_limit"], 15)


class DashboardWidgetsOrderTests(unittest.TestCase):
    """#263: dashboard_widgets.order — the server only checks "is this a
    list", since the widget catalog it orders lives client-side (a stale
    or unknown id in the list is the frontend's problem to fall back on,
    not something the server can validate without duplicating the catalog)."""

    def test_default_empty_when_absent(self):
        result = web_common._normalize_dashboard_widgets({"disabled": []})
        self.assertEqual(result["order"], [])

    def test_default_empty_for_non_dict_input(self):
        result = web_common._normalize_dashboard_widgets(None)
        self.assertEqual(result["order"], [])

    def test_valid_list_passes_through(self):
        result = web_common._normalize_dashboard_widgets({"order": ["devices", "library"]})
        self.assertEqual(result["order"], ["devices", "library"])

    def test_non_list_value_falls_back_to_empty(self):
        result = web_common._normalize_dashboard_widgets({"order": "devices,library"})
        self.assertEqual(result["order"], [])

    def test_order_does_not_disturb_disabled_or_settings(self):
        result = web_common._normalize_dashboard_widgets(
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


class ProfileAdminOnlyWidgetRouteTests(_RouteTestBase):
    """The whole-profile PUT carries the same admin-only union the narrow
    PATCH does: a non-admin's request can hide the administration widget
    further, never un-hide it. The PATCH path had three tests on this and
    the PUT path none, so neutralising the PUT's union failed nothing."""

    def test_a_non_admin_put_cannot_re_enable_the_admin_widget(self):
        _login(self.client, self.owner)
        resp = self.client.put("/api/profile", json={"dashboard_widgets": {"disabled": [], "order": [], "settings": {}}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["dashboard_widgets"]["disabled"], ["administration"])
        self.assertEqual(self.client.get("/api/profile").get_json()["dashboard_widgets"]["disabled"], ["administration"])

    def test_an_admin_put_keeps_the_list_it_sent(self):
        _login(self.client, self.admin)
        resp = self.client.put("/api/profile", json={"dashboard_widgets": {"disabled": [], "order": [], "settings": {}}})
        self.assertEqual(resp.get_json()["dashboard_widgets"]["disabled"], [])


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

    def test_with_sources_says_which_source_did_not_answer(self):
        # The web page asks for the sources beside the items, so an outage
        # isn't shown as "nothing played".
        import requests
        self.conn.execute(
            "UPDATE users SET lastfm_username = 'alice', lastfm_api_key = 'k', "
            "listenbrainz_username = 'alice' WHERE id = ?", (self.owner,))
        self.conn.commit()
        lb_body = {"payload": {"release_groups": [
            {"artist_name": "B", "release_group_name": "High playcount", "listen_count": 99},
        ]}}
        _login(self.client, self.owner)
        with mock.patch("requests.get", side_effect=[
                requests.ConnectionError("down"), self._mock_response(lb_body)]):
            body = self.client.get("/api/suggestions/most-played?with_sources=1").get_json()
        self.assertEqual([r["album"] for r in body["items"]], ["High playcount"])
        self.assertEqual(body["sources"], {"lastfm": "error", "listenbrainz": "ok", "maloja": "unconfigured",
                             "device_plays": "unconfigured"})

    def test_fresh_asks_the_service_again(self):
        # The module turns the cache off; here it is on, as in production.
        self.conn.execute("UPDATE users SET lastfm_username = 'alice', lastfm_api_key = 'k' WHERE id = ?",
                          (self.owner,))
        self.conn.commit()
        body = {"topalbums": {"album": [{"artist": {"name": "A"}, "name": "Kept", "playcount": "5"}]}}
        _login(self.client, self.owner)
        history_cache.clear()
        self.addCleanup(history_cache.clear)
        with mock.patch.object(history_cache, "TTL_SECONDS", 300), \
                mock.patch("requests.get", return_value=self._mock_response(body)) as get:
            self.client.get("/api/suggestions/most-played")
            self.client.get("/api/suggestions/most-played")
            self.assertEqual(get.call_count, 1, "the second is answered from memory")
            self.client.get("/api/suggestions/most-played?fresh=1")
            self.assertEqual(get.call_count, 2)

    def test_fresh_asks_the_services_again_for_suggestions_too(self):
        # The Suggestions tab's Refresh and Retry hit this route.
        self.conn.execute("UPDATE users SET lastfm_username = 'alice', lastfm_api_key = 'k' WHERE id = ?",
                          (self.owner,))
        self.conn.commit()
        body: dict = {"topalbums": {"album": []}, "recenttracks": {"track": []}}
        _login(self.client, self.owner)
        history_cache.clear()
        self.addCleanup(history_cache.clear)
        with mock.patch.object(history_cache, "TTL_SECONDS", 300), \
                mock.patch("requests.get", return_value=self._mock_response(body)) as get:
            self.client.get("/api/suggestions")
            asked = get.call_count  # top albums and recent tracks
            self.assertGreater(asked, 0)
            self.client.get("/api/suggestions")
            self.assertEqual(get.call_count, asked, "the second is answered from memory")
            self.client.get("/api/suggestions?fresh=1")
            self.assertEqual(get.call_count, 2 * asked)

    def test_without_with_sources_the_answer_is_still_a_bare_list(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/api/suggestions/most-played").get_json(), [])
        self.assertIsInstance(self.client.get("/api/suggestions").get_json(), list)
        body = self.client.get("/api/suggestions?with_sources=1").get_json()
        self.assertEqual(set(body), {"items", "sources"})
        self.assertEqual(body["sources"], {"lastfm": "unconfigured", "listenbrainz": "unconfigured",
                                           "maloja": "unconfigured",
                             "device_plays": "unconfigured"})

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


class MalojaRouteTests(_RouteTestBase):
    """Maloja as a third listening-history source: a per-user URL on the
    Profile, validated like the other URLs, counted by the configured flag
    and the status badge, and merged with Last.fm/ListenBrainz with one copy
    per album."""

    _CHART = {"status": "ok", "list": [
        {"scrobbles": 40, "album": {"artists": ["Ash", "Cinder"], "albumtitle": "Emberwake"}, "rank": 1},
        {"scrobbles": 7, "album": {"artists": ["Glass Meridian"], "albumtitle": "Latitude"}, "rank": 2},
    ]}

    def _response(self, body):
        resp = mock.Mock()
        resp.json.return_value = body
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        return resp

    @contextlib.contextmanager
    def _http(self, **kw):
        """One fake for every source: Last.fm and ListenBrainz call
        requests.get, Maloja a url_guard session."""
        fake = mock.Mock(**kw)
        with mock.patch("requests.get", fake), mock.patch.object(main.url_guard.GuardedSession, "get", fake):
            yield fake

    def _set_url(self, url):
        self.conn.execute("UPDATE users SET maloja_url = ? WHERE id = ?", (url, self.owner))
        self.conn.commit()

    def test_the_profile_stores_a_url_and_refuses_a_malformed_one(self):
        _login(self.client, self.owner)
        resp = self.client.put("/api/profile", json={"maloja_url": " http://192.168.1.10:42010 "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["maloja_url"], "http://192.168.1.10:42010")
        resp = self.client.put("/api/profile", json={"maloja_url": "maloja.local"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["field"], "maloja_url")
        self.assertEqual(self.client.get("/api/profile").get_json()["maloja_url"], "http://192.168.1.10:42010")
        self.assertEqual(self.client.put("/api/profile", json={"maloja_url": ""}).get_json()["maloja_url"], None)

    def test_lan_addresses_are_accepted(self):
        # A Maloja on the home network is the main use: no DNS involved here.
        _login(self.client, self.owner)
        for host in ("10.0.0.5", "172.16.0.5", "192.168.1.10", "[fd00::5]"):
            with self.subTest(host=host):
                url = f"http://{host}:42010"
                resp = self.client.put("/api/profile", json={"maloja_url": url})
                self.assertEqual((resp.status_code, resp.get_json()["maloja_url"]), (200, url))

    def test_each_blocked_range_is_refused_when_saved(self):
        _login(self.client, self.owner)
        self.client.put("/api/profile", json={"maloja_url": "http://192.168.1.10:42010"})
        cases = {
            "127.0.0.1": "Loopback", "127.8.9.10": "Loopback", "[::1]": "Loopback",
            "[::ffff:127.0.0.1]": "Loopback",
            "169.254.169.254": "Link-local", "[fe80::1]": "Link-local", "[::ffff:169.254.169.254]": "Link-local",
            "0.0.0.0": "unspecified", "[::]": "unspecified",
            "224.0.0.1": "Multicast", "[ff02::1]": "Multicast",
        }
        for host, word in cases.items():
            with self.subTest(host=host):
                resp = self.client.put("/api/profile", json={"maloja_url": f"http://{host}:42010"})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["field"], "maloja_url")
                self.assertIn(word, resp.get_json()["error"])
                self.assertTrue(resp.get_json()["error"].endswith("(Maloja URL)"))
        self.assertEqual(self.client.get("/api/profile").get_json()["maloja_url"], "http://192.168.1.10:42010")

    def test_a_hostname_resolving_to_loopback_is_refused_when_saved(self):
        _login(self.client, self.owner)
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 42010))]
        with mock.patch("socket.getaddrinfo", return_value=answer) as resolve:
            resp = self.client.put("/api/profile", json={"maloja_url": "http://maloja.example.invalid:42010"})
        self.assertEqual(resolve.call_args.args[:2], ("maloja.example.invalid", 42010))
        self.assertEqual((resp.status_code, resp.get_json()["field"]), (400, "maloja_url"))
        self.assertIn("Loopback", resp.get_json()["error"])

    def test_a_hostname_that_does_not_resolve_is_saved(self):
        # It can't be reached either, and every request checks again.
        _login(self.client, self.owner)
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
            resp = self.client.put("/api/profile", json={"maloja_url": "http://maloja.example.invalid:42010"})
        self.assertEqual(resp.status_code, 200)

    def test_the_status_badge(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/api/maloja/status").get_json(), {"configured": False, "ok": False})
        self._set_url("http://maloja.local:42010")
        with self._http(return_value=self._response({"versionstring": "3.2.6"})):
            self.assertEqual(self.client.get("/api/maloja/status").get_json(), {"configured": True, "ok": True})

    def test_maloja_alone_counts_as_configured_for_the_app(self):
        # The App API's flag, which the Android app reads: with Maloja as the
        # only source the feed is configured, not "set up a scrobbler".
        _device_id, token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        headers = {"Authorization": f"Bearer {token}"}
        self.assertFalse(self.client.get("/api/app/suggestions/most-played", headers=headers).get_json()["configured"])
        self._set_url("http://maloja.local:42010")
        with self._http(return_value=self._response(self._CHART)):
            body = self.client.get("/api/app/suggestions/most-played", headers=headers).get_json()
        self.assertTrue(body["configured"])
        self.assertEqual([i["album"] for i in body["items"]], ["Emberwake", "Latitude"])

    def test_most_played_merges_with_listenbrainz_keeping_one_copy(self):
        self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                          "VALUES ('e.flac', 'Cinder & Ash', 'Emberwake', 'T', 1, 0)")
        self.conn.execute("UPDATE users SET listenbrainz_username = 'alice', maloja_url = ? WHERE id = ?",
                          ("http://maloja.local:42010", self.owner))
        self.conn.commit()
        lb = {"payload": {"release_groups": [
            {"artist_name": "Cinder & Ash", "release_group_name": "Emberwake", "listen_count": 12}]}}

        def get(url, **_kw):
            return self._response(self._CHART if "maloja" in url else lb)

        _login(self.client, self.owner)
        with self._http(side_effect=get):
            rows = self.client.get("/api/suggestions/most-played?period=overall").get_json()
        self.assertEqual([(r["artist"], r["album"], r["playcount"]) for r in rows],
                         [("Cinder & Ash", "Emberwake", 40), ("Glass Meridian", "Latitude", 7)])

    def test_suggestions_come_from_maloja_alone(self):
        self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                          "VALUES ('e.flac', 'Cinder & Ash', 'Emberwake', 'T', 1, 0)")
        self._set_url("http://maloja.local:42010")

        def get(url, **_kw):
            return self._response(self._CHART if "charts" in url else {"status": "ok", "list": []})

        _login(self.client, self.owner)
        # Recently-added would name every album of a library this small first.
        with self._http(side_effect=get), \
             mock.patch.object(main.suggestions, "recently_added", return_value=[]):
            rows = self.client.get("/api/suggestions?period=overall").get_json()
        self.assertEqual([(r["artist"], r["album"], r["source"]) for r in rows],
                         [("Cinder & Ash", "Emberwake", "maloja")])

    def test_suggestions_include_maloja_and_dedup_against_last_fm(self):
        self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                          "VALUES ('e.flac', 'Cinder & Ash', 'Emberwake', 'T', 1, 0)")
        self.conn.execute("UPDATE users SET lastfm_username = 'alice', lastfm_api_key = 'k', maloja_url = ? "
                          "WHERE id = ?", ("http://maloja.local:42010", self.owner))
        self.conn.commit()
        lastfm_top = {"topalbums": {"album": [
            {"artist": {"name": "Cinder & Ash"}, "name": "Emberwake", "playcount": "3", "image": []}]}}

        def get(url, **kw):
            if "maloja" in url:
                return self._response(self._CHART if "charts" in url else {"status": "ok", "list": []})
            return self._response(lastfm_top if kw.get("params", {}).get("method") == "user.gettopalbums"
                                  else {"recenttracks": {"track": []}})

        _login(self.client, self.owner)
        with self._http(side_effect=get):
            rows = self.client.get("/api/suggestions?period=overall").get_json()
        # Recently added, Last.fm and Maloja all name it: one copy (recently
        # added comes first in the feed's priority order).
        self.assertEqual([r["album"] for r in rows].count("Emberwake"), 1)


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


class LibraryAlbumTracksRouteTests(_RouteTestBase):
    """GET /api/library/album-tracks: an album's tracks in disc and track
    order, the album as selecting it syncs (its album artist, or one of its
    track artists)."""

    def _track(self, title, artist, disc, no, album="Event Horizon", album_artist="Aphelion", deleted=False):
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album_artist, album, title, disc_no, track_no, duration, "
            "size, mtime, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, 241.6, 1, 0, ?)",
            (f"{album}/{disc}-{no}.flac", artist, album_artist, album, title, disc, no,
             "2026-01-01" if deleted else None))
        self.conn.commit()

    def _get(self, artist, album="Event Horizon"):
        _login(self.client, self.owner)
        resp = self.client.get("/api/library/album-tracks", query_string={"artist": artist, "album": album})
        self.assertEqual(resp.status_code, 200)
        return [(r["disc_no"], r["track_no"], r["title"], r["artist"]) for r in resp.get_json()]

    def test_tracks_come_in_disc_then_track_order_with_their_artist(self):
        self._track("Afterglow", "Aphelion", 2, 1)
        self._track("Perihelion", "Aphelion feat. Cinder", 1, 2)
        self._track("Singularity", "Aphelion", 1, 1)
        self._track("Gone", "Aphelion", 1, 3, deleted=True)
        self.assertEqual(self._get("Aphelion"), [
            (1, 1, "Singularity", "Aphelion"), (1, 2, "Perihelion", "Aphelion feat. Cinder"),
            (2, 1, "Afterglow", "Aphelion")])

    def test_by_a_track_artist_it_is_that_artists_tracks(self):
        self._track("One", "Glass Meridian", 1, 1, album="Night Drive", album_artist="Various Artists")
        self._track("Two", "Low Orbit", 1, 2, album="Night Drive", album_artist="Various Artists")
        self.assertEqual([t[2] for t in self._get("Various Artists", "Night Drive")], ["One", "Two"])
        self.assertEqual([t[2] for t in self._get("Low Orbit", "Night Drive")], ["Two"])

    def test_it_needs_a_session(self):
        self.assertEqual(self.client.get("/api/library/album-tracks?artist=A&album=B").status_code, 401)


class LibraryAlbumArtistTests(_RouteTestBase):
    """#240: the web Library groups albums by album artist, and an artist's
    page lists the albums they only appear on. The App API doesn't change."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        for path, artist, album_artist, album, title in (
                ("n/1.flac", "Glass Meridian", "Various Artists", "Night Drive", "One"),
                ("n/2.flac", "Low Orbit", "Various Artists", "Night Drive", "Two"),
                ("n/3.flac", "Low Orbit", "Various Artists", "Night Drive", "Three"),
                ("e/1.flac", "Aphelion", "Aphelion", "Event Horizon", "Singularity"),
                ("e/2.flac", "Aphelion feat. Cinder", "Aphelion", "Event Horizon", "Perihelion"),
                ("l/1.flac", "Glass Meridian", "Glass Meridian", "Latitude", "Song")):
            self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album_artist, album, title, size, mtime) "
                "VALUES (?, ?, ?, ?, ?, 1, 0)", (path, artist, album_artist, album, title))
        self.conn.commit()
        _login(self.client, self.owner)

    def _get(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_a_compilation_is_one_album_under_its_album_artist(self):
        artists = {a["artist"]: a for a in self._get("/api/library/artists")}
        self.assertEqual((artists["Various Artists"]["album_count"], artists["Various Artists"]["track_count"]), (1, 3))
        self.assertEqual(artists["Various Artists"]["albums"], ["Night Drive"])
        # Glass Meridian: one album of their own, and they appear on one.
        self.assertEqual((artists["Glass Meridian"]["album_count"], artists["Glass Meridian"]["appears_on_count"]), (1, 1))
        # Low Orbit only appears on others' albums, and is still listed.
        self.assertEqual((artists["Low Orbit"]["album_count"], artists["Low Orbit"]["appears_on_count"]), (0, 1))
        # A guest track counts with its album, not as an artist of its own album.
        self.assertEqual(artists["Aphelion"]["track_count"], 2)
        self.assertEqual(artists["Aphelion feat. Cinder"]["album_count"], 0)

    def test_an_artists_albums_are_their_own(self):
        self.assertEqual([a["album"] for a in self._get("/api/library/albums?artist=Glass%20Meridian")], ["Latitude"])
        self.assertEqual([(a["album"], a["track_count"]) for a in
                          self._get("/api/library/albums?artist=Various%20Artists")], [("Night Drive", 3)])

    def test_also_appears_on_lists_others_albums_with_their_track_count(self):
        self.assertEqual([(a["album"], a["album_artist"], a["track_count"]) for a in
                          self._get("/api/library/appears-on?artist=Low%20Orbit")],
                         [("Night Drive", "Various Artists", 2)])
        self.assertEqual(self._get("/api/library/appears-on?artist=Aphelion"), [])

    def _app(self, path):
        return self.client.get(path, headers={"Authorization": f"Bearer {self.token}"})

    def test_the_app_api_serves_the_browsers_grouping_beside_its_own(self):
        for app, session in (("album-artists", "artists"),
                             ("album-artist-albums?artist=Various%20Artists", "albums?artist=Various%20Artists"),
                             ("appears-on?artist=Low%20Orbit", "appears-on?artist=Low%20Orbit")):
            with self.subTest(route=app):
                resp = self._app(f"/api/app/library/{app}")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_json(), self._get(f"/api/library/{session}"))
                self.assertTrue(resp.get_json())

    def test_those_routes_need_the_device_token(self):
        for route in ("album-artists", "album-artist-albums?artist=A", "appears-on?artist=A"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(f"/api/app/library/{route}").status_code, 401)

    def test_the_row_counts_what_the_artists_page_lists(self):
        # The same album name under two album artists is two albums to appear on.
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album_artist, album, title, size, mtime) "
            "VALUES ('o/1.flac', 'Low Orbit', 'Someone Else', 'Night Drive', 'Other', 1, 0)")
        self.conn.commit()
        rows = {a["artist"]: a for a in self._get("/api/library/artists")}
        self.assertEqual(rows["Low Orbit"]["appears_on_count"], 2)
        for name, row in rows.items():
            with self.subTest(artist=name):
                query = urllib.parse.quote(name)
                self.assertEqual(row["appears_on_count"], len(self._get(f"/api/library/appears-on?artist={query}")))
                self.assertEqual(row["album_count"], len(self._get(f"/api/library/albums?artist={query}")))

    def test_the_app_api_still_groups_by_track_artist(self):
        resp = self.client.get("/api/app/library/artists", headers={"Authorization": f"Bearer {self.token}"})
        names = {a["artist"] for a in resp.get_json()}
        self.assertNotIn("Various Artists", names)
        self.assertIn("Low Orbit", names)

    def test_a_selection_of_one_artists_tracks_says_whose_album_it_is(self):
        for target in ("Low Orbit||Night Drive", "Various Artists||Night Drive", "Glass Meridian"):
            sync_state.create_selection(self.conn, "album" if "||" in target else "artist", target,
                                        self.owner, [self.device])
        sels = {s["target"]: s["appears_on"] for s in self._get("/api/selections")}
        self.assertEqual(sels["Low Orbit||Night Drive"], {"album_artist": "Various Artists", "tracks": 2, "of": 3})
        self.assertIsNone(sels["Various Artists||Night Drive"], "the whole album")
        self.assertIsNone(sels["Glass Meridian"], "not an album")

    def test_a_whole_album_selection_carries_its_track_count(self):
        for target in ("Low Orbit||Night Drive", "Various Artists||Night Drive", "Glass Meridian||Latitude",
                       "Glass Meridian", "Nobody||Gone"):
            sync_state.create_selection(self.conn, "album" if "||" in target else "artist", target,
                                        self.owner, [self.device])
        counts = {s["target"]: s["album_tracks"] for s in self._get("/api/selections")}
        self.assertEqual(counts["Various Artists||Night Drive"], 3)
        self.assertEqual(counts["Glass Meridian||Latitude"], 1)
        self.assertIsNone(counts["Low Orbit||Night Drive"], "one artist's tracks: appears_on says it")
        self.assertIsNone(counts["Glass Meridian"], "not an album")
        self.assertIsNone(counts["Nobody||Gone"], "no tracks left in the library")

    def test_the_whole_album_suffix_reads_right_in_french(self):
        from babel.messages.pofile import read_po
        with main.app.test_request_context("/"):
            ids = main._build_js_i18n()["wholeAlbumRow"]
        with open(os.path.join(os.path.dirname(main.__file__), "translations", "fr", "LC_MESSAGES", "messages.po"), "rb") as f:
            fr = read_po(f, locale="fr")
        self.assertEqual(str(fr[ids["other"]].string).replace("{n}", "14"), "album entier, 14 morceaux")
        self.assertEqual(str(fr[ids["one"]].string).replace("{n}", "1"), "album entier, 1 morceau")

    def test_a_cover_and_add_to_playlist_find_the_album_by_its_album_artist(self):
        self.assertIsNotNone(main._library_cover_row(self.conn, "Various Artists", "Night Drive"))
        ids = web_common._album_track_ids(self.conn, {"albums": [{"artist": "Various Artists", "album": "Night Drive"}]})
        self.assertEqual(len(ids), 3)
        # From "Also appears on": one artist's tracks on it.
        ids = web_common._album_track_ids(self.conn, {"albums": [{"artist": "Low Orbit", "album": "Night Drive"}]})
        self.assertEqual(len(ids), 2)

    def test_the_selections_row_count_reads_right_in_french(self):
        # The page picks the plural form from n, the album's total, so the
        # noun has to follow the total: one guest track on a compilation.
        from babel.messages.pofile import read_po
        with main.app.test_request_context("/"):
            ids = main._build_js_i18n()["appearsOnRow"]
        with open(os.path.join(os.path.dirname(main.__file__), "translations", "fr", "LC_MESSAGES", "messages.po"), "rb") as f:
            fr = read_po(f, locale="fr")
        fill = lambda form, count, n: str(fr[ids[form]].string).replace("{count}", str(count)).replace("{n}", str(n))
        self.assertEqual(fill("other", 1, 14), "1 sur 14 morceaux")
        self.assertEqual(fill("one", 1, 1), "1 sur 1 morceau")

    def test_counts_against_a_total_read_right_in_french(self):
        # Same rule for the upload results and the mirror line: the form is
        # picked from n, the total, so the noun follows the total.
        from babel.messages.pofile import read_po
        with main.app.test_request_context("/"):
            js = main._build_js_i18n()
        with open(os.path.join(os.path.dirname(main.__file__), "translations", "fr", "LC_MESSAGES", "messages.po"), "rb") as f:
            fr = read_po(f, locale="fr")

        def fill(key, form, **values):
            text = str(fr[js[key][form]].string)
            for name, value in values.items():
                text = text.replace("{" + name + "}", str(value))
            return text

        self.assertEqual(fill("playlistUploaded", "other", title="Mix", matched=1, n=14),
                         "« Mix » envoyée : 1 sur 14 titres associés.")
        self.assertEqual(fill("playlistUploaded", "one", title="Mix", matched=1, n=1),
                         "« Mix » envoyée : 1 sur 1 titre associé.")
        self.assertEqual(fill("playlistUploadReplaced", "other", title="Mix", matched=1, n=14),
                         "« Mix » remplacée : 1 sur 14 titres associés.")
        self.assertEqual(fill("playlistUploadReplaced", "one", title="Mix", matched=0, n=1),
                         "« Mix » remplacée : 0 sur 1 titre associé.")
        self.assertEqual(fill("mirrorSomeOnTarget", "other", on=1, n=14, provider="Plex"),
                         "1 sur 14 morceaux sur Plex")
        self.assertEqual(fill("mirrorSomeOnTarget", "one", on=1, n=1, provider="Plex"),
                         "1 sur 1 morceau sur Plex")


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



class AvatarRouteTests(_RouteTestBase):
    """POST/DELETE /api/profile/avatar and GET /api/profile/avatar-image (#259):
    a user's own picture, an upload checked for type and size, and a stored
    name that can't reach outside the avatar directory."""

    PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.avatar_dir = Path(tmp.name) / "avatars"
        patcher = mock.patch.object(user_profile, "AVATAR_DIR", self.avatar_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        _login(self.client, self.owner)

    def _upload(self, data, mimetype="image/png", filename="me.png"):
        return self.client.post("/api/profile/avatar", data={
            "avatar": (io.BytesIO(data), filename, mimetype)}, content_type="multipart/form-data")

    def _stored(self, user_id):
        return self.conn.execute("SELECT avatar_path FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    def test_an_upload_is_stored_under_the_users_id_and_served_back(self):
        self.assertEqual(self._upload(self.PNG).status_code, 200)
        self.assertEqual(self._stored(self.owner), f"{self.owner}.png")
        resp = self.client.get("/api/profile/avatar-image")
        self.assertEqual((resp.status_code, resp.mimetype, resp.data), (200, "image/png", self.PNG))

    def test_anything_but_jpeg_png_or_webp_is_refused(self):
        resp = self._upload(b"<svg onload=alert(1)>", mimetype="image/svg+xml", filename="x.svg")
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._stored(self.owner))
        self.assertFalse(self.avatar_dir.exists() and any(self.avatar_dir.iterdir()))

    def test_an_image_over_two_megabytes_is_refused(self):
        resp = self._upload(b"\0" * (user_profile.AVATAR_MAX_BYTES + 1))
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._stored(self.owner))

    def test_a_request_without_a_file_is_refused(self):
        resp = self.client.post("/api/profile/avatar", data={}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)

    def test_a_new_upload_replaces_the_old_file(self):
        self._upload(self.PNG)
        self._upload(b"\xff\xd8\xff" + b"\0" * 16, mimetype="image/jpeg", filename="me.jpg")
        self.assertEqual(self._stored(self.owner), f"{self.owner}.jpg")
        self.assertEqual(sorted(p.name for p in self.avatar_dir.iterdir()), [f"{self.owner}.jpg"])

    def test_delete_removes_the_file_and_the_picture(self):
        self._upload(self.PNG)
        self.assertEqual(self.client.delete("/api/profile/avatar").status_code, 200)
        self.assertIsNone(self._stored(self.owner))
        self.assertEqual(list(self.avatar_dir.iterdir()), [])
        self.assertEqual(self.client.get("/api/profile/avatar-image").status_code, 404)

    def test_each_user_only_ever_gets_their_own_picture(self):
        self._upload(self.PNG)
        _login(self.client, self.other)
        self.assertEqual(self.client.get("/api/profile/avatar-image").status_code, 404)

    def test_a_stored_name_outside_the_directory_is_neither_served_nor_deleted(self):
        # The directory exists, as on any server that has had one upload:
        # without it, "avatars/../secret.png" can't resolve at all, and the
        # test would pass whether or not the route checks containment.
        self.avatar_dir.mkdir()
        outside = self.avatar_dir.parent / "secret.png"
        outside.write_bytes(self.PNG)
        self.conn.execute("UPDATE users SET avatar_path = '../secret.png' WHERE id = ?", (self.owner,))
        self.conn.commit()
        self.assertEqual(self.client.get("/api/profile/avatar-image").status_code, 404)
        self.client.delete("/api/profile/avatar")
        self.assertTrue(outside.exists())

    def test_signed_out_is_refused(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.client.get("/api/profile/avatar-image").status_code, 401)
        self.assertEqual(self._upload(self.PNG).status_code, 401)
        self.assertEqual(self.client.delete("/api/profile/avatar").status_code, 401)



class OidcCallbackRouteTests(_RouteTestBase):
    """GET /oidc/callback (#259): a session starts only from a token that
    validates and names a user. Authlib's own validation (signature, iss/aud,
    exp, nonce) is stubbed here: what's tested is what the route does with
    its answer."""

    def setUp(self):
        super().setUp()
        self.oauth = mock.MagicMock()
        for target, name, value in ((settings, "AUTH_MODE", "oidc"), (auth, "oauth", self.oauth)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _session(self):
        with self.client.session_transaction() as sess:
            return dict(sess)

    def _user_ids(self, username):
        return [r[0] for r in self.conn.execute("SELECT id FROM users WHERE username = ?", (username,))]

    def test_a_valid_token_signs_the_named_user_in(self):
        self.oauth.oidc.authorize_access_token.return_value = {
            "userinfo": {settings.OIDC_USERNAME_CLAIM: "carol", "email": "carol@example.invalid"}}
        resp = self.client.get("/oidc/callback")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._session().get("local_user_id"), self._user_ids("carol")[0])

    def test_an_existing_user_is_signed_in_not_duplicated(self):
        self.oauth.oidc.authorize_access_token.return_value = {
            "userinfo": {settings.OIDC_USERNAME_CLAIM: "owner"}}
        self.client.get("/oidc/callback")
        self.assertEqual(self._user_ids("owner"), [self.owner])
        self.assertEqual(self._session().get("local_user_id"), self.owner)

    def test_a_token_that_fails_validation_starts_no_session(self):
        self.oauth.oidc.authorize_access_token.side_effect = ValueError("bad signature")
        resp = self.client.get("/oidc/callback")
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("local_user_id", self._session())

    def test_a_token_without_a_username_starts_no_session_and_makes_no_user(self):
        before = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.oauth.oidc.authorize_access_token.return_value = {"userinfo": {"email": "x@example.invalid"}}
        resp = self.client.get("/oidc/callback")
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("local_user_id", self._session())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], before)

    def test_outside_oidc_mode_the_callback_does_nothing(self):
        with mock.patch.object(settings, "AUTH_MODE", "local"):
            resp = self.client.get("/oidc/callback")
        self.assertEqual(resp.status_code, 302)
        self.oauth.oidc.authorize_access_token.assert_not_called()
        self.assertNotIn("local_user_id", self._session())



class AdminDelegationRouteTests(_RouteTestBase):
    """/api/admin/delegations (#259): admin-only grants of device-management
    rights from one user over another's devices, and their revocation."""

    def setUp(self):
        super().setUp()
        self.kid = self._make_user("kid")
        self.kid_device, _ = sync_state.create_device(self.conn, self.kid, "Kid's phone", "phone")
        self.conn.commit()

    def _grant(self, grantee, target):
        return self.client.post("/api/admin/delegations",
                                json={"grantee_user_id": grantee, "target_user_id": target})

    def test_only_an_admin_can_list_grant_or_revoke(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/api/admin/delegations").status_code, 403)
        self.assertEqual(self._grant(self.owner, self.kid).status_code, 403)
        self.assertEqual(self.client.delete("/api/admin/delegations/1").status_code, 403)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM device_delegations").fetchone()[0], 0)

    def test_an_admin_grants_and_the_list_names_both_users(self):
        _login(self.client, self.admin)
        resp = self._grant(self.owner, self.kid)
        self.assertEqual(resp.status_code, 200)
        listed = self.client.get("/api/admin/delegations").get_json()
        self.assertEqual([(d["grantee_username"], d["target_username"]) for d in listed],
                         [("owner", "kid")])

    def test_a_user_cannot_be_delegated_to_themselves(self):
        _login(self.client, self.admin)
        self.assertEqual(self._grant(self.kid, self.kid).status_code, 400)

    def test_an_unknown_user_is_refused(self):
        _login(self.client, self.admin)
        for grantee, target in ((self.owner, 9999), (9999, self.kid)):
            resp = self._grant(grantee, target)
            # The message, not only the status: without the existence check
            # the insert's foreign key would still answer 400, as a duplicate.
            self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "User not found"))

    def test_a_grant_missing_a_user_or_not_a_number_is_a_400_not_a_500(self):
        _login(self.client, self.admin)
        for body in ({}, {"grantee_user_id": self.owner}, {"target_user_id": self.kid},
                     {"grantee_user_id": str(self.owner), "target_user_id": self.kid},
                     {"grantee_user_id": True, "target_user_id": self.kid}, [1, 2]):
            with self.subTest(body=body):
                resp = self.client.post("/api/admin/delegations", json=body)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM device_delegations").fetchone()[0], 0)

    def test_the_same_grant_twice_is_refused(self):
        _login(self.client, self.admin)
        self._grant(self.owner, self.kid)
        self.assertEqual(self._grant(self.owner, self.kid).status_code, 400)

    def test_revoking_drops_the_grantees_pins_on_the_targets_devices_only(self):
        _login(self.client, self.admin)
        self._grant(self.owner, self.kid)
        delegation = self.conn.execute("SELECT id FROM device_delegations").fetchone()[0]
        # A second grant, which the revoke must leave alone.
        self._grant(self.other, self.kid)
        own_device, _ = sync_state.create_device(self.conn, self.other, "Bob's card", "sdcard")
        self.conn.execute("INSERT INTO device_pins (user_id, device_id) VALUES (?, ?), (?, ?)",
                          (self.owner, self.kid_device, self.owner, own_device))
        self.conn.commit()

        self.assertEqual(self.client.delete(f"/api/admin/delegations/{delegation}").status_code, 200)

        remaining = [tuple(r) for r in self.conn.execute(
            "SELECT grantee_user_id, target_user_id FROM device_delegations")]
        self.assertEqual(remaining, [(self.other, self.kid)])
        pins = [r[0] for r in self.conn.execute(
            "SELECT device_id FROM device_pins WHERE user_id = ?", (self.owner,))]
        self.assertEqual(pins, [own_device])

    def test_revoking_an_unknown_grant_is_a_no_op(self):
        _login(self.client, self.admin)
        self.assertEqual(self.client.delete("/api/admin/delegations/9999").status_code, 200)


class AdminRoonProfilesRouteTests(_RouteTestBase):
    """/api/admin/roon-profiles (#259): admin-only, and a suggestion only for
    a user who has no Roon profile mapped yet."""

    def test_only_an_admin_sees_the_roon_profiles(self):
        _login(self.client, self.owner)
        with mock.patch.object(main.roon_client, "list_profiles") as listed:
            self.assertEqual(self.client.get("/api/admin/roon-profiles").status_code, 403)
        listed.assert_not_called()

    def test_an_unmapped_user_gets_a_close_profile_as_a_suggestion(self):
        self.conn.execute("UPDATE users SET roon_profile = 'Living room' WHERE id = ?", (self.owner,))
        self.conn.commit()
        _login(self.client, self.admin)
        # "owner" is an exact match, so only the mapped-user rule keeps it
        # from being suggested to the already-mapped owner.
        profiles = {"status": "ok", "profiles": [
            {"title": "Living room"}, {"title": "owner"}, {"title": "bobb"}]}
        with mock.patch.object(main.roon_client, "list_profiles", return_value=profiles):
            body = self.client.get("/api/admin/roon-profiles").get_json()
        users = {u["username"]: u for u in body["users"]}
        self.assertEqual(users["bob"]["suggested"], "bobb")  # ratio 0.857 >= 0.85
        # Already mapped: no suggestion, whatever the names look like.
        self.assertIsNone(users["owner"]["suggested"])

    def test_a_roon_that_cannot_be_reached_is_reported_as_such(self):
        _login(self.client, self.admin)
        with mock.patch.object(main.roon_client, "list_profiles",
                               return_value={"status": "unreachable"}):
            body = self.client.get("/api/admin/roon-profiles").get_json()
        self.assertEqual(body, {"status": "unreachable"})



class DeviceSelfServiceRouteTests(_RouteTestBase):
    """PATCH /api/device/limit, POST /api/device/missing-tracks and
    GET /api/device/artist-image (#259): device-token routes, each acting on
    the calling device only."""

    def setUp(self):
        super().setUp()
        self.phone, self.phone_token = sync_state.create_device(
            self.conn, self.owner, "Phone", "phone", max_size_bytes=5_000)
        self.other_phone, self.other_token = sync_state.create_device(
            self.conn, self.other, "Bob's phone", "phone", max_size_bytes=7_000)
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('a/b.flac', 'A', 'B', 'C', 1, 0)")
        self.track = sync_state._new_id(cur)
        for device in (self.phone, self.other_phone):
            self.conn.execute(
                "INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, 'downloaded')",
                (device, self.track))
        self.conn.commit()

    def _as(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _limit(self, device):
        return self.conn.execute(
            "SELECT max_size_bytes FROM devices WHERE id = ?", (device,)).fetchone()[0]

    def _status(self, device):
        return self.conn.execute(
            "SELECT status FROM device_track_state WHERE device_id = ? AND track_id = ?",
            (device, self.track)).fetchone()[0]

    def test_a_device_sets_its_own_limit_and_no_one_elses(self):
        resp = self.client.patch("/api/device/limit", json={"max_size_bytes": 9_000},
                                 headers=self._as(self.phone_token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((self._limit(self.phone), self._limit(self.other_phone)), (9_000, 7_000))

    def test_an_invalid_limit_is_refused_and_nothing_changes(self):
        resp = self.client.patch("/api/device/limit", json={"max_size_bytes": -1},
                                 headers=self._as(self.phone_token))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._limit(self.phone), 5_000)

    def test_missing_tracks_change_only_the_calling_devices_state(self):
        resp = self.client.post("/api/device/missing-tracks", json={"redownload": [self.track]},
                                headers=self._as(self.phone_token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((self._status(self.phone), self._status(self.other_phone)),
                         ("pending", "downloaded"))

    def test_a_missing_track_can_be_excluded(self):
        self.client.post("/api/device/missing-tracks", json={"exclude": [self.track]},
                         headers=self._as(self.phone_token))
        self.assertEqual((self._status(self.phone), self._status(self.other_phone)),
                         ("excluded", "downloaded"))

    def test_missing_tracks_that_are_not_lists_of_ids_are_a_400_not_a_500(self):
        for body in ({"redownload": ["x"]}, {"exclude": [1.5]}, {"redownload": [True]},
                     {"exclude": "1"}, {"redownload": None}, [self.track]):
            with self.subTest(body=body):
                resp = self.client.post("/api/device/missing-tracks", json=body,
                                        headers=self._as(self.phone_token))
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._status(self.phone), "downloaded")

    def test_missing_tracks_with_neither_list_change_nothing(self):
        resp = self.client.post("/api/device/missing-tracks", json={},
                                headers=self._as(self.phone_token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._status(self.phone), "downloaded")

    def test_without_a_device_token_every_route_is_refused(self):
        for method, path, body in (("patch", "/api/device/limit", {"max_size_bytes": 1}),
                                   ("post", "/api/device/missing-tracks", {"redownload": [self.track]}),
                                   ("get", "/api/device/artist-image?artist=A", None)):
            with self.subTest(path=path):
                no_token: dict[str, str] = {}
                for headers in (no_token, self._as("not-a-token")):
                    resp = getattr(self.client, method)(path, json=body, headers=headers)
                    self.assertEqual(resp.status_code, 401)
        self.assertEqual(self._limit(self.phone), 5_000)
        self.assertEqual(self._status(self.phone), "downloaded")

    def test_an_artist_image_needs_an_artist_and_reports_when_there_is_none(self):
        self.assertEqual(self.client.get("/api/device/artist-image",
                                         headers=self._as(self.phone_token)).status_code, 404)
        with mock.patch.object(main.artist_images, "get_artist_image", return_value=None):
            resp = self.client.get("/api/device/artist-image?artist=Nobody",
                                   headers=self._as(self.phone_token))
        self.assertEqual(resp.status_code, 404)

    def test_an_artist_image_is_served_and_the_small_one_downscaled(self):
        with mock.patch.object(main.artist_images, "get_artist_image",
                               return_value=(b"full", "image/jpeg")), \
             mock.patch.object(main.artist_images, "downscale",
                               return_value=(b"small", "image/jpeg")) as downscale:
            full = self.client.get("/api/device/artist-image?artist=A",
                                   headers=self._as(self.phone_token))
            small = self.client.get("/api/device/artist-image?artist=A&size=small",
                                    headers=self._as(self.phone_token))
        self.assertEqual((full.status_code, full.data, small.data), (200, b"full", b"small"))
        downscale.assert_called_once()



class AboutDocRouteTests(_RouteTestBase):
    """GET /about/<doc> (#259): two named documents, nothing else."""

    def setUp(self):
        super().setUp()
        _login(self.client, self.owner)

    def test_the_license_and_the_notices_are_served_as_plain_text(self):
        for doc in ("license", "notices"):
            with self.subTest(doc=doc):
                resp = self.client.get(f"/about/{doc}")
                # Both files ship at the repo root and in the image: a missing
                # one is the regression this test is for, not a reason to skip.
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.content_type.startswith("text/plain"))

    def test_anything_but_the_two_names_is_a_404(self):
        for doc in ("LICENSE", "THIRD_PARTY_NOTICES.md", "..", "main.py", "%2e%2e%2fmain.py", "secret"):
            with self.subTest(doc=doc):
                self.assertEqual(self.client.get(f"/about/{doc}").status_code, 404)


class TidalLinkRouteTests(_RouteTestBase):
    """GET /profile/tidal/connect and /profile/tidal/callback (#259): a link
    only for a signed-in user, stored on that user only, and only when both
    the code exchange and the profile fetch succeed."""

    def setUp(self):
        super().setUp()
        self.tidal = mock.MagicMock()
        self.tidal.authorize_redirect.return_value = main.redirect("https://tidal.invalid/authorize")
        self.tidal.authorize_access_token.return_value = {"access_token": "a", "refresh_token": "r"}
        self.tidal.client_kwargs = {"scope": tidal_client.SCOPES}
        patcher = mock.patch.object(auth, "_tidal_oauth_client", return_value=self.tidal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _link(self, user_id):
        return tuple(self.conn.execute(
            "SELECT tidal_refresh_token, tidal_user_id, tidal_display_name FROM users WHERE id = ?",
            (user_id,)).fetchone())

    def _profile_ok(self):
        return mock.patch.object(main.tidal_client, "get_current_user", return_value={
            "status": "ok", "user_id": "t-1", "display_name": "Owner on Tidal"})

    def _to_login(self, resp):
        # A page route: signed out goes to the login page, not to Tidal.
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.location)

    def test_connect_signed_out_goes_to_login_not_to_tidal(self):
        self._to_login(self.client.get("/profile/tidal/connect"))
        self.tidal.authorize_redirect.assert_not_called()

    def test_connect_without_tidal_configured_is_a_400(self):
        _login(self.client, self.owner)
        with mock.patch.object(auth, "_tidal_oauth_client", return_value=None):
            self.assertEqual(self.client.get("/profile/tidal/connect").status_code, 400)

    def test_connect_redirects_to_tidal(self):
        _login(self.client, self.owner)
        resp = self.client.get("/profile/tidal/connect")
        self.assertEqual((resp.status_code, resp.location), (302, "https://tidal.invalid/authorize"))

    def test_the_callback_links_the_signed_in_user_only(self):
        _login(self.client, self.owner)
        with self._profile_ok():
            resp = self.client.get("/profile/tidal/callback")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("tidal_error", resp.location)
        self.assertEqual(self._link(self.owner), ("r", "t-1", "Owner on Tidal"))
        self.assertEqual(self._link(self.other), (None, None, None))

    def test_the_callback_signed_out_stores_nothing(self):
        with self._profile_ok():
            self._to_login(self.client.get("/profile/tidal/callback"))
        self.tidal.authorize_access_token.assert_not_called()
        self.assertEqual(self._link(self.owner), (None, None, None))

    def test_a_failed_exchange_or_profile_fetch_stores_nothing(self):
        _login(self.client, self.owner)
        cases: dict[str, dict[str, object]] = {
            "exchange raises": dict(side_effect=ValueError("bad code")),
            "no refresh token": dict(return_value={"access_token": "a"}),
        }
        for name, behaviour in cases.items():
            with self.subTest(name):
                self.tidal.authorize_access_token.reset_mock(return_value=True, side_effect=True)
                self.tidal.authorize_access_token.configure_mock(**behaviour)
                with self._profile_ok():
                    resp = self.client.get("/profile/tidal/callback")
                self.assertIn("tidal_error=1", resp.location)
        self.tidal.authorize_access_token.reset_mock(return_value=True, side_effect=True)
        self.tidal.authorize_access_token.return_value = {"access_token": "a", "refresh_token": "r"}
        with mock.patch.object(main.tidal_client, "get_current_user", return_value={"status": "error"}):
            resp = self.client.get("/profile/tidal/callback")
        self.assertIn("tidal_error=1", resp.location)
        self.assertEqual(self._link(self.owner), (None, None, None))


    def _scopes(self, user_id):
        return self.conn.execute("SELECT tidal_scopes FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    def test_the_callback_stores_the_granted_scope(self):
        _login(self.client, self.owner)
        self.tidal.authorize_access_token.return_value = {
            "access_token": "a", "refresh_token": "r", "scope": "user.read playlists.write"}
        with self._profile_ok():
            self.client.get("/profile/tidal/connect")
            self.client.get("/profile/tidal/callback")
        self.assertEqual(self._scopes(self.owner), "user.read playlists.write")

    def test_without_a_scope_in_the_answer_the_requested_one_is_stored(self):
        _login(self.client, self.owner)
        self.tidal.client_kwargs = {"scope": tidal_client.scopes(True)}
        with self._profile_ok():
            self.client.get("/profile/tidal/connect")
            self.client.get("/profile/tidal/callback")
        self.assertEqual(self._scopes(self.owner), tidal_client.scopes(True))

    def test_disconnect_clears_the_scope(self):
        _login(self.client, self.owner)
        self.conn.execute("UPDATE users SET tidal_refresh_token = 'r', tidal_scopes = 'x' WHERE id = ?",
                          (self.owner,))
        self.conn.commit()
        self.client.delete("/api/profile/tidal")
        self.assertIsNone(self._scopes(self.owner))


class TidalWriteScopeTests(_RouteTestBase):
    """playlists.write is requested only once the admin says their Tidal app
    has it; Profile asks for a reconnect when a link predates it."""

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "tidal_client_id", "cid")
        db.set_config(self.conn, "tidal_client_secret", "csecret")
        self.conn.commit()
        main.oauth._clients.pop("tidal", None)
        self.addCleanup(main.oauth._clients.pop, "tidal", None)
        _login(self.client, self.admin)

    def _registered_scope(self):
        with db.connection() as conn:
            return auth._tidal_oauth_client(conn).client_kwargs["scope"]

    def _link(self, scopes):
        self.conn.execute("UPDATE users SET tidal_refresh_token = 'r', tidal_scopes = ? WHERE id = ?",
                          (scopes, self.admin))
        self.conn.commit()

    def _profile(self):
        p = self.client.get("/api/profile").get_json()
        return p["tidal_can_share"], p["tidal_needs_reconnect"]

    def test_off_by_default_requests_the_read_scopes_only(self):
        self.assertEqual(self._registered_scope(), tidal_client.SCOPES)
        self.assertFalse(self.client.get("/api/admin/config").get_json()["tidal_playlists_write"])

    def test_ticking_the_box_re_registers_with_the_write_scope(self):
        self.assertNotIn("playlists.write", self._registered_scope())
        resp = self.client.put("/api/admin/config", json={"tidal_playlists_write": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._registered_scope().split(), tidal_client.SCOPES.split() + ["playlists.write"])
        self.assertTrue(self.client.get("/api/admin/config").get_json()["tidal_playlists_write"])
        self.client.put("/api/admin/config", json={"tidal_playlists_write": False})
        self.assertEqual(self._registered_scope(), tidal_client.SCOPES)

    def test_an_unchanged_box_keeps_the_cached_client(self):
        self.client.put("/api/admin/config", json={"tidal_playlists_write": True})
        self._registered_scope()
        first = main.oauth._clients["tidal"]
        self.client.put("/api/admin/config", json={"tidal_playlists_write": True})
        self.assertIs(main.oauth._clients["tidal"], first)

    def test_profile_flags_follow_the_box_and_the_grant(self):
        self.assertEqual(self._profile(), (False, False))       # not linked, box off
        self._link(tidal_client.SCOPES)
        self.assertEqual(self._profile(), (False, False))       # box off: no prompt at all
        db.set_config(self.conn, "tidal_playlists_write", "1")
        self.conn.commit()
        self.assertEqual(self._profile(), (False, True))        # read-only link: reconnect
        self._link(None)
        self.assertEqual(self._profile(), (False, True))        # link from before scopes were kept
        self._link(tidal_client.scopes(True))
        self.assertEqual(self._profile(), (True, False))
        self.conn.execute("UPDATE users SET tidal_refresh_token = NULL WHERE id = ?", (self.admin,))
        self.conn.commit()
        self.assertEqual(self._profile(), (False, False))       # not linked: Connect, not reconnect

    def test_has_write_scope_matches_whole_words(self):
        self.assertTrue(tidal_client.has_write_scope("a playlists.write b"))
        self.assertFalse(tidal_client.has_write_scope("playlists.writer"))
        self.assertFalse(tidal_client.has_write_scope(None))


class TidalShareRouteTests(_RouteTestBase):
    """/api/tidal-shares*: invitations, admin push, ending, and who may do
    each. Nothing here calls Tidal; the sync does (test_tidal_shares)."""

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "tidal_playlists_write", "1")
        for uid in (self.owner, self.other, self.admin):
            self.conn.execute("UPDATE users SET tidal_refresh_token = 'r', tidal_scopes = ? WHERE id = ?",
                              (tidal_client.scopes(True), uid))
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, "
            "last_synced_at) VALUES ('Road Trip', 'tidal', 'tid-1', ?, 0, datetime('now'))", (self.owner,))
        self.playlist = cur.lastrowid
        self.conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title) "
            "VALUES (?, 0, 'A', 'T'), (?, 1, 'B', 'U')", (self.playlist, self.playlist))
        self.conn.commit()

    def _create(self, who, **body):
        _login(self.client, who)
        payload = {"playlist_id": self.playlist, "recipient_user_id": self.other}
        payload.update(body)
        return self.client.post("/api/tidal-shares", json=payload)

    def _status(self, sid):
        return self.conn.execute("SELECT status FROM tidal_shares WHERE id = ?", (sid,)).fetchone()[0]

    def test_everything_is_hidden_while_sharing_is_off(self):
        db.set_config(self.conn, "tidal_playlists_write", "0")
        self.conn.commit()
        _login(self.client, self.owner)
        for method, url in (("GET", "/api/tidal-shares"), ("GET", "/api/tidal-shares/recipients"),
                            ("POST", "/api/tidal-shares"), ("POST", "/api/tidal-shares/1/accept"),
                            ("POST", "/api/tidal-shares/1/decline"), ("POST", "/api/tidal-shares/1/end"),
                            ("PATCH", "/api/tidal-shares/1")):
            with self.subTest(url=url, method=method):
                self.assertEqual(self.client.open(url, method=method, json={}).status_code, 404)

    def test_an_invitation_starts_pending_with_the_title_and_track_count(self):
        resp = self._create(self.owner, two_way=True)
        self.assertEqual(resp.status_code, 201)
        share = resp.get_json()
        self.assertEqual((share["status"], share["title"], share["two_way"], share["track_count"]),
                         ("pending", "Road Trip", True, 2))
        self.assertEqual(share["owner"]["username"], "owner")
        self.assertIsNone(share["accepted_at"])
        _login(self.client, self.other)
        incoming = self.client.get("/api/tidal-shares").get_json()["incoming"]
        self.assertEqual([s["id"] for s in incoming], [share["id"]])

    def test_only_your_own_tidal_playlist_can_be_shared(self):
        roon = self._make_playlist("Roon one", owner_user_id=self.owner)
        self.assertEqual(self._create(self.owner, playlist_id=roon).status_code, 404)
        self.assertEqual(self._create(self.other, recipient_user_id=self.admin).status_code, 404)
        self.assertEqual(self._create(self.owner, recipient_user_id=self.owner).status_code, 400)
        self.assertEqual(self._create(self.owner, recipient_user_id=9999).status_code, 404)
        self.assertEqual(self._create(self.owner, playlist_id="x").status_code, 400)

    def test_one_live_share_per_playlist_and_recipient(self):
        first = self._create(self.owner).get_json()
        self.assertEqual(self._create(self.owner).status_code, 409)
        self.client.post(f"/api/tidal-shares/{first['id']}/end")
        self.assertEqual(self._create(self.owner).status_code, 201)

    def test_only_an_admin_can_push(self):
        self.assertEqual(self._create(self.owner, push=True).status_code, 403)
        self.conn.execute("UPDATE playlists SET owner_user_id = ? WHERE id = ?", (self.admin, self.playlist))
        self.conn.commit()
        share = self._create(self.admin, push=True).get_json()
        self.assertEqual(share["status"], "active")
        self.assertIsNotNone(share["accepted_at"])

    def test_only_the_recipient_answers_and_only_once(self):
        sid = self._create(self.owner).get_json()["id"]
        _login(self.client, self.owner)
        self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/accept").status_code, 403)
        _login(self.client, self.admin)
        self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/accept").status_code, 403)
        _login(self.client, self.other)
        self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/accept").status_code, 200)
        self.assertEqual(self._status(sid), "active")
        self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/accept").status_code, 409)
        self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/decline").status_code, 409)

    def test_declining_ends_it(self):
        sid = self._create(self.owner).get_json()["id"]
        for who in (self.owner, self.admin):
            _login(self.client, who)
            self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/decline").status_code, 403)
        self.assertEqual(self._status(sid), "pending")
        _login(self.client, self.other)
        self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/decline").status_code, 200)
        self.assertEqual(self._status(sid), "ended")

    def test_either_side_or_an_admin_ends_it_and_no_one_else(self):
        third = self._make_user("carol")
        for who in (self.owner, self.other, self.admin):
            with self.subTest(who=who):
                sid = self._create(self.owner).get_json()["id"]
                _login(self.client, third)
                self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/end").status_code, 403)
                _login(self.client, who)
                self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/end").status_code, 200)
                self.assertEqual(self._status(sid), "ended")
                self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/end").status_code, 409)

    def test_only_the_owner_switches_two_way(self):
        sid = self._create(self.owner).get_json()["id"]
        _login(self.client, self.other)
        self.assertEqual(self.client.patch(f"/api/tidal-shares/{sid}", json={"two_way": True}).status_code, 403)
        _login(self.client, self.owner)
        self.assertEqual(self.client.patch(f"/api/tidal-shares/{sid}", json={}).status_code, 400)
        resp = self.client.patch(f"/api/tidal-shares/{sid}", json={"two_way": True})
        self.assertTrue(resp.get_json()["two_way"])

    def _ended_with_copy(self):
        sid = self._create(self.owner).get_json()["id"]
        self.conn.execute("UPDATE tidal_shares SET status = 'ended', copy_remote_id = 'copy-1' WHERE id = ?", (sid,))
        self.conn.commit()
        return sid

    def test_only_the_owner_asks_to_delete_the_original_of_an_ended_share(self):
        sid = self._ended_with_copy()
        for who in (self.other, self.admin):
            _login(self.client, who)
            self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/delete-original").status_code, 403)
        _login(self.client, self.owner)
        resp = self.client.post(f"/api/tidal-shares/{sid}/delete-original")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.get_json()["delete_original_requested_at"])
        resp = self.client.delete(f"/api/tidal-shares/{sid}/delete-original")
        self.assertIsNone(resp.get_json()["delete_original_requested_at"])

    def test_the_tracks_that_kept_the_original_are_shown_until_asked_again(self):
        sid = self._ended_with_copy()
        self.conn.execute("UPDATE tidal_shares SET last_error = 'copy_lacks_tracks', delete_blocked_tracks = ? "
                          "WHERE id = ?", ('[{"track_id": "u", "position": 2}]', sid))
        self.conn.commit()
        _login(self.client, self.owner)
        share = self.client.get("/api/tidal-shares").get_json()["outgoing"][0]
        self.assertEqual(share["delete_blocked_tracks"], [{"track_id": "u", "position": 2}])
        resp = self.client.post(f"/api/tidal-shares/{sid}/delete-original")
        self.assertEqual(resp.get_json()["delete_blocked_tracks"], [])

    def test_no_deletion_can_be_asked_while_the_share_is_live(self):
        # A request taken now would wait, and the first pass after the share
        # ends would delete the original with no fresh decision.
        for status in ("pending", "active"):
            with self.subTest(status=status):
                sid = self._create(self.owner).get_json()["id"]
                self.conn.execute("UPDATE tidal_shares SET status = ?, copy_remote_id = 'copy-1' WHERE id = ?",
                                  (status, sid))
                self.conn.commit()
                _login(self.client, self.owner)
                resp = self.client.post(f"/api/tidal-shares/{sid}/delete-original")
                self.assertEqual(resp.status_code, 409)
                self.assertIsNone(self.conn.execute(
                    "SELECT delete_original_requested_at FROM tidal_shares WHERE id = ?", (sid,)).fetchone()[0])
                self.client.post(f"/api/tidal-shares/{sid}/end")

    def test_deleting_the_original_is_refused_while_it_is_still_needed(self):
        live = self._create(self.owner).get_json()["id"]
        _login(self.client, self.owner)
        self.assertEqual(self.client.post(f"/api/tidal-shares/{live}/delete-original").status_code, 409)
        self.client.post(f"/api/tidal-shares/{live}/end")
        # Ended, but no copy was ever made: the original is the only one.
        self.assertEqual(self.client.post(f"/api/tidal-shares/{live}/delete-original").status_code, 409)
        sid = self._ended_with_copy()
        carol = self._make_user("carol")
        _login(self.client, self.owner)
        self.client.post("/api/tidal-shares", json={"playlist_id": self.playlist, "recipient_user_id": carol})
        self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/delete-original").status_code, 409)
        self.conn.execute("UPDATE tidal_shares SET status = 'ended' WHERE recipient_user_id = ?", (carol,))
        self.conn.execute("UPDATE tidal_shares SET original_deleted_at = datetime('now') WHERE id = ?", (sid,))
        self.conn.commit()
        self.assertEqual(self.client.post(f"/api/tidal-shares/{sid}/delete-original").status_code, 409)

    def test_a_third_user_sees_neither_side(self):
        self._create(self.owner)
        _login(self.client, self._make_user("carol"))
        self.assertEqual(self.client.get("/api/tidal-shares").get_json(), {"outgoing": [], "incoming": []})

    def test_recipients_are_other_linked_users_with_their_readiness(self):
        self.conn.execute("UPDATE users SET tidal_scopes = ? WHERE id = ?", (tidal_client.SCOPES, self.other))
        unlinked = self._make_user("carol")
        self.conn.commit()
        _login(self.client, self.owner)
        got = self.client.get("/api/tidal-shares/recipients").get_json()
        self.assertEqual(got, [{"id": self.admin, "username": "admin", "can_receive": True},
                               {"id": self.other, "username": "bob", "can_receive": False}])
        self.assertNotIn(unlinked, [r["id"] for r in got])


class HistoryStatusRouteTests(_RouteTestBase):
    """GET /api/lastfm/status and /api/listenbrainz/status (#259): not
    configured answers without calling the service; configured checks the
    signed-in user's own account."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "UPDATE users SET lastfm_username = 'bob_fm', lastfm_api_key = 'bobs-key', "
            "listenbrainz_username = 'bob_lb' WHERE id = ?", (self.other,))
        self.conn.commit()
        _login(self.client, self.owner)

    def test_not_configured_says_so_without_calling_either_service(self):
        with mock.patch.object(main.lastfm, "check_connection") as fm, \
             mock.patch.object(main.listenbrainz, "check_connection") as lb:
            self.assertEqual(self.client.get("/api/lastfm/status").get_json(),
                             {"configured": False, "ok": False})
            self.assertEqual(self.client.get("/api/listenbrainz/status").get_json(),
                             {"configured": False, "ok": False})
        fm.assert_not_called()
        lb.assert_not_called()

    def test_lastfm_checks_the_users_own_account_and_key(self):
        self.conn.execute("UPDATE users SET lastfm_username = 'own_fm', lastfm_api_key = 'own-key' "
                          "WHERE id = ?", (self.owner,))
        # A default exists too: the user's own key has to win over it.
        db.set_config(self.conn, "lastfm_api_key_default", "the-default")
        self.conn.commit()
        with mock.patch.object(main.lastfm, "check_connection", return_value=True) as fm:
            body = self.client.get("/api/lastfm/status").get_json()
        self.assertEqual(body, {"configured": True, "ok": True})
        self.assertEqual(fm.call_args.args[:2], ("own_fm", "own-key"))

    def test_lastfm_without_a_key_of_its_own_uses_the_default(self):
        self.conn.execute("UPDATE users SET lastfm_username = 'own_fm' WHERE id = ?", (self.owner,))
        db.set_config(self.conn, "lastfm_api_key_default", "the-default")
        self.conn.commit()
        with mock.patch.object(main.lastfm, "check_connection", return_value=False) as fm:
            body = self.client.get("/api/lastfm/status").get_json()
        self.assertEqual(body, {"configured": True, "ok": False})
        self.assertEqual(fm.call_args.args[:2], ("own_fm", "the-default"))

    def test_listenbrainz_checks_the_users_own_account(self):
        self.conn.execute("UPDATE users SET listenbrainz_username = 'own_lb' WHERE id = ?", (self.owner,))
        self.conn.commit()
        with mock.patch.object(main.listenbrainz, "check_connection", return_value=True) as lb:
            body = self.client.get("/api/listenbrainz/status").get_json()
        self.assertEqual(body, {"configured": True, "ok": True})
        self.assertEqual(lb.call_args.args[0], "own_lb")


class SetupStatusRouteTests(_RouteTestBase):
    """GET /api/setup/status (#259): admin-only."""

    def test_a_non_admin_is_refused(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/api/setup/status").status_code, 403)

    def test_an_admin_sees_whether_setup_is_complete(self):
        _login(self.client, self.admin)
        with mock.patch.object(auth, "_music_root_is_readonly", return_value=True):
            before = self.client.get("/api/setup/status").get_json()
            db.set_config(self.conn, "setup_completed", "1")
            self.conn.commit()
            after = self.client.get("/api/setup/status").get_json()
        self.assertEqual((before["completed"], after["completed"]), (False, True))
        self.assertFalse(after["music_root_writable"])



class OidcLoginRouteTests(_RouteTestBase):
    """GET /oidc/login (#259): starts the IdP flow only in oidc mode, and an
    unreachable IdP is a 503 page rather than a 500."""

    def setUp(self):
        super().setUp()
        self.oauth = mock.MagicMock()
        self.oauth.oidc.authorize_redirect.return_value = main.redirect("https://idp.invalid/authorize")
        patcher = mock.patch.object(auth, "oauth", self.oauth)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_outside_oidc_mode_it_goes_to_the_login_page(self):
        with mock.patch.object(settings, "AUTH_MODE", "local"):
            resp = self.client.get("/oidc/login")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.location)
        self.oauth.oidc.authorize_redirect.assert_not_called()

    def test_in_oidc_mode_it_redirects_to_the_identity_provider(self):
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.get("/oidc/login")
        self.assertEqual((resp.status_code, resp.location), (302, "https://idp.invalid/authorize"))

    def test_an_unreachable_identity_provider_is_a_503_page(self):
        self.oauth.oidc.authorize_redirect.side_effect = ConnectionError("down")
        with mock.patch.object(settings, "AUTH_MODE", "oidc"):
            resp = self.client.get("/oidc/login")
        self.assertEqual(resp.status_code, 503)


class ProviderPairingRouteTests(_RouteTestBase):
    """GET /api/provider/status and POST /api/provider/pair (#259, #378):
    status for any signed-in user, pairing for an admin only, and both
    refused when signed out."""

    def setUp(self):
        super().setUp()
        self.provider = mock.MagicMock()
        self.provider.status.return_value = {"status": "paired"}
        self.provider.retry_pairing.return_value = {"status": "pairing"}
        patcher = mock.patch.object(playlists, "_active_provider", return_value=self.provider)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_signed_out_neither_route_reaches_the_provider(self):
        self.assertEqual(self.client.get("/api/provider/status").status_code, 401)
        self.assertEqual(self.client.post("/api/provider/pair").status_code, 401)
        self.provider.status.assert_not_called()
        self.provider.retry_pairing.assert_not_called()

    def test_any_signed_in_user_reads_the_status(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/api/provider/status").get_json(), {"status": "paired"})

    def test_a_non_admin_cannot_start_a_pairing(self):
        _login(self.client, self.owner)
        self.assertEqual(self.client.post("/api/provider/pair").status_code, 403)
        self.provider.retry_pairing.assert_not_called()

    def test_an_admin_starts_a_pairing(self):
        _login(self.client, self.admin)
        self.assertEqual(self.client.post("/api/provider/pair").get_json(), {"status": "pairing"})
        self.provider.retry_pairing.assert_called_once()


class SetupMusicRootRouteTests(_RouteTestBase):
    """POST /api/setup/music-root (#259): admin-only; a folder that doesn't
    exist is refused; check_only checks without saving."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = tmp.name
        patcher = mock.patch.object(auth, "_music_root_is_readonly", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _saved(self):
        return db.get_config(self.conn, "music_root")

    def test_a_non_admin_is_refused_and_nothing_is_saved(self):
        _login(self.client, self.owner)
        resp = self.client.post("/api/setup/music-root", json={"path": self.folder})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(self._saved())

    def test_a_folder_that_does_not_exist_is_refused(self):
        _login(self.client, self.admin)
        for path in ("", "/nonexistent/music", f"{self.folder}/missing"):
            with self.subTest(path=path):
                resp = self.client.post("/api/setup/music-root", json={"path": path})
                self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._saved())

    def test_check_only_checks_without_saving_and_a_plain_post_saves(self):
        _login(self.client, self.admin)
        checked = self.client.post("/api/setup/music-root",
                                   json={"path": self.folder, "check_only": True})
        self.assertEqual((checked.status_code, self._saved()), (200, None))
        saved = self.client.post("/api/setup/music-root", json={"path": self.folder})
        self.assertEqual((saved.status_code, self._saved()), (200, self.folder))


class AppRecentlyRouteTests(_RouteTestBase):
    """GET /api/app/library/recently-{added,released} (#259): App API routes
    act as the owner of the device whose token is sent, and pass ?months on."""

    def setUp(self):
        super().setUp()
        self.device, self.token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        self.conn.commit()

    def test_both_lists_act_as_the_devices_owner_with_the_months_asked(self):
        for path, helper in (("/api/app/library/recently-added", "_recently_added_for"),
                             ("/api/app/library/recently-released", "_recently_released_for")):
            with self.subTest(path=path), \
                 mock.patch.object(app_api, helper, return_value=[{"album": "X"}]) as called:
                default = self.client.get(path, headers={"Authorization": f"Bearer {self.token}"})
                asked = self.client.get(f"{path}?months=12",
                                        headers={"Authorization": f"Bearer {self.token}"})
                self.assertEqual((default.get_json(), asked.status_code), ([{"album": "X"}], 200))
                self.assertEqual([c.args[1:] for c in called.call_args_list],
                                 [(self.owner, 3), (self.owner, 12)])

    def test_without_a_device_token_both_are_refused(self):
        for path in ("/api/app/library/recently-added", "/api/app/library/recently-released"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)


if __name__ == "__main__":
    unittest.main()


class SelectionAlbumCountTests(_RouteTestBase):
    """The album counts on a list of selections: one query however long the
    list, and the answers a query per selection gave."""

    # (album, artist, album_artist): a whole album, a guest on it, one with no
    # album artist tag, a name two album artists share, and a guest on both.
    TRACKS = [
        ("Solo", "One", "One"), ("Solo", "One", "One"), ("Solo", "Guest", "One"),
        ("Untagged", "Two", None), ("Untagged", "Two", None),
        ("Shared", "Beta", "Beta"), ("Shared", "Guest", "Beta"), ("Shared", "Guest", "Beta"),
        ("Shared", "Alpha", "Alpha"), ("Shared", "Guest", "Alpha"),
        ("Pipes", "A||B", "A||B"), ("With || bars", "One", "One"),
    ]
    TARGETS = ["One||Solo", "Guest||Solo", "Two||Untagged", "Beta||Shared", "Alpha||Shared",
               "Guest||Shared", "Nobody||Solo", "One||Gone", "A||B||Pipes", "One||With || bars", "Gone"]

    def setUp(self):
        super().setUp()
        self.device, _ = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        for i, (album, artist, album_artist) in enumerate(self.TRACKS):
            self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, album_artist, title, size, mtime) "
                "VALUES (?, ?, ?, ?, 'T', 1, 0)", (f"{i}.flac", artist, album, album_artist))
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, deleted_at) "
            "VALUES ('gone.flac', 'One', 'Solo', 'T', 1, 0, datetime('now'))")
        self.conn.commit()

    def _per_selection(self, sel):
        """What each selection used to run for itself, as the reference."""
        out = {"appears_on": None, "album_tracks": None}
        if sel["type"] != "album":
            return out
        artist, _, album = sel["target"].partition("||")
        expr = "COALESCE(album_artist, artist)"
        whole = self.conn.execute(
            f"SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL AND album = ? AND {expr} = ?",
            (album, artist)).fetchone()[0]
        if whole:
            return {**out, "album_tracks": whole}
        row = self.conn.execute(
            f"SELECT {expr} AS album_artist, COUNT(*) AS n FROM tracks WHERE deleted_at IS NULL "
            "AND album = ? AND artist = ? GROUP BY album_artist LIMIT 1", (album, artist)).fetchone()
        if row is None:
            return out
        of = self.conn.execute(
            f"SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL AND album = ? AND {expr} = ?",
            (album, row["album_artist"])).fetchone()[0]
        return {**out, "appears_on": {"album_artist": row["album_artist"], "tracks": row["n"], "of": of}}

    def _select(self, targets):
        for target in targets:
            sync_state.create_selection(self.conn, "album" if "||" in target else "artist", target,
                                        self.owner, [self.device])

    def test_the_answers_are_the_ones_a_query_per_selection_gave(self):
        self._select(self.TARGETS)
        got = main._selections_for(self.conn, self.owner)
        self.assertEqual({s["target"] for s in got}, set(self.TARGETS))
        for sel in got:
            with self.subTest(target=sel["target"]):
                self.assertEqual({k: sel[k] for k in ("appears_on", "album_tracks")}, self._per_selection(sel))
        # The reference can't be trusted to have meant anything by itself.
        by_target = {s["target"]: s for s in got}
        self.assertEqual(by_target["One||Solo"]["album_tracks"], 3)
        self.assertEqual(by_target["Guest||Shared"]["appears_on"], {"album_artist": "Alpha", "tracks": 1, "of": 2})
        self.assertIsNone(by_target["Nobody||Solo"]["appears_on"])

    def _queries_on_tracks(self, targets):
        self._select(targets)
        seen: list[str] = []
        self.conn.set_trace_callback(seen.append)
        try:
            main._selections_for(self.conn, self.owner)
        finally:
            self.conn.set_trace_callback(None)
        return sum(" FROM tracks " in q for q in seen)

    def test_the_query_count_does_not_grow_with_the_list(self):
        self.assertEqual(self._queries_on_tracks(self.TARGETS[:2]), 1)
        self.assertEqual(self._queries_on_tracks(self.TARGETS), 1)

    def test_no_album_selection_reads_no_tracks(self):
        self.assertEqual(self._queries_on_tracks(["Gone"]), 0)


class AppApiAuthTests(_RouteTestBase):
    """/api/app/* -- the phone's own screens, authenticated by the device
    token it already holds and acting as the device's owner. Everything
    here is the boundary: who the token acts as, what it cannot reach, and
    that the two gate exemptions it needed widened nothing else."""

    def setUp(self):
        super().setUp()
        main._rl_failures.clear()
        self.addCleanup(main._rl_failures.clear)
        self.device_id, self.token = sync_state.create_device(
            self.conn, self.owner, "Phone", "phone")
        self.conn.commit()

    def _whoami(self, token=None, **kw):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return self.client.get("/api/app/whoami", headers=headers, **kw)

    def test_no_header_is_401_with_a_json_error(self):
        resp = self._whoami()
        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.get_json())

    def test_unknown_token_is_401(self):
        self.assertEqual(self._whoami("not-a-real-token").status_code, 401)

    def test_a_valid_token_acts_as_the_device_owner(self):
        resp = self._whoami(self.token)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["user_id"], self.owner)
        self.assertEqual(body["username"], "owner")
        self.assertIs(body["is_admin"], False)
        self.assertEqual(body["device_id"], self.device_id)
        self.assertEqual(body["device_name"], "Phone")
        self.assertEqual(body["app_api"], main.APP_API_VERSION)

    def test_regenerating_the_device_token_revokes_the_old_one_here_too(self):
        sync_state.regenerate_token(self.conn, self.device_id)
        self.conn.commit()
        self.assertEqual(self._whoami(self.token).status_code, 401)

    def test_a_device_pinned_to_another_user_still_acts_as_its_owner(self):
        # Pinning is visibility for the pinner; it never changes who the
        # device belongs to, so it must never change who its token acts as.
        self.conn.execute(
            "INSERT INTO device_pins (user_id, device_id) VALUES (?, ?)", (self.other, self.device_id))
        self.conn.commit()
        self.assertEqual(self._whoami(self.token).get_json()["user_id"], self.owner)

    def test_admin_status_is_read_on_every_request_not_cached(self):
        self.conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (self.owner,))
        self.conn.commit()
        self.assertIs(self._whoami(self.token).get_json()["is_admin"], True)
        self.conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (self.owner,))
        self.conn.commit()
        self.assertIs(self._whoami(self.token).get_json()["is_admin"], False)

    def test_require_app_admin_is_403_for_a_non_admin_and_silent_for_an_admin(self):
        with main.app.test_request_context("/api/app/whoami"):
            app_api._require_app_admin(True)
            with self.assertRaises(Exception) as cm:
                app_api._require_app_admin(False)
            self.assertEqual(getattr(cm.exception, "code", None), 403)

    def test_session_routes_stay_gated_and_a_device_token_does_not_open_them(self):
        # The exemption is by prefix. A session route with no cookie is still
        # 401 -- and stays 401 when the device token is offered to it, because
        # the device credential is never consulted outside its two prefixes.
        self.assertEqual(self.client.get("/api/library/artists").status_code, 401)
        resp = self.client.get(
            "/api/library/artists", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 401)

    def test_app_prefix_needs_no_session_at_all(self):
        # No _login() anywhere in this class: every 200 above already proves
        # the login gate exempted the prefix. Pinned explicitly for the reader.
        with self.client.session_transaction() as sess:
            self.assertNotIn("local_user_id", sess)
        self.assertEqual(self._whoami(self.token).status_code, 200)

    def test_cross_origin_request_to_the_app_prefix_is_not_csrf_blocked(self):
        # Bearer-authenticated, no ambient cookie to abuse: the Origin check
        # must not fire (405 here, since whoami takes no POST -- anything but
        # the 403 the check would produce). The session route is the control.
        resp = self.client.post("/api/app/whoami", json={},
                                headers={"Origin": "https://evil.example",
                                         "Authorization": f"Bearer {self.token}"})
        self.assertNotEqual(resp.status_code, 403)
        _login(self.client, self.owner)
        control = self.client.post("/api/devices", json={"name": "x"},
                                   headers={"Origin": "https://evil.example"})
        self.assertEqual(control.status_code, 403)

    def test_wrong_tokens_share_the_device_backoff_bucket(self):
        # One bucket for both prefixes: a wrong token against the App API is
        # the same wrong token against the sync API, and blocking one while
        # leaving the other open would just move the guessing over.
        for i in range(30):
            resp = self._whoami("wrong")
            self.assertEqual(resp.status_code, 401, f"attempt {i} was not a plain 401")
        self.assertEqual(self._whoami("wrong").status_code, 429)
        sync_resp = self.client.get(
            "/api/device/info", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(sync_resp.status_code, 429)

    def test_device_info_advertises_the_app_api_version(self):
        resp = self.client.get(
            "/api/device/info", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["app_api"], main.APP_API_VERSION)

    def test_the_level_is_four_since_removing_a_selection(self):
        # Pinned rather than read from the constant: the Android app shows its
        # Playlists tab only on level 2 or above, and will offer playlist
        # authoring only on level 3, and removing a selection only on level 4,
        # so a level that slid back would hide what a server has every route for.
        resp = self.client.get(
            "/api/device/info", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.get_json()["app_api"], 6)

    def test_every_route_under_the_prefix_refuses_an_unauthenticated_request(self):
        # The login gate exempts /api/app/ wholesale, so a route added under
        # it that forgets _authenticated_app_user() is not over-permissioned
        # but UNAUTHENTICATED, and nothing else would notice. This enumerates
        # the prefix from the URL map rather than naming routes, so it covers
        # routes that do not exist yet. Path parameters get a placeholder; a
        # rule this cannot build fails loudly rather than being skipped.
        rules = [r for r in main.app.url_map.iter_rules() if r.rule.startswith("/api/app/")]
        self.assertTrue(rules, "the control: the prefix has routes to check")
        for rule in rules:
            built = rule.build({a: 1 for a in rule.arguments}, append_unknown=False)
            self.assertIsNotNone(built, f"{rule.rule}: give its parameters a placeholder here")
            assert built is not None  # the line above already failed if not; this narrows the type
            url = built[1]
            for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
                resp = self.client.open(url, method=method, json={} if method != "GET" else None)
                self.assertEqual(resp.status_code, 401, f"{method} {rule.rule} answered without a token")

    def test_a_device_whose_owner_row_is_gone_is_refused_not_a_500(self):
        # Unreachable through the UI (deleting a user refuses while devices
        # remain), so the orphan is forced with foreign keys off. The point is
        # the 401: an orphaned device must not act as anyone, and must not
        # crash the request either.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DELETE FROM users WHERE id = ?", (self.owner,))
        self.conn.commit()
        resp = self._whoami(self.token)
        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.get_json())
class DashboardWidgetsMonthsTests(unittest.TestCase):
    """settings.<widget>.months is held to 1-24 on the tolerant path too:
    a stored or PUT value outside the range is dropped, so the client's
    default applies, rather than persisted for another client to read."""

    def test_in_range_months_kept(self):
        result = web_common._normalize_dashboard_widgets(
            {"settings": {"recently_added": {"months": 6}, "recently_released": {"months": 24}}})
        self.assertEqual(result["settings"]["recently_added"]["months"], 6)
        self.assertEqual(result["settings"]["recently_released"]["months"], 24)

    def test_out_of_range_months_dropped(self):
        for bad in (0, 25, -3, 99):
            with self.subTest(months=bad):
                result = web_common._normalize_dashboard_widgets(
                    {"settings": {"recently_added": {"months": bad}}})
                self.assertNotIn("months", result["settings"]["recently_added"])

    def test_non_integer_months_dropped(self):
        for bad in ("6", 6.5, True, None, [6]):
            with self.subTest(months=bad):
                result = web_common._normalize_dashboard_widgets(
                    {"settings": {"recently_added": {"months": bad}}})
                self.assertNotIn("months", result["settings"]["recently_added"])

    def test_months_on_a_widget_without_that_setting_dropped(self):
        result = web_common._normalize_dashboard_widgets({"settings": {"devices": {"months": 3}}})
        self.assertNotIn("devices", result["settings"])

    def test_unknown_settings_keys_dropped(self):
        result = web_common._normalize_dashboard_widgets({"settings": {"theme": "dark", "cover_limit": 30}})
        self.assertEqual(result["settings"], {"playlists": {"limit": 3}, "suggestions": {"cover_limit": 30}, "recently_added": {"cover_limit": 30}, "recently_released": {"cover_limit": 30}, "cover_limit": 30})


class DashboardWidgetsPatchRouteTests(_RouteTestBase):
    """PATCH /api/profile/dashboard-widgets -- the narrow write. Two clients
    can now edit widget preferences without one overwriting the other's
    unrelated profile fields, and a malformed request is refused with the
    field named rather than silently corrected."""

    def _patch(self, body, user=None):
        _login(self.client, user or self.owner)
        return self.client.patch("/api/profile/dashboard-widgets", json=body)

    def _stored(self, user=None):
        _login(self.client, user or self.owner)
        return self.client.get("/api/profile").get_json()["dashboard_widgets"]

    def test_requires_a_session(self):
        resp = self.client.patch("/api/profile/dashboard-widgets", json={"order": []})
        self.assertEqual(resp.status_code, 401)

    def test_returns_the_stored_normalized_preferences(self):
        resp = self._patch({"order": ["devices", "library"]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), self._stored())
        self.assertEqual(resp.get_json()["order"], ["devices", "library"])

    def test_a_settings_patch_leaves_order_and_disabled_alone(self):
        self._patch({"order": ["devices", "library"], "disabled": ["most_played"]})
        resp = self._patch({"settings": {"recently_added": {"months": 6}}})
        body = resp.get_json()
        self.assertEqual(body["order"], ["devices", "library"])
        # a non-admin's list always carries the admin-only widget, sorted
        self.assertEqual(body["disabled"], ["administration", "most_played"])
        self.assertEqual(body["settings"]["recently_added"], {"months": 6, "cover_limit": 15})

    def test_settings_merge_key_by_key(self):
        self._patch({"settings": {"cover_limit": 30}})
        body = self._patch({"settings": {"recently_added": {"months": 12}}}).get_json()
        self.assertEqual(body["settings"]["cover_limit"], 30)
        self.assertEqual(body["settings"]["recently_added"], {"months": 12, "cover_limit": 30})  # its count kept

    def test_the_narrow_write_does_not_touch_other_profile_fields(self):
        _login(self.client, self.owner)
        self.client.put("/api/profile", json={"lastfm_username": "someone", "cover_view_mode": "grid"})
        self._patch({"disabled": ["devices"]})
        profile = self.client.get("/api/profile").get_json()
        self.assertEqual(profile["lastfm_username"], "someone")
        self.assertEqual(profile["cover_view_mode"], "grid")

    def test_months_out_of_range_is_400(self):
        for bad in (0, 25, "6", 6.5, True):
            with self.subTest(months=bad):
                resp = self._patch({"settings": {"recently_released": {"months": bad}}})
                self.assertEqual(resp.status_code, 400)
                self.assertIn("error", resp.get_json())

    def test_unknown_settings_key_is_400(self):
        self.assertEqual(self._patch({"settings": {"theme": "dark"}}).status_code, 400)

    def test_months_on_a_widget_without_that_setting_is_400(self):
        self.assertEqual(self._patch({"settings": {"devices": {"months": 3}}}).status_code, 400)

    def test_cover_limit_outside_the_curated_set_is_400(self):
        self.assertEqual(self._patch({"settings": {"cover_limit": 20}}).status_code, 400)
        self.assertEqual(self._patch({"settings": {"cover_limit": 45}}).status_code, 200)

    def test_ids_must_be_lists_of_strings(self):
        for bad in ("devices", [1, 2], [None], {"a": 1}):
            with self.subTest(value=bad):
                self.assertEqual(self._patch({"disabled": bad}).status_code, 400)
                self.assertEqual(self._patch({"order": bad}).status_code, 400)

    def test_body_must_be_an_object(self):
        _login(self.client, self.owner)
        resp = self.client.patch("/api/profile/dashboard-widgets", json=["order"])
        self.assertEqual(resp.status_code, 400)

    def test_unknown_widget_ids_in_order_are_kept(self):
        body = self._patch({"order": ["devices", "from_a_newer_client"]}).get_json()
        self.assertEqual(body["order"], ["devices", "from_a_newer_client"])

    def test_a_non_admin_cannot_re_enable_the_admin_widget(self):
        body = self._patch({"disabled": []}).get_json()
        self.assertIn("administration", body["disabled"])

    def test_an_admin_can_enable_the_admin_widget(self):
        body = self._patch({"disabled": []}, user=self.admin).get_json()
        self.assertEqual(body["disabled"], [])

    def test_an_empty_patch_changes_nothing(self):
        before = self._patch({"order": ["library"], "settings": {"cover_limit": 60}}).get_json()
        self.assertEqual(self._patch({}).get_json(), before)


class AppApiLibraryTests(_RouteTestBase):
    """/api/app/library/* -- the same library reads as the session routes,
    behind the device token acting as the owner. Parity with the session
    route is asserted directly wherever the response is deterministic, and
    the one per-user input (the Last.fm key) is shown to be the owner's."""

    def setUp(self):
        super().setUp()
        self.device_id, self.token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        self._track("a/x/1.flac", artist="Alpha", album="X", year=2020)
        self._track("a/x/2.flac", artist="Alpha", album="X", year=2020)
        self._track("a/y/1.flac", artist="Alpha", album="Y", year=2018, reissue_year=2021)
        self._track("b/z/1.flac", artist="Beta", album="Z")
        self._track("b/gone/1.flac", artist="Beta", album="Gone", deleted_at="2026-01-01")
        self.conn.commit()

    def _track(self, relative_path, **cols):
        fields = {"relative_path": relative_path, "artist": "A", "album": "B", "title": "T",
                  "size": 1, "mtime": 0.0}
        fields.update(cols)
        self.conn.execute(
            f"INSERT INTO tracks ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            tuple(fields.values()))

    def _app(self, path, token=None):
        return self.client.get(path, headers={"Authorization": f"Bearer {token or self.token}"})

    def _session(self, path, user=None):
        _login(self.client, user or self.owner)
        return self.client.get(path)

    def test_artists_are_grouped_by_track_artist(self):
        # The App API keeps the track-artist grouping until the Android app
        # shows "Also appears on" (#240); the web Library groups by album
        # artist (LibraryAlbumArtistTests). Without compilations the two
        # agree on everything the App API returns.
        resp = self._app("/api/app/library/artists")
        self.assertEqual(resp.status_code, 200)
        web = {a["artist"]: a for a in self._session("/api/library/artists").get_json()}
        for a in resp.get_json():
            self.assertEqual({k: web[a["artist"]][k] for k in a}, a)
        by_name = {a["artist"]: a for a in resp.get_json()}
        self.assertEqual(by_name["Alpha"],
                         {"artist": "Alpha", "track_count": 3, "album_count": 2, "albums": ["X", "Y"]})
        self.assertEqual(by_name["Beta"]["album_count"], 1)  # the deleted album is not counted
        self.assertEqual(by_name["Beta"]["albums"], ["Z"])  # nor listed

    def test_each_artist_lists_its_album_titles_once_and_none_empty(self):
        # One row per track in the database; one title per album in the answer.
        self._track("c/1.flac", artist="Gamma", album="")
        self._track("c/3.flac", artist="Gamma", album="W")
        self._track("c/4.flac", artist="Gamma", album="W")
        self.conn.commit()
        by_name = {a["artist"]: a for a in self._app("/api/app/library/artists").get_json()}
        self.assertEqual(by_name["Gamma"]["albums"], ["W"])

    def test_albums_match_the_session_route(self):
        resp = self._app("/api/app/library/albums?artist=Alpha")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), self._session("/api/library/albums?artist=Alpha").get_json())
        self.assertEqual([a["album"] for a in resp.get_json()], ["X", "Y"])
        self.assertEqual(resp.get_json()[1]["reissue_year"], 2021)

    def test_similar_artists_use_the_owners_lastfm_key_and_filter_to_the_library(self):
        self.conn.execute("UPDATE users SET lastfm_api_key = 'owner-key' WHERE id = ?", (self.owner,))
        self.conn.commit()
        with mock.patch.object(main.lastfm, "similar_artists",
                               return_value=["beta", "Nobody", "Alpha", "Beta"]) as similar:
            resp = self._app("/api/app/library/similar-artists?artist=Alpha")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), ["Beta"])  # itself skipped, unknown dropped, case from the library
        self.assertEqual(similar.call_args.args[:2], ("Alpha", "owner-key"))

    def test_similar_artists_with_no_artist_still_authenticates_first(self):
        resp = self.client.get("/api/app/library/similar-artists")
        self.assertEqual(resp.status_code, 401)
        with mock.patch.object(main.lastfm, "similar_artists") as similar:
            self.assertEqual(self._app("/api/app/library/similar-artists").get_json(), [])
        similar.assert_not_called()

    def test_the_web_cover_route_serves_the_small_variant_on_size_small(self):
        with mock.patch.object(web_common, "_contained", return_value=Path("/music/a/x/1.flac")), \
                mock.patch.object(web_common.covers, "get_cover", return_value=(b"FULL", "image/png")), \
                mock.patch.object(web_common.covers, "get_small_cover", return_value=(b"SMALL", "image/jpeg")):
            full = self._session("/api/library/cover?artist=Alpha&album=X")
            small = self._session("/api/library/cover?artist=Alpha&album=X&size=small")
            other = self._session("/api/library/cover?artist=Alpha&album=X&size=huge")
        self.assertEqual((full.data, small.data, other.data), (b"FULL", b"SMALL", b"FULL"))
        self.assertEqual(small.mimetype, "image/jpeg")

    def test_cover_404s_for_an_unknown_album_with_a_json_error(self):
        resp = self._app("/api/app/library/cover?artist=Alpha&album=Nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.get_json())

    def test_cover_serves_the_cached_bytes_with_the_cache_header(self):
        with mock.patch.object(web_common, "_contained", return_value=Path("/music/a/x/1.flac")), \
                mock.patch.object(main.covers, "get_cover", return_value=(b"JPEGBYTES", "image/jpeg")):
            resp = self._app("/api/app/library/cover?artist=Alpha&album=X")
            via_session = self._session("/api/library/cover?artist=Alpha&album=X")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"JPEGBYTES")
        self.assertEqual(resp.mimetype, "image/jpeg")
        self.assertEqual(resp.headers.get("Cache-Control"), "public, max-age=86400")
        self.assertEqual(resp.data, via_session.data)

    def test_artist_image_serves_bytes_and_a_small_variant(self):
        with mock.patch.object(main.artist_images, "get_artist_image", return_value=(b"BIG", "image/png")), \
                mock.patch.object(main.artist_images, "downscale", return_value=(b"SMALL", "image/jpeg")) as down:
            full = self._app("/api/app/library/artist-image?artist=Alpha")
            small = self._app("/api/app/library/artist-image?artist=Alpha&size=small")
        self.assertEqual((full.status_code, full.data, full.mimetype), (200, b"BIG", "image/png"))
        self.assertEqual((small.status_code, small.data, small.mimetype), (200, b"SMALL", "image/jpeg"))
        self.assertEqual(full.headers.get("Cache-Control"), "public, max-age=86400")
        down.assert_called_once()

    def test_artist_image_404s_when_absent_or_unnamed(self):
        with mock.patch.object(main.artist_images, "get_artist_image", return_value=None):
            self.assertEqual(self._app("/api/app/library/artist-image?artist=Alpha").status_code, 404)
        self.assertEqual(self._app("/api/app/library/artist-image").status_code, 404)

    def test_album_tracks_match_the_session_route(self):
        self._track("t/2.flac", artist="Guest", album_artist="Tracked", album="Two Discs",
                    title="Last", disc_no=2, track_no=1, duration=61)
        self._track("t/1.flac", artist="Tracked", album_artist="Tracked", album="Two Discs",
                    title="First", disc_no=1, track_no=1)
        self._track("t/gone.flac", artist="Tracked", album_artist="Tracked", album="Two Discs",
                    title="Deleted", disc_no=1, track_no=2)
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE title = 'Deleted'")
        self.conn.commit()
        path = "/api/library/album-tracks?artist=Tracked&album=Two%20Discs"
        resp = self._app(path.replace("/api/", "/api/app/", 1))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), self._session(path).get_json())
        self.assertEqual([(t["title"], t["artist"], t["disc_no"], t["duration"]) for t in resp.get_json()],
                         [("First", "Tracked", 1, None), ("Last", "Guest", 2, 61)])

    def test_album_tracks_of_an_album_not_held_are_empty(self):
        resp = self._app("/api/app/library/album-tracks?artist=Alpha&album=Nothing")
        self.assertEqual((resp.status_code, resp.get_json()), (200, []))

    def test_album_tracks_need_the_device_token(self):
        resp = self.client.get("/api/app/library/album-tracks?artist=Alpha&album=X")
        self.assertEqual(resp.status_code, 401)

    def test_the_session_routes_still_require_a_session(self):
        for path in ("/api/library/artists", "/api/library/albums?artist=Alpha",
                     "/api/library/similar-artists?artist=Alpha",
                     "/api/library/cover?artist=Alpha&album=X", "/api/library/artist-image?artist=Alpha"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
class AppApiBasketTests(_RouteTestBase):
    """/api/app/basket*, /api/app/selections, /api/app/devices* -- the
    staging loop behind the device token. The basket is server-side and per
    user, so the phone and the browser share one; these tests stage through
    the app prefix and read back through both."""

    def setUp(self):
        super().setUp()
        self.phone, self.token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        self.car, _ = sync_state.create_device(self.conn, self.owner, "Car", "dap")
        self.bobs, _ = sync_state.create_device(self.conn, self.other, "Bob's", "phone")
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) VALUES "
            "('a/x/1.flac', 'Alpha', 'X', 'T', 1, 0.0), ('a/y/1.flac', 'Alpha', 'Y', 'T', 1, 0.0)")
        self.conn.commit()

    def _app(self, method, path, **kw):
        return self.client.open(path, method=method, headers={"Authorization": f"Bearer {self.token}"}, **kw)

    def _stage(self, target="Alpha||X", devices=None, type_="album"):
        # None means "the phone"; an explicit [] is sent as-is (the refusal case).
        ids = [self.phone] if devices is None else devices
        return self._app("POST", "/api/app/basket", json={"type": type_, "target": target, "device_ids": ids})

    def test_staging_is_shared_with_the_browser(self):
        resp = self._stage(devices=[self.phone, self.car])
        self.assertEqual(resp.status_code, 200)
        via_app = self._app("GET", "/api/app/basket").get_json()
        _login(self.client, self.owner)
        via_session = self.client.get("/api/basket").get_json()
        self.assertEqual(via_app, via_session)
        self.assertEqual(len(via_app), 1)
        self.assertEqual(sorted(via_app[0]["device_ids"]), sorted([self.phone, self.car]))
        self.assertEqual(via_app[0]["title"], "Alpha — X" if "—" in via_app[0]["title"] else via_app[0]["title"])

    def test_staging_needs_a_destination_and_only_devices_the_owner_may_use(self):
        self.assertEqual(self._stage(devices=[]).status_code, 400)
        self.assertEqual(self._stage(devices=[self.bobs]).status_code, 403)
        self.assertEqual(self._app("GET", "/api/app/basket").get_json(), [])

    def test_unstaging_the_last_device_removes_the_item_never_leaving_it_linkless(self):
        item = self._stage(devices=[self.phone, self.car]).get_json()["id"]
        self.assertEqual(self._app("DELETE", f"/api/app/basket/{item}/devices/{self.car}").status_code, 200)
        self.assertEqual(self._app("GET", "/api/app/basket").get_json()[0]["device_ids"], [self.phone])
        self.assertEqual(self._app("DELETE", f"/api/app/basket/{item}/devices/{self.phone}").status_code, 200)
        self.assertEqual(self._app("GET", "/api/app/basket").get_json(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM basket_items").fetchone()["n"], 0)

    def test_fan_out_sends_only_the_named_devices_section(self):
        self._stage("Alpha||X", devices=[self.phone, self.car])
        self._stage("Alpha||Y", devices=[self.car])
        resp = self._app("POST", "/api/app/basket/fan-out", json={"device_ids": [self.phone]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok", "count": 1, "skipped": 0})
        sel = self.conn.execute(
            "SELECT s.target, sd.device_id FROM selections s JOIN selection_devices sd ON sd.selection_id = s.id"
        ).fetchall()
        self.assertEqual([(r["target"], r["device_id"]) for r in sel], [("Alpha||X", self.phone)])
        left = {i["target"]: sorted(i["device_ids"]) for i in self._app("GET", "/api/app/basket").get_json()}
        self.assertEqual(left, {"Alpha||X": [self.car], "Alpha||Y": [self.car]})

    def test_fan_out_to_nowhere_is_refused(self):
        self._stage()
        self.assertEqual(self._app("POST", "/api/app/basket/fan-out", json={"device_ids": []}).status_code, 400)
        self.assertEqual(len(self._app("GET", "/api/app/basket").get_json()), 1)

    def test_last_destination_is_stored_per_surface_and_visible_to_the_browser(self):
        resp = self._app("PATCH", "/api/app/basket/last-destination",
                         json={"surface": "app-library", "device_ids": [self.car]})
        self.assertEqual(resp.status_code, 200)
        _login(self.client, self.owner)
        profile = self.client.get("/api/profile").get_json()
        self.assertEqual(profile["basket_last_destinations"]["app-library"], [self.car])

    def test_selections_devices_and_usage_match_the_session_routes(self):
        self._stage(devices=[self.phone])
        self._app("POST", "/api/app/basket/fan-out", json={"device_ids": [self.phone]})
        app_sel = self._app("GET", "/api/app/selections").get_json()
        app_dev = self._app("GET", "/api/app/devices").get_json()
        app_use = self._app("GET", f"/api/app/devices/{self.phone}/usage").get_json()
        _login(self.client, self.owner)
        # The app's list also says what to show for each.
        self.assertEqual([{k: v for k, v in r.items() if k != "title"} for r in app_sel],
                         self.client.get("/api/selections").get_json())
        self.assertEqual(app_dev, self.client.get("/api/devices").get_json())
        self.assertEqual(app_use, self.client.get(f"/api/devices/{self.phone}/usage").get_json())
        self.assertEqual(sorted(d["name"] for d in app_dev), ["Car", "Phone"])  # not Bob's
        self.assertEqual(app_use["track_count"], 1)

    def test_usage_of_another_users_device_is_403(self):
        self.assertEqual(self._app("GET", f"/api/app/devices/{self.bobs}/usage").status_code, 403)

    def test_another_users_basket_is_invisible(self):
        _login(self.client, self.other)
        self.client.post("/api/basket", json={"type": "album", "target": "Alpha||X", "device_ids": [self.bobs]})
        self.assertEqual(self._app("GET", "/api/app/basket").get_json(), [])

    def test_another_users_basket_item_cannot_be_removed_by_id(self):
        # The id comes straight from the URL; the delete must be scoped to
        # the acting user, not just to the id. Bob's row surviving is the
        # assertion; the status is not the point.
        _login(self.client, self.other)
        bobs_item = self.client.post(
            "/api/basket", json={"type": "album", "target": "Alpha||X", "device_ids": [self.bobs]}).get_json()["id"]
        self._app("DELETE", f"/api/app/basket/{bobs_item}")
        self.assertEqual([i["id"] for i in self.client.get("/api/basket").get_json()], [bobs_item])

    def test_clear_and_remove(self):
        item = self._stage("Alpha||X").get_json()["id"]
        self._stage("Alpha||Y")
        self.assertEqual(self._app("DELETE", f"/api/app/basket/{item}").status_code, 200)
        self.assertEqual([i["target"] for i in self._app("GET", "/api/app/basket").get_json()], ["Alpha||Y"])
        self.assertEqual(self._app("DELETE", "/api/app/basket").status_code, 200)
        self.assertEqual(self._app("GET", "/api/app/basket").get_json(), [])

    def test_fan_out_never_sends_an_item_to_a_device_it_was_not_staged_for(self):
        # Both devices in one call, each item staged for only one of them:
        # X must land on the phone only and Y on the car only. The raw
        # request list, applied to every item, would put both on both.
        self._stage("Alpha||X", devices=[self.phone])
        self._stage("Alpha||Y", devices=[self.car])
        resp = self._app("POST", "/api/app/basket/fan-out", json={"device_ids": [self.phone, self.car]})
        self.assertEqual(resp.get_json(), {"status": "ok", "count": 2, "skipped": 0})
        sel = self.conn.execute(
            "SELECT s.target, sd.device_id FROM selections s JOIN selection_devices sd ON sd.selection_id = s.id "
            "ORDER BY s.target, sd.device_id").fetchall()
        self.assertEqual([(r["target"], r["device_id"]) for r in sel],
                         [("Alpha||X", self.phone), ("Alpha||Y", self.car)])
        self.assertEqual(self._app("GET", "/api/app/basket").get_json(), [])


class AppApiDashboardTests(_RouteTestBase):
    """/api/app/dashboard/*, the widget data routes under the prefix, and
    the admin-only counts. Every route acts as the device's owner and
    answers what the session route answers; the two feeds that need a
    listening history say whether one is configured."""

    def setUp(self):
        super().setUp()
        self.phone, self.token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, duration, year) VALUES "
            "('a/x/1.flac', 'Alpha', 'X', 'T', 100, 0.0, 200.0, 1994), "
            "('a/y/1.mp3', 'Alpha', 'Y', 'U', 50, 0.0, 100.0, 2003)")
        self.conn.commit()

    def _app(self, method, path, **kw):
        return self.client.open(path, method=method, headers={"Authorization": f"Bearer {self.token}"}, **kw)

    def _playlist(self, title, track_ids, **columns):
        names = ", ".join(["title", *columns])
        marks = ", ".join("?" * (len(columns) + 1))
        cur = self.conn.execute(f"INSERT INTO playlists ({names}) VALUES ({marks})", (title, *columns.values()))
        playlist = sync_state._new_id(cur)
        for position, track in enumerate(track_ids):
            self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
                              "VALUES (?, ?, 'A', 'T', ?)", (playlist, position, track))
        self.conn.commit()
        return playlist

    def _tracks(self):
        return [r["id"] for r in self.conn.execute("SELECT id FROM tracks ORDER BY id")]

    def _to_sync(self, query=""):
        return self._app("GET", f"/api/app/dashboard/playlists?device_id={self.phone}{query}")

    def test_playlists_widget_ranks_by_what_is_left_for_this_device(self):
        one, two = self._tracks()
        self._playlist("Done", [one])
        self._playlist("Both", [one, two, None])
        self.conn.execute("INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, 'downloaded')",
                          (self.phone, one))
        self._playlist("Other", [two])
        self.conn.execute("DELETE FROM device_track_state WHERE track_id = ? AND status != 'downloaded'", (two,))
        self.conn.commit()
        rows = self._to_sync().get_json()
        self.assertEqual([(r["title"], r["to_sync"]) for r in rows], [("Both", 1), ("Other", 1)])
        self.assertEqual({k: rows[0][k] for k in ("track_count", "matched_count")},
                         {"track_count": 3, "matched_count": 2})

    def test_a_track_twice_in_a_playlist_is_one_left_to_sync(self):
        one, two = self._tracks()
        self._playlist("Twice", [one, one])
        self._playlist("Pair", [one, two])
        rows = self._to_sync().get_json()
        self.assertEqual([(r["title"], r["to_sync"]) for r in rows], [("Pair", 2), ("Twice", 1)])

    def test_playlists_widget_counts_nothing_excluded_or_gone(self):
        one, two = self._tracks()
        self._playlist("P", [one, two])
        self.conn.execute("INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, 'excluded')",
                          (self.phone, one))
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE id = ?", (two,))
        self.conn.commit()
        self.assertEqual(self._to_sync().get_json(), [])

    def test_playlists_widget_respects_its_limit_and_the_order_of_a_tie(self):
        one, two = self._tracks()
        self._playlist("Zebra", [one])
        self._playlist("apple", [one])
        self._playlist("Most", [one, two])
        self.assertEqual([r["title"] for r in self._to_sync("&limit=2").get_json()], ["Most", "apple"])

    def test_playlists_widget_hides_another_users_private_playlist(self):
        one, _two = self._tracks()
        self._playlist("Theirs", [one], owner_user_id=self.other, shared=0)
        self.assertEqual(self._to_sync().get_json(), [])

    def test_playlists_widget_refuses_another_users_device_and_no_device(self):
        theirs, _ = sync_state.create_device(self.conn, self.other, "Theirs", "phone")
        self.conn.commit()
        self.assertEqual(self._app("GET", f"/api/app/dashboard/playlists?device_id={theirs}").status_code, 403)
        self.assertEqual(self._app("GET", "/api/app/dashboard/playlists").status_code, 400)

    def test_the_playlists_row_count_is_a_device_setting(self):
        path = "/api/app/dashboard/widgets"
        self.assertEqual(self._app("GET", path).get_json()["settings"]["playlists"], {"limit": 3})
        done = self._app("PATCH", path, json={"settings": {"playlists": {"limit": 5}}})
        self.assertEqual(done.get_json()["settings"]["playlists"], {"limit": 5})
        for bad in ({"limit": 0}, {"limit": 11}, {"limit": True}, {"other": 1}, 3):
            with self.subTest(bad=bad):
                resp = self._app("PATCH", path, json={"settings": {"playlists": bad}})
                self.assertEqual(resp.status_code, 400)

    def test_catalog_is_one_list_served_to_both_clients_and_labelled(self):
        via_app = self._app("GET", "/api/app/dashboard/catalog").get_json()
        _login(self.client, self.owner)
        via_session = self.client.get("/api/dashboard/catalog").get_json()
        # The app has one more, at the end; the browser never sees it.
        self.assertEqual(via_app, [*via_session, {"id": "playlists", "admin_only": False}])
        via_app = via_session
        self.assertEqual([w["id"] for w in via_app if w["admin_only"]], ["administration"])
        self.assertEqual(via_app[0], {"id": "library", "admin_only": False})
        # The labels the web renders are keyed by the same ids, in the same
        # order -- the drift this route exists to prevent.
        with main.app.test_request_context("/"):
            labels = main._build_js_i18n()["widgets"]
        self.assertEqual([w["id"] for w in via_app], list(labels.keys()))

    def test_stats_and_recent_widgets_answer_what_the_session_routes_answer(self):
        _login(self.client, self.owner)
        for path in ("/api/library/stats", "/api/library/recently-added?months=3",
                     "/api/library/recently-released?months=3"):
            with self.subTest(path=path):
                self.assertEqual(self._app("GET", path.replace("/api/", "/api/app/", 1)).get_json(), self.client.get(path).get_json())
        stats = self._app("GET", "/api/app/library/stats").get_json()
        self.assertEqual(stats["total_tracks"], 2)
        self.assertEqual(stats["by_codec"], {"flac": {"tracks": 1, "bytes": 100}, "mp3": {"tracks": 1, "bytes": 50}})
        self.assertEqual(stats["by_decade"], {"1990s": 1, "2000s": 1})

    def test_feeds_say_whether_a_listening_history_is_configured(self):
        _login(self.client, self.owner)
        body = self._app("GET", "/api/app/suggestions?period=6month").get_json()
        self.assertFalse(body["configured"])
        self.assertEqual(body["items"], self.client.get("/api/suggestions?period=6month").get_json())
        self.assertEqual(self._app("GET", "/api/app/suggestions/most-played").get_json(),
                         {"configured": False, "items": [], "sources": {
                             "lastfm": "unconfigured", "listenbrainz": "unconfigured",
                             "maloja": "unconfigured",
                             "device_plays": "unconfigured"}})
        self.conn.execute("UPDATE users SET lastfm_username = 'alice' WHERE id = ?", (self.owner,))
        self.conn.commit()
        ranked = [{"artist": "Alpha", "album": "X", "playcount": 5}, {"artist": "Beta", "album": "Z", "playcount": 9}]
        with mock.patch.object(main.lastfm, "suggestions", return_value=[]), \
                mock.patch.object(main.lastfm, "recently_played_suggestions", return_value=[]), \
                mock.patch.object(main.lastfm, "most_played", return_value=ranked):
            self.assertTrue(self._app("GET", "/api/app/suggestions").get_json()["configured"])
            body = self._app("GET", "/api/app/suggestions/most-played?limit=1").get_json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["items"], [ranked[1]])  # re-ranked by playcount, then cut to the limit

    def test_admin_counts_need_an_admin_owner_on_both_prefixes(self):
        self.assertEqual(self._app("GET", "/api/app/admin/counts").status_code, 403)
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/api/admin/counts").status_code, 403)
        self.conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (self.owner,))
        self.conn.execute("INSERT INTO device_delegations (grantee_user_id, target_user_id) VALUES (?, ?)",
                          (self.other, self.owner))
        self.conn.commit()
        expected = {"users": 3, "delegations": 1}
        self.assertEqual(self._app("GET", "/api/app/admin/counts").get_json(), expected)
        self.assertEqual(self.client.get("/api/admin/counts").get_json(), expected)

    def test_widget_preferences_read_and_patch_under_the_prefix(self):
        stored = self._app("GET", "/api/app/dashboard/widgets").get_json()
        self.assertEqual(stored, {"disabled": [], "order": [], "settings": {"playlists": {"limit": 3}, "suggestions": {"cover_limit": 15}, "recently_added": {"cover_limit": 15}, "recently_released": {"cover_limit": 15}, "cover_limit": 15}})
        resp = self._app("PATCH", "/api/app/dashboard/widgets",
                         json={"disabled": ["devices"], "settings": {"recently_added": {"months": 6}}})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # a non-admin's list always carries the admin-only widget, sorted
        self.assertEqual(body["disabled"], ["administration", "devices"])
        self.assertEqual(body["settings"]["recently_added"], {"cover_limit": 15, "months": 6})
        self.assertEqual(self._app("GET", "/api/app/dashboard/widgets").get_json(), body)
        # The device's own set: the browser's is untouched.
        _login(self.client, self.owner)
        self.assertEqual(self.client.get("/api/profile").get_json()["dashboard_widgets"],
                         {"disabled": [], "order": [], "settings": {"playlists": {"limit": 3}, "suggestions": {"cover_limit": 15}, "recently_added": {"cover_limit": 15}, "recently_released": {"cover_limit": 15}, "cover_limit": 15}})
        self.assertEqual(self._app("PATCH", "/api/app/dashboard/widgets", json={"settings": {"cover_limit": 7}}).status_code, 400)
        self.assertEqual(self._app("PATCH", "/api/app/dashboard/widgets", data="[]",
                                   content_type="application/json").status_code, 400)


class AppApiDeviceWidgetSetTests(_RouteTestBase):
    """Each paired device keeps its own Home widget set, apart from the
    browser's: a first read copies the browser's, and from then on neither
    moves the other, nor one device another."""

    BROWSER = {"disabled": ["devices"], "order": ["suggestions", "library"],
               "settings": {"cover_limit": 30, "recently_added": {"months": 6}}}

    def setUp(self):
        super().setUp()
        self.phone, self.token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")
        self.tablet, self.tablet_token = sync_state.create_device(self.conn, self.owner, "Tablet", "tablet")
        _login(self.client, self.owner)
        resp = self.client.patch("/api/profile/dashboard-widgets", json=self.BROWSER)
        self.assertEqual(resp.status_code, 200)
        self.browser = resp.get_json()  # as stored, with the admin-only union

    def _app(self, method, path, token=None, **kw):
        return self.client.open(path, method=method, headers={"Authorization": f"Bearer {token or self.token}"}, **kw)

    def _browser_set(self):
        return self.client.get("/api/profile").get_json()["dashboard_widgets"]

    def test_a_first_read_is_a_copy_of_the_browser_set_and_stays_one(self):
        first = self._app("GET", "/api/app/dashboard/widgets").get_json()
        self.assertEqual(first, self.browser)  # nobody's Home rearranges itself on upgrade
        # Copied, not followed: a later browser change leaves the phone as it was.
        self.client.patch("/api/profile/dashboard-widgets", json={"order": ["library"]})
        self.assertEqual(self._app("GET", "/api/app/dashboard/widgets").get_json(), first)

    def test_a_patch_through_the_app_leaves_the_browser_set_untouched_and_the_other_way_round(self):
        phone = self._app("PATCH", "/api/app/dashboard/widgets", json={"order": ["library", "devices"]}).get_json()
        self.assertEqual(self._browser_set(), self.browser)
        self.client.patch("/api/profile/dashboard-widgets", json={"settings": {"cover_limit": 45}})
        self.assertEqual(self._app("GET", "/api/app/dashboard/widgets").get_json(), phone)
        self.assertEqual(self._browser_set()["settings"]["cover_limit"], 45)

    def test_each_device_has_its_own_set(self):
        self._app("PATCH", "/api/app/dashboard/widgets", json={"disabled": ["library"]})
        tablet = self._app("GET", "/api/app/dashboard/widgets", token=self.tablet_token).get_json()
        self.assertEqual(tablet, self.browser)  # the tablet's first read: the browser's, not the phone's
        self.assertIn("library", self._app("GET", "/api/app/dashboard/widgets").get_json()["disabled"])

    def test_the_admin_only_union_applies_to_a_device_set_as_to_the_browser_set(self):
        body = self._app("PATCH", "/api/app/dashboard/widgets", json={"disabled": []}).get_json()
        self.assertEqual(body["disabled"], ["administration"])

    def test_a_replacement_device_takes_the_old_ones_set_unless_it_has_its_own(self):
        self._app("PATCH", "/api/app/dashboard/widgets", json={"order": ["most_played"]})
        sync_state.transfer_device(self.conn, self.phone, self.tablet)
        self.assertEqual(self._app("GET", "/api/app/dashboard/widgets", token=self.tablet_token).get_json()["order"],
                         ["most_played"])
        # A new device that already chose its own keeps it.
        third, third_token = sync_state.create_device(self.conn, self.owner, "DAP", "dap")
        self._app("PATCH", "/api/app/dashboard/widgets", token=third_token, json={"order": ["devices"]})
        sync_state.transfer_device(self.conn, self.tablet, third)
        self.assertEqual(self._app("GET", "/api/app/dashboard/widgets", token=third_token).get_json()["order"], ["devices"])


class AppApiProfileTests(_RouteTestBase):
    """/api/app/profile -- the owner's display preferences and last chosen
    destinations, read through the device token; what the browser set is
    what the phone sees, and nothing else from the profile row."""

    def setUp(self):
        super().setUp()
        self.phone, self.token = sync_state.create_device(self.conn, self.owner, "Phone", "phone")

    def _app(self, method, path, **kw):
        return self.client.open(path, method=method, headers={"Authorization": f"Bearer {self.token}"}, **kw)

    def test_defaults_for_a_fresh_account(self):
        self.assertEqual(self._app("GET", "/api/app/profile").get_json(),
                         {"cover_view_mode": "list", "show_reissue_year": False, "basket_last_destinations": {}})

    def test_reads_what_the_browser_set_and_what_the_phone_patched(self):
        _login(self.client, self.owner)
        self.client.put("/api/profile", json={"cover_view_mode": "grid", "show_reissue_year": True,
                                              "dashboard_widgets": {"disabled": [], "order": [], "settings": {}}})
        self._app("PATCH", "/api/app/basket/last-destination", json={"surface": "app-library", "device_ids": [self.phone]})
        body = self._app("GET", "/api/app/profile").get_json()
        self.assertEqual(body["cover_view_mode"], "grid")
        self.assertIs(body["show_reissue_year"], True)
        self.assertEqual(body["basket_last_destinations"], {"app-library": [self.phone]})
        # Only these three keys: no scrobble usernames or keys reach a phone.
        self.assertEqual(set(body), {"cover_view_mode", "show_reissue_year", "basket_last_destinations"})

    def test_is_the_owners_row_not_another_users(self):
        _login(self.client, self.other)
        self.client.put("/api/profile", json={"cover_view_mode": "grid", "show_reissue_year": False,
                                              "dashboard_widgets": {"disabled": [], "order": [], "settings": {}}})
        self.assertEqual(self._app("GET", "/api/app/profile").get_json()["cover_view_mode"], "list")


class AppApiPlaylistTests(_RouteTestBase):
    """/api/app/playlists* and /api/app/playlist-subscriptions* (#108): the
    browser's playlist section behind the device token, minus mirroring.
    #28 privacy and #81 attribution are exercised through these routes
    themselves, not trusted to the shared function -- a privacy rule that
    held in the browser and not on the phone would be a security bug."""

    def setUp(self):
        super().setUp()
        _, self.owner_token = sync_state.create_device(self.conn, self.owner, "Owner's phone", "phone")
        _, self.other_token = sync_state.create_device(self.conn, self.other, "Bob's phone", "phone")
        _, self.admin_token = sync_state.create_device(self.conn, self.admin, "Admin's phone", "phone")
        self.private = self._make_playlist("Private", owner_user_id=self.owner, shared=0)
        self.shared = self._make_playlist("Shared", owner_user_id=self.owner, shared=1)
        self.unowned = self._make_playlist("Unowned", owner_user_id=None, shared=1)
        # #81: the owner's private Tidal copy, the same playlist through the
        # household Roon connection (linked golden), and a Tidal-only one.
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, last_synced_at) "
            "VALUES ('Road Trip', 'tidal', 'tid1', ?, 0, datetime('now'))", (self.owner,))
        self.tidal = sync_state._new_id(cur)
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, owner_user_id, inferred_origin_provider, "
            "golden_source_id, last_synced_at) VALUES ('Road Trip', 'roon', NULL, 'tidal', ?, datetime('now'))",
            (self.tidal,))
        self.roon = sync_state._new_id(cur)
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, last_synced_at) "
            "VALUES ('Solo', 'tidal', 'tid2', ?, 0, datetime('now'))", (self.owner,))
        self.solo = sync_state._new_id(cur)
        self.conn.commit()

    def _app(self, token, method, path, **kw):
        return self.client.open(path, method=method, headers={"Authorization": f"Bearer {token}"}, **kw)

    def _list(self, token):
        resp = self._app(token, "GET", "/api/app/playlists")
        self.assertEqual(resp.status_code, 200)
        return {p["id"]: p for p in resp.get_json()}

    # --- #28 and #81, through the App API ---------------------------------

    def test_a_private_playlist_is_seen_by_its_owner_and_the_admin_only(self):
        self.assertIn(self.private, self._list(self.owner_token))
        self.assertIn(self.private, self._list(self.admin_token))
        other = self._list(self.other_token)
        self.assertNotIn(self.private, other)
        self.assertIn(self.shared, other)
        self.assertIn(self.unowned, other)

    def test_golden_attribution_is_per_viewer(self):
        owner = self._list(self.owner_token)
        self.assertIn(self.tidal, owner)          # the owner sees their golden Tidal copy
        self.assertNotIn(self.roon, owner)        # and not the Roon duplicate
        other = self._list(self.other_token)
        self.assertIn(self.roon, other)           # a non-owner reaches it through Roon
        self.assertEqual(other[self.roon]["golden_owner_username"], "owner")
        self.assertNotIn(self.tidal, other)       # the owner's Tidal copy stays private
        self.assertNotIn(self.solo, other)        # and so does a Tidal-only one
        admin = self._list(self.admin_token)
        self.assertIn(self.tidal, admin)
        self.assertNotIn(self.roon, admin)

    def test_every_viewer_sees_exactly_what_the_browser_shows_them(self):
        for user_id, token in ((self.owner, self.owner_token), (self.other, self.other_token),
                               (self.admin, self.admin_token)):
            _login(self.client, user_id)
            browser = {p["id"]: p for p in self.client.get("/api/provider/playlists").get_json()}
            with self.client.session_transaction() as sess:
                sess.clear()
            app = self._list(token)
            self.assertEqual(set(app), set(browser), f"user {user_id}")
            for pid, row in app.items():
                shown = {k: v for k, v in browser[pid].items()
                         if not k.startswith(app_api._APP_PLAYLIST_OMITTED_PREFIXES)}
                self.assertEqual({k: v for k, v in row.items() if k != "can_share"}, shown, f"user {user_id}, playlist {pid}")

    # --- what the phone is and is not given --------------------------------

    def test_the_list_carries_no_mirror_field(self):
        for row in self._list(self.owner_token).values():
            self.assertEqual([k for k in row if "mirror" in k], [], row["title"])

    def test_can_share_follows_the_owner_or_admin_rule(self):
        owner, other, admin = self._list(self.owner_token), self._list(self.other_token), self._list(self.admin_token)
        self.assertIs(owner[self.private]["can_share"], True)
        self.assertIs(other[self.shared]["can_share"], False)   # visible, not theirs
        self.assertIs(admin[self.shared]["can_share"], True)
        self.assertIs(owner[self.unowned]["can_share"], False)  # no owner to share for

    def test_no_app_route_toggles_a_mirror(self):
        rules = [r.rule for r in main.app.url_map.iter_rules() if r.rule.startswith("/api/app/")]
        self.assertTrue(any("playlists" in r for r in rules), "the control: the playlist routes are there")
        self.assertEqual([r for r in rules if "mirror" in r], [])

    # --- the actions --------------------------------------------------------

    def test_the_shared_toggle_is_the_owners_or_an_admins(self):
        self.assertEqual(self._app(self.other_token, "PATCH", f"/api/app/playlists/{self.shared}",
                                   json={"shared": False}).status_code, 403)
        self.assertEqual(self._app(self.owner_token, "PATCH", f"/api/app/playlists/{self.unowned}",
                                   json={"shared": False}).status_code, 400)
        self.assertEqual(self._app(self.owner_token, "PATCH", f"/api/app/playlists/{self.private}",
                                   json={"shared": True}).status_code, 200)
        self.assertEqual(self.conn.execute("SELECT shared FROM playlists WHERE id = ?", (self.private,)).fetchone()[0], 1)

    def test_tracks_come_in_playlist_order_with_whether_each_resolved(self):
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) VALUES ('a/b/1.flac', 'A', 'B', 'One', 1, 0.0)")
        track = sync_state._new_id(cur)
        # inserted out of order: the route orders by position
        self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, artist, title, album, matched_track_id) "
                          "VALUES (?, 2, 'C', 'Three', NULL, NULL)", (self.private,))
        self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, artist, title, album, matched_track_id) "
                          "VALUES (?, 1, 'A', 'One', 'B', ?)", (self.private, track))
        self.conn.commit()
        resp = self._app(self.owner_token, "GET", f"/api/app/playlists/{self.private}/tracks")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertEqual([{k: v for k, v in r.items() if k != "entry_id"} for r in rows], [
            {"position": 1, "artist": "A", "title": "One", "album": "B", "matched": True, "track_id": track,
             "library_artist": "A", "library_album": "B"},
            {"position": 2, "artist": "C", "title": "Three", "album": None, "matched": False, "track_id": None,
             "library_artist": None, "library_album": None},
        ])
        self.assertEqual(len({r["entry_id"] for r in rows}), 2)
        self.assertEqual(self._app(self.other_token, "GET", f"/api/app/playlists/{self.private}/tracks").status_code, 403)
        self.assertEqual(self._app(self.owner_token, "GET", "/api/app/playlists/999999/tracks").status_code, 404)

    def _selected(self, user, device, target="A"):
        sync_state.toggle_selection_device(self.conn, user, "artist", target, device, True)
        self.conn.commit()

    def _on(self, device, target="A"):
        return self.conn.execute(
            "SELECT COUNT(*) FROM selections s JOIN selection_devices sd ON sd.selection_id = s.id "
            "WHERE s.type = 'artist' AND s.target = ? AND sd.device_id = ?", (target, device)).fetchone()[0]

    def _failed_sync(self):
        self.conn.execute(
            "INSERT INTO jobs (type, state, result) VALUES (?, 'done', ?)",
            (playlist_sync.JOB_TYPE, json.dumps({"status": "ok", "source_failures": [
                {"provider": "tidal", "user_id": self.owner, "reason": "http_403"}]})))
        self.conn.commit()

    def _failures(self, resp):
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["last_result"]["source_failures"]

    def test_app_status_shows_a_failed_link_to_its_owner_and_an_admin_only(self):
        self._failed_sync()
        path = "/api/app/playlists/sync/status"
        owner = self._failures(self._app(self.owner_token, "GET", path))
        admin = self._failures(self._app(self.admin_token, "GET", path))
        self.assertEqual([(f["reason"], f["username"]) for f in owner], [("http_403", "owner")])
        self.assertEqual([(f["reason"], f["username"]) for f in admin], [("http_403", "owner")])
        self.assertEqual(self._failures(self._app(self.other_token, "GET", path)), [])

    def test_browser_status_shows_a_failed_link_to_its_owner_and_an_admin_only(self):
        self._failed_sync()
        path = "/api/provider/playlists/sync/status"
        _login(self.client, self.other)
        self.assertEqual(self._failures(self.client.get(path)), [])
        _login(self.client, self.owner)
        self.assertEqual([f["username"] for f in self._failures(self.client.get(path))], ["owner"])
        _login(self.client, self.admin)
        self.assertEqual([f["username"] for f in self._failures(self.client.get(path))], ["owner"])

    def test_a_selection_says_what_to_show_for_it(self):
        here, _ = sync_state.create_device(self.conn, self.owner, "here")
        cur = self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                                "VALUES ('a/b/1.flac', 'A', 'B', 'One', 1, 0)")
        track = sync_state._new_id(cur)
        self.conn.commit()
        for sel_type, target in (("artist", "A"), ("album", "A||B"), ("track", str(track)),
                                 ("playlist", str(self.private)), ("playlist", "999999")):
            sync_state.toggle_selection_device(self.conn, self.owner, sel_type, target, here, True)
        self.conn.commit()
        rows = self._app(self.owner_token, "GET", "/api/app/selections").get_json()
        self.assertEqual({(r["type"], r["target"]): r["title"] for r in rows}, {
            ("artist", "A"): "A", ("album", "A||B"): "B", ("track", str(track)): "One",
            ("playlist", str(self.private)): "Private", ("playlist", "999999"): None})

    def test_removing_a_selection_leaves_it_on_the_owners_other_devices(self):
        here, _ = sync_state.create_device(self.conn, self.owner, "here")
        there, _ = sync_state.create_device(self.conn, self.owner, "there")
        self._selected(self.owner, here)
        self._selected(self.owner, there)
        resp = self._app(self.owner_token, "POST", "/api/app/selections/remove",
                         json={"device_id": here, "type": "artist", "target": "A"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((self._on(here), self._on(there)), (0, 1))

    def test_a_numeric_target_finds_a_playlist_selection(self):
        here, _ = sync_state.create_device(self.conn, self.owner, "here")
        sync_state.toggle_selection_device(self.conn, self.owner, "playlist", str(self.private), here, True)
        self.conn.commit()
        resp = self._app(self.owner_token, "POST", "/api/app/selections/remove",
                         json={"device_id": here, "type": "playlist", "target": self.private})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM selection_devices WHERE device_id = ?", (here,)).fetchone()[0], 0)

    def test_the_removal_count_is_what_the_device_would_lose(self):
        here, _ = sync_state.create_device(self.conn, self.owner, "here")
        cur = self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                                "VALUES ('a/b/1.flac', 'A', 'B', 'One', 1, 0)")
        self._selected(self.owner, here)
        self.conn.execute("UPDATE device_track_state SET status = 'downloaded' WHERE device_id = ? AND track_id = ?",
                          (here, sync_state._new_id(cur)))
        self.conn.commit()
        resp = self._app(self.owner_token, "POST", "/api/app/selections/removal-count",
                         json={"device_id": here, "type": "artist", "target": "A"})
        self.assertEqual(resp.get_json(), {"tracks": 1})

    def test_someone_elses_selection_stays_and_says_whose(self):
        here, _ = sync_state.create_device(self.conn, self.owner, "here")
        self._selected(self.other, here)
        resp = self._app(self.owner_token, "POST", "/api/app/selections/remove",
                         json={"device_id": here, "type": "artist", "target": "A"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(resp.get_json()["selected_by"]), 1)
        self.assertEqual(self._on(here), 1)

    def test_a_device_someone_else_owns_is_refused(self):
        theirs, _ = sync_state.create_device(self.conn, self.other, "theirs")
        self._selected(self.other, theirs)
        for route in ("remove", "removal-count"):
            resp = self._app(self.owner_token, "POST", f"/api/app/selections/{route}",
                             json={"device_id": theirs, "type": "artist", "target": "A"})
            self.assertEqual(resp.status_code, 403, route)
        self.assertEqual(self._on(theirs), 1)

    def test_autofit_is_not_removed_here(self):
        here, _ = sync_state.create_device(self.conn, self.owner, "here")
        resp = self._app(self.owner_token, "POST", "/api/app/selections/remove",
                         json={"device_id": here, "type": "autofit", "target": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_an_entry_names_where_its_track_sits_in_the_library(self):
        # A guest on a compilation: the library lists it under the album artist.
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album_artist, album, title, size, mtime) "
            "VALUES ('v/c/1.flac', 'Guest', 'Various Artists', 'Compilation', 'One', 1, 0)")
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, album, matched_track_id) "
            "VALUES (?, 1, 'Guest feat. Someone', 'One', 'Single', ?)",
            (self.private, sync_state._new_id(cur)))
        self.conn.commit()
        row = self._app(self.owner_token, "GET", f"/api/app/playlists/{self.private}/tracks").get_json()[0]
        self.assertEqual((row["artist"], row["library_artist"], row["library_album"]),
                         ("Guest feat. Someone", "Various Artists", "Compilation"))

    # --- level 3: authoring a playlist made in Trobar ----------------------

    def _tracks(self, *titles):
        ids = []
        for i, title in enumerate(titles):
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
                "VALUES (?, 'A', 'B', ?, ?, 1, 0.0)", (f"a/b/{title}.flac", title, i + 1))
            ids.append(sync_state._new_id(cur))
        self.conn.commit()
        return ids

    def _create(self, token, title="Mine"):
        resp = self._app(token, "POST", "/api/app/playlists", json={"title": title})
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()["id"]

    def test_a_playlist_made_on_the_phone_is_the_owners_and_private(self):
        pid = self._create(self.owner_token, "  Phone mix  ")
        row = self.conn.execute("SELECT title, owner_user_id, shared, source_provider FROM playlists WHERE id = ?",
                                (pid,)).fetchone()
        self.assertEqual(tuple(row), ("Phone mix", self.owner, 0, playlist_sync.TROBAR_SOURCE))
        self.assertIn(pid, self._list(self.owner_token))
        self.assertNotIn(pid, self._list(self.other_token))
        self.assertEqual(self._app(self.owner_token, "POST", "/api/app/playlists", json={"title": " "}).status_code, 400)

    def test_entries_are_set_appended_and_kept_by_entry_id(self):
        one, two, three = self._tracks("One", "Two", "Three")
        pid = self._create(self.owner_token)
        path = f"/api/app/playlists/{pid}/tracks"
        resp = self._app(self.owner_token, "PUT", path, json={"track_ids": [two, one]})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual((body["tracks"], body["added"]), (2, 2))
        self.assertEqual([e["track_id"] for e in body["entries"]], [two, one])
        # The response's entries are the GET's own shape.
        self.assertEqual(body["entries"], self._app(self.owner_token, "GET", path).get_json())
        appended = self._app(self.owner_token, "POST", path, json={"track_ids": [three]}).get_json()
        self.assertEqual([e["title"] for e in appended["entries"]], ["Two", "One", "Three"])
        # Reorder by entry id, dropping "Two": the PUT is the whole list.
        kept = [{"entry_id": e["entry_id"]} for e in appended["entries"][1:]][::-1]
        resp = self._app(self.owner_token, "PUT", path, json={"items": kept})
        self.assertEqual([e["title"] for e in resp.get_json()["entries"]], ["Three", "One"])
        # Idempotent: the same full list again changes nothing.
        again = [{"entry_id": e["entry_id"]} for e in resp.get_json()["entries"]]
        resp = self._app(self.owner_token, "PUT", path, json={"items": again})
        self.assertEqual([e["title"] for e in resp.get_json()["entries"]], ["Three", "One"])

    def test_an_entry_whose_track_left_the_library_has_no_track_id(self):
        one, two = self._tracks("One", "Two")
        pid = self._create(self.owner_token)
        path = f"/api/app/playlists/{pid}/tracks"
        self._app(self.owner_token, "PUT", path, json={"track_ids": [one, two]})
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE id = ?", (one,))
        self.conn.commit()
        rows = self._app(self.owner_token, "GET", path).get_json()
        self.assertEqual([(r["title"], r["track_id"]) for r in rows], [("One", None), ("Two", two)])
        # Kept by its entry id, it stays in place; re-sent as a track id, it is refused.
        keep = [{"entry_id": r["entry_id"]} for r in rows]
        self.assertEqual(self._app(self.owner_token, "PUT", path, json={"items": keep}).status_code, 200)
        self.assertEqual(self._app(self.owner_token, "PUT", path, json={"track_ids": [one, two]}).status_code, 400)

    def test_whole_albums_append_in_track_order(self):
        self._tracks("One", "Two")
        pid = self._create(self.owner_token)
        resp = self._app(self.owner_token, "POST", f"/api/app/playlists/{pid}/tracks",
                         json={"albums": [{"artist": "A", "album": "B"}]})
        self.assertEqual([e["title"] for e in resp.get_json()["entries"]], ["One", "Two"])

    def test_unknown_tracks_and_foreign_entries_are_refused(self):
        (one,) = self._tracks("One")
        pid, theirs = self._create(self.owner_token), self._create(self.other_token)
        foreign = self._app(self.other_token, "PUT", f"/api/app/playlists/{theirs}/tracks",
                            json={"track_ids": [one]}).get_json()["entries"][0]["entry_id"]
        path = f"/api/app/playlists/{pid}/tracks"
        resp = self._app(self.owner_token, "PUT", path, json={"track_ids": [999999]})
        self.assertEqual((resp.status_code, resp.get_json()["unknown"]), (400, [999999]))
        resp = self._app(self.owner_token, "PUT", path, json={"items": [{"entry_id": foreign}]})
        self.assertEqual((resp.status_code, resp.get_json()["unknown_entries"]), (400, [foreign]))
        self.assertEqual(self._app(self.owner_token, "PUT", path, json={"items": "x"}).status_code, 400)

    def test_rename_and_delete_follow_the_owner_or_admin_rule(self):
        pid = self._create(self.owner_token)
        self.assertEqual(self._app(self.other_token, "PATCH", f"/api/app/playlists/{pid}",
                                   json={"title": "Theirs"}).status_code, 403)
        self.assertEqual(self._app(self.admin_token, "PATCH", f"/api/app/playlists/{pid}",
                                   json={"title": "Renamed"}).status_code, 200)
        self.assertEqual(self.conn.execute("SELECT title FROM playlists WHERE id = ?", (pid,)).fetchone()[0], "Renamed")
        self.assertEqual(self._app(self.other_token, "DELETE", f"/api/app/playlists/{pid}").status_code, 403)
        self.assertEqual(self._app(self.other_token, "PUT", f"/api/app/playlists/{pid}/tracks",
                                   json={"track_ids": []}).status_code, 403)
        self.assertEqual(self._app(self.owner_token, "DELETE", f"/api/app/playlists/{pid}").status_code, 200)
        self.assertIsNone(self.conn.execute("SELECT id FROM playlists WHERE id = ?", (pid,)).fetchone())
        self.assertEqual(self._app(self.owner_token, "DELETE", f"/api/app/playlists/{pid}").status_code, 404)

    def test_a_provider_playlist_is_not_authored_here(self):
        # The owner's own Tidal playlist: theirs to share, not to rename, fill or delete.
        for method, path, body in (("PATCH", f"/api/app/playlists/{self.tidal}", {"title": "X"}),
                                   ("PUT", f"/api/app/playlists/{self.tidal}/tracks", {"track_ids": []}),
                                   ("POST", f"/api/app/playlists/{self.tidal}/tracks", {"track_ids": []}),
                                   ("DELETE", f"/api/app/playlists/{self.tidal}", None)):
            with self.subTest(method=method, path=path):
                self.assertEqual(self._app(self.owner_token, method, path, json=body).status_code, 400)
        self.assertIsNotNone(self.conn.execute("SELECT id FROM playlists WHERE id = ?", (self.tidal,)).fetchone())

    def test_unresolved_review_follows_the_playlists_visibility(self):
        self.conn.execute("INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album) "
                          "VALUES (?, 1, 'X', 'Gone', 'Y')", (self.private,))
        self.conn.commit()
        path = f"/api/app/playlists/{self.private}/unresolved-tracks"
        self.assertEqual(self._app(self.other_token, "GET", path).status_code, 403)
        rows = self._app(self.owner_token, "GET", path).get_json()
        self.assertEqual([r["title"] for r in rows], ["Gone"])
        self.assertEqual(self._app(self.other_token, "POST", path + "/exclude", json={"ids": [rows[0]["id"]]}).status_code, 403)
        resp = self._app(self.owner_token, "POST", path + "/exclude", json={"ids": [rows[0]["id"]]})
        self.assertEqual(resp.get_json(), {"unresolved_count": 0})

    def test_the_lidarr_toggle_follows_the_playlists_visibility(self):
        path = f"/api/app/playlists/{self.private}/lidarr-requests"
        self.assertEqual(self._app(self.other_token, "POST", path, json={"enabled": True}).status_code, 403)
        with mock.patch("lidarr_requests.lidarr_client"):
            resp = self._app(self.owner_token, "POST", path, json={"enabled": True})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.get_json()["lidarr_request_enabled"], True)

    def test_subscriptions_are_the_owners_own(self):
        with mock.patch.object(main.ytmusic_client, "get_playlist_tracks",
                               return_value=_ok("Road Trip", [("Bon Jovi", "Livin' On A Prayer")])):
            added = self._app(self.owner_token, "POST", "/api/app/playlist-subscriptions",
                              json={"url": "https://music.youtube.com/playlist?list=PLabc123"})
        self.assertEqual(added.status_code, 200)
        sub = added.get_json()["subscription"]["id"]
        self.assertEqual([s["id"] for s in self._app(self.owner_token, "GET", "/api/app/playlist-subscriptions")
                          .get_json()["subscriptions"]], [sub])
        self.assertEqual(self._app(self.other_token, "GET", "/api/app/playlist-subscriptions").get_json(),
                         {"subscriptions": []})
        self.assertEqual(self._app(self.other_token, "POST", f"/api/app/playlist-subscriptions/{sub}/refresh").status_code, 403)
        self.assertEqual(self._app(self.other_token, "DELETE", f"/api/app/playlist-subscriptions/{sub}").status_code, 403)
        self.assertEqual(self._app(self.owner_token, "POST", "/api/app/playlist-subscriptions",
                                   json={"url": "not a link"}).status_code, 400)
        self.assertEqual(self._app(self.owner_token, "DELETE", f"/api/app/playlist-subscriptions/{sub}").status_code, 200)

    def test_a_sync_can_be_started_and_polled(self):
        with mock.patch.object(playlist_sync, "start_sync", return_value={"status": "started"}):
            self.assertEqual(self._app(self.owner_token, "POST", "/api/app/playlists/sync").status_code, 202)
        with mock.patch.object(playlist_sync, "start_sync", return_value={"already_running": True}):
            self.assertEqual(self._app(self.owner_token, "POST", "/api/app/playlists/sync").status_code, 409)
        self.assertEqual(self._app(self.owner_token, "GET", "/api/app/playlists/sync/status").status_code, 200)


class AuthInvariantTests(_RouteTestBase):
    """The session gate exempts /api/device/, /api/integrations/ and /api/app/
    wholesale: each handler there must check its own Bearer token, or it is
    open to anyone. Every /api/admin/ handler must refuse a non-admin. This
    walks the live url_map, so a route added later is covered without being
    named here."""

    BEARER_PREFIXES = ("/api/device/", "/api/integrations/", "/api/app/")
    # Public by design, under those prefixes. Empty today: every route there
    # takes a token. A route added here needs a reason beside it.
    PUBLIC: set[str] = set()
    # Admin-only routes outside /api/admin/, named because the prefix walk
    # can't find them. The last test keeps this list complete.
    ADMIN_ELSEWHERE = {
        "/api/integration-tokens", "/api/integration-tokens/<int:token_id>",
        "/api/provider/pair",
        "/api/setup/complete", "/api/setup/music-root", "/api/setup/status",
    }

    def _calls(self, prefixes=(), rules=()):
        for rule in main.app.url_map.iter_rules():
            wanted = (prefixes and rule.rule.startswith(prefixes)) or rule.rule in rules
            if not wanted or rule.rule in self.PUBLIC:
                continue
            url = rule.rule
            for name in rule.arguments:
                value = "1" if type(rule._converters[name]).__name__ == "IntegerConverter" else "x"
                url = url.replace(f"<int:{name}>", value).replace(f"<path:{name}>", value).replace(f"<{name}>", value)
            for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
                yield rule.rule, method, url

    def test_every_bearer_route_refuses_a_request_without_a_token(self):
        seen = set()
        for rule, method, url in self._calls(self.BEARER_PREFIXES):
            seen.add(next(p for p in self.BEARER_PREFIXES if rule.startswith(p)))
            with self.subTest(route=rule, method=method):
                status = self.client.open(url, method=method, json={}).status_code
                self.assertIn(status, (401, 403), f"{method} {rule} answered {status} without a token")
        # The walk has to reach every prefix, or this passes having checked nothing.
        self.assertEqual(seen, set(self.BEARER_PREFIXES))

    def test_every_admin_route_refuses_a_signed_in_non_admin(self):
        _login(self.client, self.owner)
        calls = list(self._calls(("/api/admin/",), self.ADMIN_ELSEWHERE))
        self.assertGreater(len(calls), 0)
        self.assertEqual({rule for rule, _, _ in calls if not rule.startswith("/api/admin/")},
                         self.ADMIN_ELSEWHERE, "a listed admin route no longer exists")
        for rule, method, url in calls:
            with self.subTest(route=rule, method=method):
                status = self.client.open(url, method=method, json={}).status_code
                self.assertEqual(status, 403, f"{method} {rule} answered {status} to a non-admin")

    def test_every_admin_check_outside_api_admin_is_listed(self):
        import inspect
        found = {rule.rule for rule in main.app.url_map.iter_rules()
                 if not rule.rule.startswith("/api/admin/")
                 and "require_admin(" in inspect.getsource(main.app.view_functions[rule.endpoint])}
        self.assertEqual(found, self.ADMIN_ELSEWHERE,
                         "an admin-only route outside /api/admin/ must be added to ADMIN_ELSEWHERE")


class HealthzTests(_RouteTestBase):
    """GET /healthz: the container's HEALTHCHECK. No session, and nothing in
    the answer beyond whether the database does."""

    def test_answers_ok_without_a_session(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_answers_503_when_the_database_does_not(self):
        with mock.patch.object(ops.db, "connection", side_effect=sqlite3.OperationalError("unable to open")):
            resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json(), {"status": "unavailable"})

    def test_the_exemption_is_this_path_only(self):
        # Exempted by exact name, like /login: nothing under or beside it.
        # The gate itself must answer them (a redirect to the login page);
        # a 404 would mean they got past it.
        for path in ("/healthz/", "/healthz/x", "/healthzx"):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp.headers["Location"].endswith("/login"), resp.headers["Location"])
        self.assertEqual(self.client.get("/api/profile").status_code, 401)


class ThanksCardTests(_RouteTestBase):
    """The web UI's thank-you card: shown to an admin once the server has
    counted THANKS_CARD_SYNCS successful device syncs, across every
    account, until that admin dismisses it. The count comes only from
    POST /api/device/sync-completed."""

    def setUp(self):
        super().setUp()
        self.second_admin = self._make_user("admin2", is_admin=True)
        _, self.owner_token = sync_state.create_device(self.conn, self.owner, "phone")
        _, self.bob_token = sync_state.create_device(self.conn, self.other, "sdcard")
        self.conn.commit()

    def _report(self, token):
        return self.client.post("/api/device/sync-completed",
                                headers={"Authorization": f"Bearer {token}"})

    def _set_count(self, n: int) -> None:
        db.set_config(self.conn, db.DEVICE_SYNCS_KEY, str(n))
        self.conn.commit()

    def _card(self, user_id: int) -> bool:
        _login(self.client, user_id)
        return self.client.get("/api/profile").get_json()["thanks_card"]

    def test_every_account_s_devices_add_to_one_count(self):
        self.assertEqual(self._report(self.owner_token).status_code, 200)
        self._report(self.owner_token)
        self._report(self.bob_token)
        self.assertEqual(db.device_syncs_completed(self.conn), 3)

    def test_a_report_needs_a_device_token(self):
        self.assertEqual(self.client.post("/api/device/sync-completed").status_code, 401)
        self.assertEqual(self._report("not-a-token").status_code, 401)
        self.assertEqual(db.device_syncs_completed(self.conn), 0)

    def test_an_admin_sees_it_from_the_milestone_on(self):
        self._set_count(user_profile.THANKS_CARD_SYNCS - 1)
        self.assertFalse(self._card(self.admin))
        self._report(self.owner_token)
        self.assertTrue(self._card(self.admin))

    def test_a_user_who_is_not_an_admin_never_does(self):
        self._set_count(user_profile.THANKS_CARD_SYNCS * 10)
        self.assertFalse(self._card(self.owner))

    def test_the_page_carries_the_card_with_the_milestone_in_its_text(self):
        # Rendered for everyone; profile.thanks_card decides whether it shows.
        db.set_config(self.conn, "setup_completed", "1")
        self.conn.commit()
        _login(self.client, self.admin)
        html = self.client.get("/").get_data(as_text=True)
        i = html.index("data-thanks-card")
        self.assertIn('x-show="profile.thanks_card"', html[i - 200:i])
        self.assertIn(f"Trobar has synced {user_profile.THANKS_CARD_SYNCS} times on this server.", html[i:i + 2500])

    def test_dismissing_it_hides_it_for_that_admin_only(self):
        self._set_count(user_profile.THANKS_CARD_SYNCS)
        _login(self.client, self.admin)
        response = self.client.delete("/api/profile/thanks-card")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["thanks_card"])
        self._set_count(user_profile.THANKS_CARD_SYNCS * 2)
        self.assertFalse(self._card(self.admin))  # for good, not until the next milestone
        self.assertTrue(self._card(self.second_admin))


class SupportLinkRenderTests(_RouteTestBase):
    """The About page's support link goes to Ko-fi, and no Liberapay link is
    left anywhere on the page."""

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "setup_completed", "1")  # else / redirects to the wizard
        self.conn.commit()

    def test_the_support_link_opens_ko_fi(self):
        _login(self.client, self.admin)
        html = self.client.get("/").get_data(as_text=True)
        # Two Ko-fi links, About's and the thank-you card's, and each is the
        # support one.
        starts = [m.start() for m in re.finditer(re.escape('href="https://ko-fi.com/missingfoss"'), html)]
        self.assertEqual(len(starts), 2)
        for i in starts:
            self.assertIn("Support on Ko-fi", html[i:i + 300])
        self.assertNotIn("liberapay", html.lower())

    def test_the_support_note_says_what_the_money_pays_for(self):
        _login(self.client, self.admin)
        html = self.client.get("/").get_data(as_text=True)
        i = html.index('href="https://ko-fi.com/missingfoss"')
        j = html.index("data-support-note")
        self.assertGreater(j, i)  # under the button
        self.assertIn("Support pays for test devices, infrastructure and Claude.", html[j:j + 300])


class CoverEpochRenderTests(_RouteTestBase):
    """The page puts the scan's covers_epoch into every cover URL it builds."""

    def setUp(self):
        super().setUp()
        db.set_config(self.conn, "setup_completed", "1")  # else / redirects to the wizard
        self.conn.commit()

    def test_the_cover_url_carries_the_epoch(self):
        db.set_config(self.conn, "covers_epoch", "1234567890")
        self.conn.commit()
        _login(self.client, self.admin)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("'&v=1234567890'", html)

    def test_the_thumbnails_ask_for_the_small_variant_and_the_grids_do_not(self):
        _login(self.client, self.admin)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("+ (size ? '&size=' + size : '')", html)
        # The basket, an artist's album list, "appears on", the selections
        # table: the 24-40 px slots. Every other coverUrl() call is a grid tile
        # or the album dialog's larger cover.
        self.assertEqual(html.count(", 'small')\""), 4)
        self.assertEqual(html.count('coverUrl('), 14)

    def test_before_any_scan_it_is_zero(self):
        _login(self.client, self.admin)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("encodeURIComponent(album)\n        + '&v=0'", html)

