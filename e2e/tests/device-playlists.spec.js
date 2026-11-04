// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// A playlist made on a device, uploaded by its client the way the Android
// and desktop apps do, shows in the Playlists list with that device's icon
// and "From <device>" as its tooltip. Deleting the device removes it.

// playlists.spec.js toggles the shared e2e user's "Hide playlists with no
// local tracks", saved server-side, and the playlist here matches no track.
// Serve this page the real profile with that filter off, whatever another
// spec running alongside has left saved.
async function showZeroMatchPlaylists(page) {
  await page.route("**/api/profile", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const resp = await route.fetch();
    return route.fulfill({ response: resp, json: { ...(await resp.json()), hide_zero_match_playlists: false } });
  });
}

test.describe("Playlists made on a device", () => {
  let device;
  test.beforeEach(async ({ request }) => {
    const created = await request.post("/api/devices", { data: { name: "E2E Pocket DAP", device_type: "dap" } });
    expect(created.ok()).toBeTruthy();
    device = await created.json();
  });
  test.afterEach(async ({ request }) => {
    await request.delete(`/api/devices/${device.id}`);
  });

  test("shows the device it came from, and goes when the device does", async ({ page, request }) => {
    const uploaded = await request.post("/api/device/playlists", {
      headers: { Authorization: `Bearer ${device.token}` },
      data: { playlists: [{ path: "Playlists/E2E Card Mix.m3u", entries: ["Side Loaded/Live/01 - Encore.mp3"] }] },
    });
    expect(uploaded.ok()).toBeTruthy();
    expect((await uploaded.json()).playlists).toBe(1);

    await showZeroMatchPlaylists(page);
    await page.goto("/#/playlists");
    await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
    await page.evaluate(async () => {
      const a = window.Alpine.$data(document.querySelector("[x-data]"));
      a.goToTab("playlists");
      await a.loadPlaylists();
    });
    const row = page.locator("div.flex-1", { hasText: "E2E Card Mix" }).first();
    await expect(row).toBeVisible();
    const source = row.locator('[title="From E2E Pocket DAP"]');
    await expect(source).toBeVisible();
    // The device icon is drawn (shapes, no text: toBeEmpty would not tell).
    expect(await source.locator("svg > *").count()).toBeGreaterThan(0);

    expect((await request.delete(`/api/devices/${device.id}`)).ok()).toBeTruthy();
    const left = await (await request.get("/api/provider/playlists")).json();
    expect(left.filter((p) => p.title === "E2E Card Mix")).toEqual([]);
  });
});
