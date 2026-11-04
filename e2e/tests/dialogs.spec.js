// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");
const AxeBuilder = require("@axe-core/playwright").default;

// The modals share one behaviour, x-dialog in index.html: each is a named
// dialog, takes focus when it opens, keeps Tab inside, closes on Escape and
// gives focus back to what opened it. accessibility.spec.js already scans the
// mirror picker, the quiz and the Add device modal in every theme; the other
// seven are scanned here, once each, opened from state the way the panels
// themselves set it. None of these tests writes server state.

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

async function gotoApp(page) {
  await trackFetches(page);
  await page.goto("/#/home");
  await settled(page);
}

function app(page, fn, arg) {
  return page.evaluate(([body, a]) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    return new Function("d", "a", body)(d, a);
  }, [fn, arg]);
}

const focusInDialog = (page) =>
  page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'));

// [what the test calls it, how its panel opens it, its accessible name]
const MODALS = [
  ["Sync to…", "d.picker = {open: true, type: 'album', target: 'Album', surface: null, deviceIds: []}", "Sync to…"],
  ["device token", "d.tokenModal = {open: true, name: 'Probe device', token: 'token-for-the-dialog-scan'}", "Probe device"],
  ["integration token", "d.integrationTokenModal = {open: true, name: 'Probe tool', token: 'token-for-the-dialog-scan'}", "Probe tool"],
  ["enrolment code", "d.enrollModal = {open: true, code: 'PROBE-CODE', device: null, isNew: false}", /./],
  ["password reset", "d.openResetPasswordModal({id: 999999, username: 'probe-user'})", /probe-user/],
  ["delete user", "d.openDeleteUserModal({id: 999999, username: 'probe-user'})", /probe-user/],
  ["change library source", "d.goToTab('admin'); d.setSubTab('admin', 'config'); d.openSwitchFlow()", "Change library source"],
];

test.describe("Dialogs", () => {
  test.describe.configure({ mode: "parallel" });

  for (const [what, open, name] of MODALS) {
    test(`the ${what} dialog is named and has no WCAG A/AA violations`, async ({ page }) => {
      await gotoApp(page);
      await app(page, open);
      const dialog = page.getByRole("dialog", { name });
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute("aria-modal", "true");
      await expect.poll(() => focusInDialog(page)).toBe(true);
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      expect(results.violations.map((v) => `${v.id} x${v.nodes.length}`)).toEqual([]);
    });
  }

  test("focus goes in, stays in, and comes back to the button that opened it", async ({ page }) => {
    await gotoApp(page);
    await app(page, "d.goToTab('devices')");
    await settled(page);
    const opener = page.getByRole("button", { name: "+ Add device", exact: true });
    await opener.click();
    const dialog = page.getByRole("dialog", { name: "Add device" });
    await expect(dialog).toBeVisible();
    await expect.poll(() => focusInDialog(page)).toBe(true);
    // More presses than the dialog has controls, both ways round, checked
    // after every press: a missing wrap would leave the dialog on one of them.
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press("Tab");
      expect(await focusInDialog(page), `after Tab ${i + 1}`).toBe(true);
    }
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press("Shift+Tab");
      expect(await focusInDialog(page), `after Shift+Tab ${i + 1}`).toBe(true);
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(opener).toBeFocused();
  });

  test("Escape still closes a dialog after a click on its text moved focus out", async ({ page }) => {
    await gotoApp(page);
    await app(page, "d.openResetPasswordModal({id: 999999, username: 'probe-user'})");
    const dialog = page.getByRole("dialog", { name: /probe-user/ });
    await expect(dialog).toBeVisible();
    await page.evaluate(() => document.activeElement?.blur());
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });

  test("a dialog that is mid-step refuses Escape until it is done", async ({ page }) => {
    await gotoApp(page);
    await app(page, "d.goToTab('admin'); d.setSubTab('admin', 'config'); d.openSwitchFlow(); d.switchFlow.busy = true");
    const dialog = page.getByRole("dialog", { name: "Change library source" });
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    await app(page, "d.switchFlow.busy = false");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });
});
