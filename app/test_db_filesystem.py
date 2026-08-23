#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for db.filesystem_type / db.data_dir_network_fs (#299).

DATA_DIR on a network filesystem can CORRUPT the SQLite database — locking is
unreliable on NFS/SMB, and WAL (which get_conn enables) makes it worse rather
than better. This is the detection behind the startup warning.

/proc/mounts is stubbed with real-world content: the mount-point-matching logic
is the part that can be wrong, and the interesting inputs (an NFS-backed bind
mount, nested mounts, a path with no mount entry of its own) can't all be
produced on demand. The NFS case WAS additionally confirmed live against a
genuine NFS mount through a docker bind mount, which is what established that a
bind mount carries the real underlying type into the container at all.

    python3 -m unittest test_db_filesystem -v      # from app/
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db

# Load-bearing preamble, same shape and reason as test_routes.py's: importing
# `main` writes a secret key into DATA_DIR at import time, so DATA_DIR must
# point somewhere writable BEFORE that import or it fails on the container's
# default /data. Order here matters; don't reorder.
_TMP = tempfile.mkdtemp(prefix="trobar-test-dbfs-")
os.environ["DATA_DIR"] = _TMP
db.DATA_DIR = Path(_TMP)
db.DB_PATH = Path(_TMP) / "music-sync.db"

import main  # noqa: E402 — must follow the DATA_DIR setup above


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


# Shaped exactly like a real container's /proc/mounts, including the trailing
# per-mount option columns this parser has to ignore.
_MOUNTS = """\
overlay / overlay rw,relatime,lowerdir=/x,upperdir=/y 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /dev tmpfs rw,nosuid,size=65536k,mode=755 0 0
/dev/sda1 /data ext4 rw,relatime 0 0
192.168.0.10:/export/music /music nfs4 rw,relatime,vers=4.2 0 0
//nas/share /smbdata cifs rw,relatime,vers=3.1.1 0 0
/dev/sdb1 /data/nested/local ext4 rw,relatime 0 0
"""


def _with_mounts(content=_MOUNTS):
    return mock.patch("builtins.open", mock.mock_open(read_data=content))


class FilesystemTypeTests(unittest.TestCase):
    def test_exact_mount_point_match(self):
        with _with_mounts():
            self.assertEqual(db.filesystem_type(Path("/data")), "ext4")

    def test_a_path_inside_a_mount_inherits_its_type(self):
        with _with_mounts():
            self.assertEqual(db.filesystem_type(Path("/data/music-sync.db")), "ext4")
            self.assertEqual(db.filesystem_type(Path("/music/Artist/Album")), "nfs4")

    def test_longest_mount_point_wins(self):
        # /data/nested/local is its own ext4 mount UNDER /data. A shortest- or
        # first-match implementation would attribute it to /data (or to /),
        # which is exactly how this kind of parser goes wrong.
        with _with_mounts():
            self.assertEqual(db.filesystem_type(Path("/data/nested/local/file")), "ext4")

    def test_a_path_with_no_mount_of_its_own_falls_back_to_the_root(self):
        with _with_mounts():
            self.assertEqual(db.filesystem_type(Path("/opt/whatever")), "overlay")

    def test_a_prefix_that_is_not_a_path_boundary_does_not_match(self):
        # "/database" must NOT match the "/data" mount — string prefix matching
        # without a separator check would wrongly claim it.
        mounts = "overlay / overlay rw 0 0\n/dev/sda1 /data ext4 rw 0 0\n"
        with _with_mounts(mounts):
            self.assertEqual(db.filesystem_type(Path("/database/x")), "overlay")

    def test_returns_none_when_proc_mounts_is_unavailable(self):
        # Non-Linux, or a locked-down /proc: fail open rather than guessing.
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertIsNone(db.filesystem_type(Path("/data")))

    def test_a_mount_point_containing_a_space_is_matched(self):
        # /proc/mounts octal-escapes a space as \040. Without unescaping, this
        # path never matches its own entry and falls back to '/' — reporting a
        # CIFS share as local, i.e. the warning silently doesn't fire for a
        # perfectly plausible path like "/mnt/My NAS/trobar".
        mounts = "overlay / overlay rw 0 0\n//nas/s /mnt/my\\040share cifs rw 0 0\n"
        with _with_mounts(mounts):
            self.assertEqual(db.filesystem_type(Path("/mnt/my share")), "cifs")
            self.assertEqual(db.filesystem_type(Path("/mnt/my share/trobar")), "cifs")

    def test_other_octal_escapes_are_handled(self):
        mounts = ("overlay / overlay rw 0 0\n"
                  "src /mnt/tab\\011here ext4 rw 0 0\n"
                  "src /mnt/back\\134slash ext4 rw 0 0\n")
        with _with_mounts(mounts):
            self.assertEqual(db.filesystem_type(Path("/mnt/tab\there")), "ext4")
            self.assertEqual(db.filesystem_type(Path("/mnt/back\\slash")), "ext4")

    def test_a_backslash_is_unescaped_last(self):
        # A path whose real name contains the literal text "\040" must not be
        # turned into a space: /proc/mounts writes that as \134040, so undoing
        # \134 first would corrupt it into " 040".
        mounts = "overlay / overlay rw 0 0\nsrc /mnt/lit\\134040x ext4 rw 0 0\n"
        with _with_mounts(mounts):
            self.assertEqual(db.filesystem_type(Path("/mnt/lit\\040x")), "ext4")

    def test_malformed_lines_are_skipped(self):
        mounts = "garbage\n\n/dev/sda1 /data ext4 rw 0 0\nalso bad\n"
        with _with_mounts(mounts):
            self.assertEqual(db.filesystem_type(Path("/data")), "ext4")


class DataDirNetworkFsTests(unittest.TestCase):
    def setUp(self):
        self._orig = db.DATA_DIR
        self.addCleanup(self._restore)

    def _restore(self):
        db.DATA_DIR = self._orig

    def test_nfs_data_dir_is_flagged(self):
        db.DATA_DIR = Path("/music")  # the nfs4 mount in the fixture
        with _with_mounts():
            self.assertEqual(db.data_dir_network_fs(), "nfs4")

    def test_smb_data_dir_is_flagged(self):
        db.DATA_DIR = Path("/smbdata")
        with _with_mounts():
            self.assertEqual(db.data_dir_network_fs(), "cifs")

    def test_a_local_data_dir_is_not_flagged(self):
        db.DATA_DIR = Path("/data")
        with _with_mounts():
            self.assertIsNone(db.data_dir_network_fs())

    def test_an_undetectable_filesystem_is_not_flagged(self):
        # Fail open: a false positive would nag someone whose setup is fine.
        db.DATA_DIR = Path("/data")
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertIsNone(db.data_dir_network_fs())

    def test_only_data_dir_is_checked_not_music_root(self):
        # The distinction that makes this worth warning about at all: a
        # NAS-mounted MUSIC_ROOT is fine (read-only, no locking), DATA_DIR is
        # not. This asserts the check is scoped to DATA_DIR alone.
        db.DATA_DIR = Path("/data")  # local
        with _with_mounts():
            self.assertIsNone(db.data_dir_network_fs())
            # ...even though an NFS mount exists and is in use elsewhere
            self.assertEqual(db.filesystem_type(Path("/music")), "nfs4")

    def test_every_listed_network_type_is_actually_flagged(self):
        # Guards the table itself: a typo in _NETWORK_FS_TYPES would silently
        # stop detecting that filesystem, and the failure mode is a corrupt
        # database rather than an error.
        for fs_type in db._NETWORK_FS_TYPES:
            db.DATA_DIR = Path("/mnt/x")
            with _with_mounts(f"overlay / overlay rw 0 0\nsrc /mnt/x {fs_type} rw 0 0\n"):
                self.assertEqual(db.data_dir_network_fs(), fs_type, fs_type)


class StartupWarningTests(unittest.TestCase):
    """main._network_data_dir_warning — the text an operator actually sees.
    Extracted from __main__ precisely so this is testable; the positive path
    otherwise needs a real NFS mount to exercise."""

    def setUp(self):
        self.main = main
        self._orig = db.DATA_DIR
        self.addCleanup(self._restore)

    def _restore(self):
        db.DATA_DIR = self._orig

    def test_no_warning_for_a_local_data_dir(self):
        db.DATA_DIR = Path("/data")
        with _with_mounts():
            self.assertIsNone(self.main._network_data_dir_warning())

    def test_warning_names_the_filesystem_and_the_stakes(self):
        db.DATA_DIR = Path("/smbdata")
        with _with_mounts():
            msg = self.main._network_data_dir_warning()
        self.assertIsNotNone(msg)
        assert msg is not None  # narrows for mypy
        self.assertIn("cifs", msg)              # the detected type, for credibility
        self.assertIn("CAN CORRUPT", msg)       # the stakes, unambiguously
        self.assertIn("/smbdata", msg)          # which path is wrong
        # Answers the underlying want instead of only forbidding: people do this
        # because they want the data backed up.
        self.assertIn("back it up there", msg)
        # And makes clear MUSIC_ROOT on a share is NOT the problem, since that's
        # the distinction the whole warning turns on.
        self.assertIn("MUSIC_ROOT is fine", msg)

    def test_warning_is_silent_when_the_filesystem_cannot_be_determined(self):
        db.DATA_DIR = Path("/data")
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertIsNone(self.main._network_data_dir_warning())


if __name__ == "__main__":
    unittest.main()
