// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");
const AxeBuilder = require("@axe-core/playwright").default;

// #42/#101/#109: automated accessibility checks. axe-core runs against the
// real rendered DOM (this is an Alpine SPA, so static grep can't see these)
// and asserts zero WCAG 2.1 A/AA violations — on the unauthenticated login
// page and every authenticated tab (including Administration and Profile,
// across each of their sub-tabs and per-provider config forms), in every
// brand theme and variant (contrast depends on the active theme's tokens). Regression guard
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
// Users holds the delegations too, as a section (opened below).
const ADMIN_SUBTABS = ["config", "health", "users"];
// The config sub-tab shows a different form per selected library-source provider.
const PROVIDERS = ["roon", "subsonic", "jellyfin", "emby", "plex", "lms", "filesystem"];
// #234/#235: prefs/account are scanned since a shared regression (e.g. a bad
// panel-b token) would otherwise only be caught on whichever sub-tab happens
// to get visited. Devices, with the per-device cards, has been a tab of its
// own since #266 and is scanned below.
const PROFILE_SUBTABS = ["prefs", "account"];

// The admin tab is scanned one page at a time: each sub-tab, and the config
// sub-tab once per provider form.
const ADMIN_PAGES = ADMIN_SUBTABS.flatMap(
  (s) => (s === "config" ? PROVIDERS.map((p) => [s, p]) : [[s, null]]));

// This file holds most of the suite's scans, and Playwright runs a file on
// one worker unless told otherwise, so it would set the whole run's length.
// Its tests can run in any order: the only server state any of them writes
// is the device the devices test adds, and no test here counts devices.
test.describe.configure({ mode: "parallel" });

// Every brand theme in every variant it has (trobar-server#141): contrast
// depends on the active theme's tokens, so each one is scanned. Nuèch and
// Contraste are dark only.
const VARIANTS = [
  { brand: "troubadour", mode: "dark" }, { brand: "troubadour", mode: "light" },
  { brand: "garriga", mode: "dark" }, { brand: "garriga", mode: "light" },
  { brand: "peira", mode: "dark" }, { brand: "peira", mode: "light" },
  { brand: "nuech", mode: "dark" }, { brand: "contraste", mode: "dark" },
];

async function setTheme(page, variant) {
  await page.evaluate((v) => {
    localStorage.setItem("trobar-brand", v.brand);
    localStorage.setItem("trobar-theme", v.mode);
    document.documentElement.setAttribute("data-brand", v.brand);
    document.documentElement.setAttribute("data-theme", v.mode);
  }, variant);
}

// Readable assertion: on failure print "<rule> (<impact>) x<nodes>" per
// violation rather than a giant node dump.
function summarize(violations) {
  return violations.map((v) => `${v.id} (${v.impact}) x${v.nodes.length}`);
}

for (const variant of VARIANTS) {
  const theme = `${variant.brand} ${variant.mode}`;
  test(`login page has no WCAG A/AA violations (${theme})`, async ({ browser }) => {
    // The login page is unauthenticated — use a fresh context without the
    // logged-in storageState the other tests share.
    const ctx = await browser.newContext({ storageState: undefined });
    const page = await ctx.newPage();
    try {
      await page.goto("/login");
      await setTheme(page, variant);
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
  // async fetch (Most Played, #267) as well as the generic settle the other
  // views use — a scan of whatever had rendered after a fixed sleep (#280)
  // didn't reliably include a chart behind its own data fetch. settled()
  // (#259) now waits for the page's fetches and on-demand scripts (Chart.js)
  // anyway; the Most Played wait stays as the explicit claim. Without the
  // env var only settled() runs, so the suite doesn't hard-depend on the mock.
  test(`home tab has no WCAG A/AA violations (${theme})`, async ({ page }) => {
    await trackFetches(page);
    await page.goto("/#/home");
    await setTheme(page, variant);
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
    await settled(page);
    const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);
  });

  for (const view of AUTH_VIEWS) {
    test(`${view} tab has no WCAG A/AA violations (${theme})`, async ({ page }) => {
      await trackFetches(page);
      await page.goto("/#/home");
      await setTheme(page, variant);
      // SPA navigation — goToTab() flows through applyHistoryState like a real
      // click; hidden tabs are display:none, which axe skips.
      await page.evaluate((v) => window.Alpine.$data(document.querySelector("[x-data]")).goToTab(v), view);
      // Let the tab's widgets / lazily-loaded charts / covers settle.
      await settled(page);
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
    await trackFetches(page);
    await page.goto("/#/home");
    await setTheme(page, variant);
    // Two separate evaluate() calls, not one: the seeded array raced a
    // loadPlaylists() response and lost, clobbering the seeded row back to
    // [] a few hundred ms later (caught via the failing click's
    // page-snapshot still showing the "No synced playlists yet" empty
    // state).
    //
    // The race is real; an earlier version of this comment named the wrong
    // cause and is corrected here (#40). goToTab('playlists') does NOT
    // issue a fetch of its own: it calls applyHistoryState({tab:
    // 'playlists', selectedArtist: null, ...}), which reaches loadAlbumsFor
    // only when selectedArtist is set and loadSuggestions only when the tab
    // is 'lastfm'. Neither fires here and nothing on that path reaches
    // loadPlaylists().
    //
    // What this ordering actually waits out is init()'s OWN start-up load,
    // which init() starts without awaiting -- the same in-flight response
    // that gotoPlaylists() in playlists.spec.js counts rather than sleeps
    // through. Naming the wrong mechanism mattered because the fix for the
    // real one is different: you cannot avoid a fetch that was never
    // issued, you can only wait for the one that was.
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).goToTab("playlists");
    });
    await settled(page);
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
    await settled(page);
    let results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);

    // Open the picker too — it's a distinct piece of DOM (greyed/checked
    // rows, indigo "not yet on" rows, and the per-row error line) that the
    // closed-row scan above never renders.
    await page.getByRole("button", { name: "Actions" }).first().click();
    await page.getByRole("menuitem", { name: "Mirror…" }).first().click();
    await settled(page);
    results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);
  });

  // #508: the "duel the bard" Easter egg. The closed mark button is
  // already covered by the plain "about tab" scan in AUTH_VIEWS above —
  // this exercises the modal itself in both states the closed-tab scan
  // never renders: this harness's real (empty) library response, and a
  // guess/reveal round (state injected directly, same reasoning as
  // quiz.spec.js — there's no way to get a real {available: true} pair out
  // of an empty seeded library).
  test(`duel-the-bard quiz has no WCAG A/AA violations (${theme})`, async ({ page }) => {
    await trackFetches(page);
    await page.goto("/#/home");
    await setTheme(page, variant);
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).goToTab("about");
    });
    await settled(page);
    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).openQuiz();
    });
    await settled(page);
    let results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);

    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.quiz.available = true;
      app.quiz.loading = false;
      app.quiz.a = { artist: "Small Artist", album_count: 3 };
      app.quiz.b = { artist: "Big Artist", album_count: 20 };
    });
    await settled(page);
    results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);

    // The reveal state (post-guess) is its own distinct DOM — the album
    // counts and win/lose text only render once quiz.guess is set.
    await page.getByRole("button", { name: "Small Artist" }).click();
    await settled(page);
    results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(summarize(results.violations)).toEqual([]);
  });

  // Profile: each sub-tab, one test each per theme. Then the Devices tab and
  // the Add Device modal in each of its two branches (enrollment vs.
  // direct-token, picked by device type). global-setup doesn't seed any
  // device.
  for (const sub of PROFILE_SUBTABS) {
    test(`profile ${sub} has no WCAG A/AA violations (${theme})`, async ({ page }) => {
      await trackFetches(page);
      await page.goto("/#/home");
      await setTheme(page, variant);
      await page.evaluate(async (s) => {
        const d = window.Alpine.$data(document.querySelector("[x-data]"));
        d.goToTab("profile");
        d.setSubTab("profile", s);
      }, sub);
      await settled(page);
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      expect(results.violations.map((v) => `${sub}: ${v.id} x${v.nodes.length}`)).toEqual([]);
    });
  }
  // The Devices tab's per-device cards need at least one device to render,
  // so this creates one through the UI's own API and reloads the list
  // (loadDevices() isn't run by navigation, and the poll is every 20 s).
  test(`devices tab has no WCAG A/AA violations (${theme})`, async ({ page }) => {
    await trackFetches(page);
    await page.goto("/#/home");
    await setTheme(page, variant);
    await page.request.post("/api/devices", { data: { name: "a11y-test-device" } });
    await page.evaluate(async () => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      d.goToTab("devices");
      await d.loadDevices();
    });
    await settled(page);
    const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
    expect(results.violations.map((v) => `devices: ${v.id} x${v.nodes.length}`)).toEqual([]);
  });
  for (const deviceType of ["phone", "folder"]) {
    test(`add-device modal (${deviceType}) has no WCAG A/AA violations (${theme})`, async ({ page }) => {
      await trackFetches(page);
      await page.goto("/#/home");
      await setTheme(page, variant);
      await page.evaluate((dt) => {
        const d = window.Alpine.$data(document.querySelector("[x-data]"));
        d.goToTab("profile");
        d.openAddDeviceModal();
        d.newDevice.device_type = dt;
      }, deviceType);
      await settled(page);
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      expect(results.violations.map((v) => `add-device-modal/${deviceType}: ${v.id} x${v.nodes.length}`)).toEqual([]);
    });
  }

  // Administration: every sub-tab, and every per-provider config form, one
  // test each per theme, so no single test carries the whole tab's scans
  // against one timeout. Switching adminConfig.provider is a display-only
  // Alpine state change (not saved), so no server state is touched.
  for (const [sub, prov] of ADMIN_PAGES) {
    const where = `${sub}${prov ? "/" + prov : ""}`;
    test(`admin ${where} has no WCAG A/AA violations (${theme})`, async ({ page }) => {
      await trackFetches(page);
      await page.goto("/#/home");
      await setTheme(page, variant);
      await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).goToTab("admin"));
      await page.evaluate(async ([s, p]) => {
        const d = window.Alpine.$data(document.querySelector("[x-data]"));
        d.setSubTab("admin", s);
        // Configuration is grouped into collapsible sections; open them all
        // so the scan covers every card, as it did before the grouping.
        if (s === "config") d.setAllSections('config', true);
        if (s === "users") d.setAllSections('users', true);
        if (p) d.adminConfig.provider = p;
        // #266: health's issue-count chart lives behind x-if="health",
        // which stays null until "Check" is clicked — without this, the
        // scan below would never actually render the chart's canvas at
        // all (same "at least one to actually render" reasoning as the
        // profile tab test's device fixture below).
        if (s === "health" && !d.health) await d.loadHealth();
      }, [sub, prov]);
      await settled(page);
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      expect(results.violations.map((v) => `${where}: ${v.id} x${v.nodes.length}`)).toEqual([]);
    });
  }
}
