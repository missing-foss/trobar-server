#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Administration, /api/admin/*: configuration, users and delegations, the
provider switch, jobs, health, mirrors, and the provider-side user
mappings. Session-authenticated by main's before-request gate; every route
here also requires an admin.
"""

import concurrent.futures
import difflib
import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from flask import jsonify, request, abort, Blueprint
from flask_babel import gettext as _
from werkzeug.security import generate_password_hash

import album_lookup
import artist_images
import credential_store
import db
import emby_client
import filesystem_client
import jellyfin_client
import jobs
import lidarr_client
import lms_client
import music_assistant_client
import mirror
import mirror_runs
import play_forward
import playlist_sync
import provider_config
import plex_client
import roon_client
import scanner
import settings
import subsonic_client
import transcode
from web_common import (
    _PROVIDERS,
    _PROVIDER_SWITCH_LOCK,
    _active_provider_id,
    _admin_counts,
    _config_field_error,
    _config_int,
    _effective_folder,
    _end_other_sessions,
    _exposure_peer_count,
    _failed_writes,
    _folders_overlap,
    _invalid_url_message,
    _is_valid_url,
    _music_root_is_readonly,
    _trusted_proxy,
    get_current_user_id,
    oauth,
    require_admin,
)

_log = logging.getLogger(__name__)

bp = Blueprint("admin", __name__)


_EXPOSURE_WARN_THRESHOLD = 5


# #509 item 3: (client module, test_connection's attribute NAME, field
# names, required field names) per provider key. Deliberately the module +
# an attribute name string, not a direct function reference — a direct
# reference would bind to the ORIGINAL function object at import time,
# which a test's mock.patch("subsonic_client.test_connection", ...) can
# never reach afterward (it patches the module's attribute, not whatever
# already captured the old value). getattr() at call time below always
# resolves through the module's CURRENT attribute instead.
#
# field names are positional args to test_connection, in order.
# required_field_names is a SUBSET of field_names — usually all of them,
# except LMS, whose username/password stay optional (its own "Authorize"
# setting is off by default, same as reconnect()'s own contract). One
# dispatch table rather than nine near-identical routes — same shape as
# _MIRROR_SINK_COLUMNS (#498) and _admin_provider_user_mapping (#262). A
# provider and its #189 mirror-target counterpart share the same
# test_connection function: "is this server reachable with these creds"
# doesn't depend on which config namespace the answer will end up
# persisted under, only PUT /api/admin/config's own per-block handling
# (above) does that.
_TEST_CONNECTION_PROVIDERS: dict[str, tuple[Any, str, tuple[str, ...], tuple[str, ...]]] = {
    "subsonic": (subsonic_client, "test_connection", ("url", "username", "password"),
                 ("url", "username", "password")),
    "mirror_subsonic": (subsonic_client, "test_connection", ("url", "username", "password"),
                        ("url", "username", "password")),
    "jellyfin": (jellyfin_client, "test_connection", ("url", "api_key", "username"),
                 ("url", "api_key", "username")),
    "mirror_jellyfin": (jellyfin_client, "test_connection", ("url", "api_key", "username"),
                        ("url", "api_key", "username")),
    "emby": (emby_client, "test_connection", ("url", "api_key", "username"),
             ("url", "api_key", "username")),
    "mirror_emby": (emby_client, "test_connection", ("url", "api_key", "username"),
                    ("url", "api_key", "username")),
    "plex": (plex_client, "test_connection", ("url", "token"), ("url", "token")),
    "mirror_plex": (plex_client, "test_connection", ("url", "token"), ("url", "token")),
    "lms": (lms_client, "test_connection", ("url", "username", "password"), ("url",)),
    "lidarr": (lidarr_client, "test_connection", ("url", "api_key"), ("url", "api_key")),
    "music_assistant": (music_assistant_client, "test_connection", ("url", "token"), ("url", "token")),
}


# The connection fields the Change library source flow sends per target,
# and which of them are required. A subset of what PUT /api/admin/config
# takes, under the same names test-connection uses. Filesystem needs none:
# it reads the music folder Trobar already has.
_SWITCH_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "roon": (("host", "port"), ("host", "port")),
    "subsonic": (("url", "username", "password"), ("url", "username", "password")),
    "jellyfin": (("url", "api_key", "username"), ("url", "api_key", "username")),
    "emby": (("url", "api_key", "username"), ("url", "api_key", "username")),
    "plex": (("url", "token"), ("url", "token")),
    "lms": (("url", "username", "password"), ("url",)),
    "music_assistant": (("url", "token"), ("url", "token")),
    "filesystem": ((), ()),
}


# The mirror sinks, in the order the Playlist mirrors tab lists them: key
# (the prefix of each playlist's *_mirror_* columns, "mirror" for the
# folder), and how to read the target's configuration and live status.
# A new sink is one more entry here and in the page's mirrorSinkDefs.
_MIRROR_SINKS = (
    ("filesystem", "mirror", None),
    ("subsonic", "subsonic_mirror", lambda: subsonic_client.mirror_status()),
    ("jellyfin", "jellyfin_mirror", lambda: jellyfin_client.mirror_status()),
    ("emby", "emby_mirror", lambda: emby_client.mirror_status()),
    ("music_assistant", "music_assistant_mirror", lambda: music_assistant_client.mirror_status()),
    ("plex", "plex_mirror", lambda: plex_client.mirror_status()),
)


def _exposure_warning() -> int | None:
    """#389: Library-health signal for a deployment that looks directly
    exposed with TROBAR_TRUSTED_PROXY left at "*". Returns the distinct
    peer count (for the UI to compose its own translated message from,
    same pattern as _network_data_dir_warning/data_dir_network_fs), or
    None when there's nothing to say: trusted_proxy is pinned (the pin
    itself is the acknowledgement), forward mode (different boundary),
    or too few distinct peers seen yet to mean anything."""
    if _trusted_proxy() != "*" or settings.AUTH_MODE == "forward":
        return None
    count = _exposure_peer_count()
    return count if count > _EXPOSURE_WARN_THRESHOLD else None


def _exposure_status() -> int | None:
    """#389 (review feedback): the always-shown counterpart to
    _exposure_warning, same reasoning as #365's "library last scanned N
    days ago" — a signal that only ever speaks up past its threshold is
    indistinguishable from a signal that's silently broken (e.g. if a
    given deployment's Docker networking masks real client source IPs,
    _exposure_peer_count never grows and the warning simply never fires,
    which looks identical to "nothing to report"). Showing the live count
    even below threshold lets an operator confirm the mechanism is active
    and has correctly found nothing, rather than guessing. None (not 0)
    when the mechanism isn't active at all — trusted_proxy pinned or
    forward mode — so "not applicable" is never confused with "quiet"."""
    if _trusted_proxy() != "*" or settings.AUTH_MODE == "forward":
        return None
    return _exposure_peer_count()


def _apply_folder_settings(conn, body: dict) -> None:
    """The two playlist-folder settings, resolved and validated as a PAIR,
    then written together in one commit.

    One function rather than a block each, and one commit rather than two,
    because per-field commit stops being safe the moment two fields are
    validated against each other — which this endpoint had never had
    before. A PUT whose second field is refused has already committed the
    first, and the first was only ever checked against the second's
    *pending* value, the one that just got rejected: the pair left sitting
    in the store was never compared against anything. Resolving both,
    validating, and only then writing removes that class of hole rather
    than the one instance of it — a refused save leaves this endpoint
    exactly as it found it.

    Each single-field rule fires only for a field this request is actually
    changing, so an admin saving an unrelated setting is never 400'd by a
    pre-existing value they are not touching (the whole-object PUT problem
    again). The PAIR rule is the exception and fires whenever either side
    moves, since that is precisely when a good pair can become a bad one.
    With neither field in the body this does nothing at all, including no
    commit."""
    keys = ("extra_playlist_folder", "mirror_folder", "mirror_share_location")
    if not any(k in body for k in keys):
        return

    music_root = db.get_music_root()
    extra = _effective_folder(
        body, "extra_playlist_folder", "EXTRA_PLAYLIST_ROOT", db.get_extra_playlist_folder())
    mirror_folder = _effective_folder(
        body, "mirror_folder", "MIRROR_ROOT", db.get_mirror_folder())

    # The extra root is READ, so overlap with the library matters in either
    # direction: a folder containing MUSIC_ROOT walks the library too, and
    # every playlist under it is then discovered twice, once per root.
    if "extra_playlist_folder" in body and extra is not None \
            and _folders_overlap(extra, music_root):
        _config_field_error("extra_playlist_folder", _(
            "The extra playlist folder can't overlap your music library "
            "(MUSIC_ROOT) — playlists there are already discovered, and a "
            "folder on either side of it would import each one twice."))

    # #285: the mirror folder is WRITTEN, so only "inside MUSIC_ROOT"
    # matters — a mirror folder that CONTAINS the library still writes its
    # files outside it, and nothing re-imports them. Deliberately the
    # narrower rule of the two, not an oversight.
    if "mirror_folder" in body and mirror_folder is not None \
            and (mirror_folder == music_root or music_root in mirror_folder.parents):
        _config_field_error("mirror_folder", _(
            "The mirror folder can't be inside your music library "
            "(MUSIC_ROOT) — choose a separate folder."))

    # Overlap in either direction re-imports the mirror's own output as a
    # source playlist on the next sync — the same loop the MUSIC_ROOT rule
    # above exists to prevent, reachable through a second root.
    if extra is not None and mirror_folder is not None \
            and _folders_overlap(extra, mirror_folder):
        _config_field_error("extra_playlist_folder", _(
            "The extra playlist folder and the mirror output folder can't overlap — "
            "Trobar would re-import the playlists it just wrote there."))

    # Where the mirror folder sits inside the music share, for relative
    # entries. Re-checked when the mirror folder moves too, since that is
    # when a good location can become a bad one.
    if "mirror_share_location" in body:
        location = (body.get("mirror_share_location") or "").strip().strip("/")
        body["mirror_share_location"] = location
    else:
        location = db.get_mirror_share_location() or ""
    if location and ("mirror_share_location" in body or "mirror_folder" in body):
        parts = Path(location).parts
        # Leading and trailing slashes were stripped above: "/UserPlaylists/"
        # means UserPlaylists. A backslash would be one folder name on the
        # share but a separator on a Windows client.
        if ".." in parts or "\\" in location:
            _config_field_error("mirror_share_location", _(
                "Enter the mirror folder's location as a path inside your music folder, "
                "e.g. Playlists/Trobar, with no \"..\" and no backslash."))
        if mirror_folder is None:
            _config_field_error("mirror_share_location", _(
                "Set a mirror output folder first: the location says where that folder "
                "sits inside your music share."))
        if not mirror.seen_in_library(mirror_folder, music_root, location):
            _config_field_error("mirror_share_location", _(
                "That folder inside your music library isn't the mirror output folder: "
                "a test file written to the mirror folder didn't appear there. Check that "
                "both mounts point at the same folder on your share."))

    # Past every check: write only the fields this request actually sent,
    # and store the OVERRIDE (None for a cleared field) rather than the
    # resolved value validated above — see _effective_folder.
    for key in keys:
        if key in body:
            raw = (body.get(key) or "").strip()
            if key == "mirror_share_location":
                db.set_config(conn, key, raw or None)
            else:
                db.set_config(conn, key, os.path.normpath(raw) if raw else None)
    conn.commit()


# test_connection states that let a switch go ahead. Roon can only be
# "reachable" before the switch: see roon_client.test_connection.
_SWITCH_OK_STATES = ("paired", "reachable")


def _switch_values(target: str, body: dict) -> dict:
    """The target's connection fields from the request, stripped (passwords
    kept as typed), or a 400 naming what is missing or malformed."""
    field_names, required = _SWITCH_FIELDS[target]
    values = {}
    for f in field_names:
        raw = body.get(f)
        raw = "" if raw is None else str(raw)
        values[f] = raw if f == "password" else raw.strip()
    if any(not values[f].strip() for f in required):
        abort(400, description=_("All fields are required to test the connection."))
    if "url" in values and not _is_valid_url(values["url"]):
        abort(400, description=_invalid_url_message("Server URL"))
    if target == "roon" and not values["port"].isdigit():
        abort(400, description=_("The port must be a number."))
    return values


def _test_switch_target(target: str, values: dict) -> dict:
    """The live check the flow's Connect step shows and the switch repeats,
    against the values as typed and persisting nothing."""
    if target == "filesystem":
        return {"state": "paired", "provider": "filesystem"}
    if target == "roon":
        return roon_client.test_connection(values["host"], int(values["port"]))
    module, fn_name, field_names, _required = _TEST_CONNECTION_PROVIDERS[target]
    return getattr(module, fn_name)(*[values[f] for f in field_names])


def _connect_switch_target(target: str, values: dict) -> None:
    """Stores the target's connection and connects, through each client's
    own reconnect(), the same call PUT /api/admin/config makes."""
    if target == "roon":
        roon_client.reconnect(values["host"], int(values["port"]))
    elif target == "filesystem":
        filesystem_client.reconnect()
    else:
        _PROVIDERS[target].reconnect(*[values[f] for f in _SWITCH_FIELDS[target][0]])


def _switch_provider_rows(conn, target: str, carry_pairs=()) -> int:
    """Makes `target` the library source: carries the paired playlists over
    (playlist_sync.carry_over_playlists), removes the old provider's other
    playlists (playlist_sync.remove_provider_playlists: only its own rows,
    devices told), clears the artist-image cache, which holds whichever
    provider supplied each image, and stores the new provider. Returns how
    many playlists it removed."""
    old = _active_provider_id(conn)
    playlist_sync.carry_over_playlists(conn, target, carry_pairs)
    removed = playlist_sync.remove_provider_playlists(conn, old)
    shutil.rmtree(artist_images.CACHE_DIR, ignore_errors=True)
    db.set_config(conn, "artist_images_epoch", str(int(time.time())))
    db.set_config(conn, "provider", target)
    conn.commit()
    # Roon is the one source that holds a connection open and keeps retrying it.
    if old == "roon" and target != "roon":
        roon_client.stop()
    return removed


def _plan_carry_over(conn, target: str, values: dict | None = None) -> dict:
    """playlist_sync.plan_carry_over against the target. With `values`, it
    reads the target through those unsaved values (the preview, before
    anything is stored); without, through its stored connection (the
    switch, after storing it)."""
    old = _active_provider_id(conn)
    client = _PROVIDERS[target]
    if values is None or target in ("roon", "filesystem"):
        return playlist_sync.plan_carry_over(conn, old, target, client)
    names = _SWITCH_FIELDS[target][0]
    unsaved = tuple(values[f] for f in names)
    if target in playlist_sync.PER_USER_COLUMNS:
        # These clients' stored config holds the resolved user id.
        url, api_key, username = unsaved
        unsaved = (url, api_key, client._resolve_user_id(url, api_key, username))
    with provider_config.using(target, unsaved):
        return playlist_sync.plan_carry_over(conn, old, target, client)


def _switch_target(conn, body: dict, allow_active: bool = False) -> str:
    target = body.get("provider")
    if target not in _SWITCH_FIELDS:
        abort(400, description=_("Unknown provider."))
    if target == _active_provider_id(conn) and not allow_active:
        abort(409, description=_("That is already the library source."))
    return target


_HEALTH_ITEM_LIMIT = 200  # cap the per-category worklist so the payload stays small


def _last_scan_from_folder(row) -> int | None:
    try:
        result = json.loads(row["result"]) if row is not None and row["result"] else None
    except ValueError:
        return None
    value = result.get("from_folder") if isinstance(result, dict) else None
    return value if isinstance(value, int) else None


def _mirror_targets(rows: list, lidarr_configured: bool) -> list:
    """The configured targets, each with a state the tab can show at a
    glance: "ok", "warning" (reachable, but something needs attention) or
    "error" (unreachable, or the folder can't be written). Unconfigured
    targets are listed with configured=false so the page can offer them,
    never as a state.

    The three servers and Lidarr are probed live, as the Configuration page
    does on every load (nothing caches these statuses); only configured
    ones, and in parallel, so one slow target doesn't add to the others.
    The folder is checked locally. A reachable target whose last write
    failed for some playlist is a warning, with that failure's message."""
    folder = db.get_mirror_folder()
    configured = {
        "filesystem": folder is not None,
        "subsonic": db.get_mirror_subsonic_config() is not None,
        "jellyfin": db.get_mirror_jellyfin_config() is not None,
        "emby": db.get_mirror_emby_config() is not None,
        "music_assistant": db.get_mirror_music_assistant_config() is not None,
        "plex": db.get_mirror_plex_config() is not None,
        "lidarr": db.get_lidarr_connection() is not None,
    }
    probes = {key: probe for key, _prefix, probe in _MIRROR_SINKS if probe and configured[key]}
    if configured["lidarr"]:
        probes["lidarr"] = lambda: lidarr_client.status()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(probes))) as pool:
        futures = {key: pool.submit(probe) for key, probe in probes.items()}
        status = {key: f.result() for key, f in futures.items()}

    targets = []
    for key, prefix, _probe in _MIRROR_SINKS:
        target = {"key": key, "kind": "mirror", "configured": configured[key],
                  "state": None, "detail_code": None, "detail": None, "count": 0}
        if configured[key]:
            if key == "filesystem":
                assert folder is not None  # configured["filesystem"] says so
                reachable = folder.is_dir() and os.access(folder, os.W_OK)
                target["where"] = str(folder)
                unreachable_code = "not_writable"
            else:
                reachable = status[key]["state"] == "paired"
                target["where"] = status[key].get("url") or ""
                unreachable_code = "unreachable"
            failed, message = _failed_writes(rows, prefix)
            if not reachable:
                target.update(state="error", detail_code=unreachable_code, detail=message)
            elif failed:
                target.update(state="warning", detail_code="write_failed", detail=message, count=failed)
            else:
                target["state"] = "ok"
        targets.append(target)

    lidarr = {"key": "lidarr", "kind": "request", "configured": configured["lidarr"],
              "state": None, "detail_code": None, "detail": None, "count": 0}
    if configured["lidarr"]:
        failed_requests = [r for r in rows if r.get("lidarr_request_enabled") and r.get("lidarr_request_last_error_code")]
        if status["lidarr"]["state"] != "paired":
            lidarr.update(state="error", detail_code="unreachable")
        elif not lidarr_configured:
            lidarr.update(state="warning", detail_code="incomplete")
        elif failed_requests:
            lidarr.update(state="warning", detail_code="request_failed", count=len(failed_requests),
                          detail=failed_requests[0].get("lidarr_request_last_error"))
        else:
            lidarr["state"] = "ok"
    targets.append(lidarr)
    return targets


def _admin_provider_user_mapping(conn, client, column: str) -> dict:
    """#262: shared implementation behind the Jellyfin/Emby per-Trobar-user
    mapping routes below — same shape as api_admin_roon_profiles() above,
    generalized since Jellyfin/Emby's own list_users()/mapping column
    naming is identical in structure (unlike Roon, which maps to a
    profile NAME rather than an opaque id — so here the fuzzy-matched
    suggestion has to resolve back through a name->id lookup to land on
    something actually storable in `column`, rather than being directly
    usable as-is the way Roon's profile-name suggestion is)."""
    result = client.list_users()
    users_rows = conn.execute(
        f"SELECT id, username, {column} FROM users ORDER BY username"
    ).fetchall()

    if result["status"] != "ok":
        return result

    target_users = result["users"]
    ids_by_name = {u["name"]: u["id"] for u in target_users}
    users_out = []
    for row in users_rows:
        suggested_id = suggested_name = None
        if not row[column] and ids_by_name:
            close = difflib.get_close_matches(row["username"], list(ids_by_name.keys()), n=1, cutoff=0.85)
            if close:
                suggested_name = close[0]
                suggested_id = ids_by_name[suggested_name]
        users_out.append({
            "id": row["id"], "username": row["username"], "mapped_id": row[column],
            "suggested_id": suggested_id, "suggested_name": suggested_name,
        })
    return {"status": "ok", "target_users": target_users, "users": users_out}


# #297 step 2: how many recent jobs the admin overview lists. Unpaginated on
# purpose — jobs.py prunes finished rows to a retention cap, so the table
# can't grow without bound and this is a status view, not a worklist to page
# through. Kept below that cap so the panel always shows the newest ones.
_JOBS_PAGE_LIMIT = 50


@bp.route("/api/admin/counts")
def api_admin_counts():
    with db.connection() as conn:
        require_admin(conn)
        return jsonify(_admin_counts(conn))


@bp.route("/api/admin/config", methods=["GET", "PUT"])
def api_admin_config():
    """App-wide settings (active provider + its connection, default Last.fm
    key) — restricted to ADMIN_USERNAME, unlike everything else in this app
    which any logged-in household member can see/change. Provider-agnostic
    key names (roon_host/roon_port, subsonic_url/subsonic_username/
    subsonic_password, jellyfin_url/jellyfin_api_key/jellyfin_username,
    emby_url/emby_api_key/emby_username, plex_url/plex_token,
    lms_url/lms_username/lms_password) — "one or the other",
    never more than one active simultaneously.
    filesystem_client's music root itself is set via the setup wizard's own
    dedicated /api/setup/music-root step, not here — its status is still
    echoed back here so the admin UI can show the root path and paired
    state. It does have two editable fields through this endpoint though:
    itunes_library_path (#171) and extra_playlist_folder, both optional
    playlist sources layered on top of the filesystem provider rather than
    configs of their own, and both effective whichever provider is
    active."""
    with db.connection() as conn:
        require_admin(conn)
        if request.method == "PUT":
            body = request.get_json(force=True)

            # Once setup is complete, the library source changes only
            # through POST /api/admin/provider/switch, which tests the new
            # connection first and reports what it removes. A different
            # `provider` here (an old cached page, a script) is refused
            # rather than ignored, so the caller learns the switch didn't
            # happen. The same value is accepted: the page sends its whole
            # config object on every Save.
            new_provider = body.get("provider")
            if new_provider is not None and new_provider != _active_provider_id(conn):
                if db.get_config(conn, "setup_completed"):
                    _config_field_error("provider", _(
                        "The library source can't be changed with Save. "
                        "Use Change library source, which tests the new "
                        "connection and shows what the switch removes first."))
                # First-run setup's own picker: nothing synced yet worth
                # a review, but the same scoped removal as a switch.
                if new_provider in _PROVIDERS:
                    _switch_provider_rows(conn, new_provider)

            host = (body.get("roon_host") or "").strip()
            port = body.get("roon_port")
            if host and port:
                if _active_provider_id(conn) == "roon":
                    roon_client.reconnect(host, int(port))
                else:
                    roon_client.store_address(host, int(port))

            subsonic_url = (body.get("subsonic_url") or "").strip()
            subsonic_username = (body.get("subsonic_username") or "").strip()
            subsonic_password = body.get("subsonic_password")
            if subsonic_url and subsonic_username and subsonic_password:
                if not _is_valid_url(subsonic_url):
                    _config_field_error("subsonic_url", _invalid_url_message("Subsonic URL"))
                subsonic_client.reconnect(subsonic_url, subsonic_username, subsonic_password)

            jellyfin_url = (body.get("jellyfin_url") or "").strip()
            jellyfin_api_key = (body.get("jellyfin_api_key") or "").strip()
            jellyfin_username = (body.get("jellyfin_username") or "").strip()
            if jellyfin_url and jellyfin_api_key and jellyfin_username:
                if not _is_valid_url(jellyfin_url):
                    _config_field_error("jellyfin_url", _invalid_url_message("Jellyfin URL"))
                jellyfin_client.reconnect(jellyfin_url, jellyfin_api_key, jellyfin_username)

            # #168: Emby — Jellyfin's upstream, same config shape (url +
            # api key + a username resolved to an internal userId).
            emby_url = (body.get("emby_url") or "").strip()
            emby_api_key = (body.get("emby_api_key") or "").strip()
            emby_username = (body.get("emby_username") or "").strip()
            if emby_url and emby_api_key and emby_username:
                if not _is_valid_url(emby_url):
                    _config_field_error("emby_url", _invalid_url_message("Emby URL"))
                emby_client.reconnect(emby_url, emby_api_key, emby_username)

            # #158: no username, unlike Jellyfin/Emby above — the token is
            # already scoped to one Plex account.
            plex_url = (body.get("plex_url") or "").strip()
            plex_token = (body.get("plex_token") or "").strip()
            if plex_url and plex_token:
                if not _is_valid_url(plex_url):
                    _config_field_error("plex_url", _invalid_url_message("Plex URL"))
                plex_client.reconnect(plex_url, plex_token)

            # #172: username/password optional — only sent as Basic Auth
            # when LMS's own "Authorize" setting is on (off by default).
            # reconnect() persists blank username/password fine, so this
            # only requires the URL, unlike every other server-provider
            # above.
            lms_url = (body.get("lms_url") or "").strip()
            if lms_url:
                if not _is_valid_url(lms_url):
                    _config_field_error("lms_url", _invalid_url_message("LMS URL"))
                lms_client.reconnect(lms_url, (body.get("lms_username") or "").strip(),
                                      body.get("lms_password") or "")

            if "lastfm_app_api_key" in body:
                db.set_config(conn, "lastfm_app_api_key", (body.get("lastfm_app_api_key") or "").strip() or None)
                conn.commit()
            if "lastfm_app_secret" in body:
                # Write-only, like the session it signs for. The form sends an
                # empty field with every save, so empty leaves it; null clears it.
                secret = body.get("lastfm_app_secret")
                if secret is None:
                    db.set_config(conn, play_forward.APP_SECRET_CONFIG, None)
                elif isinstance(secret, str) and secret.strip():
                    try:
                        db.set_config(conn, play_forward.APP_SECRET_CONFIG, credential_store.encrypt(
                            secret.strip(), 0, play_forward.APP_SECRET_CONFIG))
                    except credential_store.CredentialKeyError:
                        abort(503, description=_("The server's credential key can't be used."))
                conn.commit()
            if "lastfm_api_key_default" in body:
                db.set_config(conn, "lastfm_api_key_default", body.get("lastfm_api_key_default") or None)
                conn.commit()

            # TheAudioDB key makes the cleanly-licensed source
            # the primary for artist images; clearing it falls back to the
            # provider-first behaviour. Cache cleared on change so already-
            # cached provider images get re-fetched from the better source.
            if "audiodb_api_key" in body:
                old_key = db.get_config(conn, "audiodb_api_key")
                new_key = body.get("audiodb_api_key") or None
                if new_key != old_key:
                    shutil.rmtree(artist_images.CACHE_DIR, ignore_errors=True)
                    db.set_config(conn, "artist_images_epoch", str(int(time.time())))
                db.set_config(conn, "audiodb_api_key", new_key)
                conn.commit()

            # #200: opt-in AcoustID key for fingerprint.py's local-library
            # ISRC backfill (scan-triggered, see scanner.start_scan). Blank
            # = skip fingerprinting entirely, same "opt-in, blank = skip"
            # shape as the other provider keys here — no cache to
            # invalidate on change, unlike audiodb_api_key above.
            if "acoustid_api_key" in body:
                db.set_config(conn, "acoustid_api_key", (body.get("acoustid_api_key") or "").strip() or None)
                conn.commit()

            # Point suggestions/listening-history reads at a self-hosted
            # alternative (Libre.fm, self-hosted ListenBrainz) instead of
            # the real services — blank means "use the default" (the
            # LASTFM_API_BASE/LISTENBRAINZ_API_BASE env var if set, else the
            # real service). See lastfm.API_BASE / listenbrainz.API_BASE.
            if "lastfm_api_base" in body:
                lastfm_api_base = (body.get("lastfm_api_base") or "").strip()
                if lastfm_api_base and not _is_valid_url(lastfm_api_base):
                    _config_field_error("lastfm_api_base", _invalid_url_message("Last.fm API base URL"))
                db.set_config(conn, "lastfm_api_base", lastfm_api_base or None)
                conn.commit()
            if "listenbrainz_api_base" in body:
                listenbrainz_api_base = (body.get("listenbrainz_api_base") or "").strip()
                if listenbrainz_api_base and not _is_valid_url(listenbrainz_api_base):
                    _config_field_error("listenbrainz_api_base", _invalid_url_message("ListenBrainz API base URL"))
                db.set_config(conn, "listenbrainz_api_base", listenbrainz_api_base or None)
                conn.commit()

            # #21's direct Tidal provider: one admin-registered OAuth app
            # (developer.tidal.com), each Trobar user then links their own
            # Tidal account against it (Profile > "Streaming accounts").
            # Evict the cached Authlib client on change — see
            # _tidal_oauth_client's docstring for why re-register() alone
            # wouldn't pick this up without a restart.
            if "tidal_client_id" in body or "tidal_client_secret" in body:
                old_id = db.get_config(conn, "tidal_client_id")
                old_secret = db.get_config(conn, "tidal_client_secret")
                new_id = (body.get("tidal_client_id", old_id) or "").strip() or None
                new_secret = (body.get("tidal_client_secret", old_secret) or "").strip() or None
                if (new_id, new_secret) != (old_id, old_secret):
                    oauth._clients.pop("tidal", None)
                db.set_config(conn, "tidal_client_id", new_id)
                db.set_config(conn, "tidal_client_secret", new_secret)
                conn.commit()

            # The admin's word that their Tidal app has playlists.write
            # enabled. It changes the scope Connect requests, which the
            # cached client holds, so a change evicts it like new
            # credentials do.
            if "tidal_playlists_write" in body:
                new_val = "1" if body.get("tidal_playlists_write") else "0"
                if new_val != (db.get_config(conn, "tidal_playlists_write") or "0"):
                    oauth._clients.pop("tidal", None)
                db.set_config(conn, "tidal_playlists_write", new_val)
                conn.commit()

            if "spotify_client_id" in body or "spotify_client_secret" in body:
                old_id = db.get_config(conn, "spotify_client_id")
                old_secret = db.get_config(conn, "spotify_client_secret")
                new_id = (body.get("spotify_client_id", old_id) or "").strip() or None
                new_secret = (body.get("spotify_client_secret", old_secret) or "").strip() or None
                if (new_id, new_secret) != (old_id, old_secret):
                    oauth._clients.pop("spotify", None)  # #10: pick up new creds without a restart
                db.set_config(conn, "spotify_client_id", new_id)
                db.set_config(conn, "spotify_client_secret", new_secret)
                conn.commit()

            # #398: an explicit admin decision, independent of whether
            # credentials happen to be set -- toggling this off pauses the
            # feature (routes refuse, Profile hides the block) without
            # discarding spotify_client_id/secret, so re-enabling later
            # needs no re-entry.
            if "experimental_spotify_enabled" in body:
                db.set_config(
                    conn, "experimental_spotify_enabled",
                    "1" if body.get("experimental_spotify_enabled") else "0")
                conn.commit()

            # How many tracks may transcode at once, and how much CPU
            # priority ffmpeg gets — resource hygiene, not crash
            # prevention, so the admin decides rather than a hardcoded
            # constant (see transcode.py).
            if "transcode_concurrency" in body:
                try:
                    val = max(1, int(body.get("transcode_concurrency")))
                except (TypeError, ValueError):
                    _config_field_error("transcode_concurrency", _("Concurrency must be a whole number of 1 or more."))
                db.set_config(conn, "transcode_concurrency", str(val))
                conn.commit()

            if "transcode_nice_level" in body:
                try:
                    val = int(body.get("transcode_nice_level"))
                except (TypeError, ValueError):
                    _config_field_error("transcode_nice_level", _("Nice level must be a whole number."))
                if not (0 <= val <= 19):
                    _config_field_error("transcode_nice_level", _("Nice level must be between 0 and 19."))
                db.set_config(conn, "transcode_nice_level", str(val))
                conn.commit()

            # The transcode cache's cap, in GB; 0 keeps nothing. Applied at
            # once, so lowering it (or turning it off) frees the space now
            # rather than at the next transcode.
            if "transcode_cache_gb" in body:
                try:
                    val = int(body.get("transcode_cache_gb"))
                except (TypeError, ValueError):
                    _config_field_error("transcode_cache_gb", _("The cache size must be a whole number of GB."))
                if not (0 <= val <= 10000):
                    _config_field_error("transcode_cache_gb", _("The cache size must be between 0 and 10000 GB."))
                db.set_config(conn, "transcode_cache_gb", str(val))
                conn.commit()
                transcode.evict(transcode.cache_dir(db.DATA_DIR), val * 1024 ** 3)

            # #361: how long finished background jobs stay in full before
            # collapsing to one row per type/outcome — see jobs._prune_finished.
            if "job_retention_days" in body:
                try:
                    val = int(body.get("job_retention_days"))
                except (TypeError, ValueError):
                    _config_field_error("job_retention_days", _("Retention must be a whole number of days."))
                if not (1 <= val <= 3650):
                    _config_field_error("job_retention_days", _("Retention must be between 1 and 3650 days."))
                db.set_config(conn, "job_retention_days", str(val))
                conn.commit()

            # #362: 0 (the default) means off — a fresh install behaves
            # exactly as it did before this existed, per the issue's explicit
            # "defaulting to off" decision. See scanner.maybe_schedule_rescan.
            if "scan_interval_hours" in body:
                try:
                    val = int(body.get("scan_interval_hours"))
                except (TypeError, ValueError):
                    _config_field_error("scan_interval_hours", _("Scan interval must be a whole number of hours."))
                if not (0 <= val <= 8760):  # 8760h = 1 year; 0 = off
                    _config_field_error("scan_interval_hours", _("Scan interval must be between 0 (off) and 8760 hours."))
                db.set_config(conn, "scan_interval_hours", str(val))
                conn.commit()

            # 0 means off. A fresh install starts at 6, an upgraded one at 0
            # (db._seed_playlist_sync_interval_default). See
            # _maybe_schedule_playlist_sync.
            if "playlist_sync_interval_hours" in body:
                try:
                    val = int(body.get("playlist_sync_interval_hours"))
                except (TypeError, ValueError):
                    _config_field_error("playlist_sync_interval_hours",
                                        _("Playlist sync interval must be a whole number of hours."))
                if not (0 <= val <= 8760):  # 8760h = 1 year; 0 = off
                    _config_field_error("playlist_sync_interval_hours",
                                        _("Playlist sync interval must be between 0 (off) and 8760 hours."))
                db.set_config(conn, "playlist_sync_interval_hours", str(val))
                conn.commit()

            # On unless set off. See _sync_after_scan.
            if "playlist_sync_after_scan" in body:
                db.set_config(conn, "playlist_sync_after_scan",
                              "1" if body.get("playlist_sync_after_scan") else "0")
                conn.commit()

            # #171: an optional path to an exported iTunes/Apple Music
            # Library.xml, merged into filesystem_client's own playlist
            # listing alongside its .m3u discovery — see
            # filesystem_client._itunes_library_path. Not gated on the
            # active provider being filesystem (same as every other
            # field here), since it only takes effect while it is.
            if "itunes_library_path" in body:
                db.set_config(conn, "itunes_library_path", (body.get("itunes_library_path") or "").strip() or None)
                conn.commit()

            # The two playlist-folder settings: an optional SECOND root
            # walked for .m3u/.m3u8 alongside MUSIC_ROOT (read side —
            # Trobar never writes there), and #285's opt-in mirroring
            # output folder (write side). Handled together, in one place
            # and one commit, because they are validated against each
            # other — see _apply_folder_settings for why per-field commit
            # is unsafe once that is true. Neither is gated on the active
            # provider, same reasoning as itunes_library_path above.
            _apply_folder_settings(conn, body)

            # #189: the Subsonic mirror-TARGET connection — a distinct
            # write destination from subsonic_url/username/password above
            # (that triple is the active read-source; see
            # db.get_mirror_subsonic_config()'s docstring). Same
            # all-three-or-nothing shape as every other provider block
            # here.
            mirror_subsonic_url = (body.get("mirror_subsonic_url") or "").strip()
            mirror_subsonic_username = (body.get("mirror_subsonic_username") or "").strip()
            mirror_subsonic_password = body.get("mirror_subsonic_password") or ""
            if mirror_subsonic_url and mirror_subsonic_username and mirror_subsonic_password:
                if not _is_valid_url(mirror_subsonic_url):
                    _config_field_error("mirror_subsonic_url", _invalid_url_message("Subsonic mirror-target URL"))
                subsonic_client.mirror_reconnect(
                    mirror_subsonic_url, mirror_subsonic_username, mirror_subsonic_password)
            elif "mirror_subsonic_url" in body and not mirror_subsonic_url \
                    and not mirror_subsonic_username and not mirror_subsonic_password:
                # #189 review: unlike every other provider-connection
                # triple in this route, this one has no "switch away"
                # mechanism -- a read-source's unused credentials just go
                # inert when a different provider is picked, but this
                # write target stays authoritative for every
                # subsonic_mirror_enabled playlist until explicitly
                # cleared. Gated on the key's presence (unlike the connect
                # branch above) so a partial payload that never mentions
                # this triple can't accidentally wipe it.
                db.set_config(conn, "mirror_subsonic_url", None)
                db.set_config(conn, "mirror_subsonic_username", None)
                db.set_config(conn, "mirror_subsonic_password", None)
                conn.commit()

            # #189: the Jellyfin mirror-TARGET connection — same shape and
            # same reasoning as the Subsonic block just above (a distinct
            # write destination from jellyfin_url/api_key/username below,
            # explicit clear path since this sink has no "switch away"
            # mechanism).
            mirror_jellyfin_url = (body.get("mirror_jellyfin_url") or "").strip()
            mirror_jellyfin_api_key = (body.get("mirror_jellyfin_api_key") or "").strip()
            mirror_jellyfin_username = (body.get("mirror_jellyfin_username") or "").strip()
            if mirror_jellyfin_url and mirror_jellyfin_api_key and mirror_jellyfin_username:
                if not _is_valid_url(mirror_jellyfin_url):
                    _config_field_error("mirror_jellyfin_url", _invalid_url_message("Jellyfin mirror-target URL"))
                jellyfin_client.mirror_reconnect(
                    mirror_jellyfin_url, mirror_jellyfin_api_key, mirror_jellyfin_username)
            elif "mirror_jellyfin_url" in body and not mirror_jellyfin_url \
                    and not mirror_jellyfin_api_key and not mirror_jellyfin_username:
                db.set_config(conn, "mirror_jellyfin_url", None)
                db.set_config(conn, "mirror_jellyfin_api_key", None)
                db.set_config(conn, "mirror_jellyfin_username", None)
                db.set_config(conn, "mirror_jellyfin_user_id", None)
                conn.commit()

            # #189: the Emby mirror-TARGET connection — same shape and same
            # reasoning as the Subsonic/Jellyfin blocks just above (a
            # distinct write destination from emby_url/api_key/username
            # below, explicit clear path since this sink has no "switch
            # away" mechanism).
            mirror_emby_url = (body.get("mirror_emby_url") or "").strip()
            mirror_emby_api_key = (body.get("mirror_emby_api_key") or "").strip()
            mirror_emby_username = (body.get("mirror_emby_username") or "").strip()
            if mirror_emby_url and mirror_emby_api_key and mirror_emby_username:
                if not _is_valid_url(mirror_emby_url):
                    _config_field_error("mirror_emby_url", _invalid_url_message("Emby mirror-target URL"))
                emby_client.mirror_reconnect(
                    mirror_emby_url, mirror_emby_api_key, mirror_emby_username)
            elif "mirror_emby_url" in body and not mirror_emby_url \
                    and not mirror_emby_api_key and not mirror_emby_username:
                db.set_config(conn, "mirror_emby_url", None)
                db.set_config(conn, "mirror_emby_api_key", None)
                db.set_config(conn, "mirror_emby_username", None)
                db.set_config(conn, "mirror_emby_user_id", None)
                conn.commit()

            # The Plex mirror target: its own URL and token, the same shape
            # as the Emby block above (a write destination distinct from
            # plex_url/plex_token, cleared when both fields are blank).
            mirror_plex_url = (body.get("mirror_plex_url") or "").strip()
            mirror_plex_token = (body.get("mirror_plex_token") or "").strip()
            if mirror_plex_url and mirror_plex_token:
                if not _is_valid_url(mirror_plex_url):
                    _config_field_error("mirror_plex_url", _invalid_url_message("Plex mirror-target URL"))
                db.set_config(conn, "mirror_plex_url", mirror_plex_url)
                db.set_config(conn, "mirror_plex_token", mirror_plex_token)
                conn.commit()
            elif "mirror_plex_url" in body and not mirror_plex_url and not mirror_plex_token:
                db.set_config(conn, "mirror_plex_url", None)
                db.set_config(conn, "mirror_plex_token", None)
                conn.commit()

            # #494: the Lidarr connection for "Request missing albums" —
            # not a mirror target (nothing is copied to it), but the same
            # explicit-clear-on-blank shape. Two INDEPENDENT gated blocks,
            # not one: url+api_key must be saved and live BEFORE the admin
            # can even see what root folders/quality profiles/metadata
            # profiles exist to choose from (GET /api/admin/lidarr-options
            # needs a working connection) — there is no single "all fields
            # at once" moment here the way there is for every other
            # provider's connection.
            lidarr_url = (body.get("lidarr_url") or "").strip()
            lidarr_api_key = (body.get("lidarr_api_key") or "").strip()
            if lidarr_url and lidarr_api_key:
                if not _is_valid_url(lidarr_url):
                    _config_field_error("lidarr_url", _invalid_url_message("Lidarr URL"))
                lidarr_client.reconnect(lidarr_url, lidarr_api_key)
            elif "lidarr_url" in body and not lidarr_url and not lidarr_api_key:
                db.set_config(conn, "lidarr_url", None)
                db.set_config(conn, "lidarr_api_key", None)
                # Clearing the connection also invalidates the three
                # profile fields below — they're ids/paths specific to
                # THIS Lidarr instance and would mean nothing (or worse,
                # something wrong) against a different one.
                db.set_config(conn, "lidarr_root_folder_path", None)
                db.set_config(conn, "lidarr_quality_profile_id", None)
                db.set_config(conn, "lidarr_metadata_profile_id", None)
                conn.commit()

            lidarr_root_folder_path = (body.get("lidarr_root_folder_path") or "").strip()
            lidarr_quality_profile_id = body.get("lidarr_quality_profile_id")
            lidarr_metadata_profile_id = body.get("lidarr_metadata_profile_id")
            if lidarr_root_folder_path and lidarr_quality_profile_id and lidarr_metadata_profile_id:
                try:
                    quality_id = int(lidarr_quality_profile_id)
                    metadata_id = int(lidarr_metadata_profile_id)
                except (TypeError, ValueError):
                    _config_field_error("lidarr_quality_profile_id", _("Invalid Lidarr profile selection."))
                db.set_config(conn, "lidarr_root_folder_path", lidarr_root_folder_path)
                db.set_config(conn, "lidarr_quality_profile_id", str(quality_id))
                db.set_config(conn, "lidarr_metadata_profile_id", str(metadata_id))
                conn.commit()
                # Gaps synced before Lidarr was set up get their albums
                # looked up now, not at the next sync.
                album_lookup.enqueue(conn)

            # Music Assistant: an extra playlist source merged in whichever
            # provider is active, so its connection sits outside the
            # provider selector. URL and token are saved together; both
            # blank disconnects.
            ma_url = (body.get("music_assistant_url") or "").strip()
            ma_token = (body.get("music_assistant_token") or "").strip()
            if ma_url and ma_token:
                if not _is_valid_url(ma_url):
                    _config_field_error("music_assistant_url", _invalid_url_message("Music Assistant URL"))
                music_assistant_client.reconnect(ma_url, ma_token)
            elif "music_assistant_url" in body and not ma_url and not ma_token:
                music_assistant_client.reconnect("", "")

        roon_status = roon_client.status()
        subsonic_status = subsonic_client.status()
        subsonic_mirror_status = subsonic_client.mirror_status()
        jellyfin_status = jellyfin_client.status()
        jellyfin_mirror_status = jellyfin_client.mirror_status()
        emby_status = emby_client.status()
        emby_mirror_status = emby_client.mirror_status()
        plex_mirror_status = plex_client.mirror_status()
        lidarr_status = lidarr_client.status()
        music_assistant_status = music_assistant_client.status()
        plex_status = plex_client.status()
        lms_status = lms_client.status()
        filesystem_status = filesystem_client.status()
        return jsonify({
            "provider": db.get_config(conn, "provider") or "roon",
            "roon_host": roon_status["host"],
            "roon_port": roon_status["port"],
            "subsonic_url": subsonic_status["url"],
            "subsonic_username": db.get_config(conn, "subsonic_username") or "",
            "subsonic_password": db.get_config(conn, "subsonic_password") or "",
            "jellyfin_url": jellyfin_status["url"],
            "jellyfin_api_key": db.get_config(conn, "jellyfin_api_key") or "",
            "jellyfin_username": db.get_config(conn, "jellyfin_username") or "",
            "emby_url": emby_status["url"],
            "emby_api_key": db.get_config(conn, "emby_api_key") or "",
            "emby_username": db.get_config(conn, "emby_username") or "",
            "plex_url": plex_status["url"],
            "plex_token": db.get_config(conn, "plex_token") or "",
            "lms_url": lms_status["url"],
            "lms_username": db.get_config(conn, "lms_username") or "",
            "lms_password": db.get_config(conn, "lms_password") or "",
            "filesystem_root": filesystem_status["root"],
            "itunes_library_path": db.get_config(conn, "itunes_library_path") or "",
            # str() of the effective value, not the raw config row —
            # same as mirror_folder below, so an EXTRA_PLAYLIST_ROOT set
            # only in the environment still shows in the admin form.
            "extra_playlist_folder": str(db.get_extra_playlist_folder() or "") or "",
            "mirror_folder": str(db.get_mirror_folder() or "") or "",
            "mirror_share_location": db.get_mirror_share_location() or "",
            # #189: echoed back the same way every other provider's
            # connection is (subsonic_url/username/password above) — the
            # admin config form can't stay pre-filled across a reload
            # otherwise.
            "mirror_subsonic_url": subsonic_mirror_status["url"],
            "mirror_subsonic_username": db.get_config(conn, "mirror_subsonic_username") or "",
            "mirror_subsonic_password": db.get_config(conn, "mirror_subsonic_password") or "",
            # #189 review: mirror_status() was computed above but the
            # state itself never left this function -- the admin had no
            # signal the target credentials actually work until a
            # per-playlist error surfaced later. "paired"/"disconnected",
            # same values as every other provider's own status.
            "mirror_subsonic_state": subsonic_mirror_status["state"],
            # #189: same reasoning as the Subsonic mirror-target trio above.
            "mirror_jellyfin_url": jellyfin_mirror_status["url"],
            "mirror_jellyfin_api_key": db.get_config(conn, "mirror_jellyfin_api_key") or "",
            "mirror_jellyfin_username": db.get_config(conn, "mirror_jellyfin_username") or "",
            "mirror_jellyfin_state": jellyfin_mirror_status["state"],
            # #189: same reasoning, for the fourth sink.
            "mirror_emby_url": emby_mirror_status["url"],
            "mirror_emby_api_key": db.get_config(conn, "mirror_emby_api_key") or "",
            "mirror_emby_username": db.get_config(conn, "mirror_emby_username") or "",
            "mirror_emby_state": emby_mirror_status["state"],
            "mirror_plex_url": plex_mirror_status["url"],
            "mirror_plex_token": db.get_config(conn, "mirror_plex_token") or "",
            "mirror_plex_state": plex_mirror_status["state"],
            # #494: not a mirror target — echoed the same way regardless,
            # same reasoning (the admin config form can't stay pre-filled
            # across a reload otherwise).
            "lidarr_url": lidarr_status["url"],
            "lidarr_api_key": db.get_config(conn, "lidarr_api_key") or "",
            "lidarr_state": lidarr_status["state"],
            "lidarr_root_folder_path": db.get_config(conn, "lidarr_root_folder_path") or "",
            "lidarr_quality_profile_id": db.get_config(conn, "lidarr_quality_profile_id") or "",
            "lidarr_metadata_profile_id": db.get_config(conn, "lidarr_metadata_profile_id") or "",
            "music_assistant_url": music_assistant_status["url"],
            "music_assistant_token": db.get_config(conn, "music_assistant_token") or "",
            "music_assistant_state": music_assistant_status["state"],
            "tidal_client_id": db.get_config(conn, "tidal_client_id") or "",
            "tidal_client_secret": db.get_config(conn, "tidal_client_secret") or "",
            "tidal_playlists_write": db.get_config(conn, "tidal_playlists_write") == "1",
            "spotify_client_id": db.get_config(conn, "spotify_client_id") or "",
            "spotify_client_secret": db.get_config(conn, "spotify_client_secret") or "",
            "experimental_spotify_enabled": db.get_config(conn, "experimental_spotify_enabled") == "1",
            "lastfm_api_key_default": db.get_config(conn, "lastfm_api_key_default") or "",
            "lastfm_app_api_key": db.get_config(conn, "lastfm_app_api_key") or "",
            "lastfm_app_secret_set": bool(db.get_config(conn, play_forward.APP_SECRET_CONFIG)),
            "audiodb_api_key": db.get_config(conn, "audiodb_api_key") or "",
            "acoustid_api_key": db.get_config(conn, "acoustid_api_key") or "",
            "lastfm_api_base": db.get_config(conn, "lastfm_api_base") or "",
            "listenbrainz_api_base": db.get_config(conn, "listenbrainz_api_base") or "",
            "transcode_concurrency": _config_int(conn, "transcode_concurrency", 1),
            "transcode_nice_level": _config_int(conn, "transcode_nice_level", 10),
            "transcode_cache_gb": _config_int(conn, "transcode_cache_gb", transcode.DEFAULT_CACHE_GB),
            "job_retention_days": _config_int(conn, "job_retention_days", jobs.DEFAULT_JOB_RETENTION_DAYS),
            "scan_interval_hours": _config_int(conn, "scan_interval_hours", 0),
            # #362: "make the next scheduled run visible" — None when
            # scheduling is off, or when it's on but nothing has scanned yet
            # (due immediately, so there's no future timestamp to show).
            "next_scheduled_scan_at": scanner.next_scheduled_scan_at(conn),
            "playlist_sync_interval_hours": _config_int(conn, "playlist_sync_interval_hours", 0),
            # None when off, or on but nothing has synced yet (due now).
            "next_scheduled_playlist_sync_at": playlist_sync.next_scheduled_sync_at(conn),
            "playlist_sync_after_scan": db.get_config(conn, "playlist_sync_after_scan") != "0",
            "auth_mode": settings.AUTH_MODE,  # read-only display — changing it is an env var + restart, not a live toggle
            # surfaces a UI warning banner when the music library is
            # mounted writable (None/unknown is treated as not-writable so we
            # don't cry wolf before setup is done).
            "music_root_writable": _music_root_is_readonly() is False,
            # #246: under oidc, an admin's local password is the only login
            # that survives the IdP being unreachable — this drives the
            # break-glass card's warning state if none is set at all (a
            # yes/no signal, never the hash itself).
            "break_glass_set": conn.execute(
                "SELECT EXISTS(SELECT 1 FROM users WHERE is_admin = 1 AND password_hash IS NOT NULL) AS v"
            ).fetchone()["v"] == 1,
        })


@bp.route("/api/admin/config/test-connection", methods=["POST"])
def api_admin_config_test_connection():
    """#509 item 3: verify credentials AS TYPED, without saving them — the
    non-persisting counterpart to PUT /api/admin/config's bulk save (which
    sends the whole adminConfig object in one request; wiring a live check
    to THAT endpoint on every field blur would silently persist every
    other half-typed field on the page too). Reuses each provider's
    existing test_connection() (a thin wrapper over the same low-level
    ping/request helper status()/reconnect() already use, just against
    explicit values instead of the stored config) — this route's own job
    is only the provider dispatch and the pre-flight URL check, not any
    new connection logic.

    Response is the SAME {"state": ...} shape status()/test_connection()
    already return, plus one more state this route adds itself:
    "invalid_url" — a malformed URL short-circuits before any network
    call, reusing _is_valid_url() from #509's own earlier fix, same
    well-formedness check PUT /api/admin/config applies at save time."""
    with db.connection() as conn:
        require_admin(conn)
    body = request.get_json(force=True)
    provider = body.get("provider")
    entry = _TEST_CONNECTION_PROVIDERS.get(provider)
    if entry is None:
        abort(400, description=_("Unknown provider."))
    module, fn_name, field_names, required_field_names = entry
    values = {f: (body.get(f) or "") for f in field_names}
    if any(not values[f].strip() for f in required_field_names):
        abort(400, description=_("All fields are required to test the connection."))
    url = values["url"].strip()
    if not _is_valid_url(url):
        return jsonify({"state": "invalid_url"})
    args = [values[f].strip() if f != "password" else values[f] for f in field_names]
    args[0] = url
    return jsonify(getattr(module, fn_name)(*args))


@bp.route("/api/admin/provider/switch/test", methods=["POST"])
def api_admin_provider_switch_test():
    """The Change library source flow's Connect step: test the target's
    connection as typed, without saving anything. Also the setup wizard's
    test when it is run again, so the active source is allowed here: testing
    changes nothing."""
    with db.connection() as conn:
        require_admin(conn)
        body = request.get_json(force=True) or {}
        target = _switch_target(conn, body, allow_active=True)
    result = _test_switch_target(target, _switch_values(target, body))
    return jsonify({**result, "ok": result.get("state") in _SWITCH_OK_STATES})


@bp.route("/api/admin/provider/switch/preview", methods=["POST"])
def api_admin_provider_switch_preview():
    """The flow's review step: what switching to the target would carry
    over, remove and keep, counted now. Takes the same body as the switch,
    since the carry-over is computed from the target's own playlists, read
    with the connection as typed. Changes nothing. A POST so the
    credentials stay out of the URL."""
    with db.connection() as conn:
        require_admin(conn)
        body = request.get_json(force=True) or {}
        target = _switch_target(conn, body)
        values = _switch_values(target, body)
        carry = _plan_carry_over(conn, target, values)
        return jsonify(playlist_sync.switch_preview(conn, _active_provider_id(conn), carry))


@bp.route("/api/admin/provider/switch", methods=["POST"])
def api_admin_provider_switch():
    """Switch the library source: the flow's confirm step. Re-tests the
    target's connection (the Connect step's result may be stale) and plans
    the carry-over again, refusing (409, `code: plan_changed`) unless it
    carries over exactly the old playlists the body's `carry` lists (the
    ids the review step showed). Then it stores the connection, carries the
    old provider's playlists over to their namesakes in the target, removes
    the rest, makes the target active and starts the first playlist sync. Returns the preview as it
    stood just before the switch, which is what it carried over and
    removed, and the sync job. Refused while a playlist sync runs: that sync would write
    the old provider's rows back after the removal."""
    with db.connection() as conn:
        require_admin(conn)
        body = request.get_json(force=True) or {}
        target = _switch_target(conn, body)
        values = _switch_values(target, body)
        if playlist_sync.sync_status()["running"]:
            abort(409, description=_(
                "A playlist sync is running. Switch when it has finished."))
        check = _test_switch_target(target, values)
        if check.get("state") not in _SWITCH_OK_STATES:
            return jsonify({"error": _("The connection test failed, so nothing was changed."),
                            "state": check.get("state")}), 400
        # Planned again, through the connection as typed (nothing stored
        # yet), against the target as it is now. It must carry over exactly
        # the playlists the review step showed and the admin confirmed
        # (`carry`, their old ids): a target that answers differently now (a
        # failed listing, a playlist renamed or edited there) would otherwise
        # remove playlists the review promised to keep. So a different plan
        # changes nothing and asks for a fresh review.
        carry = _plan_carry_over(conn, target, values)
        confirmed = {int(i) for i in body.get("carry") or [] if str(i).isdigit()}
        if {old_id for old_id, _src, _title in carry["pairs"]} != confirmed:
            resp = jsonify({"error": _("What can be carried over has changed since the review, so "
                                       "nothing was changed. Review it again."),
                            "code": "plan_changed"})
            resp.status_code = 409
            abort(resp)
        # Again, now the test and the planning are done: both read over the
        # network and can take seconds, and a sync started meanwhile would
        # list the old provider (see _run_playlist_sync) mid-switch. Under
        # the lock, so the scheduled sync can't queue one between this
        # check and the switch's own first sync.
        with _PROVIDER_SWITCH_LOCK:
            if playlist_sync.sync_status()["running"]:
                abort(409, description=_(
                    "A playlist sync is running. Switch when it has finished."))
            report = playlist_sync.switch_preview(conn, _active_provider_id(conn), carry)
            _connect_switch_target(target, values)
            _switch_provider_rows(conn, target, carry["pairs"])
            sync = playlist_sync.start_sync(_PROVIDERS[target], target)
    return jsonify({**report, "provider": target, "sync": sync})


@bp.route("/api/admin/health")
def api_admin_health():
    """Admin-only library-health worklist: things silently falling
    through — playlist entries that matched no local track, tracks left with the
    scanner's Unknown-Artist/Album fallback, probable duplicate files, tracks
    the fingerprint pass gave up on, and tracks AcoustID/MusicBrainz couldn't
    identify (#364). Each category reports a full count plus a capped sample
    list to act on. Also carries two non-category signals (#365): a DATA_DIR
    network-filesystem alert and the last completed scan's timestamp."""
    with db.connection() as conn:
        require_admin(conn)

        unmatched_count = conn.execute(
            "SELECT COUNT(*) AS n FROM playlist_tracks WHERE matched_track_id IS NULL"
        ).fetchone()["n"]
        unmatched = [dict(r) for r in conn.execute(
            "SELECT p.title AS playlist, pt.artist, pt.title AS title, pt.album "
            "FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id "
            "WHERE pt.matched_track_id IS NULL ORDER BY p.title, pt.position LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        )]

        unknown_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE deleted_at IS NULL "
            "AND (artist = 'Unknown Artist' OR album = 'Unknown Album')"
        ).fetchone()["n"]
        unknown = [dict(r) for r in conn.execute(
            "SELECT artist, album, title, relative_path FROM tracks "
            "WHERE deleted_at IS NULL AND (artist = 'Unknown Artist' OR album = 'Unknown Album') "
            "ORDER BY relative_path LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        )]

        # Probable duplicates: the same (artist, album, title, disc, track) on
        # more than one path — e.g. two rips or a FLAC+MP3 of the same track.
        # Case-insensitive grouping; paths joined so the admin can see which
        # files collide.
        dup_rows = conn.execute(
            "SELECT artist, album, title, track_no, COUNT(*) AS n, "
            "GROUP_CONCAT(relative_path, '\n') AS paths FROM tracks "
            "WHERE deleted_at IS NULL "
            "GROUP BY LOWER(artist), LOWER(album), LOWER(title), disc_no, track_no "
            "HAVING n > 1 ORDER BY n DESC, artist, album, title LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        ).fetchall()
        dup_total = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM tracks WHERE deleted_at IS NULL "
            "GROUP BY LOWER(artist), LOWER(album), LOWER(title), disc_no, track_no HAVING COUNT(*) > 1)"
        ).fetchone()["n"]
        duplicates = [
            {"artist": r["artist"], "album": r["album"], "title": r["title"],
             "track_no": r["track_no"], "count": r["n"], "paths": (r["paths"] or "").split("\n")}
            for r in dup_rows
        ]

        # #364 population A: the fingerprint pass gave up on this file
        # (undecodable, vanished, a permissions problem) and _library_track_
        # rows keeps re-selecting it every pass — genuinely actionable, so it
        # renders like every other problem category (chart included).
        fingerprint_failed_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE deleted_at IS NULL "
            "AND fingerprint IS NULL AND fingerprint_failed_at IS NOT NULL"
        ).fetchone()["n"]
        fingerprint_failed = [dict(r) for r in conn.execute(
            "SELECT artist, album, title, relative_path FROM tracks "
            "WHERE deleted_at IS NULL AND fingerprint IS NULL AND fingerprint_failed_at IS NOT NULL "
            "ORDER BY relative_path LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        )]

        # #364 population B: fingerprinted and looked up fine, but AcoustID/
        # MusicBrainz had no match — stamped once by fingerprint.py and never
        # retried. Mostly NOT actionable (live recordings, bootlegs,
        # self-released and local music, much classical genuinely aren't in
        # either database), so this is informational rather than a problem:
        # excluded from healthStatsChart even though it's listed here like
        # every other category.
        #
        # #408: acoustid_isrc, not isrc — tracks.isrc is scanner-populated
        # from the file's own embedded tags (identity.py's tier 1), a
        # completely independent column from fingerprint.py's AcoustID/
        # MusicBrainz backfill (tier 2). Embedded ISRC tags are rare
        # regardless of AcoustID outcome, so checking isrc here counted
        # nearly every successfully-identified track as "not found."
        unidentified_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE deleted_at IS NULL "
            "AND fingerprint IS NOT NULL AND fingerprint_checked_at IS NOT NULL "
            "AND acoustid_isrc IS NULL"
        ).fetchone()["n"]
        unidentified = [dict(r) for r in conn.execute(
            "SELECT artist, album, title, relative_path FROM tracks "
            "WHERE deleted_at IS NULL AND fingerprint IS NOT NULL "
            "AND fingerprint_checked_at IS NOT NULL AND acoustid_isrc IS NULL "
            "ORDER BY relative_path LIMIT ?",
            (_HEALTH_ITEM_LIMIT,),
        )]

        # #365: last completed library_scan, so the panel can answer "why
        # isn't my new album showing up" before it becomes a support question.
        # None if a scan has never finished on this instance.
        last_scan_row = conn.execute(
            "SELECT finished_at, result FROM jobs WHERE type = ? AND state = 'done' "
            "ORDER BY finished_at DESC LIMIT 1",
            (scanner.JOB_TYPE,),
        ).fetchone()

        return jsonify({
            "unmatched_playlist_tracks": {"count": unmatched_count, "items": unmatched},
            "unknown_tags": {"count": unknown_count, "items": unknown},
            "duplicates": {"count": dup_total, "items": duplicates},
            "fingerprint_failed": {"count": fingerprint_failed_count, "items": fingerprint_failed},
            "unidentified_fingerprints": {"count": unidentified_count, "items": unidentified},
            "item_limit": _HEALTH_ITEM_LIMIT,
            # #365: a genuine alert, not a count — rendered distinctly from
            # the categories above. The fs type string, not the pre-formatted
            # console warning (_network_data_dir_warning): that text is
            # untranslated and console-styled ("[main] WARNING: ..."), so the
            # UI composes its own translated message from the type alone.
            # None when DATA_DIR is on local disk.
            "data_dir_network_fs": db.data_dir_network_fs(),
            "last_scan_finished_at": last_scan_row["finished_at"] if last_scan_row else None,
            # Files that scan read whose artist, album or title came from the
            # folder or file name: tags worth fixing. Over the files it read
            # (new or changed ones), not the whole library. None before any
            # scan, or for one from before the count.
            "last_scan_from_folder": _last_scan_from_folder(last_scan_row),
            # #389: same "genuine alert, not a count" treatment as
            # data_dir_network_fs above — a distinct-peer count (see
            # _exposure_warning), not a boolean, so the UI can name the
            # number in its own translated message. None whenever there's
            # nothing to say (pinned trusted_proxy, forward mode, or too
            # few distinct peers seen yet).
            "exposure_warning": _exposure_warning(),
            # #389 (review feedback): always-shown counterpart so "quiet
            # because nothing's wrong" and "quiet because the mechanism
            # can't see anything on this deployment's network" aren't the
            # same rendering. None only when the mechanism isn't active at
            # all (pinned trusted_proxy, forward mode).
            "exposure_peer_count": _exposure_status(),
        })


@bp.route("/api/admin/mirrors")
def api_admin_mirrors():
    """#285/#189: admin-only read-only overview of every playlist with ANY
    sink enabled — title, per-sink status (filename or remote id,
    last-written timestamp, last error), and coverage (matched/total,
    the same numbers each sink's own write_mirror() writes into its
    comment/marker line). Small and unpaginated, unlike Health's capped
    worklists — this lists a status, not a problem set expected to grow
    large."""
    with db.connection() as conn:
        require_admin(conn)
        lidarr_configured = db.get_lidarr_config() is not None
        rows = conn.execute(
            "SELECT p.id, p.title, "
            "p.mirror_enabled, p.mirror_filename, p.mirror_last_written_at, "
            "p.mirror_last_error, p.mirror_last_error_code, "
            "p.subsonic_mirror_enabled, p.subsonic_mirror_remote_id, "
            "p.subsonic_mirror_last_written_at, p.subsonic_mirror_last_error, "
            "p.subsonic_mirror_last_error_code, "
            "p.jellyfin_mirror_enabled, p.jellyfin_mirror_remote_id, "
            "p.jellyfin_mirror_last_written_at, p.jellyfin_mirror_last_error, "
            "p.jellyfin_mirror_last_error_code, "
            "p.emby_mirror_enabled, p.emby_mirror_remote_id, "
            "p.emby_mirror_last_written_at, p.emby_mirror_last_error, "
            "p.emby_mirror_last_error_code, "
            "p.music_assistant_mirror_enabled, p.music_assistant_mirror_remote_id, "
            "p.music_assistant_mirror_last_written_at, p.music_assistant_mirror_last_error, "
            "p.music_assistant_mirror_last_error_code, "
            "p.plex_mirror_enabled, p.plex_mirror_remote_id, p.plex_mirror_last_written_at, "
            "p.plex_mirror_last_error, p.plex_mirror_last_error_code, "
            # #494: not a mirror sink, but shown here for the same
            # full-admin-panel-visibility reason as the four above —
            # there's no single remote id (a run can request several
            # albums), so last_run_at/last_count stand in for
            # last_written_at/remote_id below.
            "p.lidarr_request_enabled, p.lidarr_request_last_run_at, "
            "p.lidarr_request_last_count, p.lidarr_request_last_error, "
            "p.lidarr_request_last_error_code, "
            "COUNT(pt.id) AS total, "
            "SUM(CASE WHEN pt.matched_track_id IS NOT NULL THEN 1 ELSE 0 END) AS matched "
            "FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
            "WHERE p.mirror_enabled = 1 OR p.subsonic_mirror_enabled = 1 "
            "OR p.jellyfin_mirror_enabled = 1 OR p.emby_mirror_enabled = 1 "
            "OR p.music_assistant_mirror_enabled = 1 OR p.plex_mirror_enabled = 1 "
            # A stale flag lists nothing while Lidarr requests are not
            # configured: the flag does nothing then.
            "OR (p.lidarr_request_enabled = 1 AND :lidarr_configured) "
            "GROUP BY p.id ORDER BY p.title",
            {"lidarr_configured": lidarr_configured},
        ).fetchall()
        out = []
        coverage = mirror_runs.latest(conn, [r["id"] for r in rows])
        for r in rows:
            d = dict(r)
            # The last write's counts per server sink, with the kept runs'
            # on-target counts; the tracks themselves come from
            # api_admin_mirror_missing.
            d["coverage"] = {sink: coverage[(r["id"], sink)] for sink in mirror_runs.SINKS
                             if (r["id"], sink) in coverage}
            d["mirror_enabled"] = bool(d["mirror_enabled"])
            d["subsonic_mirror_enabled"] = bool(d["subsonic_mirror_enabled"])
            d["jellyfin_mirror_enabled"] = bool(d["jellyfin_mirror_enabled"])
            d["emby_mirror_enabled"] = bool(d["emby_mirror_enabled"])
            d["music_assistant_mirror_enabled"] = bool(d["music_assistant_mirror_enabled"])
            d["plex_mirror_enabled"] = bool(d["plex_mirror_enabled"])
            d["lidarr_request_enabled"] = bool(d["lidarr_request_enabled"])
            out.append(d)
        subsonic_config = db.get_mirror_subsonic_config()
        jellyfin_config = db.get_mirror_jellyfin_config()
        emby_config = db.get_mirror_emby_config()
        plex_config = db.get_mirror_plex_config()
        lidarr_connection = db.get_lidarr_connection()
        return jsonify({
            "mirror_folder": str(db.get_mirror_folder() or "") or None,
            "subsonic_mirror_url": subsonic_config[0] if subsonic_config else None,
            "jellyfin_mirror_url": jellyfin_config[0] if jellyfin_config else None,
            "emby_mirror_url": emby_config[0] if emby_config else None,
            "plex_mirror_url": plex_config[0] if plex_config else None,
            "lidarr_url": lidarr_connection[0] if lidarr_connection else None,
            "lidarr_request_configured": lidarr_configured,
            "targets": _mirror_targets(out, lidarr_configured),
            "playlists": out,
        })


@bp.route("/api/admin/mirrors/<int:playlist_id>/<sink>/missing")
def api_admin_mirror_missing(playlist_id: int, sink: str):
    """The tracks the last write to one sink left off its target, each with
    its reason (mirror_runs). Admin only, like the overview it opens from.
    404 for an unknown sink or a pair with no recorded write."""
    with db.connection() as conn:
        require_admin(conn)
        if sink not in mirror_runs.SINKS:
            abort(404)
        run = mirror_runs.missing(conn, playlist_id, sink)
        if run is None:
            abort(404)
        return jsonify({**run, "list_max": mirror_runs.LIST_MAX})


@bp.route("/api/admin/jobs")
def api_admin_jobs():
    """#297 step 2: what background work is running, queued, or failed.

    This is the user-visible payoff of the job queue, and the reason it was
    worth building rather than being a pure refactor: before it, a failed
    library scan or fingerprint backfill left nothing but a log line the
    admin would never read. For self-hosted software the admin IS the user,
    so this is a feature surface, not internal diagnostics."""
    with db.connection() as conn:
        require_admin(conn)
        return jsonify({
            "counts": jobs.status(conn),
            "jobs": jobs.recent(conn, limit=_JOBS_PAGE_LIMIT),
        })


@bp.route("/api/admin/jobs/<int:job_id>/retry", methods=["POST"])
def api_admin_job_retry(job_id: int):
    """#297: put a failed job back in the queue, resetting its attempt
    budget so a fix (a corrected config, a provider that's back up) gets a
    real chance rather than one leftover attempt.

    Only `failed` jobs — retrying a queued one is a no-op, and 'retrying' a
    running one would produce a second concurrent copy of work already in
    flight, which is exactly what the dedupe index exists to prevent."""
    with db.connection() as conn:
        require_admin(conn)
        row = conn.execute(
            "SELECT state, dedupe_key FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            abort(404, description=_("No such job."))
        if row["state"] != "failed":
            abort(400, description=_("Only a failed job can be retried."))
        # The dedupe index covers state IN ('queued','running'), so flipping
        # this row back to 'queued' would violate it if another job with the
        # same key is already pending — an unhandled IntegrityError, i.e. a 500.
        #
        # Reachable on the only job type that exists today, and not as an edge
        # case: the backfill exhausts its retries, the NEXT library scan queues
        # a fresh one (by design), and the failed row is still sitting in the
        # panel inviting a click.
        #
        # The collision means the retry is unnecessary — the work is already
        # queued — so this is reassurance, not an error to repair. Checked
        # explicitly rather than catching IntegrityError, purely because it
        # affords a message that says what's actually true.
        if row["dedupe_key"] is not None:
            clash = conn.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key = ? AND state IN ('queued', 'running')",
                (row["dedupe_key"],),
            ).fetchone()
            if clash is not None:
                abort(409, description=_("This job is already queued to run again."))
        conn.execute(
            "UPDATE jobs SET state = 'queued', attempts = 0, run_after = NULL, "
            "started_at = NULL, finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        jobs.wake()
        return jsonify({"status": "ok"})


@bp.route("/api/admin/jobs/<int:job_id>", methods=["DELETE"])
def api_admin_job_cancel(job_id: int):
    """Drop a job that hasn't started yet, or stop a running one.

    A job that isn't running is deleted outright. A running one can't be
    interrupted from here — the worker is inside a handler doing audio decode
    or provider HTTP — so it is asked to stop: its next progress report
    raises, between two of the handler's own commits, and the job ends
    `cancelled` (see jobs.request_cancel). That can take as long as one item
    of its work, so the answer is 202 with "cancelling", and the panel says so
    until the job has stopped.

    The DELETE skips a running row, so a job the worker claims between the
    read and the delete is asked to stop instead of losing its row mid-run."""
    with db.connection() as conn:
        require_admin(conn)
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            abort(404, description=_("No such job."))
        deleted = conn.execute(
            "DELETE FROM jobs WHERE id = ? AND state != 'running'", (job_id,)).rowcount
        conn.commit()
        if deleted:
            return jsonify({"status": "ok"})
        jobs.request_cancel(job_id)
        return jsonify({"status": "cancelling"}), 202


@bp.route("/api/admin/users", methods=["GET", "POST"])
def api_admin_users():
    """Admin-only: full visibility over every account (Authentik-provisioned
    or local), and the ability to provision new local-only accounts (needed
    in AUTH_MODE=local, where there's no SSO auto-provisioning new users the
    way Authentik headers do)."""
    with db.connection() as conn:
        require_admin(conn)
        if request.method == "POST":
            # #235: this always creates a non-admin row (no is_admin flag
            # below) — exactly the "non-admin local password under oidc"
            # the break-glass model forbids, and it's a local-mode-only tool
            # to begin with (oidc auto-provisions accounts from IdP headers,
            # it doesn't need this).
            if settings.AUTH_MODE == "oidc":
                abort(400, description=_(
                    "New local accounts can't be created while AUTH_MODE=oidc — accounts are "
                    "provisioned automatically from the identity provider."))
            body = request.get_json(force=True)
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            if not username or len(password) < 8:
                abort(400, description=_("Username required, password at least 8 characters"))
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password)),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                abort(400, description=_("This username already exists"))
        # #332: owned counts come with the list, so the delete dialog can say
        # WHICH ownership blocks a deletion before anything is attempted. The old
        # flow was a native confirm() with a fixed string, then a 400 naming three
        # categories without saying which one, how many, or which items — a dead
        # end the admin had to go hunting from.
        #
        # Correlated subqueries rather than joins: three independent one-to-many
        # counts would otherwise multiply each other, and a user list is small.
        rows = conn.execute(
            "SELECT id, username, email, is_admin, "
            "(password_hash IS NOT NULL) AS has_local_password, "
            "(SELECT COUNT(*) FROM devices WHERE owner_user_id = users.id) AS owned_devices, "
            "(SELECT COUNT(*) FROM selections WHERE created_by_user_id = users.id) "
            "  AS owned_selections, "
            "(SELECT COUNT(*) FROM playlists WHERE owner_user_id = users.id) AS owned_playlists "
            "FROM users ORDER BY username"
        ).fetchall()
        return jsonify([dict(r) for r in rows])


@bp.route("/api/admin/users/<int:target_user_id>", methods=["DELETE"])
def api_admin_user_delete(target_user_id: int):
    with db.connection() as conn:
        admin_id = get_current_user_id(conn)
        require_admin(conn)
        if target_user_id == admin_id:
            abort(400, description=_("Cannot delete your own account"))
        row = conn.execute(
            "SELECT is_admin, (password_hash IS NOT NULL) AS has_local_password "
            "FROM users WHERE id = ?", (target_user_id,)
        ).fetchone()
        if row is None:
            abort(404, description=_("User not found"))
        # #237: two lockout guardrails, same reasoning as "don't delete your
        # own account" above — both are about never reaching a state with
        # no working login left, not about protecting any particular user.
        if row["is_admin"]:
            admin_count = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1").fetchone()["n"]
            if admin_count <= 1:
                abort(400, description=_("Cannot delete the last admin account"))
        if settings.AUTH_MODE == "oidc" and row["has_local_password"]:
            # Under OIDC, a local password is exceptional — everyone else
            # authenticates via the IdP. Deleting the sole remaining one
            # removes the only login that survives an IdP outage, which
            # local-only mode doesn't have the same stakes for (there's no
            # IdP to go down), so this check is OIDC-specific.
            local_pw_count = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE password_hash IS NOT NULL").fetchone()["n"]
            if local_pw_count <= 1:
                abort(400, description=_(
                    "This is the only account with a local password — deleting it would lock "
                    "everyone out if the identity provider ever becomes unreachable. Give another "
                    "admin a local password first if you still want to remove this one."))
        try:
            conn.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
            conn.commit()
        except sqlite3.IntegrityError:
            # devices.owner_user_id / selections.created_by_user_id are
            # NOT NULL foreign keys with no cascade or reassignment, and
            # #68 added playlists.owner_user_id (also a restricted FK) — so
            # this account still owns a device, a selection, or a synced
            # playlist. Delegation lets other users *manage* their devices,
            # it doesn't transfer ownership, so it doesn't help here; a
            # playlist's ownership only transfers by re-syncing it under a
            # different mapping (#70). Fail cleanly rather than a raw 500.
            # #332: "or reassign" implied a route that mostly does not exist —
            # delegation lets others MANAGE a device without transferring
            # ownership, and a playlist's owner only changes by re-syncing under a
            # different mapping (#70). The dialog now names the actual counts
            # before anything is attempted; this stays as the server-side backstop
            # for a stale list or a direct API call.
            abort(400, description=_(
                "This account still owns devices, selections or playlists. Delete those "
                "first — ownership can't be transferred from here."))
        return jsonify({"status": "ok"})


@bp.route("/api/admin/users/<int:target_user_id>/password", methods=["PUT"])
def api_admin_user_reset_password(target_user_id: int):
    """Admin-only password reset (#237) — the admin-recovery counterpart to
    a user's own change-password self-service. Deliberately allows
    resetting another admin's (or the break-glass account's own) password:
    refusing that would mean a forgotten admin password is an unrecoverable
    lockout with no in-app path out, which is worse than trusting an admin
    (who could already delete the account, or any other, outright) with
    this too. #235: under oidc, resetting a non-admin's password is refused
    instead — an admin minting one would otherwise recreate exactly the
    non-admin break-glass bypass that mode is meant to rule out."""
    with db.connection() as conn:
        require_admin(conn)
        row = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if row is None:
            abort(404, description=_("User not found"))
        if settings.AUTH_MODE == "oidc" and not row["is_admin"]:
            abort(400, description=_(
                "Local passwords are admin-only while AUTH_MODE=oidc — this account is managed by "
                "the identity provider."))
        body = request.get_json(force=True)
        password = body.get("password") or ""
        if len(password) < 8:
            abort(400, description=_("Password must be at least 8 characters"))
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), target_user_id),
        )
        if cur.rowcount == 0:
            abort(404, description=_("User not found"))
        # The user's existing sessions end: a reset is often because one of
        # them shouldn't exist.
        _end_other_sessions(conn, target_user_id)
        conn.commit()
        return jsonify({"status": "ok"})


@bp.route("/api/admin/roon-profiles")
def api_admin_roon_profiles():
    """Admin-only: live Roon profiles + every Trobar user, with a fuzzy-
    matched suggestion per unmapped user (same difflib cutoff as
    matching.py's playlist-track matching, not reinvented here) — for
    Administration > Configuration > Roon's mapping UI. Not gated on the
    active provider actually being Roon: harmless either way (just
    reflects whatever roon_client.list_profiles() can currently reach),
    the frontend only shows this section when Roon is configured."""
    with db.connection() as conn:
        require_admin(conn)
        result = roon_client.list_profiles()
        users_rows = conn.execute(
            "SELECT id, username, roon_profile FROM users ORDER BY username"
        ).fetchall()

    if result["status"] != "ok":
        return jsonify(result)

    profile_names = [p["title"] for p in result["profiles"]]
    users_out = []
    for row in users_rows:
        suggestion = None
        if not row["roon_profile"] and profile_names:
            close = difflib.get_close_matches(row["username"], profile_names, n=1, cutoff=0.85)
            suggestion = close[0] if close else None
        users_out.append({
            "id": row["id"], "username": row["username"],
            "roon_profile": row["roon_profile"], "suggested": suggestion,
        })
    return jsonify({"status": "ok", "profiles": result["profiles"], "users": users_out})


@bp.route("/api/admin/users/<int:target_user_id>/roon-profile", methods=["PUT"])
def api_admin_user_roon_profile(target_user_id: int):
    with db.connection() as conn:
        require_admin(conn)
        body = request.get_json(force=True)
        profile = (body.get("roon_profile") or "").strip() or None
        conn.execute("UPDATE users SET roon_profile = ? WHERE id = ?", (profile, target_user_id))
        conn.commit()
        return jsonify({"status": "ok"})


@bp.route("/api/admin/jellyfin-users")
def api_admin_jellyfin_users():
    """#262: live Jellyfin server users + every Trobar user, with a
    fuzzy-matched suggestion per unmapped user — for Administration >
    Configuration > Jellyfin's mapping UI. Not gated on the active
    provider actually being Jellyfin: harmless either way, the frontend
    only shows this section when Jellyfin is configured. See
    _admin_provider_user_mapping()'s own docstring for the shape."""
    with db.connection() as conn:
        require_admin(conn)
        return jsonify(_admin_provider_user_mapping(conn, jellyfin_client, "jellyfin_user_id"))


@bp.route("/api/admin/users/<int:target_user_id>/jellyfin-user", methods=["PUT"])
def api_admin_user_jellyfin_user(target_user_id: int):
    with db.connection() as conn:
        require_admin(conn)
        body = request.get_json(force=True)
        mapped_id = (body.get("jellyfin_user_id") or "").strip() or None
        conn.execute("UPDATE users SET jellyfin_user_id = ? WHERE id = ?", (mapped_id, target_user_id))
        conn.commit()
        return jsonify({"status": "ok"})


@bp.route("/api/admin/emby-users")
def api_admin_emby_users():
    """#262: live Emby server users + every Trobar user, with a
    fuzzy-matched suggestion per unmapped user — for Administration >
    Configuration > Emby's mapping UI. Not gated on the active provider
    actually being Emby: harmless either way, the frontend only shows
    this section when Emby is configured. See
    _admin_provider_user_mapping()'s own docstring for the shape."""
    with db.connection() as conn:
        require_admin(conn)
        return jsonify(_admin_provider_user_mapping(conn, emby_client, "emby_user_id"))


@bp.route("/api/admin/users/<int:target_user_id>/emby-user", methods=["PUT"])
def api_admin_user_emby_user(target_user_id: int):
    with db.connection() as conn:
        require_admin(conn)
        body = request.get_json(force=True)
        mapped_id = (body.get("emby_user_id") or "").strip() or None
        conn.execute("UPDATE users SET emby_user_id = ? WHERE id = ?", (mapped_id, target_user_id))
        conn.commit()
        return jsonify({"status": "ok"})


@bp.route("/api/admin/lidarr-options")
def api_admin_lidarr_options():
    """#494: live root-folder/quality-profile/metadata-profile lists from
    the already-saved Lidarr connection (lidarr_url/lidarr_api_key must be
    persisted first via PUT /api/admin/config — see that route's own
    two-phase docstring; nothing here can succeed before that). Same
    "admin-only, live-fetch-from-external-server-into-a-dropdown" shape as
    GET /api/admin/jellyfin-users, generalized to three lists instead of
    one Trobar-user mapping."""
    with db.connection() as conn:
        require_admin(conn)
        root_folders = lidarr_client.list_root_folders()
        if root_folders["status"] != "ok":
            return jsonify(root_folders)
        quality_profiles = lidarr_client.list_quality_profiles()
        if quality_profiles["status"] != "ok":
            return jsonify(quality_profiles)
        metadata_profiles = lidarr_client.list_metadata_profiles()
        if metadata_profiles["status"] != "ok":
            return jsonify(metadata_profiles)
        return jsonify({
            "status": "ok",
            "root_folders": root_folders["root_folders"],
            "quality_profiles": quality_profiles["quality_profiles"],
            "metadata_profiles": metadata_profiles["metadata_profiles"],
        })


@bp.route("/api/admin/delegations", methods=["GET", "POST"])
def api_admin_delegations():
    """Admin-only: grant/list delegation of full device-management rights
    between two non-admin users (e.g. mum -> kid1's devices).
    Delegation covers every device the target user owns, current and future;
    the grantee only actually *sees* one in their own Appareils list once
    they pin it (GET /api/devices/delegatable + POST .../pin)."""
    with db.connection() as conn:
        require_admin(conn)
        if request.method == "POST":
            body = request.get_json(force=True)
            grantee_id = body.get("grantee_user_id") if isinstance(body, dict) else None
            target_id = body.get("target_user_id") if isinstance(body, dict) else None
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (grantee_id, target_id)):
                abort(400, description=_("Both users are required"))
            if grantee_id == target_id:
                abort(400, description=_("A user cannot delegate to themselves"))
            for uid in (grantee_id, target_id):
                if conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None:
                    abort(400, description=_("User not found"))
            try:
                conn.execute(
                    "INSERT INTO device_delegations (grantee_user_id, target_user_id) VALUES (?, ?)",
                    (grantee_id, target_id),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                abort(400, description=_("This delegation already exists"))
        rows = conn.execute(
            "SELECT dd.id, dd.grantee_user_id, gu.username AS grantee_username, "
            "dd.target_user_id, tu.username AS target_username, dd.granted_at "
            "FROM device_delegations dd "
            "JOIN users gu ON gu.id = dd.grantee_user_id "
            "JOIN users tu ON tu.id = dd.target_user_id "
            "ORDER BY tu.username, gu.username"
        ).fetchall()
        return jsonify([dict(r) for r in rows])


@bp.route("/api/admin/delegations/<int:delegation_id>", methods=["DELETE"])
def api_admin_delegation_delete(delegation_id: int):
    with db.connection() as conn:
        require_admin(conn)
        row = conn.execute(
            "SELECT grantee_user_id, target_user_id FROM device_delegations WHERE id = ?",
            (delegation_id,),
        ).fetchone()
        if row is not None:
            conn.execute("DELETE FROM device_delegations WHERE id = ?", (delegation_id,))
            # Revoking removes management rights immediately (checks derive
            # live from this table) — also drop any pins the (former)
            # grantee held on the target's devices, so a now-inaccessible
            # device doesn't linger, unreachable, in their Appareils list.
            conn.execute(
                "DELETE FROM device_pins WHERE user_id = ? AND device_id IN "
                "(SELECT id FROM devices WHERE owner_user_id = ?)",
                (row["grantee_user_id"], row["target_user_id"]),
            )
            conn.commit()
        return jsonify({"status": "ok"})
