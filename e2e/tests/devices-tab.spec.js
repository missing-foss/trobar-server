// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// #266: Devices is a tab of its own, last in the strip on every width, and
// no longer a Profile sub-tab; links to where it used to be land on it. And
// the empty states that replace a bare "No results." or nothing at all.
// The empty states are set from state, the way their data arrives: the
// shared server's library is scanned and other specs add devices, so none of
// these can rely on the server being empty. Nothing here writes server state.

async function gotoApp(page, hash = "/#/home") {
  await trackFetches(page);
  await page.goto(hash);
  await settled(page);
}

function app(page, fn, arg) {
  return page.evaluate(([body, a]) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    return new Function("d", "a", body)(d, a);
  }, [fn, arg]);
}

const tabLabels = (page, nav) =>
  page.locator(nav).locator("button").evaluateAll((bs) => bs.map((b) => b.textContent.trim()));

test("Devices is the last tab on a wide screen", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await gotoApp(page);
  const labels = await tabLabels(page, "header nav.hidden");
  expect(labels[labels.length - 1]).toBe("Devices");
  await page.locator("header nav.hidden").getByRole("button", { name: "Devices" }).click();
  await expect(page).toHaveURL(/#\/devices$/);
});

test("Devices is the last tab on a phone, and Home is not a tab", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await gotoApp(page);
  const labels = await tabLabels(page, "nav.sm\\:hidden");
  expect(labels[labels.length - 1]).toBe("Devices");
  expect(labels).not.toContain("Home");
});

test("Profile no longer has a Devices sub-tab", async ({ page }) => {
  await gotoApp(page, "/#/profile");
  const subTabs = page.locator("section", { has: page.getByRole("button", { name: "Preferences" }) })
    .first().getByRole("button");
  await expect(page.getByRole("button", { name: "Preferences" })).toBeVisible();
  expect(await subTabs.allTextContents()).not.toContain("Devices");
});

test("a #/profile/devices link opens the Devices tab", async ({ page }) => {
  await gotoApp(page, "/#/profile/devices");
  await expect(page).toHaveURL(/#\/devices$/);
  expect(await app(page, "return d.tab")).toBe("devices");
});

test("Profile opened by someone whose last sub-tab was Devices opens Devices, once", async ({ page }) => {
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("seeded")) {
      localStorage.setItem("trobar-subtabs", JSON.stringify({ profile: "devices" }));
      sessionStorage.setItem("seeded", "1");
    }
  });
  await gotoApp(page, "/#/profile");
  await expect(page).toHaveURL(/#\/devices$/);
  // Once: the remembered sub-tab is gone, so Profile is Profile again.
  await gotoApp(page, "/#/profile");
  expect(await app(page, "return d.tab")).toBe("profile");
});

test("no devices yet: one Pair a device button, which opens the pairing dialog", async ({ page }) => {
  await gotoApp(page, "/#/devices");
  await app(page, "d.devices = []");
  const empty = page.locator('[data-empty="devices"]');
  await expect(empty).toBeVisible();
  await expect(empty.getByRole("heading", { name: "No devices yet" })).toBeVisible();
  // The top-of-list Add device button is hidden while the list is empty.
  await expect(page.locator("section").filter({ has: empty }).getByRole("button", { name: "Add device" })).toBeHidden();
  await empty.getByRole("button", { name: "Pair a device" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

/** Each click on a scan button that reaches the server, answered here so
 * the shared e2e server never starts a real scan. */
async function scanRequests(page) {
  const seen = [];
  await page.route("**/api/library/scan", async (route) => {
    seen.push(route.request().method());
    await route.fulfill({ status: 202, contentType: "application/json", body: "{}" });
  });
  // Once a scan has been asked for, the status reports one finished.
  await page.route("**/api/library/scan/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify({
      running: false, last_result: null, last_scan_at: seen.length ? "2026-10-24T18:00:00" : null }) }));
  // After a scan the page reloads the artists; the empty library stays empty.
  await page.route("**/api/library/artists", (route) =>
    route.fulfill({ contentType: "application/json", body: "[]" }));
  return seen;
}

test("an empty library says whether it was never scanned or found nothing", async ({ page }) => {
  const scans = await scanRequests(page);
  await gotoApp(page, "/#/library");
  await app(page, "d.scanProgress = null; d.loadingScan = false; d.artists = []; d.libraryNeverScanned = true");
  const unscanned = page.locator('[data-empty="library-unscanned"]');
  await expect(unscanned).toBeVisible();
  await expect(page.locator('[data-empty="library-nothing-found"]')).toBeHidden();
  // The button starts a scan.
  await unscanned.getByRole("button", { name: "Scan the library now" }).click();
  await expect.poll(() => scans).toEqual(["POST"]);

  await app(page, "d.loadingScan = false; d.scanProgress = null; d.artists = []; d.libraryNeverScanned = false");
  const nothingFound = page.locator('[data-empty="library-nothing-found"]');
  await expect(nothingFound).toBeVisible();
  await expect(unscanned).toBeHidden();
  await nothingFound.getByRole("button", { name: "Scan again" }).click();
  await expect.poll(() => scans).toEqual(["POST", "POST"]);
  await app(page, "d.loadingScan = false; d.scanProgress = null; d.artists = []");

  await app(page, "d.scanProgress = {done: 1912, total: 5030, unit: 'files'}");
  await expect(page.locator('[data-empty="library-scanning"]')).toBeVisible();
  await expect(page.locator('[data-empty="library-nothing-found"]')).toBeHidden();
});

test("a scan someone else started meanwhile is followed, not reported as a failure", async ({ page }) => {
  let elsewhere = false;
  const posts = [];
  await page.route("**/api/library/scan", (route) => {
    posts.push(route.request().method());
    elsewhere = true;
    return route.fulfill({ status: 409, contentType: "application/json",
      body: JSON.stringify({ error: "A library scan is already running." }) });
  });
  // Not running when the page loads; running once the button is pressed.
  await page.route("**/api/library/scan/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(elsewhere
      ? { running: true, progress: { done: 1912, total: 5030, unit: "files" }, last_result: null, last_scan_at: null }
      : { running: false, last_result: null, last_scan_at: null }) }));
  await page.route("**/api/library/artists", (route) =>
    route.fulfill({ contentType: "application/json", body: "[]" }));
  await gotoApp(page, "/#/library");
  const unscanned = page.locator('[data-empty="library-unscanned"]');
  await expect(unscanned).toBeVisible();
  await unscanned.getByRole("button", { name: "Scan the library now" }).click();
  await expect(page.locator('[data-empty="library-scanning"]')).toBeVisible();
  await expect(page.locator('[data-empty="library-scanning"]')).toContainText("1,912 of 5,030 files");
  await expect(page.getByText("Refresh after scan failed", { exact: false })).toHaveCount(0);
  await expect(unscanned).toBeHidden();
  expect(posts).toEqual(["POST"]);
});

test("a scan button can't start a second scan while the first is being asked for", async ({ page }) => {
  const posts = [];
  let answer = () => {};
  await page.route("**/api/library/scan", (route) => {
    posts.push(route.request().method());
    // Held until the test has tried a second press.
    answer = () => route.fulfill({ status: 202, contentType: "application/json", body: "{}" });
  });
  await page.route("**/api/library/scan/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify({
      running: false, last_result: null, last_scan_at: posts.length ? "2026-10-24T18:00:00" : null }) }));
  await page.route("**/api/library/artists", (route) =>
    route.fulfill({ contentType: "application/json", body: "[]" }));
  await gotoApp(page, "/#/library");
  const button = page.locator('[data-empty="library-unscanned"]').getByRole("button", { name: "Scan the library now" });
  await button.click();
  await expect.poll(() => posts).toEqual(["POST"]);
  // The button is gone while the request is out, and a second call does nothing.
  await expect(button).toBeHidden();
  await app(page, "d.scanLibrary()");
  answer();
  await expect(page.locator('[data-empty="library-nothing-found"]')).toBeVisible();
  expect(posts).toEqual(["POST"]);
});

test("a search that matches nothing names the search and clears it", async ({ page }) => {
  await gotoApp(page, "/#/library");
  // A library to search in, set like library-search.spec.js sets its own.
  await app(page, "d.artists = [{artist: 'Muse', track_count: 40, album_count: 4}]");
  await page.getByRole("textbox", { name: "Search for an artist or album…" }).fill("zzqqxx-no-such-artist");
  const noMatch = page.locator('[data-empty="library-no-match"]');
  await expect(noMatch).toContainText("zzqqxx-no-such-artist");
  await noMatch.getByRole("button", { name: "Clear the search" }).click();
  await expect(page.getByRole("textbox", { name: "Search for an artist or album…" })).toHaveValue("");
  await expect(noMatch).toBeHidden();
});

test("an admin alone on the server is told what accounts are for", async ({ page }) => {
  await gotoApp(page, "/#/admin");
  await app(page, "d.setSubTab('admin', 'users'); d.setSections('users', {accounts: true})");
  await app(page, "d.adminUsers = d.adminUsers.slice(0, 1); d.adminConfig = {...d.adminConfig, auth_mode: 'local'}");
  const alone = page.locator('[data-empty="users"]');
  await expect(alone).toBeVisible();
  await alone.getByRole("button", { name: "Add a user" }).click();
  await expect(page.getByRole("textbox", { name: "Username" })).toBeFocused();
  // With a second account it is not alone any more.
  await app(page, "d.adminUsers = [...d.adminUsers, {...d.adminUsers[0], id: -1, username: 'someone-else'}]");
  await expect(alone).toBeHidden();
});
