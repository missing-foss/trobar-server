// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// api() is the page's one way to call the server. A failed call shows
// the server's own message in the shared toast; no reply at all shows a
// "can't reach the server" toast; a 401 (the session is gone) goes to the
// login page. These calls used to check nothing: a failure left the page as
// if it had worked, or stopped it loading.

const app = (page) => page.evaluateHandle(() => window.Alpine.$data(document.body));

async function home(page) {
  await trackFetches(page);
  await page.goto("/#/home");
  await settled(page);
}

test("a loader that fails shows the server's message, and the page still finishes loading", async ({ page }) => {
  await page.route("**/api/selections", (route) =>
    route.fulfill({ status: 500, json: { error: "E2E: the selections could not be read." } }));
  await home(page);
  await expect(page.getByText("E2E: the selections could not be read.")).toBeVisible();
  // init()'s last steps ran: a rejected loader used to stop it before it
  // recorded where the page is.
  expect(await page.evaluate(() => history.state && history.state.tab)).toBe("home");
});

test("no reply at all shows the can't-reach-the-server toast", async ({ page }) => {
  await page.route("**/api/selections", (route) => route.abort("failed"));
  await home(page);
  await expect(page.getByText("Can't reach the server. Check the connection and try again.")).toBeVisible();
});

test("a reply that isn't JSON says so instead of breaking the page", async ({ page }) => {
  await page.route("**/api/selections", (route) =>
    route.fulfill({ status: 200, contentType: "text/html", body: "<html>a proxy's sign-in page</html>" }));
  await home(page);
  await expect(page.getByText("The server sent an unexpected response.")).toBeVisible();
});

test("a 401 sends the page to the login page", async ({ page }) => {
  await home(page);
  // Faked, not a real sign-out: every spec shares one session.
  await page.route("**/api/selections", (route) =>
    route.fulfill({ status: 401, json: { error: "Login required" } }));
  const toLogin = page.waitForRequest((r) => r.isNavigationRequest() && new URL(r.url()).pathname === "/login");
  // The navigation can destroy the page before evaluate() returns.
  await page.evaluate(async () => { await window.Alpine.$data(document.body).loadSelections(); }).catch(() => {});
  await toLogin;
});

test("the same failure twice shows one toast, not a stack of them", async ({ page }) => {
  await home(page);
  await page.route("**/api/devices", (route) =>
    route.fulfill({ status: 503, json: { error: "E2E: the devices could not be read." } }));
  const queued = await (await app(page)).evaluate(async (a) => {
    await a.loadDevices();
    await a.loadDevices();
    return a.syncToasts.filter((x) => x.text === "E2E: the devices could not be read.").length;
  });
  expect(queued).toBe(1);
  await expect(page.getByText("E2E: the devices could not be read.")).toHaveCount(1);
});

test.describe("a refused device change", () => {
  /** @type {number[]} */
  const made = [];
  test.afterEach(async ({ request }) => {
    for (const id of made.splice(0)) expect((await request.delete(`/api/devices/${id}`)).status(), `device ${id}`).toBeLessThan(300);
  });

  async function device(request, name, extra = {}) {
    const resp = await request.post("/api/devices", { data: { name, device_type: "phone", ...extra } });
    expect(resp.ok()).toBeTruthy();
    const d = await resp.json();
    made.push(d.id);
    return d;
  }

  test("a refused edit says why and keeps the editor open", async ({ page, request }) => {
    const d = await device(request, "api-errors-edit");
    await home(page);
    await page.route(`**/api/devices/${d.id}`, (route) => route.request().method() === "PATCH"
      ? route.fulfill({ status: 400, json: { error: "E2E: that name is not allowed." } })
      : route.continue());
    const editing = await (await app(page)).evaluate(async (a, dev) => {
      await a.loadDevices();
      const row = a.devices.find((x) => x.id === dev.id);
      a.startEditDevice(row);
      a.editDeviceForm.name = "api-errors-renamed";
      await a.saveDeviceEdit(row);
      return a.editingDeviceId;
    }, d);
    await expect(page.getByText("E2E: that name is not allowed.")).toBeVisible();
    expect(editing).toBe(d.id);
  });

  test("a device gone since the list was read raises no toast; any other failure does", async ({ page, request }) => {
    const d = await device(request, "api-errors-usage");
    await home(page);
    const usage = `**/api/devices/${d.id}/usage`;
    await page.route(usage, (route) => route.fulfill({ status: 404, json: { error: "E2E: device gone." } }));
    await (await app(page)).evaluate((a) => a.loadUsage());
    await expect(page.getByText("E2E: device gone.")).toHaveCount(0);
    await page.unroute(usage);
    await page.route(usage, (route) => route.fulfill({ status: 500, json: { error: "E2E: usage unreadable." } }));
    await (await app(page)).evaluate((a) => a.loadUsage());
    await expect(page.getByText("E2E: usage unreadable.")).toBeVisible();
  });

  test("a refused delete says why", async ({ page, request }) => {
    const d = await device(request, "api-errors-delete");
    await home(page);
    await page.route(`**/api/devices/${d.id}`, (route) => route.request().method() === "DELETE"
      ? route.fulfill({ status: 403, json: { error: "E2E: only its owner can delete it." } })
      : route.continue());
    page.on("dialog", (dialog) => dialog.accept());
    await (await app(page)).evaluate(async (a, dev) => {
      await a.loadDevices();
      await a.deleteDevice(a.devices.find((x) => x.id === dev.id));
    }, d);
    await expect(page.getByText("E2E: only its owner can delete it.")).toBeVisible();
  });

  test("a refused transfer says why and keeps the panel open", async ({ page, request }) => {
    const newer = await device(request, "api-errors-transfer-new");
    const older = await device(request, "api-errors-transfer-old");
    await home(page);
    await page.route(`**/api/devices/${newer.id}/transfer-from`, (route) =>
      route.fulfill({ status: 403, json: { error: "E2E: you can't move this one." } }));
    page.on("dialog", (dialog) => dialog.accept());
    const open = await (await app(page)).evaluate(async (a, ids) => {
      await a.loadDevices();
      const row = a.devices.find((x) => x.id === ids.newer);
      a.toggleTransferFrom(row);
      a.transferFromId = ids.older;
      await a.confirmTransferDevice(row);
      return a.transferFor;
    }, { newer: newer.id, older: older.id });
    await expect(page.getByText("E2E: you can't move this one.")).toBeVisible();
    expect(open).toBe(newer.id);
  });

  test("a play choice refused without a reason says so in its own words", async ({ page, request }) => {
    const watch = await device(request, "api-errors-watch", { device_type: "watch" });
    await home(page);
    await page.route(`**/api/devices/${watch.id}`, (route) => route.request().method() === "PATCH"
      ? route.fulfill({ status: 500, body: "" })
      : route.continue());
    await (await app(page)).evaluate(async (a, id) => {
      await a.loadDevices();
      await a.updateDevicePlay(a.devices.find((x) => x.id === id), "");
    }, watch.id);
    await expect(page.getByText("What the watch plays couldn't be saved.")).toBeVisible();
  });

  test("a refused auto-fit refresh says why and leaves the status alone", async ({ page, request }) => {
    const d = await device(request, "api-errors-autofit", { max_size_bytes: 5_000_000_000 });
    await home(page);
    await page.route(`**/api/devices/${d.id}/autofit`, (route) => route.request().method() === "POST"
      ? route.fulfill({ status: 400, json: { error: "E2E: that percentage is out of range." } })
      : route.continue());
    const status = await (await app(page)).evaluate(async (a, id) => {
      await a.loadDevices();
      await a.refreshAutofit(a.devices.find((x) => x.id === id));
      return a.devices.find((x) => x.id === id).autofit || {};
    }, d.id);
    await expect(page.getByText("E2E: that percentage is out of range.")).toBeVisible();
    expect(status.error).toBeUndefined();
  });
});

test.describe("a refused playlist change", () => {
  /** @type {number[]} */
  const made = [];
  test.afterEach(async ({ request }) => {
    for (const id of made.splice(0)) expect((await request.delete(`/api/provider/playlists/${id}`)).status(), `playlist ${id}`).toBeLessThan(300);
  });

  async function playlist(request, title) {
    const resp = await request.post("/api/provider/playlists", { data: { title } });
    expect(resp.ok()).toBeTruthy();
    const p = await resp.json();
    made.push(p.id);
    return p;
  }

  test("a refused mirror toggle says why", async ({ page, request }) => {
    const p = await playlist(request, "api-errors-mirror");
    await home(page);
    await page.route(`**/api/provider/playlists/${p.id}/mirror`, (route) =>
      route.fulfill({ status: 400, json: { error: "E2E: no mirror folder is set." } }));
    await (await app(page)).evaluate(async (a, id) => {
      await a.loadPlaylists();
      await a.toggleMirror(a.playlists.find((x) => x.id === id), "filesystem");
    }, p.id);
    await expect(page.getByText("E2E: no mirror folder is set.")).toBeVisible();
  });

  test("a new playlist refused without a reason says so in its own words", async ({ page }) => {
    await home(page);
    await page.route("**/api/provider/playlists", (route) => route.request().method() === "POST"
      ? route.fulfill({ status: 500, body: "" })
      : route.continue());
    const created = await (await app(page)).evaluate((a) => a.createTrobarPlaylist("api-errors-never"));
    expect(created).toBeNull();
    await expect(page.getByText("The playlist couldn't be saved.")).toBeVisible();
  });
});

test("a sync that is already running is followed, not reported", async ({ page }) => {
  await home(page);
  await page.route("**/api/provider/playlists/sync", (route) =>
    route.fulfill({ status: 409, json: { error: "E2E: a sync is already running." } }));
  await page.route("**/api/provider/playlists/sync/status", (route) =>
    route.fulfill({ json: { running: false } }));
  const polled = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/provider/playlists/sync/status");
  await (await app(page)).evaluate((a) => a.syncPlaylists());
  await polled;
  await expect.poll(() => page.evaluate(() => window.Alpine.$data(document.body).loadingPlaylists)).toBe(false);
  await expect(page.getByText("E2E: a sync is already running.")).toHaveCount(0);
});

test("an album's tracks that fail to load say so, and opening it again asks again", async ({ page }) => {
  await home(page);
  let asked = 0;
  await page.route("**/api/library/album-tracks?*", (route) => {
    asked += 1;
    return asked === 1
      ? route.fulfill({ status: 500, json: { error: "E2E: the tracks could not be read." } })
      : route.fulfill({ json: [{ id: 1, title: "E2E track", disc_no: 1 }] });
  });
  const handle = await app(page);
  await handle.evaluate((a) => a.openAlbum("E2E Artist", "E2E Album"));
  await expect(page.getByText("E2E: the tracks could not be read.")).toBeVisible();
  // Closed, not an empty dialog that reads as an album with no tracks.
  await expect(page.locator("[data-album-dialog]")).toBeHidden();
  await handle.evaluate((a) => a.openAlbum("E2E Artist", "E2E Album"));
  expect(asked).toBe(2);
  await expect(page.locator("[data-album-dialog]").getByText("E2E track")).toBeVisible();
});

test.describe("refused admin changes", () => {
  test("a switch whose plan changed reloads the preview and says why", async ({ page }) => {
    await home(page);
    await page.route("**/api/admin/provider/switch", (route) =>
      route.fulfill({ status: 409, json: { error: "E2E: the plan changed; look again.", code: "plan_changed" } }));
    await page.route("**/api/admin/provider/switch/preview", (route) =>
      route.fulfill({ json: { from: "roon", carried: { ids: [], count: 0 }, removed: { count: 0 }, kept: {} } }));
    const reviewed = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/admin/provider/switch/preview");
    const error = await (await app(page)).evaluate(async (a) => {
      a.switchFlow = { open: true, step: "review", target: "filesystem", values: {}, test: null, testing: false,
        preview: { from: "roon", carried: { ids: [], count: 0 }, removed: { count: 0 }, kept: {} },
        loading: false, busy: false, error: "", result: null, sync: null };
      await a.confirmSwitch();
      return { error: a.switchFlow.error, step: a.switchFlow.step, busy: a.switchFlow.busy };
    });
    await reviewed;
    expect(error).toEqual({ error: "E2E: the plan changed; look again.", step: "review", busy: false });
  });

  test("a mapping list that fails to load says so", async ({ page }) => {
    await home(page);
    await page.route("**/api/admin/roon-profiles", (route) =>
      route.fulfill({ status: 502, json: { error: "E2E: Roon did not answer." } }));
    await (await app(page)).evaluate((a) => a.loadRoonProfiles());
    await expect(page.getByText("E2E: Roon did not answer.")).toBeVisible();
  });

  test("a refusal without a reason still names the status in the wrapped message", async ({ page }) => {
    await home(page);
    await page.route("**/api/admin/delegations", (route) => route.request().method() === "POST"
      ? route.fulfill({ status: 500, body: "" })
      : route.continue());
    const shown = await (await app(page)).evaluate(async (a) => {
      a.newDelegation = { grantee_user_id: "1", target_user_id: "2" };
      await a.createDelegation();
      return a.delegationError;
    });
    expect(shown).toBe("Failed (HTTP 500)");
  });
});

test("a refused disconnect keeps the profile and says why", async ({ page }) => {
  await home(page);
  await page.route("**/api/profile/tidal", (route) => route.request().method() === "DELETE"
    ? route.fulfill({ status: 500, json: { error: "E2E: Tidal could not be disconnected." } })
    : route.continue());
  const kept = await (await app(page)).evaluate(async (a) => {
    const before = a.profile.username;
    await a.disconnectTidal();
    return { before, after: a.profile.username, error: a.profile.error };
  });
  await expect(page.getByText("E2E: Tidal could not be disconnected.")).toBeVisible();
  expect(kept.after).toBe(kept.before);
  expect(kept.error).toBeUndefined();
});

test("library stats that fail to load are not drawn as data", async ({ page }) => {
  await home(page);
  await page.route("**/api/library/stats", (route) =>
    route.fulfill({ status: 500, json: { error: "E2E: no stats." } }));
  const stats = await (await app(page)).evaluate(async (a) => {
    a.libraryStats = null;
    await a.loadLibraryStats();
    return a.libraryStats;
  });
  expect(stats).toBeNull();
});


test("an artist whose albums fail to load shows none, and says why", async ({ page }) => {
  await home(page);
  await page.route("**/api/library/albums?*", (route) =>
    route.fulfill({ status: 500, json: { error: "E2E: the albums could not be read." } }));
  await page.route("**/api/library/similar-artists?*", (route) =>
    route.fulfill({ status: 502, json: { error: "E2E: Last.fm did not answer." } }));
  const shown = await (await app(page)).evaluate(async (a) => {
    await a.loadAlbumsFor("E2E Artist");
    // The similar-artists load runs on after loadAlbumsFor returns.
    await new Promise((r) => setTimeout(r, 300));
    return { albums: a.albums, similar: a.similarArtists };
  });
  await expect(page.getByText("E2E: the albums could not be read.")).toBeVisible();
  expect(shown).toEqual({ albums: [], similar: [] });
});

test("a health report that fails to load is not drawn", async ({ page }) => {
  await home(page);
  await page.route("**/api/admin/health", (route) =>
    route.fulfill({ status: 500, json: { error: "E2E: health unavailable." } }));
  const health = await (await app(page)).evaluate(async (a) => {
    a.health = null;
    await a.loadHealth();
    return a.health;
  });
  await expect(page.getByText("E2E: health unavailable.")).toBeVisible();
  expect(health).toBeNull();
});

test("a refused scan says why, in the server's words", async ({ page }) => {
  await home(page);
  // Not a 409: a scan already running is followed instead (devices-tab.spec.js).
  await page.route("**/api/library/scan*", (route) => route.request().method() === "POST"
    ? route.fulfill({ status: 400, json: { error: "E2E: the music folder can't be read." } })
    : route.continue());
  const shown = await (await app(page)).evaluate(async (a) => {
    await a.scanLibrary();
    return { error: a.scanError, loading: a.loadingScan };
  });
  expect(shown.error).toContain("(E2E: the music folder can't be read.)");
  expect(shown.loading).toBe(false);
});

test("a scan refused without a reason still names the bare status", async ({ page }) => {
  await home(page);
  await page.route("**/api/library/scan*", (route) => route.request().method() === "POST"
    ? route.fulfill({ status: 500, body: "" })
    : route.continue());
  const shown = await (await app(page)).evaluate(async (a) => {
    await a.scanLibrary();
    return a.scanError;
  });
  expect(shown).toBe("Refresh after scan failed (HTTP 500) — reload the page.");
});
