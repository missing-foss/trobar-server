#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-user Tidal refresh tokens, encrypted at rest with credential_store.

A refresh token can write to its owner's Tidal account (playlist sharing), so
it is kept like the credentials that send plays: AES-256-GCM, with the key in
its own file, and bound to its person. It stays in users.tidal_refresh_token,
so "IS NOT NULL" still means linked.

A token stored before this, in cleartext, is converted at the next start
(convert_cleartext). Until it is, it still works as it is: a conversion that
can't happen, because the key file is unusable, never unlinks anyone. With
such a key, a new link is refused (seal raises), and a token Tidal rotates
during a sync is stored unencrypted until a start converts it
(seal_for_rotation). A stored
token that can't be decrypted, because the key was lost or changed, reads as
None; the sync then clears the link as it does for a token Tidal rejects, and
the person's Profile asks them to connect Tidal again. A key file that exists
but can't be used is a fault to fix, not a lost key: the tokens are kept, and
the sync skips Tidal for those people until it is fixed.
"""

import logging
import sqlite3

import credential_store

_log = logging.getLogger(__name__)

FIELD = "tidal_refresh_token"


def _is_sealed(stored: str) -> bool:
    return stored.startswith(credential_store.VERSION_PREFIX)


def seal(token: str, user_id: int) -> str:
    """`token` as stored. credential_store.CredentialKeyError if the key file
    can't be used: a new link is refused rather than stored unencrypted, as
    the Last.fm link is, and the person connects again once the key works."""
    return credential_store.encrypt(token, user_id, FIELD)


def seal_for_rotation(token: str, user_id: int) -> str:
    """seal(), for a token Tidal has just rotated during a sync. Tidal has
    already replaced the old one, so refusing to store this one would unlink
    the person. If the key file can't be used it is stored as it is instead,
    logged (never the token), and convert_cleartext seals it at a later start."""
    try:
        return seal(token, user_id)
    except credential_store.CredentialKeyError as e:
        _log.warning("A renewed Tidal refresh token is stored unencrypted until the credential key can be used: %s", e)
        return token


def unseal(stored: str | None, user_id: int) -> str | None:
    """The token to send to Tidal, or None if there is none or it can't be
    decrypted: no key file, or one that doesn't open it. A token not converted
    yet is returned as it is. A key file that exists and can't be used raises
    credential_store.CredentialKeyError: the token may be good once the key is
    fixed, so it must be kept, not cleared."""
    if not stored:
        return None
    if not _is_sealed(stored):
        return stored
    return credential_store.decrypt_strict(stored, user_id, FIELD)


def convert_cleartext(conn: sqlite3.Connection) -> int:
    """Encrypts every Tidal refresh token still stored in cleartext; returns
    how many it converted. Runs at every start and does nothing once all are
    converted. If the key can't be used it converts none, logs why, and leaves
    every token as it was: nobody's link is lost, and the next start tries
    again. The caller commits."""
    rows = conn.execute(
        "SELECT id, tidal_refresh_token FROM users "
        "WHERE tidal_refresh_token IS NOT NULL AND tidal_refresh_token != '' "
        "AND substr(tidal_refresh_token, 1, ?) != ?",
        (len(credential_store.VERSION_PREFIX), credential_store.VERSION_PREFIX),
    ).fetchall()
    converted = 0
    for row in rows:
        try:
            sealed = credential_store.encrypt(row["tidal_refresh_token"], row["id"], FIELD)
        except credential_store.CredentialKeyError as e:
            _log.warning("%d Tidal refresh token(s) stay unencrypted until the credential key can be used: %s",
                         len(rows) - converted, e)
            return converted
        # Only if it is still the token read: a sync may have rotated it since.
        updated = conn.execute("UPDATE users SET tidal_refresh_token = ? WHERE id = ? AND tidal_refresh_token = ?",
                               (sealed, row["id"], row["tidal_refresh_token"])).rowcount
        converted += updated
    if converted:
        _log.info("Encrypted %d Tidal refresh token(s) stored in cleartext", converted)
    return converted
