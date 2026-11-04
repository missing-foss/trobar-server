# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Auto-fill: filling a device's storage budget with the owner's most-played
albums.

A device with a storage limit can keep part of it filled automatically:
refresh_autofit takes the owner's albums ranked by plays (Last.fm), fits
whole albums into the budget left after their own selections and freezes
them as the device's auto-fill selection. It runs on demand, so what a
device holds stays put until the next refresh. Sizes come from the device's own
reported bytes where it has synced a track, else an estimate for its
transcode format. Split out of sync_state, which re-exports these names
for now.
"""
import logging
import sqlite3
from typing import TypedDict

from db import _new_id
from device_store import _is_lossless
from selection_store import required_track_ids_for_device

_log = logging.getLogger(__name__)


class AutofitSummary(TypedDict):
    albums: int
    tracks: int
    bytes: int
    budget_bytes: int
    used_by_manual_bytes: int
    used_by_foreign_bytes: int
    foreign_bytes_known: bool
    reason: str | None
    percent: int


DEFAULT_AUTOFIT_PERIOD = "6month"


# --- Auto-fit selections -----------------------------------------
# An auto-fit selection fills a device's *remaining* storage budget (its
# max_size_bytes, minus what its manual selections already need) with the
# device owner's most-played albums, ranked by Last.fm. It's materialized on
# demand: refresh_autofit() freezes the chosen tracks in autofit_tracks so the
# synced set is stable between refreshes and eviction only happens on a refresh.
def autofit_selection_id_for_device(conn: sqlite3.Connection, device_id: int) -> int | None:
    row = conn.execute(
        "SELECT s.id FROM selections s JOIN selection_devices sd ON sd.selection_id = s.id "
        "WHERE s.type = 'autofit' AND sd.device_id = ?",
        (device_id,),
    ).fetchone()
    return row["id"] if row else None


def create_autofit_selection(conn: sqlite3.Connection, device_id: int, user_id: int,
                             period: str = DEFAULT_AUTOFIT_PERIOD) -> int:
    """Create the device's single auto-fit selection (caller should refresh it
    afterwards to materialize tracks). The Last.fm period is stored in target."""
    cur = conn.execute(
        "INSERT INTO selections (type, target, created_by_user_id) VALUES ('autofit', ?, ?)",
        (period, user_id),
    )
    selection_id = _new_id(cur)
    conn.execute(
        "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
        (selection_id, device_id),
    )
    conn.commit()
    return selection_id


def autofit_status(conn: sqlite3.Connection, device_id: int) -> dict:
    """Cheap (no Last.fm) status of a device's auto-fit from the materialized
    set — for the devices list. bytes/tracks reflect the last refresh.
    `percent` (#217) is read regardless of enabled state, so the owner's
    chosen fill target is visible/adjustable even before first enabling."""
    dev_row = conn.execute(
        "SELECT transcode_format, autofit_percent FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    percent = dev_row["autofit_percent"]
    selection_id = autofit_selection_id_for_device(conn, device_id)
    if selection_id is None:
        return {"enabled": False, "percent": percent}
    row = conn.execute("SELECT target FROM selections WHERE id = ?", (selection_id,)).fetchone()
    track_ids = {r["track_id"] for r in conn.execute(
        "SELECT track_id FROM autofit_tracks WHERE selection_id = ?", (selection_id,)
    )}
    albums = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(t.album_artist, t.artist) || '||' || t.album) AS n FROM autofit_tracks a "
        "JOIN tracks t ON t.id = a.track_id WHERE a.selection_id = ?", (selection_id,)
    ).fetchone()["n"]
    fmt = dev_row["transcode_format"]
    return {
        "enabled": True,
        "percent": percent,
        "period": row["target"] if row else DEFAULT_AUTOFIT_PERIOD,
        "albums": albums,
        "tracks": len(track_ids),
        "bytes": _sum_device_bytes(conn, track_ids, device_id, fmt),
    }


# what a track will actually occupy on a transcoding device:
# CBR MP3 = bitrate/8 bytes per second of audio, plus slack for ID3 headers
# and embedded art. Capped at the original size (a transcode is never bigger
# in practice; if the estimate says otherwise the duration is suspect).
# Tracks without a duration fall back to the original size — a safe
# overestimate.
_TRANSCODE_BYTES_PER_SEC = {
    "mp3_320": 40_000, "mp3_256": 32_000, "mp3_192": 24_000, "mp3_128": 16_000,
}


_EST_SLACK_BYTES = 256 * 1024


def _device_size_estimate(row, transcode_format: str | None) -> int:
    """What a track (size, duration, relative_path, lossless) will take on a
    device: the transcoded estimate when the device transcodes it, else its
    size."""
    size, duration = row["size"], row["duration"]
    bps = _TRANSCODE_BYTES_PER_SEC.get(transcode_format or "")
    if bps and duration and _is_lossless(row):
        return min(size, int(duration * bps) + _EST_SLACK_BYTES)
    return size


def estimated_sizes(conn: sqlite3.Connection, track_ids: list[int],
                    transcode_format: str | None) -> dict[int, int]:
    """Per track, what it will take on a device with this format: the
    transcoded estimate where the device transcodes it, else its size. The
    same estimate the usage figures use, so the device's own space check
    counts a transcoded track as the web UI does, not as nothing."""
    if not track_ids:
        return {}
    placeholders = ",".join("?" * len(track_ids))
    return {row["id"]: _device_size_estimate(row, transcode_format) for row in conn.execute(
        f"SELECT id, size, duration, relative_path, lossless FROM tracks WHERE id IN ({placeholders})",
        tuple(track_ids))}


def _sum_device_bytes(conn: sqlite3.Connection, track_ids: set[int],
                      device_id: int, transcode_format: str | None) -> int:
    """Bytes these tracks occupy (or will occupy) on this device: real
    reported bytes where a client has acked, MP3-320 estimate for
    still-pending lossless tracks on a transcoding device, original size
    otherwise."""
    if not track_ids:
        return 0
    placeholders = ",".join("?" * len(track_ids))
    total = 0
    for row in conn.execute(
        f"SELECT t.size, t.relative_path, t.duration, t.lossless, dts.bytes_on_device "
        f"FROM tracks t LEFT JOIN device_track_state dts "
        f"ON dts.track_id = t.id AND dts.device_id = ? "
        f"WHERE t.id IN ({placeholders})",
        (device_id, *track_ids),
    ):
        total += row["bytes_on_device"] if row["bytes_on_device"] is not None else \
            _device_size_estimate(row, transcode_format)
    return total


def refresh_autofit(conn: sqlite3.Connection, selection_id: int,
                    ranked_album_keys: list[tuple[str, str]]) -> AutofitSummary:
    """Re-materialize an auto-fit selection. `ranked_album_keys` is a list of
    (artist_lower, album_lower) in descending play-count order (the caller
    builds it from Last.fm); the artist matches an album's album artist or
    one of its track artists. Greedily fits whole albums — highest-played first,
    skipping any that don't fit so smaller lower-ranked albums can still slot in
    — into the device's remaining budget, then freezes the chosen tracks in
    autofit_tracks. Returns a summary dict; `reason` is set when nothing could
    be fitted.

    #217: `devices.autofit_percent` (default 100, i.e. "fill it all" — no
    behaviour change for existing devices) caps auto-fit's own share of the
    device at a percentage of `max_size_bytes`, not of whatever's left after
    manual selections — a percentage of a shifting remainder wouldn't deliver
    the "leave headroom on the device" the cap is for; add a manual selection
    and the reserved space would silently move. So `budget_bytes` below is
    the *capped* ceiling (max_size_bytes * percent / 100), and manual
    selections are still subtracted from that, never from the full device
    size — this is the "auto-fit's own share" budget, not the device's."""
    conn.execute("DELETE FROM autofit_tracks WHERE selection_id = ?", (selection_id,))
    summary: AutofitSummary = {
        "albums": 0, "tracks": 0, "bytes": 0, "budget_bytes": 0,
        "used_by_manual_bytes": 0, "used_by_foreign_bytes": 0,
        "foreign_bytes_known": False, "reason": None, "percent": 100,
    }

    dev = conn.execute(
        "SELECT device_id FROM selection_devices WHERE selection_id = ?", (selection_id,)
    ).fetchone()
    if dev is None:
        conn.commit()
        summary["reason"] = "no_device"
        return summary
    device_id = dev["device_id"]

    dev_row = conn.execute(
        "SELECT max_size_bytes, transcode_format, autofit_percent, reported_foreign_bytes "
        "FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    max_size_bytes = dev_row["max_size_bytes"]
    fmt = dev_row["transcode_format"]
    percent = dev_row["autofit_percent"]
    summary["percent"] = percent
    if not max_size_bytes:
        conn.commit()
        summary["reason"] = "no_size_limit"  # can't auto-fit without a device budget
        return summary
    budget = max_size_bytes * percent // 100
    summary["budget_bytes"] = budget

    manual_ids = required_track_ids_for_device(conn, device_id, exclude_autofit=True)
    manual_bytes = _sum_device_bytes(conn, manual_ids, device_id, fmt)
    summary["used_by_manual_bytes"] = manual_bytes

    # Music already in the sync folder that Trobar did not put there. The
    # limit is what the user allows in that FOLDER, so this is a third
    # claim on it alongside manual selections and auto-fit's own share.
    #
    # It bounds the fill in a different way from `percent`, and both apply:
    #
    #   budget          auto-fit's own share, a percentage of the whole
    #                   limit -- unchanged, still not a percentage of a
    #                   shifting remainder (see this function's docstring)
    #   max - foreign   what is actually left in the folder for Trobar
    #
    # so what auto-fit may add is min(budget, max - foreign) - manual. The
    # cap that binds is whichever is lower, which is the only way to keep
    # BOTH promises: auto-fit never exceeds its share, and the folder never
    # exceeds the limit. Subtracting foreign from `budget` alone would
    # refuse to fit anything into a mostly-foreign folder that still has
    # room under the limit; subtracting it before applying `percent` would
    # make the reserved headroom move whenever a file appeared in the
    # folder from somewhere else, which is the objection `percent` exists
    # to answer.
    #
    # NULL means never reported, not zero. Treated as zero HERE, so a
    # device enrolled before the client could report this behaves exactly
    # as it does today rather than having auto-fit stop working on
    # upgrade -- but `foreign_bytes_known` carries the difference out to
    # the caller so the UI can say the folder has not been measured instead
    # of implying it is empty. Silent would be the wrong half of that.
    foreign_bytes = dev_row["reported_foreign_bytes"]
    summary["foreign_bytes_known"] = foreign_bytes is not None
    summary["used_by_foreign_bytes"] = foreign_bytes or 0

    remaining = min(budget, max_size_bytes - summary["used_by_foreign_bytes"]) - manual_bytes
    if remaining <= 0:
        conn.commit()
        summary["reason"] = "budget_full"
        return summary

    # Library index: (artist_lower, album_lower) -> [(track_id, size), ...]
    # — on a transcoding device `size` is the on-device
    # estimate, so a 128 GB card fits ~3x more FLAC-sourced albums as MP3.
    # Each album is listed under its album artist, so a compilation Last.fm
    # ranks under "Various Artists" is fitted whole, and under each of its
    # track artists too, so one ranked under a performer still fits that
    # performer's tracks.
    library: dict[tuple[str, str], list[tuple[int, int]]] = {}
    album_of: dict[int, tuple[str, str]] = {}  # counted by album artist, as autofit_status does
    for row in conn.execute(
        "SELECT id, artist, COALESCE(album_artist, artist) AS album_artist, album, relative_path, "
        "duration, lossless, COALESCE(size, 0) AS size FROM tracks WHERE deleted_at IS NULL"
    ):
        est = _device_size_estimate(row, fmt)
        album = row["album"].lower()
        album_of[row["id"]] = (row["album_artist"].lower(), album)
        for artist in {row["album_artist"].lower(), row["artist"].lower()}:
            library.setdefault((artist, album), []).append((row["id"], est))

    chosen: list[int] = []
    chosen_set: set[int] = set()
    used = 0
    for key in ranked_album_keys:
        # A performer's tracks on a compilation already fitted whole (or the
        # reverse) are only counted and sized once.
        tracks = [(tid, sz) for tid, sz in library.get(key, []) if tid not in chosen_set]
        if not tracks:
            continue  # scrobbled album not in the library, or already fitted
        album_ids = [tid for tid, _ in tracks]
        if all(tid in manual_ids for tid in album_ids):
            continue  # already fully covered by a manual selection
        album_size = sum(sz for _, sz in tracks)
        if used + album_size > remaining:
            continue  # doesn't fit — try smaller lower-ranked albums
        chosen.extend(album_ids)
        chosen_set.update(album_ids)
        used += album_size

    for track_id in chosen:
        conn.execute(
            "INSERT OR IGNORE INTO autofit_tracks (selection_id, track_id) VALUES (?, ?)",
            (selection_id, track_id),
        )
    conn.commit()
    summary["albums"] = len({album_of[tid] for tid in chosen})
    summary["tracks"] = len(chosen)
    summary["bytes"] = used
    return summary


def autofit_fill_basis(conn: sqlite3.Connection, device_id: int) -> dict | None:
    """#217: the percent-*independent* inputs to the live GB/track-count
    preview in the Devices panel — max_size_bytes, manual_bytes, and the
    per-device average track-size estimate. None of these depend on the
    candidate fill percentage the owner is dragging the slider to, so this
    is computed once (when the panel opens the auto-fit mini-panel, not per
    drag) and the cheap `remaining = max_size*pct/100 - manual; tracks =
    remaining/avg` arithmetic happens client-side from there — no per-drag
    round trip, no debounce needed, and no repeated full-library scan.
    Touches no state (no autofit_tracks write), unlike refresh_autofit.
    Returns None if the device doesn't exist.

    The track-count estimate is necessarily approximate: auto-fit packs whole
    albums, so the real result lands under the target rather than on it, and
    it's an average over the whole library rather than the ranked candidate
    pool a real refresh would use (that ranking needs a Last.fm call, too
    slow for the live preview). transcode_format dominates the average — at
    mp3_128 a device holds roughly 2.5x the tracks it holds at Originals —
    so the average is computed per-device, never a global constant."""
    dev_row = conn.execute(
        "SELECT max_size_bytes, transcode_format, reported_foreign_bytes "
        "FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    if dev_row is None:
        return None
    max_size_bytes = dev_row["max_size_bytes"]
    fmt = dev_row["transcode_format"]
    if not max_size_bytes:
        return {"max_size_bytes": 0, "manual_bytes": 0, "avg_track_bytes": 0,
                "foreign_bytes": 0, "foreign_bytes_known": False}

    manual_ids = required_track_ids_for_device(conn, device_id, exclude_autofit=True)
    manual_bytes = _sum_device_bytes(conn, manual_ids, device_id, fmt)

    total_size = 0
    total_count = 0
    for row in conn.execute(
        "SELECT relative_path, duration, lossless, COALESCE(size, 0) AS size FROM tracks WHERE deleted_at IS NULL"
    ):
        total_size += _device_size_estimate(row, fmt)
        total_count += 1
    avg_track_bytes = (total_size // total_count) if total_count else 0

    return {
        "max_size_bytes": max_size_bytes,
        "manual_bytes": manual_bytes,
        "avg_track_bytes": avg_track_bytes,
        # The preview has to apply the same min(budget, max - foreign)
        # bound refresh_autofit does, or the slider promises space the real
        # fill will not use. `foreign_bytes_known` is carried alongside for
        # the same reason it is there: an unmeasured folder must read as
        # unmeasured, not as an empty one.
        "foreign_bytes": dev_row["reported_foreign_bytes"] or 0,
        "foreign_bytes_known": dev_row["reported_foreign_bytes"] is not None,
    }
