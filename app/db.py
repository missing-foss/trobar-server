#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite schema and connection helper for Trobar."""

import logging
import os
import sqlite3
from pathlib import Path

_log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
# Historical filename from before the Trobar rename — kept so existing
# deployments' databases keep opening; renaming it would orphan them all.
DB_PATH = DATA_DIR / "music-sync.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT,
    lastfm_username TEXT,
    cover_view_mode TEXT NOT NULL DEFAULT 'list',
    show_reissue_year INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Generic key/value store for app-wide (not per-user) settings — provider
-- host/port, default fallback API keys, etc. Flat + provider-prefixed keys
-- (roon_host, roon_port, later e.g. jellyfin_base_url) rather than a single
-- JSON blob: only one provider is ever active at a time, so there's no need
-- for the extra structure, and plain key lookups stay simple SQL.
CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'phone',
    api_token_hash TEXT NOT NULL,
    max_size_bytes INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT
);

-- #161: the device-path manifest entries a source_of_truth='device' device
-- holds that don't match any live library track (side-loaded, or kept after
-- the library track was deleted). Refreshed on every manifest upload; `adopted`
-- lets the owner acknowledge an extra as device-owned so it stops being flagged
-- (devices.unknown_track_count counts only the non-adopted rows). artist/album/
-- title are a best-effort parse of the device_path() for display.
CREATE TABLE IF NOT EXISTS device_unknown_tracks (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    artist TEXT,
    album TEXT,
    title TEXT,
    adopted INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (device_id, path)
);

-- STRICT (#298) because fingerprint/isrc/acoustid_isrc are compared with `=`
-- as identity by #239's recovery rematch, and TEXT affinity stores Python
-- `bytes` as a BLOB without complaint — a value that then silently never
-- matches. That trap was hit twice (#292 fingerprint.py, #296 provenance.py)
-- and fixed per-call-site both times; STRICT makes the third occurrence a
-- write-time error instead of a wrong answer.
--
-- The columns here are only the original 13. fingerprint and friends are
-- added by _MIGRATIONS/_run_migrations, and ALTER TABLE ADD COLUMN works on a
-- STRICT table as long as the declared type is one of the six it permits --
-- which is already true of every entry in _MIGRATIONS. For databases created
-- before this, _migrate_tracks_strict rebuilds the table.
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path TEXT UNIQUE NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    title TEXT NOT NULL,
    track_no INTEGER,
    disc_no INTEGER,
    year INTEGER,
    reissue_year INTEGER,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    scanned_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS idx_tracks_artist_album ON tracks(artist, album);

-- Provider-neutral names (was roon_playlists/roon_playlist_tracks,
-- roon_title) — only one provider is ever active at a time (see app_config
-- above), so a shared schema makes more sense than Roon-branded table names
-- now that Subsonic is a second provider. See _migrate_playlist_tables for
-- the rename applied to any database created before this.
-- title is NOT globally UNIQUE (#75): two playlists can legitimately share
-- a title — across providers (a Roon "Party" and a filesystem "Party"), or
-- from one provider that exposes stable ids (two Subsonic "Party"s). See
-- _migrate_playlists_composite_key for the uniqueness that replaced it:
-- keyed on (source_provider, source_playlist_id) where a provider supplies
-- an id, else (source_provider, title) for Roon, whose Browse API gives no
-- stable id. source_provider/source_playlist_id/owner_user_id/shared/
-- inferred_origin_provider are all added by _MIGRATIONS/_run_migrations for
-- existing DBs; a fresh DB gets them here too so the composite-key indexes
-- (created in that migration) have their columns.
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_provider TEXT,
    source_playlist_id TEXT,
    owner_user_id INTEGER REFERENCES users(id),
    shared INTEGER NOT NULL DEFAULT 1,
    inferred_origin_provider TEXT,
    golden_source_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    artist TEXT,
    title TEXT,
    album TEXT,
    matched_track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist ON playlist_tracks(playlist_id);

-- #200: playlist_tracks rows the identity resolver couldn't match to any
-- local track after all of identity.py's tiers (exact ISRC,
-- fingerprint-backfilled ISRC, today's path/fuzzy matcher — three, not the
-- four an earlier version of this comment claimed; the mooted lazy-fingerprint
-- tier was never built, see identity.py's own docstring). Same shape/purpose as
-- device_unknown_tracks above (a review surface, `excluded` acknowledges
-- "not a real gap, stop flagging it" the same way `adopted` does there) but
-- playlist-scoped rather than device-scoped — a different axis entirely, so
-- a separate table rather than overloading that one. Repopulated by
-- identity.py on every sync (stale rows for tracks that now match are
-- removed, not just left stale) — see playlist_sync.py.
-- STRICT (#298): named alongside jobs/device_provenance as one of the "new
-- tables STRICT from now on" set. All columns are already one of the six
-- STRICT-permitted types, so this costs nothing on a fresh DB;
-- _migrate_unresolved_playlist_tracks_strict rebuilds an existing one, same
-- as _migrate_tracks_strict.
CREATE TABLE IF NOT EXISTS unresolved_playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    artist TEXT,
    title TEXT,
    album TEXT,
    isrc TEXT,
    excluded INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_unresolved_playlist_tracks_playlist
    ON unresolved_playlist_tracks(playlist_id);
-- Upsert key for sync_state._record_unresolved_playlist_tracks: a playlist
-- entry has no stable id of its own (no path for most providers, and
-- `position` shifts on reorder), so (artist, title, album) is the closest
-- thing to an identity — good enough to preserve `excluded` across a
-- resync for the common case. The recording function normalizes NULL to
-- '' for these three columns specifically so this index's uniqueness check
-- actually catches repeats (SQLite treats two NULLs as distinct, which
-- would otherwise let every resync insert a fresh duplicate row for any
-- entry missing one of these fields).
CREATE UNIQUE INDEX IF NOT EXISTS idx_unresolved_playlist_tracks_identity
    ON unresolved_playlist_tracks(playlist_id, artist, title, album);

-- #494: one row per (artist, album) Lidarr has ever been asked about, on
-- behalf of ANY playlist's "Request missing albums" toggle -- deliberately
-- NOT scoped per-playlist (no playlist_id here at all) and NOT stored on
-- unresolved_playlist_tracks above: that table is fully DELETE+recreated on
-- every single playlist sync (see sync_state.record_unresolved_playlist_tracks),
-- so anything written there forgets on the very next sync. The same missing
-- album surfacing as a gap in three different playlists must only ever be
-- requested once -- this table is what makes that true, and what makes
-- "already tried, don't ask again" a single indexed lookup instead of
-- re-querying Lidarr/MusicBrainz for data that won't change.
--
-- Records the outcome of EVERY attempt, success or failure -- a 'failed' or
-- 'partial' row is deliberately never retried on a later sync, to avoid
-- hammering Lidarr/MusicBrainz for a pair that keeps failing for a real,
-- non-transient reason (a deleted quality profile, a name MusicBrainz has
-- no match for at all). See lidarr_requests.py's own module docstring.
--
-- STRICT (#298): a new table, so this costs nothing -- see the note on
-- unresolved_playlist_tracks above for what STRICT buys here.
CREATE TABLE IF NOT EXISTS lidarr_requested_albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- matching.normalize()'d -- the same Unicode-correct casefold
    -- mirror_subsonic.py's own tag index already uses, reused rather than
    -- inventing a second normalization scheme.
    normalized_artist TEXT NOT NULL,
    normalized_album TEXT NOT NULL,
    -- Display copies, as first seen -- kept as-is (not re-derived from the
    -- normalized form) purely for the admin overview panel.
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    -- requested | partial | failed. 'partial' means the album now exists in
    -- Lidarr (POST /api/v1/album succeeded) but the follow-up PUT
    -- .../monitor call that actually puts it on the wanted list did not --
    -- see lidarr_client.add_and_monitor_album's own docstring for why that
    -- second call is required at all. Both 'partial' and 'failed' are dead
    -- ends here, same as a genuine lookup miss.
    status TEXT NOT NULL,
    -- Lidarr's own ids, when known -- kept even on a 'partial'/'failed' row
    -- so an admin looking at a stuck one can go find the half-created
    -- artist/album directly in Lidarr's own UI, rather than re-searching.
    lidarr_artist_id INTEGER,
    lidarr_album_id INTEGER,
    -- lidarr_client's own error reason, untranslated -- this is background
    -- job output with no request-scoped locale, same posture as
    -- mirror_last_error elsewhere in this file.
    error TEXT,
    requested_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_lidarr_requested_albums_identity
    ON lidarr_requested_albums(normalized_artist, normalized_album);

-- admin-granted "grantee can fully manage target's devices" rights.
-- Not a hierarchy — the admin can grant multiple independent delegations
-- over the same target (e.g. both mum and dad managing kid1's devices).
CREATE TABLE IF NOT EXISTS device_delegations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grantee_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(grantee_user_id, target_user_id)
);

-- The visibility half of delegation: a delegated device only shows up in a
-- non-admin's own Appareils list once they've pinned it (admin sees every
-- device unconditionally, pins or not). Separate from device_delegations so
-- revoking the grant and unpinning are independent, deliberate actions.
CREATE TABLE IF NOT EXISTS device_pins (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, device_id)
);

CREATE TABLE IF NOT EXISTS selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    target TEXT NOT NULL,
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- #303/#501: the cross-surface staging basket. A row here is "staged
-- against at least one device, not sent yet" — items accumulate here from
-- any surface (Suggestions, Recently Added/Released, Library, an artist
-- page, a playlist) before a fan-out step turns each (device, item) pair
-- into a real `selections` row via sync_state.create_selection, exactly
-- like the existing single-item device picker already does. Server-side
-- (not client-only) so the basket survives a reload and follows the user
-- across machines/browsers.
--
-- #501: the destination device(s) ARE chosen at stage time (see
-- basket_item_devices right below) — this table stopped being
-- "destination not chosen yet" once staging itself required device_ids.
-- What's still deferred is only WHEN each device's staged items get sent.
--
-- UNIQUE(user_id, type, target): adding the same item twice (double-click,
-- picked from two surfaces that share it, or staged for a second device
-- later) is a no-op on THIS table — same find-or-create convention as
-- `selections` itself — with the second stage's new device_ids merged
-- into basket_item_devices rather than creating a duplicate row.
--
-- #349: user_id (ON DELETE CASCADE below), not shareable/handed-over — a
-- deliberate decision, not an oversight the next reader should "fix". A
-- basket CAN still fan out to a delegated device (api_basket_fan_out()'s
-- own _require_device_access already permits that; see the note there),
-- it just always belongs to the one user who built it.
--
-- STRICT: new tables are STRICT from now on.
CREATE TABLE IF NOT EXISTS basket_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    target TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, type, target)
) STRICT;

-- #501: which device(s) a basket item is staged against — mirrors
-- selection_devices' own shape exactly. An item staged for two devices is
-- one basket_items row plus two rows here, so basket_items' own
-- UNIQUE(user_id, type, target) stays correct unchanged; the device
-- dimension lives entirely in this join table.
--
-- Invariant maintained everywhere a basket_items row is touched: a row
-- here never has zero links. A device-less basket item has nowhere to
-- render under in the per-device basket panel, so
-- sync_state.unstage_basket_item_device deletes the basket_items row too
-- the moment its last link goes, rather than keeping a device-less row
-- around the way toggle_selection_device deliberately does for
-- `selections` (which has no per-device rendering to worry about).
--
-- STRICT, matching basket_items itself (selection_devices predates the
-- #298 STRICT convention, so it's the odd one out, not a pattern to copy).
CREATE TABLE IF NOT EXISTS basket_item_devices (
    basket_item_id INTEGER NOT NULL REFERENCES basket_items(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (basket_item_id, device_id)
) STRICT;

CREATE TABLE IF NOT EXISTS selection_devices (
    selection_id INTEGER NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (selection_id, device_id)
);

CREATE TABLE IF NOT EXISTS device_track_state (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (device_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_device_track_state_device ON device_track_state(device_id, status);

-- #163: short-lived, single-use enrollment grants. An authenticated web
-- session mints one; a mobile client redeems it (QR/short code) to create its
-- own device and receive a device token, without the app ever holding user
-- credentials (#162). Only the code's hash is stored.
CREATE TABLE IF NOT EXISTS enrollment_grants (
    code_hash TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

-- #446/#474: admin-minted Bearer tokens for external integrations (Home
-- Assistant, Grafana, uptime monitors) -- a caller that's neither a browser
-- session nor a device. Deliberately its own table/hash rather than reusing
-- devices.api_token_hash: minting a fake "device" to get a readable token
-- would pollute the device list and its sync-state bookkeeping with
-- something that never syncs. Only the hash is stored, same as device
-- tokens; the raw value is shown once, at creation.
--
-- #474 originally shipped this as TWO tables -- a read-only api_tokens,
-- mintable by any user, and a separate action_tokens that could also
-- trigger a rescan -- so that "read-only" stayed a structural property of
-- one type rather than a capability check on it. That PR sat for review
-- before merging, and the actual objection was one level up: should an
-- integration credential be able to cause an action AT ALL. Once the
-- answer was yes, the two-table split stopped earning its complexity --
-- it existed to protect a boundary (read vs. write) that a *different*
-- boundary (who may mint one) protects just as well, with one secret
-- instead of two.
--
-- So: ONE table, and the safety property moved from "which table" to
-- "who was logged in when POST /api/integration-tokens was called" --
-- see main.py's api_integration_tokens, gated by require_admin(). A
-- household realistically has one admin (the person who installed the
-- server) and everyone else uses phones/watches, never their own
-- integrations. Every row
-- here is authenticated by the one authenticate_integration_token() /
-- _authenticated_integration_token(), used by all three
-- /api/integrations/* routes (devices, server, actions/scan) -- there is
-- no read-only/action distinction left to enforce at the credential
-- level, because minting one already required the same trust an admin
-- session has.
--
-- Still per-owner, still separately revocable per token (name it "Home
-- Assistant" vs "Grafana" and turn one off without the other) -- that
-- part of #446/#474's design was never the problem, and nothing here
-- changes it.
-- STRICT: new tables are STRICT from now on.
CREATE TABLE IF NOT EXISTS integration_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
) STRICT;

-- Materialized track set for an 'autofit' selection. Auto-fit ranks
-- the device owner's most-played albums (Last.fm) and greedily fits whole
-- albums into the device's remaining storage budget; the chosen tracks are
-- frozen here at refresh time so the synced set is stable between on-demand
-- refreshes (no churn as play-counts drift). The normal resolver reads these
-- like any other selection's tracks.
CREATE TABLE IF NOT EXISTS autofit_tracks (
    selection_id INTEGER NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    PRIMARY KEY (selection_id, track_id)
);

-- #297: durable queue for long-running background work (library scan,
-- playlist sync, fingerprint backfill, device provenance). Before this, each
-- was a bespoke module-level threading.Lock + fire-and-forget daemon thread +
-- hand-rolled "last result" global — which meant a failure in any of them was
-- invisible to the person running the server (`_log.exception` was the entire
-- failure story), nothing could be retried or cancelled, and work interrupted
-- by a restart was simply lost with nothing to notice it had been.
--
-- Viable in-process precisely because of two things that already hold here:
-- WAL is on (see get_conn — a long job's writes don't block readers) and the
-- app is deliberately single-process (see main.py's waitress call), so the
-- worker is just another thread and needs no IPC, supervisor or second
-- service.
--
-- STRICT (SQLite 3.37+; the shipped image has 3.46) because the
-- "bytes silently stored as a BLOB in a TEXT column" trap has now needed a
-- hand-written .decode() guard in two separate modules (fingerprint.py, then
-- provenance.py). SQLite's default type affinity does not convert bytes, so
-- it persists them as a BLOB without complaint; STRICT rejects it at insert
-- time instead. Verified directly, not assumed from the docs.
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    -- JSON object, or NULL for a job whose type says everything.
    payload TEXT,
    -- queued -> running -> done | failed. A failed job keeps last_error and
    -- can be requeued (by the admin, or by the retry logic in jobs.py).
    state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    -- The handler's own return value as JSON (scan counts and the like), so
    -- the admin overview can show what a run actually did rather than only
    -- that it finished. Replaces the per-module _last_result globals, which
    -- didn't survive a restart.
    result TEXT,
    -- #335 follow-up: how many times the PROCESS DIED while this job was
    -- running. Deliberately separate from `attempts`, which claim() increments
    -- once per claim and therefore counts handler exceptions and process deaths
    -- indistinguishably. The reaper's message reports deaths, so it must not read
    -- a number that also includes raises — a job that raised twice and was
    -- interrupted once would otherwise be reported as three crashes, sending
    -- someone hunting for crashes that never happened.
    --
    -- The retry BUDGET stays on `attempts`: a job that has consumed three
    -- attempts should stop regardless of how it consumed them.
    interruptions INTEGER NOT NULL DEFAULT 0,
    -- #297 step 3: live progress as JSON {"done": n, "total": m, "label": str},
    -- written by the handler via jobs.set_progress. NULL when a job type has
    -- nothing meaningful to report. Cleared when a job succeeds (`result` says
    -- what it did) but DELIBERATELY KEPT on failure, so a scan that died at
    -- 12,431 of 58,783 files still says where it stopped.
    progress TEXT,
    -- Set to hold a job back until a time (retry backoff). NULL = claimable
    -- immediately.
    run_after TEXT,
    -- Collapses "don't queue a second one of these" into a DB constraint (see
    -- the partial unique index below) instead of a per-module lock. NULL opts
    -- out, for job types where concurrent instances are fine.
    dedupe_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs(state, run_after, id);
-- The overlap guard, enforced by the database: at most one queued-or-running
-- job per dedupe_key. Scoped to those two states so a finished job never
-- blocks the next one with the same key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND state IN ('queued', 'running');

-- #239 PR 2: what a client says it holds. A device pushes back the provenance
-- DB it built from GET /api/device/fingerprints — (path, fingerprint) per file
-- it has — and the server rematches those fingerprints against its own library
-- instead of comparing paths.
--
-- Why fingerprints and not paths: identity today is a byte-exact device_path()
-- comparison (see sync_state.record_device_manifest), and device_path() is
-- built from tags, track_no/disc_no, fs_segment()'s sanitisation rules and the
-- transcode extension. Any drift in ANY of those makes Trobar fail to
-- recognise files it wrote itself — that's the "~50 albums listed as unknown to
-- adopt" symptom in #161. Audio content doesn't drift when a tag does.
--
-- STRICT: new tables are STRICT from now on.
CREATE TABLE IF NOT EXISTS device_provenance (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    -- device_path() wire form, exactly as the client pushed it. Shares its
    -- identity with device_unknown_tracks' own (device_id, path) PK, which is
    -- what lets a successful rematch clear the corresponding "unknown" row.
    path TEXT NOT NULL,
    -- The client's CLAIM about what it holds. Untrusted: it's only ever used to
    -- look up a candidate track, and the located file is re-fingerprinted
    -- before anything is believed. See provenance.rematch_device.
    fingerprint TEXT NOT NULL,
    -- The track id the CLIENT recorded. Deliberately NOT a foreign key and
    -- never matched on: after the server-DB loss this feature exists to
    -- recover from, those ids are gone or renumbered, so the client's id is
    -- meaningless. That is precisely why the fingerprint is the mechanism.
    -- Kept for diagnostics only.
    claimed_track_id INTEGER,
    -- pending -> matched | unmatched. `unmatched` is a real outcome, not an
    -- error: side-loaded audio the server has never seen belongs here.
    state TEXT NOT NULL DEFAULT 'pending',
    matched_track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    pushed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (device_id, path)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_device_provenance_pending
    ON device_provenance(device_id, state);
"""


# #299: filesystems where SQLite's POSIX advisory locking is unreliable or
# simply absent. SQLite's own "How To Corrupt An SQLite Database File" names
# this as a corruption cause (§2.1, "Filesystems with broken or missing lock
# implementations"), and WAL — which get_conn enables below — makes it WORSE
# rather than better on a network share, because WAL needs shared-memory
# coordination between processes that these filesystems don't provide.
#
# Not exhaustive and can't be: the point is to catch the realistic cases a
# self-hoster actually hits (a NAS over NFS or SMB), not to enumerate every
# network filesystem in existence. Unknown types are treated as local, so a
# false NEGATIVE is the failure mode — deliberately, since a false positive
# would nag someone whose setup is fine.
_NETWORK_FS_TYPES = frozenset({
    "nfs", "nfs4", "cifs", "smb2", "smb3", "smbfs", "afs", "afpfs", "9p",
    "ceph", "glusterfs", "ncpfs", "coda", "davfs", "lustre", "gfs2", "ocfs2",
    # FUSE-based ones report as fuse.<name>; these are the common remote ones.
    "fuse.sshfs", "fuse.rclone", "fuse.glusterfs", "fuse.davfs2", "fuse.s3fs",
})


def _unescape_mount_point(field: str) -> str:
    r"""/proc/mounts octal-escapes characters that would otherwise break its
    space-separated format: space as \040, tab as \011, newline as \012 and
    backslash itself as \134.

    Without this, a DATA_DIR containing a space never matches its own mount
    entry and falls back to a shorter one (usually `/`), so a share mounted at
    e.g. "/mnt/My NAS/trobar" would be reported as local — the warning would
    silently not fire for a perfectly plausible path. Backslash is undone LAST,
    so a literal backslash in a path can't be re-interpreted as the start of
    another escape."""
    for escape, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n")):
        field = field.replace(escape, char)
    return field.replace("\\134", "\\")


def filesystem_type(path: Path) -> str | None:
    """The filesystem type `path` actually sits on, per /proc/mounts, or None
    if it can't be determined (no /proc, unreadable, non-Linux).

    Longest-matching-mount-point wins, which is what makes this correct for a
    container: a docker bind mount gets its OWN /proc/mounts entry carrying the
    real underlying type, so binding a host path that lives on a network share
    shows up as `nfs`/`cifs` inside the container rather than as the image's
    overlay. Verified against a genuine NFS mount, not assumed."""
    try:
        target = os.path.realpath(path)
        best_mount, best_type = "", None
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point, fs_type = _unescape_mount_point(parts[1]), parts[2]
                if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
                    if len(mount_point) >= len(best_mount):
                        best_mount, best_type = mount_point, fs_type
        return best_type
    except OSError:
        return None


def data_dir_network_fs() -> str | None:
    """The network-filesystem type DATA_DIR is on, or None if it's local (or
    undetectable). Truthy means the SQLite database is sitting somewhere its
    locking can't be trusted — see _NETWORK_FS_TYPES.

    Only DATA_DIR matters here. A NAS-mounted MUSIC_ROOT is completely fine
    (Trobar only ever reads it, no locking involved), and that distinction is
    the whole reason this needs saying out loud: "my Trobar data lives on my
    NAS" is a natural conclusion and a damaging one."""
    fs_type = filesystem_type(DATA_DIR)
    return fs_type if fs_type in _NETWORK_FS_TYPES else None


def get_conn() -> sqlite3.Connection:
    new_db = not DB_PATH.exists()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # DATA_DIR holds plaintext provider credentials, password hashes and the
    # session-signing key — file permissions are the security
    # boundary, so keep the dir owner-only. Best-effort: on some filesystems
    # (e.g. certain bind mounts) chmod is a no-op or not permitted.
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    # timeout: wait for a competing writer (e.g. a long-running library scan)
    # instead of raising "database is locked" immediately — multiple routes
    # (web UI, device API, scanner) all open their own short-lived connection.
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    if new_db:
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after the initial schema — CREATE TABLE IF NOT EXISTS doesn't
# retrofit them onto an already-existing table (the prod DB had 58k rows
# scanned before these were added), so add them explicitly, idempotently.
_MIGRATIONS = [
    ("users", "lastfm_username", "TEXT"),
    ("users", "lastfm_api_key", "TEXT"),
    ("users", "listenbrainz_username", "TEXT"),
    ("users", "cover_view_mode", "TEXT NOT NULL DEFAULT 'list'"),
    ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
    # NULL unless: AUTH_MODE=local (every user), or the admin has set a
    # break-glass local password while in oidc/forward mode. See main.py.
    ("users", "password_hash", "TEXT"),
    # Uploaded avatar filename under DATA_DIR/avatars/ — only meaningful for
    # a local session; Authentik-authenticated requests show a Gravatar
    # instead (see main.py's _gravatar_url).
    ("users", "avatar_path", "TEXT"),
    ("devices", "max_size_bytes", "INTEGER"),
    # #217: auto-fit's own share of the device, as a percentage of
    # max_size_bytes (not of whatever's left after manual selections — see
    # refresh_autofit's docstring). 100 = fill it all, today's behaviour and
    # the default so existing devices are unaffected.
    ("devices", "autofit_percent", "INTEGER NOT NULL DEFAULT 100"),
    ("devices", "reported_free_bytes", "INTEGER"),
    ("devices", "reported_total_bytes", "INTEGER"),
    ("devices", "free_bytes_reported_at", "TEXT"),
    ("tracks", "year", "INTEGER"),
    # original release year stays in `year` (what the album list
    # sorts by); reissue_year is the file's own date/pressing year, shown
    # only when it differs and the user opted in.
    ("tracks", "reissue_year", "INTEGER"),
    ("users", "show_reissue_year", "INTEGER NOT NULL DEFAULT 0"),
    # NULL = originals (every existing device, unchanged); 'mp3_320'
    # = the desktop client transcodes lossless sources before writing.
    ("devices", "transcode_format", "TEXT"),
    # actual bytes written on the device, reported at ack — differs
    # from tracks.size on transcoding devices; usage math prefers it.
    ("device_track_state", "bytes_on_device", "INTEGER"),
    # seconds, from TinyTag at scan time — lets autofit estimate
    # MP3 320 sizes on transcoding devices. Backfills on a forced rescan.
    ("tracks", "duration", "REAL"),
    # NULL = no artist pictures on this device; 'small' (~512px)
    # or 'full'. Device-level so every client honours it and SD cards can
    # take the space-friendly variant.
    ("devices", "artist_images", "TEXT"),
    # #63: 'server' (default) = device conforms to server-computed requirements
    # (today's behavior); 'device' = the server never prunes this device's
    # tracks based on selections (it can still ADD, just not delete), so the
    # device survives a server-DB loss instead of being wiped.
    ("devices", "source_of_truth",
     "TEXT NOT NULL DEFAULT 'server' CHECK(source_of_truth IN ('server', 'device'))"),
    # #63: how many paths the device's last uploaded manifest carried that
    # aren't in the library (content the server doesn't know about). NULL = no
    # manifest uploaded yet; surfaced in the web UI so the owner can notice it.
    ("devices", "unknown_track_count", "INTEGER"),
    # JSON {"disabled": [...widget ids...], "settings": {widget_id: {...}}}.
    # NULL/unset = nothing disabled (every existing user keeps today's
    # "four fixed cards" behaviour with no backfill needed) — see
    # _profile_dict's dashboard_widgets handling in main.py.
    ("users", "dashboard_widgets", "TEXT"),
    # Best-effort YYYY-MM-DD, from the same originaldate/date tag `year`
    # already reads — but kept at full precision instead of truncated to a
    # bare year, since "recently released" needs real month granularity.
    # Backfilled below (_backfill_release_date) for rows scanned before
    # this column existed; true precision arrives on their next rescan.
    ("tracks", "release_date", "TEXT"),
    # Which provider synced this specific playlist ('roon'/'subsonic'/
    # 'jellyfin'/'filesystem') — for the icon shown next to its name.
    # filesystem-discovered playlists are always merged in alongside
    # whichever provider is active (see playlist_sync.py), so this can
    # genuinely differ per playlist even within one install. NULL for
    # rows synced before this column existed; self-heals on their next
    # sync (playlist_sync writes it on every sync, no backfill needed —
    # a provider switch already wipes all playlist rows anyway).
    ("playlists", "source_provider", "TEXT"),
    # Which Roon profile (by display name — see roon_client.py's
    # _switch_profile, resolved fresh at sync time rather than cached)
    # this Trobar user's playlist sync should run as. NULL = today's
    # unchanged fallback behaviour (whatever profile the connection
    # defaults to). Admin-set directly (Administration > Configuration),
    # no confirmation step. Locally-created Roon playlists are
    # profile-specific — but synced playlists still land in
    # the one shared playlists pool like every other source, same as
    # today, not a new per-user-private concept.
    ("users", "roon_profile", "TEXT"),
    # #21: unlike Roon (one shared Core connection, switchable "profile",
    # no separate auth per profile) Tidal has no admin-visible cross-account
    # API — a Tidal family plan is billing-only, each member is a fully
    # independent account with its own login. So this is a genuine per-user
    # OAuth link (Authorization Code + PKCE, see main.py's /profile/tidal/*
    # routes), not an admin-set mapping like roon_profile above. NULL = not
    # linked. Refresh tokens may rotate on use (grant_type=refresh_token can
    # return a new one) — tidal_client.py callers persist the latest value
    # every time they refresh, this column is never assumed stable.
    ("users", "tidal_refresh_token", "TEXT"),
    # Tidal's own account id — needed to address this user's collection
    # server-side (tidal_client.list_playlists' /userCollections/{id}/...).
    # Stashed at link time so every sync doesn't need an extra GET
    # /v2/users/me round-trip just to re-derive it.
    ("users", "tidal_user_id", "TEXT"),
    # Tidal's own display name/username for "Connected as: X" in the
    # Profile UI — fetched once at link time (GET /v2/users/me), not kept
    # live-synced; a Tidal-side rename just means a stale label until the
    # next disconnect/reconnect, not a functional problem.
    ("users", "tidal_display_name", "TEXT"),
    # #10 Part B: direct Spotify link — same per-user OAuth shape as Tidal
    # above (each household member links their own account; refresh token may
    # rotate, so callers persist the latest). NULL = not linked.
    ("users", "spotify_refresh_token", "TEXT"),
    # Spotify's own account id — stashed at link time (GET /me) so a sync
    # doesn't re-derive it, and used for per-owner attribution/stale-cleanup.
    ("users", "spotify_user_id", "TEXT"),
    # Spotify's display name for "Connected as: X" (nullable on Spotify, falls
    # back to the id) — fetched once at link time, not live-synced.
    ("users", "spotify_display_name", "TEXT"),
    # #28: which Trobar user a playlist is genuinely traceable to — only
    # ever set for the two per-user sync paths where that's actually true
    # (a profile-mapped Roon playlist, or a #21 direct-Tidal playlist —
    # #28 as filed only covered Roon, written before #21 existed, but the
    # same "one person's own account, not the shared pool" exposure
    # applies identically to Tidal, so both get the same treatment here
    # rather than leaving Tidal as an unaddressed gap). Subsonic/Jellyfin/
    # filesystem-discovered playlists and the primary Roon connection's
    # own (non-profile-specific) playlists have no natural single owner —
    # NULL, same as before this column existed, always visible to
    # everyone regardless of `shared` below.
    ("playlists", "owner_user_id", "INTEGER REFERENCES users(id)"),
    # Opt-OUT, not opt-in, default 1 (shared) — existing behavior must not
    # silently change for anyone the moment this ships; nothing vanishes
    # from another household member's device sync just because this
    # column now exists. Only meaningful when owner_user_id is set;
    # irrelevant (never filters anything out) for an unowned playlist.
    ("playlists", "shared", "INTEGER NOT NULL DEFAULT 1"),
    # #26: Roon's Browse API exposes zero source signal for a playlist (a
    # Roon-native playlist and a Tidal-imported one are indistinguishable
    # there — confirmed by research, a hard ceiling on Roon's own
    # API, not fixable from this app alone). When a direct Tidal account
    # is also linked (#21), playlist_sync.py diffs Roon-sourced playlists'
    # resolved track sets against that Tidal account's playlists —
    # substantial overlap is strong evidence of common origin. Purely a
    # display-layer enrichment, recomputed fresh every sync (cleared and
    # re-set, never left stale) — deliberately NOT tied to owner_user_id/
    # shared above, which stay strictly about which sync actually
    # produced the row for privacy enforcement; this is only ever
    # a cosmetic "likely came from Tidal" hint. NULL = no inferred match
    # (the common case), or nothing to diff against (no Tidal account
    # linked, or the primary provider isn't Roon this sync).
    ("playlists", "inferred_origin_provider", "TEXT"),
    # #75: the provider's own stable id for this playlist, where it exposes
    # one — Subsonic playlist id, Jellyfin item Id, Tidal /v2/playlists id,
    # the filesystem relative path. NULL for Roon, whose Browse API gives
    # no stable id (only titles + non-cacheable item_keys). This is the
    # upsert/uniqueness key that lets two same-titled playlists coexist as
    # separate rows: (source_provider, source_playlist_id) when set, else
    # (source_provider, title). Added here for existing DBs; the base
    # SCHEMA carries it for fresh DBs, and _migrate_playlists_composite_key
    # builds the two partial unique indexes + drops the old global
    # title-UNIQUE by rebuilding the table.
    ("playlists", "source_playlist_id", "TEXT"),
    # #81: golden-source attribution. On a Roon row that is ALSO reachable
    # via a linked streaming provider's own account (a "dual-source"
    # playlist — the household's Roon connection and the owner's direct
    # Tidal both expose it), this points at that streaming row's
    # playlists.id. Set purely by _infer_roon_playlist_origins at sync
    # time (recomputed fresh each run). Deliberately SEPARATE from
    # owner_user_id: owner_user_id is the *enforcement* owner (#28 hides an
    # owned+private row), and the Roon row must stay unowned/household-
    # visible; this is the *attribution* link used only at display time to
    # show a per-viewer "shared by X" badge and to suppress the Roon
    # duplicate for anyone who can already see the streaming golden copy.
    # NULL = not dual-source (a plain Roon playlist, or a Tidal-only
    # playlist which has no Roon row and stays private). ON DELETE
    # SET NULL: when the golden streaming row is removed (e.g. the owner
    # unlinks Tidal), the Roon row simply stops being dual-source — no
    # dangling ref, and the stale-cleanup's DELETE of that Tidal row can't
    # trip an FK violation.
    ("playlists", "golden_source_id", "INTEGER REFERENCES playlists(id) ON DELETE SET NULL"),
    # #200 (identity/matching layer, step 1 of the tiered resolver): from the
    # file's own tags (tinytag other.isrc — ID3 TSRC/TRC, FLAC/Vorbis
    # comments, WM/ISRC), populated at scan time. NULL for most files today
    # (poorly-tagged rips, formats/rippers that never wrote it) — a real gap,
    # not a bug; see matching.py/identity.py for how the resolver copes.
    ("tracks", "isrc", "TEXT"),
    # The remaining four are the AcoustID fingerprint cache (#200): written
    # only once a track has already missed every cheaper match tier, and only
    # ever computed once per track regardless of how many future syncs touch
    # it — see identity.py's resolver and scanner.py's post-lock backfill call
    # for why (fingerprinting is real audio decode + network I/O, and must
    # never run while _SCAN_LOCK/_SYNC_LOCK is held).
    #
    # #239: `fingerprint` is ALSO written by provenance.py now, on a
    # different trigger (device sync) with a different selection (any
    # device-synced track lacking one, regardless of isrc — #200 skips a
    # track that has its own ISRC tag forever). It is no longer a
    # write-only AcoustID implementation detail: it's the identity shipped
    # to clients and matched on during recovery, hence the index in
    # _post_migration_indexes.
    ("tracks", "fingerprint", "TEXT"),
    # AcoustID's own resolved ISRC/MusicBrainz recording id for this
    # fingerprint, when it has one — this is what actually makes tier 1
    # (exact ISRC) useful on a *later* sync even for a track whose own file
    # tag never had one.
    ("tracks", "acoustid_isrc", "TEXT"),
    ("tracks", "acoustid_mbid", "TEXT"),
    # NULL = never attempted. Set (to a timestamp) on both success AND
    # failure, so a track that genuinely has no AcoustID match isn't
    # re-fingerprinted (subprocess + HTTP round-trip) on every single sync.
    # Precisely: this means "an AcoustID LOOKUP was attempted", NOT "a
    # fingerprint was computed" — the two came apart, since
    # provenance.py computes fingerprints with no AcoustID involvement at all
    # (computation is purely local; only the lookup needs an API key). So
    # provenance.py deliberately never writes this column: doing so would tell
    # fingerprint.py's backfill a track had already been looked up and
    # permanently suppress its ISRC resolution.
    ("tracks", "fingerprint_checked_at", "TEXT"),
    # #239: when provenance.py last FAILED to compute a fingerprint for this
    # track (undecodable file, vanished mid-copy, exotic codec). Deliberately a
    # separate column from fingerprint_checked_at above, whose meaning must stay
    # exactly "an AcoustID lookup was attempted" — overloading it would
    # resurrect the very ISRC-suppression bug that comment warns about.
    #
    # NULL = never failed. It does NOT exclude a track from being retried
    # (there's no external cost, and a retry is what heals a transient read
    # error) — it only DEPRIORITISES it, which is what keeps a batch of
    # permanently-broken files from starving every fingerprintable track behind
    # them, and lets `pending` actually reach zero so a client polling on it
    # terminates. Cleared on a later success.
    ("tracks", "fingerprint_failed_at", "TEXT"),
    # #439: a strictly-increasing counter, NOT a timestamp, bumped exactly
    # when `fingerprint` is set to a new value (provenance._compute_one —
    # the only writer that ever changes it; fingerprint.py's own writes
    # always re-persist the identical value it read, alongside isrc/mbid,
    # so they never need to bump this). Lets GET /api/device/fingerprints
    # offer a real "what's new since I last asked" filter (?computed_after=)
    # instead of forcing every client to re-walk its entire fingerprint set
    # every sync (measured at several MB/day on a real library).
    #
    # A wall-clock timestamp was considered and rejected: provenance.py can
    # compute many fingerprints inside the same second during a bulk
    # backfill, and datetime('now')'s one-second resolution would tie
    # several rows to one value with no way to tell, mid-tie, which side of
    # a `computed_after=<that second>` cursor a given row belongs on — a
    # client could silently skip rows sharing the boundary second. A plain
    # integer counter has no resolution to lose: every row gets a distinct
    # value, so `> ?` can never split a tie.
    #
    # NULL = no fingerprint yet, same lifecycle as `fingerprint` itself
    # (scanner.py's content-changed reset clears both back to NULL
    # together, so a stale sequence value never survives past a fingerprint
    # actually going away).
    ("tracks", "fingerprint_seq", "INTEGER"),
    # #297 step 3: live progress for a running job, so the Background jobs panel
    # can show "12,431 / 58,783 files" instead of only "running". Needed as a
    # migration as well as in SCHEMA because 2.3.0 already shipped the jobs
    # table to real deployments.
    ("jobs", "progress", "TEXT"),
    # #335 follow-up: process deaths, counted separately from `attempts` so the
    # reaper's message can report crashes without counting handler exceptions.
    ("jobs", "interruptions", "INTEGER NOT NULL DEFAULT 0"),
    # #285 (playlist mirroring MVP, buildable slice of #189): opt-in,
    # per-playlist, any user who can see the playlist can toggle it (not
    # owner/admin-gated, unlike `shared` above) — see
    # main.py's playlist mirror-toggle route.
    ("playlists", "mirror_enabled", "INTEGER NOT NULL DEFAULT 0"),
    # The filename mirror.py last actually wrote under mirror_folder — kept
    # so a later title change (which changes the computed filename) can
    # find and marker-check-delete the now-stale old file instead of
    # leaving it orphaned. NULL = never written.
    ("playlists", "mirror_filename", "TEXT"),
    # NULL = never written. Set on every write attempt's success.
    ("playlists", "mirror_last_written_at", "TEXT"),
    # NULL = the last write attempt (if any) succeeded. Set on failure
    # (folder missing/unwritable, marker conflict) and surfaced in the
    # admin mirrors overview — mirror.write_mirror() never raises, so this
    # is the only record of a failure short of the server log.
    #
    # #428: holds ONLY the detail (an OS exception's text, or a computed
    # filename) as of this column pairing with it — never the whole
    # English sentence any more. mirror_last_error_code names which of the
    # five failure modes this is; the client renders a translated prefix
    # for the code and appends this detail, since an OSError's own text
    # arrives in the C library's locale and can never itself be
    # translated. NULL detail (unset_folder's case) needs no appending —
    # that one error is fully translatable on its own.
    ("playlists", "mirror_last_error", "TEXT"),
    ("playlists", "mirror_last_error_code", "TEXT"),
    # #303: JSON {surface: [device_id, ...]}. The device picker's own cheap
    # smart-default — "device_ids last chosen for a pick that came from this
    # surface" — pre-fills the picker instead of asking cold every time.
    # 'basket' is itself a valid surface key, used by the staging basket's
    # own fan-out step. NULL/unset = nothing remembered yet (today's
    # behaviour — the picker opens with nothing pre-selected).
    ("users", "basket_last_destinations", "TEXT"),
    # #411: per-user, like cover_view_mode/show_reissue_year above — matches
    # how the other Playlists/Library view preferences already persist.
    # 0 = today's behaviour (every playlist shown, unchanged).
    ("users", "hide_zero_match_playlists", "INTEGER NOT NULL DEFAULT 0"),
    # #189 second sink: a Subsonic/Navidrome API mirror, parallel to the
    # mirror_* filesystem columns above rather than a shared/normalized
    # shape — each sink's "where did we last write, and under what id"
    # is different enough (a filename vs. a remote playlist id) that a
    # generic child table would still need per-type columns anyway; two
    # sinks doesn't earn that generalization yet (revisit if a third
    # sink, e.g. Jellyfin, lands and the column count starts hurting).
    ("playlists", "subsonic_mirror_enabled", "INTEGER NOT NULL DEFAULT 0"),
    # The target server's own playlist id, returned by createPlaylist on
    # first write. NULL = never written. Passed back as the playlistId
    # param on every subsequent write, which is what makes that call a
    # full replace rather than creating a duplicate playlist each time.
    ("playlists", "subsonic_mirror_remote_id", "TEXT"),
    # Same semantics as mirror_last_written_at/mirror_last_error(_code)
    # above, for this sink. mirror_subsonic.write_mirror() never raises,
    # same contract as mirror.write_mirror() — this is the only record
    # of a failure short of the server log.
    ("playlists", "subsonic_mirror_last_written_at", "TEXT"),
    ("playlists", "subsonic_mirror_last_error", "TEXT"),
    ("playlists", "subsonic_mirror_last_error_code", "TEXT"),
    # #189 third sink: Jellyfin. This IS the "third sink lands" trigger the
    # comment above flagged for revisiting the generic-child-table
    # question — deliberately still not doing it here. A migration to
    # (playlist_id, sink, enabled, remote_id, last_written_at, last_error,
    # last_error_code) would touch three already-working sinks' worth of
    # routes/tests/frontend at once, alongside Jellyfin's own new code in
    # this same change — that's a real refactor in its own right, not a
    # side effect of adding a sink, and needs its own review round rather
    # than being bundled in silently. Worth raising explicitly before or
    # after Emby (#189's fourth planned sink) rather than deferred again
    # by default.
    ("playlists", "jellyfin_mirror_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("playlists", "jellyfin_mirror_remote_id", "TEXT"),
    ("playlists", "jellyfin_mirror_last_written_at", "TEXT"),
    ("playlists", "jellyfin_mirror_last_error", "TEXT"),
    ("playlists", "jellyfin_mirror_last_error_code", "TEXT"),
    # #189 fourth and (per the RFC) final sink: Emby. The comment above this
    # block flagged Emby by name as the point to revisit the generic
    # child-table question — still deliberately not doing it here, for the
    # same reason: that's a real refactor of three already-working sinks,
    # not a side effect of adding a fourth. Raising it explicitly again
    # rather than deferring silently a third time.
    ("playlists", "emby_mirror_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("playlists", "emby_mirror_remote_id", "TEXT"),
    ("playlists", "emby_mirror_last_written_at", "TEXT"),
    ("playlists", "emby_mirror_last_error", "TEXT"),
    ("playlists", "emby_mirror_last_error_code", "TEXT"),
    # #262: the Roon per-user profile mapping above, generalized to
    # Jellyfin/Emby -- both already do userId-scoped fetches for their own
    # read side (the admin API key can query ANY user's Items directly),
    # so extending to a per-Trobar-user map is small, unlike Roon's own
    # profile-SWITCH mechanism. This is the target-provider's own internal
    # user id (Jellyfin/Emby's `Id`, a GUID), not a username -- resolved
    # once via the admin mapping UI (Administration > Configuration),
    # same "admin-set directly, no confirmation step" posture as
    # roon_profile. NULL = today's unchanged fallback (the one configured
    # account's playlists, shared to everyone). LMS explicitly excluded
    # from this epic (#262): it has no per-user accounts to map to.
    ("users", "jellyfin_user_id", "TEXT"),
    ("users", "emby_user_id", "TEXT"),
    # #494: a fifth outbound writer, but NOT a mirror sink -- it doesn't
    # copy the playlist anywhere, it requests missing ALBUMS from a Lidarr
    # instance. Deliberately its own column set rather than reusing the
    # mirror_* naming above: "enabled"/"last run" here means something
    # different (a request COUNT this run, not a remote playlist id), and
    # conflating the two would make the still-not-done generic-sink-table
    # question (flagged repeatedly above) harder, not easier, to get right
    # later. The per-(artist,album) request log itself lives in the new
    # lidarr_requested_albums table (see SCHEMA above), not here -- these
    # columns are purely this playlist's own last-run summary.
    ("playlists", "lidarr_request_enabled", "INTEGER NOT NULL DEFAULT 0"),
    # NULL = never run. Set on every run, including one that requested zero
    # albums -- that's a normal outcome (nothing new since last time, or
    # this playlist's provider gives no album data at all), not an error.
    ("playlists", "lidarr_request_last_run_at", "TEXT"),
    ("playlists", "lidarr_request_last_count", "INTEGER NOT NULL DEFAULT 0"),
    # Holds only the MOST RECENT failure's detail, not an aggregate -- one
    # run can touch several albums, and the per-pair outcome is what
    # lidarr_requested_albums is for. NULL = the last run had no failures.
    ("playlists", "lidarr_request_last_error", "TEXT"),
    ("playlists", "lidarr_request_last_error_code", "TEXT"),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    for table, column, coldef in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
    _post_migration_indexes(conn)
    _backfill_release_date(conn)


def _post_migration_indexes(conn: sqlite3.Connection) -> None:
    """Indexes on columns that only exist after the loop above has run —
    they can't live in SCHEMA, since a fresh DB creates its tables before
    any ALTER TABLE adds these columns.

    #239: tracks.fingerprint stops being write-only with the device
    provenance work — provenance.py fills it and the recovery rematch looks
    tracks up BY it (a client pushes a fingerprint, the server finds which
    track it identifies). Fingerprints are ~2.2 KB of text each, so an
    unindexed equality scan over a real library is expensive."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_fingerprint ON tracks(fingerprint) "
        "WHERE fingerprint IS NOT NULL"
    )
    # #439: GET /api/device/fingerprints filters+orders on this for its
    # incremental ?computed_after= cursor, on a device_track_state JOIN that
    # can already span thousands of rows on a real library.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_fingerprint_seq ON tracks(fingerprint_seq) "
        "WHERE fingerprint_seq IS NOT NULL"
    )


def _backfill_release_date(conn: sqlite3.Connection) -> None:
    """Lossy but immediate: every row scanned before release_date existed
    only has a bare year, so approximate Jan 1st of that year rather than
    leaving "recently released" blind to the entire existing library until
    a full rescan. Naturally a no-op after the first run (nothing left
    matching the WHERE once backfilled) and after scanner.py starts
    populating release_date directly on every scan going forward."""
    conn.execute(
        "UPDATE tracks SET release_date = printf('%04d-01-01', year) "
        "WHERE release_date IS NULL AND year IS NOT NULL"
    )


def _dedupe_selections(conn: sqlite3.Connection) -> None:
    """One-time cleanup: POST /api/selections had no find-or-create
    guard, so a double-click/retried request could create multiple
    `selections` rows for the same (type, target, created_by_user_id) —
    which the Selections matrix UI's `x-for :key="row.target"` can't render
    distinctly, since Alpine collapses/misbehaves on a duplicate key.
    create_selection() is find-or-create now, so this can't recur going
    forward; this only cleans up rows created before that fix. Merges each
    duplicate group into its oldest row, unioning device links so no
    device's sync scope narrows. 'autofit' is excluded: its rows are
    legitimately one-per-device (see create_autofit_selection), not unique
    per (type, target, user) — the same period can back several devices."""
    groups = conn.execute(
        "SELECT GROUP_CONCAT(id) AS ids FROM selections WHERE type != 'autofit' "
        "GROUP BY type, target, created_by_user_id HAVING COUNT(*) > 1"
    ).fetchall()
    for group in groups:
        ids = sorted(int(i) for i in group["ids"].split(","))
        keep_id, dupe_ids = ids[0], ids[1:]
        for dupe_id in dupe_ids:
            for row in conn.execute(
                "SELECT device_id FROM selection_devices WHERE selection_id = ?", (dupe_id,)
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO selection_devices (selection_id, device_id) VALUES (?, ?)",
                    (keep_id, row["device_id"]),
                )
            conn.execute("DELETE FROM selections WHERE id = ?", (dupe_id,))
    # Partial index (excludes autofit, see above) — belt-and-suspenders once
    # the dedup above has run: stops any future duplicate at the DB level
    # even if a caller other than create_selection() ever bypasses it.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_selections_unique_target "
        "ON selections(type, target, created_by_user_id) WHERE type != 'autofit'"
    )


def _migrate_api_tokens_table(conn: sqlite3.Connection) -> None:
    """api_tokens -> integration_tokens (#474's second revision -- see the
    schema comment on integration_tokens for why the two-table split was
    dropped). #446 already shipped api_tokens to real installs, so this is
    a real upgrade path, not a rename for a table that never existed
    outside this branch: same rename-if-old-name-present pattern as
    _migrate_playlist_tables below, and must run before it for the same
    reason -- CREATE TABLE IF NOT EXISTS in SCHEMA would otherwise create
    an empty integration_tokens first and the rename would fail with
    "table already exists". action_tokens, by contrast, never shipped in a
    release (added and removed within this same unmerged #474 branch), so
    there is nothing to migrate away from for it -- SCHEMA simply no
    longer declares it.

    A caught-in-review bug, worth stating so it isn't reintroduced: a bare
    rename is not enough. v2.8.0/2.8.1's api_api_tokens route had NO admin
    gate -- any logged-in user could mint one, under an explicit read-only
    promise (this file's own comment, the UI copy, and the docs all said
    so). integration_tokens' safety property is "an admin minted it"; a
    row that predates the gate cannot be grandfathered into action
    capability just because ALTER TABLE moved it into the table that now
    carries that promise. So: rename, then revoke every row whose owner
    isn't an admin -- silently upgrading a read-only credential's power
    on someone's behalf is worse than making them re-mint it."""
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "api_tokens" in tables and "integration_tokens" not in tables:
        conn.execute("ALTER TABLE api_tokens RENAME TO integration_tokens")
        conn.execute(
            "DELETE FROM integration_tokens WHERE owner_user_id NOT IN "
            "(SELECT id FROM users WHERE is_admin = 1)"
        )


def _migrate_playlist_tables(conn: sqlite3.Connection) -> None:
    """roon_playlists/roon_playlist_tracks -> playlists/playlist_tracks,
    roon_title -> title. Must run before the SCHEMA executescript below —
    otherwise CREATE TABLE IF NOT EXISTS would create an empty `playlists`
    table first and this rename would then fail with "table already
    exists". A fresh install never has the old names, so this is a no-op
    for it (guarded/idempotent like every other migration here)."""
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "roon_playlists" in tables and "playlists" not in tables:
        conn.execute("ALTER TABLE roon_playlists RENAME TO playlists")
        conn.execute("ALTER TABLE playlists RENAME COLUMN roon_title TO title")
    if "roon_playlist_tracks" in tables and "playlist_tracks" not in tables:
        conn.execute("ALTER TABLE roon_playlist_tracks RENAME TO playlist_tracks")


def _migrate_playlists_composite_key(conn: sqlite3.Connection) -> None:
    """#75: replace the old global `title UNIQUE` on playlists with a
    composite key — (source_provider, source_playlist_id) where a provider
    exposes a stable id, else (source_provider, title) for Roon — so two
    same-titled playlists (across providers, or from one id-exposing
    provider) coexist as separate rows instead of silently collapsing into
    one.

    SQLite can't drop a column-level UNIQUE via ALTER, so for an existing
    DB this rebuilds the table (the canonical CREATE-new / copy / DROP /
    RENAME procedure). id values are preserved deliberately —
    playlist_tracks.playlist_id (FK) and selections.target (playlist-type,
    id-as-string) both reference playlists by id, so a rebuild that
    renumbered them would orphan every synced-playlist selection and every
    playlist's tracks. Idempotent: keyed off whether the table's own
    CREATE sql still carries a UNIQUE constraint (only the old shape does;
    a fresh DB's base SCHEMA and a rebuilt table both omit it), so it runs
    at most once. Must run AFTER _run_migrations so every column the new
    table lists already exists on the old one to be copied across."""
    tbl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='playlists'"
    ).fetchone()
    if tbl is not None and "UNIQUE" in (tbl["sql"] or "").upper():
        # Flush any transaction left open by an earlier migration step
        # (e.g. _backfill_release_date's UPDATE) — PRAGMA foreign_keys is a
        # no-op inside a transaction, and switching isolation_level below
        # only behaves predictably from a clean state.
        conn.commit()
        # FK OFF during the swap so DROP TABLE playlists doesn't cascade-
        # delete playlist_tracks; the pragma is a no-op inside a
        # transaction, so drive transactions explicitly via autocommit.
        prev_isolation = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            conn.execute(
                "CREATE TABLE playlists_new ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " title TEXT NOT NULL,"
                " source_provider TEXT,"
                " source_playlist_id TEXT,"
                " owner_user_id INTEGER REFERENCES users(id),"
                " shared INTEGER NOT NULL DEFAULT 1,"
                " inferred_origin_provider TEXT,"
                " golden_source_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,"
                " last_synced_at TEXT)"
            )
            # golden_source_id (#81) is copied too: on a pre-#75 -> current
            # upgrade _run_migrations added it (all NULL) before this rebuild
            # runs, so it exists on the old table; omitting it here would
            # drop the column entirely and break every query that reads it.
            conn.execute(
                "INSERT INTO playlists_new "
                "(id, title, source_provider, source_playlist_id, owner_user_id, "
                " shared, inferred_origin_provider, golden_source_id, last_synced_at) "
                "SELECT id, title, source_provider, source_playlist_id, owner_user_id, "
                " shared, inferred_origin_provider, golden_source_id, last_synced_at FROM playlists"
            )
            conn.execute("DROP TABLE playlists")
            conn.execute("ALTER TABLE playlists_new RENAME TO playlists")
            conn.execute("COMMIT")
            # No foreign_key_check here on purpose: the INSERT...SELECT above
            # copies every playlists row with its id unchanged, so no
            # previously-valid playlist_tracks.playlist_id can be orphaned by
            # the rebuild — the id preservation is the correctness guarantee.
            # A global PRAGMA foreign_key_check would instead flag any
            # *pre-existing* orphan anywhere in the DB (real DBs accumulate
            # some — e.g. a stray selection_devices row), unrelated to this
            # migration, and aborting on those would brick startup on upgrade.
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.isolation_level = prev_isolation
    # Idempotent — for fresh DBs (no rebuild) and rebuilt ones alike. Two
    # partial indexes so each row is covered by exactly one: id-keyed rows
    # by the first, title-keyed (Roon) rows by the second.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_provider_srcid "
        "ON playlists(source_provider, source_playlist_id) WHERE source_playlist_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_provider_title "
        "ON playlists(source_provider, title) WHERE source_playlist_id IS NULL"
    )
    conn.commit()


def _migrate_tracks_strict(conn: sqlite3.Connection) -> None:
    """#298: convert `tracks` to a STRICT table.

    Why this table first: fingerprint, isrc and acoustid_isrc are compared with
    `=` as *identity* by #239's recovery rematch. TEXT affinity accepts Python
    `bytes` and persists a BLOB, which then never matches anything — a wrong
    answer, not an error. The trap was hit twice already (#292, #296) and both
    fixes were per-call-site discipline that nothing enforces.

    SQLite can't ALTER a table into STRICT, so an existing DB needs the
    canonical CREATE-new / copy / DROP / RENAME procedure. Like
    _migrate_playlists_composite_key, this **must run AFTER _run_migrations**:
    nine of the 22 columns below arrive via _MIGRATIONS, and a rebuild that
    listed only the base SCHEMA's 13 would silently drop fingerprint — the very
    column this is for. Idempotent, keyed off whether the table's own CREATE sql
    already says STRICT (true for a fresh DB and for an already-rebuilt one).

    id values are preserved: device_track_state.track_id, playlist_tracks
    .track_id and playlist_tracks.matched_track_id all reference tracks by id,
    so renumbering would orphan every device's download state.

    TEXT columns are copied through CAST(... AS TEXT). A BLOB holding ASCII —
    which is exactly what #292/#296 wrote — casts back to the string that should
    have been stored, so the migration repairs that damage instead of refusing
    to boot on it. INTEGER/REAL columns are copied uncast on purpose: CAST would
    turn a surprising value into a plausible-looking 0, and a mismatch there
    means something we don't understand, which should fail loudly.

    Measured on a real 58,783-track database: 0.17s to copy, 0.03s to reindex.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tracks'"
    ).fetchone()
    if row is None or "STRICT" in (row["sql"] or "").upper():
        return

    # (name, declared type) — the full post-migration shape. Explicit rather
    # than derived from PRAGMA table_info so the rebuilt table's shape is
    # visible in review instead of depending on what a given DB happens to hold.
    columns = [
        ("id", "INTEGER"), ("relative_path", "TEXT"), ("artist", "TEXT"),
        ("album", "TEXT"), ("title", "TEXT"), ("track_no", "INTEGER"),
        ("disc_no", "INTEGER"), ("year", "INTEGER"), ("reissue_year", "INTEGER"),
        ("size", "INTEGER"), ("mtime", "REAL"), ("scanned_at", "TEXT"),
        ("deleted_at", "TEXT"), ("duration", "REAL"), ("release_date", "TEXT"),
        ("isrc", "TEXT"), ("fingerprint", "TEXT"), ("acoustid_isrc", "TEXT"),
        ("acoustid_mbid", "TEXT"), ("fingerprint_checked_at", "TEXT"),
        ("fingerprint_failed_at", "TEXT"), ("fingerprint_seq", "INTEGER"),
    ]

    # Report what the CAST is about to repair, before it disappears. Counted per
    # column so the log names the actual damage rather than a bare total.
    for name, decl in columns:
        if decl != "TEXT":
            continue
        bad = conn.execute(
            f'SELECT COUNT(*) FROM tracks WHERE "{name}" IS NOT NULL '
            f'AND typeof("{name}") != \'text\''
        ).fetchone()[0]
        if bad:
            _log.warning(
                "#298 migration: coercing %d non-text value(s) in tracks.%s to TEXT "
                "(the bytes-stored-as-BLOB trap — these values could never match "
                "an equality comparison)", bad, name)

    names = ", ".join(name for name, _ in columns)
    select = ", ".join(
        f'CAST("{name}" AS TEXT)' if decl == "TEXT" else f'"{name}"'
        for name, decl in columns)
    ddl = ", ".join(
        "id INTEGER PRIMARY KEY AUTOINCREMENT" if name == "id"
        # relative_path keeps its UNIQUE: scanner.py upserts on it.
        else "relative_path TEXT UNIQUE NOT NULL" if name == "relative_path"
        else f"{name} {decl} NOT NULL DEFAULT (datetime('now'))" if name == "scanned_at"
        else f"{name} {decl} NOT NULL"
        if name in ("artist", "album", "title", "size", "mtime")
        else f"{name} {decl}"
        for name, decl in columns)

    # Flush anything an earlier migration step left open: PRAGMA foreign_keys is
    # a no-op inside a transaction, so the swap has to be driven from autocommit.
    conn.commit()
    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        # FK OFF so DROP TABLE tracks doesn't cascade-delete device_track_state
        # and playlist_tracks.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute(f"CREATE TABLE tracks_new ({ddl}) STRICT")
        conn.execute(f"INSERT INTO tracks_new ({names}) SELECT {select} FROM tracks")
        conn.execute("DROP TABLE tracks")
        conn.execute("ALTER TABLE tracks_new RENAME TO tracks")
        conn.execute("COMMIT")
        # No PRAGMA foreign_key_check, same reasoning as the playlists rebuild:
        # ids are copied unchanged so this migration can't orphan anything, while
        # a global check would flag pre-existing orphans real databases
        # accumulate and abort startup over something unrelated.
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = prev_isolation

    # DROP TABLE took both indexes with it. idx_tracks_artist_album lives in
    # SCHEMA (already executed by now) and idx_tracks_fingerprint in
    # _post_migration_indexes (likewise), so neither would come back on its own.
    # Losing the fingerprint one wouldn't error — it would quietly turn #239's
    # rematch into a full table scan per pushed entry.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_artist_album ON tracks(artist, album)")
    _post_migration_indexes(conn)
    conn.commit()


def _migrate_unresolved_playlist_tracks_strict(conn: sqlite3.Connection) -> None:
    """#298: convert `unresolved_playlist_tracks` to a STRICT table.

    Named in the issue alongside jobs/device_provenance as one of the "new
    tables STRICT from now on" set, but it already existed by then (unlike
    those two), so an existing DB's copy still needs the same
    create-new/copy/drop/rename rebuild as _migrate_tracks_strict — SQLite
    can't ALTER a table into STRICT. No column here has ever needed an
    _MIGRATIONS entry, so unlike that function this one has nothing to add
    beyond SCHEMA's own 9 columns.

    playlist_id's own FK (REFERENCES playlists(id) ON DELETE CASCADE) is
    preserved verbatim in the rebuilt table. Nothing else has a FK *into*
    this table, so — unlike tracks — dropping it can't cascade-delete rows
    elsewhere; foreign_keys is still turned off for the swap purely for
    consistency with that precedent, not because this table needs it.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='unresolved_playlist_tracks'"
    ).fetchone()
    if row is None or "STRICT" in (row["sql"] or "").upper():
        return

    columns = [
        ("id", "INTEGER"), ("playlist_id", "INTEGER"), ("position", "INTEGER"),
        ("artist", "TEXT"), ("title", "TEXT"), ("album", "TEXT"),
        ("isrc", "TEXT"), ("excluded", "INTEGER"), ("first_seen_at", "TEXT"),
    ]

    for name, decl in columns:
        if decl != "TEXT":
            continue
        bad = conn.execute(
            f'SELECT COUNT(*) FROM unresolved_playlist_tracks WHERE "{name}" IS NOT NULL '
            f'AND typeof("{name}") != \'text\''
        ).fetchone()[0]
        if bad:
            _log.warning(
                "#298 migration: coercing %d non-text value(s) in "
                "unresolved_playlist_tracks.%s to TEXT (the bytes-stored-as-BLOB "
                "trap — these values could never match an equality comparison)",
                bad, name)

    names = ", ".join(name for name, _ in columns)
    select = ", ".join(
        f'CAST("{name}" AS TEXT)' if decl == "TEXT" else f'"{name}"'
        for name, decl in columns)
    ddl = ", ".join(
        "id INTEGER PRIMARY KEY AUTOINCREMENT" if name == "id"
        else "playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE"
        if name == "playlist_id"
        else f"{name} {decl} NOT NULL DEFAULT (datetime('now'))" if name == "first_seen_at"
        else f"{name} {decl} NOT NULL DEFAULT 0" if name == "excluded"
        else f"{name} {decl} NOT NULL" if name == "position"
        else f"{name} {decl}"
        for name, decl in columns)

    conn.commit()
    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute(f"CREATE TABLE unresolved_playlist_tracks_new ({ddl}) STRICT")
        conn.execute(
            f"INSERT INTO unresolved_playlist_tracks_new ({names}) "
            f"SELECT {select} FROM unresolved_playlist_tracks")
        conn.execute("DROP TABLE unresolved_playlist_tracks")
        conn.execute(
            "ALTER TABLE unresolved_playlist_tracks_new RENAME TO unresolved_playlist_tracks")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.isolation_level = prev_isolation

    # DROP TABLE took both indexes with it; neither creator runs again on a
    # DB past its first init. Losing idx_unresolved_playlist_tracks_identity
    # wouldn't error — sync_state._record_unresolved_playlist_tracks would
    # just start inserting duplicate rows on every resync instead of
    # preserving `excluded` across one.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_unresolved_playlist_tracks_playlist "
        "ON unresolved_playlist_tracks(playlist_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_unresolved_playlist_tracks_identity "
        "ON unresolved_playlist_tracks(playlist_id, artist, title, album)")
    conn.commit()


def init_db() -> None:
    conn = get_conn()
    try:
        _migrate_api_tokens_table(conn)
        _migrate_playlist_tables(conn)
        conn.executescript(SCHEMA)
        _run_migrations(conn)
        # After _run_migrations: the rebuild lists columns that _MIGRATIONS adds.
        _migrate_tracks_strict(conn)
        _migrate_unresolved_playlist_tracks_strict(conn)
        _migrate_playlists_composite_key(conn)
        _dedupe_selections(conn)
        # device_type taxonomy split (phone/tablet/watch/dap/sdcard)
        # replaced the old 'android' catch-all — reclassify existing rows so
        # they get the right icon instead of silently falling back to phone.
        conn.execute("UPDATE devices SET device_type = 'phone' WHERE device_type = 'android'")
        # #501 (caught in review): basket_items gained a required device
        # dimension via the new basket_item_devices join table, but every
        # basket_items row that predates this feature has zero links in
        # it -- it never had one to have. Left alone, such a row becomes a
        # permanent ghost: no per-device section to render under, and the
        # device-scoped fan-out filters it out of what it considers
        # sendable, so it can never be sent OR cleared by any normal path.
        # A basket is a transient staging list, not data worth preserving
        # across this shape change -- delete them. Naturally idempotent
        # (nothing left to match after the first run), unlike a seed-once
        # default, so no written-once guard is needed here.
        conn.execute(
            "DELETE FROM basket_items WHERE id NOT IN "
            "(SELECT DISTINCT basket_item_id FROM basket_item_devices)"
        )
        _seed_spotify_experimental_default(conn)
        conn.commit()
    finally:
        conn.close()


def get_config(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def _seed_spotify_experimental_default(conn: sqlite3.Connection) -> None:
    """#398: the Spotify experimental-features toggle defaults to OFF for a
    fresh install, but ON for an existing one that already has Spotify
    credentials configured — upgrading must never silently disable a
    working integration and disconnect people mid-sync. Decided ONCE, here,
    at migration time, not read live on every request: an admin who
    deliberately turns this off later must have that decision hold, not get
    silently re-enabled on the next startup just because the credentials
    are still present. Idempotent — only ever writes when the key has
    never been set at all, exactly like every other one-time seed in this
    file (compare _backfill_release_date)."""
    if get_config(conn, "experimental_spotify_enabled") is not None:
        return
    has_credentials = bool(get_config(conn, "spotify_client_id")) and bool(
        get_config(conn, "spotify_client_secret"))
    set_config(conn, "experimental_spotify_enabled", "1" if has_credentials else "0")


def set_config(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_music_root() -> Path:
    """MUSIC_ROOT, DB-editable via the setup wizard — app_config's
    'music_root' key wins once the wizard (or an admin) has set it, the env
    var only seeds the initial default before that. Re-read fresh every call
    (one indexed lookup, own short-lived connection) rather than cached at
    import time, so a change made in the wizard takes effect immediately —
    no restart needed, unlike the env var itself."""
    conn = get_conn()
    try:
        override = get_config(conn, "music_root")
    finally:
        conn.close()
    return Path(override) if override else Path(os.environ.get("MUSIC_ROOT", "/music"))


def get_mirror_folder() -> Path | None:
    """#285: same override-over-env-var precedence as get_music_root()
    above, except there's no hardcoded fallback path — mirroring is
    opt-in, so unset (both app_config's 'mirror_folder' and the
    MIRROR_ROOT env var) means "not configured" (None), not a guessed
    default. mirror.py treats None as a clean no-op."""
    conn = get_conn()
    try:
        override = get_config(conn, "mirror_folder")
    finally:
        conn.close()
    path_str = override or os.environ.get("MIRROR_ROOT")
    return Path(path_str) if path_str else None


def get_mirror_subsonic_config() -> tuple[str, str, str] | None:
    """#189: the Subsonic/Navidrome mirror-TARGET connection — a distinct
    write destination from the active-provider subsonic_url/username/
    password triple (that one is "the current read-source, never more
    than one active at a time"; see main.py's /api/admin/config
    docstring), even though in practice it's often pointed at the same
    server. Own key namespace, same "unconfigured means opt out cleanly"
    contract as get_mirror_folder() above: any of the three missing
    means not configured, returns None rather than a partial tuple."""
    conn = get_conn()
    try:
        url = get_config(conn, "mirror_subsonic_url")
        username = get_config(conn, "mirror_subsonic_username")
        password = get_config(conn, "mirror_subsonic_password")
    finally:
        conn.close()
    if not url or not username or not password:
        return None
    return url, username, password


def get_mirror_jellyfin_config() -> tuple[str, str, str] | None:
    """#189: the Jellyfin mirror-TARGET connection — a distinct write
    destination from the active-provider jellyfin_url/api_key/username/
    user_id set (see main.py's /api/admin/config docstring), even though
    in practice it's often pointed at the same server. Returns (url,
    api_key, user_id) — parity with jellyfin_client._current_config()'s
    own return shape (username itself is stored for display/re-resolution
    only, never needed for a request once user_id is resolved). Same
    "unconfigured means opt out cleanly" contract as
    get_mirror_subsonic_config() above: url/api_key/username all missing,
    OR present but the stored user_id is blank (username never resolved,
    or resolved to nothing), means not configured — returns None rather
    than a partial tuple."""
    conn = get_conn()
    try:
        url = get_config(conn, "mirror_jellyfin_url")
        api_key = get_config(conn, "mirror_jellyfin_api_key")
        username = get_config(conn, "mirror_jellyfin_username")
        user_id = get_config(conn, "mirror_jellyfin_user_id")
    finally:
        conn.close()
    if not url or not api_key or not username or not user_id:
        return None
    return url, api_key, user_id


def get_mirror_emby_config() -> tuple[str, str, str] | None:
    """#189: the Emby mirror-TARGET connection — same shape and same
    "unconfigured means opt out cleanly" contract as
    get_mirror_jellyfin_config() above, for the fourth sink. A distinct
    write destination from the active-provider emby_url/api_key/username/
    user_id set, even when both point at the same server."""
    conn = get_conn()
    try:
        url = get_config(conn, "mirror_emby_url")
        api_key = get_config(conn, "mirror_emby_api_key")
        username = get_config(conn, "mirror_emby_username")
        user_id = get_config(conn, "mirror_emby_user_id")
    finally:
        conn.close()
    if not url or not api_key or not username or not user_id:
        return None
    return url, api_key, user_id


def get_lidarr_connection() -> tuple[str, str] | None:
    """#494: just url + api_key — this pair is what
    lidarr_client.status()/the admin dropdown-options route need, and it's
    resolvable BEFORE the three profile fields below are ever chosen (the
    admin can't pick them until this pair is live and Lidarr's own lists
    have been fetched — see main.py's two-phase save)."""
    conn = get_conn()
    try:
        url = get_config(conn, "lidarr_url")
        api_key = get_config(conn, "lidarr_api_key")
    finally:
        conn.close()
    if not url or not api_key:
        return None
    return url, api_key


def get_lidarr_config() -> tuple[str, str, str, int, int] | None:
    """#494: the full connection needed to actually request an album —
    url, api_key, root_folder_path, quality_profile_id, metadata_profile_id.
    Unlike every mirror-target config above (three plain strings, all
    admin-typed), the last two of these five have no safe default: they're
    Lidarr-instance-specific ids with no meaningful value Trobar could
    guess, so they can only be *chosen* from GET /api/admin/lidarr-options'
    live lists, never defaulted. Same "any missing means not configured,
    return None rather than a partial tuple" contract as the mirror
    targets."""
    conn = get_conn()
    try:
        url = get_config(conn, "lidarr_url")
        api_key = get_config(conn, "lidarr_api_key")
        root_folder_path = get_config(conn, "lidarr_root_folder_path")
        quality_profile_id = get_config(conn, "lidarr_quality_profile_id")
        metadata_profile_id = get_config(conn, "lidarr_metadata_profile_id")
    finally:
        conn.close()
    if not url or not api_key or not root_folder_path \
            or not quality_profile_id or not metadata_profile_id:
        return None
    try:
        return (url, api_key, root_folder_path,
                int(quality_profile_id), int(metadata_profile_id))
    except ValueError:
        return None
