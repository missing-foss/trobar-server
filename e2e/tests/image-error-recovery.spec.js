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

    // toHaveCount(1) proves the <img> is in the SERVER-RENDERED DOM. It does
    // not prove Alpine has attached the @error handler this test is about, and
    // an event dispatched before that handler exists is simply lost -- after
    // which retrying the assertion below can never recover, because nothing
    // re-dispatches. The failure would read as "the element is there and the
    // style never changed", which is indistinguishable from the bug this test
    // exists to catch.
    //
    // So wait for Alpine to have initialised the component before dispatching.
    // This is the same handle the other specs use to reach app state.
    //
    // Alpine being initialised is NOT enough on its own, and this is what
    // made the test flaky in CI. The <img> is `:src`-bound to
    // profile.avatar_url, and `profile` arrives from an async fetch that
    // init() only STARTS (loadProfile(), alongside the other start-up
    // loads). So Alpine can be initialised, and this wait satisfied, while
    // that fetch is still in flight -- and when it lands, Alpine sets the
    // binding, the browser loads the image, and the real `load` event fires
    // `visibility='visible'`, undoing the synthetic error dispatched below.
    // Nothing re-dispatches after that, so the assertion polls a stable
    // "visible" until it times out: the exact reading recorded when this
    // failed, and again indistinguishable from the bug under test.
    //
    // Waiting for the image to have COMPLETED its own load closes it: the
    // synthetic sequence then owns the visibility, because there is no
    // in-flight load left to overwrite it.
    await page.waitForFunction(() => {
      const root = document.querySelector("[x-data]");
      const data = window.Alpine && window.Alpine.$data(root);
      if (!data || !data.profile || !data.profile.avatar_url) return false;
      const img = document.querySelector("[data-testid='profile-avatar-img']");
      return !!img && img.complete;
    });

    // Simulate the real-world sequence that triggers #30: a transient
    // network hiccup on the picture chooser's own <img> (error), then a
    // later successful load of the same src.
    await avatar.evaluate((el) => el.dispatchEvent(new Event("error")));
    await expect(avatar).toHaveCSS("visibility", "hidden");

    await avatar.evaluate((el) => el.dispatchEvent(new Event("load")));
    await expect(avatar).toHaveCSS("visibility", "visible");
  });

  test("an error during an in-flight load still recovers when that load lands", async ({ page }) => {
    // The case above dispatches BOTH events synthetically, so it cannot
    // exercise ordering: the "recovery" it proves is a load event we fired
    // ourselves, at a moment we chose. The failure that actually happened in
    // CI was about a REAL load arriving at a moment nobody chose.
    //
    // So hold the image response, error while it is genuinely in flight, and
    // release it. Deterministic by construction -- the route awaits a promise
    // this test resolves, not a sleep -- so this cannot itself become a race.
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route("**/api/profile/avatar-image", async (route) => {
      await held;
      await route.continue();
    });

    await page.goto("/#/profile");
    const avatar = page.getByTestId("profile-avatar-img");
    await expect(avatar).toHaveCount(1);

    // Alpine initialised AND the binding applied, so the request has been
    // issued -- and it is still outstanding, because the route is holding it.
    await page.waitForFunction(() => {
      const data = window.Alpine
        && window.Alpine.$data(document.querySelector("[x-data]"));
      return !!(data && data.profile && data.profile.avatar_url);
    });

    // Assert the precondition rather than assume it. If the hold ever stops
    // working -- a changed URL, a cache, a route that no longer matches --
    // the image would already be loaded here and everything below would pass
    // while testing nothing at all.
    expect(await avatar.evaluate((el) => el.complete)).toBe(false);

    await avatar.evaluate((el) => el.dispatchEvent(new Event("error")));
    await expect(avatar).toHaveCSS("visibility", "hidden");

    // The real load, arriving after the error. This is the recovery #30 is
    // about, driven by the browser rather than by us.
    release();
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
