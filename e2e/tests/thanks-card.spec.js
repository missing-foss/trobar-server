// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");
const AxeBuilder = require("@axe-core/playwright").default;

// The thank-you card on Home: shown when the profile says so (an admin, once
// the server has counted enough device syncs; the route tests cover that
// rule), and gone for good, for this admin, after either of its buttons.
// The profile is answered with thanks_card set, since a fresh e2e server has
// counted no syncs; the dismissal goes to the real server.

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

async function openHomeWithCard(page) {
  const dismissals = [];
  await page.route("**/api/profile", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const profile = await response.json();
    return route.fulfill({ response, json: { ...profile, thanks_card: true } });
  });
  page.on("request", (r) => {
    if (r.url().endsWith("/api/profile/thanks-card") && r.method() === "DELETE") dismissals.push(r);
  });
  await trackFetches(page);
  await page.goto("/#/home");
  await settled(page);
  const card = page.locator("[data-thanks-card]");
  await expect(card).toBeVisible();
  return { card, dismissals };
}

test("the card thanks, says what was counted, and offers Ko-fi", async ({ page }) => {
  const { card } = await openHomeWithCard(page);
  await expect(card.getByRole("heading")).toHaveText("Thank you");
  await expect(card).toContainText("Trobar has synced 50 times on this server.");
  await expect(card).toContainText("If it's useful, a coffee helps.");
  const kofi = card.getByRole("link", { name: "Support on Ko-fi" });
  await expect(kofi).toHaveAttribute("href", "https://ko-fi.com/missingfoss");
  await expect(kofi).toHaveAttribute("target", "_blank");
});

test("the cross dismisses it, here and on the server", async ({ page }) => {
  const { card, dismissals } = await openHomeWithCard(page);
  await card.getByRole("button", { name: "Dismiss" }).click();
  await expect(card).toBeHidden();
  await expect.poll(() => dismissals.length).toBe(1);
});

test("Support on Ko-fi dismisses it too", async ({ page, context }) => {
  await context.route("https://ko-fi.com/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<title>Ko-fi</title>" }));
  const { card, dismissals } = await openHomeWithCard(page);
  const [popup] = await Promise.all([
    page.waitForEvent("popup"),
    card.getByRole("link", { name: "Support on Ko-fi" }).click(),
  ]);
  await popup.close();
  await expect(card).toBeHidden();
  await expect.poll(() => dismissals.length).toBe(1);
});

test("the card has no WCAG A/AA violations", async ({ page }) => {
  await openHomeWithCard(page);
  const results = await new AxeBuilder({ page }).include("[data-thanks-card]").withTags(WCAG).analyze();
  expect(results.violations).toEqual([]);
});
