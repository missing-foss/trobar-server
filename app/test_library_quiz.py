#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for library_quiz.py's #508 pair-selection logic.

    python3 -m unittest test_library_quiz -v      # from app/
"""
import random
import unittest

import library_quiz


def _artist(name: str, album_count: int) -> dict:
    return {"artist": name, "album_count": album_count}


class IsEligibleArtistTests(unittest.TestCase):
    def test_ordinary_name_is_eligible(self):
        self.assertTrue(library_quiz.is_eligible_artist("Radiohead"))

    def test_various_artists_excluded_case_insensitively(self):
        self.assertFalse(library_quiz.is_eligible_artist("Various Artists"))
        self.assertFalse(library_quiz.is_eligible_artist("various artists"))
        self.assertFalse(library_quiz.is_eligible_artist("VARIOUS ARTISTS"))

    def test_bare_various_excluded(self):
        self.assertFalse(library_quiz.is_eligible_artist("Various"))

    def test_empty_or_none_excluded(self):
        self.assertFalse(library_quiz.is_eligible_artist(""))
        self.assertFalse(library_quiz.is_eligible_artist("   "))
        self.assertFalse(library_quiz.is_eligible_artist(None))


class EligibleCandidatesTests(unittest.TestCase):
    def test_filters_out_low_album_counts(self):
        rows = [_artist("A", library_quiz.MIN_ALBUMS - 1), _artist("B", library_quiz.MIN_ALBUMS)]
        self.assertEqual(library_quiz.eligible_candidates(rows), [rows[1]])

    def test_filters_out_excluded_names(self):
        rows = [_artist("Various Artists", 50), _artist("Real Artist", 10)]
        self.assertEqual(library_quiz.eligible_candidates(rows), [rows[1]])

    def test_missing_album_count_treated_as_zero(self):
        rows = [{"artist": "No Count Field"}]
        self.assertEqual(library_quiz.eligible_candidates(rows), [])


class PickPairTests(unittest.TestCase):
    def test_fewer_than_two_candidates_returns_none(self):
        self.assertIsNone(library_quiz.pick_pair([]))
        self.assertIsNone(library_quiz.pick_pair([_artist("Solo", 10)]))

    def test_all_tied_returns_none(self):
        candidates = [_artist(f"Artist {i}", 5) for i in range(6)]
        self.assertIsNone(library_quiz.pick_pair(candidates, rng=random.Random(1)))

    def _assert_pair(self, candidates, rng) -> tuple[dict, dict]:
        """pick_pair()'s return type is Optional — every real test here
        wants a concrete pair to make assertions about, so this is the one
        place that turns "None" into a test failure (assertIsNotNone) AND
        narrows the type for mypy (the `assert` right after it, which
        assertIsNotNone alone doesn't do)."""
        pair = library_quiz.pick_pair(candidates, rng=rng)
        self.assertIsNotNone(pair)
        assert pair is not None
        return pair

    def test_a_real_gap_pair_is_found(self):
        candidates = [_artist("Small", 3), _artist("Big", 20)]
        pair = self._assert_pair(candidates, random.Random(1))
        names = {pair[0]["artist"], pair[1]["artist"]}
        self.assertEqual(names, {"Small", "Big"})

    def test_close_pair_rejected_in_favor_of_a_real_gap(self):
        # 4 vs 5 (the issue's own "not a coin flip" example) must never be
        # returned when a real-gap pair is available instead. Covers the
        # _MIN_GAP half of _has_real_gap only — see the ratio test below for
        # the other half, which this alone doesn't exercise (30 vs either
        # close artist clears BOTH _MIN_GAP and _MIN_RATIO with room to
        # spare, so a mutation dropping the ratio term entirely wouldn't
        # change this test's outcome).
        candidates = [_artist("Close A", 4), _artist("Close B", 5), _artist("Big", 30)]
        for seed in range(20):
            pair = self._assert_pair(candidates, random.Random(seed))
            names = {pair[0]["artist"], pair[1]["artist"]}
            self.assertNotEqual(names, {"Close A", "Close B"})

    def test_near_coin_flip_ratio_is_rejected_even_though_the_gap_is_wide(self):
        # #519: 20 vs 25 is the module's own worked example of what _MIN_RATIO
        # is FOR — a gap of 5 clears _MIN_GAP (2) on its own, but 25/20 is only
        # a 1.25x edge, well under _MIN_RATIO (1.4), and still reads as a
        # near-coin-flip. Without this test, dropping the ratio term from
        # _has_real_gap left every other case here passing (#519's own
        # mutation-testing finding) — none of the existing candidate sets
        # elsewhere in this file have a pair that clears the gap floor but
        # fails the ratio floor.
        candidates = [_artist("Close A", 20), _artist("Close B", 25), _artist("Big", 100)]
        for seed in range(20):
            pair = self._assert_pair(candidates, random.Random(seed))
            names = {pair[0]["artist"], pair[1]["artist"]}
            self.assertNotEqual(names, {"Close A", "Close B"})

    def test_falls_back_to_widest_gap_when_nothing_meets_the_threshold(self):
        # #519: the ORIGINAL version of this test used 4 candidates spaced 1
        # album apart (MIN_ALBUMS, +1, +2, +3) on the theory that "every
        # ADJACENT gap is 1, so nothing qualifies" — but _has_real_gap is
        # checked across every NEARBY pair, not just adjacent ones, and
        # MIN_ALBUMS vs MIN_ALBUMS+2 (gap 2, ratio 1.67) already clears the
        # threshold on its own. That meant this test exercised the PRIMARY
        # search path, not the fallback it was named for, and a mutation
        # deleting the fallback entirely (`return None` right before it)
        # still passed (#519's own finding). Two candidates with a 1-album,
        # sub-ratio gap is the minimal case that actually forces the
        # fallback: no other pair exists to satisfy the primary search, so
        # returning a pair at all proves the fallback ran.
        candidates = [_artist("A", 4), _artist("B", 5)]
        pair = self._assert_pair(candidates, random.Random(1))
        names = {pair[0]["artist"], pair[1]["artist"]}
        self.assertEqual(names, {"A", "B"})
        # #519: the issue's own third suggested case (a minimal 2-candidate
        # tie) turns out to exercise the exact same fallback tie-check code
        # path test_all_tied_returns_none above already covers (6 candidates
        # all tied at count=5 also never satisfies _has_real_gap for any
        # pair, so it falls through to and hits the identical tie-guard) —
        # confirmed by tracing pick_pair's own control flow, not just
        # asserted. Not added, per the issue's own "worth checking before
        # adding, rather than duplicating."

    def test_returned_order_is_not_always_the_same(self):
        # Order should vary with the rng rather than always putting the
        # bigger (or smaller) count first — otherwise the "which is bigger"
        # question would be answerable from position alone.
        candidates = [_artist("Small", 3), _artist("Big", 20)]
        firsts = {
            self._assert_pair(candidates, random.Random(seed))[0]["artist"]
            for seed in range(30)
        }
        self.assertEqual(firsts, {"Small", "Big"})

    def test_never_returns_two_of_the_same_candidate(self):
        candidates = [_artist("Small", 3), _artist("Big", 20)]
        for seed in range(20):
            a, b = self._assert_pair(candidates, random.Random(seed))
            self.assertNotEqual(a["artist"], b["artist"])


if __name__ == "__main__":
    unittest.main()
