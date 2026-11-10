// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// The admins' header line and Administration's Source health list. What the
// server records is tested in test_source_health.py; here the page answers
// /api/admin/source-health with fixtures and what's under test is how it reads.

const SOURCE = (overrides) => ({
  kind: "linked_account", provider: "tidal", account: "alice", runs: 0, failing_since: null,
  reason: null, last_success_at: "2026-11-01 10:00:00", alerting: false, ...overrides,
});

async function gotoWith(page, sources, isAdmin = true) {
  await page.route("**/api/admin/source-health", (route) =>
    route.fulfill({ json: { sources, alert_runs: 3, alert_hours: 12 } }));
  if (!isAdmin) {
    await page.route("**/api/profile", async (route) => {
      const res = await route.fetch();
      route.fulfill({ response: res, json: { ...(await res.json()), is_admin: false } });
    });
  }
  await page.goto("/");
  await page.waitForFunction(
    () => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
}

const line = (page) => page.locator("[data-source-alert]");

test.describe("Source health", () => {
  test("one alerting source: the header line names it and leads to Administration", async ({ page }) => {
    await gotoWith(page, [
      SOURCE({ runs: 4, failing_since: "2026-08-02 05:35:00", reason: "http_404", alerting: true }),
      SOURCE({ kind: "library_source", provider: "jellyfin", account: null }),
    ]);
    await expect(line(page)).toBeVisible();
    await expect(line(page)).toContainText("Tidal for alice has failed since");
    await line(page).getByRole("button", { name: "see Administration" }).click();
    await expect(page.locator("[data-source-health]")).toBeInViewport();
    const rows = page.locator("[data-source-health] [data-source-row]");
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText("Failing");
    await expect(rows.nth(0)).toContainText("alerting");
    await expect(rows.nth(0)).toContainText("4 runs · http_404");
    await expect(rows.nth(1)).toContainText("Library source");
    await expect(rows.nth(1)).toContainText("OK");
  });

  test("two alerting sources share one line", async ({ page }) => {
    await gotoWith(page, [
      SOURCE({ runs: 4, failing_since: "2026-08-02 05:35:00", alerting: true }),
      SOURCE({ kind: "member_listing", provider: "jellyfin", account: "bob", runs: 3,
               failing_since: "2026-11-01 18:00:00", alerting: true }),
    ]);
    await expect(line(page)).toContainText("2 playlist sources are failing: Tidal for alice, Jellyfin for bob");
  });

  test("a source failing but not yet alerting shows no line", async ({ page }) => {
    await gotoWith(page, [SOURCE({ runs: 1, failing_since: "2026-11-05 06:00:00", reason: "timeout" })]);
    await expect(page.locator("[data-source-health]")).toBeAttached();
    await expect(line(page)).toBeHidden();
  });

  test("a member never sees the line", async ({ page }) => {
    await gotoWith(page, [SOURCE({ runs: 4, failing_since: "2026-08-02 05:35:00", alerting: true })], false);
    await page.waitForTimeout(500);
    await expect(line(page)).toBeHidden();
  });
});
