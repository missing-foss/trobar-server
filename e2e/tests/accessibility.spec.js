// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// #42/#101/#109: automated accessibility checks. axe-core runs against the
// real rendered DOM (this is an Alpine SPA, so static grep can't see these)
// and asserts zero WCAG 2.1 A/AA violations — on the unauthenticated login
// page and every authenticated tab (including Administration and Profile,
// across each of their sub-tabs and per-provider config forms), in BOTH
// themes (contrast depends on the active theme's tokens). Regression guard
// for the #101/#109 fixes: label/for associations, image alt text, the
// --c-gray-400 / --c-pill-active-fg / text-accent contrast tokens,
// always-underlined in-text links, landmarks, per-view <h1>.

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

// Every tab reachable from the main UI (admin and profile are exercised
// separately below, since both have sub-tabs — profile's default subTab is
// 'prefs' (see subTab()'s fallback map), so a plain goToTab('profile') here
// would silently never reach 'devices'/'account' at all). "home" is also
// separate (#281) — it's the only one needing to wait for a conditionally
// mocked Most Played chart before scanning.
const AUTH_VIEWS = ["library", "playlists", "selections", "lastfm", "about"];
const ADMIN_SUBTABS = ["config", "health", "users", "delegations"];
// The config sub-tab shows a different form per selected library-source provider.
const PROVIDERS = ["roon", "subsonic", "jellyfin", "emby", "plex", "lms", "filesystem"];
// #234/#235: devices is the one with the per-device cards; prefs/account are
// included too since a shared regression (e.g. a bad panel-b token) would
// otherwise only be caught on whichever sub-tab happens to get visited.
const PROFILE_SUBTABS = ["prefs", "account", "devices"];

async function setTheme(page, theme) {
  await page.evaluate((t) => {
    localStorage.setItem("trobar-theme", t);
    document.documentElement.setAttribute("data-theme", t);
  }, theme);
}

// Readable assertion: on failure print "<rule> (<impact>) x<nodes>" per
// violation rather than a giant node dump.
function summarize(violations) {
  return violations.map((v) => `${v.id} (${v.impact}) x${v.nodes.length}`);
}

for (const theme of ["dark", "light"]) {
  test(`login page has no WCAG A/AA violations (${theme})`, async ({ browser }) => {
    // The login page is unauthenticated — use a fresh context without the
    // logged-in storageState the other tests share.
    const ctx = await browser.newContext({ storageState: undefined });
    const page = await ctx.newPage();
    try {
      await page.goto("/login");
      await setTheme(page, theme);
      await page.reload(); // let the head theme-script re-apply cleanly
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      expect(summarize(results.violations)).toEqual([]);
    } finally {
      await ctx.close();
    }
  });

  // #281: home gets its own test (pulled out of the AUTH_VIEWS loop below)
  // because, when the harness seeded a mock Last.fm username (global-setup,
  // LASTFM_MOCK_USERNAME), it's the one view that needs to wait for a real
  // async fetch (Most Played, #267) rather than the flat settle-timeout the
  // other views use — otherwise this would scan whatever happened to have
  // rendered in 800ms, which #280 already found doesn't reliably include a
  // chart behind its own data fetch (the same class of gap the Health-tab
  // fix below closed for that chart). Falls back to the flat timeout alone
  // when the env var is unset, so the suite doesn't hard-depend on the mock.
  test(`home tab has no WCAG A/AA violations (${theme})`, async ({ page }) => {
    await page.goto("/#/home");
    await setTheme(page, theme);
    if (process.env.LASTFM_MOCK_USERNAME) {
      // Optional-chaining throughout (review feedback): waitForFunction
      // retries on a falsy return but rejects immediately on a thrown
      // error, and nothing here structurally guarantees Alpine has
      // initialized by the first poll (setTheme only touches
      // localStorage/documentElement) — a bare .mostPlayed access on an
      // undefined $data() would fail hard instead of just waiting longer.
      await page.waitForFunction(() => {
        return window.Alpine?.$data(document.querySelector("[x-data]"))?.mostPlayed?.length > 0;
      }, { timeout: 10000 });
    }
    await page.waitForTimeout(800);
    const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);
  });

  for (const view of AUTH_VIEWS) {
    test(`${view} tab has no WCAG A/AA violations (${theme})`, async ({ page }) => {
      await page.goto("/#/home");
      await setTheme(page, theme);
      // SPA navigation — goToTab() flows through applyHistoryState like a real
      // click; hidden tabs are display:none, which axe skips.
      await page.evaluate((v) => window.Alpine.$data(document.querySelector("[x-data]")).goToTab(v), view);
      // Let the tab's widgets / lazily-loaded charts / covers settle.
      await page.waitForTimeout(800);
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      expect(summarize(results.violations)).toEqual([]);
    });
  }

  // #507 item 5: the mirror redesign moved a lot of meaning into icons —
  // the identity-row badge (mirrored-to-X, plus colour for an error state)
  // and the picker's checkmark/greyed rows — none of which the AUTH_VIEWS
  // scan above exercises, since it never seeds a playlist with a mirror
  // actually on. Same "seed real state, then scan" pattern as the profile
  // tab's device fixture and the admin tab's health-panel load above.
  test(`playlists mirror picker has no WCAG A/AA violations (${theme})`, async ({ page }) => {
    await page.goto("/#/home");
    await setTheme(page, theme);
    // goToTab('playlists') kicks off its own loadPlaylists() fetch against
    // the real (empty) backend — setting the mock array in the SAME
    // evaluate() call raced that fetch's resolution and lost, clobbering
    // the seeded row back to [] a few hundred ms later (caught via the
    // failing click's page-snapshot still showing the "No synced
    // playlists yet" empty state). Two separate evaluate() calls, with the
    // tab switch's own fetch given a moment to land first, same ordering
    // gotoPlaylists() in playlists.spec.js already relies on.
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).goToTab("playlists");
    });
    await page.waitForTimeout(400);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 9001, title: "E2E A11y Mirror Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: false,
        subsonic_mirror_enabled: true, subsonic_mirror_configured: true,
        subsonic_mirror_last_error: null, subsonic_mirror_last_error_code: null,
        jellyfin_mirror_enabled: true, jellyfin_mirror_configured: true,
        // A second, FAILING sink — exercises the red/error-titled badge,
        // not just the plain "on" one above.
        jellyfin_mirror_last_error: "Could not reach the Jellyfin mirror target: Connection refused",
        jellyfin_mirror_last_error_code: "unreachable",
        emby_mirror_enabled: false, emby_mirror_configured: true,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(400);
    let results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);

    // Open the picker too — it's a distinct piece of DOM (greyed/checked
    // rows, indigo "not yet on" rows, and the per-row error line) that the
    // closed-row scan above never renders.
    await page.getByRole("button", { name: "Mirror…" }).click();
    await page.waitForTimeout(200);
    results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);
  });

  // #508: the "duel the bard" Easter egg. The closed bard-mark button is
  // already covered by the plain "about tab" scan in AUTH_VIEWS above —
  // this exercises the modal itself in both states the closed-tab scan
  // never renders: this harness's real (empty) library response, and a
  // guess/reveal round (state injected directly, same reasoning as
  // quiz.spec.js — there's no way to get a real {available: true} pair out
  // of an empty seeded library).
  test(`duel-the-bard quiz has no WCAG A/AA violations (${theme})`, async ({ page }) => {
    await page.goto("/#/home");
    await setTheme(page, theme);
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).goToTab("about");
    });
    await page.waitForTimeout(200);
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).openQuiz();
    });
    await page.waitForTimeout(200);
    let results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);

    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.quiz.available = true;
      app.quiz.loading = false;
      app.quiz.a = { artist: "Small Artist", album_count: 3 };
      app.quiz.b = { artist: "Big Artist", album_count: 20 };
    });
    await page.waitForTimeout(200);
    results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);

    // The reveal state (post-guess) is its own distinct DOM — the album
    // counts and win/lose text only render once quiz.guess is set.
    await page.getByRole("button", { name: "Small Artist" }).click();
    await page.waitForTimeout(200);
    results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);
  });

  // Profile: every sub-tab in one test per theme. global-setup doesn't seed
  // any device, and the 'devices' sub-tab's per-device cards (#234) need at
  // least one to actually render — an empty list wouldn't exercise that
  // markup at all — so create one via the same API the UI itself calls,
  // then force a reload (loadDevices() isn't triggered by tab/sub-tab
  // navigation, and the periodic poll is 20s, far longer than this test
  // waits).
  test(`profile tab has no WCAG A/AA violations (${theme})`, async ({ page }) => {
    await page.goto("/#/home");
    await setTheme(page, theme);
    await page.request.post("/api/devices", { data: { name: "a11y-test-device" } });
    await page.evaluate(async () => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      d.goToTab("profile");
      await d.loadDevices();
    });
    const found = [];
    for (const sub of PROFILE_SUBTABS) {
      await page.evaluate((s) => {
        window.Alpine.$data(document.querySelector("[x-data]")).setSubTab("profile", s);
      }, sub);
      await page.waitForTimeout(400);
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      for (const v of results.violations) {
        found.push(`${sub}: ${v.id} x${v.nodes.length}`);
      }
    }
    // #233: the Add Device modal branches its content on device type
    // (enrollment vs. direct-token) — check one of each rather than just
    // whichever branch happens to be selected by default.
    for (const deviceType of ["phone", "folder"]) {
      await page.evaluate((dt) => {
        const d = window.Alpine.$data(document.querySelector("[x-data]"));
        d.openAddDeviceModal();
        d.newDevice.device_type = dt;
      }, deviceType);
      await page.waitForTimeout(200);
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      for (const v of results.violations) {
        found.push(`add-device-modal/${deviceType}: ${v.id} x${v.nodes.length}`);
      }
      await page.evaluate(() => {
        window.Alpine.$data(document.querySelector("[x-data]")).addDeviceModal.open = false;
      });
    }
    expect(found).toEqual([]);
  });

  // Administration: every sub-tab, and every per-provider config form, in one
  // test per theme. Switching adminConfig.provider is a display-only Alpine
  // state change (not saved), so no server state is touched.
  test(`admin tab has no WCAG A/AA violations (${theme})`, async ({ page }) => {
    await page.goto("/#/home");
    await setTheme(page, theme);
    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).goToTab("admin"));
    const found = [];
    for (const sub of ADMIN_SUBTABS) {
      const provs = sub === "config" ? PROVIDERS : [null];
      for (const prov of provs) {
        await page.evaluate(async ([s, p]) => {
          const d = window.Alpine.$data(document.querySelector("[x-data]"));
          d.setSubTab("admin", s);
          if (p) d.adminConfig.provider = p;
          // #266: health's issue-count chart lives behind x-if="health",
          // which stays null until "Check" is clicked — without this, the
          // scan below would never actually render the chart's canvas at
          // all (same "at least one to actually render" reasoning as the
          // profile tab test's device fixture below).
          if (s === "health" && !d.health) await d.loadHealth();
        }, [sub, prov]);
        await page.waitForTimeout(400);
        const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
        for (const v of results.violations) {
          found.push(`${sub}${prov ? "/" + prov : ""}: ${v.id} x${v.nodes.length}`);
        }
      }
    }
    expect(found).toEqual([]);
  });
}
