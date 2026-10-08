# Trobar — brand specification

Version 2.0 · October 2026 · replaces proposal 1a ("the bard" figure)

## 1. The mark: Occitan rosette

The mark is the carved sound-hole rosette of a lute, read as a seal. A disc carries an eight-point star, built from two rounded squares at 0° and 45°. Twelve pommels ring the star, a nod to the twelve-pommel Occitan cross. A hub and centre dot sit in the middle.

The wordmark is unchanged: **Fredoka Bold**, "Trob" in the text colour and "ar" in the accent colour. All assets ship with the type outlined to paths, so no font is needed at runtime.

### Construction (200 × 200 grid, centre 100,100)

| Element | Geometry | Colour role |
|---|---|---|
| Disc | circle r 96 | `disc` |
| Pommels | 12 circles r 6, every 30°, on radius 80 | `star` |
| Star | two squares 88 × 88, corner radius 14, rotated 0° and 45° | `star` |
| Hub | circle r 30 | `disc` |
| Centre dot | circle r 13 | `dot` |

**Small variant** (`trobar-mark-small-*`, for 24 px and below): drops the pommels, uses a 96 star with corner radius 16, a hub of r 34 and a dot of r 16. Use it for favicons, notification icons and anything under 24 px, where the pommels would turn to noise.

**Monochrome** (`trobar-mark-mono.svg`, `ic_trobar_mark_mono.xml`, launcher `monochrome` layer): a single path with the star, pommels and dot knocked out, so the background shows through. Its fill is `currentColor`.

## 2. Lockups

| Lockup | Use | Construction |
|---|---|---|
| Horizontal | default: headers, about screen, README | mark height = 1.55 × cap height of "T"; gap = 0.28 × mark |
| Stacked | splash, square placements | mark = 2.2 × cap height, centred above; gap = 0.22 × mark |
| Wordmark only | where the mark is already visible nearby | — |

**Clear space:** keep a space of 0.25 × the mark's diameter on every side. Nothing else goes inside that area.

**Minimum sizes:** the full mark at 32 px or larger, the small variant from 16 to 31 px, and the horizontal lockup at 24 px tall or larger.

## 3. Themes

Each theme assigns six colour roles, plus a seventh role, `watch`, for the ground on a watch face. **A theme always recolours the mark and the wordmark together.** Never mix roles from different themes.

| Theme | Character | ground | disc | star | dot | text | accent | watch |
|---|---|---|---|---|---|---|---|---|
| **Troubadour** (`troubadour`) | Default · cream | `#F9EFDF` | `#A83250` | `#F9EFDF` | `#D76A83` | `#17140E` | `#C2506B` | `#100E08` |
| **Nuèch** (`nuech`) | Night · OLED | `#0E1220` | `#40569A` | `#E8C872` | `#E8C872` | `#F4EEDC` | `#E8C872` | `#0E1220` |
| **Garriga** (`garriga`) | Sage · olive | `#EEF0E4` | `#5E6B3A` | `#F4F1E2` | `#C9A227` | `#23281A` | `#8C6C12` | `#14170E` |
| **Pèira** (`peira`) | Stone · terracotta | `#E6E1D8` | `#3A3F47` | `#E6E1D8` | `#C0573E` | `#22252A` | `#B04A33` | `#16181B` |
| **Contraste** (`contraste`) | High contrast · MIP | `#000000` | `#FFFFFF` | `#000000` | `#FFD400` | `#FFFFFF` | `#FFD400` | `#000000` |

Troubadour is the default theme. Contraste uses only pure channel values, so it survives the 64-colour MIP palette (fēnix 5 Plus) unchanged. It is the recommended theme for MIP watches and for accessibility settings.

Nuèch's disc was lifted from the proof value `#2B3A67` to `#40569A`, because the original almost vanished on near-black.

### Contrast (WCAG ratio against the theme's ground)

| Theme | "Trob" | "ar" | disc edge | star on disc |
|---|---|---|---|---|
| Troubadour | 16.1:1 | 4.0:1 | 5.7:1 | 5.7:1 |
| Nuèch | 16.1:1 | 11.5:1 | 2.7:1 | 4.3:1 |
| Garriga | 13.1:1 | 4.3:1 | 5.0:1 | 5.1:1 |
| Pèira | 11.8:1 | 4.2:1 | 8.1:1 | 8.1:1 |
| Contraste | 21.0:1 | 14.7:1 | 21.0:1 | 21.0:1 |

"ar" is only ever set at display sizes (24 px or larger), where 3:1 is the threshold, and every theme passes. The disc edge on Nuèch is deliberately soft: on that theme the gold star and pommels carry the silhouette.

## 4. Theming rules per platform

- **Inside the Android app**, the mark, small mark, wordmark and lockup drawables read `?attr/trobarDisc`, `trobarStar`, `trobarDot`, `trobarText`, `trobarAccent` and `trobarGround`. Switching the theme overlay recolours them all. One drawable serves all five themes (attribute colours in `fillColor` need API 24+).
- **Android launcher icon.** Launchers cannot see app themes, so each theme has its own fixed-colour adaptive icon. The app switches between them with `activity-alias`. This is optional and off by default; see HANDOVER.md for the caveats.
- **Garmin launcher icon.** This is fixed at build time and cannot change at runtime. Ship Troubadour (or Contraste for MIP-only builds). If the watch app shows the mark on its own screens, it can draw any theme's bitmap.
- **Web and docs.** Set `data-theme` on `<html>` with `tokens/trobar-themes.css`, and inline `trobar-mark-themable.svg`, which reads the `--tb-*` variables.

## 5. Sync state icons

These are derived from the mark and exist for every theme in `svg/states/<theme>/`.

| State | Treatment |
|---|---|
| idle | full mark at 50 % opacity |
| syncing | the star rotates 90° every 2.4 s (eased). Disc, pommels and hub stay still. The motion is disabled under `prefers-reduced-motion` |
| success | a badge bottom-right in the `dot` colour with a check mark in the `ground` colour |
| error | a badge bottom-right in the `text` colour with an "!" in the `ground` colour |

On Android, rebuild "syncing" as an `AnimatedVectorDrawable` that rotates the two star paths around 100,100, and respect the system's animator duration scale. Simple flat assets don't need state-specific PNGs.

## 6. Don't

- Don't add outlines, drop shadows, gradients or bevels. The old kit's drop shadow and dark outlines were a large part of why it felt dated.
- Don't rotate the whole mark or squash it. Only the star rotates, and only in the syncing state.
- Don't recolour individual roles outside the five themes.
- Don't place the full-colour mark on a busy photo. Use the mono version.
- Don't re-typeset the wordmark from a live font. Use the outlined assets.

## 7. Typography

Fredoka 600/700 is for display use and the wordmark. Space Grotesk is for UI and supporting text. Both are licensed under the SIL OFL 1.1 (see `licenses/`).
