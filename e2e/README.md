<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# e2e (Playwright)

Browser-level regression tests for things `dev/check_inline_js.py` structurally
can't catch — it only verifies the inline `<script>` blocks *parse*, not that
they *behave* correctly at runtime. See `image-error-recovery.spec.js` (#30):
an `<img>` bound to `@error` without a matching `@load` looks fine to every
server-side check and still leaves the image stuck hidden in the browser after
one failed load.

This suite doesn't start the server — point it at one with `TROBAR_BASE_URL`.

## Running in CI

`.github/workflows/ci.yml` starts a fresh instance directly (`python
app/main.py`, `AUTH_MODE=local`), plus a mock Last.fm API on `localhost:8080`
(#281 — see below) so the Most Played chart has real data during the
accessibility scan, and runs `npx playwright install --with-deps chromium`
as a plain `run:` step — GitHub's runners have sudo, so that's fine there.

## Running locally

**This dev host has no sudo**, so `playwright install --with-deps` won't work
bare — do everything inside the official Playwright image instead, which
bundles Node, the browsers, and their system deps.

The trobar container and the mock need to resolve each other by name, so
both join a dedicated bridge network — **never run the trobar container with
`--network host` for this**: that would bind it to the host's real port
5000 directly (no way to remap it away, unlike `-p`), which on this
machine is the actual production instance's port.

```bash
# 0. A dedicated network so the two containers can resolve each other by name
#    (the default bridge network doesn't do this) — and so trobar's own port
#    stays safely remapped via -p below, never bound to the host directly.
docker network create trobar-e2e-net

# 1. The mock Last.fm API (#281) — same one dev/'s docker-compose uses for
#    Suggestions/auto-fit, so the Most Played chart (#267) has real data to
#    render during the scan, not just its empty-state hint.
docker build -t lastfm-mock:e2e ./dev/lastfm-mock
docker run --rm -d --name lastfm-mock-e2e --network trobar-e2e-net \
  -v "$(pwd)/dev/testlib.json":/testlib.json:ro \
  lastfm-mock:e2e

# 2. Start a throwaway trobar instance (or point at dev/docker-compose.yaml's
#    instance — but see the note below about a fresh admin account).
#    ALWAYS `docker build -t trobar:dev .` first, even if you already have
#    the tag — a stale image (built before a later template/UI change) runs
#    fine but serves outdated app code against the current test suite,
#    producing confusing early-test failures (e.g. a JS method the tests
#    expect that an older template genuinely doesn't have yet) that look
#    exactly like "the harness itself won't come up" (#244).
docker build -t trobar:dev .
mkdir -p /tmp/trobar-e2e-data /tmp/trobar-e2e-music
docker run --rm -d --name trobar-e2e-test --network trobar-e2e-net -p 5099:5000 \
  -e AUTH_MODE=local -e SESSION_COOKIE_SECURE=0 -e MUSIC_ROOT=/music -e DATA_DIR=/data \
  -e LASTFM_API_BASE=http://lastfm-mock-e2e:8080/2.0/ -e LASTFM_API_KEY=e2e-mock-key \
  -v /tmp/trobar-e2e-data:/data -v /tmp/trobar-e2e-music:/music \
  trobar:dev

# 3. Run the suite from the repo root, against it, via the Playwright image.
#    Keep the image tag in step with package.json's @playwright/test version
#    (currently 1.62.0) or you'll hit a "please update docker image" error.
#    LASTFM_MOCK_USERNAME tells global-setup.js to seed it — omit this var
#    entirely (see the dev-sandbox note below) if you don't want that.
docker run --rm --network host \
  -v "$(pwd)":/work -w /work \
  -e TROBAR_BASE_URL=http://localhost:5099 \
  -e MUSIC_ROOT=/music \
  -e LASTFM_MOCK_USERNAME=e2e-mock-user \
  mcr.microsoft.com/playwright:v1.62.0-noble \
  bash -c "npm ci && npx playwright test --config=e2e/playwright.config.js"

# 4. Clean up
docker rm -f trobar-e2e-test lastfm-mock-e2e
docker network rm trobar-e2e-net
rm -rf /tmp/trobar-e2e-data /tmp/trobar-e2e-music e2e/.auth test-results
```

Note on `dev/docker-compose.yaml`'s own instance: `global-setup.js` bootstraps
the admin account via the very first `/login` POST (see `login()` in
`app/main.py`) — that only works against a brand-new instance with zero
users. Against the dev sandbox (which already has an admin from the setup
wizard), point `TROBAR_TEST_USER`/`TROBAR_TEST_PASS` at its real credentials
instead, or just use a fresh throwaway instance as above. **Don't set
`LASTFM_MOCK_USERNAME`** against the dev sandbox either — `global-setup.js`
would overwrite that real admin's actual `profile.lastfm_username` (a PUT
there is a full overwrite, not a partial update) with the synthetic seeded
value.
