#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Roon Core connection — pairing, token persistence, and read-only playlist
browsing.

Connects directly to the Roon Core's extension API (bypassing SOOD multicast
discovery, which doesn't reliably traverse the container's bridge network).
Registers under its own extension_id — two RoonApi instances sharing one
extension_id fight each other for the socket and can drop the connection, so
never reuse another extension's id, even for a quick test.

Roon's Browse API never exposes a filesystem path — playlist tracks here are
returned as (artist, title) only. Resolving them to an actual file on disk is
the caller's job (see matching.py), using the locally-scanned `tracks` table.
"""

import os
import threading
import time
from pathlib import Path

import requests
from roonapi import RoonApi
from roonapi.roonapi import RoonApiException

import db

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TOKEN_FILE = DATA_DIR / "roon_token.json"

# Host/port are DB-backed (app_config, admin-editable from the web UI — see
# main.py's /api/admin/config) with these env vars only as the fallback used
# until an admin ever sets them there — existing deployments keep working
# unchanged, no migration step needed.
_ENV_ROON_HOST = os.environ.get("ROON_HOST", "")
_ENV_ROON_PORT = os.environ.get("ROON_PORT", "9330")

APP_INFO = {
    "extension_id": "org.missing_foss.trobar",
    "display_name": "Trobar",
    "display_version": "1.0.0",
    "publisher": "missing-foss",
    "email": "missing_foss@etik.com",
}

PAGE_SIZE = 100

_conn_lock = threading.Lock()
_browse_lock = threading.Lock()

_roon: "RoonApi | None" = None
_last_connect_attempt: float = 0.0
_RECONNECT_COOLDOWN = 30


def _load_token() -> str | None:
    if TOKEN_FILE.exists():
        import json
        try:
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("token")
        except Exception:
            return None
    return None


def _save_token(token: str) -> None:
    import json
    TOKEN_FILE.write_text(json.dumps({"token": token}), encoding="utf-8")
    # The Roon pairing token grants control of the Roon Core — keep it
    # owner-only at rest. Best-effort (see db.get_conn).
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def _current_host_port() -> tuple[str, int]:
    conn = db.get_conn()
    try:
        host = db.get_config(conn, "roon_host") or _ENV_ROON_HOST
        port = int(db.get_config(conn, "roon_port") or _ENV_ROON_PORT)
        return host, port
    finally:
        conn.close()


def _connect_locked() -> None:
    global _roon, _last_connect_attempt
    _last_connect_attempt = time.time()
    token = _load_token()
    host, port = _current_host_port()
    if not host:
        # No ROON_HOST env and nothing configured in the admin UI yet —
        # Roon is optional, stay disconnected until an admin sets a host.
        _roon = None
        return
    try:
        _roon = RoonApi(APP_INFO, token, host, port, blocking_init=False)
    except RoonApiException as e:
        print(f"[roon_client] connect failed: {e}")
        _roon = None


def ensure_started() -> None:
    """Call once at app startup so pairing can be approved before anyone opens
    the UI (Roon → Settings → Extensions, one-time)."""
    with _conn_lock:
        if _roon is None:
            _connect_locked()


def status() -> dict:
    host, port = _current_host_port()
    with _conn_lock:
        if _roon is None:
            return {"state": "disconnected", "host": host, "port": port, "provider": "roon"}
        if _roon.ready and _roon.token:
            if _load_token() != _roon.token:
                _save_token(_roon.token)
            return {"state": "paired", "host": host, "port": port,
                     "core_name": _roon.core_name, "provider": "roon"}
        return {"state": "pending_approval", "host": host, "port": port, "provider": "roon"}


def retry_pairing() -> dict:
    with _conn_lock:
        if _roon is not None and time.time() - _last_connect_attempt < _RECONNECT_COOLDOWN:
            pass
        else:
            _connect_locked()
    time.sleep(0.5)
    return status()


def reconnect(host: str, port: int) -> dict:
    """Admin changed the Roon host/port from the web UI — persist it and
    force a fresh connection attempt against the new address. Reuses the
    existing pairing token as-is: Roon ties that to the Core's identity, not
    the address it's reachable at, same as how Roon Remote apps shrug off a
    Core's IP changing on the LAN."""
    conn = db.get_conn()
    try:
        db.set_config(conn, "roon_host", host)
        db.set_config(conn, "roon_port", str(port))
        conn.commit()
    finally:
        conn.close()
    with _conn_lock:
        _connect_locked()
    time.sleep(0.5)
    return status()


def _get_roon() -> "RoonApi | None":
    with _conn_lock:
        if _roon is not None and _roon.ready and _roon.token:
            return _roon
        return None


def _browse_browse_retry(roon: RoonApi, opts: dict, retries: int = 2) -> dict | None:
    resp = roon.browse_browse(opts)
    for _ in range(retries):
        if resp is not None:
            return resp
        time.sleep(0.5)
        resp = roon.browse_browse(opts)
    return resp


def _pick_zone_or_output_id(roon: RoonApi) -> str | None:
    for _ in range(20):
        if roon.zones:
            return next(iter(roon.zones))
        if roon.outputs:
            return next(iter(roon.outputs))
        time.sleep(0.15)
    return None


def _collect_all_items(roon: RoonApi, load_opts: dict, total_count: int) -> list[dict]:
    opts = dict(load_opts)
    opts["offset"] = 0
    out = []
    while opts["offset"] < total_count:
        try:
            page = roon.browse_load(opts)["items"]
        except (TypeError, KeyError):
            break
        if not page:
            break
        out.extend(page)
        opts["offset"] += PAGE_SIZE
    return out


def _collect_all_titles(roon: RoonApi, load_opts: dict, total_count: int) -> list[str]:
    return [it.get("title", "") for it in _collect_all_items(roon, load_opts, total_count)]


def _find_item_by_title(roon: RoonApi, load_opts: dict, total_count: int, title: str) -> dict | None:
    for item in _collect_all_items(roon, load_opts, total_count):
        if item.get("title") == title:
            return item
    return None


def _browse_root(roon: RoonApi, zone_id: str) -> tuple[dict, dict, int] | None:
    """Pop to the absolute root menu. Returns (opts, load_opts, total_count)."""
    opts = {"zone_or_output_id": zone_id, "hierarchy": "browse",
            "count": PAGE_SIZE, "pop_all": True}
    load_opts = {"zone_or_output_id": zone_id, "hierarchy": "browse",
                 "count": PAGE_SIZE, "offset": 0}
    resp = _browse_browse_retry(roon, opts)
    if resp is None:
        return None
    total_count = resp["list"]["count"]
    del opts["pop_all"]
    return opts, load_opts, total_count


def _switch_profile(roon: RoonApi, zone_id: str, profile_name: str) -> bool:
    """Switches this shared connection's active Roon profile — confirmed
    live that this persists across every subsequent Browse call on the
    connection (until switched again), independently of zone/output
    choice. Browsing a profile item (Settings > Profile, hint="action")
    *is* the switch, not a separate select call.

    Always re-resolves the profile's item_key fresh from a root browse
    rather than caching one anywhere: cheap (one extra descend), and
    sidesteps whether item_keys are stable across reconnects (unconfirmed
    either way) since this is used immediately, in the same session that
    just looked it up. Caller must already hold _browse_lock."""
    walked = _browse_root(roon, zone_id)
    if walked is None:
        return False
    opts, load_opts, total_count = walked
    descended = _descend_path(roon, dict(opts), dict(load_opts), total_count, ("Settings", "Profile"))
    if descended is None:
        return False
    _, d_load_opts, d_total_count = descended
    target = _find_item_by_title(roon, d_load_opts, d_total_count, profile_name)
    if target is None:
        return False
    switch_opts = {"zone_or_output_id": zone_id, "hierarchy": "browse",
                   "count": PAGE_SIZE, "item_key": target["item_key"]}
    return _browse_browse_retry(roon, switch_opts) is not None


def list_profiles() -> dict:
    """Roon profiles visible to this Core, with which one is currently
    active — for the admin's user-to-profile mapping UI (Administration).
    Empty list (not an error) on a single-profile Core with no Settings >
    Profile menu at all."""
    roon = _get_roon()
    if roon is None:
        return {"status": "error", "reason": "not_paired"}

    with _browse_lock:
        zone_id = _pick_zone_or_output_id(roon)
        if zone_id is None:
            return {"status": "error", "reason": "no_zone_available"}
        walked = _browse_root(roon, zone_id)
        if walked is None:
            return {"status": "error", "reason": "browse_browse root failed"}
        opts, load_opts, total_count = walked
        descended = _descend_path(roon, dict(opts), dict(load_opts), total_count, ("Settings", "Profile"))
        if descended is None:
            return {"status": "ok", "profiles": []}
        _, d_load_opts, d_total_count = descended
        items = _collect_all_items(roon, d_load_opts, d_total_count)

    profiles = [{"title": it["title"], "selected": it.get("subtitle") == "selected"}
                for it in items if it.get("title")]
    return {"status": "ok", "profiles": profiles}


def list_playlists(roon_profile: str | None = None) -> dict:
    """Top-level Playlists menu. Returns {"status": "ok", "playlists":
    [{"id": None, "title": ...}, ...]} — the common shape every provider
    returns (#75), but `id` is always None for Roon: its Browse API exposes
    no stable playlist id (only titles and non-cacheable item_keys, see
    _switch_profile's note), so a Roon playlist is keyed by
    (source_provider, title) and two same-titled Roon playlists still
    collapse — a hard ceiling on Roon's own API, not fixable here (#75).
    `roon_profile`: switch to this profile first (Administration's user
    mapping) — None means today's unchanged behaviour, whatever profile
    the connection currently defaults to."""
    roon = _get_roon()
    if roon is None:
        return {"status": "error", "reason": "not_paired"}

    with _browse_lock:
        zone_id = _pick_zone_or_output_id(roon)
        if zone_id is None:
            return {"status": "error", "reason": "no_zone_available"}
        if roon_profile is not None and not _switch_profile(roon, zone_id, roon_profile):
            return {"status": "error", "reason": "profile_not_found"}
        walked = _browse_root(roon, zone_id)
        if walked is None:
            return {"status": "error", "reason": "browse_browse root failed"}
        opts, load_opts, total_count = walked

        found = _find_item_by_title(roon, load_opts, total_count, "Playlists")
        if found is None:
            return {"status": "ok", "playlists": []}
        opts["item_key"] = found["item_key"]
        load_opts["item_key"] = found["item_key"]
        resp = _browse_browse_retry(roon, opts)
        if resp is None:
            return {"status": "error", "reason": "browse_browse descend failed"}
        total_count = resp["list"]["count"]

        titles = [t for t in _collect_all_titles(roon, load_opts, total_count)
                  if not t.startswith("Play ")]
    return {"status": "ok", "playlists": [{"id": None, "title": t} for t in titles]}


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None,
                        roon_profile: str | None = None) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist"} — `artist` comes from the browse item's subtitle, which is all
    Roon exposes here (no album, no file path). `source_playlist_id` is
    accepted for call-shape uniformity but always None for Roon (no stable
    id) — the playlist is fetched by title. `roon_profile`: see
    list_playlists()."""
    roon = _get_roon()
    if roon is None:
        return {"status": "error", "reason": "not_paired"}

    with _browse_lock:
        zone_id = _pick_zone_or_output_id(roon)
        if zone_id is None:
            return {"status": "error", "reason": "no_zone_available"}
        if roon_profile is not None and not _switch_profile(roon, zone_id, roon_profile):
            return {"status": "error", "reason": "profile_not_found"}
        walked = _browse_root(roon, zone_id)
        if walked is None:
            return {"status": "error", "reason": "browse_browse root failed"}
        opts, load_opts, total_count = walked

        for segment in ("Playlists", playlist_title):
            found = _find_item_by_title(roon, load_opts, total_count, segment)
            if found is None:
                return {"status": "not_found", "failed_segment": segment}
            opts["item_key"] = found["item_key"]
            load_opts["item_key"] = found["item_key"]
            resp = _browse_browse_retry(roon, opts)
            if resp is None:
                return {"status": "error", "reason": "browse_browse descend failed"}
            total_count = resp["list"]["count"]

        items = [it for it in _collect_all_items(roon, load_opts, total_count)
                 if not (it.get("title") or "").startswith("Play ")]

    tracks = [
        {"position": i, "title": it.get("title", ""), "artist": it.get("subtitle", "")}
        for i, it in enumerate(items)
    ]
    return {"status": "ok", "playlist": playlist_title, "tracks": tracks}


def _descend_path(roon: RoonApi, opts: dict, load_opts: dict, total_count: int,
                   segments: tuple[str, ...]) -> tuple[dict, dict, int] | None:
    """Walk a sequence of menu titles from wherever opts/load_opts currently
    point (same technique as get_playlist_tracks's segment loop above, just
    factored out since artist-image lookup needs to try more than one root
    path — see get_artist_image)."""
    for segment in segments:
        found = _find_item_by_title(roon, load_opts, total_count, segment)
        if found is None:
            return None
        opts["item_key"] = found["item_key"]
        load_opts["item_key"] = found["item_key"]
        resp = _browse_browse_retry(roon, opts)
        if resp is None:
            return None
        total_count = resp["list"]["count"]
    return opts, load_opts, total_count


# Roon puts "Artists" either directly at the browse root or nested under a
# "Library" menu depending on version/configuration — try both rather than
# assume one, since this couldn't be verified live without risking a second
# connection fighting the app's own (see roon_client module docstring).
_ARTISTS_MENU_PATHS = (("Library", "Artists"), ("Artists",))

# artist title -> image_key, built once per process lifetime (not per
# lookup — see _get_artist_image_key_map's docstring for why that matters).
_artist_image_key_map: dict[str, str] | None = None
_artist_image_key_map_lock = threading.Lock()


def _build_artist_image_key_map(roon: RoonApi) -> dict[str, str]:
    with _browse_lock:
        zone_id = _pick_zone_or_output_id(roon)
        if zone_id is None:
            return {}
        walked = _browse_root(roon, zone_id)
        if walked is None:
            return {}
        opts, load_opts, total_count = walked

        for menu_path in _ARTISTS_MENU_PATHS:
            descended = _descend_path(roon, dict(opts), dict(load_opts), total_count, menu_path)
            if descended is not None:
                _, d_load_opts, d_total_count = descended
                items = _collect_all_items(roon, d_load_opts, d_total_count)
                return {it["title"]: it["image_key"] for it in items
                        if it.get("title") and it.get("image_key")}
    return {}


def _get_artist_image_key_map(roon: RoonApi) -> dict[str, str]:
    """First caller pays for one full walk of the Artists list (a handful of
    paginated browse_load calls — still noticeable, but a one-time cost);
    every lookup after that is a plain dict get. The naive version of this
    (browse_browse + re-walk the *entire* artist list on every single
    lookup, all serialized behind one global browse lock) is what actually
    caused most pictures to never load — with ~700+ artists, that's several
    seconds of fully-serialized Roon round-trips PER artist, so a page
    rendering 30+ rows at once queued for minutes and most requests timed
    out or were abandoned long before their turn came up. Confirmed directly
    against the real Roon Core, not just suspected — logs showed image
    requests completing roughly one per second."""
    global _artist_image_key_map
    with _artist_image_key_map_lock:
        if _artist_image_key_map is None:
            _artist_image_key_map = _build_artist_image_key_map(roon)
        return _artist_image_key_map


def get_artist_image(artist_name: str) -> tuple[bytes, str] | None:
    """Fetch one artist's image straight from Roon: resolve its image_key
    via the cached title->image_key map (exact match only — no fuzzy
    fallback like matching.py's playlist-track resolution, since a miss here
    just means no picture rather than a broken sync), then GET the image
    bytes from Roon Core's own HTTP server. Returns (bytes, content_type) or
    None if not paired / not found / no image. Callers should cache the
    result on disk too (see artist_images.py) — a cache miss here still
    means a real HTTP fetch against Roon, just not a browse-session one, so
    it's safe to run concurrently across many artists at once (no lock)."""
    roon = _get_roon()
    if roon is None:
        return None

    image_key = _get_artist_image_key_map(roon).get(artist_name)
    if not image_key:
        return None

    url = roon.get_image(image_key, scale="fit", width=500, height=500)
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return resp.content, content_type
