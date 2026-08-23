<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Trobar brand assets — "The Bard"

The Trobar mark: an original troubadour character mid-strum on a lute whose
rosette carries the "Side A" burgundy label. Chosen 2026-07-08
from a six-proposal identity round; artwork refined 2026-07-09 (instrument
geometry pivoted on the soundhole, simplified bard, new favicon reduction).
Original artwork, own copyright — licensed with the repo
(AGPL-3.0-or-later); not derived from any existing character.

SVG is the source of truth; everything scales from one file. The mark never
theme-inverts (same artwork in light and dark UI).

Palette: burgundy #A83250 (hi #E28AA0, lo #6E1A2E), rose #D76A83,
cream #F9EFDF, warm white #F2EDE1 (skin/hand), ink #17140E/#0E0C08,
canvas #100E08, warm grey #A9A08F/#5F584C, lute wood #8a7f6b.
Wordmark: Fredoka SemiBold, "Trob" in ink/cream + "ar" in rose #D76A83.

Files:

- `mark.svg` / `mark-on-dark.svg` — full-colour mark (transparent / on canvas)
- `mono-cream.svg` / `mono-on-burgundy.svg` — single-colour silhouette
- `favicon.svg` — cream tile + full bard (launcher-icon look, tighter
  12/108 inset; replaced the 16px lute-body reduction per)
- `android-adaptive-foreground.svg` — 108-grid cream tile + bard, content
  inside the 66/108 safe zone (launcher layers: bard foreground rendered
  transparent, cream tile as the background drawable)
- `android-adaptive-lute-only.svg` — adaptive variant, instrument only
- `lockup-horizontal-*.svg` / `lockup-stacked-*.svg` — icon + "Trobar" wordmark

Deployed copies (keep in sync with these sources):

- `app/static/img/bard-mark.svg` (web header + login/setup hero)
- `app/static/img/favicon.svg` (web favicon)
- `android/.../drawable-nodpi/logo_bard.png` (in-app logo, 512px render)
- `android/.../drawable-nodpi/ic_launcher_foreground_bard.png` (launcher, 432px render)

Sync-state animation ("notes from the soundhole"): musical notes spawn at the
lute's soundhole, rise ~0.4 x mark height with a slight horizontal drift,
fading in then out; ~2.5s loop, staggered copies. Implemented in CSS
(`app/static/css/logo.css`) and Compose (`MainActivity.kt` `AppLogo`).

Ready-to-upload GitHub exports live in `exports/`:
`bard-mark-512.png` (mark-on-dark, 512px raster of the bard) and
`social-preview-1280x640.png` (horizontal lockup on canvas, rendered with
the real Fredoka) — the social preview for the three Trobar repos.

The GitHub *org* avatar is NOT the bard: the org uses the Missing FOSS
design set (app-grid mark), hosted in the org's `.github` repo under
`profile/brand/`. The bard identifies Trobar; the grid identifies the org.
