#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for ytmusic_client.py — URL parsing, the response mapping, and
every failure path.

Nothing here touches the network, and that is the point rather than a
convenience: this module's whole reason for existing is that it talks to an
unofficial endpoint which can refuse, change shape, or vanish, so the tests
that matter are the ones that make it do exactly that. A fake `ytmusicapi`
is installed into sys.modules for the duration of a test, which exercises
the real fetch_playlist() body — the alternative, stubbing fetch_playlist()
itself, would test nothing this module actually does.

    python3 -m unittest test_ytmusic_client -v
"""
import sys
import types
import unittest
from contextlib import contextmanager
from unittest import mock

import ytmusic_client


@contextmanager
def fake_ytmusicapi(get_playlist):
    """Installs a stand-in `ytmusicapi` whose YTMusic().get_playlist is
    `get_playlist`. patch.dict restores whatever was there before — the
    real package is installed in this venv, and leaving a fake behind would
    silently change the meaning of every later test in the run."""
    module = types.ModuleType("ytmusicapi")

    class _YTMusic:
        def get_playlist(self, playlist_id, limit=None):
            return get_playlist(playlist_id, limit)

    setattr(module, "YTMusic", _YTMusic)
    with mock.patch.dict(sys.modules, {"ytmusicapi": module}):
        yield


class ParsePlaylistUrlTests(unittest.TestCase):
    def test_share_link(self):
        self.assertEqual(
            ytmusic_client.parse_playlist_url(
                "https://music.youtube.com/playlist?list=PLabc123"),
            "PLabc123")

    def test_address_bar_while_playing(self):
        """A /watch URL with a list= parameter — what the address bar holds
        while a playlist plays, and the copy a user is most likely to make
        without thinking about it."""
        self.assertEqual(
            ytmusic_client.parse_playlist_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc123&index=2"),
            "PLabc123")

    def test_mobile_and_short_hosts(self):
        for url in ("https://m.youtube.com/playlist?list=PLabc123",
                    "https://youtu.be/watch?list=PLabc123",
                    "youtube.com/playlist?list=PLabc123"):
            with self.subTest(url=url):
                self.assertEqual(ytmusic_client.parse_playlist_url(url), "PLabc123")

    def test_bare_id(self):
        self.assertEqual(ytmusic_client.parse_playlist_url("  OLAK5uy_abc  "), "OLAK5uy_abc")

    def test_vl_prefix_is_stripped(self):
        self.assertEqual(ytmusic_client.parse_playlist_url("VLPLabc123"), "PLabc123")

    def test_rejects_another_host(self):
        """Fails closed. The id would be fetched from YouTube whatever host
        was pasted, so accepting a look-alike would fetch something other
        than what the user named."""
        self.assertIsNone(ytmusic_client.parse_playlist_url(
            "https://music.youtube.com.evil.test/playlist?list=PLabc123"))

    def test_rejects_urls_without_a_list_parameter(self):
        self.assertIsNone(ytmusic_client.parse_playlist_url(
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_rejects_empty_and_separator_bearing_values(self):
        for raw in ("", "   ", "https://music.youtube.com/playlist?list=", "VL"):
            with self.subTest(raw=raw):
                self.assertIsNone(ytmusic_client.parse_playlist_url(raw))


class FetchPlaylistTests(unittest.TestCase):
    def test_maps_a_playlist_to_the_shape_the_matcher_takes(self):
        payload = {
            "title": "Best Rock songs of all time!",
            "tracks": [
                {"title": "Livin' On A Prayer",
                 "artists": [{"name": "Bon Jovi"}],
                 "album": {"name": "Slippery When Wet"}},
                {"title": "Die With A Smile",
                 "artists": [{"name": "Lady Gaga"}, {"name": "Bruno Mars"}],
                 "album": None},
            ],
        }
        with fake_ytmusicapi(lambda pid, limit: payload):
            result = ytmusic_client.fetch_playlist("PLabc123")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["title"], "Best Rock songs of all time!")
        self.assertEqual(result["tracks"], [
            {"position": 0, "title": "Livin' On A Prayer", "artist": "Bon Jovi",
             "album": "Slippery When Wet", "path": None},
            {"position": 1, "title": "Die With A Smile", "artist": "Lady Gaga, Bruno Mars",
             "album": None, "path": None},
        ])

    def test_fetches_every_page(self):
        """limit=None, not the library's default of 100. A silently
        truncated import would look like a matching failure rather than a
        missing page, which is the harder bug to notice."""
        seen = {}

        def capture(pid, limit):
            seen["limit"] = limit
            return {"title": "x", "tracks": []}

        with fake_ytmusicapi(capture):
            ytmusic_client.fetch_playlist("PLabc123")
        self.assertIsNone(seen["limit"])

    def test_entries_with_no_title_are_dropped_and_do_not_shift_positions(self):
        payload = {"title": "x", "tracks": [
            {"title": None, "artists": [{"name": "A"}]},
            {"title": "Real", "artists": [{"name": "A"}]},
        ]}
        with fake_ytmusicapi(lambda pid, limit: payload):
            result = ytmusic_client.fetch_playlist("PLabc123")
        self.assertEqual([(t["position"], t["title"]) for t in result["tracks"]], [(0, "Real")])

    def test_a_track_with_no_artist_still_comes_through(self):
        """The uploader-as-artist case from a mix-video playlist: artist is
        empty rather than absent, so the matcher gets a real (if useless)
        string and the entry lands in the unresolved review list instead of
        crashing the sync."""
        payload = {"title": "x", "tracks": [{"title": "90 minute mix", "artists": []}]}
        with fake_ytmusicapi(lambda pid, limit: payload):
            result = ytmusic_client.fetch_playlist("PLabc123")
        self.assertEqual(result["tracks"][0]["artist"], "")

    def test_backend_refusal_is_unavailable(self):
        class YTMusicServerError(Exception):
            pass

        def refuse(pid, limit):
            raise YTMusicServerError("nope")

        with fake_ytmusicapi(refuse):
            self.assertEqual(ytmusic_client.fetch_playlist("PLgone"), {"status": "unavailable"})

    def test_a_changed_response_shape_is_an_error_not_a_traceback(self):
        """The failure this feature is most exposed to over time: the
        endpoint is unofficial, so its parsing can break outright. It must
        come back as a status, because a raised exception here would take
        down a whole sync run that has other providers in it."""
        def explode(pid, limit):
            raise KeyError("contents")

        with fake_ytmusicapi(explode):
            result = ytmusic_client.fetch_playlist("PLabc123")
        self.assertEqual(result, {"status": "error", "reason": "KeyError"})

    def test_a_non_dict_response_is_an_error(self):
        with fake_ytmusicapi(lambda pid, limit: ["not", "a", "dict"]):
            self.assertEqual(ytmusic_client.fetch_playlist("PLabc123"),
                             {"status": "error", "reason": "unexpected_response"})

    def test_a_missing_dependency_is_reported_not_raised(self):
        """An install without ytmusicapi loses this one feature; it must
        not lose the sync run it is part of."""
        # A None entry in sys.modules is what the import system treats as
        # "this module is not importable" -- closer to the real absence
        # than deleting the key, which would just re-import the real one.
        with mock.patch.dict(sys.modules, {"ytmusicapi": None}):
            self.assertEqual(ytmusic_client.fetch_playlist("PLabc123"),
                             {"status": "error", "reason": "dependency_missing"})


class GetPlaylistTracksTests(unittest.TestCase):
    def test_carries_the_remote_title_not_the_argument(self):
        """A subscription is one playlist rather than an entry in a
        listing, so this fetch is the only place its title is ever learned
        — and a rename at the source has nowhere else to come from."""
        payload = {"title": "Renamed at the source", "tracks": []}
        with fake_ytmusicapi(lambda pid, limit: payload):
            result = ytmusic_client.get_playlist_tracks("Stale local title", "PLabc123")
        self.assertEqual(result["playlist"], "Renamed at the source")

    def test_a_failure_keeps_its_status(self):
        """_sync_one_playlist skips on any non-ok status, which is what
        stops a failed fetch from emptying an already-synced playlist."""
        class YTMusicUserError(Exception):
            pass

        def refuse(pid, limit):
            raise YTMusicUserError("gone")

        with fake_ytmusicapi(refuse):
            self.assertEqual(
                ytmusic_client.get_playlist_tracks("Party", "PLgone")["status"], "unavailable")

    def test_no_id_is_not_found(self):
        self.assertEqual(
            ytmusic_client.get_playlist_tracks("Party", None),
            {"status": "not_found", "failed_segment": "Party"})


if __name__ == "__main__":
    unittest.main()
