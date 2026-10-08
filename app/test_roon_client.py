#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for roon_client.py -- everything that does not need a live Roon Core.

Two halves:

- **The connection's own state**: the pairing token kept on disk (owner-only,
  a corrupt file read as none) and reused when an admin moves the Core to a
  new address; the host and port from Administration before the environment;
  the three states the status dot shows; the cooldown on a pairing retry.
- **The Browse walks**, against _FakeRoon: an in-memory menu tree that
  answers browse_browse / browse_load the way the client drives them --
  pop_all to the root, an item_key to descend, pages of PAGE_SIZE. What it
  pins is the client's side of that exchange: paging to the end, finding a
  menu by exact title, the "Play ..." action rows filtered out of playlists
  and their tracks, a profile switch before the walk, and both places Roon
  puts its Artists menu.

The fake follows the client's own documented reading of the Browse API; it
cannot prove Roon still answers that way. That is the live Core's job.

    python3 -m unittest test_roon_client -v
"""
import os
import socket
import stat
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from typing import Any

import requests

_TMP = tempfile.mkdtemp(prefix="trobar-test-roon-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import roon_client  # noqa: E402
from roonapi.roonapi import RoonApiException  # noqa: E402


def _node(title, key=None, children=None, **extra):
    return {"title": title, "item_key": key or title, "children": children, **extra}


class _FakeRoon:
    """Roon's Browse hierarchy, in memory. `fail_next_browse` makes that many
    browse_browse calls answer None first, as a busy Core can."""

    def __init__(self, root_children, zones=("zone-1",), outputs=()):
        self.root = _node("root", "root", root_children)
        self.zones: dict[str, dict] = {z: {} for z in zones}
        self.outputs: dict[str, dict] = {o: {} for o in outputs}
        self.ready = True
        self.token = "paired-token"
        self.core_name = "Living Room Core"
        self.current = self.root
        self.actions: list[str] = []  # item_keys browsed that have no children: a profile switch, say
        self.fail_next_browse = 0
        self.loads = 0

    def _find(self, key, node=None):
        node = node or self.root
        if node["item_key"] == key:
            return node
        for child in node["children"] or []:
            hit = self._find(key, child)
            if hit is not None:
                return hit
        return None

    def browse_browse(self, opts):
        if self.fail_next_browse:
            self.fail_next_browse -= 1
            return None
        if opts.get("pop_all"):
            self.current = self.root
        elif "item_key" in opts:
            target = self._find(opts["item_key"])
            if target is None:
                return None
            if target["children"] is None:
                self.actions.append(target["item_key"])
                return {"action": "none"}
            self.current = target
        return {"list": {"count": len(self.current["children"])}}

    def browse_load(self, opts):
        self.loads += 1
        start = opts["offset"]
        page = self.current["children"][start:start + opts["count"]]
        return {"items": [{k: v for k, v in c.items() if k != "children"} for c in page]}

    def get_image(self, image_key, scale, width, height):
        return f"http://core.invalid/image/{image_key}?w={width}"


class _RoonBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        self._db_path = Path(path)
        db.DB_PATH = self._db_path
        db.init_db()
        self._token_dir = tempfile.mkdtemp(dir=_TMP)
        patches: list[Any] = [
            mock.patch.object(roon_client, "TOKEN_FILE", Path(self._token_dir) / "roon_token.json"),
            mock.patch.object(roon_client, "_roon", None),
            mock.patch.object(roon_client, "_last_connect_attempt", 0.0),
            mock.patch.object(roon_client, "_artist_image_key_map", None),
            mock.patch.object(roon_client, "_ENV_ROON_HOST", ""),
            mock.patch.object(roon_client, "_ENV_ROON_PORT", "9330"),
            mock.patch.object(roon_client.time, "sleep"),  # the retries and the pairing wait, instant
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._db_path.unlink(missing_ok=True)

    def _config(self, **values):
        conn = db.get_conn()
        for k, v in values.items():
            db.set_config(conn, k, v)
        conn.commit()
        conn.close()

    def _paired(self, fake):
        roon_client._roon = fake
        return fake


# --- the connection's own state --------------------------------------------

class TokenTests(_RoonBase):
    def test_no_file_and_a_corrupt_file_are_both_no_token(self):
        self.assertIsNone(roon_client._load_token())
        roon_client.TOKEN_FILE.write_text("{not json", encoding="utf-8")
        self.assertIsNone(roon_client._load_token())

    def test_a_saved_token_reads_back_and_is_owner_only(self):
        roon_client._save_token("abc")
        self.assertEqual(roon_client._load_token(), "abc")
        mode = stat.S_IMODE(os.stat(roon_client.TOKEN_FILE).st_mode)
        self.assertEqual(mode, 0o600, "the token grants control of the Core")


class HostAndConnectionTests(_RoonBase):
    def test_administration_wins_over_the_environment(self):
        with mock.patch.object(roon_client, "_ENV_ROON_HOST", "env-host"), \
                mock.patch.object(roon_client, "_ENV_ROON_PORT", "9100"):
            self.assertEqual(roon_client._current_host_port(), ("env-host", 9100))
            self._config(roon_host="admin-host", roon_port="9331")
            self.assertEqual(roon_client._current_host_port(), ("admin-host", 9331))

    def test_no_host_anywhere_stays_disconnected_without_trying(self):
        with mock.patch.object(roon_client, "RoonApi") as api:
            roon_client.ensure_started()
            api.assert_not_called()
        self.assertEqual(roon_client.status()["state"], "disconnected")

    def test_the_saved_token_is_reused_on_connect(self):
        roon_client._save_token("kept")
        self._config(roon_host="core.invalid", roon_port="9330")
        with mock.patch.object(roon_client, "RoonApi") as api:
            roon_client.ensure_started()
        self.assertEqual(api.call_args.args[1:4], ("kept", "core.invalid", 9330))

    def test_a_connect_that_fails_leaves_it_disconnected(self):
        self._config(roon_host="core.invalid")
        with mock.patch.object(roon_client, "RoonApi", side_effect=RoonApiException("refused")), \
                mock.patch("builtins.print"):
            roon_client.ensure_started()
        self.assertIsNone(roon_client._roon)

    def test_a_new_address_is_stored_and_connected_to_with_the_same_token(self):
        # Roon ties the pairing to the Core, not its address: moving the Core
        # must not cost a re-approval.
        roon_client._save_token("kept")
        with mock.patch.object(roon_client, "RoonApi", return_value=_FakeRoon([])) as api:
            state = roon_client.reconnect("new-core.invalid", 9400)
        self.assertEqual(api.call_args.args[1:4], ("kept", "new-core.invalid", 9400))
        self.assertEqual(roon_client._current_host_port(), ("new-core.invalid", 9400))
        self.assertEqual(state["state"], "paired")

    def test_status_says_pending_until_the_core_approves(self):
        fake = self._paired(_FakeRoon([]))
        fake.ready, fake.token = True, None
        self.assertEqual(roon_client.status()["state"], "pending_approval")

    def test_status_when_paired_names_the_core_and_saves_a_changed_token(self):
        self._paired(_FakeRoon([]))
        roon_client._save_token("old")
        st = roon_client.status()
        self.assertEqual((st["state"], st["core_name"]), ("paired", "Living Room Core"))
        self.assertEqual(roon_client._load_token(), "paired-token")

    def test_a_retry_within_the_cooldown_does_not_reconnect(self):
        self._config(roon_host="core.invalid")
        self._paired(_FakeRoon([]))
        roon_client._last_connect_attempt = roon_client.time.time()
        with mock.patch.object(roon_client, "RoonApi", return_value=_FakeRoon([])) as api:
            roon_client.retry_pairing()
            api.assert_not_called()
        roon_client._last_connect_attempt = 0.0
        with mock.patch.object(roon_client, "RoonApi", return_value=_FakeRoon([])) as api:
            roon_client.retry_pairing()
            api.assert_called_once()


# --- the Browse walks ------------------------------------------------------

def _library(playlists=None, profiles=None, artists_at="library", artists=()):
    artist_nodes = [_node(a, f"artist:{a}", [], image_key=f"img:{a}") for a in artists]
    artists_menu = _node("Artists", "artists", artist_nodes)
    # Where Roon puts its Artists menu: under Library; at the root with no
    # Library menu; or at the root beside a Library menu that lacks it.
    root = {
        "library": [_node("Library", "library", [artists_menu])],
        "root": [artists_menu],
        "root_beside_library": [_node("Library", "library", []), artists_menu],
    }[artists_at]
    if playlists is not None:
        root.append(_node("Playlists", "playlists", [
            _node("Play Playlist", "play-all"),
            *[_node(title, f"pl:{title}", tracks) for title, tracks in playlists],
        ]))
    if profiles is not None:
        root.append(_node("Settings", "settings", [
            _node("Profile", "profile", [_node(name, f"profile:{name}", None, subtitle=sub) for name, sub in profiles]),
        ]))
    return root


class TestConnectionTests(_RoonBase):
    """test_connection is the only check before a switch to Roon: a real
    socket each way, no Core needed. Port 0 lets the kernel assign an
    ephemeral port, which can't collide with a port band any account's test
    services use, and can't run out the way a fixed range can."""

    def _bound(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        return sock

    def test_a_listening_port_is_reachable(self):
        sock = self._bound()
        try:
            sock.listen(1)
            port = sock.getsockname()[1]
            self.assertEqual(roon_client.test_connection("127.0.0.1", port)["state"], "reachable")
        finally:
            sock.close()

    def test_a_closed_port_is_disconnected(self):
        sock = self._bound()
        port = sock.getsockname()[1]
        sock.close()  # bound, never listened, now free: nothing answers there
        self.assertEqual(roon_client.test_connection("127.0.0.1", port)["state"], "disconnected")

    def test_it_stores_nothing_and_opens_no_roon_connection(self):
        self._config(roon_host="core.example", roon_port="9330")
        with mock.patch.object(roon_client, "RoonApi") as api:
            roon_client.test_connection("127.0.0.1", 9)
        api.assert_not_called()
        conn = db.get_conn()
        try:
            self.assertEqual((db.get_config(conn, "roon_host"), db.get_config(conn, "roon_port")),
                             ("core.example", "9330"))
        finally:
            conn.close()


class BrowseTests(_RoonBase):
    def test_nothing_is_browsed_before_pairing(self):
        self.assertEqual(roon_client.list_playlists(), {"status": "error", "reason": "not_paired"})
        self.assertEqual(roon_client.list_profiles()["reason"], "not_paired")
        self.assertIsNone(roon_client.get_artist_image("Anyone"))

    def test_paging_reads_every_item_and_stops_at_the_count(self):
        many: list[tuple[str, list]] = [(f"List {i:03}", []) for i in range(roon_client.PAGE_SIZE + 5)]
        fake = self._paired(_FakeRoon(_library(playlists=many)))
        out = roon_client.list_playlists()
        self.assertEqual(len(out["playlists"]), roon_client.PAGE_SIZE + 5)
        self.assertTrue(all(p["id"] is None for p in out["playlists"]))  # Roon exposes no stable id
        self.assertNotIn("Play Playlist", [p["title"] for p in out["playlists"]])
        self.assertGreaterEqual(fake.loads, 2)

    def test_a_core_with_no_playlists_menu_has_none(self):
        self._paired(_FakeRoon(_library()))
        self.assertEqual(roon_client.list_playlists(), {"status": "ok", "playlists": []})

    def test_no_zone_or_output_is_an_error_not_a_hang(self):
        self._paired(_FakeRoon(_library(playlists=[]), zones=()))
        self.assertEqual(roon_client.list_playlists()["reason"], "no_zone_available")

    def test_an_output_serves_when_there_is_no_zone(self):
        self._paired(_FakeRoon(_library(playlists=[("Mix", [])]), zones=(), outputs=("out-1",)))
        self.assertEqual(roon_client.list_playlists()["status"], "ok")

    def test_a_busy_core_is_retried_before_giving_up(self):
        fake = self._paired(_FakeRoon(_library(playlists=[("Mix", [])])))
        fake.fail_next_browse = 2  # within the two retries
        self.assertEqual(roon_client.list_playlists()["status"], "ok")
        fake.fail_next_browse = 3  # one more than it waits for
        self.assertEqual(roon_client.list_playlists()["reason"], "browse_browse root failed")

    def test_a_playlists_tracks_come_in_order_without_its_play_actions(self):
        tracks = [_node("Play Now", "act"), _node("Roygbiv", "t1", [], subtitle="Boards of Canada"),
                  _node("Xtal", "t2", [], subtitle="Aphex Twin")]
        self._paired(_FakeRoon(_library(playlists=[("Road Trip", tracks)])))
        out = roon_client.get_playlist_tracks("Road Trip")
        self.assertEqual(out["tracks"], [
            {"position": 0, "title": "Roygbiv", "artist": "Boards of Canada"},
            {"position": 1, "title": "Xtal", "artist": "Aphex Twin"},
        ])

    def test_a_missing_playlist_names_the_step_that_failed(self):
        self._paired(_FakeRoon(_library(playlists=[("Road Trip", [])])))
        self.assertEqual(roon_client.get_playlist_tracks("road trip"),  # exact title, not case-folded
                         {"status": "not_found", "failed_segment": "road trip"})

    def test_a_profile_is_switched_to_before_the_walk_and_a_missing_one_says_so(self):
        fake = self._paired(_FakeRoon(_library(playlists=[("Mix", [])], profiles=[("Alice", "selected"), ("Bob", "")])))
        self.assertEqual(roon_client.list_playlists(roon_profile="Bob")["status"], "ok")
        self.assertEqual(fake.actions, ["profile:Bob"])  # browsing the profile item *is* the switch
        self.assertEqual(roon_client.get_playlist_tracks("Mix", roon_profile="Carol"),
                         {"status": "error", "reason": "profile_not_found"})

    def test_profiles_list_with_the_active_one_marked(self):
        self._paired(_FakeRoon(_library(profiles=[("Alice", "selected"), ("Bob", "")])))
        self.assertEqual(roon_client.list_profiles(), {"status": "ok", "profiles": [
            {"title": "Alice", "selected": True}, {"title": "Bob", "selected": False},
        ]})

    def test_a_single_profile_core_has_an_empty_list_not_an_error(self):
        self._paired(_FakeRoon(_library()))
        self.assertEqual(roon_client.list_profiles(), {"status": "ok", "profiles": []})


class ArtistImageTests(_RoonBase):
    def _image_response(self, status=200):
        r = mock.Mock()
        r.content, r.headers = b"jpeg bytes", {"Content-Type": "image/png"}
        r.raise_for_status.side_effect = requests.HTTPError("gone") if status != 200 else None
        return r

    def test_the_artists_menu_is_found_wherever_roon_puts_it(self):
        # The third shape is the one a stateful walk gets wrong: a Library
        # menu without Artists, entered and missed, must not leave the next
        # attempt searching inside it rather than at the root.
        for artists_at in ("library", "root", "root_beside_library"):
            with self.subTest(artists_at=artists_at):
                roon_client._artist_image_key_map = None
                self._paired(_FakeRoon(_library(artists=["Autechre"], artists_at=artists_at)))
                with mock.patch("requests.get", return_value=self._image_response()) as get:
                    self.assertEqual(roon_client.get_artist_image("Autechre"), (b"jpeg bytes", "image/png"))
                self.assertIn("img:Autechre", get.call_args.args[0])

    def test_the_artist_list_is_walked_once_then_answered_from_memory(self):
        fake = self._paired(_FakeRoon(_library(artists=["Autechre", "Plaid"])))
        with mock.patch("requests.get", return_value=self._image_response()):
            roon_client.get_artist_image("Autechre")
            loads = fake.loads
            roon_client.get_artist_image("Plaid")
        self.assertEqual(fake.loads, loads, "a second lookup re-walked the artist list")

    def test_an_unknown_artist_or_an_image_that_fails_is_no_picture(self):
        self._paired(_FakeRoon(_library(artists=["Autechre"])))
        with mock.patch("requests.get") as get:
            self.assertIsNone(roon_client.get_artist_image("Nobody"))
            get.assert_not_called()
        with mock.patch("requests.get", return_value=self._image_response(status=404)):
            self.assertIsNone(roon_client.get_artist_image("Autechre"))


if __name__ == "__main__":
    unittest.main()
