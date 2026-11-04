// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const path = require("path");
const { defineConfig } = require("@playwright/test");

const BASKET = /(staging-basket|cover-grid)\.spec\.js$/;
const ADMIN_CONFIG = /(admin-config-sections|admin-provider-switch|proxy-headers)\.spec\.js$/;

// Runs against an already-running trobar-server instance (started by CI or
// by hand against dev/docker-compose.yaml) — this suite doesn't start the
// server itself, see e2e/README.md.
module.exports = defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.TROBAR_BASE_URL || "http://localhost:5000",
    // Relative paths here resolve against process cwd, not this file's
    // directory, when Playwright is invoked with --config from elsewhere
    // (e.g. the repo root) — make it explicit so it's correct either way.
    storageState: path.join(__dirname, ".auth", "state.json"),
    trace: "retain-on-failure",
  },
  globalSetup: require.resolve("./global-setup.js"),
  // staging-basket and cover-grid both stage into the e2e user's one
  // server-side basket, so a stage from one landing mid-test in the other
  // breaks its counts. Their own project runs them one test at a time, never
  // alongside each other; every other file runs in parallel as before.
  //
  // The admin-config files share the server's one admin config the same way:
  // the admin form's Save writes the whole form as the page loaded it, so a
  // save in admin-config-sections landing between proxy-headers' credential
  // PUT and its request undoes the PUT. They need the real server state, so
  // they run one at a time too.
  projects: [
    { name: "basket", testMatch: BASKET, workers: 1 },
    { name: "admin-config", testMatch: ADMIN_CONFIG, workers: 1 },
    { name: "rest", testIgnore: [BASKET, ADMIN_CONFIG] },
  ],
});
