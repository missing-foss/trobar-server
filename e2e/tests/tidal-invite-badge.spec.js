// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// The header's count of Tidal playlist invitations waiting for the signed-in
// user: on the avatar button, and on the avatar menu's Profile item, which
// then opens the Profile at "Tidal playlists shared with you". The e2e server
// has no Tidal, so /api/tidal-shares is answered here; left alone, the real
// server says 404 (sharing off), which every install starts with.

const me = { id: 1, username: "e2e-mock-user" };
const share = (over) => ({
  id: 1, title: "Road Trip", owner: { id: 4, username: "sam" }, recipient: me, status: "pending",
  two_way: false, playlist_id: null, track_count: 42, unavailable_count: 0, last_synced_at: null, last_error: null,
  created_at: "2026-10-29 10:00:00", accepted_at: null, ended_at: null, delete_original_requested_at: null,
  original_deleted_at: null, delete_blocked_tracks: [],
  ...over,
});

/** Answers /api/tidal-shares from `state`; accept moves that share to active. */
async function fakeShares(page, state) {
  await page.route("**/api/tidal-shares**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const accept = path.match(/^\/api\/tidal-shares\/(\d+)\/accept$/);
    if (accept && req.method() === "POST") {
      const s = state.incoming.find((x) => x.id === Number(accept[1]));
      s.status = "active";
      return route.fulfill({ json: s });
    }
    return route.fulfill({ json: { outgoing: state.outgoing, incoming: state.incoming } });
  });
}

async function open(page, hash = "/#/home") {
  await trackFetches(page);
  await page.goto(hash);
  await settled(page);
}

const badge = (page) => page.locator("[data-invite-badge]");
const avatar = (page) => page.locator("[data-avatar-button]");

test("sharing off: no badge, and the 404 raises no error", async ({ page }) => {
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  const toasts = [];
  await page.exposeFunction("recordToast", (t) => toasts.push(t));
  await page.addInitScript(() => window.addEventListener("trobar-toast", (e) => window.recordToast(e.detail)));
  await open(page);
  await expect(badge(page)).toBeHidden();
  await expect(avatar(page)).toHaveAttribute("aria-label", "Profile");
  expect(toasts).toEqual([]);
  // Chromium itself logs any 404 response as "Failed to load resource";
  // page code can't stop that line. Everything else, the app's own errors
  // included, must be absent.
  expect(errors.filter((t) => !t.startsWith("Failed to load resource: the server responded with a status of 404"))).toEqual([]);
});

test("a pending invitation shows its count, and only invitations to me count", async ({ page }) => {
  await fakeShares(page, {
    incoming: [share({ id: 1 }), share({ id: 2, title: "Bedtime" }), share({ id: 3, status: "active" }),
      share({ id: 4, status: "ended" })],
    outgoing: [share({ id: 5, owner: me, recipient: { id: 2, username: "lea" } })],
  });
  await open(page);
  await expect(badge(page)).toHaveText("2");
  await expect(avatar(page)).toHaveAttribute("aria-label", "Profile, 2 invitations");
  await avatar(page).click();
  await expect(page.locator("[data-invite-menu-count]")).toHaveText("2");
});

test("accepting clears the badge without a reload", async ({ page }) => {
  await fakeShares(page, { incoming: [share({ id: 1 })], outgoing: [] });
  await open(page);
  await expect(badge(page)).toHaveText("1");
  await expect(avatar(page)).toHaveAttribute("aria-label", "Profile, 1 invitation");
  // The menu's Profile item opens the Profile at the invitations.
  await avatar(page).click();
  await page.getByRole("button", { name: /^Profile/ }).filter({ has: page.locator("[data-invite-menu-count]") }).click();
  const incoming = page.locator("[data-tidal-shares-incoming]");
  await expect(incoming).toBeInViewport();
  await incoming.getByRole("button", { name: "Accept" }).click();
  await expect(badge(page)).toBeHidden();
  await expect(avatar(page)).toHaveAttribute("aria-label", "Profile");
});

test("the badge fits the narrow header", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await fakeShares(page, { incoming: [share({ id: 1 })], outgoing: [] });
  await open(page);
  await expect(badge(page)).toBeVisible();
  const b = await badge(page).boundingBox();
  const a = await avatar(page).boundingBox();
  expect(b && a).toBeTruthy();
  // On the avatar's corner and inside the viewport.
  expect(b.x + b.width).toBeLessThanOrEqual(360);
  expect(Math.abs((b.x + b.width) - (a.x + a.width))).toBeLessThan(12);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(360);
});

test("coming back to the tab re-reads the invitations", async ({ page }) => {
  const state = { incoming: [], outgoing: [] };
  await fakeShares(page, state);
  await open(page);
  await expect(badge(page)).toBeHidden();
  state.incoming.push(share({ id: 7 }));
  // Headless pages report "visible"; the listener only needs the event.
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(badge(page)).toHaveText("1");
});

/** Requests for the share list, made after this is called. */
function countShareListRequests(page) {
  const seen = { n: 0 };
  page.on("request", (r) => {
    if (r.method() === "GET" && new URL(r.url()).pathname === "/api/tidal-shares") seen.n += 1;
  });
  return seen;
}

test("the 20 s tick never asks while sharing is off", async ({ page }) => {
  await page.clock.install();
  const seen = countShareListRequests(page);
  await open(page);
  const atLoad = seen.n;
  expect(atLoad).toBeGreaterThan(0);
  await page.clock.runFor(65_000);   // three ticks
  await settled(page);
  expect(seen.n).toBe(atLoad);
});

test("the 20 s tick re-reads the invitations once sharing is on", async ({ page }) => {
  await page.clock.install();
  const state = { incoming: [], outgoing: [] };
  await fakeShares(page, state);
  const seen = countShareListRequests(page);
  await open(page);
  const atLoad = seen.n;
  state.incoming.push(share({ id: 8 }));
  await page.clock.runFor(21_000);
  await expect(badge(page)).toHaveText("1");
  expect(seen.n).toBeGreaterThan(atLoad);
});
