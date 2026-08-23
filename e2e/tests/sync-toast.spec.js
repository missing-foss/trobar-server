// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// #412: the sync-complete toast used to be `fixed top-4 right-4 z-50` —
// rendering ON TOP of the header's provider/scrobbler/profile icons (header
// is `sticky top-0 z-30`) for the toast's whole 6s lifetime. The fix moves
// the toast container to sit just below the header instead. Bounding-box
// comparison, not a screenshot diff — this is a layout/z-order bug, and a
// numeric "toast top >= header bottom" check is the direct claim being
// made, not an approximation of one.

test("a sync-complete toast never overlaps the header's icon row (#412)", async ({ page }) => {
  await page.goto("/#/home");
  await page.evaluate(() =>
    window.Alpine.$data(document.querySelector("[x-data]")).goToTab("home"));
  await page.waitForTimeout(400);

  await page.evaluate(() => {
    const app = window.Alpine.$data(document.querySelector("[x-data]"));
    app.pushSyncToast("E2E Toast Device");
  });

  const toast = page.getByText("E2E Toast Device finished syncing");
  await expect(toast).toBeVisible();

  const header = page.locator("header");
  const headerBox = await header.boundingBox();
  const toastBox = await toast.boundingBox();
  expect(headerBox).toBeTruthy();
  expect(toastBox).toBeTruthy();
  // The old bug: toastBox.y (~16px) sat well inside headerBox's vertical
  // span (0 to ~48-56px), directly under the profile/provider icons which
  // live in the header's own right-hand corner.
  expect(toastBox.y).toBeGreaterThanOrEqual(headerBox.y + headerBox.height);
});
