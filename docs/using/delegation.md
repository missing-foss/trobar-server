<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Delegated Management

Delegation lets one user fully manage another's devices — rename, storage limit,
token regeneration, deletion, and synced content. It was built for the
parent-managing-a-kid's-device case.

- Grants are per **(manager, owner)** pair and independent, so two parents can
  each manage the same child's devices.
- The delegate additionally **pins** the devices they want visible in their own
  Devices list; the admin always sees every device regardless.

Delegations are created in the [Administration](../administration.md) panel.
