#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for subsonic_client.py — mocks requests.get, no network access
needed.

#390: the Active* classes below cover the active-provider side (status/
reconnect/list_playlists/get_playlist_tracks/get_artist_image) — this
predated the #189 mirror-target work and had no coverage until now. Unlike
jellyfin_client.py/emby_client.py, there's no requests.get()/requests.
request() split to worry about here — every call in this module, active
and mirror alike, is a GET with query-string auth params (Subsonic's own
protocol shape), so one mock target covers everything.

The Mirror* classes cover the #189 mirror-TARGET additions (the mirror_*()
functions and the _request_as() split at the bottom of the module).

    python3 -m unittest test_subsonic_client -v      # from app/
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-subsonic-client-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import subsonic_client  # noqa: E402


def _resp(status_code=200, subsonic_body=None):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = {"subsonic-response": subsonic_body if subsonic_body is not None else {}}
    r.raise_for_status.return_value = None
    return r


def _song(artist: str, album: str, title: str, song_id, track: int | None = None) -> dict:
    song = {"artist": artist, "album": album, "title": title, "id": song_id}
    if track is not None:
        song["track"] = track
    return song


class _SubsonicClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _configure(self, url="http://nav.local", username="trobar", password="secret"):
        conn = db.get_conn()
        db.set_config(conn, "mirror_subsonic_url", url)
        db.set_config(conn, "mirror_subsonic_username", username)
        db.set_config(conn, "mirror_subsonic_password", password)
        conn.commit()
        conn.close()


class _ActiveSubsonicClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _set_config(self, url="", username="", password=""):
        conn = db.get_conn()
        db.set_config(conn, "subsonic_url", url)
        db.set_config(conn, "subsonic_username", username)
        db.set_config(conn, "subsonic_password", password)
        conn.commit()
        conn.close()


class ActiveStatusTests(_ActiveSubsonicClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(subsonic_client.status()["state"], "disconnected")

    def test_paired_when_ping_succeeds(self):
        self._set_config(url="http://nav.local", username="trobar", password="secret")
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            self.assertEqual(subsonic_client.status()["state"], "paired")

    def test_disconnected_when_ping_fails(self):
        self._set_config(url="http://nav.local", username="trobar", password="secret")
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "failed"})):
            self.assertEqual(subsonic_client.status()["state"], "disconnected")

    def test_status_reports_the_subsonic_provider_id(self):
        self.assertEqual(subsonic_client.status()["provider"], "subsonic")


class ActiveReconnectTests(_ActiveSubsonicClientTestBase):
    def test_persists_config_and_reports_status(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            result = subsonic_client.reconnect("http://nav.local", "trobar", "secret")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "subsonic_url"), "http://nav.local")
        self.assertEqual(db.get_config(conn, "subsonic_username"), "trobar")
        self.assertEqual(db.get_config(conn, "subsonic_password"), "secret")
        conn.close()
        self.assertEqual(result["state"], "paired")

    def test_never_touches_the_mirror_target_config(self):
        # The flip side of MirrorReconnectTests' own "never touches the
        # active-provider config" test below — same independence, checked
        # from the other direction.
        conn = db.get_conn()
        db.set_config(conn, "mirror_subsonic_url", "http://mirror.local")
        conn.commit()
        conn.close()
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            subsonic_client.reconnect("http://active.local", "trobar", "secret")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "mirror_subsonic_url"), "http://mirror.local")
        conn.close()


class TestConnectionTests(_ActiveSubsonicClientTestBase):
    """#509 item 3: test_connection() — same ping as status(), against
    EXPLICIT credentials, but the property that actually matters is what
    it does NOT do: touch db.py at all. The admin config form's live
    pre-save check calls this on every field blur; if it persisted
    anything, that would silently save half-typed config on every blur
    across the whole page (see PUT /api/admin/config's own single-bulk-
    object shape) -- exactly the bug this function exists to avoid."""

    def test_paired_against_explicit_credentials_with_nothing_stored(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            result = subsonic_client.test_connection("http://nav.local", "trobar", "secret")
        self.assertEqual(result["state"], "paired")

    def test_disconnected_when_the_ping_fails(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "failed"})):
            result = subsonic_client.test_connection("http://nav.local", "trobar", "wrong")
        self.assertEqual(result["state"], "disconnected")

    def test_never_persists_anything(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            subsonic_client.test_connection("http://nav.local", "trobar", "secret")
        conn = db.get_conn()
        self.assertIsNone(db.get_config(conn, "subsonic_url"))
        self.assertIsNone(db.get_config(conn, "subsonic_username"))
        self.assertIsNone(db.get_config(conn, "subsonic_password"))
        conn.close()

    def test_does_not_overwrite_an_existing_stored_connection(self):
        # The scenario #509 item 3 exists for: an admin editing already-
        # working credentials mid-typing. A test_connection() call for a
        # DIFFERENT (not-yet-saved) URL must not clobber what's live.
        self._set_config(url="http://real.example.com", username="realuser", password="realpass")
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            subsonic_client.test_connection("http://typing-this.example.com", "x", "y")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "subsonic_url"), "http://real.example.com")
        conn.close()


class ActiveListPlaylistsTests(_ActiveSubsonicClientTestBase):
    def setUp(self):
        super().setUp()
        self._set_config(url="http://nav.local", username="trobar", password="secret")

    def test_not_paired_when_request_fails(self):
        # A genuine request failure (never got a response at all) -- not
        # to be confused with a well-formed non-ok response, which is a
        # different branch (see test_server_error_surfaces_the_message).
        with mock.patch("requests.get", side_effect=requests.RequestException("boom")):
            result = subsonic_client.list_playlists()
        self.assertEqual(result, {"status": "error", "reason": "not_paired"})

    def test_maps_playlists_to_common_shape(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={
            "status": "ok", "playlists": {"playlist": [
                {"id": 1, "name": "Road Trip"}, {"id": 2, "name": "Chill"},
            ]},
        })):
            result = subsonic_client.list_playlists()
        self.assertEqual(result, {"status": "ok", "playlists": [
            {"id": "1", "title": "Road Trip"}, {"id": "2", "title": "Chill"},
        ]})

    def test_a_single_playlist_is_not_collapsed_out_of_a_list(self):
        # #189's own _as_list() concern: some server implementations
        # return a bare object instead of a one-item array for a single
        # child element.
        with mock.patch("requests.get", return_value=_resp(subsonic_body={
            "status": "ok", "playlists": {"playlist": {"id": 1, "name": "Only One"}},
        })):
            result = subsonic_client.list_playlists()
        self.assertEqual(result["playlists"], [{"id": "1", "title": "Only One"}])

    def test_server_error_surfaces_the_message(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={
            "status": "failed", "error": {"message": "wrong username or password"},
        })):
            result = subsonic_client.list_playlists()
        self.assertEqual(result, {"status": "error", "reason": "wrong username or password"})


class ActiveGetPlaylistTracksTests(_ActiveSubsonicClientTestBase):
    def setUp(self):
        super().setUp()
        self._set_config(url="http://nav.local", username="trobar", password="secret")

    def test_fetches_by_source_playlist_id_directly(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={
            "status": "ok", "playlist": {"entry": [
                {"title": "Track One", "artist": "Artist A", "path": "a/1.flac", "album": "Album A"},
            ]},
        })) as get:
            result = subsonic_client.get_playlist_tracks("Road Trip", source_playlist_id="p1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tracks"], [{
            "position": 0, "title": "Track One", "artist": "Artist A",
            "path": "a/1.flac", "album": "Album A",
        }])
        self.assertEqual(get.call_args.kwargs["params"]["id"], "p1")

    def test_falls_back_to_title_lookup_when_no_id_given(self):
        list_resp = _resp(subsonic_body={
            "status": "ok", "playlists": {"playlist": [{"id": 1, "name": "Road Trip"}]}})
        tracks_resp = _resp(subsonic_body={"status": "ok", "playlist": {"entry": []}})
        with mock.patch("requests.get", side_effect=[list_resp, tracks_resp]):
            result = subsonic_client.get_playlist_tracks("Road Trip")
        self.assertEqual(result["status"], "ok")

    def test_title_not_found_reports_not_found(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={
            "status": "ok", "playlists": {"playlist": []},
        })):
            result = subsonic_client.get_playlist_tracks("Missing")
        self.assertEqual(result, {"status": "not_found", "failed_segment": "Missing"})

    def test_getplaylist_failure_is_a_clean_error(self):
        with mock.patch("requests.get", return_value=_resp(status_code=500)):
            result = subsonic_client.get_playlist_tracks("X", source_playlist_id="p1")
        self.assertEqual(result, {"status": "error", "reason": "getPlaylist failed"})


class ActiveGetArtistImageTests(_ActiveSubsonicClientTestBase):
    def setUp(self):
        super().setUp()
        subsonic_client._artist_image_key_map = None
        self._set_config(url="http://nav.local", username="trobar", password="secret")

    def test_returns_none_when_artist_unknown(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={
            "status": "ok", "artists": {"index": []},
        })):
            self.assertIsNone(subsonic_client.get_artist_image("Nobody"))

    def test_returns_bytes_and_content_type_for_a_known_artist(self):
        artists_resp = _resp(subsonic_body={"status": "ok", "artists": {"index": [
            {"artist": [{"name": "Artist A", "coverArt": "cov1"}]},
        ]}})
        image_resp = mock.Mock(content=b"\x89PNG", headers={"Content-Type": "image/png"})
        image_resp.raise_for_status.return_value = None
        with mock.patch("requests.get", side_effect=[artists_resp, image_resp]) as get:
            result = subsonic_client.get_artist_image("Artist A")
        self.assertEqual(result, (b"\x89PNG", "image/png"))
        self.assertEqual(get.call_args.kwargs["params"]["id"], "cov1")


class MirrorStatusTests(_SubsonicClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(
            subsonic_client.mirror_status(),
            {"state": "disconnected", "url": "", "provider": "subsonic"},
        )

    def test_paired_when_ping_succeeds(self):
        self._configure(url="http://nav.local")
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            self.assertEqual(
                subsonic_client.mirror_status(),
                {"state": "paired", "url": "http://nav.local", "provider": "subsonic"},
            )

    def test_disconnected_when_ping_fails(self):
        self._configure(url="http://nav.local")
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "failed"})):
            self.assertEqual(
                subsonic_client.mirror_status(),
                {"state": "disconnected", "url": "http://nav.local", "provider": "subsonic"},
            )

    def test_ping_hits_the_mirror_target_endpoint(self):
        self._configure(url="http://nav.local")
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})) as get:
            subsonic_client.mirror_status()
        self.assertEqual(get.call_args.args[0], "http://nav.local/rest/ping.view")


class MirrorReconnectTests(_SubsonicClientTestBase):
    def test_persists_config_and_reports_status(self):
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            result = subsonic_client.mirror_reconnect("http://nav.local", "trobar", "secret")
        self.assertEqual(db.get_mirror_subsonic_config(), ("http://nav.local", "trobar", "secret"))
        self.assertEqual(result["state"], "paired")

    def test_never_touches_the_active_provider_config(self):
        # #189's whole point: this is a distinct connection from
        # subsonic_url/username/password.
        conn = db.get_conn()
        db.set_config(conn, "subsonic_url", "http://active.local")
        conn.commit()
        conn.close()
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            subsonic_client.mirror_reconnect("http://mirror.local", "trobar", "secret")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "subsonic_url"), "http://active.local")
        conn.close()


class MirrorBuildTagIndexTests(_SubsonicClientTestBase):
    """#189 review: the index used to be keyed on the song object's own
    `path` field — found live against Navidrome to be a lossy tag-derived
    synthesis (e.g. drops a "(2001)" year suffix a real filesystem path
    would keep), not the real file path. Keyed on normalized tags instead,
    which is exactly the data the target derived that path FROM."""

    def test_none_when_not_configured(self):
        self.assertIsNone(subsonic_client.mirror_build_tag_index())

    def test_builds_the_whole_index_keyed_on_normalized_tags(self):
        self._configure()
        songs = [_song("Artist", "Album", "Song One", "s1"), _song("Artist", "Album", "Song Two", "s2")]
        with mock.patch("requests.get", side_effect=[
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": songs}}),
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": []}}),
        ]):
            index = subsonic_client.mirror_build_tag_index()
        self.assertEqual(index, {
            ("artist", "album", "song one"): [{"id": "s1", "track_no": None}],
            ("artist", "album", "song two"): [{"id": "s2", "track_no": None}],
        })

    def test_keys_are_normalized_the_same_way_matching_normalize_is(self):
        # Casefold + whitespace collapse — same normalization the read-side
        # Roon matcher already uses, not a second ad hoc scheme.
        self._configure()
        songs = [_song("  ARTIST  ", "Album", "SONG", "s1")]
        with mock.patch("requests.get", side_effect=[
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": songs}}),
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": []}}),
        ]):
            index = subsonic_client.mirror_build_tag_index()
        assert index is not None
        self.assertIn(("artist", "album", "song"), index)

    def test_a_repeated_tag_key_collects_every_candidate(self):
        # The target library holds the same album twice (e.g. a FLAC and
        # an MP3 copy) -- both song ids must survive, not last-write-wins.
        self._configure()
        songs = [
            _song("Artist", "Album", "Song", "s1", track=1),
            _song("Artist", "Album", "Song", "s2", track=1),
        ]
        with mock.patch("requests.get", side_effect=[
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": songs}}),
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": []}}),
        ]):
            index = subsonic_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual(
            {c["id"] for c in index[("artist", "album", "song")]}, {"s1", "s2"})

    def test_pagination_continues_past_a_short_non_empty_page(self):
        # A short page is NOT the end of the walk -- only a genuinely empty
        # one is. A server that clamps songCount below what was requested
        # would otherwise look identical to having reached the end, and the
        # index would be silently truncated.
        self._configure()
        page1 = _resp(subsonic_body={"status": "ok", "searchResult3": {
            "song": [_song("A", "Al", "One", 1)]}})  # short: 1 < page size 2
        page2 = _resp(subsonic_body={"status": "ok", "searchResult3": {
            "song": [_song("B", "Al", "Two", 2)]}})
        page3 = _resp(subsonic_body={"status": "ok", "searchResult3": {"song": []}})
        with mock.patch.object(subsonic_client, "_MIRROR_PAGE_SIZE", 2):
            with mock.patch("requests.get", side_effect=[page1, page2, page3]) as get:
                index = subsonic_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual({("a", "al", "one"), ("b", "al", "two")}, set(index.keys()))
        offsets = [call.kwargs["params"]["songOffset"] for call in get.call_args_list]
        self.assertEqual(offsets, [0, 2, 4])

    def test_page_cap_backstop_gives_up_if_a_page_is_never_empty(self):
        # The mirror-image failure mode: a server that ignores songOffset
        # and keeps handing back a full page must not loop forever inside
        # the sync worker.
        self._configure()
        full_page = _resp(subsonic_body={"status": "ok", "searchResult3": {
            "song": [_song("A", "Al", "One", 1), _song("B", "Al", "Two", 2)]}})
        with mock.patch.object(subsonic_client, "_MIRROR_PAGE_SIZE", 2), \
             mock.patch.object(subsonic_client, "_MIRROR_MAX_PAGES", 3):
            with mock.patch("requests.get", return_value=full_page):
                self.assertIsNone(subsonic_client.mirror_build_tag_index())

    def test_a_navidrome_style_synthesized_path_divergence_does_not_affect_matching(self):
        # Regression for the bug the tag-based index replaced: Navidrome's
        # own `path` field synthesizes "Artist/Album/NN - Title.ext" from
        # tags, dropping a "(2001)" year suffix a real on-disk folder name
        # would carry (dev/gen/generate.py names album folders
        # "{title} ({year})", so a scanned local relative_path would keep
        # it). The index must never read `path` at all — confirmed here by
        # giving the song object a divergent path and checking it's simply
        # ignored in favor of the artist/album/title fields alongside it.
        self._configure()
        song = _song("Test Artist", "Blue Album", "Song 1", "s1")
        song["path"] = "Test Artist/Blue Album/01 - Song 1.flac"  # no "(2001)"
        with mock.patch("requests.get", side_effect=[
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": [song]}}),
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": []}}),
        ]):
            index = subsonic_client.mirror_build_tag_index()
        self.assertEqual(
            index, {("test artist", "blue album", "song 1"): [{"id": "s1", "track_no": None}]})

    def test_a_failing_page_mid_walk_fails_the_whole_index(self):
        self._configure()
        page1 = _resp(subsonic_body={"status": "ok", "searchResult3": {
            "song": [_song("A", "Al", "One", 1), _song("B", "Al", "Two", 2)]}})
        with mock.patch.object(subsonic_client, "_MIRROR_PAGE_SIZE", 2):
            with mock.patch("requests.get", side_effect=[page1, _resp(status_code=500)]):
                self.assertIsNone(subsonic_client.mirror_build_tag_index())

    def test_songs_missing_id_are_skipped(self):
        self._configure()
        songs = [_song("Artist", "Album", "Good", "s1"), {"artist": "A", "album": "B", "title": "C"}]
        with mock.patch("requests.get", side_effect=[
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": songs}}),
            _resp(subsonic_body={"status": "ok", "searchResult3": {"song": []}}),
        ]):
            index = subsonic_client.mirror_build_tag_index()
        assert index is not None
        self.assertEqual(list(index.keys()), [("artist", "album", "good")])


class MirrorCreateOrReplacePlaylistTests(_SubsonicClientTestBase):
    def test_error_when_not_configured(self):
        self.assertEqual(
            subsonic_client.mirror_create_or_replace_playlist("Chill", ["1", "2"], None),
            {"status": "error", "reason": "not_configured", "code": None},
        )

    def test_create_omits_playlist_id_and_returns_the_new_remote_id(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(
            subsonic_body={"status": "ok", "playlist": {"id": 42}},
        )) as get:
            result = subsonic_client.mirror_create_or_replace_playlist("Chill", ["1", "2"], None)
        self.assertEqual(result, {"status": "ok", "remote_id": "42"})
        params = get.call_args.kwargs["params"]
        self.assertNotIn("playlistId", params)
        self.assertEqual(params["name"], "Chill")
        self.assertEqual(params["songId"], ["1", "2"])

    def test_replace_passes_the_existing_remote_id(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(
            subsonic_body={"status": "ok", "playlist": {"id": "42"}},
        )) as get:
            subsonic_client.mirror_create_or_replace_playlist("Chill", ["1"], "42")
        self.assertEqual(get.call_args.kwargs["params"]["playlistId"], "42")

    def test_failure_surfaces_the_server_error_message(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(
            subsonic_body={"status": "failed", "error": {"message": "boom"}},
        )):
            result = subsonic_client.mirror_create_or_replace_playlist("Chill", [], None)
        self.assertEqual(result, {"status": "error", "reason": "boom", "code": None})

    def test_failure_surfaces_the_servers_numeric_error_code(self):
        # mirror_subsonic.write_mirror() reacts to a specific code (a stale
        # remote id -- "data not found") without string-matching `reason`.
        self._configure()
        with mock.patch("requests.get", return_value=_resp(
            subsonic_body={"status": "failed", "error": {"code": 70, "message": "data not found"}},
        )):
            result = subsonic_client.mirror_create_or_replace_playlist("Chill", [], "stale-id")
        self.assertEqual(result, {"status": "error", "reason": "data not found", "code": 70})

    def test_failure_without_a_server_message_falls_back(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(status_code=500)):
            result = subsonic_client.mirror_create_or_replace_playlist("Chill", [], None)
        self.assertEqual(result, {"status": "error", "reason": "request failed", "code": None})

    def test_ok_status_missing_playlist_key_is_still_an_error(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            result = subsonic_client.mirror_create_or_replace_playlist("Chill", [], None)
        self.assertEqual(result["status"], "error")


class MirrorSetPlaylistMetadataTests(_SubsonicClientTestBase):
    def test_no_request_when_not_configured(self):
        with mock.patch("requests.get") as get:
            subsonic_client.mirror_set_playlist_metadata("42", "Chill", "Trobar mirror")
        get.assert_not_called()

    def test_sends_playlist_id_name_and_comment(self):
        # #189 review: `name` matters because createPlaylist's own
        # replace path ignores it -- this call is what actually
        # propagates a Trobar-side rename to the target.
        self._configure()
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})) as get:
            subsonic_client.mirror_set_playlist_metadata("42", "Chill", "Trobar mirror — 2 of 2 present")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["playlistId"], "42")
        self.assertEqual(params["name"], "Chill")
        self.assertEqual(params["comment"], "Trobar mirror — 2 of 2 present")
        self.assertNotIn("songId", params)

    def test_a_failed_request_does_not_raise(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(status_code=500)):
            subsonic_client.mirror_set_playlist_metadata("42", "Chill", "x")  # must not raise


class MirrorDeletePlaylistTests(_SubsonicClientTestBase):
    def test_false_when_not_configured(self):
        self.assertFalse(subsonic_client.mirror_delete_playlist("42"))

    def test_true_on_a_confirmed_ok_delete(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "ok"})):
            self.assertTrue(subsonic_client.mirror_delete_playlist("42"))

    def test_false_when_the_server_reports_an_error(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(subsonic_body={"status": "failed"})):
            self.assertFalse(subsonic_client.mirror_delete_playlist("42"))

    def test_false_on_a_request_failure(self):
        self._configure()
        with mock.patch("requests.get", return_value=_resp(status_code=500)):
            self.assertFalse(subsonic_client.mirror_delete_playlist("42"))


if __name__ == "__main__":
    unittest.main()
