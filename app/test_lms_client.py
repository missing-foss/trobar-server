#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for lms_client.py — mocks requests, no network access needed.
Config persistence goes through a real temp-file SQLite DB (db.get_conn()
opens by DB_PATH internally, so an in-memory connection passed in wouldn't
be reachable from inside the module) rather than mocking db itself.

    python3 -m unittest test_lms_client -v
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-lms-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import lms_client  # noqa: E402


def _resp(status_code=200, json_body=None, content=True):
    r = mock.Mock()
    r.status_code = status_code
    r.content = b"x" if content else b""
    r.json.return_value = json_body if json_body is not None else {}
    if status_code >= 400:
        err = requests.HTTPError(f"{status_code}")
        err.response = r
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    return r


class _LmsClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

        self._music_dir = tempfile.TemporaryDirectory(dir=_TMP)
        self.root = Path(self._music_dir.name)
        conn = db.get_conn()
        db.set_config(conn, "music_root", str(self.root))
        conn.commit()
        conn.close()

    def tearDown(self):
        self._music_dir.cleanup()
        self._db_path.unlink(missing_ok=True)

    def _set_config(self, url="", username="", password=""):
        conn = db.get_conn()
        db.set_config(conn, "lms_url", url)
        db.set_config(conn, "lms_username", username)
        db.set_config(conn, "lms_password", password)
        conn.commit()
        conn.close()


class StatusTests(_LmsClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(lms_client.status()["state"], "disconnected")

    def test_paired_when_serverstatus_has_uuid(self):
        self._set_config(url="http://lms.local:9000")
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"uuid": "abc"}})):
            self.assertEqual(lms_client.status()["state"], "paired")

    def test_disconnected_when_request_fails(self):
        self._set_config(url="http://lms.local:9000")
        with mock.patch("requests.post", return_value=_resp(status_code=401)):
            self.assertEqual(lms_client.status()["state"], "disconnected")

    def test_status_reports_the_lms_provider_id(self):
        self.assertEqual(lms_client.status()["provider"], "lms")


class ReconnectTests(_LmsClientTestBase):
    def test_persists_config(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"uuid": "abc"}})):
            result = lms_client.reconnect("http://lms.local:9000", "", "")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "lms_url"), "http://lms.local:9000")
        conn.close()
        self.assertEqual(result["state"], "paired")

    def test_no_auth_sent_when_username_blank(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {}})) as post:
            lms_client.reconnect("http://lms.local:9000", "", "")
        self.assertIsNone(post.call_args.kwargs["auth"])

    def test_basic_auth_sent_when_username_set(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {}})) as post:
            lms_client.reconnect("http://lms.local:9000", "alice", "secret")
        self.assertEqual(post.call_args.kwargs["auth"], ("alice", "secret"))


class TestConnectionTests(_LmsClientTestBase):
    """#509 item 3: test_connection() — same check as status(), against
    EXPLICIT credentials. Never touches db.py — see subsonic_client's own
    TestConnectionTests for why that's the property that actually matters
    here. username/password stay optional here too (LMS's own "Authorize"
    setting is off by default) — main.py's dispatch only requires `url`
    for this provider before even calling this."""

    def test_paired_against_an_explicit_url_with_nothing_stored(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"uuid": "abc"}})):
            result = lms_client.test_connection("http://lms.local:9000", "", "")
        self.assertEqual(result["state"], "paired")

    def test_disconnected_when_the_request_fails(self):
        with mock.patch("requests.post", return_value=_resp(status_code=401)):
            result = lms_client.test_connection("http://lms.local:9000", "alice", "wrong")
        self.assertEqual(result["state"], "disconnected")

    def test_never_persists_anything(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"uuid": "abc"}})):
            lms_client.test_connection("http://lms.local:9000", "alice", "secret")
        conn = db.get_conn()
        self.assertIsNone(db.get_config(conn, "lms_url"))
        self.assertIsNone(db.get_config(conn, "lms_username"))
        self.assertIsNone(db.get_config(conn, "lms_password"))
        conn.close()

    def test_does_not_overwrite_an_existing_stored_connection(self):
        self._set_config(url="http://real.example.com:9000")
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"uuid": "abc"}})):
            lms_client.test_connection("http://typing-this.example.com:9000", "", "")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "lms_url"), "http://real.example.com:9000")
        conn.close()


class ListPlaylistsTests(_LmsClientTestBase):
    def test_not_paired_when_unconfigured(self):
        self.assertEqual(lms_client.list_playlists(), {"status": "error", "reason": "not_paired"})

    def test_maps_items_to_common_shape(self):
        self._set_config(url="http://lms.local:9000")
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"playlists_loop": [
            {"id": 3, "playlist": "Road Trip"}, {"id": 5, "playlist": "Chill"},
        ]}})):
            result = lms_client.list_playlists()
        self.assertEqual(result, {"status": "ok", "playlists": [
            {"id": "3", "title": "Road Trip"}, {"id": "5", "title": "Chill"},
        ]})

    def test_items_missing_playlist_or_id_are_skipped(self):
        self._set_config(url="http://lms.local:9000")
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"playlists_loop": [
            {"id": 1, "playlist": "Good"}, {"id": 2}, {"playlist": "NoId"},
        ]}})):
            result = lms_client.list_playlists()
        self.assertEqual(result["playlists"], [{"id": "1", "title": "Good"}])

    def test_empty_loop_when_no_playlists(self):
        self._set_config(url="http://lms.local:9000")
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {}})):
            result = lms_client.list_playlists()
        self.assertEqual(result, {"status": "ok", "playlists": []})


class GetPlaylistTracksTests(_LmsClientTestBase):
    def setUp(self):
        super().setUp()
        self._set_config(url="http://lms.local:9000")

    def test_fetches_by_source_playlist_id_directly(self):
        track_path = self.root / "Artist" / "Album" / "1.flac"
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"playlisttracks_loop": [
            {"playlist index": 0, "title": "Track One", "artist": "Artist A", "album": "Album A",
             "url": f"file://{track_path}"},
        ]}})) as post:
            result = lms_client.get_playlist_tracks("Road Trip", source_playlist_id="3")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tracks"], [{
            "position": 0, "title": "Track One", "artist": "Artist A",
            "path": "Artist/Album/1.flac", "album": "Album A",
        }])
        sent_command = post.call_args.kwargs["json"]["params"][1]
        self.assertIn("playlist_id:3", sent_command)

    def test_path_outside_music_root_kept_absolute(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"playlisttracks_loop": [
            {"title": "Elsewhere", "artist": "A", "album": "B", "url": "file:///srv/music/a.mp3"},
        ]}})):
            result = lms_client.get_playlist_tracks("X", source_playlist_id="3")
        self.assertEqual(result["tracks"][0]["path"], "/srv/music/a.mp3")

    def test_missing_url_yields_none_path(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"playlisttracks_loop": [
            {"title": "No Path", "artist": "A", "album": None},
        ]}})):
            result = lms_client.get_playlist_tracks("X", source_playlist_id="3")
        self.assertIsNone(result["tracks"][0]["path"])

    def test_falls_back_to_title_lookup_when_no_id_given(self):
        list_resp = _resp(json_body={"result": {"playlists_loop": [{"id": 3, "playlist": "Road Trip"}]}})
        tracks_resp = _resp(json_body={"result": {"playlisttracks_loop": []}})
        with mock.patch("requests.post", side_effect=[list_resp, tracks_resp]):
            result = lms_client.get_playlist_tracks("Road Trip")
        self.assertEqual(result["status"], "ok")

    def test_title_not_found_reports_not_found(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"result": {"playlists_loop": []}})):
            result = lms_client.get_playlist_tracks("Missing")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Missing"})


class GetArtistImageTests(_LmsClientTestBase):
    def test_always_none_stub(self):
        # #172 scopes this provider to list_playlists/get_playlist_tracks
        # only — pin the documented stub behavior directly.
        self.assertIsNone(lms_client.get_artist_image("Anyone"))


if __name__ == "__main__":
    unittest.main()
