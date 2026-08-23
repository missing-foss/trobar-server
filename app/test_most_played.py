#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""#267: lastfm.most_played() / listenbrainz.most_played() — the ranked,
un-shuffled, not-library-filtered counterpart to each module's own
suggestions(). The one behavior that actually distinguishes them from
suggestions() is "no local library filter", so that's what these mostly
check; the raw-API-parsing paths are already covered indirectly via
top_albums()/top_release_groups() in test_listening_history_sources.py.

    python3 -m unittest test_most_played -v

Mocks requests.get — no network access needed.
"""
import unittest
import unittest.mock as mock

import lastfm
import listenbrainz


def _mock_response(json_body):
    resp = mock.Mock()
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    resp.status_code = 200
    return resp


class LastfmMostPlayedTests(unittest.TestCase):
    def test_includes_albums_not_in_any_local_library(self):
        # Unlike suggestions(), which needs a conn + local library index to
        # filter against, most_played() takes no conn at all — nothing to
        # filter with, on purpose.
        body = {"topalbums": {"album": [
            {"artist": {"name": "Boards of Canada"}, "name": "Geogaddi", "playcount": "50"},
        ]}}
        with mock.patch("requests.get", return_value=_mock_response(body)):
            result = lastfm.most_played("alice", api_key="k")
        self.assertEqual(result, [
            {"artist": "Boards of Canada", "album": "Geogaddi", "playcount": 50, "image_url": None},
        ])

    def test_preserves_lastfms_playcount_descending_order(self):
        body = {"topalbums": {"album": [
            {"artist": {"name": "A"}, "name": "First", "playcount": "80"},
            {"artist": {"name": "B"}, "name": "Second", "playcount": "40"},
        ]}}
        with mock.patch("requests.get", return_value=_mock_response(body)):
            result = lastfm.most_played("alice", api_key="k")
        self.assertEqual([r["album"] for r in result], ["First", "Second"])

    def test_skips_entries_missing_artist_or_album(self):
        body = {"topalbums": {"album": [
            {"artist": {"name": ""}, "name": "No Artist", "playcount": "10"},
            {"artist": {"name": "Real Artist"}, "name": "", "playcount": "10"},
            {"artist": {"name": "Real Artist"}, "name": "Real Album", "playcount": "10"},
        ]}}
        with mock.patch("requests.get", return_value=_mock_response(body)):
            result = lastfm.most_played("alice", api_key="k")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["album"], "Real Album")

    def test_empty_without_username_or_key(self):
        self.assertEqual(lastfm.most_played(""), [])
        self.assertEqual(lastfm.most_played("alice", api_key=""), [])


class ListenBrainzMostPlayedTests(unittest.TestCase):
    def test_includes_albums_not_in_any_local_library(self):
        body = {"payload": {"release_groups": [
            {"artist_name": "Boards of Canada", "release_group_name": "Geogaddi", "listen_count": 50},
        ]}}
        with mock.patch("requests.get", return_value=_mock_response(body)):
            result = listenbrainz.most_played("alice")
        self.assertEqual(result, [
            {"artist": "Boards of Canada", "album": "Geogaddi", "playcount": 50, "image_url": None},
        ])

    def test_skips_entries_missing_artist_or_album(self):
        body = {"payload": {"release_groups": [
            {"artist_name": "", "release_group_name": "No Artist", "listen_count": 10},
            {"artist_name": "Real Artist", "release_group_name": "Real Album", "listen_count": 10},
        ]}}
        with mock.patch("requests.get", return_value=_mock_response(body)):
            result = listenbrainz.most_played("alice")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["album"], "Real Album")

    def test_empty_without_username(self):
        self.assertEqual(listenbrainz.most_played(""), [])


if __name__ == "__main__":
    unittest.main()
