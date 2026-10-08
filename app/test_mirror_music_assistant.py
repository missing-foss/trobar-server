#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Music Assistant mirroring sink (mirror_music_assistant.py), against
an in-memory Music Assistant that behaves as a real 2.9.9 was measured to
(see music_assistant_client's docstring): adds and removes are background
tasks that report success whatever they refused, a playlist holds a track
once, removal positions are 1-based, an unknown playlist answers 500, and
only an admin token may delete a playlist. Payload shapes are the recorded
ones.

    python3 -m unittest test_mirror_music_assistant -v      # from app/
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="trobar-test-mirror-ma-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import mirror_music_assistant  # noqa: E402
import music_assistant_client as ma  # noqa: E402
import playlist_sync  # noqa: E402
import sync_state  # noqa: E402

URL = "http://ma.example.invalid:8095"


class FakeMusicAssistant:
    """Just enough of Music Assistant's /api for the sink, as measured."""

    def __init__(self, tracks, admin=True):
        # tracks: [(path, artist, album, title, track_number)]
        self.library = [
            {"item_id": str(n), "uri": f"library://track/{n}", "name": title,
             "artists": [{"name": artist}], "album": {"name": album}, "track_number": no,
             "media_type": "track",
             "provider_mappings": [{"provider_domain": "filesystem_local", "item_id": path}]}
            for n, (path, artist, album, title, no) in enumerate(tracks, start=1)]
        self.playlists: dict[str, dict] = {}
        self.next_id = 100
        self.admin = admin
        self.reachable = True
        self.refuse = set()  # uris an add silently drops, as for "item not found"
        self.fail_reads = 0  # how many coming playlist_tracks calls answer 500 anyway
        self.calls: list[str] = []
        self.tasks: dict[str, dict] = {}

    def _by_uri(self, uri):
        for t in self.library:
            path = t["provider_mappings"][0]["item_id"]
            if uri in (t["uri"], f"filesystem_local://track/{path}") and uri not in self.refuse:
                return t
        return None

    def _task(self):
        tid = f"task-{len(self.tasks) + 1}"
        self.tasks[tid] = {"id": tid, "status": "running"}
        return {"id": tid, "status": "running"}

    def handle(self, command, args):
        """(status, body) for one command."""
        self.calls.append(command)
        if not self.reachable:
            raise ma.requests.ConnectionError("down")
        if command == "music/playlists/count":
            return 200, len(self.playlists)
        if command == "music/tracks/library_items":
            offset, limit = args.get("offset", 0), args.get("limit", 500)
            return 200, self.library[offset:offset + limit]
        if command == "music/playlists/library_items":
            return 200, [{"item_id": pid, "name": p["name"], "is_editable": True,
                          "provider_mappings": [{"provider_domain": "builtin", "item_id": p["name"]}]}
                         for pid, p in self.playlists.items()]
        if command == "music/playlists/create_playlist":
            pid = str(self.next_id)
            self.next_id += 1
            self.playlists[pid] = {"name": args["name"], "entries": []}
            return 200, {"item_id": pid, "provider": "library", "name": args["name"],
                         "owner": "Music Assistant", "is_editable": True}
        if command == "music/playlists/playlist_tracks":
            p = self.playlists.get(str(args["item_id"]))
            if self.fail_reads:
                self.fail_reads -= 1
                return 500, "Internal server error"
            if p is None:
                return 500, "Internal server error"
            return 200, [{**t, "position": i + 1} for i, t in enumerate(p["entries"])]
        if command == "music/playlists/add_playlist_tracks":
            p = self.playlists.get(str(args["db_playlist_id"]))
            if p is not None:
                for uri in args["uris"]:
                    t = self._by_uri(uri)
                    if t is not None and t not in p["entries"]:
                        p["entries"].append(t)
            return 200, self._task()
        if command == "music/playlists/remove_playlist_tracks":
            p = self.playlists.get(str(args["db_playlist_id"]))
            if p is not None:
                drop = {pos - 1 for pos in args["positions_to_remove"]}  # 1-based
                p["entries"] = [t for i, t in enumerate(p["entries"]) if i not in drop]
            return 200, self._task()
        if command == "tasks/get":
            return 200, {"id": args["task_id"], "status": "success"}
        if command == "music/playlists/remove":
            if not self.admin:
                return 403, "Admin access required"
            self.playlists.pop(str(args["item_id"]), None)
            return 200, None
        return 400, {"error": f"unknown command {command}"}

    def post(self, url, json, headers, timeout):
        status, body = self.handle(json["command"], json["args"])
        resp = mock.Mock()
        resp.status_code = status
        if status >= 400:
            # Music Assistant answers errors in plain text, not JSON.
            resp.json.side_effect = ValueError("not JSON")
        else:
            resp.json.return_value = body
        return resp

    def entry_paths(self, pid):
        return [t["provider_mappings"][0]["item_id"] for t in self.playlists[pid]["entries"]]


LIBRARY = [
    ("Aphelion/Parallax (2022)/01 - Apoapsis.flac", "Aphelion", "Parallax", "Apoapsis", 1),
    ("Aphelion/Parallax (2022)/02 - Perigee.flac", "Aphelion", "Parallax", "Perigee", 2),
    ("Glass Meridian/Latitude (2021)/01 - Meridian.flac", "Glass Meridian", "Latitude", "Meridian", 1),
    ("Glass Meridian/Latitude (2021)/02 - Solstice.flac", "Glass Meridian", "Latitude", "Solstice", 2),
]


class _Base(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        self.conn = db.get_conn()
        db.set_config(self.conn, "music_assistant_url", URL)
        db.set_config(self.conn, "music_assistant_token", "t")
        self.conn.commit()
        self.fake = FakeMusicAssistant(LIBRARY)
        patcher = mock.patch.object(ma.requests, "post", side_effect=self.fake.post)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch.object(ma.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def tearDown(self):
        self.conn.close()
        self._db_path.unlink(missing_ok=True)

    def _tracks(self, rows):
        """Local tracks: (relative_path, artist, album, title, track_no)."""
        ids = []
        for path, artist, album, title, no in rows:
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
                "VALUES (?, ?, ?, ?, ?, 1000, 0)", (path, artist, album, title, no))
            ids.append(sync_state._new_id(cur))
        self.conn.commit()
        return ids

    def _playlist(self, title, track_ids, enabled=True):
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, "
            "music_assistant_mirror_enabled, last_synced_at) VALUES (?, 'jellyfin', ?, ?, datetime('now'))",
            (title, f"j-{title}", 1 if enabled else 0))
        pid = sync_state._new_id(cur)
        for pos, tid in enumerate(track_ids):
            self.conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
                "VALUES (?, ?, 'a', 't', ?)", (pid, pos, tid))
        self.conn.commit()
        return pid

    def _row(self, pid):
        return self.conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()

    def _write(self, pid):
        mirror_music_assistant.write_mirror(self.conn, pid)
        self.conn.commit()
        return self._row(pid)


class WriteTests(_Base):
    def test_a_first_write_creates_a_named_copy_with_the_tracks_in_order(self):
        ids = self._tracks([LIBRARY[2], LIBRARY[0], LIBRARY[1]])
        pid = self._playlist("Party Mix", ids)
        row = self._write(pid)
        self.assertIsNone(row["music_assistant_mirror_last_error_code"], row["music_assistant_mirror_last_error"])
        remote = row["music_assistant_mirror_remote_id"]
        self.assertEqual(self.fake.playlists[remote]["name"], "Party Mix_Trobar_")
        self.assertEqual(self.fake.entry_paths(remote), [LIBRARY[2][0], LIBRARY[0][0], LIBRARY[1][0]])
        self.assertIsNotNone(row["music_assistant_mirror_last_written_at"])

    def test_a_rerun_with_nothing_changed_writes_nothing(self):
        ids = self._tracks(LIBRARY[:3])
        pid = self._playlist("Party Mix", ids)
        self._write(pid)
        self.fake.calls.clear()
        row = self._write(pid)
        self.assertIsNone(row["music_assistant_mirror_last_error_code"])
        writes = {"music/playlists/add_playlist_tracks", "music/playlists/remove_playlist_tracks",
                  "music/playlists/create_playlist"}
        self.assertEqual([c for c in self.fake.calls if c in writes], [])

    def test_a_changed_playlist_replaces_the_copys_tracks_in_place(self):
        ids = self._tracks(LIBRARY)
        pid = self._playlist("Party Mix", ids[:3])
        remote = self._write(pid)["music_assistant_mirror_remote_id"]
        self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (pid,))
        for pos, tid in enumerate([ids[3], ids[0]]):
            self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, artist, title, "
                              "matched_track_id) VALUES (?, ?, 'a', 't', ?)", (pid, pos, tid))
        row = self._write(pid)
        self.assertEqual(row["music_assistant_mirror_remote_id"], remote)
        self.assertEqual(self.fake.entry_paths(remote), [LIBRARY[3][0], LIBRARY[0][0]])

    def test_a_refused_track_is_a_readback_mismatch_not_a_clean_write(self):
        ids = self._tracks(LIBRARY[:3])
        pid = self._playlist("Party Mix", ids)
        self.fake.refuse.add("library://track/2")
        row = self._write(pid)
        self.assertEqual(row["music_assistant_mirror_last_error_code"], "readback_mismatch")
        self.assertEqual(row["music_assistant_mirror_last_error"], "1 of 3 tracks missing after the write")
        self.assertIsNone(row["music_assistant_mirror_last_written_at"])

    def test_a_repeated_track_is_mirrored_once(self):
        ids = self._tracks(LIBRARY[:2])
        pid = self._playlist("Loop", [ids[0], ids[1], ids[0]])
        row = self._write(pid)
        self.assertIsNone(row["music_assistant_mirror_last_error_code"])
        self.assertEqual(self.fake.entry_paths(row["music_assistant_mirror_remote_id"]),
                         [LIBRARY[0][0], LIBRARY[1][0]])

    def test_a_track_is_found_at_another_folder_depth_then_by_tags(self):
        # Tags that match nothing: only the trailing folders can find it.
        deeper = ("Music/" + LIBRARY[0][0], "Untagged", "Unknown", "Track 1", 1)
        renamed = ("Elsewhere/Meridian.flac",) + LIBRARY[2][1:]
        ids = self._tracks([deeper, renamed])
        row = self._write(self._playlist("Moved", ids))
        self.assertIsNone(row["music_assistant_mirror_last_error_code"])
        self.assertEqual(self.fake.entry_paths(row["music_assistant_mirror_remote_id"]),
                         [LIBRARY[0][0], LIBRARY[2][0]])

    def test_tracks_music_assistant_lacks_are_left_out(self):
        ids = self._tracks([LIBRARY[0], ("Other/Album/09 - Absent.flac", "Other", "Album", "Absent", 9)])
        row = self._write(self._playlist("Half", ids))
        self.assertIsNone(row["music_assistant_mirror_last_error_code"])
        self.assertEqual(self.fake.entry_paths(row["music_assistant_mirror_remote_id"]), [LIBRARY[0][0]])

    def test_none_on_the_target_is_reported_and_nothing_created(self):
        ids = self._tracks([("Other/Album/09 - Absent.flac", "Other", "Album", "Absent", 9)])
        row = self._write(self._playlist("Elsewhere", ids))
        self.assertEqual(row["music_assistant_mirror_last_error_code"], "no_target_matches")
        self.assertEqual(self.fake.playlists, {})

    def test_a_copy_deleted_in_music_assistant_is_recreated(self):
        ids = self._tracks(LIBRARY[:2])
        pid = self._playlist("Party Mix", ids)
        old = self._write(pid)["music_assistant_mirror_remote_id"]
        del self.fake.playlists[old]
        row = self._write(pid)
        self.assertNotEqual(row["music_assistant_mirror_remote_id"], old)
        self.assertEqual(self.fake.entry_paths(row["music_assistant_mirror_remote_id"]),
                         [LIBRARY[0][0], LIBRARY[1][0]])

    def test_a_passing_read_error_keeps_the_copy_and_creates_no_duplicate(self):
        ids = self._tracks(LIBRARY[:2])
        pid = self._playlist("Party Mix", ids)
        first = self._write(pid)["music_assistant_mirror_remote_id"]
        self.fake.fail_reads = 1
        row = self._write(pid)
        self.assertEqual(row["music_assistant_mirror_remote_id"], first)
        self.assertEqual(row["music_assistant_mirror_last_error_code"], "write_failed")
        self.assertEqual(list(self.fake.playlists), [first])
        # The next run reads it fine and clears the error.
        row = self._write(pid)
        self.assertEqual(row["music_assistant_mirror_remote_id"], first)
        self.assertIsNone(row["music_assistant_mirror_last_error_code"])

    def test_a_stored_copy_survives_an_outage_with_its_id(self):
        # The run's index was built before the server went away: the copy's
        # read and the playlist listing then both fail, which must not count
        # as "deleted" (forgetting the id would orphan the copy).
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:2]))
        cache: dict = {}
        mirror_music_assistant.write_mirror(self.conn, pid, index_cache=cache)
        first = self._row(pid)["music_assistant_mirror_remote_id"]
        self.fake.reachable = False
        mirror_music_assistant.write_mirror(self.conn, pid, index_cache=cache)
        row = self._row(pid)
        self.assertEqual(row["music_assistant_mirror_remote_id"], first)
        self.assertEqual(row["music_assistant_mirror_last_error_code"], "unreachable")
        self.assertEqual(list(self.fake.playlists), [first])

    def test_an_unreachable_target_is_reported(self):
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:1]))
        self.fake.reachable = False
        self.assertEqual(self._write(pid)["music_assistant_mirror_last_error_code"], "unreachable")

    def test_no_connection_is_unset_target(self):
        db.set_config(self.conn, "music_assistant_token", None)
        self.conn.commit()
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:1]))
        self.assertEqual(self._write(pid)["music_assistant_mirror_last_error_code"], "unset_target")

    def test_a_playlist_without_the_flag_is_not_touched(self):
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:1]), enabled=False)
        row = self._write(pid)
        self.assertIsNone(row["music_assistant_mirror_remote_id"])
        self.assertEqual(self.fake.calls, [])

    def test_one_index_serves_a_whole_sync_run(self):
        ids = self._tracks(LIBRARY[:2])
        a, b = self._playlist("A", ids[:1]), self._playlist("B", ids[1:])
        cache: dict = {}
        mirror_music_assistant.write_mirror(self.conn, a, index_cache=cache)
        mirror_music_assistant.write_mirror(self.conn, b, index_cache=cache)
        self.assertEqual(self.fake.calls.count("music/tracks/library_items"), 1)


class DeleteTests(_Base):
    def test_an_admin_token_deletes_the_copy(self):
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:1]))
        remote = self._write(pid)["music_assistant_mirror_remote_id"]
        mirror_music_assistant.delete_mirror(self.conn, pid)
        self.assertNotIn(remote, self.fake.playlists)
        self.assertIsNone(self._row(pid)["music_assistant_mirror_remote_id"])

    def test_a_user_token_empties_the_copy_it_cannot_delete(self):
        self.fake.admin = False
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:2]))
        remote = self._write(pid)["music_assistant_mirror_remote_id"]
        mirror_music_assistant.delete_mirror(self.conn, pid)
        self.assertEqual(self.fake.playlists[remote]["entries"], [])
        self.assertIsNone(self._row(pid)["music_assistant_mirror_remote_id"])

    def test_removing_the_playlist_removes_its_copy(self):
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:1]))
        remote = self._write(pid)["music_assistant_mirror_remote_id"]
        playlist_sync._remove_playlist_row(self.conn, pid)
        self.assertNotIn(remote, self.fake.playlists)


class LoopGuardTests(_Base):
    def test_the_copies_are_never_listed_as_source_playlists(self):
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:1]))
        self._write(pid)
        mine = self.fake.handle("music/playlists/create_playlist", {"name": "Made in Music Assistant"})[1]
        # A copy whose stored id was lost still carries the suffix.
        self.fake.handle("music/playlists/create_playlist", {"name": "Old copy_Trobar_"})
        listed = ma.list_playlists()
        self.assertEqual(listed["status"], "ok")
        self.assertEqual([p["id"] for p in listed["playlists"]], [mine["item_id"]])

    def test_a_copy_renamed_in_music_assistant_is_still_recognised_by_its_id(self):
        pid = self._playlist("Party Mix", self._tracks(LIBRARY[:1]))
        remote = self._write(pid)["music_assistant_mirror_remote_id"]
        self.fake.playlists[remote]["name"] = "Party Mix"
        self.assertEqual(ma.list_playlists()["playlists"], [])


if __name__ == "__main__":
    unittest.main()
