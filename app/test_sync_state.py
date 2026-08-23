#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for sync_state.py's recompute core (#41's own highest-value
target): required_track_ids_for_device, recompute_device_state, and
delete_selection. This is the central, non-trivial logic every device
sync flows through — and the mechanism #74's playlist-unshare revocation
leans on (delete selection -> recompute -> device_track_state flips to
'removed' -> the device is told to delete the files) — so a silent
regression here hurts users directly.

    python3 -m unittest test_sync_state -v      # from app/

In-memory SQLite (db.SCHEMA + db._run_migrations()), no Flask — same
harness pattern as test_selections.py / test_playlist_sync.py.
"""
import sqlite3
import unittest

import db
import sync_state


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    db._run_migrations(conn)
    return conn


def _make_user(conn: sqlite3.Connection, username: str = "alice") -> int:
    cur = conn.execute("INSERT INTO users (username) VALUES (?)", (username,))
    conn.commit()
    return sync_state._new_id(cur)


def _make_track(conn: sqlite3.Connection, artist: str, album: str, title: str,
                relative_path: str, deleted: bool = False,
                track_no: int | None = None, disc_no: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO tracks (relative_path, artist, album, title, track_no, disc_no, "
        "size, mtime, deleted_at) VALUES (?, ?, ?, ?, ?, ?, 1000, 0, ?)",
        (relative_path, artist, album, title, track_no, disc_no,
         "2026-01-01" if deleted else None),
    )
    conn.commit()
    return sync_state._new_id(cur)


def _status(conn: sqlite3.Connection, device_id: int, track_id: int) -> str | None:
    row = conn.execute(
        "SELECT status FROM device_track_state WHERE device_id = ? AND track_id = ?",
        (device_id, track_id),
    ).fetchone()
    return row["status"] if row else None


class RequiredTrackIdsTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")

    def test_artist_selection_resolves_all_that_artists_tracks(self):
        t1 = _make_track(self.conn, "Aphelion", "Event Horizon", "Singularity", "a/1.flac")
        t2 = _make_track(self.conn, "Aphelion", "Parallax", "Redshift", "a/2.flac")
        _make_track(self.conn, "Other", "X", "Y", "b/1.flac")  # different artist, excluded
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.assertEqual(sync_state.required_track_ids_for_device(self.conn, self.device), {t1, t2})

    def test_album_selection_resolves_only_that_album(self):
        t1 = _make_track(self.conn, "Aphelion", "Event Horizon", "Singularity", "a/1.flac")
        _make_track(self.conn, "Aphelion", "Parallax", "Redshift", "a/2.flac")  # other album
        sync_state.create_selection(self.conn, "album", "Aphelion||Event Horizon", self.user, [self.device])
        self.assertEqual(sync_state.required_track_ids_for_device(self.conn, self.device), {t1})

    def test_deleted_tracks_are_excluded(self):
        live = _make_track(self.conn, "Aphelion", "Event Horizon", "A", "a/1.flac")
        _make_track(self.conn, "Aphelion", "Event Horizon", "B", "a/2.flac", deleted=True)
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.assertEqual(sync_state.required_track_ids_for_device(self.conn, self.device), {live})

    def test_playlist_selection_uses_matched_tracks_only(self):
        t1 = _make_track(self.conn, "Aphelion", "Event Horizon", "A", "a/1.flac")
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider) VALUES ('Mix', 'roon')")
        pl_id = sync_state._new_id(cur)
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, 0, 'Aphelion', 'A', ?)", (pl_id, t1))
        # An unmatched playlist entry (no local file) must contribute nothing.
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, 1, 'Ghost', 'Nowhere', NULL)", (pl_id,))
        self.conn.commit()
        sync_state.create_selection(self.conn, "playlist", str(pl_id), self.user, [self.device])
        self.assertEqual(sync_state.required_track_ids_for_device(self.conn, self.device), {t1})

    def test_union_across_selections_dedups_a_shared_track(self):
        # A track in both an artist selection and a playlist selection is
        # required exactly once — the union-dedup guarantee the whole
        # recompute model rests on.
        shared = _make_track(self.conn, "Aphelion", "Event Horizon", "A", "a/1.flac")
        cur = self.conn.execute("INSERT INTO playlists (title, source_provider) VALUES ('Mix', 'roon')")
        pl_id = sync_state._new_id(cur)
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, 0, 'Aphelion', 'A', ?)", (pl_id, shared))
        self.conn.commit()
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        sync_state.create_selection(self.conn, "playlist", str(pl_id), self.user, [self.device])
        self.assertEqual(sync_state.required_track_ids_for_device(self.conn, self.device), {shared})

    def test_exclude_autofit_flag(self):
        manual = _make_track(self.conn, "Aphelion", "Event Horizon", "A", "a/1.flac")
        auto = _make_track(self.conn, "Beacon", "Drift", "B", "b/1.flac")
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        auto_sel = sync_state.create_autofit_selection(self.conn, self.device, self.user)
        self.conn.execute(
            "INSERT INTO autofit_tracks (selection_id, track_id) VALUES (?, ?)", (auto_sel, auto))
        self.conn.commit()
        self.assertEqual(
            sync_state.required_track_ids_for_device(self.conn, self.device), {manual, auto})
        self.assertEqual(
            sync_state.required_track_ids_for_device(self.conn, self.device, exclude_autofit=True),
            {manual})


class RecomputeDeviceStateTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")
        self.t1 = _make_track(self.conn, "Aphelion", "Event Horizon", "A", "a/1.flac")
        self.t2 = _make_track(self.conn, "Aphelion", "Event Horizon", "B", "a/2.flac")

    def test_newly_required_track_becomes_pending(self):
        # create_selection already calls recompute internally.
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.assertEqual(_status(self.conn, self.device, self.t1), "pending")
        self.assertEqual(_status(self.conn, self.device, self.t2), "pending")

    def test_downloaded_and_excluded_tracks_still_required_are_not_requeued(self):
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        # Simulate the device having reported one downloaded, and the user
        # having deleted the other on-device (excluded).
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded' WHERE device_id=? AND track_id=?",
            (self.device, self.t1))
        self.conn.execute(
            "UPDATE device_track_state SET status='excluded' WHERE device_id=? AND track_id=?",
            (self.device, self.t2))
        self.conn.commit()
        sync_state.recompute_device_state(self.conn, self.device)
        # Both still required, so neither is flipped back to pending.
        self.assertEqual(_status(self.conn, self.device, self.t1), "downloaded")
        self.assertEqual(_status(self.conn, self.device, self.t2), "excluded")

    def test_track_no_longer_required_is_marked_removed(self):
        sel = sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded' WHERE device_id=?", (self.device,))
        self.conn.commit()
        # Unassign the device from the selection, then recompute.
        self.conn.execute(
            "DELETE FROM selection_devices WHERE selection_id=? AND device_id=?", (sel, self.device))
        self.conn.commit()
        sync_state.recompute_device_state(self.conn, self.device)
        self.assertEqual(_status(self.conn, self.device, self.t1), "removed")
        self.assertEqual(_status(self.conn, self.device, self.t2), "removed")

    def test_device_sourced_device_is_never_pruned(self):
        # #63: with source_of_truth='device', unrequired downloaded tracks are
        # NOT marked removed — the device keeps them (survives a server-DB loss).
        sel = sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded' WHERE device_id=?", (self.device,))
        self.conn.execute(
            "UPDATE devices SET source_of_truth='device' WHERE id=?", (self.device,))
        self.conn.execute(
            "DELETE FROM selection_devices WHERE selection_id=? AND device_id=?", (sel, self.device))
        self.conn.commit()
        sync_state.recompute_device_state(self.conn, self.device)
        self.assertEqual(_status(self.conn, self.device, self.t1), "downloaded")
        self.assertEqual(_status(self.conn, self.device, self.t2), "downloaded")

    def test_device_sourced_still_downloads_newly_required(self):
        # #63: the switch is deletion-only — the server can still ADD content.
        self.conn.execute(
            "UPDATE devices SET source_of_truth='device' WHERE id=?", (self.device,))
        self.conn.commit()
        sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.assertEqual(_status(self.conn, self.device, self.t1), "pending")
        self.assertEqual(_status(self.conn, self.device, self.t2), "pending")

    def test_flipping_back_to_server_re_prunes(self):
        # #63: 'device' -> 'server' re-enables pruning on the next recompute.
        sel = sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded' WHERE device_id=?", (self.device,))
        self.conn.execute(
            "UPDATE devices SET source_of_truth='device' WHERE id=?", (self.device,))
        self.conn.execute(
            "DELETE FROM selection_devices WHERE selection_id=? AND device_id=?", (sel, self.device))
        self.conn.commit()
        sync_state.recompute_device_state(self.conn, self.device)
        self.assertEqual(_status(self.conn, self.device, self.t1), "downloaded")  # protected
        self.conn.execute(
            "UPDATE devices SET source_of_truth='server' WHERE id=?", (self.device,))
        self.conn.commit()
        sync_state.recompute_device_state(self.conn, self.device)
        self.assertEqual(_status(self.conn, self.device, self.t1), "removed")  # now pruned
        self.assertEqual(_status(self.conn, self.device, self.t2), "removed")

    def test_removed_track_that_becomes_required_again_is_requeued(self):
        sel = sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.conn.execute(
            "DELETE FROM selection_devices WHERE selection_id=? AND device_id=?", (sel, self.device))
        self.conn.commit()
        sync_state.recompute_device_state(self.conn, self.device)
        self.assertEqual(_status(self.conn, self.device, self.t1), "removed")
        # Re-assign — the 'removed' row must flip back to 'pending', not stay removed.
        self.conn.execute(
            "INSERT INTO selection_devices (selection_id, device_id) VALUES (?, ?)", (sel, self.device))
        self.conn.commit()
        sync_state.recompute_device_state(self.conn, self.device)
        self.assertEqual(_status(self.conn, self.device, self.t1), "pending")

    def test_already_removed_and_still_not_required_is_left_alone(self):
        # No selection at all — an existing 'removed' row must stay
        # 'removed' AND not be re-touched: recompute_device_state's second
        # loop guards on `status != "removed"`, so it must skip this row
        # entirely rather than re-marking it and churning updated_at.
        # Assert the timestamp is untouched, which is what "left alone"
        # actually means (status alone can't distinguish "skipped" from
        # "re-marked to the same value").
        self.conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status, updated_at) "
            "VALUES (?, ?, 'removed', '2020-01-01 00:00:00')",
            (self.device, self.t1))
        self.conn.commit()
        before = self.conn.execute(
            "SELECT updated_at FROM device_track_state WHERE device_id=? AND track_id=?",
            (self.device, self.t1)).fetchone()["updated_at"]
        sync_state.recompute_device_state(self.conn, self.device)
        after = self.conn.execute(
            "SELECT status, updated_at FROM device_track_state WHERE device_id=? AND track_id=?",
            (self.device, self.t1)).fetchone()
        self.assertEqual(after["status"], "removed")
        self.assertEqual(after["updated_at"], before)  # skipped, not re-marked


class DeleteSelectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")
        self.t1 = _make_track(self.conn, "Aphelion", "Event Horizon", "A", "a/1.flac")

    def test_delete_selection_flips_its_tracks_to_removed(self):
        sel = sync_state.create_selection(self.conn, "artist", "Aphelion", self.user, [self.device])
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded' WHERE device_id=?", (self.device,))
        self.conn.commit()
        sync_state.delete_selection(self.conn, sel)
        # Selection gone, and the device is told to remove the file.
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM selections WHERE id=?", (sel,)).fetchone())
        self.assertEqual(_status(self.conn, self.device, self.t1), "removed")

    def test_track_still_required_by_another_selection_survives(self):
        # #74's revocation (and the recompute model generally) must not
        # yank a track off a device just because ONE selection referencing
        # it was deleted — another still-assigned selection keeps it.
        sel_artist = sync_state.create_selection(
            self.conn, "artist", "Aphelion", self.user, [self.device])
        cur = self.conn.execute("INSERT INTO playlists (title, source_provider) VALUES ('Mix', 'roon')")
        pl_id = sync_state._new_id(cur)
        self.conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, artist, title, matched_track_id) "
            "VALUES (?, 0, 'Aphelion', 'A', ?)", (pl_id, self.t1))
        self.conn.commit()
        sync_state.create_selection(self.conn, "playlist", str(pl_id), self.user, [self.device])
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded' WHERE device_id=?", (self.device,))
        self.conn.commit()

        sync_state.delete_selection(self.conn, sel_artist)
        # The playlist selection still requires t1, so it stays downloaded.
        self.assertEqual(_status(self.conn, self.device, self.t1), "downloaded")


class DeviceManifestTests(unittest.TestCase):
    """#63 recovery: uploading a device manifest marks matched tracks
    'downloaded' (no re-download) and counts unmatched paths. The device
    uploads the device_path() form it downloaded (get_changes' wire path) — what
    it can recover by walking its own folder — NOT the catalog's
    tracks.relative_path (the source layout, which the device never sees)."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")
        # Catalog paths deliberately unlike the device path (a "(year)" album
        # folder, lowercased, no NN- prefix) — the device instead holds the
        # normalized Artiste/Album/NN - Titre.ext form get_changes emits.
        self.t1 = _make_track(self.conn, "Aphelion", "Event Horizon", "Ascent",
                              "Aphelion/Event Horizon (2021)/1 - ascent.flac", track_no=1)
        self.t2 = _make_track(self.conn, "Aphelion", "Event Horizon", "Bloom",
                              "Aphelion/Event Horizon (2021)/2 - bloom.flac", track_no=2)
        self.dp1 = "Aphelion/Event Horizon/01 - Ascent.flac"
        self.dp2 = "Aphelion/Event Horizon/02 - Bloom.flac"

    def test_matched_become_downloaded_unmatched_counted(self):
        result = sync_state.record_device_manifest(
            self.conn, self.device, [self.dp1, self.dp2, "Ghost/x.flac"])
        self.assertEqual(result, {"matched": 2, "unmatched": 1})
        self.assertEqual(_status(self.conn, self.device, self.t1), "downloaded")
        self.assertEqual(_status(self.conn, self.device, self.t2), "downloaded")
        cnt = self.conn.execute(
            "SELECT unknown_track_count FROM devices WHERE id=?", (self.device,)).fetchone()[0]
        self.assertEqual(cnt, 1)

    def test_catalog_relative_path_does_not_match(self):
        # The bug this guards against: the SERVER-side catalog path (which the
        # device never has) must NOT match — only the device path does.
        result = sync_state.record_device_manifest(
            self.conn, self.device, ["Aphelion/Event Horizon (2021)/1 - ascent.flac"])
        self.assertEqual(result, {"matched": 0, "unmatched": 1})

    def test_transcoding_device_matches_the_mp3_path(self):
        # A transcoding device holds .mp3 files for lossless sources; matching
        # must use that extension, not the .flac catalog source.
        dev, _ = sync_state.create_device(self.conn, self.user, "phone-mp3",
                                          transcode_format="mp3_320")
        result = sync_state.record_device_manifest(
            self.conn, dev, ["Aphelion/Event Horizon/01 - Ascent.mp3"])
        self.assertEqual(result, {"matched": 1, "unmatched": 0})
        self.assertEqual(_status(self.conn, dev, self.t1), "downloaded")

    def test_is_idempotent(self):
        sync_state.record_device_manifest(self.conn, self.device, [self.dp1])
        result = sync_state.record_device_manifest(self.conn, self.device, [self.dp1])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(_status(self.conn, self.device, self.t1), "downloaded")

    def test_a_soft_deleted_track_path_does_not_match(self):
        _make_track(self.conn, "X", "Y", "Zed", "gone/1.flac", deleted=True, track_no=1)
        result = sync_state.record_device_manifest(self.conn, self.device, ["X/Y/01 - Zed.flac"])
        self.assertEqual(result, {"matched": 0, "unmatched": 1})

    def test_duplicate_paths_are_counted_once(self):
        # #160 review: dedup so the counts reflect distinct paths, not the raw
        # list length.
        result = sync_state.record_device_manifest(
            self.conn, self.device, [self.dp1, self.dp1, "ghost.flac", "ghost.flac"])
        self.assertEqual(result, {"matched": 1, "unmatched": 1})


class DeviceUnknownTracksTests(unittest.TestCase):
    """#161: unmatched manifest paths are persisted as reviewable device extras
    (parsed for display) and can be adopted (acknowledged) to stop being
    flagged."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")
        _make_track(self.conn, "Aphelion", "Event Horizon", "Ascent",
                    "Aphelion/Event Horizon (2021)/1 - ascent.flac", track_no=1)
        self.dp1 = "Aphelion/Event Horizon/01 - Ascent.flac"  # matches the track above

    def _count(self):
        return self.conn.execute(
            "SELECT unknown_track_count FROM devices WHERE id=?", (self.device,)).fetchone()[0]

    def test_unmatched_paths_persisted_and_parsed(self):
        sync_state.record_device_manifest(
            self.conn, self.device, [self.dp1, "Ghosts/Nowhere/03 - Drift.mp3", "loose.flac"])
        rows = sync_state.list_device_unknown_tracks(self.conn, self.device)
        self.assertEqual({r["path"] for r in rows}, {"Ghosts/Nowhere/03 - Drift.mp3", "loose.flac"})
        drift = next(r for r in rows if r["path"] == "Ghosts/Nowhere/03 - Drift.mp3")
        self.assertEqual((drift["artist"], drift["album"], drift["title"]), ("Ghosts", "Nowhere", "Drift"))
        self.assertFalse(drift["adopted"])
        self.assertEqual(self._count(), 2)  # matched dp1 not counted

    def test_adopt_stops_flagging_and_survives_reupload(self):
        sync_state.record_device_manifest(
            self.conn, self.device, ["Ghosts/Nowhere/03 - Drift.mp3", "loose.flac"])
        left = sync_state.set_device_unknown_adopted(self.conn, self.device, ["loose.flac"], True)
        self.assertEqual(left, 1)
        self.assertEqual(self._count(), 1)
        # re-uploading the same manifest preserves the adopted flag
        sync_state.record_device_manifest(
            self.conn, self.device, ["Ghosts/Nowhere/03 - Drift.mp3", "loose.flac"])
        adopted = {r["path"]: r["adopted"] for r in sync_state.list_device_unknown_tracks(self.conn, self.device)}
        self.assertTrue(adopted["loose.flac"])
        self.assertFalse(adopted["Ghosts/Nowhere/03 - Drift.mp3"])
        self.assertEqual(self._count(), 1)

    def test_paths_no_longer_present_are_dropped(self):
        sync_state.record_device_manifest(self.conn, self.device, [self.dp1, "loose.flac"])
        self.assertEqual(
            {r["path"] for r in sync_state.list_device_unknown_tracks(self.conn, self.device)},
            {"loose.flac"})
        # a later manifest without loose.flac drops it (the device deleted it)
        sync_state.record_device_manifest(self.conn, self.device, [self.dp1])
        self.assertEqual(sync_state.list_device_unknown_tracks(self.conn, self.device), [])
        self.assertEqual(self._count(), 0)


class UnresolvedPlaylistTracksTests(unittest.TestCase):
    """#200: record_unresolved_playlist_tracks / list_.../ set_..._excluded —
    the playlist-scoped counterpart to DeviceUnknownTracksTests above, same
    "acknowledgment survives a resync" shape."""

    def setUp(self):
        self.conn = _make_conn()
        cur = self.conn.execute("INSERT INTO playlists (title) VALUES ('Mix')")
        self.conn.commit()
        self.playlist = sync_state._new_id(cur)

    def _count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM unresolved_playlist_tracks "
            "WHERE playlist_id=? AND excluded=0", (self.playlist,)
        ).fetchone()[0]

    def test_unresolved_entries_are_persisted(self):
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 0, "artist": "A", "title": "Song A", "album": "Alb", "isrc": None},
            {"position": 1, "artist": "B", "title": "Song B", "album": None, "isrc": "USRC1"},
        ])
        rows = sync_state.list_unresolved_playlist_tracks(self.conn, self.playlist)
        self.assertEqual({r["title"] for r in rows}, {"Song A", "Song B"})
        b = next(r for r in rows if r["title"] == "Song B")
        self.assertEqual(b["isrc"], "USRC1")
        self.assertFalse(b["excluded"])
        self.assertEqual(self._count(), 2)

    def test_exclude_stops_flagging_and_survives_resync(self):
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 0, "artist": "A", "title": "Song A", "album": None},
            {"position": 1, "artist": "B", "title": "Song B", "album": None},
        ])
        rows = sync_state.list_unresolved_playlist_tracks(self.conn, self.playlist)
        song_a_id = next(r["id"] for r in rows if r["title"] == "Song A")
        left = sync_state.set_unresolved_playlist_tracks_excluded(
            self.conn, self.playlist, [song_a_id], True)
        self.assertEqual(left, 1)
        self.assertEqual(self._count(), 1)
        # a re-sync with the same misses preserves the excluded flag (and
        # the row's id — playlist_tracks itself is fully replaced every
        # sync, but this review table isn't)
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 0, "artist": "A", "title": "Song A", "album": None},
            {"position": 1, "artist": "B", "title": "Song B", "album": None},
        ])
        rows = sync_state.list_unresolved_playlist_tracks(self.conn, self.playlist)
        excluded = {r["title"]: r["excluded"] for r in rows}
        self.assertTrue(excluded["Song A"])
        self.assertFalse(excluded["Song B"])
        self.assertEqual({r["id"] for r in rows}, {song_a_id, next(r["id"] for r in rows if r["title"] == "Song B")})

    def test_entries_that_now_resolve_are_dropped(self):
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 0, "artist": "A", "title": "Song A", "album": None},
            {"position": 1, "artist": "B", "title": "Song B", "album": None},
        ])
        # Song A now matches on a later sync — no longer passed in as unresolved.
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 1, "artist": "B", "title": "Song B", "album": None},
        ])
        rows = sync_state.list_unresolved_playlist_tracks(self.conn, self.playlist)
        self.assertEqual({r["title"] for r in rows}, {"Song B"})

    def test_null_fields_are_normalized_and_still_deduped_across_resyncs(self):
        # Regression guard for the NULL-uniqueness pitfall the identity
        # index's comment warns about: without normalizing None -> '',
        # every resync would insert a fresh duplicate row instead of
        # reusing the existing one.
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 0, "artist": None, "title": "No Artist Tag", "album": None},
        ])
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 0, "artist": None, "title": "No Artist Tag", "album": None},
        ])
        rows = sync_state.list_unresolved_playlist_tracks(self.conn, self.playlist)
        self.assertEqual(len(rows), 1)

    def test_empty_unresolved_list_clears_everything(self):
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 0, "artist": "A", "title": "Song A", "album": None},
        ])
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [])
        self.assertEqual(sync_state.list_unresolved_playlist_tracks(self.conn, self.playlist), [])

    def test_handles_far_more_than_1000_unresolved_tracks_in_one_playlist(self):
        # Regression guard: an OR-chain with one term per unresolved track
        # hits SQLite's SQLITE_MAX_EXPR_DEPTH (default 1000) — reachable
        # whenever a provider connects before the first library scan (every
        # entry in every playlist is unresolved) or a large #189 golden
        # playlist syncs against a partially-covering library. 1500 keeps
        # the first sync fast while still comfortably clearing the 1000
        # boundary; a second sync that drops half of them exercises the
        # preserve-set path (not just a flat DELETE-all) at the same scale.
        first = [
            {"position": i, "artist": "A", "title": f"Song {i}", "album": None}
            for i in range(1500)
        ]
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, first)
        self.assertEqual(self._count(), 1500)

        second = first[:750]
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, second)
        rows = sync_state.list_unresolved_playlist_tracks(self.conn, self.playlist)
        self.assertEqual(len(rows), 750)
        self.assertEqual({r["title"] for r in rows}, {f"Song {i}" for i in range(750)})

    def test_cascade_deletes_with_the_playlist(self):
        sync_state.record_unresolved_playlist_tracks(self.conn, self.playlist, [
            {"position": 0, "artist": "A", "title": "Song A", "album": None},
        ])
        self.conn.execute("DELETE FROM playlists WHERE id=?", (self.playlist,))
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM unresolved_playlist_tracks"
        ).fetchone()[0]
        self.assertEqual(count, 0)


class EnrollmentGrantTests(unittest.TestCase):
    """#163: short-lived single-use enrollment codes; redeeming one creates a
    device owned by the grant's user and returns its Bearer token."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)

    def test_redeem_creates_device_owned_by_the_grant_user(self):
        code = sync_state.create_enrollment_grant(self.conn, self.user)
        result = sync_state.redeem_enrollment_grant(self.conn, code, "Pixel", "phone", None)
        assert result is not None  # narrows tuple|None for mypy + the unpack below
        device_id, token = result
        row = self.conn.execute(
            "SELECT owner_user_id, name FROM devices WHERE id=?", (device_id,)).fetchone()
        self.assertEqual(row["owner_user_id"], self.user)
        self.assertEqual(row["name"], "Pixel")
        self.assertIsNotNone(sync_state.authenticate_device(self.conn, token))  # token works

    def test_single_use(self):
        code = sync_state.create_enrollment_grant(self.conn, self.user)
        self.assertIsNotNone(sync_state.redeem_enrollment_grant(self.conn, code, "A", "phone", None))
        self.assertIsNone(sync_state.redeem_enrollment_grant(self.conn, code, "B", "phone", None))

    def _grant_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM enrollment_grants").fetchone()[0]

    def test_minting_purges_expired_grants(self):
        # #166: a stale expired grant is dropped when the next one is minted.
        old = sync_state.create_enrollment_grant(self.conn, self.user)
        self.conn.execute(
            "UPDATE enrollment_grants SET expires_at = datetime('now', '-1 minute') "
            "WHERE code_hash = ?", (sync_state.hash_token(old),))
        self.conn.commit()
        sync_state.create_enrollment_grant(self.conn, self.user)
        self.assertEqual(self._grant_count(), 1)  # only the fresh live grant survives

    def test_minting_purges_consumed_grants(self):
        # #166: a redeemed (consumed) grant is dropped on the next mint.
        code = sync_state.create_enrollment_grant(self.conn, self.user)
        sync_state.redeem_enrollment_grant(self.conn, code, "A", "phone", None)
        sync_state.create_enrollment_grant(self.conn, self.user)
        self.assertEqual(self._grant_count(), 1)  # consumed one purged, new live one kept

    def test_minting_keeps_other_live_grants(self):
        # #166: a still-valid grant must NOT be purged when another is minted.
        first = sync_state.create_enrollment_grant(self.conn, self.user)
        sync_state.create_enrollment_grant(self.conn, self.user)
        self.assertEqual(self._grant_count(), 2)
        self.assertIsNotNone(  # the first is still redeemable
            sync_state.redeem_enrollment_grant(self.conn, first, "A", "phone", None))

    def test_invalid_code_returns_none(self):
        self.assertIsNone(
            sync_state.redeem_enrollment_grant(self.conn, "BOGUS123", "A", "phone", None))

    def test_expired_code_returns_none(self):
        code = sync_state.create_enrollment_grant(self.conn, self.user)
        self.conn.execute(
            "UPDATE enrollment_grants SET expires_at = datetime('now', '-1 minute') "
            "WHERE code_hash = ?", (sync_state.hash_token(code),))
        self.conn.commit()
        self.assertIsNone(sync_state.redeem_enrollment_grant(self.conn, code, "A", "phone", None))


def _make_track_sd(conn: sqlite3.Connection, artist: str, album: str, title: str,
                   relative_path: str, size: int, duration: float | None = None) -> int:
    """A track with an explicit size (and optional duration) — the auto-fit
    budget/estimate logic is all about bytes, which the fixed-size _make_track
    helper can't express."""
    cur = conn.execute(
        "INSERT INTO tracks (relative_path, artist, album, title, size, duration, mtime) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (relative_path, artist, album, title, size, duration),
    )
    conn.commit()
    return sync_state._new_id(cur)


class DeviceSizeEstimateTests(unittest.TestCase):
    """_device_size_estimate: what a track will actually occupy on a device.
    A transcoding device stores lossless sources as CBR MP3 (bitrate/8 per
    second + slack), capped at the original; everything else is the original
    size. Getting this wrong silently over- or under-fills a card."""

    def test_non_transcoding_device_uses_original_size(self):
        self.assertEqual(
            sync_state._device_size_estimate(5_000_000, "a/x.flac", 60.0, None), 5_000_000)

    def test_transcoding_lossless_uses_bitrate_estimate(self):
        # mp3_320 = 40_000 B/s; 60s -> 2_400_000 + 262_144 slack.
        self.assertEqual(
            sync_state._device_size_estimate(5_000_000, "a/x.flac", 60.0, "mp3_320"),
            60 * 40_000 + 256 * 1024)

    def test_estimate_never_exceeds_the_original(self):
        # A suspiciously long duration must not inflate the estimate past the
        # real file — the min() cap.
        self.assertEqual(
            sync_state._device_size_estimate(100_000, "a/x.flac", 600.0, "mp3_320"), 100_000)

    def test_non_lossless_source_is_not_estimated(self):
        # An mp3 source is copied, not transcoded — original size.
        self.assertEqual(
            sync_state._device_size_estimate(3_000_000, "a/x.mp3", 60.0, "mp3_320"), 3_000_000)

    def test_missing_duration_falls_back_to_original(self):
        self.assertEqual(
            sync_state._device_size_estimate(5_000_000, "a/x.flac", None, "mp3_320"), 5_000_000)


class SumDeviceBytesTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")

    def test_empty_set_is_zero(self):
        self.assertEqual(sync_state._sum_device_bytes(self.conn, set(), self.device, None), 0)

    def test_acked_bytes_win_over_the_estimate(self):
        # Once a client reports what it actually wrote, that real figure is used
        # instead of the estimate.
        t = _make_track_sd(self.conn, "A", "H", "x", "a/x.flac", size=5_000_000, duration=60.0)
        self.conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status, bytes_on_device) "
            "VALUES (?, ?, 'downloaded', 777)", (self.device, t))
        self.conn.commit()
        self.assertEqual(
            sync_state._sum_device_bytes(self.conn, {t}, self.device, "mp3_320"), 777)

    def test_pending_track_uses_the_estimate(self):
        t = _make_track_sd(self.conn, "A", "H", "x", "a/x.flac", size=5_000_000, duration=60.0)
        self.assertEqual(
            sync_state._sum_device_bytes(self.conn, {t}, self.device, "mp3_320"),
            60 * 40_000 + 256 * 1024)


class RefreshAutofitTests(unittest.TestCase):
    """The greedy whole-album budget-fit engine — the substantive, silently
    regressing part of auto-fit. Non-transcoding device unless noted, so each
    track's on-device size is just its original `size` and budgets are exact."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)

    def _autofit_for(self, budget, transcode_format=None):
        device, _ = sync_state.create_device(
            self.conn, self.user, "phone", max_size_bytes=budget,
            transcode_format=transcode_format)
        sel = sync_state.create_autofit_selection(self.conn, device, self.user)
        return device, sel

    def _autofit_track_ids(self, sel):
        return {r["track_id"] for r in self.conn.execute(
            "SELECT track_id FROM autofit_tracks WHERE selection_id = ?", (sel,))}

    def test_no_device_selection_reports_reason(self):
        # An autofit selection with no device attached can't fit anything.
        cur = self.conn.execute(
            "INSERT INTO selections (type, target, created_by_user_id) "
            "VALUES ('autofit', '6month', ?)", (self.user,))
        sel = sync_state._new_id(cur)
        self.conn.commit()
        summary = sync_state.refresh_autofit(self.conn, sel, [("a", "h")])
        self.assertEqual(summary["reason"], "no_device")

    def test_device_without_a_size_limit_reports_reason(self):
        _, sel = self._autofit_for(budget=None)
        self.assertEqual(
            sync_state.refresh_autofit(self.conn, sel, [("a", "h")])["reason"], "no_size_limit")

    def test_manual_selections_over_budget_leave_nothing_to_fit(self):
        device, sel = self._autofit_for(budget=1000)
        # A manual artist selection already needs 2000 B > the 1000 B budget.
        _make_track_sd(self.conn, "Manual", "M", "a", "m/a.flac", size=1000)
        _make_track_sd(self.conn, "Manual", "M", "b", "m/b.flac", size=1000)
        sync_state.create_selection(self.conn, "artist", "Manual", self.user, [device])
        summary = sync_state.refresh_autofit(self.conn, sel, [("manual", "m")])
        self.assertEqual(summary["reason"], "budget_full")
        self.assertEqual(self._autofit_track_ids(sel), set())

    def test_highest_ranked_album_that_fits_is_chosen(self):
        device, sel = self._autofit_for(budget=2500)
        a1 = _make_track_sd(self.conn, "Aa", "Ha", "1", "aa/1.flac", size=1000)
        a2 = _make_track_sd(self.conn, "Aa", "Ha", "2", "aa/2.flac", size=1000)
        _make_track_sd(self.conn, "Bb", "Hb", "1", "bb/1.flac", size=1000)
        _make_track_sd(self.conn, "Bb", "Hb", "2", "bb/2.flac", size=1000)
        summary = sync_state.refresh_autofit(
            self.conn, sel, [("aa", "ha"), ("bb", "hb")])
        # Only one 2000 B album fits in 2500 B — the higher-ranked Aa.
        self.assertEqual(self._autofit_track_ids(sel), {a1, a2})
        self.assertEqual(summary["albums"], 1)
        self.assertEqual(summary["tracks"], 2)
        self.assertEqual(summary["bytes"], 2000)

    def test_a_too_big_album_is_skipped_so_a_smaller_lower_ranked_one_fits(self):
        # The key greedy property: a non-fitting top album doesn't stop the scan.
        device, sel = self._autofit_for(budget=1500)
        _make_track_sd(self.conn, "Big", "Hb", "1", "big/1.flac", size=1000)
        _make_track_sd(self.conn, "Big", "Hb", "2", "big/2.flac", size=1000)  # 2000 > 1500
        small = _make_track_sd(self.conn, "Small", "Hs", "1", "sm/1.flac", size=500)
        summary = sync_state.refresh_autofit(
            self.conn, sel, [("big", "hb"), ("small", "hs")])
        self.assertEqual(self._autofit_track_ids(sel), {small})
        self.assertEqual(summary["albums"], 1)

    def test_album_already_covered_by_a_manual_selection_is_skipped(self):
        device, sel = self._autofit_for(budget=100_000)
        m1 = _make_track_sd(self.conn, "Aa", "Ha", "1", "aa/1.flac", size=1000)
        m2 = _make_track_sd(self.conn, "Aa", "Ha", "2", "aa/2.flac", size=1000)
        sync_state.create_selection(self.conn, "artist", "Aa", self.user, [device])
        summary = sync_state.refresh_autofit(self.conn, sel, [("aa", "ha")])
        # Every track is manual — auto-fit adds nothing on top of it.
        self.assertEqual(self._autofit_track_ids(sel), set())
        self.assertEqual(summary["albums"], 0)
        self.assertNotIn(m1, self._autofit_track_ids(sel))
        self.assertNotIn(m2, self._autofit_track_ids(sel))

    def test_a_scrobbled_album_absent_from_the_library_is_skipped(self):
        _, sel = self._autofit_for(budget=100_000)
        keep = _make_track_sd(self.conn, "Have", "Hh", "1", "h/1.flac", size=1000)
        summary = sync_state.refresh_autofit(
            self.conn, sel, [("ghost", "nowhere"), ("have", "hh")])
        self.assertEqual(self._autofit_track_ids(sel), {keep})
        self.assertEqual(summary["albums"], 1)

    def test_refresh_clears_the_previous_materialization(self):
        device, sel = self._autofit_for(budget=100_000)
        _make_track_sd(self.conn, "Aa", "Ha", "1", "aa/1.flac", size=1000)
        sync_state.refresh_autofit(self.conn, sel, [("aa", "ha")])
        self.assertTrue(self._autofit_track_ids(sel))
        # A re-run with an empty ranking wipes the old set rather than appending.
        summary = sync_state.refresh_autofit(self.conn, sel, [])
        self.assertEqual(self._autofit_track_ids(sel), set())
        self.assertEqual(summary["tracks"], 0)

    def test_transcoding_shrinks_the_on_device_size_so_more_fits(self):
        # A 10 MB FLAC album won't fit a 3 MB budget as-is, but at mp3_320 its
        # on-device estimate (~2.66 MB) does — the transcode-aware budgeting.
        device, sel = self._autofit_for(budget=3_000_000, transcode_format="mp3_320")
        t = _make_track_sd(self.conn, "Aa", "Ha", "1", "aa/1.flac",
                           size=10_000_000, duration=60.0)
        summary = sync_state.refresh_autofit(self.conn, sel, [("aa", "ha")])
        self.assertEqual(self._autofit_track_ids(sel), {t})
        self.assertEqual(summary["bytes"], 60 * 40_000 + 256 * 1024)

    def _set_percent(self, device, percent):
        self.conn.execute("UPDATE devices SET autofit_percent = ? WHERE id = ?", (percent, device))
        self.conn.commit()

    def test_percent_caps_the_budget_against_the_device_limit(self):
        # #217: a 2000 B album fits a 2500 B device limit at 100%, but not at
        # 50% (a 1250 B cap) — the smaller, lower-ranked 1000 B album is
        # chosen instead, proving the cap applies before the greedy fit runs.
        device, sel = self._autofit_for(budget=2500)
        self._set_percent(device, 50)
        a1 = _make_track_sd(self.conn, "Aa", "Ha", "1", "aa/1.flac", size=1000)
        _make_track_sd(self.conn, "Aa", "Ha", "2", "aa/2.flac", size=1000)
        small = _make_track_sd(self.conn, "Small", "Hs", "1", "sm/1.flac", size=1000)
        summary = sync_state.refresh_autofit(self.conn, sel, [("aa", "ha"), ("small", "hs")])
        self.assertEqual(summary["budget_bytes"], 1250)
        self.assertEqual(self._autofit_track_ids(sel), {small})
        self.assertNotIn(a1, self._autofit_track_ids(sel))

    def test_percent_is_of_the_device_limit_not_the_remainder_after_manual(self):
        # #217's chosen semantics: percent applies to max_size_bytes itself,
        # so a manual selection eating into the budget doesn't change what
        # "50%" means — it still caps at half the device, then subtracts
        # manual bytes from THAT, not from the full device size.
        device, sel = self._autofit_for(budget=1000)
        self._set_percent(device, 50)  # cap = 500
        _make_track_sd(self.conn, "Manual", "M", "a", "m/a.flac", size=300)
        sync_state.create_selection(self.conn, "artist", "Manual", self.user, [device])
        summary = sync_state.refresh_autofit(self.conn, sel, [("a", "h")])
        self.assertEqual(summary["budget_bytes"], 500)
        self.assertEqual(summary["used_by_manual_bytes"], 300)

    def test_percent_defaults_to_100_for_a_freshly_created_device(self):
        device, sel = self._autofit_for(budget=2000)
        summary = sync_state.refresh_autofit(self.conn, sel, [])
        self.assertEqual(summary["percent"], 100)
        self.assertEqual(summary["budget_bytes"], 2000)


class AutofitFillBasisTests(unittest.TestCase):
    """#217: the percent-independent basis for the slider-preview estimate
    (max_size_bytes/manual_bytes/avg_track_bytes) — must never write
    autofit_tracks or any other state, and must scope its per-track average
    to the device's own transcode_format (a global average would be wrong
    by ~2.5x between Originals and mp3_128). The percent-dependent
    arithmetic itself is the frontend's job now (see autofitPreviewSummary
    in index.html), not this function's — kept out of a per-drag round trip
    since none of these three values change as the slider moves."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)

    def test_nonexistent_device_returns_none(self):
        self.assertIsNone(sync_state.autofit_fill_basis(self.conn, 999999))

    def test_no_size_limit_is_an_all_zero_basis(self):
        device, _ = sync_state.create_device(self.conn, self.user, "phone")
        self.assertEqual(
            sync_state.autofit_fill_basis(self.conn, device),
            {"max_size_bytes": 0, "manual_bytes": 0, "avg_track_bytes": 0})

    def test_reports_the_device_limit_and_average_track_size(self):
        device, _ = sync_state.create_device(self.conn, self.user, "phone", max_size_bytes=10_000)
        _make_track_sd(self.conn, "A", "H", "1", "a/1.mp3", size=1000)
        _make_track_sd(self.conn, "A", "H", "2", "a/2.mp3", size=1000)
        basis = sync_state.autofit_fill_basis(self.conn, device)
        assert basis is not None
        self.assertEqual(basis["max_size_bytes"], 10_000)
        self.assertEqual(basis["avg_track_bytes"], 1000)
        self.assertEqual(basis["manual_bytes"], 0)

    def test_reports_manual_selection_bytes_separately(self):
        device, _ = sync_state.create_device(self.conn, self.user, "phone", max_size_bytes=10_000)
        _make_track_sd(self.conn, "Manual", "M", "a", "m/a.mp3", size=4_000)
        sync_state.create_selection(self.conn, "artist", "Manual", self.user, [device])
        basis = sync_state.autofit_fill_basis(self.conn, device)
        assert basis is not None
        self.assertEqual(basis["manual_bytes"], 4_000)
        self.assertEqual(basis["max_size_bytes"], 10_000)  # uncapped — the caller applies percent

    def test_does_not_write_any_autofit_tracks_state(self):
        # A preview must be side-effect-free — no selection even exists here.
        device, _ = sync_state.create_device(self.conn, self.user, "phone", max_size_bytes=10_000)
        _make_track_sd(self.conn, "A", "H", "1", "a/1.mp3", size=1000)
        sync_state.autofit_fill_basis(self.conn, device)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM autofit_tracks").fetchone()["n"], 0)

    def test_average_uses_the_devices_own_transcode_format(self):
        # Same library, two devices with different formats — the average
        # must differ per device, not share one global figure.
        original_device, _ = sync_state.create_device(
            self.conn, self.user, "phone", max_size_bytes=10_000)
        mp3_device, _ = sync_state.create_device(
            self.conn, self.user, "tablet", max_size_bytes=10_000, transcode_format="mp3_128")
        _make_track_sd(self.conn, "A", "H", "1", "a/1.flac", size=10_000_000, duration=300.0)
        original = sync_state.autofit_fill_basis(self.conn, original_device)
        transcoded = sync_state.autofit_fill_basis(self.conn, mp3_device)
        assert original is not None and transcoded is not None
        self.assertGreater(original["avg_track_bytes"], transcoded["avg_track_bytes"])


class AutofitStatusTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)

    def test_disabled_when_no_autofit_selection(self):
        device, _ = sync_state.create_device(self.conn, self.user, "phone")
        # #217: percent is always reported, even before autofit is ever enabled.
        self.assertEqual(
            sync_state.autofit_status(self.conn, device), {"enabled": False, "percent": 100})

    def test_reports_period_albums_tracks_and_bytes_after_a_refresh(self):
        device, _ = sync_state.create_device(
            self.conn, self.user, "phone", max_size_bytes=100_000)
        sel = sync_state.create_autofit_selection(self.conn, device, self.user, period="12month")
        _make_track_sd(self.conn, "Aa", "Ha", "1", "aa/1.flac", size=1000)
        _make_track_sd(self.conn, "Aa", "Ha", "2", "aa/2.flac", size=1000)
        _make_track_sd(self.conn, "Bb", "Hb", "1", "bb/1.flac", size=500)
        sync_state.refresh_autofit(self.conn, sel, [("aa", "ha"), ("bb", "hb")])
        status = sync_state.autofit_status(self.conn, device)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["period"], "12month")
        self.assertEqual(status["albums"], 2)
        self.assertEqual(status["tracks"], 3)
        self.assertEqual(status["bytes"], 2500)
        # #455: the individual assertions above catch a renamed or removed
        # field (KeyError) but not an added one -- nothing references a new
        # key, so every assertion above would still pass. This is what
        # test_routes.py's test_device_shape_is_locked relies on for the
        # enabled-autofit shape, since that route calls autofit_status()
        # verbatim and its own fixture only ever has autofit disabled.
        self.assertEqual(
            set(status.keys()),
            {"enabled", "percent", "period", "albums", "tracks", "bytes"},
        )


class SyncStatusTests(unittest.TestCase):
    """trobar-server#229: sync_status() must reflect actual track-transfer
    activity (device_track_state), never devices.last_seen_at — that
    column bumps on any authenticated call, including one that found
    nothing to do, which would make an in-progress/never-synced device
    look "just synced"."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")

    def _set_state(self, track_id: int, status: str, updated_at: str) -> None:
        self.conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (self.device, track_id, status, updated_at),
        )
        self.conn.commit()

    def test_never_had_anything_to_sync(self):
        self.assertEqual(
            sync_state.sync_status(self.conn, self.device),
            {"pending_count": 0, "last_synced_at": None},
        )

    def test_pending_tracks_report_a_positive_count_and_no_last_synced_at(self):
        t1 = _make_track(self.conn, "A", "H", "1", "a/1.flac")
        t2 = _make_track(self.conn, "A", "H", "2", "a/2.flac")
        self._set_state(t1, "pending", "2026-07-24 08:00:00")
        self._set_state(t2, "pending", "2026-07-24 08:00:00")
        self.assertEqual(
            sync_state.sync_status(self.conn, self.device),
            {"pending_count": 2, "last_synced_at": None},
        )

    def test_fully_downloaded_reports_zero_pending_and_the_latest_ack_time(self):
        t1 = _make_track(self.conn, "A", "H", "1", "a/1.flac")
        t2 = _make_track(self.conn, "A", "H", "2", "a/2.flac")
        self._set_state(t1, "downloaded", "2026-07-24 08:00:00")
        self._set_state(t2, "downloaded", "2026-07-24 08:05:00")
        self.assertEqual(
            sync_state.sync_status(self.conn, self.device),
            {"pending_count": 0, "last_synced_at": "2026-07-24 08:05:00"},
        )

    def test_mixed_pending_and_downloaded_still_counts_only_pending(self):
        t1 = _make_track(self.conn, "A", "H", "1", "a/1.flac")
        t2 = _make_track(self.conn, "A", "H", "2", "a/2.flac")
        self._set_state(t1, "downloaded", "2026-07-24 08:00:00")
        self._set_state(t2, "pending", "2026-07-24 08:05:00")
        self.assertEqual(
            sync_state.sync_status(self.conn, self.device),
            {"pending_count": 1, "last_synced_at": "2026-07-24 08:00:00"},
        )

    def test_removed_and_excluded_also_count_as_already_synced(self):
        t1 = _make_track(self.conn, "A", "H", "1", "a/1.flac")
        t2 = _make_track(self.conn, "A", "H", "2", "a/2.flac")
        self._set_state(t1, "removed", "2026-07-24 08:00:00")
        self._set_state(t2, "excluded", "2026-07-24 08:10:00")
        self.assertEqual(
            sync_state.sync_status(self.conn, self.device),
            {"pending_count": 0, "last_synced_at": "2026-07-24 08:10:00"},
        )


class CreateDeviceTranscodeDefaultTests(unittest.TestCase):
    """#221: a watch device defaults to the lowest transcode tier when
    unspecified, since Connect IQ watches can't decode lossless audio and
    leaving it unset manifests as a stuck/incomplete sync rather than an
    obvious error."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)

    def _transcode_format(self, device_id: int) -> str | None:
        return self.conn.execute(
            "SELECT transcode_format FROM devices WHERE id = ?", (device_id,)
        ).fetchone()["transcode_format"]

    def test_watch_with_no_format_defaults_to_lowest_tier(self):
        device, _ = sync_state.create_device(self.conn, self.user, "My Watch", "watch")
        self.assertEqual(self._transcode_format(device), "mp3_128")

    def test_watch_explicit_format_is_not_overridden(self):
        device, _ = sync_state.create_device(
            self.conn, self.user, "My Watch", "watch", transcode_format="mp3_256")
        self.assertEqual(self._transcode_format(device), "mp3_256")

    def test_non_watch_device_type_unaffected(self):
        device, _ = sync_state.create_device(self.conn, self.user, "My Phone", "phone")
        self.assertIsNone(self._transcode_format(device))


class ParseTargetIdTests(unittest.TestCase):
    """#434: strict on purpose. A plain int() accepts several forms SQLite's
    own CAST(? AS INTEGER) -- used by main.py's _require_playlist_visible()
    against the identical target string -- does not, which is exactly what
    let a basket target be authorized against one playlist and served back
    as a different one. Public (not _int_or_none): shared across the
    module boundary with main.py, per PR #469 review -- a leading
    underscore misdescribed something meant to be called from outside."""

    def test_plain_digits_parse(self):
        self.assertEqual(sync_state.parse_target_id("42"), 42)

    def test_none_input_is_none(self):
        self.assertIsNone(sync_state.parse_target_id(None))

    def test_empty_string_is_none(self):
        self.assertIsNone(sync_state.parse_target_id(""))

    def test_non_numeric_is_none(self):
        self.assertIsNone(sync_state.parse_target_id("abc"))

    def test_trailing_junk_is_none(self):
        # CAST('1x' AS INTEGER) is 1; a bare int('1x') already raised, so
        # this direction never diverged -- pinned anyway since it's the
        # same family of malformed input.
        self.assertIsNone(sync_state.parse_target_id("1x"))

    def test_underscore_separator_is_none(self):
        # The actual #434 divergence: CAST('1_0' AS INTEGER) is 1 (leading
        # prefix only), but plain int("1_0") is 10 (PEP-515 digit
        # grouping). Neither side should now resolve this at all.
        self.assertIsNone(sync_state.parse_target_id("1_0"))

    def test_surrounding_whitespace_is_none(self):
        # CAST(' 7 ' AS INTEGER) is 7 and plain int(' 7 ') also happens to
        # agree here, but a client never sends whitespace-padded ids, so
        # the strict parser doesn't need to admit it either.
        self.assertIsNone(sync_state.parse_target_id(" 7 "))

    def test_non_ascii_digits_are_none(self):
        # Arabic-Indic '١٠' (10) is isdigit()-true and int() parses it, so
        # isascii() must be checked alongside isdigit() or this form would
        # still be admitted despite no client ever sending it.
        self.assertIsNone(sync_state.parse_target_id("١٠"))

    def test_decimal_point_is_none(self):
        self.assertIsNone(sync_state.parse_target_id("5.0"))


class ListBasketTests(unittest.TestCase):
    """#413: list_basket resolves a display title (and, for playlists, the
    source provider) server-side instead of leaving the panel to guess from
    the raw target string -- the guess is exactly how a basket'd playlist
    ended up rendering as a bare row id, since target is String(p.id) for
    that type and nothing else in it identifies the playlist."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device, _ = sync_state.create_device(self.conn, self.user, "phone")

    def _make_playlist(self, title: str, source_provider: str | None = "roon") -> int:
        cur = self.conn.execute(
            "INSERT INTO playlists (title, source_provider) VALUES (?, ?)",
            (title, source_provider),
        )
        self.conn.commit()
        return sync_state._new_id(cur)

    def _item(self, items: list[dict], item_id: int) -> dict:
        return next(i for i in items if i["id"] == item_id)

    def test_artist_title_is_the_target_itself(self):
        item_id = sync_state.add_basket_item(self.conn, self.user, "artist", "Some Artist", [self.device])
        item = self._item(sync_state.list_basket(self.conn, self.user), item_id)
        self.assertEqual(item["title"], "Some Artist")
        self.assertFalse(item["missing"])
        self.assertIsNone(item["source_provider"])

    def test_album_title_is_the_album_half_of_the_target(self):
        item_id = sync_state.add_basket_item(self.conn, self.user, "album", "Some Artist||Some Album", [self.device])
        item = self._item(sync_state.list_basket(self.conn, self.user), item_id)
        self.assertEqual(item["title"], "Some Album")
        self.assertFalse(item["missing"])

    def test_playlist_title_is_resolved_from_the_playlists_table(self):
        # The bug this closes: before, this exact case rendered as the bare
        # target string (a numeric row id), unreadable and indistinguishable
        # from any other playlist in the basket.
        playlist_id = self._make_playlist("My Great Playlist", "jellyfin")
        item_id = sync_state.add_basket_item(self.conn, self.user, "playlist", str(playlist_id), [self.device])
        item = self._item(sync_state.list_basket(self.conn, self.user), item_id)
        self.assertEqual(item["title"], "My Great Playlist")
        self.assertEqual(item["source_provider"], "jellyfin")
        self.assertFalse(item["missing"])

    def test_a_deleted_playlist_is_flagged_missing_not_silently_a_number(self):
        playlist_id = self._make_playlist("Will Be Deleted")
        item_id = sync_state.add_basket_item(self.conn, self.user, "playlist", str(playlist_id), [self.device])
        self.conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        self.conn.commit()
        item = self._item(sync_state.list_basket(self.conn, self.user), item_id)
        self.assertIsNone(item["title"])
        self.assertIsNone(item["source_provider"])
        self.assertTrue(item["missing"])

    def test_track_title_is_resolved_from_the_tracks_table(self):
        track_id = _make_track(self.conn, "A", "B", "A Track", "a/b.flac")
        item_id = sync_state.add_basket_item(self.conn, self.user, "track", str(track_id), [self.device])
        item = self._item(sync_state.list_basket(self.conn, self.user), item_id)
        self.assertEqual(item["title"], "A Track")
        self.assertFalse(item["missing"])

    def test_multiple_playlists_are_resolved_in_one_batch_not_n_plus_one(self):
        # Not a performance assertion (that needs a query-count harness this
        # file doesn't have) -- just confirms the batched IN-query path
        # correctly maps each row back to the right playlist, which is the
        # part that's easy to get wrong when batching.
        p1 = self._make_playlist("First", "roon")
        p2 = self._make_playlist("Second", "subsonic")
        id1 = sync_state.add_basket_item(self.conn, self.user, "playlist", str(p1), [self.device])
        id2 = sync_state.add_basket_item(self.conn, self.user, "playlist", str(p2), [self.device])
        items = sync_state.list_basket(self.conn, self.user)
        self.assertEqual(self._item(items, id1)["title"], "First")
        self.assertEqual(self._item(items, id1)["source_provider"], "roon")
        self.assertEqual(self._item(items, id2)["title"], "Second")
        self.assertEqual(self._item(items, id2)["source_provider"], "subsonic")

    def test_a_non_numeric_playlist_target_is_flagged_missing_not_a_crash(self):
        # #424: int(target) used to raise here, 500ing the whole endpoint --
        # the panel renders from this response, so that took the Clear
        # button (which lives inside the panel) down with it. Only reachable
        # via a malformed POST /api/basket or a hand-edited DB (#352
        # validates the type, not the target's format), but storable today.
        item_id = sync_state.add_basket_item(self.conn, self.user, "playlist", "abc", [self.device])
        item = self._item(sync_state.list_basket(self.conn, self.user), item_id)
        self.assertIsNone(item["title"])
        self.assertIsNone(item["source_provider"])
        self.assertTrue(item["missing"])

    def test_a_non_numeric_track_target_is_flagged_missing_not_a_crash(self):
        item_id = sync_state.add_basket_item(self.conn, self.user, "track", "abc", [self.device])
        item = self._item(sync_state.list_basket(self.conn, self.user), item_id)
        self.assertIsNone(item["title"])
        self.assertTrue(item["missing"])

    def test_a_non_numeric_target_does_not_break_a_valid_row_in_the_same_basket(self):
        # The bug's actual blast radius: one bad row previously took the
        # WHOLE basket down, not just itself.
        playlist_id = self._make_playlist("Still Fine", "roon")
        good_id = sync_state.add_basket_item(self.conn, self.user, "playlist", str(playlist_id), [self.device])
        bad_id = sync_state.add_basket_item(self.conn, self.user, "playlist", "not-a-number", [self.device])
        items = sync_state.list_basket(self.conn, self.user)
        self.assertEqual(self._item(items, good_id)["title"], "Still Fine")
        self.assertFalse(self._item(items, good_id)["missing"])
        self.assertTrue(self._item(items, bad_id)["missing"])

    def test_device_ids_are_reported_sorted(self):
        other_device, _ = sync_state.create_device(self.conn, self.user, "tablet")
        item_id = sync_state.add_basket_item(
            self.conn, self.user, "artist", "A", [other_device, self.device])
        item = self._item(sync_state.list_basket(self.conn, self.user), item_id)
        self.assertEqual(item["device_ids"], sorted([self.device, other_device]))


class BasketItemDevicesTests(unittest.TestCase):
    """#501: staging is per-device now -- an item can be linked to several
    devices (one basket_items row, several basket_item_devices links), and
    add_basket_item()'s own find-or-create merges new device_ids onto an
    existing row rather than creating a duplicate."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)
        self.device_a, _ = sync_state.create_device(self.conn, self.user, "phone")
        self.device_b, _ = sync_state.create_device(self.conn, self.user, "tablet")

    def _item(self, items: list[dict], item_id: int) -> dict:
        return next(i for i in items if i["id"] == item_id)

    def test_staging_the_same_target_for_a_second_device_merges_not_duplicates(self):
        id1 = sync_state.add_basket_item(self.conn, self.user, "artist", "A", [self.device_a])
        id2 = sync_state.add_basket_item(self.conn, self.user, "artist", "A", [self.device_b])
        self.assertEqual(id1, id2)
        items = sync_state.list_basket(self.conn, self.user)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["device_ids"], sorted([self.device_a, self.device_b]))

    def test_staging_for_the_same_device_twice_is_a_no_op(self):
        sync_state.add_basket_item(self.conn, self.user, "artist", "A", [self.device_a])
        sync_state.add_basket_item(self.conn, self.user, "artist", "A", [self.device_a])
        items = sync_state.list_basket(self.conn, self.user)
        self.assertEqual(items[0]["device_ids"], [self.device_a])

    def test_unstaging_one_of_two_devices_leaves_the_item_staged_for_the_other(self):
        item_id = sync_state.add_basket_item(
            self.conn, self.user, "artist", "A", [self.device_a, self.device_b])
        sync_state.unstage_basket_item_device(self.conn, self.user, item_id, self.device_a)
        items = sync_state.list_basket(self.conn, self.user)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["device_ids"], [self.device_b])

    def test_unstaging_the_last_device_deletes_the_item_entirely(self):
        item_id = sync_state.add_basket_item(self.conn, self.user, "artist", "A", [self.device_a])
        sync_state.unstage_basket_item_device(self.conn, self.user, item_id, self.device_a)
        self.assertEqual(sync_state.list_basket(self.conn, self.user), [])

    def test_unstaging_is_scoped_to_the_owning_user(self):
        other_user = _make_user(self.conn, "bob")
        item_id = sync_state.add_basket_item(self.conn, self.user, "artist", "A", [self.device_a])
        # A different user's id can't unstage someone else's item -- silently
        # a no-op (matches remove_basket_item's own ownership-scoped style),
        # not a crash or a cross-user mutation.
        sync_state.unstage_basket_item_device(self.conn, other_user, item_id, self.device_a)
        items = sync_state.list_basket(self.conn, self.user)
        self.assertEqual(items[0]["device_ids"], [self.device_a])

    def test_removing_the_whole_item_cascades_its_device_links(self):
        item_id = sync_state.add_basket_item(
            self.conn, self.user, "artist", "A", [self.device_a, self.device_b])
        sync_state.remove_basket_item(self.conn, self.user, item_id)
        links = self.conn.execute(
            "SELECT COUNT(*) AS n FROM basket_item_devices WHERE basket_item_id = ?", (item_id,)
        ).fetchone()["n"]
        self.assertEqual(links, 0)


class TransferDeviceTests(unittest.TestCase):
    """#440: "new device replaces old device" -- sync_state.transfer_device."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn)

    def test_settings_are_copied_from_old_to_new(self):
        old, _ = sync_state.create_device(
            self.conn, self.user, "Old Phone", "phone", max_size_bytes=5_000_000,
            transcode_format="mp3_320", artist_images="full")
        self.conn.execute("UPDATE devices SET source_of_truth='device', autofit_percent=42 WHERE id=?", (old,))
        self.conn.commit()
        new, _ = sync_state.create_device(self.conn, self.user, "New Phone", "phone")

        sync_state.transfer_device(self.conn, old, new)

        row = self.conn.execute(
            "SELECT transcode_format, max_size_bytes, autofit_percent, artist_images, source_of_truth, name "
            "FROM devices WHERE id = ?", (new,)).fetchone()
        self.assertEqual(row["transcode_format"], "mp3_320")
        self.assertEqual(row["max_size_bytes"], 5_000_000)
        self.assertEqual(row["autofit_percent"], 42)
        self.assertEqual(row["artist_images"], "full")
        self.assertEqual(row["source_of_truth"], "device")
        # Identity (name) is the new device's own, never overwritten.
        self.assertEqual(row["name"], "New Phone")

    def test_by_default_transferred_downloads_land_as_pending_not_downloaded(self):
        # #442 review: the safe default. A device that gets told a track is
        # 'downloaded' when it actually holds nothing (the ordinary "my
        # watch broke, here's the replacement" case) would never re-fetch
        # it -- get_changes() only ever offers 'pending' rows for download
        # -- and Garmin has no self-correcting missing-tracks report to
        # notice and fix that. So the default has to assume the new device
        # is blank unless told otherwise.
        old, _ = sync_state.create_device(self.conn, self.user, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.user, "New", "phone")
        _make_track(self.conn, "A", "H", "1", "a/1.flac")
        sel = sync_state.create_selection(self.conn, "artist", "A", self.user, [old])
        t1 = self.conn.execute(
            "SELECT track_id FROM device_track_state WHERE device_id = ?", (old,)).fetchone()["track_id"]
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded', bytes_on_device=1234 "
            "WHERE device_id=? AND track_id=?", (old, t1))
        self.conn.commit()

        sync_state.transfer_device(self.conn, old, new)  # assume_present defaults to False

        row = self.conn.execute(
            "SELECT status, bytes_on_device FROM device_track_state WHERE device_id=? AND track_id=?",
            (new, t1)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["bytes_on_device"])
        # Selections and settings still move over -- only the presence
        # claim is downgraded, nothing else about the transfer changes.
        linked = [r["device_id"] for r in self.conn.execute(
            "SELECT device_id FROM selection_devices WHERE selection_id = ?", (sel,))]
        self.assertEqual(linked, [new])

    def test_assume_present_true_carries_downloaded_status_over_when_formats_match(self):
        old, _ = sync_state.create_device(self.conn, self.user, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.user, "New", "phone")
        _make_track(self.conn, "A", "H", "1", "a/1.flac")
        # A selection covering the track, same as any real device_track_state
        # row would have -- recompute_device_state (transfer_device's final
        # step) prunes anything not required by a live selection, and this
        # selection moves to `new` along with everything else.
        sel = sync_state.create_selection(self.conn, "artist", "A", self.user, [old])
        t1 = self.conn.execute(
            "SELECT track_id FROM device_track_state WHERE device_id = ?", (old,)).fetchone()["track_id"]
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded', bytes_on_device=1234 "
            "WHERE device_id=? AND track_id=?", (old, t1))
        self.conn.commit()

        sync_state.transfer_device(self.conn, old, new, assume_present=True)

        row = self.conn.execute(
            "SELECT status, bytes_on_device FROM device_track_state WHERE device_id=? AND track_id=?",
            (new, t1)).fetchone()
        self.assertEqual(row["status"], "downloaded")
        self.assertEqual(row["bytes_on_device"], 1234)
        # And the selection itself moved over too.
        linked = [r["device_id"] for r in self.conn.execute(
            "SELECT device_id FROM selection_devices WHERE selection_id = ?", (sel,))]
        self.assertEqual(linked, [new])

    def test_new_devices_own_downloaded_rows_reset_to_pending_when_its_format_changes(self):
        # The new device already synced something under its OWN prior
        # format before the transfer ran. Once the transfer changes its
        # transcode_format to match the old device, that pre-existing
        # 'downloaded' row now names a file under the wrong extension --
        # same fix a plain PATCH format change already applies.
        old, _ = sync_state.create_device(self.conn, self.user, "Old", "phone", transcode_format="mp3_320")
        new, _ = sync_state.create_device(self.conn, self.user, "New", "phone")  # originals
        t_own = _make_track(self.conn, "Own", "H", "1", "own/1.flac")
        # Covered by a selection of its own, still assigned to `new` after
        # the transfer -- otherwise recompute_device_state would prune it
        # for a reason unrelated to the format check this test targets.
        sync_state.create_selection(self.conn, "artist", "Own", self.user, [new])
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded', bytes_on_device=999 "
            "WHERE device_id=? AND track_id=?", (new, t_own))
        self.conn.commit()

        sync_state.transfer_device(self.conn, old, new)

        row = self.conn.execute(
            "SELECT status, bytes_on_device FROM device_track_state WHERE device_id=? AND track_id=?",
            (new, t_own)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["bytes_on_device"])

    def test_old_devices_track_state_overwrites_a_conflicting_row_on_the_new_device(self):
        old, _ = sync_state.create_device(self.conn, self.user, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.user, "New", "phone")
        _make_track(self.conn, "A", "H", "1", "a/1.flac")
        # Both devices already cover the same track (a real, if unusual,
        # setup -- e.g. it was manually assigned to both before the owner
        # decided one replaces the other); old's row must win over new's
        # pre-existing one, regardless of what assume_present then does to
        # the resulting status -- assume_present=True isolates that part of
        # this test from #442's separate default-downgrade behaviour.
        sync_state.create_selection(self.conn, "artist", "A", self.user, [old, new])
        t1 = self.conn.execute(
            "SELECT track_id FROM device_track_state WHERE device_id = ?", (old,)).fetchone()["track_id"]
        self.conn.execute(
            "UPDATE device_track_state SET status='downloaded' WHERE device_id=? AND track_id=?", (old, t1))
        self.conn.commit()

        sync_state.transfer_device(self.conn, old, new, assume_present=True)

        status = _status(self.conn, new, t1)
        self.assertEqual(status, "downloaded")

    def test_selections_move_from_old_device_to_new(self):
        old, _ = sync_state.create_device(self.conn, self.user, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.user, "New", "phone")
        _make_track(self.conn, "A", "H", "1", "a/1.flac")
        sel = sync_state.create_selection(self.conn, "artist", "A", self.user, [old])

        sync_state.transfer_device(self.conn, old, new)

        linked = [r["device_id"] for r in self.conn.execute(
            "SELECT device_id FROM selection_devices WHERE selection_id = ?", (sel,))]
        self.assertEqual(linked, [new])

    def test_a_selection_already_linked_to_both_devices_collapses_without_error(self):
        old, _ = sync_state.create_device(self.conn, self.user, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.user, "New", "phone")
        _make_track(self.conn, "A", "H", "1", "a/1.flac")
        sel = sync_state.create_selection(self.conn, "artist", "A", self.user, [old, new])

        sync_state.transfer_device(self.conn, old, new)  # must not raise (UNIQUE)

        linked = [r["device_id"] for r in self.conn.execute(
            "SELECT device_id FROM selection_devices WHERE selection_id = ?", (sel,))]
        self.assertEqual(linked, [new])

    def test_old_device_is_deleted(self):
        old, _ = sync_state.create_device(self.conn, self.user, "Old", "phone")
        new, _ = sync_state.create_device(self.conn, self.user, "New", "phone")

        sync_state.transfer_device(self.conn, old, new)

        self.assertIsNone(self.conn.execute("SELECT id FROM devices WHERE id = ?", (old,)).fetchone())

    def test_returns_a_summary(self):
        old, _ = sync_state.create_device(self.conn, self.user, "Old Watch", "watch")
        new, _ = sync_state.create_device(self.conn, self.user, "New Watch", "watch")
        t1 = _make_track(self.conn, "A", "H", "1", "a/1.flac")
        t2 = _make_track(self.conn, "A", "H", "2", "a/2.flac")
        for t in (t1, t2):
            self.conn.execute(
                "INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, 'downloaded')", (old, t))
        sync_state.create_selection(self.conn, "artist", "A", self.user, [old])
        self.conn.commit()

        summary = sync_state.transfer_device(self.conn, old, new)

        self.assertEqual(summary, {"old_device_name": "Old Watch", "tracks": 2, "selections": 1})


class IntegrationTokenTests(unittest.TestCase):
    """#446/#474: Bearer tokens for external integrations -- one credential
    now authenticates both the read-only routes and the rescan action (see
    db.py's integration_tokens comment for why the earlier
    api_tokens/action_tokens split was dropped in favour of gating who may
    call create_integration_token(), not which table a token lives in).
    Admin-only minting is enforced by the caller (main.py's
    api_integration_tokens, via require_admin()), not by this module --
    these functions themselves don't know or care who's an admin, same
    division of responsibility as every other sync_state function that
    trusts its caller to have checked authorization first."""

    def setUp(self):
        self.conn = _make_conn()
        self.user = _make_user(self.conn, "alice")
        self.other = _make_user(self.conn, "bob")

    def test_create_returns_an_id_and_a_raw_token(self):
        token_id, raw = sync_state.create_integration_token(self.conn, self.user, "Home Assistant")
        self.assertIsInstance(token_id, int)
        self.assertTrue(raw)

    def test_only_the_hash_is_stored_not_the_raw_token(self):
        _, raw = sync_state.create_integration_token(self.conn, self.user, "Home Assistant")
        row = self.conn.execute("SELECT token_hash FROM integration_tokens").fetchone()
        self.assertNotEqual(row["token_hash"], raw)
        self.assertEqual(row["token_hash"], sync_state.hash_token(raw))

    def test_authenticate_resolves_a_valid_token_to_its_owner(self):
        _, raw = sync_state.create_integration_token(self.conn, self.user, "Home Assistant")
        row = sync_state.authenticate_integration_token(self.conn, raw)
        assert row is not None
        self.assertEqual(row["owner_user_id"], self.user)

    def test_authenticate_rejects_an_unknown_token(self):
        self.assertIsNone(sync_state.authenticate_integration_token(self.conn, "not-a-real-token"))

    def test_authenticate_records_last_used_at(self):
        _, raw = sync_state.create_integration_token(self.conn, self.user, "Home Assistant")
        before = sync_state.authenticate_integration_token(self.conn, raw)
        assert before is not None
        self.assertIsNone(before["last_used_at"])

        after = sync_state.authenticate_integration_token(self.conn, raw)
        assert after is not None
        self.assertIsNotNone(after["last_used_at"])

    def test_list_scopes_to_the_owner_and_never_includes_the_hash(self):
        sync_state.create_integration_token(self.conn, self.user, "Home Assistant")
        sync_state.create_integration_token(self.conn, self.user, "Grafana")
        sync_state.create_integration_token(self.conn, self.other, "Someone else's token")

        rows = sync_state.list_integration_tokens(self.conn, self.user)

        self.assertEqual({r["name"] for r in rows}, {"Home Assistant", "Grafana"})
        self.assertNotIn("token_hash", rows[0].keys())

    def test_revoke_deletes_the_token_and_it_stops_authenticating(self):
        token_id, raw = sync_state.create_integration_token(self.conn, self.user, "Home Assistant")

        self.assertTrue(sync_state.revoke_integration_token(self.conn, self.user, token_id))

        self.assertIsNone(sync_state.authenticate_integration_token(self.conn, raw))
        self.assertEqual(sync_state.list_integration_tokens(self.conn, self.user), [])

    def test_revoke_returns_false_for_an_unknown_token_id(self):
        self.assertFalse(sync_state.revoke_integration_token(self.conn, self.user, 999999))

    def test_revoke_cannot_be_used_by_a_different_owner_to_delete_someone_elses_token(self):
        # The ownership check is IN the DELETE's WHERE clause, not a
        # separate lookup beforehand -- this pins that a caller can't
        # revoke another user's token by guessing its id.
        token_id, raw = sync_state.create_integration_token(self.conn, self.user, "Home Assistant")

        self.assertFalse(sync_state.revoke_integration_token(self.conn, self.other, token_id))

        self.assertIsNotNone(sync_state.authenticate_integration_token(self.conn, raw))


if __name__ == "__main__":
    unittest.main()
