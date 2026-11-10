// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;
const { trackFetches, settled } = require("../support/settle");

// The Profile's Navidrome username (#225): proved once with the Navidrome
// password, which is sent with Save and never kept. The e2e stack has no
// Navidrome, so the profile and the claim route are mocked answers.

const block = (page) => page.locator("[data-subsonic-username]");

/** The real profile with the Navidrome fields set to `extra`. */
async function withProfile(page, extra) {
  await page.route("**/api/profile", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), subsonic_available: true,
      subsonic_username: null, subsonic_user_state: "none", subsonic_playlists_owned: 0, ...extra } });
  });
}

async function openProfile(page) {
  await trackFetches(page);
  await page.goto("/#/home");
  await settled(page);
  await page.evaluate(() => window.Alpine.$data(document.body).goToTab("profile"));
  await page.getByRole("button", { name: "Accounts", exact: true }).click();
}

test("hidden when no Navidrome server is set up", async ({ page }) => {
  await withProfile(page, { subsonic_available: false });
  await openProfile(page);
  await expect(block(page)).toBeHidden();
});

test("a proved username is sent once with its password, which is not kept", async ({ page }) => {
  await withProfile(page, {});
  const sent = [];
  await page.route("**/api/profile/subsonic-username", async (route) => {
    sent.push(route.request().postDataJSON());
    const profile = await (await page.request.get("/api/profile")).json();
    await route.fulfill({ json: { ...profile, subsonic_available: true, subsonic_username: "bob",
      subsonic_user_state: "known", subsonic_playlists_owned: 3 } });
  });
  await openProfile(page);
  const save = block(page).getByRole("button", { name: "Save" });
  await expect(save).toBeDisabled();
  await block(page).getByLabel("Your Navidrome username").fill("bob");
  await expect(save).toBeDisabled();
  await block(page).getByLabel("Your Navidrome password").fill("letmein");
  await expect(block(page)).toContainText("checked once and never stored");
  await save.click();
  await expect(block(page)).toContainText("Navidrome user bob");
  await expect(block(page)).toContainText("3 of your Navidrome playlists sync as yours.");
  expect(sent).toEqual([{ username: "bob", password: "letmein" }]);
  const held = await page.evaluate(() => window.Alpine.$data(document.body).subsonicClaim.password);
  expect(held).toBe("");
});

test("a refused proof says why, saves nothing and clears the password", async ({ page }) => {
  await withProfile(page, {});
  await page.route("**/api/profile/subsonic-username", (route) => route.fulfill({
    status: 400, json: { error: "Navidrome didn't accept that username and password." } }));
  await openProfile(page);
  await block(page).getByLabel("Your Navidrome username").fill("bob");
  await block(page).getByLabel("Your Navidrome password").fill("wrong");
  await block(page).getByRole("button", { name: "Save" }).click();
  await expect(block(page).getByRole("alert")).toHaveText("Navidrome didn't accept that username and password.");
  await expect(block(page).getByLabel("Your Navidrome password")).toHaveValue("");
  await expect(block(page)).not.toContainText("Navidrome user bob");
});

test("a username the server doesn't know is shown as such", async ({ page }) => {
  await withProfile(page, { subsonic_username: "dave", subsonic_user_state: "unknown" });
  await openProfile(page);
  await expect(block(page)).toContainText("No Navidrome user called dave");
  await expect(block(page)).toContainText("Check the spelling");
  const results = await new AxeBuilder({ page }).include("[data-subsonic-username]").analyze();
  expect(results.violations).toEqual([]);
});

test("Remove sends no password", async ({ page }) => {
  await withProfile(page, { subsonic_username: "bob", subsonic_user_state: "known", subsonic_playlists_owned: 1 });
  const sent = [];
  await page.route("**/api/profile/subsonic-username", async (route) => {
    sent.push(route.request().postDataJSON());
    const profile = await (await page.request.get("/api/profile")).json();
    await route.fulfill({ json: { ...profile, subsonic_available: true, subsonic_username: null,
      subsonic_user_state: "none" } });
  });
  await openProfile(page);
  await expect(block(page)).toContainText("1 of your Navidrome playlists syncs as yours.");
  await block(page).getByRole("button", { name: "Remove" }).click();
  await expect(block(page).getByLabel("Your Navidrome username")).toBeVisible();
  expect(sent).toEqual([{ username: "" }]);
});

test("Administration says whether the Subsonic account is a Navidrome admin", async ({ page }) => {
  await trackFetches(page);
  await page.goto("/#/admin");
  await settled(page);
  const line = page.locator('[data-subsonic-admin="mirror_subsonic"]');
  await page.evaluate(() => {
    const d = window.Alpine.$data(document.body);
    d.liveCheck = { ...d.liveCheck, mirror_subsonic: { status: "ok", isAdmin: true } };
  });
  // Matched on wording only each line has: hasText ignores case, so "A
  // Navidrome admin account" is also inside "Not a Navidrome admin account".
  const yes = line.filter({ hasText: "get their own playlists as theirs" });
  const no = line.filter({ hasText: "only the shared library pass runs" });
  // The section may be collapsed, so each line's own x-show is what's
  // checked: Alpine hides it with an inline display: none.
  await expect(yes).not.toHaveAttribute("style", /display: none/);
  await expect(no).toHaveAttribute("style", /display: none/);
  await page.evaluate(() => {
    const d = window.Alpine.$data(document.body);
    d.liveCheck = { ...d.liveCheck, mirror_subsonic: { status: "ok", isAdmin: false } };
  });
  await expect(no).not.toHaveAttribute("style", /display: none/);
  await expect(yes).toHaveAttribute("style", /display: none/);
  // A server that doesn't say (not Navidrome) gets neither line.
  await page.evaluate(() => {
    const d = window.Alpine.$data(document.body);
    d.liveCheck = { ...d.liveCheck, mirror_subsonic: { status: "ok", isAdmin: null } };
  });
  await expect(yes).toHaveAttribute("style", /display: none/);
  await expect(no).toHaveAttribute("style", /display: none/);
});
