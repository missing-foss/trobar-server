<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Roon

Roon gives Trobar playlists and a live pairing-status badge. Because Roon has no
API keys, the integration pairs like a remote control rather than
authenticating with a token you paste.

## The pairing dance

1. Enter the Core's host/port in the setup wizard (or later in
   [Administration](../administration.md#provider-connection)). The default Roon
 extension port is `9330`.
2. Open Roon on any control device: **Settings → Extensions**.
3. Trobar appears in the list — click **Enable**.
4. The web UI's header badge goes green when paired.

The pairing token survives Core IP changes — it's tied to the Core's identity,
not its address. Changing the host/port in Administration re-pairs immediately,
reusing the existing token.

If the extension never appears, or pairing keeps flapping, see
[Troubleshooting → Roon pairing](../troubleshooting.md#roon-pairing). The short
version: Trobar and the Core must see each other on the LAN (discovery uses
multicast), and only **one** Trobar deployment may point at a given Core —
Roon allows a single connection per extension identity, so a stray second copy
makes both lose the pairing.

## Streaming backends (Tidal, Qobuz, KKBOX)

Playlists backed by Tidal, Qobuz, or KKBOX — Roon's supported streaming
sources — sync exactly like playlists of local tracks. Trobar only ever walks
Roon's generic Browse hierarchy for each entry's name/artist/title, with no code
path specific to any one streaming service, so Roon's own abstraction over the
source is all that matters. Tracks resolve against your local catalog the same
way regardless of what's backing them in Roon; anything not in your library is
flagged and skipped.

This is also why **KKBOX** and **Qobuz** have no *direct* Trobar integration:

- **KKBOX** — to our knowledge its public Open API is catalog-only
  (client-credentials) and does not expose a user's own playlists, so a direct
  client would add no capability Trobar can use.
- **Qobuz** — its API *can* read a user's playlists, so attribution would be
  technically possible, but Qobuz has no self-serve developer portal: API
  credentials are partner-only, and the only self-serve route violates Qobuz's
  Terms of Use, which Trobar will not ship. A bring-your-own official partner
  credentials path is the only clean option.
