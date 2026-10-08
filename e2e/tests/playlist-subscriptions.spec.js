// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// The "Add a public playlist by link" rows. A subscription can't be created
// here for real -- that would fetch from YouTube or Spotify -- so the list
// the page loads is answered with fixtures, and what's under test is how
// each row reads.
function sub(overrides) {
  return {
    id: 1, url: "", provider: "spotify_public", title: "E2E Playlist",
    last_synced_at: "2026-10-06 12:00:00", last_error: null, last_error_at: null,
    track_count: 50, matched_count: 20, track_limit: 100, ...overrides,
  };
}

async function gotoWithSubscriptions(page, subscriptions) {
  await page.route("**/api/playlist-subscriptions", (route) => {
    if (route.request().method() !== "GET") return route.continue();
    return route.fulfill({ json: { subscriptions } });
  });
  await page.goto("/#/playlists");
  await page.waitForFunction(
    () => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(() =>
    window.Alpine.$data(document.querySelector("[x-data]")).goToTab("playlists"));
}

const AT_LIMIT = "Spotify shares only the first 100 tracks of a playlist, so this one may be longer.";

test.describe("Public playlists by link", () => {
  test("a Spotify playlist at the limit says it may be longer; others don't", async ({ page }) => {
    await gotoWithSubscriptions(page, [
      sub({ id: 1, title: "E2E Spotify Full", track_count: 100, matched_count: 40 }),
      sub({ id: 2, title: "E2E Spotify Short", track_count: 50 }),
      sub({ id: 3, title: "E2E YouTube Long", provider: "ytmusic", track_limit: null,
            track_count: 250, matched_count: 30 }),
    ]);
    await expect(page.getByText("E2E Spotify Full")).toBeVisible();
    await expect(page.getByText("40 of 100 tracks in your library")).toBeVisible();
    // Exactly one shown: the Spotify playlist at 100, not the shorter one,
    // and not YouTube Music's, which returns every track. Every row renders
    // the note and x-show hides it, so only visible ones count.
    await expect(page.getByText(AT_LIMIT).locator("visible=true")).toHaveCount(1);
    await expect(page.getByText("30 of 250 tracks in your library")).toBeVisible();
  });

  test("a failed Spotify fetch names Spotify, and hides the limit note", async ({ page }) => {
    await gotoWithSubscriptions(page, [
      sub({ id: 4, title: "E2E Spotify Broken", last_error: "error", track_count: 100 }),
      sub({ id: 5, title: "E2E YouTube Broken", provider: "ytmusic", track_limit: null,
            last_error: "error" }),
    ]);
    await expect(page.getByText(
      "Couldn't read this playlist from Spotify. Nothing already imported was changed.")).toBeVisible();
    await expect(page.getByText(
      "Couldn't reach YouTube Music. Nothing already imported was changed.")).toBeVisible();
    await expect(page.getByText(AT_LIMIT).locator("visible=true")).toHaveCount(0);
  });
});
