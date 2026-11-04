// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { trackFetches, settled } = require("../support/settle");

// The Profile's Plays from my devices card: the switch goes to the real
// server. The e2e library has no tracks, so a stored count is a mocked answer.

const card = (page) => page.locator("[data-device-plays]");
const toggle = (page) => page.locator("[data-device-plays-switch]");

async function openProfile(page) {
  await trackFetches(page);
  await page.goto("/#/home");
  await settled(page);
  await page.evaluate(() => window.Alpine.$data(document.body).goToTab("profile"));
  await page.getByRole("button", { name: "Accounts", exact: true }).click();
  await expect(card(page)).toBeVisible();
}

/** The real profile, claiming `count` stored plays. */
async function withPlays(page, count) {
  await page.route("**/api/profile", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), device_plays_count: count } });
  });
}

test.afterEach(async ({ request }) => {
  await request.put("/api/profile/device-plays", { data: { enabled: false } });
});

test("the switch is off by default, says what off means, and persists", async ({ page }) => {
  await openProfile(page);
  await expect(toggle(page)).not.toBeChecked();
  await expect(card(page)).toContainText("stay stored but aren't used, until you delete them");
  await expect(card(page).locator("[data-device-plays-count]")).toBeHidden();
  await toggle(page).check();
  await expect(card(page).locator("[data-device-plays-count]")).toBeVisible();
  await expect(card(page).locator("[data-device-plays-count]")).toHaveText("No plays stored.");
  await expect(card(page).getByRole("button", { name: "Delete my plays…" })).toBeHidden();
  await openProfile(page);
  await expect(toggle(page)).toBeChecked();
});

test("unticking the switch turns keeping off on the server", async ({ page, request }) => {
  await request.put("/api/profile/device-plays", { data: { enabled: true } });
  await openProfile(page);
  await expect(toggle(page)).toBeChecked();
  const saved = page.waitForResponse((r) => r.url().endsWith("/api/profile/device-plays"));
  await toggle(page).uncheck();
  await saved;
  expect((await (await request.get("/api/profile")).json()).device_plays_enabled).toBe(false);
});

test("stored plays show with the switch off, and delete goes through a confirmation", async ({ page }) => {
  await withPlays(page, 1284);
  await openProfile(page);
  await expect(card(page).locator("[data-device-plays-count]")).toHaveText("1284 plays stored.");
  await card(page).getByRole("button", { name: "Delete my plays…" }).click();
  const dialog = page.locator("[data-delete-plays-dialog]");
  await expect(dialog.getByRole("heading")).toHaveText("Delete 1284 plays?");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  await card(page).getByRole("button", { name: "Delete my plays…" }).click();
  const deleted = page.waitForRequest((r) => r.url().endsWith("/api/profile/device-plays") && r.method() === "DELETE");
  await page.locator("[data-delete-plays-confirm]").click();
  await deleted;
  await expect(dialog).toBeHidden();
  await expect(card(page).locator("[data-device-plays-count]")).toBeHidden();
});

test("a switch the server refuses says so", async ({ page }) => {
  await page.route("**/api/profile/device-plays", (route) =>
    route.fulfill({ status: 500, json: { error: "E2E: not saved." } }));
  await openProfile(page);
  // click(), not check(): the refused answer puts the box back, which check()
  // would take for a click that did nothing.
  await toggle(page).click();
  await expect(page.getByText("E2E: not saved.")).toBeVisible();
  await expect(toggle(page)).not.toBeChecked();
});

test("a send switch is greyed out until its credential is there, and turns on", async ({ page }) => {
  await page.route("**/api/profile", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), device_plays_enabled: true, maloja_url: "" } });
  });
  await openProfile(page);
  const maloja = page.locator('[data-play-send="maloja"]');
  await expect(maloja.getByRole("checkbox")).toBeDisabled();
  await expect(maloja).toContainText("Add your Maloja URL in Scrobbler accounts.");
  await page.unroute("**/api/profile");
  await page.route("**/api/profile/play-sends", (route) => route.fulfill({
    json: { play_sends: { maloja: { on: true, ready: true, error: null },
                          listenbrainz: { on: false, ready: false, error: null } } } }));
  await page.evaluate(() => {
    const app = window.Alpine.$data(document.body);
    app.profile.play_sends = { maloja: { on: false, ready: true, error: "refused" },
                               listenbrainz: { on: false, ready: false, error: null } };
  });
  await expect(maloja).toContainText("It refused the credential");
  await maloja.getByRole("checkbox").click();
  await expect(maloja.getByRole("checkbox")).toBeChecked();
  await expect(maloja).not.toContainText("It refused");
});

test("Last.fm: no connect button until an admin has set it up, then it links", async ({ page }) => {
  await page.route("**/api/profile", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), device_plays_enabled: true } });
  });
  await openProfile(page);
  await expect(page.getByRole("link", { name: "Connect Last.fm" })).toBeHidden();
  await expect(page.locator('[data-play-send="lastfm"]')).toContainText("An admin needs to set up Last.fm first.");
  await page.evaluate(() => { window.Alpine.$data(document.body).profile.lastfm_app_configured = true; });
  await expect(page.getByRole("link", { name: "Connect Last.fm" })).toHaveAttribute("href", "/profile/lastfm/connect");
  await expect(page.locator('[data-play-send="lastfm"]')).toContainText("Connect Last.fm in Scrobbler accounts.");
  await page.evaluate(() => {
    const app = window.Alpine.$data(document.body);
    app.profile.lastfm_session_state = "set";
    app.profile.lastfm_session_name = "e2e-listener";
  });
  await expect(page.locator("[data-lastfm-link]")).toContainText("Connected as e2e-listener");
  await expect(page.getByRole("link", { name: "Connect Last.fm" })).toBeHidden();
});

test("a failed Last.fm link says so", async ({ page }) => {
  await page.route("**/api/profile", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), lastfm_app_configured: true } });
  });
  await trackFetches(page);
  await page.goto("/?lastfm_error=1#/home");
  await settled(page);
  await page.evaluate(() => window.Alpine.$data(document.body).goToTab("profile"));
  await page.getByRole("button", { name: "Accounts", exact: true }).click();
  await expect(page.getByText("Last.fm wasn't connected. Try again.")).toBeVisible();
});

test("a send credential saves, is never shown, and can be removed", async ({ page, request }) => {
  await openProfile(page);
  const token = page.locator("#profile-maloja-key");
  await token.fill("e2e-secret");
  await page.getByRole("button", { name: "Save", exact: true }).first().click();
  await expect(token).toHaveValue("");
  await expect(token).toHaveAttribute("placeholder", "Saved, encrypted");
  const profile = await (await request.get("/api/profile")).text();
  expect(profile).not.toContain("e2e-secret");
  expect(JSON.parse(profile).maloja_api_key_state).toBe("set");
  await page.locator("[data-scrobbler-accounts]").getByRole("button", { name: "Remove" }).click();
  await expect(token).toHaveAttribute("placeholder", "");
  expect((await (await request.get("/api/profile")).json()).maloja_api_key_state).toBe("none");
});

for (const mode of ["light", "dark"]) {
  test(`the card and its dialog pass axe, ${mode}`, async ({ page }) => {
    await withPlays(page, 3);
    await openProfile(page);
    await page.evaluate((m) => document.documentElement.setAttribute("data-theme", m), mode);
    await card(page).getByRole("button", { name: "Delete my plays…" }).click();
    const results = await new AxeBuilder({ page }).include("[data-delete-plays-dialog]").include("[data-device-plays]")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations).toEqual([]);
  });
}
