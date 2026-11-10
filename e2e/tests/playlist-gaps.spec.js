// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// The Gaps sub-tab of Playlists: every visible playlist's unresolved tracks,
// grouped by album, with Exclude and "Request from Lidarr" for ticked
// albums. The e2e library has no gaps of its own, so the list is served by a
// mocked GET (the server side has its own tests) and the two POSTs are
// caught to check what the page sends. One playlist is real, so a playlist
// named in a gap can be opened in the list.

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

function gapsFor(playlistId, { lidarr = true } = {}) {
  const pl = { id: playlistId, title: "E2E Gaps Road Trip", owner: "alice", shared: true };
  return {
    open_count: 6, owners: ["", "alice"], sources: ["roon", "trobar"], lidarr_configured: lidarr,
    groups: [
      { artist: "Aphelion", album: "Parallax", album_from: "source", requestable: true, open_count: 2,
        lidarr: null, playlists: [pl],
        tracks: [{ id: 1, artist: "Aphelion", title: "Halcyon", playlist_id: playlistId, excluded: false },
                 { id: 2, artist: "Aphelion", title: "Low Orbit", playlist_id: playlistId, excluded: false }] },
      { artist: "Marigold Avenue", album: "Copper Days", album_from: "musicbrainz", requestable: true, open_count: 1,
        lidarr: { status: "requested", error: null, at: "2026-11-03 10:00:00" }, playlists: [pl],
        tracks: [{ id: 3, artist: "Marigold Avenue", title: "Salt Road", playlist_id: playlistId, excluded: false }] },
      { artist: "Orchard Hill", album: "Paper Crowns", album_from: "source", requestable: true, open_count: 1,
        lidarr: { status: "failed", error: "no_artist_match", at: "2026-11-03 10:00:00" }, playlists: [pl],
        tracks: [{ id: 4, artist: "Orchard Hill", title: "Glass Garden", playlist_id: playlistId, excluded: false }] },
      { artist: null, album: null, album_from: null, requestable: false, open_count: 2, lidarr: null,
        playlists: [pl],
        tracks: [{ id: 5, artist: "Wren", title: "Kite", playlist_id: playlistId, excluded: false },
                 { id: 6, artist: "Lumen", title: "Ember", playlist_id: playlistId, excluded: false }] },
    ],
  };
}

async function showZeroMatchPlaylists(page) {
  // Another spec may leave "Hide playlists with no local tracks" saved for the
  // shared e2e user, and the real playlist here matches nothing.
  await page.route("**/api/profile", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const resp = await route.fetch();
    return route.fulfill({ response: resp, json: { ...(await resp.json()), hide_zero_match_playlists: false } });
  });
}

test.describe("Playlists → Gaps", () => {
  let playlistId;
  test.beforeEach(async ({ request }) => {
    const made = await request.post("/api/provider/playlists", { data: { title: "E2E Gaps Road Trip" } });
    expect(made.ok()).toBeTruthy();
    playlistId = (await made.json()).id;
  });
  test.afterEach(async ({ request }) => {
    await request.delete(`/api/provider/playlists/${playlistId}`);
  });

  async function openGaps(page, data) {
    await showZeroMatchPlaylists(page);
    await page.route("**/api/provider/playlists/gaps*", (route) =>
      route.request().method() === "GET" ? route.fulfill({ json: data }) : route.fallback());
    await page.goto("/#playlists");
    await page.locator('[data-playlists-subtab="gaps"]').click();
    await expect(page.locator("[data-gap-group]")).toHaveCount(4);
  }

  test("groups by album, marks MusicBrainz albums, and says what Lidarr did", async ({ page }) => {
    await openGaps(page, gapsFor(playlistId));
    await expect(page.locator("[data-gaps-count]")).toHaveText("6");
    const groups = page.locator("[data-gap-group]");
    await expect(groups.nth(0)).toContainText("Aphelion — Parallax");
    await expect(groups.nth(0)).toContainText("Halcyon · Low Orbit");
    await expect(groups.nth(1)).toContainText("(from MusicBrainz)");
    await expect(groups.nth(1).locator("[data-gap-lidarr]")).toContainText("Requested from Lidarr");
    await expect(groups.nth(2).locator("[data-gap-lidarr]")).toHaveText("Lidarr found no such artist");
    await expect(groups.nth(3)).toContainText("No album known");
    await expect(groups.nth(3)).toContainText("can be excluded, not requested");
    await expect(groups.nth(3)).toContainText("Wren — Kite");
    await expect(page.locator("[data-gaps-actions]")).toBeHidden();
  });

  test("Request from Lidarr sends only the ticked albums that can be requested", async ({ page }) => {
    await openGaps(page, gapsFor(playlistId));
    let sent = null;
    await page.route("**/api/provider/playlists/gaps/request", (route) => {
      sent = route.request().postDataJSON();
      return route.fulfill({ json: { albums: 1, queued: true } });
    });
    const groups = page.locator("[data-gap-group]");
    await groups.nth(0).locator("[data-gap-tick]").check();
    await groups.nth(3).locator("[data-gap-tick]").check();
    await expect(page.locator("[data-gaps-actions]")).toContainText("2 albums selected (4 tracks)");
    await page.locator("[data-gaps-request]").click();
    await expect.poll(() => sent).toEqual({ albums: [{ artist: "Aphelion", album: "Parallax" }] });
    await expect(page.locator("[data-gaps-actions]")).toBeHidden();
  });

  test("a request the server drops says nothing was asked", async ({ page }) => {
    await openGaps(page, gapsFor(playlistId));
    await page.route("**/api/provider/playlists/gaps/request",
      (route) => route.fulfill({ json: { albums: 0, queued: false } }));
    await page.locator("[data-gap-group]").nth(0).locator("[data-gap-tick]").check();
    await page.locator("[data-gaps-request]").click();
    await expect(page.getByRole("status")).toContainText("Nothing was asked");
    await expect(page.getByText("Asked Lidarr for")).toHaveCount(0);
  });

  test("a group without an album alone can't be requested, only excluded", async ({ page }) => {
    await openGaps(page, gapsFor(playlistId));
    await page.locator("[data-gap-group]").nth(3).locator("[data-gap-tick]").check();
    await expect(page.locator("[data-gaps-request]")).toBeDisabled();
    await expect(page.locator("[data-gaps-exclude]")).toBeEnabled();
  });

  test("Exclude sends every open track of the ticked groups", async ({ page }) => {
    await openGaps(page, gapsFor(playlistId));
    let sent = null;
    await page.route("**/api/provider/playlists/gaps/exclude", (route) => {
      sent = route.request().postDataJSON();
      return route.fulfill({ json: { changed: 3 } });
    });
    const groups = page.locator("[data-gap-group]");
    await groups.nth(0).locator("[data-gap-tick]").check();
    await groups.nth(1).locator("[data-gap-tick]").check();
    await page.locator("[data-gaps-exclude]").click();
    await expect.poll(() => sent).toEqual({ ids: [1, 2, 3], excluded: true });
  });

  test("without Lidarr set up there is no request button", async ({ page }) => {
    await openGaps(page, gapsFor(playlistId, { lidarr: false }));
    await page.locator("[data-gap-group]").nth(0).locator("[data-gap-tick]").check();
    await expect(page.locator("[data-gaps-exclude]")).toBeVisible();
    await expect(page.locator("[data-gaps-request]")).toBeHidden();
  });

  test("a playlist named in a gap opens with its own review", async ({ page }) => {
    await openGaps(page, gapsFor(playlistId));
    await page.locator("[data-gap-group]").nth(0).getByRole("link", { name: "E2E Gaps Road Trip" }).click();
    const row = page.locator(`[data-playlist-id="${playlistId}"]`);
    await expect(row).toBeVisible();
    await expect(page.locator("[data-gaps]")).toBeHidden();
    await expect(page.locator("[data-unresolved-review]:visible")).toHaveCount(1);
  });

  test("nothing missing says so", async ({ page }) => {
    await showZeroMatchPlaylists(page);
    await page.route("**/api/provider/playlists/gaps*", (route) => route.fulfill({
      json: { groups: [], open_count: 0, owners: [], sources: [], lidarr_configured: true } }));
    await page.goto("/#playlists");
    await page.locator('[data-playlists-subtab="gaps"]').click();
    await expect(page.locator("[data-gaps-empty]")).toHaveText(/Nothing missing/);
    await expect(page.locator("[data-gaps-count]")).toBeHidden();
  });

  test("passes axe with albums ticked", async ({ page }) => {
    await openGaps(page, gapsFor(playlistId));
    await page.locator("[data-gap-group]").nth(0).locator("[data-gap-tick]").check();
    const results = await new AxeBuilder({ page }).withTags(WCAG).include("[data-gaps]").analyze();
    expect(results.violations).toEqual([]);
  });
});
