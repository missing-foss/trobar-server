#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes app/static/css/brand-themes.css from brand/tokens/trobar-themes.json:
each theme's six brand roles as --tb-* (read by the inlined mark and the
wordmark), and its whole-UI palette as the --c-* variables app.css and the
Tailwind build consume, for every variant the theme has.

    python3 dev/gen_brand_css.py          # rewrite the CSS
    python3 dev/gen_brand_css.py --check  # dev/verify.sh: stale or failing = exit 1

--check also measures every text/background and UI pair below against WCAG
AA and fails on any pair under its minimum, so a palette that would ship
unreadable text cannot pass verify. One set of derivation rules serves every
theme; no colour here is picked by hand except Contraste's error red and the
pure black/white it is built from.

The same table drives trobar-android and trobar-desktop (trobar-server#141).

It also writes the documentation site's colours, docs/stylesheets/brand.css
(Troubadour for Material's light scheme, Nuèch for its dark one), and copies
the header marks and favicon into docs/assets/brand/, since MkDocs serves
only docs/. --check covers those files and the docs' own pairs too.
"""
import colorsys
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKENS = ROOT / "brand/tokens/trobar-themes.json"
OUT = ROOT / "app/static/css/brand-themes.css"
APP_CSS = ROOT / "app/static/css/app.css"
DOCS_CSS = ROOT / "docs/stylesheets/brand.css"
DOCS_ASSETS = ROOT / "docs/assets/brand"
# Copied as they are. The header mark renders at 24 px, the small variant's
# range (BRAND.md: the full mark from 32 px).
DOCS_ASSET_SOURCES = {
    "trobar-mark-small-troubadour.svg": ROOT / "brand/svg/mark/trobar-mark-small-troubadour.svg",
    "trobar-mark-small-nuech.svg": ROOT / "brand/svg/mark/trobar-mark-small-nuech.svg",
    "favicon.svg": ROOT / "brand/web/favicon.svg",
}

WHITE, BLACK = "#FFFFFF", "#000000"
ERROR_LIGHT = "#B3261E"  # the light error red the UI had before themes
ERROR_DARK_HUE = "#E2586E"  # the dark one; lifted below, as it fell under 4.5:1 on the darker panels
CONTRASTE_YELLOW = "#FFD400"
CONTRASTE_ERROR = "#FF6B6B"
DARK_ONLY = ("nuech", "contraste")


# --- colour arithmetic -------------------------------------------------------

def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def hexc(c):
    return "#" + "".join(f"{round(max(0.0, min(1.0, v)) * 255):02X}" for v in c)


def mix(a, b, t):
    """a moved a fraction t of the way to b."""
    return hexc(tuple(x + (y - x) * t for x, y in zip(rgb(a), rgb(b))))


def at_lightness(h, light):
    r, g, b = rgb(h)
    hue, _, sat = colorsys.rgb_to_hls(r, g, b)
    return hexc(colorsys.hls_to_rgb(hue, light, sat))


def alpha(h, a):
    r, g, b = (round(v * 255) for v in rgb(h))
    return f"rgba({r},{g},{b},{a})"


def over(fg, a, bg):
    """fg at opacity a composited over bg, as the browser paints it."""
    return mix(bg, fg, a)


def luminance(h):
    def channel(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def neutral_with_white_at(a, b, target):
    """The colour between a and b, closest to a, on which white text reaches
    the target contrast: a button background that carries white labels."""
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if ratio(WHITE, mix(a, b, mid)) >= target:
            hi = mid
        else:
            lo = mid
    return mix(a, b, hi)


# --- the derivation (the table in trobar-server#141) --------------------------

def light_roles(t):
    return {
        "background": t["ground"], "surface": WHITE,
        "surfaceVariant": mix(t["ground"], t["text"], 0.06),
        "onSurface": t["text"], "onSurfaceVariant": mix(t["text"], t["ground"], 0.30),
        "primary": t["disc"], "onPrimary": t["star"],
        "primaryContainer": mix(t["disc"], WHITE, 0.85), "onPrimaryContainer": mix(t["disc"], BLACK, 0.6),
        "accent": t["accent"], "tertiary": t["dot"],
        "outline": mix(t["text"], t["ground"], 0.50), "error": ERROR_LIGHT, "onError": WHITE,
    }


def dark_roles(t, key):
    night = key == "nuech"
    base = t["watch"]
    ink = t["text"] if night else t["ground"]
    return {
        "background": base, "surface": mix(base, ink, 0.08), "surfaceVariant": mix(base, ink, 0.14),
        "onSurface": ink, "onSurfaceVariant": mix(ink, base, 0.30),
        "primary": t["star"] if night else at_lightness(t["disc"], 0.72),
        "onPrimary": t["ground"] if night else mix(t["disc"], BLACK, 0.7),
        "primaryContainer": t["disc"] if night else mix(t["disc"], BLACK, 0.35),
        "onPrimaryContainer": mix(t["disc"], WHITE, 0.82),
        "accent": t["accent"] if night else at_lightness(t["accent"], 0.70), "tertiary": t["dot"],
        "outline": mix(ink, base, 0.50), "error": at_lightness(ERROR_DARK_HUE, 0.72), "onError": BLACK,
    }


def contraste_roles():
    return {
        "background": BLACK, "surface": BLACK, "surfaceVariant": BLACK,
        "onSurface": WHITE, "onSurfaceVariant": WHITE,
        "primary": CONTRASTE_YELLOW, "onPrimary": BLACK,
        "primaryContainer": BLACK, "onPrimaryContainer": CONTRASTE_YELLOW,
        "accent": CONTRASTE_YELLOW, "tertiary": CONTRASTE_YELLOW,
        "outline": WHITE, "error": CONTRASTE_ERROR, "onError": BLACK,
    }


def variants(themes):
    """(theme key, light|dark, roles, theme tokens) for every variant shipped."""
    out = []
    for key, t in themes.items():
        if key == "contraste":
            out.append((key, "dark", contraste_roles(), t))
            continue
        if key not in DARK_ONLY:
            out.append((key, "light", light_roles(t), t))
        out.append((key, "dark", dark_roles(t, key), t))
    return out


# --- the roles as app.css's variables -----------------------------------------

def web_vars(key, mode, r, t):
    """app.css's --c-* names, from the roles. Status (green/yellow/amber) and
    chart colours are not themed: they mean the same in every theme and stay
    in app.css per light/dark."""
    light = mode == "light"
    bg, panel, ink, muted = r["background"], r["surface"], r["onSurface"], r["onSurfaceVariant"]
    if key == "contraste":
        grays = {50: BLACK, 100: "#262626", 200: WHITE, 300: WHITE,
                 400: WHITE, 500: WHITE, 600: WHITE, 700: WHITE, 800: WHITE, 900: WHITE}
    else:
        # 50-100 backgrounds and hovers, 200-300 borders, 400-600 secondary
        # text (pinned to onSurfaceVariant and darker), 700-900 strong text.
        grays = {
            50: mix(bg, ink, 0.03 if light else 0.00), 100: mix(bg, ink, 0.07), 200: mix(bg, ink, 0.12),
            300: mix(bg, ink, 0.20),
            400: muted, 500: mix(muted, ink, 0.05), 600: mix(muted, ink, 0.20),
            700: mix(muted, ink, 0.55), 800: mix(muted, ink, 0.80), 900: ink,
        }
    if key == "contraste":
        primary_far = WHITE
    else:
        primary_far = mix(r["primary"], BLACK, 0.35) if light else mix(r["primary"], WHITE, 0.35)
    if key == "contraste":
        # Yellow buttons with black labels, white on hover: a grey button on
        # black would barely show, and white labels fit neither yellow nor white.
        metal = (CONTRASTE_YELLOW, WHITE, CONTRASTE_YELLOW)
    else:
        # From the light end toward the dark one in both modes: the lightest
        # neutral that still carries white labels, so the button stands out
        # from a dark page as well as a light one.
        light_end, dark_end = (t["ground"], t["text"]) if light else (ink, bg)
        metal = tuple(neutral_with_white_at(light_end, dark_end, target) for target in (4.7, 5.5, 7.0))
    v = {
        "--c-canvas-grad": f"radial-gradient(120% 90% at 50% -10%, {mix(bg, panel, 0.5)} 0%, {bg} 55%, {mix(bg, ink, 0.03)} 100%)",
        "--c-panel": panel, "--c-panel-b": mix(panel, bg, 0.5),
        # The metal colours are button backgrounds under --c-on-metal labels
        # (bg-metal text-on-metal hover:bg-metal-hi), so they are set by the
        # contrast the label needs on them, between the theme's ground and
        # its text colour.
        "--c-metal": metal[0], "--c-metal-hi": metal[1], "--c-metal-lo": metal[2],
        "--c-on-metal": BLACK if key == "contraste" else WHITE,
        "--c-accent": r["accent"], "--c-pill-active-fg": r["primary"],
        # Primary buttons (bg-indigo-600 / bg-burgundy, label text-on-primary)
        # take the scheme's primary, as in the table in trobar-server#141;
        # their hover and the indigo-700 text colour push it away from the
        # panel: darker in light mode, lighter in dark.
        "--c-burgundy": r["primary"],
        "--c-burgundy-hi": r["accent"] if light else mix(r["primary"], WHITE, 0.2),
        "--c-burgundy-lo": primary_far,
        "--c-on-primary": r["onPrimary"],
        "--c-indigo-50": alpha(r["accent"], 0.10 if light else 0.14),
        "--c-indigo-300": alpha(r["accent"], 0.35 if light else 0.45),
        "--c-indigo-400": r["accent"], "--c-indigo-500": r["accent"],
        "--c-indigo-600": r["primary"],
        "--c-indigo-700": primary_far,
        "--c-red-50": alpha(r["error"], 0.08 if light else 0.14),
        "--c-red-500": r["error"], "--c-red-600": r["error"],
        "--c-selection-bg": alpha(r["accent"], 0.25 if light else 0.35),
        "--c-selection-fg": WHITE if light else ink,
    }
    v.update({f"--c-gray-{n}": c for n, c in grays.items()})
    return v


# --- what must stay readable ---------------------------------------------------

def pairs(key, mode, r, v):
    """(what, foreground, background, minimum)."""
    light = mode == "light"
    panel, bg = v["--c-panel"], r["background"]
    pill = over(r["accent"], 0.10 if light else 0.14, panel)
    out = [
        ("body text (gray-900) on panel", v["--c-gray-900"], panel, 4.5),
        ("body text (gray-900) on canvas", v["--c-gray-900"], bg, 4.5),
        ("secondary text (gray-400) on panel", v["--c-gray-400"], panel, 4.5),
        ("secondary text (gray-400) on canvas", v["--c-gray-400"], bg, 4.5),
        ("secondary text (gray-500) on panel", v["--c-gray-500"], panel, 4.5),
        ("secondary text (gray-600) on panel", v["--c-gray-600"], panel, 4.5),
        ("secondary text (gray-400) on panel-b", v["--c-gray-400"], v["--c-panel-b"], 4.5),
        ("accent text on panel", r["accent"], panel, 4.5),
        ("selected pill text on its pill", v["--c-pill-active-fg"], pill, 4.5),
        ("error text on panel", r["error"], panel, 4.5),
        ("primary button label", v["--c-on-primary"], v["--c-indigo-600"], 4.5),
        ("primary button label (hover)", v["--c-on-primary"], v["--c-indigo-700"], 4.5),
        ("indigo-500 text on panel", v["--c-indigo-500"], panel, 4.5),
        ("indigo-600 link text on panel", v["--c-indigo-600"], panel, 4.5),
        ("indigo-700 text on panel", v["--c-indigo-700"], panel, 4.5),
        ("indigo-700 text on its selected pill", v["--c-indigo-700"], pill, 4.5),
        ("gray-700 text on panel", v["--c-gray-700"], panel, 4.5),
        ("gray-800 text on panel", v["--c-gray-800"], panel, 4.5),
        ("red-500 text on panel", v["--c-red-500"], panel, 4.5),
        ("text on primary container", r["onPrimaryContainer"], r["primaryContainer"], 4.5),
        ("outline on panel", r["outline"], panel, 3.0),
        ("button label on metal", v["--c-on-metal"], v["--c-metal"], 4.5),
        ("button label on metal (hover)", v["--c-on-metal"], v["--c-metal-hi"], 4.5),
        ("metal button against its panel (WCAG 1.4.11)", v["--c-metal"], panel, 3.0),
        # The sync toast is bg-gray-900: dark in light mode, near-white in dark
        # mode, as the grey scale inverts. Its text is gray-50, which inverts with it.
        ("sync toast text (gray-50) on gray-900", v["--c-gray-50"], v["--c-gray-900"], 4.5),
    ]
    return out


# --- output --------------------------------------------------------------------

HEADER = """/*
 * SPDX-FileCopyrightText: 2026 missing-foss
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
/* GENERATED by dev/gen_brand_css.py from brand/tokens/trobar-themes.json.
   Do not edit by hand: change the tokens or the rules there, and rerun it.
   dev/verify.sh fails when this file is stale or any pair misses WCAG AA.

   data-brand on <html> names the theme (default troubadour); data-theme is
   the effective light/dark. Nuèch and Contraste are dark only: the head
   script sets data-theme="dark" for them. */
"""


def render():
    data = json.loads(TOKENS.read_text())
    themes = data["themes"]
    lines = [HEADER]
    lines.append("/* --- the brand roles: the mark and the wordmark --- */")
    for key, t in themes.items():
        sel = f':root[data-brand="{key}"]'
        if key == data["default"]:
            sel = ":root, " + sel
        roles = "".join(f"--tb-{role}:{t[role]};" for role in ("ground", "disc", "star", "dot", "text", "accent", "watch"))
        lines.append(f"{sel}{{{roles}}}")
    lines.append("")
    lines.append("/* --- the whole-UI palette, per theme and variant --- */")
    for key, mode, r, t in variants(themes):
        v = web_vars(key, mode, r, t)
        sel = f':root[data-brand="{key}"][data-theme="{mode}"]'
        if key == data["default"]:
            # Before the head script runs, or with no data-brand at all.
            sel = f':root:not([data-brand])[data-theme="{mode}"], ' + sel
        body = "\n".join(f"  {k}:{val};" for k, val in v.items())
        lines.append(f"{sel}{{\n{body}\n}}")
    return "\n".join(lines) + "\n"


# --- the documentation site (mkdocs-material) ---------------------------------

DOCS_SCHEMES = (("default", "troubadour", "light"), ("slate", "nuech", "dark"))


def docs_vars(key, mode, r, t):
    """Material's colour variables for one scheme, from the same roles as
    the app. The header is a quiet surface with body-coloured text rather
    than the disc: the mark's own disc is that colour, and would vanish on
    it. Links are the primary role, which passes 4.5:1 on the page in both
    schemes; Troubadour's accent does not, so it is not used for text. The
    header sits one step off the page in both schemes, so it reads as a bar
    before the page scrolls under it."""
    light = mode == "light"
    bg, ink, muted = r["background"], r["onSurface"], r["onSurfaceVariant"]
    header = r["surfaceVariant"] if light else r["surface"]
    link = r["primary"]
    hover = mix(link, BLACK, 0.35) if light else mix(link, WHITE, 0.35)
    code_bg = r["surfaceVariant"]
    footer_bg, footer_fg = (ink, bg) if light else (r["surface"], ink)
    return {
        "--md-default-fg-color": ink,
        "--md-default-fg-color--light": muted,
        "--md-default-fg-color--lighter": alpha(ink, 0.32),
        "--md-default-fg-color--lightest": alpha(ink, 0.12),
        "--md-default-bg-color": bg,
        "--md-default-bg-color--light": alpha(bg, 0.7),
        "--md-default-bg-color--lighter": alpha(bg, 0.3),
        "--md-default-bg-color--lightest": alpha(bg, 0.12),
        "--md-primary-fg-color": header,
        "--md-primary-fg-color--light": mix(header, ink, 0.08),
        "--md-primary-fg-color--dark": mix(header, ink, 0.04),
        "--md-primary-bg-color": ink,
        "--md-primary-bg-color--light": muted,
        "--md-accent-fg-color": hover,
        "--md-accent-fg-color--transparent": alpha(hover, 0.1),
        "--md-accent-bg-color": bg,
        "--md-accent-bg-color--light": alpha(bg, 0.7),
        "--md-typeset-a-color": link,
        "--md-code-bg-color": code_bg,
        "--md-code-fg-color": ink,
        "--md-admonition-bg-color": bg,
        "--md-admonition-fg-color": ink,
        "--md-footer-bg-color": footer_bg,
        "--md-footer-bg-color--dark": mix(footer_bg, BLACK if light else WHITE, 0.15),
        "--md-footer-fg-color": footer_fg,
        "--md-footer-fg-color--light": mix(footer_fg, footer_bg, 0.25),
        "--md-footer-fg-color--lighter": mix(footer_fg, footer_bg, 0.45),
    }


def docs_pairs(v):
    return [
        ("docs: body text on page", v["--md-default-fg-color"], v["--md-default-bg-color"], 4.5),
        ("docs: secondary text (nav) on page", v["--md-default-fg-color--light"], v["--md-default-bg-color"], 4.5),
        ("docs: link on page", v["--md-typeset-a-color"], v["--md-default-bg-color"], 4.5),
        ("docs: link (hover) on page", v["--md-accent-fg-color"], v["--md-default-bg-color"], 4.5),
        ("docs: header text on header", v["--md-primary-bg-color"], v["--md-primary-fg-color"], 4.5),
        ("docs: header secondary text on header", v["--md-primary-bg-color--light"], v["--md-primary-fg-color"], 4.5),
        ("docs: code on code background", v["--md-code-fg-color"], v["--md-code-bg-color"], 4.5),
        ("docs: link on code background", v["--md-typeset-a-color"], v["--md-code-bg-color"], 4.5),
        ("docs: footer text on footer", v["--md-footer-fg-color"], v["--md-footer-bg-color"], 4.5),
        ("docs: footer secondary text on footer", v["--md-footer-fg-color--light"], v["--md-footer-bg-color"], 4.5),
    ]


def docs_variants():
    themes = json.loads(TOKENS.read_text())["themes"]
    by = {(k, m): (r, t) for k, m, r, t in variants(themes)}
    for scheme, key, mode in DOCS_SCHEMES:
        r, t = by[(key, mode)]
        yield scheme, key, mode, docs_vars(key, mode, r, t)


DOCS_HEADER = """/*
 * SPDX-FileCopyrightText: 2026 missing-foss
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
/* GENERATED by dev/gen_brand_css.py from brand/tokens/trobar-themes.json.
   Do not edit by hand. The documentation site's colours: Troubadour for
   Material's light scheme ("default"), Nuèch for its dark one ("slate").
   dev/verify.sh fails when this file is stale or a pair misses WCAG AA. */
"""

# A theme recolours the mark (BRAND.md), but Material's logo is one <img>:
# in the dark scheme it is hidden and the Nuèch mark drawn in its place.
DOCS_LOGO = """
/* --- the mark: Troubadour's is theme.logo; Nuèch's replaces it in slate --- */
[data-md-color-scheme="slate"] .md-logo img { opacity: 0; }
[data-md-color-scheme="slate"] .md-logo {
  background: url("../assets/brand/trobar-mark-small-nuech.svg") no-repeat center / 1.2rem 1.2rem;
}
"""


def render_docs():
    lines = [DOCS_HEADER]
    for scheme, key, mode, v in docs_variants():
        body = "\n".join(f"  {k}:{val};" for k, val in v.items())
        lines.append(f'/* {key}, {mode} */\n[data-md-color-scheme="{scheme}"] {{\n{body}\n}}')
    return "\n".join(lines) + DOCS_LOGO


def status_colours():
    """app.css's untouched status text colours, per light/dark: they sit on
    every theme's panels, so they are checked against each one too."""
    import re
    css = APP_CSS.read_text()
    out = {}
    for mode in ("light", "dark"):
        block = re.search(r':root\[data-theme="%s"\]\{(.*?)\}' % mode, css, re.S).group(1)
        out[mode] = {name: val for name, val in re.findall(r"--c-((?:green|yellow|amber)-[0-9]+):(#[0-9a-fA-F]{6});", block)}
    return out


def check_contrast():
    data = json.loads(TOKENS.read_text())
    status = status_colours()
    failures, rows = [], []
    for key, mode, r, t in variants(data["themes"]):
        v = web_vars(key, mode, r, t)
        extra = [(f"status text ({name}) on panel", val, v["--c-panel"], 4.5) for name, val in sorted(status[mode].items())]
        for what, fg, bg, minimum in pairs(key, mode, r, v) + extra:
            got = ratio(fg, bg)
            rows.append((key, mode, what, got, minimum))
            if got < minimum:
                failures.append(f"{key} {mode}: {what} is {got:.2f}:1, needs {minimum}:1 ({fg} on {bg})")
    for scheme, key, mode, v in docs_variants():
        for what, fg, bg, minimum in docs_pairs(v):
            got = ratio(fg, bg)
            rows.append((key, mode, what, got, minimum))
            if got < minimum:
                failures.append(f"{key} {mode}: {what} is {got:.2f}:1, needs {minimum}:1 ({fg} on {bg})")
    return rows, failures


def docs_outputs():
    """{path: expected content} for every docs file this script writes."""
    out = {DOCS_CSS: render_docs().encode()}
    for name, src in DOCS_ASSET_SOURCES.items():
        out[DOCS_ASSETS / name] = src.read_bytes()
    return out


def main():
    expected = render()
    rows, failures = check_contrast()
    if "--table" in sys.argv:
        for key, mode, what, got, minimum in rows:
            print(f"{key:10} {mode:5} {got:5.2f} (min {minimum})  {what}")
    docs = docs_outputs()
    if "--check" in sys.argv:
        stale = not OUT.exists() or OUT.read_text() != expected
        if stale:
            print(f"{OUT.relative_to(ROOT)} is stale: run python3 dev/gen_brand_css.py")
        for path, content in docs.items():
            if not path.exists() or path.read_bytes() != content:
                stale = True
                print(f"{path.relative_to(ROOT)} is stale: run python3 dev/gen_brand_css.py")
        for f in failures:
            print("FAIL", f)
        if stale or failures:
            sys.exit(1)
        print(f"ok ({len(rows)} pairs across {len({(k, m) for k, m, *_ in rows})} theme variants, all at WCAG AA)")
        return
    if failures:
        for f in failures:
            print("FAIL", f)
        sys.exit(1)
    OUT.write_text(expected)
    print(f"wrote {OUT.relative_to(ROOT)}")
    for path, content in docs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
