// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// Regression coverage for #30: a rendered template can bind an <img> to
// @error="...visibility='hidden'" without a matching @load to reset it —
// since x-show only toggles `display`, one failed load then leaves the
// image stuck hidden forever, even after a later successful load. Neither
// py_compile nor dev/check_inline_js.py (which only checks the inline JS
// *parses*) can catch this: it's a runtime DOM behavior, not a syntax error.

test.describe("profile picture survives a transient load failure", () => {
  test("recovers to visible after an error is followed by a successful load", async ({ page }) => {
    await page.goto("/#/profile");

    const avatar = page.getByTestId("profile-avatar-img");
    await expect(avatar).toHaveCount(1);

    // Simulate the real-world sequence that triggers #30: a transient
    // network hiccup on the picture chooser's own <img> (error), then a
    // later successful load of the same src.
    await avatar.evaluate((el) => el.dispatchEvent(new Event("error")));
    await expect(avatar).toHaveCSS("visibility", "hidden");

    await avatar.evaluate((el) => el.dispatchEvent(new Event("load")));
    await expect(avatar).toHaveCSS("visibility", "visible");
  });
});

test.describe("every @error-bound image has a matching @load to recover from it", () => {
  // Structural companion to the behavioral test above: catches the same bug
  // class app-wide (covers, artist images, suggestions widgets, ...) and
  // keeps catching it as templates change, without hardcoding every
  // location — rather than re-deriving the current list of #30's 8 fixed
  // occurrences here and having it silently stop covering new ones.
  for (const route of ["/#/home", "/#/profile"]) {
    test(`on ${route}`, async ({ page }) => {
      await page.goto(route);

      const offenders = await page.evaluate(() =>
        Array.from(document.querySelectorAll("img[\\@error]"))
          .filter((img) => !img.hasAttribute("@load"))
          .map((img) => img.outerHTML),
      );

      expect(offenders).toEqual([]);
    });
  }
});
