// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// #303/#501: the cross-surface staging basket, now per-device. Drives the
// real API (server-persisted, so this also proves the backend round-trip,
// not just optimistic UI state) and asserts on rendered output — the
// indicator count, the per-device panel sections, and the resulting
// `selections` after a section is sent.
//
// global-setup seeds an empty library (see e2e/README.md), so there's no real
// album/artist to click through Suggestions/Recently Added the way a human
// would. Staging is driven directly via POST /api/basket (with device_ids —
// #501 made staging choose its destination(s) up front) — legitimate here
// because what's under test is the basket's own plumbing (indicator, panel,
// per-section send, last-destination memory), not the surfaces that feed
// it, which #304's own suite already covers for rendering.

function appData(page, fn, arg) {
  return page.evaluate(
    ([body, a]) => {
      // eslint-disable-next-line no-new-func
      const f = new Function("app", "arg", body);
      return f(window.Alpine.$data(document.querySelector("[x-data]")), a);
    },
    [fn, arg ?? null],
  );
}

async function gotoHome(page) {
  await page.goto("/#/home");
  await page.evaluate(() =>
    window.Alpine.$data(document.querySelector("[x-data]")).goToTab("home"));
  await page.waitForTimeout(400);
}

async function stage(page, type, target, deviceId) {
  const resp = await page.request.post("/api/basket", {
    data: { type, target, device_ids: [deviceId] },
  });
  expect(resp.ok()).toBeTruthy();
}

test.describe("cross-surface staging basket (#303/#501)", () => {
  // The basket is server-side and per-user, not isolated between Playwright
  // browser contexts the way cookies/localStorage are. A leftover item from
  // an earlier test (in this file or another) throws off every count
  // assertion below (PR #421 review: cover-grid.spec.js's #415 test leaked
  // one in). This doesn't replace cleanup in the test that adds an item —
  // it's a backstop so a leak anywhere doesn't cascade into failures here.
  test.beforeEach(async ({ page }) => {
    await page.request.delete("/api/basket");
  });

  test("items staged for one device accumulate in its section, and sending it creates one selection per item", async ({ page }) => {
    const created = await page.request.post("/api/devices", {
      data: { name: "basket-test-device" },
    });
    expect(created.ok()).toBeTruthy();
    const device = await created.json();

    // Two different types, standing in for two different surfaces (an album
    // pick from a cover grid, an artist pick from the Library artist page) —
    // the point is that both land in the SAME device's section, unlike the
    // four separate selection buckets #304 dealt with.
    await stage(page, "album", "E2E Artist||E2E Album", device.id);
    await stage(page, "artist", "E2E Other Artist", device.id);

    await gotoHome(page);
    const basketButton = page.locator('[title="Basket"]');
    await expect(basketButton).toHaveText("2");

    await basketButton.click();
    const panelItems = page.locator("li").filter({ hasText: /E2E Album|E2E Other Artist/ });
    await expect(panelItems).toHaveCount(2);
    // The device name also appears in the device picker modal's own
    // (hidden) checkbox list AND in the Home dashboard's own "Devices"
    // widget, which is genuinely visible on this same page — :visible
    // alone doesn't disambiguate those from the basket panel's own
    // section header, so scope to the basket panel itself (the div whose
    // first child is the "Basket" heading).
    const basketPanel = page.locator('xpath=//h3[normalize-space(text())="Basket"]/..');
    await expect(basketPanel.getByText(device.name)).toBeVisible();

    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/basket/fan-out") && r.request().method() === "POST"),
      page.getByRole("button", { name: "Send" }).click(),
    ]);

    // The basket clears — the indicator (x-show="count > 0") disappears.
    await expect(basketButton).toBeHidden();

    // The real assertion: the server actually created a selection per item,
    // targeting the device just sent to — not just that the UI looks empty.
    const selectionsResp = await page.request.get("/api/selections");
    const selections = await selectionsResp.json();
    const albumSel = selections.find((s) => s.type === "album" && s.target === "E2E Artist||E2E Album");
    const artistSel = selections.find((s) => s.type === "artist" && s.target === "E2E Other Artist");
    expect(albumSel).toBeTruthy();
    expect(artistSel).toBeTruthy();
    expect(albumSel.device_ids.split(",").map(Number)).toContain(device.id);
    expect(artistSel.device_ids.split(",").map(Number)).toContain(device.id);
  });

  test("sending a device's own section leaves another device's section untouched (#501)", async ({ page }) => {
    const createdA = await page.request.post("/api/devices", { data: { name: "basket-device-a" } });
    const createdB = await page.request.post("/api/devices", { data: { name: "basket-device-b" } });
    const deviceA = await createdA.json();
    const deviceB = await createdB.json();

    await stage(page, "artist", "E2E Only A", deviceA.id);
    await stage(page, "artist", "E2E Both", deviceA.id);
    await page.request.post("/api/basket", {
      data: { type: "artist", target: "E2E Both", device_ids: [deviceB.id] },
    });

    await gotoHome(page);
    await page.locator('[title="Basket"]').click();

    // Each section's header is a <span>device name</span> next to its own
    // <button>Send</button>, siblings in the same row — the precise way to
    // scope to "device A's own Send", not just any Send button on the page.
    const sendForA = page.locator(
      `xpath=//span[normalize-space(text())="${deviceA.name}"]/following-sibling::button[normalize-space(text())="Send"]`);
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/basket/fan-out") && r.request().method() === "POST"),
      sendForA.click(),
    ]);

    // Device A's section is gone (both its items were sent), but device
    // B's section — with the item they shared — is still there. Scoped to
    // the basket panel itself (see above) since both device names are
    // also genuinely visible in the Home dashboard's own "Devices" widget
    // on this same page, and (hidden) in the picker's own checkbox list.
    const basketPanel = page.locator('xpath=//h3[normalize-space(text())="Basket"]/..');
    await expect(basketPanel.getByText(deviceA.name)).not.toBeVisible();
    await expect(basketPanel.getByText(deviceB.name)).toBeVisible();
    // "E2E Both" also now genuinely appears in the Selections tab's own row
    // (sending to A just created that selection) — scope to the basket
    // panel here too, same reason as the device-name assertions above.
    await expect(basketPanel.getByText("E2E Both")).toBeVisible();

    const selectionsResp = await page.request.get("/api/selections");
    const selections = await selectionsResp.json();
    const onlyA = selections.find((s) => s.type === "artist" && s.target === "E2E Only A");
    const both = selections.find((s) => s.type === "artist" && s.target === "E2E Both");
    expect(onlyA.device_ids.split(",").map(Number)).toEqual([deviceA.id]);
    // "E2E Both" was only ever staged for A so far (B's stage is still
    // pending, not yet sent) — the selection reflects exactly that.
    expect(both.device_ids.split(",").map(Number)).toContain(deviceA.id);
    expect(both.device_ids.split(",").map(Number)).not.toContain(deviceB.id);
  });

  test("a successful section send shows a toast naming the count and destination (#416)", async ({ page }) => {
    // Before #416, the fan-out response was discarded entirely — the
    // basket emptying was the only signal anything happened, indistin-
    // guishable from Clear. This is the confirmation itself.
    const created = await page.request.post("/api/devices", {
      data: { name: "basket-toast-device" },
    });
    const device = await created.json();

    await stage(page, "artist", "E2E Toast Artist", device.id);
    await stage(page, "artist", "E2E Toast Artist Two", device.id);

    await gotoHome(page);
    await page.locator('[title="Basket"]').click();
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/basket/fan-out") && r.request().method() === "POST"),
      page.getByRole("button", { name: "Send" }).click(),
    ]);

    await expect(page.getByText(`2 items queued to ${device.name}`)).toBeVisible();
  });

  test("the device picker's Add & send now stages and sends in one step, and remembers the destination (#303)", async ({ page }) => {
    // #501: staging/sending a brand-new item now goes through the device
    // picker (opened here directly via openDevicePicker(), the same call
    // every real "Sync to…" button makes — driven this way for the same
    // reason the rest of this file drives the basket's own plumbing
    // directly: the seeded e2e library has no real album/artist to click
    // through a cover grid to reach it).
    const created = await page.request.post("/api/devices", {
      data: { name: "basket-memory-device" },
    });
    const device = await created.json();

    await gotoHome(page);
    await appData(page, "app.openDevicePicker('artist', 'E2E Memory Artist', 'library');");
    // A fresh device's section is empty, so the button carries no count yet.
    await expect(page.getByRole("button", { name: "Add & send now", exact: true })).toBeVisible();
    await page.getByRole("checkbox", { name: "basket-memory-device" }).check();
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/basket/fan-out") && r.request().method() === "POST"),
      page.getByRole("button", { name: "Add & send now", exact: true }).click(),
    ]);
    // addAndSendNow() keeps going after that response resolves (its own
    // $store.basket.load(), the toast, loadSelections(), THEN
    // _closePickerAfterAction() closes the picker) — waiting only for the
    // fan-out response races reopening the picker against that still-
    // running tail. picker.open flipping back to false is the real
    // completion signal for "safe to reopen it."
    await expect.poll(() => appData(page, "return app.picker.open;")).toBe(false);

    // Open the picker again for a different item, no manual re-selection
    // this time — the cheap smart-default (#303) should have remembered it.
    await appData(page, "app.openDevicePicker('artist', 'E2E Memory Artist Two', 'library');");
    await expect(page.getByRole("checkbox", { name: "basket-memory-device" })).toBeChecked();
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/basket/fan-out") && r.request().method() === "POST"),
      page.getByRole("button", { name: "Add & send now", exact: true }).click(),
    ]);

    const selectionsResp = await page.request.get("/api/selections");
    const selections = await selectionsResp.json();
    const sel = selections.find((s) => s.type === "artist" && s.target === "E2E Memory Artist Two");
    expect(sel).toBeTruthy();
    expect(sel.device_ids.split(",").map(Number)).toContain(device.id);
  });

  test("Add & send now's label names the count once the section already has something staged (#501)", async ({ page }) => {
    // The issue's own edge case: a quick add must not silently ride along
    // on a pile already built for that device without being visible first.
    const created = await page.request.post("/api/devices", {
      data: { name: "basket-count-device" },
    });
    const device = await created.json();
    await stage(page, "artist", "E2E Already Staged One", device.id);
    await stage(page, "artist", "E2E Already Staged Two", device.id);

    await gotoHome(page);
    await appData(page, "app.openDevicePicker('artist', 'E2E New One', 'library');");
    await page.getByRole("checkbox", { name: "basket-count-device" }).check();
    // 2 already staged + the 1 new one this click would add.
    await expect(page.getByRole("button", { name: "Add & send now (3 items)" })).toBeVisible();
  });

  test("a playlist basket item never renders as a bare row id, even when stale (#413)", async ({ page }) => {
    // #413's bug: the panel used to render a basket'd playlist as its raw
    // target string (String(p.id)) — a bare number, indistinguishable from
    // any other. Fixed by resolving a title server-side (list_basket).
    // There's no public API to seed a real playlist in this harness (only
    // provider sync creates one, and the seeded e2e library has no
    // provider connected), so this exercises the "stale/deleted playlist"
    // branch specifically — the same template code path, and the one case
    // this harness *can* reach: a target that was never a real playlist.
    // The happy-path title/source_provider resolution itself is covered at
    // the unit level (test_sync_state.ListBasketTests).
    const created = await page.request.post("/api/devices", { data: { name: "basket-413-device" } });
    const device = await created.json();
    await stage(page, "playlist", "999999", device.id);

    await gotoHome(page);
    await page.locator('[title="Basket"]').click();
    // The fix: always the translated fallback naming it as a playlist...
    await expect(page.getByText("Playlist #999999")).toBeVisible();
    // ...never a bare "999999" with nothing else — the old bug's exact
    // rendering, which this exact-match query would still find as a
    // substring of "Playlist #999999" if it weren't exact.
    await expect(page.getByText("999999", { exact: true })).not.toBeVisible();
  });

  test("clearing the basket closes the panel instead of orphaning it (#414)", async ({ page }) => {
    const created = await page.request.post("/api/devices", { data: { name: "basket-414-clear-device" } });
    const device = await created.json();
    await stage(page, "artist", "E2E Clear Artist", device.id);

    await gotoHome(page);
    await page.locator('[title="Basket"]').click();
    const panelHeading = page.getByRole("heading", { name: "Basket" });
    await expect(panelHeading).toBeVisible();

    // Wait for clear()'s own DELETE to actually land before asserting —
    // .click() resolves once the click is dispatched, not once the async
    // handler completes (#400's exact race, applied here too). Checking
    // "not visible" immediately after click() would trivially pass on
    // BOTH the buggy and fixed code, since the DOM hasn't reacted to
    // either yet at that instant.
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/basket") && r.request().method() === "DELETE"),
      page.getByRole("button", { name: "Clear" }).click(),
    ]);

    // The whole point: not just that the panel's gone, but that the trigger
    // (the only other way to reach it) is gone too — nothing left orphaned.
    await expect(panelHeading).not.toBeVisible();
    await expect(page.locator('[title="Basket"]')).toBeHidden();
  });

  test("unstaging the last item via × closes the panel too, not just Clear (#414/#501)", async ({ page }) => {
    // #414's own report: pruning to zero one item at a time is arguably the
    // more likely route to an empty basket than the explicit Clear button.
    // #501: × now unstages one device's link (DELETE .../devices/<id>), not
    // the whole item — but for an item staged for only one device, the
    // observable effect (basket empties, panel closes) is identical.
    const created = await page.request.post("/api/devices", { data: { name: "basket-414-last-item-device" } });
    const device = await created.json();
    await stage(page, "artist", "E2E Last Item Artist", device.id);

    await gotoHome(page);
    await page.locator('[title="Basket"]').click();
    const panelHeading = page.getByRole("heading", { name: "Basket" });
    await expect(panelHeading).toBeVisible();

    // Same real-completion-signal requirement as the Clear case above —
    // the button's accessible name is its "×" text content, not its
    // title="Remove" attribute (text content wins when both are present).
    await Promise.all([
      page.waitForResponse((r) => /\/api\/basket\/\d+\/devices\/\d+$/.test(r.url()) && r.request().method() === "DELETE"),
      page.getByRole("button", { name: "×" }).click(),
    ]);

    await expect(panelHeading).not.toBeVisible();
    await expect(page.locator('[title="Basket"]')).toBeHidden();
  });
});
