// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// Maloja as a listening-history source, on the user's Profile: a malformed
// URL and a loopback one are refused with the server's own message, a saved
// one brings up the
// header's Maloja badge and silences the "no listening history" hint on the
// Suggestions tab. The URL saved here doesn't resolve (no Maloja runs in the
// e2e stack), so the badge shows the not-reachable state; the client itself
// is unit-tested against recorded payloads.
//
// It runs as an account of its own: the shared test account has a Last.fm
// username that other specs, running at the same time in other workers,
// rely on, and this test needs no scrobbler at all.

const PASSWORD = "e2e-password-not-a-secret";

async function gotoProfile(page) {
  await page.goto("/#/profile");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(() => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.goToTab("profile");
    d.setSubTab("profile", "account");
  });
  await expect(page.getByLabel("Maloja URL")).toBeVisible();
}

test.describe("Maloja on the Profile", () => {
  let user;
  let context;
  test.beforeEach(async ({ request, browser }) => {
    // Created through the admin API by the shared (admin) account, which
    // answers with the whole user list.
    const username = `maloja-${Date.now()}`;
    const resp = await request.post("/api/admin/users", { data: { username, password: PASSWORD } });
    expect(resp.ok()).toBeTruthy();
    user = (await resp.json()).find((u) => u.username === username);
    context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const login = await context.request.post("/login", { form: { username, password: PASSWORD } });
    expect(new URL(login.url()).pathname).not.toBe("/login");
  });
  test.afterEach(async ({ request }) => {
    await context.close();
    await request.delete(`/api/admin/users/${user.id}`);
  });

  test("malformed and loopback URLs are refused; a saved one shows the badge and silences the hint", async () => {
    const page = await context.newPage();
    // The hint appears in several places; count the visible ones.
    const hints = page.locator("p:visible", { hasText: "to also see your most-played tracks and albums" });
    const suggestionsTab = page.locator("section[x-show=\"tab==='lastfm'\"]");
    await page.goto("/#/lastfm");
    await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).goToTab("lastfm"));
    await expect(suggestionsTab).toBeVisible();
    await expect(hints).toHaveCount(1);

    await gotoProfile(page);
    const form = page.locator("form").filter({ has: page.getByLabel("Maloja URL") });
    const save = form.getByRole("button", { name: "Save" });
    const saved = async () => (await (await page.request.get("/api/profile")).json()).maloja_url;

    await page.getByLabel("Maloja URL").fill("maloja.local");
    await save.click();
    await expect(form.getByText(/Enter a valid http:\/\/ or https:\/\/ URL\. \(Maloja URL\)/)).toBeVisible();
    expect(await saved()).toBeNull();

    // The server itself: refused, whatever listens there.
    await page.getByLabel("Maloja URL").fill("http://127.0.0.1:42010");
    await save.click();
    await expect(form.getByText(/Loopback addresses, .* aren't allowed\. \(Maloja URL\)/)).toBeVisible();
    expect(await saved()).toBeNull();

    // A name that never resolves (.invalid is reserved for that).
    await page.getByLabel("Maloja URL").fill("http://maloja.invalid:42010");
    await save.click();
    await expect(form.getByText("Saved ✓")).toBeVisible();
    expect(await saved()).toBe("http://maloja.invalid:42010");
    const badge = page.locator('[title^="Maloja: "]');
    await expect(badge).toBeVisible();
    await expect(badge.locator("span.bg-red-500")).toBeVisible();

    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).goToTab("lastfm"));
    // Counting before the tab has switched would find none on the Profile.
    await expect(suggestionsTab).toBeVisible();
    await expect(hints).toHaveCount(0);
  });
});
