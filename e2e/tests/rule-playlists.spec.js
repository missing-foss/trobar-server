// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// Rule playlists: the dialog, its live count, and saving to the real server.
// The e2e library is empty, so a count with matches is a mocked answer whose
// request is checked; a playlist saved here holds no tracks.

const COUNT = "**/api/provider/playlists/rule/count";

async function openTab(page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Playlists", exact: true }).first().click();
}

async function deleteAll(page) {
  const list = await (await page.request.get("/api/provider/playlists")).json();
  for (const p of list.filter((x) => x.source_provider === "rule")) {
    await page.request.delete(`/api/provider/playlists/${p.id}`);
  }
}

const dialog = (page) => page.locator("[data-rule-dialog]");
const count = (page) => page.locator("[data-rule-count]");

test.afterEach(async ({ page }) => { await deleteAll(page); });

test("the count follows the rule, and what is sent is what the dialog shows", async ({ page }) => {
  const sent = [];
  await page.route(COUNT, (route) => {
    sent.push(route.request().postDataJSON().rule);
    route.fulfill({ json: { matches: 176, tracks: 100 } });
  });
  await openTab(page);
  await page.locator("[data-new-rule-playlist]").click();
  await expect(dialog(page).getByRole("heading")).toHaveText("New rule playlist");
  await expect(page.locator("[data-rule-save]")).toBeDisabled();

  const first = dialog(page).locator("[data-rule-condition]").first();
  const inputs = first.getByRole("spinbutton");
  await inputs.nth(0).fill("2010");
  await inputs.nth(1).fill("2019");
  await dialog(page).getByRole("button", { name: "+ Add a condition" }).last().click();
  await expect(count(page)).toContainText("Finish the conditions");
  const second = dialog(page).locator("[data-rule-condition]").nth(1);
  await second.getByRole("combobox", { name: "Field" }).selectOption("duration");
  await second.getByRole("spinbutton").fill("2.5");
  await page.locator("[data-rule-limit]").fill("100");

  await expect(count(page)).toContainText("Would hold 100 tracks");
  await expect(count(page)).toContainText("176 match; the limit keeps the most recently added.");
  expect(sent.at(-1)).toEqual({
    match: "all", sort: "added", limit: 100,
    conditions: [{ field: "year", op: "between", value: [2010, 2019] },
                 { field: "duration", op: "at_least", value: 150 }],
  });
});

test("the buttons stay put when the count comes in", async ({ page }) => {
  // The count arrives a moment after typing. If its line grew the dialog, which
  // is centred, everything would move under a click already on its way.
  await openTab(page);
  await page.locator("[data-new-rule-playlist]").click();
  const first = dialog(page).locator("[data-rule-condition]").first();
  await first.getByRole("combobox", { name: "Field" }).selectOption("artist");
  await expect(count(page)).toContainText("Finish the conditions");
  const button = dialog(page).getByRole("button", { name: "+ Add a group" });
  const before = await button.boundingBox();
  await first.getByRole("textbox", { name: "Value", exact: true }).fill("Glass");
  await expect(count(page)).toContainText("No tracks match this rule yet.");
  expect((await button.boundingBox()).y).toBe(before.y);
});

test("a rule saves, reopens with what was saved, and deletes", async ({ page }) => {
  await openTab(page);
  await page.locator("[data-new-rule-playlist]").click();
  await page.locator("[data-rule-name]").fill("Odd rule");
  const first = dialog(page).locator("[data-rule-condition]").first();
  await first.getByRole("combobox", { name: "Field" }).selectOption("artist");
  await first.getByRole("textbox", { name: "Value", exact: true }).fill("Glass");
  await dialog(page).getByRole("button", { name: "+ Add a group" }).click();
  const group = dialog(page).locator("[data-rule-group]");
  await group.getByRole("textbox", { name: "Value", exact: true }).nth(0).fill("Emberwake");
  await group.getByRole("textbox", { name: "Value", exact: true }).nth(1).fill("Latitude");
  await expect(count(page)).toContainText("No tracks match this rule yet.");
  await page.locator("[data-rule-save]").click();
  await expect(dialog(page)).toBeHidden();

  const row = page.locator("section").getByText("Odd rule").first();
  await expect(row).toBeVisible();
  const card = page.locator("[data-testid^=playlist], li, div").filter({ has: page.getByRole("button", { name: "Actions" }) })
    .filter({ hasText: "Odd rule" }).last();
  await card.getByRole("button", { name: "Actions" }).click();
  await card.getByRole("menuitem", { name: "Edit rule…" }).click();
  await expect(dialog(page).getByRole("heading")).toHaveText("Edit rule playlist");
  await expect(page.locator("[data-rule-name]")).toHaveValue("Odd rule");
  await expect(dialog(page).locator("[data-rule-condition]").first()
    .getByRole("textbox", { name: "Value", exact: true })).toHaveValue("Glass");
  await expect(dialog(page).locator("[data-rule-group]")
    .getByRole("textbox", { name: "Value", exact: true }).nth(1)).toHaveValue("Latitude");

  await page.locator("[data-rule-name]").fill("Renamed rule");
  await page.locator("[data-rule-save]").click();
  await expect(dialog(page)).toBeHidden();
  await expect(page.locator("section").getByText("Renamed rule").first()).toBeVisible();
});

test("a year range needs both years, and reopens with both", async ({ page }) => {
  await openTab(page);
  await page.locator("[data-new-rule-playlist]").click();
  await page.locator("[data-rule-name]").fill("Range rule");
  const years = dialog(page).locator("[data-rule-condition]").first().getByRole("spinbutton");
  await years.nth(0).fill("1990");
  await years.nth(1).fill("");
  await expect(count(page)).toContainText("Finish the conditions");
  await expect(page.locator("[data-rule-save]")).toBeDisabled();
  await years.nth(1).fill("1999");
  await expect(count(page)).toContainText("No tracks match this rule yet.");
  await page.locator("[data-rule-save]").click();
  await expect(dialog(page)).toBeHidden();

  const row = page.locator("div").filter({ has: page.getByRole("button", { name: "Actions" }) })
    .filter({ hasText: "Range rule" }).last();
  await row.getByRole("button", { name: "Actions" }).click();
  await row.getByRole("menuitem", { name: "Edit rule…" }).click();
  await expect(years.nth(0)).toHaveValue("1990");
  await expect(years.nth(1)).toHaveValue("1999");
});

test("removing a group that is the rule's only item leaves a new condition", async ({ page }) => {
  await openTab(page);
  await page.locator("[data-new-rule-playlist]").click();
  await dialog(page).getByRole("button", { name: "+ Add a group" }).click();
  await dialog(page).locator("[data-rule-condition]").first().getByRole("button", { name: "Remove this condition" }).click();
  await expect(dialog(page).locator("[data-rule-group]")).toHaveCount(1);
  await dialog(page).getByRole("button", { name: "Remove this group" }).click();
  await expect(dialog(page).locator("[data-rule-group]")).toHaveCount(0);
  await expect(dialog(page).locator("[data-rule-condition]")).toHaveCount(1);
});

test("a value the server refuses says so", async ({ page }) => {
  await openTab(page);
  await page.locator("[data-new-rule-playlist]").click();
  await dialog(page).locator("[data-rule-condition]").first().getByRole("spinbutton").first().fill("12");
  await expect(count(page)).toContainText("One of the values in this rule can't be used.");
});

for (const mode of ["light", "dark"]) {
  test(`the dialog passes axe, ${mode}`, async ({ page }) => {
    await page.route(COUNT, (route) => route.fulfill({ json: { matches: 176, tracks: 100 } }));
    await openTab(page);
    await page.evaluate((m) => document.documentElement.setAttribute("data-theme", m), mode);
    await page.locator("[data-new-rule-playlist]").click();
    await dialog(page).getByRole("button", { name: "+ Add a group" }).click();
    await dialog(page).locator("[data-rule-group]").getByRole("textbox").first().fill("x");
    await dialog(page).locator("[data-rule-group]").getByRole("textbox").nth(1).fill("y");
    await expect(count(page)).toContainText("Would hold");
    const results = await new AxeBuilder({ page }).include("[data-rule-dialog]")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations).toEqual([]);
  });
}
