// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// Administration > Users: accounts and delegations in one tab, as two
// collapsible sections whose open/closed state is kept per device. What needs
// a browser: the single pill, the first-visit state, collapse surviving a
// reload, the remembered sub-tab of a device that last had the old Delegations
// tab open, and a deleted user's delegations leaving the page at once. Each
// test starts in a fresh browser context, so localStorage starts empty unless
// a test seeds it.

async function gotoUsers(page) {
  await page.goto("/#/admin");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.goToTab("admin");
    d.setSubTab("admin", "users");
    await Promise.all([d.loadAdminUsers(), d.loadDelegations()]);
  });
  await expect(header(page, "accounts")).toBeVisible();
}

const header = (page, id) => page.locator(`[data-users-section="${id}"] h2 button`);
const body = (page, id) => page.locator(`#users-section-${id}`);

// Two throwaway accounts and a delegation between them, through the admin API
// (creating an account answers with the whole user list).
async function seedPair(page, tag) {
  const create = async (name) => {
    const resp = await page.request.post("/api/admin/users", {
      data: { username: `${name}-${tag}`, password: "e2e-password-not-a-secret" },
    });
    expect(resp.ok()).toBeTruthy();
    return (await resp.json()).find((u) => u.username === `${name}-${tag}`);
  };
  const parent = await create("parent");
  const child = await create("child");
  const resp = await page.request.post("/api/admin/delegations", {
    data: { grantee_user_id: parent.id, target_user_id: child.id },
  });
  expect(resp.ok()).toBeTruthy();
  return { parent, child };
}

async function removeUsers(page, ...users) {
  for (const u of users) await page.request.delete(`/api/admin/users/${u.id}`);
}

test.describe("Users tab", () => {
  test("one Users pill, both sections open on a first visit, each with a summary", async ({ page }) => {
    await gotoUsers(page);
    await expect(page.getByRole("button", { name: "Delegations", exact: true })).toHaveCount(0);
    for (const id of ["accounts", "delegations"]) {
      await expect(body(page, id)).toBeVisible();
      await expect(header(page, id)).toHaveAttribute("aria-expanded", "true");
      await expect(header(page, id).locator("span.text-xs").first()).not.toHaveText("");
    }
    await expect(header(page, "accounts")).toContainText(/\d+ users? · \d+ admins?/);
  });

  test("a collapsed section stays collapsed after a reload", async ({ page }) => {
    await gotoUsers(page);
    await header(page, "accounts").click();
    await expect(body(page, "accounts")).toBeHidden();
    await gotoUsers(page);
    await expect(body(page, "accounts")).toBeHidden();
    await expect(header(page, "accounts")).toHaveAttribute("aria-expanded", "false");
    await expect(body(page, "delegations")).toBeVisible();
  });

  test("a device that last had Delegations open lands on Users with Delegations expanded", async ({ page }) => {
    await page.addInitScript(() => {
      if (sessionStorage.getItem("seeded")) return;
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem("trobar-subtabs", JSON.stringify({ admin: "delegations" }));
      localStorage.setItem("trobar-users-sections", JSON.stringify({ accounts: true, delegations: false }));
    });
    // Loaded straight onto Administration (a reload, a bookmark): the menu's
    // goToTab always opens Configuration, so only this path reads the saved
    // sub-tab.
    await page.goto("/#/admin");
    await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
    expect(await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).subTab("admin"))).toBe("users");
    await expect(header(page, "accounts")).toBeVisible();
    await expect(body(page, "delegations")).toBeVisible();
    await expect(page.getByRole("button", { name: "+ Delegate" })).toBeVisible();
    // Mapped once and stored: the old id is gone from this device.
    const saved = await page.evaluate(() => JSON.parse(localStorage.getItem("trobar-subtabs")));
    expect(saved.admin).toBe("users");
  });

  test("an unknown saved sub-tab falls back to Configuration", async ({ page }) => {
    await page.addInitScript(() => {
      if (sessionStorage.getItem("seeded")) return;
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem("trobar-subtabs", JSON.stringify({ admin: "no-such-tab" }));
    });
    await page.goto("/#/admin");
    await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
    expect(await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).subTab("admin"))).toBe("config");
    await expect(page.locator('[data-config-section="library"]')).toBeVisible();
  });

  test("delegations show on their users' rows, and leave the page when a user is deleted", async ({ page }) => {
    const tag = String(Date.now()).slice(-6);
    const { parent, child } = await seedPair(page, tag);
    try {
      await gotoUsers(page);
      await expect(body(page, "accounts").getByText(`manages: ${child.username}`)).toBeVisible();
      await expect(body(page, "accounts").getByText(`managed by: ${parent.username}`)).toBeVisible();
      const row = body(page, "delegations").locator("div", { hasText: parent.username }).filter({ hasText: child.username });
      await expect(row.first()).toBeVisible();

      // Delete the child through the dialog: its delegation goes too, no reload.
      await page.evaluate((id) => {
        const d = window.Alpine.$data(document.querySelector("[x-data]"));
        d.openDeleteUserModal(d.adminUsers.find((u) => u.id === id));
      }, child.id);
      await page.getByRole("button", { name: /^Delete account$/ }).click();
      await expect(body(page, "accounts").getByText(child.username, { exact: true })).toHaveCount(0);
      await expect(body(page, "delegations").getByText(child.username)).toHaveCount(0);
      await expect(body(page, "accounts").getByText(`manages: ${child.username}`)).toHaveCount(0);
    } finally {
      await removeUsers(page, parent, child);
    }
  });
});
