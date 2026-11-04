// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");
const AxeBuilder = require("@axe-core/playwright").default;

// Tidal playlist shares: "Share with…" on your own Tidal playlist, the
// invitation and the share's controls on the Profile, and deleting the
// original after a share ends. The e2e server has no Tidal, so
// /api/tidal-shares is answered here; the route tests cover the server.
// With nothing answered, the real server says 404 (sharing off).

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

const share = (over) => ({
  id: 1, title: "Road Trip", owner: { id: 1, username: "e2e-mock-user" }, recipient: { id: 2, username: "lea" },
  status: "active", two_way: false, playlist_id: 901, track_count: 42, unavailable_count: 0,
  last_synced_at: null, last_error: null, created_at: "2026-10-29 10:00:00", accepted_at: "2026-10-29 10:05:00",
  ended_at: null, delete_original_requested_at: null, original_deleted_at: null, delete_blocked_tracks: [],
  ...over,
});

/** Answers /api/tidal-shares* from `state`, and records every write. */
async function fakeShares(page, state) {
  const writes = [];
  await page.route("**/api/tidal-shares**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (req.method() === "GET" && path === "/api/tidal-shares") {
      return route.fulfill({ json: { outgoing: state.outgoing, incoming: state.incoming } });
    }
    if (req.method() === "GET" && path === "/api/tidal-shares/recipients") {
      return route.fulfill({ json: state.recipients });
    }
    writes.push({ method: req.method(), path, body: req.postDataJSON() });
    return route.fulfill({ status: req.method() === "POST" && path === "/api/tidal-shares" ? 201 : 200, json: share({}) });
  });
  return writes;
}

const playlist = {
  owner_user_id: 1, owner_username: "e2e-mock-user", shared: 0, mirror_enabled: false, mirror_last_error: null,
  mirror_folder_configured: true, golden_owner_username: null, inferred_origin_provider: null,
  track_count: 42, matched_count: 30, unresolved_count: 12, lidarr_request_configured: false,
};

async function openPlaylists(page, isAdmin) {
  await trackFetches(page);
  await page.goto("/#/playlists");
  await settled(page);
  await page.evaluate(([pls, admin]) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.playlists = pls;
    d.profile.is_admin = admin;
  }, [[
    { ...playlist, id: 901, title: "E2E Road Trip", source_provider: "tidal", is_own: true },
    { ...playlist, id: 902, title: "E2E Followed Mix", source_provider: "tidal", is_own: false, owner_user_id: 2 },
    { ...playlist, id: 903, title: "E2E Made Here", source_provider: "trobar", is_own: true },
  ], isAdmin]);
  await settled(page);
}

const row = (page, title) => page.locator("[data-playlist-row]", { hasText: title });

async function menuItems(page, title) {
  await row(page, title).getByRole("button", { name: "Actions" }).click();
  const menu = row(page, title).getByRole("menu");
  await expect(menu).toBeVisible();
  const items = (await menu.getByRole("menuitem").allTextContents()).map((t) => t.trim());
  await page.keyboard.press("Escape");
  return items;
}

const recipients = [
  { id: 2, username: "lea", can_receive: true },
  { id: 3, username: "tom", can_receive: false },
];

test("sharing off: no Share with…, no share blocks on the Profile", async ({ page }) => {
  await openPlaylists(page, true);
  expect(await menuItems(page, "E2E Road Trip")).not.toContain("Share with…");
  await page.goto("/#/profile");
  await settled(page);
  await page.getByRole("button", { name: "Accounts", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Streaming accounts" })).toBeVisible();
  await expect(page.locator("[data-tidal-shares-incoming]")).toBeHidden();
  await expect(page.locator("[data-tidal-shares-outgoing]")).toBeHidden();
});

test("Share with… shows on your own Tidal playlist only", async ({ page }) => {
  await fakeShares(page, { outgoing: [], incoming: [], recipients });
  await openPlaylists(page, false);
  expect(await menuItems(page, "E2E Road Trip")).toContain("Share with…");
  expect(await menuItems(page, "E2E Followed Mix")).not.toContain("Share with…");
  expect(await menuItems(page, "E2E Made Here")).not.toContain("Share with…");
});

test("an invitation goes to the chosen person, two-way as ticked", async ({ page }) => {
  const writes = await fakeShares(page, { outgoing: [], incoming: [], recipients });
  await openPlaylists(page, false);
  await row(page, "E2E Road Trip").getByRole("button", { name: "Actions" }).click();
  await row(page, "E2E Road Trip").getByRole("menuitem", { name: "Share with…" }).click();
  const dialog = page.locator("[data-share-dialog]");
  await expect(dialog.getByRole("heading")).toHaveText("Share “E2E Road Trip”");
  // Not an admin: no "Share now".
  await expect(dialog.getByText("Share now, without asking them")).toBeHidden();
  // Someone who must reconnect is said so, before anything is sent.
  await dialog.getByRole("radio", { name: /tom/ }).check();
  await expect(dialog).toContainText("tom has to reconnect Tidal before the copy can appear.");
  await dialog.getByRole("radio", { name: /lea/ }).check();
  await dialog.getByRole("checkbox", { name: /Two-way/ }).check();
  await dialog.getByRole("button", { name: "Send invitation" }).click();
  await expect(dialog).toBeHidden();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ method: "POST", path: "/api/tidal-shares",
    body: { playlist_id: 901, recipient_user_id: 2, two_way: true, push: false } });
  await expect(page.getByRole("status").filter({ hasText: "Invitation sent to lea." })).toBeVisible();
});

test("an admin can share without asking", async ({ page }) => {
  const writes = await fakeShares(page, { outgoing: [], incoming: [], recipients });
  await openPlaylists(page, true);
  await row(page, "E2E Road Trip").getByRole("button", { name: "Actions" }).click();
  await row(page, "E2E Road Trip").getByRole("menuitem", { name: "Share with…" }).click();
  const dialog = page.locator("[data-share-dialog]");
  await dialog.getByRole("checkbox", { name: /Share now, without asking them/ }).check();
  await dialog.getByRole("button", { name: "Share now" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].body).toEqual({ playlist_id: 901, recipient_user_id: 2, two_way: false, push: true });
  await expect(page.getByRole("status").filter({ hasText: "Shared with lea." })).toBeVisible();
});

test("the share dialog has no WCAG A/AA violations", async ({ page }) => {
  await fakeShares(page, { outgoing: [], incoming: [], recipients });
  await openPlaylists(page, true);
  await row(page, "E2E Road Trip").getByRole("button", { name: "Actions" }).click();
  await row(page, "E2E Road Trip").getByRole("menuitem", { name: "Share with…" }).click();
  await expect(page.locator("[data-share-dialog]")).toBeVisible();
  const results = await new AxeBuilder({ page }).include("[data-share-dialog]").withTags(WCAG).analyze();
  expect(results.violations).toEqual([]);
});

const profileState = {
  recipients,
  incoming: [
    share({ id: 11, title: "E2E Invited", status: "pending", owner: { id: 4, username: "sam" },
      recipient: { id: 1, username: "e2e-mock-user" }, accepted_at: null }),
    share({ id: 12, title: "E2E Summer", two_way: true, owner: { id: 3, username: "tom" },
      recipient: { id: 1, username: "e2e-mock-user" }, unavailable_count: 2, last_synced_at: "2026-10-29 09:00:00" }),
  ],
  outgoing: [
    share({ id: 21, title: "E2E Waiting", status: "pending", accepted_at: null }),
    share({ id: 22, title: "E2E Followed", two_way: true, last_error: "two_way_needs_own_playlist" }),
    share({ id: 23, title: "E2E Bedtime", status: "ended", ended_at: "2026-10-12 08:00:00" }),
    share({ id: 24, title: "E2E Lullabies", status: "ended", delete_original_requested_at: "2026-10-29 10:00:00" }),
    share({ id: 25, title: "E2E Car songs", status: "ended", last_error: "copy_lacks_tracks",
      delete_blocked_tracks: [{ track_id: "a", position: 3 }, { track_id: "b", position: 17 }] }),
  ],
};

async function openProfile(page, state) {
  const writes = await fakeShares(page, state);
  await trackFetches(page);
  await page.goto("/#/profile");
  await settled(page);
  // Both lists sit under Profile → Accounts, beside Streaming accounts.
  await page.getByRole("button", { name: "Accounts", exact: true }).click();
  return writes;
}

const card = (page, side, title) => page.locator(`[data-tidal-shares-${side}] > div`, { hasText: title });

test("the recipient answers an invitation and sees what an active share lacks", async ({ page }) => {
  const writes = await openProfile(page, profileState);
  const invite = card(page, "incoming", "E2E Invited");
  await expect(invite).toContainText("New invitation");
  await expect(invite).toContainText("From sam · one-way · 42 tracks");
  await expect(invite).toContainText("keeps it the same as sam's on each playlist sync");
  await invite.getByRole("button", { name: "Accept" }).click();
  await expect.poll(() => writes.map((w) => w.method + " " + w.path)).toEqual(["POST /api/tidal-shares/11/accept"]);

  const summer = card(page, "incoming", "E2E Summer");
  await expect(summer).toContainText("2 tracks aren't available in your region, so your copy doesn't have them.");
  await summer.getByRole("button", { name: "Stop sharing" }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].path).toBe("/api/tidal-shares/12/end");
});

test("the owner sees each share's state and the action it allows", async ({ page }) => {
  const writes = await openProfile(page, profileState);
  await card(page, "outgoing", "E2E Waiting").getByRole("button", { name: "Withdraw" }).click();
  await expect.poll(() => writes.map((w) => w.path)).toEqual(["/api/tidal-shares/21/end"]);

  const followed = card(page, "outgoing", "E2E Followed");
  await expect(followed).toContainText("Needs attention");
  await expect(followed).toContainText("you only follow this one");
  await followed.getByRole("checkbox", { name: /Two-way/ }).uncheck();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toEqual({ method: "PATCH", path: "/api/tidal-shares/22", body: { two_way: false } });

  await expect(card(page, "outgoing", "E2E Car songs"))
    .toContainText("lacks some of its tracks (by position in your playlist: 3 and 17)");
  const queued = card(page, "outgoing", "E2E Lullabies");
  await expect(queued).toContainText("will be deleted at the next playlist sync");
  await queued.getByRole("button", { name: "Keep my original" }).click();
  await expect.poll(() => writes.length).toBe(3);
  expect(writes[2]).toMatchObject({ method: "DELETE", path: "/api/tidal-shares/24/delete-original" });
});

test("deleting the original asks first, and says it can't be undone", async ({ page }) => {
  const writes = await openProfile(page, profileState);
  await card(page, "outgoing", "E2E Bedtime").getByRole("button", { name: "Delete my original…" }).click();
  const dialog = page.locator("[data-delete-original-dialog]");
  await expect(dialog.getByRole("heading")).toHaveText("Delete your original “E2E Bedtime”?");
  await expect(dialog).toContainText("This can't be undone.");
  await dialog.getByRole("button", { name: "Keep it" }).click();
  await expect(dialog).toBeHidden();
  expect(writes).toEqual([]);
  await card(page, "outgoing", "E2E Bedtime").getByRole("button", { name: "Delete my original…" }).click();
  await dialog.getByRole("button", { name: "Delete my original" }).click();
  await expect.poll(() => writes.map((w) => w.method + " " + w.path)).toEqual(["POST /api/tidal-shares/23/delete-original"]);
});

test("the Profile's share lists have no WCAG A/AA violations", async ({ page }) => {
  await openProfile(page, profileState);
  await expect(page.locator("[data-tidal-shares-outgoing]")).toBeVisible();
  const results = await new AxeBuilder({ page })
    .include("[data-tidal-shares-incoming]").include("[data-tidal-shares-outgoing]").withTags(WCAG).analyze();
  expect(results.violations).toEqual([]);
});
