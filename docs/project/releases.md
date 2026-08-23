<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Release Cycle & Versioning

Trobar is five components in five repositories, each **released
independently** with its own tag namespace:

| Component | Repo | Tag | Artifact |
|---|---|---|---|
| Server | [trobar-server](https://github.com/missing-foss/trobar-server) | `vX.Y.Z` | Source / Docker image |
| Android app | [trobar-android](https://github.com/missing-foss/trobar-android) | `vX.Y.Z` | Signed APK on the GitHub Release |
| Desktop app | [trobar-desktop](https://github.com/missing-foss/trobar-desktop) | `vX.Y.Z` | Linux / macOS / Windows builds on the GitHub Release |
| Garmin watch app | [trobar-garmin](https://github.com/missing-foss/trobar-garmin) | `vX.Y.Z` | `.prg` on the GitHub Release (sideload-only — no Connect IQ Store listing yet) |
| Home Assistant integration | [trobar-ha](https://github.com/missing-foss/trobar-ha) | `vX.Y.Z` | Source only — install via [HACS custom repository](../reference/home-assistant.md#installation) (no signed/compiled artifact) |

Versions follow semantic-versioning intent: a **major** bump signals
substantial new capability or a compatibility watershed, **minor** adds
features, **patch** fixes. The five components' numbers are **not** kept in
lockstep — each moves at its own pace.

## Compatibility

The device sync API is backward-compatible within a major line, so a slightly
older client keeps working against a newer server. Update clients at your own
pace — see [Upgrading](../operations/upgrading.md).

## Getting releases

- **Server** — `git pull && docker compose up -d --build`.
- **Android** — [Obtainium](https://github.com/ImranR98/Obtainium) auto-updates,
  or download the APK from Releases. APKs are signed with the maintainer's key;
  verify the certificate as described in the app's README.
- **Desktop** — download the platform build from Releases. Windows/macOS builds
  are currently unsigned.
- **Garmin** — sideload the `.prg` from Releases by copying it onto the watch
  over USB (see the [Garmin client docs](../clients/garmin.md) for the exact
  steps). No Connect IQ Store listing yet, so there's no in-watch
  auto-update path.
- **Home Assistant integration** — add as a HACS custom repository (not yet
  in the HACS default store) — see
  [Home Assistant Integration](../reference/home-assistant.md#installation)
  for the exact steps.
