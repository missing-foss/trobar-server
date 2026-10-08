#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Music Assistant API client: connectivity, playlists, artist images, and
the write side of the Music Assistant mirroring sink.

Three roles over one admin-configured connection:
- a library provider, selectable in main.py's single-active _PROVIDERS;
- an EXTRA playlist source while another provider is active, merged in the
  way filesystem .m3u discovery and the Tidal/Spotify accounts are, so
  someone running Roon for the library and Music Assistant for playback
  gets both sets of playlists (playlist_sync skips that merge while Music
  Assistant is the active provider);
- a mirroring sink (mirror_music_assistant.py), through the mirror_*()
  functions at the end of this module.

Admin-configured: the server URL plus a long-lived access token. Music
Assistant 2.x gates its whole API behind its own users, so unlike LMS the
token is required, not optional.

Confirmed live against a real Music Assistant 2.9.9 (the official
ghcr.io/music-assistant/server image) with a local-files provider over a
small tagged library and a hand-built playlist; 2.9.9 is the minimum
version verified:
- Every call is `POST {base}/api` with body `{"command": <name>, "args":
  {...}}` and header `Authorization: Bearer <token>`. That plain-HTTP
  route runs the same commands as the WebSocket API, so no WebSocket
  client is needed. A missing or unknown token answers 401.
- Tokens: the first admin is created at `/setup`; a long-lived token is
  then minted with the `auth/token/create` command (argument `name`). It
  is valid for one year and does not renew itself on use. The session
  tokens a login returns do renew on use, but only up to 90 days from
  creation, so they are the wrong thing to store here. (2.9.9's profile
  page says long-lived tokens last ten years; the server sets one.)
- `music/playlists/library_items` with `limit`/`offset` → a list of
  playlist objects `{"item_id", "name", "uri", "is_editable", "owner",
  "provider_mappings": [{"provider_domain", "item_id", ...}], ...}`,
  paginated. `item_id` is the library id, stable across renames.
- The list always includes Music Assistant's own generated playlists
  (random tracks, random album/artist, infinite mix, recently added and
  played, all favourites): provider domain `builtin`, `is_editable`
  false. They are views, not playlists anyone made, and are skipped. A
  playlist the user created in Music Assistant is also `builtin` but
  editable.
- `music/playlists/playlist_tracks` with `item_id` (the library id) and
  `provider_instance_id_or_domain: "library"` → the whole playlist in one
  response, each entry `{"media_type", "position", "name", "artists":
  [{"name"}, ...], "album": {"name"} | null, "duration",
  "provider_mappings": [...]}`.
- A track from a local-files provider carries `provider_domain`
  `filesystem_local` (`filesystem_smb` for the network-share variant) and,
  as the mapping's `item_id`, its path RELATIVE to that provider's music
  folder, e.g. `Artist/Album (2022)/01 - Title.flac`. When Music Assistant
  reads the same share as MUSIC_ROOT, that is exactly tracks.relative_path,
  and matching.py's trailing-segment comparison tolerates a different
  folder depth on either side.
- Anything else (a streaming provider's track, a URL added as a track)
  carries no path: domain `builtin` with the URL as `item_id`, or the
  streaming provider's own id. Those fall back to artist/title matching.
- The declared return type also allows radio stations, podcast episodes
  and audiobooks. Only `media_type == "track"` entries are kept; the rest
  are dropped before matching, so they are not counted as misses.

Measured live for the provider and sink roles, against the same 2.9.9:
- Playlists are server-wide: every Music Assistant account sees and can
  edit every playlist (`owner` is "Music Assistant" whoever made it). A
  token from a non-admin (`role: user`) account can create playlists and
  add or remove their tracks, but deleting a playlist
  (`music/playlists/remove`) and renaming one (`music/playlists/update`)
  answer 403 "Admin access required".
- `music/tracks/library_items` (limit/offset; a page past the end is an
  empty list) lists library tracks with `artists`, `album`,
  `track_number` and the same `provider_mappings` paths as above.
- `music/playlists/create_playlist {name}` returns the playlist, whose
  `item_id` is the library id.
- `music/playlists/add_playlist_tracks {db_playlist_id, uris}` and
  `music/playlists/remove_playlist_tracks {db_playlist_id,
  positions_to_remove}` return a background task. `tasks/get {task_id}`
  reports it; it finishes as `success`, "Added N item(s)", even when items
  were refused, which only its `logs` mention ("Can't add <uri> to
  playlist - item not found"). Only reading the playlist back tells what
  it holds.
- `positions_to_remove` is 1-based, as the read-back's `position` is.
  Position 0 removes the LAST entry.
- A playlist holds a track at most once: adding one already there, even
  twice in one call, adds nothing.
- A deleted or unknown playlist id answers 500 to `playlist_tracks` and
  `music/playlists/get`, while an add to it is still accepted as a task.
- Images: `metadata.images[]` entries carry a `proxy_id`, and
  `GET {base}/imageproxy/<proxy_id>` returns the image (Music Assistant
  serves it without authentication). `music/artists/library_items
  {search}` finds an artist by name.
"""

import time

import requests

import db
import matching
import mirror

_PAGE = 500
# A backstop against a server that ignores `offset` and returns the same
# full page forever; far above any real playlist count.
_MAX_PAGES = 100
# The same backstop for the track listing the mirroring sink walks: a
# million tracks.
_MAX_TRACK_PAGES = 2000
_FILE_DOMAINS = ("filesystem_",)


def _current_config() -> tuple[str, str]:
    conn = db.get_conn()
    try:
        url = db.get_config(conn, "music_assistant_url") or ""
        token = db.get_config(conn, "music_assistant_token") or ""
        return url, token
    finally:
        conn.close()


def _command_status(command: str, args: dict, url: str, token: str) -> tuple[int | None, object]:
    """One API command: (HTTP status, parsed body). The status is None when
    not configured or when no response came back (network error,
    unparseable body). For the callers that must tell a 403 or a 500 from
    other failures."""
    if not url or not token:
        return None, None
    try:
        resp = requests.post(
            f"{url.rstrip('/')}/api",
            json={"command": command, "args": args},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except requests.RequestException:
        return None, None
    try:
        return resp.status_code, resp.json()
    except ValueError:
        # Errors come back as plain text ("Admin access required",
        # "Internal server error"): the status is still the answer.
        return resp.status_code, None


def _command(command: str, args: dict, url: str, token: str):
    """One API command. Returns the parsed result, or None when not
    configured or when the request failed (network error, non-2xx
    including 401 for a bad token, unparseable body)."""
    status, body = _command_status(command, args, url, token)
    return body if status is not None and status < 400 else None


def _check(url: str, token: str) -> dict:
    # A cheap authenticated command: a count comes back as a bare integer.
    result = _command("music/playlists/count", {}, url, token)
    state = "paired" if isinstance(result, int) else "disconnected"
    return {"state": state, "url": url, "provider": "music_assistant"}


def ensure_started() -> None:
    """No persistent connection — kept for interface parity with the other
    clients."""
    pass


def status() -> dict:
    url, token = _current_config()
    return _check(url, token)


def mirror_status() -> dict:
    """status(), for the mirror target: the same one connection
    (db.get_mirror_music_assistant_config)."""
    return status()


def retry_pairing() -> dict:
    """No pairing step to retry: a fresh check is the whole story, as for
    LMS. For main.py's active-provider dispatch."""
    return status()


def test_connection(url: str, token: str) -> dict:
    """Same check as status(), against EXPLICIT settings rather than the
    stored config — never persists anything."""
    return _check(url, token)


def reconnect(url: str, token: str) -> dict:
    """Admin (re)configured the connection — persist it and report whether
    it checks out. Blank values disconnect."""
    conn = db.get_conn()
    try:
        db.set_config(conn, "music_assistant_url", url or None)
        db.set_config(conn, "music_assistant_token", token or None)
        conn.commit()
    finally:
        conn.close()
    return status()


def _is_generated(playlist: dict) -> bool:
    mappings = playlist.get("provider_mappings") or []
    return (not playlist.get("is_editable")
            and bool(mappings)
            and all(m.get("provider_domain") == "builtin" for m in mappings))


def is_mirror_copy(name: str | None, item_id: str | None, mirror_ids: set[str]) -> bool:
    """A playlist the mirroring sink manages: one whose id a Trobar
    playlist stores as its Music Assistant mirror, or whose name carries
    the mirror suffix (mirror_name). Never read back in as a source
    playlist, or each sync would import Trobar's own copies (the loop
    mirror.py's docstring describes for the filesystem sink)."""
    return (item_id is not None and str(item_id) in mirror_ids) \
        or (name or "").endswith(mirror.MIRROR_SUFFIX)


def _mirror_ids() -> set[str]:
    conn = db.get_conn()
    try:
        return {r[0] for r in conn.execute(
            "SELECT music_assistant_mirror_remote_id FROM playlists "
            "WHERE music_assistant_mirror_remote_id IS NOT NULL")}
    finally:
        conn.close()


def list_playlists() -> dict:
    """Returns {"status": "ok", "playlists": [{"id", "title"}, ...]}, the
    common shape every provider client returns, or an error when not
    configured or unreachable — which the sync treats as "this source has
    nothing this run", never as "every playlist was deleted". Leaves out
    the mirroring sink's own copies (is_mirror_copy)."""
    url, token = _current_config()
    mirror_ids = _mirror_ids()
    playlists: list[dict] = []
    for page in range(_MAX_PAGES):
        items = _command("music/playlists/library_items",
                         {"limit": _PAGE, "offset": page * _PAGE}, url, token)
        if not isinstance(items, list):
            return {"status": "error", "reason": "not_paired"}
        playlists += [
            {"id": str(p["item_id"]), "title": p["name"]}
            for p in items
            if p.get("name") and p.get("item_id") is not None and not _is_generated(p)
            and not is_mirror_copy(p.get("name"), p.get("item_id"), mirror_ids)
        ]
        if len(items) < _PAGE:
            break
    return {"status": "ok", "playlists": playlists}


def _file_path(entry: dict) -> str | None:
    for m in entry.get("provider_mappings") or []:
        if (m.get("provider_domain") or "").startswith(_FILE_DOMAINS) and m.get("item_id"):
            return m["item_id"]
    return None


def get_playlist_tracks(playlist_title: str, source_playlist_id: str | None = None) -> dict:
    """Tracks of one playlist, in order, as {"position", "title", "artist",
    "path", "album"}. `path` is set only for local-files tracks (see the
    module docstring); everything else matches by artist and title.

    Fetched by `source_playlist_id` (the library id from list_playlists);
    falls back to a title lookup only when no id is given."""
    url, token = _current_config()

    if source_playlist_id is None:
        listed = list_playlists()
        if listed["status"] != "ok":
            return {"status": "error", "reason": "not_paired"}
        match = next((p for p in listed["playlists"] if p["title"] == playlist_title), None)
        if match is None:
            return {"status": "not_found", "failed_segment": playlist_title}
        source_playlist_id = match["id"]

    entries = _command("music/playlists/playlist_tracks",
                       {"item_id": source_playlist_id, "provider_instance_id_or_domain": "library"},
                       url, token)
    if not isinstance(entries, list):
        return {"status": "error", "reason": "playlist items fetch failed"}

    tracks = [
        {
            "position": i,
            "title": e.get("name", ""),
            "artist": ", ".join(a["name"] for a in e.get("artists") or [] if a.get("name")),
            "path": _file_path(e),
            "album": (e.get("album") or {}).get("name"),
        }
        for i, e in enumerate(x for x in entries if x.get("media_type") == "track")
    ]
    return {"status": "ok", "playlist": playlist_title, "tracks": tracks}


def get_artist_image(artist_name: str) -> tuple[bytes, str] | None:
    """(bytes, content type) for the library artist named `artist_name`
    (case aside), from the first image in its metadata, or None. A miss
    just means no picture from here; artist_images.py falls back as usual.
    Music Assistant has artist images only where its metadata found some;
    a local library without album-artist tags often has none."""
    url, token = _current_config()
    found = _command("music/artists/library_items", {"search": artist_name, "limit": 10}, url, token)
    if not isinstance(found, list):
        return None
    wanted = artist_name.casefold()
    for artist in found:
        if (artist.get("name") or "").casefold() != wanted:
            continue
        for image in ((artist.get("metadata") or {}).get("images") or []):
            proxy_id = image.get("proxy_id")
            if not proxy_id:
                continue
            try:
                resp = requests.get(f"{url.rstrip('/')}/imageproxy/{proxy_id}", timeout=15)
            except requests.RequestException:
                return None
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if resp.ok and content_type.startswith("image/") and resp.content:
                return resp.content, content_type
            return None
    return None


# --- The mirroring sink's write side (mirror_music_assistant.py) ---

# How long the sink waits for one add/remove background task: a few
# thousand tracks finished in well under a second against 2.9.9.
_TASK_TIMEOUT = 60.0
_TASK_POLL = 0.25
_TASK_DONE = ("success", "error", "failed", "cancelled")


def mirror_name(title: str) -> str:
    """The name a mirrored playlist gets in Music Assistant: the title plus
    mirror.MIRROR_SUFFIX, the filesystem sink's marker, so it reads as
    Trobar's copy and is never imported back (is_mirror_copy)."""
    return f"{title}{mirror.MIRROR_SUFFIX}"


def mirror_build_index() -> dict | None:
    """The target library's file-backed tracks, for resolving Trobar tracks
    to Music Assistant ones: {"by_path": {relative path: entry},
    "by_tags": {(artist, album, title) normalized: [entry, ...]}}, each
    entry {"uri", "path", "track_no"}. None when not configured or a page
    failed. Only tracks with a local-files path are indexed, since a write
    is verified by path on read-back."""
    url, token = _current_config()
    if not url or not token:
        return None
    by_path: dict[str, dict] = {}
    by_tags: dict[tuple[str, str, str], list[dict]] = {}
    for page in range(_MAX_TRACK_PAGES):
        items = _command("music/tracks/library_items",
                         {"limit": _PAGE, "offset": page * _PAGE}, url, token)
        if not isinstance(items, list):
            return None
        for t in items:
            path = _file_path(t)
            if not path or not t.get("uri"):
                continue
            entry = {"uri": t["uri"], "path": path, "track_no": t.get("track_number")}
            by_path.setdefault(path, entry)
            artist = ", ".join(a["name"] for a in t.get("artists") or [] if a.get("name"))
            key = (matching.normalize(artist), matching.normalize((t.get("album") or {}).get("name") or ""),
                   matching.normalize(t.get("name") or ""))
            by_tags.setdefault(key, []).append(entry)
        if len(items) < _PAGE:
            return {"by_path": by_path, "by_tags": by_tags}
    return None


def _wait_task(task: object, url: str, token: str) -> bool:
    """Waits for a background task to finish. True when it ended (whatever
    it says about the items: see the module docstring), False when it
    can't be followed or doesn't finish in time."""
    if not isinstance(task, dict) or not task.get("id"):
        return False
    deadline = time.monotonic() + _TASK_TIMEOUT
    while time.monotonic() < deadline:
        state = _command("tasks/get", {"task_id": task["id"]}, url, token)
        if isinstance(state, dict) and state.get("status") in _TASK_DONE:
            return True
        time.sleep(_TASK_POLL)
    return False


def mirror_read(remote_id: str) -> list[str] | None:
    """The paths of a mirrored playlist's entries, in order, or None when it
    can't be read: gone (500), or the server unreachable."""
    url, token = _current_config()
    entries = _command("music/playlists/playlist_tracks",
                       {"item_id": str(remote_id), "provider_instance_id_or_domain": "library"}, url, token)
    if not isinstance(entries, list):
        return None
    return [_file_path(e) or e.get("uri") or "" for e in entries]


def mirror_listed(remote_id: str) -> bool | None:
    """Whether Music Assistant still lists the playlist `remote_id`: True,
    False (absent from the complete listing), or None when the listing
    can't be read. A failed read of a copy is ambiguous, since a deleted
    playlist and a passing server error both answer 500; this tells them
    apart before a copy is given up on."""
    url, token = _current_config()
    for page in range(_MAX_PAGES):
        items = _command("music/playlists/library_items",
                         {"limit": _PAGE, "offset": page * _PAGE}, url, token)
        if not isinstance(items, list):
            return None
        if any(str(p.get("item_id")) == str(remote_id) for p in items):
            return True
        if len(items) < _PAGE:
            return False
    return None


def mirror_create(title: str) -> str | None:
    """Creates an empty mirror playlist; its library id, or None."""
    url, token = _current_config()
    created = _command("music/playlists/create_playlist", {"name": mirror_name(title)}, url, token)
    if isinstance(created, dict) and created.get("item_id") is not None:
        return str(created["item_id"])
    return None


def mirror_replace(remote_id: str, current_count: int, uris: list[str]) -> bool:
    """Replaces a mirror playlist's entries with `uris`, in order: removes
    every current entry (positions 1..current_count), then adds. True when
    both tasks finished; the caller confirms the result by reading back."""
    url, token = _current_config()
    if current_count:
        task = _command("music/playlists/remove_playlist_tracks",
                        {"db_playlist_id": str(remote_id),
                         "positions_to_remove": list(range(1, current_count + 1))}, url, token)
        if not _wait_task(task, url, token):
            return False
    if uris:
        task = _command("music/playlists/add_playlist_tracks",
                        {"db_playlist_id": str(remote_id), "uris": uris}, url, token)
        if not _wait_task(task, url, token):
            return False
    return True


def mirror_delete(remote_id: str) -> str:
    """Deletes a mirror playlist: "deleted"; "emptied" when the token's
    account isn't an admin, which can't delete (403), so its entries are
    removed instead and the empty playlist stays; or "failed"."""
    url, token = _current_config()
    status, _body = _command_status("music/playlists/remove", {"item_id": str(remote_id)}, url, token)
    if status is not None and status < 400:
        return "deleted"
    if status == 403:
        current = mirror_read(remote_id)
        if current is not None and mirror_replace(remote_id, len(current), []):
            return "emptied"
    return "failed"
