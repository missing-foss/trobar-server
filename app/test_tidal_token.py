#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""tidal_token: Tidal refresh tokens are encrypted at rest, the ones stored in
cleartext are converted at start without losing anyone's link, and a lost key
asks the person to connect Tidal again instead of failing.

    python3 -m unittest test_tidal_token -v
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import flask

import credential_store
import db
import playlist_sync
import tidal_client
import tidal_token
import user_profile
from test_playlist_sync import _FullStubProvider


def _user(conn, name: str, token: str | None) -> int:
    uid = conn.execute("INSERT INTO users (username) VALUES (?)", (name,)).lastrowid
    conn.execute("UPDATE users SET tidal_refresh_token = ?, tidal_user_id = ? WHERE id = ?",
                 (token, f"{name}-tid", uid))
    conn.commit()
    return uid


def _stored(uid: int) -> str | None:
    with db.connection() as conn:
        return conn.execute("SELECT tidal_refresh_token FROM users WHERE id = ?", (uid,)).fetchone()[0]


def _sealed(uid: int) -> str:
    """The stored token, which the test expects to be there."""
    stored = _stored(uid)
    assert stored is not None, f"user {uid} has no stored token"
    return stored


class _DataDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name, value in (("DATA_DIR", self.dir), ("DB_PATH", self.dir / "t.db")):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TROBAR_CREDENTIAL_KEY_FILE", None)

    def break_the_key(self):
        """A key file that exists and can't be used: the wrong length."""
        (self.dir / "credential_key").write_bytes(b"short")


class SealTests(_DataDir):
    def test_a_sealed_token_reads_back_and_is_not_stored_readable(self):
        stored = tidal_token.seal("alice-refresh-token", 1)
        self.assertTrue(stored.startswith(credential_store.VERSION_PREFIX))
        self.assertNotIn("alice-refresh-token", stored)
        self.assertEqual(tidal_token.unseal(stored, 1), "alice-refresh-token")

    def test_a_token_is_bound_to_its_person(self):
        stored = tidal_token.seal("alice-refresh-token", 1)
        with self.assertLogs("credential_store", level="WARNING"):
            self.assertIsNone(tidal_token.unseal(stored, 2))

    def test_a_token_not_converted_yet_still_works(self):
        self.assertEqual(tidal_token.unseal("alice-rt", 1), "alice-rt")
        self.assertIsNone(tidal_token.unseal(None, 1))
        self.assertIsNone(tidal_token.unseal("", 1))

    def test_a_damaged_key_is_an_error_to_fix_not_a_lost_token(self):
        stored = tidal_token.seal("alice-rt", 1)
        self.break_the_key()
        with self.assertRaises(credential_store.CredentialKeyError):
            tidal_token.unseal(stored, 1)
        # #224's callers still read it as gone, as before.
        with self.assertLogs("credential_store", level="WARNING"):
            self.assertIsNone(credential_store.decrypt(stored, 1, tidal_token.FIELD))

    def test_with_an_unusable_key_a_new_link_is_refused(self):
        self.break_the_key()
        with self.assertRaises(credential_store.CredentialKeyError):
            tidal_token.seal("alice-rt", 1)

    def test_with_an_unusable_key_a_rotated_token_is_kept_rather_than_lost(self):
        self.break_the_key()
        with self.assertLogs("tidal_token", level="WARNING") as logs:
            self.assertEqual(tidal_token.seal_for_rotation("alice-rt", 1), "alice-rt")
        self.assertNotIn("alice-rt", "\n".join(logs.output))


class ConversionTests(_DataDir):
    def setUp(self):
        super().setUp()
        db.init_db()

    def test_cleartext_tokens_are_encrypted_once(self):
        with db.connection() as conn:
            alice = _user(conn, "alice", "alice-rt")
            bob = _user(conn, "bob", "bob-rt")
            carol = _user(conn, "carol", None)
            self.assertEqual(tidal_token.convert_cleartext(conn), 2)
            conn.commit()
            self.assertEqual(tidal_token.convert_cleartext(conn), 0, "a second start has nothing to do")
        for uid, token in ((alice, "alice-rt"), (bob, "bob-rt")):
            self.assertTrue(_sealed(uid).startswith(credential_store.VERSION_PREFIX))
            self.assertEqual(tidal_token.unseal(_stored(uid), uid), token)
        self.assertIsNone(_stored(carol))

    def test_a_token_rotated_while_converting_is_not_overwritten(self):
        with db.connection() as conn:
            alice = _user(conn, "alice", "alice-rt")
            real_encrypt = credential_store.encrypt

            def rotate_then_encrypt(value, user_id, field):
                # A sync rotates the token between the read and the write.
                with db.connection() as other:
                    other.execute("UPDATE users SET tidal_refresh_token = 'alice-rt-2' WHERE id = ?", (alice,))
                    other.commit()
                return real_encrypt(value, user_id, field)

            with mock.patch.object(credential_store, "encrypt", side_effect=rotate_then_encrypt):
                self.assertEqual(tidal_token.convert_cleartext(conn), 0)
            conn.commit()
        self.assertEqual(_stored(alice), "alice-rt-2", "the rotated token stands, not the old one sealed")
        db.init_db()
        self.assertEqual(tidal_token.unseal(_sealed(alice), alice), "alice-rt-2")

    def test_with_an_unusable_key_nothing_is_converted_or_lost(self):
        with db.connection() as conn:
            alice = _user(conn, "alice", "alice-rt")
            self.break_the_key()
            with self.assertLogs("tidal_token", level="WARNING") as logs:
                self.assertEqual(tidal_token.convert_cleartext(conn), 0)
            conn.commit()
        self.assertEqual(_stored(alice), "alice-rt")
        self.assertNotIn("alice-rt", "\n".join(logs.output))
        # Once the key is usable again, the next start converts it.
        (self.dir / "credential_key").unlink()
        db.init_db()
        self.assertEqual(tidal_token.unseal(_stored(alice), alice), "alice-rt")
        self.assertTrue(_sealed(alice).startswith(credential_store.VERSION_PREFIX))


class UpgradeTests(_DataDir):
    """A real version-4 database, frozen in testdata, with linked people."""

    def test_an_upgrade_encrypts_the_tokens_and_keeps_every_link(self):
        fixture = (Path(__file__).parent / "testdata" / "schema-v4.sql").read_text()
        with db.connection() as conn:
            conn.executescript(fixture)
            conn.execute("PRAGMA user_version = 4")
            alice = _user(conn, "alice", "alice-rt")
            bob = _user(conn, "bob", "bob-rt")
        db.init_db()
        with db.connection() as conn:
            linked = conn.execute("SELECT COUNT(*) FROM users WHERE tidal_refresh_token IS NOT NULL").fetchone()[0]
        self.assertEqual(linked, 2)
        for uid, token in ((alice, "alice-rt"), (bob, "bob-rt")):
            self.assertNotIn(token, _sealed(uid))
            self.assertEqual(tidal_token.unseal(_stored(uid), uid), token)


class SyncTests(_DataDir):
    def setUp(self):
        super().setUp()
        db.init_db()
        with db.connection() as conn:
            db.set_config(conn, "tidal_client_id", "cid")
            db.set_config(conn, "tidal_client_secret", "csecret")
            conn.commit()

    def _sync(self, refresh):
        with mock.patch.object(tidal_client, "refresh_access_token", side_effect=refresh) as called, \
                mock.patch.object(tidal_client, "list_playlists", return_value={"status": "ok", "playlists": []}):
            result = playlist_sync.sync_playlists(_FullStubProvider([], []), "subsonic")
        return result, called

    def test_the_sync_sends_the_decrypted_token_and_stores_a_new_one_encrypted(self):
        with db.connection() as conn:
            alice = _user(conn, "alice", tidal_token.seal("alice-rt", 1))
        self.assertEqual(alice, 1)
        _, called = self._sync(lambda cid, secret, token: ("access", "alice-rt-2"))
        self.assertEqual(called.call_args.args[2], "alice-rt")
        self.assertNotIn("alice-rt-2", _sealed(alice))
        self.assertEqual(tidal_token.unseal(_stored(alice), alice), "alice-rt-2")

    def test_a_token_that_cant_be_decrypted_asks_to_connect_again(self):
        with db.connection() as conn:
            alice = _user(conn, "alice", tidal_token.seal("alice-rt", 1))
        # The key is lost: a new one is made at the next write, and this
        # token can't be read with it.
        (self.dir / "credential_key").unlink()
        with db.connection() as conn, flask.Flask(__name__).test_request_context():
            self.assertFalse(user_profile._profile_dict(conn, alice)["tidal_connected"])
        result, called = self._sync(lambda *a: self.fail("an unreadable token is never sent"))
        called.assert_not_called()
        self.assertEqual([f["reason"] for f in result["source_failures"]], ["token_unreadable"])
        self.assertIsNone(_stored(alice), "the link is cleared, as for a token Tidal rejects")

    def test_a_token_rotated_while_the_key_is_damaged_is_kept_and_sealed_later(self):
        # Only a token not converted yet can be refreshed with a damaged key;
        # Tidal then rotates it, and the old one is gone.
        with db.connection() as conn:
            alice = _user(conn, "alice", "alice-rt")
        self.break_the_key()
        with self.assertLogs("tidal_token", level="WARNING") as logs:
            _, called = self._sync(lambda cid, secret, token: ("access", "alice-rt-2"))
        self.assertEqual(called.call_args.args[2], "alice-rt")
        self.assertEqual(_stored(alice), "alice-rt-2", "kept, unencrypted, rather than unlinked")
        self.assertNotIn("alice-rt-2", "\n".join(logs.output))
        (self.dir / "credential_key").unlink()
        db.init_db()
        self.assertEqual(tidal_token.unseal(_sealed(alice), alice), "alice-rt-2")
        self.assertTrue(_sealed(alice).startswith(credential_store.VERSION_PREFIX))

    def test_a_damaged_key_keeps_every_link_until_it_is_fixed(self):
        with db.connection() as conn:
            alice = _user(conn, "alice", tidal_token.seal("alice-rt", 1))
        good_key = (self.dir / "credential_key").read_bytes()
        self.break_the_key()
        with db.connection() as conn, flask.Flask(__name__).test_request_context():
            self.assertTrue(user_profile._profile_dict(conn, alice)["tidal_connected"])
        result, called = self._sync(lambda *a: self.fail("nothing is sent while the key can't be used"))
        called.assert_not_called()
        self.assertEqual([f["reason"] for f in result["source_failures"]], ["credential_key_unusable"])
        # The key is fixed: the link is still there and works.
        (self.dir / "credential_key").write_bytes(good_key)
        self.assertEqual(tidal_token.unseal(_stored(alice), alice), "alice-rt")
        _, called = self._sync(lambda cid, secret, token: ("access", token))
        self.assertEqual(called.call_args.args[2], "alice-rt")


if __name__ == "__main__":
    unittest.main()
