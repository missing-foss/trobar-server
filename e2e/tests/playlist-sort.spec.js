// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// The Playlists tab's Sort: each order, ties by name, a playlist with no
// tracks, and the choice kept by the browser across a reload. The list is
// served from a stub, so the orders are exact.

// Ties on purpose: Focus and Workout are both 100 % available and Focus,
// Sunday morning and kids' songs share track counts with someone.
const rows = [
  { id: 12, title: "Road trip mix", track_count: 41, matched_count: 38 },
  { id: 30, title: "Sunday morning", track_count: 120, matched_count: 60 },
  { id: 7, title: "Focus", track_count: 18, matched_count: 18 },
  { id: 41, title: "kids' songs", track_count: 18, matched_count: 3 },
  { id: 25, title: "Night drive", track_count: 120, matched_count: 40 },
  { id: 3, title: "Workout", track_count: 75, matched_count: 75 },
  { id: 50, title: "Empty", track_count: 0, matched_count: 0 },
];

const expected = {
  // 100, 100 (by name), 93, 50, 33, 17, then the empty one at 0.
  availability: ["Focus", "Workout", "Road trip mix", "Sunday morning", "Night drive", "kids' songs", "Empty"],
  // 120, 120 (by name), 75, 41, 18, 18 (by name, ignoring case), 0.
  tracks: ["Night drive", "Sunday morning", "Workout", "Road trip mix", "Focus", "kids' songs", "Empty"],
  newest: ["Empty", "kids' songs", "Sunday morning", "Night drive", "Road trip mix", "Focus", "Workout"],
  // Case doesn't count: "kids' songs" sits between Focus and Night drive.
  name: ["Empty", "Focus", "kids' songs", "Night drive", "Road trip mix", "Sunday morning", "Workout"],
};

let template;

test.beforeAll(async ({ request }) => {
  const made = await (await request.post("/api/provider/playlists", { data: { title: "E2E sort template" } })).json();
  const all = await (await request.get("/api/provider/playlists")).json();
  template = all.find((p) => p.id === made.id);
  await request.delete(`/api/provider/playlists/${made.id}`);
});

async function open(page) {
  const list = rows.map((r) => ({ ...template, ...r, source_provider: null, shared: 1, unresolved_count: 0 }));
  await page.route("**/api/provider/playlists", (route) =>
    route.request().method() === "GET" ? route.fulfill({ json: list }) : route.continue());
  await page.goto("/#/playlists");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const a = window.Alpine.$data(document.querySelector("[x-data]"));
    a.goToTab("playlists");
    await a.loadPlaylists();
  });
}

const shownTitles = (page) => page.evaluate(() =>
  window.Alpine.$data(document.querySelector("[x-data]")).sortedPlaylists().map((p) => p.title));

test("each sort orders the list, ties by name", async ({ page }) => {
  await open(page);
  await page.evaluate(() => localStorage.removeItem("playlistSort"));
  for (const [key, titles] of Object.entries(expected)) {
    await page.locator("[data-playlist-sort]").selectOption(key);
    expect(await shownTitles(page), key).toEqual(titles);
  }
  // The rows on the page follow it, not just the list behind them.
  await page.locator("[data-playlist-sort]").selectOption("tracks");
  await expect(page.locator("[data-playlist-row]").first()).toContainText("Night drive");
});

test("the choice is remembered by the browser", async ({ page }) => {
  await open(page);
  await page.locator("[data-playlist-sort]").selectOption("name");
  await page.reload();
  await open(page);
  await expect(page.locator("[data-playlist-sort]")).toHaveValue("name");
  expect(await shownTitles(page)).toEqual(expected.name);
});

test("availability is the default", async ({ page }) => {
  await open(page);
  await page.evaluate(() => localStorage.removeItem("playlistSort"));
  await page.reload();
  await open(page);
  await expect(page.locator("[data-playlist-sort]")).toHaveValue("availability");
  expect(await shownTitles(page)).toEqual(expected.availability);
});
