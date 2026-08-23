<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The Administration panel

**Profile → Administration.** Only the `ADMIN_USERNAME` account sees it;
everything else in the app (own profile, own devices, own selections) is
per-user and needs no admin.

This page is the admin-panel reference. Related admin-managed topics have their
own pages: [Devices & Storage](using/devices.md),
[Delegated Management](using/delegation.md), and the per-provider setup under
[Providers](providers/index.md).

## Provider connection

The active provider (Roon / Jellyfin / Emby / Plex / LMS / Subsonic / Filesystem) and its connection
details are editable here, **live — no container restart**. Changing the Roon
host/port re-pairs immediately, reusing the existing pairing token. Switching to
a *different* provider clears playlists and the artist-image cache (they belong
to the provider that produced them); library, selections, users, and devices are
untouched. Per-provider details: [Providers](providers/index.md).

The **iTunes/Apple Music Library.xml path** field here is the one exception to
"one provider at a time" — it's an optional local playlist source layered on
top of the `.m3u`/`.m3u8` discovery every provider already gets, editable and
effective regardless of which provider is active. See
[Filesystem](providers/filesystem.md#what-you-get).

## API keys

- **Default Last.fm API key** — used for suggestions and listening stats for any
  user who hasn't set a personal key in their own profile.
- **TheAudioDB API key (artist images)** — when set, artist pictures are fetched
  from [TheAudioDB](https://www.theaudiodb.com) (whose API terms are written for
  exactly this use) instead of the active provider, with the provider and an
 `artist.jpg`-style folder image as fallbacks. Get a free key from their site;
  entering or changing it clears the image cache so everything re-fetches. Leave
  it empty to keep provider-sourced images — fine for private use, but recommended
  to set for anything public-facing, since provider imagery (Roon especially) is
  licensed for display inside that provider's own products.

## Streaming accounts (OAuth registration)

Personal streaming accounts are linked per-user, but the admin registers each
OAuth app **once** here under **Configuration** so those logins have something to
authenticate against:

- **[Tidal](providers/tidal.md)** — client ID/secret; three developer-console
  permissions must be enabled or the login fails at Tidal's own screen.
- **[Spotify](providers/spotify.md)** — client ID/secret (**validation
  pending**; Dev-Mode Premium + 5-user limits apply).

Each household member then connects their own account from **Profile → Streaming
accounts**.

## Listening history sources

Suggestions (top-played / recently-played) read from Last.fm and/or
ListenBrainz — **read-only**, nothing here ever submits a scrobble. Both default
to the real services; two admin fields point them at a self-hosted alternative
instead, live, no restart:

- **Last.fm API base URL** — overrides where Last.fm reads go. **Libre.fm** is
  the closest drop-in (a genuinely Last.fm-API-compatible free-software
  alternative; test after switching, as not every method is independently
  verified). A plain **Maloja** instance will **not** work — its
  Last.fm-compatible endpoints *accept* scrobbles (write), they don't serve the
  read methods this app calls.
- **ListenBrainz API base URL** — self-hosted ListenBrainz is the same software
  as the public instance, so just the URL changes.

Leave either blank to use the default (the real service, or the
`LASTFM_API_BASE` / `LISTENBRAINZ_API_BASE` env var if set). See
[Suggestions](using/suggestions.md).

## Users

- `local` mode: create household accounts here (username + password).
- `oidc` / `forward` modes: accounts appear automatically at first login; this
  panel gives the overview and lets you provision extra local-only accounts
  alongside.
- Deleting a user who still owns devices or selections fails with a clear
  message — reassign or remove those first.

!!! tip "Locked out of the admin account?"
    Lost the admin's local password, or need to provision one without a
 browser? Run `flask --app main.py create-admin <username>` inside the
    container — it creates the account as admin, or, if the account already
    exists, grants admin and resets its password. Prompts for the password if
 `--password` isn't given.
