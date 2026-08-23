#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the admin-configurable LASTFM_API_BASE/LISTENBRAINZ_API_BASE
override (lastfm.py / listenbrainz.py's api_base param) — confirms an
explicit api_base is actually used in the request URL, and that omitting
it falls back to the module-level default (env var or the real service).

    python3 -m unittest test_listening_history_sources -v

Mocks requests.get — no network access needed.
"""
import unittest
import unittest.mock as mock

import lastfm
import listenbrainz


class LastfmApiBaseTests(unittest.TestCase):
    def _mock_response(self, json_body):
        resp = mock.Mock()
        resp.json.return_value = json_body
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        return resp

    def test_check_connection_uses_override_base(self):
        with mock.patch("requests.get", return_value=self._mock_response({"user": {}})) as get:
            lastfm.check_connection("alice", api_key="k", api_base="http://libre.example/2.0/")
            self.assertEqual(get.call_args[0][0], "http://libre.example/2.0/")

    def test_check_connection_falls_back_to_module_default_when_no_override(self):
        with mock.patch("requests.get", return_value=self._mock_response({"user": {}})) as get:
            lastfm.check_connection("alice", api_key="k")
            self.assertEqual(get.call_args[0][0], lastfm.API_BASE)

    def test_top_albums_uses_override_base(self):
        with mock.patch("requests.get", return_value=self._mock_response({"topalbums": {"album": []}})) as get:
            lastfm.top_albums("alice", api_key="k", api_base="http://libre.example/2.0/")
            self.assertEqual(get.call_args[0][0], "http://libre.example/2.0/")

    def test_similar_artists_uses_override_base(self):
        with mock.patch("requests.get", return_value=self._mock_response({"similarartists": {"artist": []}})) as get:
            lastfm.similar_artists("Boards of Canada", api_key="k", api_base="http://libre.example/2.0/")
            self.assertEqual(get.call_args[0][0], "http://libre.example/2.0/")

    def test_recent_tracks_uses_override_base(self):
        with mock.patch("requests.get", return_value=self._mock_response({"recenttracks": {"track": []}})) as get:
            lastfm.recent_tracks("alice", api_key="k", api_base="http://libre.example/2.0/")
            self.assertEqual(get.call_args[0][0], "http://libre.example/2.0/")

    def test_suggestions_and_recently_played_suggestions_thread_the_override(self):
        # Both wrap a leaf function (top_albums / recent_tracks) — confirm
        # the override actually reaches the underlying request, not just
        # that the wrapper accepts the parameter.
        with mock.patch("requests.get", return_value=self._mock_response({"topalbums": {"album": []}})) as get:
            lastfm.suggestions(None, "alice", api_key="k", api_base="http://libre.example/2.0/")
            self.assertEqual(get.call_args[0][0], "http://libre.example/2.0/")
        with mock.patch("requests.get", return_value=self._mock_response({"recenttracks": {"track": []}})) as get:
            lastfm.recently_played_suggestions(None, "alice", api_key="k", api_base="http://libre.example/2.0/")
            self.assertEqual(get.call_args[0][0], "http://libre.example/2.0/")


class ListenBrainzApiBaseTests(unittest.TestCase):
    def _mock_response(self, json_body=None, status_code=200):
        resp = mock.Mock()
        resp.json.return_value = json_body or {}
        resp.raise_for_status.return_value = None
        resp.status_code = status_code
        return resp

    def test_check_connection_uses_override_base(self):
        with mock.patch("requests.get", return_value=self._mock_response()) as get:
            listenbrainz.check_connection("alice", api_base="http://selfhosted.example")
            self.assertTrue(get.call_args[0][0].startswith("http://selfhosted.example/"))

    def test_check_connection_falls_back_to_module_default_when_no_override(self):
        with mock.patch("requests.get", return_value=self._mock_response()) as get:
            listenbrainz.check_connection("alice")
            self.assertTrue(get.call_args[0][0].startswith(listenbrainz.API_BASE))

    def test_top_release_groups_uses_override_base(self):
        with mock.patch("requests.get", return_value=self._mock_response({"payload": {"release_groups": []}})) as get:
            listenbrainz.top_release_groups("alice", api_base="http://selfhosted.example")
            self.assertTrue(get.call_args[0][0].startswith("http://selfhosted.example/"))

    def test_recent_listens_uses_override_base(self):
        with mock.patch("requests.get", return_value=self._mock_response({"payload": {"listens": []}})) as get:
            listenbrainz.recent_listens("alice", api_base="http://selfhosted.example")
            self.assertTrue(get.call_args[0][0].startswith("http://selfhosted.example/"))

    def test_suggestions_and_recently_played_suggestions_thread_the_override(self):
        with mock.patch("requests.get", return_value=self._mock_response({"payload": {"release_groups": []}})) as get:
            listenbrainz.suggestions(None, "alice", api_base="http://selfhosted.example")
            self.assertTrue(get.call_args[0][0].startswith("http://selfhosted.example/"))
        with mock.patch("requests.get", return_value=self._mock_response({"payload": {"listens": []}})) as get:
            listenbrainz.recently_played_suggestions(None, "alice", api_base="http://selfhosted.example")
            self.assertTrue(get.call_args[0][0].startswith("http://selfhosted.example/"))


if __name__ == "__main__":
    unittest.main()
