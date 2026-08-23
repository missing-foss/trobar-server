#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lidarr API client — #494's "Request missing albums" feature. Lidarr is
never a read source for Trobar (unlike Jellyfin/Emby/Subsonic, which can be
either the active library provider or a mirror target); there is exactly
one connection, one key namespace (lidarr_*), no active/mirror duality to
disambiguate.

Auth is a single `X-Api-Key` header, simpler than Jellyfin's MediaBrowser
scheme. Same never-raises, status-code-and-parsed-body contract as
jellyfin_client._request_as — every public function here returns a dict,
never an exception; a caller reacts to `status`/`code`/`stage`, never to a
caught error.

Picking the RIGHT album from a lookup is deliberately NOT this module's
job — lookup_album() returns Lidarr's raw, unfiltered candidate list.
Confirmed live against a real Lidarr 3.1.0.4875 instance: the
top-ranked candidate for a plain "<artist> <album>" search is wrong more
often than not — tribute albums, lullaby-cover collections, and soundfont
remixes routinely outrank the real release. Filtering for an exact
normalized-artist match is business logic that belongs with the rest of
the request orchestration in lidarr_requests.py, the same split
mirror_subsonic.py's own candidate-selection logic keeps from
subsonic_client.py.

The two-call add sequence in add_and_monitor_album() is the other
confirmed-live trap: `POST /api/v1/album` returns 201 with `monitored:
true` in its own response body, but the album is NOT actually on Lidarr's
wanted list until the follow-up `PUT /api/v1/album/monitor` call succeeds
too — an implementation that trusts the POST's response would report
success and request nothing at all. See that function's own docstring.

A THIRD trap, found only by testing against a live instance (a plain read
of the API docs gives no hint of it): when the artist in the POST body is
new to this Lidarr instance, Lidarr queues its own asynchronous
`RefreshArtist` command as a side effect of creating it — a deep
metadata sync that, once it completes, re-derives every one of the
artist's albums' `monitored` flags from the artist-level
`addOptions.monitor` we set ("none", deliberately — see above). That
command is still running when our own `PUT /api/v1/album/monitor` call
returns 202, and finishes anywhere from under a second to a few seconds
later — silently reverting the album we just monitored back to
unmonitored, with no error anywhere in this sequence. Reproduced
directly, repeatably, in dev against 3.1.0.4875: request a brand-new
artist's album, our PUT succeeds, and within ~1-2s Lidarr's own async
refresh flips it back to unmonitored. An artist Lidarr already knows
triggers a lighter `RefreshAlbum` command instead, which does not race
this way. `_wait_for_pending_artist_refresh()` closes this: after create,
it explicitly triggers and waits out its OWN RefreshArtist command for
this artist (also confirmed live: scanning for Lidarr's auto-triggered
one races the enqueue itself and can find nothing to wait for at all —
see that function's own docstring) before issuing the monitor PUT.

A FOURTH trap, also only found live: the deep RefreshArtist above doesn't
just settle monitored flags — it also creates an unmonitored album stub
row for every OFFICIAL release in the artist's whole discography, not
just whichever single album prompted adding the artist in the first
place. Request a second, different, well-known album by an artist Trobar
already asked Lidarr about, and `POST /api/v1/album` 400s with
`AlbumExistsValidator` ("This album has already been added") — even
though this module never created that row itself. Confirmed live: an
obscure/non-canonical release (a tribute compilation, a rarities
collection) is NOT always pre-populated this way and POSTs cleanly, so
this isn't reachable only for exact duplicates. `_is_album_exists_error()`
/ `_find_existing_album()` handle it: on that specific validator error,
look up the existing stub's own id via `GET /api/v1/album?
foreignAlbumId=...` instead of treating it as a hard failure, then
monitor that id directly — the artist is provably already fully settled
in this case (that's why the stub exists), so no RefreshArtist wait is
needed on this path."""

import time

import requests

import db

_TIMEOUT_SECONDS = 10
# Empirically: RefreshArtist for a newly-created artist completed in ~1s
# every time in dev testing. 10 x 0.5s gives comfortable margin without
# blocking a sync for long if Lidarr is unusually slow -- and if the
# refresh is genuinely still running past this budget, add_and_monitor_
# album() proceeds anyway rather than blocking indefinitely; the caller's
# own retry-never policy (see lidarr_requests.py) means a monitor PUT that
# loses this particular race still gets recorded, just as 'partial'.
_ARTIST_REFRESH_POLL_INTERVAL_SECONDS = 0.5
_ARTIST_REFRESH_POLL_MAX_ATTEMPTS = 10


def _request(
    method: str, endpoint: str, api_key: str, url: str,
    params: dict | None = None, json_body: dict | None = None,
) -> tuple[int | None, dict | list | None]:
    """Low-level authenticated call against an explicit server/api-key, any
    HTTP method. Returns (status_code, parsed body) — status_code is None
    only when the request itself never got a response at all (network
    error, unparseable body); a real HTTP status (including a 4xx) is
    always surfaced so a caller can react to a SPECIFIC failure (see
    add_and_monitor_album's create-vs-monitor stage distinction) rather
    than string-matching a message. Never raises."""
    if not url or not api_key:
        return None, None
    headers = {"X-Api-Key": api_key}
    try:
        resp = requests.request(
            method, f"{url.rstrip('/')}{endpoint}", headers=headers,
            params=params, json=json_body, timeout=_TIMEOUT_SECONDS,
        )
        body = resp.json() if resp.content else {}
        return resp.status_code, body
    except (requests.RequestException, ValueError):
        return None, None


def status() -> dict:
    """{"state": "disconnected"|"paired", "url": str}. Paired = the stored
    api_key is actually valid against the stored url, verified via a
    cheap, side-effect-free GET /api/v1/system/status."""
    conn = db.get_conn()
    try:
        url = db.get_config(conn, "lidarr_url") or ""
    finally:
        conn.close()
    connection = db.get_lidarr_connection()
    if connection is None:
        return {"state": "disconnected", "url": url}
    _url, api_key = connection
    status_code, _body = _request("GET", "/api/v1/system/status", api_key, url)
    if status_code == 200:
        return {"state": "paired", "url": url}
    return {"state": "disconnected", "url": url}


def test_connection(url: str, api_key: str) -> dict:
    """#509 item 3: same check as status(), against EXPLICIT credentials
    rather than the stored config — never persists anything. See
    subsonic_client.test_connection's own docstring for the full
    rationale (the admin config form's live pre-save check)."""
    status_code, _body = _request("GET", "/api/v1/system/status", api_key, url)
    if status_code == 200:
        return {"state": "paired", "url": url}
    return {"state": "disconnected", "url": url}


def reconnect(url: str, api_key: str) -> dict:
    """Admin (re)configured the connection — persists url+api_key ONLY.
    The three profile fields (root folder/quality/metadata profile) are
    deliberately untouched here: they can't be meaningfully chosen until
    THIS pair is live and GET /api/admin/lidarr-options has something to
    query — see main.py's two-phase save for the full flow. Returns
    status()."""
    conn = db.get_conn()
    try:
        db.set_config(conn, "lidarr_url", url)
        db.set_config(conn, "lidarr_api_key", api_key)
        conn.commit()
    finally:
        conn.close()
    return status()


def list_root_folders() -> dict:
    """{"status": "ok", "root_folders": [{"path": str, "free_space": int | None}, ...]}
    or {"status": "error", "reason": str, "code": int | None}."""
    connection = db.get_lidarr_connection()
    if connection is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, api_key = connection
    status_code, body = _request("GET", "/api/v1/rootfolder", api_key, url)
    if status_code != 200 or not isinstance(body, list):
        return {"status": "error", "reason": "unreachable", "code": status_code}
    return {
        "status": "ok",
        "root_folders": [
            {"path": f["path"], "free_space": f.get("freeSpace")}
            for f in body if f.get("path")
        ],
    }


def list_quality_profiles() -> dict:
    """{"status": "ok", "quality_profiles": [{"id": int, "name": str}, ...]}
    or {"status": "error", "reason": str, "code": int | None}."""
    connection = db.get_lidarr_connection()
    if connection is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, api_key = connection
    status_code, body = _request("GET", "/api/v1/qualityprofile", api_key, url)
    if status_code != 200 or not isinstance(body, list):
        return {"status": "error", "reason": "unreachable", "code": status_code}
    return {
        "status": "ok",
        "quality_profiles": [{"id": p["id"], "name": p["name"]} for p in body],
    }


def list_metadata_profiles() -> dict:
    """{"status": "ok", "metadata_profiles": [{"id": int, "name": str}, ...]}
    or {"status": "error", "reason": str, "code": int | None}."""
    connection = db.get_lidarr_connection()
    if connection is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, api_key = connection
    status_code, body = _request("GET", "/api/v1/metadataprofile", api_key, url)
    if status_code != 200 or not isinstance(body, list):
        return {"status": "error", "reason": "unreachable", "code": status_code}
    return {
        "status": "ok",
        "metadata_profiles": [{"id": p["id"], "name": p["name"]} for p in body],
    }


def _is_album_exists_error(body: dict | list | None) -> bool:
    """True if a 400 from POST /api/v1/album is specifically Lidarr's
    AlbumExistsValidator — {"errorCode": "AlbumExistsValidator", ...} in a
    list of validation errors — rather than some other 400 (a bad
    rootFolderPath, an invalid profile id, ...) that should stay a hard
    create_failed. See add_and_monitor_album's own docstring for why this
    specific case gets a fallback instead of an error."""
    if not isinstance(body, list):
        return False
    return any(isinstance(e, dict) and e.get("errorCode") == "AlbumExistsValidator" for e in body)


def _find_existing_album(
    api_key: str, url: str, foreign_album_id: str,
) -> tuple[int | None, int | None]:
    """(album_id, artist_id) for an album Lidarr already has a stub row
    for, via GET /api/v1/album?foreignAlbumId=... — (None, None) on any
    failure. Only ever called after AlbumExistsValidator has already
    confirmed the row exists; a lookup miss here would mean Lidarr's own
    state changed between those two calls, not a normal outcome."""
    status_code, body = _request(
        "GET", "/api/v1/album", api_key, url, params={"foreignAlbumId": foreign_album_id})
    if status_code != 200 or not isinstance(body, list) or not body:
        return None, None
    album = body[0]
    return album.get("id"), album.get("artistId")


def _wait_for_pending_artist_refresh(api_key: str, url: str, artist_id: int) -> None:
    """Blocks until this artist's metadata has settled, or the poll budget
    runs out — see this module's own docstring for why this exists.

    Deliberately does NOT scan GET /api/v1/command for Lidarr's own
    automatically-queued RefreshArtist (the one it triggers as a side
    effect of creating a brand-new artist): confirmed live, that scan
    races the enqueue itself — immediately after create returns, the
    auto-triggered command may not be listed yet at all, so a single
    "is anything pending?" check can come back empty and this would
    return right away without having waited for anything, silently
    reproducing the exact bug this function exists to close.

    Instead, this explicitly triggers its OWN `POST /api/v1/command
    {"name": "RefreshArtist", "artistId": ...}` and polls the returned
    command's own id via GET /api/v1/command/<id> — a concrete id with no
    ambiguity about whether it's been queued yet. Redundant with whatever
    Lidarr already queued on its own, but cheap, and correct regardless of
    whether an auto-triggered refresh is also in flight: by the time OUR
    command reaches a terminal status, the artist's data (and therefore
    the monitored flags Lidarr derives from it) has settled either way."""
    status_code, command = _request(
        "POST", "/api/v1/command", api_key, url,
        json_body={"name": "RefreshArtist", "artistId": artist_id})
    if status_code not in (200, 201) or not isinstance(command, dict):
        return
    command_id = command.get("id")
    if command_id is None:
        return
    for attempt in range(_ARTIST_REFRESH_POLL_MAX_ATTEMPTS):
        if command.get("status") in ("completed", "failed"):
            return
        if attempt:
            time.sleep(_ARTIST_REFRESH_POLL_INTERVAL_SECONDS)
        status_code, command = _request("GET", f"/api/v1/command/{command_id}", api_key, url)
        if status_code != 200 or not isinstance(command, dict):
            return


def lookup_album(term: str) -> dict:
    """GET /api/v1/album/lookup?term=<term> -> {"status": "ok",
    "candidates": [<raw Lidarr album-lookup objects, unfiltered>]} or
    {"status": "error", "reason": str, "code": int | None}. Deliberately
    does NOT pick a candidate — see this module's own docstring for why."""
    connection = db.get_lidarr_connection()
    if connection is None:
        return {"status": "error", "reason": "not_configured", "code": None}
    url, api_key = connection
    status_code, body = _request("GET", "/api/v1/album/lookup", api_key, url, params={"term": term})
    if status_code != 200 or not isinstance(body, list):
        return {"status": "error", "reason": "unreachable", "code": status_code}
    return {"status": "ok", "candidates": body}


def add_and_monitor_album(foreign_album_id: str, foreign_artist_id: str) -> dict:
    """The confirmed-live two-call sequence, as ONE function — every
    caller wants both steps together, and this is the one place that can
    guarantee step (b) is attempted even when nothing about the caller's
    own loop does.

    Step a: POST /api/v1/album — foreignAlbumId, monitored: true (see
    below for why this flag is misleading), addOptions.searchForNewAlbum:
    false (#494's settled decision: monitor-only, never trigger an
    immediate search), and a nested artist object (foreignArtistId,
    qualityProfileId, metadataProfileId, rootFolderPath,
    addOptions.monitor: "none" — this is what stops the artist's WHOLE
    discography becoming wanted, confirmed live: without it, adding one
    album pulls in every other album by that artist too). Creates the
    artist if Lidarr doesn't know it yet.

    Confirmed live: the response's own "monitored: true" is misleading —
    the album is NOT actually on the wanted list after step (a) alone.
    Between steps a and b, _wait_for_pending_artist_refresh() waits out
    Lidarr's own async RefreshArtist command if step (a) just created a
    new artist — see this module's own docstring for why skipping this
    wait silently loses the race on a new artist's very first album.

    Step b: PUT /api/v1/album/monitor — {"albumIds": [<id from step a>],
    "monitored": true}. THIS is what actually puts it on the wanted list.

    Returns:
      {"status": "ok", "artist_id": int, "album_id": int}
      {"status": "error", "reason": str, "code": int | None,
       "stage": "create", "artist_id": None, "album_id": None}
        -- step (a) itself failed; nothing exists in Lidarr for this pair.
      {"status": "error", "reason": str, "code": int | None,
       "stage": "monitor", "artist_id": int, "album_id": int}
        -- PARTIAL: step (a) succeeded (an artist/album now exists in
        Lidarr, artist-level unmonitored per addOptions.monitor:"none")
        but step (b) failed, so the album is NOT on the wanted list
        despite existing. ids ARE populated here specifically so the
        caller can still record Lidarr's own ids for a stuck row — see
        lidarr_requests.py's own docstring for why this case is
        deliberately never auto-retried."""
    config = db.get_lidarr_config()
    if config is None:
        return {"status": "error", "reason": "not_configured", "code": None,
                "stage": "create", "artist_id": None, "album_id": None}
    url, api_key, root_folder_path, quality_profile_id, metadata_profile_id = config

    create_body = {
        "foreignAlbumId": foreign_album_id,
        "monitored": True,
        "addOptions": {"searchForNewAlbum": False},
        "artist": {
            "foreignArtistId": foreign_artist_id,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "addOptions": {"monitor": "none"},
        },
    }
    create_status, create_result = _request("POST", "/api/v1/album", api_key, url, json_body=create_body)
    already_added = create_status == 400 and _is_album_exists_error(create_result)
    if not already_added and (create_status != 201 or not isinstance(create_result, dict)):
        return {"status": "error", "reason": "create_failed", "code": create_status,
                "stage": "create", "artist_id": None, "album_id": None}

    if already_added:
        # Confirmed live: for an artist Lidarr already fully knows, the
        # deep RefreshArtist run when that artist was FIRST added already
        # created an unmonitored album stub row for every official release
        # in the artist's whole discography -- not just whatever single
        # album prompted adding the artist. A later request for one of
        # those other official albums 400s here ("This album has already
        # been added") even though it was never OUR create call that put
        # it there. The artist is provably already settled in this case
        # (that's why the stub exists at all), so there's no new-artist
        # refresh race to wait out -- just resolve the existing stub's own
        # id and go straight to monitoring it.
        album_id, artist_id = _find_existing_album(api_key, url, foreign_album_id)
        if album_id is None:
            return {"status": "error", "reason": "create_failed", "code": create_status,
                    "stage": "create", "artist_id": None, "album_id": None}
    else:
        assert isinstance(create_result, dict)
        album_id = create_result.get("id")
        artist = create_result.get("artist") or {}
        artist_id = artist.get("id")
        if album_id is None:
            return {"status": "error", "reason": "create_response_missing_id", "code": create_status,
                    "stage": "create", "artist_id": artist_id, "album_id": None}
        if artist_id is not None:
            _wait_for_pending_artist_refresh(api_key, url, artist_id)

    monitor_body = {"albumIds": [album_id], "monitored": True}
    monitor_status, _monitor_result = _request(
        "PUT", "/api/v1/album/monitor", api_key, url, json_body=monitor_body)
    if monitor_status != 202:
        return {"status": "error", "reason": "monitor_failed", "code": monitor_status,
                "stage": "monitor", "artist_id": artist_id, "album_id": album_id}

    return {"status": "ok", "artist_id": artist_id, "album_id": album_id}
