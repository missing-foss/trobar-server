# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A sink's network calls and file work never run with the write lock held.

Every client call below first writes on a second connection that won't wait,
so a transaction left open fails the test at the call that met it. The clients
are fakes: this pins when the calls happen, not what a real server answers."""

import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import db
import matching
import mirror_sinks
import playlist_sync
import subsonic_client

_KEY = (matching.normalize("A"), matching.normalize("B"), matching.normalize("T"))
_INDEX = {_KEY: [{"id": "s1", "track_no": 1}]}
_PLEX_INDEX = {"machine": "m1", "by_tail": {}, "by_tags": {}}
_MA_ENTRY = {"uri": "u1", "path": "A/B/t.flac", "track_no": 1}
_MA_INDEX = {"by_path": {"A/B/t.flac": _MA_ENTRY}, "by_tags": {}}


class _Checked:
    """A fake client module: each call checks the lock, then answers."""

    def __init__(self, check, fake):
        self._check = check
        self._fake = fake
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._fake, name)
        if not isinstance(attr, mock.Mock):
            return attr

        def call(*args, **kwargs):
            self._check(name)
            self.calls.append(name)
            return attr(*args, **kwargs)
        return call


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="trobar-test-no-lock-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._saved = (db.DATA_DIR, db.DB_PATH)
        self.addCleanup(self._restore)
        db.DATA_DIR = Path(self._tmp)
        db.DB_PATH = Path(self._tmp) / "music-sync.db"
        db.init_db()
        self.conn = db.get_conn()
        self.addCleanup(self.conn.close)
        self.other = sqlite3.connect(db.DB_PATH, timeout=0)
        self.addCleanup(self.other.close)
        for key, value in {
                "mirror_subsonic_url": "http://mirror.invalid", "mirror_subsonic_username": "t",
                "mirror_subsonic_password": "p", "mirror_jellyfin_url": "http://mirror.invalid",
                "mirror_jellyfin_api_key": "k", "mirror_jellyfin_username": "t",
                "mirror_jellyfin_user_id": "u1", "mirror_emby_url": "http://mirror.invalid",
                "mirror_emby_api_key": "k", "mirror_emby_username": "t",
                "mirror_emby_user_id": "u1", "music_assistant_url": "http://mirror.invalid",
                "music_assistant_token": "k", "mirror_plex_url": "http://mirror.invalid",
                "mirror_plex_token": "k"}.items():
            db.set_config(self.conn, key, value)
        self.track = self.conn.execute(
            "INSERT INTO tracks (relative_path, artist, album, title, track_no, size, mtime) "
            "VALUES ('A/B/t.flac', 'A', 'B', 'T', 1, 1, 0)").lastrowid
        self.conn.commit()

    def _restore(self):
        db.DATA_DIR, db.DB_PATH = self._saved

    def _check(self, name=""):
        try:
            self.other.execute(
                "INSERT OR REPLACE INTO app_config (key, value) VALUES ('other_writer', '1')")
            self.other.commit()
        except sqlite3.OperationalError:
            self.fail(f"the write lock was held at {name}")

    def _playlist(self, **columns):
        names = ", ".join(["title", *columns])
        marks = ", ".join("?" * (len(columns) + 1))
        playlist_id = self.conn.execute(
            f"INSERT INTO playlists ({names}) VALUES ({marks})",
            ["P", *columns.values()]).lastrowid
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, 0, 'A', 'T', ?)", (playlist_id, self.track))
        self.conn.commit()
        return playlist_id

    def _value(self, playlist_id, column):
        return self.conn.execute(
            f"SELECT {column} FROM playlists WHERE id = ?", (playlist_id,)).fetchone()[0]


class SubsonicTests(_Base):

    def _client(self):
        fake = mock.MagicMock()
        fake.ERROR_DATA_NOT_FOUND = subsonic_client.ERROR_DATA_NOT_FOUND
        fake.mirror_build_tag_index.return_value = _INDEX
        return fake

    def test_stale_id_retry(self):
        playlist_id = self._playlist(subsonic_mirror_enabled=1, subsonic_mirror_remote_id="old")
        fake = self._client()
        fake.mirror_create_or_replace_playlist.side_effect = [
            {"status": "error", "code": subsonic_client.ERROR_DATA_NOT_FOUND, "reason": "gone"},
            {"status": "ok", "remote_id": "new"}]
        client = _Checked(self._check, fake)
        with mock.patch("mirror_subsonic.subsonic_client", client):
            mirror_sinks.write_all(self.conn, playlist_id)
        self.assertEqual(client.calls, [
            "mirror_build_tag_index", "mirror_read_items", "mirror_create_or_replace_playlist",
            "mirror_create_or_replace_playlist", "mirror_set_playlist_metadata"])
        self.assertEqual(self._value(playlist_id, "subsonic_mirror_remote_id"), "new")

    def test_delete(self):
        playlist_id = self._playlist(subsonic_mirror_enabled=1, subsonic_mirror_remote_id="old")
        client = _Checked(self._check, self._client())
        with mock.patch("mirror_subsonic.subsonic_client", client):
            mirror_sinks.delete_all(self.conn, playlist_id)
        self.assertEqual(client.calls, ["mirror_delete_playlist"])


class _MediaBrowser(_Base):
    name: str

    def _client(self):
        fake = mock.MagicMock()
        fake.mirror_build_tag_index.return_value = _INDEX
        return fake

    def _patch(self, client):
        return mock.patch(f"mirror_{self.name}.{self.name}_client", client)

    def test_stale_id_retry(self):
        playlist_id = self._playlist(**{f"{self.name}_mirror_enabled": 1,
                                        f"{self.name}_mirror_remote_id": "old",
                                        f"{self.name}_mirror_owner_id": "u1"})
        fake = self._client()
        fake.mirror_create_or_replace_playlist.side_effect = [
            {"status": "error", "code": 404, "reason": "gone"},
            {"status": "ok", "remote_id": "new"}]
        client = _Checked(self._check, fake)
        with self._patch(client):
            mirror_sinks.write_all(self.conn, playlist_id)
        self.assertEqual(client.calls.count("mirror_create_or_replace_playlist"), 2)
        self.assertEqual(self._value(playlist_id, f"{self.name}_mirror_remote_id"), "new")

    def test_copy_moved_to_another_account(self):
        playlist_id = self._playlist(**{f"{self.name}_mirror_enabled": 1,
                                        f"{self.name}_mirror_remote_id": "old",
                                        f"{self.name}_mirror_owner_id": "someone-else"})
        fake = self._client()
        fake.mirror_create_or_replace_playlist.return_value = {"status": "ok", "remote_id": "new"}
        client = _Checked(self._check, fake)
        with self._patch(client):
            mirror_sinks.write_all(self.conn, playlist_id)
        self.assertIn("mirror_delete_playlist", client.calls)
        self.assertIn("mirror_create_or_replace_playlist", client.calls)
        self.assertEqual(self._value(playlist_id, f"{self.name}_mirror_remote_id"), "new")

    def test_delete(self):
        playlist_id = self._playlist(**{f"{self.name}_mirror_enabled": 1,
                                        f"{self.name}_mirror_remote_id": "old"})
        client = _Checked(self._check, self._client())
        with self._patch(client):
            mirror_sinks.delete_all(self.conn, playlist_id)
        self.assertEqual(client.calls, ["mirror_delete_playlist"])


class JellyfinTests(_MediaBrowser):
    name = "jellyfin"


class EmbyTests(_MediaBrowser):
    name = "emby"


del _MediaBrowser  # not itself a test case


class PlexTests(_Base):

    def _client(self):
        fake = mock.MagicMock()
        fake.mirror_build_index.return_value = _PLEX_INDEX
        fake.mirror_name.return_value = "P"
        fake.mirror_create.return_value = "new"
        fake.mirror_read.return_value = {"title": "P", "summary": "", "keys": ["k1"],
                                         "items": [{"id": "k1", "artist": "a", "title": "t"}]}
        fake.mirror_set_metadata.return_value = True
        fake.mirror_machine.return_value = "m1"
        return fake

    def _run(self, playlist_id, fake):
        client = _Checked(self._check, fake)
        with mock.patch("mirror_plex.plex_client", client), \
                mock.patch("mirror_plex._resolve", return_value="k1"):
            mirror_sinks.write_all(self.conn, playlist_id)
        return client

    def test_copy_on_another_server(self):
        playlist_id = self._playlist(plex_mirror_enabled=1, plex_mirror_remote_id="old",
                                     plex_mirror_machine="another")
        client = self._run(playlist_id, self._client())
        self.assertIn("mirror_create", client.calls)
        self.assertEqual(self._value(playlist_id, "plex_mirror_remote_id"), "new")

    def test_copy_gone_on_the_target(self):
        playlist_id = self._playlist(plex_mirror_enabled=1, plex_mirror_remote_id="old",
                                     plex_mirror_machine="m1")
        fake = self._client()
        fake.mirror_read.side_effect = [None, {"title": "P", "summary": "", "keys": ["k1"],
                                               "items": [{"id": "k1", "artist": "a", "title": "t"}]}]
        fake.mirror_exists.return_value = False
        client = self._run(playlist_id, fake)
        self.assertIn("mirror_exists", client.calls)
        self.assertIn("mirror_create", client.calls)
        self.assertEqual(self._value(playlist_id, "plex_mirror_remote_id"), "new")

    def test_delete(self):
        playlist_id = self._playlist(plex_mirror_enabled=1, plex_mirror_remote_id="old",
                                     plex_mirror_machine="m1")
        client = _Checked(self._check, self._client())
        with mock.patch("mirror_plex.plex_client", client):
            mirror_sinks.delete_all(self.conn, playlist_id)
        self.assertEqual(client.calls, ["mirror_machine", "mirror_delete"])


class MusicAssistantTests(_Base):

    def _client(self):
        fake = mock.MagicMock()
        fake.mirror_build_index.return_value = dict(_MA_INDEX)
        return fake

    def test_copy_gone_on_the_target(self):
        playlist_id = self._playlist(music_assistant_mirror_enabled=1,
                                     music_assistant_mirror_remote_id="old")
        fake = self._client()
        fake.mirror_read_items.side_effect = [None, [{"id": "A/B/t.flac", "artist": "a", "title": "t"}]]
        fake.mirror_listed.return_value = False
        fake.mirror_create.return_value = "new"
        fake.mirror_replace.return_value = True
        client = _Checked(self._check, fake)
        with mock.patch("mirror_music_assistant.music_assistant_client", client):
            mirror_sinks.write_all(self.conn, playlist_id)
        self.assertEqual(client.calls, [
            "mirror_build_index", "mirror_read_items", "mirror_listed", "mirror_create",
            "mirror_replace", "mirror_read_items"])
        self.assertEqual(self._value(playlist_id, "music_assistant_mirror_remote_id"), "new")

    def test_delete(self):
        playlist_id = self._playlist(music_assistant_mirror_enabled=1,
                                     music_assistant_mirror_remote_id="old")
        fake = self._client()
        fake.mirror_delete.return_value = "deleted"
        client = _Checked(self._check, fake)
        with mock.patch("mirror_music_assistant.music_assistant_client", client):
            mirror_sinks.delete_all(self.conn, playlist_id)
        self.assertEqual(client.calls, ["mirror_delete"])


class FilesystemTests(_Base):

    def test_the_folder_is_touched_with_nothing_open(self):
        folder = Path(self._tmp) / "mirror"
        folder.mkdir()
        db.set_config(self.conn, "mirror_folder", str(folder))
        db.set_config(self.conn, "music_root", str(Path(self._tmp) / "music"))
        user = self.conn.execute(
            "INSERT INTO users (username) VALUES ('a person')").lastrowid
        self.conn.commit()
        playlist_id = self._playlist(mirror_enabled=1, owner_user_id=user)
        real_mkdir = Path.mkdir
        real_write = Path.write_text

        def mkdir(path, *args, **kwargs):
            self._check("mkdir")
            return real_mkdir(path, *args, **kwargs)

        def write_text(path, *args, **kwargs):
            self._check("write_text")
            return real_write(path, *args, **kwargs)
        with mock.patch.object(Path, "mkdir", mkdir), mock.patch.object(Path, "write_text", write_text):
            mirror_sinks.write_all(self.conn, playlist_id)
        self.assertIsNone(self._value(playlist_id, "mirror_last_error_code"))
        self.assertIsNotNone(self._value(playlist_id, "mirror_filename"))


class TwoWritersTests(_Base):
    """One writer blocked inside the target's client, another run beside it:
    the second must wait, then see what the first stored."""

    def setUp(self):
        super().setUp()
        self.inside = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.creates: list[str | None] = []
        self.deleted: list[str] = []
        self.client = mock.MagicMock()
        self.client.mirror_build_tag_index.return_value = _INDEX
        self.client.mirror_create_or_replace_playlist.side_effect = self._create
        self.client.mirror_delete_playlist.side_effect = self._delete
        patch = mock.patch("mirror_subsonic.subsonic_client", self.client)
        patch.start()
        self.addCleanup(patch.stop)

    def _create(self, _title, _songs, remote_id):
        first = not self.creates
        self.creates.append(remote_id)
        if first:
            self.inside.set()
            self.assertTrue(self.release.wait(10))
        return {"status": "ok", "remote_id": remote_id or f"r{len(self.creates)}"}

    def _delete(self, remote_id):
        self.deleted.append(remote_id)
        return True

    def _thread(self, action):
        def run():
            conn = db.get_conn()
            try:
                action(conn)
            finally:
                conn.close()
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread

    def _start_blocked_write(self, playlist_id):
        writer = self._thread(lambda conn: mirror_sinks.write_all(conn, playlist_id))
        self.assertTrue(self.inside.wait(10))
        return writer

    def _finish(self, writer, second):
        second.join(0.2)
        self.assertTrue(second.is_alive(), "the second writer did not wait")
        self.release.set()
        writer.join(10)
        second.join(10)
        self.assertFalse(writer.is_alive() or second.is_alive())

    def test_two_writes_make_one_copy(self):
        playlist_id = self._playlist(subsonic_mirror_enabled=1)
        writer = self._start_blocked_write(playlist_id)
        second = self._thread(lambda conn: mirror_sinks.write_all(conn, playlist_id))
        self._finish(writer, second)
        self.assertEqual(self.creates, [None, "r1"])
        self.assertEqual(self._value(playlist_id, "subsonic_mirror_remote_id"), "r1")

    def _switch_off(self, conn, playlist_id):
        with mirror_sinks.playlist_lock(playlist_id):
            conn.execute("UPDATE playlists SET subsonic_mirror_enabled = 0 WHERE id = ?",
                         (playlist_id,))
            conn.commit()
            mirror_sinks.BY_KEY["subsonic"].backend.delete_mirror(conn, playlist_id)
            conn.commit()

    def test_a_delete_during_a_write_deletes_the_copy_it_made(self):
        playlist_id = self._playlist(subsonic_mirror_enabled=1)
        writer = self._start_blocked_write(playlist_id)
        second = self._thread(lambda conn: self._switch_off(conn, playlist_id))
        self._finish(writer, second)
        self.assertEqual(self.deleted, ["r1"])
        self.assertIsNone(self._value(playlist_id, "subsonic_mirror_remote_id"))

    def test_delete_all_waits_for_a_write(self):
        playlist_id = self._playlist(subsonic_mirror_enabled=1)
        writer = self._start_blocked_write(playlist_id)
        second = self._thread(lambda conn: mirror_sinks.delete_all(conn, playlist_id))
        self._finish(writer, second)
        self.assertEqual(self.deleted, ["r1"])

    def test_removing_a_playlist_during_a_write_deletes_the_copy_it_made(self):
        playlist_id = self._playlist(subsonic_mirror_enabled=1)
        writer = self._start_blocked_write(playlist_id)
        second = self._thread(lambda conn: playlist_sync._remove_playlist_row(conn, playlist_id))
        self._finish(writer, second)
        self.assertEqual(self.deleted, ["r1"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM playlists WHERE id = ?", (playlist_id,)).fetchone()[0], 0)

    def test_a_write_waiting_on_a_removal_finds_the_playlist_gone(self):
        playlist_id = self._playlist(subsonic_mirror_enabled=1, subsonic_mirror_remote_id="r0")
        inside = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def delete(remote_id):
            self.deleted.append(remote_id)
            inside.set()
            self.assertTrue(release.wait(10))
            return True
        self.client.mirror_delete_playlist.side_effect = delete
        remover = self._thread(lambda conn: playlist_sync._remove_playlist_row(conn, playlist_id))
        self.assertTrue(inside.wait(10))
        writer = self._thread(lambda conn: mirror_sinks.write_all(conn, playlist_id))
        writer.join(0.2)
        self.assertTrue(writer.is_alive(), "the write did not wait")
        release.set()
        remover.join(10)
        writer.join(10)
        self.assertEqual(self.creates, [])

    def test_a_removal_with_uncommitted_writes_does_not_stall_a_sink(self):
        # The lock is taken with nothing uncommitted, so the blocked sink can
        # store its result while the removal waits.
        playlist_id = self._playlist(subsonic_mirror_enabled=1)
        writer = self._start_blocked_write(playlist_id)

        def remove(conn):
            conn.execute("PRAGMA busy_timeout = 0")
            conn.execute("UPDATE playlists SET title = 'pending' WHERE id = ?", (playlist_id,))
            playlist_sync._remove_playlist_row(conn, playlist_id)
        second = self._thread(remove)
        second.join(0.2)
        self._check("the removal waiting for the lock")
        self.release.set()
        writer.join(10)
        second.join(10)
        self.assertEqual(self.deleted, ["r1"])


class CallerTests(_Base):

    def _every_sink_checks(self):
        return [mock.patch.object(s.backend, "write_mirror", side_effect=self._entry(s.key))
                for s in mirror_sinks.SINKS]

    def _entry(self, key):
        def enter(conn, playlist_id, **_kwargs):
            self._check(key)
            # What a failing sink leaves: the next must still start clean.
            conn.execute("UPDATE playlists SET mirror_last_error = 'x' WHERE id = ?", (playlist_id,))
        return enter

    def _run_with_sinks_checked(self, action):
        patches = self._every_sink_checks()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        action()

    def test_write_all_starts_each_sink_clean_and_ends_committed(self):
        playlist_id = self._playlist()
        self.conn.execute("UPDATE playlists SET title = 'edited' WHERE id = ?", (playlist_id,))
        self._run_with_sinks_checked(lambda: mirror_sinks.write_all(self.conn, playlist_id))
        self.assertFalse(self.conn.in_transaction)

    def test_delete_all_starts_each_sink_clean_and_ends_committed(self):
        playlist_id = self._playlist()
        self.conn.execute("UPDATE playlists SET title = 'edited' WHERE id = ?", (playlist_id,))
        patches = [mock.patch.object(s.backend, "delete_mirror", side_effect=self._entry(s.key))
                   for s in mirror_sinks.SINKS]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        mirror_sinks.delete_all(self.conn, playlist_id)
        self.assertFalse(self.conn.in_transaction)

    def test_a_sync_commits_the_playlist_before_the_sinks(self):
        class Provider:
            def get_playlist_tracks(self, title, source_playlist_id=None, **_kwargs):
                return {"status": "ok", "tracks": [
                    {"position": 0, "artist": "A", "title": "T", "album": "B"}]}

        self._run_with_sinks_checked(lambda: playlist_sync._sync_one_playlist(
            self.conn, Provider(), "roon", "Synced"))
        self.assertFalse(self.conn.in_transaction)

    def test_a_playlist_edit_is_kept_if_a_sink_dies(self):
        playlist_id = self._playlist()
        with mock.patch.object(mirror_sinks.SINKS[0].backend, "write_mirror",
                               side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            playlist_sync.rename_trobar_playlist(self.conn, playlist_id, "renamed")
        self.conn.rollback()
        self.assertEqual(self._value(playlist_id, "title"), "renamed")

    def test_removing_a_playlist_deletes_its_mirrors_before_touching_its_selections(self):
        playlist_id = self._playlist(subsonic_mirror_enabled=1, subsonic_mirror_remote_id="old")
        user = self.conn.execute("INSERT INTO users (username) VALUES ('a person')").lastrowid
        self.conn.execute(
            "INSERT INTO selections (type, target, created_by_user_id) VALUES ('playlist', ?, ?)",
            (str(playlist_id), user))
        self.conn.commit()
        with mock.patch("mirror_subsonic.subsonic_client") as client, \
                mock.patch.object(playlist_sync.sync_state, "delete_selection") as selection:
            client.mirror_delete_playlist.side_effect = lambda _id: selection.assert_not_called()
            playlist_sync._remove_playlist_row(self.conn, playlist_id)
        client.mirror_delete_playlist.assert_called_once()
        selection.assert_called_once()
