// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// The playlist list on page load: it waits for no other startup request, and
// says it is loading rather than that there are no playlists.

const PLAYLIST = {
  id: 901, title: "Startup list", source_provider: "trobar", is_own: true,
  track_count: 1, matched_count: 1, created_at: "2026-01-01 00:00:00",
};

const loading = (page) => page.locator("[data-playlists-loading]");
const empty = (page) => page.locator("[data-playlists-empty]");

/** Holds a route until the returned function is called. */
async function hold(page, pattern) {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route(pattern, async (route) => {
    await gate;
    return route.continue().catch(() => {});
  });
  return release;
}

test("the list arrives while /api/selections is still out", async ({ page }) => {
  const release = await hold(page, "**/api/selections");
  await page.route("**/api/provider/playlists", (route) => route.fulfill({ json: [PLAYLIST] }));
  // By hand: the page restores a tab from its address after those requests.
  await page.goto("/");
  await page.getByRole("button", { name: "Playlists", exact: true }).filter({ visible: true }).first().click();
  await expect(page.locator("[data-playlist-row]", { hasText: "Startup list" })).toBeVisible();
  await expect(empty(page)).toBeHidden();
  await expect(loading(page)).toBeHidden();
  release();
});

test("while the list loads it says so, and no empty message shows", async ({ page }) => {
  let answer;
  const gate = new Promise((resolve) => { answer = resolve; });
  await page.route("**/api/provider/playlists", async (route) => {
    await gate;
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Playlists", exact: true }).filter({ visible: true }).first().click();
  await expect(loading(page)).toBeVisible();
  await expect(empty(page)).toBeHidden();
  answer();
  await expect(empty(page)).toBeVisible();
  await expect(loading(page)).toBeHidden();
});

test("a list that fails to load stops saying it is loading", async ({ page }) => {
  await page.route("**/api/provider/playlists", (route) => route.fulfill({
    status: 500, json: { error: "E2E: no playlists today." },
  }));
  await page.goto("/#/playlists");
  await expect(page.getByText("E2E: no playlists today.")).toBeVisible();
  await expect(loading(page)).toBeHidden();
});
