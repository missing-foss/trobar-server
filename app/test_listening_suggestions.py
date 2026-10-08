#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the listening-history suggestion sources -- lastfm.py,
listenbrainz.py -- and the helpers they share in suggestions.py.

Every network call here is documented as degrading to [] or False rather
than raising: a Last.fm or ListenBrainz hiccup must leave the dashboard
with an empty widget, not a broken page. So each way a service can
disappoint -- no configuration, an HTTP error, a body that is not what was
expected -- gets its own assertion, beside the parsing and the library
filter that decide what a suggestion is: an album in the local library and
not yet synced to *every* device the caller manages.

Mocks requests.get -- no network access. The library filter runs against a
real temp-file SQLite database built by db.init_db(), so the schema it
reads is the one the app uses.

    python3 -m unittest test_listening_suggestions -v
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-suggestions-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import lastfm  # noqa: E402
import listenbrainz  # noqa: E402
import suggestions  # noqa: E402

_PLACEHOLDER = f"https://lastfm.freetls.fastly.net/i/u/300x300/{lastfm._LASTFM_PLACEHOLDER_HASH}.png"


def _resp(json_body=None, status_code=200, http_error=False):
    r = mock.Mock()
    r.status_code = status_code
    if isinstance(json_body, Exception):
        r.json.side_effect = json_body
    else:
        r.json.return_value = json_body if json_body is not None else {}
    r.raise_for_status.side_effect = requests.HTTPError("boom") if http_error else None
    return r


class _LibraryBase(unittest.TestCase):
    """A library of three albums, two devices owned by one user, and helpers
    to cover an album or an artist on some of them."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        self.conn = db.get_conn()
        self.user = self.conn.execute("INSERT INTO users (username) VALUES ('alice')").lastrowid
        self.phone = self._device("Phone")
        self.dap = self._device("DAP")
        self._track("Boards of Canada", "Geogaddi", scanned_at="2026-09-01 10:00:00", release_date="2002-02-18")
        self._track("Boards of Canada", "Tomorrow's Harvest", scanned_at="2026-09-10 10:00:00", release_date="2013-06-05")
        self._track("Aphex Twin", "Syro", scanned_at="2026-09-20 10:00:00", release_date=None)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._db_path.unlink(missing_ok=True)

    def _device(self, name):
        return self.conn.execute(
            "INSERT INTO devices (owner_user_id, name, api_token_hash) VALUES (?, ?, 'x')", (self.user, name),
        ).lastrowid

    def _track(self, artist, album, scanned_at, release_date, n=1):
        for i in range(n):
            self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, scanned_at, release_date) "
                "VALUES (?, ?, ?, ?, 1, 0, ?, ?)",
                (f"{artist}/{album}/{i}.flac", artist, album, f"t{i}", scanned_at, release_date),
            )

    def _cover(self, type_, target, *device_ids):
        sel = self.conn.execute(
            "INSERT INTO selections (type, target, created_by_user_id) VALUES (?, ?, ?)", (type_, target, self.user),
        ).lastrowid
        for d in device_ids:
            self.conn.execute("INSERT INTO selection_devices (selection_id, device_id) VALUES (?, ?)", (sel, d))
        self.conn.commit()


# --- suggestions.py -------------------------------------------------------

class SharedHelperTests(_LibraryBase):
    def test_the_library_index_maps_lowercased_keys_to_the_casing_on_disk(self):
        index = suggestions.local_library_index(self.conn)
        self.assertEqual(index[("boards of canada", "geogaddi")], ("Boards of Canada", "Geogaddi"))

    def test_a_deleted_track_is_not_in_the_library(self):
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE album = 'Syro'")
        self.assertNotIn(("aphex twin", "syro"), suggestions.local_library_index(self.conn))

    def test_an_album_selection_covers_that_album_and_an_artist_selection_every_album(self):
        self._cover("album", "Boards of Canada||Geogaddi", self.phone)
        self._cover("artist", "aphex twin", self.dap)  # an artist target matches case-insensitively
        covered = suggestions.covered_devices(self.conn, suggestions.local_library_index(self.conn))
        self.assertEqual(covered[("boards of canada", "geogaddi")], {self.phone})
        self.assertEqual(covered[("aphex twin", "syro")], {self.dap})
        self.assertNotIn(("boards of canada", "tomorrow's harvest"), covered)

    def test_a_selection_with_no_device_covers_nothing(self):
        self._cover("album", "Boards of Canada||Geogaddi")
        covered = suggestions.covered_devices(self.conn, suggestions.local_library_index(self.conn))
        self.assertNotIn(("boards of canada", "geogaddi"), covered)

    def test_fully_synced_means_every_device_the_caller_manages(self):
        covered = {("a", "b"): {1, 2}}
        self.assertTrue(suggestions.is_fully_synced(covered, ("a", "b"), {1, 2}))
        self.assertFalse(suggestions.is_fully_synced(covered, ("a", "b"), {1, 2, 3}))  # one device still lacks it
        self.assertFalse(suggestions.is_fully_synced(covered, ("x", "y"), {1}))
        # No devices to manage is never "fully synced": nothing would ever be suggested.
        self.assertFalse(suggestions.is_fully_synced(covered, ("a", "b"), None))
        self.assertFalse(suggestions.is_fully_synced(covered, ("a", "b"), set()))

    def test_recently_added_is_newest_first_capped_and_drops_the_fully_synced(self):
        self._cover("album", "Aphex Twin||Syro", self.phone, self.dap)
        out = suggestions.recently_added(self.conn, {self.phone, self.dap}, limit=1)
        self.assertEqual([(o["artist"], o["album"]) for o in out], [("Boards of Canada", "Tomorrow's Harvest")])
        self.assertEqual(out[0]["source"], "recent")

    def test_recently_added_widget_keeps_what_was_first_scanned_on_or_after_the_threshold(self):
        # A later rescan of one track does not make an old album new: the
        # album's date is its first scan.
        self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, scanned_at) "
            "VALUES ('Boards of Canada/Geogaddi/late.flac', 'Boards of Canada', 'Geogaddi', 'late', 1, 0, '2026-09-25 10:00:00')",
        )
        out = suggestions.recently_added_widget(self.conn, "2026-09-10")
        self.assertEqual([o["album"] for o in out], ["Syro", "Tomorrow's Harvest"])
        self.assertEqual(out[1]["added_at"], "2026-09-10 10:00:00")  # on the threshold counts

    def test_both_widgets_drop_what_every_device_has_and_stop_at_the_limit(self):
        self._cover("album", "Boards of Canada||Tomorrow's Harvest", self.phone, self.dap)
        self._cover("album", "Aphex Twin||Syro", self.phone, self.dap)
        managed = {self.phone, self.dap}
        added = suggestions.recently_added_widget(self.conn, "2000-01-01", managed, limit=1)
        self.assertEqual([o["album"] for o in added], ["Geogaddi"])  # the two newer ones are on both
        released = suggestions.recently_released_widget(self.conn, "2000-01-01", managed, limit=1)
        self.assertEqual([o["album"] for o in released], ["Geogaddi"])  # the only dated one left

    def test_recently_released_widget_excludes_albums_with_no_release_date(self):
        out = suggestions.recently_released_widget(self.conn, "2000-01-01")
        self.assertEqual([o["album"] for o in out], ["Tomorrow's Harvest", "Geogaddi"])
        self.assertNotIn("Syro", [o["album"] for o in out])
        self.assertEqual(suggestions.recently_released_widget(self.conn, "2010-01-01")[0]["released_at"], "2013-06-05")


# --- lastfm.py --------------------------------------------------------------

class LastfmParsingTests(unittest.TestCase):
    def test_the_largest_image_is_taken(self):
        images = [{"size": "small", "#text": "s"}, {"size": "large", "#text": "l"}, {"size": "extralarge", "#text": "xl"}]
        self.assertEqual(lastfm._album_image_url(images), "xl")
        self.assertEqual(lastfm._album_image_url([{"size": "medium", "#text": "m"}]), "m")

    def test_the_grey_placeholder_is_no_image(self):
        self.assertIsNone(lastfm._album_image_url([{"size": "extralarge", "#text": _PLACEHOLDER}]))
        self.assertIsNone(lastfm._album_image_url([]))
        self.assertIsNone(lastfm._album_image_url(None))  # type: ignore[arg-type]


class LastfmFailureTests(unittest.TestCase):
    """Missing configuration never reaches the network; every failure after
    it degrades to [] or False."""

    def test_no_key_or_no_username_makes_no_request(self):
        with mock.patch.object(lastfm, "LASTFM_API_KEY", ""), mock.patch("requests.get") as get:
            self.assertFalse(lastfm.check_connection("alice"))
            self.assertEqual(lastfm.top_albums("alice"), [])
            self.assertEqual(lastfm.recent_tracks("alice"), [])
            self.assertEqual(lastfm.similar_artists("Autechre"), [])
            self.assertFalse(lastfm.check_connection("", api_key="k"))
            self.assertEqual(lastfm.similar_artists("", api_key="k"), [])
            get.assert_not_called()

    def test_the_app_wide_key_is_used_when_the_user_has_none(self):
        with mock.patch.object(lastfm, "LASTFM_API_KEY", "app-key"), \
                mock.patch("requests.get", return_value=_resp({"topalbums": {"album": []}})) as get:
            lastfm.top_albums("alice")
            self.assertEqual(get.call_args.kwargs["params"]["api_key"], "app-key")

    def test_check_connection_is_false_on_an_error_body_an_http_error_or_no_answer(self):
        with mock.patch("requests.get", return_value=_resp({"error": 6, "message": "User not found"})):
            self.assertFalse(lastfm.check_connection("nobody", api_key="k"))
        with mock.patch("requests.get", return_value=_resp({}, status_code=403, http_error=True)):
            self.assertFalse(lastfm.check_connection("alice", api_key="k"))
        with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
            self.assertFalse(lastfm.check_connection("alice", api_key="k"))
        with mock.patch("requests.get", return_value=_resp({"user": {"name": "alice"}})):
            self.assertTrue(lastfm.check_connection("alice", api_key="k"))

    def test_every_list_call_is_empty_on_an_http_error_or_a_body_that_is_not_json(self):
        for failing in (_resp(http_error=True, status_code=500), _resp(ValueError("not json"))):
            with mock.patch("requests.get", return_value=failing), mock.patch("builtins.print"):
                self.assertEqual(lastfm.top_albums("alice", api_key="k"), [])
                self.assertEqual(lastfm.recent_tracks("alice", api_key="k"), [])
                self.assertEqual(lastfm.similar_artists("Autechre", api_key="k"), [])

    def test_similar_artists_keeps_the_order_and_skips_nameless_entries(self):
        body = {"similarartists": {"artist": [{"name": "Autechre"}, {"name": ""}, {}, {"name": "Plaid"}]}}
        with mock.patch("requests.get", return_value=_resp(body)):
            self.assertEqual(lastfm.similar_artists("Boards of Canada", api_key="k"), ["Autechre", "Plaid"])

    def test_most_played_keeps_last_fms_order_and_skips_half_named_entries(self):
        albums = [
            {"artist": {"name": "Aphex Twin"}, "name": "Syro", "playcount": "40", "image": []},
            {"artist": {"name": ""}, "name": "Untitled", "playcount": "30"},
            {"artist": {"name": "Plaid"}, "name": "Polymer", "playcount": "12"},
        ]
        with mock.patch("requests.get", return_value=_resp({"topalbums": {"album": albums}})):
            out = lastfm.most_played("alice", api_key="k")
        self.assertEqual([(o["album"], o["playcount"]) for o in out], [("Syro", 40), ("Polymer", 12)])


class LastfmSuggestionTests(_LibraryBase):
    def _top(self, *pairs):
        return {"topalbums": {"album": [
            {"artist": {"name": a}, "name": b, "playcount": "7", "image": [{"size": "large", "#text": f"{b}.jpg"}]}
            for a, b in pairs
        ]}}

    def test_only_albums_in_the_library_and_not_everywhere_already_are_suggested(self):
        self._cover("album", "Boards of Canada||Geogaddi", self.phone, self.dap)  # on both: nothing to do
        self._cover("album", "Aphex Twin||Syro", self.phone)                      # on one of two: still suggested
        body = self._top(("boards of canada", "geogaddi"), ("aphex twin", "syro"), ("Not In", "The Library"))
        with mock.patch("requests.get", return_value=_resp(body)):
            out = lastfm.suggestions(self.conn, "alice", api_key="k", user_device_ids={self.phone, self.dap})
        self.assertEqual(len(out), 1)
        s = out[0]
        # The provider's casing is kept for display; the library's is what a selection must target.
        self.assertEqual((s["artist"], s["album"]), ("aphex twin", "syro"))
        self.assertEqual((s["library_artist"], s["library_album"]), ("Aphex Twin", "Syro"))
        self.assertEqual((s["playcount"], s["image_url"], s["source"]), (7, "syro.jpg", "lastfm"))

    def test_no_top_albums_means_no_suggestions_and_no_library_read(self):
        with mock.patch("requests.get", return_value=_resp({"topalbums": {"album": []}})), \
                mock.patch.object(suggestions, "local_library_index") as index:
            self.assertEqual(lastfm.suggestions(self.conn, "alice", api_key="k"), [])
            index.assert_not_called()

    def test_recently_played_skips_the_track_now_playing_and_repeats(self):
        tracks = [
            {"artist": {"#text": "Aphex Twin"}, "album": {"#text": "Syro"}, "@attr": {"nowplaying": "true"}},
            {"artist": {"#text": "boards of canada"}, "album": {"#text": "geogaddi"}, "image": []},
            {"artist": {"#text": "Boards of Canada"}, "album": {"#text": "Geogaddi"}},  # the same album again
            {"artist": {"#text": "Nobody"}, "album": {"#text": "Nothing"}},           # not in the library
            {"artist": {"#text": ""}, "album": {"#text": "Untagged"}},
            "not a dict",
        ]
        with mock.patch("requests.get", return_value=_resp({"recenttracks": {"track": tracks}})):
            out = lastfm.recently_played_suggestions(self.conn, "alice", api_key="k")
        self.assertEqual([(o["artist"], o["album"], o["source"]) for o in out], [("Boards of Canada", "Geogaddi", "lastfm-recent")])

    def test_recently_played_drops_what_every_device_already_has(self):
        self._cover("artist", "Boards of Canada", self.phone)
        tracks = [{"artist": {"#text": "Boards of Canada"}, "album": {"#text": "Geogaddi"}}]
        with mock.patch("requests.get", return_value=_resp({"recenttracks": {"track": tracks}})):
            self.assertEqual(lastfm.recently_played_suggestions(self.conn, "alice", api_key="k", user_device_ids={self.phone}), [])


# --- listenbrainz.py --------------------------------------------------------

class ListenBrainzParsingAndFailureTests(unittest.TestCase):
    def test_cover_art_needs_both_the_release_and_the_image_id(self):
        self.assertEqual(
            listenbrainz._caa_image_url({"caa_release_mbid": "abc", "caa_id": 42}),
            "https://coverartarchive.org/release/abc/42-250.jpg",
        )
        self.assertIsNone(listenbrainz._caa_image_url({"caa_release_mbid": "abc"}))
        self.assertIsNone(listenbrainz._caa_image_url({"caa_id": 42}))

    def test_no_username_makes_no_request(self):
        with mock.patch("requests.get") as get:
            self.assertFalse(listenbrainz.check_connection(""))
            self.assertEqual(listenbrainz.top_release_groups(""), [])
            self.assertEqual(listenbrainz.recent_listens(""), [])
            get.assert_not_called()

    def test_check_connection_is_true_only_on_200(self):
        with mock.patch("requests.get", return_value=_resp(status_code=200)):
            self.assertTrue(listenbrainz.check_connection("alice"))
        with mock.patch("requests.get", return_value=_resp(status_code=404)):
            self.assertFalse(listenbrainz.check_connection("nobody"))
        with mock.patch("requests.get", side_effect=requests.Timeout("slow")):
            self.assertFalse(listenbrainz.check_connection("alice"))

    def test_every_list_call_is_empty_on_an_http_error_or_a_body_that_is_not_json(self):
        for failing in (_resp(http_error=True, status_code=500), _resp(ValueError("not json"))):
            with mock.patch("requests.get", return_value=failing), mock.patch("builtins.print"):
                self.assertEqual(listenbrainz.top_release_groups("alice"), [])
                self.assertEqual(listenbrainz.recent_listens("alice"), [])

    def test_recent_listens_asks_for_at_most_a_hundred(self):
        with mock.patch("requests.get", return_value=_resp({"payload": {"listens": []}})) as get:
            listenbrainz.recent_listens("alice", limit=500)
            self.assertEqual(get.call_args.kwargs["params"]["count"], 100)

    def test_most_played_keeps_the_order_and_carries_cover_art(self):
        groups = [
            {"artist_name": "Aphex Twin", "release_group_name": "Syro", "listen_count": 40, "caa_release_mbid": "m", "caa_id": 1},
            {"artist_name": "Plaid", "release_group_name": "", "listen_count": 30},
            {"artist_name": "Plaid", "release_group_name": "Polymer", "listen_count": 12},
        ]
        with mock.patch("requests.get", return_value=_resp({"payload": {"release_groups": groups}})):
            out = listenbrainz.most_played("alice")
        self.assertEqual([(o["album"], o["playcount"]) for o in out], [("Syro", 40), ("Polymer", 12)])
        self.assertEqual(out[0]["image_url"], "https://coverartarchive.org/release/m/1-250.jpg")
        self.assertIsNone(out[1]["image_url"])


class ListenBrainzSuggestionTests(_LibraryBase):
    def test_only_albums_in_the_library_and_not_everywhere_already_are_suggested(self):
        self._cover("album", "Boards of Canada||Geogaddi", self.phone, self.dap)
        groups = [
            {"artist_name": "Boards of Canada", "release_group_name": "Geogaddi", "listen_count": 9},
            {"artist_name": "aphex twin", "release_group_name": "SYRO", "listen_count": 5},
            {"artist_name": "Not In", "release_group_name": "The Library", "listen_count": 99},
        ]
        with mock.patch("requests.get", return_value=_resp({"payload": {"release_groups": groups}})):
            out = listenbrainz.suggestions(self.conn, "alice", user_device_ids={self.phone, self.dap})
        self.assertEqual([(o["library_artist"], o["library_album"], o["playcount"], o["source"]) for o in out],
                         [("Aphex Twin", "Syro", 5, "listenbrainz")])

    def test_recently_played_dedups_and_uses_the_library_casing(self):
        listens = [
            {"track_metadata": {"artist_name": "boards of canada", "release_name": "geogaddi"}},
            {"track_metadata": {"artist_name": "Boards of Canada", "release_name": "Geogaddi"}},
            {"track_metadata": {"artist_name": "Nobody", "release_name": "Nothing"}},
            {"track_metadata": {"artist_name": "", "release_name": "Untagged"}},
            {"track_metadata": None},
        ]
        with mock.patch("requests.get", return_value=_resp({"payload": {"listens": listens}})):
            out = listenbrainz.recently_played_suggestions(self.conn, "alice")
        self.assertEqual([(o["artist"], o["album"], o["source"]) for o in out], [("Boards of Canada", "Geogaddi", "listenbrainz-recent")])

    def test_recently_played_drops_what_every_device_already_has(self):
        self._cover("artist", "Boards of Canada", self.phone)
        listens = [{"track_metadata": {"artist_name": "Boards of Canada", "release_name": "Geogaddi"}}]
        with mock.patch("requests.get", return_value=_resp({"payload": {"listens": listens}})):
            self.assertEqual(listenbrainz.recently_played_suggestions(self.conn, "alice", user_device_ids={self.phone}), [])

    def test_no_listens_means_no_suggestions(self):
        with mock.patch("requests.get", return_value=_resp({"payload": {"listens": []}})):
            self.assertEqual(listenbrainz.recently_played_suggestions(self.conn, "alice"), [])
        with mock.patch("requests.get", return_value=_resp({"payload": {"release_groups": []}})):
            self.assertEqual(listenbrainz.suggestions(self.conn, "alice"), [])


if __name__ == "__main__":
    unittest.main()
