// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// #268: the Sync to… picker shows, per device, a bar of what it holds, what
// this send adds and the room it has, up to its limit or its reported free
// space, whichever runs out first. Over the limit warns; over the free space
// stops the send, since the phone then downloads nothing new. The devices and the
// projection are set here, so the numbers are known; one test reads the real
// server's projection.

const GB = 1e9;
const devices = [
  { id: 9101, name: "E2E Pixel", device_type: "phone", icon: null },
  { id: 9102, name: "E2E Car Card", device_type: "phone", icon: null },
  { id: 9103, name: "E2E Watch", device_type: "watch", icon: null },
  { id: 9104, name: "E2E No Limit", device_type: "phone", icon: null },
  { id: 9105, name: "E2E Both", device_type: "phone", icon: null },
  { id: 9106, name: "E2E Reserve", device_type: "phone", icon: null },
  { id: 9107, name: "E2E Full", device_type: "phone", icon: null },
];
const projection = {
  tracks: 6, bytes: 0.4 * GB,
  devices: {
    // A limit, and more free space than that: the limit is the room.
    9101: { new_tracks: 6, new_bytes: 0.4 * GB, used_bytes: 18.4 * GB, on_device_bytes: 18.4 * GB,
      max_size_bytes: 32 * GB, reported_free_bytes: 41 * GB },
    // 0.2 GB short of its limit for this send.
    9102: { new_tracks: 6, new_bytes: 0.4 * GB, used_bytes: 15.8 * GB, on_device_bytes: 15.8 * GB,
      max_size_bytes: 16 * GB, reported_free_bytes: null },
    // No limit; its free space is the room, beside what is on it (2.4 GB),
    // not beside what is also still queued (0.5 GB of the 2.9 GB used).
    9103: { new_tracks: 6, new_bytes: 0.4 * GB, used_bytes: 2.9 * GB, on_device_bytes: 2.4 * GB,
      max_size_bytes: null, reported_free_bytes: 0.8 * GB },
    9104: { new_tracks: 0, new_bytes: 0, used_bytes: 2 * GB, on_device_bytes: 2 * GB,
      max_size_bytes: null, reported_free_bytes: null },
    // The Watch's numbers with a 3.0 GB limit, below its 3.2 GB of free
    // room: over both, and the limit is what ends the bar.
    9105: { new_tracks: 6, new_bytes: 0.4 * GB, used_bytes: 2.9 * GB, on_device_bytes: 2.4 * GB,
      max_size_bytes: 3 * GB, reported_free_bytes: 0.8 * GB },
    // 1.0 GB free, of which it keeps 0.5 GB free: 0.6 GB fits the free
    // space but not what it can take.
    9106: { new_tracks: 6, new_bytes: 0.6 * GB, used_bytes: 2 * GB, on_device_bytes: 2 * GB,
      max_size_bytes: null, reported_free_bytes: 1 * GB, reserve_bytes: 0.5 * GB },
    // Over its free space before this send: 2.4 GB on it + 0.8 GB free is
    // 3.2 GB of room, and 3.5 GB is already used with what is still queued.
    9107: { new_tracks: 6, new_bytes: 0.4 * GB, used_bytes: 3.5 * GB, on_device_bytes: 2.4 * GB,
      max_size_bytes: null, reported_free_bytes: 0.8 * GB },
  },
};

async function openPicker(page, type, target) {
  const asked = [];
  await page.route("**/api/devices/send-projection?*", (route) => {
    asked.push(new URL(route.request().url()).searchParams);
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(projection) });
  });
  await trackFetches(page);
  await page.goto("/#/home");
  await settled(page);
  await page.evaluate(([devs, t, tg]) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.devices = devs;
    d.openDevicePicker(t, tg);
  }, [devices, type, target]);
  await expect(page.locator("[data-picker-what]")).toBeVisible();
  return asked;
}

const row = (page, id) => page.locator(`[data-picker-device="${id}"]`);
const tick = (page, id) => row(page, id).getByRole("checkbox").check();

test("the picker asks for the item and names it with its size", async ({ page }) => {
  const asked = await openPicker(page, "album", ["Muse||Absolution", "Muse||Origin of Symmetry"]);
  expect(asked).toHaveLength(1);
  expect(asked[0].get("type")).toBe("album");
  expect(asked[0].getAll("target")).toEqual(["Muse||Absolution", "Muse||Origin of Symmetry"]);
  await expect(page.locator("[data-picker-what]")).toHaveText("2 albums · 6 tracks · 0.4 GB");
});

test("unticked, each device shows what it holds against its room", async ({ page }) => {
  await openPicker(page, "artist", "Muse");
  await expect(page.locator("[data-picker-what]")).toHaveText("Muse · 6 tracks · 0.4 GB");
  await expect(row(page, 9101).locator("[data-picker-line]")).toHaveText("18.4 GB used of 32.0 GB (its limit)");
  await expect(row(page, 9103).locator("[data-picker-line]"))
    .toHaveText("2.9 GB used of 3.2 GB (the device's free space)");
  await expect(row(page, 9104).locator("[data-picker-line]")).toHaveText("2.0 GB used");
  await expect(page.locator("[data-picker-warning]:visible")).toHaveCount(0);
  // Nothing of this send on an unticked device's bar: used, then all the room.
  expect(await row(page, 9101).locator("canvas").evaluate((c) => window.Chart.getChart(c).data.datasets
    .map((s) => Math.round(s.data[0] / 1e8) / 10))).toEqual([18.4, 0, 13.6, 0]);
});

test("ticked, a device with room says what is left", async ({ page }) => {
  await openPicker(page, "artist", "Muse");
  await tick(page, 9101);
  await expect(row(page, 9101).locator("[data-picker-line]"))
    .toHaveText("18.4 GB used of 32.0 GB (its limit) · this send 0.4 GB · 13.2 GB left");
  await expect(row(page, 9101).locator("[data-picker-warning]")).toBeHidden();
  const canvas = row(page, 9101).locator("canvas");
  await expect(canvas).toHaveAttribute("aria-label",
    "E2E Pixel. 18.4 GB used of 32.0 GB (its limit) · this send 0.4 GB · 13.2 GB left");
  // Used, this send, room left, doesn't fit.
  expect(await canvas.evaluate((c) => window.Chart.getChart(c).data.datasets
    .map((s) => Math.round(s.data[0] / 1e8) / 10))).toEqual([18.4, 0.4, 13.2, 0]);
});

test("over its limit warns, in words that claim no check, and still lets it send", async ({ page }) => {
  await openPicker(page, "artist", "Muse");
  await tick(page, 9102);
  await expect(row(page, 9102).locator("[data-picker-line]"))
    .toHaveText("15.8 GB used of 16.0 GB (its limit) · this send needs 0.4 GB");
  await expect(row(page, 9102).locator("[data-picker-warning]")).toHaveText(
    "Over by 0.2 GB (its limit). You can still send it; the limit is what the Devices tab warns about, "
    + "and nothing refuses it.");
  const parts = await row(page, 9102).locator("canvas")
    .evaluate((c) => window.Chart.getChart(c).data.datasets.map((s) => Math.round(s.data[0] / 1e8) / 10));
  expect(parts).toEqual([15.8, 0.2, 0, 0.2]);
  await expect(page.getByRole("button", { name: "Add & send now" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Add & keep browsing" })).toBeEnabled();
});

test("over its free space is a stop: both actions are disabled until it is unticked", async ({ page }) => {
  await openPicker(page, "artist", "Muse");
  await tick(page, 9101);
  await tick(page, 9103);
  // 2.4 GB on it + 0.8 GB free = 3.2 GB of room; 2.9 GB used + 0.4 GB = 3.3 GB.
  await expect(row(page, 9103).locator("[data-picker-warning]")).toHaveText(
    "Doesn't fit: needs 0.1 GB more than the device's free space. Free some space on the device or send less.");
  await expect(page.getByRole("button", { name: "Add & send now" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Add & keep browsing" })).toBeDisabled();
  // Unticking it takes the stop away; the Pixel alone can be sent to.
  await row(page, 9103).getByRole("checkbox").uncheck();
  await expect(row(page, 9103).locator("[data-picker-warning]")).toBeHidden();
  await expect(page.getByRole("button", { name: "Add & send now" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Add & keep browsing" })).toBeEnabled();
});

test("a device already over its free space stops nothing until it is ticked", async ({ page }) => {
  await openPicker(page, "artist", "Muse");
  await tick(page, 9101);
  await expect(row(page, 9107).locator("[data-picker-line]"))
    .toHaveText("3.5 GB used of 3.2 GB (the device's free space)");
  await expect(row(page, 9107).locator("[data-picker-warning]")).toBeHidden();
  await expect(page.getByRole("button", { name: "Add & send now" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Add & keep browsing" })).toBeEnabled();
  // The control: ticked, it is the stop the other tests show.
  await tick(page, 9107);
  await expect(row(page, 9107).locator("[data-picker-warning]")).toHaveText(
    "Doesn't fit: needs 0.7 GB more than the device's free space. Free some space on the device or send less.");
  await expect(page.getByRole("button", { name: "Add & send now" })).toBeDisabled();
});

test("over both its limit and its free space, the free space decides", async ({ page }) => {
  await openPicker(page, "artist", "Muse");
  await tick(page, 9105);
  await expect(row(page, 9105).locator("[data-picker-line]"))
    .toHaveText("2.9 GB used of 3.0 GB (its limit) · this send needs 0.4 GB");
  await expect(row(page, 9105).locator("[data-picker-warning]")).toHaveText(
    "Doesn't fit: needs 0.1 GB more than the device's free space. Free some space on the device or send less.");
  await expect(page.getByRole("button", { name: "Add & send now" })).toBeDisabled();
});

test("the space a device keeps free is not room: its room and its stop say so", async ({ page }) => {
  await openPicker(page, "artist", "Muse");
  await expect(row(page, 9106).locator("[data-picker-line]"))
    .toHaveText("2.0 GB used of 2.5 GB (the device's free space, keeping 0.5 GB free)");
  await tick(page, 9106);
  await expect(row(page, 9106).locator("[data-picker-warning]")).toHaveText(
    "Doesn't fit: needs 0.1 GB more than the device can take while keeping 0.5 GB free. "
    + "Free some space on the device or send less.");
  await expect(page.getByRole("button", { name: "Add & send now" })).toBeDisabled();
});

test("the real server's projection fills the picker", async ({ page }) => {
  const created = await page.request.post("/api/devices", { data: { name: "E2E Projection Phone", device_type: "phone" } });
  expect(created.ok()).toBeTruthy();
  const { id: deviceId } = await created.json();
  const up = await page.request.post("/api/provider/playlists/upload", {
    multipart: { file: { name: "E2E Projection List.m3u8", mimeType: "audio/x-mpegurl",
      buffer: Buffer.from("#EXTM3U\n/music/Nobody/None/01 Nothing.flac\n") } },
  });
  expect(up.ok()).toBeTruthy();
  const { playlist_id: playlistId } = await up.json();
  try {
    await trackFetches(page);
    await page.goto("/#/home");
    await settled(page);
    await page.evaluate(async (id) => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      await Promise.all([d.loadPlaylists(), d.loadDevices()]);
      d.openDevicePicker("playlist", String(id));
    }, playlistId);
    // Nothing in it matched the (empty) e2e library, so nothing to add.
    await expect(page.locator("[data-picker-what]")).toHaveText("E2E Projection List · 0 tracks · 0.0 GB");
    await expect(row(page, deviceId).locator("[data-picker-line]")).toHaveText("0.0 GB used");
    await expect(row(page, deviceId).locator("canvas")).toHaveAttribute("aria-label", "E2E Projection Phone. 0.0 GB used");
  } finally {
    await page.request.delete(`/api/devices/${deviceId}`);
    await page.request.delete(`/api/provider/playlists/${playlistId}`);
  }
});
