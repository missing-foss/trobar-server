# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Jellyfin and Emby API clients' shared half: connectivity, playlist
browsing, users, artist-image keys and the mirror-target reads and deletes.
The two servers share their API lineage and these functions were the same
code under two names.

What differs lives in each server's own module: authentication (Jellyfin
sends a MediaBrowser Authorization header, Emby an X-Emby-Token header, and
their error bodies differ), the playlist-track fields (Jellyfin also asks for
ProviderIds, for the recording MBID), the artist-image request and the
playlist create/replace sequence. jellyfin_client.py and emby_client.py each
bind one MediaBrowserClient to their own module and export its methods under
their old names.

A method reads every module-level name through `self.m`, the server's own
module, never directly: `_get`, the constants, `_music_library_id` and the
other functions. So a test that patches `jellyfin_client._get` or
`emby_client._MIRROR_PAGE_SIZE` still reaches every caller, and each server's
own `_get()` and `_request_as()` are the ones these methods use.
"""
import logging
from collections.abc import Callable
from types import ModuleType
from typing import Any

import requests

import db
import matching
import provider_config

_log = logging.getLogger(__name__)



def copy_item(item: dict) -> dict:
    """One entry of a mirrored playlist's copy, as mirror_copies keeps it:
    the item's own Id, and its artist and title for showing."""
    return {"id": str(item["Id"]), "title": item.get("Name") or "",
            "artist": ", ".join(a for a in item.get("Artists") or [] if a)}

class MediaBrowserClient:
    def __init__(self, name: str, module: ModuleType,
                 mirror_config: Callable[[], tuple[str, str, str] | None]) -> None:
        assert name in ("jellyfin", "emby")
        # The config-key and column prefix, and provider name in status().
        self.name = name
        # Any, not ModuleType: the methods read and set the module's own names.
        self.m: Any = module
        self.mirror_config = mirror_config

    def _current_config(self) -> tuple[str, str, str]:
        # An unsaved connection the switch preview reads through: provider_config.
        unsaved = provider_config.override(self.name)
        if unsaved is not None:
            return unsaved
        conn = db.get_conn()
        try:
            url = db.get_config(conn, f"{self.name}_url") or ""
            api_key = db.get_config(conn, f"{self.name}_api_key") or ""
            user_id = db.get_config(conn, f"{self.name}_user_id") or ""
            return url, api_key, user_id
        finally:
            conn.close()

    def ensure_started(self) -> None:
        """No persistent connection to establish — kept for interface parity
        with roon_client so main.py's startup dispatch needs no special case."""
        pass

    def status(self) -> dict:
        url, api_key, user_id = self.m._current_config()
        if not url:
            return {"state": "disconnected", "url": url, "provider": self.name}
        # Confirms both API-key validity and the resolved userId still exists in
        # one call, rather than two separate checks.
        resp = self.m._get(f"/Users/{user_id}", api_key, url) if user_id else None
        if resp is not None and resp.get("Id") == user_id:
            return {"state": "paired", "url": url, "provider": self.name}
        return {"state": "disconnected", "url": url, "provider": self.name}

    def retry_pairing(self) -> dict:
        """Neither server has a Roon-style manual-approval step to retry — a
        fresh check is the whole story, so this is just status(). Kept as its own
        function purely for interface parity with roon_client.retry_pairing,
        so main.py's dispatch never needs a provider-specific branch."""
        return self.m.status()

    def _resolve_user_id(self, url: str, api_key: str, username: str) -> str:
        """Resolves `username` to the server's internal userId via GET /Users —
        playlist endpoints need that id explicitly, not just the API key.
        Shared by reconnect() (persists the result) and test_connection()
        (#509 item 3, never persists) so the resolution logic exists once."""
        users = self.m._get("/Users", api_key, url)
        if isinstance(users, list):
            match = next((u for u in users if u.get("Name") == username), None)
            if match is not None:
                return match.get("Id", "")
        return ""

    def test_connection(self, url: str, api_key: str, username: str) -> dict:
        """#509 item 3: same check as status(), against EXPLICIT credentials
        rather than the stored config — never persists anything. See
        subsonic_client.test_connection's own docstring for the full
        rationale (the admin config form's live pre-save check)."""
        user_id = self.m._resolve_user_id(url, api_key, username)
        resp = self.m._get(f"/Users/{user_id}", api_key, url) if user_id else None
        if resp is not None and resp.get("Id") == user_id:
            return {"state": "paired", "url": url, "provider": self.name}
        return {"state": "disconnected", "url": url, "provider": self.name}

    def reconnect(self, url: str, api_key: str, username: str) -> dict:
        """Admin (re)configured the connection from the web UI — persist it,
        resolve `username` to the server's internal userId (playlist
        endpoints need that id explicitly, not just the API key), and report
        whether the result checks out."""
        user_id = self.m._resolve_user_id(url, api_key, username)

        conn = db.get_conn()
        try:
            db.set_config(conn, f"{self.name}_url", url)
            db.set_config(conn, f"{self.name}_api_key", api_key)
            db.set_config(conn, f"{self.name}_username", username)
            db.set_config(conn, f"{self.name}_user_id", user_id)
            conn.commit()
        finally:
            conn.close()
        return self.m.status()

    def _list_playlist_items(self, user_id: str | None = None) -> list[dict] | None:
        """`user_id`: #262's per-Trobar-user override — pass a specific
        server userId to list THAT user's own visible playlists (their
        private ones included) instead of the server-wide configured
        default's. None (every pre-#262 call site) keeps today's behaviour."""
        url, api_key, default_user_id = self.m._current_config()
        uid = user_id or default_user_id
        if not uid:
            return None
        resp = self.m._get(f"/Users/{uid}/Items", api_key, url,
                           {"IncludeItemTypes": "Playlist", "Recursive": "true"})
        if resp is None:
            return None
        return resp.get("Items", [])

    def list_playlists(self, user_id: str | None = None) -> dict:
        """Returns {"status": "ok", "playlists": [{"id", "title"}, ...]} — the
        common shape every provider client returns (#75). Both servers expose a
        real, stable item Id, so `id` is set and drives the sync's composite
        key — two same-named playlists coexist as separate rows.

        `user_id`: see _list_playlist_items()'s own docstring — #262's
        per-Trobar-user mapping passes this for a mapped user's own listing;
        the ordinary default-account pass (main.py's active-provider dispatch)
        leaves it unset."""
        items = self.m._list_playlist_items(user_id)
        if items is None:
            return {"status": "error", "reason": "not_paired"}
        # Trobar's own mirror copies are never sources. When the mirror target
        # is this server, a copy sits in a member's account or in the mirror
        # account, and either may be the account listed here. Known by the ids
        # the sink stored: item ids are server-generated GUIDs, so one held
        # for another server can't name a playlist here.
        copies = self.m._mirror_ids()
        return {"status": "ok", "playlists": [
            {"id": i["Id"], "title": i["Name"]}
            for i in items
            if i.get("Name") and i.get("Id") and self.m._norm_id(i["Id"]) not in copies
        ]}

    def _norm_id(self, item_id: str) -> str:
        """An item id as the server spells it either way: with or without dashes,
        in either case."""
        return str(item_id).replace("-", "").lower()

    def _mirror_ids(self) -> set[str]:
        """The ids of every copy the mirror sink made, normalised."""
        conn = db.get_conn()
        try:
            return {self.m._norm_id(r[0]) for r in conn.execute(
                f"SELECT {self.name}_mirror_remote_id FROM playlists"
                f" WHERE {self.name}_mirror_remote_id IS NOT NULL")}
        finally:
            conn.close()

    def list_users(self) -> dict:
        """Returns {"status": "ok", "users": [{"id", "name"}, ...]} — every
        account on the active-provider server, for #262's Administration >
        Configuration per-Trobar-user mapping UI (same shape
        roon_client.list_profiles() already returns for the Roon equivalent).
        Not gated on this server actually being the active provider —
        harmless either way, the frontend only shows this section when it is
        configured."""
        url, api_key, _user_id = self.m._current_config()
        users = self.m._get("/Users", api_key, url)
        if not isinstance(users, list):
            return {"status": "error", "reason": "not_paired"}
        return {"status": "ok", "users": [
            {"id": u["Id"], "name": u["Name"]} for u in users if u.get("Id") and u.get("Name")
        ]}

    def _get_music_library_id(self) -> str | None:
        """Resolved once per process — the music library's own item id, needed
        to scope the artist listing below to just that library."""
        with self.m._music_library_id_lock:
            if self.m._music_library_id is None:
                url, api_key, _user_id = self.m._current_config()
                folders = self.m._get("/Library/VirtualFolders", api_key, url) or []
                music = next((f for f in folders if f.get("CollectionType") == "music"), None)
                self.m._music_library_id = music.get("ItemId", "") if music else ""
            return self.m._music_library_id or None

    def _build_artist_image_key_map(self) -> dict[str, str]:
        url, api_key, user_id = self.m._current_config()
        library_id = self.m._get_music_library_id()
        if not user_id or not library_id:
            return {}
        resp = self.m._get(f"/Users/{user_id}/Items", api_key, url,
                           {"IncludeItemTypes": "MusicArtist", "Recursive": "true",
                            "ParentId": library_id})
        if resp is None:
            return {}
        return {a["Name"]: a["Id"] for a in resp.get("Items", []) if a.get("Name") and a.get("Id")}

    def _get_artist_image_key_map(self) -> dict[str, str]:
        """Mirrors roon_client's exact caching pattern — one full artist walk
        per process lifetime, every lookup after that a plain dict get."""
        return self.m._artist_image_key_map.get(self.m._build_artist_image_key_map)

    def mirror_status(self) -> dict:
        """Same shape as status() above, for the mirror-TARGET connection —
        see db.get_mirror_jellyfin_config()'s docstring (Emby's getter is the
        same) for why this is a distinct connection from the active-provider
        one status() reports."""
        config = self.mirror_config()
        if config is None:
            return {"state": "disconnected", "url": "", "provider": self.name}
        url, api_key, user_id = config
        resp = self.m._get(f"/Users/{user_id}", api_key, url) if user_id else None
        if resp is not None and resp.get("Id") == user_id:
            return {"state": "paired", "url": url, "provider": self.name}
        return {"state": "disconnected", "url": url, "provider": self.name}

    def mirror_reconnect(self, url: str, api_key: str, username: str) -> dict:
        """Admin (re)configured the mirror-TARGET connection — persist it,
        resolve `username` to the server's internal userId (same reason
        reconnect() above needs to: playlist endpoints need that id
        explicitly), and report whether the result checks out."""
        user_id = ""
        users = self.m._get("/Users", api_key, url)
        if isinstance(users, list):
            match = next((u for u in users if u.get("Name") == username), None)
            if match is not None:
                user_id = match.get("Id", "")

        conn = db.get_conn()
        try:
            db.set_config(conn, f"mirror_{self.name}_url", url)
            db.set_config(conn, f"mirror_{self.name}_api_key", api_key)
            db.set_config(conn, f"mirror_{self.name}_username", username)
            db.set_config(conn, f"mirror_{self.name}_user_id", user_id)
            conn.commit()
        finally:
            conn.close()
        return self.m.mirror_status()

    def mirror_build_tag_index(self) -> dict[tuple[str, str, str], list[dict]] | None:
        """{(normalized artist, normalized album, normalized title): [{"id",
        "track_no"}, ...]} for the WHOLE mirror-target library, or None if not
        configured or the request failed. Same shape and same reasoning as
        subsonic_client.mirror_build_tag_index() — see that function's own
        docstring for why tags, not Path, and why every candidate for a key is
        kept rather than a single one.

        `Artists` is a list here (unlike Subsonic's single string), joined
        with ", " — the exact same convention this file's own read-side
        get_playlist_tracks() already uses for matching.py, so a track's
        identity is computed the same way whichever direction it's used.
        `IndexNumber` is the server's own track-number field.

        Paginated via StartIndex/Limit (confirmed live on both servers: a
        short final page is genuinely the end, StartIndex past the end returns
        zero items rather than erroring) — walks until a page comes back with zero items, not
        merely fewer than requested, same defensive stance as the Subsonic
        sink for a server that might clamp the page size lower than asked."""
        config = self.mirror_config()
        if config is None:
            return None
        url, api_key, user_id = config
        if not user_id:
            return None
        index: dict[tuple[str, str, str], list[dict]] = {}
        start = 0
        for _page in range(self.m._MIRROR_MAX_PAGES):
            resp = self.m._get(f"/Users/{user_id}/Items", api_key, url, {
                "IncludeItemTypes": "Audio", "Recursive": "true",
                "StartIndex": start, "Limit": self.m._MIRROR_PAGE_SIZE,
            })
            if resp is None:
                return None
            items = resp.get("Items", [])
            if not items:
                return index
            for item in items:
                if item.get("Id") is None:
                    continue
                key = (
                    matching.normalize(", ".join(item.get("Artists") or [])),
                    matching.normalize(item.get("Album") or ""),
                    matching.normalize(item.get("Name") or ""),
                )
                index.setdefault(key, []).append(
                    {"id": item["Id"], "track_no": item.get("IndexNumber")})
            start += self.m._MIRROR_PAGE_SIZE
        return None

    def _server_id(self, url: str) -> str | None:
        """A server's own unique id, from /System/Info/Public (no key needed),
        or None if it doesn't answer."""
        if not url:
            return None
        try:
            resp = requests.get(f"{url.rstrip('/')}/System/Info/Public", timeout=10)
            resp.raise_for_status()
            # Jellyfin answers in camelCase ("id") for its first seconds after
            # a start, PascalCase ("Id") after that.
            body = {k.lower(): v for k, v in resp.json().items()}
            return body.get("id") or None
        except (requests.RequestException, ValueError):
            return None

    def mirror_target_is_library_server(self) -> bool | None:
        """Whether the mirror target is the same server as the library
        connection: only then is a member's mapped user id (resolved on the
        library server) an account on the mirror target. Compared by server
        id, not URL, since two URLs can reach one server. None when it can't
        be told (a server didn't answer), which the sink must not read as "a
        different server": that would move every member's copy."""
        config = self.mirror_config()
        library_url = self.m._current_config()[0]
        if config is None or not library_url:
            return False  # no library connection: nothing was mapped on it
        library_id, mirror_id = self.m._server_id(library_url), self.m._server_id(config[0])
        if library_id is None or mirror_id is None:
            return None
        return library_id == mirror_id

    def mirror_set_playlist_metadata(self, remote_id: str, name: str, comment: str) -> None:
        """Best-effort name + subset-transparency comment. Silently no-ops on
        any failure — the song list is what write_mirror() reports errors
        for; a missing/stale name or comment is cosmetic and not worth
        failing the whole write over.

        Confirmed live there's no partial-update endpoint for this on either
        server. On Jellyfin, neither a bare `{"Name": ...}` POST to
        /Playlists/{id} nor a fuller Name+Ids+UserId body ever returned
        anything but 400 against a real 10.11.11 instance; on Emby a direct
        POST to /Playlists/{id} is a 404, that path simply doesn't exist.
        What actually works on both is the generic metadata-edit path — GET
        the playlist's own full item representation, mutate just the fields
        this cares about, POST the WHOLE thing back to /Items/{id} — the same
        round-trip the admin "Edit Metadata" UI feature itself does.
        `Overview` (normally a movie/show synopsis field) is the closest thing
        a playlist item has to Subsonic's `comment`; confirmed live it
        round-trips correctly and leaves the song list untouched.

        On Emby the rename half reliably sticks (confirmed live), but the
        comment usually does NOT for a playlist that has any items in it —
        confirmed live and reproducible: Emby schedules its own internal
        metadata-refresh pass shortly after a playlist's item list changes
        (add or replace, whether via create or a later write), and that pass
        resets `Overview` back to whatever it was before (empty, the first
        time), a few seconds later — with or without `Overview`/`Name` added
        to the item's own `LockedFields` first, which made no difference.
        Confirmed this is item-list-triggered specifically: the same call
        against a playlist created with an EMPTY id list keeps its comment
        with no reversion. There's no known way from this API to suppress
        that refresh pass, so this is accepted as an Emby-side limitation
        already covered by this function's own best-effort contract above,
        not a bug to chase further here."""
        config = self.mirror_config()
        if config is None:
            return
        url, api_key, user_id = config
        if not user_id:
            return
        item = self.m._get(f"/Users/{user_id}/Items/{remote_id}", api_key, url)
        if item is None:
            return
        item["Name"] = name
        item["Overview"] = comment
        self.m._request_as("POST", f"/Items/{remote_id}", api_key, url, json_body=item)

    def mirror_delete_playlist(self, remote_id: str) -> bool:
        """True on a confirmed-ok delete. False otherwise (not configured, or
        the request failed) — the caller decides what that means for its own
        stored state."""
        config = self.mirror_config()
        if config is None:
            return False
        url, api_key, _user_id = config
        status, _ = self.m._request_as("DELETE", f"/Items/{remote_id}", api_key, url)
        return status is not None and status < 400
