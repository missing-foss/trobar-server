#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""device_plays: off by default, stored once, and only for the device's owner.

    python3 -m unittest test_device_plays -v
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import db
import device_plays as dp
import sync_state


class DevicePlaysTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("DATA_DIR", Path(tmp.name)), ("DB_PATH", Path(tmp.name) / "t.db")):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)
        self.user = sync_state._new_id(self.conn.execute("INSERT INTO users (username) VALUES ('a')"))
        self.other = sync_state._new_id(self.conn.execute("INSERT INTO users (username) VALUES ('b')"))
        self.track = sync_state._new_id(self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('a/1.flac', 'Air', 'Moon Safari', 'La Femme', 1, 0)"))
        self.conn.commit()
        self.when = int(time.time()) - 60

    def play(self, **over):
        return {"track_id": self.track, "played_at": self.when, **over}

    def test_off_by_default_stores_nothing(self):
        self.assertFalse(dp.is_enabled(self.conn, self.user))
        result = dp.record(self.conn, None, self.user, [self.play()])
        self.assertEqual(result, {"enabled": False, "stored": 0, "duplicates": 0, "unknown": 0, "bad_time": 0})
        self.assertEqual(dp.count(self.conn, self.user), 0)

    def test_stored_with_its_own_tags_at_its_time(self):
        dp.set_enabled(self.conn, self.user, True)
        self.assertEqual(dp.record(self.conn, None, self.user, [self.play(played_at=1700000000)])["stored"], 1)
        row = self.conn.execute("SELECT * FROM device_plays").fetchone()
        self.assertEqual((row["user_id"], row["track_id"], row["artist"], row["title"], row["album"], row["played_at"]),
                         (self.user, self.track, "Air", "La Femme", "Moon Safari", "2023-11-14 22:13:20"))

    def test_a_retry_stores_nothing_twice(self):
        dp.set_enabled(self.conn, self.user, True)
        dp.record(self.conn, None, self.user, [self.play()])
        result = dp.record(self.conn, None, self.user, [self.play(), self.play(played_at=self.when + 1)])
        self.assertEqual((result["stored"], result["duplicates"]), (1, 1))
        self.assertEqual(dp.count(self.conn, self.user), 2)

    def test_the_same_play_by_two_people_is_two_plays(self):
        for user in (self.user, self.other):
            dp.set_enabled(self.conn, user, True)
            self.assertEqual(dp.record(self.conn, None, user, [self.play()])["stored"], 1)

    def test_unknown_tracks_and_impossible_times_are_counted_and_dropped(self):
        dp.set_enabled(self.conn, self.user, True)
        result = dp.record(self.conn, None, self.user, [
            self.play(track_id=99999), self.play(played_at=100),
            self.play(played_at=int(time.time()) + 2 * 86400), self.play(played_at=int(time.time()) + 3600)])
        self.assertEqual(result, {"enabled": True, "stored": 1, "duplicates": 0, "unknown": 1, "bad_time": 2})

    def test_off_again_keeps_what_is_stored_and_clear_is_one_person(self):
        for user in (self.user, self.other):
            dp.set_enabled(self.conn, user, True)
            dp.record(self.conn, None, user, [self.play()])
        dp.set_enabled(self.conn, self.user, False)
        self.assertEqual(dp.record(self.conn, None, self.user, [self.play(played_at=self.when + 1)])["stored"], 0)
        self.assertEqual(dp.count(self.conn, self.user), 1)
        self.assertEqual(dp.clear(self.conn, self.user), 1)
        self.assertEqual((dp.count(self.conn, self.user), dp.count(self.conn, self.other)), (0, 1))

    def test_a_track_leaving_the_library_leaves_the_play(self):
        dp.set_enabled(self.conn, self.user, True)
        dp.record(self.conn, None, self.user, [self.play()])
        self.conn.execute("DELETE FROM tracks WHERE id = ?", (self.track,))
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT title FROM device_plays").fetchone()[0], "La Femme")

    def test_removing_the_person_removes_their_plays(self):
        dp.set_enabled(self.conn, self.user, True)
        dp.record(self.conn, None, self.user, [self.play()])
        self.conn.execute("DELETE FROM users WHERE id = ?", (self.user,))
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM device_plays").fetchone()[0], 0)

    def test_valid_plays(self):
        self.assertTrue(dp.valid_plays([]))
        self.assertTrue(dp.valid_plays([self.play()]))
        cases: list[object] = [None, {}, "x", [1], [{"track_id": 1}], [{"track_id": "1", "played_at": 1}],
                               [{"track_id": True, "played_at": 1}], [{"track_id": 1, "played_at": 1.5}],
                               [{"track_id": 2 ** 63, "played_at": 1}], [{"track_id": 1, "played_at": -2 ** 63}],
                               [self.play()] * (dp.PLAYS_MAX + 1)]
        for bad in cases:
            self.assertFalse(dp.valid_plays(bad), bad)

    def add(self, artist, album, n=1, when=None):
        track = sync_state._new_id(self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) VALUES (?, ?, ?, 't', 1, 0)",
            (f"{artist}/{album}/{self.when}{self.conn.total_changes}.flac", artist, album)))
        dp.set_enabled(self.conn, self.user, True)
        dp.record(self.conn, None, self.user, [
            {"track_id": track, "played_at": (when or self.when) - i} for i in range(n)])
        return track

    def test_most_played_counts_albums_in_the_period_with_the_librarys_names(self):
        self.add("Beck", "Odelay", 2)
        self.add("Caribou", "Swim", 3)
        self.add("Old", "Gone", 9, when=int(time.time()) - 20 * 86400)
        self.conn.execute("UPDATE device_plays SET artist = 'BECK' WHERE artist = 'Beck'")
        self.conn.commit()
        chart = dp.most_played(self.conn, self.user, "7day")
        self.assertEqual([(c["artist"], c["album"], c["playcount"]) for c in chart],
                         [("Caribou", "Swim", 3), ("Beck", "Odelay", 2)])
        self.assertEqual(dp.most_played(self.conn, self.user, "overall")[0]["album"], "Gone")
        self.assertEqual(len(dp.most_played(self.conn, self.user, "overall", limit=1)), 1)

    def test_of_two_albums_played_as_often_the_latest_comes_first(self):
        self.add("Beck", "Odelay", when=self.when - 500)
        self.add("Caribou", "Swim")
        self.add("Air", "Talkie", when=self.when - 900)
        self.assertEqual([c["album"] for c in dp.most_played(self.conn, self.user, "overall")],
                         ["Swim", "Odelay", "Talkie"])

    def test_a_person_sees_their_own_plays(self):
        self.add("Beck", "Odelay")
        self.assertEqual(dp.most_played(self.conn, self.other, "overall"), [])
        self.assertEqual(dp.recently_played_suggestions(self.conn, self.other), [])
        self.assertEqual(dp.suggestions(self.conn, self.other), [])

    def test_a_play_without_an_album_counts_for_none(self):
        self.add("Beck", "")
        self.assertEqual(dp.most_played(self.conn, self.user, "overall"), [])
        self.assertEqual(dp.recently_played_suggestions(self.conn, self.user), [])

    def test_suggestions_are_library_albums_not_on_every_device(self):
        self.add("Beck", "Odelay", 2)
        self.add("Gone", "Away")
        self.conn.execute("UPDATE tracks SET deleted_at = '2026-01-01 00:00:00' WHERE artist = 'Gone'")
        device, _ = sync_state.create_device(self.conn, self.user, "phone")
        self.conn.commit()
        got = dp.suggestions(self.conn, self.user, user_device_ids={device})
        self.assertEqual([(s["artist"], s["playcount"], s["source"]) for s in got], [("Beck", 2, "device_plays")])
        sync_state.create_selection(self.conn, "album", "Beck||Odelay", self.user, [device])
        self.conn.commit()
        self.assertEqual(dp.suggestions(self.conn, self.user, user_device_ids={device}), [])
        self.assertEqual(dp.recently_played_suggestions(self.conn, self.user, user_device_ids={device}), [])

    def test_recent_is_newest_first_once_each(self):
        self.add("Beck", "Odelay", 2, when=self.when - 500)
        self.add("Caribou", "Swim", 1)
        self.assertEqual([(s["album"], s["source"]) for s in dp.recently_played_suggestions(self.conn, self.user)],
                         [("Swim", "device_plays-recent"), ("Odelay", "device_plays-recent")])

    def test_plays_are_a_source_only_while_the_switch_is_on(self):
        self.assertFalse(dp.is_source(self.conn, self.user))
        dp.set_enabled(self.conn, self.user, True)
        self.assertTrue(dp.is_source(self.conn, self.user))
        dp.record(self.conn, None, self.user, [self.play()])
        dp.set_enabled(self.conn, self.user, False)
        self.assertFalse(dp.is_source(self.conn, self.user))
        self.assertEqual(dp.count(self.conn, self.user), 1)
        dp.set_enabled(self.conn, self.user, True)
        self.assertTrue(dp.is_source(self.conn, self.user))


if __name__ == "__main__":
    unittest.main()
