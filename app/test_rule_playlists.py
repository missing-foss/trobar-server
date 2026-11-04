#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""rule_playlists: rules validate, match the library as written, order and
cap the result, and refresh a playlist's tracks.

    python3 -m unittest test_rule_playlists -v
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import rule_playlists as rp


class RuleTests(unittest.TestCase):
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
        self.ids = {}
        for key, path, artist, album, year, dur, added, lossless in (
            ("a", "A/one.flac", "Air", "Moon Safari", 1998, 300, "2026-01-10 10:00:00", 1),
            ("b", "B/two.mp3", "Beck", "Odelay", 1996, 200, "2026-03-05 10:00:00", 0),
            ("c", "C/three.flac", "Caribou", "Swim", 2010, 400, "2026-04-01 10:00:00", 1),
            ("d", "D/four.FLAC", "Daft Punk", "Discovery", 2001, 250, "2026-02-01 10:00:00", 1),
            ("e", "E/five.m4a", "Erykah 100%_", "Mama's Gun", 2000, 500, "2026-05-01 10:00:00", 0),
        ):
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, year, size, mtime, duration, "
                "scanned_at, lossless) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)",
                (path, artist, album, key, year, dur, added, lossless))
            self.ids[key] = cur.lastrowid
        self.conn.commit()

    def match(self, conditions, match="all", **extra):
        rule = rp.validate({"match": match, "conditions": conditions, **extra})
        by_id = {v: k for k, v in self.ids.items()}
        return [by_id[i] for i in rp.track_ids(self.conn, rule)], rp.count(self.conn, rule)

    def test_validate_keeps_only_the_keys_a_rule_has(self):
        rule = rp.validate({
            "match": "all", "junk": 1,
            "conditions": [{"field": "year", "op": "is", "value": 1998, "junk": 2},
                           {"match": "any", "junk": 3,
                            "conditions": [{"field": "artist", "op": "is", "value": "Air", "junk": 4}]}]})
        self.assertEqual(rule, {
            "match": "all", "sort": "added",
            "conditions": [{"field": "year", "op": "is", "value": 1998},
                           {"match": "any", "conditions": [{"field": "artist", "op": "is", "value": "Air"}]}]})

    def test_groups_nest_one_level_as_the_dialog_does(self):
        leaf = {"field": "year", "op": "is", "value": 1998}
        rp.validate({"match": "all", "conditions": [{"match": "any", "conditions": [leaf]}]})
        with self.assertRaises(rp.RuleError):
            rp.validate({"match": "all", "conditions": [
                {"match": "any", "conditions": [{"match": "all", "conditions": [leaf]}]}]})

    def test_year(self):
        self.assertEqual(sorted(self.match([{"field": "year", "op": "between", "value": [2000, 2009]}])[0]),
                         ["d", "e"])
        self.assertEqual(self.match([{"field": "year", "op": "from", "value": 2001}])[1], 2)
        self.assertEqual(self.match([{"field": "year", "op": "until", "value": 1996}])[1], 1)
        self.assertEqual(self.match([{"field": "year", "op": "is", "value": 1998}])[0], ["a"])

    def test_format_ignores_case_and_lossless(self):
        self.assertEqual(sorted(self.match([{"field": "format", "op": "in", "value": ["flac"]}])[0]),
                         ["a", "c", "d"])
        self.assertEqual(sorted(self.match([{"field": "lossless", "op": "is", "value": False}])[0]),
                         ["b", "e"])
        # The whole extension, after its dot: "lac" is not ".flac".
        self.assertEqual(self.match([{"field": "format", "op": "in", "value": ["lac"]}])[1], 0)

    def track(self, path, added):
        cur = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, size, mtime, scanned_at) "
            "VALUES (?, 'x', 'x', ?, 1, 0, " + added + ")", (path, path))
        self.ids[path] = cur.lastrowid
        self.conn.commit()

    def test_format_takes_any_of_its_types_in_any_case(self):
        self.assertEqual(sorted(self.match([{"field": "format", "op": "in", "value": ["flac", "mp3"]}])[0]),
                         ["a", "b", "c", "d"])
        self.assertEqual(self.match([{"field": "format", "op": "in", "value": ["FLAC"]}])[1], 3)
        self.assertEqual(self.match([{"field": "format", "op": "in", "value": [".Flac"]}])[1], 3)

    def test_format_is_the_end_of_the_name(self):
        self.track("F/six.flac.txt", "'2026-01-01 10:00:00'")
        self.assertEqual(self.match([{"field": "format", "op": "in", "value": ["flac"]}])[1], 3)

    def test_added_within_days_looks_back(self):
        self.track("F/recent.flac", "datetime('now', '-2 days')")
        self.track("F/old.flac", "datetime('now', '-40 days')")
        self.assertEqual(self.match([{"field": "added", "op": "within_days", "value": 7}])[0], ["F/recent.flac"])

    def test_added_since_includes_that_moment(self):
        self.track("F/edge.flac", "'2026-06-01'")
        self.assertEqual(self.match([{"field": "added", "op": "since", "value": "2026-06-01"}])[0], ["F/edge.flac"])

    def test_a_boolean_is_not_a_number(self):
        for condition in ({"field": "year", "op": "is", "value": True},
                          {"field": "duration", "op": "at_least", "value": True}):
            with self.assertRaises(rp.RuleError):
                rp.validate({"match": "all", "conditions": [condition]})

    def test_a_field_or_test_that_is_not_text_is_refused(self):
        for condition in ({"field": [], "op": "is", "value": 1}, {"field": "year", "op": {}, "value": 1},
                          {"field": "duration", "op": [], "value": 1}):
            with self.assertRaises(rp.RuleError):
                rp.validate({"match": "all", "conditions": [condition]})

    def test_added(self):
        self.assertEqual(sorted(self.match([{"field": "added", "op": "since", "value": "2026-03-01"}])[0]),
                         ["b", "c", "e"])

    def test_text_match_is_literal(self):
        self.assertEqual(self.match([{"field": "artist", "op": "contains", "value": "100%_"}])[0], ["e"])
        # % and _ match themselves, not anything.
        self.assertEqual(self.match([{"field": "album", "op": "contains", "value": "%"}])[1], 0)
        self.assertEqual(self.match([{"field": "artist", "op": "is", "value": "daft punk"}])[0], ["d"])

    def test_a_value_is_never_sql(self):
        self.assertEqual(self.match([{"field": "artist", "op": "contains", "value": "' OR 1=1 --"}])[1], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 5)

    def test_duration_and_groups(self):
        got, n = self.match([
            {"field": "format", "op": "in", "value": ["flac"]},
            {"match": "any", "conditions": [
                {"field": "duration", "op": "at_most", "value": 260},
                {"field": "artist", "op": "contains", "value": "carib"}]},
        ])
        self.assertEqual((sorted(got), n), (["c", "d"], 2))
        self.assertEqual(self.match([{"field": "year", "op": "is", "value": 1998},
                                     {"field": "year", "op": "is", "value": 1996}], match="any")[1], 2)

    def test_removed_tracks_never_match(self):
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE id = ?", (self.ids["a"],))
        self.conn.commit()
        self.assertNotIn("a", self.match([{"field": "lossless", "op": "is", "value": True}])[0])

    def test_sorts_and_limit(self):
        all_flac = [{"field": "year", "op": "from", "value": 1990}]
        self.assertEqual(self.match(all_flac, sort="added")[0], ["e", "c", "b", "d", "a"])
        self.assertEqual(self.match(all_flac, sort="artist")[0], ["a", "b", "c", "d", "e"])
        self.assertEqual(self.match(all_flac, sort="added", limit=2), (["e", "c"], 5))

    def test_random_is_stable_per_seed(self):
        cond = [{"field": "year", "op": "from", "value": 1990}]
        one = self.match(cond, sort="random", seed=7)[0]
        self.assertEqual(self.match(cond, sort="random", seed=7)[0], one)
        self.assertEqual(sorted(one), ["a", "b", "c", "d", "e"])
        others = {tuple(self.match(cond, sort="random", seed=s)[0]) for s in range(1, 12)}
        self.assertGreater(len(others), 1)
        # A track leaving keeps the others in their order.
        self.conn.execute("UPDATE tracks SET deleted_at = datetime('now') WHERE id = ?", (self.ids[one[0]],))
        self.conn.commit()
        self.assertEqual(self.match(cond, sort="random", seed=7)[0], one[1:])

    def test_validation(self):
        bad = [
            {"match": "some", "conditions": [{"field": "year", "op": "is", "value": 2000}]},
            {"match": "all", "conditions": []},
            {"match": "all", "conditions": [{"field": "genre", "op": "is", "value": "Rock"}]},
            {"match": "all", "conditions": [{"field": "year", "op": "is", "value": "2000"}]},
            {"match": "all", "conditions": [{"field": "year", "op": "between", "value": [2010, 2000]}]},
            {"match": "all", "conditions": [{"field": "format", "op": "in", "value": ["fl ac"]}]},
            {"match": "all", "conditions": [{"field": "added", "op": "since", "value": "March"}]},
            {"match": "all", "conditions": [{"field": "year", "op": "is", "value": 2000}], "sort": "plays"},
            {"match": "all", "conditions": [{"field": "year", "op": "is", "value": 2000}], "limit": 0},
            {"match": "all", "conditions": [{"match": "all", "conditions": [{"match": "all", "conditions": [
                {"match": "all", "conditions": [{"field": "year", "op": "is", "value": 2000}]}]}]}]},
            {"match": "all", "conditions": [{"field": "year", "op": "is", "value": 2000}] * 51},
        ]
        for rule in bad:
            with self.subTest(rule=json.dumps(rule)[:80]):
                with self.assertRaises(rp.RuleError):
                    rp.validate(rule)

    def _rule_playlist(self, rule):
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, owner_user_id, shared, rule) VALUES (?, ?, NULL, 0, ?)",
            (f"Rule {rule}", rp.RULE_SOURCE, json.dumps(rule)))
        self.conn.commit()
        return cur.lastrowid

    def _tracks_of(self, pid):
        return [r[0] for r in self.conn.execute(
            "SELECT matched_track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (pid,))]

    def test_refresh_writes_matched_tracks_in_order(self):
        pid = self._rule_playlist({"match": "all", "sort": "artist",
                                   "conditions": [{"field": "lossless", "op": "is", "value": True}]})
        self.assertEqual(rp.refresh_all(self.conn), {"refreshed": 1, "failed": 0})
        self.assertEqual(self._tracks_of(pid), [self.ids["a"], self.ids["c"], self.ids["d"]])
        # The library changes; the next refresh follows it.
        self.conn.execute("UPDATE tracks SET lossless = 1 WHERE id = ?", (self.ids["b"],))
        self.conn.commit()
        rp.refresh_all(self.conn)
        self.assertEqual(self._tracks_of(pid), [self.ids["a"], self.ids["b"], self.ids["c"], self.ids["d"]])

    def test_a_broken_rule_leaves_its_playlist_as_it_was(self):
        pid = self._rule_playlist({"match": "all", "conditions": [{"field": "lossless", "op": "is", "value": True}]})
        rp.refresh_all(self.conn)
        before = self._tracks_of(pid)
        self.conn.execute("UPDATE playlists SET rule = '{\"match\": \"nope\"}' WHERE id = ?", (pid,))
        self.conn.commit()
        with self.assertLogs("rule_playlists", level="WARNING"):
            self.assertEqual(rp.refresh_all(self.conn), {"refreshed": 0, "failed": 1})
        self.assertEqual(self._tracks_of(pid), before)

    def test_a_rule_that_cannot_be_read_stops_nothing_else(self):
        keep = {"match": "all", "conditions": [{"field": "lossless", "op": "is", "value": True}]}
        bad_json, no_rule = self._rule_playlist(keep), self._rule_playlist({**keep, "sort": "artist"})
        rp.refresh_all(self.conn)
        before = self._tracks_of(bad_json)
        self.conn.execute("UPDATE playlists SET rule = 'not json' WHERE id = ?", (bad_json,))
        self.conn.execute("UPDATE playlists SET rule = NULL WHERE id = ?", (no_rule,))
        good = self._rule_playlist({"match": "all", "conditions": [{"field": "lossless", "op": "is", "value": False}]})
        self.conn.commit()
        with self.assertLogs("rule_playlists", level="WARNING"):
            self.assertEqual(rp.refresh_all(self.conn), {"refreshed": 1, "failed": 2})
        self.assertEqual(self._tracks_of(bad_json), before)
        self.assertEqual(sorted(self._tracks_of(no_rule)), sorted(before))
        self.assertEqual(sorted(self._tracks_of(good)), sorted([self.ids["b"], self.ids["e"]]))


if __name__ == "__main__":
    unittest.main()
