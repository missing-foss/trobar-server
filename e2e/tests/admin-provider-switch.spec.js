// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// Administration > Configuration > Library source: the card shows the active
// provider only, and switching goes through the Change library source dialog.
// The e2e instance has no provider configured, so the active one is the
// default, Roon. The tests run in order, and the last one really switches to
// Filesystem: nothing earlier depends on Roon staying active, and nothing
// later depends on it either (the accessibility spec sets the provider
// client-side to show each form).

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const VARIANTS = [
  { brand: "troubadour", mode: "dark" }, { brand: "troubadour", mode: "light" },
  { brand: "garriga", mode: "dark" }, { brand: "garriga", mode: "light" },
  { brand: "peira", mode: "dark" }, { brand: "peira", mode: "light" },
  { brand: "nuech", mode: "dark" }, { brand: "contraste", mode: "dark" },
];

async function gotoConfig(page) {
  await page.goto("/#/admin");
  await page.waitForFunction(
    () => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.goToTab("admin");
    d.setSubTab("admin", "config");
    await Promise.all([d.loadAdminConfig(), d.loadProviderStatus()]);
    d.setAllSections('config', false);
    d.toggleSection('config', "library");
  });
  await expect(page.locator("#cfg-section-library")).toBeVisible();
}

const dialog = (page) => page.getByTestId("switch-flow");

async function storedConfig(page) {
  return (await page.request.get("/api/admin/config")).json();
}

async function scanDialog(page) {
  for (const v of VARIANTS) {
    await page.evaluate((x) => {
      document.documentElement.setAttribute("data-brand", x.brand);
      document.documentElement.setAttribute("data-theme", x.mode);
    }, v);
    const results = await new AxeBuilder({ page }).include("[data-testid=switch-flow]").withTags(WCAG).analyze();
    expect(results.violations.map((r) => `${r.id} (${v.brand} ${v.mode}) x${r.nodes.length}: ${r.nodes.map((n) => n.target.join(" ") + " " + n.any.map((a) => a.message).join(" ")).join(" | ")}`)).toEqual([]);
  }
}

test.describe.serial("Change library source", () => {
  test("the card shows the active provider only", async ({ page }) => {
    await gotoConfig(page);
    await expect(page.getByTestId("active-provider")).toHaveText("Roon");
    // The old row of seven provider glyph buttons is gone.
    await expect(page.locator("#cfg-section-library button[aria-label='Jellyfin']")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Change library source…" })).toBeVisible();
  });

  test("cancel at any step changes nothing", async ({ page }) => {
    await gotoConfig(page);
    const before = await storedConfig(page);
    await page.getByRole("button", { name: "Change library source…" }).click();
    await expect(dialog(page)).toBeVisible();
    // The active provider isn't offered.
    await expect(dialog(page).getByRole("radio")).toHaveCount(7);
    await expect(dialog(page).getByLabel("Roon")).toHaveCount(0);
    await scanDialog(page);

    await dialog(page).getByLabel("Subsonic").check();
    await dialog(page).getByRole("button", { name: "Next" }).click();
    await dialog(page).getByLabel("Server URL").fill("http://127.0.0.1:9");
    await dialog(page).getByLabel("Username").fill("someone");
    await dialog(page).getByLabel("Password").fill("not-a-password");
    await scanDialog(page);
    await dialog(page).getByRole("button", { name: "Cancel" }).click();
    await expect(dialog(page)).toBeHidden();

    // Nothing reached the page's own config, so Save can't send it either.
    const pageConfig = await page.evaluate(
      () => window.Alpine.$data(document.querySelector("[x-data]")).adminConfig.subsonic_url);
    expect(pageConfig || "").toBe(before.subsonic_url || "");
    const after = await storedConfig(page);
    expect(after.provider).toBe(before.provider);
    expect(after.subsonic_url || "").toBe(before.subsonic_url || "");
  });

  test("Next stays off until the connection test passes", async ({ page }) => {
    await gotoConfig(page);
    await page.getByRole("button", { name: "Change library source…" }).click();
    await dialog(page).getByLabel("Subsonic").check();
    await dialog(page).getByRole("button", { name: "Next" }).click();
    const next = dialog(page).getByRole("button", { name: "Next" });
    await expect(next).toBeDisabled();
    await expect(dialog(page).getByText("Test the connection to continue.")).toBeVisible();
    // A port nothing listens on, tested for real by the server.
    await dialog(page).getByLabel("Server URL").fill("http://127.0.0.1:9");
    await dialog(page).getByLabel("Username").fill("someone");
    await dialog(page).getByLabel("Password").fill("not-a-password");
    await dialog(page).getByRole("button", { name: "Test connection" }).click();
    await expect(dialog(page).getByText("Couldn't connect with these details.")).toBeVisible();
    await expect(next).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog(page)).toBeHidden();
    expect((await storedConfig(page)).provider).toBe("roon");
  });

  // The e2e instance has no second provider to carry playlists over to, so
  // this one injects a review-step fixture: what's under test is how the
  // step lays out a carry-over (the counts themselves are route-tested).
  test("the review step shows what is carried over and what is removed", async ({ page }) => {
    await gotoConfig(page);
    await page.getByRole("button", { name: "Change library source…" }).click();
    await page.evaluate(() => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      Object.assign(d.switchFlow, {
        target: "subsonic", step: "review", loading: false,
        preview: {
          from: "roon",
          carried: { count: 2, titles: ["Party Mix", "Road Trip"], status: "ok" },
          removed: { count: 1, titles: ["Kids"], mirrored: 1 },
          devices: [{ name: "Kitchen Garmin", playlists: 1 }],
          kept: { tidal: 3, filesystem: 12 },
        },
      });
    });
    await expect(dialog(page).getByText("Carried over: 2 of Roon's 3 playlists")).toBeVisible();
    await expect(dialog(page).getByText("Party Mix, Road Trip")).toBeVisible();
    await expect(dialog(page).getByTestId("switch-removed"))
      .toContainText("Removed: 1 playlist Subsonic doesn't have");
    await expect(dialog(page).getByText("Kitchen Garmin has 1 of them selected.")).toBeVisible();
    await scanDialog(page);
    await dialog(page).getByRole("button", { name: "Cancel" }).click();
    await expect(dialog(page)).toBeHidden();
  });

  test("a switch shows the server's counts, then the first sync's result", async ({ page }) => {
    await gotoConfig(page);
    // An unsaved edit elsewhere on the page must survive the switch, unsaved.
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).adminConfig.lastfm_api_key_default = "e2e-unsaved-edit";
    });
    await page.getByRole("button", { name: "Change library source…" }).click();
    await dialog(page).getByLabel("Filesystem").check();
    await dialog(page).getByRole("button", { name: "Next" }).click();
    // Filesystem needs no connection test.
    await expect(dialog(page).getByText(/Nothing to connect/)).toBeVisible();
    await dialog(page).getByRole("button", { name: "Next" }).click();

    await expect(dialog(page).getByTestId("switch-removed"))
      .toContainText("Removed: no playlists, Roon has none of its own");
    await expect(dialog(page).getByText(/Nothing has changed yet/)).toBeVisible();
    await scanDialog(page);
    expect((await storedConfig(page)).provider).toBe("roon");

    await dialog(page).getByRole("button", { name: "Switch to Filesystem" }).click();
    await expect(dialog(page).getByTestId("switch-done").getByText("✓ Switched to Filesystem")).toBeVisible();
    await expect(dialog(page).getByText(/^First playlist sync: \d+ playlists?\.$/)).toBeVisible({ timeout: 20_000 });
    await scanDialog(page);
    await dialog(page).getByRole("button", { name: "Close" }).click();

    await expect(page.getByTestId("active-provider")).toHaveText("Filesystem");
    const stored = await storedConfig(page);
    expect(stored.provider).toBe("filesystem");
    expect(stored.lastfm_api_key_default || "").not.toBe("e2e-unsaved-edit");
    expect(await page.evaluate(
      () => window.Alpine.$data(document.querySelector("[x-data]")).adminConfig.lastfm_api_key_default))
      .toBe("e2e-unsaved-edit");
    // The general Save can't switch it back.
    const resp = await page.request.put("/api/admin/config", { data: { provider: "roon" } });
    expect(resp.status()).toBe(400);
    expect((await resp.json()).field).toBe("provider");
  });
});
