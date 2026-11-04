// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// A linked account that failed its last sync says so on the Playlists page,
// without anyone pressing Refresh: three months of silence hid one before.

async function status(page, last_result) {
  await page.route("**/api/provider/playlists/sync/status", (route) =>
    route.fulfill({ json: { running: false, last_result } }));
}

async function gotoPlaylists(page) {
  await page.goto("/");
  await page.waitForFunction(
    () => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(() => {
    window.Alpine.$data(document.querySelector("[x-data]")).goToTab("playlists");
  });
}

const failed = { status: "ok", source_failures: [
  { provider: "tidal", user_id: 1, username: "alice", reason: "http_403" },
] };

test("a failed source shows on arrival and can be dismissed", async ({ page }) => {
  await status(page, failed);
  await gotoPlaylists(page);
  const banner = page.locator("[data-source-failure]");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Tidal");
  await expect(banner).toContainText("alice");
  await expect(banner).toContainText("http_403");
  await banner.getByRole("button").click();
  await expect(banner).toHaveCount(0);
});

test("a healthy sync shows nothing", async ({ page }) => {
  await status(page, { status: "ok" });
  await gotoPlaylists(page);
  await expect(page.getByRole("button", { name: "New playlist", exact: true })).toBeVisible();
  await expect(page.locator("[data-source-failure]")).toHaveCount(0);
});
