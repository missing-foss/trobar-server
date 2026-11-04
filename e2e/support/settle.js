// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check

// Waiting for the page to finish what it started, instead of a fixed
// waitForTimeout() that is too short on a slow CI runner and wasted time on a
// fast one (#259).
//
// trackFetches() must run before the test's FIRST page.goto(): it is an init
// script, and a later goto() that only changes the hash ("/" then "/#/library")
// is not a new document, so the script would never run. It wraps window.fetch so the
// page counts its own requests until each response body has been read. A tab
// opened with goToTab() fires its loads through fetch, and a test that then
// overwrites Alpine state has to wait for those loads, or a late response
// replaces what the test set.

/** @param {import("@playwright/test").Page} page */
async function trackFetches(page) {
  await page.addInitScript(() => {
    const pending = new Map();
    let next = 0;
    const realFetch = window.fetch.bind(window);
    // @ts-ignore -- test hooks, read by settled() below
    window.__e2eInflight = () => pending.size;
    // @ts-ignore
    window.__e2ePending = () => [...pending.values()];
    window.fetch = async (...args) => {
      const id = next++;
      const input = args[0];
      pending.set(id, typeof input === "string" ? input : (input && input.url) || String(input));
      let response;
      try {
        response = await realFetch(...args);
      } catch (e) {
        pending.delete(id);
        throw e;
      }
      // Counted until the body is read too: the app reads it next.
      response.clone().arrayBuffer().catch(() => {}).finally(() => { pending.delete(id); });
      return response;
    };
  });
}

/**
 * Resolves once no fetch is in flight and every script the page loaded on
 * demand (window.loadScript, e.g. Chart.js behind a chart) has loaded, then
 * lets one macrotask pass so the code that awaited them has set its state and
 * Alpine has rendered it. Repeats while that settling started more work.
 * @param {import("@playwright/test").Page} page
 */
async function settled(page) {
  for (let round = 0; round < 5; round++) {
    try {
      // @ts-ignore -- set by trackFetches()
      await page.waitForFunction(() => typeof window.__e2eInflight === "function" && window.__e2eInflight() === 0,
        null, { timeout: 10_000 });
    } catch (e) {
      // @ts-ignore
      const pending = await page.evaluate(() => (window.__e2ePending ? window.__e2ePending() : "no tracker"));
      throw new Error(`requests still in flight after 10 s: ${JSON.stringify(pending)}`);
    }
    // @ts-ignore -- the app's own lazy-script registry (index.html, loadScript)
    const scripts = await page.evaluate(() => Object.keys(window._lazyScripts || {}).length);
    await page.evaluate(() =>
      // @ts-ignore
      Promise.allSettled(Object.values(window._lazyScripts || {}))
        .then(() => new Promise((resolve) => setTimeout(resolve, 0))));
    const stable = await page.evaluate((n) =>
      // @ts-ignore
      window.__e2eInflight() === 0 && Object.keys(window._lazyScripts || {}).length === n, scripts);
    if (stable) return;
  }
  throw new Error("the page kept starting new loads for 5 rounds");
}

module.exports = { trackFetches, settled };
