#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for album_lookup.py: which release counts as the album, the
artist + title match, ISRC before artist + title, the lookup cache (and
that a failed request is not cached), the Lidarr gate, and that a found
album makes a gap requestable. MusicBrainz is mocked at the HTTP call.

    python3 -m unittest test_album_lookup -v      # from app/
"""
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import album_lookup
import db
import lidarr_requests


def _release(title, date, status="Official", primary="Album", secondary=None):
    group = {"title": title, "primary-type": primary}
    if secondary:
        group["secondary-types"] = secondary
    release = {"title": title, "status": status, "release-group": group}
    if date is not None:
        release["date"] = date
    return release


def _recording(title, artist, releases, joinphrase=""):
    return {"title": title, "artist-credit": [{"name": artist, "joinphrase": joinphrase}], "releases": releases}


class PickAlbumTests(unittest.TestCase):
    def test_the_earliest_official_studio_album_wins(self):
        recordings = [_recording("Song", "Band", [
            _release("Later Album", "2009-09-09"),
            _release("First Album", "1969-09-26"),
        ])]
        self.assertEqual(album_lookup.pick_album(recordings), "First Album")

    def test_compilations_live_sets_and_soundtracks_are_not_albums(self):
        recordings = [_recording("Song", "Band", [
            _release("Best Of", "1970", secondary=["Compilation"]),
            _release("Live At", "1971", secondary=["Live"]),
            _release("Film", "1972", secondary=["Soundtrack"]),
            _release("The Album", "1990"),
        ])]
        self.assertEqual(album_lookup.pick_album(recordings), "The Album")

    def test_singles_eps_bootlegs_and_promos_are_not_albums(self):
        recordings = [_recording("Song", "Band", [
            _release("Song", "1960", primary="Single"),
            _release("EP", "1961", primary="EP"),
            _release("Bootleg", "1962", status="Bootleg"),
            _release("Promo", "1963", status="Promotion"),
            _release("The Album", "1990"),
        ])]
        self.assertEqual(album_lookup.pick_album(recordings), "The Album")

    def test_an_undated_release_loses_to_a_dated_one(self):
        recordings = [_recording("Song", "Band", [_release("Undated", None), _release("Dated", "2001")])]
        self.assertEqual(album_lookup.pick_album(recordings), "Dated")

    def test_nothing_but_compilations_leaves_the_album_empty(self):
        recordings = [_recording("Song", "Band", [_release("Best Of", "1970", secondary=["Compilation"])])]
        self.assertIsNone(album_lookup.pick_album(recordings))

    def test_the_release_group_title_names_the_album(self):
        release = _release("Abbey Road (Remastered)", "2009")
        release["release-group"]["title"] = "Abbey Road"
        self.assertEqual(album_lookup.pick_album([_recording("Song", "Band", [release])]), "Abbey Road")


class FindAlbumTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(album_lookup, "_search")
        self.search = patcher.start()
        self.addCleanup(patcher.stop)

    def test_isrc_first(self):
        self.search.return_value = [_recording("Song", "Band", [_release("By ISRC", "1990")])]
        self.assertEqual(album_lookup.find_album("Band", "Song", "gbaye0601690"), (True, "By ISRC"))
        self.assertEqual(self.search.call_count, 1)
        self.assertEqual(self.search.call_args.args[0], 'isrc:"GBAYE0601690"')

    def test_an_isrc_with_no_studio_album_falls_back_to_artist_and_title(self):
        self.search.side_effect = [
            [_recording("Song", "Band", [_release("Best Of", "1970", secondary=["Compilation"])])],
            [_recording("Song", "Band", [_release("By Name", "1990")])],
        ]
        self.assertEqual(album_lookup.find_album("Band", "Song", "X1"), (True, "By Name"))
        self.assertEqual(self.search.call_args.args[0],
                         'recording:"Song" AND artist:"Band" AND status:official AND primarytype:album')

    def test_artist_and_title_must_match_exactly(self):
        self.search.return_value = [
            _recording("Song", "Tribute Band", [_release("Tribute", "1980")]),
            _recording("Song (Karaoke Version)", "Band", [_release("Karaoke", "1981")]),
            _recording("Another Song", "Band", [_release("Other", "1982")]),
            _recording("SONG", "band", [_release("The Album", "1990")]),
        ]
        self.assertEqual(album_lookup.find_album("Band", "Song", None), (True, "The Album"))

    def test_a_featured_artist_matches_on_any_credited_name(self):
        recording = {"title": "Duet", "artist-credit": [
            {"name": "Singer", "joinphrase": " feat. "}, {"name": "Guest", "joinphrase": ""}],
            "releases": [_release("Their Album", "2010")]}
        self.search.return_value = [recording]
        self.assertEqual(album_lookup.find_album("Guest", "Duet", None), (True, "Their Album"))
        self.assertEqual(album_lookup.find_album("Singer feat. Guest", "Duet", None), (True, "Their Album"))

    def test_a_credits_list_artist_matches_on_its_performer(self):
        self.search.return_value = [_recording("Cover", "Performer", [_release("Covers", "2002")])]
        self.assertEqual(album_lookup.find_album("Songwriter, Performer", "Cover", None), (True, "Covers"))
        self.assertIn('artist:"Performer"', self.search.call_args.args[0])

    def test_a_remaster_tag_on_the_title_is_ignored(self):
        self.search.return_value = [_recording("Rebel Rebel", "Singer", [_release("Diamond Dogs", "1974")])]
        self.assertEqual(album_lookup.find_album("Singer", "Rebel Rebel (1999 Remaster)", None),
                         (True, "Diamond Dogs"))
        self.assertIn('recording:"Rebel Rebel"', self.search.call_args.args[0])

    def test_query_syntax_in_a_name_is_escaped(self):
        self.search.return_value = []
        album_lookup.find_album('AC/DC', 'Who Made Who?', None)
        self.assertTrue(self.search.call_args.args[0].startswith('recording:"Who Made Who\\?" AND artist:"AC\\/DC"'))

    def test_a_failed_request_is_not_an_answer(self):
        self.search.return_value = None
        self.assertEqual(album_lookup.find_album("Band", "Song", None), (False, None))
        self.assertEqual(album_lookup.find_album("Band", "Song", "X1"), (False, None))


class RunJobTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-album-lookup-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._orig = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.addCleanup(self._restore)
        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)
        for key, value in (("lidarr_url", "http://lidarr.local"), ("lidarr_api_key", "key1"),
                           ("lidarr_root_folder_path", "/music"), ("lidarr_quality_profile_id", "1"),
                           ("lidarr_metadata_profile_id", "2")):
            db.set_config(self.conn, key, value)
        self.conn.commit()
        patcher = mock.patch.object(album_lookup, "find_album", return_value=(True, "Found Album"))
        self.find = patcher.start()
        self.addCleanup(patcher.stop)

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._orig

    def _playlist(self, enabled=False) -> int:
        cur = self.conn.execute("INSERT INTO playlists (title, lidarr_request_enabled) VALUES ('P', ?)",
                                (1 if enabled else 0,))
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _gap(self, playlist_id, artist="Band", title="Song", album="", isrc=None, excluded=False) -> int:
        cur = self.conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album, isrc, excluded) "
            "VALUES (?, 0, ?, ?, ?, ?, ?)", (playlist_id, artist, title, album, isrc, 1 if excluded else 0))
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _inferred(self, gap_id):
        return self.conn.execute("SELECT inferred_album FROM unresolved_playlist_tracks WHERE id = ?",
                                 (gap_id,)).fetchone()[0]

    def test_fills_in_the_album_of_a_gap_without_one(self):
        gap = self._gap(self._playlist())
        result = album_lookup.run_job()
        self.assertEqual(self._inferred(gap), "Found Album")
        self.assertEqual(result["asked"], 1)

    def test_leaves_gaps_with_an_album_and_excluded_ones_alone(self):
        p = self._playlist()
        with_album = self._gap(p, album="Their Own")
        excluded = self._gap(p, title="Other", excluded=True)
        album_lookup.run_job()
        self.find.assert_not_called()
        self.assertIsNone(self._inferred(with_album))
        self.assertIsNone(self._inferred(excluded))

    def test_asks_once_for_the_same_gap_in_two_playlists(self):
        a, b = self._gap(self._playlist()), self._gap(self._playlist())
        album_lookup.run_job()
        self.assertEqual(self.find.call_count, 1)
        self.assertEqual((self._inferred(a), self._inferred(b)), ("Found Album", "Found Album"))

    def test_an_answer_is_never_asked_again_even_when_it_found_nothing(self):
        self.find.return_value = (True, None)
        p = self._playlist()
        self._gap(p)
        album_lookup.run_job()
        self._gap(self._playlist())  # the same gap, synced into another playlist later
        album_lookup.run_job()
        self.assertEqual(self.find.call_count, 1)

    def test_a_failed_request_is_asked_again_next_run(self):
        self.find.return_value = (False, None)
        gap = self._gap(self._playlist())
        album_lookup.run_job()
        self.find.return_value = (True, "Found Album")
        album_lookup.run_job()
        self.assertEqual(self.find.call_count, 2)
        self.assertEqual(self._inferred(gap), "Found Album")

    def test_does_nothing_while_lidarr_is_not_configured(self):
        db.set_config(self.conn, "lidarr_metadata_profile_id", None)
        self.conn.commit()
        gap = self._gap(self._playlist())
        self.assertEqual(album_lookup.run_job(), {"skipped": "lidarr_not_configured"})
        self.find.assert_not_called()
        self.assertIsNone(self._inferred(gap))

    def test_a_found_album_is_requested_from_lidarr_at_once_where_requests_are_on(self):
        p = self._playlist(enabled=True)
        self._gap(p, artist="Band")
        with mock.patch.object(lidarr_requests, "_attempt_one",
                               return_value=("requested", None, 1, 2)) as attempt:
            album_lookup.run_job()
        attempt.assert_called_once_with("Band", "Found Album")

    def test_a_playlist_with_requests_off_requests_nothing(self):
        self._gap(self._playlist(enabled=False))
        with mock.patch.object(lidarr_requests, "_attempt_one") as attempt:
            album_lookup.run_job()
        attempt.assert_not_called()

    def test_the_source_album_wins_over_a_looked_up_one_for_lidarr(self):
        p = self._playlist(enabled=True)
        gap = self._gap(p, album="Source Album")
        self.conn.execute("UPDATE unresolved_playlist_tracks SET inferred_album = 'Other' WHERE id = ?", (gap,))
        self.conn.commit()
        with mock.patch.object(lidarr_requests, "_attempt_one",
                               return_value=("requested", None, 1, 2)) as attempt:
            lidarr_requests.run_for_playlist(self.conn, p)
        attempt.assert_called_once_with("Band", "Source Album")

    def test_enqueue_if_pending_queues_one_job_only_for_a_playlist_with_a_pending_gap(self):
        done, pending = self._playlist(), self._playlist()
        self._gap(done, album="Has One")
        self._gap(pending)
        album_lookup.enqueue_if_pending(self.conn, done)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        album_lookup.enqueue_if_pending(self.conn, pending)
        album_lookup.enqueue_if_pending(self.conn, pending)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = ?", (album_lookup.JOB_TYPE,)).fetchone()[0], 1)

    def test_enqueue_queues_nothing_while_lidarr_is_not_configured(self):
        db.set_config(self.conn, "lidarr_url", None)
        self.conn.commit()
        self._gap(self._playlist())
        album_lookup.enqueue(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)


class SearchTests(unittest.TestCase):
    """The HTTP call itself: identifying User-Agent, rate limit, and a
    failure reported as None rather than raised."""

    def setUp(self):
        patcher = mock.patch.object(album_lookup.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_sends_the_query_with_an_identifying_user_agent_after_waiting(self):
        response = mock.Mock()
        response.json.return_value = {"recordings": [{"title": "Song"}]}
        with mock.patch.object(album_lookup.requests, "get", return_value=response) as get:
            self.assertEqual(album_lookup._search('isrc:"X1"'), [{"title": "Song"}])
        self.sleep.assert_called_once_with(1.0)
        self.assertEqual(get.call_args.kwargs["params"]["query"], 'isrc:"X1"')
        self.assertIn("Trobar-Server", get.call_args.kwargs["headers"]["User-Agent"])

    def test_a_failure_is_none(self):
        with mock.patch.object(album_lookup.requests, "get", side_effect=OSError("down")):
            self.assertIsNone(album_lookup._search('isrc:"X1"'))


if __name__ == "__main__":
    unittest.main()
