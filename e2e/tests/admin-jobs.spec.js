// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// Administration › Background jobs says so when the jobs API fails, whether
// the first load fails or a later refresh does, and throws nothing doing it.

async function openJobs(page) {
  await page.goto("/#/admin");
  await page.getByRole("button", { name: "Background jobs" }).click();
}

test.describe("Background jobs panel errors", () => {
  let errors;
  test.beforeEach(({ page }) => {
    errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
  });

  test("a first load that fails says so", async ({ page }) => {
    await page.route("**/api/admin/jobs", (route) =>
      route.fulfill({ status: 500, json: { error: "boom" } }));
    await openJobs(page);
    await expect(page.locator("[data-jobs-error]")).toBeVisible();
    await expect(page.locator("[data-jobs-error]")).not.toHaveText("");
    expect(errors).toEqual([]);
  });

  test("a refresh that fails says so, under the jobs it had", async ({ page }) => {
    let fail = false;
    await page.route("**/api/admin/jobs", (route) => fail
      ? route.fulfill({ status: 500, json: { error: "boom" } })
      : route.fulfill({ json: { counts: { running: 0, queued: 0, failed: 0 }, jobs: [] } }));
    await openJobs(page);
    await expect(page.getByText("Running: 0")).toBeVisible();
    fail = true;
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(page.locator("[data-jobs-error]")).toBeVisible();
    expect(errors).toEqual([]);
  });
});
