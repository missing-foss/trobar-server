// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// The Library by album artist: an artist's page lists the albums by others
// they play on under "Also appears on", each a link to its album artist, and
// the artist list says "appears on N". The library routes are stubbed.

/** @param {import('@playwright/test').Page} page */
async function gotoLibrary(page) {
  await page.route("**/api/library/artists", (route) => route.fulfill({ json: [
    { artist: "Low Orbit", track_count: 0, album_count: 0, appears_on_count: 1, albums: [] },
    { artist: "Various Artists", track_count: 14, album_count: 1, appears_on_count: 0, albums: ["Night Drive"] },
  ] }));
  await page.route("**/api/library/albums?*", (route) => {
    const artist = new URL(route.request().url()).searchParams.get("artist");
    return route.fulfill({ json: artist === "Various Artists"
      ? [{ album: "Night Drive", year: 2025, reissue_year: null, track_count: 14 }] : [] });
  });
  await page.route("**/api/library/appears-on?*", (route) => {
    const artist = new URL(route.request().url()).searchParams.get("artist");
    return route.fulfill({ json: artist === "Low Orbit"
      ? [{ album: "Night Drive", album_artist: "Various Artists", year: 2025, track_count: 1 }] : [] });
  });
  await page.goto("/#/library");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  // init() restores the view from the URL hash once its first loads are
  // back, and replaceState is the last thing it does there. Navigating
  // before that lets the restore read "#/library" mid-navigation and clear
  // the artist, and lets the profile load overwrite the view mode set below.
  await page.waitForFunction(() => history.state !== null);
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.profile.cover_view_mode = "list";
    await d.loadArtists();
  });
}

test.describe("Library by album artist", () => {
  test("an artist known only from others' albums says so, and lists them", async ({ page }) => {
    await gotoLibrary(page);
    await expect(page.getByRole("button", { name: /Low Orbit\s+appears on 1/ })).toBeVisible();
    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).selectArtist("Low Orbit"));
    await expect(page.getByRole("heading", { name: "Also appears on" })).toBeVisible();
    const box = page.getByRole("checkbox", { name: "Low Orbit on Night Drive" });
    await expect(box).toHaveValue("Low Orbit||Night Drive");
    await expect(page.locator("div.flex.items-center", { has: box }).first()
      .getByRole("button", { name: "Add to playlist…" })).toBeVisible();
    const results = await new AxeBuilder({ page }).include("section[x-show=\"tab==='library'\"]").analyze();
    expect(results.violations).toEqual([]);
    await page.getByRole("button", { name: "Various Artists", exact: true }).last().click();
    await expect(page.locator("h2", { hasText: "Various Artists" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Also appears on" })).toBeHidden();
  });
});
