#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""maloja.py against payloads recorded from a throwaway Maloja 3.2.6 (the
official image), seeded with invented artists through its own scrobble API.
The recording holds the cases the client has to get right: an album whose
"Cinder & Ash" credit Maloja split into ["Ash", "Cinder"], an album by two
artists, a "feat." track whose album has one artist, and a scrobble with no
album. Every failure degrades to [] or False, as for the other sources.

Mocks the guarded session's get, no network, except RequestGuardTests:
those run the real client against HTTP servers on 127.0.0.1 to show the
url_guard checks hold through maloja.py. The library filter runs against a
real temp-file SQLite database built by db.init_db().

    python3 -m unittest test_maloja -v      # from app/
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
import unittest.mock as mock
from datetime import date
from pathlib import Path

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-maloja-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import maloja  # noqa: E402
import sync_state  # noqa: E402
import url_guard  # noqa: E402
from test_url_guard import _Server, relaxed, resolving  # noqa: E402

BASE = "http://maloja.example.invalid:42010"

# GET /apis/mlj_1/charts/albums?from=1970, as recorded.
CHART = [
    {"scrobbles": 63, "album": {"artists": ["Aphelion"], "albumtitle": "Event Horizon"}, "album_id": 5, "rank": 1},
    {"scrobbles": 63, "album": {"artists": ["Glass Meridian"], "albumtitle": "Latitude"}, "album_id": 1, "rank": 1},
    {"scrobbles": 60, "album": {"artists": ["Aphelion"], "albumtitle": "Parallax"}, "album_id": 2, "rank": 3},
    {"scrobbles": 57, "album": {"artists": ["Ash", "Cinder"], "albumtitle": "Emberwake"}, "album_id": 4, "rank": 4},
    {"scrobbles": 57, "album": {"artists": ["Glass Meridian", "Nebula Drift"], "albumtitle": "Shared Orbit"},
     "album_id": 3, "rank": 4},
    {"scrobbles": 16, "album": {"artists": ["Paper Cartographer"], "albumtitle": "Foldlines"}, "album_id": 6, "rank": 6},
]


def _scrobble(time, artists, title, album_artists=None, album=None):
    return {"time": time, "track": {"artists": artists, "title": title,
                                    "album": {"artists": album_artists, "albumtitle": album} if album else None,
                                    "length": None}, "duration": None, "origin": "client:default"}


# GET /apis/mlj_1/scrobbles?from=1970, as recorded: newest first.
SCROBBLES = [
    _scrobble(1791136204, ["Aphelion", "Paper Cartographer"], "Contour", ["Paper Cartographer"], "Foldlines"),
    _scrobble(1791126618, ["Ash", "Cinder"], "Smoulder", ["Ash", "Cinder"], "Emberwake"),
    _scrobble(1791049804, ["Aphelion", "Paper Cartographer"], "Contour", ["Paper Cartographer"], "Foldlines"),
    _scrobble(1790963404, ["Nebula Drift"], "Loose Single"),
    _scrobble(1790003788, ["Glass Meridian", "Nebula Drift"], "Two Suns", ["Glass Meridian", "Nebula Drift"], "Shared Orbit"),
]

SERVERINFO = {"name": "Generic Maloja User", "version": ["3", "2", "6"], "versionstring": "3.2.6",
              "db_status": {"healthy": True, "rebuildinprogress": False, "complete": True}}


def _resp(body=None, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body
    r.raise_for_status.side_effect = requests.HTTPError(str(status)) if status >= 400 else None
    return r


class _Library(unittest.TestCase):
    """The library holds five of the six charted albums (not Shared Orbit),
    under their own tags: "Cinder & Ash" as one artist string."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        self.conn = db.get_conn()
        for i, (artist, album) in enumerate([
                ("Aphelion", "Event Horizon"), ("Glass Meridian", "Latitude"), ("Aphelion", "Parallax"),
                ("Cinder & Ash", "Emberwake"), ("Paper Cartographer", "Foldlines")]):
            self.conn.execute("INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
                              "VALUES (?, ?, ?, 'T', 1, 0)", (f"{i}.flac", artist, album))
        self.user = sync_state._new_id(self.conn.execute("INSERT INTO users (username) VALUES ('alice')"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._db_path.unlink(missing_ok=True)


class ArtistSetTests(unittest.TestCase):
    def test_credits_split_the_way_maloja_splits_them(self):
        self.assertEqual(maloja._artist_set("Cinder & Ash"), {"cinder", "ash"})
        self.assertEqual(maloja._artist_set(["Ash", "Cinder"]), {"cinder", "ash"})
        self.assertEqual(maloja._artist_set("A feat. B"), {"a", "b"})
        self.assertEqual(maloja._artist_set("A ft B vs. C"), {"a", "b", "c"})
        self.assertEqual(maloja._artist_set("A; B/C|D"), {"a", "b", "c", "d"})

    def test_a_comma_or_an_unspaced_ampersand_is_not_a_split(self):
        self.assertEqual(maloja._artist_set("Crosby, Stills"), {"crosby, stills"})
        self.assertEqual(maloja._artist_set("AC&DC"), {"ac&dc"})


class PeriodTests(unittest.TestCase):
    def test_each_trobar_period_becomes_a_from_date(self):
        today = date(2026, 10, 4)
        with mock.patch.object(maloja, "date", wraps=date) as d:
            d.today.return_value = today
            self.assertEqual({p: maloja._from(p) for p in maloja._PERIOD_DAYS}, {
                "overall": "1970", "7day": "2026/09/27", "1month": "2026/09/04",
                "3month": "2026/07/05", "6month": "2026/04/05", "12month": "2025/10/04"})

    def test_every_period_the_routes_accept_is_mapped(self):
        import main  # noqa: PLC0415 - main is heavy; only this test needs it
        self.assertEqual(set(maloja._PERIOD_DAYS), set(main._LASTFM_PERIODS))

    def test_the_chart_is_asked_for_with_a_from_never_without_one(self):
        # Without a range Maloja answers for today only.
        with mock.patch.object(url_guard.GuardedSession, "get", return_value=_resp({"status": "ok", "list": CHART})) as get:
            maloja.album_charts(BASE, "overall")
        args, kwargs = get.call_args
        self.assertEqual(args[0], BASE + "/apis/mlj_1/charts/albums")
        self.assertEqual(kwargs["params"], {"from": "1970"})


class SuggestionTests(_Library):
    def _suggest(self, **kw):
        with mock.patch.object(url_guard.GuardedSession, "get", return_value=_resp({"status": "ok", "list": CHART})):
            return maloja.suggestions(self.conn, BASE, "overall", **kw)

    def test_charted_albums_in_the_library_are_suggested_under_the_librarys_names(self):
        out = self._suggest()
        self.assertEqual(sorted((o["artist"], o["album"], o["playcount"], o["source"]) for o in out), [
            ("Aphelion", "Event Horizon", 63, "maloja"), ("Aphelion", "Parallax", 60, "maloja"),
            ("Cinder & Ash", "Emberwake", 57, "maloja"), ("Glass Meridian", "Latitude", 63, "maloja"),
            ("Paper Cartographer", "Foldlines", 16, "maloja")])

    def test_a_split_credit_matches_only_through_the_artist_set(self):
        # The library's "Cinder & Ash" is ["Ash", "Cinder"] in Maloja: a
        # joined-string lookup would miss it (1 of the 5 library albums here).
        with mock.patch.object(maloja, "_artist_set", side_effect=lambda n: frozenset(
                [", ".join(n).lower()] if isinstance(n, list) else [n.lower()])):
            out = self._suggest()
        self.assertNotIn("Emberwake", {o["album"] for o in out})
        self.assertEqual(len(out), 4)

    def test_an_album_every_device_has_is_not_suggested(self):
        dev, _ = sync_state.create_device(self.conn, self.user, "Phone")
        sync_state.create_selection(self.conn, "album", "Cinder & Ash||Emberwake", self.user, [dev])
        out = self._suggest(user_device_ids={dev})
        self.assertNotIn("Emberwake", {o["album"] for o in out})

    def test_recently_played_skips_scrobbles_without_an_album_and_dedups(self):
        with mock.patch.object(url_guard.GuardedSession, "get", return_value=_resp({"status": "ok", "list": SCROBBLES})) as get:
            out = maloja.recently_played_suggestions(self.conn, BASE, limit=50)
        self.assertEqual([(o["artist"], o["album"], o["source"]) for o in out], [
            ("Paper Cartographer", "Foldlines", "maloja-recent"), ("Cinder & Ash", "Emberwake", "maloja-recent")])
        self.assertEqual(get.call_args.kwargs["params"], {"from": "1970", "page": 0, "perpage": 50})


class MostPlayedTests(_Library):
    def test_ranked_with_library_names_and_maloja_credits_for_the_rest(self):
        with mock.patch.object(url_guard.GuardedSession, "get", return_value=_resp({"status": "ok", "list": CHART})):
            out = maloja.most_played(self.conn, BASE, "overall", limit=10)
        self.assertEqual([(o["artist"], o["album"], o["playcount"]) for o in out], [
            ("Aphelion", "Event Horizon", 63), ("Glass Meridian", "Latitude", 63), ("Aphelion", "Parallax", 60),
            ("Cinder & Ash", "Emberwake", 57), ("Glass Meridian & Nebula Drift", "Shared Orbit", 57),
            ("Paper Cartographer", "Foldlines", 16)])

    def test_limit_cuts_the_chart(self):
        with mock.patch.object(url_guard.GuardedSession, "get", return_value=_resp({"status": "ok", "list": CHART})):
            self.assertEqual(len(maloja.most_played(self.conn, BASE, "overall", limit=2)), 2)


class FailureTests(_Library):
    def test_unreachable_is_empty_everywhere(self):
        with mock.patch.object(url_guard.GuardedSession, "get", side_effect=requests.ConnectionError("down")):
            self.assertFalse(maloja.check_connection(BASE))
            self.assertEqual(maloja.suggestions(self.conn, BASE), [])
            self.assertEqual(maloja.most_played(self.conn, BASE), [])
            self.assertEqual(maloja.recently_played_suggestions(self.conn, BASE), [])

    def test_a_server_error_is_empty(self):
        # An unparseable range, for one, answers 500.
        with mock.patch.object(url_guard.GuardedSession, "get", return_value=_resp({"status": "failure"}, status=500)):
            self.assertEqual(maloja.album_charts(BASE), [])
            self.assertEqual(maloja.recent_scrobbles(BASE), [])

    def test_no_url_makes_no_request(self):
        with mock.patch.object(url_guard.GuardedSession, "get") as get:
            self.assertFalse(maloja.check_connection(""))
            self.assertEqual(maloja.suggestions(self.conn, ""), [])
            self.assertEqual(maloja.most_played(self.conn, ""), [])
        get.assert_not_called()

    def test_something_else_answering_is_not_a_maloja(self):
        with mock.patch.object(url_guard.GuardedSession, "get", return_value=_resp({"hello": "world"})):
            self.assertFalse(maloja.check_connection(BASE))
        with mock.patch.object(url_guard.GuardedSession, "get", return_value=_resp(SERVERINFO)) as get:
            self.assertTrue(maloja.check_connection(BASE))
        self.assertEqual(get.call_args.args[0], BASE + "/apis/mlj_1/serverinfo")


class RequestGuardTests(_Library):
    """A Maloja that answers, reached in ways the guard refuses."""

    def setUp(self):
        super().setUp()
        info = (200, {"Content-Type": "application/json"}, json.dumps(SERVERINFO).encode())
        chart = (200, {"Content-Type": "application/json"}, json.dumps({"status": "ok", "list": CHART}).encode())
        self.maloja = _Server({"/apis/mlj_1/serverinfo": info, "/apis/mlj_1/charts/albums": chart})
        self.addCleanup(self.maloja.close)

    def _quietly(self, call):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = call()
        return result, out.getvalue()

    def test_a_hostname_now_resolving_to_loopback_is_unreachable(self):
        base = f"http://maloja.example.invalid:{self.maloja.port}"
        with resolving("maloja.example.invalid", "127.0.0.1"):
            ok, log = self._quietly(lambda: maloja.check_connection(base))
            charted, log2 = self._quietly(lambda: maloja.most_played(self.conn, base, "overall"))
            self.assertEqual((ok, charted, self.maloja.hits), (False, [], []))
            for line in (log, log2):
                self.assertIn("loopback", line)
                self.assertNotIn("maloja.example.invalid", line)
                self.assertNotIn(str(self.maloja.port), line)
            with relaxed():  # control: the same calls reach it unguarded
                self.assertTrue(maloja.check_connection(base))
                self.assertEqual(len(maloja.most_played(self.conn, base, "overall")), len(CHART))

    def test_a_redirect_is_not_followed(self):
        bouncer = _Server({"/apis/mlj_1/serverinfo": (
            302, {"Location": self.maloja.url + "/apis/mlj_1/serverinfo"}, b"")})
        self.addCleanup(bouncer.close)
        # With the guard relaxed only the refusal to follow stands between
        # the bouncer and the Maloja behind it.
        with relaxed():
            self.assertFalse(maloja.check_connection(bouncer.url))
        self.assertEqual((bouncer.hits, self.maloja.hits), (["/apis/mlj_1/serverinfo"], []))


if __name__ == "__main__":
    unittest.main()
