// SPDX-FileCopyrightText: 2026 missing-foss
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// @ts-check
const { test, expect } = require("@playwright/test");
const { trackFetches, settled } = require("../support/settle");

// The library search folds the query and each artist name the same way before
// comparing: a keyboard types U+002D and plain letters, while tags written by
// MusicBrainz carry U+2010 HYPHEN and the accents.

const ARTISTS = [
  { artist: "‐M‐", track_count: 60, album_count: 5 }, // ‐M‐, U+2010 on both sides
  { artist: "Beyoncé", track_count: 20, album_count: 2 },
  { artist: "Björk", track_count: 30, album_count: 3 },
  { artist: "Beyonce Tribute Band", track_count: 5, album_count: 1 },
  { artist: "Jean–Michel Jarre", track_count: 12, album_count: 1 }, // en dash
  { artist: "Muse", track_count: 40, album_count: 4 },
];

/** The artists the library list shows for [query], by name as displayed. */
function search(page, query) {
  return page.evaluate(([artists, q]) => {
    const app = window.Alpine.$data(document.querySelector("[x-data]"));
    app.artists = artists;
    app.artistSearch = q;
    return app.filteredArtists().map((a) => a.artist);
  }, [ARTISTS, query]);
}

test.describe("library search folds dashes and accents", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.waitForFunction(() => window.Alpine && window.Alpine.$data(document.querySelector("[x-data]")));
  });

  test("a typed hyphen finds a name written with U+2010", async ({ page }) => {
    expect(await search(page, "-m-")).toEqual(["‐M‐"]);
    expect(await search(page, "-M-")).toEqual(["‐M‐"]);
  });

  test("an unaccented query finds the accented name, and the other way round", async ({ page }) => {
    expect(await search(page, "beyonce")).toEqual(["Beyoncé", "Beyonce Tribute Band"]);
    expect(await search(page, "beyoncé")).toEqual(["Beyoncé", "Beyonce Tribute Band"]);
    expect(await search(page, "bjork")).toEqual(["Björk"]);
  });

  test("an en dash in the name matches a typed hyphen", async ({ page }) => {
    expect(await search(page, "jean-michel")).toEqual(["Jean–Michel Jarre"]);
  });

  test("the fold does not widen a query into unrelated names", async ({ page }) => {
    expect(await search(page, "m-x")).toEqual([]);
    expect(await search(page, "mus")).toEqual(["Muse"]);
  });

  test("the names are shown as stored, not folded", async ({ page }) => {
    const shown = await search(page, "");
    expect(shown).toContain("‐M‐");
    expect(shown).toContain("Beyoncé");
  });
});

// Album titles are searched too, and the result is still the artist list.
const WITH_ALBUMS = [
  { artist: "Muse", track_count: 40, album_count: 2, albums: ["Absolution", "Origin of Symmetry"] },
  { artist: "Björk", track_count: 30, album_count: 2, albums: ["Homogenic", "Vespertine"] },
  { artist: "Symmetry Studies", track_count: 8, album_count: 1, albums: ["Études"] },
  // A title that also matches the name: a name hit shows no album line.
  { artist: "Radiohead", track_count: 50, album_count: 2, albums: ["OK Computer", "Radio Silence"] },
];

/** [query]'s result as {artist: the album titles shown under it}. */
function searchWithAlbums(page, query, artists = WITH_ALBUMS) {
  return page.evaluate(([list, q]) => {
    const app = window.Alpine.$data(document.querySelector("[x-data]"));
    app.artists = list;
    app.artistSearch = q;
    return Object.fromEntries(app.filteredArtists().map((a) => [a.artist, app.albumOnlyHits(a)]));
  }, [artists, query]);
}

test.describe("library search matches album titles", () => {
  test.beforeEach(async ({ page }) => {
    await trackFetches(page);
    await page.goto("/");
    await page.waitForFunction(() => window.Alpine && window.Alpine.$data(document.querySelector("[x-data]")));
  });

  test("an artist-name hit lists the artist, with no album line", async ({ page }) => {
    expect(await searchWithAlbums(page, "radio")).toEqual({ Radiohead: [] });
  });

  test("an album-only hit lists the artist, with the matching titles", async ({ page }) => {
    expect(await searchWithAlbums(page, "vesper")).toEqual({ "Björk": ["Vespertine"] });
    // Folded like the names: an unaccented query finds an accented title.
    expect(await searchWithAlbums(page, "etudes")).toEqual({ "Symmetry Studies": ["Études"] });
  });

  test("one query can match a name for one artist and a title for another", async ({ page }) => {
    // "symmetry" is Symmetry Studies' name and one of Muse's albums.
    expect(await searchWithAlbums(page, "symmetry"))
      .toEqual({ Muse: ["Origin of Symmetry"], "Symmetry Studies": [] });
  });

  test("an older server's rows, without albums, match on the name alone", async ({ page }) => {
    const older = WITH_ALBUMS.map(({ albums, ...rest }) => rest);
    expect(await searchWithAlbums(page, "vesper", older)).toEqual({});
    expect(await searchWithAlbums(page, "muse", older)).toEqual({ Muse: [] });
  });

  test("the list shows the matching album under the artist's name", async ({ page }) => {
    await page.goto("/#/library");
    await page.evaluate(() => window.Alpine.$data(document.querySelector("[x-data]")).goToTab("library"));
    await settled(page); // the tab's own artist load settles before the rows are replaced
    await page.evaluate((list) => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.artists = list;
      app.artistSearch = "vesper";
    }, WITH_ALBUMS);
    const row = page.locator("button", { hasText: "Björk" });
    await expect(row).toHaveCount(1);
    await expect(row).toContainText("Vespertine");
  });
});


// The filtered list is remembered for the last list, query and sort: the page
// reads it three times per render. A remembered list must never be a stale
// one, so the rendered list is checked after each input changes.
test.describe("the filtered artist list is remembered, never stale", () => {
  const LIST = [
    { artist: "Muse", track_count: 40, album_count: 4 },
    { artist: "Björk", track_count: 30, album_count: 3 },
    { artist: "Air", track_count: 10, album_count: 6 },
  ];
  const rows = (page) => page.locator("button span.truncate:not(.text-xs)").filter({ hasText: /^(Muse|Björk|Air|Bonobo)$/ });

  test.beforeEach(async ({ page }) => {
    await trackFetches(page);
    await page.goto("/#/library");
    await settled(page);
    await page.evaluate((list) => { window.Alpine.$data(document.querySelector("[x-data]")).artists = list; }, LIST);
  });

  test("the same inputs give the same list without filtering again", async ({ page }) => {
    const same = await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      return app.filteredArtists() === app.filteredArtists();
    });
    expect(same).toBe(true);
  });

  test("typing, sorting and a new list each show at once", async ({ page }) => {
    await expect(rows(page)).toHaveText(["Air", "Björk", "Muse"]);
    await page.getByPlaceholder("Search for an artist or album…").fill("mu");
    await expect(rows(page)).toHaveText(["Muse"]);
    await page.getByPlaceholder("Search for an artist or album…").fill("");
    await page.getByRole("button", { name: "Sort: name" }).click();
    await expect(rows(page)).toHaveText(["Air", "Muse", "Björk"]);
    await page.evaluate(() => {
      const app = window.Alpine.$data(document.querySelector("[x-data]"));
      app.artists = [...app.artists, { artist: "Bonobo", track_count: 9, album_count: 9 }];
    });
    await expect(rows(page)).toHaveText(["Bonobo", "Air", "Muse", "Björk"]);
  });
});
