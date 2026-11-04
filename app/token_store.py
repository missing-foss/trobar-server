# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tokens: how a device, a pairing code or an integration proves who it is.

A device's token, an enrollment grant's pairing code and an integration
token are each shown to their holder once and stored only as a SHA-256
hash (hash_token, which says why that hash is enough). This module mints
and checks them; redeeming a pairing code, which creates the device,
stays with the devices. Split out of sync_state, which re-exports these
names for now.
"""
import hashlib
import logging
import secrets
import sqlite3

from db import _new_id

_log = logging.getLogger(__name__)


def hash_token(raw_token: str) -> str:
    # SHA-256 (not a slow/salted KDF) is deliberate, never a human password.
    # Device tokens and #446's API tokens are secrets.token_urlsafe(32) --
    # 2^256 keyspace, nothing to brute-force, so a slow KDF (which exists to
    # make guessing a *low-entropy* secret expensive) buys nothing here and
    # would slow every device sync request, which authenticates by this
    # hash on every call. Enrollment codes are the one caller this doesn't
    # apply to -- 8 chars from a 31-symbol alphabet (~2^40, meant to be
    # hand-typed) -- but those are protected structurally instead: a
    # 20-minute TTL, single-use, purge-on-mint (see ENROLLMENT_TTL_SECONDS
    # below), so a stolen database holds at most a couple of live codes with
    # minutes left, and an attacker with database access can mint their own
    # grant anyway, making the hash moot at that point. See SECURITY.md
    # ("device tokens are secrets.token_urlsafe(32) stored only as
    # SHA-256") -- passwords use Werkzeug's scrypt hash instead (main.py).
    #
    # CodeQL's py/weak-sensitive-data-hashing flags every call site here
    # regardless (its "password" classification is a shape heuristic, not a
    # real flow) -- dismissed on GitHub's code-scanning alerts with this
    # same reasoning, matching the existing won't-fix precedent for the
    # same rule on subsonic_client.py. (Inline `lgtm[...]` suppression
    # comments are LGTM.com's retired syntax and are NOT honoured by
    # GitHub code scanning -- don't rely on one appearing to work here.)
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def regenerate_token(conn: sqlite3.Connection, device_id: int, *,
                     commit: bool = True) -> str:
    """Issues a brand-new token for an existing device (invalidating the
    old one), since the raw token is never stored -- only its hash.

    Kept for trobar-desktop builds that predate enrollment codes: they read
    only a {server_url, token} payload, as a pasted config or as
    .trobar/device.json on the card. Every current client (Android, desktop,
    the Garmin watch) re-pairs through create_enrollment_grant(device_id=...)
    instead, which hands back the same device through an 8-character code;
    redeeming one calls this to issue the device its new token. The web UI
    offers the raw token only as a secondary action for those older builds."""
    raw_token = secrets.token_urlsafe(32)
    conn.execute(
        "UPDATE devices SET api_token_hash = ? WHERE id = ?",
        (hash_token(raw_token), device_id),
    )
    if commit:
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
# symbols is ~40 bits — plenty for a single-use code that expires within the
# hour and is rate-limited (30 failed redeems per 5 minutes per IP).
_ENROLL_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


# Phones and the desktop app redeem a code at once. A watch takes it from
# Garmin Connect and redeems it at its next sync, which comes within minutes
# when the owner syncs it on request; 20 minutes leaves room for that.
ENROLLMENT_TTL_SECONDS = 1200


def _generate_enrollment_code() -> str:
    return "".join(secrets.choice(_ENROLL_ALPHABET) for _ in range(8))


def create_enrollment_grant(conn: sqlite3.Connection, owner_user_id: int,
                            device_id: int | None = None) -> str:
    """#163: mint a short-lived, single-use, owner-scoped enrollment code. The
    web UI presents it as a QR + human code; a mobile client redeems it via
    redeem_enrollment_grant to create its own device — the app never holds user
    credentials (#162). Returns the raw code; only its hash is stored.

    device_id names an EXISTING device to re-attach to instead of creating a
    new one — the reinstall case, where the app is gone but the device's
    selections, track state and settings are not. Default None is the original
    behaviour, unchanged. The caller is responsible for checking that the
    owner may manage that device; redeem re-checks ownership regardless,
    because the code's lifetime is long enough for a device to be deleted."""
    # #166: purge-on-mint. This ephemeral table (short TTL, single-use) is only
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
                "INSERT INTO enrollment_grants (code_hash, owner_user_id, device_id, expires_at) "
                "VALUES (?, ?, ?, datetime('now', ?))",
                (hash_token(code), owner_user_id, device_id,
                 f"+{ENROLLMENT_TTL_SECONDS} seconds"),
            )
            conn.commit()
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not mint a unique enrollment code")


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
