// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");

// #410/#507: the Playlists row's mirror controls. global-setup seeds an
// empty library with no provider connected (see e2e/README.md), so there's
// no way to reach a real playlist row through actual provider sync — the
// same constraint #413's own staging-basket test hit. Seeding
// `app.playlists` directly is the same established pattern used for the
// #415/#416-part-3 Library tests: what's under test is the row's own
// markup reacting to mirror_folder_configured/mirror_enabled/
// mirror_last_error, not the fetch that normally populates the array.
//
// None of these tests actually click a picker row to toggle a sink —
// toggleMirror() posts to a real playlist id that doesn't exist in the
// seeded DB, so that stays untested here the same way it always has been
// (these rows are client-injected fixtures, not real playlists).

async function gotoPlaylists(page) {
  await page.goto("/#/playlists");
  await page.evaluate(() =>
    window.Alpine.$data(document.querySelector("[x-data]")).goToTab("playlists"));
  await page.waitForTimeout(400);
}

test.describe("Playlists row mirror picker (#507)", () => {
  test("the Mirror… button is disabled with a hint when no sink is configured", async ({ page }) => {
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 1, title: "E2E Unconfigured Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: false,
        subsonic_mirror_enabled: false, subsonic_mirror_configured: false,
        jellyfin_mirror_enabled: false, jellyfin_mirror_configured: false,
        emby_mirror_enabled: false, emby_mirror_configured: false,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    const mirrorButton = page.getByRole("button", { name: "Mirror…" });
    await expect(mirrorButton).toBeDisabled();
    await expect(mirrorButton).toHaveAttribute("title", /ask an admin/i);
  });

  test("the Mirror… button stays enabled (to allow turning off) once already mirroring, even if unconfigured", async ({ page }) => {
    // The target can be unset by an admin AFTER a playlist started
    // mirroring — the row must still let that be turned off, not get stuck
    // "on" (same #410 reasoning the old per-sink buttons had).
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 2, title: "E2E Stuck-On Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: true, mirror_last_error: "No mirror folder configured (Administration > Configuration).",
        mirror_folder_configured: false,
        subsonic_mirror_enabled: false, subsonic_mirror_configured: false,
        jellyfin_mirror_enabled: false, jellyfin_mirror_configured: false,
        emby_mirror_enabled: false, emby_mirror_configured: false,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    const mirrorButton = page.getByRole("button", { name: "Mirror…" });
    await expect(mirrorButton).toBeEnabled();

    // #507 item 3: the identity row now carries a badge for the on sink,
    // with the error folded into its title — same "readable, not just a
    // title='' tooltip" point #410 made about the old per-sink error line,
    // now made about the picker (checked below) instead of an always-on
    // row of text.
    await expect(page.locator('[title*="No mirror folder configured"]')).toBeVisible();

    await mirrorButton.click();
    // #507 item 1: greyed out, but still clickable to turn off — labelled
    // "Mirroring" (filesystem's own on-label), not hidden just because
    // it's unconfigured.
    const filesystemRow = page.getByRole("button", { name: "Mirroring", exact: true });
    await expect(filesystemRow).toBeVisible();
    await expect(filesystemRow).toBeEnabled();
    // The detail itself now lives in the picker, per the issue's own
    // "icon says something's wrong, the picker says what" suggestion.
    await expect(page.getByText("No mirror folder configured (Administration > Configuration).")).toBeVisible();
  });

  test("the Mirror… button is enabled, no hint, once a sink is configured", async ({ page }) => {
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 3, title: "E2E Configured Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: true,
        subsonic_mirror_enabled: false, subsonic_mirror_configured: false,
        jellyfin_mirror_enabled: false, jellyfin_mirror_configured: false,
        emby_mirror_enabled: false, emby_mirror_configured: false,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    const mirrorButton = page.getByRole("button", { name: "Mirror…" });
    await expect(mirrorButton).toBeEnabled();
    await expect(mirrorButton).toHaveAttribute("title", "");
  });

  test("the picker lists only configured/already-mirrored sinks, not every sink (item 1)", async ({ page }) => {
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 4, title: "E2E Subsonic-Only Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: false,
        subsonic_mirror_enabled: false, subsonic_mirror_configured: true,
        jellyfin_mirror_enabled: false, jellyfin_mirror_configured: false,
        emby_mirror_enabled: false, emby_mirror_configured: false,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    await page.getByRole("button", { name: "Mirror…" }).click();
    await expect(page.getByRole("button", { name: "Mirror to Subsonic…" })).toBeVisible();
    // No rows for sinks that aren't configured — the issue's own "no rows
    // for targets that don't exist" requirement.
    await expect(page.getByRole("button", { name: "Mirror to Jellyfin…" })).not.toBeVisible();
    await expect(page.getByRole("button", { name: "Mirror to Emby…" })).not.toBeVisible();
    await expect(page.getByRole("button", { name: "Mirroring", exact: true })).not.toBeVisible();
  });

  test("an already-mirrored sink shows a checkmark and greyed styling in the picker (item 2)", async ({ page }) => {
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 5, title: "E2E Jellyfin-Mirrored Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: false,
        subsonic_mirror_enabled: false, subsonic_mirror_configured: false,
        jellyfin_mirror_enabled: true, jellyfin_mirror_configured: true,
        emby_mirror_enabled: false, emby_mirror_configured: false,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    await page.getByRole("button", { name: "Mirror…" }).click();
    const jellyfinRow = page.getByRole("button", { name: "Mirroring to Jellyfin" });
    await expect(jellyfinRow).toBeVisible();
    // Greyed (not the still-off rows' darker, actionable treatment), and
    // carries the checkmark icon.
    await expect(jellyfinRow).toHaveClass(/border-gray-200/);
    await expect(jellyfinRow.locator("svg")).toHaveCount(2); // provider icon + checkmark
  });

  test("the identity row shows a mirrored-sink badge with an accessible title (items 3-5)", async ({ page }) => {
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 6, title: "E2E Badge Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: false,
        subsonic_mirror_enabled: true, subsonic_mirror_configured: true,
        subsonic_mirror_last_error: null, subsonic_mirror_last_error_code: null,
        jellyfin_mirror_enabled: false, jellyfin_mirror_configured: false,
        emby_mirror_enabled: false, emby_mirror_configured: false,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    // Not a bare glyph — a real accessible name, per the issue's own item 5.
    await expect(page.getByText("E2E Badge Playlist")).toBeVisible();
    await expect(page.locator('[title="Mirrored to Subsonic"]')).toBeVisible();
  });

  // #507 review: a real reachable crash — loadPlaylists() (called after
  // every toggleMirror(), and by any resync) replaces `this.playlists`
  // wholesale. With the picker open, mirrorPickerPlaylist() then resolves
  // to null (its own row's id no longer present — a ghost-cleanup resync
  // renumbering ids reproduced this for the reviewer with zero unusual
  // action on their part), and every unguarded `mirrorPickerPlaylist()[…]`
  // read inside the still-mounted picker rows threw before Alpine could
  // tear them down, leaving a dimmed backdrop with no dialog and no
  // reachable Close button — the only escape an undiscoverable backdrop
  // click. Reproduced directly (not just "no console errors" as a weak
  // proxy): assert the WHOLE overlay is gone, not just emptied out.
  test("the picker closes cleanly, without throwing, if its playlist disappears from under it (review fix)", async ({ page }) => {
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 7, title: "E2E Vanishing Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: true, mirror_last_error: null, mirror_folder_configured: true,
        subsonic_mirror_enabled: true, subsonic_mirror_configured: true,
        subsonic_mirror_last_error: "Could not reach the Subsonic mirror target: timed out",
        subsonic_mirror_last_error_code: "unreachable",
        jellyfin_mirror_enabled: false, jellyfin_mirror_configured: false,
        emby_mirror_enabled: false, emby_mirror_configured: false,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    await page.getByRole("button", { name: "Mirror…" }).click();
    await expect(page.locator("#mirror-picker-overlay")).toBeVisible();
    await expect(page.getByRole("button", { name: "Mirroring", exact: true })).toBeVisible();

    // The exact shape of a resync/toggle response: a wholesale replacement
    // of the array, this row's id no longer present (renumbered/removed),
    // while the picker is still open.
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 8, title: "E2E Some Other Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: false,
        subsonic_mirror_enabled: false, subsonic_mirror_configured: false,
        jellyfin_mirror_enabled: false, jellyfin_mirror_configured: false,
        emby_mirror_enabled: false, emby_mirror_configured: false,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    // The whole overlay (backdrop included) is gone — not a dimmed,
    // undismissable dead end.
    await expect(page.locator("#mirror-picker-overlay")).not.toBeVisible();
    await expect(page.getByRole("button", { name: "Mirroring", exact: true })).not.toBeVisible();
    expect(errors).toEqual([]);
  });
});

test.describe("Playlists row layout on mobile (#410 parts 1+2)", () => {
  // #410's report: the row's three action buttons (Mirror to…, Select, Add
  // to basket) were all shrink-0 on one line with the title/availability-bar
  // block, so on a phone that block was squeezed to a sliver. The fix wraps
  // the actions in their own group and stacks it below via flex-col
  // (sm:flex-row restores the single-line layout with room to spare).
  // Bounding-box comparison, not a screenshot diff — a numeric "did this
  // actually move to its own line" claim is the thing under test.

  test("action buttons sit on their own line below the title on a narrow viewport", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 });
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 4, title: "E2E Narrow Viewport Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: true,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    const titleBox = await page.getByText("E2E Narrow Viewport Playlist").boundingBox();
    const selectBox = await page.getByRole("button", { name: "Sync to…" }).boundingBox();
    expect(titleBox).toBeTruthy();
    expect(selectBox).toBeTruthy();
    // Stacked, not squeezed onto the title's own line.
    expect(selectBox.y).toBeGreaterThanOrEqual(titleBox.y + titleBox.height);
  });

  test("action buttons stay on the same line as the title on a wide viewport", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 5, title: "E2E Wide Viewport Playlist", source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: true,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    const titleBox = await page.getByText("E2E Wide Viewport Playlist").boundingBox();
    const selectBox = await page.getByRole("button", { name: "Sync to…" }).boundingBox();
    expect(titleBox).toBeTruthy();
    expect(selectBox).toBeTruthy();
    // Vertically overlapping, not stacked — sm:flex-row's job.
    expect(selectBox.y).toBeLessThan(titleBox.y + titleBox.height);
  });

  test("a title with no break opportunity truncates instead of forcing horizontal page scroll (review fix)", async ({ page }) => {
    // Caught in review: the flex-col fix above stops the ACTIONS from being
    // squeezed, but the title block itself still had no min-w-0 — a flex
    // item won't shrink below its content's intrinsic width otherwise. A
    // title with spaces/hyphens wraps and so never hits this, but a
    // filesystem-provider playlist's title comes straight from an .m3u
    // FILENAME, where dot/underscore separators (no wrap points) are the
    // norm — exactly the case none of this file's other titles exercise.
    await page.setViewportSize({ width: 375, height: 800 });
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 7, title: "Now_Playing_2026_Summer_Roadtrip_Mix_Extended_Edition_Remastered",
        source_provider: "filesystem",
        owner_user_id: null, owner_username: null, shared: 0, is_own: false,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: true,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
    });
    await page.waitForTimeout(200);

    const overflowsHorizontally = await page.evaluate(() =>
      document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflowsHorizontally).toBe(false);
  });
});

test.describe("Shared/Private toggle affordance (#410 part 2)", () => {
  test("the Shared/Private toggle carries an icon and its own colour, distinct from the action buttons", async ({ page }) => {
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [{
        id: 6, title: "E2E Owned Playlist", source_provider: "filesystem",
        owner_user_id: 1, owner_username: "e2e-mock-user", is_own: true, shared: 1,
        mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: true,
        golden_owner_username: null, inferred_origin_provider: null,
        track_count: 10, matched_count: 5, unresolved_count: 0,
      }];
      app.profile.is_admin = true;
    });
    await page.waitForTimeout(200);

    const sharedToggle = page.getByRole("button", { name: "Shared" });
    await expect(sharedToggle).toBeVisible();
    // Was a bare text label before #410 — now carries the globe/lock icon.
    await expect(sharedToggle.locator("svg")).toBeVisible();
    // Amber, not the actions' indigo/gray — this is what makes it read as a
    // state toggle rather than a fourth action button.
    await expect(sharedToggle).toHaveClass(/border-amber-300/);

    const selectButton = page.getByRole("button", { name: "Sync to…" });
    await expect(selectButton).not.toHaveClass(/border-amber-300/);
  });
});

test.describe("Hide zero-match playlists filter (#411)", () => {
  // hide_zero_match_playlists is persisted per-user (like cover_view_mode/
  // show_reissue_year), not per-browser-context — same leak class the
  // basket's own tests guard against. Reset unconditionally so a test here
  // can't change what a later, unrelated test sees.
  //
  // Drives the reset through the real app.saveProfile() (sends the FULL
  // current profile object), not a raw page.request.put() with a partial
  // body — PUT /api/profile is a full overwrite, not a partial update
  // (global-setup.js's own comment says so). A minimal {hide_zero_match_
  // playlists: false} body was tried first and silently wiped
  // lastfm_username off the shared test account for every later test in
  // the suite, surfacing as an unrelated Home-tab accessibility test
  // failing to find Most Played data — caught by seeing that failure
  // reproduce only when this file's tests ran, never in isolation.
  test.afterEach(async ({ page }) => {
    await page.evaluate(async () => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.profile.hide_zero_match_playlists = false;
      await app.saveProfile();
    });
  });

  test("checking the filter hides zero-match playlists and shows the shown/total count", async ({ page }) => {
    await gotoPlaylists(page);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.playlists = [
        {
          id: 8, title: "E2E Matched Playlist", source_provider: "filesystem",
          owner_user_id: null, owner_username: null, shared: 0, is_own: false,
          mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: true,
          golden_owner_username: null, inferred_origin_provider: null,
          track_count: 10, matched_count: 5, unresolved_count: 0,
        },
        {
          id: 9, title: "E2E Zero Match Playlist", source_provider: "filesystem",
          owner_user_id: null, owner_username: null, shared: 0, is_own: false,
          mirror_enabled: false, mirror_last_error: null, mirror_folder_configured: true,
          golden_owner_username: null, inferred_origin_provider: null,
          track_count: 10, matched_count: 0, unresolved_count: 0,
        },
      ];
    });

    await expect(page.getByText("E2E Matched Playlist")).toBeVisible();
    await expect(page.getByText("E2E Zero Match Playlist")).toBeVisible();
    // Off by default — a filtered-down count line would be noise otherwise.
    await expect(page.getByText(/of 2 shown/)).not.toBeVisible();

    const checkbox = page.getByRole("checkbox", { name: "Hide playlists with no local tracks" });
    await checkbox.check();

    await expect(page.getByText("E2E Zero Match Playlist")).not.toBeVisible();
    await expect(page.getByText("E2E Matched Playlist")).toBeVisible();
    // Says what's hidden, not just a shorter-looking list (#411's own
    // "silent omission is worse than a stated one" reasoning).
    await expect(page.getByText("1 of 2 shown")).toBeVisible();

    await checkbox.uncheck();
    await expect(page.getByText("E2E Zero Match Playlist")).toBeVisible();
    await expect(page.getByText(/of 2 shown/)).not.toBeVisible();
  });

  test("the preference persists across a reload", async ({ page }) => {
    await gotoPlaylists(page);
    const checkbox = page.getByRole("checkbox", { name: "Hide playlists with no local tracks" });

    // A real completion signal for the actual claim under test (server-side
    // persistence), not a guessed wait — saveProfile()'s PUT must land
    // before reloading or this proves nothing.
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/profile") && r.request().method() === "PUT"),
      checkbox.check(),
    ]);

    await page.reload();
    await page.evaluate(() =>
      window.Alpine.$data(document.querySelector("[x-data]")).goToTab("playlists"));
    await page.waitForTimeout(400);

    await expect(page.getByRole("checkbox", { name: "Hide playlists with no local tracks" })).toBeChecked();
  });
});
