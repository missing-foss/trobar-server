#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TheAudioDB artist images — the cleanly-licensed source for
artist pictures. Unlike the Roon-sourced path (TiVo/Rovi-licensed content,
display-inside-Roon-products only), TheAudioDB's API terms are written for
exactly this fetch-and-cache use, which is what a public deployment needs.

Only used when the admin has configured an API key (Administration panel,
`audiodb_api_key` in app_config) — without one, behaviour is exactly the
the earlier provider-first flow. Deliberately artist-search by exact-ish name,
same philosophy as the provider path: a miss just means no picture."""

import urllib.parse

import requests

_SEARCH_URL = "https://www.theaudiodb.com/api/v1/json/{key}/search.php?s={artist}"
_TIMEOUT = 10


def get_artist_image(artist: str, api_key: str) -> tuple[bytes, str, str] | None:
    """Returns (bytes, content_type, source_url) or None. Never raises —
    an API hiccup must degrade to the provider fallback, not break the
    endpoint."""
    try:
        resp = requests.get(
            _SEARCH_URL.format(key=urllib.parse.quote(api_key),
                               artist=urllib.parse.quote(artist)),
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        artists = (resp.json() or {}).get("artists") or []
        if not artists:
            return None
        # thumb is the square portrait (what the UI/device wants); fanart
        # only as a fallback if no thumb was ever uploaded.
        image_url = artists[0].get("strArtistThumb") or artists[0].get("strArtistFanart")
        if not image_url:
            return None
        img = requests.get(image_url, timeout=_TIMEOUT)
        if img.status_code != 200 or not img.content:
            return None
        content_type = img.headers.get("Content-Type", "image/jpeg").split(";")[0]
        return img.content, content_type, image_url
    except (requests.RequestException, ValueError):
        return None
