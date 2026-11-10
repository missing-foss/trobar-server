// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// Administration's Music Assistant streaming accounts: giving one to a member
// warns first and saves only on Save; back to everyone saves at once. The
// server side is tested in test_routes.py; here its answers are fixtures.

const ACCOUNTS = {
  status: "ok",
  users: [{ id: 2, username: "alice" }, { id: 3, username: "bob" }],
  accounts: [
    { instance_id: "tidal--a1", domain: "tidal", name: "alice-music", owner_user_id: 2, playlists: 38 },
    { instance_id: "tidal--b2", domain: "tidal", name: "bob.listens", owner_user_id: null, playlists: 12 },
  ],
};

async function openAccounts(page, puts) {
  await page.route("**/api/admin/config", async (route) => {
    const res = await route.fetch();
    route.fulfill({ response: res, json: { ...(await res.json()), music_assistant_url: "http://ma.example:8095" } });
  });
  await page.route("**/api/admin/music-assistant/accounts", (route) => route.fulfill({ json: ACCOUNTS }));
  await page.route("**/api/admin/music-assistant/accounts/*/preview", (route) =>
    route.fulfill({ json: { playlists: 12, revoked: [{ username: "carol", count: 2 }, { username: "dave", count: 1 }] } }));
  await page.route(/\/api\/admin\/music-assistant\/accounts\/[^/]+$/, (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    puts.push({ url: route.request().url(), body: route.request().postDataJSON() });
    return route.fulfill({ json: { status: "ok" } });
  });
  await page.goto("/");
  await page.waitForFunction(
    () => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(() => {
    const app = window.Alpine.$data(document.querySelector("[x-data]"));
    app.goToTab("admin");
    app.setAllSections("config", true);
  });
  await expect(page.locator("[data-ma-account]")).toHaveCount(2);
}

test.describe("Music Assistant streaming accounts", () => {
  test("giving an account to a member warns first and saves on Save", async ({ page }) => {
    const puts = [];
    await openAccounts(page, puts);
    const bobRow = page.locator("[data-ma-account]").nth(1);
    await expect(bobRow).toContainText("bob.listens");
    await expect(bobRow).toContainText("12 playlists");
    await bobRow.locator("select").selectOption({ label: "bob" });
    const warning = page.locator("[data-ma-warning]");
    await expect(warning).toBeVisible();
    await expect(warning).toContainText("bob.listens → bob: 12 playlists become private to bob.");
    await expect(warning).toContainText("3 device selections by other members (carol: 2, dave: 1)");
    expect(puts).toEqual([]);
    await warning.getByRole("button", { name: "Save" }).click();
    await expect(warning).toBeHidden();
    expect(puts).toHaveLength(1);
    expect(puts[0].url).toContain("tidal--b2");
    expect(puts[0].body).toEqual({ owner_user_id: 3 });
  });

  test("cancel saves nothing", async ({ page }) => {
    const puts = [];
    await openAccounts(page, puts);
    await page.locator("[data-ma-account]").nth(1).locator("select").selectOption({ label: "bob" });
    await page.locator("[data-ma-warning]").getByRole("button", { name: "Cancel" }).click();
    await expect(page.locator("[data-ma-warning]")).toBeHidden();
    expect(puts).toEqual([]);
  });

  test("back to everyone saves at once, with no warning", async ({ page }) => {
    const puts = [];
    await openAccounts(page, puts);
    await page.locator("[data-ma-account]").nth(0).locator("select").selectOption({ label: "Everyone (as today)" });
    await expect.poll(() => puts.length).toBe(1);
    expect(puts[0].body).toEqual({ owner_user_id: null });
    await expect(page.locator("[data-ma-warning]")).toBeHidden();
  });
});
