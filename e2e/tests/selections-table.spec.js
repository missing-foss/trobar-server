// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// The Selections table: a box someone else's selection keeps is locked and
// names them, unticking your own asks first (with the server's count), a
// refused change says why, and a row ticked nowhere can be forgotten. The
// selections and their writes are stubbed against the viewer's own username,
// so the shared session's real selections are never touched.

/** @param {import('@playwright/test').Page} page */
async function setup(page, { toggleStatus = 200 } = {}) {
  await page.goto("/#/selections");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  const me = await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).profile.username);
  const state = {
    selections: [
      { id: 1, type: "album", target: "Glass Meridian||Latitude", created_by_username: me, device_ids: "901",
        album_tracks: 1 },
      { id: 2, type: "album", target: "Cinder & Ash||Emberwake", created_by_username: "ana", device_ids: "902" },
      { id: 3, type: "album", target: "Various Artists||Night Drive", created_by_username: me, device_ids: "",
        album_tracks: 14 },
      { id: 4, type: "album", target: "Low Orbit||Night Drive", created_by_username: me, device_ids: "901",
        appears_on: { album_artist: "Various Artists", tracks: 2, of: 14 } },
    ],
    writes: /** @type {any[]} */ ([]),
  };
  await page.route("**/api/selections", (route) => route.request().method() === "GET"
    ? route.fulfill({ json: state.selections }) : route.fallback());
  await page.route("**/api/selections/removal-count", (route) => route.fulfill({ json: { tracks: 12 } }));
  await page.route("**/api/selections/toggle-device", (route) => {
    state.writes.push(route.request().postDataJSON());
    return toggleStatus === 200
      ? route.fulfill({ json: { status: "ok" } })
      : route.fulfill({ status: toggleStatus, json: { error: "the server didn't answer" } });
  });
  await page.route("**/api/selections/*", (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    state.writes.push({ deleted: route.request().url().split("/").pop() });
    state.selections = state.selections.filter((s) => String(s.id) !== route.request().url().split("/").pop());
    return route.fulfill({ json: { status: "ok" } });
  });
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.devices = [{ id: 901, name: "Phone", device_type: "phone" }, { id: 902, name: "SD card", device_type: "sdcard" }];
    d.goToTab("selections");
    d.setSubTab("selections", "albums");
    await d.loadSelections();
  });
  return state;
}

test.describe("The Selections table", () => {
  test("someone else's selection is locked and names them", async ({ page }) => {
    await setup(page);
    const box = page.getByRole("checkbox", { name: "Sync Cinder & Ash - Emberwake to SD card — Selected by ana" });
    await expect(box).toBeChecked();
    await expect(box).toBeDisabled();
    const results = await new AxeBuilder({ page }).include("section[x-show=\"tab==='selections'\"]").analyze();
    expect(results.violations).toEqual([]);
  });

  test("unticking asks first, with the count, and Cancel keeps it", async ({ page }) => {
    const state = await setup(page);
    const box = page.getByRole("checkbox", { name: "Sync Glass Meridian - Latitude to Phone", exact: true });
    /** @type {string[]} */
    const asked = [];
    page.once("dialog", (d) => { asked.push(d.message()); d.dismiss(); });
    await box.click();
    await expect.poll(() => asked.length).toBe(1);
    expect(asked[0]).toBe("Remove Glass Meridian - Latitude from Phone? Its 12 tracks are deleted from Phone at its next sync, unless another selection still needs them.");
    await expect(box).toBeChecked();
    expect(state.writes).toEqual([]);
    page.once("dialog", (d) => d.accept());
    await box.click();
    await expect.poll(() => state.writes.length).toBe(1);
    expect(state.writes[0]).toMatchObject({ target: "Glass Meridian||Latitude", device_id: 901, checked: false });
  });

  test("a refused change says why", async ({ page }) => {
    await setup(page, { toggleStatus: 503 });
    await page.getByRole("checkbox", { name: "Sync Glass Meridian - Latitude to SD card", exact: true }).click();
    await expect(page.getByRole("alert")).toHaveText(
      "Couldn't add Glass Meridian - Latitude to SD card: the server didn't answer. Nothing changed.");
  });

  test("one artist's part of someone else's album says so", async ({ page }) => {
    await setup(page);
    const row = page.getByRole("row", { name: /Low Orbit on Night Drive/ });
    await expect(row).toContainText("Low Orbit on Night Drive");
    await expect(row.getByText("· 2 of 14 tracks")).toBeVisible();
    await expect(row.getByRole("checkbox", { name: "Sync Low Orbit on Night Drive to Phone", exact: true })).toBeChecked();
    // A whole album keeps its plain name.
    await expect(page.getByRole("row", { name: /Various Artists - Night Drive/ }).getByText(/of \d+ tracks/)).toHaveCount(0);
    const results = await new AxeBuilder({ page }).include("section[x-show=\"tab==='selections'\"]").analyze();
    expect(results.violations).toEqual([]);
  });

  test("a whole album says so, with its track count", async ({ page }) => {
    await setup(page);
    await expect(page.getByRole("row", { name: /Various Artists - Night Drive/ }).getByText("· whole album, 14 tracks")).toBeVisible();
    await expect(page.getByRole("row", { name: /Glass Meridian - Latitude/ }).getByText("· whole album, 1 track", { exact: true })).toBeVisible();
    // A per-artist row keeps its own count, and a row without a count shows none.
    const guest = page.getByRole("row", { name: /Low Orbit on Night Drive/ });
    await expect(guest.getByText("· 2 of 14 tracks")).toBeVisible();
    await expect(guest.getByText(/whole album/)).toHaveCount(0);
    await expect(page.getByRole("row", { name: /Cinder & Ash - Emberwake/ }).getByText(/whole album/)).toHaveCount(0);
  });

  test("a row ticked nowhere can be forgotten", async ({ page }) => {
    const state = await setup(page);
    const forget = page.getByRole("row", { name: /Various Artists - Night Drive/ }).getByRole("button", { name: "Forget" });
    await expect(forget).toBeVisible();
    await expect(page.getByRole("row", { name: /Latitude/ }).getByRole("button", { name: "Forget" })).toBeHidden();
    await forget.click();
    await expect.poll(() => state.writes).toEqual([{ deleted: "3" }]);
    await expect(page.getByRole("row", { name: /Various Artists - Night Drive/ })).toHaveCount(0);
  });
});
