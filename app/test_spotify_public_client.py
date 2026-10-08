#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for spotify_public_client.py — URL parsing, the page mapping, and
every failure path.

Nothing here touches the network. `requests.get` is replaced with pages
built in the shape the embed page was measured to have, so the real
fetch_playlist() body runs: the parse is the part that breaks when the
page changes, and stubbing fetch_playlist() would test none of it.

    python3 -m unittest test_spotify_public_client -v
"""
import json
import unittest
from unittest import mock

import requests

import spotify_public_client

PID = "0aBcDeFgHiJkLmNoPqRsTu"  # 22 base62 characters, invented


def _page(page_props) -> str:
    data = {"props": {"pageProps": page_props}, "page": "/embed/playlist/[id]"}
    return ("<!DOCTYPE html><html><head></head><body><div id=\"__next\"></div>"
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
            "</body></html>")


def _playlist_page(name, tracks) -> str:
    """tracks: (title, artist line) pairs."""
    track_list = [{"uri": f"spotify:track:{i:022d}", "title": title, "subtitle": artist,
                   "duration": 200000, "isPlayable": True, "entityType": "track"}
                  for i, (title, artist) in enumerate(tracks)]
    return _page({"state": {"data": {"entity": {
        "type": "playlist", "name": name, "id": PID, "subtitle": "Someone",
        "trackList": track_list}}}})


def _resp(text="", status=200):
    r = mock.Mock()
    r.status_code = status
    r.text = text
    return r


class ParsePlaylistUrlTests(unittest.TestCase):
    def test_share_link(self):
        self.assertEqual(spotify_public_client.parse_playlist_url(
            f"https://open.spotify.com/playlist/{PID}?si=abc123"), PID)

    def test_locale_segment(self):
        self.assertEqual(spotify_public_client.parse_playlist_url(
            f"https://open.spotify.com/intl-fr/playlist/{PID}"), PID)

    def test_embed_link_and_no_scheme(self):
        for url in (f"https://open.spotify.com/embed/playlist/{PID}?utm_source=generator",
                    f"open.spotify.com/playlist/{PID}"):
            with self.subTest(url=url):
                self.assertEqual(spotify_public_client.parse_playlist_url(url), PID)

    def test_uri(self):
        self.assertEqual(spotify_public_client.parse_playlist_url(f"spotify:playlist:{PID}"), PID)

    def test_a_bare_id_is_left_to_youtube_music(self):
        """The subscription route tries Spotify first. Taking a bare id
        here would send every bare YouTube id to Spotify."""
        self.assertIsNone(spotify_public_client.parse_playlist_url(PID))

    def test_rejects_other_hosts_and_short_links(self):
        for url in (f"https://open.spotify.com.evil.test/playlist/{PID}",
                    "https://spotify.link/AbCdEf",
                    f"https://example.test/playlist/{PID}"):
            with self.subTest(url=url):
                self.assertIsNone(spotify_public_client.parse_playlist_url(url))

    def test_rejects_other_kinds_of_link(self):
        for url in (f"https://open.spotify.com/album/{PID}",
                    f"https://open.spotify.com/track/{PID}",
                    f"https://open.spotify.com/playlist/{PID}/extra",
                    f"spotify:album:{PID}"):
            with self.subTest(url=url):
                self.assertIsNone(spotify_public_client.parse_playlist_url(url))

    def test_rejects_a_malformed_id(self):
        for url in ("https://open.spotify.com/playlist/short",
                    f"https://open.spotify.com/playlist/{PID}x",
                    f"https://open.spotify.com/playlist/{PID[:-1]}-",
                    "spotify:playlist:"):
            with self.subTest(url=url):
                self.assertIsNone(spotify_public_client.parse_playlist_url(url))

    def test_empty(self):
        self.assertIsNone(spotify_public_client.parse_playlist_url(""))
        self.assertIsNone(spotify_public_client.parse_playlist_url(None))  # type: ignore[arg-type]


class FetchPlaylistTests(unittest.TestCase):
    def _fetch(self, response=None, side_effect=None):
        with mock.patch.object(spotify_public_client.requests, "get",
                               return_value=response, side_effect=side_effect) as get:
            return spotify_public_client.fetch_playlist(PID), get

    def test_a_playlist(self):
        result, get = self._fetch(_resp(_playlist_page(
            "Road Trip", [("Song One", "Artist A"), ("Song Two", "Artist B")])))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["title"], "Road Trip")
        self.assertEqual(result["tracks"], [
            {"position": 0, "title": "Song One", "artist": "Artist A", "album": None, "path": None},
            {"position": 1, "title": "Song Two", "artist": "Artist B", "album": None, "path": None},
        ])
        url = get.call_args.args[0]
        self.assertEqual(url, f"https://open.spotify.com/embed/playlist/{PID}")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertTrue(get.call_args.kwargs["timeout"])

    def test_several_artists(self):
        """The page joins credits with a comma and a no-break space; a
        plain comma inside one name stays."""
        result, _ = self._fetch(_resp(_playlist_page(
            "Mix", [("Song", "Tyler, The Creator,\u00a0Kali Uchis")])))
        self.assertEqual(result["tracks"][0]["artist"], "Tyler, The Creator, Kali Uchis")

    def test_untitled_and_malformed_entries_are_skipped(self):
        page = _page({"state": {"data": {"entity": {"name": "P", "trackList": [
            {"title": "", "subtitle": "A"}, "junk", {"title": "Kept", "subtitle": "B"}]}}}})
        result, _ = self._fetch(_resp(page))
        self.assertEqual([(t["position"], t["title"]) for t in result["tracks"]], [(0, "Kept")])

    def test_no_name_falls_back_to_the_id(self):
        page = _page({"state": {"data": {"entity": {"trackList": []}}}})
        result, _ = self._fetch(_resp(page))
        self.assertEqual(result, {"status": "ok", "title": PID, "tracks": []})

    def test_a_missing_playlist_is_unavailable(self):
        """As measured: HTTP 200, with the 404 inside the page data."""
        result, _ = self._fetch(_resp(_page({"status": 404, "title": "Page not found"})))
        self.assertEqual(result, {"status": "unavailable"})

    def test_the_pages_own_500_is_an_error(self):
        result, _ = self._fetch(_resp(_page({"status": 500, "title": "Page not available"})))
        self.assertEqual(result, {"status": "error", "reason": "unexpected_response"})

    def test_http_errors_and_redirects(self):
        for status in (301, 302, 403, 429, 500, 503):
            with self.subTest(status=status):
                result, _ = self._fetch(_resp("", status))
                self.assertEqual(result, {"status": "error", "reason": f"http_{status}"})

    def test_network_failure(self):
        for exc in (requests.ConnectionError(), requests.Timeout()):
            with self.subTest(exc=type(exc).__name__):
                result, _ = self._fetch(side_effect=exc)
                self.assertEqual(result, {"status": "error", "reason": type(exc).__name__})

    def test_a_page_that_changed_shape_is_an_error_not_an_empty_playlist(self):
        pages = {
            "no data script": "<html><body>nothing here</body></html>",
            "bad json": '<script id="__NEXT_DATA__" type="application/json">{not json</script>',
            "no props": '<script id="__NEXT_DATA__" type="application/json">{"x": 1}</script>',
            "props not a dict": _page([1, 2]),
            "no state": _page({"config": {}}),
            "no entity": _page({"state": {"data": {}}}),
            "no trackList": _page({"state": {"data": {"entity": {"name": "P"}}}}),
            "trackList not a list": _page({"state": {"data": {"entity": {"trackList": {}}}}}),
            "entity null": _page({"state": {"data": {"entity": None}}}),
        }
        for label, text in pages.items():
            with self.subTest(label):
                result, _ = self._fetch(_resp(text))
                self.assertEqual(result, {"status": "error", "reason": "unexpected_response"})


class GetPlaylistTracksTests(unittest.TestCase):
    def test_ok_carries_the_remote_title(self):
        with mock.patch.object(spotify_public_client, "fetch_playlist", return_value={
                "status": "ok", "title": "Renamed", "tracks": []}):
            result = spotify_public_client.get_playlist_tracks("Old", PID)
        self.assertEqual(result, {"status": "ok", "playlist": "Renamed", "tracks": []})

    def test_failure_passes_through(self):
        with mock.patch.object(spotify_public_client, "fetch_playlist",
                               return_value={"status": "unavailable"}):
            self.assertEqual(spotify_public_client.get_playlist_tracks("T", PID),
                             {"status": "unavailable"})

    def test_no_id(self):
        with mock.patch.object(spotify_public_client, "fetch_playlist") as fetch:
            result = spotify_public_client.get_playlist_tracks("T", None)
        self.assertEqual(result, {"status": "not_found", "failed_segment": "T"})
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
