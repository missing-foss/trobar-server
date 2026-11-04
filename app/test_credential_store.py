#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""credential_store: values round-trip, stay bound to their row, and a lost key
reads as "enter it again", never as a crash.

    python3 -m unittest test_credential_store -v
"""
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import credential_store as cs
import db


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(db, "DATA_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TROBAR_CREDENTIAL_KEY_FILE", None)

    def test_round_trip_and_nothing_readable_stored(self):
        stored = cs.encrypt("sécret-token", 1, "maloja_api_key")
        self.assertNotIn("token", stored)
        self.assertEqual(cs.decrypt(stored, 1, "maloja_api_key"), "sécret-token")

    def test_the_same_value_encrypts_differently_each_time(self):
        self.assertNotEqual(cs.encrypt("x", 1, "f"), cs.encrypt("x", 1, "f"))

    def test_a_value_is_bound_to_its_person_and_field(self):
        stored = cs.encrypt("x", 1, "maloja_api_key")
        with self.assertLogs("credential_store", level="WARNING"):
            self.assertIsNone(cs.decrypt(stored, 2, "maloja_api_key"))
        with self.assertLogs("credential_store", level="WARNING"):
            self.assertIsNone(cs.decrypt(stored, 1, "listenbrainz_token"))

    def test_the_key_file_is_made_once_and_is_private(self):
        cs.encrypt("x", 1, "f")
        key = self.dir / "credential_key"
        first = key.read_bytes()
        self.assertEqual(len(first), 32)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        cs.encrypt("y", 1, "f")
        self.assertEqual(key.read_bytes(), first)

    def test_a_key_that_appears_first_wins(self):
        winner = os.urandom(32)
        real_link = os.link

        def another_start_got_there_first(src, dst):
            Path(dst).write_bytes(winner)
            real_link(src, dst)

        with mock.patch("credential_store.os.link", another_start_got_there_first):
            stored = cs.encrypt("x", 1, "f")
        self.assertEqual((self.dir / "credential_key").read_bytes(), winner)
        self.assertEqual(cs.decrypt(stored, 1, "f"), "x")

    def test_concurrent_first_uses_share_one_whole_key(self):
        barrier = threading.Barrier(8)
        results, errors = [], []

        def first_use():
            barrier.wait()
            try:
                results.append(cs.encrypt("x", 1, "f"))
            except cs.CredentialKeyError as e:
                errors.append(e)

        threads = [threading.Thread(target=first_use) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual([cs.decrypt(r, 1, "f") for r in results], ["x"] * 8)

    def test_making_a_key_leaves_only_the_key(self):
        cs.encrypt("x", 1, "f")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["credential_key"])

    def test_a_filesystem_without_links_says_so_and_leaves_nothing(self):
        with mock.patch("credential_store.os.link", side_effect=PermissionError):
            with self.assertRaises(cs.CredentialKeyError):
                cs.encrypt("x", 1, "f")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_the_key_can_live_elsewhere(self):
        elsewhere = self.dir / "other" / "key"
        os.environ["TROBAR_CREDENTIAL_KEY_FILE"] = str(elsewhere)
        stored = cs.encrypt("x", 1, "f")
        self.assertTrue(elsewhere.exists())
        self.assertFalse((self.dir / "credential_key").exists())
        self.assertEqual(cs.decrypt(stored, 1, "f"), "x")

    def test_a_lost_key_reads_as_unreadable_and_makes_no_new_key(self):
        stored = cs.encrypt("x", 1, "f")
        (self.dir / "credential_key").unlink()
        self.assertIsNone(cs.decrypt(stored, 1, "f"))
        self.assertEqual(cs.state(stored, 1, "f"), "unreadable")
        self.assertFalse((self.dir / "credential_key").exists())

    def test_a_changed_key_reads_as_unreadable(self):
        stored = cs.encrypt("x", 1, "f")
        (self.dir / "credential_key").write_bytes(os.urandom(32))
        with self.assertLogs("credential_store", level="WARNING"):
            self.assertEqual(cs.state(stored, 1, "f"), "unreadable")

    def test_a_damaged_key_refuses_to_encrypt_and_reads_as_unreadable(self):
        stored = cs.encrypt("x", 1, "f")
        (self.dir / "credential_key").write_bytes(b"short")
        with self.assertRaises(cs.CredentialKeyError):
            cs.encrypt("y", 1, "f")
        with self.assertLogs("credential_store", level="WARNING"):
            self.assertEqual(cs.state(stored, 1, "f"), "unreadable")

    def test_damaged_or_foreign_text_reads_as_unreadable(self):
        cs.encrypt("x", 1, "f")
        self.assertEqual(cs.state("plaintext", 1, "f"), "unreadable")
        with self.assertLogs("credential_store", level="WARNING"):
            self.assertEqual(cs.state("v1:!!!", 1, "f"), "unreadable")

    def test_state_of_nothing_is_none(self):
        self.assertEqual(cs.state(None, 1, "f"), "none")
        self.assertEqual(cs.state("", 1, "f"), "none")
        self.assertEqual(cs.state(cs.encrypt("x", 1, "f"), 1, "f"), "set")

    def test_nothing_secret_reaches_the_log(self):
        key_path = self.dir / "credential_key"
        stored = cs.encrypt("hunter2-value", 1, "f")
        key = key_path.read_bytes()
        with self.assertLogs("credential_store", level="WARNING") as logs:
            cs.decrypt(stored, 2, "f")
        text = " ".join(logs.output)
        self.assertNotIn("hunter2-value", text)
        self.assertNotIn(key.hex(), text)


if __name__ == "__main__":
    unittest.main()
