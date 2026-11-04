// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// Five actions that throw something away now ask first, with the same native
// confirm() the page's other destructive actions use. Each test dismisses the
// question (nothing is sent) and then accepts it (the request goes). The
// requests are answered here, so the shared e2e session's data isn't touched.

async function gotoHome(page) {
  await trackFetches(page);
  await page.goto("/#/home");
  await settled(page);
}

/** Counts requests matching method + URL, answering each with `json`. */
async function answer(page, method, pattern, json) {
  const seen = [];
  await page.route(pattern, (route) => {
    if (route.request().method() !== method) return route.fallback();
    seen.push(route.request().url());
    return route.fulfill({ json });
  });
  return seen;
}

/** Answers the next confirm() and returns its message. */
function nextConfirm(page, accept) {
  return new Promise((resolve) => {
    page.once("dialog", async (dialog) => {
      const message = dialog.message();
      if (accept) await dialog.accept(); else await dialog.dismiss();
      resolve(message);
    });
  });
}

function app(page, body, arg) {
  return page.evaluate(([b, a]) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    return new Function("d", "a", `return (async () => { ${b} })()`)(d, a);
  }, [body, arg]);
}

test.describe("Destructive actions ask first", () => {
  test("Clear in the basket", async ({ page }) => {
    await gotoHome(page);
    const cleared = await answer(page, "DELETE", "**/api/basket", { status: "ok" });
    await page.evaluate(() => {
      window.Alpine.store("basket").items = [{ id: 1, kind: "album", key: "A||B", label: "B", devices: [] },
                                             { id: 2, kind: "album", key: "A||C", label: "C", devices: [] }];
    });
    await app(page, "d.basketOpen = true");
    const clear = page.getByRole("button", { name: "Clear", exact: true });
    let asked = nextConfirm(page, false);
    await clear.click();
    expect(await asked).toBe("Clear the basket? Its 2 items are removed; nothing on your devices changes.");
    await settled(page);
    expect(cleared).toHaveLength(0);
    asked = nextConfirm(page, true);
    await clear.click();
    await asked;
    await expect.poll(() => cleared.length).toBe(1);
  });

  test("Delete on the profile picture", async ({ page }) => {
    await gotoHome(page);
    const removed = await answer(page, "DELETE", "**/api/profile/avatar", { avatar_url: null });
    let asked = nextConfirm(page, false);
    await app(page, "await d.removeAvatar()");
    expect(await asked).toBe("Remove your profile picture?");
    expect(removed).toHaveLength(0);
    asked = nextConfirm(page, true);
    await app(page, "await d.removeAvatar()");
    await asked;
    expect(removed).toHaveLength(1);
  });

  test("Off for auto-fill says whether the albums leave the device", async ({ page }) => {
    await gotoHome(page);
    const off = await answer(page, "DELETE", "**/api/devices/999999/autofit", { enabled: false });
    let asked = nextConfirm(page, false);
    await app(page, "await d.disableAutofit({id: 999999, name: 'Probe', source_of_truth: 'server'})");
    expect(await asked).toBe('Turn off auto-fill for "Probe"? The albums it picked are removed from the device at its next sync, unless another selection includes them.');
    asked = nextConfirm(page, false);
    await app(page, "await d.disableAutofit({id: 999999, name: 'Probe', source_of_truth: 'device'})");
    expect(await asked).toContain("Its albums stay on the device while Keep device content is on");
    expect(off).toHaveLength(0);
    asked = nextConfirm(page, true);
    await app(page, "await d.disableAutofit({id: 999999, name: 'Probe', source_of_truth: 'server'})");
    await asked;
    expect(off).toHaveLength(1);
  });

  // Its device id doesn't exist, so the count request fails: the question is
  // asked without the number.
  test("switching Keep device content off, and only off", async ({ page }) => {
    await gotoHome(page);
    const patched = await answer(page, "PATCH", "**/api/devices/999999", { status: "ok" });
    // Turning it on asks nothing.
    await app(page, "await d.updateDeviceSourceOfTruth({id: 999999, name: 'Probe'}, true)");
    expect(patched).toHaveLength(1);
    // Off, refused: nothing sent, and the box is ticked again.
    let asked = nextConfirm(page, false);
    const ticked = await page.evaluate(async () => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = false;
      await d.updateDeviceSourceOfTruth({ id: 999999, name: "Probe" }, false, box);
      return box.checked;
    });
    expect(await asked).toBe('Let the server manage "Probe" again? Tracks on it that no selection includes are removed at its next sync.');
    expect(ticked).toBe(true);
    expect(patched).toHaveLength(1);
    asked = nextConfirm(page, true);
    await app(page, "await d.updateDeviceSourceOfTruth({id: 999999, name: 'Probe'}, false)");
    await asked;
    expect(patched).toHaveLength(2);
  });

  test("switching Keep device content off says how many tracks will go", async ({ page }) => {
    await gotoHome(page);
    const patched = await answer(page, "PATCH", "**/api/devices/999999", { status: "ok" });
    let tracks = 214;
    await page.route("**/api/devices/999999/prune-preview", (route) => route.fulfill({ json: { tracks } }));
    let asked = nextConfirm(page, false);
    await app(page, "await d.updateDeviceSourceOfTruth({id: 999999, name: 'Pixel'}, false)");
    expect(await asked).toBe('Let the server manage "Pixel" again? This will remove 214 tracks from it at its next sync: no selection includes them.');
    expect(patched).toHaveLength(0);
    tracks = 1;
    asked = nextConfirm(page, true);
    await app(page, "await d.updateDeviceSourceOfTruth({id: 999999, name: 'Pixel'}, false)");
    expect(await asked).toBe('Let the server manage "Pixel" again? This will remove 1 track from it at its next sync: no selection includes it.');
    expect(patched).toHaveLength(1);
    // Nothing would be removed: nothing to ask, and the switch goes through.
    tracks = 0;
    let dialogs = 0;
    page.on("dialog", (dialog) => { dialogs += 1; dialog.dismiss(); });
    await app(page, "await d.updateDeviceSourceOfTruth({id: 999999, name: 'Pixel'}, false)");
    expect(dialogs).toBe(0);
    expect(patched).toHaveLength(2);
  });

  test("Cancel on a background job, queued or running", async ({ page }) => {
    await gotoHome(page);
    const cancelled = await answer(page, "DELETE", "**/api/admin/jobs/*", { status: "ok" });
    await answer(page, "GET", "**/api/admin/jobs", { counts: {}, jobs: [] });
    await app(page, "d.jobs = {counts: {}, jobs: [{id: 7, type: 'library_scan', state: 'queued'}, {id: 8, type: 'library_scan', state: 'running'}]}");
    let asked = nextConfirm(page, false);
    await app(page, "await d.cancelJob(7)");
    expect(await asked).toBe("Cancel this library_scan job?");
    asked = nextConfirm(page, false);
    await app(page, "d.jobs = {counts: {}, jobs: [{id: 8, type: 'library_scan', state: 'running'}]}; await d.cancelJob(8)");
    expect(await asked).toBe("Stop this running library_scan job? It stops at its next safe point.");
    expect(cancelled).toHaveLength(0);
    asked = nextConfirm(page, true);
    await app(page, "d.jobs = {counts: {}, jobs: [{id: 7, type: 'library_scan', state: 'queued'}]}; await d.cancelJob(7)");
    await asked;
    expect(cancelled).toHaveLength(1);
  });
});
