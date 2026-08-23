// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// #508: the "duel the bard" Easter egg. global-setup seeds an empty library
// (see e2e/README.md) — genuinely zero tracks, no provider connected — so
// /api/library/quiz-pair's real response in this harness is always
// {available: false}. That's exercised for real below (no mocking needed);
// the guess/reveal interaction is tested by injecting a deterministic pair
// directly into app.quiz, the same established pattern staging-basket.spec.js
// and playlists.spec.js use for state this harness can't produce for real.
// The pair-selection ALGORITHM itself (gap thresholds, Various Artists
// exclusion, ...) has its own thorough, DB-free unit tests in
// app/test_library_quiz.py — nothing here re-tests that.

async function gotoAbout(page) {
  await page.goto("/#/about");
  await page.evaluate(() =>
    window.Alpine.$data(document.querySelector("[x-data]")).goToTab("about"));
  await page.waitForTimeout(200);
}

test.describe("duel-the-bard trigger (#508)", () => {
  test("five taps on the bard mark opens the quiz", async ({ page }) => {
    await gotoAbout(page);
    const bard = page.getByRole("button", { name: "Trobar bard mark" });
    for (let i = 0; i < 5; i++) await bard.click();
    await expect(page.getByText("Duel the bard")).toBeVisible();
  });

  test("fewer than five taps does nothing", async ({ page }) => {
    await gotoAbout(page);
    const bard = page.getByRole("button", { name: "Trobar bard mark" });
    for (let i = 0; i < 4; i++) await bard.click();
    await expect(page.getByText("Duel the bard")).not.toBeVisible();
  });

  test("a gap longer than the tap window resets the count instead of accumulating forever", async ({ page }) => {
    await gotoAbout(page);
    const bard = page.getByRole("button", { name: "Trobar bard mark" });
    await bard.click();
    await bard.click();
    // Longer than tapBard()'s own 1500ms reset window.
    await page.waitForTimeout(1800);
    await bard.click();
    await bard.click();
    await bard.click();
    // 2 (before the gap, discarded) + 3 (after) — never reaches 5 in one run.
    await expect(page.getByText("Duel the bard")).not.toBeVisible();
  });
});

test.describe("duel-the-bard empty-library state (#508)", () => {
  test("opening the quiz against this harness's real (empty) library shows the not-enough-data message", async ({ page }) => {
    await gotoAbout(page);
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).openQuiz();
    });
    await expect(page.getByText("Duel the bard")).toBeVisible();
    await expect(page.getByText(/Not enough of a library yet/)).toBeVisible();
  });
});

test.describe("duel-the-bard guess/reveal (#508)", () => {
  // Opens the modal via the real openQuiz() (exercising the same code path
  // taps trigger) but then overrides the fetched pair with a deterministic
  // one — this harness's library is genuinely empty, so there's no way to
  // get a real {available: true} pair to interact with otherwise.
  async function openWithFixedPair(page) {
    await gotoAbout(page);
    await page.evaluate(async () => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      await app.openQuiz();
      app.quiz.available = true;
      app.quiz.loading = false;
      app.quiz.a = { artist: "Small Artist", album_count: 3 };
      app.quiz.b = { artist: "Big Artist", album_count: 20 };
    });
    await page.waitForTimeout(100);
  }

  test("guessing the artist with fewer albums reveals the correct answer", async ({ page }) => {
    await openWithFixedPair(page);
    await page.getByRole("button", { name: "Small Artist" }).click();

    // "Not quite — Big Artist actually has 20 albums, Small Artist has 3 albums."
    await expect(page.getByText(/Not quite.+Big Artist actually has 20 albums.+Small Artist has 3 albums/)).toBeVisible();
    // Per-card album counts only appear once a guess has been made, not
    // before — scoped to the card itself since the reveal text above now
    // also legitimately contains "3 albums" as a substring.
    await expect(page.getByRole("button", { name: "Small Artist" })).toContainText("3 albums");
  });

  test("guessing the artist with more albums is marked correct", async ({ page }) => {
    await openWithFixedPair(page);
    await page.getByRole("button", { name: "Big Artist" }).click();

    await expect(page.getByText(/^Correct/)).toBeVisible();
    const bigCard = page.getByRole("button", { name: "Big Artist" });
    await expect(bigCard).toHaveClass(/border-green-600/);
  });

  test("the wrongly-picked card is marked red, the actual winner green", async ({ page }) => {
    await openWithFixedPair(page);
    await page.getByRole("button", { name: "Small Artist" }).click();

    await expect(page.getByRole("button", { name: "Small Artist" })).toHaveClass(/border-red-500/);
    await expect(page.getByRole("button", { name: "Big Artist" })).toHaveClass(/border-green-600/);
  });

  test("cards are unclickable after a guess is made", async ({ page }) => {
    await openWithFixedPair(page);
    await page.getByRole("button", { name: "Small Artist" }).click();
    await expect(page.getByRole("button", { name: "Big Artist" })).toBeDisabled();
  });

  test("Play again fetches a new round (loading, then this harness's real empty-library state)", async ({ page }) => {
    await openWithFixedPair(page);
    await page.getByRole("button", { name: "Small Artist" }).click();
    await expect(page.getByText(/Not quite/)).toBeVisible();

    await page.getByRole("button", { name: "Play again" }).click();
    // loadQuizPair() hits the real endpoint on this click — the harness's
    // library is empty, so the round after "Play again" genuinely reports
    // unavailable, same as the dedicated empty-library test above.
    await expect(page.getByText(/Not enough of a library yet/)).toBeVisible();
  });

  test("Close dismisses the modal", async ({ page }) => {
    await openWithFixedPair(page);
    await page.getByRole("button", { name: "Close" }).click();
    await expect(page.getByText("Duel the bard")).not.toBeVisible();
  });
});
