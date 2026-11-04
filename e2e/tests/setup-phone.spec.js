// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// The setup page on a phone, where someone often is when they set Trobar up.
// Below Tailwind's sm breakpoint every field is 16px, so iOS Safari doesn't
// zoom the page when one takes focus, and the fixed-width fields fill the
// card; from sm up they are as before. The setup status is mocked, as in
// setup-wizard.spec.js, so the shared (already set up) instance isn't changed.

// [its button, a field its form shows]
const PROVIDERS = [["Roon", "setup-roon-host"], ["Subsonic", "setup-subsonic-url"], ["Jellyfin", "setup-jellyfin-url"],
  ["Emby", "setup-emby-url"], ["Plex", "setup-plex-url"], ["Music Assistant", "setup-music-assistant-url"],
  ["LMS", "setup-lms-url"]];

async function openSetup(page) {
  await page.route("**/api/setup/status", (route) =>
    route.fulfill({ json: { completed: false, music_root: "/music", music_root_writable: false } }));
  await page.route("**/api/setup/music-root", (route) =>
    route.fulfill({ json: { music_root: "/music", music_root_writable: false } }));
  await page.goto("/setup");
  await expect(page.locator("#setup-musicRoot")).toBeVisible();
}

/** Every visible field: its font size, and whether it fits in the viewport. */
function fields(page) {
  return page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    return {
      pageOverflows: document.documentElement.scrollWidth > vw,
      fields: [...document.querySelectorAll("input, select")]
        .filter((e) => e.getClientRects().length > 0)
        .map((e) => ({
          id: e.id,
          px: parseFloat(getComputedStyle(e).fontSize),
          fits: e.getBoundingClientRect().right <= vw,
          width: Math.round(e.getBoundingClientRect().width),
        })),
    };
  });
}

test.describe("Setup on a phone", () => {
  test.use({ viewport: { width: 375, height: 667 } });

  test("every field is 16px and fits, on every step", async ({ page }) => {
    await openSetup(page);
    const seen = [];
    const check = async (where) => {
      const m = await fields(page);
      expect(m.pageOverflows, `${where}: the page scrolls sideways`).toBe(false);
      for (const f of m.fields) {
        expect(f.px, `${where}: #${f.id} is ${f.px}px`).toBeGreaterThanOrEqual(16);
        expect(f.fits, `${where}: #${f.id} runs off the screen`).toBe(true);
        seen.push(f.id);
      }
    };
    await check("music library step");
    await page.getByRole("button", { name: "Continue" }).click();
    for (const [name, field] of PROVIDERS) {
      await page.getByRole("button", { name, exact: true }).click();
      await expect(page.locator(`#${field}`)).toBeVisible();
      await check(`${name} form`);
    }
    // The check reached the provider forms, not just the first step.
    expect(seen).toContain("setup-subsonic-url");
    expect(seen).toContain("setup-lms-password");
  });
});

test.describe("Setup on a desktop", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("fields keep their desktop size", async ({ page }) => {
    await openSetup(page);
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Subsonic", exact: true }).click();
    await expect(page.locator("#setup-subsonic-url")).toBeVisible();
    const byId = Object.fromEntries((await fields(page)).fields.map((f) => [f.id, f]));
    expect(byId["setup-subsonic-url"].px).toBe(14);
    expect(byId["setup-subsonic-url"].width).toBe(224); // w-56
    expect(byId["setup-subsonic-username"].width).toBe(128); // w-32
  });
});
