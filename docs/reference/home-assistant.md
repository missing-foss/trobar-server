<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Home Assistant Integration

The fifth Trobar repository:
[trobar-ha](https://github.com/missing-foss/trobar-ha). Not a fourth
client — it doesn't sync music anywhere. It's a **monitoring/automation
surface over the sync lifecycle**: per-device status inside Home Assistant,
and a base for notifications and automations on sync events. The
`media_player` model was deliberately not used: Trobar isn't a renderer, and
forcing that model would fight what the integration is actually for.

For the sync clients themselves — the things music actually lands on — see
[Clients](../clients/index.md).

## What it does

Adds via Home Assistant's normal config flow (server URL + a read-only
token), then polls the [Integration API](integration-api.md) and creates
one Home Assistant device per Trobar device, each with five sensors:
pending tracks, last synced, free space, total space, unknown tracks. Ships
in English and French.

Everything the integration does is a client of the
[Integration API](integration-api.md) — its endpoint, field shapes, null
handling, and rate limit are documented there in full, independent of
Home Assistant.

## Installation

Not yet in the HACS default store — add it as a HACS **custom repository**:

1. HACS → ⋮ → **Custom repositories**
2. URL `https://github.com/missing-foss/trobar-ha`, category **Integration**
3. Install, then restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Trobar**, and enter
   your server's URL and a token from **Profile → Integrations** in
   Trobar's web UI — that tab is only visible when logged in as an
   **admin**, since minting one requires it

## Requirements

- A Trobar server on **2.8.1 or later** (2.8.0 shipped the read-only
  integration API; 2.8.1 fixed a boolean-serialization bug in it).
- Home Assistant **2026.3.0 or later** — the integration ships its own
  brand images, which only take priority over the CDN fallback from that
  version onward.

## Source and issues

Source, releases (tag `vX.Y.Z`), and the issue tracker are all in
[trobar-ha](https://github.com/missing-foss/trobar-ha). Its own README and
`CONTRIBUTING.md` cover local development.
