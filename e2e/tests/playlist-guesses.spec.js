// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// A playlist made on a device: an entry whose file Trobar didn't put on the
// card, but whose name fits a library track, is offered as a guess with
// Accept and Exclude, and matched only once accepted. The e2e library is
// empty, so no real guess can happen here: the playlist and its one
// unmatched entry are real, the review list is served by a mocked GET, and
// the accept and exclude requests are caught to check what the page sends.

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const VARIANTS = [
  { brand: "troubadour", mode: "dark" }, { brand: "troubadour", mode: "light" },
  { brand: "garriga", mode: "dark" }, { brand: "garriga", mode: "light" },
  { brand: "peira", mode: "dark" }, { brand: "peira", mode: "light" },
  { brand: "nuech", mode: "dark" }, { brand: "contraste", mode: "dark" },
];
const ROWS = [
  { id: 11, artist: "Glass Meridian", title: "Two Suns", album: "Latitude", excluded: false, accepted: false,
    source_path: "Music/Glass Meridian/Latitude/01 - Two Suns.mp3",
    suggested: { track_id: 501, artist: "Glass Meridian", title: "Two Suns", album: "Latitude" } },
  { id: 12, artist: "Cinder & Ash", title: "Smoulder", album: "Emberwake", excluded: false, accepted: false,
    source_path: "Music/Cinder & Ash/Emberwake/03 - Smoulder.mp3",
    suggested: { track_id: 502, artist: "Cinder & Ash", title: "Smoulder", album: "Emberwake" } },
  { id: 13, artist: "Unknown", title: "track07", album: null, excluded: false, accepted: false,
    source_path: "Music/track07.mp3", suggested: null },
];

// playlists.spec.js toggles the shared e2e user's "Hide playlists with no
// local tracks", saved server-side, and a playlist here matches no track (the
// e2e library is empty). Serve this page the real profile with that filter
// off, whatever another spec running alongside has left saved.
async function showZeroMatchPlaylists(page) {
  await page.route("**/api/profile", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const resp = await route.fetch();
    return route.fulfill({ response: resp, json: { ...(await resp.json()), hide_zero_match_playlists: false } });
  });
}

test.describe("File-name guesses in a device playlist's review list", () => {
  let device;
  test.beforeEach(async ({ request }) => {
    const created = await request.post("/api/devices", { data: { name: "E2E Guess DAP", device_type: "dap" } });
    expect(created.ok()).toBeTruthy();
    device = await created.json();
    const uploaded = await request.post("/api/device/playlists", {
      headers: { Authorization: `Bearer ${device.token}` },
      data: { playlists: [{ path: "Playlists/E2E Guess Mix.m3u", entries: ["Side Loaded/Bootleg/01 - Live.mp3"] }] },
    });
    expect(uploaded.ok()).toBeTruthy();
  });
  test.afterEach(async ({ request }) => {
    await request.delete(`/api/devices/${device.id}`);
  });

  async function openReview(page) {
    const accepts = [];
    const excludes = [];
    await page.route("**/api/provider/playlists/*/unresolved-tracks", (route) =>
      route.fulfill({ json: JSON.parse(JSON.stringify(ROWS)) }));
    await page.route("**/api/provider/playlists/*/unresolved-tracks/accept", (route) => {
      const body = route.request().postDataJSON();
      accepts.push(body);
      return route.fulfill({ json: { changed: body.ids, unresolved_count: 1 } });
    });
    await page.route("**/api/provider/playlists/*/unresolved-tracks/exclude", (route) => {
      excludes.push(route.request().postDataJSON());
      return route.fulfill({ json: { unresolved_count: 1 } });
    });
    await showZeroMatchPlaylists(page);
    await page.goto("/#/playlists");
    await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
    await page.evaluate(async () => {
      const a = window.Alpine.$data(document.querySelector("[x-data]"));
      a.goToTab("playlists");
      await a.loadPlaylists();
    });
    const row = page.locator("[data-playlist-row]", { hasText: "E2E Guess Mix" });
    await row.getByRole("button", { name: "1 not matched locally" }).click();
    return { row, accepts, excludes };
  }

  test("a guess shows what it would match and where it came from, and Accept and Undo send it", async ({ page }) => {
    const { row, accepts } = await openReview(page);
    const guesses = row.locator("[data-guess-row]");
    await expect(guesses).toHaveCount(2);
    await expect(guesses.first()).toContainText("Guessed from the file name: Glass Meridian – Two Suns");
    await expect(guesses.first()).toContainText("Music/Glass Meridian/Latitude/01 - Two Suns.mp3");
    await expect(row.getByText("Not sent to a device or a mirror until you accept it.")).toBeVisible();

    await row.getByRole("button", { name: "Accept Glass Meridian – Two Suns" }).click();
    await expect(guesses.first()).toContainText("Accepted: matched from now on");
    expect(accepts).toEqual([{ ids: [11], accepted: true }]);

    await guesses.first().getByRole("button", { name: "Undo" }).click();
    await expect(row.getByRole("button", { name: "Accept Glass Meridian – Two Suns" })).toBeVisible();
    expect(accepts[1]).toEqual({ ids: [11], accepted: false });
  });

  test("Exclude on a guess excludes it alone, and Exclude all leaves the guesses out", async ({ page }) => {
    const { row, excludes } = await openReview(page);
    await row.getByRole("button", { name: "Exclude Cinder & Ash – Smoulder" }).click();
    await expect(row.locator("[data-guess-row]").nth(1)).toContainText("Excluded");
    expect(excludes[0]).toEqual({ ids: [12], excluded: true });

    // The plain unmatched entry is listed below the guesses, as before.
    await expect(row.getByText("Unknown")).toBeVisible();
    await row.getByRole("button", { name: "Exclude all" }).click();
    await expect.poll(() => excludes.length).toBe(2);
    expect(excludes[1]).toEqual({ ids: [13], excluded: true });
  });

  // One scan per test, as in accessibility.spec.js: a test scanning every
  // theme runs close to its 30 s timeout on a loaded runner.
  for (const v of VARIANTS) {
    test(`the open review list has no WCAG A/AA violations (${v.brand} ${v.mode})`, async ({ page }) => {
      const { row } = await openReview(page);
      await expect(row.locator("[data-guess-row]")).toHaveCount(2);
      await page.evaluate((x) => {
        document.documentElement.setAttribute("data-brand", x.brand);
        document.documentElement.setAttribute("data-theme", x.mode);
      }, v);
      const results = await new AxeBuilder({ page }).include("[data-guess-row]").withTags(WCAG).analyze();
      expect(results.violations.map((r) => `${r.id} x${r.nodes.length}`)).toEqual([]);
    });
  }
});
