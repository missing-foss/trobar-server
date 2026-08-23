#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for lidarr_client.py — mocks requests, no network access needed.

#494: HTTP-mechanics only, same idiom as test_jellyfin_client.py's Mirror*
classes. Candidate-picking and dedup logic live in lidarr_requests.py and
are covered by test_lidarr_requests.py instead — this file only pins what
lidarr_client.py itself does: request shape, response parsing, and the
never-raises contract.

    python3 -m unittest test_lidarr_client -v      # from app/
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="trobar-test-lidarr-client-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import lidarr_client  # noqa: E402


def _resp(status_code=200, body=None):
    r = mock.Mock()
    r.status_code = status_code
    r.content = b"x" if body is not None else b""
    r.json.return_value = body if body is not None else {}
    return r


class _LidarrClientTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _configure_connection(self, url="http://lidarr.local", api_key="key1"):
        conn = db.get_conn()
        db.set_config(conn, "lidarr_url", url)
        db.set_config(conn, "lidarr_api_key", api_key)
        conn.commit()
        conn.close()

    def _configure_full(
        self, url="http://lidarr.local", api_key="key1",
        root_folder_path="/music", quality_profile_id="1", metadata_profile_id="2",
    ):
        conn = db.get_conn()
        db.set_config(conn, "lidarr_url", url)
        db.set_config(conn, "lidarr_api_key", api_key)
        db.set_config(conn, "lidarr_root_folder_path", root_folder_path)
        db.set_config(conn, "lidarr_quality_profile_id", quality_profile_id)
        db.set_config(conn, "lidarr_metadata_profile_id", metadata_profile_id)
        conn.commit()
        conn.close()


class StatusTests(_LidarrClientTestBase):
    def test_disconnected_when_unconfigured(self):
        self.assertEqual(lidarr_client.status(), {"state": "disconnected", "url": ""})

    def test_paired_when_system_status_succeeds(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(body={"version": "3.1.0.4875"})):
            self.assertEqual(
                lidarr_client.status(),
                {"state": "paired", "url": "http://lidarr.local"},
            )

    def test_disconnected_when_the_api_key_is_rejected(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(status_code=401)):
            self.assertEqual(
                lidarr_client.status(),
                {"state": "disconnected", "url": "http://lidarr.local"},
            )

    def test_url_is_echoed_even_when_disconnected(self):
        # An admin who typed a URL but a bad key should see the URL they
        # typed reflected back, not a blank field.
        self._configure_connection(api_key="")
        conn = db.get_conn()
        db.set_config(conn, "lidarr_url", "http://lidarr.local")
        db.set_config(conn, "lidarr_api_key", "")
        conn.commit()
        conn.close()
        self.assertEqual(lidarr_client.status()["url"], "http://lidarr.local")


class ReconnectTests(_LidarrClientTestBase):
    def test_persists_url_and_api_key(self):
        with mock.patch("requests.request", return_value=_resp(body={"version": "3.1.0.4875"})):
            result = lidarr_client.reconnect("http://lidarr.local", "key1")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "lidarr_url"), "http://lidarr.local")
        self.assertEqual(db.get_config(conn, "lidarr_api_key"), "key1")
        conn.close()
        self.assertEqual(result["state"], "paired")

    def test_never_touches_the_three_profile_fields(self):
        # They can't be meaningfully chosen until this pair is live — see
        # this function's own docstring.
        conn = db.get_conn()
        db.set_config(conn, "lidarr_root_folder_path", "/existing")
        conn.commit()
        conn.close()
        with mock.patch("requests.request", return_value=_resp(body={"version": "x"})):
            lidarr_client.reconnect("http://lidarr.local", "key1")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "lidarr_root_folder_path"), "/existing")
        conn.close()


class TestConnectionTests(_LidarrClientTestBase):
    """#509 item 3: test_connection() — same check as status(), against
    EXPLICIT credentials. Never touches db.py — see subsonic_client's own
    TestConnectionTests for why that's the property that actually matters
    here."""

    def test_paired_against_explicit_credentials_with_nothing_stored(self):
        with mock.patch("requests.request", return_value=_resp(body={"version": "3.1.0.4875"})):
            result = lidarr_client.test_connection("http://lidarr.local", "key1")
        self.assertEqual(result, {"state": "paired", "url": "http://lidarr.local"})

    def test_disconnected_when_the_api_key_is_rejected(self):
        with mock.patch("requests.request", return_value=_resp(status_code=401)):
            result = lidarr_client.test_connection("http://lidarr.local", "bad")
        self.assertEqual(result["state"], "disconnected")

    def test_never_persists_anything(self):
        with mock.patch("requests.request", return_value=_resp(body={"version": "3.1.0.4875"})):
            lidarr_client.test_connection("http://lidarr.local", "key1")
        conn = db.get_conn()
        self.assertIsNone(db.get_config(conn, "lidarr_url"))
        self.assertIsNone(db.get_config(conn, "lidarr_api_key"))
        conn.close()

    def test_does_not_overwrite_an_existing_stored_connection(self):
        self._configure_connection(url="http://real.example.com", api_key="realkey")
        with mock.patch("requests.request", return_value=_resp(body={"version": "x"})):
            lidarr_client.test_connection("http://typing-this.example.com", "x")
        conn = db.get_conn()
        self.assertEqual(db.get_config(conn, "lidarr_url"), "http://real.example.com")
        conn.close()


class ListOptionsTests(_LidarrClientTestBase):
    def test_root_folders_not_configured(self):
        self.assertEqual(
            lidarr_client.list_root_folders(),
            {"status": "error", "reason": "not_configured", "code": None},
        )

    def test_root_folders_maps_path_and_free_space(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(body=[
            {"path": "/music", "freeSpace": 1000}, {"path": "/downloads", "freeSpace": None},
        ])):
            result = lidarr_client.list_root_folders()
        self.assertEqual(result, {"status": "ok", "root_folders": [
            {"path": "/music", "free_space": 1000}, {"path": "/downloads", "free_space": None},
        ]})

    def test_root_folders_entries_missing_path_are_skipped(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(body=[
            {"path": "/music", "freeSpace": 1000}, {"freeSpace": 500},
        ])):
            result = lidarr_client.list_root_folders()
        self.assertEqual(result["root_folders"], [{"path": "/music", "free_space": 1000}])

    def test_root_folders_unreachable(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(status_code=500)):
            result = lidarr_client.list_root_folders()
        self.assertEqual(result, {"status": "error", "reason": "unreachable", "code": 500})

    def test_quality_profiles_not_configured(self):
        self.assertEqual(
            lidarr_client.list_quality_profiles(),
            {"status": "error", "reason": "not_configured", "code": None},
        )

    def test_quality_profiles_maps_id_and_name(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(body=[
            {"id": 1, "name": "Lossless"}, {"id": 2, "name": "Standard"},
        ])):
            result = lidarr_client.list_quality_profiles()
        self.assertEqual(result, {"status": "ok", "quality_profiles": [
            {"id": 1, "name": "Lossless"}, {"id": 2, "name": "Standard"},
        ]})

    def test_metadata_profiles_maps_id_and_name(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(body=[{"id": 1, "name": "Standard"}])):
            result = lidarr_client.list_metadata_profiles()
        self.assertEqual(result, {"status": "ok", "metadata_profiles": [{"id": 1, "name": "Standard"}]})

    def test_metadata_profiles_unreachable(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(status_code=500)):
            result = lidarr_client.list_metadata_profiles()
        self.assertEqual(result, {"status": "error", "reason": "unreachable", "code": 500})


class LookupAlbumTests(_LidarrClientTestBase):
    def test_not_configured(self):
        self.assertEqual(
            lidarr_client.lookup_album("Artist Album"),
            {"status": "error", "reason": "not_configured", "code": None},
        )

    def test_returns_raw_unfiltered_candidates(self):
        # lookup_album() deliberately does no artist-match filtering —
        # that's lidarr_requests._pick_candidate's job. Pin that this
        # function hands back exactly what Lidarr returned.
        self._configure_connection()
        candidates = [
            {"foreignAlbumId": "a1", "artist": {"artistName": "Wrong Artist"}},
            {"foreignAlbumId": "a2", "artist": {"artistName": "Right Artist"}},
        ]
        with mock.patch("requests.request", return_value=_resp(body=candidates)) as req:
            result = lidarr_client.lookup_album("Right Artist Some Album")
        self.assertEqual(result, {"status": "ok", "candidates": candidates})
        self.assertEqual(req.call_args.args[:2], ("GET", "http://lidarr.local/api/v1/album/lookup"))
        self.assertEqual(req.call_args.kwargs["params"], {"term": "Right Artist Some Album"})

    def test_unreachable(self):
        self._configure_connection()
        with mock.patch("requests.request", return_value=_resp(status_code=500)):
            result = lidarr_client.lookup_album("x")
        self.assertEqual(result, {"status": "error", "reason": "unreachable", "code": 500})

    def test_auth_header_uses_x_api_key(self):
        self._configure_connection(api_key="secret-key")
        with mock.patch("requests.request", return_value=_resp(body=[])) as req:
            lidarr_client.lookup_album("x")
        self.assertEqual(req.call_args.kwargs["headers"]["X-Api-Key"], "secret-key")


class AddAndMonitorAlbumTests(_LidarrClientTestBase):
    def test_not_configured(self):
        result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result, {
            "status": "error", "reason": "not_configured", "code": None,
            "stage": "create", "artist_id": None, "album_id": None,
        })

    def test_full_success_calls_create_then_waits_then_monitors(self):
        self._configure_full()
        create_resp = _resp(status_code=201, body={"id": 99, "artist": {"id": 7}})
        trigger_resp = _resp(status_code=201, body={"id": 55, "status": "started"})
        settled_resp = _resp(body={"id": 55, "status": "completed"})
        monitor_resp = _resp(status_code=202, body={})
        with mock.patch("requests.request", side_effect=[create_resp, trigger_resp, settled_resp, monitor_resp]) as req:
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result, {"status": "ok", "artist_id": 7, "album_id": 99})
        create_call, trigger_call, poll_call, monitor_call = req.call_args_list
        self.assertEqual(create_call.args[:2], ("POST", "http://lidarr.local/api/v1/album"))
        self.assertEqual(trigger_call.args[:2], ("POST", "http://lidarr.local/api/v1/command"))
        self.assertEqual(trigger_call.kwargs["json"], {"name": "RefreshArtist", "artistId": 7})
        self.assertEqual(poll_call.args[:2], ("GET", "http://lidarr.local/api/v1/command/55"))
        self.assertEqual(monitor_call.args[:2], ("PUT", "http://lidarr.local/api/v1/album/monitor"))

    def test_create_request_body_sets_monitor_none_and_no_search(self):
        # The two confirmed-live traps: without artist.addOptions.monitor
        # "none" the whole discography becomes wanted, and
        # searchForNewAlbum must stay false (#494's settled monitor-only
        # decision).
        self._configure_full(root_folder_path="/music", quality_profile_id="3", metadata_profile_id="4")
        create_resp = _resp(status_code=201, body={"id": 99, "artist": {"id": 7}})
        trigger_resp = _resp(status_code=201, body={"id": 55, "status": "completed"})
        monitor_resp = _resp(status_code=202, body={})
        with mock.patch("requests.request", side_effect=[create_resp, trigger_resp, monitor_resp]) as req:
            lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        body = req.call_args_list[0].kwargs["json"]
        self.assertEqual(body["foreignAlbumId"], "fa1")
        self.assertEqual(body["addOptions"], {"searchForNewAlbum": False})
        self.assertEqual(body["artist"]["foreignArtistId"], "fa-artist1")
        self.assertEqual(body["artist"]["qualityProfileId"], 3)
        self.assertEqual(body["artist"]["metadataProfileId"], 4)
        self.assertEqual(body["artist"]["rootFolderPath"], "/music")
        self.assertEqual(body["artist"]["addOptions"], {"monitor": "none"})

    def test_monitor_request_body_uses_the_created_album_id(self):
        self._configure_full()
        create_resp = _resp(status_code=201, body={"id": 99, "artist": {"id": 7}})
        trigger_resp = _resp(status_code=201, body={"id": 55, "status": "completed"})
        monitor_resp = _resp(status_code=202, body={})
        with mock.patch("requests.request", side_effect=[create_resp, trigger_resp, monitor_resp]) as req:
            lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        monitor_body = req.call_args_list[-1].kwargs["json"]
        self.assertEqual(monitor_body, {"albumIds": [99], "monitored": True})

    def test_create_failure_never_calls_monitor(self):
        self._configure_full()
        with mock.patch("requests.request", return_value=_resp(status_code=500)) as req:
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result, {
            "status": "error", "reason": "create_failed", "code": 500,
            "stage": "create", "artist_id": None, "album_id": None,
        })
        self.assertEqual(len(req.call_args_list), 1)

    def test_create_response_missing_id_is_a_create_stage_error(self):
        self._configure_full()
        with mock.patch("requests.request", return_value=_resp(status_code=201, body={"artist": {"id": 7}})):
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stage"], "create")
        self.assertEqual(result["reason"], "create_response_missing_id")

    def test_an_unrelated_400_is_still_a_create_stage_error(self):
        # Only AlbumExistsValidator gets the fallback treatment below —
        # any other validation failure (a bad rootFolderPath, an invalid
        # profile id) must still surface as a real error, not be silently
        # swallowed by the same-looking recovery path.
        self._configure_full()
        bad_folder = _resp(status_code=400, body=[
            {"propertyName": "RootFolderPath", "errorCode": "RootFolderValidator"},
        ])
        with mock.patch("requests.request", return_value=bad_folder) as req:
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result, {
            "status": "error", "reason": "create_failed", "code": 400,
            "stage": "create", "artist_id": None, "album_id": None,
        })
        self.assertEqual(len(req.call_args_list), 1)

    def test_album_already_exists_falls_back_to_the_existing_stub_and_monitors_it(self):
        # Confirmed live against Lidarr 3.1.0.4875 (see this module's own
        # docstring): requesting a second official album by an artist
        # Trobar already asked about 400s here, because the artist's deep
        # metadata refresh already created an unmonitored stub row for
        # every official release, not just the first one requested. This
        # is the recovery path, not a failure — it goes straight to
        # monitoring the existing stub, no create, no refresh wait (the
        # artist is provably already settled).
        self._configure_full()
        already_exists = _resp(status_code=400, body=[
            {"propertyName": "ForeignAlbumId", "errorMessage": "This album has already been added.",
             "errorCode": "AlbumExistsValidator"},
        ])
        lookup_resp = _resp(body=[{"id": 42, "artistId": 7, "foreignAlbumId": "fa1"}])
        monitor_resp = _resp(status_code=202, body={})
        with mock.patch("requests.request", side_effect=[already_exists, lookup_resp, monitor_resp]) as req:
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result, {"status": "ok", "artist_id": 7, "album_id": 42})
        create_call, lookup_call, monitor_call = req.call_args_list
        self.assertEqual(lookup_call.args[:2], ("GET", "http://lidarr.local/api/v1/album"))
        self.assertEqual(lookup_call.kwargs["params"], {"foreignAlbumId": "fa1"})
        monitor_body = monitor_call.kwargs["json"]
        self.assertEqual(monitor_body, {"albumIds": [42], "monitored": True})

    def test_album_already_exists_but_the_followup_lookup_fails_is_still_a_create_error(self):
        self._configure_full()
        already_exists = _resp(status_code=400, body=[
            {"propertyName": "ForeignAlbumId", "errorCode": "AlbumExistsValidator"},
        ])
        with mock.patch("requests.request", side_effect=[already_exists, _resp(status_code=500)]):
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stage"], "create")

    def test_album_already_exists_never_waits_for_an_artist_refresh(self):
        # The artist is provably already fully known in this path (that's
        # WHY the stub already exists), so there's no new-artist-refresh
        # race to close here -- pin that no extra command POST/GET happens.
        self._configure_full()
        already_exists = _resp(status_code=400, body=[
            {"propertyName": "ForeignAlbumId", "errorCode": "AlbumExistsValidator"},
        ])
        lookup_resp = _resp(body=[{"id": 42, "artistId": 7, "foreignAlbumId": "fa1"}])
        monitor_resp = _resp(status_code=202, body={})
        with mock.patch("requests.request", side_effect=[already_exists, lookup_resp, monitor_resp]) as req:
            lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(len(req.call_args_list), 3)

    def test_monitor_failure_is_a_partial_success_with_both_ids_populated(self):
        # This is the 'partial' case lidarr_requests.py never retries —
        # ids must be populated so a stuck row is traceable in Lidarr's UI.
        self._configure_full()
        create_resp = _resp(status_code=201, body={"id": 99, "artist": {"id": 7}})
        trigger_resp = _resp(status_code=201, body={"id": 55, "status": "completed"})
        with mock.patch("requests.request", side_effect=[create_resp, trigger_resp, _resp(status_code=500)]):
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result, {
            "status": "error", "reason": "monitor_failed", "code": 500,
            "stage": "monitor", "artist_id": 7, "album_id": 99,
        })

    def test_the_refresh_command_is_polled_by_id_until_it_settles(self):
        # Reproduced live against Lidarr 3.1.0.4875 (see this module's own
        # docstring): a brand-new artist's own async RefreshArtist command
        # can still be running when create returns, and finishing AFTER an
        # immediate monitor PUT silently reverts it. Pin that the
        # EXPLICITLY-triggered command is polled by its own id until it
        # reaches a terminal status before the monitor PUT is ever sent.
        self._configure_full()
        create_resp = _resp(status_code=201, body={"id": 99, "artist": {"id": 7}})
        trigger_resp = _resp(status_code=201, body={"id": 55, "status": "started"})
        still_running = _resp(body={"id": 55, "status": "started"})
        now_done = _resp(body={"id": 55, "status": "completed"})
        monitor_resp = _resp(status_code=202, body={})
        with mock.patch("time.sleep") as sleep_mock, \
             mock.patch("requests.request",
                         side_effect=[create_resp, trigger_resp, still_running, now_done, monitor_resp]) as req:
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result["status"], "ok")
        sleep_mock.assert_called_once()
        self.assertEqual(len(req.call_args_list), 5)

    def test_a_failed_terminal_refresh_still_proceeds_to_monitor(self):
        self._configure_full()
        create_resp = _resp(status_code=201, body={"id": 99, "artist": {"id": 7}})
        trigger_resp = _resp(status_code=201, body={"id": 55, "status": "started"})
        failed_resp = _resp(body={"id": 55, "status": "failed"})
        monitor_resp = _resp(status_code=202, body={})
        with mock.patch("requests.request", side_effect=[create_resp, trigger_resp, failed_resp, monitor_resp]):
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result["status"], "ok")

    def test_a_failure_triggering_the_refresh_still_proceeds_to_monitor(self):
        # Never blocks the actual monitor attempt on this side channel —
        # if triggering (or polling) the refresh itself fails outright,
        # still send the monitor PUT rather than giving up early.
        self._configure_full()
        create_resp = _resp(status_code=201, body={"id": 99, "artist": {"id": 7}})
        trigger_failed = _resp(status_code=500)
        monitor_resp = _resp(status_code=202, body={})
        with mock.patch("requests.request", side_effect=[create_resp, trigger_failed, monitor_resp]) as req:
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(req.call_args_list), 3)

    def test_gives_up_waiting_after_the_poll_budget_and_still_sends_monitor(self):
        # Never blocks a sync indefinitely -- if Lidarr is unusually slow,
        # the monitor PUT is still attempted (and may lose the race, in
        # which case lidarr_requests.py records it as 'partial' and never
        # retries -- see that module's own docstring).
        self._configure_full()
        create_resp = _resp(status_code=201, body={"id": 99, "artist": {"id": 7}})
        trigger_resp = _resp(status_code=201, body={"id": 55, "status": "started"})
        always_running = _resp(body={"id": 55, "status": "started"})
        monitor_resp = _resp(status_code=202, body={})
        responses = (
            [create_resp, trigger_resp]
            + [always_running] * lidarr_client._ARTIST_REFRESH_POLL_MAX_ATTEMPTS
            + [monitor_resp]
        )
        with mock.patch("time.sleep"), mock.patch("requests.request", side_effect=responses) as req:
            result = lidarr_client.add_and_monitor_album("fa1", "fa-artist1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(req.call_args_list), len(responses))


if __name__ == "__main__":
    unittest.main()
