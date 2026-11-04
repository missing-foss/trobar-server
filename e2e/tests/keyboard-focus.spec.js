// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// Keyboard focus is visible, and the first Tab offers a way past the header.

async function gotoHome(page) {
  await trackFetches(page);
  await page.goto("/#/home");
  await settled(page);
}

test.describe("Keyboard focus", () => {
  test("the first Tab reaches the skip link, and it moves focus to the page without leaving the tab", async ({ page }) => {
    await gotoHome(page);
    const skip = page.getByRole("link", { name: "Skip to content" });
    // Off screen until focused.
    expect((await skip.boundingBox())?.y ?? 0).toBeLessThan(0);
    await page.keyboard.press("Tab");
    await expect(skip).toBeFocused();
    expect((await skip.boundingBox())?.y ?? -1).toBeGreaterThanOrEqual(0);
    await page.keyboard.press("Enter");
    await expect(page.locator("main#main")).toBeFocused();
    expect(new URL(page.url()).hash).toBe("#/home");
  });

  test("a control reached by keyboard shows the focus ring; one clicked with the mouse doesn't", async ({ page }) => {
    await gotoHome(page);
    const outline = (loc) => loc.evaluate((el) => {
      const s = getComputedStyle(el);
      return `${s.outlineStyle} ${s.outlineWidth}`;
    });
    // Tab past the skip link to the first header control.
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    const focused = page.locator(":focus");
    expect(await outline(focused)).toBe("solid 2px");
    const accent = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--c-accent").trim());
    expect(accent).not.toBe("");
    // A mouse click on a button: no ring.
    const tab = page.getByRole("button", { name: "Library", exact: true }).first();
    await tab.click();
    await expect(tab).toBeFocused();
    expect(await outline(tab)).not.toBe("solid 2px");
  });
});
