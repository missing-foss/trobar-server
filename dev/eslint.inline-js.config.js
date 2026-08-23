// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// ESLint config for the JS extracted from app/templates/*.html <script>
// blocks by check_inline_js.py — not the repo's general config (there isn't
// one; e2e/ isn't linted by this). Always invoked explicitly with
// --config, never relied on for auto-discovery, so it only ever applies to
// what check_inline_js.py points it at.
const js = require("@eslint/js");
const globals = require("globals");

module.exports = [
  js.configs.recommended,
  {
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: {
        ...globals.browser,
        // Loaded via separate <script src> tags (vendored alpine-*.min.js,
        // qrcode-*.min.js, icons.js), never inline — see check_inline_js.py's
        // SCRIPT_RE, which only matches bare <script> with no src.
        Alpine: "readonly",
        QRCode: "readonly",
        ICONS: "readonly",
        Chart: "readonly",
      },
    },
    rules: {
      // Alpine calls each template's top-level functions (app(), t(),
      // setupWizard()) from x-data="..." attributes outside any <script>
      // block, invisible to a per-file analysis — so a plain "unused"
      // top-level function is expected and correct here, not a bug. Inner
      // (function-scoped) unused vars are still real findings and stay on.
      "no-unused-vars": ["error", { vars: "local" }],
    },
  },
];
