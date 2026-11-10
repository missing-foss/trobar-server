#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Credentials that can write to someone's accounts, encrypted at rest.

AES-256-GCM, with a key in a file of its own: DATA_DIR/credential_key, or
wherever TROBAR_CREDENTIAL_KEY_FILE points. The key is not in the database, so
a leaked database or snapshot gives nobody these values. A key left in DATA_DIR
does not help against someone who can read all of DATA_DIR; docs/operations has
how to keep it elsewhere.

Each value is bound to its person and field, so a ciphertext copied onto another
row doesn't decrypt. A value that can't be decrypted, because the key was lost or
changed, reads as None: nothing fails, and the person is asked for it again.
"""

import base64
import logging
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import db

_log = logging.getLogger(__name__)

_KEY_BYTES = 32
_NONCE_BYTES = 12
_VERSION = "v1:"
# What every stored value starts with, so a caller can tell one from cleartext.
VERSION_PREFIX = _VERSION


class CredentialKeyError(RuntimeError):
    """The key file exists but can't be used, so nothing can be encrypted."""


def _key_path() -> Path:
    configured = os.environ.get("TROBAR_CREDENTIAL_KEY_FILE", "").strip()
    return Path(configured) if configured else db.DATA_DIR / "credential_key"


def _read_key(path: Path) -> bytes | None:
    """The key, None if there is no file, CredentialKeyError if it's unusable.
    Never logs the key's bytes."""
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CredentialKeyError(f"credential key can't be read: {type(e).__name__}") from e
    if len(key) != _KEY_BYTES:
        raise CredentialKeyError("credential key has the wrong length")
    return key


def _key_for_writing() -> bytes:
    """The key, made on first use. It is written to a private temporary file
    and published with a hard link, so it appears whole or not at all, and a
    link fails if the file exists: two starts can't each make one and lose the
    other's values, and a crash leaves no empty key behind."""
    path = _key_path()
    key = _read_key(path)
    if key is not None:
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credential_key.")  # mode 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(_KEY_BYTES))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # another start got there first: its key is the key
        except OSError as e:
            raise CredentialKeyError(f"credential key can't be made: {type(e).__name__}") from e
    finally:
        os.unlink(temporary)
    key = _read_key(path)
    if key is None:
        raise CredentialKeyError("credential key vanished while being made")
    return key


def _context(user_id: int, field: str) -> bytes:
    return f"{user_id}:{field}".encode()


def encrypt(value: str, user_id: int, field: str) -> str:
    """`value` as stored text. CredentialKeyError if the key is unusable."""
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_key_for_writing()).encrypt(nonce, value.encode(), _context(user_id, field))
    return _VERSION + base64.b64encode(nonce + sealed).decode()


def decrypt_strict(stored: str | None, user_id: int, field: str) -> str | None:
    """As decrypt(), but a key file that exists and can't be used raises
    CredentialKeyError instead of reading as None: the value may still be
    good once the key is fixed, so a caller can keep it. None still means
    there is no value, no key file, or one that doesn't open it."""
    if not stored:
        return None
    key = _read_key(_key_path())
    if key is None or not stored.startswith(_VERSION):
        return None
    try:
        raw = base64.b64decode(stored[len(_VERSION):], validate=True)
        plain = AESGCM(key).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _context(user_id, field))
    except (InvalidTag, ValueError) as e:
        _log.warning("A stored credential can't be read: %s", type(e).__name__)
        return None
    return plain.decode()


def decrypt(stored: str | None, user_id: int, field: str) -> str | None:
    """The value, or None if there is none or it can't be decrypted."""
    try:
        return decrypt_strict(stored, user_id, field)
    except CredentialKeyError as e:
        _log.warning("A stored credential can't be read: %s", type(e).__name__)
        return None


def state(stored: str | None, user_id: int, field: str) -> str:
    """"none", "set", or "unreadable": there is a value and it can't be read."""
    if not stored:
        return "none"
    return "set" if decrypt(stored, user_id, field) is not None else "unreadable"
