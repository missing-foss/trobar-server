#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for music_assistant_client.py — mocks requests, no network access
needed. The payloads are trimmed from responses recorded against a real
Music Assistant 2.9.9 (the stream URL's host rewritten), keeping every field
the client reads plus enough of the rest to show the real shape. Config
persistence goes through a real temp-file SQLite DB, as in test_lms_client.

    python3 -m unittest test_music_assistant_client -v
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-ma-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import music_assistant_client as ma  # noqa: E402

URL = "http://ma.example.invalid:8095"
TOKEN = "long-lived-token"


def _builtin(item_id):
    return [{"item_id": item_id, "provider_domain": "builtin", "provider_instance": "builtin", "available": True}]


# music/playlists/library_items, as recorded: the generated playlists Music
# Assistant always lists, plus one the user made.
PLAYLISTS = [
    {"item_id": "4", "provider": "library", "name": "500 Random tracks (from library)",
     "uri": "library://playlist/4", "media_type": "playlist", "owner": "Music Assistant",
     "is_editable": False, "provider_mappings": _builtin("random_tracks")},
    {"item_id": "1", "provider": "library", "name": "All favorited tracks",
     "uri": "library://playlist/1", "media_type": "playlist", "owner": "Music Assistant",
     "is_editable": False, "provider_mappings": _builtin("all_favorite_tracks")},
    {"item_id": "7", "provider": "library", "name": "Infinite Mix (library)",
     "uri": "library://playlist/7", "media_type": "playlist", "owner": "Music Assistant",
     "is_editable": False, "provider_mappings": _builtin("infinite_mix")},
    {"item_id": "9", "provider": "library", "name": "Probe mix",
     "uri": "library://playlist/9", "media_type": "playlist", "owner": "Music Assistant",
     "is_editable": True, "provider_mappings": _builtin("Probe mix")},
]


def _track(position, name, artists, album, mapping, media_type="track"):
    return {
        "item_id": str(position), "provider": "library", "name": name, "media_type": media_type,
        "position": position, "duration": 4,
        "artists": [{"item_id": a, "provider": "library", "name": a, "media_type": "artist"} for a in artists],
        "album": {"item_id": "2", "provider": "library", "name": album, "media_type": "album"} if album else None,
        "provider_mappings": [mapping],
    }


LOCAL = {"item_id": "Aphelion/Parallax (2022)/01 - Event Horizon.flac",
         "provider_domain": "filesystem_local", "provider_instance": "filesystem_local--8N8LjbEH",
         "available": True, "in_library": True}
STREAM = {"item_id": "http://stream.example.invalid/Red%20Shift.flac",
          "provider_domain": "builtin", "provider_instance": "builtin", "available": True}

# music/playlists/playlist_tracks, as recorded: a local file, a track added
# by URL, and (not recordable in a playlist on 2.9.9, but allowed by the
# declared return type) a radio station.
PLAYLIST_TRACKS = [
    _track(1, "Event Horizon", ["Aphelion"], "Parallax", LOCAL),
    _track(2, "Red Shift", ["Aphelion", "Guest"], None, STREAM),
    _track(3, "Night Radio", [], None,
           {"item_id": "radio-1", "provider_domain": "radiobrowser", "provider_instance": "radiobrowser"},
           media_type="radio"),
]


def _resp(status_code=200, json_body=None):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_body
    if status_code >= 400:
        err = requests.HTTPError(f"{status_code}")
        err.response = r
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    return r


class _Base(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _configure(self, url=URL, token=TOKEN):
        conn = db.get_conn()
        db.set_config(conn, "music_assistant_url", url)
        db.set_config(conn, "music_assistant_token", token)
        conn.commit()
        conn.close()


class RequestShapeTests(_Base):
    def test_posts_the_command_to_api_with_a_bearer_token(self):
        self._configure(url=URL + "/")
        with mock.patch("requests.post", return_value=_resp(json_body=9)) as post:
            ma.status()
        args, kwargs = post.call_args
        self.assertEqual(args[0], URL + "/api")
        self.assertEqual(kwargs["json"], {"command": "music/playlists/count", "args": {}})
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer " + TOKEN})

    def test_no_request_without_a_token(self):
        self._configure(token="")
        with mock.patch("requests.post") as post:
            self.assertEqual(ma.status()["state"], "disconnected")
        post.assert_not_called()


class StatusTests(_Base):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(ma.status(), {"state": "disconnected", "url": "", "provider": "music_assistant"})

    def test_paired_when_the_count_comes_back(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=9)):
            self.assertEqual(ma.status()["state"], "paired")

    def test_disconnected_on_401(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(status_code=401)):
            self.assertEqual(ma.status()["state"], "disconnected")

    def test_disconnected_on_network_error(self):
        self._configure()
        with mock.patch("requests.post", side_effect=requests.ConnectionError()):
            self.assertEqual(ma.status()["state"], "disconnected")

    def test_test_connection_uses_the_explicit_settings_and_stores_nothing(self):
        with mock.patch("requests.post", return_value=_resp(json_body=0)) as post:
            self.assertEqual(ma.test_connection(URL, "other")["state"], "paired")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer other"})
        self.assertEqual(ma.status()["state"], "disconnected")

    def test_reconnect_persists_and_blank_values_disconnect(self):
        with mock.patch("requests.post", return_value=_resp(json_body=0)):
            self.assertEqual(ma.reconnect(URL, TOKEN)["state"], "paired")
        with mock.patch("requests.post") as post:
            self.assertEqual(ma.reconnect("", "")["state"], "disconnected")
        post.assert_not_called()


class ListPlaylistsTests(_Base):
    def test_skips_the_generated_playlists(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=PLAYLISTS)):
            self.assertEqual(ma.list_playlists(),
                             {"status": "ok", "playlists": [{"id": "9", "title": "Probe mix"}]})

    def test_keeps_a_read_only_playlist_from_another_provider(self):
        synced = dict(PLAYLISTS[3], item_id="12", name="Discover Weekly", is_editable=False,
                      provider_mappings=[{"item_id": "37i9", "provider_domain": "spotify"}])
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=[synced])):
            self.assertEqual(ma.list_playlists()["playlists"], [{"id": "12", "title": "Discover Weekly"}])

    def test_pages_until_a_short_page(self):
        self._configure()
        pages = [PLAYLISTS[:2], PLAYLISTS[2:]]
        with mock.patch.object(ma, "_PAGE", 2), \
                mock.patch("requests.post", side_effect=[_resp(json_body=pages[0]), _resp(json_body=pages[1]),
                                                        _resp(json_body=[])]) as post:
            result = ma.list_playlists()
        self.assertEqual(result["playlists"], [{"id": "9", "title": "Probe mix"}])
        offsets = [c.kwargs["json"]["args"]["offset"] for c in post.call_args_list]
        self.assertEqual(offsets, [0, 2, 4])

    def test_error_when_unconfigured_or_unreachable(self):
        self.assertEqual(ma.list_playlists()["status"], "error")
        self._configure()
        with mock.patch("requests.post", return_value=_resp(status_code=500)):
            self.assertEqual(ma.list_playlists()["status"], "error")

    def test_a_failing_later_page_is_an_error_not_a_short_list(self):
        self._configure()
        with mock.patch.object(ma, "_PAGE", 2), \
                mock.patch("requests.post", side_effect=[_resp(json_body=PLAYLISTS[:2]),
                                                        requests.ConnectionError()]):
            self.assertEqual(ma.list_playlists()["status"], "error")


class GetPlaylistTracksTests(_Base):
    def test_tracks_in_order_with_paths_for_local_files_only(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=PLAYLIST_TRACKS)) as post:
            result = ma.get_playlist_tracks("Probe mix", "9")
        self.assertEqual(post.call_args.kwargs["json"], {
            "command": "music/playlists/playlist_tracks",
            "args": {"item_id": "9", "provider_instance_id_or_domain": "library"},
        })
        self.assertEqual(result, {"status": "ok", "playlist": "Probe mix", "tracks": [
            {"position": 0, "title": "Event Horizon", "artist": "Aphelion",
             "path": "Aphelion/Parallax (2022)/01 - Event Horizon.flac", "album": "Parallax"},
            {"position": 1, "title": "Red Shift", "artist": "Aphelion, Guest", "path": None, "album": None},
        ]})

    def test_the_network_share_provider_counts_as_a_file(self):
        smb = dict(LOCAL, provider_domain="filesystem_smb")
        self._configure()
        with mock.patch("requests.post",
                        return_value=_resp(json_body=[_track(1, "Event Horizon", ["Aphelion"], "Parallax", smb)])):
            track = ma.get_playlist_tracks("x", "9")["tracks"][0]
        self.assertEqual(track["path"], LOCAL["item_id"])

    def test_looks_the_id_up_by_title_when_none_is_given(self):
        self._configure()
        with mock.patch("requests.post", side_effect=[_resp(json_body=PLAYLISTS),
                                                      _resp(json_body=PLAYLIST_TRACKS)]) as post:
            self.assertEqual(ma.get_playlist_tracks("Probe mix")["status"], "ok")
        self.assertEqual(post.call_args.kwargs["json"]["args"]["item_id"], "9")

    def test_unknown_title_is_not_found(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=PLAYLISTS)):
            self.assertEqual(ma.get_playlist_tracks("Nope")["status"], "not_found")

    def test_error_when_the_fetch_fails(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(status_code=401)):
            self.assertEqual(ma.get_playlist_tracks("Probe mix", "9")["status"], "error")


# music/artists/library_items with `search`, as recorded, plus an image entry
# in the shape album images carry (artists in the recorded library had none).
ARTISTS = [
    {"item_id": "3", "provider": "library", "name": "Aphelion", "media_type": "artist",
     "metadata": {"images": [{"type": "thumb", "path": "Aphelion/artist.jpg",
                              "provider": "filesystem_local--8N8LjbEH", "remotely_accessible": False,
                              "proxy_id": "9067d2fe"}]}},
    {"item_id": "8", "provider": "library", "name": "Aphelion Tribute", "media_type": "artist",
     "metadata": {"images": None}},
]


def _image(status_code=200, content_type="image/png", content=b"png-bytes"):
    r = mock.Mock()
    r.status_code = status_code
    r.ok = status_code < 400
    r.headers = {"Content-Type": content_type}
    r.content = content
    return r


class ArtistImageTests(_Base):
    def test_the_named_artists_image_comes_from_the_image_proxy(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=ARTISTS)) as post, \
             mock.patch("requests.get", return_value=_image()) as get:
            self.assertEqual(ma.get_artist_image("aphelion"), (b"png-bytes", "image/png"))
        self.assertEqual(post.call_args.kwargs["json"],
                         {"command": "music/artists/library_items", "args": {"search": "aphelion", "limit": 10}})
        self.assertEqual(get.call_args.args[0], URL + "/imageproxy/9067d2fe")

    def test_no_image_for_an_artist_without_one(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=ARTISTS)), \
             mock.patch("requests.get") as get:
            self.assertIsNone(ma.get_artist_image("Aphelion Tribute"))
        get.assert_not_called()

    def test_no_image_for_a_name_that_only_partly_matches(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=ARTISTS[1:])), \
             mock.patch("requests.get") as get:
            self.assertIsNone(ma.get_artist_image("Aphelion"))
        get.assert_not_called()

    def test_a_non_image_answer_is_no_image(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=ARTISTS)), \
             mock.patch("requests.get", return_value=_image(content_type="text/html")):
            self.assertIsNone(ma.get_artist_image("Aphelion"))

    def test_unconfigured_is_no_image(self):
        with mock.patch("requests.post") as post:
            self.assertIsNone(ma.get_artist_image("Aphelion"))
        post.assert_not_called()


class ActiveProviderInterfaceTests(_Base):
    def test_retry_pairing_is_a_fresh_check(self):
        self._configure()
        with mock.patch("requests.post", return_value=_resp(json_body=3)):
            self.assertEqual(ma.retry_pairing()["state"], "paired")


if __name__ == "__main__":
    unittest.main()
