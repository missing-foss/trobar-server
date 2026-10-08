// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// A device's icon: an optional picture chosen apart from its type. The
// lists draw the chosen one, else the type's own; an id this page doesn't
// know falls back to the type's, and an unknown type to the phone. The
// device editor sets it, and "as its type" clears it.

test.describe("device icons", () => {
  test("a device draws its chosen icon, else its type's, and never a blank", async ({ page }) => {
    await page.goto("/#/home");
    const drawn = await page.evaluate(() => {
      const a = window.Alpine.$data(document.querySelector("[x-data]"));
      const is = (d, key) => a.deviceIconSvg(d) === ICONS["device-" + key];
      return {
        chosen: is({ icon: "cassette", device_type: "phone" }, "cassette"),
        byType: is({ icon: null, device_type: "dap" }, "headphones"),
        tabletLandscape: is({ icon: null, device_type: "tablet" }, "tablet"),
        sdcardAsBefore: is({ icon: null, device_type: "sdcard" }, "usb"),
        unknownIcon: is({ icon: "from-a-newer-server", device_type: "watch" }, "watch"),
        unknownType: is({ icon: null, device_type: "toaster" }, "phone"),
      };
    });
    expect(drawn).toEqual({
      chosen: true, byType: true, tabletLandscape: true, sdcardAsBefore: true, unknownIcon: true, unknownType: true,
    });
  });

  test("the device editor sets an icon, and 'as its type' clears it", async ({ page }) => {
    const created = await page.request.post("/api/devices", { data: { name: "icon-test-device", device_type: "dap" } });
    expect(created.ok()).toBeTruthy();
    const device = await created.json();
    await page.goto("/#/home");
    await page.evaluate(async (id) => {
      const a = window.Alpine.$data(document.querySelector("[x-data]"));
      a.goToTab("profile");
      a.setSubTab("profile", "devices");
      await a.loadDevices();
      a.startEditDevice(a.devices.find((d) => d.id === id));
    }, device.id);
    const picker = page.locator('select[aria-label="Icon"]');
    await expect(picker).toHaveCount(1);
    await picker.selectOption("clickwheel");
    await page.getByRole("button", { name: "Save" }).first().click();
    const stored = async () => (await (await page.request.get("/api/devices")).json()).find((d) => d.id === device.id);
    await expect.poll(async () => (await stored()).icon).toBe("clickwheel");
    expect((await stored()).device_type).toBe("dap"); // the type is untouched

    await page.evaluate((id) => {
      const a = window.Alpine.$data(document.querySelector("[x-data]"));
      a.startEditDevice(a.devices.find((d) => d.id === id));
    }, device.id);
    await page.locator('select[aria-label="Icon"]').selectOption("");
    await page.getByRole("button", { name: "Save" }).first().click();
    await expect.poll(async () => (await stored()).icon).toBe(null);

    await page.request.delete(`/api/devices/${device.id}`);
  });
});
