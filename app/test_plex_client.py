#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for plex_client.py — mocks requests, no network access needed.
Config persistence goes through a real temp-file SQLite DB (db.get_conn()
opens by DB_PATH internally, so an in-memory connection passed in wouldn't
be reachable from inside the module) rather than mocking db itself.

    python3 -m unittest test_plex_client -v
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-plex-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import plex_client  # noqa: E402


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


class _PlexClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _set_config(self, url="", token=""):
        conn = db.get_conn()
        db.set_config(conn, "plex_url", url)
        db.set_config(conn, "plex_token", token)
        conn.commit()
        conn.close()


class StatusTests(_PlexClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(plex_client.status()["state"], "disconnected")

    def test_paired_when_root_returns_media_container(self):
        self._set_config(url="http://plex.local:32400", token="tok1")
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {"size": 0}})):
            self.assertEqual(plex_client.status()["state"], "paired")

    def test_disconnected_when_token_invalid(self):
        self._set_config(url="http://plex.local:32400", token="bad")
        with mock.patch("requests.get", return_value=_resp(status_code=401)):
            self.assertEqual(plex_client.status()["state"], "disconnected")

    def test_status_reports_the_plex_provider_id(self):
        self.assertEqual(plex_client.status()["provider"], "plex")


class ReconnectTests(_PlexClientTestBase):
    def test_persists_config(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {}})):
            result = plex_client.reconnect("http://plex.local:32400", "tok1")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "plex_url"), "http://plex.local:32400")
        self.assertEqual(db.get_config(conn, "plex_token"), "tok1")
        conn.close()
        self.assertEqual(result["state"], "paired")

    def test_auth_uses_x_plex_token_header_and_json_accept(self):
        # The one wire-level detail worth pinning directly: Plex answers XML
        # by default, and auth is a plain per-request token header — no
        # userId resolution step, unlike jellyfin_client.
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {}})) as get:
            plex_client.reconnect("http://plex.local:32400", "secret-token")
        self.assertEqual(get.call_args.kwargs["headers"],
                          {"Accept": "application/json", "X-Plex-Token": "secret-token"})


class TestConnectionTests(_PlexClientTestBase):
    """#509 item 3: test_connection() — same check as status(), against an
    EXPLICIT token. Never touches db.py — see subsonic_client's own
    TestConnectionTests for why that's the property that actually matters
    here."""

    def test_paired_against_an_explicit_token_with_nothing_stored(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {}})):
            result = plex_client.test_connection("http://plex.local:32400", "tok1")
        self.assertEqual(result["state"], "paired")

    def test_disconnected_when_the_token_is_rejected(self):
        with mock.patch("requests.get", return_value=_resp(status_code=401)):
            result = plex_client.test_connection("http://plex.local:32400", "bad")
        self.assertEqual(result["state"], "disconnected")

    def test_never_persists_anything(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {}})):
            plex_client.test_connection("http://plex.local:32400", "tok1")
        conn = db.get_conn()
        self.assertIsNone(db.get_config(conn, "plex_url"))
        self.assertIsNone(db.get_config(conn, "plex_token"))
        conn.close()

    def test_does_not_overwrite_an_existing_stored_connection(self):
        self._set_config(url="http://real.example.com:32400", token="realtok")
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {}})):
            plex_client.test_connection("http://typing-this.example.com:32400", "x")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "plex_url"), "http://real.example.com:32400")
        conn.close()


class ListPlaylistsTests(_PlexClientTestBase):
    def test_not_paired_when_unconfigured(self):
        self.assertEqual(plex_client.list_playlists(), {"status": "error", "reason": "not_paired"})

    def test_maps_items_to_common_shape(self):
        self._set_config(url="http://plex.local:32400", token="tok1")
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {"Metadata": [
            {"title": "Road Trip", "ratingKey": 100}, {"title": "Chill", "ratingKey": 101},
        ]}})):
            result = plex_client.list_playlists()
        self.assertEqual(result, {"status": "ok", "playlists": [
            {"id": "100", "title": "Road Trip"}, {"id": "101", "title": "Chill"},
        ]})

    def test_items_missing_title_or_rating_key_are_skipped(self):
        self._set_config(url="http://plex.local:32400", token="tok1")
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {"Metadata": [
            {"title": "Good", "ratingKey": 1}, {"title": "NoKey"}, {"ratingKey": 3},
        ]}})):
            result = plex_client.list_playlists()
        self.assertEqual(result["playlists"], [{"id": "1", "title": "Good"}])

    def test_requests_audio_playlist_type_only(self):
        self._set_config(url="http://plex.local:32400", token="tok1")
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {}})) as get:
            plex_client.list_playlists()
        self.assertEqual(get.call_args.kwargs["params"], {"playlistType": "audio"})

    def test_empty_metadata_list_when_no_playlists(self):
        self._set_config(url="http://plex.local:32400", token="tok1")
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {}})):
            result = plex_client.list_playlists()
        self.assertEqual(result, {"status": "ok", "playlists": []})


class GetPlaylistTracksTests(_PlexClientTestBase):
    def setUp(self):
        super().setUp()
        self._set_config(url="http://plex.local:32400", token="tok1")

    def test_fetches_by_source_playlist_id_directly(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {"Metadata": [
            {
                "title": "Track One", "grandparentTitle": "Artist A", "parentTitle": "Album A",
                "Media": [{"Part": [{"file": "/music/a/1.flac"}]}],
            },
        ]}})) as get:
            result = plex_client.get_playlist_tracks("Road Trip", source_playlist_id="100")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tracks"], [{
            "position": 0, "title": "Track One", "artist": "Artist A",
            "path": "/music/a/1.flac", "album": "Album A",
        }])
        self.assertIn("/playlists/100/items", get.call_args.args[0])

    def test_missing_media_or_part_yields_none_path(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {"Metadata": [
            {"title": "No Path Track", "grandparentTitle": "Artist B", "parentTitle": None, "Media": []},
        ]}})):
            result = plex_client.get_playlist_tracks("X", source_playlist_id="100")
        self.assertIsNone(result["tracks"][0]["path"])

    def test_falls_back_to_title_lookup_when_no_id_given(self):
        list_resp = _resp(json_body={"MediaContainer": {"Metadata": [{"title": "Road Trip", "ratingKey": 100}]}})
        tracks_resp = _resp(json_body={"MediaContainer": {"Metadata": []}})
        with mock.patch("requests.get", side_effect=[list_resp, tracks_resp]):
            result = plex_client.get_playlist_tracks("Road Trip")
        self.assertEqual(result["status"], "ok")

    def test_title_not_found_reports_not_found(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"MediaContainer": {"Metadata": []}})):
            result = plex_client.get_playlist_tracks("Missing")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Missing"})


class GetArtistImageTests(_PlexClientTestBase):
    def test_always_none_stub(self):
        # #158 scopes Plex artist images as an optional follow-up, not part
        # of this provider's initial acceptance criteria — pin the
        # documented stub behavior directly.
        self.assertIsNone(plex_client.get_artist_image("Anyone"))


if __name__ == "__main__":
    unittest.main()
