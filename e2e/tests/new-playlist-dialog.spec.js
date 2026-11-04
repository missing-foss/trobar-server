// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// New playlist used to make an empty row at the bottom of a long list and open
// its editor off screen, so it looked as if nothing had happened and each
// click made another. Hence: nothing exists until a name is confirmed, and the
// new row shows on top, whatever the sort or the zero-match filter.

const names = Array.from({ length: 40 }, (_, i) => `E2E filler ${i}`);
const made = [];

function app(page) {
  return page.evaluate(() => {
    const a = window.Alpine.$data(document.querySelector("[x-data]"));
    return { pin: a.newPlaylistPin, count: a.playlists.length };
  });
}

async function open(page, hideZero) {
  await page.route("**/api/profile", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const resp = await route.fetch();
    return route.fulfill({ response: resp, json: { ...(await resp.json()), hide_zero_match_playlists: hideZero } });
  });
  await page.goto("/#/playlists");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const a = window.Alpine.$data(document.querySelector("[x-data]"));
    a.goToTab("playlists");
    await a.loadPlaylists();
  });
}

const newButton = (page) => page.getByRole("button", { name: "New playlist", exact: true });

test.beforeAll(async ({ request }) => {
  for (const title of names) {
    made.push((await (await request.post("/api/provider/playlists", { data: { title } })).json()).id);
  }
});

test.afterAll(async ({ request }) => {
  for (const id of made.splice(0)) await request.delete(`/api/provider/playlists/${id}`);
});

for (const hideZero of [false, true]) {
  test(`a new playlist shows on top in a long list, zero-match hidden: ${hideZero}`, async ({ page }) => {
    await open(page, hideZero);
    await newButton(page).click();
    await expect(page.locator("[data-new-playlist-dialog]")).toBeVisible();
    await page.locator("[data-new-playlist-name]").fill(`E2E new ${hideZero}`);
    await page.keyboard.press("Enter");
    await page.locator("[data-new-playlist-done]").click();
    const row = page.locator("[data-playlist-row][data-new-playlist]");
    await expect(row).toContainText(`E2E new ${hideZero}`);
    await expect(row).toBeInViewport();
    await expect(page.locator("[data-playlist-row]").first()).toContainText(`E2E new ${hideZero}`);
    made.push((await app(page)).pin);
  });
}

test("cancelling creates nothing", async ({ page }) => {
  await open(page, false);
  const before = (await app(page)).count;
  await newButton(page).click();
  await page.locator("[data-new-playlist-name]").fill("E2E never");
  await page.locator("[data-new-playlist-dialog]").getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator("[data-new-playlist-dialog]")).toBeHidden();
  const list = await (await page.request.get("/api/provider/playlists")).json();
  expect(list.filter((p) => p.title === "E2E never")).toHaveLength(0);
  expect(list.length).toBe(before);
});

test("a double-click on Create makes one playlist", async ({ page }) => {
  await open(page, false);
  await newButton(page).click();
  await page.locator("[data-new-playlist-name]").fill("E2E once");
  await page.locator("[data-new-playlist-create]").dblclick();
  await expect(page.locator("[data-new-playlist-done]")).toBeVisible();
  const rows = (await (await page.request.get("/api/provider/playlists")).json()).filter((p) => p.title === "E2E once");
  expect(rows).toHaveLength(1);
  made.push(rows[0].id);
});

test("the mark goes when the tab is left", async ({ page }) => {
  await open(page, false);
  await newButton(page).click();
  await page.locator("[data-new-playlist-name]").fill("E2E leaves");
  await page.locator("[data-new-playlist-create]").click();
  await expect(page.locator("[data-new-playlist-done]")).toBeVisible();
  made.push((await app(page)).pin);
  await page.locator("[data-new-playlist-dialog]").getByRole("button", { name: "Go to the Library" }).click();
  expect((await app(page)).pin).toBeNull();
});
