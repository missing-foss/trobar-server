// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// Administration > Configuration is grouped into collapsible sections. What
// needs a browser: which sections start open, that the open/closed state
// survives a reload, that a collapsed page is still an overview (every header
// carries a summary), that warnings and unsaved edits show on a collapsed
// header, and that a failed save opens the section holding the bad field.
// Each test starts in a fresh browser context, so localStorage starts empty:
// a first visit.

const SECTIONS = ["library", "sources", "playlists", "listening", "devices", "maintenance", "security"];

async function gotoConfig(page) {
  await page.goto("/#/admin");
  await page.waitForFunction(
    () => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.goToTab("admin");
    d.setSubTab("admin", "config");
    await d.loadAdminConfig();
  });
  await expect(header(page, "library")).toBeVisible();
}

const header = (page, id) => page.locator(`[data-config-section="${id}"] h2 button`);
const body = (page, id) => page.locator(`#cfg-section-${id}`);

test.describe("Configuration sections", () => {
  test("a first visit opens Library only", async ({ page }) => {
    await gotoConfig(page);
    await expect(body(page, "library")).toBeVisible();
    await expect(header(page, "library")).toHaveAttribute("aria-expanded", "true");
    for (const id of SECTIONS.slice(1)) {
      await expect(body(page, id)).toBeHidden();
      await expect(header(page, id)).toHaveAttribute("aria-expanded", "false");
    }
  });

  test("everything collapsed is still an overview, and stays collapsed after a reload", async ({ page }) => {
    await gotoConfig(page);
    await page.getByRole("button", { name: "Close all" }).click();
    for (const id of SECTIONS) {
      await expect(body(page, id)).toBeHidden();
      // Never a blank header: each one names its section and summarises it.
      await expect(header(page, id).locator("span.text-xs").first()).not.toHaveText("");
    }
    await gotoConfig(page);
    for (const id of SECTIONS) await expect(body(page, id)).toBeHidden();
    await header(page, "devices").click();
    await gotoConfig(page);
    await expect(body(page, "devices")).toBeVisible();
    await expect(body(page, "library")).toBeHidden();
  });

  test("a warning shows on its collapsed section's header", async ({ page }) => {
    await gotoConfig(page);
    await page.evaluate(() => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      d.adminConfig.music_root_writable = true;
      d.setAllSections('config', false);
    });
    await expect(body(page, "library")).toBeHidden();
    await expect(header(page, "library")).toContainText("Library mounted writable");
  });

  test("an edit made before collapsing shows on the header, and is saved", async ({ page }) => {
    await gotoConfig(page);
    const original = await page.evaluate(() =>
      window.Alpine.$data(document.querySelector("[x-data]")).adminConfig.job_retention_days);
    try {
      await header(page, "maintenance").click();
      await page.locator("#cfg-job-retention").fill(String(original + 2));
      await header(page, "maintenance").click();
      await expect(body(page, "maintenance")).toBeHidden();
      // The chip keeps its text while hidden, so test that it shows, not
      // what the header's text contains.
      const unsaved = header(page, "maintenance").getByText("Unsaved change");
      await expect(unsaved).toBeVisible();
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect(page.locator('[x-show="adminConfigSaved"]')).toBeVisible();
      await expect(unsaved).toBeHidden();
      await gotoConfig(page);
      await expect(header(page, "maintenance")).toContainText(`kept ${original + 2} days`);
    } finally {
      await page.request.put("/api/admin/config", { data: { job_retention_days: original } });
    }
  });

  test("a failed save opens the section holding the bad field and focuses it", async ({ page }) => {
    await gotoConfig(page);
    await page.evaluate(() => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      d.setAllSections('config', false);
      d.adminConfig.lastfm_api_base = "http//not-a-url";
    });
    await expect(body(page, "listening")).toBeHidden();
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(body(page, "listening")).toBeVisible();
    await expect(page.locator('[x-model="adminConfig.lastfm_api_base"]')).toBeFocused();
    await expect(page.getByRole("alert")).toContainText("Last.fm API base URL");
    await expect(header(page, "listening")).toContainText("A field to fix");
  });

  test("a refused mirror location opens Playlists and focuses the field", async ({ page }) => {
    // Refused by its own rule, whatever the stack's mirror folder is.
    await gotoConfig(page);
    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).setAllSections('config', false));
    await header(page, "playlists").click();
    await page.getByLabel("Location inside the music share").fill("../Music");
    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).setAllSections('config', false));
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(body(page, "playlists")).toBeVisible();
    await expect(page.getByLabel("Location inside the music share")).toBeFocused();
    await expect(page.getByRole("alert")).toContainText('no ".." and no backslash');
    const saved = await (await page.request.get("/api/admin/config")).json();
    expect(saved.mirror_share_location).toBe("");
  });
});
