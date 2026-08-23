// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// #113: waitress sits in front of ProxyFix with its own proxy-trust gate and
// defaults to stripping X-Forwarded-* from untrusted proxies — so without
// `trusted_proxy` on the serve() call it drops the reverse proxy's
// X-Forwarded-Proto/Host, and url_for(_external=True) builds http:// URLs
// behind the TLS proxy. That broke the OIDC/Tidal redirect_uri in prod (a
// mismatched http:// URI the IdP rejects). This regression slipped through
// #82/#104 because nothing tested the serving layer — this is that test.
//
// The CI e2e server runs the real waitress (`python app/main.py`), so sending
// X-Forwarded-Proto: https on a request that generates an external URL must
// come back https — proving waitress trusts and forwards the header to
// ProxyFix. Uses the Tidal connect redirect, the one _external URL reachable
// in local auth mode (OIDC needs a live IdP).

test("waitress forwards X-Forwarded-Proto/Host so external URLs are https", async ({ request }) => {
  // Tidal connect needs client credentials configured (admin-only).
  const put = await request.put("/api/admin/config", {
    data: { tidal_client_id: "e2e-tidal-client", tidal_client_secret: "e2e-tidal-secret" },
  });
  expect(put.ok()).toBeTruthy();
  try {
    const resp = await request.get("/profile/tidal/connect", {
      headers: { "X-Forwarded-Proto": "https", "X-Forwarded-Host": "trobar.e2e.example" },
      maxRedirects: 0,
    });
    expect(resp.status()).toBe(302);
    // The redirect to Tidal's authorize endpoint must carry an https:// (and
    // forwarded-host) redirect_uri — i.e. ProxyFix saw the forwarded scheme
    // through waitress's trusted_proxy. Before the fix this was http://.
    const location = decodeURIComponent(resp.headers()["location"] || "");
    expect(location).toContain("redirect_uri=https://trobar.e2e.example/profile/tidal/callback");
  } finally {
    // Restore: leave Tidal unconfigured so other specs see a clean instance.
    await request.put("/api/admin/config", { data: { tidal_client_id: "", tidal_client_secret: "" } });
  }
});

// #382: the brute-force login backoff used to key on the
// LEFT-most X-Forwarded-For entry — whatever the client itself claimed — so
// an attacker rotating a fake leftmost IP every request never accumulated a
// failure in any one bucket. The fix (ProxyFix x_for=1) trusts only the
// RIGHT-most hop, the one the proxy itself appends. Same reasoning as the
// test above (waitress's trusted_proxy gate must actually forward the
// header for ProxyFix to see it at all), proven end-to-end through the real
// waitress server rather than against Flask's test client in isolation —
// see test_routes.RateLimitTrustedProxyTests for that in-process coverage.
//
// A dedicated, never-reused trusted right-most IP (TEST-NET-2, RFC 5737) so
// this can safely exhaust its own rate-limit bucket without colliding with
// any other spec's login traffic.
test("waitress-forwarded X-Forwarded-For rate-limits on the trusted hop, not a spoofed one", async ({ request }) => {
  const trustedHop = "198.51.100.77";
  const failedLogin = (spoofedPrefix) =>
    request.post("/login", {
      form: { username: "no-such-e2e-user", password: "wrong" },
      headers: { "X-Forwarded-For": `${spoofedPrefix}, ${trustedHop}` },
      maxRedirects: 0,
    });

  for (let i = 0; i < 10; i++) {
    const resp = await failedLogin(`203.0.113.${i}`); // rotates every request
    expect(resp.status(), `attempt ${i} was already rate-limited`).not.toBe(429);
  }
  const limited = await failedLogin("203.0.113.250");
  expect(limited.status()).toBe(429);
});
