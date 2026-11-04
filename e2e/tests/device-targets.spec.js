// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// Every control in a device card is at least 24px tall (WCAG 2.5.8, target
// size minimum): the row's Edit, Re-pair, Delete and the rest were 20px. A
// checkbox inside its <label> counts the label, which is what takes the click.
// Adds one device, like accessibility.spec.js's devices scan; nothing here
// counts devices.

test("every control in a device card is at least 24px tall", async ({ page }) => {
  await trackFetches(page);
  await page.goto("/#/home");
  await page.request.post("/api/devices", { data: { name: "target-size-device" } });
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.goToTab("devices");
    await d.loadDevices();
  });
  await settled(page);
  // The name also sits in closed menus and selects: wait for a visible copy.
  await expect.poll(() => page.evaluate(() => [...document.querySelectorAll("main *")]
    .some((e) => e.children.length === 0 && e.textContent.trim() === "target-size-device"
      && e.getClientRects().length > 0))).toBe(true);
  const small = await page.evaluate(() => {
    const main = document.querySelector("main");
    return [...main.querySelectorAll("button, input, select, a[href]")]
      .filter((e) => e.getClientRects().length > 0)
      .map((e) => {
        const target = e.matches('input[type="checkbox"], input[type="radio"]') && e.closest("label") || e;
        const r = target.getBoundingClientRect();
        return { what: (e.textContent || e.getAttribute("aria-label") || e.type || "").trim().slice(0, 30), h: r.height };
      })
      .filter((x) => x.h < 24);
  });
  expect(small).toEqual([]);
});
