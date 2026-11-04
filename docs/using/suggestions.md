<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Suggestions

The **Suggestions** tab proposes music to sync, drawn from three sources and
each filtered down to what's **actually in your library and not already synced
everywhere**:

- **Recently added** to the library.
- **Top-played** (from Last.fm, ListenBrainz and/or Maloja).
- **Recently-played** (from Last.fm, ListenBrainz and/or Maloja).

Suggestions work regardless of your library provider — including on
[Filesystem](../providers/filesystem.md) — because listening data comes from
Last.fm/ListenBrainz/Maloja, plays kept from your own devices while you have
that switched on, and your own library, not from the provider. With more than one set up, their suggestions
merge, one copy per album.

**Most played over** sets the period top-played draws on: the last 7 days,
month, 3 months, 6 months (the default), year, or all time. The home
**Most played** widget has the same control; the choice is remembered in your
browser and shared by both.

**Plays from your devices** are one of those sources, if you want them. Turn on
**Keep plays from my devices** on your Profile, and what your devices report feeds
your own Suggestions and Most played. With it off, stored plays stay stored but
aren't used, until you delete them. Nothing is kept unless you turn it on.

**When a source doesn't answer**, the tab says so above what the others found
("Last.fm didn't answer, so its most-played albums are missing below"), with
**Retry**. If nothing is left to show, the notice takes the place of the
empty grid, so an outage never reads as "nothing played". **Refresh** and
**Retry** also refresh the source dots at the top of the page.

What the services answer is kept for five minutes, so moving between tabs
doesn't wait on them each time; **Refresh** and **Retry** always ask them
again. A source that didn't answer isn't kept: the next look asks it again.
Which albums are already on your devices is worked out fresh every time.

## Reading and sending listening history

Trobar reads your listening history from these services. It sends to them only
if you ask it to, and only from your own devices' plays: keep plays from your
devices, then turn on **Also send my plays to** for Maloja, ListenBrainz or
Last.fm on your Profile. Last.fm needs **Connect Last.fm** first, once an admin
has set it up. Each is a switch of its own, off until you turn it on. It sends
plays from then on, once each, and never your older history. Turn off keeping
plays and sending stops too.

Each user can set a personal Last.fm key in their profile. A user without one
uses the admin's
[**Default Last.fm API key**](../administration.md#api-keys), and, if that
is empty too, the deployment's `LASTFM_API_KEY` environment variable.

## Pointing at self-hosted services

Two [Administration](../administration.md#listening-history-sources) fields let
you redirect the reads to a self-hosted alternative, live, no restart:

- **Last.fm API base URL** — **Libre.fm** is the closest drop-in (a genuinely
  Last.fm-API-compatible free-software alternative; test after switching).
  Don't point it at Maloja, whose Last.fm-compatible endpoints only accept
  scrobbles: Maloja has a source of its own, below.
- **ListenBrainz API base URL** — self-hosted ListenBrainz is the same software
  as the public instance, so just the URL changes.

Leave either blank to use the default (the real service, or an
`LASTFM_API_BASE` / `LISTENBRAINZ_API_BASE` env var if your deployment sets
one).

## Maloja

[Maloja](https://github.com/krateng/maloja) is a self-hosted scrobble server,
and a listening-history source of its own here, beside Last.fm and
ListenBrainz: its history feeds top-played and recently-played suggestions
and the most-played chart, alone or merged with the other two.

- **Set it up** on your **Profile**, under Scrobbler accounts: **Maloja URL**,
  the address of your Maloja including its port (42010 by default), e.g.
  `http://192.168.1.10:42010`. Maloja serves its history without an API key,
  so the URL is all it needs. It is per user, since one Maloja instance holds
  one person's history.
- **Which addresses work.** LAN addresses (`10.x`, `172.16`–`172.31`,
  `192.168.x`, and IPv6 `fc00::/7`) and public ones do. Any user of the
  server can enter one, so any user can make the server send requests into
  your LAN: worth knowing on an install shared by several households.
  Addresses that only the server itself can reach are refused: loopback
  (`127.x`, `::1`, `localhost`), link-local (`169.254.x`, `fe80::`, which
  includes cloud metadata services), the unspecified address (`0.0.0.0`,
  `::`) and multicast. That applies when the URL is saved, and again on
  every request, to the address actually connected to, so a hostname that
  later resolves to one of them stops working (a log line says which kind,
  without the URL). Redirects are not followed. If Maloja runs on the same
  machine as Trobar, use that machine's LAN address rather than
  `localhost`.
- **multi-scrobbler** setups work through it: multi-scrobbler relays plays
  from your players to Maloja (and elsewhere) and keeps no history itself, so
  point Trobar at the Maloja it feeds.
- **Matching.** Maloja carries no MusicBrainz ids, so albums match your
  library by artist and album name. Maloja splits artist credits
  ("Cinder & Ash" becomes "Ash" and "Cinder", "A feat. B" two artists);
  Trobar compares the set of names, so such an album still matches your
  library's tags.
- **Covers** are your library's own, as for the other sources.

Verified against Maloja **3.2.6**. Earlier versions have not been tested.
