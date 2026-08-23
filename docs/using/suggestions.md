<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Suggestions

The **Suggestions** tab proposes music to sync, drawn from three sources and
each filtered down to what's **actually in your library and not already synced
everywhere**:

- **Recently added** to the library.
- **Top-played** (from Last.fm and/or ListenBrainz).
- **Recently-played** (from Last.fm and/or ListenBrainz).

Suggestions work regardless of your library provider — including on
[Filesystem](../providers/filesystem.md) — because listening data comes from
Last.fm/ListenBrainz and your own library, not from the provider.

## Listening history is read-only

Trobar **only ever reads** listening history — nothing here submits a scrobble
anywhere. Each user can set a personal Last.fm key in their profile; the admin
can set an app-wide fallback key (`LASTFM_API_KEY`).

## Pointing at self-hosted services

Two [Administration](../administration.md#listening-history-sources) fields let
you redirect the reads to a self-hosted alternative, live, no restart:

- **Last.fm API base URL** — **Libre.fm** is the closest drop-in (a genuinely
  Last.fm-API-compatible free-software alternative; test after switching). A
  plain **Maloja** instance will **not** work — its Last.fm-compatible endpoints
  accept scrobbles (write), they don't serve the read methods Trobar calls.
- **ListenBrainz API base URL** — self-hosted ListenBrainz is the same software
  as the public instance, so just the URL changes.

Leave either blank to use the default (the real service, or an
`LASTFM_API_BASE` / `LISTENBRAINZ_API_BASE` env var if your deployment sets
one).
