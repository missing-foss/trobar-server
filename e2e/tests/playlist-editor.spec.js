// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

/** Opens a playlist row's Actions menu (#287) and picks one of its items. */
async function action(row, name) {
  await row.getByRole("button", { name: "Actions" }).click();
  await row.getByRole("menuitem", { name }).click();
}
const AxeBuilder = require("@axe-core/playwright").default;

// Playlists made in Trobar: New playlist, the editor (rename, reorder,
// remove, delete) and the Library's "Add to playlist…" on an album row.
// The e2e library is empty, so a playlist here can't hold real tracks: the
// editor's track list is served by a mocked GET, and its writes are caught
// to check the exact order sent. Creating, renaming and deleting go to the
// real server. Each test deletes the playlists it made.

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const VARIANTS = [
  { brand: "troubadour", mode: "dark" }, { brand: "troubadour", mode: "light" },
  { brand: "garriga", mode: "dark" }, { brand: "garriga", mode: "light" },
  { brand: "peira", mode: "dark" }, { brand: "peira", mode: "light" },
  { brand: "nuech", mode: "dark" }, { brand: "contraste", mode: "dark" },
];
const TRACKS = [
  { entry_id: 1, track_id: 9001, title: "Smoulder", artist: "Cinder & Ash", album: "Emberwake", duration: 241 },
  { entry_id: 2, track_id: 9002, title: "Two Suns", artist: "Glass Meridian", album: "Latitude", duration: 199 },
  { entry_id: 3, track_id: 9003, title: "Northbound", artist: "Glass Meridian", album: "Latitude", duration: 227 },
];
// A track since removed from the library: no track_id, kept by its entry.
const GONE = { entry_id: 4, track_id: null, title: "Old Tidewater", artist: "Glass Meridian", album: "Latitude", duration: null };

async function setTheme(page, v) {
  await page.evaluate((x) => {
    document.documentElement.setAttribute("data-brand", x.brand);
    document.documentElement.setAttribute("data-theme", x.mode);
  }, v);
}

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

async function gotoPlaylists(page) {
  await showZeroMatchPlaylists(page);
  await page.goto("/#/playlists");
  await page.waitForFunction(() => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const a = window.Alpine.$data(document.querySelector("[x-data]"));
    a.goToTab("playlists");
    await a.loadPlaylists();
  });
}

async function create(page, title) {
  const resp = await page.request.post("/api/provider/playlists", { data: { title } });
  expect(resp.status()).toBe(201);
  return (await resp.json()).id;
}

async function stored(page, id) {
  return (await (await page.request.get("/api/provider/playlists")).json()).find((p) => p.id === id);
}

// Serves `tracks` as the playlist's entries, and records each PUT's items.
// Like the server, a write returns the entries in their new order, with
// fresh entry ids.
async function mockTracks(page, id, tracks = TRACKS) {
  const puts = [];
  let current = tracks;
  let next = 100;
  await page.route(`**/api/provider/playlists/${id}/tracks`, async (route) => {
    const req = route.request();
    if (req.method() === "GET") return route.fulfill({ json: { tracks: current } });
    if (req.method() === "PUT") {
      const items = req.postDataJSON().items;
      puts.push(items);
      current = items.map((it) => ({
        ...current.find((e) => ("track_id" in it ? e.track_id === it.track_id : e.entry_id === it.entry_id)),
        entry_id: next++,
      }));
      return route.fulfill({ json: { status: "ok", tracks: current.length, entries: current } });
    }
    return route.continue();
  });
  return puts;
}

const ids = (items) => items.map((it) => ("track_id" in it ? it.track_id : `entry ${it.entry_id}`));

test.describe("Playlists made in Trobar", () => {
  const made = [];
  test.afterEach(async ({ request }) => {
    for (const id of made.splice(0)) await request.delete(`/api/provider/playlists/${id}`);
  });

  test("New playlist asks for a name, then shows the one it made", async ({ page }) => {
    await gotoPlaylists(page);
    await page.getByRole("button", { name: "New playlist", exact: true }).click();
    const dialog = page.locator("[data-new-playlist-dialog]");
    await expect(page.locator("[data-new-playlist-create]")).toBeDisabled();
    await expect(page.locator("[data-new-playlist-name]")).toBeFocused();
    await page.locator("[data-new-playlist-name]").fill("E2E Made Here");
    await page.locator("[data-new-playlist-create]").click();
    await expect(dialog.getByText("No tracks yet.", { exact: false })).toBeVisible();
    await page.locator("[data-new-playlist-done]").click();
    await expect(dialog).toBeHidden();
    const id = await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).newPlaylistPin);
    made.push(id);
    expect((await stored(page, id)).title).toBe("E2E Made Here");
    const row = page.locator("[data-playlist-row]", { hasText: "E2E Made Here" });
    await expect(row.locator('[title="Made in Trobar"]')).toHaveCount(1);
    expect((await stored(page, id)).shared).toBeFalsy();
  });

  test("the arrows, ✕ and dragging write the list back in the new order", async ({ page }) => {
    const id = await create(page, "E2E Reorder Me");
    made.push(id);
    const puts = await mockTracks(page, id);
    await gotoPlaylists(page);
    const row = page.locator("[data-playlist-row]", { hasText: "E2E Reorder Me" });
    await action(row, "Edit");
    const editor = row.locator("[data-playlist-editor]");
    await expect(editor.getByRole("listitem")).toHaveCount(3);
    await expect(editor.getByRole("button", { name: "Move Smoulder up" })).toBeDisabled();
    await expect(editor.getByRole("button", { name: "Move Northbound down" })).toBeDisabled();

    await editor.getByRole("button", { name: "Move Two Suns up" }).click();
    await expect.poll(() => puts.length).toBe(1);
    expect(ids(puts[0])).toEqual([9002, 9001, 9003]);
    await expect(editor.getByRole("listitem").first()).toContainText("Two Suns");

    await editor.getByRole("button", { name: "Remove Smoulder" }).click();
    await expect.poll(() => puts.length).toBe(2);
    expect(ids(puts[1])).toEqual([9002, 9003]);
    await expect(editor.getByRole("listitem")).toHaveCount(2);

    await editor.getByRole("listitem").nth(1).dragTo(editor.getByRole("listitem").nth(0));
    await expect.poll(() => puts.length).toBe(3);
    expect(ids(puts[2])).toEqual([9003, 9002]);
  });

  test("a track that left the library stays, marked, and moves by its entry", async ({ page }) => {
    const id = await create(page, "E2E Gone Track");
    made.push(id);
    const puts = await mockTracks(page, id, [TRACKS[0], GONE]);
    await gotoPlaylists(page);
    const row = page.locator("[data-playlist-row]", { hasText: "E2E Gone Track" });
    await action(row, "Edit");
    const editor = row.locator("[data-playlist-editor]");
    await expect(editor.getByRole("listitem").nth(1)).toContainText("Old Tidewater · not in the library");
    await editor.getByRole("button", { name: "Move Old Tidewater up" }).click();
    await expect.poll(() => puts.length).toBe(1);
    expect(puts[0]).toEqual([{ entry_id: 4 }, { track_id: 9001 }]);
    // The next write uses the entry id the server just returned, not the old one.
    await editor.getByRole("button", { name: "Move Old Tidewater down" }).click();
    await expect.poll(() => puts.length).toBe(2);
    expect(puts[1]).toEqual([{ track_id: 9001 }, { entry_id: 100 }]);
  });

  test("Delete… asks first, and only removes the playlist when confirmed", async ({ page }) => {
    const id = await create(page, "E2E Delete Me");
    made.push(id);
    await gotoPlaylists(page);
    const row = page.locator("[data-playlist-row]", { hasText: "E2E Delete Me" });

    page.once("dialog", (d) => d.dismiss());
    await action(row, "Delete…");
    expect(await stored(page, id)).toBeTruthy();

    page.once("dialog", (d) => {
      expect(d.message()).toContain("“E2E Delete Me”");
      // #287's plain words, the same for every playlist delete.
      expect(d.message()).toContain("The songs stay in your library.");
      d.accept();
    });
    await action(row, "Delete…");
    await expect(row).toHaveCount(0);
    expect(await stored(page, id)).toBeUndefined();
  });

  test("Add to playlist… on an album row appends the album, or starts a playlist named after it", async ({ page }) => {
    const id = await create(page, "E2E Album Target");
    made.push(id);
    const posts = [];
    await page.route("**/api/provider/playlists/*/tracks", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      posts.push({ url: route.request().url(), body: route.request().postDataJSON() });
      return route.fulfill({ json: { status: "ok", tracks: 4, added: 4 } });
    });
    await trackFetches(page);
    await page.goto("/#/library");
    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).goToTab("library"));
    await settled(page);
    await page.evaluate(() => {
      const a = window.Alpine.$data(document.querySelector("[x-data]"));
      a.selectedArtist = "E2E Playlist Artist";
      a.libraryView = "albums";
      a.albums = [{ album: "E2E Playlist Album", track_count: 4, year: 2019 }];
      a.profile.cover_view_mode = "list";
    });
    await settled(page);

    const open = page.getByRole("button", { name: "Add to playlist…" });
    await open.click();
    const menu = page.getByRole("menu");
    await expect(menu).toContainText("Add the 4 tracks to");
    await menu.getByRole("menuitem", { name: "E2E Album Target" }).click();
    await expect(page.getByText("Added 4 tracks to “E2E Album Target”.")).toBeVisible();
    expect(posts[0].url).toContain(`/api/provider/playlists/${id}/tracks`);
    expect(posts[0].body).toEqual({ albums: [{ artist: "E2E Playlist Artist", album: "E2E Playlist Album" }] });
    await expect(menu).toBeHidden();

    await open.click();
    await menu.getByRole("menuitem", { name: "New playlist…" }).click();
    await expect(page.getByText("Added 4 tracks to “E2E Playlist Album”.")).toBeVisible();
    const fresh = (await (await page.request.get("/api/provider/playlists")).json())
      .find((p) => p.title === "E2E Playlist Album" && p.source_provider === "trobar");
    expect(fresh).toBeTruthy();
    made.push(fresh.id);
    expect(posts[1].url).toContain(`/api/provider/playlists/${fresh.id}/tracks`);
  });

  // One scan per test, as in accessibility.spec.js: a test scanning every
  // theme ran close to its 30 s timeout, and a loaded runner took it over.
  for (const v of VARIANTS) {
    const theme = `${v.brand} ${v.mode}`;

    test(`the editor has no WCAG A/AA violations (${theme})`, async ({ page }) => {
      const id = await create(page, `E2E Scan Me ${theme}`);
      made.push(id);
      await mockTracks(page, id);
      await gotoPlaylists(page);
      const row = page.locator("[data-playlist-row]", { hasText: `E2E Scan Me ${theme}` });
      await action(row, "Edit");
      await expect(row.locator("[data-playlist-editor]").getByRole("listitem")).toHaveCount(3);
      await setTheme(page, v);
      const results = await new AxeBuilder({ page }).include("[data-playlist-row]").withTags(WCAG).analyze();
      expect(results.violations.map((r) => `${r.id} x${r.nodes.length}`)).toEqual([]);
    });

    test(`the Add to playlist menu has no WCAG A/AA violations (${theme})`, async ({ page }) => {
      await trackFetches(page);
      await page.goto("/#/library");
      await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).goToTab("library"));
      await settled(page);
      await page.evaluate(() => {
        const a = window.Alpine.$data(document.querySelector("[x-data]"));
        a.selectedArtist = "E2E Playlist Artist";
        a.libraryView = "albums";
        a.albums = [{ album: "E2E Playlist Album", track_count: 4, year: 2019 }];
        a.profile.cover_view_mode = "list";
      });
      await settled(page);
      await page.getByRole("button", { name: "Add to playlist…" }).click();
      await expect(page.getByRole("menu")).toBeVisible();
      await setTheme(page, v);
      const results = await new AxeBuilder({ page }).include("[role=menu]").withTags(WCAG).analyze();
      expect(results.violations.map((r) => `${r.id} x${r.nodes.length}`)).toEqual([]);
    });
  }
});
