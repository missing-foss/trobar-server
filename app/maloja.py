#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maloja suggestions: the same contracts as lastfm.py and listenbrainz.py
(suggestions() / recently_played_suggestions() for /api/suggestions,
most_played() for the most-played chart), backed by a self-hosted Maloja
scrobble server. A multi-scrobbler relay that feeds Maloja is covered by
this as it is: it keeps no history of its own.

Per user, not app-wide: a Maloja instance is one person's history (it has no
accounts of its own), so its URL sits on the user's Profile beside the
Last.fm and ListenBrainz usernames.

Measured live against the official image, Maloja 3.2.6 (the minimum version
verified), seeded with a few hundred scrobbles through its own
`/apis/mlj_1/newscrobble`:
- Reads need no API key on a default install; only writes do.
  `GET {base}/apis/mlj_1/serverinfo` answers `{"name", "version",
  "versionstring", "db_status"}`: the connection check.
- `charts/albums` → `{"status": "ok", "list": [{"scrobbles", "album":
  {"artists": [...], "albumtitle"}, "album_id", "rank"}, ...]}`, ranked,
  not paginated (the whole chart comes back).
- `scrobbles` → `{"status": "ok", "list": [{"time", "track": {"artists":
  [...], "title", "album": {"artists", "albumtitle"} | null, "length"},
  "duration", "origin"}, ...], "pagination": {...}}`, newest first, paged
  with `page` (from 0) and `perpage`. A scrobble with no album has
  `"album": null`.
- Time range: `from` (also `since`), `until`, `in`, as `2022`, `2022/08`,
  `2022/08/01`, `today`, `thismonth`... **With no range given, both
  answer for today only**, so every call here sends `from`: a date for the
  Trobar periods, `1970` for all history (`in=alltime` is not a thing: it
  also answers today only). An unparseable range answers 500.
- Artist credits are split and normalised: "Cinder & Ash" is stored as
  the artists ["Ash", "Cinder"] (sorted), "A feat. B" as ["A", "B"]. The
  splits are " feat. ", " ft. ", " featuring ", " vs. ", " vs ", " & "
  (spaces around them), and ";", "/", "|" (Maloja's default delimiters);
  a comma is not one. So an album can't be matched by its joined artist
  string: matching here compares the SET of artist names, splitting the
  library's artist the same way (_artist_set). No MusicBrainz ids are
  given, so artist and album names are all there is.
- Album art: not used. Trobar's own covers serve library albums, as for
  the other sources; Maloja's image proxy isn't relied on.

Any user can set the URL, so every request goes through url_guard: an
address the server won't contact (loopback, link-local...) is refused at
connect time and no redirect is followed. Either way the call fails like an
unreachable Maloja, with a log line that leaves the URL out.
"""

import random
import re
from datetime import date, timedelta

import suggestions as suggestions_mod
import url_guard

_API = "/apis/mlj_1"
_HEADERS = {"User-Agent": "Trobar/1.0 (+https://github.com/missing-foss)"}

# Trobar's periods (Last.fm's vocabulary) as a number of days back; None is
# all history. Sent as Maloja's `from`, which defaults to today when absent.
_PERIOD_DAYS = {"overall": None, "7day": 7, "1month": 30, "3month": 91, "6month": 182, "12month": 365}

# Maloja's default artist delimiters: the spaced ones, then the formal ones.
_SPACED = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|vs\.?|&)\s+", re.IGNORECASE)
_FORMAL = re.compile(r"\s*[;/|]\s*")


def _from(period: str) -> str:
    days = _PERIOD_DAYS.get(period, _PERIOD_DAYS["6month"])
    if days is None:
        return "1970"
    return (date.today() - timedelta(days=days)).strftime("%Y/%m/%d")


def _artist_set(names) -> frozenset[str]:
    """The lowercased artist names in one or more credits, split the way
    Maloja splits them, so "Cinder & Ash" and ["Ash", "Cinder"] compare
    equal."""
    out = set()
    for name in ([names] if isinstance(names, str) else names):
        for part in _SPACED.split(name or ""):
            for piece in _FORMAL.split(part):
                if piece.strip():
                    out.add(piece.strip().lower())
    return frozenset(out)


def _get(base: str, endpoint: str, params: dict, timeout: int = 15):
    with url_guard.session() as session:
        resp = session.get(f"{base.rstrip('/')}{_API}/{endpoint}", params=params,
                           headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()


def _log_failure(e: Exception) -> None:
    if isinstance(e, url_guard.BlockedAddress):
        # The URL is the user's own; the kind is enough to act on.
        print(f"[maloja] refused: the Maloja URL leads to a {e.kind} address")
    else:
        print(f"[maloja] error: {e}")


def check_connection(base: str) -> bool:
    """Header status-dot check: does a Maloja answer at this URL."""
    if not base:
        return False
    try:
        return bool(_get(base, "serverinfo", {}, timeout=8).get("versionstring"))
    except url_guard.BlockedAddress as e:
        _log_failure(e)
        return False
    except Exception:
        return False


def album_charts(base: str, period: str = "6month") -> list[dict]:
    """The raw album chart for a Trobar period. [] on any failure or
    missing config, as lastfm.top_albums() and
    listenbrainz.top_release_groups() do."""
    if not base:
        return []
    try:
        return _get(base, "charts/albums", {"from": _from(period)}).get("list", [])
    except Exception as e:
        _log_failure(e)
        return []


def recent_scrobbles(base: str, limit: int = 50) -> list[dict]:
    """The latest scrobbles, newest first. [] on any failure."""
    if not base:
        return []
    try:
        return _get(base, "scrobbles", {"from": "1970", "page": 0, "perpage": limit}).get("list", [])
    except Exception as e:
        _log_failure(e)
        return []


def _library_by_artist_set(conn) -> dict[tuple[frozenset[str], str], tuple[str, str]]:
    """The local library keyed by (artist set, lowercased album): the
    form a Maloja album can be looked up by."""
    return {(_artist_set(artist), album_key): (artist, album)
            for (_a, album_key), (artist, album) in suggestions_mod.local_library_index(conn).items()}


def _album_of(entry: dict) -> tuple[list[str], str] | None:
    album = entry.get("album") or {}
    title = album.get("albumtitle") or ""
    return (album.get("artists") or [], title) if title else None


def suggestions(conn, base: str, period: str = "6month", limit: int = 50,
                user_device_ids: set[int] | None = None) -> list[dict]:
    """Top-played albums already in the local catalog but not yet synced to
    every device the caller manages: the Maloja counterpart of
    lastfm.suggestions(). `artist`/`album` are the library's own names, so
    the merge with the other sources dedups on them."""
    chart = album_charts(base, period)[:limit]
    if not chart:
        return []
    library = _library_by_artist_set(conn)
    covered = suggestions_mod.covered_devices(conn, suggestions_mod.local_library_index(conn))
    out = []
    for entry in chart:
        found = _album_of(entry)
        if found is None:
            continue
        artists, title = found
        local = library.get((_artist_set(artists), title.lower()))
        if local is None:
            continue
        key = (local[0].lower(), local[1].lower())
        if suggestions_mod.is_fully_synced(covered, key, user_device_ids):
            continue
        out.append({
            "artist": local[0], "album": local[1],
            "playcount": int(entry.get("scrobbles", 0)),
            "library_artist": local[0], "library_album": local[1],
            "image_url": None,  # the library's own cover
            "source": "maloja",
        })
    random.shuffle(out)  # same reasoning as lastfm.suggestions()
    return out


def most_played(conn, base: str, period: str = "6month", limit: int = 10) -> list[dict]:
    """The Maloja counterpart of lastfm.most_played(): ranked by scrobbles,
    no library filter, no shuffle. An album in the library is named as the
    library names it, so the merge with Last.fm or ListenBrainz keeps one
    copy; another keeps Maloja's credits, joined with " & "."""
    library = _library_by_artist_set(conn)
    out = []
    for entry in album_charts(base, period)[:limit]:
        found = _album_of(entry)
        if found is None:
            continue
        artists, title = found
        local = library.get((_artist_set(artists), title.lower()))
        artist, album = local if local else (" & ".join(artists), title)
        if not artist:
            continue
        out.append({"artist": artist, "album": album,
                    "playcount": int(entry.get("scrobbles", 0)), "image_url": None})
    return out


def recently_played_suggestions(conn, base: str, limit: int = 50,
                                user_device_ids: set[int] | None = None) -> list[dict]:
    """Distinct albums from the latest scrobbles, in the library and not
    yet everywhere: the Maloja counterpart of
    lastfm.recently_played_suggestions(). Scrobbles without an album are
    skipped."""
    scrobbles = recent_scrobbles(base, limit)
    if not scrobbles:
        return []
    library = _library_by_artist_set(conn)
    covered = suggestions_mod.covered_devices(conn, suggestions_mod.local_library_index(conn))
    seen: set[tuple[frozenset[str], str]] = set()
    out = []
    for s in scrobbles:
        found = _album_of(s.get("track") or {})
        if found is None:
            continue
        artists, title = found
        lookup = (_artist_set(artists), title.lower())
        if lookup in seen:
            continue
        seen.add(lookup)
        local = library.get(lookup)
        if local is None:
            continue
        if suggestions_mod.is_fully_synced(covered, (local[0].lower(), local[1].lower()), user_device_ids):
            continue
        out.append({
            "artist": local[0], "album": local[1],
            "library_artist": local[0], "library_album": local[1],
            "image_url": None,
            "source": "maloja-recent",
        })
    return out
