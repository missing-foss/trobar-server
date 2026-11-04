#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every form control in the page templates has a name a screen reader can
announce, and nothing that isn't a control takes a click.

The e2e axe pass only sees what is on screen when it runs, so a field in a
closed dialog or an edit row nobody opened escapes it. This reads the
templates instead, so every control is checked whether or not a test opens
it. A placeholder doesn't count as a name here, although axe accepts one: it
disappears as soon as someone types.

    python3 -m unittest test_template_names -v      # from app/
"""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

_TEMPLATES = Path(__file__).parent / "templates"
_PAGES = ("index.html", "login.html", "setup.html")
_NAMED_BY = ("aria-label", ":aria-label", "aria-labelledby", ":aria-labelledby")
_CLICK_ONLY_ON_CONTROLS = ("div", "span", "li", "td", "tr", "img", "p")
# Handlers on a container that react to clicks elsewhere, not on it.
_NOT_A_CLICK_TARGET = ("@click.outside", "@click.self", "@click.away")
# An icon's markup, which a screen reader doesn't read out, even when x-html fills it.
_SVG_PARTS = ("svg", "path", "circle", "rect", "line", "polyline", "polygon", "g")


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.label_depth = 0
        self.label_for: set[str] = set()
        self.controls: list[tuple[int, str, dict]] = []
        self.clickable: list[tuple[int, str]] = []
        # Buttons with nothing to announce: no text, no text-bearing child
        # and no aria-label. A `title` alone isn't read by every screen reader.
        self.unnamed_buttons: list[int] = []
        self._button: list | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        line = self.getpos()[0]
        if tag == "button":
            self._button = [line, any(k in a for k in _NAMED_BY + ("x-text",))]
        elif self._button is not None and tag not in _SVG_PARTS and (
                "x-text" in a or "sr-only" in a.get("class", "")):
            self._button[1] = True
        if tag == "label":
            self.label_depth += 1
            # A static `for`, or an Alpine `:for` whose expression a `:id` repeats.
            for key in ("for", ":for"):
                if a.get(key):
                    self.label_for.add(a[key])
        elif tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button"):
            if self.label_depth == 0:
                self.controls.append((line, tag, a))
        if tag in _CLICK_ONLY_ON_CONTROLS:
            clicks = [k for k in a if k == "@click" or k == "x-on:click"
                      or (k.startswith("@click.") and k not in _NOT_A_CLICK_TARGET)]
            if clicks and a.get("role") not in ("button", "menuitem", "option", "tab", "link"):
                self.clickable.append((line, tag))

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth -= 1
        elif tag == "button" and self._button is not None:
            if not self._button[1]:
                self.unnamed_buttons.append(self._button[0])
            self._button = None

    def handle_data(self, data):
        if self._button is not None and data.strip():
            self._button[1] = True


def _scan(name: str) -> _Scan:
    source = (_TEMPLATES / name).read_text()
    # Jinja tags become plain text, so the parser sees only the markup; their
    # newlines stay, so a failure names the template's own line numbers.
    source = re.sub(r"\{\{.*?\}\}", lambda m: "X" + "\n" * m.group(0).count("\n"), source, flags=re.S)
    source = re.sub(r"\{%.*?%\}", lambda m: "\n" * m.group(0).count("\n"), source, flags=re.S)
    scan = _Scan()
    scan.feed(source)
    return scan


class TemplateNameTests(unittest.TestCase):
    def test_every_form_control_has_a_name(self):
        for page in _PAGES:
            scan = _scan(page)
            self.assertGreater(len(scan.controls) + len(scan.label_for), 0, f"{page}: found no controls at all")
            unnamed = [
                (line, tag) for line, tag, a in scan.controls
                if not any(k in a for k in _NAMED_BY)
                and not (a.get("id") and a["id"] in scan.label_for)
                and not (a.get(":id") and a[":id"] in scan.label_for)
            ]
            with self.subTest(page=page):
                self.assertEqual(unnamed, [], f"{page}: controls with no label or aria-label, by line")

    def test_every_button_has_something_to_announce(self):
        for page in _PAGES:
            with self.subTest(page=page):
                self.assertEqual(_scan(page).unnamed_buttons, [],
                                 f"{page}: buttons with no text and no aria-label, by line")

    def test_nothing_but_a_control_takes_a_click(self):
        for page in _PAGES:
            with self.subTest(page=page):
                self.assertEqual(_scan(page).clickable, [],
                                 f"{page}: clickable elements a keyboard can't reach, by line")

    def test_the_scan_finds_what_it_is_meant_to(self):
        # A check that can't fail looks just like one that passed.
        scan = _Scan()
        scan.feed('<label for="a">A</label><input id="a">'
                  '<input placeholder="only a placeholder">'
                  '<label>Wrapped <input></label>'
                  '<div @click="x()">row</div><div @click.outside="close()">menu</div>'
                  '<button title="Icon only"><svg x-html="icon"></svg></button>'
                  '<button><span x-text="label"></span></button><button>Save</button>')
        self.assertEqual([(line, tag) for line, tag, a in scan.controls
                          if not any(k in a for k in _NAMED_BY) and a.get("id") not in scan.label_for],
                         [(1, "input")])
        self.assertEqual(len(scan.clickable), 1)
        self.assertEqual(scan.unnamed_buttons, [1])


if __name__ == "__main__":
    unittest.main()
