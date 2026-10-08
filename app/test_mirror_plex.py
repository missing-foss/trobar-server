#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Plex mirroring sink (mirror_plex.py) and plex_client's mirror side,
against an in-memory Plex Media Server that behaves as a real 1.43.4 was
measured to (see plex_client's mirror section): an add appends and skips
tracks already there or unknown, still with a 200; a playlist holds a track
once; there is no replace, only a clear; errors are HTML; playlists share
their keys with tracks; a deleted playlist answers 404 from then on.
Payload shapes are the recorded ones.

    python3 -m unittest test_mirror_plex -v      # from app/
"""
import json
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_TMP = tempfile.mkdtemp(prefix="trobar-test-mirror-plex-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import mirror_plex  # noqa: E402
import playlist_sync  # noqa: E402
import plex_client  # noqa: E402
import sync_state  # noqa: E402

URL = "http://plex.example.invalid:32400"
MACHINE = "d7ff522b777be1a66e7fd502a64a153daee09dcd"

# The target's library: (Plex's file path, artist, album, title, index).
# Plex's music root (/music) differs from Trobar's.
LIBRARY = [
    ("/music/Cinder & Ash/Emberwake/01 Smoulder.flac", "Cinder & Ash", "Emberwake", "Smoulder", 1),
    ("/music/Cinder & Ash/Emberwake/02 Kindling.flac", "Cinder & Ash", "Emberwake", "Kindling", 2),
    ("/music/Cinder & Ash/Emberwake/03 Afterglow.flac", "Cinder & Ash", "Emberwake", "Afterglow", 3),
    ("/music/Orrery/Seven Moons/01 Perihelion.flac", "Orrery", "Seven Moons", "Perihelion", 1),
    ("/music/Velvet Tide/Low Water/01 Undertow.flac", "Velvet Tide", "Low Water", "Undertow", 1),
]


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        if body is None:
            # Errors and empty successes are HTML, whatever Accept says.
            text = {401: "<html><h1>401 Unauthorized</h1></html>",
                    404: "<html><h1>404 Not Found</h1></html>"}.get(status, "")
            self.content = text.encode()
            self.headers = {"Content-Type": "text/html"}
        else:
            self.content = json.dumps(body).encode()
            self.headers = {"Content-Type": "application/json"}

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise plex_client.requests.HTTPError(str(self.status_code))


class FakePlex:
    """Just enough of a Plex Media Server for the sink, as measured."""

    def __init__(self, tracks):
        self.tracks = {}
        for n, (path, artist, album, title, index) in enumerate(tracks, start=3):
            self.tracks[str(n)] = {
                "ratingKey": str(n), "key": f"/library/metadata/{n}", "type": "track",
                "title": title, "grandparentTitle": artist, "parentTitle": album, "index": index,
                "Media": [{"Part": [{"file": path}]}]}
        self.playlists: dict[str, dict] = {}
        self.next_key = 100
        self.next_item = 1
        self.reachable = True
        self.fail_playlist_reads = 0  # coming GETs of a playlist that answer 500
        self.calls: list[tuple[str, str]] = []

    def _keys(self, uri):
        """The keys a uri names that this server has: a wrong machine
        identifier, a garbage uri and unknown keys add nothing."""
        prefix = f"server://{MACHINE}/com.plexapp.plugins.library/library/metadata/"
        if not uri or not uri.startswith(prefix):
            return []
        return [k for k in uri[len(prefix):].split(",") if k in self.tracks]

    def _add(self, playlist, keys):
        added = 0
        for k in keys:
            if k not in [e["key"] for e in playlist["entries"]]:
                playlist["entries"].append({"key": k, "item": self.next_item})
                self.next_item += 1
                added += 1
        return added

    def _meta(self, key):
        p = self.playlists[key]
        return {"ratingKey": key, "type": "playlist", "playlistType": "audio", "smart": False,
                "title": p["title"], "summary": p["summary"], "leafCount": len(p["entries"])}

    def request(self, method, url, headers=None, params=None, timeout=None):
        path = urlsplit(url).path
        params = dict(params or {})
        self.calls.append((method, path))
        if not self.reachable:
            raise plex_client.requests.ConnectionError("down")
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/":
            return _Response(200, {"MediaContainer": {"machineIdentifier": MACHINE}})
        if method == "GET" and path == "/identity":
            return _Response(200, {"MediaContainer": {"machineIdentifier": MACHINE, "version": "1.43.4"}})
        if method == "GET" and path == "/library/sections":
            return _Response(200, {"MediaContainer": {"Directory": [
                {"key": "1", "type": "artist", "title": "Music"},
                {"key": "2", "type": "movie", "title": "Films"}]}})
        if method == "GET" and parts[:2] == ["library", "sections"] and parts[3:] == ["all"]:
            if parts[2] != "1":
                return _Response(200, {"MediaContainer": {"size": 0}})
            items = sorted(self.tracks.values(), key=lambda t: int(t["ratingKey"]))
            start = int(params.get("X-Plex-Container-Start", 0))
            size = int(params.get("X-Plex-Container-Size", len(items)))
            page = items[start:start + size]
            container: dict[str, Any] = {"size": len(page), "totalSize": len(items), "offset": start}
            if page:
                container["Metadata"] = page
            return _Response(200, {"MediaContainer": container})
        if parts[0] != "playlists":
            return _Response(404)
        if method == "POST" and len(parts) == 1:
            if params.get("type") != "audio" or "uri" not in params:
                return _Response(400)
            key = str(self.next_key)
            self.next_key += 1
            self.playlists[key] = {"title": params["title"], "summary": "", "entries": []}
            self._add(self.playlists[key], self._keys(params["uri"]))
            return _Response(200, {"MediaContainer": {"size": 1, "Metadata": [self._meta(key)]}})
        key = parts[1]
        if method == "GET" and self.fail_playlist_reads:
            self.fail_playlist_reads -= 1
            return _Response(500)
        if key not in self.playlists:
            if method == "GET" and key in self.tracks:
                # Playlists share their keys with tracks: the key answers as
                # the track, and its "items" are a 500.
                if len(parts) == 2:
                    return _Response(200, {"MediaContainer": {"size": 1, "Metadata": [self.tracks[key]]}})
                return _Response(500)
            return _Response(404)
        playlist = self.playlists[key]
        if len(parts) == 2:
            if method == "GET":
                return _Response(200, {"MediaContainer": {"size": 1, "Metadata": [self._meta(key)]}})
            if method == "PUT":
                playlist["title"] = params.get("title", playlist["title"])
                playlist["summary"] = params.get("summary", playlist["summary"])
                return _Response(200)
            if method == "DELETE":
                del self.playlists[key]
                return _Response(204)
        if parts[2:] == ["items"]:
            if method == "GET":
                listing: dict[str, Any] = {"size": len(playlist["entries"]),
                                           "leafCount": len(playlist["entries"])}
                if playlist["entries"]:
                    listing["Metadata"] = [dict(self.tracks[e["key"]], playlistItemID=e["item"])
                                             for e in playlist["entries"]]
                return _Response(200, {"MediaContainer": listing})
            if method == "DELETE":
                playlist["entries"] = []
                return _Response(200, {"MediaContainer": {"size": 1, "Metadata": [self._meta(key)]}})
            if method == "PUT":
                keys = self._keys(params.get("uri"))
                added = self._add(playlist, keys)
                return _Response(200, {"MediaContainer": {"size": 1, "leafCountAdded": added,
                                                          "leafCountRequested": len(set(keys)),
                                                          "Metadata": [self._meta(key)]}})
        return _Response(404)

    def keys(self, key):
        return [e["key"] for e in self.playlists[key]["entries"]]

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class _Base(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        self.conn = db.get_conn()
        db.set_config(self.conn, "mirror_plex_url", URL)
        db.set_config(self.conn, "mirror_plex_token", "t")
        self.conn.commit()
        self.fake = FakePlex(LIBRARY)
        patcher = mock.patch.object(plex_client.requests, "request", side_effect=self.fake.request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.conn.close()
        self._db_path.unlink(missing_ok=True)

    def _tracks(self, rows):
        """Local tracks, under Trobar's own root: (relative_path, artist,
        album, title, track_no)."""
        ids = []
        for path, artist, album, title, no in rows:
            cur = self.conn.execute(
                "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
                "VALUES (?, ?, ?, ?, ?, 1000, 0)", (path, artist, album, title, no))
            ids.append(sync_state._new_id(cur))
        self.conn.commit()
        return ids

    def _local(self, n):
        """LIBRARY[n] as Trobar holds it: the same folders under another root."""
        path, artist, album, title, no = LIBRARY[n]
        return ("Library/" + path.removeprefix("/music/"), artist, album, title, no)

    def _playlist(self, title, track_ids, enabled=True):
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, plex_mirror_enabled, "
            "last_synced_at) VALUES (?, 'jellyfin', ?, ?, datetime('now'))",
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

    def _write(self, pid, cache=None):
        mirror_plex.write_mirror(self.conn, pid, index_cache=cache)
        self.conn.commit()
        return self._row(pid)

    def _keys_of(self, *library_rows):
        return [str(n + 3) for n in library_rows]


class WriteTests(_Base):
    def test_a_first_write_creates_a_named_copy_with_the_tracks_in_order(self):
        ids = self._tracks([self._local(2), self._local(0), self._local(3)])
        pid = self._playlist("Road Trip", ids)
        row = self._write(pid)
        self.assertIsNone(row["plex_mirror_last_error_code"])
        self.assertIsNotNone(row["plex_mirror_last_written_at"])
        copy = self.fake.playlists[row["plex_mirror_remote_id"]]
        self.assertEqual(copy["title"], "Road Trip_Trobar_")
        self.assertEqual(copy["summary"], "Trobar mirror — 3 of 3 present, grows with your library")
        self.assertEqual(self.fake.keys(row["plex_mirror_remote_id"]), self._keys_of(2, 0, 3))

    def test_a_rerun_with_nothing_changed_writes_nothing(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0), self._local(1)]))
        self._write(pid)
        self.fake.calls.clear()
        row = self._write(pid)
        self.assertEqual(self.fake.writes(), [])
        self.assertIsNone(row["plex_mirror_last_error_code"])

    def test_a_changed_playlist_replaces_the_copys_tracks_in_place(self):
        a, b, c = self._tracks([self._local(0), self._local(1), self._local(4)])
        pid = self._playlist("Road Trip", [a, b])
        first = self._write(pid)["plex_mirror_remote_id"]
        self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (pid,))
        for pos, tid in enumerate([c, a]):
            self.conn.execute("INSERT INTO playlist_tracks (playlist_id, position, artist, title, "
                              "matched_track_id) VALUES (?, ?, 'a', 't', ?)", (pid, pos, tid))
        self.conn.commit()
        row = self._write(pid)
        self.assertEqual(row["plex_mirror_remote_id"], first, "the same copy, not a new one")
        self.assertEqual(self.fake.keys(first), self._keys_of(4, 0))
        self.assertEqual(len(self.fake.playlists), 1)

    def test_a_renamed_playlist_renames_its_copy(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        key = self._write(pid)["plex_mirror_remote_id"]
        self.conn.execute("UPDATE playlists SET title = 'Long Drive' WHERE id = ?", (pid,))
        self.conn.commit()
        self.fake.calls.clear()
        self._write(pid)
        self.assertEqual(self.fake.playlists[key]["title"], "Long Drive_Trobar_")
        self.assertEqual(self.fake.writes(), [("PUT", f"/playlists/{key}")], "only the rename")

    def test_a_repeated_track_is_mirrored_once(self):
        a, b = self._tracks([self._local(0), self._local(1)])
        pid = self._playlist("Loop", [a, b, a])
        row = self._write(pid)
        self.assertIsNone(row["plex_mirror_last_error_code"])
        self.assertEqual(self.fake.keys(row["plex_mirror_remote_id"]), self._keys_of(0, 1))

    def test_a_track_is_found_by_its_folders_then_by_its_tags(self):
        by_path = self._local(0)
        by_tags = ("Elsewhere/kindling.flac", "Cinder & Ash", "Emberwake", "Kindling", 2)
        pid = self._playlist("Mix", self._tracks([by_path, by_tags]))
        row = self._write(pid)
        self.assertEqual(self.fake.keys(row["plex_mirror_remote_id"]), self._keys_of(0, 1))

    def test_the_folder_match_ignores_case(self):
        path, artist, album, title, no = self._local(3)
        pid = self._playlist("Mix", self._tracks([(path.upper(), "x", "y", "z", None)]))
        self.assertEqual(self.fake.keys(self._write(pid)["plex_mirror_remote_id"]), self._keys_of(3))

    def test_tracks_plex_lacks_are_left_out(self):
        ids = self._tracks([self._local(0), ("Library/Nobody/Nowhere/01 Ghost.flac", "Nobody",
                                             "Nowhere", "Ghost", 1)])
        row = self._write(self._playlist("Mix", ids))
        self.assertIsNone(row["plex_mirror_last_error_code"])
        self.assertEqual(self.fake.keys(row["plex_mirror_remote_id"]), self._keys_of(0))
        self.assertIn("1 of 2 present", self.fake.playlists[row["plex_mirror_remote_id"]]["summary"])

    def test_none_on_the_target_is_reported_and_nothing_created(self):
        ids = self._tracks([("Library/Nobody/Nowhere/01 Ghost.flac", "Nobody", "Nowhere", "Ghost", 1)])
        row = self._write(self._playlist("Mix", ids))
        self.assertEqual(row["plex_mirror_last_error_code"], "no_target_matches")
        self.assertEqual(self.fake.playlists, {})

    def test_an_empty_playlist_is_mirrored_empty(self):
        row = self._write(self._playlist("Empty", []))
        self.assertIsNone(row["plex_mirror_last_error_code"])
        self.assertEqual(self.fake.keys(row["plex_mirror_remote_id"]), [])

    def test_a_dropped_track_is_a_readback_mismatch_not_a_clean_write(self):
        # Plex answers 200 even for a key it doesn't add.
        pid = self._playlist("Mix", self._tracks([self._local(0), self._local(1)]))
        real_add = self.fake._add
        with mock.patch.object(self.fake, "_add", side_effect=lambda playlist, keys: real_add(playlist, keys[:1])):
            row = self._write(pid)
        self.assertEqual(row["plex_mirror_last_error_code"], "readback_mismatch")
        self.assertEqual(row["plex_mirror_last_error"], "1 of 2 tracks missing after the write")
        self.assertIsNone(row["plex_mirror_last_written_at"])

    def test_a_copy_deleted_in_plex_is_recreated(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        first = self._write(pid)["plex_mirror_remote_id"]
        del self.fake.playlists[first]
        row = self._write(pid)
        self.assertNotEqual(row["plex_mirror_remote_id"], first)
        self.assertEqual(self.fake.keys(row["plex_mirror_remote_id"]), self._keys_of(0))

    def test_a_stored_key_now_naming_a_track_is_taken_as_gone(self):
        # Playlists share their keys with tracks: a 200 that isn't a playlist.
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        self._write(pid)
        self.conn.execute("UPDATE playlists SET plex_mirror_remote_id = '4' WHERE id = ?", (pid,))
        self.conn.commit()
        row = self._write(pid)
        self.assertNotEqual(row["plex_mirror_remote_id"], "4")
        self.assertIsNone(row["plex_mirror_last_error_code"])
        self.assertIn("4", self.fake.tracks, "the track itself is untouched")

    def test_a_passing_read_error_keeps_the_copy_and_creates_no_duplicate(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        first = self._write(pid)["plex_mirror_remote_id"]
        self.fake.fail_playlist_reads = 3  # the read, then the existence check
        row = self._write(pid)
        self.assertEqual(row["plex_mirror_remote_id"], first)
        self.assertEqual(row["plex_mirror_last_error_code"], "unreachable")
        self.assertEqual(list(self.fake.playlists), [first])
        # Once Plex answers again, the same copy carries on.
        self.fake.fail_playlist_reads = 0
        row = self._write(pid)
        self.assertEqual((row["plex_mirror_remote_id"], row["plex_mirror_last_error_code"]), (first, None))

    def test_an_unreachable_target_is_reported(self):
        self.fake.reachable = False
        row = self._write(self._playlist("Road Trip", self._tracks([self._local(0)])))
        self.assertEqual(row["plex_mirror_last_error_code"], "unreachable")

    def test_no_connection_is_unset_target(self):
        db.set_config(self.conn, "mirror_plex_token", None)
        self.conn.commit()
        row = self._write(self._playlist("Road Trip", self._tracks([self._local(0)])))
        self.assertEqual(row["plex_mirror_last_error_code"], "unset_target")
        self.assertEqual(self.fake.calls, [])

    def test_a_playlist_without_the_flag_is_not_touched(self):
        row = self._write(self._playlist("Road Trip", self._tracks([self._local(0)]), enabled=False))
        self.assertIsNone(row["plex_mirror_remote_id"])
        self.assertEqual(self.fake.calls, [])

    def test_one_index_serves_a_whole_sync_run(self):
        ids = self._tracks([self._local(0)])
        cache: dict = {}
        self._write(self._playlist("A", ids), cache)
        self._write(self._playlist("B", ids), cache)
        self.assertEqual(self.fake.calls.count(("GET", "/library/sections/1/all")), 1)

    def test_the_index_pages_through_every_track(self):
        with mock.patch.object(plex_client, "_PAGE_SIZE", 2):
            index = plex_client.mirror_build_index()
        assert index is not None
        self.assertEqual(sum(len(v) for v in index["by_tail"].values()), len(LIBRARY))
        self.assertEqual(self.fake.calls.count(("GET", "/library/sections/1/all")), 3)
        self.assertNotIn(("GET", "/library/sections/2/all"), self.fake.calls, "not the film section")
        self.assertEqual(index["machine"], MACHINE)


class DeleteTests(_Base):
    def test_stopping_the_mirror_deletes_the_copy(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        key = self._write(pid)["plex_mirror_remote_id"]
        mirror_plex.delete_mirror(self.conn, pid)
        self.assertNotIn(key, self.fake.playlists)
        self.assertIsNone(self._row(pid)["plex_mirror_remote_id"])

    def test_a_copy_already_gone_is_not_an_error(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        key = self._write(pid)["plex_mirror_remote_id"]
        del self.fake.playlists[key]
        self.assertTrue(plex_client.mirror_delete(key))
        mirror_plex.delete_mirror(self.conn, pid)
        self.assertIsNone(self._row(pid)["plex_mirror_remote_id"])

    def test_removing_the_playlist_removes_its_copy(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        key = self._write(pid)["plex_mirror_remote_id"]
        playlist_sync._remove_playlist_row(self.conn, pid)
        self.conn.commit()
        self.assertNotIn(key, self.fake.playlists)


class OtherServerTests(_Base):
    """Plex keys are small integers numbered per server, so a key is only
    meaningful on the server that holds the copy."""

    def test_a_changed_target_never_touches_the_old_key_there(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        key = self._write(pid)["plex_mirror_remote_id"]
        # The admin points the mirror at another server, where the same key
        # happens to be someone's own playlist.
        theirs = {"title": "Theirs", "summary": "", "entries": [{"key": "4", "item": 900}]}
        self.fake.playlists = {key: theirs}
        self.conn.execute("UPDATE playlists SET plex_mirror_machine = 'the-old-server' WHERE id = ?", (pid,))
        self.conn.commit()
        row = self._write(pid)
        self.assertEqual(self.fake.playlists[key], theirs, "the other playlist is untouched")
        self.assertNotEqual(row["plex_mirror_remote_id"], key)
        self.assertEqual(row["plex_mirror_machine"], MACHINE)
        self.assertEqual(self.fake.keys(row["plex_mirror_remote_id"]), self._keys_of(0))

    def test_switching_off_after_a_target_change_deletes_nothing_there(self):
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        key = self._write(pid)["plex_mirror_remote_id"]
        self.conn.execute("UPDATE playlists SET plex_mirror_machine = 'the-old-server' WHERE id = ?", (pid,))
        self.conn.commit()
        mirror_plex.delete_mirror(self.conn, pid)
        self.assertIn(key, self.fake.playlists)
        self.assertIsNone(self._row(pid)["plex_mirror_remote_id"])

    def test_a_library_playlist_whose_key_collides_with_a_copy_elsewhere_is_listed(self):
        # The library server isn't the mirror target, and one of its own
        # playlists has the key a copy has on the target.
        pid = self._playlist("Road Trip", self._tracks([self._local(0)]))
        key = self._write(pid)["plex_mirror_remote_id"]
        db.set_config(self.conn, "plex_url", "http://library.example.invalid:32400")
        db.set_config(self.conn, "plex_token", "t")
        self.conn.commit()

        def library(url, **kw):
            path = urlsplit(url).path
            if path == "/identity":
                return _Response(200, {"MediaContainer": {"machineIdentifier": "the-library-server"}})
            return _Response(200, {"MediaContainer": {"Metadata": [{"ratingKey": key, "title": "Family Favourites"}]}})

        with mock.patch.object(plex_client.requests, "get", side_effect=library):
            listed = plex_client.list_playlists()
        self.assertEqual([p["title"] for p in listed["playlists"]], ["Family Favourites"])


class LoopGuardTests(_Base):
    def test_the_copies_are_never_listed_as_source_playlists(self):
        # The library and the mirror target are the same server here.
        db.set_config(self.conn, "plex_url", URL)
        db.set_config(self.conn, "plex_token", "t")
        self.conn.commit()
        self.fake.playlists["50"] = {"title": "Made in Plex", "summary": "", "entries": []}
        # A copy whose key Trobar no longer holds (its delete failed after
        # the key was cleared): known by its name alone.
        self.fake.playlists["51"] = {"title": "Old Mix_Trobar_", "summary": "", "entries": []}
        self._write(self._playlist("Road Trip", self._tracks([self._local(0)])))
        original_request = self.fake.request

        def listing(method, url, headers=None, params=None, timeout=None):
            if urlsplit(url).path == "/playlists" and method == "GET":
                return _Response(200, {"MediaContainer": {"Metadata": [
                    {"ratingKey": k, "title": p["title"]} for k, p in self.fake.playlists.items()]}})
            return original_request(method, url, headers, params, timeout)

        with mock.patch.object(plex_client.requests, "get",
                               side_effect=lambda url, **kw: listing("GET", url, **kw)):
            listed = plex_client.list_playlists()
        self.assertEqual([p["title"] for p in listed["playlists"]], ["Made in Plex"])

        # Renamed in Plex, a copy is still known by the key the sink stored.
        [copy_key] = [k for k in self.fake.playlists if k not in ("50", "51")]
        self.fake.playlists[copy_key]["title"] = "Renamed By Hand"
        with mock.patch.object(plex_client.requests, "get",
                               side_effect=lambda url, **kw: listing("GET", url, **kw)):
            listed = plex_client.list_playlists()
        self.assertEqual([p["title"] for p in listed["playlists"]], ["Made in Plex"])


class StatusTests(_Base):
    def test_the_mirror_status_reads_the_mirror_connection(self):
        self.assertEqual(plex_client.mirror_status(), {"state": "paired", "url": URL, "provider": "plex"})
        self.fake.reachable = False
        self.assertEqual(plex_client.mirror_status()["state"], "disconnected")
        db.set_config(self.conn, "mirror_plex_url", None)
        self.conn.commit()
        self.assertEqual(plex_client.mirror_status(), {"state": "disconnected", "url": "", "provider": "plex"})


if __name__ == "__main__":
    unittest.main()
