#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public YouTube Music playlists, fetched by URL with no account.

This is not a provider in the sense the rest of this app uses the word.
There is no connection to configure, no credentials to store, nothing to
pair, and no active-provider slot to occupy: a user pastes the URL of a
*public* playlist, and its tracks are matched against the local library
like any other playlist source. Subscriptions live in
`playlist_subscriptions` (see db.py), one row per URL per user, and
playlist_sync merges them in alongside whichever provider is active — the
same shape the per-user Tidal and Spotify blocks already have, minus every
part of those that exists to manage a credential.

WHAT THIS DEPENDS ON, SAID PLAINLY: `ytmusicapi` talks to YouTube Music's
internal `/youtubei/v1/*` endpoints. Those are reverse-engineered, not a
sanctioned API, and Google does not promise they will keep working or keep
their shape. Nothing here changes that, and the read being unauthenticated
and read-only does not make the endpoints official. Every failure mode in
this module is therefore treated as expected rather than exceptional: a
fetch that fails must degrade to a visible "unavailable" and must never
cause an already-synced playlist to be deleted (playlist_sync's prune
protection is the other half of that).

Why YouTube and not "any playlist URL", which is the obvious next
question: Spotify's Get Playlist returns `items` only for playlists the
authenticated user owns or collaborates on, so no token of any kind reads
a stranger's public playlist — a hard block, not friction. Tidal's
playlist endpoints likewise need a user token regardless of whether the
playlist is public. The provider where this works today is the one without
a sanctioned API, and the ones with sanctioned APIs forbid exactly this.
So the module is deliberately named for YouTube Music rather than dressed
up as a generic URL importer it cannot be.

The import is function-local, not module-level. Two reasons, both load-
bearing: nothing external is contacted (or even imported) until a user has
actually subscribed to something, and an install whose dependency is
missing degrades to one unusable feature instead of a server that will not
start.
"""

import logging
from urllib.parse import parse_qs, urlparse

_log = logging.getLogger(__name__)

PROVIDER_ID = "ytmusic"

# Hosts whose /playlist?list= or /watch?list= means a YouTube playlist.
# An allowlist rather than a "contains youtube" test: the id is fetched
# from YouTube Music whatever the pasted host was, so accepting a
# look-alike host would silently fetch something the user did not name.
_HOSTS = {
    "music.youtube.com", "www.youtube.com", "youtube.com", "m.youtube.com",
    "youtu.be", "www.youtu.be",
}

# A playlist id is the opaque `list=` value. Length and alphabet are not
# validated beyond "non-empty and no separators": the shapes YouTube uses
# have changed before (PL…, OLAK5uy_…, RDCLAK5uy_…, VL… prefixes among
# them), and a client-side format rule would reject valid ids the day
# YouTube adds another one. An id that does not resolve is reported by the
# fetch, which is the check that can actually tell.
_DISALLOWED_IN_ID = set("/?&#\\ \t\n")


def parse_playlist_url(raw: str) -> str | None:
    """The playlist id in a pasted YouTube/YouTube Music URL, or None.

    Accepts what a user actually copies: the Share link
    (`music.youtube.com/playlist?list=…`), the address bar while a playlist
    is playing (`…/watch?v=…&list=…`), the www/m/youtu.be variants of both,
    and a bare id pasted on its own. A `VL` prefix — what YouTube Music's
    own browse ids carry — is stripped, since ytmusicapi expects the
    unprefixed form and a user copying from the wrong place should not have
    to know that.

    Returns None for anything else, including a URL on a host we do not
    recognise: this is the only validation before a network call, so it
    fails closed."""
    raw = (raw or "").strip()
    if not raw:
        return None

    if "://" not in raw and "/" not in raw and "?" not in raw:
        return _clean_id(raw)  # a bare id, pasted on its own

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.hostname is None or parsed.hostname.lower() not in _HOSTS:
        return None
    values = parse_qs(parsed.query).get("list")
    if not values:
        return None
    return _clean_id(values[0])


def _clean_id(value: str) -> str | None:
    value = value.strip()
    if value.startswith("VL"):
        value = value[2:]
    if not value or any(c in _DISALLOWED_IN_ID for c in value):
        return None
    return value


def _entry(index: int, item: dict) -> dict:
    """One track in the shape playlist_sync/identity.py consume.

    `artists` is a list because YouTube Music credits features
    individually; they are joined with ", " to match how the other
    providers hand over a multi-artist credit, and because
    matching.match_playlist_track() takes a single artist string. `album`
    is absent on most user-curated playlists (measured: 0-6% of tracks on
    the ones sampled for this feature) and stays None there — the matcher
    does not use it, so this costs nothing but is worth not pretending
    about."""
    artists = item.get("artists") or []
    names = [str(a["name"]) for a in artists if isinstance(a, dict) and a.get("name")]
    album = item.get("album") or {}
    return {
        "position": index,
        "title": item.get("title"),
        "artist": ", ".join(names),
        "album": album.get("name") if isinstance(album, dict) else None,
        "path": None,
    }


def fetch_playlist(playlist_id: str) -> dict:
    """One public playlist by id.

    Returns `{"status": "ok", "title": str, "tracks": [...]}`, or
    `{"status": "unavailable"}` when YouTube will not give us the playlist
    (deleted, made private, or an id that never existed — the API does not
    reliably distinguish these, so neither does this), or
    `{"status": "error", "reason": str}` for anything else, which includes
    the whole class of "the unofficial endpoint changed shape". Callers
    treat the last two identically for safety; they are kept apart only so
    the UI can say something true about which happened.

    `limit=None` fetches every page. A playlist is a bounded thing the user
    explicitly asked for, and a silently truncated import would look like a
    matching failure rather than a missing page."""
    try:
        from ytmusicapi import YTMusic  # noqa: PLC0415 — see module docstring
    except ImportError:
        return {"status": "error", "reason": "dependency_missing"}

    try:
        data = YTMusic().get_playlist(playlist_id, limit=None)
    except Exception as e:  # noqa: BLE001
        # Deliberately broad. ytmusicapi raises its own YTMusicError family
        # for backend refusals, but a reverse-engineered endpoint also
        # produces plain parse errors (KeyError/TypeError out of its
        # navigation helpers) when the response shape moves, and requests'
        # own network exceptions underneath. None of those should reach a
        # sync run as a traceback: every one of them means "no tracks this
        # time", which is already a state this feature has to handle.
        name = type(e).__name__
        _log.info("[ytmusic] playlist %s unavailable (%s)", playlist_id, name)
        if name in ("YTMusicUserError", "YTMusicServerError", "YTMusicGatedError"):
            return {"status": "unavailable"}
        return {"status": "error", "reason": name}

    if not isinstance(data, dict):
        return {"status": "error", "reason": "unexpected_response"}

    tracks: list[dict] = []
    for item in data.get("tracks") or []:
        if not isinstance(item, dict):
            continue
        entry = _entry(len(tracks), item)
        if not entry["title"]:
            continue  # nothing for the matcher to work with
        tracks.append(entry)
    return {"status": "ok", "title": data.get("title") or playlist_id, "tracks": tracks}


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """The provider-shaped call playlist_sync._sync_one_playlist() makes.

    `source_playlist_id` is the playlist id and is what actually
    identifies the playlist; `playlist_title` is only a fallback for the
    response's `playlist` field. Unlike every other provider here, that
    field carries the REMOTE playlist's current title rather than echoing
    the argument back -- a subscription is a single playlist rather than an
    entry in a listing, so this fetch is the only place its title is ever
    learned, and a rename at the source has nowhere else to come from.

    A non-"ok" status makes _sync_one_playlist() skip the playlist rather
    than write an empty one, which is the behaviour that keeps a failed
    fetch from emptying a playlist somebody already synced to a device."""
    if not source_playlist_id:
        return {"status": "not_found", "failed_segment": playlist_title}
    result = fetch_playlist(source_playlist_id)
    if result["status"] != "ok":
        return result
    return {"status": "ok", "playlist": result["title"] or playlist_title,
            "tracks": result["tracks"]}
