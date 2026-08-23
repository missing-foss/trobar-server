#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for jellyfin_client.py — mocks requests, no network access needed.

#390: the Active* classes below cover the active-provider side (status/
reconnect/list_playlists/get_playlist_tracks/get_artist_image) — this
predated the #189 mirror-target work and had no coverage until now. Nearly
every active-provider call goes through _get(), itself built on
_request_as()'s requests.request() — mocked accordingly — EXCEPT
get_artist_image()'s own image fetch, which bypasses _get() entirely for a
direct requests.get() call with a simpler auth header (no Client/Device/
DeviceId/Version, just the bare token), so that one test mocks requests.get
instead.

The Mirror* classes cover the #189 mirror-TARGET additions (the mirror_*()
functions and the _request_as()/_get() split at the bottom of the module).

    python3 -m unittest test_jellyfin_client -v      # from app/
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="trobar-test-jellyfin-client-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import jellyfin_client  # noqa: E402


def _resp(status_code=200, body=None):
    r = mock.Mock()
    r.status_code = status_code
    r.content = b"x" if body is not None else b""
    r.json.return_value = body if body is not None else {}
    return r


def _item(artist: str, album: str, title: str, item_id, track: int | None = None) -> dict:
    item = {"Artists": [artist], "Album": album, "Name": title, "Id": item_id}
    if track is not None:
        item["IndexNumber"] = track
    return item


class _JellyfinClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _configure(self, url="http://jf.local", api_key="key", username="trobar", user_id="u1"):
        conn = db.get_conn()
        db.set_config(conn, "mirror_jellyfin_url", url)
        db.set_config(conn, "mirror_jellyfin_api_key", api_key)
        db.set_config(conn, "mirror_jellyfin_username", username)
        db.set_config(conn, "mirror_jellyfin_user_id", user_id)
        conn.commit()
        conn.close()


class _ActiveJellyfinClientTestBase(unittest.TestCase):
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
        db.set_config(conn, "jellyfin_url", url)
        db.set_config(conn, "jellyfin_api_key", api_key)
        db.set_config(conn, "jellyfin_user_id", user_id)
        conn.commit()
        conn.close()


class ActiveStatusTests(_ActiveJellyfinClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(jellyfin_client.status()["state"], "disconnected")

    def test_disconnected_when_user_id_not_set(self):
        self._set_config(url="http://jf.local", api_key="key1")
        self.assertEqual(jellyfin_client.status()["state"], "disconnected")

    def test_paired_when_user_id_resolves(self):
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(body={"Id": "u1"})):
            self.assertEqual(jellyfin_client.status()["state"], "paired")

    def test_disconnected_when_user_lookup_fails(self):
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(status_code=404)):
            self.assertEqual(jellyfin_client.status()["state"], "disconnected")

    def test_status_reports_the_jellyfin_provider_id(self):
        self.assertEqual(jellyfin_client.status()["provider"], "jellyfin")


class ActiveReconnectTests(_ActiveJellyfinClientTestBase):
    def test_persists_config_and_resolves_user_id(self):
        # reconnect() finishes by calling status(), a second call (a dict
        # response, not the /Users list) — two distinct mocked responses.
        users_list = _resp(body=[{"Name": "alice", "Id": "u1"}, {"Name": "bob", "Id": "u2"}])
        status_check = _resp(body={"Id": "u2"})
        with mock.patch("requests.request", side_effect=[users_list, status_check]):
            result = jellyfin_client.reconnect("http://jf.local", "key1", "bob")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "jellyfin_url"), "http://jf.local")
        self.assertEqual(db.get_config(conn, "jellyfin_api_key"), "key1")
        self.assertEqual(db.get_config(conn, "jellyfin_username"), "bob")
        self.assertEqual(db.get_config(conn, "jellyfin_user_id"), "u2")
        conn.close()
        self.assertEqual(result["state"], "paired")

    def test_unmatched_username_leaves_user_id_blank(self):
        with mock.patch("requests.request", return_value=_resp(body=[{"Name": "alice", "Id": "u1"}])):
            jellyfin_client.reconnect("http://jf.local", "key1", "nobody")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "jellyfin_user_id"), "")
        conn.close()

    def test_auth_header_uses_the_mediabrowser_scheme(self):
        # The one real behavioral delta from emby_client's bare X-Emby-Token
        # scheme — worth pinning directly rather than trusting it stays
        # right through future edits.
        with mock.patch("requests.request", return_value=_resp(body=[])) as req:
            jellyfin_client.reconnect("http://jf.local", "secret-key", "alice")
        auth = req.call_args.kwargs["headers"]["Authorization"]
        self.assertIn('Token="secret-key"', auth)
        self.assertIn("MediaBrowser", auth)


class TestConnectionTests(_ActiveJellyfinClientTestBase):
    """#509 item 3: test_connection() — same check as status() (resolve
    username -> userId, then confirm it), against EXPLICIT credentials.
    Never touches db.py — see subsonic_client's own TestConnectionTests
    for why that's the property that actually matters here."""

    def test_paired_when_username_resolves_and_confirms(self):
        users_list = _resp(body=[{"Name": "alice", "Id": "u1"}, {"Name": "bob", "Id": "u2"}])
        confirm = _resp(body={"Id": "u2"})
        with mock.patch("requests.request", side_effect=[users_list, confirm]):
            result = jellyfin_client.test_connection("http://jf.local", "key1", "bob")
        self.assertEqual(result["state"], "paired")

    def test_disconnected_when_username_does_not_resolve(self):
        with mock.patch("requests.request", return_value=_resp(body=[{"Name": "alice", "Id": "u1"}])):
            result = jellyfin_client.test_connection("http://jf.local", "key1", "nobody")
        self.assertEqual(result["state"], "disconnected")

    def test_never_persists_anything(self):
        users_list = _resp(body=[{"Name": "bob", "Id": "u2"}])
        confirm = _resp(body={"Id": "u2"})
        with mock.patch("requests.request", side_effect=[users_list, confirm]):
            jellyfin_client.test_connection("http://jf.local", "key1", "bob")
        conn = db.get_conn()
        self.assertIsNone(db.get_config(conn, "jellyfin_url"))
        self.assertIsNone(db.get_config(conn, "jellyfin_api_key"))
        self.assertIsNone(db.get_config(conn, "jellyfin_username"))
        self.assertIsNone(db.get_config(conn, "jellyfin_user_id"))
        conn.close()

    def test_does_not_overwrite_an_existing_stored_connection(self):
        self._set_config(url="http://real.example.com", api_key="realkey", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(body=[])):
            jellyfin_client.test_connection("http://typing-this.example.com", "x", "nobody")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "jellyfin_url"), "http://real.example.com")
        conn.close()


class ActiveListPlaylistsTests(_ActiveJellyfinClientTestBase):
    def test_not_paired_without_user_id(self):
        self._set_config(url="http://jf.local", api_key="key1")
        self.assertEqual(jellyfin_client.list_playlists(), {"status": "error", "reason": "not_paired"})

    def test_maps_items_to_common_shape(self):
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(body={"Items": [
            {"Name": "Road Trip", "Id": "p1"}, {"Name": "Chill", "Id": "p2"},
        ]})):
            result = jellyfin_client.list_playlists()
        self.assertEqual(result, {"status": "ok", "playlists": [
            {"id": "p1", "title": "Road Trip"}, {"id": "p2", "title": "Chill"},
        ]})

    def test_items_missing_name_or_id_are_skipped(self):
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(body={"Items": [
            {"Name": "Good", "Id": "p1"}, {"Name": "NoId"}, {"Id": "p3"},
        ]})):
            result = jellyfin_client.list_playlists()
        self.assertEqual(result["playlists"], [{"id": "p1", "title": "Good"}])

    def test_user_id_override_replaces_the_configured_default(self):
        # #262: per-Trobar-user mapping — a mapped user's own playlists,
        # not the server-wide default account's.
        self._set_config(url="http://jf.local", api_key="key1", user_id="default-user")
        with mock.patch("requests.request", return_value=_resp(body={"Items": [
            {"Name": "Mapped User's Mix", "Id": "p9"},
        ]})) as req:
            result = jellyfin_client.list_playlists(user_id="mapped-user")
        self.assertEqual(result["playlists"], [{"id": "p9", "title": "Mapped User's Mix"}])
        self.assertIn("/Users/mapped-user/Items", req.call_args.args[1])

    def test_no_override_falls_back_to_the_configured_default(self):
        self._set_config(url="http://jf.local", api_key="key1", user_id="default-user")
        with mock.patch("requests.request", return_value=_resp(body={"Items": []})) as req:
            jellyfin_client.list_playlists()
        self.assertIn("/Users/default-user/Items", req.call_args.args[1])


class ActiveListUsersTests(_ActiveJellyfinClientTestBase):
    def test_not_paired_when_request_fails(self):
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(status_code=500)):
            result = jellyfin_client.list_users()
        self.assertEqual(result, {"status": "error", "reason": "not_paired"})

    def test_maps_users_to_common_shape(self):
        # #262: the mapping UI's target-user list — id + name, same shape
        # as list_playlists()'s own {"id", "title"} convention adapted to
        # users instead of playlists.
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(body=[
            {"Name": "alice", "Id": "u1"}, {"Name": "bob", "Id": "u2"},
        ])):
            result = jellyfin_client.list_users()
        self.assertEqual(result, {"status": "ok", "users": [
            {"id": "u1", "name": "alice"}, {"id": "u2", "name": "bob"},
        ]})

    def test_users_missing_name_or_id_are_skipped(self):
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(body=[
            {"Name": "good", "Id": "u1"}, {"Name": "noid"}, {"Id": "u3"},
        ])):
            result = jellyfin_client.list_users()
        self.assertEqual(result["users"], [{"id": "u1", "name": "good"}])


class ActiveGetPlaylistTracksTests(_ActiveJellyfinClientTestBase):
    def setUp(self):
        super().setUp()
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")

    def test_fetches_by_source_playlist_id_directly(self):
        with mock.patch("requests.request", return_value=_resp(body={"Items": [
            {"Name": "Track One", "Artists": ["Artist A"], "Path": "/music/a/1.flac", "Album": "Album A"},
        ]})) as req:
            result = jellyfin_client.get_playlist_tracks("Road Trip", source_playlist_id="p1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tracks"], [{
            "position": 0, "title": "Track One", "artist": "Artist A",
            "path": "/music/a/1.flac", "album": "Album A",
        }])
        self.assertIn("/Playlists/p1/Items", req.call_args.args[1])

    def test_joins_multiple_artists_with_comma(self):
        with mock.patch("requests.request", return_value=_resp(body={"Items": [
            {"Name": "Feat Track", "Artists": ["Artist A", "Artist B"], "Path": None, "Album": None},
        ]})):
            result = jellyfin_client.get_playlist_tracks("X", source_playlist_id="p1")
        self.assertEqual(result["tracks"][0]["artist"], "Artist A, Artist B")

    def test_user_id_override_is_sent_as_the_userid_param(self):
        # #262: a mapped user's own track fetch, not the default account's.
        with mock.patch("requests.request", return_value=_resp(body={"Items": []})) as req:
            jellyfin_client.get_playlist_tracks("X", source_playlist_id="p1", user_id="mapped-user")
        self.assertEqual(req.call_args.kwargs["params"]["userId"], "mapped-user")

    def test_no_override_uses_the_configured_default_userid(self):
        with mock.patch("requests.request", return_value=_resp(body={"Items": []})) as req:
            jellyfin_client.get_playlist_tracks("X", source_playlist_id="p1")
        self.assertEqual(req.call_args.kwargs["params"]["userId"], "u1")

    def test_falls_back_to_title_lookup_when_no_id_given(self):
        list_resp = _resp(body={"Items": [{"Name": "Road Trip", "Id": "p1"}]})
        tracks_resp = _resp(body={"Items": []})
        with mock.patch("requests.request", side_effect=[list_resp, tracks_resp]):
            result = jellyfin_client.get_playlist_tracks("Road Trip")
        self.assertEqual(result["status"], "ok")

    def test_title_not_found_reports_not_found(self):
        with mock.patch("requests.request", return_value=_resp(body={"Items": []})):
            result = jellyfin_client.get_playlist_tracks("Missing")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Missing"})


class ActiveGetArtistImageTests(_ActiveJellyfinClientTestBase):
    def setUp(self):
        super().setUp()
        jellyfin_client._artist_image_key_map = None
        jellyfin_client._music_library_id = None
        self._set_config(url="http://jf.local", api_key="key1", user_id="u1")

    def test_returns_none_when_artist_unknown(self):
        with mock.patch("requests.request", return_value=_resp(body=[])):
            self.assertIsNone(jellyfin_client.get_artist_image("Nobody"))

    def test_returns_bytes_and_content_type_for_a_known_artist(self):
        folders_resp = _resp(body=[{"CollectionType": "music", "ItemId": "lib1"}])
        artists_resp = _resp(body={"Items": [{"Name": "Artist A", "Id": "a1"}]})
        image_resp = mock.Mock(content=b"\x89PNG", headers={"Content-Type": "image/png"})
        image_resp.raise_for_status.return_value = None
        with mock.patch("requests.request", side_effect=[folders_resp, artists_resp]), \
             mock.patch("requests.get", return_value=image_resp) as get:
            result = jellyfin_client.get_artist_image("Artist A")
        self.assertEqual(result, (b"\x89PNG", "image/png"))
        # #390: the one function on the active-provider side that bypasses
        # _get()/_request_as() entirely for a direct requests.get() call —
        # confirm it's using the simpler bare-token header, not the fuller
        # MediaBrowser scheme every other call here sends.
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": 'MediaBrowser Token="key1"'})


class MirrorStatusTests(_JellyfinClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(
            jellyfin_client.mirror_status(),
            {"state": "disconnected", "url": "", "provider": "jellyfin"},
        )

    def test_paired_when_user_lookup_confirms_the_id(self):
        self._configure(url="http://jf.local", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(body={"Id": "u1"})):
            self.assertEqual(
                jellyfin_client.mirror_status(),
                {"state": "paired", "url": "http://jf.local", "provider": "jellyfin"},
            )

    def test_disconnected_when_user_lookup_mismatches(self):
        self._configure(url="http://jf.local", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(body={"Id": "someone-else"})):
            self.assertEqual(
                jellyfin_client.mirror_status(),
                {"state": "disconnected", "url": "http://jf.local", "provider": "jellyfin"},
            )

    def test_disconnected_when_request_fails(self):
        self._configure(url="http://jf.local", user_id="u1")
        with mock.patch("requests.request", return_value=_resp(status_code=500)):
            self.assertEqual(
                jellyfin_client.mirror_status(),
                {"state": "disconnected", "url": "http://jf.local", "provider": "jellyfin"},
            )


class MirrorReconnectTests(_JellyfinClientTestBase):
    def test_persists_config_and_resolves_user_id(self):
        users = [{"Name": "someone", "Id": "u0"}, {"Name": "trobar", "Id": "u1"}]
        with mock.patch("requests.request", side_effect=[
            _resp(body=users),               # GET /Users (reconnect's own lookup)
            _resp(body={"Id": "u1"}),        # GET /Users/u1 (mirror_status() at the end)
        ]):
            result = jellyfin_client.mirror_reconnect("http://jf.local", "key", "trobar")
        self.assertEqual(
            db.get_mirror_jellyfin_config(), ("http://jf.local", "key", "u1"))
        self.assertEqual(result["state"], "paired")

    def test_unmatched_username_leaves_user_id_blank_and_unconfigured(self):
        with mock.patch("requests.request", return_value=_resp(body=[{"Name": "nope", "Id": "u9"}])):
            jellyfin_client.mirror_reconnect("http://jf.local", "key", "trobar")
        self.assertIsNone(db.get_mirror_jellyfin_config())

    def test_never_touches_the_active_provider_config(self):
        # #189's whole point: this is a distinct connection from
        # jellyfin_url/api_key/username/user_id.
        conn = db.get_conn()
        db.set_config(conn, "jellyfin_url", "http://active.local")
        conn.commit()
        conn.close()
        with mock.patch("requests.request", return_value=_resp(body=[])):
            jellyfin_client.mirror_reconnect("http://mirror.local", "key", "trobar")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "jellyfin_url"), "http://active.local")
        conn.close()


class MirrorBuildTagIndexTests(_JellyfinClientTestBase):
    def test_none_when_not_configured(self):
        self.assertIsNone(jellyfin_client.mirror_build_tag_index())

    def test_builds_the_whole_index_keyed_on_normalized_tags(self):
        self._configure()
        items = [_item("Artist", "Album", "Song One", "i1"), _item("Artist", "Album", "Song Two", "i2")]
        with mock.patch("requests.request", side_effect=[
            _resp(body={"Items": items}),
            _resp(body={"Items": []}),
        ]):
            index = jellyfin_client.mirror_build_tag_index()
        self.assertEqual(index, {
            ("artist", "album", "song one"): [{"id": "i1", "track_no": None}],
            ("artist", "album", "song two"): [{"id": "i2", "track_no": None}],
        })

    def test_multiple_artists_are_joined_the_same_way_the_read_side_matches(self):
        self._configure()
        item = {"Artists": ["A", "B"], "Album": "Al", "Name": "Song", "Id": "i1"}
        with mock.patch("requests.request", side_effect=[
            _resp(body={"Items": [item]}),
            _resp(body={"Items": []}),
        ]):
            index = jellyfin_client.mirror_build_tag_index()
        assert index is not None
        self.assertIn(("a, b", "al", "song"), index)

    def test_a_repeated_tag_key_collects_every_candidate(self):
        self._configure()
        items = [
            _item("Artist", "Album", "Song", "i1", track=1),
            _item("Artist", "Album", "Song", "i2", track=1),
        ]
        with mock.patch("requests.request", side_effect=[
            _resp(body={"Items": items}),
            _resp(body={"Items": []}),
        ]):
            index = jellyfin_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual({c["id"] for c in index[("artist", "album", "song")]}, {"i1", "i2"})

    def test_pagination_continues_past_a_short_non_empty_page(self):
        page1 = _resp(body={"Items": [_item("A", "Al", "One", "1")]})  # short: 1 < page size 2
        page2 = _resp(body={"Items": [_item("B", "Al", "Two", "2")]})
        page3 = _resp(body={"Items": []})
        self._configure()
        with mock.patch.object(jellyfin_client, "_MIRROR_PAGE_SIZE", 2):
            with mock.patch("requests.request", side_effect=[page1, page2, page3]) as req:
                index = jellyfin_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual({("a", "al", "one"), ("b", "al", "two")}, set(index.keys()))
        starts = [call.kwargs["params"]["StartIndex"] for call in req.call_args_list]
        self.assertEqual(starts, [0, 2, 4])

    def test_page_cap_backstop_gives_up_if_a_page_is_never_empty(self):
        self._configure()
        full_page = _resp(body={"Items": [_item("A", "Al", "One", "1"), _item("B", "Al", "Two", "2")]})
        with mock.patch.object(jellyfin_client, "_MIRROR_PAGE_SIZE", 2), \
             mock.patch.object(jellyfin_client, "_MIRROR_MAX_PAGES", 3):
            with mock.patch("requests.request", return_value=full_page):
                self.assertIsNone(jellyfin_client.mirror_build_tag_index())

    def test_a_failing_page_mid_walk_fails_the_whole_index(self):
        self._configure()
        page1 = _resp(body={"Items": [_item("A", "Al", "One", "1"), _item("B", "Al", "Two", "2")]})
        with mock.patch.object(jellyfin_client, "_MIRROR_PAGE_SIZE", 2):
            with mock.patch("requests.request", side_effect=[page1, _resp(status_code=500)]):
                self.assertIsNone(jellyfin_client.mirror_build_tag_index())

    def test_items_missing_id_are_skipped(self):
        self._configure()
        items = [_item("Artist", "Album", "Good", "i1"), {"Artists": ["A"], "Album": "B", "Name": "C"}]
        with mock.patch("requests.request", side_effect=[
            _resp(body={"Items": items}),
            _resp(body={"Items": []}),
        ]):
            index = jellyfin_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual(list(index.keys()), [("artist", "album", "good")])


class MirrorCreateOrReplacePlaylistTests(_JellyfinClientTestBase):
    def test_error_when_not_configured(self):
        self.assertEqual(
            jellyfin_client.mirror_create_or_replace_playlist("Chill", ["1", "2"], None),
            {"status": "error", "reason": "not_configured", "code": None},
        )

    def test_create_posts_to_playlists_and_returns_the_new_remote_id(self):
        self._configure()
        with mock.patch("requests.request", return_value=_resp(body={"Id": "42"})) as req:
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", ["1", "2"], None)
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        self.assertEqual(req.call_args.args[:2], ("POST", "http://jf.local/Playlists"))
        body = req.call_args.kwargs["json"]
        self.assertEqual(body["Name"], "Chill")
        self.assertEqual(body["Ids"], ["1", "2"])

    def test_create_failure_surfaces_the_status_code(self):
        self._configure()
        with mock.patch("requests.request", return_value=_resp(status_code=500)):
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", [], None)
        self.assertEqual(result, {"status": "error", "reason": "create failed", "code": 500})

    def test_create_ok_status_missing_id_is_still_an_error(self):
        self._configure()
        with mock.patch("requests.request", return_value=_resp(body={})):
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", [], None)
        self.assertEqual(result["status"], "error")

    def test_replace_reads_current_items_deletes_then_adds(self):
        self._configure()
        current = _resp(body={"Items": [{"Id": "old1"}, {"Id": "old2"}]})
        delete_resp = _resp(status_code=204)
        add_resp = _resp(status_code=204)
        with mock.patch("requests.request", side_effect=[current, delete_resp, add_resp]) as req:
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", ["new1"], "42")
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        get_call, delete_call, add_call = req.call_args_list
        self.assertEqual(get_call.args[:2], ("GET", "http://jf.local/Playlists/42/Items"))
        self.assertEqual(delete_call.args[:2], ("DELETE", "http://jf.local/Playlists/42/Items"))
        self.assertEqual(delete_call.kwargs["params"]["entryIds"], "old1,old2")
        self.assertEqual(add_call.args[:2], ("POST", "http://jf.local/Playlists/42/Items"))
        self.assertEqual(add_call.kwargs["params"]["ids"], "new1")

    def test_replace_skips_delete_when_nothing_exists_yet(self):
        self._configure()
        current = _resp(body={"Items": []})
        add_resp = _resp(status_code=204)
        with mock.patch("requests.request", side_effect=[current, add_resp]) as req:
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", ["new1"], "42")
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        self.assertEqual(len(req.call_args_list), 2)

    def test_replace_skips_add_when_song_ids_is_empty(self):
        self._configure()
        current = _resp(body={"Items": [{"Id": "old1"}]})
        delete_resp = _resp(status_code=204)
        with mock.patch("requests.request", side_effect=[current, delete_resp]) as req:
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", [], "42")
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        self.assertEqual(len(req.call_args_list), 2)

    def test_replace_surfaces_a_404_as_a_specific_not_found_code(self):
        # mirror_jellyfin.write_mirror() reacts to this SPECIFIC code (a
        # stale remote id) without string-matching `reason`.
        self._configure()
        with mock.patch("requests.request", return_value=_resp(status_code=404)):
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", [], "stale-id")
        self.assertEqual(result, {"status": "error", "reason": "playlist not found", "code": 404})

    def test_replace_failure_reading_current_items(self):
        self._configure()
        with mock.patch("requests.request", return_value=_resp(status_code=500)):
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", [], "42")
        self.assertEqual(result, {"status": "error", "reason": "failed to read current items", "code": 500})

    def test_replace_failure_deleting_existing_items(self):
        self._configure()
        current = _resp(body={"Items": [{"Id": "old1"}]})
        with mock.patch("requests.request", side_effect=[current, _resp(status_code=500)]):
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", ["new1"], "42")
        self.assertEqual(
            result, {"status": "error", "reason": "failed to clear existing items", "code": 500})

    def test_replace_failure_adding_new_items(self):
        self._configure()
        current = _resp(body={"Items": []})
        with mock.patch("requests.request", side_effect=[current, _resp(status_code=500)]):
            result = jellyfin_client.mirror_create_or_replace_playlist("Chill", ["new1"], "42")
        self.assertEqual(result, {"status": "error", "reason": "failed to add items", "code": 500})


class MirrorSetPlaylistMetadataTests(_JellyfinClientTestBase):
    def test_no_request_when_not_configured(self):
        with mock.patch("requests.request") as req:
            jellyfin_client.mirror_set_playlist_metadata("42", "Chill", "Trobar mirror")
        req.assert_not_called()

    def test_gets_then_mutates_and_posts_the_whole_item_back(self):
        # #189 review analog to the Subsonic sink: `name` matters because
        # the create/replace path never carries a rename.
        self._configure()
        item = {"Id": "42", "Name": "Old Name", "Overview": "old", "SomeOtherField": "keep-me"}
        with mock.patch("requests.request", side_effect=[
            _resp(body=item), _resp(status_code=204),
        ]) as req:
            jellyfin_client.mirror_set_playlist_metadata("42", "Chill", "Trobar mirror — 2 of 2 present")
        get_call, post_call = req.call_args_list
        self.assertEqual(get_call.args[:2], ("GET", "http://jf.local/Users/u1/Items/42"))
        self.assertEqual(post_call.args[:2], ("POST", "http://jf.local/Items/42"))
        posted = post_call.kwargs["json"]
        self.assertEqual(posted["Name"], "Chill")
        self.assertEqual(posted["Overview"], "Trobar mirror — 2 of 2 present")
        self.assertEqual(posted["SomeOtherField"], "keep-me")

    def test_no_post_when_the_get_fails(self):
        self._configure()
        with mock.patch("requests.request", return_value=_resp(status_code=500)) as req:
            jellyfin_client.mirror_set_playlist_metadata("42", "Chill", "x")  # must not raise
        self.assertEqual(len(req.call_args_list), 1)


class MirrorDeletePlaylistTests(_JellyfinClientTestBase):
    def test_false_when_not_configured(self):
        self.assertFalse(jellyfin_client.mirror_delete_playlist("42"))

    def test_true_on_a_confirmed_delete(self):
        self._configure()
        with mock.patch("requests.request", return_value=_resp(status_code=204)) as req:
            self.assertTrue(jellyfin_client.mirror_delete_playlist("42"))
        self.assertEqual(req.call_args.args[:2], ("DELETE", "http://jf.local/Items/42"))

    def test_false_on_a_failed_delete(self):
        self._configure()
        with mock.patch("requests.request", return_value=_resp(status_code=500)):
            self.assertFalse(jellyfin_client.mirror_delete_playlist("42"))


if __name__ == "__main__":
    unittest.main()
