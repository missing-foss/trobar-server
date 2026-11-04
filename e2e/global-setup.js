// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const fs = require("fs");
const path = require("path");
const { request } = require("@playwright/test");

// A 1x1 transparent PNG — just needs to be a valid image the upload
// endpoint accepts; content doesn't matter for these tests.
const ONE_PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

// #244: every step below now asserts success instead of firing-and-forgetting
// — a transient failure used to produce a half-authenticated state.json that
// cascaded into confusing per-test failures far from the actual problem,
// instead of one clear "bootstrap step N failed" error right here. Extensive
// local repro attempts (25+ runs, including CPU-throttled to simulate a
// noisy CI runner) never reproduced a real failure — the "known flake"
// blamed in #243 is at least sometimes actually a STALE `trobar:dev` image
// (predating a later UI change) producing an unrelated, misleading early
// test failure that looks like "the harness didn't come up right"; see
// e2e/README.md's note on always rebuilding the image first. This hardening
// stands regardless, since a silent half-bootstrap is worth guarding against
// on its own merits.
//
// /login is checked differently from the rest: a failed login (wrong
// credentials, or — the actual race this retry targets — the admin account
// already existing from a previous incomplete run so the bootstrap branch
// no longer applies) re-renders login.html with a PLAIN 200, same status as
// a real success; only the final URL differs (redirected to "/" vs. still
// on "/login"). Status-code-only checking, the obvious first fix, would
// have silently passed right through that failure — this checks
// resp.url() instead, and retries with backoff since it's the one request
// racing "is the server fully up" (the CI/manual readiness-poll before this
// runs only confirms Flask answers GET /login, not that everything
// downstream is warm).
async function assertOk(resp, label) {
  if (!resp.ok()) {
    throw new Error(
      `global-setup bootstrap step failed: ${label} → HTTP ${resp.status()} ${resp.statusText()}`,
    );
  }
  return resp;
}

// The server under test has to be running this checkout's code. A stale
// `trobar:dev` image (built before a later UI change) runs fine but serves
// old code, and the first tests then fail in ways that look like a broken
// harness. In CI the server runs from the checkout itself, so this always
// passes there; it is for local runs against a container.
//
// Two checks, so that a template-only change is caught too (most UI changes
// touch a template and no static file):
// - every file under app/static must be served byte for byte;
// - a page's template text that every render contains must appear in the
//   rendered page. That is the literal text between Jinja tags at nesting
//   depth 0: text inside an if/for/macro may legitimately not render. Flask
//   emits that text unchanged (no trim_blocks), so a template edit to it shows
//   up as a missing chunk.
const APP_DIR = path.join(__dirname, "..", "app");
const STALE_HINT = "the server under test is not running this checkout's code. " +
  "Rebuild the image (docker build -t trobar:dev .) and start it again; see e2e/README.md";

function staticFiles(dir, prefix = "") {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? staticFiles(path.join(dir, e.name), `${prefix}${e.name}/`) : [`${prefix}${e.name}`]);
}

// The server may still be starting when the run begins: a container started
// moments ago. CI waits for /login before running Playwright; a local run may
// not. So wait here, before the first check, instead of failing it on a
// refused connection.
async function waitForServer(ctx, timeoutMs = 30_000, delayMs = 500) {
  const deadline = Date.now() + timeoutMs;
  let last = "no answer";
  while (Date.now() < deadline) {
    try {
      const resp = await ctx.get("/login");
      if (resp.ok()) return;
      last = `HTTP ${resp.status()}`;
    } catch (e) {
      last = e.message.split("\n")[0];
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  throw new Error(`global-setup: the server never answered GET /login within ${timeoutMs / 1000} s (last: ${last})`);
}

async function assertStaticMatchesCheckout(ctx) {
  const files = staticFiles(path.join(APP_DIR, "static"));
  if (files.length === 0) throw new Error("source check: no files under app/static, so it would check nothing");
  for (const rel of files) {
    const resp = await ctx.get(`/static/${rel}`);
    const local = fs.readFileSync(path.join(APP_DIR, "static", rel));
    if (!resp.ok() || !Buffer.from(await resp.body()).equals(local)) {
      throw new Error(`source check: /static/${rel} differs from the checkout (HTTP ${resp.status()}): ${STALE_HINT}`);
    }
  }
  return files.length;
}

const BLOCK_OPEN = /^\{%\s*(if|for|macro|block|call|filter|with|raw)\b/;
const BLOCK_SET = /^\{%\s*set\b[^=]*%\}$/;  // `{% set x %}...{% endset %}`, not `{% set x = 1 %}`
const BLOCK_CLOSE = /^\{%\s*end\w+/;

function alwaysRenderedText(template) {
  const chunks = [];
  let depth = 0;
  const pieces = template.split(/(\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}|\{#[\s\S]*?#\})/);
  pieces.forEach((piece, i) => {
    if (piece.startsWith("{%")) {
      if (BLOCK_CLOSE.test(piece.replace("{%-", "{%"))) depth -= 1;
      else if (BLOCK_OPEN.test(piece.replace("{%-", "{%")) || BLOCK_SET.test(piece.replace("{%-", "{%"))) depth += 1;
      return;
    }
    if (piece.startsWith("{{") || piece.startsWith("{#") || depth !== 0) return;
    // Whitespace control: a `-` inside a tag's delimiter strips the
    // whitespace on that side of it.
    let text = piece;
    if (i > 0 && /-(%\}|\}\}|#\})$/.test(pieces[i - 1])) text = text.trimStart();
    if (i + 1 < pieces.length && /^(\{%|\{\{|\{#)-/.test(pieces[i + 1])) text = text.trimEnd();
    if (text.trim().length >= 40) chunks.push(text);
  });
  if (depth !== 0) throw new Error(`source check: unbalanced Jinja blocks (depth ${depth} at the end)`);
  return chunks;
}

async function assertPageMatchesTemplate(ctx, urlPath, templateName) {
  // Jinja drops a template's single trailing newline (keep_trailing_newline
  // is off by default).
  const template = fs.readFileSync(path.join(APP_DIR, "templates", templateName), "utf8").replace(/\n$/, "");
  const chunks = alwaysRenderedText(template);
  if (chunks.length === 0) throw new Error(`source check: ${templateName} has no text to compare, so it would check nothing`);
  const resp = await assertOk(await ctx.get(urlPath), `source check: GET ${urlPath}`);
  const page = await resp.text();
  const missing = chunks.find((c) => !page.includes(c));
  if (missing !== undefined) {
    throw new Error(`source check: ${templateName} text is missing from GET ${urlPath} ` +
      `(${JSON.stringify(missing.trim().slice(0, 80))}...): ${STALE_HINT}`);
  }
  return chunks.length;
}

async function loginWithRetry(ctx, baseURL, username, password, attempts = 5, delayMs = 500) {
  let lastError;
  for (let i = 1; i <= attempts; i++) {
    const resp = await ctx.post("/login", { form: { username, password } });
    if (resp.ok() && new URL(resp.url()).pathname !== "/login") {
      return resp;
    }
    lastError = new Error(
      `global-setup bootstrap step failed: /login (attempt ${i}/${attempts}) → ` +
      `HTTP ${resp.status()}, landed on ${resp.url()} instead of ${baseURL}/`,
    );
    if (i < attempts) await new Promise((r) => setTimeout(r, delayMs * i));
  }
  throw lastError;
}

// Bootstraps a fresh trobar-server instance the same way a human does on
// first run: the very first POST to /login (with AUTH_MODE=local and zero
// existing users) creates the admin account (see login() in app/main.py),
// then the setup wizard's two endpoints point it at a music root and mark
// setup complete. Also uploads a real avatar so profile.avatar_url is
// truthy — the profile-picture bug (#30) only reproduces once there's an
// actual picture for the <img> to try (and fail, then succeed) loading.
// Saves the resulting session cookie so every test starts already logged in.
module.exports = async () => {
  const baseURL = process.env.TROBAR_BASE_URL || "http://localhost:5000";
  const username = process.env.TROBAR_TEST_USER || "e2e-admin";
  const password = process.env.TROBAR_TEST_PASS || "e2e-test-password-not-a-secret";
  const musicRoot = process.env.MUSIC_ROOT || "/music";

  const ctx = await request.newContext({ baseURL });
  try {
    await waitForServer(ctx);
    const staticCount = await assertStaticMatchesCheckout(ctx);
    const loginChunks = await assertPageMatchesTemplate(ctx, "/login", "login.html");
    await loginWithRetry(ctx, baseURL, username, password);
    await assertOk(
      await ctx.post("/api/setup/music-root", { data: { path: musicRoot } }),
      "/api/setup/music-root",
    );
    await assertOk(await ctx.post("/api/setup/complete"), "/api/setup/complete");
    const indexChunks = await assertPageMatchesTemplate(ctx, "/", "index.html");
    console.log(`source check: ${staticCount} static files, ${loginChunks} login.html and ` +
      `${indexChunks} index.html text chunks match the checkout`);
    await assertOk(
      await ctx.post("/api/profile/avatar", {
        multipart: { avatar: { name: "avatar.png", mimeType: "image/png", buffer: ONE_PIXEL_PNG } },
      }),
      "/api/profile/avatar",
    );

    // #281: if the harness started a mock Last.fm (LASTFM_MOCK_USERNAME set —
    // never a real account), seed it here so Suggestions/Most Played (#267)
    // have real data to render during the accessibility scans, not just
    // their "connect a service" empty-state hint. Skipped when unset, so
    // running e2e any other way (e.g. against a real dev sandbox per
    // e2e/README.md's alternate path) never overwrites a real, unmocked
    // profile.lastfm_username. PUT /api/profile is a full overwrite, not a
    // partial update (see main.py's api_profile) — safe only this early,
    // before any other test has set cover_view_mode/dashboard_widgets/etc.
    const lastfmMockUsername = process.env.LASTFM_MOCK_USERNAME;
    if (lastfmMockUsername) {
      await assertOk(
        await ctx.put("/api/profile", { data: { lastfm_username: lastfmMockUsername } }),
        "/api/profile (lastfm_username)",
      );
    }

    const authDir = path.join(__dirname, ".auth");
    fs.mkdirSync(authDir, { recursive: true });
    await ctx.storageState({ path: path.join(authDir, "state.json") });
  } finally {
    await ctx.dispose();
  }
};
