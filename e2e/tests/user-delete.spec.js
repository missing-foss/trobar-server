// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// #332: the delete-user confirmation is an in-app dialog, not confirm().
//
// Two things it can do that a native dialog cannot, and both are the point:
// it states WHICH ownership blocks the deletion (a confirm() string is fixed
// before the server is asked), and it cannot be suppressed by the browser's
// "prevent this page from creating additional dialogs" — which this UI is
// reachable for, since it uses confirm() in several other places.
//
// The counts themselves are covered server-side by test_routes.py's
// AdminUserOwnedCountsTests; what needs a browser is the rendering.

async function gotoUsers(page) {
  await page.goto("/#/admin");
  await page.evaluate(() => {
    const app = window.Alpine.$data(document.querySelector("[x-data]"));
    app.goToTab("admin");
    app.setSubTab("admin", "users");
  });
  await page.waitForTimeout(600);
}

/** Open the dialog for a user with the given owned counts. */
async function openFor(page, counts) {
  await page.evaluate(async (c) => {
    const app = window.Alpine.$data(document.querySelector("[x-data]"));
    await app.loadAdminUsers();
    const u = {...app.adminUsers[0], ...c};
    app.openDeleteUserModal(u);
  }, counts);
  await page.waitForTimeout(300);
}

test.describe("delete-user dialog (#332)", () => {
  test("names each blocker with its count, and offers no delete button",
    async ({ page }) => {
      await gotoUsers(page);
      await openFor(page, {owned_devices: 3, owned_selections: 12, owned_playlists: 2});
      await expect(page.locator("text=/still owns/i")).toBeVisible();
      for (const s of ["3 devices", "12 selections", "2 playlists"]) {
        await expect(page.locator(`text=${s}`).first()).toBeVisible();
      }
      // The old flow let you press delete and then explained the failure.
      await expect(page.getByRole("button", {name: /^Delete account$/})).toBeHidden();
    });

  test("lists only the categories that actually block", async ({ page }) => {
    await gotoUsers(page);
    await openFor(page, {owned_devices: 0, owned_selections: 4, owned_playlists: 0});
    // Read the dialog's OWN list. A page-wide text assertion is useless here:
    // "device" appears in the Devices tab and the delegations copy, so a broad
    // locator fails for reasons that have nothing to do with this dialog.
    const items = await page.evaluate(() =>
      Array.from(document.querySelectorAll("ul.list-disc li"))
        .filter((li) => li.offsetParent !== null)
        .map((li) => li.textContent.trim()));
    expect(items).toEqual(["4 selections"]);
  });

  test("singular counts are not rendered as \"1 devices\"", async ({ page }) => {
    // A pluralisation slip inside a blocking message reads as a bug in itself.
    await gotoUsers(page);
    await openFor(page, {owned_devices: 1, owned_selections: 0, owned_playlists: 0});
    await expect(page.locator("text=1 device").first()).toBeVisible();
    await expect(page.locator("text=1 devices")).toHaveCount(0);
  });

  test("an unencumbered account offers the delete action", async ({ page }) => {
    await gotoUsers(page);
    await openFor(page, {owned_devices: 0, owned_selections: 0, owned_playlists: 0});
    await expect(page.getByRole("button", {name: /^Delete account$/})).toBeVisible();
    await expect(page.locator("text=/still owns/i")).toBeHidden();
  });
});
