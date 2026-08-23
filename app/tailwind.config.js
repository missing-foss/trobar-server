// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Build input for the self-hosted Tailwind CSS (nothing loads from
 * a CDN). Regenerate after changing template classes — run from THIS `app/`
 * directory, since the `content` glob below is relative to the working dir
 * (running from the repo root matches no templates and purges everything —
 * ~4.7 KB of output instead of ~18 KB):
 *   cd app && /path/to/tailwindcss -c tailwind.config.js \
 *             -i tailwind-input.css -o static/css/tailwind.css --minify
 * (tailwindcss = the standalone v3 binary; no npm needed.) The output
 * app/static/css/tailwind.css is committed and served locally.
 *
 * USE v3.4.19 — the version CI's drift guard pins (#306). The committed
 * artifact has to match a build from that exact version or the guard fails,
 * and it has already flip-flopped between 3.4.17 and 3.4.19 across machines
 * from being pinned nowhere.
 *   https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.19/tailwindcss-linux-x64
 *
 * To bump it: change the version AND its sha256 in .github/workflows/ci.yml,
 * rebuild this artifact, and commit both together. Take the hash from
 * upstream's PUBLISHED sums file, not from your own download:
 *   curl -fsSL .../releases/download/vX.Y.Z/sha256sums.txt | grep linux-x64
 * Hashing the file you just fetched only proves it downloaded intact — if that
 * fetch were tampered with you'd pin the tampered hash, and CI would then
 * verify the bad binary faithfully forever. The hash has to come from a
 * different source than the artifact it's vouching for.
 *
 * Colors are CSS custom properties (defined per-theme in app.css under
 * [data-theme="dark"/"light"]) so toggling data-theme re-themes instantly —
 * mirrors the old inline `tailwind.config` the CDN script read at runtime. */
module.exports = {
  content: ["./templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Space Grotesk"', "system-ui", "sans-serif"],
        display: ["Fredoka", "system-ui", "sans-serif"],
        mono: ['"Space Mono"', "ui-monospace", "monospace"],
      },
      colors: {
        panel: { DEFAULT: "var(--c-panel)", b: "var(--c-panel-b)" },
        metal: { DEFAULT: "var(--c-metal)", hi: "var(--c-metal-hi)", lo: "var(--c-metal-lo)" },
        accent: "var(--c-accent)",
        burgundy: { DEFAULT: "var(--c-burgundy)", hi: "var(--c-burgundy-hi)", lo: "var(--c-burgundy-lo)" },
        gray: {
          50: "var(--c-gray-50)", 100: "var(--c-gray-100)", 200: "var(--c-gray-200)", 300: "var(--c-gray-300)",
          400: "var(--c-gray-400)", 500: "var(--c-gray-500)", 600: "var(--c-gray-600)", 700: "var(--c-gray-700)",
          800: "var(--c-gray-800)", 900: "var(--c-gray-900)",
        },
        indigo: {
          50: "var(--c-indigo-50)", 300: "var(--c-indigo-300)",
          400: "var(--c-indigo-400)", 500: "var(--c-indigo-500)", 600: "var(--c-indigo-600)", 700: "var(--c-indigo-700)",
        },
        green: { 100: "var(--c-green-100)", 600: "var(--c-green-600)", 700: "var(--c-green-700)" },
        yellow: { 100: "var(--c-yellow-100)", 700: "var(--c-yellow-700)" },
        red: { 50: "var(--c-red-50)", 500: "var(--c-red-500)", 600: "var(--c-red-600)" },
        amber: { 600: "var(--c-amber-600)" },
      },
    },
  },
};
