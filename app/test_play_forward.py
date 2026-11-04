#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""play_forward: new plays only, once each, oldest first, and only while the
person keeps plays and the target is on.

    python3 -m unittest test_play_forward -v
"""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

import credential_store
import db
import device_plays
import jobs
import play_forward as pf
import sync_state


def reply(status=200):
    resp = requests.Response()
    resp.status_code = status
    return resp


class ForwardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("DATA_DIR", Path(tmp.name)), ("DB_PATH", Path(tmp.name) / "t.db")):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TROBAR_CREDENTIAL_KEY_FILE", None)
        db.init_db()
        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)
        self.user = sync_state._new_id(self.conn.execute("INSERT INTO users (username) VALUES ('a')"))
        self.track = sync_state._new_id(self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime) "
            "VALUES ('a/1.flac', 'Air', 'Moon Safari', 'La Femme', 1, 0)"))
        self.conn.commit()
        self.when = int(time.time()) - 1000
        device_plays.set_enabled(self.conn, self.user, True)

    def play(self, n=1, offset=0):
        device_plays.record(self.conn, None, self.user, [
            {"track_id": self.track, "played_at": self.when + offset + i} for i in range(n)])

    def lb(self, on=True):
        self.conn.execute(
            "UPDATE users SET listenbrainz_username = 'a', listenbrainz_token_enc = ? WHERE id = ?",
            (credential_store.encrypt("the-token", self.user, "listenbrainz_token"), self.user))
        self.conn.commit()
        if on:
            pf.set_enabled(self.conn, self.user, "listenbrainz", True)

    def maloja(self, on=True):
        self.conn.execute(
            "UPDATE users SET maloja_url = 'http://8.8.8.8:42010/', maloja_api_key_enc = ? WHERE id = ?",
            (credential_store.encrypt("the-key", self.user, "maloja_api_key"), self.user))
        self.conn.commit()
        if on:
            pf.set_enabled(self.conn, self.user, "maloja", True)

    def run_job(self):
        return pf.run_job({"user_id": self.user}, lambda *a, **k: None)

    def sent_lb(self, post):
        return [li["listened_at"] for call in post.call_args_list for li in call.kwargs["json"]["payload"]]

    def status(self, target="listenbrainz"):
        return pf.status(self.conn, self.user)[target]

    def test_the_turn_on_mark_is_above_everyones_plays(self):
        other = sync_state._new_id(self.conn.execute("INSERT INTO users (username) VALUES ('b')"))
        device_plays.set_enabled(self.conn, other, True)
        device_plays.record(self.conn, None, other, [{"track_id": self.track, "played_at": self.when}])
        self.lb()
        self.play()
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(self.sent_lb(post), [self.when])

    def test_old_plays_are_never_sent_and_new_ones_go_oldest_first(self):
        self.play(2)
        self.lb()
        self.play(2, offset=10)
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(self.sent_lb(post), [self.when + 10, self.when + 11])

    def test_listenbrainz_request(self):
        self.lb()
        self.play()
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.listenbrainz.org/1/submit-listens")
        self.assertEqual(kwargs["headers"], {"Authorization": "Token the-token"})
        self.assertEqual(kwargs["json"]["listen_type"], "import")
        self.assertEqual(kwargs["json"]["payload"], [{"listened_at": self.when, "track_metadata": {
            "artist_name": "Air", "track_name": "La Femme", "release_name": "Moon Safari"}}])

    def test_only_this_persons_plays_go(self):
        other = sync_state._new_id(self.conn.execute("INSERT INTO users (username) VALUES ('b')"))
        device_plays.set_enabled(self.conn, other, True)
        self.lb()
        device_plays.record(self.conn, None, other, [{"track_id": self.track, "played_at": self.when + 50}])
        self.play()
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(self.sent_lb(post), [self.when])

    def test_a_server_error_loses_nothing(self):
        self.lb()
        self.play()
        with mock.patch("play_forward.requests.post", return_value=reply(500)):
            self.run_job()
        self.assertEqual(self.status()["error"], pf.ERROR_UNREACHABLE)
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(self.sent_lb(post), [self.when])

    def test_new_plays_go_after_a_delete(self):
        self.lb()
        self.play(3)
        with mock.patch("play_forward.requests.post", return_value=reply()):
            self.run_job()
        device_plays.clear(self.conn, self.user)
        self.play(2, offset=100)
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(self.sent_lb(post), [self.when + 100, self.when + 101])

    def test_a_play_id_is_never_used_again(self):
        self.play(3)
        highest = self.conn.execute("SELECT MAX(id) FROM device_plays").fetchone()[0]
        device_plays.clear(self.conn, self.user)
        self.play(offset=100)
        self.assertGreater(self.conn.execute("SELECT MAX(id) FROM device_plays").fetchone()[0], highest)

    def maloja_stopped_at(self, n, stop):
        session = mock.MagicMock()
        calls = []

        def post(url, timeout, json):
            calls.append(json["time"])
            if len(calls) == n:
                stop()
            return reply()

        session.__enter__.return_value.post.side_effect = post
        with mock.patch("play_forward.url_guard.session", return_value=session):
            self.run_job()
        return calls

    def test_switching_a_target_off_stops_a_running_send(self):
        self.maloja()
        self.play(5)
        second = db.get_conn()
        self.addCleanup(second.close)
        calls = self.maloja_stopped_at(2, lambda: pf.set_enabled(second, self.user, "maloja", False))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.conn.execute("SELECT last_play_id FROM play_sends").fetchone()[0],
                         self.conn.execute("SELECT id FROM device_plays ORDER BY id LIMIT 1 OFFSET 1").fetchone()[0])

    def test_turning_keeping_off_stops_a_running_send(self):
        self.maloja()
        self.play(5)
        second = db.get_conn()
        self.addCleanup(second.close)
        calls = self.maloja_stopped_at(2, lambda: device_plays.set_enabled(second, self.user, False))
        self.assertEqual(len(calls), 2)

    def test_switching_off_between_batches_stops_listenbrainz(self):
        self.lb()
        self.play(pf._BATCH + 5)
        second = db.get_conn()
        self.addCleanup(second.close)

        def post(*a, **k):
            pf.set_enabled(second, self.user, "listenbrainz", False)
            return reply()

        with mock.patch("play_forward.requests.post", side_effect=post) as sent:
            self.run_job()
        self.assertEqual(sent.call_count, 1)

    def test_maloja_passes_over_a_rejected_play_and_says_so(self):
        self.maloja()
        self.play(3)
        session = mock.MagicMock()
        session.__enter__.return_value.post.side_effect = [reply(400), reply(200), reply(200)]
        with mock.patch("play_forward.url_guard.session", return_value=session):
            self.run_job()
        status = self.status("maloja")
        self.assertEqual((status["error"], status["rejected"]), (None, 1))
        self.assertEqual(session.__enter__.return_value.post.call_count, 3)

    def test_listenbrainz_retries_a_rejected_batch_one_play_at_a_time(self):
        self.lb()
        self.play(3)
        with mock.patch("play_forward.requests.post",
                        side_effect=[reply(400), reply(200), reply(400), reply(200)]) as post:
            self.run_job()
        self.assertEqual([len(c.kwargs["json"]["payload"]) for c in post.call_args_list], [3, 1, 1, 1])
        status = self.status()
        self.assertEqual((status["error"], status["rejected"]), (None, 1))
        with mock.patch("play_forward.requests.post") as again:
            self.run_job()
        again.assert_not_called()

    def test_rejections_add_up_until_the_target_is_turned_on_again(self):
        self.lb()
        for i in range(2):
            self.play(offset=10 * i)
            with mock.patch("play_forward.requests.post", return_value=reply(400)):
                self.run_job()
        self.assertEqual(self.status()["rejected"], 2)
        pf.set_enabled(self.conn, self.user, "listenbrainz", False)
        pf.set_enabled(self.conn, self.user, "listenbrainz", True)
        self.assertEqual(self.status()["rejected"], 0)

    def test_switching_off_stops_the_one_at_a_time_retry(self):
        self.lb()
        self.play(3)
        second = db.get_conn()
        self.addCleanup(second.close)
        answers = [reply(400), reply(200)]

        def post(*a, **k):
            if len(answers) == 1:
                pf.set_enabled(second, self.user, "listenbrainz", False)
            return answers.pop(0)

        with mock.patch("play_forward.requests.post", side_effect=post) as sent:
            self.run_job()
        self.assertEqual(sent.call_count, 2)

    def test_being_told_to_slow_down_is_not_a_rejection(self):
        self.lb()
        self.play()
        with mock.patch("play_forward.requests.post", return_value=reply(429)):
            self.run_job()
        status = self.status()
        self.assertEqual((status["error"], status["rejected"]), (pf.ERROR_UNREACHABLE, 0))
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(post.call_count, 1)

    def test_a_play_goes_once(self):
        self.lb()
        self.play()
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
            self.run_job()
        self.assertEqual(post.call_count, 1)

    def test_a_failed_send_tries_again_from_where_it_stopped(self):
        self.lb()
        self.play()
        with mock.patch("play_forward.requests.post", side_effect=requests.ConnectionError):
            with self.assertLogs("play_forward", level="WARNING"):
                self.run_job()
        self.assertEqual(self.status()["error"], pf.ERROR_UNREACHABLE)
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(self.sent_lb(post), [self.when])
        self.assertIsNone(self.status()["error"])

    def test_a_refused_credential_says_so_and_loses_nothing(self):
        self.lb()
        self.play()
        with mock.patch("play_forward.requests.post", return_value=reply(401)):
            self.run_job()
        self.assertEqual(self.status()["error"], pf.ERROR_REFUSED)
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(post.call_count, 1)

    def test_batches_cover_a_long_backlog(self):
        self.lb()
        self.play(pf._BATCH + 5)
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(self.sent_lb(post)), pf._BATCH + 5)

    def test_nothing_goes_while_keeping_is_off(self):
        self.lb()
        self.play()
        device_plays.set_enabled(self.conn, self.user, False)
        with mock.patch("play_forward.requests.post") as post:
            self.run_job()
        post.assert_not_called()

    def test_a_target_switched_off_sends_nothing(self):
        self.lb()
        self.play()
        pf.set_enabled(self.conn, self.user, "listenbrainz", False)
        with mock.patch("play_forward.requests.post") as post:
            self.run_job()
        post.assert_not_called()

    def test_turning_a_target_back_on_starts_at_the_newest_play(self):
        self.lb()
        pf.set_enabled(self.conn, self.user, "listenbrainz", False)
        self.play()
        pf.set_enabled(self.conn, self.user, "listenbrainz", True)
        with mock.patch("play_forward.requests.post") as post:
            self.run_job()
        post.assert_not_called()

    def test_switching_on_twice_keeps_what_is_waiting(self):
        self.lb()
        self.play()
        pf.set_enabled(self.conn, self.user, "listenbrainz", True)
        with mock.patch("play_forward.requests.post", return_value=reply()) as post:
            self.run_job()
        self.assertEqual(post.call_count, 1)

    def test_an_unreadable_credential_sends_nothing_and_says_not_ready(self):
        self.lb()
        self.play()
        (db.DATA_DIR / "credential_key").unlink()
        self.assertFalse(self.status()["ready"])
        with mock.patch("play_forward.requests.post") as post:
            self.run_job()
        post.assert_not_called()

    def test_maloja_request_and_a_play_it_already_has(self):
        self.maloja()
        self.play(2)
        session = mock.MagicMock()
        session.__enter__.return_value.post.side_effect = [reply(409), reply(200)]
        with mock.patch("play_forward.url_guard.session", return_value=session):
            self.run_job()
        post = session.__enter__.return_value.post
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], "http://8.8.8.8:42010/apis/mlj_1/newscrobble")
        self.assertEqual(post.call_args_list[0].kwargs["json"], {
            "key": "the-key", "title": "La Femme", "artist": "Air", "album": "Moon Safari", "time": self.when})
        self.assertIsNone(self.status("maloja")["error"])

    def test_maloja_stopping_partway_keeps_what_went(self):
        self.maloja()
        self.play(3)
        session = mock.MagicMock()
        session.__enter__.return_value.post.side_effect = [reply(200), reply(500)]
        with mock.patch("play_forward.url_guard.session", return_value=session):
            self.run_job()
        self.assertEqual(self.status("maloja")["error"], pf.ERROR_UNREACHABLE)
        retry = mock.MagicMock()
        retry.__enter__.return_value.post.return_value = reply()
        with mock.patch("play_forward.url_guard.session", return_value=retry):
            self.run_job()
        self.assertEqual([c.kwargs["json"]["time"] for c in retry.__enter__.return_value.post.call_args_list],
                         [self.when + 1, self.when + 2])

    def test_a_refused_maloja_key_says_so(self):
        self.maloja()
        self.play()
        session = mock.MagicMock()
        session.__enter__.return_value.post.return_value = reply(403)
        with mock.patch("play_forward.url_guard.session", return_value=session):
            self.run_job()
        self.assertEqual(self.status("maloja")["error"], pf.ERROR_REFUSED)

    def lfm(self):
        db.set_config(self.conn, "lastfm_app_api_key", "app-key")
        db.set_config(self.conn, pf.APP_SECRET_CONFIG, credential_store.encrypt("app-secret", 0, pf.APP_SECRET_CONFIG))
        self.conn.execute("UPDATE users SET lastfm_session_enc = ? WHERE id = ?",
                          (credential_store.encrypt("the-session", self.user, "lastfm_session"), self.user))
        self.conn.commit()
        pf.set_enabled(self.conn, self.user, "lastfm", True)

    @staticmethod
    def lfm_reply(body, status=200):
        resp = mock.Mock(status_code=status, ok=status < 400)
        resp.json.return_value = body
        return resp

    def accepted(self, ignored=0):
        return self.lfm_reply({"scrobbles": {"@attr": {"accepted": 1, "ignored": ignored}}})

    def test_lastfm_request_is_signed_and_carries_the_play(self):
        self.lfm()
        self.play()
        with mock.patch("play_forward.requests.post", return_value=self.accepted()) as post:
            self.run_job()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://ws.audioscrobbler.com/2.0/")
        data = kwargs["data"]
        self.assertEqual((data["method"], data["api_key"], data["sk"], data["format"]),
                         ("track.scrobble", "app-key", "the-session", "json"))
        self.assertEqual((data["artist[0]"], data["track[0]"], data["album[0]"], data["timestamp[0]"]),
                         ("Air", "La Femme", "Moon Safari", self.when))
        unsigned = {k: v for k, v in data.items() if k != "api_sig"}
        self.assertEqual(data["api_sig"], pf.lastfm.api_signature(unsigned, "app-secret"))
        self.assertNotIn("app-secret", str(data))

    def test_a_play_with_no_album_sends_none_and_stays_signed(self):
        self.lfm()
        self.play()
        self.conn.execute("UPDATE device_plays SET album = NULL")
        self.conn.commit()
        with mock.patch("play_forward.requests.post", return_value=self.accepted()) as post:
            self.run_job()
        data = post.call_args.kwargs["data"]
        self.assertNotIn("album[0]", data)
        unsigned = {k: v for k, v in data.items() if k != "api_sig"}
        self.assertEqual(data["api_sig"], pf.lastfm.api_signature(unsigned, "app-secret"))

    def test_lastfm_takes_fifty_at_a_time(self):
        self.lfm()
        self.play(51)
        with mock.patch("play_forward.requests.post", return_value=self.accepted()) as post:
            self.run_job()
        self.assertEqual(post.call_count, 2)
        self.assertIn("artist[49]", post.call_args_list[0].kwargs["data"])
        self.assertNotIn("artist[50]", post.call_args_list[0].kwargs["data"])

    def test_lastfm_ignoring_a_play_counts_as_rejected(self):
        self.lfm()
        self.play()
        with mock.patch("play_forward.requests.post", return_value=self.accepted(ignored=1)):
            self.run_job()
        self.assertEqual(self.status("lastfm")["rejected"], 1)

    def test_lastfm_error_codes(self):
        for code, expected in ((9, pf.ERROR_REFUSED), (13, pf.ERROR_REFUSED), (11, pf.ERROR_UNREACHABLE),
                               (16, pf.ERROR_UNREACHABLE), (29, pf.ERROR_UNREACHABLE)):
            self.conn.execute("DELETE FROM play_sends")
            self.conn.commit()
            self.lfm()
            self.play(offset=code)
            with mock.patch("play_forward.requests.post", return_value=self.lfm_reply({"error": code}, 403)):
                self.run_job()
            self.assertEqual(self.status("lastfm")["error"], expected, code)

    def test_lastfm_a_rejected_chunk_goes_one_at_a_time(self):
        self.lfm()
        self.play(2)
        with mock.patch("play_forward.requests.post", side_effect=[
                self.lfm_reply({"error": 6}, 400), self.lfm_reply({"error": 6}, 400), self.accepted()]) as post:
            self.run_job()
        self.assertEqual(post.call_count, 3)
        self.assertEqual((self.status("lastfm")["rejected"], self.status("lastfm")["error"]), (1, None))

    def test_switching_off_stops_lastfms_one_at_a_time_retry(self):
        self.lfm()
        self.play(3)
        second = db.get_conn()
        self.addCleanup(second.close)
        answers = [self.lfm_reply({"error": 6}, 400), self.accepted()]

        def post(*a, **k):
            if len(answers) == 1:
                pf.set_enabled(second, self.user, "lastfm", False)
            return answers.pop(0)

        with mock.patch("play_forward.requests.post", side_effect=post) as sent:
            self.run_job()
        self.assertEqual(sent.call_count, 2)

    def test_lastfm_unreachable_loses_nothing(self):
        self.lfm()
        self.play()
        with mock.patch("play_forward.requests.post", side_effect=requests.ConnectionError):
            with self.assertLogs("play_forward", level="WARNING"):
                self.run_job()
        with mock.patch("play_forward.requests.post", return_value=self.accepted()) as post:
            self.run_job()
        self.assertEqual(post.call_count, 1)

    def test_lastfm_needs_the_admins_secret_and_the_persons_session(self):
        self.lfm()
        self.assertTrue(self.status("lastfm")["ready"])
        db.set_config(self.conn, pf.APP_SECRET_CONFIG, None)
        self.conn.commit()
        self.assertFalse(self.status("lastfm")["ready"])
        self.play()
        with mock.patch("play_forward.requests.post") as post:
            self.run_job()
        post.assert_not_called()

    def test_switching_off_stops_lastfm_between_chunks(self):
        self.lfm()
        self.play(pf._LASTFM_BATCH + 5)
        second = db.get_conn()
        self.addCleanup(second.close)

        def post(*a, **k):
            pf.set_enabled(second, self.user, "lastfm", False)
            return self.accepted()

        with mock.patch("play_forward.requests.post", side_effect=post) as sent:
            self.run_job()
        self.assertEqual(sent.call_count, 1)

    def test_status_never_carries_a_credential(self):
        self.lb()
        self.assertNotIn("the-token", str(pf.status(self.conn, self.user)))

    def test_enqueue_only_with_a_target_on(self):
        with mock.patch.object(jobs, "enqueue") as enqueue:
            pf.enqueue_for(self.conn, self.user)
            enqueue.assert_not_called()
            self.lb()
            pf.enqueue_for(self.conn, self.user)
            enqueue.assert_called_once()


class RebuildTests(unittest.TestCase):
    def test_rebuilding_device_plays_keeps_every_row_and_stops_id_reuse(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY); CREATE TABLE devices (id INTEGER PRIMARY KEY); "
                           "INSERT INTO users VALUES (1); INSERT INTO devices VALUES (7);")
        conn.execute("ALTER TABLE users ADD COLUMN device_plays_enabled INTEGER NOT NULL DEFAULT 0")
        db._migrate_device_plays(conn)
        conn.execute("INSERT INTO device_plays (id, user_id, device_id, track_id, artist, title, album, played_at) "
                     "VALUES (5, 1, 7, 9, 'a', 't', 'x', '2026-01-01 00:00:00')")
        db._migrate_play_sends(conn)
        row = conn.execute("SELECT * FROM device_plays").fetchone()
        self.assertEqual(tuple(row), (5, 1, 7, 9, "a", "t", "x", "2026-01-01 00:00:00"))
        self.assertEqual([r["name"] for r in conn.execute("PRAGMA index_list(device_plays)")
                          if r["name"] == "idx_device_plays_user_time"], ["idx_device_plays_user_time"])
        conn.execute("DELETE FROM device_plays")
        new = conn.execute("INSERT INTO device_plays (user_id, track_id, played_at) VALUES (1, 9, 'x')").lastrowid
        self.assertEqual(new, 6)
        db._migrate_play_sends(conn)  # a second run changes nothing


if __name__ == "__main__":
    unittest.main()
