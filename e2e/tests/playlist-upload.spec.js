// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const VARIANTS = [
  { brand: "troubadour", mode: "dark" }, { brand: "troubadour", mode: "light" },
  { brand: "garriga", mode: "dark" }, { brand: "garriga", mode: "light" },
  { brand: "peira", mode: "dark" }, { brand: "peira", mode: "light" },
  { brand: "nuech", mode: "dark" }, { brand: "contraste", mode: "dark" },
];

// A .m3u8 picked with "Upload playlist…" becomes one of the user's playlists,
// and any playlist downloads as an .m3u8. The e2e library is empty, so every
// entry of an uploaded file is unresolved: the status line says so, and the
// download carries the entries only when unmatched ones are asked for.
// Uploaded playlists can't be deleted, so the one made here stays for the
// rest of the run; the specs that count playlists inject their own lists.

const NAME = "E2E Upload Mix";
const FILE = "#EXTM3U\n#EXTINF:241,E2E Upload Artist - First Song\nE2E Upload Artist/One/01 First Song.flac\n"
  + "#EXTINF:199,E2E Upload Artist - Second Song\nE2E Upload Artist/One/02 Second Song.flac\n";

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

async function upload(page, name, body) {
  await page.getByLabel("Playlist file to upload").setInputFiles({
    name, mimeType: "audio/x-mpegurl", buffer: Buffer.from(body),
  });
}

test.describe.serial("Upload and download a playlist file", () => {
  test("an upload becomes a playlist and says what matched; the same name replaces it", async ({ page }) => {
    await gotoPlaylists(page);
    await upload(page, `${NAME}.m3u8`, FILE);
    const status = page.getByRole("status").filter({ hasText: NAME });
    await expect(status).toHaveText(`“${NAME}” uploaded: 0 of 2 tracks matched. They are all in its unresolved list.`);
    const row = page.locator("[data-playlist-row]", { hasText: NAME });
    await expect(row).toHaveCount(1);
    await expect(row.locator('[title="Uploaded file"]')).toHaveCount(1);

    await upload(page, `${NAME}.m3u8`, "#EXTM3U\nE2E Upload Artist/One/03 Third Song.flac\n");
    await expect(status).toHaveText(`“${NAME}” replaced: 0 of 1 track matched. They are all in its unresolved list.`);
    await page.getByRole("button", { name: "Dismiss" }).click();
    await expect(status).toBeHidden();
  });

  test("a file that isn't a playlist is refused with the reason", async ({ page }) => {
    await gotoPlaylists(page);
    await upload(page, "notes.txt", "hello");
    await expect(page.getByRole("status").filter({ hasText: "Choose a .m3u or .m3u8 file." })).toBeVisible();
  });

  test("Download… gives the .m3u8, with unmatched entries only when asked", async ({ page }) => {
    await gotoPlaylists(page);
    await upload(page, `${NAME}.m3u8`, FILE);
    await expect(page.getByRole("status").filter({ hasText: NAME })).toBeVisible();
    const card = page.locator("[data-playlist-row]", { hasText: NAME });
    await expect(card).toHaveCount(1);
    // Download… is an item of the row's Actions menu (#287); picking it
    // closes the menu and opens the download panel below the row.
    await expect(card.getByRole("link", { name: "Download .m3u8" })).toBeHidden();
    await card.getByRole("button", { name: "Actions" }).click();
    await card.getByRole("menuitem", { name: "Download…" }).click();

    const link = card.getByRole("link", { name: "Download .m3u8" });
    await expect(link).toHaveCount(1);
    const plain = await link.getAttribute("href");
    expect(plain).toMatch(/^\/api\/provider\/playlists\/\d+\/export\.m3u8$/);
    const bare = await (await page.request.get(plain)).text();
    expect(bare).toBe("#EXTM3U\n");

    await card.getByRole("checkbox", { name: "Include unmatched entries, as comments" }).check();
    await expect(link).toHaveAttribute("href", `${plain}?unresolved=1`);
    const resp = await page.request.get(`${plain}?unresolved=1`);
    expect(resp.headers()["content-disposition"]).toContain(`${NAME}.m3u8`);
    expect(await resp.text()).toBe(
      "#EXTM3U\n# E2E Upload Artist - First Song\n# E2E Upload Artist - Second Song\n");
  });

  // One scan per test, as in accessibility.spec.js: a test scanning every
  // theme ran close to its 30 s timeout, and a loaded runner takes it over.
  for (const v of VARIANTS) {
    test(`the status line and the open Download panel have no WCAG A/AA violations (${v.brand} ${v.mode})`, async ({ page }) => {
      await gotoPlaylists(page);
      await upload(page, `${NAME}.m3u8`, FILE);
      await expect(page.getByRole("status").filter({ hasText: NAME })).toBeVisible();
      const card = page.locator("[data-playlist-row]", { hasText: NAME });
      await card.getByRole("button", { name: "Actions" }).click();
      await card.getByRole("menuitem", { name: "Download…" }).click();
      await expect(card.getByRole("link", { name: "Download .m3u8" })).toBeVisible();
      await page.evaluate((x) => {
        document.documentElement.setAttribute("data-brand", x.brand);
        document.documentElement.setAttribute("data-theme", x.mode);
      }, v);
      const results = await new AxeBuilder({ page })
        .include("[data-playlist-row]").include("[role=status]").withTags(WCAG).analyze();
      expect(results.violations.map((r) => `${r.id} x${r.nodes.length}`)).toEqual([]);
    });
  }
});
