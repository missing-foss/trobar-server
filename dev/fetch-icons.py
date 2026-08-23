#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate app/static/js/icons.js from the Iconify API.

The web UI's icons are vendored, not fetched at runtime — same policy as the
bundled fonts and JS libraries (the app must work on a LAN with no internet).
This script is the one sanctioned way to add or update an icon: edit ICONS
below, run it once from a machine with outbound access, commit the result.

Icon sets are pulled through https://api.iconify.design (one JSON call per
set). Keep to permissively-licensed sets and record any new set in
THIRD_PARTY_NOTICES.md.
"""

import json
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

# Semantic name used in templates -> "set:icon" on api.iconify.design.
ICONS = {
    # device types (deviceIconSvg)
    "device-phone": "lucide:smartphone",
    "device-tablet": "lucide:tablet",
    "device-watch": "lucide:watch",
    "device-dap": "lucide:headphones",
    "device-sdcard": "lucide:memory-stick",
    # #218: local-folder sync targets, distinct from a removable SD/USB
    # device — same glyph as provider-filesystem (also lucide:folder),
    # different semantic key since this one's driven by deviceIconSvg().
    "device-folder": "lucide:folder",
    # providers (header status badge). Brand glyphs, nominative
    # interoperability use: an all-generic set made the badges
    # indistinguishable from each other.
    "provider-roon": "simple-icons:roon",
    # Navidrome's mark stands in for the Subsonic provider (the most common
    # server behind that API; the Subsonic project itself has no icon in any
    # permissively-licensed set). selfhst = CC BY 4.0, attributed in notices.
    # The -dark variant is the single-colour drawing; normalization below
    # makes it currentColor like everything else.
    "provider-subsonic": "selfhst:navidrome-dark",
    "provider-jellyfin": "simple-icons:jellyfin",
    # #168: Emby, Jellyfin's upstream — distinct glyph so the two aren't
    # visually confused in the provider picker.
    "provider-emby": "simple-icons:emby",
    # #158: distinct from Jellyfin's glyph despite the shared server lineage
    # — Simple Icons ships Plex's own mark separately.
    "provider-plex": "simple-icons:plex",
    # #172: no Simple Icons entry for Lyrion/Squeezebox exists; selfh.st's
    # icon pack (already used above for provider-subsonic's Navidrome mark)
    # does. The -dark variant is the single-colour drawing, normalized to
    # currentColor like every other selfhst icon here.
    "provider-lms": "selfhst:lyrion-music-server-dark",
    "provider-filesystem": "lucide:folder",
    "provider-tidal": "simple-icons:tidal",
    # #235: same brand-glyph treatment as Tidal above — missing until now,
    # which silently rendered as a blank icon next to "Spotify" in the
    # streaming-accounts card.
    "provider-spotify": "simple-icons:spotify",
    # integrations / suggestion sources
    "lastfm": "simple-icons:lastdotfm",
    "listenbrainz": "selfhst:listenbrainz-dark",
    "source-recent": "lucide:music",
    # chrome
    "settings": "lucide:settings",
    "check": "lucide:check",
    # #303: the cross-surface staging basket's header indicator.
    "basket": "lucide:shopping-basket",
    # #410: the Playlists row's Shared/Private toggle used to be a text-only
    # label styled almost identically to the action buttons beside it and
    # the read-only owner badges next to it — nothing distinguished a
    # clickable state toggle from either. A globe/lock pair reads instantly.
    "shared-yes": "lucide:globe",
    "shared-no": "lucide:lock",
    # #507: the mirrored-sink badge on a playlist's identity row — a hand
    # "offering" a provider glyph (composed in the template, see
    # mirrorIconTitle()'s call sites), distinct from the plain provider
    # glyph already used for source_provider/inferred_origin_provider so
    # "mirrored TO here" doesn't read as "came FROM here".
    "mirror-hand": "lucide:hand-helping",
}

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "icons.js"


def normalize(body: str) -> str:
    """Unify icons to a single theme-driven colour.

    Every icon must render in the colour of its surrounding text, whatever
    set it came from. Two steps:
    - hardcoded fills/strokes (hex or named colours) -> currentColor. Pick a
      set's mono/-dark/-light variant for multi-colour logos first — blindly
      flattening a multi-colour drawing produces a blob;
    - wrap in <g fill="currentColor"> so fill-less paths (SVG defaults them
      to black) inherit too. Harmless for bodies that set their own fill
      (an element's explicit fill="none"/currentColor always wins).
    """
    body = re.sub(r'(fill|stroke)="(?!none|currentColor)[^"]*"', r'\1="currentColor"', body)
    return f'<g fill="currentColor">{body}</g>'


def main() -> None:
    by_set: dict[str, list[str]] = defaultdict(list)
    for full in ICONS.values():
        prefix, name = full.split(":", 1)
        by_set[prefix].append(name)

    bodies: dict[str, str] = {}
    sizes: dict[str, tuple[int, int]] = {}
    for prefix, names in sorted(by_set.items()):
        url = f"https://api.iconify.design/{prefix}.json?icons={','.join(sorted(set(names)))}"
        req = urllib.request.Request(url, headers={"User-Agent": "trobar-fetch-icons/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        missing = set(names) - set(data.get("icons", {}))
        if missing:
            sys.exit(f"missing from {prefix}: {sorted(missing)}")
        for name, icon in data["icons"].items():
            bodies[f"{prefix}:{name}"] = icon["body"]
            sizes[f"{prefix}:{name}"] = (
                icon.get("width", data.get("width", 24)),
                icon.get("height", data.get("height", 24)),
            )

    # REUSE-IgnoreStart: this is the generated *output* file's own header,
    # not a license declaration for fetch-icons.py — reuse's file scanner is
    # a plain-text match and can't tell the difference, so without this
    # marker it reads the two lines below as a second, garbled SPDX tag on
    # this file (the trailing Python string-literal syntax after
    # "AGPL-3.0-or-later" gets swept into the parsed expression).
    lines = [
        "// SPDX-FileCopyrightText: 2026 missing-foss",
        "//",
        "// SPDX-License-Identifier: AGPL-3.0-or-later",
        # REUSE-IgnoreEnd
        "",
        "// GENERATED by dev/fetch-icons.py - do not edit by hand.",
        "// Icon artwork: Lucide (ISC), Simple Icons (CC0), selfh.st/icons (CC BY 4.0) - see THIRD_PARTY_NOTICES.md.",
        "// Each entry is inner SVG markup for a 24x24 viewBox, rendered via",
        '// <svg viewBox="0 0 24 24" x-html="ICONS[name]">.',
        "const ICONS = {",
    ]
    for semantic, full in ICONS.items():
        body = normalize(bodies[full])
        w, h = sizes[full]
        if (w, h) != (24, 24):
            # Templates always render into a 24x24 viewBox — rescale bodies
            # from sets with a different native size (e.g. selfhst is 512).
            body = f'<g transform="scale({24 / w:.6g} {24 / h:.6g})">{body}</g>'
        # The outer <svg> in the templates carries the sizing classes; the body
        # keeps its own stroke/fill attributes so icons stay self-contained.
        lines.append(f"  {json.dumps(semantic)}: {json.dumps(body)},")
    lines += ["};", ""]
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT} ({len(ICONS)} icons)")


if __name__ == "__main__":
    main()
