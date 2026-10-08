// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// #111: the page runs init() once per load. Alpine 3 calls a data object's
// init() itself, and an x-init="init()" beside it ran it a second time: every
// start-up request went out twice, and init()'s listeners and poll were
// registered twice. The requests are the part a test can count.

test("a page load makes each start-up request once", async ({ page }) => {
  /** @type {Record<string, number>} */
  const counts = {};
  page.on("request", (r) => {
    const path = new URL(r.url()).pathname;
    if (path.startsWith("/api/")) counts[path] = (counts[path] || 0) + 1;
  });
  await page.goto("/#/home");
  await page.waitForLoadState("networkidle");
  expect(counts["/api/profile"]).toBe(1);
  expect(counts["/api/dashboard/catalog"]).toBe(1);
});
