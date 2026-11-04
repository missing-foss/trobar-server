// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// A provider setting the server rejects (400 {error, field}) used to replace
// the wizard's whole config with that error object: the step advanced with no
// message, and the next save PUT the error object back, blanking every other
// setting. The setup status and the music-root save are mocked, so the shared
// e2e instance (already set up) isn't changed by walking the wizard.

test("a rejected provider setting stops the step and keeps the config", async ({ page }) => {
  await page.route("**/api/setup/status", (route) =>
    route.fulfill({ json: { completed: false, music_root: "/music", music_root_writable: false } }));
  await page.route("**/api/setup/music-root", (route) =>
    route.fulfill({ json: { music_root: "/music", music_root_writable: false } }));
  /** @type {any[]} */
  const puts = [];
  await page.route("**/api/admin/config", (route) => {
    if (route.request().method() !== "PUT") return route.fallback();
    puts.push(route.request().postDataJSON());
    return route.fulfill({
      status: 400,
      json: { error: "The Subsonic URL must start with http:// or https://.", field: "subsonic_url" },
    });
  });

  await page.goto("/setup");
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByRole("button", { name: "Subsonic" }).click();
  // By its label: the wizard's labels are tied to their fields.
  await page.getByLabel("Subsonic URL").fill("navidrome.lan");

  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("The Subsonic URL must start with http:// or https://.")).toBeVisible();
  await expect(page.getByLabel("Subsonic URL")).toHaveValue("navidrome.lan");

  // Saving again sends the config, not the error body it was rejected with.
  await page.getByRole("button", { name: "Continue" }).click();
  await expect.poll(() => puts.length).toBe(2);
  expect(puts[1]).toMatchObject({ provider: "subsonic", subsonic_url: "navidrome.lan" });
  expect(puts[1]).not.toHaveProperty("error");
});

test("a config that fails to load says so and isn't saved back", async ({ page }) => {
  await page.route("**/api/setup/status", (route) =>
    route.fulfill({ json: { completed: false, music_root: "/music", music_root_writable: false } }));
  await page.route("**/api/setup/music-root", (route) =>
    route.fulfill({ json: { music_root: "/music", music_root_writable: false } }));
  /** @type {any[]} */
  const puts = [];
  await page.route("**/api/admin/config", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ status: 500, json: { error: "The configuration could not be read." } });
    }
    puts.push(route.request().postDataJSON());
    return route.fulfill({ status: 400, json: { error: "stop here", field: "provider" } });
  });

  await page.goto("/setup");
  await expect(page.getByText("The configuration could not be read.")).toBeVisible();
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect.poll(() => puts.length).toBe(1);
  // What gets saved is the wizard's own config, with no trace of the error body.
  expect(puts[0]).toHaveProperty("provider");
  expect(puts[0]).not.toHaveProperty("error");
});

// Run again from Administration (?again=1): the instance is already set up,
// the wizard opens anyway, and nothing is saved before Finish. Every write
// is recorded and answered here, so the shared instance isn't changed.
test.describe("Run setup again", () => {
  /** @param {import('@playwright/test').Page} page */
  async function recordWrites(page) {
    /** @type {{url: string, method: string, body: any}[]} */
    const writes = [];
    await page.route(/\/api\/(setup\/music-root|setup\/complete|admin\/config|admin\/provider\/switch\/test)$/, (route) => {
      const req = route.request();
      if (req.method() === "GET") return route.fallback();
      const url = new URL(req.url()).pathname;
      writes.push({ url, method: req.method(), body: req.postDataJSON() });
      if (url.endsWith("/music-root")) return route.fulfill({ json: { music_root: "/music", music_root_writable: false } });
      if (url.endsWith("/switch/test")) return route.fulfill({ json: { state: "disconnected", ok: false } });
      if (url.endsWith("/complete")) return route.fulfill({ json: { status: "ok", scan_started: true } });
      return route.fulfill({ json: req.postDataJSON() });
    });
    return writes;
  }

  test("Administration offers it, and the wizard opens on a set-up instance", async ({ page }) => {
    await page.goto("/#/admin");
    await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).setSubTab("admin", "config"));
    const section = page.getByRole("button", { name: /Maintenance/ });
    if ((await section.getAttribute("aria-expanded")) !== "true") await section.click();
    const link = page.getByRole("link", { name: "Run setup again" });
    await expect(link).toHaveAttribute("href", "/setup?again=1");
    await link.click();
    await expect(page).toHaveURL(/\/setup\?again=1$/);
    await expect(page.getByText("Running setup again.")).toBeVisible();
    await expect(page.getByRole("link", { name: "Back to Administration" })).toHaveAttribute("href", "/#/admin");
  });

  test("nothing is saved until Finish, then everything is", async ({ page }) => {
    const writes = await recordWrites(page);
    await page.goto("/setup?again=1");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("heading", { name: /library metadata/ })).toBeVisible();
    // The library source can't be changed here: only the current one is enabled.
    const current = page.locator("button[aria-pressed='true']");
    await expect(current).toHaveCount(1);
    await expect(page.locator("button[aria-pressed='false']").first()).toBeDisabled();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("heading", { name: /Listening history/ })).toBeVisible();
    expect(writes).toEqual([{ url: "/api/setup/music-root", method: "POST", body: { path: expect.any(String), check_only: true } }]);

    await page.getByRole("button", { name: "Finish setup" }).click();
    await expect.poll(() => writes.map((w) => w.url)).toEqual(
      ["/api/setup/music-root", "/api/setup/music-root", "/api/admin/config", "/api/setup/complete"]);
    expect(writes[1].body).not.toHaveProperty("check_only");
  });
});
