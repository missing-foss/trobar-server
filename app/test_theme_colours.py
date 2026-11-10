# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every colour utility the templates use follows the theme.

Tailwind compiles a colour the config maps (tailwind.config.js) to a
var(--c-…), which each theme sets. Any other shade compiles to Tailwind's own
fixed rgb(…) and keeps its light-mode colour in every theme: a cream box on a
dark page, its themed text unreadable on it. This reads the classes the
templates use and the built tailwind.css, and fails on any colour utility
whose rule carries a literal colour and no theme variable, unless it is in
DELIBERATELY_FIXED with its reason."""

import pathlib
import re
import unittest

APP = pathlib.Path(__file__).resolve().parent
CSS = APP / "static" / "css" / "tailwind.css"
TEMPLATES = APP / "templates"

# Utilities that set a colour, with Tailwind's variant prefixes (hover:, sm:,
# disabled:, …) and an optional /opacity.
_COLOUR_UTILITY = re.compile(
    r"(?<![\w-])((?:[a-z-]+:)*(?:bg|text|border(?:-[trblxy])?|ring|ring-offset|divide|outline|"
    r"placeholder|accent|caret|fill|stroke|from|via|to|decoration|shadow)-"
    r"(?:[a-z]+-\d{2,3}|black|white|\[[^\]\s]+\])(?:/\d{1,3})?)(?![\w-])"
)

# Kept fixed on purpose, each with why.
DELIBERATELY_FIXED = {
    # The dialog scrim: a darkening of whatever is behind, the same in every theme.
    "bg-black/40": "dialog scrim",
    # The status dot's outline on a source icon in the header: a faint shadow
    # ring, darkening whatever the dot sits on.
    "ring-black/20": "shadow ring around a status dot",
}


# Named in the templates, but with no rule in the build. Tailwind builds none
# for an /opacity on a colour that is a var(), which has no <alpha-value>
# slot, so such a class does nothing at all. Empty, and kept empty: requiring
# every scanned class to have a rule is also what keeps the check above from
# passing a class it never found.
NOT_COMPILED: set[str] = set()


def template_colour_classes(text: str) -> set[str]:
    """The colour utilities named in a template's text (class attributes,
    Alpine :class strings and string literals in its script alike)."""
    return set(_COLOUR_UTILITY.findall(text))


def css_selector(cls: str) -> str:
    """The class as it appears in compiled CSS, escaped as Tailwind escapes
    it: ':', '/', '[', ']', '#', '(', ')', ',' and '.'."""
    return "." + re.sub(r"([:/\[\]#(),.])", r"\\\1", cls)


def fixed_classes(css: str, classes: set[str]) -> dict[str, str]:
    """The classes whose compiled rules set a literal colour and no theme
    variable, with the offending declarations."""
    out = {}
    for cls in classes:
        sel = re.escape(css_selector(cls))
        # The class's own rules: its selector, maybe followed by a pseudo-class
        # (:hover, :focus, ::placeholder), then the declarations.
        bodies = re.findall(sel + r"(?![\w-])[^{,]*\{([^}]*)\}", css)
        for body in bodies:
            if "var(--c-" in body:
                continue
            if re.search(r"rgba?\(\s*\d|#[0-9a-fA-F]{3,8}\b", body):
                out[cls] = body.strip()
    return out


class ThemeColourTests(unittest.TestCase):
    def test_every_colour_utility_in_the_templates_follows_the_theme(self):
        classes = set()
        for path in TEMPLATES.rglob("*.html"):
            classes |= template_colour_classes(path.read_text(encoding="utf-8"))
        self.assertGreater(len(classes), 50, "the template scan found almost nothing: the pattern is broken")
        fixed = fixed_classes(CSS.read_text(encoding="utf-8"), classes - set(DELIBERATELY_FIXED))
        self.assertEqual({}, fixed, "colour utilities compiled to a fixed colour; map them in tailwind.config.js "
                                    "to a theme variable, or add them to DELIBERATELY_FIXED with the reason")

    def test_every_scanned_class_is_found_in_the_built_css(self):
        """A class whose rule the scan can't find (a selector escaped wrongly,
        say) would pass the check above without being checked."""
        css = CSS.read_text(encoding="utf-8")
        classes = set()
        for path in TEMPLATES.rglob("*.html"):
            classes |= template_colour_classes(path.read_text(encoding="utf-8"))
        missing = {c for c in classes if not re.search(re.escape(css_selector(c)) + r"(?![\w-])", css)}
        self.assertEqual(NOT_COMPILED, missing)

    def test_a_planted_fixed_class_is_caught(self):
        css = ".bg-amber-50{--tw-bg-opacity:1;background-color:rgb(255 251 235/var(--tw-bg-opacity))}" \
              ".hover\\:bg-amber-100:hover{background-color:rgb(254 243 199/1)}" \
              ".bg-black\\/30{background-color:rgba(0,0,0,.3)}" \
              ".text-white{color:#fff}" \
              ".bg-\\[\\#fffbeb\\]{background-color:#fffbeb}" \
              ".bg-panel{background-color:var(--c-panel)}"
        found = template_colour_classes(
            '<div class="bg-amber-50 hover:bg-amber-100 bg-black/30 text-white bg-[#fffbeb] bg-panel">')
        self.assertEqual({"bg-amber-50", "hover:bg-amber-100", "bg-black/30", "text-white", "bg-[#fffbeb]"},
                         set(fixed_classes(css, found)))

    def test_the_deliberately_fixed_ones_are_still_used(self):
        """An allow-list entry nothing uses any more is dead weight, and could
        later let a real regression through under its name."""
        text = "".join(p.read_text(encoding="utf-8") for p in TEMPLATES.rglob("*.html"))
        used = template_colour_classes(text)
        self.assertEqual(set(), set(DELIBERATELY_FIXED) - used)


if __name__ == "__main__":
    unittest.main()
