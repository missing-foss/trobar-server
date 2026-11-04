// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// What a watch plays: Everything, or one of the playlists selected for it.
// The choice sits in the watch's card and saves as it changes. Only a watch
// shows it (the Garmin app is the only client that reads it), and only when
// at least one playlist is selected for it, since Everything would be the
// only choice otherwise.

test.describe("what a watch plays", () => {
  /** @type {{devices: number[], selections: number[], playlists: number[]}} */
  const made = { devices: [], selections: [], playlists: [] };

  test.afterEach(async ({ request }) => {
    for (const id of made.selections.splice(0)) expect((await request.delete(`/api/selections/${id}`)).status(), `selection ${id}`).toBeLessThan(300);
    for (const id of made.devices.splice(0)) expect((await request.delete(`/api/devices/${id}`)).status(), `device ${id}`).toBeLessThan(300);
    for (const id of made.playlists.splice(0)) expect((await request.delete(`/api/provider/playlists/${id}`)).status(), `playlist ${id}`).toBeLessThan(300);
  });

  async function device(request, name, device_type) {
    const resp = await request.post("/api/devices", { data: { name, device_type } });
    expect(resp.ok()).toBeTruthy();
    const d = await resp.json();
    made.devices.push(d.id);
    return d;
  }

  async function playlistOn(request, title, deviceIds) {
    const created = await request.post("/api/provider/playlists", { data: { title } });
    expect(created.ok()).toBeTruthy();
    const p = await created.json();
    made.playlists.push(p.id);
    const sel = await request.post("/api/selections", {
      data: { type: "playlist", target: String(p.id), device_ids: deviceIds },
    });
    expect(sel.ok()).toBeTruthy();
    made.selections.push((await sel.json()).id);
    return p;
  }

  async function openDevices(page) {
    // From another page, so a second call really loads it: the same URL
    // again is only a hash change, and would keep what the select showed.
    await page.goto("about:blank");
    await page.goto("/#/home");
    await page.evaluate(async () => {
      const a = window.Alpine.$data(document.querySelector("[x-data]"));
      a.goToTab("devices");
      await Promise.all([a.loadDevices(), a.loadSelections(), a.loadPlaylists()]);
    });
  }

  const card = (page, device) => page.locator(`[data-testid="device-card"][data-device-id="${device.id}"]`);
  const stored = async (request, id) =>
    (await (await request.get("/api/devices")).json()).find((d) => d.id === id).play_playlist_id;

  test("a watch offers its playlists, keeps the choice, and Everything clears it", async ({ page, request }) => {
    const watch = await device(request, "play-choice-watch", "watch");
    const phone = await device(request, "play-choice-phone", "phone");
    const running = await playlistOn(request, "Play choice: running", [watch.id, phone.id]);
    await playlistOn(request, "Play choice: long run", [watch.id, phone.id]);
    // Selected for the phone only: the watch must not offer it.
    await playlistOn(request, "Play choice: phone only", [phone.id]);

    await openDevices(page);
    const play = card(page, watch).getByLabel("Play:");
    await expect(play).toHaveCount(1);
    await expect(play.locator("option")).toHaveText(["Everything", "Play choice: long run", "Play choice: running"]);
    await expect(play).toHaveValue("");

    await play.selectOption({ label: "Play choice: running" });
    await expect.poll(() => stored(request, watch.id)).toBe(running.id);

    await openDevices(page); // a fresh page shows the stored choice
    await expect(card(page, watch).getByLabel("Play:")).toHaveValue(String(running.id));

    await card(page, watch).getByLabel("Play:").selectOption("");
    await expect.poll(() => stored(request, watch.id)).toBe(null);

    // The same playlists are on the phone, which has no Play choice.
    await expect(card(page, phone)).toHaveCount(1);
    await expect(card(page, phone).getByLabel("Play:")).toHaveCount(0);
  });

  test("a watch with no playlist selected shows no Play choice", async ({ page, request }) => {
    const watch = await device(request, "play-choice-bare-watch", "watch");
    await openDevices(page);
    await expect(card(page, watch)).toHaveCount(1);
    await expect(card(page, watch).getByLabel("Play:")).toHaveCount(0);
  });

  test("a refused choice says so and the card keeps the stored one", async ({ page, request }) => {
    const watch = await device(request, "play-choice-refused-watch", "watch");
    await playlistOn(request, "Play choice: refused", [watch.id]);
    await openDevices(page);
    await page.route(`**/api/devices/${watch.id}`, (route) => route.request().method() === "PATCH"
      ? route.fulfill({ status: 400, json: { description: "Choose one of the playlists selected for this device." } })
      : route.continue());
    await card(page, watch).getByLabel("Play:").selectOption({ label: "Play choice: refused" });
    await expect(page.getByText("Choose one of the playlists selected for this device.")).toBeVisible();
    await expect.poll(() => stored(request, watch.id)).toBe(null);
    await expect(card(page, watch).getByLabel("Play:")).toHaveValue("");
  });
});
