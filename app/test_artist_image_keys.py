#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The artist-image key map the provider clients share: built once, under a
lock, until cleared.

    python3 -m unittest test_artist_image_keys -v      # from app/
"""
import threading
import time
import unittest

from artist_image_keys import ArtistImageKeys


class ArtistImageKeysTests(unittest.TestCase):
    def test_the_map_is_built_once_then_answered_from_memory(self):
        keys = ArtistImageKeys()
        builds = []

        def build():
            builds.append(1)
            return {"Aphelion": "img-1"}

        self.assertEqual(keys.get(build), {"Aphelion": "img-1"})
        self.assertEqual(keys.get(build)["Aphelion"], "img-1")
        self.assertEqual(len(builds), 1)

    def test_two_first_lookups_at_once_make_one_walk(self):
        keys = ArtistImageKeys()
        builds = []
        started = threading.Event()

        def slow_build():
            builds.append(1)
            started.set()
            time.sleep(0.2)  # long enough for the second lookup to arrive
            return {"Aphelion": "img-1"}

        results = []
        first = threading.Thread(target=lambda: results.append(keys.get(slow_build)))
        first.start()
        self.assertTrue(started.wait(5))
        second = threading.Thread(target=lambda: results.append(keys.get(slow_build)))
        second.start()
        first.join(5)
        second.join(5)
        self.assertEqual(len(builds), 1)
        self.assertEqual(results, [{"Aphelion": "img-1"}] * 2)

    def test_clear_makes_the_next_lookup_build_again(self):
        keys = ArtistImageKeys()
        keys.get(lambda: {"old": "1"})
        keys.clear()
        self.assertEqual(keys.get(lambda: {"new": "2"}), {"new": "2"})

    def test_an_empty_map_is_kept_too(self):
        # A provider with no artist images builds {}: that is an answer, not
        # a reason to walk the artist list again on every lookup.
        keys = ArtistImageKeys()
        builds = []

        def build_empty():
            builds.append(1)
            return {}

        keys.get(build_empty)
        keys.get(build_empty)
        self.assertEqual(len(builds), 1)


if __name__ == "__main__":
    unittest.main()
