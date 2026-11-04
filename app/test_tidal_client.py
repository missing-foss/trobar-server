#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for tidal_client.py — mocks requests, no network access needed.

    python3 -m unittest test_tidal_client -v
"""
import unittest
import unittest.mock as mock
from typing import Any

import requests

import tidal_client


def _resp(status_code=200, json_body=None, headers=None):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.headers = headers or {}   # real dict so .get("Retry-After") -> None
    if status_code >= 400:
        # Real requests attaches the response to the HTTPError; mirror that so
        # _error_reason() can read the status off e.response (#127).
        err = requests.HTTPError(f"{status_code}")
        err.response = r
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    return r


class RefreshAccessTokenTests(unittest.TestCase):
    def test_returns_access_and_possibly_new_refresh_token(self):
        with mock.patch("requests.post", return_value=_resp(json_body={
            "access_token": "at1", "refresh_token": "rt2",
        })) as post:
            access, refresh = tidal_client.refresh_access_token("cid", "csecret", "rt1")
        self.assertEqual(access, "at1")
        self.assertEqual(refresh, "rt2")
        self.assertEqual(post.call_args.kwargs["auth"], ("cid", "csecret"))
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "rt1")

    def test_falls_back_to_input_refresh_token_when_not_rotated(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"access_token": "at1"})):
            access, refresh = tidal_client.refresh_access_token("cid", "csecret", "rt1")
        self.assertEqual(refresh, "rt1")

    def test_401_raises_auth_error(self):
        with mock.patch("requests.post", return_value=_resp(status_code=401)):
            with self.assertRaises(tidal_client.TidalAuthError):
                tidal_client.refresh_access_token("cid", "csecret", "bad-token")

    def test_connection_failure_raises_transient_error_not_auth_error(self):
        # A network blip must never be mistaken for a dead credential —
        # the caller (playlist_sync.py) treats these very differently
        # (retry next sync vs. clear the stored refresh_token).
        with mock.patch("requests.post", side_effect=requests.ConnectionError()):
            with self.assertRaises(tidal_client.TidalTransientError):
                tidal_client.refresh_access_token("cid", "csecret", "rt1")

    def test_server_error_raises_transient_error(self):
        with mock.patch("requests.post", return_value=_resp(status_code=503)):
            with self.assertRaises(tidal_client.TidalTransientError):
                tidal_client.refresh_access_token("cid", "csecret", "rt1")

    def test_malformed_response_raises_transient_error_not_a_crash(self):
        with mock.patch("requests.post", return_value=_resp(json_body={"no_access_token": True})):
            with self.assertRaises(tidal_client.TidalTransientError):
                tidal_client.refresh_access_token("cid", "csecret", "rt1")


class GetCurrentUserTests(unittest.TestCase):
    def test_happy_path(self):
        user_resp = _resp(json_body={"data": {"id": "12345", "attributes": {"username": "alice"}}})
        with mock.patch("requests.get", return_value=user_resp):
            result = tidal_client.get_current_user("at1")
        self.assertEqual(result, {"status": "ok", "user_id": "12345", "display_name": "alice"})

    def test_malformed_response_is_a_clean_error_not_a_crash(self):
        bad_resp = _resp(json_body={"unexpected": "shape"})
        with mock.patch("requests.get", return_value=bad_resp):
            result = tidal_client.get_current_user("at1")
        self.assertEqual(result, {"status": "error"})

    def test_request_failure_is_a_clean_error_not_a_crash(self):
        with mock.patch("requests.get", side_effect=requests.ConnectionError()):
            result = tidal_client.get_current_user("at1")
        self.assertEqual(result, {"status": "error"})


# --- Fixtures for the #132 collection endpoint ---
# /userCollectionPlaylists/me/relationships/items is a JSON:API relationship:
# `data` holds {type: "playlists", id} refs, the playlist objects (with
# attributes.name) are sideloaded into `included` via include=items, and
# it paginates by the same opaque links.next cursor as the items endpoint.


def _collection_page(entries, next_cursor=None):  # entries: [(id, name|None), ...]
    body: dict[str, Any] = {
        "data": [{"type": "playlists", "id": pid} for pid, _ in entries],
        "included": [{"type": "playlists", "id": pid, "attributes": {"name": name}}
                     for pid, name in entries if name is not None],
    }
    if next_cursor:
        body["links"] = {
            "next": f"/userCollectionPlaylists/me/relationships/items?page[cursor]={next_cursor}"}
    else:
        body["links"] = {"self": "/userCollectionPlaylists/me/relationships/items"}
    return body


class ListPlaylistsTests(unittest.TestCase):
    def test_resolves_collection_playlists_from_included(self):
        # #75: returns {id, title} dicts (id drives the sync's composite key).
        # #132: names come from the sideloaded `included`, not each ref.
        page = _collection_page([("p1", "Road Trip"), ("p2", "Chill")])
        with mock.patch("requests.get", return_value=_resp(json_body=page)) as get:
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["playlists"], [
            {"id": "p1", "title": "Road Trip"}, {"id": "p2", "title": "Chill"}])
        # hits the collection endpoint (not the owned-only filter), sideloading
        url = get.call_args[0][0]
        self.assertTrue(url.endswith("/userCollectionPlaylists/me/relationships/items"), url)
        self.assertEqual(get.call_args.kwargs["params"], {"include": "items", "collectionView": "FLAT"})
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer at1")

    def test_paginates_the_collection(self):
        # The saved playlists past the endpoint's per-page cap must be
        # included — the whole point of #132 (53 > one page).
        pages = [_collection_page([("p1", "A"), ("p2", "B")], next_cursor="C"),
                 _collection_page([("p3", "C3")])]
        calls = {"n": 0}

        def side_effect(url, params=None, headers=None, timeout=None):
            page = pages[1] if "cursor" in url else pages[0]
            calls["n"] += 1
            return _resp(json_body=page)

        with mock.patch("requests.get", side_effect=side_effect):
            result = tidal_client.list_playlists("at1")
        self.assertEqual([p["id"] for p in result["playlists"]], ["p1", "p2", "p3"])
        self.assertEqual(calls["n"], 2)

    def test_skips_refs_with_no_sideloaded_name(self):
        # A ref whose playlist object isn't in `included` (or has no name) is
        # skipped rather than crashing or landing a titleless playlist.
        page = {
            "data": [{"type": "playlists", "id": "p1"},
                     {"type": "playlists", "id": "missing"}],
            "included": [{"type": "playlists", "id": "p1", "attributes": {"name": "Keep"}}],
            "links": {"self": "x"},
        }
        with mock.patch("requests.get", return_value=_resp(json_body=page)):
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result["playlists"], [{"id": "p1", "title": "Keep"}])

    def test_refs_of_another_type_are_an_error_not_an_empty_list(self):
        # An empty "ok" would make the sync delete every Tidal playlist.
        page = {"data": [{"type": "userCollectionPlaylistItems", "id": "p1"}],
                "included": [{"type": "playlists", "id": "p1", "attributes": {"name": "Keep"}}],
                "links": {"self": "x"}}
        with mock.patch("requests.get", return_value=_resp(json_body=page)):
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "unexpected_shape"})

    def test_refs_with_nothing_sideloaded_are_an_error_not_an_empty_list(self):
        page = {"data": [{"type": "playlists", "id": "p1"}, {"type": "playlists", "id": "p2"}],
                "links": {"self": "x"}}
        with mock.patch("requests.get", return_value=_resp(json_body=page)):
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "unexpected_shape"})

    def test_an_empty_collection_is_ok(self):
        page = {"data": [], "included": [], "links": {"self": "x"}}
        with mock.patch("requests.get", return_value=_resp(json_body=page)):
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result, {"status": "ok", "playlists": []})

    def test_a_retired_route_is_a_clean_error(self):
        # The old /userCollections route answered 404 in production.
        with mock.patch("requests.get", return_value=_resp(status_code=404)):
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "http_404"})

    def test_request_failure_is_a_clean_error(self):
        # A connection error is retried (#127); after the attempts are
        # exhausted it's a clean, reasoned error — not a crash.
        with mock.patch("time.sleep"), \
                mock.patch("requests.get", side_effect=requests.ConnectionError()):
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "network"})


# --- Fixtures mirroring the REAL Tidal v2 shapes captured ---
#
# 1. /playlists/{id}/relationships/items caps at 20/page and paginates by an
#    opaque cursor in links.next; its included track objects carry a title but
#    NO artist and NO `relationships` key.
# 2. Artists come only from GET /tracks?filter[id]=...&include=artists, where
#    each track's relationships.artists.data[0] points at a sideloaded
#    `artists` object with attributes.name.


def _item_ref(tid):
    return {"type": "tracks", "id": tid}


def _item_included(tid, title):
    # The real items-endpoint track object: title, no artists, no relationships.
    return {"type": "tracks", "id": tid,
            "attributes": {"title": title, "isrc": "X", "duration": "PT3M"}}


def _items_page(ids, next_cursor=None):
    body: dict[str, Any] = {"data": [_item_ref(t) for t in ids],
                            "included": [_item_included(t, f"Title {t}") for t in ids]}
    if next_cursor:
        rel = f"/playlists/pl1/relationships/items?page[cursor]={next_cursor}"
        body["links"] = {"next": rel, "meta": {"nextCursor": next_cursor}}
    else:
        body["links"] = {"self": "/playlists/pl1/relationships/items"}
    return body


def _tracks_batch(ids, artist_of, omit=()):  # artist_of: tid -> (artist_id, name)
    data, included, seen = [], [], set()
    for tid in ids:
        if tid in omit:
            continue
        aid, name = artist_of.get(tid, (None, None))
        rels = {}
        if aid is not None:
            rels = {"artists": {"data": [{"type": "artists", "id": aid}]}}
            if aid not in seen:  # deduped in `included`, like real JSON:API
                seen.add(aid)
                included.append({"type": "artists", "id": aid,
                                 "attributes": {"name": name}})
        data.append({"type": "tracks", "id": tid,
                     "attributes": {"title": f"Title {tid}"},
                     "relationships": rels})
    return {"data": data, "included": included}


def _router(pages, tracks_by_chunk):
    """Build a requests.get side_effect routing items pages vs /tracks batches."""
    calls: dict[str, Any] = {"items": 0, "tracks": []}

    def _side_effect(url, params=None, headers=None, timeout=None):
        params = params or {}
        if "/tracks" in url and "playlists" not in url:
            ids = params["filter[id]"].split(",")
            calls["tracks"].append(ids)
            return _resp(json_body=tracks_by_chunk(ids))
        if "relationships/items" in url:
            page = pages[1] if "cursor" in url else pages[0]
            calls["items"] += 1
            return _resp(json_body=page)
        return _resp(json_body={})

    return _side_effect, calls


class GetPlaylistTracksTests(unittest.TestCase):
    def test_paginates_past_the_20_cap_and_resolves_artists(self):
        # 25 tracks across two item pages (proving we don't stop at page 1),
        # artists resolved from batched /tracks (proving Bug 2's real source).
        ids = [f"t{i}" for i in range(25)]
        artist_of = {t: (f"a{i}", f"Artist {i}") for i, t in enumerate(ids)}
        pages = [_items_page(ids[:20], next_cursor="CUR"), _items_page(ids[20:])]
        side_effect, calls = _router(pages, lambda chunk: _tracks_batch(chunk, artist_of))

        with mock.patch("requests.get", side_effect=side_effect):
            result = tidal_client.get_playlist_tracks(
                "Road Trip", source_playlist_id="pl1", access_token="at1")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["tracks"]), 25)  # not silently capped at 20
        self.assertEqual(result["tracks"][0],
                         {"position": 0, "artist": "Artist 0", "title": "Title t0", "album": None, "isrc": "X"})
        self.assertEqual(result["tracks"][24]["artist"], "Artist 24")
        self.assertEqual([t["position"] for t in result["tracks"]], list(range(25)))
        # two item pages walked, and /tracks batched at 20 (chunks of 20 + 5)
        self.assertEqual(calls["items"], 2)
        self.assertEqual([len(c) for c in calls["tracks"]], [20, 5])
        # artist lookups address /tracks by filter[id]+include=artists
        # (checked implicitly by the router; assert the id set is exhaustive)
        self.assertEqual(sorted(sum(calls["tracks"], [])), sorted(ids))

    def test_single_page_artist_resolution(self):
        ids = ["t1", "t2"]
        artist_of = {"t1": ("a1", "Artist A"), "t2": ("a2", "Artist B")}
        pages = [_items_page(ids), None]
        side_effect, _ = _router(pages, lambda chunk: _tracks_batch(chunk, artist_of))
        with mock.patch("requests.get", side_effect=side_effect) as get:
            result = tidal_client.get_playlist_tracks(
                "Road Trip", source_playlist_id="pl1", access_token="at1")
        self.assertEqual(result["tracks"], [
            {"position": 0, "artist": "Artist A", "title": "Title t1", "album": None, "isrc": "X"},
            {"position": 1, "artist": "Artist B", "title": "Title t2", "album": None, "isrc": "X"},
        ])
        self.assertIn("/playlists/pl1/relationships/items", get.call_args_list[0][0][0])

    def test_track_missing_from_tracks_batch_keeps_title_empty_artist(self):
        # A track present in the playlist but absent from /tracks (e.g. pulled
        # from the catalog) keeps its items-title with an empty artist rather
        # than vanishing; one with neither title nor artist is dropped.
        ids = ["t1", "t2"]
        artist_of = {"t1": ("a1", "Artist A")}  # t2 has no artist entry
        pages = [_items_page(ids), None]
        # /tracks omits t2 entirely
        side_effect, _ = _router(pages, lambda chunk: _tracks_batch(chunk, artist_of, omit=("t2",)))
        with mock.patch("requests.get", side_effect=side_effect):
            result = tidal_client.get_playlist_tracks(
                "Road Trip", source_playlist_id="pl1", access_token="at1")
        self.assertEqual(result["tracks"], [
            {"position": 0, "artist": "Artist A", "title": "Title t1", "album": None, "isrc": "X"},
            {"position": 1, "artist": "", "title": "Title t2", "album": None, "isrc": "X"},
        ])

    def test_no_source_id_is_a_clean_error(self):
        # Without a source_playlist_id there's nothing to fetch (Tidal is an
        # id-provider) — a clean error, no request attempted.
        with mock.patch("requests.get") as get:
            result = tidal_client.get_playlist_tracks("Whatever", access_token="at1", tidal_user_id="12345")
        self.assertEqual(result, {"status": "error", "reason": "not_found"})
        get.assert_not_called()

    def test_request_failure_is_a_clean_error_not_a_crash(self):
        with mock.patch("time.sleep"), \
                mock.patch("requests.get", side_effect=requests.ConnectionError()):
            result = tidal_client.get_playlist_tracks(
                "Road Trip", source_playlist_id="pl1", access_token="at1")
        self.assertEqual(result, {"status": "error", "reason": "network"})

    def test_empty_playlist_is_ok_with_no_tracks(self):
        pages = [_items_page([]), None]
        side_effect, _ = _router(pages, lambda chunk: _tracks_batch(chunk, {}))
        with mock.patch("requests.get", side_effect=side_effect):
            result = tidal_client.get_playlist_tracks(
                "Empty", source_playlist_id="pl1", access_token="at1")
        self.assertEqual(result, {"status": "ok", "tracks": []})


class RetryAndTruncationTests(unittest.TestCase):
    """#127: _auth_get retries 429/5xx/connection blips with backoff (honoring
    Retry-After); a persistent failure gives up with a distinguishable reason;
    the _MAX_PAGES valve logs a warning if it ever truncates. Exercised through
    list_playlists (the simplest _auth_get caller)."""

    def test_retries_on_429_then_succeeds(self):
        page = _collection_page([("p1", "A")])
        with mock.patch("time.sleep") as sleep, \
                mock.patch("requests.get",
                           side_effect=[_resp(status_code=429), _resp(json_body=page)]) as get:
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["playlists"], [{"id": "p1", "title": "A"}])
        self.assertEqual(get.call_count, 2)   # one retry
        sleep.assert_called_once()

    def test_retries_on_5xx_then_succeeds(self):
        page = _collection_page([("p1", "A")])
        with mock.patch("time.sleep"), \
                mock.patch("requests.get",
                           side_effect=[_resp(status_code=503), _resp(json_body=page)]):
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result["status"], "ok")

    def test_persistent_429_gives_up_with_rate_limited_reason(self):
        with mock.patch("time.sleep"), \
                mock.patch("requests.get", return_value=_resp(status_code=429)) as get:
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "rate_limited"})
        self.assertEqual(get.call_count, tidal_client._RETRY_ATTEMPTS)

    def test_honors_retry_after_header(self):
        page = _collection_page([("p1", "A")])
        r429 = _resp(status_code=429, headers={"Retry-After": "7"})
        with mock.patch("time.sleep") as sleep, \
                mock.patch("requests.get", side_effect=[r429, _resp(json_body=page)]):
            tidal_client.list_playlists("at1")
        sleep.assert_called_once_with(7.0)

    def test_non_retryable_4xx_is_not_retried(self):
        with mock.patch("time.sleep"), \
                mock.patch("requests.get", return_value=_resp(status_code=404)) as get:
            result = tidal_client.list_playlists("at1")
        self.assertEqual(result, {"status": "error", "reason": "http_404"})
        self.assertEqual(get.call_count, 1)   # no retry on a 404

    def test_max_pages_truncation_logs_a_warning(self):
        # Every page keeps advertising a next cursor, so the valve trips.
        page = _collection_page([("p1", "A")], next_cursor="always")
        with mock.patch.object(tidal_client, "_MAX_PAGES", 2), \
                mock.patch("requests.get", return_value=_resp(json_body=page)):
            with self.assertLogs("tidal_client", level="WARNING") as cm:
                result = tidal_client.list_playlists("at1")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any("_MAX_PAGES" in line for line in cm.output))


class WriteTests(unittest.TestCase):
    """The write calls: request shapes from the published schema, batching
    at its per-request caps, retries that reuse one Idempotency-Key, and
    the occurrence ids a removal needs."""

    def _calls(self, req):
        return [(c.args[0], c.args[1].removeprefix(tidal_client.API_BASE), c.kwargs["json"])
                for c in req.call_args_list]

    def test_create_is_unlisted_and_returns_the_id(self):
        with mock.patch("requests.request",
                        return_value=_resp(status_code=201, json_body={"data": {"id": "new1"}})) as req:
            result = tidal_client.create_playlist("at1", "Road Trip")
        self.assertEqual(result, {"status": "ok", "id": "new1"})
        method, path, body = self._calls(req)[0]
        self.assertEqual((method, path), ("POST", "/playlists"))
        self.assertEqual(body["data"]["attributes"], {"name": "Road Trip", "accessType": "UNLISTED"})
        self.assertEqual(req.call_args.kwargs["headers"]["Content-Type"], "application/vnd.api+json")

    def test_create_with_no_id_in_the_answer_is_an_error(self):
        with mock.patch("requests.request", return_value=_resp(status_code=201, json_body={"data": {}})):
            result = tidal_client.create_playlist("at1", "Road Trip")
        self.assertEqual(result, {"status": "error", "reason": "parse_error"})

    def test_rename_and_delete_target_the_given_id(self):
        with mock.patch("requests.request", return_value=_resp()) as req:
            self.assertEqual(tidal_client.rename_playlist("at1", "pl9", "New"), {"status": "ok"})
            self.assertEqual(tidal_client.delete_playlist("at1", "pl9"), {"status": "ok"})
        calls = self._calls(req)
        self.assertEqual(calls[0][:2], ("PATCH", "/playlists/pl9"))
        self.assertEqual(calls[0][2]["data"], {"type": "playlists", "id": "pl9", "attributes": {"name": "New"}})
        self.assertEqual(calls[1], ("DELETE", "/playlists/pl9", None))

    def test_add_batches_at_50_in_order_and_keeps_duplicates(self):
        ids = [f"t{i}" for i in range(120)]
        with mock.patch("requests.request", return_value=_resp()) as req:
            result = tidal_client.add_tracks("at1", "pl1", ids)
        self.assertEqual(result, {"status": "ok", "not_found": []})
        calls = self._calls(req)
        self.assertEqual([len(b["data"]) for _, _, b in calls], [50, 50, 20])
        self.assertEqual([d["id"] for _, _, b in calls for d in b["data"]], ids)
        self.assertTrue(all(b["meta"] == {"onDuplicates": "ADD"} for _, _, b in calls))
        self.assertEqual(calls[0][:2], ("POST", "/playlists/pl1/relationships/items"))

    def test_add_before_an_item_and_reports_not_found(self):
        answer = {"meta": {"skipped": [{"id": "t2", "type": "tracks", "reason": "NOT_FOUND"},
                                       {"id": "t3", "type": "tracks", "reason": "ALREADY_PRESENT"}]}}
        with mock.patch("requests.request", return_value=_resp(json_body=answer)) as req:
            result = tidal_client.add_tracks("at1", "pl1", ["t1", "t2", "t3"], before_item_id="it7")
        self.assertEqual(result, {"status": "ok", "not_found": ["t2"]})
        self.assertEqual(self._calls(req)[0][2]["meta"], {"onDuplicates": "ADD", "positionBefore": "it7"})

    def test_remove_sends_occurrence_ids(self):
        items = [{"track_id": "t1", "item_id": "a"}, {"track_id": "t1", "item_id": "b"}]
        with mock.patch("requests.request", return_value=_resp()) as req:
            self.assertEqual(tidal_client.remove_items("at1", "pl1", items), {"status": "ok"})
        method, path, body = self._calls(req)[0]
        self.assertEqual((method, path), ("DELETE", "/playlists/pl1/relationships/items"))
        self.assertEqual(body["data"], [{"type": "tracks", "id": "t1", "meta": {"itemId": "a"}},
                                        {"type": "tracks", "id": "t1", "meta": {"itemId": "b"}}])

    def test_move_sends_batches_last_first_chained_by_anchor(self):
        items = [{"track_id": f"t{i}", "item_id": f"i{i}"} for i in range(45)]
        with mock.patch("requests.request", return_value=_resp()) as req:
            self.assertEqual(tidal_client.move_items("at1", "pl1", items, "end0"), {"status": "ok"})
        calls = self._calls(req)
        self.assertEqual([[d["meta"]["itemId"] for d in b["data"]][0] for _, _, b in calls],
                         ["i40", "i20", "i0"])
        self.assertEqual([b["meta"]["positionBefore"] for _, _, b in calls], ["end0", "i40", "i20"])
        self.assertTrue(all(m == "PATCH" for m, _, _ in calls))

    @staticmethod
    def _key_recorder(answers):
        # The key is read at call time: a mock's call_args keep a reference
        # to the headers dict, so they would all show its final state.
        keys: list[str] = []
        answers = iter(answers)

        def send(*args, **kwargs):
            keys.append(kwargs["headers"]["Idempotency-Key"])
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return keys, send

    def test_retry_reuses_one_idempotency_key(self):
        keys, send = self._key_recorder([_resp(status_code=503), requests.ConnectionError(), _resp()])
        with mock.patch("time.sleep"), mock.patch("requests.request", side_effect=send):
            result = tidal_client.add_tracks("at1", "pl1", ["t1"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(set(keys)), 1)

    def test_separate_writes_get_separate_keys(self):
        keys, send = self._key_recorder([_resp(), _resp()])
        with mock.patch("requests.request", side_effect=send):
            tidal_client.add_tracks("at1", "pl1", ["t1"])
            tidal_client.add_tracks("at1", "pl1", ["t1"])
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)

    def test_persistent_429_is_rate_limited(self):
        with mock.patch("time.sleep"), \
                mock.patch("requests.request", return_value=_resp(status_code=429)) as req:
            result = tidal_client.delete_playlist("at1", "pl1")
        self.assertEqual(result, {"status": "error", "reason": "rate_limited"})
        self.assertEqual(req.call_count, tidal_client._RETRY_ATTEMPTS)

    def test_403_is_not_retried(self):
        # What a token without playlists.write gets.
        with mock.patch("time.sleep"), \
                mock.patch("requests.request", return_value=_resp(status_code=403)) as req:
            result = tidal_client.create_playlist("at1", "X")
        self.assertEqual(result, {"status": "error", "reason": "http_403"})
        self.assertEqual(req.call_count, 1)

    def test_empty_answer_body_is_ok(self):
        r = _resp()
        r.content = b""
        r.json.side_effect = ValueError("no body")
        with mock.patch("requests.request", return_value=r):
            self.assertEqual(tidal_client.delete_playlist("at1", "pl1"), {"status": "ok"})


class GetPlaylistItemsTests(unittest.TestCase):
    def test_walks_pages_and_keeps_occurrence_ids(self):
        p1 = {"data": [{"type": "tracks", "id": "t1", "meta": {"itemId": "a"}},
                       {"type": "videos", "id": "v1", "meta": {"itemId": "v"}},
                       {"type": "tracks", "id": "t1", "meta": {"itemId": "b"}}],
              "links": {"next": "/playlists/pl1/relationships/items?page[cursor]=c2"}}
        p2 = {"data": [{"type": "tracks", "id": "t2", "meta": {"itemId": "c"}}], "links": {}}
        with mock.patch("requests.get", side_effect=[_resp(json_body=p1), _resp(json_body=p2)]) as get:
            result = tidal_client.get_playlist_items("at1", "pl1")
        self.assertEqual(result, {"status": "ok", "items": [
            {"track_id": "t1", "item_id": "a"}, {"track_id": "t1", "item_id": "b"},
            {"track_id": "t2", "item_id": "c"}]})
        self.assertTrue(get.call_args_list[1].args[0].endswith("page[cursor]=c2"))

    def test_truncation_is_an_error_not_a_short_list(self):
        page = {"data": [{"type": "tracks", "id": "t1", "meta": {"itemId": "a"}}],
                "links": {"next": "/again"}}
        with mock.patch.object(tidal_client, "_MAX_PAGES", 2), \
                mock.patch("requests.get", return_value=_resp(json_body=page)):
            result = tidal_client.get_playlist_items("at1", "pl1")
        self.assertEqual(result, {"status": "error", "reason": "truncated"})

    def test_no_id_makes_no_request(self):
        with mock.patch("requests.get") as get:
            self.assertEqual(tidal_client.get_playlist_items("at1", ""),
                             {"status": "error", "reason": "not_found"})
        get.assert_not_called()


class GetPlaylistTests(unittest.TestCase):
    def test_name_and_owners(self):
        body = {"data": {"id": "pl1", "attributes": {"name": "Mix"},
                         "relationships": {"owners": {"data": [{"type": "users", "id": "u7"}]}}}}
        with mock.patch("requests.get", return_value=_resp(json_body=body)) as get:
            self.assertEqual(tidal_client.get_playlist("at1", "pl1"),
                             {"status": "ok", "name": "Mix", "owner_ids": ["u7"]})
        self.assertTrue(get.call_args.args[0].endswith("/playlists/pl1"))
        self.assertEqual(get.call_args.kwargs["params"], {"include": "owners"})

    def test_no_owners_relationship_is_no_owners(self):
        body = {"data": {"id": "pl1", "attributes": {"name": "Mix"}}}
        with mock.patch("requests.get", return_value=_resp(json_body=body)):
            self.assertEqual(tidal_client.get_playlist("at1", "pl1")["owner_ids"], [])

    def test_a_404_is_not_found_and_other_errors_keep_their_reason(self):
        with mock.patch("requests.get", return_value=_resp(status_code=404)):
            self.assertEqual(tidal_client.get_playlist("at1", "pl1"), {"status": "error", "reason": "not_found"})
        with mock.patch("time.sleep"), mock.patch("requests.get", return_value=_resp(status_code=429)):
            self.assertEqual(tidal_client.get_playlist("at1", "pl1"), {"status": "error", "reason": "rate_limited"})
        with mock.patch("requests.get", return_value=_resp(json_body={"data": {}})):
            self.assertEqual(tidal_client.get_playlist("at1", "pl1"), {"status": "error", "reason": "parse_error"})

    def test_items_404_is_not_found(self):
        with mock.patch("requests.get", return_value=_resp(status_code=404)):
            self.assertEqual(tidal_client.get_playlist_items("at1", "pl1"), {"status": "error", "reason": "not_found"})


if __name__ == "__main__":
    unittest.main()
