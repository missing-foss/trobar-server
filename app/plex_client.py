#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plex Media Server API client — connectivity, playlist browsing.

Server-provider, like subsonic_client.py/jellyfin_client.py: an
admin-configured connection (server URL + a server-scoped X-Plex-Token),
not a per-user OAuth streaming link. Unlike jellyfin_client.py, there's no
separate userId resolution step — the token itself is scoped to the Plex
account that owns (or was shared) the server, so playlist visibility
follows the token, not a chosen username. Plex is the one
directly-buildable provider among the streaming candidates surveyed: an
official, documented 2025 API, no
credential/partner gate.

Unlike roon_client.py, there's no persistent connection/pairing handshake
to maintain — Plex's HTTP API is stateless, authenticated per-request via
a static token (X-Plex-Token header). No "pending_approval" state exists
either: a valid authenticated call means paired, anything else means
disconnected.

Plex returns XML by default; `Accept: application/json` gets the JSON
shape used throughout this module (`{"MediaContainer": {"Metadata": [...]}}`
for every list-shaped response).

Playlist tracks here carry a real on-disk path (Media[0].Part[0].file) and
album (parentTitle), unlike Roon's Browse API which only ever exposes a
display title/subtitle — see matching.py's match_playlist_track_by_path,
which this enables as the primary match strategy instead of relying on the
fuzzy artist/title heuristics built for Roon's fewer-than-ideal metadata.
This is a real accuracy win over the streaming providers (Tidal/Spotify),
which have no local path to give — free here since Plex, as a server that
scans and tags the same kind of local library Trobar does, already has it.

Not live-verified against a real Plex server (none available in this
environment) — built directly from Plex's official API docs
(developer.plex.tv / plexapi.dev) and the python-plexapi reference
implementation. This is worth a real-server pass (confirming the exact JSON
shapes and current token/JWT behavior) before treating the provider as
fully proven out — same caveat as emby_client.py carries for Emby.

get_artist_image() is deliberately a stub (see its own docstring) — #158
explicitly scopes Plex artist images as an "(optional follow-up)", not
part of this provider's initial acceptance criteria.
"""

import requests

import db
import matching
import provider_config
from mirror import MIRROR_SUFFIX

_MUSIC_PLAYLIST_TYPE = "audio"


def _current_config() -> tuple[str, str]:
    # An unsaved connection the switch preview reads through: provider_config.
    unsaved = provider_config.override("plex")
    if unsaved is not None:
        return unsaved
    conn = db.get_conn()
    try:
        url = db.get_config(conn, "plex_url") or ""
        token = db.get_config(conn, "plex_token") or ""
        return url, token
    finally:
        conn.close()


def _get(endpoint: str, token: str, url: str, params: dict | None = None):
    """Low-level authenticated GET. Returns the parsed JSON body, or None if
    not configured or the request itself failed (network error, non-2xx,
    unparseable body)."""
    if not url or not token:
        return None
    headers = {"Accept": "application/json", "X-Plex-Token": token}
    try:
        resp = requests.get(f"{url.rstrip('/')}{endpoint}", headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except (requests.RequestException, ValueError):
        return None


def ensure_started() -> None:
    """No persistent connection to establish — kept for interface parity
    with roon_client so main.py's startup dispatch needs no special case."""
    pass


def status() -> dict:
    url, token = _current_config()
    if not url:
        return {"state": "disconnected", "url": url, "provider": "plex"}
    # The PMS root ("/") returns server identity (MediaContainer with
    # friendlyName/machineIdentifier) for any request carrying a valid
    # token, and a 401 (-> None from _get) otherwise — the simplest
    # available "is this token still good" check.
    resp = _get("/", token, url)
    if resp is not None and resp.get("MediaContainer") is not None:
        return {"state": "paired", "url": url, "provider": "plex"}
    return {"state": "disconnected", "url": url, "provider": "plex"}


def retry_pairing() -> dict:
    """Plex has no Roon-style manual-approval step to retry — a fresh
    check is the whole story, so this is just status(). Kept as its own
    function purely for interface parity with roon_client.retry_pairing,
    so main.py's dispatch never needs a provider-specific branch."""
    return status()


def test_connection(url: str, token: str) -> dict:
    """#509 item 3: same check as status(), against an EXPLICIT token
    rather than the stored config — never persists anything. See
    subsonic_client.test_connection's own docstring for the full
    rationale (the admin config form's live pre-save check)."""
    resp = _get("/", token, url)
    if resp is not None and resp.get("MediaContainer") is not None:
        return {"state": "paired", "url": url, "provider": "plex"}
    return {"state": "disconnected", "url": url, "provider": "plex"}


def reconnect(url: str, token: str) -> dict:
    """Admin (re)configured the Plex connection from the web UI — persist
    it and report whether the result checks out. No username to resolve
    (unlike jellyfin_client.reconnect): the token is already scoped to one
    Plex account."""
    conn = db.get_conn()
    try:
        db.set_config(conn, "plex_url", url)
        db.set_config(conn, "plex_token", token)
        conn.commit()
    finally:
        conn.close()
    return status()


def list_playlists() -> dict:
    """Returns {"status": "ok", "playlists": [{"id", "title"}, ...]} — the
    common shape every provider client returns (#75). `playlistType=audio`
    filters out Plex's video/photo playlists at the source. Plex exposes a
    real, stable `ratingKey`, so `id` is set (stringified) and drives the
    sync's composite key — two same-named Plex playlists coexist as
    separate rows instead of collapsing."""
    url, token = _current_config()
    resp = _get("/playlists", token, url, {"playlistType": _MUSIC_PLAYLIST_TYPE})
    if resp is None:
        return {"status": "error", "reason": "not_paired"}
    items = resp.get("MediaContainer", {}).get("Metadata") or []
    # Trobar's own copies are never sources, when the library and the
    # mirror target are one server: known by their name (MIRROR_SUFFIX),
    # and by the keys the sink stored ON THIS SERVER, so a copy renamed in
    # Plex is still recognised. Keys are numbered per server, so a copy's
    # key on another server says nothing about a playlist here.
    copies: set[str] = set()
    held = _mirror_ids()
    if held:  # only then is it worth asking which server this is
        identity = _get("/identity", token, url)
        machine = (identity or {}).get("MediaContainer", {}).get("machineIdentifier")
        copies = {key for key, held_on in held.items() if machine and held_on == machine}
    return {"status": "ok", "playlists": [
        {"id": str(i["ratingKey"]), "title": i["title"]}
        for i in items if i.get("title") and i.get("ratingKey") is not None
        and not i["title"].endswith(MIRROR_SUFFIX) and str(i["ratingKey"]) not in copies
    ]}


def _mirror_ids() -> dict[str, str]:
    """The copies the mirror sink made: {key: machineIdentifier of the
    server holding it}."""
    conn = db.get_conn()
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT plex_mirror_remote_id, plex_mirror_machine FROM playlists "
            "WHERE plex_mirror_remote_id IS NOT NULL")}
    finally:
        conn.close()


def _first_file(entry: dict) -> str | None:
    """A track item's on-disk path lives three levels deep:
    Media[0].Part[0].file — a multi-version track (rare for typical
    libraries) would have more than one Media entry, but the first is
    always the one Plex itself would play."""
    media = entry.get("Media") or []
    if not media:
        return None
    parts = media[0].get("Part") or []
    if not parts:
        return None
    return parts[0].get("file")


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """Tracks of one playlist, in order. Each item is {"position", "title",
    "artist", "path", "album"} — path/album are what let matching.py skip
    straight to an exact-path match instead of its Roon-Browse-API-specific
    fuzzy fallback.

    Fetched by `source_playlist_id` directly (the ratingKey from
    list_playlists) — fetching by title would be ambiguous now that
    same-named playlists can coexist. Falls back to a title match only if
    no id is given (the sync always passes it)."""
    url, token = _current_config()
    if source_playlist_id is not None:
        rating_key: str | None = source_playlist_id
    else:
        listed = list_playlists()
        if listed["status"] != "ok":
            return {"status": "error", "reason": "not_paired"}
        match = next((p for p in listed["playlists"] if p["title"] == playlist_title), None)
        if match is None:
            return {"status": "not_found", "failed_segment": playlist_title}
        rating_key = match["id"]

    resp = _get(f"/playlists/{rating_key}/items", token, url)
    if resp is None:
        return {"status": "error", "reason": "playlist items fetch failed"}
    entries = resp.get("MediaContainer", {}).get("Metadata") or []

    tracks = [
        {
            "position": i,
            "title": e.get("title", ""),
            "artist": e.get("grandparentTitle", ""),
            "path": _first_file(e),
            "album": e.get("parentTitle"),
        }
        for i, e in enumerate(entries)
    ]
    return {"status": "ok", "playlist": playlist_title, "tracks": tracks}


def get_artist_image(artist_name: str) -> tuple[bytes, str] | None:
    """Always None — #158 explicitly scopes Plex artist images (via each
    artist's `thumb` field, under a music-library section walk mirroring
    jellyfin_client's _build_artist_image_key_map) as an optional
    follow-up, not part of this provider's initial acceptance criteria.
    A miss here just means no picture (same contract as every other
    provider's get_artist_image) — artist_images.py still falls back to
    the filesystem provider, so this never blocks a sync."""
    return None


# --- The mirror sink's side (mirror_plex.py) --------------------------------
#
# Measured live against Plex Media Server 1.43.4.10903 (the official
# plexinc/pms-docker image, unclaimed, a music section of tagged FLACs):
# - Tracks: GET /library/sections/{key}/all?type=10, paged with
#   X-Plex-Container-Start/Size (as query parameters; headers work too);
#   totalSize is only sent when paging is asked for. Each item has
#   ratingKey (a string), grandparentTitle (artist), parentTitle (album),
#   index (track number) and Media[0].Part[0].file. sort=id keeps the order
#   stable between pages.
# - Create: POST /playlists?type=audio&title=…&smart=0&uri=server://{machine
#   identifier}/com.plexapp.plugins.library/library/metadata/{k1},{k2}. The
#   order is kept. An empty `uri=` makes an empty playlist; no uri at all is
#   a 400, as is no type.
# - Items: GET /playlists/{id}/items, in order, each with its own
#   playlistItemID; an empty playlist has no Metadata key.
# - Replace: there is none. PUT /playlists/{id}/items?uri=… APPENDS, and
#   skips tracks already there; DELETE /playlists/{id}/items clears the list
#   (the playlist stays). So a replace is a clear, then a PUT of the new
#   list, which keeps the order sent.
# - A playlist holds a track at most once, whatever is sent.
# - A key Plex doesn't have is dropped silently, still with a 200, as is a
#   wrong machine identifier: only a read-back tells.
# - Rename and summary: PUT /playlists/{id}?title=…&summary=… (200, empty).
# - Delete: DELETE /playlists/{id} answers 204; afterwards every call on the
#   id is a 404. Playlists share their key space with tracks and albums:
#   GET /playlists/{a track's key} is a 200 of type "track", so a copy is
#   only known to exist when the answer is of type "playlist".
# - Errors (401, 404) come back as HTML, whatever Accept says.
# - A playlist's updatedAt moves on a plain GET of it or its items, so it
#   can't show whether anything was written; the request log can.
# Not measured: managed (Plex Home) users' tokens, which only plex.tv
# issues; the copies land in the account of whichever token is configured.

_PAGE_SIZE = 500
_LIBRARY_URI = "server://{machine}/com.plexapp.plugins.library/library/metadata/{keys}"
# The path segments compared between Plex's view of a file and Trobar's:
# artist folder, album folder, file. The two music roots differ.
_TAIL_SEGMENTS = 3


def mirror_name(title: str) -> str:
    """The name a mirrored playlist gets in Plex: the title plus
    mirror.MIRROR_SUFFIX, so it reads as Trobar's copy and is never
    imported back (list_playlists skips it)."""
    return f"{title}{MIRROR_SUFFIX}"


def _mirror_request(method: str, endpoint: str, params: dict | None = None):
    """One call to the mirror target: (status, parsed JSON or None), or None
    when it isn't configured or didn't answer."""
    config = db.get_mirror_plex_config()
    if config is None:
        return None
    url, token = config
    headers = {"Accept": "application/json", "X-Plex-Token": token}
    try:
        resp = requests.request(method, f"{url.rstrip('/')}{endpoint}", headers=headers,
                                params=params, timeout=15)
    except requests.RequestException:
        return None
    try:
        body = resp.json() if resp.content and "json" in resp.headers.get("Content-Type", "") else None
    except ValueError:
        body = None
    return resp.status_code, body


def _container(body) -> dict:
    return (body or {}).get("MediaContainer") or {}


def mirror_status() -> dict:
    """Same shape as status(), for the mirror target."""
    config = db.get_mirror_plex_config()
    if config is None:
        return {"state": "disconnected", "url": "", "provider": "plex"}
    answer = _mirror_request("GET", "/")
    paired = answer is not None and answer[0] == 200 and answer[1] is not None
    return {"state": "paired" if paired else "disconnected", "url": config[0], "provider": "plex"}


def mirror_machine() -> str | None:
    """The mirror target's machineIdentifier, or None if it doesn't say."""
    answer = _mirror_request("GET", "/identity")
    return _container(answer[1]).get("machineIdentifier") if answer and answer[0] == 200 else None


def tail_key(path: str) -> tuple[str, ...]:
    """The last folders and the file name of a path, case-folded: how a
    Trobar track and Plex's view of the same file are compared, since the
    two music roots differ."""
    return tuple(p.casefold() for p in path.replace("\\", "/").split("/") if p)[-_TAIL_SEGMENTS:]


def mirror_build_index() -> dict | None:
    """The target's music tracks, once per sync run: {"machine": its machine
    identifier, "by_tail": {tail_key: [entry]}, "by_tags": {(artist, album,
    title) normalised: [entry]}}, each entry {"key", "track_no"}. None when
    the target can't be read (a partial index would read as missing tracks)."""
    identity = _mirror_request("GET", "/identity")
    machine = _container(identity[1]).get("machineIdentifier") if identity and identity[0] == 200 else None
    sections = _mirror_request("GET", "/library/sections")
    if not machine or sections is None or sections[0] != 200:
        return None
    by_tail: dict[tuple[str, ...], list[dict]] = {}
    by_tags: dict[tuple[str, str, str], list[dict]] = {}
    for section in _container(sections[1]).get("Directory") or []:
        if section.get("type") != "artist":
            continue
        start = 0
        while True:
            page = _mirror_request("GET", f"/library/sections/{section['key']}/all", {
                "type": 10, "sort": "id",
                "X-Plex-Container-Start": start, "X-Plex-Container-Size": _PAGE_SIZE})
            if page is None or page[0] != 200:
                return None
            container = _container(page[1])
            items = container.get("Metadata") or []
            for item in items:
                entry = {"key": str(item["ratingKey"]), "track_no": item.get("index")}
                path = _first_file(item)
                if path:
                    by_tail.setdefault(tail_key(path), []).append(entry)
                tags = (matching.normalize(item.get("grandparentTitle") or ""),
                        matching.normalize(item.get("parentTitle") or ""),
                        matching.normalize(item.get("title") or ""))
                by_tags.setdefault(tags, []).append(entry)
            start += len(items)
            if not items or start >= int(container.get("totalSize") or 0):
                break
    return {"machine": machine, "by_tail": by_tail, "by_tags": by_tags}


def mirror_read(remote_id: str) -> dict | None:
    """A copy as it stands: {"title", "summary", "keys": [track keys, in
    order]}, or None when it can't be read (gone, or no answer:
    mirror_exists tells which)."""
    meta = _mirror_request("GET", f"/playlists/{remote_id}")
    items = _mirror_request("GET", f"/playlists/{remote_id}/items")
    if meta is None or items is None or meta[0] != 200 or items[0] != 200:
        return None
    # A key that now names a track has no items to read (a 500), so this
    # is only reached for a playlist; mirror_exists tells the two apart.
    [playlist] = _container(meta[1]).get("Metadata") or [{}]
    return {"title": playlist.get("title"), "summary": playlist.get("summary") or "",
            "keys": [str(i["ratingKey"]) for i in _container(items[1]).get("Metadata") or []]}


def mirror_exists(remote_id: str) -> bool | None:
    """Whether the copy `remote_id` is still a playlist on the target:
    False when it is gone (a 404, or the key now names something else, as
    playlists share keys with tracks), None when the target didn't say
    (no answer, a 5xx, a 401), so a passing error is never taken for a
    deletion."""
    answer = _mirror_request("GET", f"/playlists/{remote_id}")
    if answer is None:
        return None
    status, body = answer
    if status == 404:
        return False
    if status != 200:
        return None
    items = _container(body).get("Metadata") or []
    return bool(items) and items[0].get("type") == "playlist"


def _uri(machine: str, keys: list[str]) -> str:
    return _LIBRARY_URI.format(machine=machine, keys=",".join(keys)) if keys else ""


def mirror_create(title: str, machine: str, keys: list[str]) -> str | None:
    """Creates the copy with these tracks, in order; its key, or None."""
    answer = _mirror_request("POST", "/playlists", {
        "type": "audio", "title": mirror_name(title), "smart": 0, "uri": _uri(machine, keys)})
    if answer is None or answer[0] != 200:
        return None
    created = _container(answer[1]).get("Metadata") or []
    return str(created[0]["ratingKey"]) if created and created[0].get("ratingKey") is not None else None


def mirror_replace(remote_id: str, machine: str, keys: list[str]) -> bool:
    """Sets the copy's tracks to `keys`, in order: a clear, then one add
    (an add appends, so it can't replace on its own)."""
    cleared = _mirror_request("DELETE", f"/playlists/{remote_id}/items")
    if cleared is None or cleared[0] != 200:
        return False
    if not keys:
        return True
    added = _mirror_request("PUT", f"/playlists/{remote_id}/items", {"uri": _uri(machine, keys)})
    return added is not None and added[0] == 200


def mirror_set_metadata(remote_id: str, title: str, summary: str) -> bool:
    answer = _mirror_request("PUT", f"/playlists/{remote_id}",
                             {"title": mirror_name(title), "summary": summary})
    return answer is not None and answer[0] == 200


def mirror_delete(remote_id: str) -> bool:
    """Deletes the copy; True once it is gone, including when it already was."""
    answer = _mirror_request("DELETE", f"/playlists/{remote_id}")
    return answer is not None and answer[0] in (200, 204, 404)
