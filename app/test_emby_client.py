#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for emby_client.py — mocks requests, no network access needed.
Config persistence goes through a real temp-file SQLite DB (db.get_conn()
opens by DB_PATH internally, so an in-memory connection passed in wouldn't
be reachable from inside the module) rather than mocking db itself.

#189: the mirror_*() classes at the bottom cover the mirror-TARGET
additions. Two DIFFERENT low-level calls are in play here, unlike
jellyfin_client.py's single _request_as() for everything — _get() (used by
mirror_status/mirror_reconnect/mirror_build_tag_index/the read half of
mirror_set_playlist_metadata) still goes through requests.get() directly
(deliberately not rebuilt on _request_as(), see _get()'s own docstring),
while the actual mutating calls (mirror_create_or_replace_playlist, the
write half of mirror_set_playlist_metadata, mirror_delete_playlist) go
through _request_as()'s requests.request(). Tests that exercise a function
touching both mock both.

    python3 -m unittest test_emby_client -v
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-emby-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import emby_client  # noqa: E402


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


class _EmbyClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _set_config(self, url="", api_key="", user_id=""):
        conn = db.get_conn()
        db.set_config(conn, "emby_url", url)
        db.set_config(conn, "emby_api_key", api_key)
        db.set_config(conn, "emby_user_id", user_id)
        conn.commit()
        conn.close()


class StatusTests(_EmbyClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(emby_client.status()["state"], "disconnected")

    def test_disconnected_when_user_id_not_set(self):
        self._set_config(url="http://emby.local", api_key="key1")
        self.assertEqual(emby_client.status()["state"], "disconnected")

    def test_paired_when_user_id_resolves(self):
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(json_body={"Id": "u1"})):
            self.assertEqual(emby_client.status()["state"], "paired")

    def test_disconnected_when_user_lookup_fails(self):
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(status_code=404)):
            self.assertEqual(emby_client.status()["state"], "disconnected")

    def test_status_reports_the_emby_provider_id(self):
        self.assertEqual(emby_client.status()["provider"], "emby")


class ReconnectTests(_EmbyClientTestBase):
    def test_persists_config_and_resolves_user_id(self):
        # reconnect() finishes by calling status(), a second GET (a dict
        # response, not the /Users list) — two distinct mocked responses.
        users_list = _resp(json_body=[{"Name": "alice", "Id": "u1"}, {"Name": "bob", "Id": "u2"}])
        status_check = _resp(json_body={"Id": "u2"})
        with mock.patch("requests.get", side_effect=[users_list, status_check]):
            result = emby_client.reconnect("http://emby.local", "key1", "bob")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "emby_url"), "http://emby.local")
        self.assertEqual(db.get_config(conn, "emby_api_key"), "key1")
        self.assertEqual(db.get_config(conn, "emby_username"), "bob")
        self.assertEqual(db.get_config(conn, "emby_user_id"), "u2")
        conn.close()
        self.assertEqual(result["state"], "paired")

    def test_unmatched_username_leaves_user_id_blank(self):
        with mock.patch("requests.get", return_value=_resp(json_body=[{"Name": "alice", "Id": "u1"}])):
            emby_client.reconnect("http://emby.local", "key1", "nobody")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "emby_user_id"), "")
        conn.close()

    def test_auth_header_uses_x_emby_token(self):
        # The one real behavioral delta from jellyfin_client's MediaBrowser
        # scheme — worth pinning directly rather than trusting it stays
        # right through future edits.
        with mock.patch("requests.get", return_value=_resp(json_body=[])) as get:
            emby_client.reconnect("http://emby.local", "secret-key", "alice")
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Emby-Token": "secret-key"})


class TestConnectionTests(_EmbyClientTestBase):
    """#509 item 3: test_connection() — same check as status() (resolve
    username -> userId, then confirm it), against EXPLICIT credentials.
    Never touches db.py — see subsonic_client's own TestConnectionTests
    for why that's the property that actually matters here."""

    def test_paired_when_username_resolves_and_confirms(self):
        users_list = _resp(json_body=[{"Name": "alice", "Id": "u1"}, {"Name": "bob", "Id": "u2"}])
        confirm = _resp(json_body={"Id": "u2"})
        with mock.patch("requests.get", side_effect=[users_list, confirm]):
            result = emby_client.test_connection("http://emby.local", "key1", "bob")
        self.assertEqual(result["state"], "paired")

    def test_disconnected_when_username_does_not_resolve(self):
        with mock.patch("requests.get", return_value=_resp(json_body=[{"Name": "alice", "Id": "u1"}])):
            result = emby_client.test_connection("http://emby.local", "key1", "nobody")
        self.assertEqual(result["state"], "disconnected")

    def test_never_persists_anything(self):
        users_list = _resp(json_body=[{"Name": "bob", "Id": "u2"}])
        confirm = _resp(json_body={"Id": "u2"})
        with mock.patch("requests.get", side_effect=[users_list, confirm]):
            emby_client.test_connection("http://emby.local", "key1", "bob")
        conn = db.get_conn()
        self.assertIsNone(db.get_config(conn, "emby_url"))
        self.assertIsNone(db.get_config(conn, "emby_api_key"))
        self.assertIsNone(db.get_config(conn, "emby_username"))
        self.assertIsNone(db.get_config(conn, "emby_user_id"))
        conn.close()

    def test_does_not_overwrite_an_existing_stored_connection(self):
        self._set_config(url="http://real.example.com", api_key="realkey", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(json_body=[])):
            emby_client.test_connection("http://typing-this.example.com", "x", "nobody")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "emby_url"), "http://real.example.com")
        conn.close()


class ListPlaylistsTests(_EmbyClientTestBase):
    def test_not_paired_without_user_id(self):
        self._set_config(url="http://emby.local", api_key="key1")
        self.assertEqual(emby_client.list_playlists(), {"status": "error", "reason": "not_paired"})

    def test_maps_items_to_common_shape(self):
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": [
            {"Name": "Road Trip", "Id": "p1"}, {"Name": "Chill", "Id": "p2"},
        ]})):
            result = emby_client.list_playlists()
        self.assertEqual(result, {"status": "ok", "playlists": [
            {"id": "p1", "title": "Road Trip"}, {"id": "p2", "title": "Chill"},
        ]})

    def test_items_missing_name_or_id_are_skipped(self):
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": [
            {"Name": "Good", "Id": "p1"}, {"Name": "NoId"}, {"Id": "p3"},
        ]})):
            result = emby_client.list_playlists()
        self.assertEqual(result["playlists"], [{"id": "p1", "title": "Good"}])

    def test_user_id_override_replaces_the_configured_default(self):
        # #262: per-Trobar-user mapping — a mapped user's own playlists,
        # not the server-wide default account's.
        self._set_config(url="http://emby.local", api_key="key1", user_id="default-user")
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": [
            {"Name": "Mapped User's Mix", "Id": "p9"},
        ]})) as get:
            result = emby_client.list_playlists(user_id="mapped-user")
        self.assertEqual(result["playlists"], [{"id": "p9", "title": "Mapped User's Mix"}])
        self.assertIn("/Users/mapped-user/Items", get.call_args.args[0])

    def test_no_override_falls_back_to_the_configured_default(self):
        self._set_config(url="http://emby.local", api_key="key1", user_id="default-user")
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": []})) as get:
            emby_client.list_playlists()
        self.assertIn("/Users/default-user/Items", get.call_args.args[0])


class ListUsersTests(_EmbyClientTestBase):
    def test_not_paired_when_request_fails(self):
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(status_code=500)):
            result = emby_client.list_users()
        self.assertEqual(result, {"status": "error", "reason": "not_paired"})

    def test_maps_users_to_common_shape(self):
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(json_body=[
            {"Name": "alice", "Id": "u1"}, {"Name": "bob", "Id": "u2"},
        ])):
            result = emby_client.list_users()
        self.assertEqual(result, {"status": "ok", "users": [
            {"id": "u1", "name": "alice"}, {"id": "u2", "name": "bob"},
        ]})

    def test_users_missing_name_or_id_are_skipped(self):
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(json_body=[
            {"Name": "good", "Id": "u1"}, {"Name": "noid"}, {"Id": "u3"},
        ])):
            result = emby_client.list_users()
        self.assertEqual(result["users"], [{"id": "u1", "name": "good"}])


class GetPlaylistTracksTests(_EmbyClientTestBase):
    def setUp(self):
        super().setUp()
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")

    def test_fetches_by_source_playlist_id_directly(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": [
            {"Name": "Track One", "Artists": ["Artist A"], "Path": "/music/a/1.flac", "Album": "Album A"},
        ]})) as get:
            result = emby_client.get_playlist_tracks("Road Trip", source_playlist_id="p1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tracks"], [{
            "position": 0, "title": "Track One", "artist": "Artist A",
            "path": "/music/a/1.flac", "album": "Album A",
        }])
        self.assertIn("/Playlists/p1/Items", get.call_args.args[0])

    def test_joins_multiple_artists_with_comma(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": [
            {"Name": "Feat Track", "Artists": ["Artist A", "Artist B"], "Path": None, "Album": None},
        ]})):
            result = emby_client.get_playlist_tracks("X", source_playlist_id="p1")
        self.assertEqual(result["tracks"][0]["artist"], "Artist A, Artist B")

    def test_user_id_override_is_sent_as_the_userid_param(self):
        # #262: a mapped user's own track fetch, not the default account's.
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": []})) as get:
            emby_client.get_playlist_tracks("X", source_playlist_id="p1", user_id="mapped-user")
        self.assertEqual(get.call_args.kwargs["params"]["userId"], "mapped-user")

    def test_no_override_uses_the_configured_default_userid(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": []})) as get:
            emby_client.get_playlist_tracks("X", source_playlist_id="p1")
        self.assertEqual(get.call_args.kwargs["params"]["userId"], "u1")

    def test_falls_back_to_title_lookup_when_no_id_given(self):
        list_resp = _resp(json_body={"Items": [{"Name": "Road Trip", "Id": "p1"}]})
        tracks_resp = _resp(json_body={"Items": []})
        with mock.patch("requests.get", side_effect=[list_resp, tracks_resp]):
            result = emby_client.get_playlist_tracks("Road Trip")
        self.assertEqual(result["status"], "ok")

    def test_title_not_found_reports_not_found(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"Items": []})):
            result = emby_client.get_playlist_tracks("Missing")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Missing"})


class GetArtistImageTests(_EmbyClientTestBase):
    def setUp(self):
        super().setUp()
        emby_client._artist_image_key_map = None
        emby_client._music_library_id = None
        self._set_config(url="http://emby.local", api_key="key1", user_id="u1")

    def test_returns_none_when_artist_unknown(self):
        with mock.patch("requests.get", return_value=_resp(json_body=[])):
            self.assertIsNone(emby_client.get_artist_image("Nobody"))

    def test_returns_bytes_and_content_type_for_a_known_artist(self):
        folders_resp = _resp(json_body=[{"CollectionType": "music", "ItemId": "lib1"}])
        artists_resp = _resp(json_body={"Items": [{"Name": "Artist A", "Id": "a1"}]})
        image_resp = mock.Mock(content=b"\x89PNG", headers={"Content-Type": "image/png"})
        image_resp.raise_for_status.return_value = None
        with mock.patch("requests.get", side_effect=[folders_resp, artists_resp, image_resp]):
            result = emby_client.get_artist_image("Artist A")
        self.assertEqual(result, (b"\x89PNG", "image/png"))


def _req_resp(status_code=200, json_body=None, content=True):
    """Same shape as _resp() above, for requests.request() (_request_as())
    rather than requests.get() (_get()) — no raise_for_status() side effect,
    since _request_as() never calls it (that's the whole point of the
    split: a 4xx/5xx status must still come back, not raise)."""
    r = mock.Mock()
    r.status_code = status_code
    r.content = b"x" if content else b""
    r.json.return_value = json_body if json_body is not None else {}
    return r


class _MirrorEmbyClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _configure(self, url="http://emby.local", api_key="key", username="admin", user_id="u1"):
        conn = db.get_conn()
        db.set_config(conn, "mirror_emby_url", url)
        db.set_config(conn, "mirror_emby_api_key", api_key)
        db.set_config(conn, "mirror_emby_username", username)
        db.set_config(conn, "mirror_emby_user_id", user_id)
        conn.commit()
        conn.close()


def _item(artist: str, album: str, title: str, item_id, track: int | None = None) -> dict:
    item = {"Artists": [artist], "Album": album, "Name": title, "Id": item_id}
    if track is not None:
        item["IndexNumber"] = track
    return item


class MirrorStatusTests(_MirrorEmbyClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(
            emby_client.mirror_status(),
            {"state": "disconnected", "url": "", "provider": "emby"},
        )

    def test_paired_when_user_lookup_confirms_the_id(self):
        self._configure(url="http://emby.local", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(json_body={"Id": "u1"})):
            self.assertEqual(
                emby_client.mirror_status(),
                {"state": "paired", "url": "http://emby.local", "provider": "emby"},
            )

    def test_disconnected_when_user_lookup_mismatches(self):
        self._configure(url="http://emby.local", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(json_body={"Id": "someone-else"})):
            self.assertEqual(
                emby_client.mirror_status(),
                {"state": "disconnected", "url": "http://emby.local", "provider": "emby"},
            )

    def test_disconnected_when_request_fails(self):
        self._configure(url="http://emby.local", user_id="u1")
        with mock.patch("requests.get", return_value=_resp(status_code=500)):
            self.assertEqual(
                emby_client.mirror_status(),
                {"state": "disconnected", "url": "http://emby.local", "provider": "emby"},
            )


class MirrorReconnectTests(_MirrorEmbyClientTestBase):
    def test_persists_config_and_resolves_user_id(self):
        users = [{"Name": "someone", "Id": "u0"}, {"Name": "admin", "Id": "u1"}]
        with mock.patch("requests.get", side_effect=[
            _resp(json_body=users),           # GET /Users (reconnect's own lookup)
            _resp(json_body={"Id": "u1"}),     # GET /Users/u1 (mirror_status() at the end)
        ]):
            result = emby_client.mirror_reconnect("http://emby.local", "key", "admin")
        self.assertEqual(db.get_mirror_emby_config(), ("http://emby.local", "key", "u1"))
        self.assertEqual(result["state"], "paired")

    def test_unmatched_username_leaves_user_id_blank_and_unconfigured(self):
        with mock.patch("requests.get", return_value=_resp(json_body=[{"Name": "nope", "Id": "u9"}])):
            emby_client.mirror_reconnect("http://emby.local", "key", "admin")
        self.assertIsNone(db.get_mirror_emby_config())

    def test_never_touches_the_active_provider_config(self):
        # #189's whole point: this is a distinct connection from
        # emby_url/api_key/username/user_id.
        conn = db.get_conn()
        db.set_config(conn, "emby_url", "http://active.local")
        conn.commit()
        conn.close()
        with mock.patch("requests.get", return_value=_resp(json_body=[])):
            emby_client.mirror_reconnect("http://mirror.local", "key", "admin")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "emby_url"), "http://active.local")
        conn.close()


class MirrorBuildTagIndexTests(_MirrorEmbyClientTestBase):
    def test_none_when_not_configured(self):
        self.assertIsNone(emby_client.mirror_build_tag_index())

    def test_builds_the_whole_index_keyed_on_normalized_tags(self):
        self._configure()
        items = [_item("Artist", "Album", "Song One", "i1"), _item("Artist", "Album", "Song Two", "i2")]
        with mock.patch("requests.get", side_effect=[
            _resp(json_body={"Items": items}),
            _resp(json_body={"Items": []}),
        ]):
            index = emby_client.mirror_build_tag_index()
        self.assertEqual(index, {
            ("artist", "album", "song one"): [{"id": "i1", "track_no": None}],
            ("artist", "album", "song two"): [{"id": "i2", "track_no": None}],
        })

    def test_a_repeated_tag_key_collects_every_candidate(self):
        self._configure()
        items = [
            _item("Artist", "Album", "Song", "i1", track=1),
            _item("Artist", "Album", "Song", "i2", track=1),
        ]
        with mock.patch("requests.get", side_effect=[
            _resp(json_body={"Items": items}),
            _resp(json_body={"Items": []}),
        ]):
            index = emby_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual({c["id"] for c in index[("artist", "album", "song")]}, {"i1", "i2"})

    def test_pagination_continues_past_a_short_non_empty_page(self):
        page1 = _resp(json_body={"Items": [_item("A", "Al", "One", "1")]})  # short: 1 < page size 2
        page2 = _resp(json_body={"Items": [_item("B", "Al", "Two", "2")]})
        page3 = _resp(json_body={"Items": []})
        self._configure()
        with mock.patch.object(emby_client, "_MIRROR_PAGE_SIZE", 2):
            with mock.patch("requests.get", side_effect=[page1, page2, page3]) as get:
                index = emby_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual({("a", "al", "one"), ("b", "al", "two")}, set(index.keys()))
        starts = [call.kwargs["params"]["StartIndex"] for call in get.call_args_list]
        self.assertEqual(starts, [0, 2, 4])

    def test_page_cap_backstop_gives_up_if_a_page_is_never_empty(self):
        self._configure()
        full_page = _resp(json_body={"Items": [_item("A", "Al", "One", "1"), _item("B", "Al", "Two", "2")]})
        with mock.patch.object(emby_client, "_MIRROR_PAGE_SIZE", 2), \
             mock.patch.object(emby_client, "_MIRROR_MAX_PAGES", 3):
            with mock.patch("requests.get", return_value=full_page):
                self.assertIsNone(emby_client.mirror_build_tag_index())

    def test_a_failing_page_mid_walk_fails_the_whole_index(self):
        self._configure()
        page1 = _resp(json_body={"Items": [_item("A", "Al", "One", "1"), _item("B", "Al", "Two", "2")]})
        with mock.patch.object(emby_client, "_MIRROR_PAGE_SIZE", 2):
            with mock.patch("requests.get", side_effect=[page1, _resp(status_code=500)]):
                self.assertIsNone(emby_client.mirror_build_tag_index())

    def test_items_missing_id_are_skipped(self):
        self._configure()
        items = [_item("Artist", "Album", "Good", "i1"), {"Artists": ["A"], "Album": "B", "Name": "C"}]
        with mock.patch("requests.get", side_effect=[
            _resp(json_body={"Items": items}),
            _resp(json_body={"Items": []}),
        ]):
            index = emby_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual(list(index.keys()), [("artist", "album", "good")])


class MirrorCreateOrReplacePlaylistTests(_MirrorEmbyClientTestBase):
    def test_error_when_not_configured(self):
        self.assertEqual(
            emby_client.mirror_create_or_replace_playlist("Chill", ["1", "2"], None),
            {"status": "error", "reason": "not_configured", "code": None},
        )

    def test_create_posts_query_params_and_returns_the_new_remote_id(self):
        self._configure()
        with mock.patch("requests.request", return_value=_req_resp(json_body={"Id": "42"})) as req:
            result = emby_client.mirror_create_or_replace_playlist("Chill", ["1", "2"], None)
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        self.assertEqual(req.call_args.args[:2], ("POST", "http://emby.local/Playlists"))
        params = req.call_args.kwargs["params"]
        self.assertEqual(params["Name"], "Chill")
        self.assertEqual(params["Ids"], "1,2")
        self.assertIsNone(req.call_args.kwargs["json"])

    def test_create_with_no_songs_omits_the_ids_param_entirely(self):
        # Confirmed live: Emby's create succeeds with Ids simply absent, not
        # an empty string -- an empty Ids= was never verified and isn't
        # worth risking.
        self._configure()
        with mock.patch("requests.request", return_value=_req_resp(json_body={"Id": "42"})) as req:
            emby_client.mirror_create_or_replace_playlist("Empty", [], None)
        self.assertNotIn("Ids", req.call_args.kwargs["params"])

    def test_create_failure_surfaces_the_status_code(self):
        self._configure()
        with mock.patch("requests.request", return_value=_req_resp(status_code=500, content=False)):
            result = emby_client.mirror_create_or_replace_playlist("Chill", [], None)
        self.assertEqual(result, {"status": "error", "reason": "create failed", "code": 500})

    def test_create_ok_status_missing_id_is_still_an_error(self):
        self._configure()
        with mock.patch("requests.request", return_value=_req_resp(json_body={})):
            result = emby_client.mirror_create_or_replace_playlist("Chill", [], None)
        self.assertEqual(result["status"], "error")

    def test_replace_checks_existence_then_reads_deletes_and_adds(self):
        self._configure()
        exists = _req_resp(json_body={"Id": "42"})
        current = _req_resp(json_body={"Items": [
            {"Id": "old1", "PlaylistItemId": "1"}, {"Id": "old2", "PlaylistItemId": "2"}]})
        delete_resp = _req_resp(status_code=204, content=False)
        add_resp = _req_resp(status_code=204, content=False)
        with mock.patch("requests.request", side_effect=[exists, current, delete_resp, add_resp]) as req:
            result = emby_client.mirror_create_or_replace_playlist("Chill", ["new1"], "42")
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        exists_call, get_call, delete_call, add_call = req.call_args_list
        self.assertEqual(exists_call.args[:2], ("GET", "http://emby.local/Users/u1/Items/42"))
        self.assertEqual(get_call.args[:2], ("GET", "http://emby.local/Playlists/42/Items"))
        self.assertEqual(delete_call.args[:2], ("DELETE", "http://emby.local/Playlists/42/Items"))
        # #189 review-analog: entry ids, NOT the tracks' own ids -- Emby's
        # two fields are genuinely different values.
        self.assertEqual(delete_call.kwargs["params"]["EntryIds"], "1,2")
        self.assertEqual(add_call.args[:2], ("POST", "http://emby.local/Playlists/42/Items"))
        self.assertEqual(add_call.kwargs["params"]["Ids"], "new1")

    def test_replace_skips_delete_when_nothing_exists_yet(self):
        self._configure()
        exists = _req_resp(json_body={"Id": "42"})
        current = _req_resp(json_body={"Items": []})
        add_resp = _req_resp(status_code=204, content=False)
        with mock.patch("requests.request", side_effect=[exists, current, add_resp]) as req:
            result = emby_client.mirror_create_or_replace_playlist("Chill", ["new1"], "42")
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        self.assertEqual(len(req.call_args_list), 3)

    def test_replace_skips_add_when_song_ids_is_empty(self):
        self._configure()
        exists = _req_resp(json_body={"Id": "42"})
        current = _req_resp(json_body={"Items": [{"Id": "old1", "PlaylistItemId": "1"}]})
        delete_resp = _req_resp(status_code=204, content=False)
        with mock.patch("requests.request", side_effect=[exists, current, delete_resp]) as req:
            result = emby_client.mirror_create_or_replace_playlist("Chill", [], "42")
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        self.assertEqual(len(req.call_args_list), 3)

    def test_a_stale_id_is_caught_by_the_upfront_existence_check_not_a_500(self):
        # #189: the real divergence from Jellyfin's sink -- GET .../Items
        # for a bad playlist id is a bare 500 here (confirmed live), so the
        # existence check (a DIFFERENT endpoint, confirmed live to 404
        # cleanly) has to run first and short-circuit before ever reaching
        # that flaky call.
        self._configure()
        with mock.patch("requests.request", return_value=_req_resp(status_code=404, content=False)) as req:
            result = emby_client.mirror_create_or_replace_playlist("Chill", [], "stale-id")
        self.assertEqual(result, {"status": "error", "reason": "playlist not found", "code": 404})
        req.assert_called_once()  # never reached the flaky GET .../Items call

    def test_existence_check_failure_for_an_unrelated_reason(self):
        self._configure()
        with mock.patch("requests.request", return_value=_req_resp(status_code=500, content=False)):
            result = emby_client.mirror_create_or_replace_playlist("Chill", [], "42")
        self.assertEqual(result, {"status": "error", "reason": "failed to check playlist", "code": 500})

    def test_replace_failure_reading_current_items(self):
        self._configure()
        exists = _req_resp(json_body={"Id": "42"})
        with mock.patch("requests.request", side_effect=[exists, _req_resp(status_code=500, content=False)]):
            result = emby_client.mirror_create_or_replace_playlist("Chill", [], "42")
        self.assertEqual(
            result, {"status": "error", "reason": "failed to read current items", "code": 500})

    def test_replace_failure_deleting_existing_items(self):
        self._configure()
        exists = _req_resp(json_body={"Id": "42"})
        current = _req_resp(json_body={"Items": [{"Id": "old1", "PlaylistItemId": "1"}]})
        with mock.patch("requests.request", side_effect=[
            exists, current, _req_resp(status_code=500, content=False),
        ]):
            result = emby_client.mirror_create_or_replace_playlist("Chill", ["new1"], "42")
        self.assertEqual(
            result, {"status": "error", "reason": "failed to clear existing items", "code": 500})

    def test_replace_failure_adding_new_items(self):
        self._configure()
        exists = _req_resp(json_body={"Id": "42"})
        current = _req_resp(json_body={"Items": []})
        with mock.patch("requests.request", side_effect=[
            exists, current, _req_resp(status_code=500, content=False),
        ]):
            result = emby_client.mirror_create_or_replace_playlist("Chill", ["new1"], "42")
        self.assertEqual(result, {"status": "error", "reason": "failed to add items", "code": 500})


class MirrorSetPlaylistMetadataTests(_MirrorEmbyClientTestBase):
    def test_no_request_when_not_configured(self):
        with mock.patch("requests.get") as get, mock.patch("requests.request") as req:
            emby_client.mirror_set_playlist_metadata("42", "Chill", "Trobar mirror")
        get.assert_not_called()
        req.assert_not_called()

    def test_gets_then_mutates_and_posts_the_whole_item_back(self):
        self._configure()
        item = {"Id": "42", "Name": "Old Name", "Overview": "old", "SomeOtherField": "keep-me"}
        with mock.patch("requests.get", return_value=_resp(json_body=item)) as get, \
             mock.patch("requests.request", return_value=_req_resp(status_code=204, content=False)) as req:
            emby_client.mirror_set_playlist_metadata("42", "Chill", "Trobar mirror — 2 of 2 present")
        self.assertIn("/Users/u1/Items/42", get.call_args.args[0])
        self.assertEqual(req.call_args.args[:2], ("POST", "http://emby.local/Items/42"))
        posted = req.call_args.kwargs["json"]
        self.assertEqual(posted["Name"], "Chill")
        self.assertEqual(posted["Overview"], "Trobar mirror — 2 of 2 present")
        self.assertEqual(posted["SomeOtherField"], "keep-me")

    def test_no_post_when_the_get_fails(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(status_code=500)), \
             mock.patch("requests.request") as req:
            emby_client.mirror_set_playlist_metadata("42", "Chill", "x")  # must not raise
        req.assert_not_called()


class MirrorDeletePlaylistTests(_MirrorEmbyClientTestBase):
    def test_false_when_not_configured(self):
        self.assertFalse(emby_client.mirror_delete_playlist("42"))

    def test_true_on_a_confirmed_delete(self):
        self._configure()
        with mock.patch("requests.request", return_value=_req_resp(status_code=204, content=False)) as req:
            self.assertTrue(emby_client.mirror_delete_playlist("42"))
        self.assertEqual(req.call_args.args[:2], ("DELETE", "http://emby.local/Items/42"))

    def test_false_on_a_failed_delete(self):
        self._configure()
        with mock.patch("requests.request", return_value=_req_resp(status_code=500, content=False)):
            self.assertFalse(emby_client.mirror_delete_playlist("42"))


if __name__ == "__main__":
    unittest.main()
