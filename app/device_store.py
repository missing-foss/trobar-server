# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Devices: creating and pairing them, what each holds, what it is told to
change, and where each track lands on it.

A device is created with a token (token_store) or by redeeming a pairing
code, and can replace another (transfer_device). Its device_track_state
rows, computed from its selections (selection_store), are what get_changes
tells it to fetch or delete and what ack records as done. A manifest it
reports says what it already holds; files that match no library track are
its unknown tracks. device_path names a track's file on the device, the
contract every client matches by. Split out of sync_state, which re-exports
these names for now.
"""
import hashlib
import logging
import posixpath
import re
import secrets
import sqlite3

import transcode
from basket_store import remap_device_in_last_destinations
from db import _new_id
from selection_store import _DEVICE_PLAYLIST_IDS_SQL, recompute_device_state
from token_store import hash_token, regenerate_token

_log = logging.getLogger(__name__)


def _lowest_transcode_tier() -> str:
    """The lowest available bitrate tier. Derived from transcode.BITRATES
    (parsing "128k" etc. back to a number) rather than hardcoded, so a
    future lower tier — if one's ever added — is picked up automatically
    without a second change here."""
    return min(transcode.BITRATES, key=lambda fmt: int(transcode.BITRATES[fmt].rstrip("k")))


def create_device(conn: sqlite3.Connection, owner_user_id: int, name: str,
                   device_type: str = "phone", max_size_bytes: int | None = None, *,
                   transcode_format: str | None = None,
                   artist_images: str | None = None) -> tuple[int, str]:
    """Returns (device_id, raw_token). The raw token is shown to the user
    exactly once (QR code / config download) — only its hash is stored.

    transcode_format/artist_images (#97) are honoured at create time so an API
    client can pair and configure in one call, matching what PATCH already
    accepts; the caller is responsible for validating them first.

    #221: a watch device that doesn't specify a format defaults to the
    lowest tier rather than staying on "original" — Connect IQ watches
    can't decode lossless audio at all, and the failure mode when left
    unset isn't an obvious error, it's a stuck/incomplete sync. An
    explicit value from the client (if one's ever sent) still wins."""
    if transcode_format is None and device_type == "watch":
        transcode_format = _lowest_transcode_tier()
    raw_token = secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO devices (owner_user_id, name, device_type, api_token_hash, "
        "max_size_bytes, transcode_format, artist_images) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (owner_user_id, name, device_type, hash_token(raw_token), max_size_bytes,
         transcode_format, artist_images),
    )
    conn.commit()
    return _new_id(cur), raw_token


def redeem_enrollment_grant(conn: sqlite3.Connection, code: str, name: str,
                            device_type: str, max_size_bytes: int | None,
                            transcode_format: str | None = None) -> tuple[int, str] | None:
    """#163: atomically consume a valid (unexpired, unconsumed) enrollment code
    and create the device owned by the grant's user, returning (device_id,
    raw_token). Returns None if the code is invalid / expired / already used.
    The consume is a single UPDATE guarded on `consumed_at IS NULL AND
    expires_at > now`, so two racing redemptions can't both succeed."""
    code_hash = hash_token(code)
    cur = conn.execute(
        "UPDATE enrollment_grants SET consumed_at = datetime('now') "
        "WHERE code_hash = ? AND consumed_at IS NULL AND expires_at > datetime('now')",
        (code_hash,),
    )
    if cur.rowcount != 1:
        conn.commit()
        return None
    grant = conn.execute(
        "SELECT owner_user_id, device_id FROM enrollment_grants WHERE code_hash = ?",
        (code_hash,),
    ).fetchone()
    owner = grant["owner_user_id"]

    if grant["device_id"] is not None:
        # Re-pairing an existing device. Ownership is re-checked here rather
        # than trusted from mint time: the grant lives for up to 20 minutes, and a
        # device can be deleted or change hands inside that window. A code
        # whose device is gone is spent (consumed above) and refused, which is
        # the same answer the caller gets for any other invalid code -- it must
        # not fall through to creating a device, or "re-pair" would silently
        # become "enrol a stranger" at the moment it is least expected.
        owned = conn.execute(
            "SELECT id FROM devices WHERE id = ? AND owner_user_id = ?",
            (grant["device_id"], owner),
        ).fetchone()
        if owned is None:
            conn.commit()
            return None
        # Name, type and limits are deliberately NOT taken from the caller.
        # The device already has them and they are the reason to re-pair at
        # all; letting a fresh install overwrite them with its defaults would
        # quietly undo the thing being recovered.
        raw_token = regenerate_token(conn, grant["device_id"], commit=False)
        conn.commit()
        return grant["device_id"], raw_token

    device_id, raw_token = create_device(
        conn, owner, name, device_type, max_size_bytes, transcode_format=transcode_format)
    conn.commit()
    return device_id, raw_token


def sync_status(conn: sqlite3.Connection, device_id: int) -> dict:
    """Cheap, trustworthy sync status for a device, derived straight from
    device_track_state — for the devices list (trobar-server#229).
    Deliberately not based on devices.last_seen_at: that column bumps on
    *every* authenticated device call (a plain "any changes?" check that
    finds none included), so it can't tell "just finished syncing" from
    "asked and there was nothing to do". 'pending' rows are still queued;
    anything else (downloaded/removed/excluded) has already been acted on,
    so the most recent such row's updated_at is the real last-synced time.
    last_synced_at is None if the device has never had anything to sync."""
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count, "
        "MAX(CASE WHEN status != 'pending' THEN updated_at END) AS last_synced_at "
        "FROM device_track_state WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    return {
        "pending_count": row["pending_count"] or 0,
        "last_synced_at": row["last_synced_at"],
    }


def transfer_device(conn: sqlite3.Connection, old_device_id: int, new_device_id: int, *,
                     assume_present: bool = False) -> dict:
    """#440: "new_device_id replaces old_device_id" -- moves everything the
    old device holds onto the new one, then deletes the old device.

    Settings (transcode_format, max_size_bytes, autofit_percent,
    artist_images, source_of_truth) are copied from old to new BEFORE
    device_track_state is reassigned. Once the new device's transcode
    format matches the old one's, device_path() resolves identically on
    both, so a transferred 'downloaded' status -- when assume_present says
    to keep one -- is still true: the file the old device fetched under
    that format is exactly what the new device would fetch too. Skipping
    this ordering (or not copying transcode_format at all) would carry
    "downloaded" over to a device that names the file differently, telling
    the server a file is present under a name that doesn't exist on it.

    assume_present (default False, per PR #442 review): whether the new
    device is assumed to already hold what the old one had (a cloned card,
    a restored backup) rather than being blank. False is the safe default
    -- transferred 'downloaded' rows land as 'pending' instead, so the new
    device re-fetches them. The unsafe alternative (carrying 'downloaded'
    over unconditionally) self-corrects on Android/desktop, which report
    locally-missing files back via /api/device/missing-tracks -- but
    Garmin has no such path (no manifest, no missing-tracks report, no
    provenance store: #38), so a blank watch told its tracks are already
    'downloaded' would sync "successfully" while staying permanently
    empty, with nothing on either side able to notice or correct it. A
    caller certain the new device genuinely already holds the content
    passes assume_present=True to skip the redundant re-fetch.

    Any device_track_state the new device already had (it may have paired
    and synced before the transfer ran) is overwritten for tracks the old
    device covered -- that's the whole point, this device now stands in
    for the old one. But if copying settings actually changes the new
    device's OWN transcode_format, its pre-existing 'downloaded' rows for
    tracks the OLD device did NOT have are now stale under the previous
    extension -- same fix a plain PATCH format change already applies
    (see api_device_update), reused here for the same reason.

    Selections assigned to the old device move to the new one (INSERT OR
    IGNORE, so a selection already assigned to both collapses to one row
    instead of a UNIQUE violation) -- otherwise the replacement device
    would sync nothing going forward, a silent half-transfer. Basket items
    staged for the old device move the same way.

    Caller is responsible for the permission check (touches two devices,
    see _require_device_access at both call sites) and for ensuring
    old_device_id != new_device_id."""
    old = conn.execute(
        "SELECT name, transcode_format, max_size_bytes, autofit_percent, artist_images, source_of_truth, play_playlist_id "
        "FROM devices WHERE id = ?", (old_device_id,)
    ).fetchone()
    new_prev_transcode = conn.execute(
        "SELECT transcode_format FROM devices WHERE id = ?", (new_device_id,)
    ).fetchone()["transcode_format"]

    conn.execute(
        "UPDATE devices SET transcode_format=?, max_size_bytes=?, autofit_percent=?, "
        "artist_images=?, source_of_truth=?, play_playlist_id=? WHERE id=?",
        (old["transcode_format"], old["max_size_bytes"], old["autofit_percent"],
         old["artist_images"], old["source_of_truth"], old["play_playlist_id"], new_device_id),
    )
    # The old device's Home widget set comes too, unless the new one already
    # has one of its own: that is a choice made on the new device.
    conn.execute(
        "UPDATE devices SET dashboard_widgets = "
        "(SELECT dashboard_widgets FROM devices WHERE id = ?) "
        "WHERE id = ? AND dashboard_widgets IS NULL",
        (old_device_id, new_device_id),
    )
    if old["transcode_format"] != new_prev_transcode:
        conn.execute(
            "UPDATE device_track_state SET status='pending', bytes_on_device=NULL, "
            "updated_at=datetime('now') WHERE device_id=? AND status='downloaded'",
            (new_device_id,),
        )

    track_count = conn.execute(
        "SELECT COUNT(*) AS n FROM device_track_state WHERE device_id = ?", (old_device_id,)
    ).fetchone()["n"]
    # assume_present=False downgrades a transferred 'downloaded' to
    # 'pending' (and drops the now-stale bytes_on_device) right in the
    # same INSERT -- everything else (pending/excluded/removed) passes
    # through untouched either way, since only 'downloaded' is a presence
    # claim worth doubting.
    status_expr = "status" if assume_present else "CASE WHEN status='downloaded' THEN 'pending' ELSE status END"
    bytes_expr = ("bytes_on_device" if assume_present
                  else "CASE WHEN status='downloaded' THEN NULL ELSE bytes_on_device END")
    conn.execute(
        f"INSERT OR REPLACE INTO device_track_state "
        f"(device_id, track_id, status, updated_at, bytes_on_device, delivered_path) "
        f"SELECT ?, track_id, {status_expr}, updated_at, {bytes_expr}, delivered_path "
        f"FROM device_track_state WHERE device_id = ?",
        (new_device_id, old_device_id),
    )

    selection_ids = [r["selection_id"] for r in conn.execute(
        "SELECT selection_id FROM selection_devices WHERE device_id = ?", (old_device_id,)
    )]
    for selection_id in selection_ids:
        conn.execute(
            "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
            (selection_id, new_device_id),
        )
    conn.execute("DELETE FROM selection_devices WHERE device_id = ?", (old_device_id,))
    # Basket items staged for the old device are staged for the new one, the
    # same way. Left to the cascade below, an item staged only for the old
    # device would keep no link: the basket can't show it, send it or clear it.
    conn.execute(
        "INSERT OR IGNORE INTO basket_item_devices (basket_item_id, device_id) "
        "SELECT basket_item_id, ? FROM basket_item_devices WHERE device_id = ?",
        (new_device_id, old_device_id),
    )
    conn.execute("DELETE FROM basket_item_devices WHERE device_id = ?", (old_device_id,))

    # Playlists made on the old device become the new one's: same rows (so
    # their selections, sharing and mirrors stay), keyed on the new id.
    # The key is "<device id>:<path>" (playlist_sync.device_playlist_id).
    old_prefix = f"{old_device_id}:"
    conn.execute(
        "UPDATE playlists SET source_playlist_id = ? || substr(source_playlist_id, ?) "
        "WHERE source_provider = 'device' AND substr(source_playlist_id, 1, ?) = ?",
        (f"{new_device_id}:", len(old_prefix) + 1, len(old_prefix), old_prefix),
    )

    # Cascades cleanup of anything left tied to old_device_id (its own
    # device_track_state rows -- the source rows the INSERT OR REPLACE
    # above read from, never touched directly -- plus device_unknown_tracks
    # and device_pins, neither of which has anywhere meaningful to move to).
    conn.execute("DELETE FROM devices WHERE id = ?", (old_device_id,))
    # NOT cascaded, because it is a JSON column rather than a join table --
    # and remapped rather than pruned, for the same reason selection_devices
    # above is moved rather than dropped: the replacement device stands in
    # for the old one, so a surface that used to send there still should.
    remap_device_in_last_destinations(conn, old_device_id, new_device_id, commit=False)
    conn.commit()

    recompute_device_state(conn, new_device_id)

    return {"old_device_name": old["name"], "tracks": track_count, "selections": len(selection_ids)}


def device_playlist_ids(conn: sqlite3.Connection, device_id: int) -> set[int]:
    return {r[0] for r in conn.execute(_DEVICE_PLAYLIST_IDS_SQL, (device_id,))}


def record_device_manifest(conn: sqlite3.Connection, device_id: int, paths: list) -> dict:
    """#63 recovery path: a re-paired device uploads the relative paths it
    already holds; match them against the (non-deleted) library and mark the
    matched tracks 'downloaded' so they aren't re-fetched after a server-DB
    loss. Returns {"matched": n, "unmatched": n} — unmatched = paths not in the
    library (content the server doesn't know about), also stored on the device
    row for the web UI to surface. Idempotent (re-uploading is safe).

    Ordering is the CLIENT's responsibility (the enrollment wizard, #162): this
    only helps if source_of_truth='device' (#155) is already set — otherwise the
    next recompute prunes the tracks it just marked 'downloaded'. This function
    is deliberately agnostic to that; it assumes the caller sequenced it.

    Contract: `paths` are the wire/on-disk paths the device actually downloaded
    — i.e. device_path() form (Artiste/Album/[CDn-]NN - Titre.ext, transcoded
    extension included), NOT the catalog's tracks.relative_path. get_changes
    only ever hands the device a device_path(), so a reinstalled device walking its
    own SAF folder can produce nothing else; matching against relative_path (the
    source layout — disc subfolders, "(year)" suffixes) would match ~nothing,
    and never for a transcoding device (.mp3 on disk vs .flac source). So we
    rebuild each live track's device_path() with THIS device's transcode_format
    and match on that. Duplicate paths are de-duplicated, so the returned counts
    reflect distinct paths."""
    seen_paths = list(dict.fromkeys(paths))  # dedup, preserve order
    by_device_path = {p: row["id"] for p, row in device_path_index(conn, device_id).items()}
    # The path the device reported is the file it holds: recorded as
    # delivered_path, so a later deletion names it.
    matched: list[tuple[int, str]] = [
        (by_device_path[p], p) for p in seen_paths if p in by_device_path]
    matched_track_ids = [track_id for track_id, _path in matched]
    for track_id, path in matched:
        conn.execute(
            "INSERT INTO device_track_state (device_id, track_id, status, delivered_path) "
            "VALUES (?, ?, 'downloaded', ?) "
            "ON CONFLICT(device_id, track_id) DO UPDATE SET "
            "status='downloaded', delivered_path=excluded.delivered_path, updated_at=datetime('now')",
            (device_id, track_id, path),
        )
    # #161: persist the unmatched device paths as reviewable "device extras",
    # not just a count — so the owner can see WHAT they are and adopt them.
    unmatched_paths = [p for p in seen_paths if p not in by_device_path]
    _record_device_unknown_tracks(conn, device_id, unmatched_paths)
    conn.commit()
    return {"matched": len(matched_track_ids), "unmatched": len(unmatched_paths)}


def device_path_index(conn: sqlite3.Connection, device_id: int) -> dict[str, sqlite3.Row]:
    """Every live library track keyed by its device_path() on this device
    (its transcode_format applied): the form get_changes emits, so a path the
    device reports from its own storage matches exactly, extension and all.
    Rows carry id, artist, album_artist, album, title, track_no, disc_no,
    relative_path, lossless.

    Each track is there under its legacy_device_path() too: a device's files
    from before the album-artist layout keep their names, and a manifest or
    a card playlist names them so. Where a legacy name is also another
    track's current name, the current one wins."""
    fmt_row = conn.execute(
        "SELECT transcode_format FROM devices WHERE id = ?", (device_id,)).fetchone()
    fmt = fmt_row["transcode_format"] if fmt_row is not None else None
    rows = conn.execute(
        "SELECT id, artist, album_artist, album, title, track_no, disc_no, relative_path, lossless "
        "FROM tracks WHERE deleted_at IS NULL"
    ).fetchall()
    index = {legacy_device_path(row, fmt): row for row in rows}
    index.update({device_path(row, fmt): row for row in rows})
    return index


def resolve_card_entry(entry: str, playlist_path: str, index: dict) -> str | None:
    """The device_path() an entry of a playlist found on the device names,
    or None. `playlist_path` is the playlist file's own path, relative to the
    device's sync root; `index` is device_path_index(). Every device_path()
    is exactly artist/album/file, so:
    - an entry with at least three segments matches by its last three. That
      covers entries relative to the sync root (`Artist/Album/x.flac`), to
      the playlist's folder (`../Artist/Album/x.flac`), and absolute ones
      from the player's own mount (`/mnt/sdcard/Artist/Album/x.flac`);
    - a shorter one is resolved against the playlist's folder: a playlist
      saved in an album folder listing bare file names, or in an artist
      folder listing `Album/x.flac`.
    Backslashes count as separators (Windows-made playlists)."""
    raw = entry.strip().replace("\\", "/")
    parts = [p for p in raw.split("/") if p and p != "."]
    if len(parts) >= 3:
        tail = "/".join(parts[-3:])
        return tail if tail in index else None
    if not parts or raw.startswith("/"):
        return None
    beside = posixpath.normpath(posixpath.join(posixpath.dirname(playlist_path), *parts))
    return beside if beside in index else None


def add_device_unknown_tracks(conn: sqlite3.Connection, device_id: int, paths: list) -> None:
    """Adds paths to this device's unknown tracks without dropping the ones
    already there (unlike _record_device_unknown_tracks, which replaces the
    set from a full manifest), then refreshes the count. Used for playlist
    entries on the device that match no library track. Does not commit."""
    for path in dict.fromkeys(paths):
        artist, album, title = _parse_device_path(path)
        conn.execute(
            "INSERT INTO device_unknown_tracks (device_id, path, artist, album, title) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(device_id, path) DO NOTHING",
            (device_id, path, artist, album, title),
        )
    _refresh_unknown_track_count(conn, device_id)


def device_playlist_filenames(conn: sqlite3.Connection, device_id: int) -> set[str]:
    """The playlist files Trobar itself writes to this device's sync root."""
    fmt = conn.execute("SELECT transcode_format FROM devices WHERE id = ?", (device_id,)).fetchone()
    return {p["filename"] for p in _device_playlists(conn, device_id, fmt["transcode_format"] if fmt else None)}


def _parse_device_path(path: str) -> tuple[str | None, str | None, str | None]:
    """#161: best-effort (artist, album, title) from a _device_path
    (Artiste/Album/[CDn-]NN - Titre.ext), for displaying an unknown track in the
    web UI. Any field may be None when the shape doesn't fit — the raw path is
    always kept alongside, so a bad parse just means less-pretty display."""
    parts = path.split("/")
    if len(parts) < 3:
        return (None, None, None)
    stem = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
    m = re.match(r"^(?:CD\d+-)?\d+ - (.+)$", stem)  # drop a leading [CDn-]NN - prefix
    title = m.group(1) if m else stem
    return (parts[0] or None, parts[-2] or None, title or None)


def _record_device_unknown_tracks(conn: sqlite3.Connection, device_id: int,
                                  unmatched_paths: list) -> None:
    """#161: replace this device's unknown set with the current unmatched paths,
    preserving the `adopted` flag on paths that persist; paths that now match (or
    left the device) are dropped. Then refresh devices.unknown_track_count to the
    NON-adopted count — an adopted extra is acknowledged and no longer flagged."""
    keep = list(dict.fromkeys(unmatched_paths))  # dedup, preserve order
    if keep:
        placeholders = ",".join("?" * len(keep))
        conn.execute(
            f"DELETE FROM device_unknown_tracks WHERE device_id = ? AND path NOT IN ({placeholders})",
            (device_id, *keep),
        )
    else:
        conn.execute("DELETE FROM device_unknown_tracks WHERE device_id = ?", (device_id,))
    for path in keep:
        artist, album, title = _parse_device_path(path)
        conn.execute(
            "INSERT INTO device_unknown_tracks (device_id, path, artist, album, title) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(device_id, path) DO NOTHING",
            (device_id, path, artist, album, title),
        )
    _refresh_unknown_track_count(conn, device_id)


def _refresh_unknown_track_count(conn: sqlite3.Connection, device_id: int) -> int:
    """Set devices.unknown_track_count to the count of NON-adopted unknowns (the
    ones still flagged for review). Returns that count."""
    count = conn.execute(
        "SELECT COUNT(*) FROM device_unknown_tracks WHERE device_id = ? AND adopted = 0",
        (device_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE devices SET unknown_track_count = ? WHERE id = ?", (count, device_id))
    return count


def list_device_unknown_tracks(conn: sqlite3.Connection, device_id: int) -> list:
    """#161: the device's unknown extras for the web review list — un-adopted
    first, then by parsed artist/album/title."""
    rows = conn.execute(
        "SELECT path, artist, album, title, adopted, first_seen_at "
        "FROM device_unknown_tracks WHERE device_id = ? "
        "ORDER BY adopted, artist, album, title, path",
        (device_id,),
    ).fetchall()
    return [{**dict(r), "adopted": bool(r["adopted"])} for r in rows]


def set_device_unknown_adopted(conn: sqlite3.Connection, device_id: int,
                               paths: list, adopted: bool) -> int:
    """#161: mark the given device paths adopted (acknowledged as device-owned
    extras the server records but never manages) or un-adopted, then refresh the
    flagged count. Returns the resulting unknown_track_count (non-adopted)."""
    seen = list(dict.fromkeys(paths))
    if seen:
        placeholders = ",".join("?" * len(seen))
        conn.execute(
            f"UPDATE device_unknown_tracks SET adopted = ? "
            f"WHERE device_id = ? AND path IN ({placeholders})",
            (1 if adopted else 0, device_id, *seen),
        )
    count = _refresh_unknown_track_count(conn, device_id)
    conn.commit()
    return count


# the marker line clients use to tell Trobar-managed playlist
# files from ones the user put on the card themselves (only marked files are
# ever deleted when a playlist unassigns).
M3U_MARKER = "# Generated by Trobar"


def _device_playlists(conn: sqlite3.Connection, device_id: int,
                      transcode_format: str | None) -> list[dict]:
    return [p for _, p in _device_playlists_by_id(conn, device_id, transcode_format)]


def _device_playlists_by_id(conn: sqlite3.Connection, device_id: int,
                            transcode_format: str | None) -> list[tuple[int, dict]]:
    """one.m3u8 per playlist selection assigned to this device, each with
    its playlist id:
    same name, playlist order, only tracks that are (or will be after this
    sync) actually on the device, paths as the device knows them (transcoded
    extension included). Empty playlists (no local matches on this device)
    are omitted, which also stale-deletes their file client-side."""
    out: list[tuple[int, dict]] = []
    used_names: set[str] = set()
    playlists = conn.execute(
        "SELECT DISTINCT p.id, p.title FROM selections s "
        "JOIN selection_devices sd ON sd.selection_id = s.id "
        "JOIN playlists p ON p.id = CAST(s.target AS INTEGER) "
        "WHERE s.type = 'playlist' AND sd.device_id = ? ORDER BY p.title",
        (device_id,),
    ).fetchall()
    for pl in playlists:
        entries = conn.execute(
            "SELECT t.id, t.artist, t.album_artist, t.album, t.title, t.track_no, t.disc_no, "
            "t.relative_path, t.lossless, t.duration, dts.status, dts.delivered_path "
            "FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.matched_track_id "
            "JOIN device_track_state dts ON dts.track_id = t.id AND dts.device_id = ? "
            "WHERE pt.playlist_id = ? AND t.deleted_at IS NULL "
            "AND dts.status IN ('pending', 'downloaded') "
            "ORDER BY pt.position",
            (device_id, pl["id"]),
        ).fetchall()
        if not entries:
            continue
        lines = ["#EXTM3U", M3U_MARKER, f"#PLAYLIST:{pl['title']}"]
        for row in entries:
            duration = int(row["duration"]) if row["duration"] else -1
            lines.append(f"#EXTINF:{duration},{row['artist']} - {row['title']}")
            lines.append(on_device_path(row, transcode_format))
        filename = f"{fs_segment(pl['title'])}.m3u8"
        if filename in used_names:  # two titles sanitising to the same name
            filename = f"{fs_segment(pl['title'])} ({pl['id']}).m3u8"
        used_names.add(filename)
        out.append((pl["id"], {"name": pl["title"], "filename": filename,
                               "content": "\n".join(lines) + "\n"}))
    return out


# The most album covers a device is offered (trobar-garmin#54): what a watch
# was measured to hold.
COVER_ALBUMS_MAX = 60


def cover_albums(conn: sqlite3.Connection, device_id: int) -> list[dict]:
    """The albums of the tracks on (or coming to) this device, for their
    covers: {"key", "track_ids", "track_id"}, at most COVER_ALBUMS_MAX.
    Albums with a track in one of the device's playlists come first, in the
    playlists' title order, then the rest by their most recent sync. `key`
    is stable and short (the album artist, else the artist, and the album),
    `track_ids` the device's tracks in it, so a device that never stored
    which album a track belongs to learns it here; `track_id` is the one the
    cover is fetched by."""
    rows = conn.execute(
        "SELECT t.id, COALESCE(NULLIF(t.album_artist, ''), t.artist) AS artist, t.album, "
        "dts.updated_at FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
        "WHERE dts.device_id = ? AND dts.status IN ('pending', 'downloaded') "
        "AND t.deleted_at IS NULL AND COALESCE(t.album, '') != '' ORDER BY t.id",
        (device_id,)).fetchall()
    in_playlists = [r["id"] for r in conn.execute(
        "SELECT pt.matched_track_id AS id FROM selections s "
        "JOIN selection_devices sd ON sd.selection_id = s.id "
        "JOIN playlists p ON p.id = CAST(s.target AS INTEGER) "
        "JOIN playlist_tracks pt ON pt.playlist_id = p.id "
        "WHERE s.type = 'playlist' AND sd.device_id = ? AND pt.matched_track_id IS NOT NULL "
        "ORDER BY p.title, pt.position", (device_id,))]
    albums: dict[str, dict] = {}
    for r in rows:
        key = hashlib.sha256(f"{r['artist'] or ''}\x1f{r['album']}".encode("utf-8")).hexdigest()[:16]
        album = albums.setdefault(key, {"key": key, "track_ids": [], "track_id": r["id"], "latest": ""})
        album["track_ids"].append(r["id"])
        album["latest"] = max(str(album["latest"]), str(r["updated_at"] or ""))
    by_track = {tid: a["key"] for a in albums.values() for tid in a["track_ids"]}
    order: list[str] = []
    for tid in in_playlists:
        album_key = by_track.get(tid)
        if album_key is not None and album_key not in order:
            order.append(album_key)
    rest = sorted((a for a in albums.values() if a["key"] not in order),
                  key=lambda a: (a["latest"], a["key"]), reverse=True)
    order += [a["key"] for a in rest]
    return [{k: albums[key][k] for k in ("key", "track_ids", "track_id")}
            for key in order[:COVER_ALBUMS_MAX]]


def get_changes(conn: sqlite3.Connection, device_id: int) -> dict:
    # on a device with transcode_format set, every expected
    # on-device path below carries the target extension for lossless
    # sources — the missing-file spot check, deletes and dedup all key off
    # these server-computed names, so the transcoded reality on the card
    # never drifts from the server's bookkeeping.
    device = conn.execute(
        "SELECT transcode_format, play_playlist_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    fmt = device["transcode_format"]
    to_download = [
        {"track_id": row["id"], "relative_path": device_path(row, fmt), "size": row["size"],
         "transcode": wants_transcode(row, fmt)}
        for row in conn.execute(
            "SELECT t.id, t.artist, t.album_artist, t.album, t.title, t.track_no, t.disc_no, "
            "t.relative_path, t.lossless, t.size "
            "FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status = 'pending'",
            (device_id,),
        )
    ]
    # A deletion names the file as it was delivered, when that's recorded:
    # after a format change, a retag or the album-artist layout the computed
    # name is a file the device never received. A removed row without one
    # was either never delivered or delivered under the legacy layout, so
    # that name is the one to give.
    to_delete = [
        {"track_id": row["id"], "relative_path": row["delivered_path"] or legacy_device_path(row, fmt)}
        for row in conn.execute(
            "SELECT t.id, t.artist, t.album, t.title, t.track_no, t.disc_no, t.relative_path, t.lossless, "
            "dts.delivered_path "
            "FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status = 'removed'",
            (device_id,),
        )
    ]
    # Everything the server currently believes is already on-device. Not
    # acted on here — the client uses this purely to spot-check that each
    # file still actually exists locally before trusting this
    # bookkeeping, since nothing else here ever verifies that.
    downloaded = [
        {"track_id": row["id"], "relative_path": on_device_path(row, fmt), "size": row["size"]}
        for row in conn.execute(
            "SELECT t.id, t.artist, t.album_artist, t.album, t.title, t.track_no, t.disc_no, "
            "t.relative_path, t.lossless, t.size, dts.status, dts.delivered_path "
            "FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
            "WHERE dts.device_id = ? AND dts.status = 'downloaded'",
            (device_id,),
        )
    ]
    playlists = _device_playlists_by_id(conn, device_id, fmt)
    # The Play choice as the filename of one of `playlists`, or None for
    # Everything: also None when that playlist has no file on this device
    # (none of its tracks are here), since there is nothing to play.
    play_playlist = next((p["filename"] for pid, p in playlists if pid == device["play_playlist_id"]), None)
    return {"to_download": to_download, "to_delete": to_delete, "downloaded": downloaded,
            "playlists": [p for _, p in playlists], "play_playlist": play_playlist,
            # The format the flags/names above were computed with — clients
            # must transcode to exactly this, not to a device-info value
            # fetched at some other moment (a mid-session format change made
            # a desktop client encode at the old bitrate; found live).
            "transcode_format": fmt}


def ack(conn: sqlite3.Connection, device_id: int, track_id: int, status: str,
        bytes_on_device: int | None = None) -> bool:
    """Records a device's report that it downloaded or removed a track.
    Conditional on the row still being in the state the device was told
    about: a recompute between the device's /changes and its ack (a track
    re-selected while it was being deleted, or deselected while it was
    downloading) wins, and the next /changes tells the device again.
    Returns whether the ack applied; a stale one is logged and ignored."""
    cur = None
    if status == "downloaded":
        # bytes_on_device is what actually landed on the device — on a
        # transcoding device that's the MP3's size, not the original's.
        # Clients that don't report it leave NULL; usage math then falls back
        # to tracks.size. delivered_path is the file it landed as, so a later
        # deletion names that file even after a format change or a retag.
        row = conn.execute(
            "SELECT t.artist, t.album_artist, t.album, t.title, t.track_no, t.disc_no, t.relative_path, "
            "t.lossless, d.transcode_format FROM tracks t, devices d WHERE t.id = ? AND d.id = ?",
            (track_id, device_id),
        ).fetchone()
        delivered = device_path(row, row["transcode_format"]) if row is not None else None
        # A row already downloaded keeps the name it has: an ack repeated for
        # it is not a new file.
        cur = conn.execute(
            "UPDATE device_track_state SET status='downloaded', bytes_on_device=?, "
            "delivered_path=CASE WHEN status = 'downloaded' AND delivered_path IS NOT NULL "
            "THEN delivered_path ELSE ? END, updated_at=datetime('now') "
            "WHERE device_id=? AND track_id=? AND status IN ('pending', 'downloaded')",
            (bytes_on_device, delivered, device_id, track_id),
        )
    elif status == "removed":
        cur = conn.execute(
            "DELETE FROM device_track_state WHERE device_id=? AND track_id=? AND status='removed'",
            (device_id, track_id),
        )
    conn.commit()
    if cur is None or cur.rowcount == 0:
        _log.info("ignored a stale %r ack from device %d for track %d", status, device_id, track_id)
        return False
    return True


def resolve_missing_tracks(conn: sqlite3.Connection, device_id: int,
                            redownload_ids: list[int], exclude_ids: list[int]) -> None:
    """the client found tracks the server believes are
    `downloaded` actually missing on disk, and the user (or a standing
    client-side preference) decided what to do about each: re-fetch them, or
    leave them deleted. `excluded` gets the same "untouched while still
    required" treatment as `pending`/`downloaded` in recompute_device_state,
    so it's never silently re-queued — but still cleans up the normal way
    if a later selection change actually drops the requirement."""
    if redownload_ids:
        placeholders = ",".join("?" * len(redownload_ids))
        conn.execute(
            f"UPDATE device_track_state SET status='pending', updated_at=datetime('now') "
            f"WHERE device_id=? AND track_id IN ({placeholders})",
            (device_id, *redownload_ids),
        )
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        conn.execute(
            f"UPDATE device_track_state SET status='excluded', updated_at=datetime('now') "
            f"WHERE device_id=? AND track_id IN ({placeholders})",
            (device_id, *exclude_ids),
        )
    conn.commit()


_FS_ILLEGAL = re.compile(r'[\\*?"<>|\x00-\x1f]')


def fs_segment(name: str) -> str:
    """Make a tag value safe as one path segment on every filesystem/API a
    client writes to. Android's MediaProvider enforces FAT-style
    naming at the FUSE layer even on ext4-backed internal storage (a ':' in
    an album title made SAF createDirectory fail with EPERM on a real
    album, "Song Machine, Season One: Strange Timez"); DAP SD cards are
    usually exFAT; Windows has the same reserved set. ':' gets the readable
    ' - ' treatment since it's overwhelmingly a subtitle separator in album/
    track titles; the rarer reserved characters just become '_'. FAT also
    forbids trailing dots/spaces in a segment.

    Public (#285): also used by mirror.py for the first, human-readable
    pass on a playlist title before werkzeug.secure_filename() — path
    containment for a second module now depends on this, not just this
    one's own device-`.m3u8` naming, so it stays a shared, deliberately
    stable util rather than a private implementation detail either module
    could quietly change."""
    name = name.replace("/", "-")
    name = name.replace(": ", " - ").replace(":", "-")
    name = _FS_ILLEGAL.sub("_", name)
    name = name.strip().rstrip(".")
    return name or "_"


# extensions we confidently know are lossless. .m4a is deliberately
# absent — it can hold ALAC (lossless) or AAC (lossy) and the extension can't
# tell them apart, so .m4a is always copied as-is rather than risk
# re-encoding an already-lossy file.
_LOSSLESS_EXTS = {"flac", "wav", "aiff", "aif"}


_TRANSCODE_EXT = {"mp3_320": "mp3", "mp3_256": "mp3", "mp3_192": "mp3", "mp3_128": "mp3"}


TRANSCODE_FORMATS = frozenset(_TRANSCODE_EXT)


def _source_ext(relative_path: str) -> str:
    # Case preserved: existing devices' expected paths must not change for a
    # file named .FLAC just because this code landed (lowercasing only
    # happens for the lossless-set membership test).
    return relative_path.rsplit(".", 1)[-1] if "." in relative_path else "flac"


def _is_lossless(row) -> bool:
    """Whether a track is lossless: tracks.lossless, which the scan reads
    from the file (ALAC in an .m4a included). Read unconditionally, so a
    query that feeds this without selecting the column fails loudly rather
    than naming an ALAC track's file differently from everywhere else. NULL
    (a library from before the column, until its next scan) falls back to
    the extension."""
    lossless = row["lossless"]
    if lossless is None:
        return _source_ext(row["relative_path"]).lower() in _LOSSLESS_EXTS
    return bool(lossless)


def backfill_delivered_paths(conn: sqlite3.Connection) -> int:
    """Record, for each track a device holds with no delivered_path (it was
    delivered before deliveries recorded one), the name it has on the
    device: legacy_device_path(), the layout every delivery used before
    files were grouped by album artist. Done once, before the layout can change, so a
    deletion still names the file the device has. Only 'downloaded' rows: a
    'removed' row without a path may never have been delivered at all (see
    recompute_device_state), and delivered_path is the proof that it was.
    Does not commit. Returns how many rows it filled."""
    rows = conn.execute(
        "SELECT dts.device_id, dts.track_id, d.transcode_format, t.artist, t.album_artist, t.album, t.title, "
        "t.track_no, t.disc_no, t.relative_path, t.lossless "
        "FROM device_track_state dts JOIN tracks t ON t.id = dts.track_id "
        "JOIN devices d ON d.id = dts.device_id "
        "WHERE dts.status = 'downloaded' AND dts.delivered_path IS NULL"
    ).fetchall()
    conn.executemany(
        "UPDATE device_track_state SET delivered_path = ? WHERE device_id = ? AND track_id = ?",
        [(legacy_device_path(r, r["transcode_format"]), r["device_id"], r["track_id"]) for r in rows])
    if rows:
        _log.info("recorded the on-device name of %d track(s) delivered before names were recorded", len(rows))
    return len(rows)


def wants_transcode(row: sqlite3.Row, transcode_format: str | None) -> bool:
    """True when the client should transcode this track rather than copy it:
    the device asks for a format AND the source is lossless."""
    return transcode_format in _TRANSCODE_EXT and _is_lossless(row)


def device_path(row: sqlite3.Row, transcode_format: str | None = None) -> str:
    """Wire/on-disk relative path for a device — always AlbumArtist/Album/NN -
    Titre.ext (the album artist, else the track's artist), regardless of the on-disk catalog path's actual folder
    structure (which may have disc subfolders, parenthetical years, etc).

    Public (#239): this is the device-facing path CONTRACT, not an internal
    detail — get_changes, record_device_manifest and main.py's
    /api/device/fingerprints all have to produce byte-identical strings or a
    client can't correlate a fingerprint with the file it wrote. Now that a
    third caller outside this module depends on it, it stays a deliberately
    stable shared util rather than a private helper someone could quietly
    change (same reasoning as fs_segment's promotion in #285).

    Discnumber is folded into the track number prefix (CD2-01 instead of just
    01) whenever the tag is present — without it, a multi-disc album whose
    discs both start numbering at 1 would silently collide/overwrite on the
    device (found on a real album in testing: a bonus disc and the main
    disc, both tagged with the same plain album name).

    On a transcoding device lossless sources get the target
    format's extension — the server always names files by what should
    actually be on the device.

    The name for a NEW delivery. A track already on a device keeps the name
    it was delivered under (device_track_state.delivered_path): clients match
    files by path only and can't rename, so every path the server gives for
    a delivered track is that one."""
    return _device_path(row, transcode_format, row["album_artist"] or row["artist"])


def on_device_path(row: sqlite3.Row, transcode_format: str | None) -> str:
    """The name a track has on a device: the one it was delivered under for
    a 'downloaded' row that recorded it, else the name a new delivery gets.
    The row carries status and delivered_path beside the track's columns."""
    if row["status"] == "downloaded" and row["delivered_path"]:
        return row["delivered_path"]
    return device_path(row, transcode_format)


def legacy_device_path(row: sqlite3.Row, transcode_format: str | None = None) -> str:
    """device_path() before files were grouped by album artist: the track's
    own artist as the folder. What a file delivered before then is called,
    when no delivered_path recorded it."""
    return _device_path(row, transcode_format, row["artist"])


def _device_path(row: sqlite3.Row, transcode_format: str | None, folder: str) -> str:
    ext = _source_ext(row["relative_path"])
    # wants_transcode() being True already implies transcode_format is a real
    # key (it checks `transcode_format in _TRANSCODE_EXT`) — the redundant
    # `transcode_format and` just narrows it from str | None for mypy too.
    if transcode_format and wants_transcode(row, transcode_format):
        ext = _TRANSCODE_EXT[transcode_format]
    if row["track_no"] and row["disc_no"]:
        prefix = f"CD{row['disc_no']}-{row['track_no']:02d} - "
    elif row["track_no"]:
        prefix = f"{row['track_no']:02d} - "
    else:
        prefix = ""
    artist = fs_segment(folder)
    album = fs_segment(row["album"])
    title = fs_segment(row["title"])
    return f"{artist}/{album}/{prefix}{title}.{ext}"
