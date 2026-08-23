#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for spotify_client.py — mocks requests, no network access needed.

    python3 -m unittest test_spotify_client -v
"""
import unittest
import unittest.mock as mock

import requests

import spotify_client


def _resp(status_code=200, json_body=None, headers=None):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.headers = headers or {}   # real dict so .get("Retry-After") -> None
    if status_code >= 400:
        err = requests.HTTPError(f"{status_code}")
        err.response = r   # real requests attaches the response; mirror it
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    return r


def _playlists_page(entries, next_url=None):   # entries: [(id, name), ...]
    return {"items": [{"id": pid, "name": name} for pid, name in entries],
            "next": next_url}


def _track_item(name, artist, album="Alb"):
    return {"track": {"type": "track", "name": name,
                      "artists": [{"name": artist}], "album": {"name": album}}}


def _tracks_page(items, next_url=None):
    return {"items": items, "next": next_url}


class RefreshAccessTokenTests(unittest.TestCase):
    def test_returns_access_and_rotated_refresh(self):
        with mock.patch("requests.post", return_value=_resp(json_body={
            "access_token": "at1", "refresh_token": "rt2"})) as post:
            access, refresh = spotify_client.refresh_access_token("cid", "csec", "rt1")
        self.assertEqual((access, refresh), ("at1", "rt2"))
        self.assertEqual(post.call_args.kwargs["auth"], ("cid", "csec"))
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "rt1")

    def test_falls_back_to_input_refresh_when_not_rotated(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"access_token": "at1"})):
            _, refresh = spotify_client.refresh_access_token("cid", "csec", "rt1")
        self.assertEqual(refresh, "rt1")

    def test_400_raises_auth_error(self):
        with mock.patch("requests.post", return_value=_resp(status_code=400)):
            with self.assertRaises(spotify_client.SpotifyAuthError):
                spotify_client.refresh_access_token("cid", "csec", "bad-token")

    def test_connection_failure_raises_transient_not_auth(self):
        with mock.patch("requests.post", side_effect=requests.ConnectionError()):
            with self.assertRaises(spotify_client.SpotifyTransientError):
                spotify_client.refresh_access_token("cid", "csec", "rt1")

    def test_server_error_raises_transient(self):
        with mock.patch("requests.post", return_value=_resp(status_code=503)):
            with self.assertRaises(spotify_client.SpotifyTransientError):
                spotify_client.refresh_access_token("cid", "csec", "rt1")

    def test_malformed_response_raises_transient(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"no_access_token": True})):
            with self.assertRaises(spotify_client.SpotifyTransientError):
                spotify_client.refresh_access_token("cid", "csec", "rt1")


class GetCurrentUserTests(unittest.TestCase):
    def test_happy_path(self):
        with mock.patch("requests.get", return_value=_resp(json_body={
            "id": "u1", "display_name": "Alice"})):
            result = spotify_client.get_current_user("at1")
        self.assertEqual(result, {"status": "ok", "user_id": "u1", "display_name": "Alice"})

    def test_null_display_name_falls_back_to_id(self):
        with mock.patch("requests.get", return_value=_resp(json_body={
            "id": "u1", "display_name": None})):
            result = spotify_client.get_current_user("at1")
        self.assertEqual(result["display_name"], "u1")

    def test_malformed_is_a_clean_error(self):
        with mock.patch("requests.get", return_value=_resp(json_body={"unexpected": "x"})):
            result = spotify_client.get_current_user("at1")
        self.assertEqual(result, {"status": "error"})

    def test_request_failure_is_a_clean_error(self):
        with mock.patch("requests.get", side_effect=requests.ConnectionError()):
            result = spotify_client.get_current_user("at1")
        self.assertEqual(result, {"status": "error"})


class ListPlaylistsTests(unittest.TestCase):
    def test_extracts_id_title_pairs(self):
        page = _playlists_page([("p1", "Road Trip"), ("p2", "Chill")])
        with mock.patch("requests.get", return_value=_resp(json_body=page)) as get:
            result = spotify_client.list_playlists("at1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["playlists"],
                         [{"id": "p1", "title": "Road Trip"}, {"id": "p2", "title": "Chill"}])
        self.assertIn("/me/playlists", get.call_args[0][0])
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer at1")

    def test_paginates_via_next(self):
        pages = [_playlists_page([("p1", "A")],
                                 next_url="https://api.spotify.com/v1/me/playlists?offset=50"),
                 _playlists_page([("p2", "B")])]

        def side_effect(url, headers=None, timeout=None):
            return _resp(json_body=pages[1] if "offset" in url else pages[0])

        with mock.patch("requests.get", side_effect=side_effect):
            result = spotify_client.list_playlists("at1")
        self.assertEqual([p["id"] for p in result["playlists"]], ["p1", "p2"])

    def test_skips_items_missing_id_or_name(self):
        page = {"items": [{"id": "p1", "name": "Keep"}, {"id": "p2"}, {"name": "NoId"}],
                "next": None}
        with mock.patch("requests.get", return_value=_resp(json_body=page)):
            result = spotify_client.list_playlists("at1")
        self.assertEqual(result["playlists"], [{"id": "p1", "title": "Keep"}])

    def test_request_failure_is_a_clean_error(self):
        with mock.patch("time.sleep"), \
                mock.patch("requests.get", side_effect=requests.ConnectionError()):
            result = spotify_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "network"})


class GetPlaylistTracksTests(unittest.TestCase):
    def test_resolves_tracks_in_order(self):
        page = _tracks_page([_track_item("First", "Artist A", "Album A"),
                             _track_item("Second", "Artist B", "Album B")])
        with mock.patch("requests.get", return_value=_resp(json_body=page)) as get:
            result = spotify_client.get_playlist_tracks(
                "PL", source_playlist_id="pl1", access_token="at1")
        self.assertEqual(result["tracks"], [
            {"position": 0, "artist": "Artist A", "title": "First", "album": "Album A"},
            {"position": 1, "artist": "Artist B", "title": "Second", "album": "Album B"}])
        self.assertIn("/playlists/pl1/items", get.call_args[0][0])  # #146: /items, not /tracks

    def test_skips_null_track_and_episode_keeping_contiguous_positions(self):
        page = _tracks_page([
            {"track": None},                                   # removed track
            {"track": {"type": "episode", "name": "Podcast"}}, # podcast episode
            _track_item("Keep", "A")])
        with mock.patch("requests.get", return_value=_resp(json_body=page)):
            result = spotify_client.get_playlist_tracks(
                "PL", source_playlist_id="pl1", access_token="at1")
        self.assertEqual([t["title"] for t in result["tracks"]], ["Keep"])
        self.assertEqual(result["tracks"][0]["position"], 0)

    def test_paginates_via_next(self):
        pages = [_tracks_page(
                     [_track_item("A", "x")],
                     next_url="https://api.spotify.com/v1/playlists/pl1/items?offset=100"),
                 _tracks_page([_track_item("B", "y")])]

        def side_effect(url, headers=None, timeout=None):
            return _resp(json_body=pages[1] if "offset" in url else pages[0])

        with mock.patch("requests.get", side_effect=side_effect):
            result = spotify_client.get_playlist_tracks(
                "PL", source_playlist_id="pl1", access_token="at1")
        self.assertEqual([t["title"] for t in result["tracks"]], ["A", "B"])
        self.assertEqual([t["position"] for t in result["tracks"]], [0, 1])

    def test_no_source_id_is_a_clean_error(self):
        with mock.patch("requests.get") as get:
            result = spotify_client.get_playlist_tracks("PL", access_token="at1")
        self.assertEqual(result, {"status": "error", "reason": "not_found"})
        get.assert_not_called()

    def test_request_failure_is_a_clean_error(self):
        with mock.patch("time.sleep"), \
                mock.patch("requests.get", side_effect=requests.ConnectionError()):
            result = spotify_client.get_playlist_tracks(
                "PL", source_playlist_id="pl1", access_token="at1")
        self.assertEqual(result, {"status": "error", "reason": "network"})


class RetryAndTruncationTests(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        page = _playlists_page([("p1", "A")])
        with mock.patch("time.sleep") as sleep, \
                mock.patch("requests.get",
                           side_effect=[_resp(status_code=429), _resp(json_body=page)]) as get:
            result = spotify_client.list_playlists("at1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()

    def test_persistent_429_gives_up_with_rate_limited_reason(self):
        with mock.patch("time.sleep"), \
                mock.patch("requests.get", return_value=_resp(status_code=429)) as get:
            result = spotify_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "rate_limited"})
        self.assertEqual(get.call_count, spotify_client._RETRY_ATTEMPTS)

    def test_honors_retry_after_header(self):
        page = _playlists_page([("p1", "A")])
        r429 = _resp(status_code=429, headers={"Retry-After": "5"})
        with mock.patch("time.sleep") as sleep, \
                mock.patch("requests.get", side_effect=[r429, _resp(json_body=page)]):
            spotify_client.list_playlists("at1")
        sleep.assert_called_once_with(5.0)

    def test_non_retryable_4xx_is_not_retried(self):
        with mock.patch("time.sleep"), \
                mock.patch("requests.get", return_value=_resp(status_code=404)) as get:
            result = spotify_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "http_error"})
        self.assertEqual(get.call_count, 1)

    def test_max_pages_truncation_logs_a_warning(self):
        page = _playlists_page([("p1", "A")],
                               next_url="https://api.spotify.com/v1/me/playlists?offset=50")
        with mock.patch.object(spotify_client, "_MAX_PAGES", 2), \
                mock.patch("requests.get", return_value=_resp(json_body=page)):
            with self.assertLogs("spotify_client", level="WARNING") as cm:
                result = spotify_client.list_playlists("at1")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any("_MAX_PAGES" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
