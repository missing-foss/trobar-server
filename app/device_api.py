#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The device-facing sync API, /api/device/* (#253): every route here
authenticates the calling device's Bearer token itself, and acts on that
device only. In forward-auth mode this path must be exempted from the
ForwardAuth gate at the proxy (native clients can't follow an HTML login
redirect).
"""

import logging
import os

from flask import jsonify, request, send_file, abort, Response, Blueprint, current_app
from flask_babel import gettext as _

import artist_images
import autofit
import cover_links
import covers
import device_store
import db
import device_plays
import jobs
import play_forward
import playlist_sync
import provenance
import sync_state
import transcode
from web_common import (
    APP_API_VERSION,
    _DEVICE_PLAYLIST_ENTRIES_MAX,
    _active_provider,
    _authenticated_device,
    _config_int,
    _contained,
    _refresh_autofit_after_limit_change,
    _validated_artist_images,
    _validated_max_size_bytes,
    _validated_source_of_truth,
)

bp = Blueprint("device_api", __name__)
_log = logging.getLogger(__name__)


# #239: /api/device/fingerprints page sizes. A chromaprint fingerprint is
# ~2.2 KB of text (measured: ~18.5 chars per second of audio, capped by
# pyacoustid at 120 seconds' worth), so 200 entries is roughly a 450 KB page
# before gzip — the reason this is paginated at all, and the reason the cap
# exists rather than trusting a client-supplied limit.
_FINGERPRINT_PAGE_DEFAULT = 200


_FINGERPRINT_PAGE_MAX = 500


# #239 PR 2: entries accepted per provenance push. Same reasoning as the page
# sizes above, in the other direction — a fingerprint is ~2.2 KB, so an
# uncapped push from a 5,000-track device would be a ~12 MB request body.
# Clients push in pages; the response's `pending` tells them there's more.
_PROVENANCE_PUSH_MAX = 500


# Bounds on one upload of device-made playlists: far above any real card,
# low enough that a broken client can't hand the server unbounded work.
_DEVICE_PLAYLISTS_MAX = 1000


def _valid_device_playlist_path(path) -> bool:
    """A playlist file's path from the device's sync root: relative, no
    "..", not inside Trobar's own .trobar/ folder, and a playlist file."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    return (".." not in parts and "" not in parts and parts[0] != ".trobar"
            and path.lower().endswith((".m3u", ".m3u8")))


def _validated_missing_track_ids(value) -> list[int]:
    if not isinstance(value, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in value):
        abort(400, description=_("redownload and exclude must be lists of track ids."))
    return value


@bp.route("/api/device/info")
def api_device_info():
    """Lets the client show its own server-side identity (name as set in
    the web UI) — the app only stores server_url+token from pairing, never
    its own name."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        return jsonify({"name": device["name"], "device_type": device["device_type"],
                         # A picture only, alongside the type: null = the type's own.
                         "icon": device["icon"],
                         "max_size_bytes": device["max_size_bytes"],
                         "transcode_format": device["transcode_format"],
                         "artist_images": device["artist_images"],
                         # #63: so the client can show/reflect the current
                         # source_of_truth (single field, no client-local drift).
                         "source_of_truth": device["source_of_truth"],
                         # Which App API this server speaks (see APP_API_VERSION).
                         # Absent on older servers, which a client treats as 0.
                         "app_api": APP_API_VERSION})


@bp.route("/api/device/storage", methods=["POST"])
def api_device_storage():
    """Client reports free + total space of its actual storage volume
    (StatFs-derived on Android, will be shutil.disk_usage() of the mounted
    volume for the future desktop/SD-card CLI client) — surfaced in the web
    UI's Profil > Appareils so the limit set there can be sanity-checked
    against what's physically available, not just trusted blindly, and so
    the device's *overall* storage consumption (not just Trobar's own
    share of it) is visible too.

    `foreign_bytes` is optional and different in kind from those two: not
    the volume, but the bytes of music sitting in the SYNC FOLDER that
    Trobar did not put there. "Max music size" is what the user allows in
    that folder, so it is the one number that lets the budget mean what the
    setting says.

    ABSENT IS NOT ZERO, and the distinction is the whole reason this is a
    separate optional field rather than a third positional value. A client
    too old to measure it sends nothing, and overwriting a stored figure
    with NULL — or storing 0 — would state a measurement nobody made. So
    the column is only written when the key is actually present, and its
    own timestamp records when that last happened. A client that stops
    reporting leaves a stale number that says how stale it is, which is
    recoverable; one that silently reports zero is not."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        conn.execute(
            "UPDATE devices SET reported_free_bytes = ?, reported_total_bytes = ?, "
            "free_bytes_reported_at = datetime('now') WHERE id = ?",
            (body.get("free_bytes"), body.get("total_bytes"), device["id"]),
        )
        if "foreign_bytes" in body:
            raw = body.get("foreign_bytes")
            # A negative or non-numeric value is a client bug, and storing
            # it would quietly shrink or inflate the budget for as long as
            # nobody looked. Ignored rather than 400'd: this whole endpoint
            # is best-effort on the client side (SyncWorker swallows its
            # failures), so rejecting the report would lose the free/total
            # figures too, over a field that is allowed to be missing.
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                conn.execute(
                    "UPDATE devices SET reported_foreign_bytes = ?, "
                    "foreign_bytes_reported_at = datetime('now') WHERE id = ?",
                    (raw, device["id"]),
                )
            else:
                current_app.logger.warning(
                    "[storage] device %s reported an unusable foreign_bytes: %r",
                    device["id"], raw)
        # The space the device keeps free on its volume: its space check
        # refuses a sync that would eat into it, and the Sync to… picker
        # measures room against the same number. Sent with free/total, so
        # their timestamp dates it; absent (an older client) leaves it as
        # it was, and a bad value is ignored for the same reason as above.
        if "reserve_bytes" in body:
            raw = body.get("reserve_bytes")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                conn.execute("UPDATE devices SET reported_reserve_bytes = ? WHERE id = ?", (raw, device["id"]))
            else:
                current_app.logger.warning(
                    "[storage] device %s reported an unusable reserve_bytes: %r",
                    device["id"], raw)
        conn.commit()
        return jsonify({"status": "ok"})


@bp.route("/api/device/sync-completed", methods=["POST"])
def api_device_sync_completed():
    """The client says one sync finished with no error. It only adds one to
    the server-wide count (db.record_device_sync), which is what the web
    UI's thank-you card for admins counts; nothing about the device is
    stored. Clients call it best-effort and ignore a 404 from an older
    server."""
    with db.connection() as conn:
        _authenticated_device(conn)
        db.record_device_sync(conn)
        conn.commit()
        return jsonify({"status": "ok"})


@bp.route("/api/device/limit", methods=["PATCH"])
def api_device_limit():
    """Lets the device itself set its own allocation, mirroring what
    PATCH /api/devices/<id> already lets the web UI do — added because the
    Android app's Settings screen authenticates by device Bearer token, not
    an Authentik session, so it can't call the web-facing devices route."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        new_limit = _validated_max_size_bytes(body.get("max_size_bytes"))
        conn.execute(
            "UPDATE devices SET max_size_bytes = ? WHERE id = ?", (new_limit, device["id"]))
        conn.commit()
        if new_limit != (device["max_size_bytes"] or None):
            _refresh_autofit_after_limit_change(conn, device["id"])
        return jsonify({"status": "ok"})


@bp.route("/api/device/source-of-truth", methods=["PATCH"])
def api_device_source_of_truth():
    """#63: lets the device set its own source_of_truth, mirroring what
    PATCH /api/devices/<id> lets the web UI do (device Bearer token, not an
    Authentik session). 'device' stops the server pruning this device's tracks
    (so it survives a server-DB loss); 'server' is the default conform-to-server
    behavior. Flipping to 'server' re-prunes on the recompute below."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        sot = _validated_source_of_truth(body.get("source_of_truth"))
        conn.execute(
            "UPDATE devices SET source_of_truth = ? WHERE id = ?", (sot, device["id"]))
        conn.commit()
        sync_state.recompute_device_state(conn, device["id"])
        return jsonify({"status": "ok"})


@bp.route("/api/device/artist-images", methods=["PATCH"])
def api_device_artist_images():
    """Lets the device set its own artist_images (null = off, "small",
    "full"), the same field and the same check as the web device routes.
    Only the calling device's row: the token decides which, never the body.
    Nothing to recompute: the next sync writes or leaves out artist.jpg by
    what it reads here."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        if not isinstance(body, dict) or "artist_images" not in body:
            # Absent is not "off": a body without the field changes nothing.
            abort(400, description=_("Unsupported artist image setting."))
        imgs = _validated_artist_images(body.get("artist_images"))
        conn.execute("UPDATE devices SET artist_images = ? WHERE id = ?", (imgs, device["id"]))
        conn.commit()
        return jsonify({"status": "ok", "artist_images": imgs})


@bp.route("/api/device/manifest", methods=["POST"])
def api_device_manifest():
    """#63 recovery: a re-paired device POSTs {"paths": [...]} — the relative
    paths it already holds — and the server marks those tracks 'downloaded' so
    they aren't re-fetched after a server-DB loss (pair with
    source_of_truth='device', set before the first sync). Device Bearer token.
    Returns {"matched": n, "unmatched": n}."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        paths = body.get("paths")
        if not isinstance(paths, list):
            abort(400, description=_("paths must be a list of relative paths."))
        return jsonify(sync_state.record_device_manifest(conn, device["id"], paths))


@bp.route("/api/device/plays", methods=["POST"])
def api_device_plays():
    """Plays on this device: {"plays": [{"track_id": n, "played_at": unix
    seconds}]}, at most device_plays.PLAYS_MAX. Stored only if the device's
    owner keeps a play history (device_plays.py); a retry stores nothing
    twice. Device Bearer token. Returns {"enabled", "stored", "duplicates",
    "unknown", "bad_time"}."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True, silent=True)
        plays = body.get("plays") if isinstance(body, dict) else None
        if not isinstance(plays, list) or not device_plays.valid_plays(plays):
            abort(400, description=_("plays must be a list of at most %(n)d track_id and played_at pairs.",
                                     n=device_plays.PLAYS_MAX))
        result = device_plays.record(conn, device["id"], device["owner_user_id"], plays)
        if result["stored"]:
            play_forward.enqueue_for(conn, device["owner_user_id"])
        return jsonify(result)


@bp.route("/api/device/playlists", methods=["POST"])
def api_device_playlists():
    """Playlists a person made on this device, read off its storage by the
    client: {"playlists": [{"path": "Playlists/Road Trip.m3u", "entries":
    ["../Artist/Album/01 - Song.flac", ...]}]}, the whole set found this
    sync (files carrying Trobar's marker left out). Each becomes a playlist
    of the device's owner; one missing from a later upload is removed.
    Device Bearer token. Returns {"playlists", "tracks", "matched",
    "unknown"} counts. See playlist_sync.sync_device_playlists."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True, silent=True)
        playlists = body.get("playlists") if isinstance(body, dict) else None
        if not isinstance(playlists, list) or len(playlists) > _DEVICE_PLAYLISTS_MAX:
            abort(400, description=_("playlists must be a list of at most %(n)d playlists.",
                                     n=_DEVICE_PLAYLISTS_MAX))
        for pl in playlists:
            entries = pl.get("entries") if isinstance(pl, dict) else None
            if not isinstance(pl, dict) or not _valid_device_playlist_path(pl.get("path")) \
                    or not isinstance(entries, list) or len(entries) > _DEVICE_PLAYLIST_ENTRIES_MAX \
                    or not all(isinstance(e, str) for e in entries):
                abort(400, description=_(
                    "Each playlist needs a path (a .m3u or .m3u8 file, relative, no \"..\") "
                    "and a list of entries."))
        return jsonify(playlist_sync.sync_device_playlists(conn, device["id"], playlists))


@bp.route("/api/device/changes")
def api_device_changes():
    with db.connection() as conn:
        device = _authenticated_device(conn)
        changes = sync_state.get_changes(conn, device["id"])
        # What each download will take on the device, for its space check
        # before downloading: a transcoded file's size is only known once
        # written, so it gets the estimate the usage figures use.
        estimates = autofit.estimated_sizes(
            conn, [t["track_id"] for t in changes["to_download"]], changes["transcode_format"])
        for t in changes["to_download"]:
            t["estimated_size"] = estimates.get(t["track_id"], t["size"])
        # trobar-garmin#54: a device that shows covers itself asks for them at
        # its size, and gets one signed address per album (cover_links): it
        # can't send its token with an image request.
        cover_size = request.args.get("cover_size", type=int)
        if cover_size is not None and _COVER_MIN_PX <= cover_size <= _COVER_MAX_PX:
            secret = _cover_secret()
            changes["covers"] = [
                {"key": a["key"], "track_ids": a["track_ids"],
                 "url": cover_links.address(secret, device["id"], a["track_id"], cover_size)}
                for a in device_store.cover_albums(conn, device["id"])]
    # #239: device sync is the trigger for computing the fingerprints this
    # device's provenance DB will store (the locked design decision: reuse
    # #200's cache when a track already has one, compute on first device sync
    # when it doesn't). Fire-and-forget AFTER the response is built and the
    # connection released — a real audio decode must never run inline in a
    # request holding a DB connection, and this is a client-polled hot path.
    # It no-ops harmlessly when there's nothing left to compute.
    #
    # Swallowed deliberately: provenance is a side-benefit of syncing and must
    # never be able to break the sync itself. start_ensure_fingerprints only
    # enqueues a job row, but that write CAN fail (a DB hiccup) — and that's
    # exactly when you'd least want every device's sync to start 500ing. The
    # pass is retried on the device's next sync anyway.
    try:
        provenance.start_ensure_fingerprints(device["id"])
    except Exception:
        current_app.logger.exception("could not start the device fingerprint pass")

    # #239 PR 2: continue any in-flight provenance rematch. The rematch handler
    # can't re-enqueue itself — it still holds its own dedupe_key while running,
    # so the follow-up would be refused — and its batch cap means one push
    # rarely finishes the work. A recovering device syncs repeatedly, so
    # continuing here drains it with no client involvement. No-ops (dedupe, or
    # nothing pending) in the overwhelmingly common case of no recovery underway.
    # Swallowed for the same reason as above: this must never break a sync.
    try:
        with db.connection() as conn:
            if provenance.pushed_pending_count(conn, device["id"]):
                if provenance.library_fingerprints_pending(conn):
                    jobs.enqueue(conn, provenance.JOB_TYPE_LIBRARY_FINGERPRINTS,
                                 dedupe_key=provenance.JOB_TYPE_LIBRARY_FINGERPRINTS)
                jobs.enqueue(conn, provenance.JOB_TYPE_REMATCH,
                             payload={"device_id": device["id"]},
                             dedupe_key=f"{provenance.JOB_TYPE_REMATCH}:{device['id']}")
    except Exception:
        current_app.logger.exception("could not continue the provenance rematch")
    return jsonify(changes)


@bp.route("/api/device/fingerprints")
def api_device_fingerprints():
    """#239: the server-computed fingerprint for each track this device holds
    or is about to hold, so the client can keep a local provenance DB —
    "these files came from Trobar, and here's the identity the server itself
    assigned them". Clients never compute fingerprints; they only store what
    this returns. Device Bearer token.

    Cursor-paginated on ascending track_id (`?after=`, `?limit=`) because a
    fingerprint is ~2.2 KB of text and a large DAP holds thousands of tracks —
    measured, and the reason this is its own endpoint rather than extra fields
    on /api/device/changes (which lists everything the device holds and is
    polled, so it would have grown by megabytes per poll).

    `path` is the same _device_path wire form /api/device/changes and
    /api/device/manifest already speak, so the client can key its provenance
    rows by the path it actually wrote. `fingerprint` is always the SOURCE
    audio's, even on a transcoding device — see provenance.py's docstring for
    why that's the only form that rematches on recovery. `pending` counts this
    device's tracks whose fingerprint isn't computed yet, so a client knows to
    come back rather than treat a short page as complete.

    #439: `?computed_after=` is a SECOND, orthogonal filter on top of the
    `after`/`next_after` track_id cursor above, not a replacement for it —
    that cursor still drives the initial full walk. Once a client has walked
    everything once, it can remember the highest `fingerprint_seq` it saw
    across every entry (each one is included below for exactly this) and
    pass it back as `computed_after` on every later sync, getting only
    tracks whose fingerprint is NEW or CHANGED since then — no more
    re-fetching megabytes of unchanged data every sync (the entire point:
    and measured). fingerprint_seq is a strictly-increasing
    counter, not a timestamp (db.py explains why: a wall-clock second can
    tie many rows together during a bulk backfill, which a `>` filter can't
    safely resolve; a counter can't ever tie). track_id ordering is
    UNCHANGED even when this filter is active — fingerprint_seq is never
    the sort key, only a WHERE predicate, so the existing cursor's
    resume-a-cut-off-page guarantee still holds exactly as before."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        try:
            after = int(request.args.get("after", 0))
            limit = int(request.args.get("limit", _FINGERPRINT_PAGE_DEFAULT))
            computed_after_raw = request.args.get("computed_after")
            computed_after = int(computed_after_raw) if computed_after_raw is not None else None
        except (TypeError, ValueError):
            abort(400, description=_("after, limit, and computed_after must be whole numbers."))
        if after < 0 or limit < 1 or (computed_after is not None and computed_after < 0):
            abort(400, description=_("after must be 0 or more and limit at least 1."))
        limit = min(limit, _FINGERPRINT_PAGE_MAX)

        fmt = device["transcode_format"]
        query = (
            "SELECT t.id, t.artist, t.album_artist, t.album, t.title, t.track_no, t.disc_no, "
            "t.relative_path, t.lossless, t.fingerprint, t.fingerprint_seq, dts.status, dts.delivered_path "
            "FROM device_track_state dts "
            "JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status IN ('pending', 'downloaded') "
            "AND t.deleted_at IS NULL AND t.fingerprint IS NOT NULL AND t.id > ? "
        )
        params = [device["id"], after]
        if computed_after is not None:
            query += "AND t.fingerprint_seq > ? "
            params.append(computed_after)
        query += "ORDER BY t.id LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        entries = [
            {"track_id": row["id"], "fingerprint": row["fingerprint"],
             "fingerprint_seq": row["fingerprint_seq"],
             "path": sync_state.on_device_path(row, fmt)}
            for row in rows
        ]
        return jsonify({
            "entries": entries,
            # Only a full page can have more behind it; a short page is the
            # end of the cursor walk (though `pending` may still be > 0 —
            # those tracks have no fingerprint YET, so they aren't in this
            # result set at all and a later call will pick them up).
            "next_after": entries[-1]["track_id"] if len(rows) == limit else None,
            "pending": provenance.pending_count(conn, device["id"]),
        })


@bp.route("/api/device/provenance", methods=["POST"])
def api_device_provenance():
    """#239 PR 2: a device pushes back the provenance DB it built from
    /api/device/fingerprints, and the server rematches it BY FINGERPRINT
    against its own library. Device Bearer token.

    Body: {"entries": [{"track_id": N, "fingerprint": "...", "path": "..."}]}

    Why this exists alongside /api/device/manifest: the manifest matches on a
    byte-exact device_path() comparison, which only works while the layout still
    agrees. device_path() is built from tags, track_no/disc_no, fs_segment()'s
    sanitisation and the transcode extension, so any drift there makes Trobar
    fail to recognise files it wrote itself — the "listed as unknown to adopt"
    symptom in #161. Audio content doesn't drift when a tag does. Both are
    accepted; provenance alone is sufficient and strictly better.

    Stores and returns immediately — matching happens in a background job,
    because verifying each entry costs an audio decode and this must not block a
    device's sync. `pending` is how the client knows there's more to do."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        entries = body.get("entries")
        if not isinstance(entries, list):
            abort(400, description=_("entries must be a list."))
        if len(entries) > _PROVENANCE_PUSH_MAX:
            abort(400, description=_(
                "Too many entries in one request — push them in smaller pages."))
        # Validated up front, so one malformed entry can't leave half a page
        # stored. path/fingerprint are what the rematch needs; track_id is
        # optional and only ever kept for diagnostics.
        for entry in entries:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("path"), str) or not entry["path"]
                    or not isinstance(entry.get("fingerprint"), str)
                    or not entry["fingerprint"]):
                abort(400, description=_(
                    "Each entry needs a non-empty path and fingerprint."))
            if entry.get("track_id") is not None and not isinstance(entry["track_id"], int):
                abort(400, description=_("track_id must be a whole number when given."))

        stored = provenance.store_pushed_provenance(conn, device["id"], entries)
        conn.commit()
        pending = provenance.pushed_pending_count(conn, device["id"])
        # One job however many pages were pushed — the dedupe key collapses them
        # while one is queued or running, and it frees once that finishes.
        # A pushed fingerprint can only match a track the server has itself
        # fingerprinted, and after a DB loss NOTHING in the library has been —
        # device-scoped fingerprinting (PR 1) covers only tracks a device
        # already syncs, which is empty in exactly that situation. So drive a
        # library-wide pass too; the rematch defers rows until it finishes.
        if provenance.library_fingerprints_pending(conn):
            jobs.enqueue(conn, provenance.JOB_TYPE_LIBRARY_FINGERPRINTS,
                         dedupe_key=provenance.JOB_TYPE_LIBRARY_FINGERPRINTS)
        jobs.enqueue(conn, provenance.JOB_TYPE_REMATCH, payload={"device_id": device["id"]},
                     dedupe_key=f"{provenance.JOB_TYPE_REMATCH}:{device['id']}")
        return jsonify({"received": len(entries), "stored": stored, "pending": pending})


@bp.route("/api/device/file/<int:track_id>")
def api_device_file(track_id: int):
    with db.connection() as conn:
        device = _authenticated_device(conn)
        # #110: authorize, not just authenticate. The Bearer token proves which
        # device is asking; this proves the track is one the server actually
        # offered THAT device (its own device_track_state set — an O(1) PK
        # lookup, exactly what get_changes() told it to fetch), not any track_id
        # in the library. So a stolen/copied device token (a plaintext
        # credential on the card, trobar-desktop#12) reaches only that device's
        # selections instead of the whole catalog. 'downloaded' is included for
        # re-download/integrity retries; 'removed'/absent are excluded. Checked
        # before the transcode branch below so that path inherits it too. 404
        # (not 403) so it doesn't confirm which track ids exist.
        allowed = conn.execute(
            "SELECT 1 FROM device_track_state WHERE device_id = ? AND track_id = ? "
            "AND status IN ('pending', 'downloaded')",
            (device["id"], track_id),
        ).fetchone()
        if allowed is None:
            abort(404, description=_("Track not found (removed from the library?)"))
        row = conn.execute(
            "SELECT relative_path, lossless FROM tracks WHERE id = ? AND deleted_at IS NULL", (track_id,)
        ).fetchone()
        if row is None:
            abort(404, description=_("Track not found (removed from the library?)"))
        abs_path = _contained(db.get_music_root(), row["relative_path"])
        if abs_path is None or not abs_path.is_file():
            abort(404, description=_("File not found on the server"))

        fmt = device["transcode_format"]
        if not sync_state.wants_transcode(row, fmt):
            return send_file(abs_path, conditional=True)

        # A transcode served before is served again from the cache (see
        # transcode.py): no slot, no ffmpeg, and Range works on it.
        cache_bytes = max(0, _config_int(conn, "transcode_cache_gb", transcode.DEFAULT_CACHE_GB)) * 1024 ** 3
        cached = None
        if cache_bytes:
            cached = transcode.cache_path(db.DATA_DIR, track_id, abs_path.stat().st_mtime_ns, fmt)
            if transcode.cache_hit(cached):
                return send_file(cached, mimetype="audio/mpeg", conditional=True)

        if not transcode.ffmpeg_available():
            abort(500, description=_("Transcoding is unavailable on the server right now"))
        limit = _config_int(conn, "transcode_concurrency", 1)
        nice_level = _config_int(conn, "transcode_nice_level", 10)
        if not transcode.acquire_slot(limit, timeout=transcode.SLOT_WAIT_SECONDS):
            # Busy, not broken: every slot has been taken for as long as a
            # web thread may wait. The client tries again on its next sync.
            resp = jsonify({"error": _("The server is busy transcoding other tracks. Try again shortly.")})
            resp.status_code = 503
            resp.headers["Retry-After"] = str(transcode.SLOT_WAIT_SECONDS)
            return resp
        # Another request may have encoded this track while this one waited
        # for the slot: a second device fetching the same new album.
        if cached is not None and transcode.cache_hit(cached):
            transcode.release_slot()
            return send_file(cached, mimetype="audio/mpeg", conditional=True)
        try:
            proc, out_path, err_path = transcode.start(
                abs_path, fmt, nice_level, out_dir=cached.parent if cached is not None else None)
        except transcode.TranscodeStartError:
            transcode.release_slot()
            abort(500, description=_("Transcoding failed to start"))
        # The encode finishes into a temp file before any byte is sent (see
        # transcode.py), so a failure is an error response, never an empty
        # 200 a client would take for the track.
        if not transcode.wait_for(proc, out_path, err_path):
            abort(500, description=_("Transcoding this track failed"))
        if cached is not None and transcode.keep(out_path, err_path, cached, cache_bytes):
            return send_file(cached, mimetype="audio/mpeg", conditional=True)
        # With no cache (or a cache that couldn't take it): the size is known
        # (the encode is finished) and sent, so a client can tell a cut-off
        # download from a complete one. Range/resume isn't offered: Accept-Ranges: none tells well-behaved
        # clients not to bother, and a dropped connection just means the
        # client restarts the track from scratch on its next sync, same as
        # any other failed download.
        return Response(transcode.iter_file(out_path, err_path), mimetype="audio/mpeg",
                        headers={"Accept-Ranges": "none", "Content-Length": str(os.path.getsize(out_path))})


def _cover_secret() -> bytes:
    """The key cover addresses are signed with: the app's own secret key."""
    key = current_app.secret_key
    if not key:
        raise RuntimeError("the app has no secret key to sign cover addresses with")
    return key.encode("utf-8") if isinstance(key, str) else key


# A watch's album cover (trobar-garmin#54): small enough to store dozens of,
# never so small it's a smudge.
_COVER_MIN_PX = 32
_COVER_MAX_PX = 256


@bp.route("/api/device/cover/<int:track_id>")
def api_device_cover(track_id: int):
    """The album cover of a track this device was offered, at most `size`
    pixels on its longer side (JPEG, never enlarged), for a device that
    shows covers itself rather than reading them from the file: a Garmin
    watch's player doesn't.

    Authenticated by a signed address (cover_links), not a token: the watch
    can't send one with an image request. The address comes from that
    device's own /api/device/changes answer and works for this one device,
    track and size until it expires. Anything else is 403, before anything
    is looked at; nothing here logs the address. 404 for a track no longer
    offered to the device, as /api/device/file does, and for an album with
    no cover."""
    args = request.args
    device_id = args.get("d", type=int)
    size = args.get("size", type=int)
    expires = args.get("exp", type=int)
    sig = args.get("sig", "")
    if device_id is None or size is None or expires is None or not cover_links.valid(
            _cover_secret(), device_id, track_id, size, expires, sig):
        abort(403, description=_("This cover address is not valid."))
    if not _COVER_MIN_PX <= size <= _COVER_MAX_PX:
        abort(400, description=_("The size must be between {min} and {max} pixels.").format(
            min=_COVER_MIN_PX, max=_COVER_MAX_PX))
    with db.connection() as conn:
        device = {"id": device_id}
        row = conn.execute(
            "SELECT t.relative_path, COALESCE(NULLIF(t.album_artist, ''), t.artist) AS artist, t.album "
            "FROM device_track_state s JOIN tracks t ON t.id = s.track_id "
            "WHERE s.device_id = ? AND s.track_id = ? AND s.status IN ('pending', 'downloaded') "
            "AND t.deleted_at IS NULL",
            (device["id"], track_id),
        ).fetchone()
    if row is None:
        abort(404, description=_("Track not found (removed from the library?)"))
    source = _contained(db.get_music_root(), row["relative_path"])
    cover = covers.get_cover(row["artist"] or "", row["album"] or "", source) if source else None
    if cover is None:
        abort(404, description=_("No cover for this album"))
    data, content_type = artist_images.downscale(*cover, max_px=size)
    return Response(data, mimetype=content_type, headers={"Cache-Control": "private, max-age=86400"})


@bp.route("/api/device/artist-image")
def api_device_artist_image():
    with db.connection() as conn:
        _authenticated_device(conn)
        provider = _active_provider(conn)
        audiodb_key = db.get_config(conn, "audiodb_api_key")
    artist = request.args.get("artist", "")
    if not artist:
        abort(404)
    found = artist_images.get_artist_image(artist, provider, audiodb_key)
    if found is None:
        abort(404, description=_("No artist image available"))
    data, content_type = found
    if request.args.get("size") == "small": # DAP-friendly variant
        data, content_type = artist_images.downscale(data, content_type)
    return Response(data, mimetype=content_type, headers={"Cache-Control": "public, max-age=86400"})


@bp.route("/api/device/ack", methods=["POST"])
def api_device_ack():
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        raw_bytes = body.get("bytes_on_device")
        sync_state.ack(conn, device["id"], int(body["track_id"]), body["status"],
                       int(raw_bytes) if raw_bytes is not None else None)
        return jsonify({"status": "ok"})


@bp.route("/api/device/missing-tracks", methods=["POST"])
def api_device_missing_tracks():
    """the client spot-checks its own "should be downloaded"
    tracks against what's actually on disk (this endpoint never does that
    itself, it only records the client's — or its own standing preference's
    — decision): `redownload` flips those track ids back to `pending`,
    `exclude` marks them so they stop being silently re-queued without
    touching the selection that still nominally requires them."""
    with db.connection() as conn:
        device = _authenticated_device(conn)
        body = request.get_json(force=True)
        if not isinstance(body, dict):
            abort(400, description=_("redownload and exclude must be lists of track ids."))
        redownload_ids = _validated_missing_track_ids(body.get("redownload", []))
        exclude_ids = _validated_missing_track_ids(body.get("exclude", []))
        sync_state.resolve_missing_tracks(conn, device["id"], redownload_ids, exclude_ids)
        return jsonify({"status": "ok"})
