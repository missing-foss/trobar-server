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
Last.fm/ListenBrainz/Maloja and your own library, not from the provider. With
more than one set up, their suggestions merge, one copy per album.

## Listening history is read-only

Trobar **only ever reads** listening history — nothing here submits a scrobble
anywhere. Each user can set a personal Last.fm key in their profile; the admin
can set an app-wide fallback key (`LASTFM_API_KEY`).

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
