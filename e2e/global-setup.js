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
    await loginWithRetry(ctx, baseURL, username, password);
    await assertOk(
      await ctx.post("/api/setup/music-root", { data: { path: musicRoot } }),
      "/api/setup/music-root",
    );
    await assertOk(await ctx.post("/api/setup/complete"), "/api/setup/complete");
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
