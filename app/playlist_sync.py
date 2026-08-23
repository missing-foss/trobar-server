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

import db
import emby_client
import filesystem_client
import identity
import jellyfin_client
import jobs
import lidarr_requests
import mirror
import mirror_emby
import mirror_jellyfin
import mirror_subsonic
import roon_client
import spotify_client
import sync_state
import tidal_client

_log = logging.getLogger(__name__)

# #297 step 3: same durable-queue upgrade scanner.py already got — a sync now
# gets a row in `jobs` (survives a restart, observable/retryable from the
# admin panel) instead of a bare module-level lock + daemon thread.
#
# The handler itself is NOT registered here. It needs to resolve `provider_id`
# back to a live provider module, and only main.py's _PROVIDERS dict can do
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
            "AND state IN ('done', 'failed') ORDER BY id DESC LIMIT 1",
            (JOB_TYPE,)).fetchone()
    finally:
        conn.close()

    last_result = None
    if pending is not None:
        finished = None
    if finished is not None:
        if finished["state"] == "failed":
            last_result = {"status": "error", "reason": finished["last_error"] or "error"}
        elif finished["result"]:
            last_result = json.loads(finished["result"])
    return {"running": pending is not None, "last_result": last_result}


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


def _sync_one_playlist(
    conn, provider, provider_id: str, title: str, source_playlist_id: str | None = None,
    owner_user_id: int | None = None, subsonic_mirror_cache: dict | None = None,
    jellyfin_mirror_cache: dict | None = None, emby_mirror_cache: dict | None = None,
    **provider_kwargs
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

    `subsonic_mirror_cache`/`jellyfin_mirror_cache`/`emby_mirror_cache`:
    forwarded to mirror_subsonic.write_mirror()/mirror_jellyfin.write_
    mirror()/mirror_emby.write_mirror() — a dict PER SINK sync_playlists()
    creates ONCE per run and passes to every one of its
    _sync_one_playlist() calls, so N mirrored playlists share one target
    tag-index build per sink instead of each triggering their own full
    library walk against that sink's mirror target."""
    result = provider.get_playlist_tracks(title, source_playlist_id, **provider_kwargs)
    if result["status"] != "ok":
        return None

    if source_playlist_id is not None:
        row = conn.execute(
            "SELECT id, owner_user_id FROM playlists WHERE source_provider = ? AND source_playlist_id = ?",
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
                "SELECT id, owner_user_id FROM playlists "
                "WHERE source_provider = ? AND source_playlist_id IS NULL AND title = ?",
                (provider_id, title),
            ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, owner_user_id FROM playlists "
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
            "INSERT INTO playlists (title, source_provider, source_playlist_id, owner_user_id, shared, last_synced_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (title, provider_id, source_playlist_id, owner_user_id, initial_shared),
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
        conn.execute(
            f"UPDATE playlists SET title = ?, source_provider = ?, source_playlist_id = ?, "
            f"owner_user_id = ?{shared_clause}, last_synced_at = datetime('now') WHERE id = ?",
            (title, provider_id, source_playlist_id, owner_user_id, playlist_id),
        )

    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    track_count = matched_count = 0
    # #200: entries the resolver couldn't match, collected for the review
    # surface (sync_state.record_unresolved_playlist_tracks below) — a
    # provider-only track (streamed, never downloaded) is expected to show
    # up here and isn't itself a bug; it's what the review list lets an
    # owner acknowledge.
    unresolved: list[dict] = []
    for t in result["tracks"]:
        # identity.py's tiered resolver — path/fuzzy matching (matching.py,
        # unchanged) is still tiers 3 here; see its own docstring for the
        # full cascade. `t.get("isrc")` is None for every provider today
        # (#200 planning: checked all nine clients) — passed through anyway
        # so tiers 1/2 activate automatically once a future PR starts
        # supplying one, with no change needed at this call site.
        path = t.get("path")
        matched_id = identity.resolve_playlist_track(
            conn, artist=t["artist"], title=t["title"], path=path, isrc=t.get("isrc"),
        )
        if matched_id is not None:
            matched_count += 1
        else:
            unresolved.append({
                "position": t["position"], "artist": t["artist"], "title": t["title"],
                "album": t.get("album"), "isrc": t.get("isrc"),
            })
        track_count += 1
        conn.execute(
            "INSERT INTO playlist_tracks "
            "(playlist_id, position, artist, title, album, matched_track_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (playlist_id, t["position"], t["artist"], t["title"], t.get("album"), matched_id),
        )
    sync_state.record_unresolved_playlist_tracks(conn, playlist_id, unresolved)
    # #285/#189: rewrite this playlist's mirror(s) -- each a no-op unless
    # its own *_mirror_enabled flag is set -- now that matched_track_id is
    # fresh for every track. Same "resolve, then act on the result"
    # placement as the unresolved-tracks recording just above. Four
    # independent sinks, four independent failure surfaces (see each
    # module's own never-raises contract) -- one failing never blocks
    # any other or this playlist's own sync.
    mirror.write_mirror(conn, playlist_id)
    mirror_subsonic.write_mirror(conn, playlist_id, tag_index_cache=subsonic_mirror_cache)
    mirror_jellyfin.write_mirror(conn, playlist_id, tag_index_cache=jellyfin_mirror_cache)
    mirror_emby.write_mirror(conn, playlist_id, tag_index_cache=emby_mirror_cache)
    # #494: same "resolve, then act" placement as the four mirror sinks
    # above, and the same never-raises contract -- but a different verb
    # (requests missing ALBUMS from Lidarr, doesn't copy the playlist
    # anywhere) so it lives in its own module rather than being a fifth
    # mirror sink. See lidarr_requests.py's own module docstring.
    lidarr_requests.run_for_playlist(conn, playlist_id)
    # #133: commit this playlist's writes now, before the caller fetches the
    # NEXT playlist over the network. Otherwise a whole sync holds one open
    # write transaction across every provider round-trip — and since #136's
    # retry/backoff those fetches can sleep for many seconds — keeping the
    # SQLite write lock the entire time and colliding with a concurrent
    # library scan or device/selection write past the 30s busy_timeout. This
    # mirrors the scanner's own _COMMIT_EVERY periodic commits. Each playlist
    # is still written atomically (its old tracks are deleted + re-inserted in
    # this one transaction); the only thing given up is whole-run atomicity,
    # which is fine — a sync is re-runnable and the stale-cleanup at the end
    # only runs if the whole pass completes.
    conn.commit()
    return track_count, matched_count


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


def _infer_roon_playlist_origins(conn) -> None:
    """#26/#81/#147: Roon's Browse API exposes no source signal for a playlist —
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
    conn.execute(
        "UPDATE playlists SET inferred_origin_provider = NULL, golden_source_id = NULL "
        "WHERE inferred_origin_provider IS NOT NULL OR golden_source_id IS NOT NULL")

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


def sync_playlists(provider, provider_id: str) -> dict:
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
    tidal_failed_user_ids: set[int] = set()
    # #10 Part B: same per-owner protection for Spotify (see the Spotify block).
    spotify_failed_user_ids: set[int] = set()
    # #128: only providers we authoritatively listed this run are eligible for
    # stale-cleanup. The primary joins only when its listing succeeded;
    # filesystem/tidal add themselves below on their own success.
    provider_ids: set[str] = set()
    # One target tag-index build per sink, shared by every playlist this
    # run mirrors to it (see mirror_subsonic._get_tag_index()'s docstring)
    # — created once here rather than once per _sync_one_playlist() call.
    subsonic_mirror_cache: dict = {}
    jellyfin_mirror_cache: dict = {}
    emby_mirror_cache: dict = {}
    try:
        if primary_ok:
            provider_ids.add(provider_id)
            for pl in listing["playlists"]:
                src_id, title = pl["id"], pl["title"]
                key = _playlist_key(provider_id, src_id, title)
                listed_keys.add(key)
                outcome = _sync_one_playlist(
                    conn, provider, provider_id, title, source_playlist_id=src_id,
                    subsonic_mirror_cache=subsonic_mirror_cache, jellyfin_mirror_cache=jellyfin_mirror_cache,
                    emby_mirror_cache=emby_mirror_cache)
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
                    outcome = _sync_one_playlist(
                        conn, filesystem_client, "filesystem", title, source_playlist_id=src_id,
                        subsonic_mirror_cache=subsonic_mirror_cache, jellyfin_mirror_cache=jellyfin_mirror_cache,
                        emby_mirror_cache=emby_mirror_cache)
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
                    outcome = _sync_one_playlist(
                        conn, roon_client, "roon", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], roon_profile=profile,
                        subsonic_mirror_cache=subsonic_mirror_cache, jellyfin_mirror_cache=jellyfin_mirror_cache,
                        emby_mirror_cache=emby_mirror_cache,
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
                    outcome = _sync_one_playlist(
                        conn, jellyfin_client, "jellyfin", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], user_id=mapped_user_id,
                        subsonic_mirror_cache=subsonic_mirror_cache, jellyfin_mirror_cache=jellyfin_mirror_cache,
                        emby_mirror_cache=emby_mirror_cache,
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
                    outcome = _sync_one_playlist(
                        conn, emby_client, "emby", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], user_id=mapped_user_id,
                        subsonic_mirror_cache=subsonic_mirror_cache, jellyfin_mirror_cache=jellyfin_mirror_cache,
                        emby_mirror_cache=emby_mirror_cache,
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
                    access_token, new_refresh_token = tidal_client.refresh_access_token(
                        client_id, client_secret, user_row["tidal_refresh_token"]
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
                        "tidal_display_name = NULL WHERE id = ?",
                        (user_row["id"],),
                    )
                    conn.commit()
                    tidal_failed_user_ids.add(user_row["id"])
                    continue
                except tidal_client.TidalTransientError:
                    # Network/5xx/timeout — nothing wrong with the stored
                    # token, retry next sync (same as every other
                    # provider's transient-failure handling here).
                    tidal_failed_user_ids.add(user_row["id"])
                    continue
                if new_refresh_token != user_row["tidal_refresh_token"]:
                    conn.execute(
                        "UPDATE users SET tidal_refresh_token = ? WHERE id = ?",
                        (new_refresh_token, user_row["id"]),
                    )
                    conn.commit()

                tidal_user_id = user_row["tidal_user_id"]
                tidal_listing = tidal_client.list_playlists(access_token, tidal_user_id)
                if tidal_listing["status"] != "ok":
                    tidal_failed_user_ids.add(user_row["id"])
                    continue  # transient error — retry next sync
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
                    outcome = _sync_one_playlist(
                        conn, tidal_client, "tidal", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], access_token=access_token, tidal_user_id=tidal_user_id,
                        subsonic_mirror_cache=subsonic_mirror_cache, jellyfin_mirror_cache=jellyfin_mirror_cache,
                        emby_mirror_cache=emby_mirror_cache,
                    )
                    if outcome is None:
                        continue
                    seen_keys.add(key)
                    playlist_count += 1
                    track_count += outcome[0]
                    matched_count += outcome[1]
            if linked:
                provider_ids.add("tidal")

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
                    continue
                except spotify_client.SpotifyTransientError:
                    spotify_failed_user_ids.add(user_row["id"])
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
                    continue  # transient error — retry next sync
                for pl in sp_listing["playlists"]:
                    src_id, title = pl["id"], pl["title"]
                    key = _playlist_key("spotify", src_id, title)
                    listed_keys.add(key)
                    if key in seen_keys:
                        continue
                    outcome = _sync_one_playlist(
                        conn, spotify_client, "spotify", title, source_playlist_id=src_id,
                        owner_user_id=user_row["id"], access_token=access_token,
                        subsonic_mirror_cache=subsonic_mirror_cache, jellyfin_mirror_cache=jellyfin_mirror_cache,
                        emby_mirror_cache=emby_mirror_cache,
                    )
                    if outcome is None:
                        continue
                    seen_keys.add(key)
                    playlist_count += 1
                    track_count += outcome[0]
                    matched_count += outcome[1]
            if linked:
                provider_ids.add("spotify")

        # #26: only meaningful once this run has actually populated both
        # sides — a Roon sync (this pass, or the per-profile merge above)
        # and at least one linked Tidal account's playlists (the block
        # just above). No-ops cleanly (see the function's own docstring)
        # if either side is empty.
        if provider is roon_client:
            _infer_roon_playlist_origins(conn)

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
            for sel in conn.execute(
                "SELECT id FROM selections WHERE type = 'playlist' AND target = ?", (str(row["id"]),)
            ).fetchall():
                sync_state.delete_selection(conn, sel["id"])
            # #285/#189: delete every sink's mirror BEFORE the row itself
            # goes away, so a removed golden source doesn't leave an
            # orphaned mirror file or remote playlist behind.
            mirror.delete_mirror(conn, row["id"])
            mirror_subsonic.delete_mirror(conn, row["id"])
            mirror_jellyfin.delete_mirror(conn, row["id"])
            mirror_emby.delete_mirror(conn, row["id"])
            conn.execute("DELETE FROM playlists WHERE id = ?", (row["id"],))
            removed_count += 1
        # #93: reclaim orphaned NULL-source_provider ghosts the scan above
        # can't see (selection-safe — see the helper).
        removed_count += _cleanup_ghost_playlists(conn)
        conn.commit()
    finally:
        conn.close()

    result: dict = {"status": "ok", "playlists": playlist_count, "tracks": track_count,
                    "matched": matched_count, "removed": removed_count}
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
