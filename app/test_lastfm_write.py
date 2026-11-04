#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sending plays to Last.fm: signing, batches, its error codes, and the link.

    python3 -m unittest test_lastfm_write -v
"""
import hashlib
import unittest
from unittest import mock

import requests

import lastfm


class SignatureTests(unittest.TestCase):
    def test_the_documented_shape(self):
        params = {"method": "auth.getSession", "api_key": "k", "token": "t"}
        self.assertEqual(lastfm.api_signature(params, "s"),
                         hashlib.md5(b"api_keykmethodauth.getSessiontokents").hexdigest())

    def test_format_is_not_signed(self):
        self.assertEqual(lastfm.api_signature({"a": "1", "format": "json"}, "s"),
                         lastfm.api_signature({"a": "1"}, "s"))

    def test_names_sort_in_ascii_order(self):
        """Last.fm's own example: artist[10] comes before artist[1]... by byte."""
        joined = lastfm.api_signature({"artist[10]": "b", "artist[1]": "a"}, "")
        self.assertEqual(joined, hashlib.md5(b"artist[10]bartist[1]a").hexdigest())

    def test_auth_url_carries_key_and_callback(self):
        url = lastfm.auth_url("k", "https://t.example/cb?state=x")
        self.assertEqual(url, "https://www.last.fm/api/auth/?api_key=k&cb=https%3A%2F%2Ft.example%2Fcb%3Fstate%3Dx")

    def test_get_session(self):
        resp = mock.Mock()
        resp.json.return_value = {"session": {"name": "air", "key": "sk", "subscriber": 0}}
        with mock.patch("lastfm.requests.post", return_value=resp) as post:
            self.assertEqual(lastfm.get_session("k", "s", "t"), {"name": "air", "key": "sk"})
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["method"], "auth.getSession")
        self.assertEqual(data["format"], "json")
        self.assertNotIn("s", data)

    def test_no_session_is_none(self):
        resp = mock.Mock()
        resp.json.return_value = {"error": 14, "message": "unauthorized"}
        with mock.patch("lastfm.requests.post", return_value=resp):
            self.assertIsNone(lastfm.get_session("k", "s", "t"))
        with mock.patch("lastfm.requests.post", side_effect=requests.ConnectionError):
            with self.assertLogs("lastfm", level="WARNING"):
                self.assertIsNone(lastfm.get_session("k", "s", "t"))


if __name__ == "__main__":
    unittest.main()
