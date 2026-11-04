// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// The password form on Profile → Account: the current password once the
// account has one, a confirmation checked in the page, and the server's
// "wrong current password" shown under its field. The endpoint is stubbed:
// every spec runs on the shared session, and a real change would end every
// other session of that account, signing out the rest of the run.

async function gotoAccount(page, hasPassword) {
  await page.goto("/#/profile");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate((has) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.goToTab("profile");
    d.setSubTab("profile", "account");
    d.profile.has_local_password = has;
  }, hasPassword);
  await expect(page.locator("#pw-new")).toBeVisible();
}

test.describe("Changing your password", () => {
  test("a first password asks for no current one", async ({ page }) => {
    await gotoAccount(page, false);
    await expect(page.locator("#pw-current")).toBeHidden();
    await expect(page.locator("form").getByRole("button", { name: "Set password" })).toBeVisible();
  });

  test("Save waits for the current password and a matching confirmation", async ({ page }) => {
    await gotoAccount(page, true);
    const save = page.locator("form").getByRole("button", { name: "Change password" });
    await page.locator("#pw-new").fill("a long new one");
    await page.locator("#pw-confirm").fill("a long new one");
    await expect(save).toBeDisabled();
    await page.locator("#pw-current").fill("the old one");
    await expect(save).toBeEnabled();
    await page.locator("#pw-confirm").fill("a long new on");
    await expect(page.getByText("The two new passwords don't match.").first()).toBeVisible();
    await expect(save).toBeDisabled();
    await page.locator("#pw-confirm").fill("a long new one");
    await expect(save).toBeEnabled();
  });

  test("a wrong current password is shown under its field", async ({ page }) => {
    let sent;
    await page.route("**/api/account/password", (route) => {
      sent = route.request().postDataJSON();
      route.fulfill({ status: 400, contentType: "application/json",
        body: JSON.stringify({ error: "That isn't your current password.", field: "current_password" }) });
    });
    await gotoAccount(page, true);
    await page.locator("#pw-current").fill("not it");
    await page.locator("#pw-new").fill("a long new one");
    await page.locator("#pw-confirm").fill("a long new one");
    await page.locator("form").getByRole("button", { name: "Change password" }).click();
    await expect(page.locator("#pw-current-error")).toHaveText("That isn't your current password.");
    await expect(page.locator("#pw-current")).toHaveAttribute("aria-invalid", "true");
    expect(sent).toEqual({ password: "a long new one", current_password: "not it" });
    const results = await new AxeBuilder({ page }).include("#pw-current").analyze();
    expect(results.violations).toEqual([]);
    // Retyping clears it: no copy is left anywhere in the form.
    await page.locator("#pw-current").pressSequentially("x");
    await expect(page.locator("form").getByText("That isn't your current password.")).toHaveCount(0);
    await expect(page.locator("#pw-current")).not.toHaveAttribute("aria-invalid", "true");
  });

  test("a saved change says the other sessions were signed out", async ({ page }) => {
    await page.route("**/api/account/password", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ok" }) }));
    await gotoAccount(page, true);
    await page.locator("#pw-current").fill("the old one");
    await page.locator("#pw-new").fill("a long new one");
    await page.locator("#pw-confirm").fill("a long new one");
    await page.locator("form").getByRole("button", { name: "Change password" }).click();
    await expect(page.getByText("Saved ✓ — your other sessions were signed out.").first()).toBeVisible();
    await expect(page.locator("#pw-current")).toHaveValue("");
  });
});
