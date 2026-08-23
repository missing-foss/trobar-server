<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Providers we don't support

Looking for Deezer, YouTube Music, Apple Music, Qobuz, or KKBOX? None of these
have a direct Trobar integration. This page says why, and — for the ones
where it's not a dead end — what would change that.

These are almost all limitations on their side, not choices on ours: a closed
developer portal, a partnership-only credential gate, or simply no API for
what we'd need. Each entry links to the research issue behind it, so the full
detail (live-tested, not guessed) is one click away.

!!! info "Looking for Spotify?"
    [Spotify](spotify.md) **is** supported — it has its own page. It's only
    pending live-payload verification from a Premium-account holder, which is
    a different thing entirely from the providers below.

## Deezer

Deezer's API is a clean fit — OAuth 2.0, `GET /user/me/playlists`, the same
shape as [Tidal](tidal.md)/Spotify. The blocker is that Deezer's developer
portal has **frozen new app registration**, with no announced timeline:
existing apps keep working, but nobody — Trobar included — can obtain a new
`app_id`/`app_secret` to stand one up. Other self-hosted projects worked
around this with ARL cookie auth (a bearer credential lifted from a browser
session); we ruled that out deliberately, since it violates Deezer's Terms of
Service and carries real account-ban risk for the person using it.

**What would change this:** Deezer reopens app registration. The technology
is ready the moment that happens —.

## YouTube Music

Won't build. There's no official API for the YouTube *Music* library
specifically — only the general YouTube Data API, which lists YouTube
playlists of videos ("Artist – Song (Official Video)"), not clean
artist/title/album data our matcher needs. The only route that reaches the
actual YT Music library is `ytmusicapi` and similar, which talk to YouTube
Music's internal, unofficial endpoints — reverse-engineered, not sanctioned,
regardless of how the session is authenticated. Access here is also
tightening rather than opening (Google removed YT Music's own OAuth support
in late 2024).

**What would change this:** nothing on the horizon — this is the one entry
on this page without a plausible reopening. See.

## Apple Music

Apple Music is the one service on this page that isn't blocked by a
technical or policy limit. The API exposes library playlists
(`GET /v1/me/library/playlists`), and the integration is buildable. What it
needs is an Apple Developer Program membership — a recurring $99/year cost —
plus signing your own developer tokens (an ES256 JWT, re-signed at least
every 6 months) and loading Apple's hosted MusicKit JS for the per-user
sign-in step. That's an ongoing cost and a maintenance burden the project
doesn't currently carry.

**What would change this:** if you already hold an Apple Developer
membership, the bring-your-own-credentials route is open — get in touch on
. It would
also get built if the project's own circumstances change later.

## Qobuz

Qobuz's API can read a user's own playlists, so this is a genuine capability
gap on the credential side, not the API. Unlike Deezer, there's no self-serve
developer portal at all — API access is granted only through a partner
relationship (email `api@qobuz.com` and wait). Other self-hosted apps get
around this by scraping an `app_id`/secret out of Qobuz's web-player
JavaScript bundle; we've deliberately gone the other way on this kind of
thing elsewhere in the project (removing GPL-incompatible bundled code,
the REUSE licensing push), so shipping scraped credentials that violate
Qobuz's Terms of Service isn't something we're willing to do by default.

**What would change this:** a partner relationship becoming available.

## KKBOX

The most clear-cut case: KKBOX's public API is catalog-only
(client-credentials), and has no resource that exposes a user's own
playlists at all. There's nothing to build against.

**What would change this:** nothing known — this is a hard technical block,
not a policy one, and it was investigated and closed on that basis.

## Reached indirectly through Roon

Qobuz and KKBOX are both streaming backends Roon itself supports. If your
library provider is [Roon](roon.md), playlists backed by either sync exactly
like any other Roon content — you just don't get a direct link or
source-attribution badge, since Roon's own abstraction is all Trobar ever
sees. See [Roon: streaming backends](roon.md#streaming-backends-tidal-qobuz-kkbox)
for how that works. Deezer, YouTube Music, and Apple Music have no such path
— Roon doesn't support them as backends either.
