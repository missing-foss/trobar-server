#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pulls the active provider's playlists + tracks into playlists/
playlist_tracks, resolving each track against the local catalog via
identity.py's tiered resolver (#200; matching.py underneath, unchanged).
Provider-agnostic — takes the active provider module (roon_client or
subsonic_client) as a parameter rather than importing one directly, so
callers (main.py's _active_provider() dispatch) decide which is active.

filesystem_client's own .m3u/.m3u8 discovery is always merged in alongside
whichever provider is active — a hand-curated playlist file never
imported into Roon/Navidrome/Jellyfin still shows up here. Skipped when
filesystem *is* the active provider (nothing to merge, it's already the only
source), and any title collision with the active provider's own playlist is
left alone — its curated copy wins over the filesystem-discovered one.

A configured Music Assistant server merges in the same way, right after the
filesystem files: its playlists are an extra source, never the active
provider, so it is merged whichever provider is active.

Direct per-user Tidal accounts (#21) merge in the same unconditional way,
regardless of the active provider — see the dedicated block near the end of
sync_playlists().

#262: Trobar users individually mapped to their own account on the active
Jellyfin/Emby server (Administration > Configuration) merge in too, same
"still lands in the one shared pool, attributed via owner_user_id" shape
as the existing Roon-profile mapping — but gated on that sink actually
being the active provider (unlike Tidal above), since the mapping only
means something once its own server is the configured source."""

import json
import logging
import posixpath
import uuid

import album_lookup
import db
import emby_client
import filesystem_client
import identity
import jellyfin_client
import jobs
import lidarr_requests
# The sink modules are reached through mirror_sinks; tests still patch them
# as playlist_sync.mirror_x, the same module objects.
import mirror  # noqa: F401
import mirror_emby  # noqa: F401
import mirror_jellyfin  # noqa: F401
import mirror_music_assistant  # noqa: F401
import mirror_plex  # noqa: F401
import mirror_notes
import mirror_sinks
import mirror_subsonic  # noqa: F401
import music_assistant_client
import roon_client
import rule_playlists
import source_health
import spotify_client
import spotify_public_client
import subsonic_client
import tidal_public_client
import sync_state
import credential_store
import tidal_client
import tidal_token
import tidal_shares
import ytmusic_client

_log = logging.getLogger(__name__)

# #297 step 3: same durable-queue upgrade scanner.py already got — a sync now
# gets a row in `jobs` (survives a restart, observable/retryable from the
# admin panel) instead of a bare module-level lock + daemon thread.
#
# The handler itself is NOT registered here. It needs to resolve the active
# provider to a live provider module, and only main.py's _PROVIDERS dict can do
# that (see jobs.register's docstring: cross-module wiring lives in main.py,
# not in the module that does the work — same reason
# provenance.ensure_library_fingerprints's wrapper lives there too).
JOB_TYPE = "playlist_sync"
# One sync queued or running at a time, enforced by the partial unique index
# on jobs.dedupe_key — replaces _SYNC_LOCK. Not per-provider: there's exactly
# one active provider at a time (see main.py's _PROVIDERS/_active_provider).
JOB_DEDUPE = "playlist_sync"


def sync_status() -> dict:
    """{"running": bool, "last_result": <the last completed run's result, or
    None>}. #138: lets the UI poll to know when a backgrounded sync (started
    via start_sync) has finished, and show its counts. Same shape and same
    #141 "a poll during a run must not report the previous run's counts"
    property as scanner.scan_status(), read from the jobs table instead of a
    lock + a hand-rolled last-result global."""
    conn = db.get_conn()
    try:
        pending = conn.execute(
            "SELECT 1 FROM jobs WHERE type = ? AND state IN ('queued', 'running') "
            "LIMIT 1", (JOB_TYPE,)).fetchone()
        finished = conn.execute(
            "SELECT state, result, last_error FROM jobs WHERE type = ? "
            "AND state IN ('done', 'failed', 'cancelled') ORDER BY id DESC LIMIT 1",
            (JOB_TYPE,)).fetchone()
    finally:
        conn.close()

    last_result = None
    if pending is not None:
        finished = None
    if finished is not None:
        if finished["state"] == "cancelled":
            last_result = {"status": "error", "reason": "cancelled"}
        elif finished["state"] == "failed":
            last_result = {"status": "error", "reason": finished["last_error"] or "error"}
        elif finished["result"]:
            last_result = json.loads(finished["result"])
    return {"running": pending is not None, "last_result": last_result}


def _config_hours(conn) -> int:
    raw = db.get_config(conn, "playlist_sync_interval_hours")
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


def _last_finished_at(conn) -> str | None:
    """When the last sync ended, successfully or not. A failed sync counts,
    unlike scanner's last *done* scan: a provider that is down would
    otherwise make the schedule due again on every idle poll (30 s), and
    retry it that often for as long as it stays down. A cancelled one counts
    too, or the schedule would start again the sync an admin just stopped. jobs._prune_finished
    keeps the newest row per (type, state), so this survives retention."""
    row = conn.execute(
        "SELECT finished_at FROM jobs WHERE type = ? AND state IN ('done', 'failed', 'cancelled') "
        "ORDER BY finished_at DESC LIMIT 1", (JOB_TYPE,),
    ).fetchone()
    return row["finished_at"] if row is not None else None


def next_scheduled_sync_at(conn) -> str | None:
    """When the next scheduled sync becomes due, as a UTC datetime string
    (same shape as jobs.finished_at), or None when scheduling is off
    (playlist_sync_interval_hours <= 0) or nothing has synced yet (due now,
    so there is no future timestamp to show). The twin of
    scanner.next_scheduled_scan_at."""
    hours = _config_hours(conn)
    if hours <= 0:
        return None
    last = _last_finished_at(conn)
    if last is None:
        return None
    return conn.execute(
        "SELECT datetime(?, ?)", (last, f"+{hours} hours"),
    ).fetchone()[0]


def sync_job_state(conn) -> str | None:
    """'running' or 'queued' when a sync job is in either state (running
    wins), None otherwise."""
    row = conn.execute(
        "SELECT state FROM jobs WHERE type = ? AND state IN ('queued', 'running') "
        "ORDER BY state = 'running' DESC LIMIT 1", (JOB_TYPE,),
    ).fetchone()
    return row["state"] if row is not None else None


def sync_due(conn) -> bool:
    """Whether the schedule wants a sync now: enabled, and either nothing
    has synced yet or the interval has passed since the last one ended.
    Measured from the end, not a wall-clock cadence, as for the rescan. A
    sync already queued or running is not this function's concern:
    start_sync's dedupe_key turns a second one into a no-op."""
    hours = _config_hours(conn)
    if hours <= 0:
        return False
    last = _last_finished_at(conn)
    if last is None:
        return True
    return bool(conn.execute(
        "SELECT ? <= datetime('now', ?)", (last, f"-{hours} hours"),
    ).fetchone()[0])


def start_sync(provider, provider_id: str) -> dict:
    """Queue a sync and return immediately.

    `provider` is accepted for call-site compatibility — main.py's route
    already resolves both it and `provider_id` from the same _PROVIDERS
    lookup — but deliberately NOT stored in the payload, matching
    scanner.start_scan's `root`: only the id is JSON-serializable, and the
    worker re-resolves the live module at run time via main.py's wrapper
    instead of trusting a stale reference.

    Returns {"status": "started", "job_id": ...}, or {"status": "error",
    "already_running": True} when one is already queued or running — the
    dedupe index decides that, not a lock, so it holds across a restart too."""
    conn = db.get_conn()
    try:
        job_id = jobs.enqueue(conn, JOB_TYPE, {"provider_id": provider_id},
                              dedupe_key=JOB_DEDUPE)
    finally:
        conn.close()
    if job_id is None:
        return {"status": "error", "already_running": True}
    return {"status": "started", "job_id": job_id}


_SUBSCRIPTION_CLIENTS = {ytmusic_client.PROVIDER_ID: ytmusic_client,
                         spotify_public_client.PROVIDER_ID: spotify_public_client,
                         tidal_public_client.PROVIDER_ID: tidal_public_client}


def subscription_playlist_key(sub_id: int) -> str:
    """The `playlists.source_playlist_id` a subscription's row carries.

    The subscription's own primary key, NOT the external playlist id, and
    that is deliberate. _sync_one_playlist() finds a row by
    (source_provider, source_playlist_id) with no owner in the lookup, so
    two household members who subscribe to the same public URL would
    otherwise land on one shared row that changes hands on every sync --
    each run reassigning owner_user_id to whoever synced last, and resetting
    the other's sharing choice with it (see the owner_changed branch
    there). Keyed on the subscription instead, each subscriber gets their
    own playlist, which is also what the per-user ownership model already
    promises everywhere else."""
    return str(sub_id)


def sync_one_subscription(conn, sub, *, mirror_caches: dict[str, dict] | None = None) -> dict:
    """Fetches one URL subscription and syncs it, recording the outcome on
    the subscription row either way.

    Returns {"status", "title", "tracks", "matched"} -- `status` is "ok",
    "unavailable" (the remote playlist is gone, private, or never existed)
    or "error" (anything else, including the unofficial endpoint changing
    shape). On a non-ok status NOTHING about the already-synced playlist is
    touched: no rows deleted, no tracks cleared, the previous import left
    exactly as it was. That is the whole contract of this function, and the
    caller's prune protection is its other half -- a playlist somebody has
    already synced onto a device must not evaporate because a
    reverse-engineered endpoint had a bad afternoon.

    `tracks`/`matched` are recorded on the row as well as returned. A
    playlist of hour-long mix videos parses perfectly and matches nothing,
    and 0-of-79 is the only thing that tells a user that apart from a
    successful import of music they simply do not own."""
    client = _SUBSCRIPTION_CLIENTS[sub["provider"]]
    fetched = client.get_playlist_tracks(sub["title"] or "", sub["external_id"])
    if fetched["status"] != "ok":
        status = "unavailable" if fetched["status"] in ("unavailable", "not_found") else "error"
        conn.execute(
            "UPDATE playlist_subscriptions SET last_error = ?, "
            "last_error_at = datetime('now') WHERE id = ?",
            (status, sub["id"]),
        )
        conn.commit()
        return {"status": status, "title": sub["title"], "tracks": 0, "matched": 0}

    # The title is the remote playlist's, re-read on every sync so a rename
    # at the source follows through -- the same thing an id-keyed row gets
    # from any other provider.
    title = fetched.get("playlist") or sub["title"] or sub["external_id"]
    outcome = _sync_one_playlist(
        conn, client, sub["provider"], title,
        source_playlist_id=subscription_playlist_key(sub["id"]),
        owner_user_id=sub["owner_user_id"], prefetched=fetched,
        mirror_caches=mirror_caches,
    )
    tracks, matched = outcome if outcome is not None else (0, 0)
    conn.execute(
        "UPDATE playlist_subscriptions SET title = ?, last_synced_at = datetime('now'), "
        "last_error = NULL, last_error_at = NULL, last_track_count = ?, "
        "last_matched_count = ? WHERE id = ?",
        (title, tracks, matched, sub["id"]),
    )
    conn.commit()
    return {"status": "ok", "title": title, "tracks": tracks, "matched": matched}


def delete_subscription(conn, sub_id: int) -> None:
    """Removes a subscription and the playlist it produced.

    Unlike a fetch failure -- which must leave everything alone -- this is
    the user saying "take it away", so the playlist row goes through the
    same removal path a source-side deletion would, devices included."""
    row = conn.execute(
        "SELECT id, provider FROM playlist_subscriptions WHERE id = ?", (sub_id,)
    ).fetchone()
    if row is None:
        return
    pl = conn.execute(
        "SELECT id FROM playlists WHERE source_provider = ? AND source_playlist_id = ?",
        (row["provider"], subscription_playlist_key(sub_id)),
    ).fetchone()
    if pl is not None:
        _remove_playlist_row(conn, pl["id"])
    conn.execute("DELETE FROM playlist_subscriptions WHERE id = ?", (sub_id,))
    conn.commit()


def _remove_playlist_row(conn, playlist_id: int) -> None:
    """Deletes one playlists row and everything that must go with it.

    Any playlist-type selection targeting it goes through the normal
    delete_selection() path so affected devices are actually told to remove
    the files, same as an explicit user deletion -- not just silently
    orphaned to resolve to nothing on the next sync. #285/#189: every
    sink's mirror is deleted BEFORE the row itself goes away, so a removed
    golden source doesn't leave an orphaned mirror file or remote playlist
    behind.

    Extracted so the stale-cleanup pass and the deliberate removal of a URL
    subscription share one definition of "and everything that goes with
    it". They had drifted apart in every previous provider addition; the
    subscription case is the first where a user, not a source, triggers the
    removal."""
    conn.commit()
    with mirror_sinks.playlist_lock(playlist_id):
        # First, because it commits: the selections and the row then go
        # together, and a crash after it leaves a playlist that is still whole.
        mirror_sinks.delete_all(conn, playlist_id)
        for sel in conn.execute(
            "SELECT id FROM selections WHERE type = 'playlist' AND target = ?", (str(playlist_id),)
        ).fetchall():
            sync_state.delete_selection(conn, sel["id"])
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        # Inside the lock: a write waiting for it must find the row gone, or
        # it would make a copy nothing could ever delete.
        conn.commit()


_MIRROR_COPY_COLUMNS = {"jellyfin": "jellyfin_mirror_remote_id", "emby": "emby_mirror_remote_id"}


def _carry_selections_from_mirror_copy(conn, row) -> None:
    """Before the stale pass prunes a row that was one of Trobar's own
    Jellyfin/Emby mirror copies read back as a source, moves its device
    selections to the playlist it mirrors.

    Older versions listed such a copy as a source when it sat in an account
    the provider lists, so an install may hold a duplicate row a member
    selected for a device. The listing now skips copies, so that row is
    pruned once; moving its selections first means the device keeps the
    files instead of being told to delete them. A device the original is
    already selected for loses only the duplicate's link, and a selection
    left with no device is dropped without telling any device anything.
    A selection whose creator may not see the original stays behind and is
    pruned with the duplicate."""
    column = _MIRROR_COPY_COLUMNS.get(row["source_provider"])
    if column is None or not row["source_playlist_id"]:
        return
    copy_id = str(row["source_playlist_id"]).replace("-", "").lower()
    original = next((r["id"] for r in conn.execute(
        f"SELECT id, {column} AS remote_id FROM playlists WHERE {column} IS NOT NULL")
        if str(r["remote_id"]).replace("-", "").lower() == copy_id), None)
    if original is None:
        return
    for sel in conn.execute(
        "SELECT s.id FROM selections s JOIN users u ON u.id = s.created_by_user_id "
        "JOIN playlists o ON o.id = ? "
        "WHERE s.type = 'playlist' AND s.target = ? "
        # Only where the selection's creator may see the original, by the
        # rule main._visible_playlists uses. A duplicate read back through
        # the default pass is unowned, so anyone could select it, including
        # a copy of someone else's private playlist. Such a selection is
        # left on the duplicate and goes with it, as before.
        "AND (u.is_admin OR (o.source_private = 0 AND (o.owner_user_id IS NULL OR o.shared = 1)) "
        "OR o.owner_user_id = s.created_by_user_id)",
        (original, str(row["id"])),
    ).fetchall():
        conn.execute(
            "DELETE FROM selection_devices WHERE selection_id = ? AND device_id IN ("
            "SELECT sd.device_id FROM selection_devices sd JOIN selections s ON s.id = sd.selection_id "
            "WHERE s.type = 'playlist' AND s.target = ?)", (sel["id"], str(original)))
        if conn.execute("SELECT 1 FROM selection_devices WHERE selection_id = ?", (sel["id"],)).fetchone():
            conn.execute("UPDATE selections SET target = ? WHERE id = ?", (str(original), sel["id"]))
        else:
            conn.execute("DELETE FROM selections WHERE id = ?", (sel["id"],))


# Sources merged in under every library source, not only when they are the
# active one: the next sync lists them again whichever provider replaces
# them, so a switch away from one of these must leave its playlists alone.
ALWAYS_MERGED_SOURCES = frozenset({"filesystem", "music_assistant"})

_MIRROR_FLAGS = tuple(s.enabled_column for s in mirror_sinks.SINKS)


def _switch_removal_ids(conn, old_provider: str) -> list[int]:
    """The playlists a switch away from `old_provider` removes: that
    provider's own rows, and nothing from the extra sources (linked
    streaming accounts, subscriptions, the .m3u folder, Music Assistant),
    whose own syncs keep owning their rows across the switch."""
    if old_provider in ALWAYS_MERGED_SOURCES:
        return []
    return [r["id"] for r in conn.execute(
        "SELECT id FROM playlists WHERE source_provider = ? ORDER BY title COLLATE NOCASE",
        (old_provider,))]


# Users-table column holding each Trobar user's account on a provider whose
# first sync also lists per-user playlists (see sync_playlists).
PER_USER_COLUMNS = {"jellyfin": "jellyfin_user_id", "emby": "emby_user_id"}


def _norm_title(title: str | None) -> str:
    return " ".join((title or "").casefold().split())


def _overlap_confirms(a: set[int], b: set[int]) -> bool:
    """The origin-inference overlap rule: at least _ORIGIN_MIN_OVERLAP_TRACKS
    shared tracks, and at least _ORIGIN_MIN_OVERLAP_RATIO of the smaller
    playlist."""
    shared = len(a & b)
    return (shared >= _ORIGIN_MIN_OVERLAP_TRACKS
            and shared >= _ORIGIN_MIN_OVERLAP_RATIO * min(len(a), len(b)))


def _target_listing(conn, target_id: str, client) -> list[dict] | None:
    """The target provider's playlists, each owned as its first sync would
    own it: the server-wide listing first (no owner), then each mapped
    user's own listing for what it hasn't seen (owned by that user). None
    when the server-wide listing fails."""
    listing = client.list_playlists()
    if listing["status"] != "ok":
        return None
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []

    def add(pl: dict, owner: int | None, kwargs: dict) -> None:
        key = _playlist_key(target_id, pl["id"], pl["title"])
        if key not in seen:
            seen.add(key)
            out.append({"src_id": pl["id"], "title": pl["title"], "owner": owner, "kwargs": kwargs})

    for pl in listing["playlists"]:
        add(pl, None, {})
    column = PER_USER_COLUMNS.get(target_id)
    if column:
        for user in conn.execute(
                f"SELECT id, {column} AS mapped FROM users WHERE {column} IS NOT NULL").fetchall():
            user_listing = client.list_playlists(user_id=user["mapped"])
            if user_listing["status"] == "ok":
                for pl in user_listing["playlists"]:
                    add(pl, user["id"], {"user_id": user["mapped"]})
    return out


def plan_carry_over(conn, old_provider: str, target_id: str, client) -> dict:
    """Which of the old provider's playlists a switch to `target_id` carries
    over to their namesake there, instead of removing them. Reads the
    target through `client` (its stored connection, or an unsaved one set
    with provider_config.using) and writes nothing.

    A pair is one old playlist and one target playlist with the same title
    (case and spacing aside) and the same owner, whose tracks overlap by
    the origin-inference rule (_overlap_confirms). Two old or two target
    playlists sharing a title and owner are left unmatched rather than
    guessed, and so is a target playlist Trobar already has a row for.

    Returns {"status", "pairs"}: pairs of (old playlist id, target
    playlist id, target title). status is "ok"; "already_listed" for a
    target whose playlists Trobar lists under every provider (they already
    have rows of their own); "not_listable" for Roon, which can't be read
    before Trobar is approved in Roon; or "listing_failed"."""
    if target_id in ALWAYS_MERGED_SOURCES:
        return {"status": "already_listed", "pairs": []}
    if target_id == "roon":
        return {"status": "not_listable", "pairs": []}
    old_ids = _switch_removal_ids(conn, old_provider)
    if not old_ids:
        return {"status": "ok", "pairs": []}
    listing = _target_listing(conn, target_id, client)
    if listing is None:
        return {"status": "listing_failed", "pairs": []}

    ph = ",".join("?" for _ in old_ids)
    old_groups: dict[tuple[str, int | None], list[int]] = {}
    for r in conn.execute(f"SELECT id, title, owner_user_id FROM playlists WHERE id IN ({ph})", old_ids):
        old_groups.setdefault((_norm_title(r["title"]), r["owner_user_id"]), []).append(r["id"])
    new_groups: dict[tuple[str, int | None], list[dict]] = {}
    for pl in listing:
        new_groups.setdefault((_norm_title(pl["title"]), pl["owner"]), []).append(pl)

    pairs: list[tuple[int, str, str]] = []
    for key, olds in old_groups.items():
        news = new_groups.get(key, [])
        if len(olds) != 1 or len(news) != 1:
            continue
        old_id, new = olds[0], news[0]
        if conn.execute("SELECT 1 FROM playlists WHERE source_provider = ? AND source_playlist_id = ?",
                        (target_id, new["src_id"])).fetchone():
            continue
        old_tracks = {r["matched_track_id"] for r in conn.execute(
            "SELECT matched_track_id FROM playlist_tracks "
            "WHERE playlist_id = ? AND matched_track_id IS NOT NULL", (old_id,))}
        result = client.get_playlist_tracks(new["title"], source_playlist_id=new["src_id"], **new["kwargs"])
        if result.get("status") != "ok":
            continue
        new_tracks = set()
        for t in result["tracks"]:
            track_id = identity.resolve_playlist_track(
                conn, artist=t["artist"], title=t["title"], path=t.get("path"), isrc=t.get("isrc"),
                album=t.get("album"), mbid=t.get("mbid"))
            if track_id is not None:
                new_tracks.add(track_id)
        if _overlap_confirms(old_tracks, new_tracks):
            pairs.append((old_id, new["src_id"], new["title"]))
    return {"status": "ok", "pairs": pairs}


def switch_preview(conn, old_provider: str, carry: dict | None = None) -> dict:
    """What a library-source switch away from `old_provider` would do,
    counted now and changing nothing: the admin's review step, and the
    switch's own report of what it did. `carry` is plan_carry_over()'s
    result: its playlists are carried over, not removed.

    `devices` lists each device with playlist selections on a removed
    playlist. Those selections are deleted with the playlist, so the
    device's next sync removes the playlist file and any tracks no other
    selection keeps on it."""
    carry = carry or {"status": "ok", "pairs": []}
    carried_ids = {old_id for old_id, _src, _title in carry["pairs"]}
    ids = [i for i in _switch_removal_ids(conn, old_provider) if i not in carried_ids]
    titles: list[str] = []
    mirrored = 0
    if ids:
        ph = ",".join("?" for _ in ids)
        flags = " OR ".join(f"{f} = 1" for f in _MIRROR_FLAGS)
        titles = [r["title"] for r in conn.execute(
            f"SELECT title FROM playlists WHERE id IN ({ph}) ORDER BY title COLLATE NOCASE", ids)]
        mirrored = conn.execute(
            f"SELECT COUNT(*) FROM playlists WHERE id IN ({ph}) AND ({flags})", ids).fetchone()[0]
    devices = []
    if ids:
        ph = ",".join("?" for _ in ids)
        devices = [{"name": r["name"], "playlists": r["n"]} for r in conn.execute(
            "SELECT d.name AS name, COUNT(DISTINCT s.id) AS n FROM selections s "
            "JOIN selection_devices sd ON sd.selection_id = s.id "
            "JOIN devices d ON d.id = sd.device_id "
            f"WHERE s.type = 'playlist' AND s.target IN ({ph}) "
            "GROUP BY d.id ORDER BY d.name COLLATE NOCASE",
            [str(i) for i in ids])]
    removing = set(ids) | carried_ids
    kept: dict[str, int] = {}
    for r in conn.execute("SELECT id, source_provider FROM playlists"):
        if r["id"] not in removing:
            key = r["source_provider"] or "other"
            kept[key] = kept.get(key, 0) + 1
    carried_titles = sorted((title for _old, _src, title in carry["pairs"]), key=str.casefold)
    return {"from": old_provider,
            # `ids`: the old playlists carried over, which the switch is
            # confirmed with (main.api_admin_provider_switch).
            "carried": {"count": len(carried_titles), "titles": carried_titles,
                        "ids": sorted(carried_ids), "status": carry["status"]},
            "removed": {"count": len(ids), "titles": titles, "mirrored": mirrored},
            "devices": devices, "kept": kept}


def carry_over_playlists(conn, target_id: str, pairs) -> int:
    """Re-keys each paired old playlist to its namesake in the target
    (plan_carry_over's pairs), keeping its row and id, so its selections,
    mirror flags, Lidarr requests, shared/private choice and excluded gaps
    stay. The first sync from the target finds the row by its new key and
    refreshes its tracks. A Roon-only origin hint is cleared: it is
    recomputed only under Roon. Returns how many."""
    for old_id, src_id, title in pairs:
        conn.execute(
            "UPDATE playlists SET source_provider = ?, source_playlist_id = ?, title = ?, "
            "inferred_origin_provider = NULL, golden_source_id = NULL WHERE id = ?",
            (target_id, src_id, title, old_id))
    conn.commit()
    return len(pairs)


def remove_provider_playlists(conn, old_provider: str) -> int:
    """Removes the playlists a switch away from `old_provider` removes (see
    _switch_removal_ids), each through _remove_playlist_row so devices with
    one selected are told and its mirrors go with it. Returns how many."""
    ids = _switch_removal_ids(conn, old_provider)
    for playlist_id in ids:
        _remove_playlist_row(conn, playlist_id)
    conn.commit()
    return len(ids)


def _matched_track_ids(conn, playlist_id: int) -> set[int]:
    return {r["matched_track_id"] for r in conn.execute(
        "SELECT matched_track_id FROM playlist_tracks WHERE playlist_id = ? AND matched_track_id IS NOT NULL",
        (playlist_id,))}


def _sync_one_playlist(
    conn, provider, provider_id: str, title: str, source_playlist_id: str | None = None,
    owner_user_id: int | None = None, mirror_caches: dict[str, dict] | None = None,
    prefetched: dict | None = None, source_owner: str | None = None,
    source_private: bool = False, **provider_kwargs
) -> tuple[int, int] | None:
    """Syncs a single playlist's tracks from `provider` into
    playlists/playlist_tracks. Returns (track_count, matched_count), or None
    if the provider listed this playlist but couldn't actually produce
    tracks for it — treated as a skip, not a hard sync failure. `provider_id`
    (e.g. "roon", "filesystem") is persisted so the web UI can show which
    provider each playlist actually came from — meaningful because
    filesystem-discovered playlists are always merged in alongside
    whichever provider is active, so a single sync can write rows from
    two different provider_ids at once.

    `source_playlist_id` (#75) is the provider's own stable id where it has
    one, and is the upsert/uniqueness key: the row is found/created by
    (source_provider, source_playlist_id) when set, else by
    (source_provider, title) for Roon (no id). That's what lets two
    same-titled playlists coexist. An id-keyed row also picks up a
    provider-side *rename* correctly — its title is updated to follow the
    id, rather than a renamed playlist spawning an orphan second row.

    `provider_kwargs` passes through to get_playlist_tracks() untouched —
    empty for every provider except a per-user Roon-profile sync
    (roon_profile=...), a per-user Tidal sync (access_token=...,
    tidal_user_id=...), or a #262 per-user Jellyfin/Emby-account sync
    (user_id=...). `owner_user_id` (#28) is written to the playlists row
    itself, not passed to get_playlist_tracks().

    `mirror_caches`: mirror_sinks.new_caches(), a dict PER SINK that
    sync_playlists() creates ONCE per run and passes to every one of its
    _sync_one_playlist() calls; write_all() forwards each to its server
    sink's write_mirror(), so N mirrored playlists share one target
    tag-index build per sink instead of each triggering their own full
    library walk against that sink's mirror target.

    `prefetched` is the provider's get_playlist_tracks() response, already
    obtained by the caller, used instead of calling it again. Only the
    URL-subscription source passes it, and for a specific reason: a
    subscription is one playlist rather than a listing, so its TITLE only
    becomes known by fetching it -- and the title is an argument here.
    Without this the caller would have to fetch once to learn the title and
    this function would then fetch the same playlist a second time to get
    the tracks, against an unofficial endpoint, for every playlist on every
    sync."""
    result = prefetched if prefetched is not None else \
        provider.get_playlist_tracks(title, source_playlist_id, **provider_kwargs)
    if result["status"] != "ok":
        # Otherwise silent: a whole source can fail this way, sync after sync.
        _log.warning("playlist sync: %s %s not read: %s", provider_id, source_playlist_id or title,
                     result.get("reason") or result.get("error") or "no reason given")
        return None

    if source_playlist_id is not None:
        row = conn.execute(
            "SELECT id, owner_user_id, source_private FROM playlists "
            "WHERE source_provider = ? AND source_playlist_id = ?",
            (provider_id, source_playlist_id),
        ).fetchone()
        if row is None:
            # #85: first sync after the #75 upgrade. This playlist's
            # pre-existing row still has source_playlist_id NULL (provider
            # ids were never stored before #75), so the id lookup above
            # can't find it. Adopt that legacy row in place by title —
            # pre-#75 titles were globally UNIQUE, so this is unambiguous
            # within a provider — rather than inserting a fresh row and
            # letting the caller's stale-cleanup delete the old one, which
            # would revoke its device selections and reset its shared/
            # ownership (#28/#70/#74). One-time: the UPDATE below stamps
            # source_playlist_id, so next sync matches by id directly and
            # this branch never fires again for it.
            row = conn.execute(
                "SELECT id, owner_user_id, source_private FROM playlists "
                "WHERE source_provider = ? AND source_playlist_id IS NULL AND title = ?",
                (provider_id, title),
            ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, owner_user_id, source_private FROM playlists "
            "WHERE source_provider = ? AND source_playlist_id IS NULL AND title = ?",
            (provider_id, title),
        ).fetchone()

    if row is None:
        # #496: an owned playlist starts PRIVATE — the person it belongs to
        # decides to share it (one click on the existing per-playlist
        # toggle, #28), rather than the sync silently publishing it to the
        # whole household on their behalf. This applies to every ownership
        # route (Roon profile mapping, direct Tidal link, #262's per-user
        # Jellyfin/Emby mapping) since they all funnel through here — a
        # rule scoped to only one of them is one nobody would remember.
        # An UNOWNED playlist (the one configured account's own listing)
        # stays shared: it belongs to the household by definition, and the
        # visibility check (#28) passes it on owner_user_id IS NULL before
        # `shared` is ever consulted anyway.
        initial_shared = 0 if owner_user_id is not None else 1
        cur = conn.execute(
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, "
            "source_owner, source_private, last_synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (title, provider_id, source_playlist_id, owner_user_id, initial_shared,
             source_owner, int(source_private)),
        )
        playlist_id = cur.lastrowid
    else:
        playlist_id = row["id"]
        # `shared` is deliberately left untouched on a normal re-sync (same
        # owner) — that's load-bearing, it's what stops every scheduled
        # sync from silently resetting a user's own sharing choice back to
        # private (#28, guarded by
        # test_resync_never_resets_an_owners_privacy_choice). BUT when the
        # OWNER actually changes (#70 — e.g. a Roon profile mapping
        # reassigned to a different household member, or a Tidal link
        # changing hands), carrying the old owner's shared/private flag
        # over to the new owner is wrong: they'd inherit a choice made by
        # someone else, never their own. So on an owner change
        # specifically, reset `shared` to the same private default a
        # freshly-inserted owned playlist gets (#496) — 0, not the
        # column's own default of 1. For an unowned<->owned transition the
        # reset is a harmless no-op (shared is irrelevant while
        # owner_user_id IS NULL). A new owner who wants it shared can flip
        # it via the PATCH toggle.
        #
        # title=? is set on every update so an id-keyed provider rename is
        # reflected; for a title-keyed (Roon) row title is the lookup key
        # so it's a no-op there. source_playlist_id=? is set on every update
        # too: idempotent for an already-id'd row, NULL->NULL for Roon, and
        # crucially NULL->id when adopting a legacy row (#85) — which
        # re-keys it so this run's stale-cleanup sees it as still-listed
        # (its key is now the id) and leaves its selections/ownership alone.
        owner_changed = row["owner_user_id"] != owner_user_id
        shared_clause = ", shared = 0" if owner_changed else ""
        became_private = bool(source_private) and not row["source_private"]
        conn.execute(
            f"UPDATE playlists SET title = ?, source_provider = ?, source_playlist_id = ?, "
            f"owner_user_id = ?{shared_clause}, source_owner = ?, source_private = ?, "
            f"last_synced_at = datetime('now') WHERE id = ?",
            (title, provider_id, source_playlist_id, owner_user_id, source_owner,
             int(source_private), playlist_id),
        )
        if (owner_changed and owner_user_id is not None) or became_private:
            # Now private to its new owner, or to admins: whoever else had it
            # on a device can no longer see it, so it goes from there, as
            # when an owner stops sharing.
            revoke_non_owner_selections(conn, playlist_id, owner_user_id)

    matched_before = _matched_track_ids(conn, playlist_id)
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    track_count = matched_count = 0
    # #200: entries the resolver couldn't match, collected for the review
    # surface (unresolved_store.record_unresolved_playlist_tracks below) — a
    # provider-only track (streamed, never downloaded) is expected to show
    # up here and isn't itself a bug; it's what the review list lets an
    # owner acknowledge.
    unresolved: list[dict] = []
    for t in result["tracks"]:
        # identity.py's tiered resolver: the provider's ISRC first (Tidal,
        # Spotify and Navidrome give one), then its MusicBrainz recording id
        # (Jellyfin and Navidrome), then path and artist/title matching, where the
        # entry's album breaks ties. See its docstring for the full cascade.
        path = t.get("path")
        if "suggested_track_id" in t:
            # A guess (from a device playlist's file name): matched only once
            # accepted, and kept in the review list either way, so the
            # acceptance survives the next upload.
            guess = t["suggested_track_id"]
            matched_id = guess if guess is not None and sync_state.accepted_suggestion(
                conn, playlist_id, t["artist"], t["title"], t.get("album"), guess) else None
            unresolved.append({
                "position": t["position"], "artist": t["artist"], "title": t["title"],
                "album": t.get("album"), "isrc": None,
                "suggested_track_id": guess, "source_path": t.get("source_path"),
            })
        else:
            matched_id = identity.resolve_playlist_track(
                conn, artist=t["artist"], title=t["title"], path=path, isrc=t.get("isrc"),
                album=t.get("album"), mbid=t.get("mbid"),
            )
            if matched_id is None:
                unresolved.append({
                    "position": t["position"], "artist": t["artist"], "title": t["title"],
                    "album": t.get("album"), "isrc": t.get("isrc"),
                })
        if matched_id is not None:
            matched_count += 1
        track_count += 1
        conn.execute(
            "INSERT INTO playlist_tracks "
            "(playlist_id, position, artist, title, album, matched_track_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (playlist_id, t["position"], t["artist"], t["title"], t.get("album"), matched_id),
        )
    sync_state.record_unresolved_playlist_tracks(conn, playlist_id, unresolved)
    # A device selecting this playlist hears of a track it gained or lost now,
    # not at the next selection change or scan.
    if _matched_track_ids(conn, playlist_id) != matched_before:
        sync_state.recompute_playlist_devices(conn, playlist_id)
    # #285/#189: rewrite this playlist's mirror(s) -- each a no-op unless
    # its own *_mirror_enabled flag is set -- now that matched_track_id is
    # fresh for every track. Same "resolve, then act on the result"
    # placement as the unresolved-tracks recording just above. Six
    # independent sinks, six independent failure surfaces (see each
    # module's own never-raises contract) -- one failing never blocks
    # any other or this playlist's own sync. This commits the playlist first:
    # the sinks' network calls must not run with the write lock held.
    mirror_sinks.write_all(conn, playlist_id, mirror_caches, with_caches=True)
    # Requests this playlist's missing ALBUMS from Lidarr. Queued rather than
    # run here: each album is a Lidarr round trip. See lidarr_requests.py.
    lidarr_requests.enqueue(conn, playlist_id)
    # #133: commit this playlist's writes now, before the caller fetches the
    # NEXT playlist over the network. Otherwise a whole sync holds one open
    # write transaction across every provider round-trip — and since #136's
    # retry/backoff those fetches can sleep for many seconds — keeping the
    # SQLite write lock the entire time and colliding with a concurrent
    # library scan or device/selection write past the 30s busy_timeout. This
    # mirrors the scanner's own commit per file. Each playlist
    # is still written atomically (its old tracks are deleted + re-inserted in
    # one transaction, which write_all commits before the first sink); what is
    # given up is whole-run atomicity, and a mirror's matching the playlist
    # after a crash, which is fine — a sync is re-runnable and the
    # stale-cleanup at the end only runs if the whole pass completes.
    conn.commit()
    # Gaps without an album get one looked up in the background (after the
    # commit: queueing a job commits the connection).
    album_lookup.enqueue_if_pending(conn, playlist_id)
    return track_count, matched_count



# Playlists made on a device (an SD-card player, a phone) and read off its
# storage by the client that syncs it. Not a provider: the client uploads
# them, and their rows key on the device.
DEVICE_SOURCE = "device"


def device_playlist_id(device_id: int, path: str) -> str:
    return f"{device_id}:{path}"


def sync_device_playlists(conn, device_id: int, playlists: list[dict]) -> dict:
    """Records the playlists a person made on `device_id`, as uploaded by its
    client: [{"path": <file path from the sync root>, "entries": [<line>, ...]}],
    the whole set found on the device this sync. Each becomes a playlist
    owned by the device's owner (private to start with, like any owned
    playlist), keyed on device id + path, and goes through _sync_one_playlist
    like a provider's, so its mirrors are written the same way. A playlist
    from an earlier upload that isn't in this one has left the device and
    is removed through _remove_playlist_row, so devices it was sent to are
    told and its mirrors go with it.

    Loop guard, device side: a path Trobar itself writes to this device
    (device_store.device_playlist_filenames) is ignored, whatever the client
    sent, so a Trobar playlist that went out to the card never comes back
    as one of the device's own. The clients also skip any file carrying
    device_store.M3U_MARKER.

    Entries resolve through sync_state.resolve_card_entry(); one that names
    no library track is added to the device's unknown tracks. Returns counts."""
    owner = conn.execute("SELECT owner_user_id FROM devices WHERE id = ?", (device_id,)).fetchone()
    if owner is None:
        raise ValueError(f"no device {device_id}")
    ours = sync_state.device_playlist_filenames(conn, device_id)
    index = sync_state.device_path_index(conn, device_id)
    listed: set[str] = set()
    counts = {"playlists": 0, "tracks": 0, "matched": 0, "unknown": 0}
    unknown: list[str] = []
    for pl in playlists:
        path = pl["path"]
        if path in ours or device_playlist_id(device_id, path) in listed:
            continue
        tracks = []
        for position, entry in enumerate(pl["entries"]):
            found = sync_state.resolve_card_entry(entry, path, index)
            row = index.get(found) if found else None
            if row is not None:
                tracks.append({"position": position, "artist": row["artist"], "title": row["title"],
                               "album": row["album"], "path": row["relative_path"]})
            else:
                # No file Trobar put on the card. The artist and title its
                # name gives (the Artist/Album/NN - Title.ext pattern Trobar
                # writes) may still name a library track, but only as a
                # guess: it's offered for review, and matched only once its
                # owner accepts it (_sync_one_playlist).
                card_path = entry.strip().replace("\\", "/")
                artist, album, title = sync_state._parse_device_path(
                    "/".join([p for p in card_path.split("/") if p][-3:]))
                guess = identity.resolve_playlist_track(
                    conn, artist=artist or "", title=title or card_path, album=album) if artist and title else None
                tracks.append({"position": position, "artist": artist or "", "title": title or card_path,
                               "album": album, "path": None,
                               "suggested_track_id": guess, "source_path": card_path})
                unknown.append(card_path)
        source_id = device_playlist_id(device_id, path)
        title = posixpath.splitext(posixpath.basename(path))[0] or path
        result = _sync_one_playlist(conn, None, DEVICE_SOURCE, title, source_playlist_id=source_id,
                                    owner_user_id=owner["owner_user_id"],
                                    prefetched={"status": "ok", "tracks": tracks})
        listed.add(source_id)
        if result is not None:
            counts["playlists"] += 1
            counts["tracks"] += result[0]
            counts["matched"] += result[1]
    sync_state.add_device_unknown_tracks(conn, device_id, unknown)
    counts["unknown"] = len(set(unknown))
    prefix = device_playlist_id(device_id, "")
    for row in conn.execute(
        "SELECT id, source_playlist_id FROM playlists WHERE source_provider = ? "
        "AND substr(source_playlist_id, 1, ?) = ?", (DEVICE_SOURCE, len(prefix), prefix)
    ).fetchall():
        if row["source_playlist_id"] not in listed:
            _remove_playlist_row(conn, row["id"])
    conn.commit()
    return counts


# A playlist made in Trobar itself: created empty, filled from the library,
# reordered, renamed and deleted here, never re-read from anywhere. Owned
# by whoever made it, private to start with, and mirrored and selected like
# any other row. Provider syncs and switches leave these rows alone: each
# only cleans its own sources.
TROBAR_SOURCE = "trobar"
TROBAR_TRACKS_MAX = 20000


def write_playlist_mirrors(conn, playlist_id: int) -> None:
    """Rewrite every mirror of one playlist after an edit made here; each
    sink is a no-op unless its own flag is on, and never raises. The same
    calls _sync_one_playlist makes after a sync. Commits what the caller has
    written first, so the edit stands whatever a sink does."""
    mirror_sinks.write_all(conn, playlist_id)


def create_trobar_playlist(conn, user_id: int, title: str) -> int:
    """A new, empty playlist owned by `user_id`, private. Commits."""
    cur = conn.execute(
        "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, "
        "last_synced_at) VALUES (?, ?, ?, ?, 0, datetime('now'))",
        (title, TROBAR_SOURCE, uuid.uuid4().hex, user_id))
    conn.commit()
    return sync_state._new_id(cur)


def set_trobar_playlist_tracks(conn, playlist_id: int, items: list) -> int:
    """Replace the playlist's entries with `items`, in that order: adding,
    removing and reordering are all this one write. An item is a library
    track id (an int; the same track may appear more than once), or
    {"entry_id": n} for an entry of this playlist kept exactly as it is, so
    a track since removed from the library stays in its place with its own
    artist and title instead of blocking the write or vanishing. The caller
    checks both kinds first (unknown_track_ids, foreign_entry_ids). Recomputes
    the devices that select it and rewrites the mirrors. Commits. Returns the
    entry count."""
    track_ids = {i for i in items if isinstance(i, int)}
    tracks = {r["id"]: r for r in conn.execute(
        f"SELECT id, artist, title, album FROM tracks WHERE id IN ({','.join('?' * len(track_ids))})",
        tuple(track_ids))} if track_ids else {}
    entries = {r["id"]: r for r in conn.execute(
        "SELECT id, artist, title, album, matched_track_id FROM playlist_tracks WHERE playlist_id = ?",
        (playlist_id,))}
    values = []
    for pos, item in enumerate(items):
        if isinstance(item, int):
            t = tracks[item]
            values.append((playlist_id, pos, t["artist"], t["title"], t["album"], item))
        else:
            e = entries[item["entry_id"]]
            values.append((playlist_id, pos, e["artist"], e["title"], e["album"], e["matched_track_id"]))
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    conn.executemany(
        "INSERT INTO playlist_tracks (playlist_id, position, artist, title, album, matched_track_id) "
        "VALUES (?, ?, ?, ?, ?, ?)", values)
    conn.execute("UPDATE playlists SET last_synced_at = datetime('now') WHERE id = ?", (playlist_id,))
    sync_state.record_unresolved_playlist_tracks(conn, playlist_id, [])
    sync_state.recompute_playlist_devices(conn, playlist_id)
    write_playlist_mirrors(conn, playlist_id)
    conn.commit()
    return len(values)


def foreign_entry_ids(conn, playlist_id: int, entry_ids: list[int]) -> list[int]:
    """The ids among `entry_ids` that aren't entries of this playlist."""
    if not entry_ids:
        return []
    own = {r["id"] for r in conn.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))}
    return sorted({i for i in entry_ids if i not in own})


def unknown_track_ids(conn, track_ids: list[int]) -> list[int]:
    """The ids among `track_ids` that name no library track, or one since
    removed from the library."""
    if not track_ids:
        return []
    found = {r["id"] for r in conn.execute(
        f"SELECT id FROM tracks WHERE deleted_at IS NULL AND id IN ({','.join('?' * len(set(track_ids)))})",
        tuple(set(track_ids)))}
    return sorted(set(track_ids) - found)


def rename_trobar_playlist(conn, playlist_id: int, title: str) -> None:
    """Rename, and rewrite the mirrors so their copies follow. Commits."""
    conn.execute("UPDATE playlists SET title = ? WHERE id = ?", (title, playlist_id))
    write_playlist_mirrors(conn, playlist_id)
    conn.commit()


def delete_trobar_playlist(conn, playlist_id: int) -> None:
    """Delete, telling devices and removing mirrors with it
    (_remove_playlist_row). Commits."""
    _remove_playlist_row(conn, playlist_id)
    conn.commit()


# A playlist someone uploaded as an .m3u/.m3u8 file. The file is the source,
# as for a playlist made on a device: uploading a file of the same name
# again replaces that playlist's entries, so it is keyed on the uploader
# and the file name, not on the content.
UPLOAD_SOURCE = "upload"


def upload_playlist_id(user_id: int, filename: str) -> str:
    return f"{user_id}:{filename}"


def import_uploaded_playlist(conn, user_id: int, filename: str, tracks: list[dict]) -> dict:
    """Records an uploaded playlist file as a playlist of `user_id`, private
    to start with like any owned playlist, through _sync_one_playlist like
    a provider's, so its entries are matched, its unmatched ones go to the
    unresolved review, and its mirrors are written the same way. `tracks`
    are filesystem_client.uploaded_playlist_tracks() items. Matched once,
    here: nothing re-reads the file later, so uploading it again is how a
    track added to the library since joins it.

    Returns {"playlist_id", "title", "replaced", "tracks", "matched"};
    commits."""
    source_id = upload_playlist_id(user_id, filename)
    existing = conn.execute(
        "SELECT id FROM playlists WHERE source_provider = ? AND source_playlist_id = ?",
        (UPLOAD_SOURCE, source_id)).fetchone()
    title = posixpath.splitext(filename)[0] or filename
    result = _sync_one_playlist(conn, None, UPLOAD_SOURCE, title, source_playlist_id=source_id,
                                owner_user_id=user_id, prefetched={"status": "ok", "tracks": tracks})
    conn.commit()
    row = conn.execute(
        "SELECT id FROM playlists WHERE source_provider = ? AND source_playlist_id = ?",
        (UPLOAD_SOURCE, source_id)).fetchone()
    track_count, matched = result if result is not None else (len(tracks), 0)
    return {"playlist_id": row["id"] if row else None, "title": title, "replaced": existing is not None,
            "tracks": track_count, "matched": matched}


def export_m3u8(conn, playlist_id: int, include_unresolved: bool = False) -> str:
    """The playlist as an .m3u8 to take elsewhere: UTF-8, in order, an
    #EXTINF line per track and its path relative to the music root, so it
    works for any player that sees the same library. Only matched tracks,
    unless `include_unresolved`: then an entry that matched nothing (or a
    track since removed from the library) is kept in its place as a
    comment, "# Artist - Title", which players skip. A tag with a line
    break in it is put on one line, so it can't start a line of its own."""
    def one_line(text):
        return " ".join((text or "").splitlines())

    lines = ["#EXTM3U"]
    for r in conn.execute(
            "SELECT pt.artist AS entry_artist, pt.title AS entry_title, "
            "t.relative_path, t.artist, t.title, t.duration "
            "FROM playlist_tracks pt LEFT JOIN tracks t "
            "ON t.id = pt.matched_track_id AND t.deleted_at IS NULL "
            "WHERE pt.playlist_id = ? ORDER BY pt.position", (playlist_id,)):
        if r["relative_path"] is not None:
            duration = int(r["duration"]) if r["duration"] else -1
            lines.append(f"#EXTINF:{duration},{one_line(r['artist'])} - {one_line(r['title'])}")
            lines.append(r["relative_path"])
        elif include_unresolved:
            lines.append(f"# {one_line(r['entry_artist'])} - {one_line(r['entry_title'])}")
    return "\n".join(lines) + "\n"


def remove_device_playlists(conn, device_id: int) -> int:
    """Removes the playlists made on `device_id`, through _remove_playlist_row,
    when the device itself is deleted. Commits, as mirror_sinks does.
    Returns how many."""
    prefix = device_playlist_id(device_id, "")
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM playlists WHERE source_provider = ? AND substr(source_playlist_id, 1, ?) = ?",
        (DEVICE_SOURCE, len(prefix), prefix))]
    for playlist_id in ids:
        _remove_playlist_row(conn, playlist_id)
    return len(ids)


# #26: both counts are judgment calls, not derived from anything in the
# issue — no exact threshold was specified there. Picked to avoid noisy
# false positives on short/generic playlists (a 2-track overlap between
# unrelated small playlists proves little) while still catching a real
# Tidal-imported playlist, which should share nearly all of its tracks
# with its Tidal-side counterpart.
_ORIGIN_MIN_OVERLAP_TRACKS = 3
_ORIGIN_MIN_OVERLAP_RATIO = 0.6
# #147: the directly-linked streaming accounts a Roon playlist can be attributed
# to by diffing tracks (Roon exposes no source signal — #23). Any future direct
# per-user client slots in here; order is display/tie-break only. (KKBOX can't
# get one — its API exposes no user playlists, #149/#152; Qobuz only via BYO
# official partner credentials, #150/#153.)
_DIRECT_PROVIDERS = ("tidal", "spotify")


def _ma_sources(listing: dict) -> dict[str, list[tuple[str, str]]] | None:
    """Each Music Assistant playlist's source ids from this run's listing, or
    None if there was no listing to go by."""
    if listing.get("status") != "ok":
        return None
    return {pl["id"]: pl.get("source_ids", []) for pl in listing["playlists"]}


def _link_music_assistant_copies(conn, ma_sources: dict[str, list[tuple[str, str]]]) -> None:
    """Sets the golden link of each Music Assistant row whose source is a
    playlist synced directly. Several rows may share one: a playlist can show
    under two linked accounts."""
    direct = {(r["source_provider"], r["source_playlist_id"]): r["id"] for r in conn.execute(
        f"SELECT id, source_provider, source_playlist_id FROM playlists "
        f"WHERE source_provider IN ({','.join('?' * len(_DIRECT_PROVIDERS))})", _DIRECT_PROVIDERS)}
    for row in conn.execute("SELECT id, source_playlist_id FROM playlists WHERE source_provider = 'music_assistant'"):
        for domain, source_id in ma_sources.get(row["source_playlist_id"], ()):
            golden = direct.get((domain, source_id))
            if golden is not None:
                conn.execute("UPDATE playlists SET inferred_origin_provider = ?, golden_source_id = ? WHERE id = ?",
                             (domain, golden, row["id"]))
                break


def _linked_account_health(health: list[dict], provider_id: str, linked, failures: list[dict]) -> None:
    """One source_health outcome per linked account this run tried: failed
    with its first recorded reason if _source_failed recorded one, else ok."""
    for user_row in linked:
        reasons = [f["reason"] for f in failures
                   if f["provider"] == provider_id and f["user_id"] == user_row["id"]]
        health.append(source_health.outcome(
            source_health.LINKED_ACCOUNT, provider_id, not reasons,
            reasons[0] if reasons else None, user_id=user_row["id"]))


def ma_account_owners(conn) -> dict[str, int]:
    """Music Assistant streaming account (provider instance id) -> the member
    it belongs to, as an admin set it."""
    return {r["instance_id"]: r["user_id"] for r in conn.execute(
        "SELECT instance_id, user_id FROM ma_account_owners")}


def ma_playlist_owner(pl: dict, owners: dict[str, int]) -> int | None:
    """The member a Music Assistant playlist belongs to: its one account's
    owner. A playlist several accounts carry, or none, stays everyone's."""
    accounts = pl.get("accounts") or []
    return owners.get(accounts[0]) if len(accounts) == 1 else None


def subsonic_owners(conn) -> dict[str, int]:
    """Navidrome username (case-folded) -> the member who gave it on Profile."""
    return {r["subsonic_username"].casefold(): r["id"] for r in conn.execute(
        "SELECT id, subsonic_username FROM users WHERE subsonic_username IS NOT NULL "
        "AND subsonic_username != ''")}


def subsonic_playlist_owner(pl: dict, owners: dict[str, int], account: str) -> tuple[int | None, bool]:
    """(owner_user_id, source_private) for one Subsonic playlist. Its owner
    on the server, if a member gave that username, owns it in Trobar too. A
    private playlist of someone else on the server who isn't a member yet is
    visible to admins only. The library account's own playlists, and every
    playlist from a server that names no owner, stay everyone's as before."""
    server_owner = (pl.get("owner") or "").casefold()
    # The library account's own come first: they are the household's, even
    # if a member's username somehow matches it.
    if not server_owner or server_owner == (account or "").casefold():
        return None, False
    owner = owners.get(server_owner)
    if owner is not None:
        return owner, False
    return None, not pl.get("public")


def revoke_non_owner_selections(conn, playlist_id: int, owner_user_id: int | None) -> int:
    """Deletes every selection of this playlist made by someone other than
    its owner or an admin: they can no longer see it, so their devices must
    not go on pulling it. delete_selection() tells each device to remove the
    files. Used when a playlist turns private: its owner un-sharing it, or a
    sync giving it a new owner. Returns how many went."""
    # With no owner (a playlist visible to admins only), every non-admin's
    # selection goes: IS NOT, so a NULL owner excludes nobody.
    rows = conn.execute(
        "SELECT s.id FROM selections s JOIN users u ON u.id = s.created_by_user_id "
        "WHERE s.type = 'playlist' AND s.target = ? AND s.created_by_user_id IS NOT ? AND u.is_admin = 0",
        (str(playlist_id), owner_user_id),
    ).fetchall()
    for row in rows:
        sync_state.delete_selection(conn, row["id"])
    return len(rows)


def _source_failed(failures: list[dict], provider_id: str, user_row, reason) -> None:
    """Records that one linked account's source failed this run, for the job's
    result and the log. A linked account that fails every sync must not look
    healthy: its playlists stay, ageing, with nothing else to show for it."""
    reason = str(reason) if reason else "no reason given"
    _log.warning("playlist sync: %s failed for user %s: %s", provider_id, user_row["id"], reason)
    failures.append({"provider": provider_id, "user_id": user_row["id"], "reason": reason})


def _infer_playlist_origins(conn, ma_sources: dict[str, list[tuple[str, str]]] | None = None) -> None:
    """Links a library provider's copy of a streaming playlist to the row
    synced directly from that service, its golden source.

    Music Assistant says where each playlist came from, so its rows match by
    the service's own playlist id and nothing else: `ma_sources` is each row's
    source_ids from this run's listing, and a row with none has no origin. None
    means no listing this run, so those rows keep the links they have, rather
    than every copy reappearing because Music Assistant didn't answer.

    #26/#81/#147: Roon's Browse API exposes no source signal for a playlist —
    a Roon-native playlist and one imported from a streaming service are
    indistinguishable there (confirmed in #23's research). When the household
    has a directly-linked streaming account (Tidal #21, Spotify #10, future
    qobuz/kkbox) and #75 keeps the Roon row and the direct-provider row as
    separate rows, match each Roon row to its counterpart across ANY linked
    provider (its "golden source") and record the link:

    - `inferred_origin_provider` = the matched provider ('tidal'/'spotify'/…) —
      the #26 cosmetic "likely imported from X" hint.
    - `golden_source_id` = the matched direct row's id — the #81 attribution
      link. Deliberately NOT owner_user_id: the Roon row stays
      unowned/household-visible for #28; golden_source_id is display-only
      (per-viewer badge + suppressing the Roon duplicate for anyone who can
      already see the golden copy).

    Matching, in priority order:
    1. Exact same title (strong evidence now that #75 keeps both rows) —
       UNLESS both rows carry enough resolved tracks to judge and their
       overlap actively contradicts it (two genuinely different playlists
       that merely share a name, the #23 case). If either side has too few
       resolved tracks, the title alone is trusted.
    2. Otherwise substantial track overlap regardless of title — the
       renamed-on-import case. Ratio is normalised by the Roon side, so a small
       provider playlist fully inside a large Roon one does NOT flag it.

    #147: both tiers now consider EVERY directly-linked provider's rows, not
    just Tidal, and set inferred_origin_provider to whichever matched. When a
    Roon playlist plausibly matches counterparts on two providers, the highest
    track-overlap ratio wins (ties broken by scan order — only reachable with no
    track evidence either way, where the pick is cosmetic).

    Recomputed from scratch every call (cleared then re-set), so a stale link
    never survives a provider being disconnected or a playlist diverging. A
    direct-provider-only playlist (no Roon counterpart) is never touched here.
    Does not touch owner_user_id/shared."""
    keep = " AND source_provider != 'music_assistant'" if ma_sources is None else ""
    conn.execute(
        "UPDATE playlists SET inferred_origin_provider = NULL, golden_source_id = NULL "
        f"WHERE (inferred_origin_provider IS NOT NULL OR golden_source_id IS NOT NULL){keep}")
    if ma_sources:
        _link_music_assistant_copies(conn, ma_sources)
    # Nothing below is about Music Assistant: its rows matched by id or not at all.

    def _track_sets(providers: tuple[str, ...]) -> dict[int, set[int]]:
        ph = ",".join("?" for _ in providers)
        rows = conn.execute(
            f"SELECT p.id AS playlist_id, pt.matched_track_id FROM playlists p "
            f"JOIN playlist_tracks pt ON pt.playlist_id = p.id "
            f"WHERE p.source_provider IN ({ph}) AND pt.matched_track_id IS NOT NULL",
            providers,
        ).fetchall()
        sets: dict[int, set[int]] = {}
        for row in rows:
            sets.setdefault(row["playlist_id"], set()).add(row["matched_track_id"])
        return sets

    roon_rows = conn.execute(
        "SELECT id, title FROM playlists WHERE source_provider = 'roon'").fetchall()
    placeholders = ",".join("?" for _ in _DIRECT_PROVIDERS)
    direct_rows = conn.execute(
        f"SELECT id, title, source_provider FROM playlists "
        f"WHERE source_provider IN ({placeholders})", _DIRECT_PROVIDERS).fetchall()
    if not roon_rows or not direct_rows:
        return
    roon_sets = _track_sets(("roon",))
    direct_sets = _track_sets(_DIRECT_PROVIDERS)

    direct_by_title: dict[str, list] = {}
    for dr in direct_rows:
        direct_by_title.setdefault(dr["title"], []).append(dr)

    def _contradicts(roon_tracks: set[int], other_tracks: set[int]) -> bool:
        # Only a verdict when BOTH sides have enough resolved tracks to
        # judge; otherwise there's no evidence to contradict the title.
        if len(roon_tracks) < _ORIGIN_MIN_OVERLAP_TRACKS or len(other_tracks) < _ORIGIN_MIN_OVERLAP_TRACKS:
            return False
        return len(roon_tracks & other_tracks) / len(roon_tracks) < _ORIGIN_MIN_OVERLAP_RATIO

    def _ratio(roon_tracks: set[int], other_tracks: set[int]) -> float:
        return len(roon_tracks & other_tracks) / len(roon_tracks) if roon_tracks else 0.0

    for rr in roon_rows:
        roon_id, roon_title = rr["id"], rr["title"]
        roon_tracks = roon_sets.get(roon_id, set())
        match = None

        # 1. same-title across every provider: best non-contradicted overlap wins
        best_ratio = -1.0
        for dr in direct_by_title.get(roon_title, []):
            if _contradicts(roon_tracks, direct_sets.get(dr["id"], set())):
                continue
            r = _ratio(roon_tracks, direct_sets.get(dr["id"], set()))
            if r > best_ratio:
                best_ratio, match = r, dr

        # 2. renamed-on-import: best substantial overlap across every provider
        if match is None and roon_tracks:
            best_ratio = 0.0
            for dr in direct_rows:
                other_tracks = direct_sets.get(dr["id"], set())
                overlap = len(roon_tracks & other_tracks)
                if overlap < _ORIGIN_MIN_OVERLAP_TRACKS:
                    continue
                r = overlap / len(roon_tracks)
                if r > best_ratio:
                    best_ratio, match = r, dr
            if best_ratio < _ORIGIN_MIN_OVERLAP_RATIO:
                match = None

        if match is not None:
            conn.execute(
                "UPDATE playlists SET inferred_origin_provider = ?, golden_source_id = ? WHERE id = ?",
                (match["source_provider"], match["id"], roon_id))


def _playlist_key(source_provider: str, source_playlist_id: str | None, title: str) -> tuple[str, str]:
    """#75: the per-run dedup + stale-cleanup identity of a playlist, matching
    the DB's composite uniqueness. id-keyed and title-keyed rows share one
    set without colliding via the discriminator prefix. Two playlists with
    the same key are the same playlist (e.g. a Roon playlist seen in both
    the default listing and a mapped profile pass); different keys —
    including a same title from a different provider — are distinct rows."""
    return (source_provider,
            f"id:{source_playlist_id}" if source_playlist_id is not None else f"title:{title}")


def _cleanup_ghost_playlists(conn) -> int:
    """#93: reclaim legacy 'ghost' playlists — rows stuck at source_provider
    IS NULL (synced before the source_provider column existed, and not listed
    by any provider since). The normal stale-cleanup scans
    `WHERE source_provider IN (...)`, so a NULL-provider row is never in that
    set and lingers forever; it's also never re-listed, so never re-tagged.

    Selection-safe (#85): a ghost whose id still backs a playlist-type
    selection is PRESERVED — its playlist_tracks may still carry valid
    matched_track_ids syncing real files, and deleting it would revoke that
    selection and tell devices to drop those files. This differs from the
    provider stale-cleanup, which deletes-and-revokes a row a provider
    authoritatively stopped listing: a NULL-provider row has no known provider
    to confirm it's gone, so we only reclaim the truly-orphaned ones and leave
    the rest exactly as they are. playlist_tracks cascade on the DELETE.
    Returns the number removed."""
    removed = 0
    for row in conn.execute(
        "SELECT id FROM playlists WHERE source_provider IS NULL"
    ).fetchall():
        if conn.execute(
            "SELECT 1 FROM selections WHERE type = 'playlist' AND target = ?", (str(row["id"]),)
        ).fetchone():
            continue  # still backs a live selection — keep it
        conn.execute("DELETE FROM playlists WHERE id = ?", (row["id"],))
        removed += 1
    return removed


def sync_status_for(conn, user_id: int, is_admin: bool) -> dict:
    """sync_status() for one viewer: an admin sees every failed account, anyone
    else only their own. Whose link failed, and why, is that person's."""
    status = sync_status()
    result = status["last_result"]
    if isinstance(result, dict) and "source_failures" in result:
        kept = [f for f in result["source_failures"] if is_admin or f["user_id"] == user_id]
        for failure in kept:
            row = conn.execute("SELECT username FROM users WHERE id = ?", (failure["user_id"],)).fetchone()
            failure["username"] = row["username"] if row else None
        status["last_result"] = {**result, "source_failures": kept}
    return status


def sync_playlists(provider, provider_id: str, report=None) -> dict:
    """One sync, as _sync_playlists does it, and one mirror run: the notes Emby
    drops after a write are written again once, after the run (see mirror_notes)."""
    with mirror_notes.run():
        return _sync_playlists(provider, provider_id, report)


def _sync_playlists(provider, provider_id: str, report=None) -> dict:
    """Returns {"status": "ok", "playlists": n, "tracks": n, "matched": n,
    "removed": n}.

    #297 step 3: this used to be a private worker wrapped by a lock-guarded
    public `sync_playlists`; the queue (JOB_DEDUPE) is what now guarantees
    only one runs at a time, so the lock and the wrapper are both gone — this
    IS the job body, called directly by main.py's queue handler (and by
    tests, which call it synchronously against a stub provider).

    #128: the active/primary provider's own listing is best-effort. If it
    fails (Roon not re-paired yet after a restart, a transient blip, a
    misconfig), the independent secondary merges — filesystem, Roon-profile,
    and per-user Tidal — STILL run, since none of them depend on the primary
    provider. A primary-listing failure is surfaced as `primary_status:
    "error"` (plus `primary_provider`/`primary_error`) on an otherwise-ok
    partial result, instead of aborting the whole sync and silently skipping
    Tidal too. On primary failure the primary provider is also kept OUT of
    the stale-cleanup set, so its existing playlists aren't deleted just
    because we couldn't list them this run — the same protection #71 gives a
    Tidal user whose own fetch failed."""
    listing = provider.list_playlists()
    primary_ok = listing["status"] == "ok"

    conn = db.get_conn()

    def checkpoint() -> None:
        # The job's `report`, called before each playlist: the one before has
        # committed (see _sync_one_playlist), so a cancel here leaves every
        # synced playlist whole. Skipped while a write is still open, which
        # only delays the stop to the next playlist. A cancelled sync never
        # reaches the stale cleanup below, so nothing is removed because it
        # wasn't listed yet.
        if report is not None and not conn.in_transaction:
            report(playlist_count)
    playlist_count = track_count = matched_count = removed_count = 0
    # Composite keys (#75), not bare titles: two same-titled playlists from
    # different providers are distinct and both sync; a Roon playlist seen
    # in both the default and a profile pass is one key and dedups.
    seen_keys: set[tuple[str, str]] = set()
    # Every key actually LISTED this run (synced or skipped) — a fetch
    # failure for a still-listed playlist isn't grounds for removal, only a
    # playlist the provider stopped listing at all is.
    listed_keys: set[tuple[str, str]] = set()
    # #71: linked Tidal users whose fetch failed this run — the stale-cleanup
    # pass skips their owner_user_id-tagged rows so one user's failure never
    # deletes their still-valid playlists (see the Tidal block below).
    source_failures: list[dict] = []
    # Every source this run tries, ok or not, for source_health: written once
    # the run finishes, so a cancelled run records nothing.
    health: list[dict] = [source_health.outcome(
        source_health.LIBRARY_SOURCE, provider_id, primary_ok,
        listing.get("reason") or listing.get("error"))]
    tidal_failed_user_ids: set[int] = set()
    # This pass's Tidal access token per linked user, reused by the share
    # sync below rather than refreshed a second time.
    tidal_tokens: dict[int, str] = {}
    # #10 Part B: same per-owner protection for Spotify (see the Spotify block).
    spotify_failed_user_ids: set[int] = set()
    # Same protection for URL subscriptions, keyed on the subscription
    # rather than on a user: one subscription failing must not remove the
    # playlist it produced, and a user may hold several.
    subscription_failed_ids: set[str] = set()
    # #128: only providers we authoritatively listed this run are eligible for
    # stale-cleanup. The primary joins only when its listing succeeded;
    # filesystem/tidal add themselves below on their own success.
    provider_ids: set[str] = set()
    # One target tag-index build per sink, shared by every playlist this
    # run mirrors to it (see mirror_subsonic._get_tag_index()'s docstring)
    # — created once here rather than once per _sync_one_playlist() call.
    mirror_caches = mirror_sinks.new_caches()
    try:
        ma_owners = ma_account_owners(conn)
        sub_owners = subsonic_owners(conn) if provider is subsonic_client else {}
        sub_account = subsonic_client.account_username() if provider is subsonic_client else ""
        if primary_ok:
            provider_ids.add(provider_id)
            for pl in listing["playlists"]:
                src_id, title = pl["id"], pl["title"]
                key = _playlist_key(provider_id, src_id, title)
                listed_keys.add(key)
                checkpoint()
                owner, source_private = None, False
                if provider is music_assistant_client:
                    owner = ma_playlist_owner(pl, ma_owners)
                elif provider is subsonic_client:
                    owner, source_private = subsonic_playlist_owner(pl, sub_owners, sub_account)
                outcome = _sync_one_playlist(
                    conn, provider, provider_id, title, source_playlist_id=src_id,
                    owner_user_id=owner, source_owner=pl.get("owner"),
                    source_private=source_private, mirror_caches=mirror_caches)
                if outcome is None:
                    continue
                seen_keys.add(key)
                playlist_count += 1
                track_count += outcome[0]
                matched_count += outcome[1]

        if provider is not filesystem_client:
            fs_listing = filesystem_client.list_playlists()
            if fs_listing["status"] == "ok":
                provider_ids.add("filesystem")
                for pl in fs_listing["playlists"]:
                    src_id, title = pl["id"], pl["title"]
                    key = _playlist_key("filesystem", src_id, title)
                    listed_keys.add(key)
                    if key in seen_keys:
                        continue
                    checkpoint()
                    outcome = _sync_one_playlist(
                        conn, filesystem_client, "filesystem", title, source_playlist_id=src_id,
                        mirror_caches=mirror_caches)
                    if outcome is None:
                        continue
                    seen_keys.add(key)
                    playlist_count += 1
                    track_count += outcome[0]
                    matched_count += outcome[1]

        # Music Assistant: an extra source, merged whichever provider is
        # active -- except when it IS the active provider, whose pass above
        # already listed it. Skipped explicitly rather than left to
        # seen_keys, which would also hide it but only because both passes
        # happen to build the same key. Unconfigured or unreachable lists as
        # an error and joins nothing, so its earlier rows are left alone
        # rather than cleaned up as deleted -- the same posture as a failed
        # filesystem listing.
        ma_listing: dict = (music_assistant_client.list_playlists()
                      if provider is not music_assistant_client else {"status": "skipped"})
        if ma_listing["status"] != "skipped" and db.get_config(conn, "music_assistant_url"):
            # Configured as an extra source: it counts. Unconfigured, its
            # "not_paired" is only "not set up", never a failure.
            health.append(source_health.outcome(
                source_health.LIBRARY_SOURCE, "music_assistant", ma_listing["status"] == "ok",
                ma_listing.get("reason")))
        if ma_listing["status"] == "ok":
            provider_ids.add("music_assistant")
            for pl in ma_listing["playlists"]:
                src_id, title = pl["id"], pl["title"]
                key = _playlist_key("music_assistant", src_id, title)
                listed_keys.add(key)
                if key in seen_keys:
                    continue
                checkpoint()
                outcome = _sync_one_playlist(
                    conn, music_assistant_client, "music_assistant", title, source_playlist_id=src_id,
                    owner_user_id=ma_playlist_owner(pl, ma_owners), mirror_caches=mirror_caches)
                if outcome is None:
                    continue
                seen_keys.add(key)
                playlist_count += 1
                track_count += outcome[0]
                matched_count += outcome[1]

        # Locally-created Roon playlists are profile-specific (#23) — the
        # pass above only ever sees whichever profile the connection
        # currently defaults to. For every Trobar user mapped to a Roon
        # profile (Administration > Configuration), switch to their
        # profile and merge their playlists in too — still the one shared
        # pool everything else lands in. Roon has no stable id, so its keys
        # are (roon, title): a profile playlist already synced by the
        # default pass above has the same key and is skipped (default
        # wins, first-synced-this-run precedence).
        if provider is roon_client:
            mapped = conn.execute(
                "SELECT id, username, roon_profile FROM users WHERE roon_profile IS NOT NULL"
            ).fetchall()
            for user_row in mapped:
                profile = user_row["roon_profile"]
                profile_listing = roon_client.list_playlists(roon_profile=profile)
                health.append(source_health.outcome(
                    source_health.MEMBER_LISTING, "roon", profile_listing["status"] == "ok",
                    profile_listing.get("reason"), user_id=user_row["id"]))
                if profile_listing["status"] != "ok":
                    continue  # profile not found / transient error — retry next sync
                for pl in profile_listing["playlists"]:
                    src_id, title = pl["id"], pl["title"]
                    key = _playlist_key("roon", src_id, title)
                    listed_keys.add(key)
                    if key in seen_keys:
                        continue
                    # owner_user_id (#28): this playlist is genuinely
                    # traceable to this one Trobar user's own Roon profile.
                    checkpoint()
                    outcome = _sync_one_playlist(
                        conn, roon_client, "roon", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], roon_profile=profile,
                        mirror_caches=mirror_caches,
                    )
                    if outcome is None:
                        continue
                    seen_keys.add(key)
                    playlist_count += 1
                    track_count += outcome[0]
                    matched_count += outcome[1]

        # #262: the same per-user mapping idea as the Roon block just
        # above, generalized to Jellyfin/Emby -- but mechanically closer to
        # the Tidal block below than to Roon's, since neither needs a
        # profile SWITCH: the admin API key can query any mapped user's
        # own Items directly via a userId param (see jellyfin_client.py/
        # emby_client.py's list_playlists(user_id=...) — #262's addition).
        # Still gated on the sink actually being the active provider, same
        # posture as the Roon block: the mapping is meaningless against a
        # server that isn't even configured as the current source. Both
        # already expose a real, stable per-item id, so — unlike Roon —
        # the default pass above and this mapped-user pass share the exact
        # same key scheme without needing title-based collapsing; a key
        # collision here only means the mapped user can see a playlist the
        # default account already synced (same "first-synced-this-run
        # wins" precedence as every other merge in this function).
        if provider is jellyfin_client:
            mapped = conn.execute(
                "SELECT id, username, jellyfin_user_id FROM users WHERE jellyfin_user_id IS NOT NULL"
            ).fetchall()
            for user_row in mapped:
                mapped_user_id = user_row["jellyfin_user_id"]
                user_listing = jellyfin_client.list_playlists(user_id=mapped_user_id)
                health.append(source_health.outcome(
                    source_health.MEMBER_LISTING, "jellyfin", user_listing["status"] == "ok",
                    user_listing.get("reason"), user_id=user_row["id"]))
                if user_listing["status"] != "ok":
                    continue  # mapped user id stale / transient error — retry next sync
                for pl in user_listing["playlists"]:
                    src_id, title = pl["id"], pl["title"]
                    key = _playlist_key("jellyfin", src_id, title)
                    listed_keys.add(key)
                    if key in seen_keys:
                        continue
                    # owner_user_id (#28): this playlist is genuinely
                    # traceable to this one Trobar user's own Jellyfin account.
                    checkpoint()
                    outcome = _sync_one_playlist(
                        conn, jellyfin_client, "jellyfin", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], user_id=mapped_user_id,
                        mirror_caches=mirror_caches,
                    )
                    if outcome is None:
                        continue
                    seen_keys.add(key)
                    playlist_count += 1
                    track_count += outcome[0]
                    matched_count += outcome[1]

        if provider is emby_client:
            mapped = conn.execute(
                "SELECT id, username, emby_user_id FROM users WHERE emby_user_id IS NOT NULL"
            ).fetchall()
            for user_row in mapped:
                mapped_user_id = user_row["emby_user_id"]
                user_listing = emby_client.list_playlists(user_id=mapped_user_id)
                health.append(source_health.outcome(
                    source_health.MEMBER_LISTING, "emby", user_listing["status"] == "ok",
                    user_listing.get("reason"), user_id=user_row["id"]))
                if user_listing["status"] != "ok":
                    continue  # mapped user id stale / transient error — retry next sync
                for pl in user_listing["playlists"]:
                    src_id, title = pl["id"], pl["title"]
                    key = _playlist_key("emby", src_id, title)
                    listed_keys.add(key)
                    if key in seen_keys:
                        continue
                    # owner_user_id (#28): this playlist is genuinely
                    # traceable to this one Trobar user's own Emby account.
                    checkpoint()
                    outcome = _sync_one_playlist(
                        conn, emby_client, "emby", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], user_id=mapped_user_id,
                        mirror_caches=mirror_caches,
                    )
                    if outcome is None:
                        continue
                    seen_keys.add(key)
                    playlist_count += 1
                    track_count += outcome[0]
                    matched_count += outcome[1]

        # #21: direct per-user Tidal accounts, always merged in regardless
        # of which provider is active — a separate, parallel playlist
        # source from Roon/Subsonic/Jellyfin/filesystem, same relationship
        # the filesystem merge above has to the active provider. One
        # refresh_access_token() call per linked user, its access_token
        # reused for that user's list_playlists() + every
        # get_playlist_tracks() call — see tidal_client.py's module
        # docstring for why refreshing again per-call would be wrong
        # (rotation invalidates the previous token). Same title-collision
        # precedence as every other merge here: first synced this run wins.
        client_id = db.get_config(conn, "tidal_client_id")
        client_secret = db.get_config(conn, "tidal_client_secret")
        if client_id and client_secret:
            # #71: per-owner stale-cleanup precision. Every tidal row now
            # carries owner_user_id (#28/#68), so we can attribute a row to
            # the specific linked user who synced it. "tidal" joins
            # provider_ids whenever any user is linked (making tidal rows
            # eligible for the stale-cleanup below), but a user whose fetch
            # FAILED this run (auth or transient) has their id collected in
            # tidal_failed_user_ids — and the cleanup pass skips rows owned
            # by those users, so a single failed fetch never deletes (nor
            # tells devices to remove) that user's still-valid, just-not-
            # relisted Tidal tracks, while other users' successfully-synced
            # playlists are still cleaned normally on the same run. This
            # replaces #67's all-or-nothing tidal_all_ok gate, which
            # suppressed cleanup for the entire tidal bucket if ANY user
            # failed.
            linked = conn.execute(
                "SELECT id, tidal_refresh_token, tidal_user_id FROM users WHERE tidal_refresh_token IS NOT NULL"
            ).fetchall()
            for user_row in linked:
                try:
                    refresh_token = tidal_token.unseal(user_row["tidal_refresh_token"], user_row["id"])
                except credential_store.CredentialKeyError:
                    # The key file exists and can't be used: a fault to fix,
                    # not a lost key. The token is kept, as for a transient
                    # failure, and the next sync tries again.
                    tidal_failed_user_ids.add(user_row["id"])
                    _source_failed(source_failures, "tidal", user_row, "credential_key_unusable")
                    continue
                try:
                    if refresh_token is None:
                        # Stored, but it can't be decrypted: the credential key
                        # was lost or changed. Handled like a token Tidal
                        # rejects, below, under a reason of its own: the link
                        # is cleared and the Profile asks to connect again.
                        raise tidal_client.TidalAuthError("the stored refresh token can't be decrypted")
                    access_token, new_refresh_token = tidal_client.refresh_access_token(
                        client_id, client_secret, refresh_token
                    )
                except tidal_client.TidalAuthError:
                    # Revoked/expired at Tidal's end (or the admin's app
                    # credentials changed) — clear the stale link so the
                    # Profile UI shows "reconnect" instead of silently
                    # failing every sync forever. Deliberately NOT treated
                    # as "this user's Tidal playlists are gone"
                    # above; this user's rows stay protected from cleanup
                    # this run (they may reconnect), so nothing already
                    # synced is deleted on the strength of a revoked token.
                    conn.execute(
                        "UPDATE users SET tidal_refresh_token = NULL, tidal_user_id = NULL, "
                        "tidal_display_name = NULL, tidal_scopes = NULL WHERE id = ?",
                        (user_row["id"],),
                    )
                    conn.commit()
                    tidal_failed_user_ids.add(user_row["id"])
                    _source_failed(source_failures, "tidal", user_row,
                                   "token_unreadable" if refresh_token is None else "token_rejected")
                    continue
                except tidal_client.TidalTransientError as e:
                    # Network/5xx/timeout — nothing wrong with the stored
                    # token, retry next sync (same as every other
                    # provider's transient-failure handling here).
                    tidal_failed_user_ids.add(user_row["id"])
                    _source_failed(source_failures, "tidal", user_row, e)
                    continue
                if new_refresh_token != refresh_token:
                    conn.execute(
                        "UPDATE users SET tidal_refresh_token = ? WHERE id = ?",
                        (tidal_token.seal_for_rotation(new_refresh_token, user_row["id"]), user_row["id"]),
                    )
                    conn.commit()
                tidal_tokens[user_row["id"]] = access_token

                tidal_user_id = user_row["tidal_user_id"]
                tidal_listing = tidal_client.list_playlists(access_token)
                if tidal_listing["status"] != "ok":
                    tidal_failed_user_ids.add(user_row["id"])
                    _source_failed(source_failures, "tidal", user_row, tidal_listing.get("reason"))
                    continue  # transient error — retry next sync
                unread = 0
                for pl in tidal_listing["playlists"]:
                    src_id, title = pl["id"], pl["title"]
                    key = _playlist_key("tidal", src_id, title)
                    listed_keys.add(key)
                    if key in seen_keys:
                        continue
                    # owner_user_id (#28): genuinely this Trobar user's own
                    # linked Tidal account — same treatment as the Roon
                    # per-profile case above, not covered by #28 as
                    # originally filed (written before #21 existed) but
                    # the same exposure applies.
                    checkpoint()
                    outcome = _sync_one_playlist(
                        conn, tidal_client, "tidal", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], access_token=access_token, tidal_user_id=tidal_user_id,
                        mirror_caches=mirror_caches,
                    )
                    if outcome is None:
                        unread += 1
                        continue
                    seen_keys.add(key)
                    playlist_count += 1
                    track_count += outcome[0]
                    matched_count += outcome[1]
                if unread:
                    _source_failed(source_failures, "tidal", user_row,
                                   f"{unread} of {len(tidal_listing['playlists'])} playlists not read")
            _linked_account_health(health, "tidal", linked, source_failures)
            if linked:
                provider_ids.add("tidal")
            # Shares run after every linked account is read, on this pass's
            # tokens. Their changes reach Trobar's own copy of each playlist
            # on the next pass.
            if tidal_tokens:
                tidal_shares.sync_all(conn, tidal_tokens)

        # #10 Part B: direct per-user Spotify accounts, merged in exactly like
        # the Tidal block above — a separate parallel playlist source, one
        # refresh per linked user reused for that user's list + track fetches,
        # and the same per-owner stale-cleanup protection
        # (spotify_failed_user_ids) so one user's failed fetch never deletes
        # their still-valid, just-not-relisted playlists.
        sp_client_id = db.get_config(conn, "spotify_client_id")
        sp_client_secret = db.get_config(conn, "spotify_client_secret")
        # #398: gated on the experimental toggle too, not just whether
        # credentials are configured -- an admin turning this off must stop
        # a still-linked user's account from silently continuing to sync
        # in the background. Deliberately NOT clearing spotify_refresh_token
        # when the toggle flips off: that's a separate, reversible "pause"
        # rather than a disconnect, so re-enabling needs no re-link.
        if sp_client_id and sp_client_secret and db.get_config(conn, "experimental_spotify_enabled") == "1":
            linked = conn.execute(
                "SELECT id, spotify_refresh_token FROM users "
                "WHERE spotify_refresh_token IS NOT NULL"
            ).fetchall()
            for user_row in linked:
                try:
                    access_token, new_refresh_token = spotify_client.refresh_access_token(
                        sp_client_id, sp_client_secret, user_row["spotify_refresh_token"]
                    )
                except spotify_client.SpotifyAuthError:
                    # Revoked/expired — clear the link (Profile shows "reconnect")
                    # but protect this user's rows from cleanup this run.
                    conn.execute(
                        "UPDATE users SET spotify_refresh_token = NULL, spotify_user_id = NULL, "
                        "spotify_display_name = NULL WHERE id = ?",
                        (user_row["id"],),
                    )
                    conn.commit()
                    spotify_failed_user_ids.add(user_row["id"])
                    _source_failed(source_failures, "spotify", user_row, "token_rejected")
                    continue
                except spotify_client.SpotifyTransientError as e:
                    spotify_failed_user_ids.add(user_row["id"])
                    _source_failed(source_failures, "spotify", user_row, e)
                    continue
                if new_refresh_token != user_row["spotify_refresh_token"]:
                    conn.execute(
                        "UPDATE users SET spotify_refresh_token = ? WHERE id = ?",
                        (new_refresh_token, user_row["id"]),
                    )
                    conn.commit()

                # /me/playlists is scoped to the token's user — no id needed.
                sp_listing = spotify_client.list_playlists(access_token)
                if sp_listing["status"] != "ok":
                    spotify_failed_user_ids.add(user_row["id"])
                    _source_failed(source_failures, "spotify", user_row, sp_listing.get("reason"))
                    continue  # transient error — retry next sync
                unread = 0
                for pl in sp_listing["playlists"]:
                    src_id, title = pl["id"], pl["title"]
                    key = _playlist_key("spotify", src_id, title)
                    listed_keys.add(key)
                    if key in seen_keys:
                        continue
                    checkpoint()
                    outcome = _sync_one_playlist(
                        conn, spotify_client, "spotify", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], access_token=access_token,
                        mirror_caches=mirror_caches,
                    )
                    if outcome is None:
                        unread += 1
                        continue
                    seen_keys.add(key)
                    playlist_count += 1
                    track_count += outcome[0]
                    matched_count += outcome[1]
                if unread:
                    _source_failed(source_failures, "spotify", user_row,
                                   f"{unread} of {len(sp_listing['playlists'])} playlists not read")
            _linked_account_health(health, "spotify", linked, source_failures)
            if linked:
                provider_ids.add("spotify")

        # Public playlists subscribed to by URL. Unlike the two blocks
        # above there is no credential to refresh, nothing to link and no
        # admin configuration to gate on -- a subscription row IS the whole
        # configuration -- so this runs whenever one exists, under whatever
        # provider is active. With none, not a single external call is
        # made: the query returns nothing, so no client is called (and
        # ytmusicapi is not even imported: its import is function-local).
        #
        # Each subscription is ONE playlist, so there is no listing step
        # and no listing failure to distinguish from an empty listing --
        # the fetch either produces the playlist or reports why not, and
        # sync_one_subscription records that on the row.
        subscriptions = conn.execute(
            "SELECT id, owner_user_id, provider, external_id, title "
            "FROM playlist_subscriptions ORDER BY id"
        ).fetchall()
        for sub in subscriptions:
            if sub["provider"] not in _SUBSCRIPTION_CLIENTS:
                continue  # a row from a newer version, or a removed source
            provider_ids.add(sub["provider"])
            src_id = subscription_playlist_key(sub["id"])
            checkpoint()
            # Named apart from the `outcome` the provider blocks above use:
            # that one is a (tracks, matched) tuple-or-None, this one is a
            # summary dict, and they are in the same function scope.
            sub_outcome = sync_one_subscription(
                conn, sub, mirror_caches=mirror_caches)
            health.append(source_health.outcome(
                source_health.PUBLIC_LINK, sub["provider"], sub_outcome["status"] == "ok",
                sub_outcome["status"], user_id=sub["owner_user_id"], subscription_id=sub["id"]))
            if sub_outcome["status"] != "ok":
                # #71's lesson, and the one this source is most exposed to:
                # an unofficial endpoint refusing us today must not read as
                # "the user deleted this playlist". Not listing its key
                # leaves the row unprotected by the pass below, so protect
                # it explicitly instead.
                subscription_failed_ids.add(src_id)
                continue
            listed_keys.add(_playlist_key(sub["provider"], src_id, sub_outcome["title"]))
            playlist_count += 1
            track_count += sub_outcome["tracks"]
            matched_count += sub_outcome["matched"]

        # #26: only meaningful once this run has actually populated both
        # sides — a library sync (this pass, or a merge above) and at least
        # one linked account's playlists (the block just above). No-ops
        # cleanly (see the function's own docstring) if either side is empty.
        ma_sources = _ma_sources(listing if provider is music_assistant_client else ma_listing)
        if provider is roon_client or ma_sources is not None:
            _infer_playlist_origins(conn, ma_sources)

        # Rule playlists are computed from the library, not listed by any
        # source, so "rule" never joins provider_ids and the cleanup below
        # leaves them alone.
        rule_playlists.refresh_all(conn)

        conn.commit()

        # Anything this sync is authoritative for (source_provider among
        # the provider(s) just synced) that wasn't listed has been
        # deleted/renamed at the source — remove it here too rather than
        # leaving a stale row forever. Any playlist-type selection
        # targeting it goes through the normal delete_selection() path so
        # affected devices are actually told to remove the files, same as
        # an explicit user deletion — not just silently orphaned to
        # resolve to nothing on the next sync.
        placeholders = ",".join("?" for _ in provider_ids)
        stale = conn.execute(
            f"SELECT id, source_provider, source_playlist_id, title, owner_user_id FROM playlists "
            f"WHERE source_provider IN ({placeholders})",
            tuple(provider_ids),
        ).fetchall()
        for row in stale:
            if _playlist_key(row["source_provider"], row["source_playlist_id"], row["title"]) in listed_keys:
                continue
            # #71: don't delete a Tidal row whose owner's fetch failed this
            # run — it wasn't relisted only because that one user's Tidal
            # sync errored, not because the playlist is gone.
            if row["source_provider"] == "tidal" and row["owner_user_id"] in tidal_failed_user_ids:
                continue
            # #10: same per-owner protection for Spotify rows.
            if row["source_provider"] == "spotify" and row["owner_user_id"] in spotify_failed_user_ids:
                continue
            # Same for a subscription whose fetch failed this run.
            if row["source_provider"] in _SUBSCRIPTION_CLIENTS \
                    and row["source_playlist_id"] in subscription_failed_ids:
                continue
            _carry_selections_from_mirror_copy(conn, row)
            _remove_playlist_row(conn, row["id"])
            removed_count += 1
        # #93: reclaim orphaned NULL-source_provider ghosts the scan above
        # can't see (selection-safe — see the helper).
        removed_count += _cleanup_ghost_playlists(conn)
        conn.commit()
        source_health.record(conn, health)
    finally:
        conn.close()

    result: dict = {"status": "ok", "playlists": playlist_count, "tracks": track_count,
                    "matched": matched_count, "removed": removed_count}
    if source_failures:
        result["source_failures"] = source_failures
    if not primary_ok:
        # Partial result (#128): the secondary merges ran, but the active
        # provider's own listing failed this run — surface it rather than
        # hiding a skipped primary behind a normal-looking response.
        result["primary_status"] = "error"
        result["primary_provider"] = provider_id
        reason = listing.get("reason") or listing.get("error")
        if reason:
            result["primary_error"] = reason
    return result
