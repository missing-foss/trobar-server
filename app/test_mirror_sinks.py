#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The mirror sink registry: every sink listed once, with a real column and
the cache keyword its write_mirror() actually takes, and write_all() /
delete_all() calling each the way playlist_sync always has.

    python3 -m unittest test_mirror_sinks -v      # from app/
"""
import inspect
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="trobar-test-mirror-sinks-")
os.environ["DATA_DIR"] = _TMP

import db  # noqa: E402
db.DATA_DIR = Path(_TMP)

import mirror_runs  # noqa: E402
import mirror_sinks  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_every_sink_is_listed_once_in_the_order_callers_always_used(self):
        keys = [s.key for s in mirror_sinks.SINKS]
        self.assertEqual(keys, ["filesystem", "subsonic", "jellyfin", "emby", "music_assistant", "plex"])
        self.assertEqual(sorted(keys[1:]), sorted(mirror_runs.SINKS))
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(set(mirror_sinks.BY_KEY), set(keys))

    def test_each_enabled_column_is_a_playlists_column(self):
        fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP)
        os.close(fd)
        db.DB_PATH = Path(path)
        db.init_db()
        conn = db.get_conn()
        try:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(playlists)")}
        finally:
            conn.close()
        for s in mirror_sinks.SINKS:
            with self.subTest(sink=s.key):
                self.assertIn(s.enabled_column, columns)

    def test_each_cache_keyword_is_one_its_write_mirror_takes(self):
        for s in mirror_sinks.SINKS:
            params = list(inspect.signature(s.backend.write_mirror).parameters)
            with self.subTest(sink=s.key):
                if s.cache_keyword is None:
                    self.assertEqual(len(params), 2, params)  # (conn, playlist_id)
                else:
                    self.assertEqual(params[2:], [s.cache_keyword])

    def test_new_caches_gives_each_cached_sink_its_own_dict(self):
        caches = mirror_sinks.new_caches()
        self.assertEqual(set(caches), {s.key for s in mirror_sinks.SINKS if s.cache_keyword})
        self.assertNotIn("filesystem", caches)
        self.assertEqual(len({id(c) for c in caches.values()}), len(caches))
        self.assertTrue(all(c == {} for c in caches.values()))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple] = []
        self.conn = mock.Mock()
        self.patches = []
        for s in mirror_sinks.SINKS:
            for fn in ("write_mirror", "delete_mirror"):
                p = mock.patch.object(
                    s.backend, fn,
                    side_effect=lambda *a, _k=s.key, _f=fn, **kw: self.calls.append((_k, _f, a, kw)))
                p.start()
                self.patches.append(p)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_write_all_without_caches_is_the_bare_call_in_order(self):
        mirror_sinks.write_all(self.conn, 7)
        self.assertEqual(self.calls, [(s.key, "write_mirror", (self.conn, 7), {}) for s in mirror_sinks.SINKS])

    def test_write_all_with_caches_hands_each_sink_its_own(self):
        caches = mirror_sinks.new_caches()
        mirror_sinks.write_all(self.conn, 7, caches, with_caches=True)
        self.assertEqual([c[0] for c in self.calls], [s.key for s in mirror_sinks.SINKS])
        for key, _fn, args, kw in self.calls:
            with self.subTest(sink=key):
                self.assertEqual(args, (self.conn, 7))
                sink = mirror_sinks.BY_KEY[key]
                if sink.cache_keyword is None:
                    self.assertEqual(kw, {})
                else:
                    self.assertEqual(list(kw), [sink.cache_keyword])
                    self.assertIs(kw[sink.cache_keyword], caches[key])

    def test_write_all_with_no_caches_passes_none_by_keyword(self):
        # What a sync of one playlist outside sync_playlists() passes.
        mirror_sinks.write_all(self.conn, 7, None, with_caches=True)
        for key, _fn, _args, kw in self.calls:
            keyword = mirror_sinks.BY_KEY[key].cache_keyword
            with self.subTest(sink=key):
                self.assertEqual(kw, {} if keyword is None else {keyword: None})

    def test_delete_all_deletes_from_every_sink_in_order(self):
        mirror_sinks.delete_all(self.conn, 7)
        self.assertEqual(self.calls, [(s.key, "delete_mirror", (self.conn, 7), {}) for s in mirror_sinks.SINKS])


def setUpModule():
    # unittest calls this every time it enters the module; _TMP is made once,
    # at import.
    os.makedirs(_TMP, exist_ok=True)


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
