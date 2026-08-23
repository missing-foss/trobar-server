// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// #304: guards for the cover-grid extraction, and for the #284 widget-ordering
// property it has to preserve.
//
// Why these are e2e and not unit tests: index.html is an Alpine SPA with no
// unit-level coverage, and the properties at risk here are *rendered* ones —
// DOM order, which element carries the grid span, and whether a click reaches
// the store. Static checks can't see any of it.
//
// The ordering assertion is NEW, not a port. #284 restructured these same
// widgets (hand-authored blocks -> x-for + x-if + display:contents) and verified
// DOM order by hand, describing the procedure in its commit message but leaving
// no committed test. So this codifies a property that has been unguarded since.

const HOME_WIDGET_IDS = ["library", "devices", "suggestions", "recently_added",
  "recently_released", "most_played", "administration"];

/** Read state off the single root Alpine component (same handle the other specs use). */
function appData(page, fn, arg) {
  return page.evaluate(
    ([body, a]) => {
      // eslint-disable-next-line no-new-func
      const f = new Function("app", "arg", body);
      return f(window.Alpine.$data(document.querySelector("[x-data]")), a);
    },
    [fn, arg ?? null],
  );
}

async function gotoHome(page) {
  await page.goto("/#/home");
  await page.evaluate(() =>
    window.Alpine.$data(document.querySelector("[x-data]")).goToTab("home"));
  await page.waitForTimeout(800); // widgets + lazily-loaded covers settle
}

test.describe("home dashboard cover grids (#304)", () => {
  test("widget DOM order matches orderedVisibleWidgetIds (#284)", async ({ page }) => {
    // The property #284 established and nothing has guarded since: the rendered
    // order must equal the model's order. A CSS-order trick would satisfy the
    // eye and break keyboard/screen-reader order, which is the bug #284 fixed.
    await gotoHome(page);
    const expectedIds = await appData(page, "return app.orderedVisibleWidgetIds();");
    expect(expectedIds.length).toBeGreaterThan(0);

    const headings = await page.$$eval(
      "section:not([style*='display: none']) h2",
      (els) => els.map((e) => e.textContent.trim()).filter(Boolean));

    // Map ids -> their rendered headings via the catalog, so this compares the
    // model's ORDER rather than hard-coded English strings.
    const titles = await appData(page,
      "return app.orderedWidgetCatalog().reduce((m, w) => (m[w.id] = w.label, m), {});");
    const expectedTitles = expectedIds
      .map((id) => titles[id])
      .filter((label) => label && headings.includes(label));

    const renderedInOrder = headings.filter((h) => expectedTitles.includes(h));
    expect(renderedInOrder).toEqual(expectedTitles);
  });

  test("the grid span stays on the direct child of the display:contents wrapper",
    async ({ page }) => {
      // #284's wrapper is display:contents so each widget is itself the grid
      // item. An extra wrapper inside a coverGrid component would silently break
      // md:col-span-2 — the layout regression this refactor is most likely to
      // cause, and one that passing tests would never show.
      await gotoHome(page);
      const bad = await page.$$eval(".contents", (wrappers) =>
        wrappers.flatMap((w) =>
          Array.from(w.children)
            .filter((child) => !child.classList.contains("md:col-span-2")
              && child.querySelector(".md\\:col-span-2"))
            .map((child) => child.outerHTML.slice(0, 200))));
      expect(bad).toEqual([]);
    });

  test("recently_added selection flows through Alpine.store('selection')",
    async ({ page }) => {
      await gotoHome(page);
      const hasItems = await appData(page, "return (app.recentlyAdded || []).length > 0;");
      test.skip(!hasItems, "no recently-added albums in this fixture library");

      const bucket = () => page.evaluate(() =>
        [...window.Alpine.store("selection").buckets.recentlyAdded]);

      expect(await bucket()).toEqual([]);

      // Click the first card in the Recently added grid.
      const heading = page.locator("h2", { hasText: /recently added/i }).first();
      const widget = heading.locator("xpath=ancestor::div[contains(@class,'bg-panel')][1]");
      await widget.locator("button.group").first().click();

      const afterClick = await bucket();
      expect(afterClick).toHaveLength(1);
      expect(afterClick[0]).toContain("||"); // the artist||album key shape

      // The checkmark badge is the visible consequence of store state.
      await expect(widget.locator("button.group").first().locator("span.bg-indigo-600"))
        .toBeVisible();

      // Clearing empties the bucket. Located by ACCESSIBLE NAME, not hasText:
      // the label sits on its own line in the template, so a /^Cancel$/ regex
      // never matches the untrimmed text content.
      await widget.getByRole("button", { name: /cancel/i }).first().click();
      expect(await bucket()).toEqual([]);
    });

  test("all three dashboard grids are on the store, and only they are",
    async ({ page }) => {
      // #304 is complete for the dashboard once these three are present. The
      // Library grid is deliberately NOT here — its x-model binding needs its own
      // thought (see the issue), so selectedAlbums stays on the root for now.
      await gotoHome(page);
      const buckets = await page.evaluate(() =>
        Object.keys(window.Alpine.store("selection").buckets).sort());
      expect(buckets).toEqual(["recentlyAdded", "recentlyReleased", "suggestions"]);
      // Named explicitly rather than enumerated: Object.keys() on Alpine's merge
      // proxy does not report the root scope's own keys, so a filter over it
      // silently returns nothing and the assertion passes for the wrong reason.
      const rootState = await appData(page,
        "return {albumsStillHere: Array.isArray(app.selectedAlbums),"
        + " suggestionsGone: app.selectedSuggestions === undefined,"
        + " recentlyAddedGone: app.selectedRecentlyAdded === undefined,"
        + " recentlyReleasedGone: app.selectedRecentlyReleased === undefined};");
      expect(rootState).toEqual({
        albumsStillHere: true, suggestionsGone: true,
        recentlyAddedGone: true, recentlyReleasedGone: true,
      });
    });

  test("every converted grid is actually wired to a coverGrid component",
    async ({ page }) => {
      // Found by mutation testing THIS spec: deleting x-data="coverGrid(...)"
      // from the recently_released widget passed all ten other tests. The widget
      // then resolves selectedCount/isSelected up to the root scope, where they
      // don't exist — Alpine logs an expression error and renders a dead grid,
      // which nothing else here notices. So assert the wiring directly.
      await gotoHome(page);
      const perBucket = await page.evaluate(() =>
        Array.from(document.querySelectorAll("[x-data]"))
          .map((el) => window.Alpine.$data(el))
          .filter((d) => d && typeof d.bucket === "string")
          .reduce((acc, d) => (acc[d.bucket] = (acc[d.bucket] || 0) + 1, acc), {}));
      // suggestions twice: the home widget and the Suggestions tab section.
      expect(perBucket).toEqual({
        recentlyAdded: 1, recentlyReleased: 1, suggestions: 2,
      });
    });

  test("the store keeps buckets separate — no cross-surface bleed yet",
    async ({ page }) => {
      // #303 is what makes selection accumulate across surfaces. Until then a
      // pick in Recently added must NOT appear anywhere else; this pins that so
      // the change is a deliberate one when it happens.
      await gotoHome(page);
      await page.evaluate(() =>
        window.Alpine.store("selection").toggle("recentlyAdded", "A||B"));
      const leaked = await appData(page,
        "return [app.selectedAlbums.length,"
        + " Alpine.store('selection').buckets.suggestions.length,"
        + " Alpine.store('selection').buckets.recentlyReleased.length];");
      expect(leaked).toEqual([0, 0, 0]);
    });

  test("a completed multi-album sync clears every selection surface (#323)",
    async ({ page }) => {
      // The bug: the picker's confirm handler enumerated the buckets and
      // cleared only selectedAlbums + selectedSuggestions, so Recently
      // Added / Released stayed selected after a successful sync and the
      // next pick silently re-sent them.
      await gotoHome(page);
      await page.evaluate(() => {
        window.Alpine.store("selection").toggle("recentlyAdded", "A||B");
        const app = window.Alpine.$data(document.querySelector("[x-data]"));
        window.Alpine.store("selection").toggle("suggestions", "E||F");
        window.Alpine.store("selection").toggle("recentlyReleased", "G||H");
        app.selectedAlbums = ["C||D"];
      });

      // Drive the real code path against a real device. #497 made the
      // picker's own send action check the fan-out response's own
      // success/failure (it used to clear selection state unconditionally,
      // even on a failed POST) — so unlike before, this needs a real
      // device and a real 200 for the clearing under test to actually run.
      const created = await page.request.post("/api/devices", {
        data: { name: "cover-grid-test-device" },
      });
      expect(created.ok()).toBeTruthy();
      const device = await created.json();
      await page.evaluate(async (deviceId) => {
        const app = window.Alpine.$data(document.querySelector("[x-data]"));
        // #497: a multi-select batch is now "picker.target is an array",
        // not a separate 'album_batch' type the server never knew about.
        // #501: confirmDevicePicker() split into addAndKeepBrowsing() /
        // addAndSendNow() — this test needs the actual send, since it's
        // the send path's success that triggers the clearing under test.
        app.picker = { open: true, type: "album", target: ["A||B"], deviceIds: [deviceId] };
        await app.addAndSendNow();
      }, device.id);

      const left = await page.evaluate(() => {
        const app = window.Alpine.$data(document.querySelector("[x-data]"));
        return {
          recentlyAdded: window.Alpine.store("selection").buckets.recentlyAdded.length,
          suggestions: window.Alpine.store("selection").buckets.suggestions.length,
          recentlyReleased: window.Alpine.store("selection").buckets.recentlyReleased.length,
          albums: app.selectedAlbums.length,
        };
      });
      expect(left).toEqual({
        recentlyAdded: 0, albums: 0, suggestions: 0, recentlyReleased: 0,
      });
    });

  test("the home Suggestions widget and the Suggestions tab share one bucket (#304)",
    async ({ page }) => {
      // The property most at risk in PR 2. Both surfaces used the SAME
      // selectedSuggestions array, so a pick on the dashboard already appeared on
      // the tab. Converting one surface and not the other would have silently
      // split them — which is why the migration unit is a bucket, not a widget.
      await gotoHome(page);
      await page.evaluate(() =>
        window.Alpine.store("selection").toggle("suggestions", "Artist 1||Album 1"));

      // Both surfaces read the same store bucket, so the count each renders must
      // agree — checked through the component's own accessor, not the raw store.
      const counts = await page.evaluate(() =>
        Array.from(document.querySelectorAll("[x-data]"))
          .map((el) => window.Alpine.$data(el))
          .filter((d) => d && d.bucket === "suggestions")
          .map((d) => d.selectedCount));
      expect(counts.length).toBe(2); // the home widget and the tab section
      expect(counts).toEqual([1, 1]);
    });

  test("every img with @error also has @load, in the extracted grid too",
    async ({ page }) => {
      // #30's invariant, re-checked here because the extraction rewrites exactly
      // these attributes. image-error-recovery.spec.js asserts it app-wide; this
      // keeps the failure attributable to this widget.
      await gotoHome(page);
      const missing = await page.evaluate(() =>
        Array.from(document.querySelectorAll("img[\\@error]"))
          .filter((img) => !img.hasAttribute("@load"))
          .map((img) => img.outerHTML.slice(0, 160)));
      expect(missing).toEqual([]);
    });
});

test.describe("background jobs panel (#333)", () => {
  /** Insert a job row directly, so the panel can be driven into a given state. */
  async function seedJob(page, {type, state, attempts, lastError, progress}) {
    return page.evaluate(async (j) => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      // No API for planting a row; the panel reads /api/admin/jobs, so stub the
      // fetch it makes rather than reaching into the database from a browser.
      app.jobs = {counts: {running: 0, queued: 0, done: 0, failed: 0}, jobs: [j]};
    }, {id: 1, type, state, attempts, last_error: lastError, result: null,
        progress: progress || null, created_at: "2026-07-26 12:00:00",
        started_at: null, finished_at: null});
  }

  async function gotoJobs(page) {
    // Sub-tabs go through setSubTab('admin', …), not a bare property — subTab()
    // reads this.subTabs with a per-group default ('config' for admin). Setting a
    // made-up property left the panel in the DOM but hidden, which is exactly how
    // the first run of these tests failed: the assertions found the right element
    // with the right text and it simply wasn't visible.
    await page.goto("/#/admin");
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.goToTab("admin");
      app.setSubTab("admin", "jobs");
      app.jobsLoading = false;
    });
    await page.waitForTimeout(400);
  }

  test("a FAILED job shows its error in red", async ({ page }) => {
    await gotoJobs(page);
    await seedJob(page, {type: "library_scan", state: "failed", attempts: 3,
                         lastError: "the server died 3 times while this job was running"});
    await page.waitForTimeout(200);
    const red = page.locator(".text-red-600", {hasText: /the server died 3 times/});
    await expect(red).toBeVisible();
  });

  test("a RETRIED job does not present the old error as a current failure",
    async ({ page }) => {
      // The bug: Retry resets state/attempts but keeps last_error, and the panel
      // rendered it red regardless — so a working retry looked broken, and with
      // #329's old wording it read as "the server is still crashing".
      await gotoJobs(page);
      await seedJob(page, {type: "library_scan", state: "queued", attempts: 0,
                           lastError: "the server died 3 times while this job was running"});
      await page.waitForTimeout(200);
      // toBeHidden, NOT toHaveCount(0): Alpine's x-show sets display:none, so the
      // element stays in the DOM. Counting matches would always find it and the
      // assertion would fail even with the fix in place.
      const red = page.locator(".text-red-600", {hasText: /the server died 3 times/});
      await expect(red).toBeHidden();
      // Still shown, but as history rather than a live failure.
      await expect(page.locator("text=/previous attempt/i")).toBeVisible();
    });

  test("a count-only progress reports its unit, not a bare number",
    async ({ page }) => {
      // fingerprint_backfill reports `checked` with no denominator, which rendered
      // as a lone "3400" beside a bar with no percentage.
      await gotoJobs(page);
      await seedJob(page, {type: "fingerprint_backfill", state: "running", attempts: 1,
                           lastError: null, progress: {done: 3400}});
      await page.waitForTimeout(200);
      // #357: the same text also lives in a visually-hidden aria-live
      // announcement span now, so a bare text locator matches both —
      // scope to the visible one via the progressbar it's inside.
      await expect(page.getByRole("progressbar").getByText(/3,400 checked/)).toBeVisible();
    });

  test("a running job's progressbar has no WCAG A/AA violations (#357)",
    async ({ page }) => {
      // The #357 accessibility fix shipped a Serious-impact gap anyway
      // (aria-progressbar-name) because role="progressbar" only exists
      // while x-show="j.progress" is true, and every other axe run in this
      // suite scans an otherwise-idle instance — the element the rule needs
      // to see is never there. This is the fix for THAT blind spot: seed a
      // running job so the progressbar actually renders, then scan.
      await gotoJobs(page);
      await seedJob(page, {type: "library_scan", state: "running", attempts: 0,
                           lastError: null, progress: {done: 7, total: 15}});
      await page.waitForTimeout(200);
      await expect(page.getByRole("progressbar")).toBeVisible();
      const results = await new AxeBuilder({page}).withTags(
        ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations).toEqual([]);
    });
});

test.describe("Library tab scan progress (#317/#357)", () => {
  test("a running scan's progressbar has no WCAG A/AA violations",
    async ({ page }) => {
      // Same blind spot as the admin panel's version, same fix: the
      // progressbar only exists while x-show="scanProgress" is truthy, so
      // an idle-instance axe scan (every other run in this suite) never
      // reaches it.
      await page.goto("/#/library");
      await page.evaluate(() => {
        const app = window.Alpine.$data(document.querySelector("[x-data]"));
        app.goToTab("library");
        app.scanProgress = {done: 7, total: 15};
      });
      await page.waitForTimeout(200);
      await expect(page.getByRole("progressbar")).toBeVisible();
      const results = await new AxeBuilder({page}).withTags(
        ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations).toEqual([]);
    });
});

test.describe("Library health panel (#364/#365)", () => {
  /** Stub app.health directly — same "no API for planting state, stub what
   * the panel reads" approach as the jobs panel's seedJob above. Every
   * category needs a full {count, items} shape since the templates read
   * health[cat.k].count/.items unconditionally once health is non-null. */
  async function seedHealth(page, overrides) {
    return page.evaluate((extra) => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.health = Object.assign({
        unmatched_playlist_tracks: {count: 0, items: []},
        unknown_tags: {count: 0, items: []},
        duplicates: {count: 0, items: []},
        fingerprint_failed: {count: 0, items: []},
        unidentified_fingerprints: {count: 0, items: []},
        item_limit: 200,
        data_dir_network_fs: null,
        last_scan_finished_at: null,
        exposure_warning: null,
        exposure_peer_count: null,
      }, extra);
    }, overrides);
  }

  async function gotoHealth(page) {
    await page.goto("/#/admin");
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.goToTab("admin");
      app.setSubTab("admin", "health");
    });
    await page.waitForTimeout(200);
  }

  test("the DATA_DIR network-filesystem alert renders when flagged", async ({page}) => {
    await gotoHealth(page);
    await seedHealth(page, {data_dir_network_fs: "nfs4"});
    await page.waitForTimeout(200);
    await expect(page.getByText(/DATA_DIR is on a network filesystem \(nfs4\)/)).toBeVisible();
  });

  test("the exposure-warning alert renders when flagged (#389)", async ({page}) => {
    await gotoHealth(page);
    await seedHealth(page, {exposure_warning: 8});
    await page.waitForTimeout(200);
    await expect(page.getByText(/reached directly by 8 different addresses/)).toBeVisible();
  });

  test("the exposure peer count shows even below the alert threshold (#389)", async ({page}) => {
    // Review feedback: a signal that only ever speaks up past its
    // threshold looks identical to one that's silently seeing nothing —
    // this neutral line is shown whenever the mechanism is active at all.
    await gotoHealth(page);
    await seedHealth(page, {exposure_peer_count: 2});
    await page.waitForTimeout(200);
    await expect(page.getByText(/2 distinct addresses have reached this instance/)).toBeVisible();
  });

  test("population B renders as informational, not a problem pill", async ({page}) => {
    // #364: population B (unidentified_fingerprints) must not use the same
    // yellow "problem" styling as the real categories — it's not a fault.
    await gotoHealth(page);
    await seedHealth(page, {unidentified_fingerprints: {
      count: 1, items: [{artist: "Obscure Artist", title: "Unreleased Jam",
                          album: "Live Bootleg", relative_path: "Obscure/Bootleg.flac"}],
    }});
    await page.waitForTimeout(200);
    const pill = page.locator("span", {hasText: /^1$/}).last();
    await expect(pill).not.toHaveClass(/bg-yellow-100/);
  });

  test("the DATA_DIR alert and the populated panel have no WCAG A/AA violations",
    async ({page}) => {
      // #365's alert and #364's two new categories only exist behind
      // x-if="health" / x-if="health && health.data_dir_network_fs" —
      // absent from every other axe run in this suite (idle instance, #357's
      // progressbar hit the same gap), so this seeds all of it before scanning.
      // #389's exposure_warning alert is the same shape, seeded here too.
      await gotoHealth(page);
      await seedHealth(page, {
        data_dir_network_fs: "nfs4",
        exposure_warning: 8,
        exposure_peer_count: 8,
        fingerprint_failed: {count: 1, items: [
          {artist: "Some Artist", title: "Broken Track", album: "Some Album",
           relative_path: "Broken/File.flac"}]},
        unidentified_fingerprints: {count: 1, items: [
          {artist: "Obscure Artist", title: "Unreleased Jam", album: "Live Bootleg",
           relative_path: "Obscure/Bootleg.flac"}]},
        last_scan_finished_at: "2026-07-26 12:00:00",
      });
      await page.waitForTimeout(200);
      await expect(page.getByText(/DATA_DIR is on a network filesystem/)).toBeVisible();
      await expect(page.getByText(/reached directly by 8 different addresses/)).toBeVisible();
      await expect(page.getByText(/8 distinct addresses have reached this instance/)).toBeVisible();
      const results = await new AxeBuilder({page}).withTags(
        ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations).toEqual([]);
    });
});

test.describe("Suggestions empty state (#324)", () => {
  test("the empty state tracks the rendered list, not the raw source",
    async ({ page }) => {
      // The widget renders homeSuggestions(coverLimit()) but used to test
      // `suggestions` for emptiness. When the two disagree you get neither cards
      // nor a message — a bare heading over blank space, with no clue whether it
      // is loading, broken, or genuinely empty.
      //
      // Unreachable through the UI today (coverLimit() is clamped to 15/30/45/60,
      // never 0, so mixSuggestions cannot return empty from a non-empty list), so
      // the disagreement is forced directly.
      await gotoHome(page);
      await page.evaluate(() => {
        const app = window.Alpine.$data(document.querySelector("[x-data]"));
        app.suggestions = [{artist: "A", album: "B", source: "recent",
                            library_artist: "A", library_album: "B"}];
        app.homeSuggestions = () => [];   // a source that mixes to nothing
      });
      await page.waitForTimeout(300);
      await expect(page.locator("p", {hasText: /No suggestions yet/i}).first())
        .toBeVisible();
    });

  test("the empty state is hidden when there are cards to show", async ({ page }) => {
    await gotoHome(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.suggestions = [{artist: "A", album: "B", source: "recent",
                          library_artist: "A", library_album: "B"}];
    });
    await page.waitForTimeout(300);
    await expect(page.locator("p", {hasText: /No suggestions yet/i}).first())
      .toBeHidden();
  });
});

test.describe("coverGrid registration (#304)", () => {
  test("the store and component are registered before Alpine initialises",
    async ({ page }) => {
      // The failure mode worth guarding: Alpine is loaded `defer`, so the
      // alpine:init listener has to live in a NON-deferred inline script. Get
      // that wrong and there's no error at all — the widget just never binds.
      await gotoHome(page);
      const registered = await page.evaluate(() => ({
        store: !!window.Alpine.store("selection"),
        buckets: Object.keys(window.Alpine.store("selection").buckets),
      }));
      expect(registered.store).toBe(true);
      expect(registered.buckets).toContain("recentlyAdded");
    });

  test("the widget ids the home loop knows about are unchanged", async ({ page }) => {
    await gotoHome(page);
    const ids = await appData(page,
      "return app.orderedWidgetCatalog().map((w) => w.id).sort();");
    expect(ids).toEqual([...HOME_WIDGET_IDS].sort());
  });
});

test.describe("Library list view (#415)", () => {
  // The basket is server-side and per-user — Playwright isolates browser
  // contexts, not server state. This test adds an item and never removes
  // it, which staging-basket.spec.js's own tests (run against the same
  // account) would otherwise inherit as an off-by-one in their own count
  // assertions. Unconditional (afterEach, not end-of-test cleanup) so a
  // failed assertion above still doesn't leak the item.
  test.afterEach(async ({ page }) => {
    await page.request.delete("/api/basket");
  });

  test("an album row's one button opens the picker and Add & keep browsing stages it (#415/#501)", async ({ page }) => {
    // #415: this was the one openDevicePicker() trigger in the app with no
    // basket equivalent — every other surface got one from #348. #501 later
    // collapsed the separate "Select" + "Add to basket" pair every OTHER
    // surface had into this same one-button shape, so this row now matches
    // them instead of being the odd one out. The seeded e2e library is
    // empty (see e2e/README.md), so there's no real artist to click through
    // the Library tab's own artist list; seeding selectedArtist/albums/
    // cover_view_mode directly is the same established pattern the other
    // Library-adjacent tests in this file use for selectedAlbums, and
    // what's under test here is the row's own markup, not the fetch that
    // normally populates it.
    // Two settled steps, not one — matching gotoHome()'s own pattern below.
    // goToTab() itself resets selectedArtist to null (by design: ordinary
    // tab navigation always lands on the artist list, never a stale
    // selection). The page's own hash-routing also drives tab state
    // asynchronously on initial load; setting the test's own
    // selectedArtist/albums in the SAME evaluate as the goto risked losing
    // the race against that and being overwritten back to null a moment
    // later — caught by seeing "Choose an artist on the left." still
    // rendered at assertion time before this split fixed it.
    const created = await page.request.post("/api/devices", {
      data: { name: "list-view-row-device" },
    });
    const device = await created.json();

    await page.goto("/#/library");
    await page.evaluate(() =>
      window.Alpine.$data(document.querySelector("[x-data]")).goToTab("library"));
    await page.waitForTimeout(400);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.selectedArtist = "E2E List View Artist";
      app.libraryView = "albums";
      app.albums = [{album: "E2E List View Album", track_count: 8, year: 2020}];
      app.profile.cover_view_mode = "list";
    });
    await page.waitForTimeout(200);

    // "Sync to…" appears twice: the artist header's own more specific
    // "Sync entire artist" label is different text, so only the album
    // row's button reads "Sync to…" — no further scoping needed.
    const rowButton = page.getByRole("button", { name: "Sync to…" });
    await expect(rowButton).toBeVisible();
    await rowButton.click();

    await page.getByRole("checkbox", { name: "list-view-row-device" }).check();
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/basket") && r.request().method() === "POST"),
      page.getByRole("button", { name: "Add & keep browsing" }).click(),
    ]);

    await expect(page.locator('[title="Basket"]')).toHaveText("1");

    const basketResp = await page.request.get("/api/basket");
    const items = await basketResp.json();
    const item = items.find((i) => i.type === "album" && i.target === "E2E List View Artist||E2E List View Album");
    expect(item).toBeTruthy();
    expect(item.device_ids).toContain(device.id);
  });
});

test.describe("basket indicator on single-item Add to basket buttons (#416 part 3)", () => {
  // Same reasoning as the #415 block above: the basket is server-side and
  // per-user, so anything added here must be cleaned up unconditionally.
  test.afterEach(async ({ page }) => {
    await page.request.delete("/api/basket");
  });

  test("a button switches to In basket once its own (type, target) is staged, not any basket activity", async ({ page }) => {
    // Same seeding approach as the #415/#501 test above — see its comment
    // for why the two evaluate() calls are split (a race against the
    // page's own hash-routing init) and why direct state injection is
    // legitimate here (what's under test is has()-driven button state,
    // not the fetch that normally populates selectedArtist/albums).
    const created = await page.request.post("/api/devices", {
      data: { name: "indicator-test-device" },
    });
    const device = await created.json();

    await page.goto("/#/library");
    await page.evaluate(() =>
      window.Alpine.$data(document.querySelector("[x-data]")).goToTab("library"));
    await page.waitForTimeout(400);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.selectedArtist = "E2E Indicator Artist";
      app.libraryView = "albums";
      app.albums = [{ album: "E2E Indicator Album", track_count: 5, year: 2021 }];
      app.profile.cover_view_mode = "list";
    });
    await page.waitForTimeout(200);

    // #501: the header button ("Sync entire artist"/"In basket") and the
    // row button ("Sync to…"/"In basket") share "In basket" once either is
    // staged, so a per-button regex ends up matching BOTH once one of them
    // gets there. One combined query across all three possible texts (only
    // ever exactly these two buttons on the page) + stable DOM-order nth()
    // — same technique the pre-#501 version of this test used.
    const bothButtons = page.getByRole("button", { name: /Sync entire artist|Sync to…|In basket/ });
    const headerButton = bothButtons.nth(0);
    const rowButton = bothButtons.nth(1);

    await expect(headerButton).toHaveText("Sync entire artist");
    await expect(rowButton).toHaveText("Sync to…");

    // Stage the artist (the whole-artist header button) via the picker's
    // Add & keep browsing — the artist and the album are different
    // (type, target) pairs, so this must not flip the row button's own
    // indicator too.
    await headerButton.click();
    await page.getByRole("checkbox", { name: "indicator-test-device" }).check();
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/basket") && r.request().method() === "POST"),
      page.getByRole("button", { name: "Add & keep browsing" }).click(),
    ]);
    await expect(page.locator('[title="Basket"]')).toHaveText("1");
    await expect(headerButton).toHaveText("In basket");
    await expect(rowButton).toHaveText("Sync to…");

    // Now the album row — its own picker remembers the same device via
    // #303's per-surface smart default ('library'), so no re-checking.
    await rowButton.click();
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/basket") && r.request().method() === "POST"),
      page.getByRole("button", { name: "Add & keep browsing" }).click(),
    ]);
    await expect(page.locator('[title="Basket"]')).toHaveText("2");
    await expect(rowButton).toHaveText("In basket");
  });
});

test.describe("Dashboard widget dedup (#418)", () => {
  // Calling the computed getters directly (not parsing the rendered grid)
  // — same style as #303's "no cross-surface bleed" test above. What's
  // under test is the dedup LOGIC (precedence, visibility-awareness,
  // reactivity), which these functions fully determine; DOM assertions
  // would just be a more fragile way of checking the same thing.

  test("an album in both Recently released and Recently added shows only in Recently released", async ({ page }) => {
    await gotoHome(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.recentlyReleased = [{ artist: "A", album: "Shared", year: 2026 }];
      app.recentlyAdded = [{ artist: "A", album: "Shared" }, { artist: "B", album: "Solo" }];
    });
    const [added, released] = await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      return [
        app.dedupedRecentlyAdded(30).map((a) => `${a.artist}||${a.album}`),
        app.dedupedRecentlyReleased(30).map((a) => `${a.artist}||${a.album}`),
      ];
    });
    expect(added).toEqual(["B||Solo"]);
    expect(released).toEqual(["A||Shared"]);
  });

  test("an album in both Suggestions and Recently added shows only in Recently added, keyed on library identity not display spelling", async ({ page }) => {
    await gotoHome(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.recentlyAdded = [{ artist: "A", album: "Shared" }];
      // Deliberately different DISPLAY spelling from the library identity —
      // matching on artist/album (Last.fm's own text) instead of
      // library_artist/library_album would fail to dedupe this pair.
      app.suggestions = [
        { artist: "A (last.fm)", album: "Shared", source: "lastfm", library_artist: "A", library_album: "Shared" },
        { artist: "C", album: "Unique", source: "lastfm", library_artist: "C", library_album: "Unique" },
      ];
    });
    const sugg = await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      return app.homeSuggestions(30).map((s) => `${s.library_artist}||${s.library_album}`);
    });
    expect(sugg).toEqual(["C||Unique"]);
  });

  test("hiding Recently released reveals its albums in Recently added instead of dropping them from the dashboard entirely", async ({ page }) => {
    await gotoHome(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.recentlyReleased = [{ artist: "A", album: "Shared", year: 2026 }];
      app.recentlyAdded = [{ artist: "A", album: "Shared" }];
      app.profile.dashboard_widgets = { disabled: ["recently_released"] };
    });
    const added = await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      return app.dedupedRecentlyAdded(30).map((a) => `${a.artist}||${a.album}`);
    });
    expect(added).toEqual(["A||Shared"]);
  });

  test("dedupe is reactive: re-enabling a widget re-filters lower-precedence ones without a reload", async ({ page }) => {
    await gotoHome(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.recentlyReleased = [{ artist: "A", album: "Shared", year: 2026 }];
      app.recentlyAdded = [{ artist: "A", album: "Shared" }];
      app.profile.dashboard_widgets = { disabled: ["recently_released"] };
    });
    const beforeReenable = await page.evaluate(() =>
      window.Alpine.$data(document.querySelector("[x-data]")).dedupedRecentlyAdded(30).length);
    expect(beforeReenable).toBe(1);

    await page.evaluate(() => {
      window.Alpine.$data(document.querySelector("[x-data]")).profile.dashboard_widgets = { disabled: [] };
    });
    const afterReenable = await page.evaluate(() =>
      window.Alpine.$data(document.querySelector("[x-data]")).dedupedRecentlyAdded(30).length);
    expect(afterReenable).toBe(0);
  });

  test("an album past Recently released's own display cutoff does not suppress a visible copy in Recently added (review fix)", async ({ page }) => {
    // Caught in review on this PR: the exclude set was built from the FULL
    // recentlyReleased array (server default up to 60), not what's actually
    // shown after slicing to coverLimit() (15/30/45/60). An album sitting
    // past that cutoff in Recently released could still suppress a
    // genuinely visible copy of itself in Recently added — vanishing it
    // from the dashboard entirely, exactly the failure the hidden-widget
    // rule (the test above) exists to prevent, just reached a different way.
    await gotoHome(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      // 20 releases, the shared album LAST (position 19) — well past a
      // coverLimit() of 15, so Recently released itself never shows it.
      const released = [];
      for (let i = 0; i < 19; i++) released.push({ artist: "A", album: `Released ${i}`, year: 2026 });
      released.push({ artist: "Z", album: "PastTheCutoff", year: 2026 });
      app.recentlyReleased = released;
      app.recentlyAdded = [{ artist: "Z", album: "PastTheCutoff" }];
    });

    const n = 15;
    const [releasedShown, addedShown] = await page.evaluate((n) => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      return [
        app.dedupedRecentlyReleased(n).map((a) => `${a.artist}||${a.album}`),
        app.dedupedRecentlyAdded(n).map((a) => `${a.artist}||${a.album}`),
      ];
    }, n);

    expect(releasedShown).not.toContain("Z||PastTheCutoff");
    // The real assertion: not shown in Released (past its own cutoff) must
    // mean it's still shown in Added — visible SOMEWHERE, not nowhere.
    expect(addedShown).toContain("Z||PastTheCutoff");
  });
});
