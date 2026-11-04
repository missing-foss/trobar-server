# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""history_cache: a listening-history service's raw answer is kept for a few
minutes, an outage never is, and clear() forgets everything."""

import unittest
from unittest import mock

import history_cache


class CacheTests(unittest.TestCase):
    def setUp(self):
        history_cache.clear()
        self.addCleanup(history_cache.clear)
        self.calls: list[tuple[str, str]] = []
        self.answer: list[dict] | None = [{"album": "Latitude"}]

        @history_cache.cached("test.fetch")
        def fetch(user, period="6month"):
            self.calls.append((user, period))
            return self.answer
        self.fetch = fetch

    def test_a_second_call_within_the_ttl_is_answered_from_memory(self):
        self.assertEqual(self.fetch("alice"), [{"album": "Latitude"}])
        self.assertEqual(self.fetch("alice"), [{"album": "Latitude"}])
        self.assertEqual(self.calls, [("alice", "6month")])

    def test_other_arguments_are_another_entry(self):
        self.fetch("alice")
        self.fetch("bob")
        self.fetch("alice", period="12month")
        self.assertEqual(len(self.calls), 3)

    def test_an_outage_is_never_kept(self):
        self.answer = None
        self.assertIsNone(self.fetch("alice"))
        self.answer = [{"album": "Back"}]
        self.assertEqual(self.fetch("alice"), [{"album": "Back"}])
        self.assertEqual(len(self.calls), 2)

    def test_an_answer_older_than_the_ttl_is_asked_again(self):
        with mock.patch.object(history_cache.time, "monotonic", return_value=1000.0):
            self.fetch("alice")
        with mock.patch.object(history_cache.time, "monotonic", return_value=1000.0 + history_cache.TTL_SECONDS):
            self.fetch("alice")
        self.assertEqual(len(self.calls), 2)

    def test_clear_forgets_everything(self):
        self.fetch("alice")
        history_cache.clear()
        self.fetch("alice")
        self.assertEqual(len(self.calls), 2)

    def test_a_caller_changing_its_copy_does_not_change_the_kept_one(self):
        self.fetch("alice")[0]["album"] = "changed"            # the service's own answer
        self.fetch("alice")[0]["album"] = "changed from memory"  # one given from memory
        self.assertEqual(self.fetch("alice"), [{"album": "Latitude"}])

    def test_past_the_limit_the_oldest_entry_goes(self):
        with mock.patch.object(history_cache, "_MAX_ENTRIES", 2):
            for t, user in enumerate(("a", "b", "c")):
                with mock.patch.object(history_cache.time, "monotonic", return_value=float(t)):
                    self.fetch(user)
            with mock.patch.object(history_cache.time, "monotonic", return_value=3.0):
                self.fetch("b")
                self.fetch("a")
        self.assertEqual([u for u, _ in self.calls], ["a", "b", "c", "a"])


if __name__ == "__main__":
    unittest.main()
