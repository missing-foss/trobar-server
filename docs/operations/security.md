<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Security & Threat Model

Trobar is a **self-hosted, household-scale** application. The authoritative,
versioned threat model lives in
[`SECURITY.md`](https://github.com/missing-foss/trobar-server/blob/main/SECURITY.md)
at the repo root (it also powers GitHub's private vulnerability reporting). This
page is a short orientation and points there for the detail.

## The essentials

- **Run behind a TLS-terminating reverse proxy.** The app speaks plain HTTP on
  its container port; the proxy provides HTTPS. **Never expose the container
  port directly.** See [Networking & Reverse Proxy](networking.md).
- **Treat `DATA_DIR` like a password store.** It holds plaintext provider
  credentials and the session key. Back it up encrypted — see
  [Backups & DATA_DIR](backups.md).
- **Set `ADMIN_USERNAME` before first launch.** In `local` mode it locks
  first-run admin creation to that username, so a stranger can't claim admin on
  a fresh instance. See [Authentication Modes](../getting-started/authentication.md).
- **`forward` mode trusts the proxy completely.** The app does no auth of its
 own; the proxy must gate every request, and `FORWARD_AUTH_SECRET` should be
  set so a directly-exposed port fails closed.

## No telemetry, no CDN

Nothing phones home and every web asset is served from your own instance. The
only outbound requests are the integrations you configure (your provider,
Last.fm/ListenBrainz, TheAudioDB) — plus Gravatar avatars for SSO users who
haven't uploaded a picture, the one documented exception.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** ("Report a vulnerability" under
the repository's Security tab) or email **missing_foss@etik.com**. Please don't
open a public issue for anything exploitable until it's been addressed.
