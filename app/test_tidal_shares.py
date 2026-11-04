#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for tidal_shares.py, against a fake Tidal holding both accounts.

    python3 -m unittest test_tidal_shares -v
"""
import itertools
import json
import sqlite3
import unittest
import unittest.mock as mock

import db
import tidal_client
import tidal_shares

WRITE = tidal_client.scopes(True)
READ = tidal_client.SCOPES


class FakeTidal:
    """Playlists keyed by id, each owned by one account token. Records every
    write so tests can tell an append from a rewrite."""

    def __init__(self):
        self.playlists: dict[str, dict] = {}
        self.unavailable: dict[str, set[str]] = {}
        self.writes: list[tuple] = []
        self.drop_next_add = False
        self._ids = itertools.count(1)

    def make(self, token, name, tracks):
        pid = f"pl{next(self._ids)}"
        self.playlists[pid] = {"token": token, "name": name, "items": []}
        for t in tracks:
            self._append(pid, t)
        return pid

    def tracks(self, pid):
        return [t for t, _ in self.playlists[pid]["items"]]

    def _append(self, pid, t):
        self.playlists[pid]["items"].append((t, f"it{next(self._ids)}"))

    def _get(self, token, pid):
        pl = self.playlists.get(pid)
        return pl if pl is not None and pl["token"] == token else None

    # The tidal_client surface tidal_shares uses.
    def get_playlist(self, token, pid):
        pl = self._get(token, pid)
        if pl is None:
            return {"status": "error", "reason": "not_found"}
        owner = "tid-editorial" if pl.get("followed") else f"tid-{pl['token']}"
        return {"status": "ok", "name": pl["name"], "owner_ids": [owner]}

    def get_playlist_items(self, token, pid):
        pl = self._get(token, pid)
        if pl is None:
            return {"status": "error", "reason": "not_found"}
        return {"status": "ok", "items": [{"track_id": t, "item_id": i} for t, i in pl["items"]]}

    def create_playlist(self, token, name):
        self.writes.append(("create", token, name))
        return {"status": "ok", "id": self.make(token, name, [])}

    def rename_playlist(self, token, pid, name):
        self.writes.append(("rename", pid, name))
        self._get(token, pid)["name"] = name
        return {"status": "ok"}

    def add_tracks(self, token, pid, ids, before_item_id=None):
        self.writes.append(("add", pid, list(ids)))
        refused = []
        for t in ids:
            if t in self.unavailable.get(token, set()):
                refused.append(t)
            elif self.drop_next_add:
                self.drop_next_add = False   # accepted, silently not stored
            else:
                self._append(pid, t)
        return {"status": "ok", "not_found": refused}

    def delete_playlist(self, token, pid):
        self.writes.append(("delete", pid))
        del self.playlists[pid]
        return {"status": "ok"}

    def remove_items(self, token, pid, items):
        self.writes.append(("remove", pid, [i["track_id"] for i in items]))
        gone = {i["item_id"] for i in items}
        pl = self._get(token, pid)
        pl["items"] = [(t, i) for t, i in pl["items"] if i not in gone]
        return {"status": "ok"}


class MergeTests(unittest.TestCase):
    def test_both_sides_additions_kept_owner_order_then_recipient_order(self):
        self.assertEqual(tidal_shares.merge_two_way(["a", "b"], ["a", "x", "b"], ["a", "b", "y", "z"]),
                         ["a", "x", "b", "y", "z"])

    def test_a_removal_on_either_side_wins(self):
        self.assertEqual(tidal_shares.merge_two_way(["a", "b", "c"], ["a", "c"], ["a", "b", "c"]), ["a", "c"])
        self.assertEqual(tidal_shares.merge_two_way(["a", "b", "c"], ["a", "b", "c"], ["a", "c"]), ["a", "c"])

    def test_removed_by_one_side_and_moved_by_the_other_is_removed(self):
        self.assertEqual(tidal_shares.merge_two_way(["a", "b", "c"], ["a", "b"], ["c", "a", "b"]), ["a", "b"])

    def test_order_follows_the_owner(self):
        self.assertEqual(tidal_shares.merge_two_way(["a", "b", "c"], ["c", "b", "a"], ["b", "a", "c"]),
                         ["c", "b", "a"])

    def test_the_same_track_added_on_both_sides_appears_once(self):
        self.assertEqual(tidal_shares.merge_two_way(["a"], ["a", "n"], ["a", "n"]), ["a", "n"])

    def test_duplicates_are_counted(self):
        self.assertEqual(tidal_shares.merge_two_way(["a", "a"], ["a", "a"], ["a"]), ["a"])
        self.assertEqual(tidal_shares.merge_two_way(["a"], ["a", "a"], ["a"]), ["a", "a"])

    def test_a_track_unavailable_to_the_recipient_is_not_a_removal(self):
        self.assertEqual(tidal_shares.merge_two_way(["a", "u", "b"], ["a", "u", "b"], ["a", "b"],
                                                    recipient_unavailable=["u"]), ["a", "u", "b"])

    def test_a_track_unavailable_to_the_owner_is_not_a_removal(self):
        self.assertEqual(tidal_shares.merge_two_way(["a", "u"], ["a"], ["a", "u"],
                                                    owner_unavailable=["u"]), ["a", "u"])


class PlanEditsTests(unittest.TestCase):
    def _items(self, ids):
        return [{"track_id": t, "item_id": f"{t}{n}"} for n, t in enumerate(ids)]

    def test_appends_only_when_current_is_a_start_of_desired(self):
        self.assertEqual(tidal_shares.plan_edits(self._items(["a", "b"]), ["a", "b", "c"]), ([], ["c"]))

    def test_surplus_is_removed_and_nothing_else_moves(self):
        removals, additions = tidal_shares.plan_edits(self._items(["a", "x", "b", "a"]), ["a", "b"])
        self.assertEqual(([i["item_id"] for i in removals], additions), (["x1", "a3"], []))

    def test_a_reorder_rewrites_from_the_first_difference(self):
        removals, additions = tidal_shares.plan_edits(self._items(["a", "b", "c", "d"]), ["a", "c", "b", "d"])
        self.assertEqual(([i["track_id"] for i in removals], additions), (["b", "c", "d"], ["c", "b", "d"]))

    def test_equal_lists_need_nothing(self):
        self.assertEqual(tidal_shares.plan_edits(self._items(["a", "a", "b"]), ["a", "a", "b"]), ([], []))


class ShareSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(db.SCHEMA)
        db._run_migrations(self.conn)
        # Migration 1's schema stops before these.
        db._migrate_users_thanks_and_tidal_scopes(self.conn)
        db._migrate_tidal_shares(self.conn)
        db._migrate_tidal_shares_delete_original(self.conn)
        self.owner = self._user("parent", WRITE, "tid-tok-parent")
        self.kid = self._user("kid", WRITE, "tid-tok-kid")
        db.set_config(self.conn, "tidal_playlists_write", "1")
        self.conn.commit()
        self.tokens = {self.owner: "tok-parent", self.kid: "tok-kid"}
        self.fake = FakeTidal()
        for name in ("get_playlist", "get_playlist_items", "create_playlist", "rename_playlist",
                     "add_tracks", "remove_items", "delete_playlist"):
            patcher = mock.patch.object(tidal_client, name, getattr(self.fake, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _user(self, name, scopes, tidal_id):
        cur = self.conn.execute("INSERT INTO users (username, tidal_refresh_token, tidal_scopes, tidal_user_id) "
                                "VALUES (?, 'r', ?, ?)", (name, scopes, tidal_id))
        return cur.lastrowid

    def _share(self, source, two_way=False, status="active"):
        cur = self.conn.execute(
            "INSERT INTO tidal_shares (owner_user_id, recipient_user_id, source_remote_id, title, status, two_way) "
            "VALUES (?, ?, ?, ?, ?, ?)", (self.owner, self.kid, source, self.fake.playlists[source]["name"],
                                         status, 1 if two_way else 0))
        self.conn.commit()
        return cur.lastrowid

    def _row(self, sid):
        return self.conn.execute("SELECT * FROM tidal_shares WHERE id = ?", (sid,)).fetchone()

    def _sync(self):
        return tidal_shares.sync_all(self.conn, self.tokens)

    def _copy(self, sid):
        return self._row(sid)["copy_remote_id"]

    # -- one-way --------------------------------------------------------------

    def test_first_pass_creates_a_copy_with_the_exact_title_and_order(self):
        src = self.fake.make("tok-parent", "Road Trip", ["a", "b", "c"])
        sid = self._share(src)
        self.assertEqual(self._sync(), {"synced": 1, "failed": 0})
        copy = self._copy(sid)
        self.assertEqual(self.fake.playlists[copy]["token"], "tok-kid")
        self.assertEqual(self.fake.playlists[copy]["name"], "Road Trip")
        self.assertEqual(self.fake.tracks(copy), ["a", "b", "c"])
        row = self._row(sid)
        self.assertEqual(json.loads(row["snapshot"]), ["a", "b", "c"])
        self.assertIsNone(row["last_error"])
        self.assertIsNotNone(row["last_synced_at"])

    def test_a_same_named_playlist_of_the_recipient_is_never_touched(self):
        theirs = self.fake.make("tok-kid", "Road Trip", ["z"])
        src = self.fake.make("tok-parent", "Road Trip", ["a"])
        sid = self._share(src)
        self._sync()
        self.assertNotEqual(self._copy(sid), theirs)
        self.assertEqual(self.fake.tracks(theirs), ["z"])
        self.assertFalse([w for w in self.fake.writes if theirs in w])

    def test_one_way_overwrites_the_recipients_edits(self):
        src = self.fake.make("tok-parent", "Mix", ["a", "b"])
        sid = self._share(src)
        self._sync()
        copy = self._copy(sid)
        self.fake._append(copy, "kid-added")
        self.fake.playlists[copy]["items"].reverse()
        self._sync()
        self.assertEqual(self.fake.tracks(copy), ["a", "b"])
        self.assertEqual(self.fake.tracks(src), ["a", "b"])          # owner untouched

    def test_an_owner_append_is_appended_not_rewritten(self):
        src = self.fake.make("tok-parent", "Mix", ["a", "b"])
        sid = self._share(src)
        self._sync()
        self.fake._append(src, "c")
        self.fake.writes.clear()
        self._sync()
        self.assertEqual(self.fake.tracks(self._copy(sid)), ["a", "b", "c"])
        self.assertEqual([w[0] for w in self.fake.writes], ["add"])
        self.assertEqual(self.fake.writes[0][2], ["c"])

    def test_a_track_unavailable_to_the_recipient_is_reported_not_an_error(self):
        self.fake.unavailable["tok-kid"] = {"u"}
        src = self.fake.make("tok-parent", "Mix", ["a", "u", "b"])
        sid = self._share(src)
        self.assertEqual(self._sync(), {"synced": 1, "failed": 0})
        row = self._row(sid)
        self.assertEqual(self.fake.tracks(self._copy(sid)), ["a", "b"])
        self.assertEqual(json.loads(row["unavailable_recipient"]), ["u"])
        self.assertIsNone(row["last_error"])
        # Steady state: the next pass retries it at the end and rewrites
        # nothing around it.
        self.fake.writes.clear()
        self._sync()
        self.assertEqual(self.fake.tracks(self._copy(sid)), ["a", "b"])
        self.assertIsNone(self._row(sid)["last_error"])
        self.assertEqual(self.fake.writes, [("add", self._copy(sid), ["u"])])

    def test_a_track_that_becomes_available_lands_in_its_place(self):
        self.fake.unavailable["tok-kid"] = {"u"}
        src = self.fake.make("tok-parent", "Mix", ["a", "u", "b"])
        sid = self._share(src)
        self._sync()
        self.fake.unavailable["tok-kid"] = set()
        self._sync()
        self.assertEqual(self.fake.tracks(self._copy(sid)), ["a", "u", "b"])
        self.assertEqual(json.loads(self._row(sid)["unavailable_recipient"]), [])

    # -- two-way --------------------------------------------------------------

    def test_two_way_recipient_additions_reach_the_owner_appended(self):
        src = self.fake.make("tok-parent", "Mix", ["a", "b"])
        sid = self._share(src, two_way=True)
        self._sync()
        copy = self._copy(sid)
        self.fake._append(copy, "k1")
        self.fake._append(src, "o1")
        self._sync()
        self.assertEqual(self.fake.tracks(src), ["a", "b", "o1", "k1"])
        self.assertEqual(self.fake.tracks(copy), ["a", "b", "o1", "k1"])

    def test_two_way_a_removal_on_either_side_reaches_both(self):
        src = self.fake.make("tok-parent", "Mix", ["a", "b", "c"])
        sid = self._share(src, two_way=True)
        self._sync()
        copy = self._copy(sid)
        self.fake.playlists[copy]["items"] = [x for x in self.fake.playlists[copy]["items"] if x[0] != "b"]
        self.fake.playlists[src]["items"] = [x for x in self.fake.playlists[src]["items"] if x[0] != "c"]
        self._sync()
        self.assertEqual(self.fake.tracks(src), ["a"])
        self.assertEqual(self.fake.tracks(copy), ["a"])

    def test_two_way_never_deletes_the_owners_track_the_recipient_cannot_have(self):
        self.fake.unavailable["tok-kid"] = {"u"}
        src = self.fake.make("tok-parent", "Mix", ["a", "u", "b"])
        sid = self._share(src, two_way=True)
        for _ in range(3):
            self._sync()
        self.assertEqual(self.fake.tracks(src), ["a", "u", "b"])
        self.assertEqual(self.fake.tracks(self._copy(sid)), ["a", "b"])

    def test_one_way_needs_only_the_recipients_write_scope(self):
        self.conn.execute("UPDATE users SET tidal_scopes = ? WHERE id = ?", (READ, self.owner))
        self.conn.commit()
        src = self.fake.make("tok-parent", "Mix", ["a"])
        one = self._share(src)
        self.assertEqual(self._sync(), {"synced": 1, "failed": 0})
        self.conn.execute("UPDATE tidal_shares SET two_way = 1 WHERE id = ?", (one,))
        self.conn.commit()
        self.assertEqual(self._sync(), {"synced": 0, "failed": 1})
        self.assertEqual(self._row(one)["last_error"], "reconnect_needed")

    def test_two_way_on_a_followed_playlist_writes_nothing(self):
        src = self.fake.make("tok-parent", "Editorial", ["a"])
        self.fake.playlists[src]["followed"] = True
        sid = self._share(src, two_way=True)
        self.assertEqual(self._sync(), {"synced": 0, "failed": 1})
        self.assertEqual(self._row(sid)["last_error"], "two_way_needs_own_playlist")
        self.assertEqual(self.fake.writes, [])
        # One-way only reads the owner's side, so a followed playlist is fine.
        self.conn.execute("UPDATE tidal_shares SET two_way = 0 WHERE id = ?", (sid,))
        self.conn.commit()
        self.assertEqual(self._sync(), {"synced": 1, "failed": 0})

    def test_a_read_only_recipient_gets_no_copy(self):
        self.conn.execute("UPDATE users SET tidal_scopes = NULL WHERE id = ?", (self.kid,))
        self.conn.commit()
        sid = self._share(self.fake.make("tok-parent", "Mix", ["a"]))
        self._sync()
        self.assertEqual(self._row(sid)["last_error"], "reconnect_needed")
        self.assertEqual(self.fake.writes, [])

    # -- titles ---------------------------------------------------------------

    def test_the_title_follows_the_owner_unless_the_recipient_renamed_it(self):
        src = self.fake.make("tok-parent", "Mix", ["a"])
        sid = self._share(src)
        self._sync()
        copy = self._copy(sid)
        self.fake.playlists[src]["name"] = "Summer Mix"
        self._sync()
        self.assertEqual(self.fake.playlists[copy]["name"], "Summer Mix")
        self.fake.playlists[copy]["name"] = "My summer"
        self.fake.playlists[src]["name"] = "Autumn Mix"
        self._sync()
        self.assertEqual(self.fake.playlists[copy]["name"], "My summer")
        self.assertEqual(self._row(sid)["title"], "Autumn Mix")

    # -- loud failures ---------------------------------------------------------

    def test_a_deleted_copy_ends_the_share_and_is_not_recreated(self):
        src = self.fake.make("tok-parent", "Mix", ["a"])
        sid = self._share(src)
        self._sync()
        del self.fake.playlists[self._copy(sid)]
        self.fake.writes.clear()
        with self.assertLogs("tidal_shares", level="WARNING"):
            self._sync()
        row = self._row(sid)
        self.assertEqual((row["status"], row["last_error"]), ("ended", "copy_deleted"))
        self.assertEqual(self.fake.writes, [])
        self._sync()
        self.assertEqual(self.fake.writes, [])

    def test_a_deleted_source_ends_the_share(self):
        src = self.fake.make("tok-parent", "Mix", ["a"])
        sid = self._share(src)
        self._sync()
        del self.fake.playlists[src]
        self._sync()
        self.assertEqual((self._row(sid)["status"], self._row(sid)["last_error"]), ("ended", "source_deleted"))

    def test_a_read_back_mismatch_is_an_error_and_keeps_the_snapshot(self):
        src = self.fake.make("tok-parent", "Mix", ["a"])
        sid = self._share(src)
        self._sync()
        self.fake._append(src, "b")
        self.fake.drop_next_add = True
        # Both rounds lose their add: the copy never matches.
        orig_add = self.fake.add_tracks

        def lossy(token, pid, ids, before_item_id=None):
            self.fake.drop_next_add = True
            return orig_add(token, pid, ids, before_item_id)
        with mock.patch.object(tidal_client, "add_tracks", lossy), \
                self.assertLogs("tidal_shares", level="WARNING") as cm:
            self.assertEqual(self._sync(), {"synced": 0, "failed": 1})
        row = self._row(sid)
        self.assertEqual(row["last_error"], "copy_read_back_mismatch")
        self.assertEqual(row["status"], "active")
        self.assertEqual(json.loads(row["snapshot"]), ["a"])
        self.assertTrue(any("read_back_mismatch" in line for line in cm.output))

    def test_the_copy_id_is_kept_when_the_first_fill_fails(self):
        src = self.fake.make("tok-parent", "Mix", ["a"])
        sid = self._share(src)
        with mock.patch.object(tidal_client, "add_tracks", return_value={"status": "error", "reason": "network"}):
            self._sync()
        copy = self._copy(sid)
        self.assertIsNotNone(copy)
        self.assertEqual(self._row(sid)["last_error"], "copy_network")
        self._sync()
        self.assertEqual(self._copy(sid), copy)
        self.assertEqual([w for w in self.fake.writes if w[0] == "create"], [("create", "tok-kid", "Mix")])
        self.assertEqual(self.fake.tracks(copy), ["a"])

    def test_a_failed_removal_is_reported_and_keeps_the_snapshot(self):
        src = self.fake.make("tok-parent", "Mix", ["a", "b"])
        sid = self._share(src)
        self._sync()
        self.fake.playlists[src]["items"].pop()
        with mock.patch.object(tidal_client, "remove_items", return_value={"status": "error", "reason": "rate_limited"}):
            self.assertEqual(self._sync(), {"synced": 0, "failed": 1})
        row = self._row(sid)
        self.assertEqual(row["last_error"], "copy_rate_limited")
        self.assertEqual(json.loads(row["snapshot"]), ["a", "b"])

    def test_one_failing_share_does_not_stop_the_others(self):
        bad = self._share(self.fake.make("tok-parent", "Bad", ["a"]))
        good = self._share(self.fake.make("tok-parent", "Good", ["b"]))
        real = self.fake.get_playlist

        def boom(token, pid):
            if pid == self._row(bad)["source_remote_id"]:
                raise RuntimeError("unexpected")
            return real(token, pid)
        with mock.patch.object(tidal_client, "get_playlist", boom), \
                self.assertLogs("tidal_shares", level="WARNING"):
            self.assertEqual(self._sync(), {"synced": 1, "failed": 1})
        self.assertEqual(self._row(bad)["last_error"], "internal_error")
        self.assertEqual(self.fake.tracks(self._copy(good)), ["b"])

    # -- deleting the original ------------------------------------------------

    def _handed_over(self, tracks=("a", "b")):
        """A share synced once, then ended, with the original's deletion asked."""
        src = self.fake.make("tok-parent", "Kids", list(tracks))
        sid = self._share(src)
        self._sync()
        self.conn.execute("UPDATE tidal_shares SET status = 'ended', ended_at = datetime('now'), "
                          "delete_original_requested_at = datetime('now') WHERE id = ?", (sid,))
        self.conn.commit()
        self.fake.writes.clear()
        return src, sid

    def test_the_original_is_deleted_and_the_copy_kept(self):
        src, sid = self._handed_over()
        copy = self._copy(sid)
        # Left over from an earlier refusal the owner has since fixed.
        self.conn.execute("UPDATE tidal_shares SET delete_blocked_tracks = '[{\"track_id\": \"x\", "
                          "\"position\": 1}]' WHERE id = ?", (sid,))
        self.conn.commit()
        self._sync()
        self.assertNotIn(src, self.fake.playlists)
        self.assertEqual(self.fake.tracks(copy), ["a", "b"])
        self.assertEqual(self.fake.writes, [("delete", src)])
        self.assertIsNotNone(self._row(sid)["original_deleted_at"])
        self.assertIsNone(self._row(sid)["delete_blocked_tracks"])
        self._sync()
        self.assertEqual(self.fake.writes, [("delete", src)])      # once

    def test_a_copy_missing_a_track_keeps_the_original(self):
        src, sid = self._handed_over()
        copy = self._copy(sid)
        self.fake.playlists[copy]["items"].pop()
        with self.assertLogs("tidal_shares", level="WARNING"):
            self._sync()
        row = self._row(sid)
        self.assertIn(src, self.fake.playlists)
        self.assertEqual(row["last_error"], "copy_lacks_tracks")
        self.assertEqual(json.loads(row["delete_blocked_tracks"]), [{"track_id": "b", "position": 2}])
        self.assertIsNone(row["delete_original_requested_at"])
        self.assertIsNone(row["original_deleted_at"])
        self.assertEqual(self.fake.writes, [])

    def test_a_track_the_copy_has_fewer_times_keeps_the_original(self):
        src, sid = self._handed_over(("a", "b", "a"))
        copy = self._copy(sid)
        items = self.fake.playlists[copy]["items"]
        items.pop(next(n for n in range(len(items) - 1, -1, -1) if items[n][0] == "a"))
        self._sync()
        self.assertIn(src, self.fake.playlists)
        self.assertEqual(self._row(sid)["last_error"], "copy_lacks_tracks")
        # The copy has "a" once: the original's second "a" is the one missing.
        self.assertEqual(json.loads(self._row(sid)["delete_blocked_tracks"]), [{"track_id": "a", "position": 3}])

    def test_a_track_blocked_in_the_recipients_region_keeps_the_original(self):
        # The copy never had it, so deleting the original would lose it.
        self.fake.unavailable["tok-kid"] = {"u"}
        src, sid = self._handed_over(("a", "u"))
        self._sync()
        row = self._row(sid)
        self.assertIn(src, self.fake.playlists)
        self.assertEqual(row["last_error"], "copy_lacks_tracks")
        self.assertEqual(json.loads(row["delete_blocked_tracks"]), [{"track_id": "u", "position": 2}])
        self.assertFalse([w for w in self.fake.writes if w[0] == "delete"])

    def test_a_deleted_copy_keeps_the_original(self):
        src, sid = self._handed_over()
        del self.fake.playlists[self._copy(sid)]
        self._sync()
        self.assertIn(src, self.fake.playlists)
        self.assertEqual(self._row(sid)["last_error"], "copy_missing")

    def test_another_live_share_keeps_the_original(self):
        src, sid = self._handed_over()
        third = self._user("sibling", WRITE, "tid-tok-sib")
        self.conn.execute("INSERT INTO tidal_shares (owner_user_id, recipient_user_id, source_remote_id, title, "
                          "status) VALUES (?, ?, ?, 'Kids', 'pending')", (self.owner, third, src))
        self.conn.commit()
        self._sync()
        self.assertIn(src, self.fake.playlists)
        self.assertEqual(self._row(sid)["last_error"], "original_still_shared")

    def test_a_followed_playlist_is_never_deleted(self):
        src, sid = self._handed_over()
        self.fake.playlists[src]["followed"] = True
        self._sync()
        self.assertIn(src, self.fake.playlists)
        self.assertEqual(self._row(sid)["last_error"], "original_not_owned")

    def test_without_the_write_scope_the_request_waits(self):
        src, sid = self._handed_over()
        self.conn.execute("UPDATE users SET tidal_scopes = ? WHERE id = ?", (READ, self.owner))
        self.conn.commit()
        self._sync()
        row = self._row(sid)
        self.assertIn(src, self.fake.playlists)
        self.assertEqual(row["last_error"], "reconnect_needed")
        self.assertIsNotNone(row["delete_original_requested_at"])

    def test_an_original_already_gone_counts_as_deleted(self):
        src, sid = self._handed_over()
        del self.fake.playlists[src]
        self._sync()
        self.assertIsNotNone(self._row(sid)["original_deleted_at"])
        self.assertEqual(self.fake.writes, [])

    def test_no_request_no_deletion(self):
        src, sid = self._handed_over()
        self.conn.execute("UPDATE tidal_shares SET delete_original_requested_at = NULL WHERE id = ?", (sid,))
        self.conn.commit()
        self._sync()
        self.assertIn(src, self.fake.playlists)
        self.assertEqual(self.fake.writes, [])

    def test_an_active_share_never_deletes_its_original(self):
        src = self.fake.make("tok-parent", "Kids", ["a"])
        sid = self._share(src)
        self.conn.execute("UPDATE tidal_shares SET delete_original_requested_at = datetime('now') WHERE id = ?",
                          (sid,))
        self.conn.commit()
        self._sync()
        self.assertIn(src, self.fake.playlists)
        self.assertFalse([w for w in self.fake.writes if w[0] == "delete"])

    # -- what is not synced ----------------------------------------------------

    def test_nothing_runs_while_sharing_is_off(self):
        self._share(self.fake.make("tok-parent", "Mix", ["a"]))
        db.set_config(self.conn, "tidal_playlists_write", "0")
        self.conn.commit()
        self.assertEqual(self._sync(), {"synced": 0, "failed": 0})
        self.assertEqual(self.fake.writes, [])

    def test_pending_and_ended_shares_write_nothing(self):
        self._share(self.fake.make("tok-parent", "A", ["a"]), status="pending")
        self._share(self.fake.make("tok-parent", "B", ["b"]), status="ended")
        self.assertEqual(self._sync(), {"synced": 0, "failed": 0})
        self.assertEqual(self.fake.writes, [])

    def test_a_user_without_a_token_this_pass_is_not_linked(self):
        sid = self._share(self.fake.make("tok-parent", "Mix", ["a"]))
        del self.tokens[self.kid]
        self._sync()
        self.assertEqual(self._row(sid)["last_error"], "not_linked")
        self.assertEqual(self.fake.writes, [])

    def test_sharing_with_yourself_is_refused_by_the_table(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO tidal_shares (owner_user_id, recipient_user_id, source_remote_id, "
                              "title, status) VALUES (?, ?, 'x', 't', 'active')", (self.owner, self.owner))

    def test_one_live_share_per_playlist_and_recipient(self):
        src = self.fake.make("tok-parent", "Mix", ["a"])
        self._share(src, status="ended")
        self._share(src)
        with self.assertRaises(sqlite3.IntegrityError):
            self._share(src, status="pending")


if __name__ == "__main__":
    unittest.main()
