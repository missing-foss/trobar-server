// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// A listening-history source that didn't answer is said so, with Retry,
// instead of an empty grid that reads as "nothing played". The feeds are
// stubbed: which source is down is the point, and no real one is reachable
// from the e2e stack.

const ALBUM = { artist: "Glass Meridian", album: "Latitude", library_artist: "Glass Meridian",
  library_album: "Latitude", source: "listenbrainz", playcount: 41 };

/** @param {import('@playwright/test').Page} page */
async function stubFeeds(page, sources, items) {
  /** @type {string[]} */
  const asked = [];
  for (const path of ["**/api/suggestions?*", "**/api/suggestions/most-played?*"]) {
    await page.route(path, (route) => {
      asked.push(route.request().url());
      return route.fulfill({ json: { items, sources } });
    });
  }
  return asked;
}

// The header's source dots: counted, so Retry can be seen to ask again.
/** @param {import('@playwright/test').Page} page */
async function countStatusChecks(page) {
  const counts = { lastfm: 0, listenbrainz: 0 };
  for (const name of Object.keys(counts)) {
    await page.route(`**/api/${name}/status`, (route) => {
      counts[name] += 1;
      return route.fulfill({ json: { configured: true, ok: name !== "lastfm" } });
    });
  }
  return counts;
}

/** @param {import('@playwright/test').Page} page */
async function gotoSuggestions(page) {
  await page.goto("/#/lastfm");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.profile.lastfm_username = "e2e";
    d.profile.listenbrainz_username = "e2e";
    d.goToTab("lastfm");
    await d.loadSuggestions();
  });
}

const section = (page) => page.locator("section[x-show=\"tab==='lastfm'\"]");

test.describe("A history source that didn't answer", () => {
  test("one down: a notice above what the others found, with Retry", async ({ page }) => {
    const asked = await stubFeeds(page, { lastfm: "error", listenbrainz: "ok", maloja: "unconfigured" }, [ALBUM]);
    const checks = await countStatusChecks(page);
    await gotoSuggestions(page);
    const notice = section(page).getByText("Last.fm didn't answer, so its most-played albums are missing below.");
    await expect(notice).toBeVisible();
    await expect(section(page).getByText("Latitude")).toBeVisible();
    const before = asked.length;
    const dots = { ...checks };
    await section(page).getByRole("button", { name: "Retry" }).click();
    await expect.poll(() => asked.length).toBeGreaterThan(before);
    // Retry asks the services again, past the server's few minutes' memory;
    // the page's own load doesn't.
    expect(asked[before - 1]).not.toContain("fresh=1");
    expect(asked.at(-1)).toContain("fresh=1");
    // The header's dots are asked again too, for each configured source.
    await expect.poll(() => checks.lastfm).toBeGreaterThan(dots.lastfm);
    await expect.poll(() => checks.listenbrainz).toBeGreaterThan(dots.listenbrainz);
    const results = await new AxeBuilder({ page }).include("section[x-show=\"tab==='lastfm'\"]").analyze();
    expect(results.violations).toEqual([]);
  });

  test("none answered: the notice replaces the empty grid", async ({ page }) => {
    await stubFeeds(page, { lastfm: "error", listenbrainz: "error", maloja: "unconfigured" }, []);
    await gotoSuggestions(page);
    await expect(section(page).getByText("Last.fm and ListenBrainz didn't answer", { exact: true })).toBeVisible();
    await expect(section(page).getByText('No suggestions yet — click "Refresh".')).toBeHidden();
  });

  test("all answered and nothing found: the plain empty text", async ({ page }) => {
    await stubFeeds(page, { lastfm: "ok", listenbrainz: "ok", maloja: "unconfigured" }, []);
    await gotoSuggestions(page);
    await expect(section(page).getByText('No suggestions yet — click "Refresh".')).toBeVisible();
    await expect(section(page).getByRole("status")).toHaveCount(0);
  });

  test("the period is sent, and remembered", async ({ page }) => {
    const asked = await stubFeeds(page, { lastfm: "ok", listenbrainz: "ok", maloja: "unconfigured" }, [ALBUM]);
    await gotoSuggestions(page);
    expect(asked.at(-1)).toContain("period=6month");
    await section(page).getByLabel("Most played over").selectOption({ label: "Last year" });
    await expect.poll(() => asked.at(-1)).toContain("period=12month");
    await page.reload();
    await gotoSuggestions(page);
    await expect(section(page).getByLabel("Most played over")).toHaveValue("12month");
    expect(asked.at(-1)).toContain("period=12month");
  });

  test("the home Most played widget says so instead of 'Nothing played yet.'", async ({ page }) => {
    await stubFeeds(page, { lastfm: "error", listenbrainz: "unconfigured", maloja: "unconfigured" }, []);
    const checks = await countStatusChecks(page);
    await page.goto("/#/home");
    await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
    await page.evaluate(async () => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      d.profile.lastfm_username = "e2e";
      d.profile.dashboard_widgets = { disabled: [] };
      await d.loadMostPlayed();
    });
    await expect(page.getByRole("heading", { name: "Most played" })).toBeVisible();
    await expect(page.getByText("Last.fm didn't answer, so the chart can't be drawn right now.")).toBeVisible();
    await expect(page.getByText("Nothing played yet.")).toBeHidden();
    await expect(page.getByLabel("Over", { exact: true })).toHaveValue("6month");
    const dots = checks.lastfm;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect.poll(() => checks.lastfm).toBeGreaterThan(dots);
  });
});
