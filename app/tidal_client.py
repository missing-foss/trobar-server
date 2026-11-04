#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct (non-Roon) Tidal playlist provider (#21), per Trobar user's own
linked Tidal account. Reads the user's playlists; the write calls at the end
exist only to keep a shared playlist's copy in another member's account.

Unlike roon_client.py/subsonic_client.py/jellyfin_client.py, this is NOT an
admin-configured shared connection: a Tidal family plan is billing-only,
each member is a fully independent Tidal account with its own login, and
Tidal's API grants access strictly per-account via OAuth (Authorization
Code + PKCE) — there is no cross-account "family admin" credential. So
every read here is scoped by a specific user's own access token.

Refresh and "use" are deliberately separate functions, not one call that
refreshes internally: Tidal's refresh grant may rotate the refresh token on
every use (OAuth 2.1 best practice), so calling refresh_access_token()
once per sync pass and reusing the resulting access_token for every
list_playlists()/get_playlist_tracks() call in that pass avoids each of
those calls invalidating the token the previous one just got — refreshing
per-call would mean the second call in a run fails against a token the
first call already rotated past. Callers persist the (possibly new)
refresh_token immediately after the one refresh_access_token() call, not
scattered across every subsequent read.

The redirect/session half of the OAuth dance (authorize_redirect,
authorize_access_token) lives in main.py via Authlib's Flask-integrated
OAuth client, since it needs the Flask session for PKCE/state — this module
only does the token refresh + authenticated API calls, plain requests,
matching this repo's other provider clients.

The OAuth flow (AUTHORIZE_URL, TOKEN_URL) and the API_BASE paths used here
— /users/me, playlist listing, and /playlists/{id}/relationships/items —
are confirmed working against a live account and a real registered app: a
full Tidal playlist sync (#21/#67) runs end-to-end in prod, a real
Authorization-Code+PKCE round-trip followed by list_playlists() +
get_playlist_tracks() landing playlists in Trobar. The refresh-token
rotation handling described above (one refresh_access_token() per pass,
persist the possibly-new refresh token immediately) is the one subtlety
that still bites if changed; the endpoint shapes are settled.
"""

import logging
import time
import uuid

import requests

AUTHORIZE_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
API_BASE = "https://openapi.tidal.com/v2"
# Space-separated per OAuth2 convention. These cover the reads; the write
# calls at the end of this module also need WRITE_SCOPE (see scopes()).
SCOPES = "user.read collection.read playlists.read"
# Requested only once the admin says their registered app has it enabled:
# Tidal refuses an authorize request asking for a scope the app lacks, so
# requesting it unconditionally would break Connect for every install whose
# app predates sharing.
WRITE_SCOPE = "playlists.write"


def scopes(write: bool) -> str:
    """The scope string to request, with WRITE_SCOPE when `write`."""
    return f"{SCOPES} {WRITE_SCOPE}" if write else SCOPES


def has_write_scope(granted: str | None) -> bool:
    """Whether a stored grant (space-separated scopes, None for a link made
    before grants were recorded) includes WRITE_SCOPE."""
    return WRITE_SCOPE in (granted or "").split()

_TIMEOUT = 15
# Tidal's /tracks?filter[id]= returns every requested track in one call;
# 20 ids/request is confirmed working (#125) and matches the items page cap.
_ID_BATCH = 20
# Cursor-loop safety valve (~10k tracks) so a misbehaving links.next can never
# spin forever.
_MAX_PAGES = 500
# #127: a Tidal-heavy sync now makes ~2·ceil(N/20) sequential calls per
# playlist (paginated items walk + batched /tracks), so a transient 429/5xx or
# connection blip on any one call is much more likely and would otherwise fail
# the whole playlist. Retry those calls (all go through _auth_get) with backoff.
_RETRY_ATTEMPTS = 4          # 1 initial try + 3 retries
_RETRY_BASE_DELAY = 1.0      # seconds; exponential per attempt, capped
_RETRY_MAX_DELAY = 30.0

_log = logging.getLogger(__name__)


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before retrying (attempt is 1-based). Honors a numeric
    Retry-After header when present, else exponential backoff — both capped."""
    if retry_after:
        try:
            return min(float(retry_after), _RETRY_MAX_DELAY)
        except ValueError:
            pass
    return min(_RETRY_BASE_DELAY * (2 ** (attempt - 1)), _RETRY_MAX_DELAY)


def _auth_get(access_token: str, url: str, params: dict | None = None) -> dict:
    """Authenticated GET returning parsed JSON, raising on HTTP error. `url`
    may be absolute or server-relative — Tidal's pagination `links.next` is a
    relative path, so it can be fed straight back in.

    #127: retries on 429, 5xx, and connection errors with backoff (honoring
    Retry-After) up to _RETRY_ATTEMPTS, so a transient rate-limit or server
    blip on one page of a long sync doesn't fail the whole playlist. Other 4xx
    raise immediately (no point retrying a 400/404); the final attempt's
    failure propagates to the caller's except."""
    if url.startswith("/"):
        url = API_BASE + url
    headers = {"Authorization": f"Bearer {access_token}"}
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        last = attempt == _RETRY_ATTEMPTS
        try:
            resp = requests.get(url, params=params or {}, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException:
            if last:
                raise
            time.sleep(_retry_delay(attempt, None))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if last:
                resp.raise_for_status()  # give up — raises HTTPError
            _log.info("Tidal %s -> %s, retry %d/%d",
                      url, resp.status_code, attempt, _RETRY_ATTEMPTS)
            time.sleep(_retry_delay(attempt, resp.headers.get("Retry-After")))
            continue
        resp.raise_for_status()  # other 4xx: raise, don't retry
        return resp.json()
    raise RuntimeError("unreachable")  # pragma: no cover


def _error_reason(exc: Exception) -> str:
    """#127: a distinguishable reason for the {"status": "error"} returns, so a
    caller/UI can tell a rate-limit from a not-found from a parse error rather
    than every failure looking identical."""
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return "rate_limited" if status == 429 else f"http_{status}"
    if isinstance(exc, requests.RequestException):
        return "network"
    return "parse_error"


class TidalAuthError(Exception):
    """Refresh token rejected (revoked at Tidal's end, expired, or the
    admin's client_id/client_secret changed) — caller's cue to clear the
    stored refresh_token and show the user "reconnect" rather than retry.
    Distinct from TidalTransientError below on purpose: this one means the
    credential itself is dead, that one means "ask again later"."""


class TidalTransientError(Exception):
    """Network failure, timeout, a 5xx, or a malformed response body while
    refreshing — nothing wrong with the stored refresh_token itself.
    Caller's cue to retry next sync, same as this module's other
    functions returning {"status": "error"} for the equivalent case;
    raised here instead only because this function's return type is a
    plain tuple, not a status dict."""


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    """Returns (access_token, refresh_token) — the second may be a new
    value (Tidal's refresh grant can rotate it) or the same one handed in;
    callers persist the returned value, never assume the input is still
    valid after this call. Raises TidalAuthError on a rejected token,
    TidalTransientError on anything else that went wrong making the call —
    the same broad (requests.RequestException, KeyError, ValueError) catch
    every other function in this module uses, just re-raised as a
    dedicated type since this one can't fold the failure into a returned
    status dict the way the others do."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret),
            timeout=_TIMEOUT,
        )
        if resp.status_code in (400, 401):
            raise TidalAuthError(f"Tidal refresh token rejected ({resp.status_code})")
        resp.raise_for_status()
        body = resp.json()
        return body["access_token"], body.get("refresh_token", refresh_token)
    except (requests.RequestException, KeyError, ValueError) as e:
        raise TidalTransientError(str(e)) from e


def get_current_user(access_token: str) -> dict:
    """{"status": "ok", "user_id": ..., "display_name": ...} or
    {"status": "error"}. Called once at link time (see main.py's
    /profile/tidal/callback) to populate the "Connected as: X" label and
    store the account id."""
    try:
        resp = requests.get(
            f"{API_BASE}/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        attrs = data.get("attributes", {})
        display_name = attrs.get("username") or attrs.get("displayName") or attrs.get("email") or data["id"]
        return {"status": "ok", "user_id": data["id"], "display_name": display_name}
    except (requests.RequestException, KeyError, ValueError):
        return {"status": "error"}


def list_playlists(access_token: str) -> dict:
    """{"status": "ok", "playlists": [{"id", "title"}, ...]} or an error
    dict — the common shape every provider client returns (#75). Each row's
    `id` is Tidal's real, stable playlist id, so it drives the sync's
    composite key (two same-named Tidal playlists coexist) and
    get_playlist_tracks() fetches by it directly.

    Uses the account's whole COLLECTION, not just owned playlists (#132):
    `/userCollectionPlaylists/me/relationships/items` also surfaces
    saved/followed playlists the account didn't create (editorial, Tidal
    mixes, playlists shared by others), matching what Roon's Tidal
    integration shows — the old owned-only `/playlists?filter[r.owners.id]=`
    missed them entirely (53 vs 20 for the reference account). Covered by the
    already-granted `collection.read` scope; no new consent needed.

    Same JSON:API relationship shape as the items endpoint: `data` holds
    {type, id} refs, the playlist objects (with attributes.name) are
    sideloaded into `included` via `include=items`, and it paginates by
    the same opaque `links.next` cursor (#126) — so, like a playlist's items,
    it must be walked to the end rather than trusting one page.

    Tidal retired the older `/userCollections/{id}/relationships/playlists`,
    which now answers 404. The new path addresses the account as `me`, and
    `collectionView=FLAT` asks for the playlists inside folders rather than
    the folders themselves."""
    try:
        playlists = []
        seen_ids: set[str] = set()
        params: dict | None = {"include": "items", "collectionView": "FLAT"}
        next_url = "/userCollectionPlaylists/me/relationships/items"
        for _ in range(_MAX_PAGES):
            body = _auth_get(access_token, next_url, params)
            included = {(i.get("type"), i.get("id")): i
                        for i in body.get("included", [])}
            refs = body.get("data", [])
            named = 0
            for ref in refs:
                if ref.get("type") != "playlists" or not ref.get("id"):
                    continue
                pid = ref["id"]
                pl = included.get(("playlists", pid))
                name = pl.get("attributes", {}).get("name") if pl else None
                if not name:
                    continue
                named += 1
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                playlists.append({"id": pid, "title": name})
            if refs and not named:
                # A page of refs none of which reads as a named playlist means
                # the answer's shape isn't the one expected. An empty "ok"
                # would tell the sync the account stopped listing every
                # playlist, and it would delete them.
                return {"status": "error", "reason": "unexpected_shape"}
            nxt = body.get("links", {}).get("next")
            if not nxt:
                break
            next_url = nxt
            params = None  # links.next already carries page[cursor] and the rest
        else:
            # #127: exhausted the valve without a final page — at Tidal's
            # default page size, which isn't known, the collection would be
            # silently truncated, so make it visible.
            _log.warning("Tidal collection: hit _MAX_PAGES=%d, "
                         "playlist list may be truncated", _MAX_PAGES)
        return {"status": "ok", "playlists": playlists}
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}


def get_playlist_tracks(title: str, source_playlist_id: str | None = None,
                        access_token: str = "", tidal_user_id: str = "") -> dict:
    """{"status": "ok", "tracks": [{"position", "artist", "title", "album"}, ...]}
    or an error dict. No `path` in each track (Tidal has no concept of a
    local file path) — matching.py falls back to its fuzzy artist/title
    heuristic, same as Roon's tracks. Fetched by `source_playlist_id` (the
    /v2/playlists id from list_playlists) directly — no title re-lookup,
    which would be ambiguous now that same-named playlists can coexist.
    `title`/`tidal_user_id` are accepted for call-shape uniformity;
    `access_token`/`source_playlist_id` are what this actually uses.

    Two Tidal-v2 realities drive the shape here, confirmed against a live
    account for #125 rather than guessed:

    * The playlist-items relationship endpoint caps at 20 items/page
      regardless of `page[size]`, and paginates by an opaque cursor in
      `links.next` (`page[number]` is silently ignored). So we follow
      `links.next` until it's absent instead of trusting a single page —
      that single-page read was silently truncating every playlist to 20.
    * Its track objects carry NO artist info (no `relationships` at all), so
      artist names come from a separate `GET /tracks?filter[id]=...&
      include=artists` batch (<=20 ids/call): each track's
      `relationships.artists.data[0]` points at a sideloaded `artists`
      object whose `attributes.name` is the artist. Reading `artists`/
      `artist` off the item attributes (the old code) always found nothing,
      so every Tidal track matched with an empty artist — i.e. never."""
    if not source_playlist_id:
        return {"status": "error", "reason": "not_found"}
    try:
        # 1) Walk every page of playlist items -> ordered track ids, with a
        #    title fallback from the items' own sideloaded track objects.
        ordered_ids: list[str] = []
        title_fallback: dict[str, str] = {}
        # The ISRC the same sideloaded track objects carry (captured shape:
        # attributes.isrc), for identity's ISRC tiers.
        isrc_by_id: dict[str, str] = {}
        params: dict | None = {"include": "items", "page[size]": "500"}
        next_url = f"/playlists/{source_playlist_id}/relationships/items"
        for _ in range(_MAX_PAGES):
            body = _auth_get(access_token, next_url, params)
            included = {(i.get("type"), i.get("id")): i
                        for i in body.get("included", [])}
            for item in body.get("data", []):
                if item.get("type") != "tracks" or not item.get("id"):
                    continue
                tid = item["id"]
                ordered_ids.append(tid)
                inc = included.get(("tracks", tid))
                if inc:
                    title_fallback[tid] = inc.get("attributes", {}).get("title", "")
                    isrc = inc.get("attributes", {}).get("isrc")
                    if isinstance(isrc, str) and isrc:
                        isrc_by_id[tid] = isrc
            nxt = body.get("links", {}).get("next")
            if not nxt:
                break
            next_url = nxt
            params = None  # links.next already carries page[cursor]/page[size]
        else:
            # #127: exhausted the valve without a final page — a >10k-track
            # playlist would be silently truncated, so make it visible.
            _log.warning("Tidal playlist %s: hit _MAX_PAGES=%d, "
                         "tracks may be truncated", source_playlist_id, _MAX_PAGES)

        # 2) Resolve artist (and authoritative title) per track id, batched.
        artist_by_id: dict[str, str] = {}
        title_by_id: dict[str, str] = {}
        unique_ids = list(dict.fromkeys(ordered_ids))
        for start in range(0, len(unique_ids), _ID_BATCH):
            chunk = unique_ids[start:start + _ID_BATCH]
            tbody = _auth_get(access_token, "/tracks",
                              {"filter[id]": ",".join(chunk), "include": "artists"})
            artists = {a.get("id"): a for a in tbody.get("included", [])
                       if a.get("type") == "artists"}
            for d in tbody.get("data", []):
                did = d.get("id")
                if not did:
                    continue
                title_by_id[did] = d.get("attributes", {}).get("title", "")
                refs = (((d.get("relationships") or {}).get("artists") or {})
                        .get("data") or [])
                name = ""
                if refs:
                    art = artists.get(refs[0].get("id"))
                    if art:
                        name = art.get("attributes", {}).get("name", "")
                artist_by_id[did] = name

        # 3) Assemble in playlist order, dropping only the truly unresolvable.
        tracks = []
        for position, tid in enumerate(ordered_ids):
            artist = artist_by_id.get(tid, "")
            track_title = title_by_id.get(tid) or title_fallback.get(tid, "")
            if not artist and not track_title:
                continue
            tracks.append({
                "position": position,
                "artist": artist,
                "title": track_title,
                "album": None,
                "isrc": isrc_by_id.get(tid),
            })
        return {"status": "ok", "tracks": tracks}
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}


# --- Writes ---------------------------------------------------------------
#
# Used only to keep a copy of one member's playlist in another member's
# account. Each needs the playlists.write scope. Built against Tidal's
# published OpenAPI document, not yet against a live account.

_JSONAPI = "application/vnd.api+json"
# Per-request caps from the published schema.
_ADD_BATCH = 50
_REMOVE_BATCH = 50
_MOVE_BATCH = 20


def _auth_send(access_token: str, method: str, path: str, body: dict | None = None) -> dict:
    """Authenticated write returning parsed JSON ({} for an empty body),
    raising on HTTP error. Retries exactly as _auth_get does. Every attempt
    carries the same Idempotency-Key, so a retry after a lost response or a
    5xx replays the first outcome instead of adding the same items twice."""
    url = API_BASE + path
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": _JSONAPI,
        "Accept": _JSONAPI,
        "Idempotency-Key": str(uuid.uuid4()),
    }
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        last = attempt == _RETRY_ATTEMPTS
        try:
            resp = requests.request(method, url, json=body, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException:
            if last:
                raise
            time.sleep(_retry_delay(attempt, None))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if last:
                resp.raise_for_status()
            _log.info("Tidal %s %s -> %s, retry %d/%d",
                      method, path, resp.status_code, attempt, _RETRY_ATTEMPTS)
            time.sleep(_retry_delay(attempt, resp.headers.get("Retry-After")))
            continue
        resp.raise_for_status()
        if not resp.content:
            return {}
        return resp.json()
    raise RuntimeError("unreachable")  # pragma: no cover


def get_playlist_items(access_token: str, playlist_id: str) -> dict:
    """{"status": "ok", "items": [{"track_id", "item_id"}, ...]} in playlist
    order, or an error dict. `item_id` names one occurrence, which is what a
    removal or a move needs: the same track can appear twice."""
    if not playlist_id:
        return {"status": "error", "reason": "not_found"}
    try:
        items = []
        params: dict | None = {"page[size]": "500"}
        next_url = f"/playlists/{playlist_id}/relationships/items"
        for _ in range(_MAX_PAGES):
            body = _auth_get(access_token, next_url, params)
            for ref in body.get("data", []):
                item_id = (ref.get("meta") or {}).get("itemId")
                if ref.get("type") != "tracks" or not ref.get("id") or not item_id:
                    continue
                items.append({"track_id": ref["id"], "item_id": item_id})
            nxt = body.get("links", {}).get("next")
            if not nxt:
                break
            next_url = nxt
            params = None
        else:
            # A truncated read would make a sync remove what it never saw.
            return {"status": "error", "reason": "truncated"}
        return {"status": "ok", "items": items}
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"status": "error", "reason": _write_side_reason(e)}


def _write_side_reason(exc: Exception) -> str:
    """_error_reason, plus "not_found" for a 404: a share sync must tell a
    playlist that no longer exists from one it couldn't reach."""
    if isinstance(exc, requests.HTTPError) and getattr(exc.response, "status_code", None) == 404:
        return "not_found"
    return _error_reason(exc)


def get_playlist(access_token: str, playlist_id: str) -> dict:
    """{"status": "ok", "name": ..., "owner_ids": [Tidal user ids]} or an
    error dict ("not_found" when the playlist is gone). A playlist in a
    collection may be one the account only follows, which it can't edit."""
    try:
        body = _auth_get(access_token, f"/playlists/{playlist_id}", {"include": "owners"})
        name = body["data"]["attributes"]["name"]
        if not isinstance(name, str):
            raise ValueError("no playlist name")
        owners = (((body["data"].get("relationships") or {}).get("owners") or {}).get("data") or [])
        owner_ids = [o["id"] for o in owners if isinstance(o, dict) and o.get("id")]
        return {"status": "ok", "name": name, "owner_ids": owner_ids}
    except (requests.RequestException, KeyError, TypeError, ValueError) as e:
        return {"status": "error", "reason": _write_side_reason(e)}


def create_playlist(access_token: str, name: str) -> dict:
    """{"status": "ok", "id": ...} or an error dict. Created UNLISTED, so the
    copy is not published on the recipient's profile."""
    try:
        body = _auth_send(access_token, "POST", "/playlists", {"data": {
            "type": "playlists",
            "attributes": {"name": name[:250], "accessType": "UNLISTED"},
        }})
        pid = body["data"]["id"]
        if not pid:
            raise ValueError("empty playlist id")
        return {"status": "ok", "id": pid}
    except (requests.RequestException, KeyError, TypeError, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}


def rename_playlist(access_token: str, playlist_id: str, name: str) -> dict:
    try:
        _auth_send(access_token, "PATCH", f"/playlists/{playlist_id}", {"data": {
            "type": "playlists", "id": playlist_id,
            "attributes": {"name": name[:250]},
        }})
        return {"status": "ok"}
    except (requests.RequestException, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}


def delete_playlist(access_token: str, playlist_id: str) -> dict:
    try:
        _auth_send(access_token, "DELETE", f"/playlists/{playlist_id}")
        return {"status": "ok"}
    except (requests.RequestException, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}


def add_tracks(access_token: str, playlist_id: str, track_ids: list[str],
               before_item_id: str | None = None) -> dict:
    """Inserts `track_ids` in order, at the end or before the occurrence
    `before_item_id`. {"status": "ok", "not_found": [track ids Tidal refused
    as unavailable]} or an error dict. Duplicates are added, not skipped: a
    copy repeats a track as often as its source does.

    A failure part-way leaves the earlier batches added; the caller re-reads
    the playlist rather than assuming either outcome."""
    not_found: list[str] = []
    try:
        for start in range(0, len(track_ids), _ADD_BATCH):
            chunk = track_ids[start:start + _ADD_BATCH]
            meta: dict = {"onDuplicates": "ADD"}
            if before_item_id:
                meta["positionBefore"] = before_item_id
            body = _auth_send(access_token, "POST", f"/playlists/{playlist_id}/relationships/items", {
                "data": [{"type": "tracks", "id": t} for t in chunk],
                "meta": meta,
            })
            for skipped in (body.get("meta") or {}).get("skipped") or []:
                if skipped.get("reason") == "NOT_FOUND" and skipped.get("id"):
                    not_found.append(skipped["id"])
        return {"status": "ok", "not_found": not_found}
    except (requests.RequestException, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}


def remove_items(access_token: str, playlist_id: str, items: list[dict]) -> dict:
    """Removes the given occurrences ({"track_id", "item_id"} as
    get_playlist_items returns them)."""
    try:
        for start in range(0, len(items), _REMOVE_BATCH):
            chunk = items[start:start + _REMOVE_BATCH]
            _auth_send(access_token, "DELETE", f"/playlists/{playlist_id}/relationships/items", {
                "data": [{"type": "tracks", "id": i["track_id"], "meta": {"itemId": i["item_id"]}}
                         for i in chunk],
            })
        return {"status": "ok"}
    except (requests.RequestException, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}


def move_items(access_token: str, playlist_id: str, items: list[dict],
               before_item_id: str) -> dict:
    """Moves the given occurrences, keeping their order, to just before the
    occurrence `before_item_id`. Batches are sent last-first, each before
    the previous batch's first item, so the moved run stays contiguous."""
    try:
        anchor = before_item_id
        batches = [items[i:i + _MOVE_BATCH] for i in range(0, len(items), _MOVE_BATCH)]
        for chunk in reversed(batches):
            _auth_send(access_token, "PATCH", f"/playlists/{playlist_id}/relationships/items", {
                "data": [{"type": "tracks", "id": i["track_id"], "meta": {"itemId": i["item_id"]}}
                         for i in chunk],
                "meta": {"positionBefore": anchor},
            })
            anchor = chunk[0]["item_id"]
        return {"status": "ok"}
    except (requests.RequestException, ValueError) as e:
        return {"status": "error", "reason": _error_reason(e)}
