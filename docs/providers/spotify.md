<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Spotify

!!! warning "Experimental — off by default"
    The Spotify provider is implemented and unit-tested, but **not yet confirmed
    end-to-end against a live account** — verifying it needs a Spotify
    **Premium** account (see the limits below), which we don't have in-house.
    Because of that, it sits behind an **Enable Spotify** toggle in
    [Administration → Configuration](../administration.md) — off by default for
    a new install, and left on automatically for an existing one that already
    has Spotify credentials configured (upgrading never silently disconnects a
    working setup). While it's off, the whole feature is hidden from Profile
    and the connect/callback/disconnect routes all refuse. If you turn it on
    and try it, please share sanitized API payloads — or just a comment saying
    a sync worked — on
 [](https://github.com/missing-foss/trobar-server/issues/146) so
    the response shapes can be confirmed and this warning can come down.

Same per-user model as [Tidal](tidal.md): each member connects their own account
from **Profile → Streaming accounts** ("Connect Spotify"); the admin registers
the OAuth app once and enables the feature.

## Admin: register the OAuth app (once)

1. Register an app at [developer.spotify.com](https://developer.spotify.com).
2. Set the redirect URI to `https://<your-domain>/profile/spotify/callback` — it
   must match **exactly**.
3. Enter the **Client ID** and **Client secret**, and check **Enable Spotify**,
   under [Administration → Configuration](../administration.md). Trobar requests
 the read-only `playlist-read-private` and `playlist-read-collaborative`
   scopes.

## Spotify Dev-Mode limits

These bite self-hosters, and neither is Trobar's to lift:

- The person who **registers** the app needs Spotify **Premium** for it to
  function at all (the members who link their accounts do not).
- A Dev-Mode app is capped at **5 linked users**. Raising it needs Spotify's
  Extended Quota, which is not attainable for a self-hosted app.
