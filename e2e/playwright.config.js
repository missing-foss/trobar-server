// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const path = require("path");
const { defineConfig } = require("@playwright/test");

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
});
