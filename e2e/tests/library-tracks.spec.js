// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// An album's details: opened by a click on the album,
// disc headings only for a multi-disc album, and a track's artist only when
// it isn't the album's. The library routes are stubbed with a two-disc album
// and a one-disc one, so the shared e2e library isn't relied on.

const TRACKS = {
  "Event Horizon": [
    { id: 1, disc_no: 1, track_no: 1, title: "Singularity", artist: "Aphelion", duration: 252 },
    { id: 2, disc_no: 1, track_no: 2, title: "Perihelion", artist: "Aphelion feat. Cinder & Ash", duration: 228 },
    { id: 3, disc_no: 2, track_no: 1, title: "Afterglow", artist: "Aphelion", duration: 213 },
  ],
  "Parallax": [
    { id: 4, disc_no: null, track_no: 1, title: "Baseline", artist: "Aphelion", duration: 238 },
  ],
};

/** @param {import('@playwright/test').Page} page */
async function gotoAphelion(page) {
  await page.route("**/api/library/albums?*", (route) => route.fulfill({ json: [
    { album: "Event Horizon", year: 2024, reissue_year: null, track_count: 3 },
    { album: "Parallax", year: 2021, reissue_year: null, track_count: 1 },
  ] }));
  await page.route("**/api/library/album-tracks?*", (route) => {
    const album = new URL(route.request().url()).searchParams.get("album") || "";
    return route.fulfill({ json: TRACKS[album] || [] });
  });
  await page.goto("/#/library");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  // init() restores the view from the URL hash once its first loads are
  // back, and replaceState is the last thing it does there. Navigating
  // before that lets the restore read "#/library" mid-navigation and clear
  // the artist, and lets the profile load overwrite the view mode set below.
  await page.waitForFunction(() => history.state !== null);
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.profile.cover_view_mode = "list";
    await d.selectArtist("Aphelion");
  });
}

const row = (page, album) => page.locator("div.flex.items-center.justify-between", { hasText: album }).first();
const dialog = (page) => page.locator("[data-album-dialog]");

/** Opens an album from its row in the list view. */
async function openFromRow(page, album) {
  await row(page, album).locator("[data-album-open]").click();
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).getByRole("heading", { name: album })).toBeVisible();
  await expect(dialog(page).locator("li").first()).toBeVisible();
}

test.describe("An album's details", () => {
  test("a multi-disc album lists its tracks under disc headings, with a guest's name", async ({ page }) => {
    await gotoAphelion(page);
    await openFromRow(page, "Event Horizon");
    const list = dialog(page);
    await expect(list.getByRole("heading", { name: "Event Horizon" })).toBeVisible();
    await expect(list.getByText("2024 · 3 tracks")).toBeVisible();
    await expect(list.getByText("Disc 1", { exact: true })).toBeVisible();
    await expect(list.getByText("Disc 2", { exact: true })).toBeVisible();
    await expect(list.getByText("· Aphelion feat. Cinder & Ash")).toBeVisible();
    await expect(list.getByText("4:12")).toBeVisible();
    const results = await new AxeBuilder({ page }).include("[data-album-dialog]").analyze();
    expect(results.violations).toEqual([]);
  });

  test("a one-disc album has no disc heading and names no artist", async ({ page }) => {
    await gotoAphelion(page);
    await openFromRow(page, "Parallax");
    const list = dialog(page);
    await expect(list.getByText("Baseline")).toBeVisible();
    await expect(list.getByText(/^Disc /)).toHaveCount(0);
    // Hidden, not absent: the span is there for tracks whose artist differs.
    await expect(list.getByText("· Aphelion", { exact: true })).toBeHidden();
  });

  test("the list view has no Tracks button left", async ({ page }) => {
    await gotoAphelion(page);
    await expect(row(page, "Parallax").getByRole("button", { name: "Tracks", exact: true })).toHaveCount(0);
  });

  test("Escape closes it and focus goes back to the album", async ({ page }) => {
    await gotoAphelion(page);
    await openFromRow(page, "Parallax");
    await page.keyboard.press("Escape");
    await expect(dialog(page)).toBeHidden();
    await expect(row(page, "Parallax").locator("[data-album-open]")).toBeFocused();
  });

  test("Tab stays inside it", async ({ page }) => {
    await gotoAphelion(page);
    await openFromRow(page, "Parallax");
    for (let i = 0; i < 8; i += 1) {
      await page.keyboard.press("Tab");
      expect(await dialog(page).evaluate((el) => el.contains(document.activeElement))).toBe(true);
    }
  });

  test("the Close button and a click outside close it", async ({ page }) => {
    await gotoAphelion(page);
    await openFromRow(page, "Parallax");
    await dialog(page).getByRole("button", { name: "Close" }).click();
    await expect(dialog(page)).toBeHidden();
    await openFromRow(page, "Parallax");
    await page.mouse.click(5, 5);
    await expect(dialog(page)).toBeHidden();
  });

  test("a slow album doesn't fill the dialog after another was opened", async ({ page }) => {
    await gotoAphelion(page);
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route("**/api/library/album-tracks?*", async (route) => {
      const album = new URL(route.request().url()).searchParams.get("album") || "";
      if (album === "Event Horizon") await held;
      return route.fulfill({ json: TRACKS[album] || [] });
    });
    await page.evaluate(() => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      d.openAlbum("Aphelion", "Event Horizon");
      return d.openAlbum("Aphelion", "Parallax");
    });
    await expect(dialog(page).getByText("Baseline")).toBeVisible();
    release();
    await page.waitForTimeout(200);
    await expect(dialog(page).getByRole("heading", { name: "Parallax" })).toBeVisible();
    await expect(dialog(page).locator("li")).toHaveCount(1);
  });

  test("Add to basket goes through the device picker, for the album", async ({ page }) => {
    await gotoAphelion(page);
    await openFromRow(page, "Parallax");
    await dialog(page).getByRole("button", { name: "Add to basket" }).click();
    await expect(dialog(page)).toBeHidden();
    const picker = page.getByRole("dialog", { name: "Sync to…" });
    await expect(picker).toBeVisible();
    expect(await page.evaluate(() => {
      const p = window.Alpine.$data(document.querySelector("[x-data]")).picker;
      return [p.type, p.target];
    })).toEqual(["album", "Aphelion||Parallax"]);
    // One Escape, one dialog: back on the page, on the album.
    await page.keyboard.press("Escape");
    await expect(picker).toBeHidden();
    await expect(dialog(page)).toBeHidden();
  });

  test("in the cover view a click opens the album and the checkbox selects it", async ({ page }) => {
    await gotoAphelion(page);
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).profile.cover_view_mode = "grid";
    });
    const cover = page.locator("div.group.relative", { hasText: "Parallax" }).filter({ visible: true }).first();
    const box = cover.getByRole("checkbox", { name: "Aphelion - Parallax" });
    await box.check();
    await expect(dialog(page)).toBeHidden();
    await expect(page.getByRole("button", { name: "Select 1 album" }).filter({ visible: true })).toBeVisible();
    await box.uncheck();
    await cover.locator("[data-album-open]").click();
    await expect(dialog(page).getByText("Baseline")).toBeVisible();
    await expect(box).not.toBeChecked();
  });
});

test.describe("+ Playlist on a track", () => {
  /** Playlists the menu offers, and what the server is asked for. */
  async function withPlaylists(page) {
    const posts = [];
    const created = [];
    await page.route("**/api/provider/playlists", (route) => {
      if (route.request().method() === "POST") {
        created.push(route.request().postDataJSON());
        return route.fulfill({ json: { id: 77, title: route.request().postDataJSON().title, source_provider: "trobar", is_own: true } });
      }
      return route.fulfill({ json: [
        { id: 11, title: "Morning run", source_provider: "trobar", is_own: true },
        { id: 12, title: "From Subsonic", source_provider: "subsonic", is_own: true },
      ] });
    });
    await page.route("**/api/provider/playlists/*/tracks", (route) => {
      posts.push({ url: route.request().url(), body: route.request().postDataJSON() });
      return route.fulfill({ json: { status: "ok", tracks: 1, added: 1 } });
    });
    return { posts, created };
  }

  test("every track has the button, and the menu adds just that track", async ({ page }) => {
    const { posts } = await withPlaylists(page);
    await gotoAphelion(page);
    await openFromRow(page, "Event Horizon");
    const buttons = dialog(page).getByRole("button", { name: /to a playlist$/ });
    await expect(buttons).toHaveCount(3);
    const perihelion = dialog(page).getByRole("button", { name: "Add “Perihelion” to a playlist" });
    await expect(perihelion).toBeVisible();
    await expect(perihelion).toHaveText("+ Playlist");
    await perihelion.click();
    await expect(perihelion).toHaveAttribute("aria-expanded", "true");
    const menu = page.getByRole("menu").filter({ hasText: "Add this track to" });
    await expect(menu).toBeVisible();
    // Only playlists made in Trobar can take a track.
    await expect(menu.getByRole("menuitem", { name: "From Subsonic" })).toHaveCount(0);
    await menu.getByRole("menuitem", { name: "Morning run" }).click();
    await expect(page.getByText("Added 1 track to “Morning run”.")).toBeVisible();
    expect(posts).toEqual([{ url: expect.stringContaining("/api/provider/playlists/11/tracks"), body: { track_ids: [2] } }]);
    await expect(menu).toBeHidden();
  });

  test("Escape closes the menu first, then the album; a click elsewhere closes the menu", async ({ page }) => {
    const { posts } = await withPlaylists(page);
    await gotoAphelion(page);
    await openFromRow(page, "Event Horizon");
    const button = dialog(page).getByRole("button", { name: "Add “Singularity” to a playlist" });
    const menu = page.getByRole("menu").filter({ hasText: "Add this track to" });
    await button.click();
    await expect(menu).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
    await expect(dialog(page)).toBeVisible();
    await expect(button).toHaveAttribute("aria-expanded", "false");
    await button.click();
    await expect(menu).toBeVisible();
    await dialog(page).getByRole("heading", { name: "Event Horizon" }).click();
    await expect(menu).toBeHidden();
    await expect(dialog(page)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog(page)).toBeHidden();
    expect(posts).toEqual([]);
  });

  test("the whole album goes to a playlist from the dialog", async ({ page }) => {
    const { posts } = await withPlaylists(page);
    await gotoAphelion(page);
    await openFromRow(page, "Parallax");
    await dialog(page).getByRole("button", { name: "Add to playlist…" }).click();
    await page.getByRole("menu").filter({ visible: true }).getByRole("menuitem", { name: "Morning run" }).click();
    await expect(page.getByText("Added 1 track to “Morning run”.")).toBeVisible();
    expect(posts.map((p) => p.body)).toEqual([{ albums: [{ artist: "Aphelion", album: "Parallax" }] }]);
  });

  test("the last track's menu stays inside the dialog", async ({ page }) => {
    await withPlaylists(page);
    await page.setViewportSize({ width: 1000, height: 420 });
    await gotoAphelion(page);
    await openFromRow(page, "Event Horizon");
    await dialog(page).getByRole("button", { name: "Add “Afterglow” to a playlist" }).click();
    const menu = page.getByRole("menu").filter({ hasText: "Add this track to" });
    await expect(menu).toBeVisible();
    const box = await menu.boundingBox();
    const panel = await dialog(page).boundingBox();
    expect(box.y + box.height).toBeLessThanOrEqual(panel.y + panel.height);
    expect(box.y).toBeGreaterThanOrEqual(panel.y);
  });

  test("New playlist… makes one named after the track", async ({ page }) => {
    const { posts, created } = await withPlaylists(page);
    await gotoAphelion(page);
    await openFromRow(page, "Parallax");
    await dialog(page).getByRole("button", { name: "Add “Baseline” to a playlist" }).click();
    await page.getByRole("menu").filter({ hasText: "Add this track to" })
      .getByRole("menuitem", { name: "New playlist…" }).click();
    await expect(page.getByText("Added 1 track to “Baseline”.")).toBeVisible();
    expect(created).toEqual([{ title: "Baseline" }]);
    expect(posts.map((p) => p.body)).toEqual([{ track_ids: [4] }]);
  });
});
