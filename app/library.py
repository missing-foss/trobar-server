#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web Library, /api/library/*: artists, albums, an album's tracks,
covers and artist images, stats, the quiz, recently added/released, and
library scans. Session-authenticated: main's before-request gate covers
these paths.
"""

import logging

from flask import jsonify, request, abort, Blueprint
from flask_babel import gettext as _

import db
import library_quiz
import scanner
from web_common import (
    _ALBUM_ARTIST,
    _artist_image_config,
    _artist_image_response,
    _cover_response,
    _is_admin,
    _library_album_artist_albums,
    _library_album_artists,
    _library_album_tracks,
    _library_appears_on,
    _library_cover_row,
    _library_similar_artists,
    _library_stats,
    _recently_added_for,
    _recently_released_for,
    get_current_user_id,
)

_log = logging.getLogger(__name__)

bp = Blueprint("library", __name__)


@bp.route("/api/library/artists")
def api_library_artists():
    with db.connection() as conn:
        return jsonify(_library_album_artists(conn))


@bp.route("/api/library/albums")
def api_library_albums():
    with db.connection() as conn:
        return jsonify(_library_album_artist_albums(conn, request.args.get("artist", "")))


@bp.route("/api/library/appears-on")
def api_library_appears_on():
    with db.connection() as conn:
        return jsonify(_library_appears_on(conn, request.args.get("artist", "")))


@bp.route("/api/library/album-tracks")
def api_library_album_tracks():
    with db.connection() as conn:
        return jsonify(_library_album_tracks(
            conn, request.args.get("artist", ""), request.args.get("album", "")))


@bp.route("/api/library/stats")
def api_library_stats():
    """Real numbers for the home dashboard's Library widget — codec split,
    storage per codec, total duration, decade breakdown. One full pass over
    tracks (relative_path/size/duration/year only — cheap columns, no tag
    blobs), aggregated in Python: sqlite has no clean "extension of a path"
    built-in, and this is a snapshot computed on dashboard load, not a hot
    path worth a fancier query."""
    with db.connection() as conn:
        return jsonify(_library_stats(conn))


@bp.route("/api/library/similar-artists")
def api_library_similar_artists():
    """Artists similar to `artist` (Last.fm artist.getSimilar) that are actually
    in the local library — so they're selectable/syncable, not
    external discovery. Returns up to 8 library-cased names, most-similar first.
    [] if Last.fm isn't configured or nothing similar is in the library."""
    with db.connection() as conn:
        user_id = get_current_user_id(conn)
        return jsonify(_library_similar_artists(conn, user_id, request.args.get("artist", "")))


@bp.route("/api/library/cover")
def api_library_cover():
    artist = request.args.get("artist", "")
    album = request.args.get("album", "")
    with db.connection() as conn:
        row = _library_cover_row(conn, artist, album)
    # size=small: the thumbnail variant (see covers.get_small_cover).
    return _cover_response(artist, album, row, small=request.args.get("size") == "small")


@bp.route("/api/library/artist-image")
def api_library_artist_image():
    with db.connection() as conn:
        provider, audiodb_key = _artist_image_config(conn)
    return _artist_image_response(request.args.get("artist", ""), provider, audiodb_key)


@bp.route("/api/library/quiz-pair")
def api_library_quiz_pair():
    """#508: the About tab's "duel the bard" Easter egg — "which artist has
    more albums?", scoped to the local library. Pair selection is a pure
    function in library_quiz.py (unit-testable without a browser or a DB);
    this route is just the read plus the JSON shape.

    Artist images are NOT pre-checked for availability here — artist_images.py
    caches on a hit only (no negative cache), so there's no cheap way to know
    a miss without actually fetching, and doing that here would add real
    network cost to a for-fun endpoint. The client already handles a missing
    artist image gracefully everywhere else (artistImageUrl() + the
    established @error/@load pattern degrades to a neutral placeholder), so
    the quiz cards reuse that instead of adding server-side prefetch."""
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT artist, COUNT(DISTINCT album) AS album_count FROM tracks "
            "WHERE deleted_at IS NULL GROUP BY artist"
        ).fetchall()
    candidates = library_quiz.eligible_candidates([dict(r) for r in rows])
    pair = library_quiz.pick_pair(candidates)
    if pair is None:
        return jsonify({"available": False})
    a, b = pair
    return jsonify({"available": True, "a": a, "b": b})


@bp.route("/api/library/scan", methods=["POST"])
def api_library_scan():
    # A forced scan re-reads every file and clears the cover cache: an
    # admin's call, as the UI already has it.
    force = request.args.get("force") == "1"
    # Lets a scan through that would remove most of the library (an
    # unmounted share looks the same: scanner.MassRemovalRefused), so it is
    # an admin's call too.
    confirm = request.args.get("confirm_mass_removal") == "1"
    if force or confirm:
        with db.connection() as conn:
            if not _is_admin(conn, get_current_user_id(conn)):
                if confirm:
                    abort(403, description=_("Only an admin can confirm removing most of the library."))
                abort(403, description=_("Only an admin can run a full rescan."))
    # #140: background the scan and return immediately (202) — a full scan takes
    # tens of minutes; the UI polls .../scan/status and shows counts when done.
    result = scanner.start_scan(db.get_music_root(), force=force, confirm_mass_removal=confirm)
    if result.get("already_running"):
        return jsonify({"error": _("A library scan is already running.")}), 409
    return jsonify(result), 202


@bp.route("/api/library/scan/status")
def api_library_scan_status():
    # #140: running/idle + the last completed scan's counts, polled by the UI.
    return jsonify(scanner.scan_status())


@bp.route("/api/library/recently-added")
def api_library_recently_added():
    """Home dashboard's Recently Added widget — distinct from the
    Suggestions tab's own fixed top-N recently-added section (see
    suggestions.recently_added_widget's docstring)."""
    months = request.args.get("months", 3, type=int)
    with db.connection() as conn:
        return jsonify(_recently_added_for(conn, get_current_user_id(conn), months))


@bp.route("/api/library/recently-released")
def api_library_recently_released():
    """Home dashboard's Recently Released widget — new to the world
    (tag-derived release_date), independent of when the scanner saw the
    file (that's Recently Added, above)."""
    months = request.args.get("months", 3, type=int)
    with db.connection() as conn:
        return jsonify(_recently_released_for(conn, get_current_user_id(conn), months))
