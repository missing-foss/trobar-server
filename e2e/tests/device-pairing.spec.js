// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// Pairing always shows the short code: a new DAP/card/folder device, and the
// one pairing action on every device. The raw device token is only behind
// "Older desktop app?", for desktop builds that predate codes.

const CODE = /^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{8}$/;

async function gotoDevices(page) {
  await page.goto("/#/home");
  await page.waitForFunction(
    () => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const a = window.Alpine.$data(document.querySelector("[x-data]"));
    a.goToTab("devices");
    await a.loadDevices();
  });
}

const codeDialog = (page) => page.locator("div.fixed").filter({ has: page.locator(".tracking-widest") });

test.describe("device pairing", () => {
  test("a new card device shows a code with the server URL, and the token only on request", async ({ page }) => {
    const name = `pairing-card-${Date.now()}`;
    await gotoDevices(page);
    await page.getByRole("button", { name: "Add device" }).click();
    await page.locator("#add-device-type").selectOption("sdcard");
    await page.getByPlaceholder("Name (e.g. Pixel 9)").fill(name);
    await page.getByRole("button", { name: "Create device" }).click();

    const dialog = codeDialog(page);
    await expect(dialog.getByRole("heading")).toHaveText(`Pair "${name}"`);
    await expect(dialog.locator(".tracking-widest")).toHaveText(CODE);
    await expect(dialog.getByText(new URL(page.url()).origin)).toBeVisible();
    // The token isn't on screen until asked for.
    await expect(page.locator('input[readonly][class*="font-mono"]:visible')).toHaveCount(0);

    page.once("dialog", (d) => d.accept());
    await dialog.getByRole("button", { name: "Older desktop app? Get a config file instead" }).click();
    const token = page.locator('input[readonly][class*="font-mono"]:visible');
    await expect(token).toHaveCount(1);
    await expect(token).toHaveValue(/^[A-Za-z0-9_-]{43}$/);
    await expect(page.getByRole("button", { name: "Download config file (desktop / SD card)" })).toBeVisible();

    const devices = await (await page.request.get("/api/devices")).json();
    const created = devices.find((d) => d.name === name);
    expect(created.device_type).toBe("sdcard");
    await page.request.delete(`/api/devices/${created.id}`);
  });

  test("every device has one pairing action; a phone's code has no desktop fallback", async ({ page }) => {
    const name = `pairing-phone-${Date.now()}`;
    const created = await page.request.post("/api/devices", { data: { name, device_type: "phone" } });
    expect(created.ok()).toBeTruthy();
    const device = await created.json();
    await gotoDevices(page);

    await expect(page.getByRole("button", { name: "Regenerate + QR" })).toHaveCount(0);
    // By id: the last div holding the name and a Re-pair button can be another
    // device's card while other specs add devices.
    const row = page.locator(`[data-testid="device-card"][data-device-id="${device.id}"]`);
    page.once("dialog", (d) => d.accept());
    await row.getByRole("button", { name: "Re-pair in app" }).click();

    const dialog = codeDialog(page);
    await expect(dialog.getByRole("heading")).toHaveText(`Re-pair "${name}"`);
    await expect(dialog.locator(".tracking-widest")).toHaveText(CODE);
    await expect(dialog.getByRole("button", { name: "Older desktop app? Get a config file instead" })).toBeHidden();
    await page.request.delete(`/api/devices/${device.id}`);
  });
});
