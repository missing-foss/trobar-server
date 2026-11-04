// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// #287: every playlist row has one Actions button; its menu lists what that
// playlist allows for that user, and Delete… (made in Trobar or uploaded,
// owner or admin) comes last. The rights cases are set from state, the way
// the list arrives; the delete of an upload goes through the real server.

async function gotoPlaylists(page) {
  await trackFetches(page);
  await page.goto("/#/playlists");
  await settled(page);
}

const row = (page, title) => page.locator("[data-playlist-row]", { hasText: title });

/** The item names of a row's Actions menu, opened. */
async function menuItems(page, title) {
  await row(page, title).getByRole("button", { name: "Actions" }).click();
  const menu = row(page, title).getByRole("menu");
  await expect(menu).toBeVisible();
  return (await menu.getByRole("menuitem").allTextContents()).map((t) => t.trim());
}

const base = {
  owner_user_id: 1, owner_username: "e2e-mock-user", shared: 1,
  mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: true,
  golden_owner_username: null, inferred_origin_provider: null,
  track_count: 10, matched_count: 5, unresolved_count: 0, lidarr_request_configured: false,
};

async function seed(page, playlists, admin) {
  await page.evaluate(([pls, isAdmin]) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.playlists = pls;
    d.profile.is_admin = isAdmin;
  }, [playlists, admin]);
  await settled(page);
}

test("the menu lists what each playlist allows, Delete… last", async ({ page }) => {
  await gotoPlaylists(page);
  await seed(page, [
    { ...base, id: 901, title: "E2E Own Upload", source_provider: "upload", is_own: true },
    { ...base, id: 902, title: "E2E Own Made Here", source_provider: "trobar", is_own: true },
    { ...base, id: 903, title: "E2E Someone's Upload", source_provider: "upload", is_own: false,
      owner_user_id: 2, owner_username: "someone" },
  ], false);

  expect(await menuItems(page, "E2E Own Upload")).toEqual(["Mirror…", "Download…", "Sync to…", "Delete…"]);
  await page.keyboard.press("Escape");
  expect(await menuItems(page, "E2E Own Made Here")).toEqual(["Edit", "Mirror…", "Download…", "Sync to…", "Delete…"]);
  await page.keyboard.press("Escape");
  // Not the uploader and not an admin: neither Edit nor Delete….
  expect(await menuItems(page, "E2E Someone's Upload")).toEqual(["Mirror…", "Download…", "Sync to…"]);
});

test("an admin can delete someone else's upload", async ({ page }) => {
  await gotoPlaylists(page);
  await seed(page, [{ ...base, id: 903, title: "E2E Someone's Upload", source_provider: "upload", is_own: false,
    owner_user_id: 2, owner_username: "someone" }], true);
  expect(await menuItems(page, "E2E Someone's Upload")).toContain("Delete…");
});

test("a provider's playlist has no Delete…, whoever owns it", async ({ page }) => {
  await gotoPlaylists(page);
  await seed(page, [{ ...base, id: 904, title: "E2E From A Folder", source_provider: "filesystem", is_own: true }], true);
  expect(await menuItems(page, "E2E From A Folder")).not.toContain("Delete…");
});

test("Escape and a click elsewhere close the menu; each item has its icon", async ({ page }) => {
  await gotoPlaylists(page);
  await seed(page, [{ ...base, id: 901, title: "E2E Own Upload", source_provider: "upload", is_own: true }], false);
  const r = row(page, "E2E Own Upload");
  await r.getByRole("button", { name: "Actions" }).click();
  const menu = r.getByRole("menu");
  await expect(menu).toBeVisible();
  for (const item of await menu.getByRole("menuitem").all()) {
    await expect(item.locator("svg")).toHaveCount(1);
  }
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await r.getByRole("button", { name: "Actions" }).click();
  await expect(menu).toBeVisible();
  await page.locator("header").click({ position: { x: 5, y: 5 } });
  await expect(menu).toBeHidden();
});

test("Sync to… opens the device picker for that playlist", async ({ page }) => {
  // The one item whose wiring moved into the menu without a test of its own
  // (reviewer-bot on #441): the old row button was only ever measured.
  await gotoPlaylists(page);
  await seed(page, [{ ...base, id: 905, title: "E2E Sync Me", source_provider: "trobar", is_own: true }], false);
  const r = row(page, "E2E Sync Me");
  await r.getByRole("button", { name: "Actions" }).click();
  await r.getByRole("menuitem", { name: "Sync to…" }).click();
  await expect(r.getByRole("menu")).toBeHidden();
  await expect(page.getByRole("dialog", { name: "Sync to…" })).toBeVisible();
  expect(await page.evaluate(() => {
    const p = window.Alpine.$data(document.querySelector("[x-data]")).picker;
    return { open: p.open, type: p.type, target: p.target };
  })).toEqual({ open: true, type: "playlist", target: "905" });
});

test("Delete… removes an uploaded playlist, after the plain-words confirm", async ({ page }) => {
  await gotoPlaylists(page);
  const up = await page.request.post("/api/provider/playlists/upload", {
    multipart: { file: { name: "E2E Delete This Upload.m3u8", mimeType: "audio/x-mpegurl",
      buffer: Buffer.from("#EXTM3U\n/music/Nobody/None/01 Nothing.flac\n") } },
  });
  expect(up.ok()).toBeTruthy();
  const { playlist_id: id } = await up.json();
  await page.evaluate(async () => {
    await window.Alpine.$data(document.querySelector("[x-data]")).loadPlaylists();
  });
  const r = row(page, "E2E Delete This Upload");
  await expect(r).toHaveCount(1);

  page.once("dialog", (d) => d.dismiss());
  await r.getByRole("button", { name: "Actions" }).click();
  await r.getByRole("menuitem", { name: "Delete…" }).click();
  await expect(r).toHaveCount(1);

  page.once("dialog", (d) => {
    expect(d.message()).toBe(
      "Delete the playlist “E2E Delete This Upload”? Devices it was synced to remove it at their next sync, "
      + "except devices set to keep their content. Copies Trobar made of it on other music servers or in the "
      + "playlist folder are deleted too. The songs stay in your library.");
    d.accept();
  });
  await r.getByRole("button", { name: "Actions" }).click();
  await r.getByRole("menuitem", { name: "Delete…" }).click();
  await expect(r).toHaveCount(0);
  const gone = await page.request.get(`/api/provider/playlists/${id}/tracks`);
  expect(gone.status()).toBe(404);
});
