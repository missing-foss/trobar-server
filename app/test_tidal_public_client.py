#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for tidal_public_client.py: link parsing, the client-credentials
token, the playlist read, and every failure path.

Nothing here touches the network. `requests.post` (the token) and
`requests.get` (the API) are replaced by a fake Tidal in the shapes measured
against the live API, so the real walk in tidal_client runs, rate limit
included. time.sleep is patched out.

    python3 -m unittest test_tidal_public_client -v
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlsplit, parse_qs

_TMP = tempfile.mkdtemp(prefix="trobar-test-tidal-public-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import tidal_client  # noqa: E402
import tidal_public_client  # noqa: E402

PID = "11111111-2222-4333-8444-555555555555"


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise tidal_client.requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeTidal:
    """A token endpoint and the three reads the walk makes, as measured:
    items 20 to a page whatever page[size] says, linked by links.next."""

    PAGE = 20

    def __init__(self, n_tracks=45, access="PUBLIC"):
        self.tracks = [{"id": str(1000 + i), "title": f"Song {i}", "artist": f"Band {i % 3}",
                        "isrc": f"XXA0D2600{i:03d}"} for i in range(n_tracks)]
        self.access = access
        self.playlist_status = 200
        self.token_status = 200
        self.tokens = 0
        self.credentials = ("cid", "secret")  # what the token endpoint accepts
        self.throttle = []  # statuses to answer, one per coming items read, before the real one
        self.gets = []

    def post(self, url, data=None, auth=None, timeout=None):
        self.tokens += 1
        if self.token_status != 200 or auth != self.credentials:
            return _Resp(self.token_status, {"error": "invalid_client"})
        assert data == {"grant_type": "client_credentials"}
        return _Resp(200, {"access_token": f"tok{self.tokens}", "token_type": "Bearer",
                           "expires_in": 14400, "scope": ""})

    def get(self, url, params=None, headers=None, timeout=None):
        parts = urlsplit(url)
        path = parts.path.removeprefix("/v2")
        params = dict(params or {})
        params.update({k: v[0] for k, v in parse_qs(parts.query).items()})
        self.gets.append(path)
        if path == f"/playlists/{PID}":
            if self.playlist_status != 200:
                return _Resp(self.playlist_status, {"errors": []})
            return _Resp(200, {"data": {"id": PID, "type": "playlists", "attributes": {
                "name": "Night Bus", "accessType": self.access, "numberOfItems": len(self.tracks)}}})
        if path == f"/playlists/{PID}/relationships/items":
            if self.throttle:
                return _Resp(self.throttle.pop(0), None, {"Retry-After": "4"})
            start = int(params.get("page[cursor]", 0))
            page = self.tracks[start:start + self.PAGE]
            body: dict[str, Any] = {"data": [{"id": t["id"], "type": "tracks"} for t in page],
                    "included": [{"id": t["id"], "type": "tracks",
                                  "attributes": {"title": t["title"], "isrc": t["isrc"]}} for t in page],
                    "links": {}}
            if start + self.PAGE < len(self.tracks):
                body["links"]["next"] = (f"/playlists/{PID}/relationships/items"
                                         f"?page[cursor]={start + self.PAGE}")
            return _Resp(200, body)
        if path == "/tracks":
            ids = params["filter[id]"].split(",")
            by_id = {t["id"]: t for t in self.tracks}
            return _Resp(200, {
                "data": [{"id": i, "type": "tracks", "attributes": {"title": by_id[i]["title"]},
                          "relationships": {"artists": {"data": [{"id": "a" + i, "type": "artists"}]}}}
                         for i in ids],
                "included": [{"id": "a" + i, "type": "artists", "attributes": {"name": by_id[i]["artist"]}}
                             for i in ids]})
        return _Resp(404)


class _Base(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        self.addCleanup(self._db_path.unlink, missing_ok=True)
        self._configure("cid", "secret")
        tidal_public_client._token.update(value=None, expires_at=0.0)
        tidal_public_client._token["for"] = None
        self.fake = FakeTidal()
        for target, fn in ((tidal_public_client.requests, "post"), (tidal_client.requests, "get")):
            patcher = mock.patch.object(target, fn, side_effect=getattr(self.fake, fn))
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = mock.patch.object(tidal_client.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def _configure(self, client_id, secret):
        conn = db.get_conn()
        db.set_config(conn, "tidal_client_id", client_id)
        db.set_config(conn, "tidal_client_secret", secret)
        conn.commit()
        conn.close()


class ParseTests(unittest.TestCase):
    def test_the_link_forms_tidal_hands_out(self):
        for raw in (f"https://tidal.com/playlist/{PID}",
                    f"https://tidal.com/browse/playlist/{PID}?u",
                    f"https://listen.tidal.com/playlist/{PID}",
                    f"tidal.com/browse/playlist/{PID}",
                    f"https://tidal.com/playlist/{PID.upper()}"):
            self.assertEqual(tidal_public_client.parse_playlist_url(raw), PID, raw)

    def test_anything_else_is_not_a_tidal_link(self):
        for raw in (PID, f"https://open.spotify.com/playlist/{PID}",
                    f"https://tidal.com/album/{PID}", "https://tidal.com/playlist/12345",
                    f"https://tidal.com.example/playlist/{PID}", ""):
            self.assertIsNone(tidal_public_client.parse_playlist_url(raw), raw)


class FetchTests(_Base):
    def test_a_public_playlist_comes_back_whole_in_order_with_isrcs(self):
        result = tidal_public_client.fetch_playlist(PID)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["title"], "Night Bus")
        self.assertEqual(len(result["tracks"]), 45)  # three pages of 20
        first, last = result["tracks"][0], result["tracks"][-1]
        self.assertEqual((first["position"], first["artist"], first["title"], first["isrc"]),
                         (0, "Band 0", "Song 0", "XXA0D2600000"))
        self.assertEqual((last["position"], last["title"]), (44, "Song 44"))

    def test_a_rate_limit_mid_walk_is_waited_out(self):
        self.fake.throttle = [429]
        result = tidal_public_client.fetch_playlist(PID)
        self.assertEqual((result["status"], len(result["tracks"])), ("ok", 45))
        self.sleep.assert_called_with(4.0)

    def test_a_rate_limit_that_outlasts_the_retries_is_an_error_not_a_short_playlist(self):
        self.fake.throttle = [429] * tidal_client._RETRY_ATTEMPTS
        self.assertEqual(tidal_public_client.fetch_playlist(PID),
                         {"status": "error", "reason": "rate_limited"})

    def test_a_private_playlist_is_unavailable(self):
        self.fake.access = "PRIVATE"
        self.assertEqual(tidal_public_client.fetch_playlist(PID), {"status": "unavailable"})

    def test_an_unlisted_playlist_is_read(self):
        self.fake.access = "UNLISTED"
        self.assertEqual(tidal_public_client.fetch_playlist(PID)["status"], "ok")

    def test_a_missing_or_refused_playlist_is_unavailable(self):
        for status in (403, 404):
            self.fake.playlist_status = status
            self.assertEqual(tidal_public_client.fetch_playlist(PID), {"status": "unavailable"}, status)

    def test_a_server_error_on_the_playlist_is_an_error(self):
        self.fake.playlist_status = 400
        self.assertEqual(tidal_public_client.fetch_playlist(PID), {"status": "error", "reason": "http_400"})

    def test_no_credentials_is_not_configured_and_asks_nothing(self):
        self._configure(None, None)
        self.assertFalse(tidal_public_client.is_configured())
        self.assertEqual(tidal_public_client.fetch_playlist(PID),
                         {"status": "error", "reason": "not_configured"})
        self.assertEqual((self.fake.tokens, self.fake.gets), (0, []))

    def test_refused_credentials_are_said(self):
        self.fake.token_status = 401
        self.assertEqual(tidal_public_client.fetch_playlist(PID),
                         {"status": "error", "reason": "token_refused"})

    def test_the_provider_call_names_the_playlist(self):
        result = tidal_public_client.get_playlist_tracks("Old name", PID)
        self.assertEqual((result["status"], result["playlist"], len(result["tracks"])), ("ok", "Night Bus", 45))
        self.assertEqual(tidal_public_client.get_playlist_tracks("Old name", None)["status"], "not_found")


class TokenTests(_Base):
    def test_one_token_serves_many_reads(self):
        tidal_public_client.fetch_playlist(PID)
        tidal_public_client.fetch_playlist(PID)
        self.assertEqual(self.fake.tokens, 1)

    def test_a_token_near_its_expiry_is_renewed(self):
        tidal_public_client.fetch_playlist(PID)
        tidal_public_client._token["expires_at"] = tidal_public_client.time.monotonic() + 60
        tidal_public_client.fetch_playlist(PID)
        self.assertEqual(self.fake.tokens, 2)

    def test_changed_credentials_get_a_new_token(self):
        tidal_public_client.fetch_playlist(PID)
        self._configure("cid2", "secret2")
        self.fake.credentials = ("cid2", "secret2")
        tidal_public_client.fetch_playlist(PID)
        self.assertEqual(self.fake.tokens, 2)


def setUpModule():
    # unittest calls this each time it enters the module, and tearDownModule
    # each time it leaves; _TMP is made once, at import.
    os.makedirs(_TMP, exist_ok=True)


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
