// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// Log out is the real menu item, clicked. /logout is POST-only, and the menu
// used to link to it, which answered 405.
//
// Its own login, not the shared one: logging out ends the session on the
// server, and every other spec runs on the session global-setup saved, so
// logging that one out would sign the whole run out.
test.use({ storageState: { cookies: [], origins: [] } });

const USER = process.env.TROBAR_TEST_USER || "e2e-admin";
const PASS = process.env.TROBAR_TEST_PASS || "e2e-test-password-not-a-secret";

test("Log out in the profile menu signs the user out", async ({ page }) => {
  await page.goto("/login");
  await page.locator('input[name="username"]').fill(USER);
  await page.locator('input[name="password"]').fill(PASS);
  await page.locator('input[name="password"]').press("Enter");
  await expect(page).not.toHaveURL(/\/login(#|$)/);

  await page.goto("/#/home");
  await page.getByTitle("Profile").click();
  // By its text, not its role, so this fails on the old link for the 405 itself.
  await page.getByText("Log out", { exact: true }).click();
  await expect(page).toHaveURL(/\/login(#|$)/);
  await page.goto("/#/home");
  await expect(page).toHaveURL(/\/login(#|$)/);
});
