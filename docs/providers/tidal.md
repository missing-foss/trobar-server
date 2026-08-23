<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Tidal

Unlike the library provider, Tidal is **not** a shared, admin-configured
source — each household member links their own personal Tidal account from their
own **Profile → Streaming accounts** tab ("Connect Tidal"). Their playlists
merge into the shared pool alongside whatever the library provider syncs. The
admin's only job is to register the OAuth app once so those per-user logins have
something to authenticate against.

## Admin: register the OAuth app (once)

1. Register an app at [developer.tidal.com](https://developer.tidal.com).
2. Set the redirect URI to `https://<your-domain>/profile/tidal/callback` — it
   must match **exactly**, no trailing slash.
3. **Enable all three of these permissions for the app in the developer
   console.** These are separate toggles from the OAuth scopes Trobar sends in
   its own request (the console lists them by these same names):
 - `user.read`
 - `collection.read`
 - `playlists.read`
4. Enter the **Client ID** and **Client secret** under
   [Administration → Configuration](../administration.md).

!!! warning "The permissions step is easy to miss"
    Registering the app and entering the client ID/secret + redirect URI is
    **not** sufficient on its own. Missing any of the three permissions fails at
 Tidal's own `login.tidal.com` screen with a generic, unhelpful error, and
 the request never reaches Trobar's `/profile/tidal/callback` — so there's
    nothing to debug on this side. If a member's "Connect Tidal" bounces off
    Tidal's login page, check these permissions first.

## Members: link your account

Each household member connects their own account from their own **Profile →
Streaming accounts** tab. Playlists then merge into the shared pool; per-playlist
ownership/sharing controls keep a personal playlist private if you want — see
[Playlists](../using/playlists.md).
