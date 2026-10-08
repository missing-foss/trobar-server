// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// Administration > Playlist mirrors: only configured targets, each with a
// state; one explanation when nothing is configured; Lidarr apart from the
// mirrors, and a pointer to set mirroring up when it is the only target.
// The per-target states themselves are the server's (AdminMirrorTargetsTests
// in test_routes.py); here the page's rendering of them. The e2e instance
// has nothing configured, so the other cases seed `mirrors` the way
// playlists.spec.js seeds rows: what's under test is the markup.

async function gotoMirrors(page) {
  await page.goto("/#/admin");
  await page.waitForFunction(
    () => window.Alpine && !!window.Alpine.$data(document.querySelector("[x-data]")));
  await page.evaluate(async () => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.goToTab("admin");
    d.setSubTab("admin", "mirrors");
    await d.loadMirrors();
  });
}

const target = (key, over) => ({ key, kind: key === "lidarr" ? "request" : "mirror", configured: false,
  state: null, detail_code: null, detail: null, count: 0, ...over });

async function seed(page, targets, playlists = []) {
  await page.evaluate(([targets, playlists]) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.mirrors = { targets, playlists, lidarr_request_configured: true };
    d.mirrorsLoading = false;
  }, [targets, playlists]);
}

const header = (page, id) => page.locator(`[data-mirror-section="${id}"] h2 button`);

test.describe("Playlist mirrors tab", () => {
  test("with nothing configured, one explanation and a way to set it up", async ({ page }) => {
    await gotoMirrors(page);
    await expect(page.getByRole("heading", { name: "Mirror your playlists elsewhere" })).toBeVisible();
    await expect(page.locator("[data-mirror-section]")).toHaveCount(0);
    await page.getByRole("button", { name: "Set up in Configuration" }).click();
    await expect(page.locator('[x-model="adminConfig.mirror_folder"]')).toBeFocused();
  });

  test("Lidarr alone: a green check, no address, and a pointer to set up mirroring", async ({ page }) => {
    await gotoMirrors(page);
    await seed(page, [target("filesystem"), target("subsonic"), target("jellyfin"), target("emby"),
      target("lidarr", { configured: true, state: "ok" })]);
    const targets = page.locator("#mirror-section-targets");
    await expect(targets).toContainText("Lidarr");
    await expect(targets).not.toContainText("http");
    await expect(targets.getByText("No mirror target is set up yet", { exact: false })).toBeVisible();
    await expect(header(page, "targets")).toContainText("No mirror target · Lidarr");
    await expect(header(page, "targets")).toContainText("No mirror target set up");
    // The quiet "also available" line is for when some mirror exists.
    await expect(targets.getByText("Also available", { exact: false })).toBeHidden();
  });

  test("configured targets only, each with its state; unconfigured ones as one quiet line", async ({ page }) => {
    await gotoMirrors(page);
    await seed(page, [
      target("filesystem", { configured: true, state: "ok", where: "/mirror" }),
      target("subsonic"),
      target("jellyfin", { configured: true, state: "error", detail_code: "unreachable", where: "http://jellyfin.example:8096" }),
      target("emby"),
      target("lidarr", { configured: true, state: "warning", detail_code: "incomplete" }),
    ]);
    const targets = page.locator("#mirror-section-targets");
    await expect(targets).toContainText("✓ Ready");
    await expect(targets).toContainText("⚠ Unreachable");
    await expect(targets).toContainText("Needs attention: root folder and profiles not chosen");
    await expect(targets).toContainText("Also available: Subsonic, Emby, Music Assistant, Plex.");
    await expect(targets).not.toContainText("Subsonic mirror target");
    // The states' colours and the chips, which the accessibility spec never
    // sees on this unconfigured instance.
    const axe = await new AxeBuilder({ page }).include("[data-mirror-section]").withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(axe.violations.map(v => `${v.id} x${v.nodes.length}`)).toEqual([]);
    // Collapsed, the warnings stay on the header.
    await header(page, "targets").click();
    await expect(targets).toBeHidden();
    await expect(header(page, "targets")).toContainText("Jellyfin: Unreachable");
    await expect(header(page, "targets")).toContainText("Lidarr: Needs attention");
  });

  test("each line under a playlist names its sink", async ({ page }) => {
    await gotoMirrors(page);
    await seed(page, [target("filesystem", { configured: true, state: "ok", where: "/mirror" }),
      target("subsonic"), target("jellyfin", { configured: true, state: "ok", where: "http://jf.example" }), target("emby"),
      target("lidarr")], [{
      id: 1, title: "Road Trip", matched: 41, total: 48,
      mirror_enabled: true, mirror_filename: "Road Trip.m3u", mirror_last_written_at: "2026-10-04 10:00:00",
      jellyfin_mirror_enabled: true, jellyfin_mirror_remote_id: "abc123",
      jellyfin_mirror_last_error_code: "unreachable", jellyfin_mirror_last_error: "timed out",
    }]);
    const rows = page.locator("#mirror-section-playlists");
    await expect(rows.getByText(".m3u folder", { exact: true })).toBeVisible();
    await expect(rows.getByText("Jellyfin", { exact: true })).toBeVisible();
    await expect(rows).toContainText("Road Trip.m3u");
    await expect(rows).toContainText("timed out");
    await expect(header(page, "playlists")).toContainText("1 playlist with a failed write");
  });

  test("a Settings link opens the Configuration section holding that target's field", async ({ page }) => {
    await gotoMirrors(page);
    await seed(page, [target("filesystem"), target("subsonic"), target("jellyfin"), target("emby"),
      target("lidarr", { configured: true, state: "ok" })]);
    await page.locator("#mirror-section-targets").getByRole("button", { name: "Settings" }).click();
    await expect(page.locator("#cfg-section-playlists")).toBeVisible();
    await expect(page.locator('[x-model="adminConfig.lidarr_url"]')).toBeFocused();
  });
});
