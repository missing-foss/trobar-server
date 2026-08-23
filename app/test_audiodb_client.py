#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for audiodb_client.py (#390) — mocks requests, no network access.

get_artist_image is documented as never raising: an API hiccup must degrade
to the provider fallback in artist_images.py rather than break the endpoint.
That contract is only worth the docstring if every way TheAudioDB can
disappoint us is actually exercised, so each early return below gets its own
test — a wrong status, an empty or malformed body, an artist with no picture,
and a picture URL that itself fails.

    python3 -m unittest test_audiodb_client -v
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-audiodb-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import audiodb_client  # noqa: E402

_IMG = b"\xff\xd8\xff\xe0 not really a jpeg, but bytes are bytes"


def _resp(status_code=200, json_body=None, content=b"", headers=None):
    r = mock.Mock()
    r.status_code = status_code
    r.content = content
    r.headers = headers if headers is not None else {}
    if isinstance(json_body, Exception):
        r.json.side_effect = json_body
    else:
        r.json.return_value = json_body
    return r


def _search_hit(thumb="https://example.invalid/thumb.jpg", fanart=None):
    artist: dict[str, str] = {"idArtist": "111"}
    if thumb is not None:
        artist["strArtistThumb"] = thumb
    if fanart is not None:
        artist["strArtistFanart"] = fanart
    return {"artists": [artist]}


def _found(result: tuple[bytes, str, str] | None) -> tuple[bytes, str, str]:
    """Narrow the Optional so mypy (check_untyped_defs is on) can see through
    to the tuple, and fail with a readable message rather than a bare
    TypeError when a lookup that should have succeeded returned None."""
    assert result is not None, "expected an image, got None"
    return result


class GetArtistImageTests(unittest.TestCase):

    def test_returns_the_thumb_bytes_content_type_and_source_url(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit()),
                _resp(content=_IMG, headers={"Content-Type": "image/jpeg"}),
            ]
            result = audiodb_client.get_artist_image("Portishead", "KEY")
        self.assertEqual(result, (_IMG, "image/jpeg", "https://example.invalid/thumb.jpg"))

    def test_url_encodes_the_artist_name(self):
        # An artist with a space and an ampersand is ordinary ("Simon &
        # Garfunkel"); pasted straight into the query string it would
        # truncate the search. Artist names come from file tags, so this is
        # the encoding that actually gets exercised in the wild.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit()),
                _resp(content=_IMG, headers={"Content-Type": "image/jpeg"}),
            ]
            audiodb_client.get_artist_image("Simon & Garfunkel", "KEY")
            search_url = req.get.call_args_list[0].args[0]
        self.assertIn("Simon%20%26%20Garfunkel", search_url)
        self.assertNotIn(" ", search_url)

    def test_a_slash_in_the_api_key_is_not_escaped(self):
        # Pinning current behaviour rather than endorsing it: urllib's quote()
        # defaults to safe="/", so a slash survives — and the key sits in a
        # PATH segment (.../json/{key}/search.php), so a malformed key with a
        # slash in it silently requests a different path instead of failing
        # cleanly. Harmless today (the key is admin-supplied, not user input,
        # and a wrong key just means no picture) but it is not what the
        # surrounding quote() calls look like they promise. If this is ever
        # tightened to quote(api_key, safe=""), this test should flip
        # deliberately rather than the change passing unnoticed.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.return_value = _resp(json_body=None)
            audiodb_client.get_artist_image("Portishead", "k/ey")
            search_url = req.get.call_args_list[0].args[0]
        self.assertIn("/json/k/ey/search.php", search_url)

    def test_falls_back_to_fanart_when_the_artist_has_no_thumb(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit(thumb=None,
                                            fanart="https://example.invalid/fan.jpg")),
                _resp(content=_IMG, headers={"Content-Type": "image/png"}),
            ]
            result = audiodb_client.get_artist_image("Portishead", "KEY")
        self.assertEqual(_found(result)[2], "https://example.invalid/fan.jpg")

    def test_prefers_the_thumb_over_fanart_when_both_exist(self):
        # thumb is the square portrait the UI and devices want; fanart is
        # only the consolation prize.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit(thumb="https://example.invalid/thumb.jpg",
                                            fanart="https://example.invalid/fan.jpg")),
                _resp(content=_IMG, headers={"Content-Type": "image/jpeg"}),
            ]
            result = audiodb_client.get_artist_image("Portishead", "KEY")
        self.assertEqual(_found(result)[2], "https://example.invalid/thumb.jpg")

    def test_strips_charset_from_the_content_type(self):
        # A Content-Type of "image/jpeg; charset=binary" must not be handed
        # on verbatim — it ends up in a response header downstream.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit()),
                _resp(content=_IMG,
                      headers={"Content-Type": "image/jpeg; charset=binary"}),
            ]
            result = audiodb_client.get_artist_image("Portishead", "KEY")
        self.assertEqual(_found(result)[1], "image/jpeg")

    def test_defaults_the_content_type_when_the_image_response_omits_it(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit()),
                _resp(content=_IMG, headers={}),
            ]
            result = audiodb_client.get_artist_image("Portishead", "KEY")
        self.assertEqual(_found(result)[1], "image/jpeg")

    def test_returns_none_when_the_search_is_not_200(self):
        # The 429 body is deliberately a COMPLETE, valid search hit, and a
        # working image response is queued behind it. Without that, removing
        # the status-code check still yields None via the empty-artists path
        # and this test passes for the wrong reason — which is exactly what
        # happened on the first draft, caught by mutating the guard away.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(status_code=429, json_body=_search_hit()),
                _resp(content=_IMG, headers={"Content-Type": "image/jpeg"}),
            ]
            self.assertIsNone(audiodb_client.get_artist_image("Portishead", "KEY"))

    def test_returns_none_when_the_search_body_is_null(self):
        # TheAudioDB answers a miss with a literal JSON `null`, which is why
        # the code says `(resp.json() or {})` rather than indexing directly.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.return_value = _resp(json_body=None)
            self.assertIsNone(audiodb_client.get_artist_image("Nobody", "KEY"))

    def test_returns_none_when_artists_is_null(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.return_value = _resp(json_body={"artists": None})
            self.assertIsNone(audiodb_client.get_artist_image("Nobody", "KEY"))

    def test_returns_none_when_artists_is_empty(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.return_value = _resp(json_body={"artists": []})
            self.assertIsNone(audiodb_client.get_artist_image("Nobody", "KEY"))

    def test_returns_none_when_the_artist_has_neither_thumb_nor_fanart(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.return_value = _resp(json_body=_search_hit(thumb=None))
            self.assertIsNone(audiodb_client.get_artist_image("Portishead", "KEY"))

    def test_returns_none_when_the_picture_url_is_an_empty_string(self):
        # TheAudioDB uses "" rather than omitting the field for artists with
        # no picture, so a truthiness check is what catches it.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.return_value = _resp(
                json_body={"artists": [{"strArtistThumb": "", "strArtistFanart": ""}]})
            self.assertIsNone(audiodb_client.get_artist_image("Portishead", "KEY"))

    def test_returns_none_when_the_image_fetch_is_not_200(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit()),
                _resp(status_code=404),
            ]
            self.assertIsNone(audiodb_client.get_artist_image("Portishead", "KEY"))

    def test_returns_none_when_the_image_body_is_empty(self):
        # A 200 with zero bytes would otherwise be cached as a valid picture
        # and served as a broken image forever.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit()),
                _resp(content=b"", headers={"Content-Type": "image/jpeg"}),
            ]
            self.assertIsNone(audiodb_client.get_artist_image("Portishead", "KEY"))

    def test_returns_none_on_a_network_error(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.RequestException = requests.RequestException
            req.get.side_effect = requests.ConnectionError("no route to host")
            self.assertIsNone(audiodb_client.get_artist_image("Portishead", "KEY"))

    def test_returns_none_on_a_timeout(self):
        with mock.patch.object(audiodb_client, "requests") as req:
            req.RequestException = requests.RequestException
            req.get.side_effect = requests.Timeout("too slow")
            self.assertIsNone(audiodb_client.get_artist_image("Portishead", "KEY"))

    def test_returns_none_when_the_body_is_not_json(self):
        # An HTML error page returned with a 200 — .json() raises ValueError,
        # which the except clause names explicitly.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.RequestException = requests.RequestException
            req.get.return_value = _resp(json_body=ValueError("no json"))
            self.assertIsNone(audiodb_client.get_artist_image("Portishead", "KEY"))

    def test_sends_a_timeout_on_both_requests(self):
        # A hung TheAudioDB must not hold an artist-image request open
        # indefinitely — the whole point of degrading to the fallback.
        with mock.patch.object(audiodb_client, "requests") as req:
            req.get.side_effect = [
                _resp(json_body=_search_hit()),
                _resp(content=_IMG, headers={"Content-Type": "image/jpeg"}),
            ]
            audiodb_client.get_artist_image("Portishead", "KEY")
            timeouts = [c.kwargs.get("timeout") for c in req.get.call_args_list]
        self.assertEqual(timeouts, [audiodb_client._TIMEOUT, audiodb_client._TIMEOUT])


if __name__ == "__main__":
    unittest.main()
