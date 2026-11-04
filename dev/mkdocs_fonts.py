# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""MkDocs hook: copy the woff2 files that docs/stylesheets/fonts.css names
from the web app's app/static/fonts/ into the built site's assets/fonts/,
so the documentation self-hosts the brand fonts without a second copy of
them in the repository. A file the stylesheet names and the web app lacks
fails the build."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from mkdocs.exceptions import PluginError

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "app/static/fonts"
STYLESHEET = ROOT / "docs/stylesheets/fonts.css"
NAMED = re.compile(r"url\(\.\./assets/fonts/([^)]+\.woff2)\)")


def on_post_build(config, **kwargs) -> None:
    names = sorted(set(NAMED.findall(STYLESHEET.read_text(encoding="utf-8"))))
    if not names:
        raise PluginError(f"{STYLESHEET.name} names no font files")
    missing = [n for n in names if not (FONTS / n).is_file()]
    if missing:
        raise PluginError(f"{STYLESHEET.name} names fonts the web app lacks: {', '.join(missing)}")
    dest = Path(config["site_dir"]) / "assets/fonts"
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(FONTS / name, dest / name)
