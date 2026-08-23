#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Devices, selections, and the device_track_state recompute.

Core invariant (see the approved plan): device_track_state is a union over
all of a device's assigned selections, deduplicated by track_id — not by
selection. A track already `downloaded` and still required stays untouched
even if a second selection (e.g. a playlist sharing a track with an already-
synced album) also requires it. A track no longer required by *any* assigned
selection is marked `removed` so the device is told to delete it.

A 4th status, `excluded`, covers a track the user deliberately
deleted on-device and chose not to resync: recompute_device_state treats it
exactly like `pending`/`downloaded` — untouched while still required, so it's
never silently re-queued — but it still falls out of the required set the
normal way (-> `removed`) if a later selection change actually drops it.
"""

import hashlib
import json
import re
import secrets
import sqlite3
from typing import TypedDict

import transcode


class AutofitSummary(TypedDict):
    albums: int
    tracks: int
    bytes: int
    budget_bytes: int
    used_by_manual_bytes: int
    reason: str | None
    percent: int


def _new_id(cur: sqlite3.Cursor) -> int:
    """cur.lastrowid is Optional per sqlite3's stub (None before any INSERT
    has ever run on the cursor), but always set right after one — narrow it
    once here instead of asserting at each call site."""
    assert cur.lastrowid is not None
    return cur.lastrowid


def hash_token(raw_token: str) -> str:
    # SHA-256 (not a slow/salted KDF) is deliberate, never a human password.
    # Device tokens and #446's API tokens are secrets.token_urlsafe(32) --
    # 2^256 keyspace, nothing to brute-force, so a slow KDF (which exists to
    # make guessing a *low-entropy* secret expensive) buys nothing here and
    # would slow every device sync request, which authenticates by this
    # hash on every call. Enrollment codes are the one caller this doesn't
    # apply to -- 8 chars from a 31-symbol alphabet (~2^40, meant to be
    # hand-typed) -- but those are protected structurally instead: a
    # 10-minute TTL, single-use, purge-on-mint (see ENROLLMENT_TTL_SECONDS
    # below), so a stolen database holds at most a couple of live codes with
    # minutes left, and an attacker with database access can mint their own
    # grant anyway, making the hash moot at that point. See SECURITY.md
    # ("device tokens are secrets.token_urlsafe(32) stored only as
    # SHA-256") -- passwords use Werkzeug's PBKDF hash instead (main.py).
    #
    # CodeQL's py/weak-sensitive-data-hashing flags every call site here
    # regardless (its "password" classification is a shape heuristic, not a
    # real flow) -- dismissed on GitHub's code-scanning alerts with this
    # same reasoning, matching the existing won't-fix precedent for the
    # same rule on subsonic_client.py. (Inline `lgtm[...]` suppression
    # comments are LGTM.com's retired syntax and are NOT honoured by
    # GitHub code scanning -- don't rely on one appearing to work here.)
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _lowest_transcode_tier() -> str:
    """The lowest available bitrate tier. Derived from transcode.BITRATES
    (parsing "128k" etc. back to a number) rather than hardcoded, so a
    future lower tier — if one's ever added — is picked up automatically
    without a second change here."""
    return min(transcode.BITRATES, key=lambda fmt: int(transcode.BITRATES[fmt].rstrip("k")))


def create_device(conn: sqlite3.Connection, owner_user_id: int, name: str,
                   device_type: str = "phone", max_size_bytes: int | None = None, *,
                   transcode_format: str | None = None,
                   artist_images: str | None = None) -> tuple[int, str]:
    """Returns (device_id, raw_token). The raw token is shown to the user
    exactly once (QR code / config download) — only its hash is stored.

    transcode_format/artist_images (#97) are honoured at create time so an API
    client can pair and configure in one call, matching what PATCH already
    accepts; the caller is responsible for validating them first.

    #221: a watch device that doesn't specify a format defaults to the
    lowest tier rather than staying on "original" — Connect IQ watches
    can't decode lossless audio at all, and the failure mode when left
    unset isn't an obvious error, it's a stuck/incomplete sync. An
    explicit value from the client (if one's ever sent) still wins."""
    if transcode_format is None and device_type == "watch":
        transcode_format = _lowest_transcode_tier()
    raw_token = secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO devices (owner_user_id, name, device_type, api_token_hash, "
        "max_size_bytes, transcode_format, artist_images) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (owner_user_id, name, device_type, hash_token(raw_token), max_size_bytes,
         transcode_format, artist_images),
    )
    conn.commit()
    return _new_id(cur), raw_token


def regenerate_token(conn: sqlite3.Connection, device_id: int) -> str:
    """Issues a brand-new token for an existing device (invalidating the
    old one) — used when the QR/token needs to be shown again (lost,
    app reinstalled) since the raw token is never stored, only its hash."""
    raw_token = secrets.token_urlsafe(32)
    conn.execute(
        "UPDATE devices SET api_token_hash = ? WHERE id = ?",
        (hash_token(raw_token), device_id),
    )
    conn.commit()
    return raw_token


def authenticate_device(conn: sqlite3.Connection, raw_token: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM devices WHERE api_token_hash = ?", (hash_token(raw_token),)
    ).fetchone()
    if row is not None:
        conn.execute("UPDATE devices SET last_seen_at = datetime('now') WHERE id = ?", (row["id"],))
        conn.commit()
    return row


# #163: enrollment grants. Unambiguous alphabet (no 0/O/1/I/L); 8 chars from 31
# symbols is ~40 bits — plenty for a single-use code that expires in minutes and
# is rate-limited.
_ENROLL_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ENROLLMENT_TTL_SECONDS = 600  # 10 minutes


def _generate_enrollment_code() -> str:
    return "".join(secrets.choice(_ENROLL_ALPHABET) for _ in range(8))


def create_enrollment_grant(conn: sqlite3.Connection, owner_user_id: int) -> str:
    """#163: mint a short-lived, single-use, owner-scoped enrollment code. The
    web UI presents it as a QR + human code; a mobile client redeems it via
    redeem_enrollment_grant to create its own device — the app never holds user
    credentials (#162). Returns the raw code; only its hash is stored."""
    # #166: purge-on-mint. This ephemeral table (10-min TTL, single-use) is only
    # ever appended to and marked consumed, never cleaned — so drop the dead rows
    # (expired, or already redeemed) before adding a new one. Self-limiting, no
    # scheduler; committed atomically with the INSERT below. Redeem stays correct
    # regardless (it already filters consumed_at IS NULL AND expires_at > now).
    conn.execute(
        "DELETE FROM enrollment_grants "
        "WHERE expires_at < datetime('now') OR consumed_at IS NOT NULL")
    for _ in range(5):  # retry on the astronomically-unlikely code collision
        code = _generate_enrollment_code()
        try:
            conn.execute(
                "INSERT INTO enrollment_grants (code_hash, owner_user_id, expires_at) "
                "VALUES (?, ?, datetime('now', ?))",
                (hash_token(code), owner_user_id, f"+{ENROLLMENT_TTL_SECONDS} seconds"),
            )
            conn.commit()
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not mint a unique enrollment code")


def redeem_enrollment_grant(conn: sqlite3.Connection, code: str, name: str,
                            device_type: str, max_size_bytes: int | None,
                            transcode_format: str | None = None) -> tuple[int, str] | None:
    """#163: atomically consume a valid (unexpired, unconsumed) enrollment code
    and create the device owned by the grant's user, returning (device_id,
    raw_token). Returns None if the code is invalid / expired / already used.
    The consume is a single UPDATE guarded on `consumed_at IS NULL AND
    expires_at > now`, so two racing redemptions can't both succeed."""
    code_hash = hash_token(code)
    cur = conn.execute(
        "UPDATE enrollment_grants SET consumed_at = datetime('now') "
        "WHERE code_hash = ? AND consumed_at IS NULL AND expires_at > datetime('now')",
        (code_hash,),
    )
    if cur.rowcount != 1:
        conn.commit()
        return None
    owner = conn.execute(
        "SELECT owner_user_id FROM enrollment_grants WHERE code_hash = ?", (code_hash,)
    ).fetchone()["owner_user_id"]
    device_id, raw_token = create_device(
        conn, owner, name, device_type, max_size_bytes, transcode_format=transcode_format)
    conn.commit()
    return device_id, raw_token


# #446/#474: admin-minted Bearer tokens for external integrations -- see
# db.py's integration_tokens comment for why this is one table/one
# credential rather than a read-only/action split. hash_token() (SHA-256,
# defined above) is reused as-is; these differ from device tokens only in
# what they're allowed to authenticate (see main.py's
# _authenticated_integration_token and the /api/integrations/* routes it
# guards, never get_current_user_id).

def create_integration_token(conn: sqlite3.Connection, owner_user_id: int, name: str) -> tuple[int, str]:
    """Returns (token_id, raw_token). The raw token is shown to the owner
    exactly once, at creation — only its hash is stored. Caller (main.py's
    api_integration_tokens) is responsible for checking the owner is an
    admin before calling this — not re-checked here, same division of
    responsibility as every other route-level authorization check in this
    file."""
    raw_token = secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO integration_tokens (owner_user_id, name, token_hash) VALUES (?, ?, ?)",
        (owner_user_id, name, hash_token(raw_token)),
    )
    conn.commit()
    return _new_id(cur), raw_token


def authenticate_integration_token(conn: sqlite3.Connection, raw_token: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM integration_tokens WHERE token_hash = ?", (hash_token(raw_token),)
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE integration_tokens SET last_used_at = datetime('now') WHERE id = ?", (row["id"],))
        conn.commit()
    return row


def list_integration_tokens(conn: sqlite3.Connection, owner_user_id: int) -> list[sqlite3.Row]:
    """Never includes the raw token or its hash — only what the owner needs
    to recognise and manage an existing grant (#446: "the revoke path
    matters more than usual")."""
    return conn.execute(
        "SELECT id, name, created_at, last_used_at FROM integration_tokens "
        "WHERE owner_user_id = ? ORDER BY created_at",
        (owner_user_id,),
    ).fetchall()


def revoke_integration_token(conn: sqlite3.Connection, owner_user_id: int, token_id: int) -> bool:
    """Scoped to owner_user_id in the DELETE itself (not a separate
    ownership check before it) so one caller can never revoke another
    user's token by guessing its id. Returns False if no row matched --
    already revoked, or never this owner's."""
    cur = conn.execute(
        "DELETE FROM integration_tokens WHERE id = ? AND owner_user_id = ?", (token_id, owner_user_id))
    conn.commit()
    return cur.rowcount > 0


def create_selection(conn: sqlite3.Connection, sel_type: str, target: str,
                      created_by_user_id: int, device_ids: list[int], *,
                      commit: bool = True) -> int:
    """`target` convention: artist name for type='artist'; 'Artist||Album' for
    type='album'; playlists.id (as str) for type='playlist'; tracks.id
    (as str) for type='track'.

    Find-or-create on (type, target, created_by_user_id) — a repeat call for
    a target the user already selected (double-click, retried request) joins
    device_ids onto the existing row instead of creating a duplicate
    (unguarded duplicate rows once broke the Selections matrix UI's
    `x-for :key="row.target"`). Matches the unique index in db.py and the
    same find-or-create shape toggle_selection_device already used.

    #351: `commit=False` lets a caller doing several of these as one
    all-or-nothing operation (the basket's fan-out) defer every commit to a
    single one of its own at the end — the default stays True so every other
    existing call site is unaffected."""
    existing = conn.execute(
        "SELECT id FROM selections WHERE type=? AND target=? AND created_by_user_id=?",
        (sel_type, target, created_by_user_id),
    ).fetchone()
    if existing is not None:
        selection_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO selections (type, target, created_by_user_id) VALUES (?, ?, ?)",
            (sel_type, target, created_by_user_id),
        )
        selection_id = _new_id(cur)
    for device_id in device_ids:
        conn.execute(
            "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
            (selection_id, device_id),
        )
    if commit:
        conn.commit()
    for device_id in device_ids:
        recompute_device_state(conn, device_id, commit=commit)
    return selection_id


def toggle_selection_device(conn: sqlite3.Connection, user_id: int, sel_type: str,
                             target: str, device_id: int, checked: bool) -> int | None:
    """Add/remove one device from a `sel_type` selection for `target`
    ("Artist||Album" for albums, an artist name, or a playlist id), creating
    the selection on first check and leaving it in place (just device-less)
    on a last uncheck rather than deleting it — used by the Selections
    matrix views (albums/artists/playlists), where every cell is independent
    of how the selection happened to get created. Returns the selection id,
    or None if there was nothing to do (unchecking when no selection exists)."""
    row = conn.execute(
        "SELECT id FROM selections WHERE type=? AND target=? AND created_by_user_id=?",
        (sel_type, target, user_id),
    ).fetchone()

    if row is None:
        if not checked:
            return None
        return create_selection(conn, sel_type, target, user_id, [device_id])

    selection_id = row["id"]
    if checked:
        conn.execute(
            "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
            (selection_id, device_id),
        )
    else:
        conn.execute(
            "DELETE FROM selection_devices WHERE selection_id=? AND device_id=?",
            (selection_id, device_id),
        )
    conn.commit()
    recompute_device_state(conn, device_id)
    return selection_id


def delete_selection(conn: sqlite3.Connection, selection_id: int) -> None:
    affected = [row["device_id"] for row in conn.execute(
        "SELECT device_id FROM selection_devices WHERE selection_id = ?", (selection_id,)
    )]
    conn.execute("DELETE FROM selections WHERE id = ?", (selection_id,))
    conn.commit()
    for device_id in affected:
        recompute_device_state(conn, device_id)


def parse_target_id(v: str | None) -> int | None:
    """#434: strict on purpose -- ids are always positive integers, so a
    strict parse loses nothing legitimate. Plain `int()` accepts things no
    client ever sends (leading/trailing whitespace, PEP-515 underscore
    separators like '1_0' -> 10, non-ASCII digits like the Arabic-Indic
    '١٠'), each of which disagreed with SQLite's `CAST(? AS INTEGER)` (which
    takes only a leading-integer prefix) at main.py's
    _require_playlist_visible() -- the gate and this reader parsing the same
    string differently meant the object authorized wasn't always the object
    served. `isascii()` matters alongside `isdigit()`: non-ASCII digit
    strings are `isdigit()`-true and `int()` parses them, so without the
    ASCII guard the strict version would still admit a form no client
    sends."""
    s = "" if v is None else str(v)
    return int(s) if s.isascii() and s.isdigit() else None


def list_basket(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """#413: enriched with a resolved display title -- and, for playlists,
    the source provider for an icon -- so the panel doesn't have to guess
    from the raw target string. That guess (`'||' in target`) is exactly how
    a basket'd playlist ended up rendering as a bare row id: target is
    String(p.id) for that type, and nothing else identifies it.

    #501: also enriched with `device_ids` -- the sorted list of devices
    this item is currently staged against -- so the basket panel can group
    items into per-device sections and api_basket_fan_out can filter each
    device's own section. Batched (one IN query), same reasoning as the
    playlist/track lookups below: a basket can hold several items, and
    each one needs this, not just the ones of one type.

    Playlist/track lookups are batched (one IN query each) rather than N+1,
    since a basket can hold several of either. `missing` is true only when
    a playlist/track target no longer resolves (deleted after being
    basketed) -- artist/album targets have no equivalent existence check,
    so they're never flagged missing here."""
    rows = conn.execute(
        "SELECT id, type, target, added_at FROM basket_items "
        "WHERE user_id = ? ORDER BY added_at, id", (user_id,),
    ).fetchall()

    item_ids = [r["id"] for r in rows]
    device_ids_by_item: dict[int, list[int]] = {item_id: [] for item_id in item_ids}
    if item_ids:
        placeholders = ",".join("?" * len(item_ids))
        for link in conn.execute(
            f"SELECT basket_item_id, device_id FROM basket_item_devices "
            f"WHERE basket_item_id IN ({placeholders}) ORDER BY device_id",
            tuple(item_ids),
        ):
            device_ids_by_item[link["basket_item_id"]].append(link["device_id"])

    # #424: a target that isn't numeric (a malformed POST /api/basket, or a
    # hand-edited DB -- #352 validates the type, not the target's format)
    # used to raise here and 500 the whole endpoint, taking the panel and
    # its own Clear button down with it -- the one unrecoverable-basket path
    # #414/#419 didn't already close. parse_target_id() skips it when building
    # the id sets; the per-row loop below then falls into the same
    # already-missing=true handling a deleted playlist/track gets.
    playlist_ids = {pid for r in rows if r["type"] == "playlist"
                     and (pid := parse_target_id(r["target"])) is not None}
    playlists: dict[int, sqlite3.Row] = {}
    if playlist_ids:
        placeholders = ",".join("?" * len(playlist_ids))
        for p in conn.execute(
            f"SELECT id, title, source_provider FROM playlists WHERE id IN ({placeholders})",
            tuple(playlist_ids),
        ):
            playlists[p["id"]] = p

    track_ids = {tid for r in rows if r["type"] == "track"
                 and (tid := parse_target_id(r["target"])) is not None}
    tracks: dict[int, sqlite3.Row] = {}
    if track_ids:
        placeholders = ",".join("?" * len(track_ids))
        for t in conn.execute(
            f"SELECT id, title FROM tracks WHERE id IN ({placeholders})",
            tuple(track_ids),
        ):
            tracks[t["id"]] = t

    items = []
    for r in rows:
        item = dict(r)
        item["device_ids"] = device_ids_by_item[r["id"]]
        item["source_provider"] = None
        item["missing"] = False
        if r["type"] == "artist":
            item["title"] = r["target"]
        elif r["type"] == "album":
            _artist, _, album = r["target"].partition("||")
            item["title"] = album
        elif r["type"] == "playlist":
            pid = parse_target_id(r["target"])
            p = playlists.get(pid) if pid is not None else None
            item["title"] = p["title"] if p else None
            item["source_provider"] = p["source_provider"] if p else None
            item["missing"] = p is None
        elif r["type"] == "track":
            tid = parse_target_id(r["target"])
            t = tracks.get(tid) if tid is not None else None
            item["title"] = t["title"] if t else None
            item["missing"] = t is None
        else:
            # Pre-#352 invalid type, or a hand-edited DB -- fall back to the
            # raw target rather than crashing on an unrecognized type.
            item["title"] = r["target"]
        items.append(item)
    return items


def add_basket_item(conn: sqlite3.Connection, user_id: int, item_type: str, target: str,
                     device_ids: list[int]) -> int:
    """Find-or-create on (user_id, type, target) — matches the basket_items
    unique index, so adding an item already in the basket (from the same
    surface, or a different one that shares it) joins `device_ids` onto the
    existing row instead of creating a duplicate — same find-or-create shape
    create_selection() already uses for `selections`/`selection_devices`.

    #501: `device_ids` is the destination(s) this item is staged against,
    chosen in the device picker at add time — required and non-empty (the
    caller validates that; see api_basket()'s own check, matching
    api_basket_fan_out's)."""
    existing = conn.execute(
        "SELECT id FROM basket_items WHERE user_id=? AND type=? AND target=?",
        (user_id, item_type, target),
    ).fetchone()
    if existing is not None:
        item_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO basket_items (user_id, type, target) VALUES (?, ?, ?)",
            (user_id, item_type, target),
        )
        item_id = _new_id(cur)
    for device_id in device_ids:
        conn.execute(
            "INSERT OR IGNORE INTO basket_item_devices (basket_item_id, device_id) VALUES (?, ?)",
            (item_id, device_id),
        )
    conn.commit()
    return item_id


def unstage_basket_item_device(conn: sqlite3.Connection, user_id: int, item_id: int,
                                device_id: int, *, commit: bool = True) -> None:
    """#501: removes one device from one item's staging — the per-device
    basket panel's own per-section × (unstage this item from just THIS
    device's section, leaving it staged for any others). Ownership-scoped
    via user_id in the same query, matching remove_basket_item's own style,
    rather than a separate existence check first.

    If that was the item's last remaining device link, the basket_items row
    itself is deleted too — see this table's own SCHEMA comment for why a
    device-less basket item can't be left lying around the way a
    device-less `selections` row deliberately is.

    `commit=False` lets api_basket_fan_out call this once per (item,
    device) pair it just sent, as part of its own single all-or-nothing
    transaction — same convention create_selection's own commit flag
    uses."""
    conn.execute(
        "DELETE FROM basket_item_devices WHERE device_id = ? AND basket_item_id IN "
        "(SELECT id FROM basket_items WHERE id = ? AND user_id = ?)",
        (device_id, item_id, user_id),
    )
    remaining = conn.execute(
        "SELECT 1 FROM basket_item_devices WHERE basket_item_id = ?", (item_id,)
    ).fetchone()
    if remaining is None:
        conn.execute("DELETE FROM basket_items WHERE id = ? AND user_id = ?", (item_id, user_id))
    if commit:
        conn.commit()


def remove_basket_item(conn: sqlite3.Connection, user_id: int, item_id: int) -> None:
    conn.execute("DELETE FROM basket_items WHERE id = ? AND user_id = ?", (item_id, user_id))
    conn.commit()


def clear_basket(conn: sqlite3.Connection, user_id: int, *, commit: bool = True) -> None:
    conn.execute("DELETE FROM basket_items WHERE user_id = ?", (user_id,))
    if commit:
        conn.commit()


def _resolve_selection_track_ids(conn: sqlite3.Connection, sel_type: str, target: str,
                                 selection_id: int | None = None) -> set[int]:
    if sel_type == "autofit":
        # A materialized set frozen at refresh time — resolved by
        # selection id, not target, since the chosen tracks live in autofit_tracks.
        rows = conn.execute(
            "SELECT track_id AS id FROM autofit_tracks WHERE selection_id = ?", (selection_id,)
        )
        return {row["id"] for row in rows}
    if sel_type == "artist":
        rows = conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND artist = ?", (target,)
        )
    elif sel_type == "album":
        artist, _, album = target.partition("||")
        rows = conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND artist = ? AND album = ?",
            (artist, album),
        )
    elif sel_type == "playlist":
        rows = conn.execute(
            "SELECT matched_track_id AS id FROM playlist_tracks "
            "WHERE playlist_id = ? AND matched_track_id IS NOT NULL",
            (int(target),),
        )
    elif sel_type == "track":
        rows = conn.execute(
            "SELECT id FROM tracks WHERE deleted_at IS NULL AND id = ?", (int(target),)
        )
    else:
        return set()
    return {row["id"] for row in rows}


def required_track_ids_for_device(conn: sqlite3.Connection, device_id: int,
                                  exclude_autofit: bool = False) -> set[int]:
    required: set[int] = set()
    sql = (
        "SELECT s.id, s.type, s.target FROM selections s "
        "JOIN selection_devices sd ON sd.selection_id = s.id "
        "WHERE sd.device_id = ?"
    )
    if exclude_autofit:
        sql += " AND s.type != 'autofit'"
    for row in conn.execute(sql, (device_id,)):
        required |= _resolve_selection_track_ids(conn, row["type"], row["target"], row["id"])
    return required


# --- Auto-fit selections -----------------------------------------
# An auto-fit selection fills a device's *remaining* storage budget (its
# max_size_bytes, minus what its manual selections already need) with the
# device owner's most-played albums, ranked by Last.fm. It's materialized on
# demand: refresh_autofit() freezes the chosen tracks in autofit_tracks so the
# synced set is stable between refreshes and eviction only happens on a refresh.

DEFAULT_AUTOFIT_PERIOD = "6month"


def autofit_selection_id_for_device(conn: sqlite3.Connection, device_id: int) -> int | None:
    row = conn.execute(
        "SELECT s.id FROM selections s JOIN selection_devices sd ON sd.selection_id = s.id "
        "WHERE s.type = 'autofit' AND sd.device_id = ?",
        (device_id,),
    ).fetchone()
    return row["id"] if row else None


def create_autofit_selection(conn: sqlite3.Connection, device_id: int, user_id: int,
                             period: str = DEFAULT_AUTOFIT_PERIOD) -> int:
    """Create the device's single auto-fit selection (caller should refresh it
    afterwards to materialize tracks). The Last.fm period is stored in target."""
    cur = conn.execute(
        "INSERT INTO selections (type, target, created_by_user_id) VALUES ('autofit', ?, ?)",
        (period, user_id),
    )
    selection_id = _new_id(cur)
    conn.execute(
        "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
        (selection_id, device_id),
    )
    conn.commit()
    return selection_id


def sync_status(conn: sqlite3.Connection, device_id: int) -> dict:
    """Cheap, trustworthy sync status for a device, derived straight from
    device_track_state — for the devices list (trobar-server#229).
    Deliberately not based on devices.last_seen_at: that column bumps on
    *every* authenticated device call (a plain "any changes?" check that
    finds none included), so it can't tell "just finished syncing" from
    "asked and there was nothing to do". 'pending' rows are still queued;
    anything else (downloaded/removed/excluded) has already been acted on,
    so the most recent such row's updated_at is the real last-synced time.
    last_synced_at is None if the device has never had anything to sync."""
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count, "
        "MAX(CASE WHEN status != 'pending' THEN updated_at END) AS last_synced_at "
        "FROM device_track_state WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    return {
        "pending_count": row["pending_count"] or 0,
        "last_synced_at": row["last_synced_at"],
    }


def autofit_status(conn: sqlite3.Connection, device_id: int) -> dict:
    """Cheap (no Last.fm) status of a device's auto-fit from the materialized
    set — for the devices list. bytes/tracks reflect the last refresh.
    `percent` (#217) is read regardless of enabled state, so the owner's
    chosen fill target is visible/adjustable even before first enabling."""
    dev_row = conn.execute(
        "SELECT transcode_format, autofit_percent FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    percent = dev_row["autofit_percent"]
    selection_id = autofit_selection_id_for_device(conn, device_id)
    if selection_id is None:
        return {"enabled": False, "percent": percent}
    row = conn.execute("SELECT target FROM selections WHERE id = ?", (selection_id,)).fetchone()
    track_ids = {r["track_id"] for r in conn.execute(
        "SELECT track_id FROM autofit_tracks WHERE selection_id = ?", (selection_id,)
    )}
    albums = conn.execute(
        "SELECT COUNT(DISTINCT t.artist || '||' || t.album) AS n FROM autofit_tracks a "
        "JOIN tracks t ON t.id = a.track_id WHERE a.selection_id = ?", (selection_id,)
    ).fetchone()["n"]
    fmt = dev_row["transcode_format"]
    return {
        "enabled": True,
        "percent": percent,
        "period": row["target"] if row else DEFAULT_AUTOFIT_PERIOD,
        "albums": albums,
        "tracks": len(track_ids),
        "bytes": _sum_device_bytes(conn, track_ids, device_id, fmt),
    }


def _sum_track_sizes(conn: sqlite3.Connection, track_ids: set[int]) -> int:
    if not track_ids:
        return 0
    placeholders = ",".join("?" * len(track_ids))
    row = conn.execute(
        f"SELECT COALESCE(SUM(size), 0) AS total FROM tracks WHERE id IN ({placeholders})",
        tuple(track_ids),
    ).fetchone()
    return row["total"] or 0


# what a track will actually occupy on a transcoding device:
# CBR MP3 = bitrate/8 bytes per second of audio, plus slack for ID3 headers
# and embedded art. Capped at the original size (a transcode is never bigger
# in practice; if the estimate says otherwise the duration is suspect).
# Tracks without a duration fall back to the original size — a safe
# overestimate.
_TRANSCODE_BYTES_PER_SEC = {
    "mp3_320": 40_000, "mp3_256": 32_000, "mp3_192": 24_000, "mp3_128": 16_000,
}
_EST_SLACK_BYTES = 256 * 1024


def _device_size_estimate(size: int, relative_path: str, duration: float | None,
                          transcode_format: str | None) -> int:
    bps = _TRANSCODE_BYTES_PER_SEC.get(transcode_format or "")
    if (bps and duration
            and _source_ext(relative_path).lower() in _LOSSLESS_EXTS):
        return min(size, int(duration * bps) + _EST_SLACK_BYTES)
    return size


def _sum_device_bytes(conn: sqlite3.Connection, track_ids: set[int],
                      device_id: int, transcode_format: str | None) -> int:
    """Bytes these tracks occupy (or will occupy) on this device: real
    reported bytes where a client has acked, MP3-320 estimate for
    still-pending lossless tracks on a transcoding device, original size
    otherwise."""
    if not track_ids:
        return 0
    placeholders = ",".join("?" * len(track_ids))
    total = 0
    for row in conn.execute(
        f"SELECT t.size, t.relative_path, t.duration, dts.bytes_on_device "
        f"FROM tracks t LEFT JOIN device_track_state dts "
        f"ON dts.track_id = t.id AND dts.device_id = ? "
        f"WHERE t.id IN ({placeholders})",
        (device_id, *track_ids),
    ):
        total += row["bytes_on_device"] if row["bytes_on_device"] is not None else \
            _device_size_estimate(row["size"], row["relative_path"],
                                  row["duration"], transcode_format)
    return total


def refresh_autofit(conn: sqlite3.Connection, selection_id: int,
                    ranked_album_keys: list[tuple[str, str]]) -> AutofitSummary:
    """Re-materialize an auto-fit selection. `ranked_album_keys` is a list of
    (artist_lower, album_lower) in descending play-count order (the caller
    builds it from Last.fm). Greedily fits whole albums — highest-played first,
    skipping any that don't fit so smaller lower-ranked albums can still slot in
    — into the device's remaining budget, then freezes the chosen tracks in
    autofit_tracks. Returns a summary dict; `reason` is set when nothing could
    be fitted.

    #217: `devices.autofit_percent` (default 100, i.e. "fill it all" — no
    behaviour change for existing devices) caps auto-fit's own share of the
    device at a percentage of `max_size_bytes`, not of whatever's left after
    manual selections — a percentage of a shifting remainder wouldn't deliver
    the "leave headroom on the device" the cap is for; add a manual selection
    and the reserved space would silently move. So `budget_bytes` below is
    the *capped* ceiling (max_size_bytes * percent / 100), and manual
    selections are still subtracted from that, never from the full device
    size — this is the "auto-fit's own share" budget, not the device's."""
    conn.execute("DELETE FROM autofit_tracks WHERE selection_id = ?", (selection_id,))
    summary: AutofitSummary = {
        "albums": 0, "tracks": 0, "bytes": 0, "budget_bytes": 0,
        "used_by_manual_bytes": 0, "reason": None, "percent": 100,
    }

    dev = conn.execute(
        "SELECT device_id FROM selection_devices WHERE selection_id = ?", (selection_id,)
    ).fetchone()
    if dev is None:
        conn.commit()
        summary["reason"] = "no_device"
        return summary
    device_id = dev["device_id"]

    dev_row = conn.execute(
        "SELECT max_size_bytes, transcode_format, autofit_percent FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    max_size_bytes = dev_row["max_size_bytes"]
    fmt = dev_row["transcode_format"]
    percent = dev_row["autofit_percent"]
    summary["percent"] = percent
    if not max_size_bytes:
        conn.commit()
        summary["reason"] = "no_size_limit"  # can't auto-fit without a device budget
        return summary
    budget = max_size_bytes * percent // 100
    summary["budget_bytes"] = budget

    manual_ids = required_track_ids_for_device(conn, device_id, exclude_autofit=True)
    manual_bytes = _sum_device_bytes(conn, manual_ids, device_id, fmt)
    summary["used_by_manual_bytes"] = manual_bytes
    remaining = budget - manual_bytes
    if remaining <= 0:
        conn.commit()
        summary["reason"] = "budget_full"
        return summary

    # Library index: (artist_lower, album_lower) -> [(track_id, size), ...]
    # — on a transcoding device `size` is the on-device
    # estimate, so a 128 GB card fits ~3x more FLAC-sourced albums as MP3.
    library: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row in conn.execute(
        "SELECT id, artist, album, relative_path, duration, COALESCE(size, 0) AS size "
        "FROM tracks WHERE deleted_at IS NULL"
    ):
        est = _device_size_estimate(row["size"], row["relative_path"], row["duration"], fmt)
        library.setdefault((row["artist"].lower(), row["album"].lower()), []).append(
            (row["id"], est)
        )

    chosen: list[int] = []
    used = 0
    for key in ranked_album_keys:
        tracks = library.get(key)
        if not tracks:
            continue  # scrobbled album not in the library
        album_ids = [tid for tid, _ in tracks]
        if all(tid in manual_ids for tid in album_ids):
            continue  # already fully covered by a manual selection
        album_size = sum(sz for _, sz in tracks)
        if used + album_size > remaining:
            continue  # doesn't fit — try smaller lower-ranked albums
        chosen.extend(album_ids)
        used += album_size
        summary["albums"] += 1

    for track_id in chosen:
        conn.execute(
            "INSERT OR IGNORE INTO autofit_tracks (selection_id, track_id) VALUES (?, ?)",
            (selection_id, track_id),
        )
    conn.commit()
    summary["tracks"] = len(chosen)
    summary["bytes"] = used
    return summary


def autofit_fill_basis(conn: sqlite3.Connection, device_id: int) -> dict | None:
    """#217: the percent-*independent* inputs to the live GB/track-count
    preview in the Devices panel — max_size_bytes, manual_bytes, and the
    per-device average track-size estimate. None of these depend on the
    candidate fill percentage the owner is dragging the slider to, so this
    is computed once (when the panel opens the auto-fit mini-panel, not per
    drag) and the cheap `remaining = max_size*pct/100 - manual; tracks =
    remaining/avg` arithmetic happens client-side from there — no per-drag
    round trip, no debounce needed, and no repeated full-library scan.
    Touches no state (no autofit_tracks write), unlike refresh_autofit.
    Returns None if the device doesn't exist.

    The track-count estimate is necessarily approximate: auto-fit packs whole
    albums, so the real result lands under the target rather than on it, and
    it's an average over the whole library rather than the ranked candidate
    pool a real refresh would use (that ranking needs a Last.fm call, too
    slow for the live preview). transcode_format dominates the average — at
    mp3_128 a device holds roughly 2.5x the tracks it holds at Originals —
    so the average is computed per-device, never a global constant."""
    dev_row = conn.execute(
        "SELECT max_size_bytes, transcode_format FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    if dev_row is None:
        return None
    max_size_bytes = dev_row["max_size_bytes"]
    fmt = dev_row["transcode_format"]
    if not max_size_bytes:
        return {"max_size_bytes": 0, "manual_bytes": 0, "avg_track_bytes": 0}

    manual_ids = required_track_ids_for_device(conn, device_id, exclude_autofit=True)
    manual_bytes = _sum_device_bytes(conn, manual_ids, device_id, fmt)

    total_size = 0
    total_count = 0
    for row in conn.execute(
        "SELECT relative_path, duration, COALESCE(size, 0) AS size FROM tracks WHERE deleted_at IS NULL"
    ):
        total_size += _device_size_estimate(row["size"], row["relative_path"], row["duration"], fmt)
        total_count += 1
    avg_track_bytes = (total_size // total_count) if total_count else 0

    return {
        "max_size_bytes": max_size_bytes,
        "manual_bytes": manual_bytes,
        "avg_track_bytes": avg_track_bytes,
    }


def transfer_device(conn: sqlite3.Connection, old_device_id: int, new_device_id: int, *,
                     assume_present: bool = False) -> dict:
    """#440: "new_device_id replaces old_device_id" -- moves everything the
    old device holds onto the new one, then deletes the old device.

    Settings (transcode_format, max_size_bytes, autofit_percent,
    artist_images, source_of_truth) are copied from old to new BEFORE
    device_track_state is reassigned. Once the new device's transcode
    format matches the old one's, device_path() resolves identically on
    both, so a transferred 'downloaded' status -- when assume_present says
    to keep one -- is still true: the file the old device fetched under
    that format is exactly what the new device would fetch too. Skipping
    this ordering (or not copying transcode_format at all) would carry
    "downloaded" over to a device that names the file differently, telling
    the server a file is present under a name that doesn't exist on it.

    assume_present (default False, per PR #442 review): whether the new
    device is assumed to already hold what the old one had (a cloned card,
    a restored backup) rather than being blank. False is the safe default
    -- transferred 'downloaded' rows land as 'pending' instead, so the new
    device re-fetches them. The unsafe alternative (carrying 'downloaded'
    over unconditionally) self-corrects on Android/desktop, which report
    locally-missing files back via /api/device/missing-tracks -- but
    Garmin has no such path (no manifest, no missing-tracks report, no
    provenance store: #38), so a blank watch told its tracks are already
    'downloaded' would sync "successfully" while staying permanently
    empty, with nothing on either side able to notice or correct it. A
    caller certain the new device genuinely already holds the content
    passes assume_present=True to skip the redundant re-fetch.

    Any device_track_state the new device already had (it may have paired
    and synced before the transfer ran) is overwritten for tracks the old
    device covered -- that's the whole point, this device now stands in
    for the old one. But if copying settings actually changes the new
    device's OWN transcode_format, its pre-existing 'downloaded' rows for
    tracks the OLD device did NOT have are now stale under the previous
    extension -- same fix a plain PATCH format change already applies
    (see api_device_update), reused here for the same reason.

    Selections assigned to the old device move to the new one (INSERT OR
    IGNORE, so a selection already assigned to both collapses to one row
    instead of a UNIQUE violation) -- otherwise the replacement device
    would sync nothing going forward, a silent half-transfer.

    Caller is responsible for the permission check (touches two devices,
    see _require_device_access at both call sites) and for ensuring
    old_device_id != new_device_id."""
    old = conn.execute(
        "SELECT name, transcode_format, max_size_bytes, autofit_percent, artist_images, source_of_truth "
        "FROM devices WHERE id = ?", (old_device_id,)
    ).fetchone()
    new_prev_transcode = conn.execute(
        "SELECT transcode_format FROM devices WHERE id = ?", (new_device_id,)
    ).fetchone()["transcode_format"]

    conn.execute(
        "UPDATE devices SET transcode_format=?, max_size_bytes=?, autofit_percent=?, "
        "artist_images=?, source_of_truth=? WHERE id=?",
        (old["transcode_format"], old["max_size_bytes"], old["autofit_percent"],
         old["artist_images"], old["source_of_truth"], new_device_id),
    )
    if old["transcode_format"] != new_prev_transcode:
        conn.execute(
            "UPDATE device_track_state SET status='pending', bytes_on_device=NULL, "
            "updated_at=datetime('now') WHERE device_id=? AND status='downloaded'",
            (new_device_id,),
        )

    track_count = conn.execute(
        "SELECT COUNT(*) AS n FROM device_track_state WHERE device_id = ?", (old_device_id,)
    ).fetchone()["n"]
    # assume_present=False downgrades a transferred 'downloaded' to
    # 'pending' (and drops the now-stale bytes_on_device) right in the
    # same INSERT -- everything else (pending/excluded/removed) passes
    # through untouched either way, since only 'downloaded' is a presence
    # claim worth doubting.
    status_expr = "status" if assume_present else "CASE WHEN status='downloaded' THEN 'pending' ELSE status END"
    bytes_expr = ("bytes_on_device" if assume_present
                  else "CASE WHEN status='downloaded' THEN NULL ELSE bytes_on_device END")
    conn.execute(
        f"INSERT OR REPLACE INTO device_track_state (device_id, track_id, status, updated_at, bytes_on_device) "
        f"SELECT ?, track_id, {status_expr}, updated_at, {bytes_expr} FROM device_track_state WHERE device_id = ?",
        (new_device_id, old_device_id),
    )

    selection_ids = [r["selection_id"] for r in conn.execute(
        "SELECT selection_id FROM selection_devices WHERE device_id = ?", (old_device_id,)
    )]
    for selection_id in selection_ids:
        conn.execute(
            "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
            (selection_id, new_device_id),
        )
    conn.execute("DELETE FROM selection_devices WHERE device_id = ?", (old_device_id,))

    # Cascades cleanup of anything left tied to old_device_id (its own
    # device_track_state rows -- the source rows the INSERT OR REPLACE
    # above read from, never touched directly -- plus device_unknown_tracks
    # and device_pins, neither of which has anywhere meaningful to move to).
    conn.execute("DELETE FROM devices WHERE id = ?", (old_device_id,))
    conn.commit()

    recompute_device_state(conn, new_device_id)

    return {"old_device_name": old["name"], "tracks": track_count, "selections": len(selection_ids)}


def recompute_device_state(conn: sqlite3.Connection, device_id: int, *,
                            commit: bool = True) -> None:
    required = required_track_ids_for_device(conn, device_id)

    existing = {
        row["track_id"]: row["status"]
        for row in conn.execute(
            "SELECT track_id, status FROM device_track_state WHERE device_id = ?", (device_id,)
        )
    }

    for track_id in required:
        status = existing.get(track_id)
        if status is None:
            conn.execute(
                "INSERT INTO device_track_state (device_id, track_id, status) VALUES (?, ?, 'pending')",
                (device_id, track_id),
            )
        elif status == "removed":
            conn.execute(
                "UPDATE device_track_state SET status='pending', updated_at=datetime('now') "
                "WHERE device_id=? AND track_id=?",
                (device_id, track_id),
            )
        # status already 'pending', 'downloaded', or 'excluded' and still
        # required: untouched, never re-queued.

    # #63: a 'device'-sourced device is never told to delete a track just
    # because no selection currently requires it — the server can still ADD
    # (the required-tracks loop above is untouched) but won't prune, so the
    # device survives a server-DB loss. 'server' (the default) prunes as usual;
    # flipping back to 'server' re-marks the now-unrequired tracks 'removed' on
    # the next recompute. `excluded` and an already-`removed` track are left
    # alone either way.
    row = conn.execute(
        "SELECT source_of_truth FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    if row is None or row["source_of_truth"] != "device":
        for track_id, status in existing.items():
            if track_id not in required and status != "removed":
                conn.execute(
                    "UPDATE device_track_state SET status='removed', updated_at=datetime('now') "
                    "WHERE device_id=? AND track_id=?",
                    (device_id, track_id),
                )

    if commit:
        conn.commit()


def record_device_manifest(conn: sqlite3.Connection, device_id: int, paths: list) -> dict:
    """#63 recovery path: a re-paired device uploads the relative paths it
    already holds; match them against the (non-deleted) library and mark the
    matched tracks 'downloaded' so they aren't re-fetched after a server-DB
    loss. Returns {"matched": n, "unmatched": n} — unmatched = paths not in the
    library (content the server doesn't know about), also stored on the device
    row for the web UI to surface. Idempotent (re-uploading is safe).

    Ordering is the CLIENT's responsibility (the enrollment wizard, #162): this
    only helps if source_of_truth='device' (#155) is already set — otherwise the
    next recompute prunes the tracks it just marked 'downloaded'. This function
    is deliberately agnostic to that; it assumes the caller sequenced it.

    Contract: `paths` are the wire/on-disk paths the device actually downloaded
    — i.e. device_path() form (Artiste/Album/[CDn-]NN - Titre.ext, transcoded
    extension included), NOT the catalog's tracks.relative_path. get_changes
    only ever hands the device a device_path(), so a reinstalled device walking its
    own SAF folder can produce nothing else; matching against relative_path (the
    source layout — disc subfolders, "(year)" suffixes) would match ~nothing,
    and never for a transcoding device (.mp3 on disk vs .flac source). So we
    rebuild each live track's device_path() with THIS device's transcode_format
    and match on that. Duplicate paths are de-duplicated, so the returned counts
    reflect distinct paths."""
    seen_paths = list(dict.fromkeys(paths))  # dedup, preserve order
    fmt_row = conn.execute(
        "SELECT transcode_format FROM devices WHERE id = ?", (device_id,)).fetchone()
    fmt = fmt_row["transcode_format"] if fmt_row is not None else None
    # Rebuild device-path -> track_id over the live library in the same wire
    # form get_changes emits, so an on-disk path the device uploads matches
    # exactly (extension and all).
    by_device_path: dict[str, int] = {}
    for row in conn.execute(
        "SELECT id, artist, album, title, track_no, disc_no, relative_path "
        "FROM tracks WHERE deleted_at IS NULL"
    ):
        by_device_path[device_path(row, fmt)] = row["id"]
    matched_track_ids: list[int] = [
        by_device_path[p] for p in seen_paths if p in by_device_path]
    for track_id in matched_track_ids:
        conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status) "
            "VALUES (?, ?, 'downloaded') "
            "ON CONFLICT(device_id, track_id) DO UPDATE SET "
            "status='downloaded', updated_at=datetime('now')",
            (device_id, track_id),
        )
    # #161: persist the unmatched device paths as reviewable "device extras",
    # not just a count — so the owner can see WHAT they are and adopt them.
    unmatched_paths = [p for p in seen_paths if p not in by_device_path]
    _record_device_unknown_tracks(conn, device_id, unmatched_paths)
    conn.commit()
    return {"matched": len(matched_track_ids), "unmatched": len(unmatched_paths)}


def _parse_device_path(path: str) -> tuple[str | None, str | None, str | None]:
    """#161: best-effort (artist, album, title) from a _device_path
    (Artiste/Album/[CDn-]NN - Titre.ext), for displaying an unknown track in the
    web UI. Any field may be None when the shape doesn't fit — the raw path is
    always kept alongside, so a bad parse just means less-pretty display."""
    parts = path.split("/")
    if len(parts) < 3:
        return (None, None, None)
    stem = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
    m = re.match(r"^(?:CD\d+-)?\d+ - (.+)$", stem)  # drop a leading [CDn-]NN - prefix
    title = m.group(1) if m else stem
    return (parts[0] or None, parts[-2] or None, title or None)


def _record_device_unknown_tracks(conn: sqlite3.Connection, device_id: int,
                                  unmatched_paths: list) -> None:
    """#161: replace this device's unknown set with the current unmatched paths,
    preserving the `adopted` flag on paths that persist; paths that now match (or
    left the device) are dropped. Then refresh devices.unknown_track_count to the
    NON-adopted count — an adopted extra is acknowledged and no longer flagged."""
    keep = list(dict.fromkeys(unmatched_paths))  # dedup, preserve order
    if keep:
        placeholders = ",".join("?" * len(keep))
        conn.execute(
            f"DELETE FROM device_unknown_tracks WHERE device_id = ? AND path NOT IN ({placeholders})",
            (device_id, *keep),
        )
    else:
        conn.execute("DELETE FROM device_unknown_tracks WHERE device_id = ?", (device_id,))
    for path in keep:
        artist, album, title = _parse_device_path(path)
        conn.execute(
            "INSERT INTO device_unknown_tracks (device_id, path, artist, album, title) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(device_id, path) DO NOTHING",
            (device_id, path, artist, album, title),
        )
    _refresh_unknown_track_count(conn, device_id)


def _refresh_unknown_track_count(conn: sqlite3.Connection, device_id: int) -> int:
    """Set devices.unknown_track_count to the count of NON-adopted unknowns (the
    ones still flagged for review). Returns that count."""
    count = conn.execute(
        "SELECT COUNT(*) FROM device_unknown_tracks WHERE device_id = ? AND adopted = 0",
        (device_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE devices SET unknown_track_count = ? WHERE id = ?", (count, device_id))
    return count


def list_device_unknown_tracks(conn: sqlite3.Connection, device_id: int) -> list:
    """#161: the device's unknown extras for the web review list — un-adopted
    first, then by parsed artist/album/title."""
    rows = conn.execute(
        "SELECT path, artist, album, title, adopted, first_seen_at "
        "FROM device_unknown_tracks WHERE device_id = ? "
        "ORDER BY adopted, artist, album, title, path",
        (device_id,),
    ).fetchall()
    return [{**dict(r), "adopted": bool(r["adopted"])} for r in rows]


def set_device_unknown_adopted(conn: sqlite3.Connection, device_id: int,
                               paths: list, adopted: bool) -> int:
    """#161: mark the given device paths adopted (acknowledged as device-owned
    extras the server records but never manages) or un-adopted, then refresh the
    flagged count. Returns the resulting unknown_track_count (non-adopted)."""
    seen = list(dict.fromkeys(paths))
    if seen:
        placeholders = ",".join("?" * len(seen))
        conn.execute(
            f"UPDATE device_unknown_tracks SET adopted = ? "
            f"WHERE device_id = ? AND path IN ({placeholders})",
            (1 if adopted else 0, device_id, *seen),
        )
    count = _refresh_unknown_track_count(conn, device_id)
    conn.commit()
    return count


def record_unresolved_playlist_tracks(conn: sqlite3.Connection, playlist_id: int,
                                      unresolved: list) -> None:
    """#200: replace this playlist's unresolved-track review rows with the
    current sync's misses (called from playlist_sync._sync_one_playlist,
    once per playlist, with a list of {"position", "artist", "title",
    "album", "isrc"} dicts for entries identity.py's resolver couldn't
    match). Preserves `excluded`/`first_seen_at` for any entry that
    persists across the resync — same "acknowledgment survives a resync"
    shape as _record_device_unknown_tracks above, needed for the same
    reason: playlist_tracks itself is fully DELETE+recreated every sync
    (see _sync_one_playlist), so there's nowhere else to keep an "I've seen
    this, stop flagging it" choice.

    Identity is (artist, title, album), normalized NULL->'' — see
    idx_unresolved_playlist_tracks_identity in db.py for why the
    normalization matters. A playlist entry has no better stable id: most
    providers give no path, and `position` shifts on reorder/insert.

    The preserve-set DELETE below uses json_each() with ONE bound
    parameter, not an OR-chain with one term per row: a naive
    "(artist=? AND title=? AND album=?) OR (...) OR ..." expression tree
    hits SQLite's SQLITE_MAX_EXPR_DEPTH (default 1000) once a single
    playlist has >=1000 unresolved tracks — not a theoretical edge case,
    since "provider connected before the first library scan" or a large
    #189 golden playlist synced against a partially-covering library both
    put every entry in that state. json_extract(), not the shorter ->>
    operator, for compatibility with older SQLite builds (->> needs
    >=3.38)."""
    seen: set[tuple[str, str, str]] = set()
    deduped: list[tuple[int, str, str, str, str | None]] = []
    for e in unresolved:
        artist, title, album = e.get("artist") or "", e.get("title") or "", e.get("album") or ""
        key = (artist, title, album)
        if key in seen:
            continue  # two genuinely-identical misses in one playlist -> one review row
        seen.add(key)
        deduped.append((e.get("position", 0), artist, title, album, e.get("isrc")))

    if deduped:
        keep_json = json.dumps([[artist, title, album] for _pos, artist, title, album, _isrc in deduped])
        conn.execute(
            "DELETE FROM unresolved_playlist_tracks WHERE playlist_id = ? "
            "AND (artist, title, album) NOT IN ("
            "  SELECT json_extract(je.value, '$[0]'), json_extract(je.value, '$[1]'), "
            "         json_extract(je.value, '$[2]') FROM json_each(?) je"
            ")",
            (playlist_id, keep_json),
        )
    else:
        conn.execute("DELETE FROM unresolved_playlist_tracks WHERE playlist_id = ?", (playlist_id,))

    for pos, artist, title, album, isrc in deduped:
        conn.execute(
            "INSERT INTO unresolved_playlist_tracks (playlist_id, position, artist, title, album, isrc) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(playlist_id, artist, title, album) "
            "DO UPDATE SET position = excluded.position, isrc = excluded.isrc",
            (playlist_id, pos, artist, title, album, isrc),
        )


def list_unresolved_playlist_tracks(conn: sqlite3.Connection, playlist_id: int) -> list:
    """#200: this playlist's unresolved entries for the web review list —
    un-excluded first, then by artist/title/album."""
    rows = conn.execute(
        "SELECT id, artist, title, album, isrc, excluded, first_seen_at "
        "FROM unresolved_playlist_tracks WHERE playlist_id = ? "
        "ORDER BY excluded, artist, title, album",
        (playlist_id,),
    ).fetchall()
    return [{**dict(r), "excluded": bool(r["excluded"])} for r in rows]


def set_unresolved_playlist_tracks_excluded(conn: sqlite3.Connection, playlist_id: int,
                                            ids: list, excluded: bool) -> int:
    """#200: mark the given unresolved_playlist_tracks row ids excluded
    (acknowledged as "not actually a gap" — e.g. a provider-only track that
    will never exist locally, streamed but never downloaded — so it stops
    being flagged) or un-excluded. Returns the resulting non-excluded count
    for this playlist."""
    seen = list(dict.fromkeys(ids))
    if seen:
        placeholders = ",".join("?" * len(seen))
        conn.execute(
            f"UPDATE unresolved_playlist_tracks SET excluded = ? "
            f"WHERE playlist_id = ? AND id IN ({placeholders})",
            (1 if excluded else 0, playlist_id, *seen),
        )
    count = conn.execute(
        "SELECT COUNT(*) FROM unresolved_playlist_tracks WHERE playlist_id = ? AND excluded = 0",
        (playlist_id,),
    ).fetchone()[0]
    conn.commit()
    return count


# the marker line clients use to tell Trobar-managed playlist
# files from ones the user put on the card themselves (only marked files are
# ever deleted when a playlist unassigns).
M3U_MARKER = "# Generated by Trobar"


def _device_playlists(conn: sqlite3.Connection, device_id: int,
                      transcode_format: str | None) -> list[dict]:
    """one.m3u8 per playlist selection assigned to this device:
    same name, playlist order, only tracks that are (or will be after this
    sync) actually on the device, paths as the device knows them (transcoded
    extension included). Empty playlists (no local matches on this device)
    are omitted, which also stale-deletes their file client-side."""
    out: list[dict] = []
    used_names: set[str] = set()
    playlists = conn.execute(
        "SELECT DISTINCT p.id, p.title FROM selections s "
        "JOIN selection_devices sd ON sd.selection_id = s.id "
        "JOIN playlists p ON p.id = CAST(s.target AS INTEGER) "
        "WHERE s.type = 'playlist' AND sd.device_id = ? ORDER BY p.title",
        (device_id,),
    ).fetchall()
    for pl in playlists:
        entries = conn.execute(
            "SELECT t.id, t.artist, t.album, t.title, t.track_no, t.disc_no, "
            "t.relative_path, t.duration "
            "FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.matched_track_id "
            "JOIN device_track_state dts ON dts.track_id = t.id AND dts.device_id = ? "
            "WHERE pt.playlist_id = ? AND t.deleted_at IS NULL "
            "AND dts.status IN ('pending', 'downloaded') "
            "ORDER BY pt.position",
            (device_id, pl["id"]),
        ).fetchall()
        if not entries:
            continue
        lines = ["#EXTM3U", M3U_MARKER, f"#PLAYLIST:{pl['title']}"]
        for row in entries:
            duration = int(row["duration"]) if row["duration"] else -1
            lines.append(f"#EXTINF:{duration},{row['artist']} - {row['title']}")
            lines.append(device_path(row, transcode_format))
        filename = f"{fs_segment(pl['title'])}.m3u8"
        if filename in used_names:  # two titles sanitising to the same name
            filename = f"{fs_segment(pl['title'])} ({pl['id']}).m3u8"
        used_names.add(filename)
        out.append({"name": pl["title"], "filename": filename,
                    "content": "\n".join(lines) + "\n"})
    return out


def get_changes(conn: sqlite3.Connection, device_id: int) -> dict:
    # on a device with transcode_format set, every expected
    # on-device path below carries the target extension for lossless
    # sources — the missing-file spot check, deletes and dedup all key off
    # these server-computed names, so the transcoded reality on the card
    # never drifts from the server's bookkeeping.
    fmt = conn.execute(
        "SELECT transcode_format FROM devices WHERE id = ?", (device_id,)
    ).fetchone()["transcode_format"]
    to_download = [
        {"track_id": row["id"], "relative_path": device_path(row, fmt), "size": row["size"],
         "transcode": wants_transcode(row, fmt)}
        for row in conn.execute(
            "SELECT t.id, t.artist, t.album, t.title, t.track_no, t.disc_no, t.relative_path, t.size "
            "FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status = 'pending'",
            (device_id,),
        )
    ]
    to_delete = [
        {"track_id": row["id"], "relative_path": device_path(row, fmt)}
        for row in conn.execute(
            "SELECT t.id, t.artist, t.album, t.title, t.track_no, t.disc_no, t.relative_path "
            "FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status = 'removed'",
            (device_id,),
        )
    ]
    # Everything the server currently believes is already on-device. Not
    # acted on here — the client uses this purely to spot-check that each
    # file still actually exists locally before trusting this
    # bookkeeping, since nothing else here ever verifies that.
    downloaded = [
        {"track_id": row["id"], "relative_path": device_path(row, fmt), "size": row["size"]}
        for row in conn.execute(
            "SELECT t.id, t.artist, t.album, t.title, t.track_no, t.disc_no, t.relative_path, t.size "
            "FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status = 'downloaded'",
            (device_id,),
        )
    ]
    return {"to_download": to_download, "to_delete": to_delete, "downloaded": downloaded,
            "playlists": _device_playlists(conn, device_id, fmt),
            # The format the flags/names above were computed with — clients
            # must transcode to exactly this, not to a device-info value
            # fetched at some other moment (a mid-session format change made
            # a desktop client encode at the old bitrate; found live).
            "transcode_format": fmt}


def ack(conn: sqlite3.Connection, device_id: int, track_id: int, status: str,
        bytes_on_device: int | None = None) -> None:
    if status == "downloaded":
        # bytes_on_device is what actually landed on the device — on a
        # transcoding device that's the MP3's size, not the original's
        #. Clients that don't report it leave NULL; usage math
        # then falls back to tracks.size.
        conn.execute(
            "UPDATE device_track_state SET status='downloaded', bytes_on_device=?, "
            "updated_at=datetime('now') WHERE device_id=? AND track_id=?",
            (bytes_on_device, device_id, track_id),
        )
    elif status == "removed":
        conn.execute(
            "DELETE FROM device_track_state WHERE device_id=? AND track_id=?",
            (device_id, track_id),
        )
    conn.commit()


def resolve_missing_tracks(conn: sqlite3.Connection, device_id: int,
                            redownload_ids: list[int], exclude_ids: list[int]) -> None:
    """the client found tracks the server believes are
    `downloaded` actually missing on disk, and the user (or a standing
    client-side preference) decided what to do about each: re-fetch them, or
    leave them deleted. `excluded` gets the same "untouched while still
    required" treatment as `pending`/`downloaded` in recompute_device_state,
    so it's never silently re-queued — but still cleans up the normal way
    if a later selection change actually drops the requirement."""
    if redownload_ids:
        placeholders = ",".join("?" * len(redownload_ids))
        conn.execute(
            f"UPDATE device_track_state SET status='pending', updated_at=datetime('now') "
            f"WHERE device_id=? AND track_id IN ({placeholders})",
            (device_id, *redownload_ids),
        )
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        conn.execute(
            f"UPDATE device_track_state SET status='excluded', updated_at=datetime('now') "
            f"WHERE device_id=? AND track_id IN ({placeholders})",
            (device_id, *exclude_ids),
        )
    conn.commit()


_FS_ILLEGAL = re.compile(r'[\\*?"<>|\x00-\x1f]')


def fs_segment(name: str) -> str:
    """Make a tag value safe as one path segment on every filesystem/API a
    client writes to. Android's MediaProvider enforces FAT-style
    naming at the FUSE layer even on ext4-backed internal storage (a ':' in
    an album title made SAF createDirectory fail with EPERM on a real
    album, "Song Machine, Season One: Strange Timez"); DAP SD cards are
    usually exFAT; Windows has the same reserved set. ':' gets the readable
    ' - ' treatment since it's overwhelmingly a subtitle separator in album/
    track titles; the rarer reserved characters just become '_'. FAT also
    forbids trailing dots/spaces in a segment.

    Public (#285): also used by mirror.py for the first, human-readable
    pass on a playlist title before werkzeug.secure_filename() — path
    containment for a second module now depends on this, not just this
    one's own device-`.m3u8` naming, so it stays a shared, deliberately
    stable util rather than a private implementation detail either module
    could quietly change."""
    name = name.replace("/", "-")
    name = name.replace(": ", " - ").replace(":", "-")
    name = _FS_ILLEGAL.sub("_", name)
    name = name.strip().rstrip(".")
    return name or "_"


# extensions we confidently know are lossless. .m4a is deliberately
# absent — it can hold ALAC (lossless) or AAC (lossy) and the extension can't
# tell them apart, so .m4a is always copied as-is rather than risk
# re-encoding an already-lossy file.
_LOSSLESS_EXTS = {"flac", "wav", "aiff", "aif"}
_TRANSCODE_EXT = {"mp3_320": "mp3", "mp3_256": "mp3", "mp3_192": "mp3", "mp3_128": "mp3"}
TRANSCODE_FORMATS = frozenset(_TRANSCODE_EXT)


def _source_ext(relative_path: str) -> str:
    # Case preserved: existing devices' expected paths must not change for a
    # file named .FLAC just because this code landed (lowercasing only
    # happens for the lossless-set membership test).
    return relative_path.rsplit(".", 1)[-1] if "." in relative_path else "flac"


def wants_transcode(row: sqlite3.Row, transcode_format: str | None) -> bool:
    """True when the client should transcode this track rather than copy it:
    the device asks for a format AND the source is (recognisably) lossless."""
    return transcode_format in _TRANSCODE_EXT and _source_ext(row["relative_path"]).lower() in _LOSSLESS_EXTS


def device_path(row: sqlite3.Row, transcode_format: str | None = None) -> str:
    """Wire/on-disk relative path for a device — always Artiste/Album/NN -
    Titre.ext, regardless of the on-disk catalog path's actual folder
    structure (which may have disc subfolders, parenthetical years, etc).

    Public (#239): this is the device-facing path CONTRACT, not an internal
    detail — get_changes, record_device_manifest and main.py's
    /api/device/fingerprints all have to produce byte-identical strings or a
    client can't correlate a fingerprint with the file it wrote. Now that a
    third caller outside this module depends on it, it stays a deliberately
    stable shared util rather than a private helper someone could quietly
    change (same reasoning as fs_segment's promotion in #285).

    Discnumber is folded into the track number prefix (CD2-01 instead of just
    01) whenever the tag is present — without it, a multi-disc album whose
    discs both start numbering at 1 would silently collide/overwrite on the
    device (found on a real album in testing: a bonus disc and the main
    disc, both tagged with the same plain album name).

    On a transcoding device lossless sources get the target
    format's extension — the server always names files by what should
    actually be on the device."""
    ext = _source_ext(row["relative_path"])
    # wants_transcode() being True already implies transcode_format is a real
    # key (it checks `transcode_format in _TRANSCODE_EXT`) — the redundant
    # `transcode_format and` just narrows it from str | None for mypy too.
    if transcode_format and wants_transcode(row, transcode_format):
        ext = _TRANSCODE_EXT[transcode_format]
    if row["track_no"] and row["disc_no"]:
        prefix = f"CD{row['disc_no']}-{row['track_no']:02d} - "
    elif row["track_no"]:
        prefix = f"{row['track_no']:02d} - "
    else:
        prefix = ""
    artist = fs_segment(row["artist"])
    album = fs_segment(row["album"])
    title = fs_segment(row["title"])
    return f"{artist}/{album}/{prefix}{title}.{ext}"
