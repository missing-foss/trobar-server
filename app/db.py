#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite schema and connection helper for Trobar."""

import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

_log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
# Historical filename from before the Trobar rename — kept so existing
# deployments' databases keep opening; renaming it would orphan them all.
DB_PATH = DATA_DIR / "music-sync.db"

# Migration 1's schema, frozen: see the note above _MIGRATIONS.
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
-- Upsert key for unresolved_store.record_unresolved_playlist_tracks: a playlist
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

-- One row per public playlist URL a user has subscribed to. Deliberately
-- NOT a provider connection: there is nothing to authenticate, so there is
-- no credential column here and no entry in app_config -- a subscription IS
-- the whole configuration. Per-user rather than instance-wide, matching how
-- a linked Tidal/Spotify account is scoped, so the playlists it produces
-- get owner_user_id set and start unshared.
--
-- external_id is the playlist id parsed out of the URL and is what is
-- actually fetched; `url` is kept verbatim purely so the UI can show the
-- user what they pasted. The unique index is on the id rather than the URL,
-- because the same playlist has several URL spellings (music. vs www., a
-- /watch?v=..&list=.. address-bar copy) and re-pasting one of them must
-- update the existing subscription rather than create a rival second one
-- pointing at the same playlist.
--
-- last_error is the sticky "this stopped working" state, not a log: a fetch
-- that fails leaves the already-synced playlist rows alone (playlist_sync's
-- prune protection) and records why here, so the failure is visible in the
-- UI instead of showing up as a playlist that mysteriously stopped
-- changing. It is cleared on the next successful fetch.
--
-- last_track_count/last_matched_count exist for one specific bad outcome:
-- a playlist of hour-long mix videos parses fine and matches nothing, and
-- without the pair of numbers the result is indistinguishable from a
-- successful import of a playlist you happen to own none of.
--
-- STRICT (#298): a new table, so this costs nothing.
CREATE TABLE IF NOT EXISTS playlist_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    last_synced_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    last_track_count INTEGER,
    last_matched_count INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_subscriptions_identity
    ON playlist_subscriptions(owner_user_id, provider, external_id);

-- #494: one row per (artist, album) Lidarr has ever been asked about, on
-- behalf of ANY playlist's "Request missing albums" toggle -- deliberately
-- NOT scoped per-playlist (no playlist_id here at all) and NOT stored on
-- unresolved_playlist_tracks above: that table is fully DELETE+recreated on
-- every single playlist sync (see unresolved_store.record_unresolved_playlist_tracks),
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

-- One row per MusicBrainz album lookup for a playlist gap whose source gave
-- no album (album_lookup.py), keyed by the gap's ISRC ("isrc:...") or its
-- normalized artist and title ("at:..."). album is NULL when MusicBrainz
-- answered with no studio album. Every answer is kept and never asked
-- again: the same gap reappears on every sync, and in several playlists.
-- A failed request is not an answer and leaves no row.
CREATE TABLE IF NOT EXISTS musicbrainz_album_lookups (
    lookup_key TEXT PRIMARY KEY,
    album TEXT,
    looked_up_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

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
-- into a real `selections` row via selection_store.create_selection, exactly
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
-- basket_store.unstage_basket_item_device deletes the basket_items row too
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
-- Sessions ended by logging out. A session cookie is re-signed on every
-- request (its lifetime slides), so a request still in flight when the user
-- logs out comes back with a fresh copy of the signed-in cookie; recording
-- the session's id as ended is what keeps that copy from signing them back
-- in. Rows older than the session lifetime are pruned, as no cookie that
-- old can be read anyway.
CREATE TABLE IF NOT EXISTS ended_sessions (
    sid TEXT PRIMARY KEY,
    ended_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

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
    -- queued -> running -> done | failed | cancelled. A failed job keeps
    -- last_error and can be requeued (by the admin, or by the retry logic in
    -- jobs.py); a cancelled one, stopped by an admin, is never retried.
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
-- comparison (see device_store.record_device_manifest), and device_path() is
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
-- One row per mirror write attempt, per playlist and server sink (the
-- filesystem mirror has no target, so no rows): how many of the tracks
-- Trobar sent ended up on the target, and which did not. Only the last
-- mirror_runs.KEEP rows per (playlist, sink) are kept. See mirror_runs.py.
CREATE TABLE IF NOT EXISTS mirror_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    sink TEXT NOT NULL,
    run_at TEXT NOT NULL DEFAULT (datetime('now')),
    total INTEGER NOT NULL,
    on_target INTEGER NOT NULL,
    lacked INTEGER NOT NULL,
    refused INTEGER NOT NULL,
    -- JSON list of {"artist", "title", "reason"}, at most mirror_runs.LIST_MAX.
    missing TEXT NOT NULL DEFAULT '[]'
) STRICT;
CREATE INDEX IF NOT EXISTS idx_mirror_runs_pair
    ON mirror_runs(playlist_id, sink, id);
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


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """A get_conn() connection for one `with` block: rolled back if the block
    raises, and closed either way. Only explicit commits persist, as with
    the try/finally pairs this replaces (closing an uncommitted transaction
    also discards it)."""
    conn = get_conn()
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """One short write transaction: committed when the block ends, rolled back
    if it raises. New code writes through this.

    Takes the write lock on entry, so keep slow work out of the block: every
    other writer waits for as long as it runs. Read the file or call the
    server first, then write what came back.

    With `conn`, uses that connection and leaves it open. Without, opens one
    and closes it. A transaction already open on `conn` is an error, because
    this would commit someone else's half-done writes."""
    own = conn is None
    active = get_conn() if conn is None else conn
    try:
        if active.in_transaction:
            raise RuntimeError("db.write() inside an open transaction")
        active.execute("BEGIN IMMEDIATE")
        try:
            yield active
        except BaseException:
            active.rollback()
            raise
        active.commit()
    finally:
        if own:
            active.close()


def _new_id(cur: sqlite3.Cursor) -> int:
    """cur.lastrowid is Optional per sqlite3's stub (None before any INSERT
    has ever run on the cursor), but always set right after one — narrow it
    once here instead of asserting at each call site."""
    assert cur.lastrowid is not None
    return cur.lastrowid


# Columns added after the initial schema — CREATE TABLE IF NOT EXISTS doesn't
# retrofit them onto an already-existing table (the prod DB had 58k rows
# scanned before these were added), so add them explicitly, idempotently.
#
# Frozen, like SCHEMA: both run only inside migration 1, which a database
# past version 1 never runs again. A new column or table goes in a new
# numbered migration (MIGRATIONS below), or existing databases never get it.
# test_db_migrations checks an upgraded database against a new one.
_MIGRATIONS = [
    ("users", "lastfm_username", "TEXT"),
    ("users", "lastfm_api_key", "TEXT"),
    ("users", "listenbrainz_username", "TEXT"),
    # A third listening-history source: the URL of the user's own Maloja
    # (one instance is one person's history, so per user, not app-wide).
    ("users", "maloja_url", "TEXT"),
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
    # Bytes of music already in the sync folder that Trobar did not put
    # there. "Max music size" is the total a user allows in that folder, so
    # this has to come off the budget -- without it the real footprint can
    # exceed the configured limit from the first sync, which is what a fresh
    # install pointed at an existing folder does.
    #
    # NULL is "never reported", NOT zero, and the two must stay
    # distinguishable: every device enrolled before the client learned to
    # report this has NULL, and reading that as "no foreign content" would
    # silently claim a measurement nobody made. The timestamp is what says
    # which of the two a NULL is, and how stale a number is.
    ("devices", "reported_foreign_bytes", "INTEGER"),
    ("devices", "foreign_bytes_reported_at", "TEXT"),
    # The space the device keeps free on its volume, as it reports it with
    # its free/total figures. NULL: a client that doesn't keep one, or
    # hasn't said; not zero.
    ("devices", "reported_reserve_bytes", "INTEGER"),
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
    # One of main.DEVICE_ICONS, a picture only: NULL = the device type's own
    # icon. device_type keeps driving behaviour (enrollment, transcoding);
    # this never does. No backfill: NULL is how every existing device looks.
    ("devices", "icon", "TEXT"),
    # JSON {"disabled": [...widget ids...], "settings": {widget_id: {...}}}.
    # NULL/unset = nothing disabled (every existing user keeps today's
    # "four fixed cards" behaviour with no backfill needed) — see
    # _profile_dict's dashboard_widgets handling in main.py.
    ("users", "dashboard_widgets", "TEXT"),
    # The same JSON shape, a paired device's own Home widget set -- the
    # app's, apart from the browser's. NULL = never read or saved: the first
    # read copies the account's browser set here (main.py,
    # _device_dashboard_widgets). Deleted with the device.
    ("devices", "dashboard_widgets", "TEXT"),
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
    # Tidal's own account id, from GET /v2/users/me at link time. Sharing
    # compares it with a playlist's owners; the playlist listing doesn't need
    # it, since it addresses the account as `me`.
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
    # playlists.id. Set purely by _infer_playlist_origins at sync
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
    # Last among the tracks columns, so a fresh database and an upgraded one
    # (where ALTER TABLE appends) have the same column order.
    #
    # The file's own MusicBrainz recording id (the song, the same on every
    # release that carries it), read from its tags at scan time: FLAC/Vorbis
    # MUSICBRAINZ_TRACKID, MP4 "MusicBrainz Track Id", ID3 UFID. NULL when
    # the file has none; identity.py's recording-id tier matches a source's
    # recording id against it, then against acoustid_mbid. Files scanned
    # before this column existed are filled by mbid_backfill.py's one-time
    # pass, since an unchanged file is never re-read by the scanner.
    ("tracks", "mbid", "TEXT"),
    # matching.normalize() of artist and title, stored so the artist/title
    # matcher's candidate query is an index lookup, not a Python call per
    # row per lookup. Written by the scanner with the tags, and filled for
    # existing rows at startup by _refresh_norms, which also recomputes them
    # all when normalize() changes (matching.NORM_VERSION).
    ("tracks", "artist_norm", "TEXT"),
    ("tracks", "title_norm", "TEXT"),
    # The album artist (album_artist.py): the file's albumartist tag as read,
    # and what the library groups the album under, derived from the whole
    # album. The second is filled for existing rows at startup.
    ("tracks", "album_artist_tag", "TEXT"),
    ("tracks", "album_artist", "TEXT"),
    # Whether the scan has read this file's albumartist tag yet: NULL in
    # album_artist_tag means "no tag" only once it's 1. A library from before
    # these columns has every row at 0, and its next scan reads the tag of
    # the files it would otherwise skip as unchanged.
    ("tracks", "album_artist_read", "INTEGER NOT NULL DEFAULT 0"),
    # Whether the file is lossless audio (scanner._lossless): 1 for FLAC, WAV,
    # AIFF and ALAC in .m4a, 0 for lossy, NULL not yet known (a library from
    # before this, until its next scan). sync_state transcodes only lossless
    # files; while NULL it decides by the extension, as it did before.
    ("tracks", "lossless", "INTEGER"),
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
    # Re-pairing: which existing device this enrollment code re-attaches to.
    # NULL is the original behaviour and the common case -- redeeming mints a
    # brand-new device. Non-NULL means "this code hands the caller back the
    # device it names", so an app reinstalled on the same phone recovers its
    # selections, track state and settings instead of orphaning them.
    #
    # Nullable and additive on purpose: every existing row and every code minted
    # by the ordinary "Add device" flow keeps meaning exactly what it meant.
    # ON DELETE CASCADE so deleting a device also drops any outstanding code
    # pointing at it, which would otherwise be a grant that can never be
    # satisfied.
    ("enrollment_grants", "device_id",
     "INTEGER REFERENCES devices(id) ON DELETE CASCADE"),
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
    # A fifth sink after all: Music Assistant. Columns again, the same way
    # as the four above, rather than the child table the comment above
    # keeps deferring; that refactor is still its own change.
    # The account each Jellyfin/Emby copy was written into, so a copy that
    # belongs in another account (the mirror account changed, or a member's
    # mapping did) is moved rather than written into the wrong one. NULL for
    # a copy from before this was tracked: the configured mirror account.
    ("playlists", "jellyfin_mirror_owner_id", "TEXT"),
    ("playlists", "emby_mirror_owner_id", "TEXT"),
    ("playlists", "music_assistant_mirror_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("playlists", "music_assistant_mirror_remote_id", "TEXT"),
    ("playlists", "music_assistant_mirror_last_written_at", "TEXT"),
    ("playlists", "music_assistant_mirror_last_error", "TEXT"),
    ("playlists", "music_assistant_mirror_last_error_code", "TEXT"),
    # A sixth: Plex. Columns once more, the same way; the child-table
    # refactor above is still its own change.
    ("playlists", "plex_mirror_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("playlists", "plex_mirror_remote_id", "TEXT"),
    ("playlists", "plex_mirror_last_written_at", "TEXT"),
    ("playlists", "plex_mirror_last_error", "TEXT"),
    ("playlists", "plex_mirror_last_error_code", "TEXT"),
    # The machineIdentifier of the server holding the copy: Plex keys are
    # small integers numbered per server, so a key alone could name another
    # server's playlist once the mirror target changes, or the library
    # server's when the two differ. See mirror_plex.write_mirror.
    ("playlists", "plex_mirror_machine", "TEXT"),
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
    # The folder this user's mirrors go to, under the mirror root: derived
    # from the username once (secure_filename, or user-<id>) and kept, so
    # it never changes under a Kodi profile pointed at it. NULL until the
    # first per-user write. See mirror.user_folder_name().
    ("users", "mirror_folder_name", "TEXT"),
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
    # The album MusicBrainz names for a gap whose source gave none
    # (album_lookup.py). Kept apart from `album`, which is part of the row's
    # identity across resyncs; readers take `album` first, this otherwise.
    # NULL = not looked up, or no studio album found.
    ("unresolved_playlist_tracks", "inferred_album", "TEXT"),
    # A playlist made on a device: an entry whose card path names no file
    # Trobar put there is matched by the artist and title its file name
    # gives, but only as a suggestion. suggested_track_id is that guess,
    # accepted whether its owner took it (only then is the entry matched),
    # and source_path the entry as the card had it, for the review list.
    ("unresolved_playlist_tracks", "suggested_track_id", "INTEGER"),
    ("unresolved_playlist_tracks", "accepted", "INTEGER NOT NULL DEFAULT 0"),
    ("unresolved_playlist_tracks", "source_path", "TEXT"),
    # The device path a track was delivered under, set when its download is
    # acked: a later format change or retag renames the expected file, and a
    # deletion has to name the one the device actually holds. NULL = not
    # recorded (delivered before this column); readers compute it then.
    ("device_track_state", "delivered_path", "TEXT"),
    # Bumped when a user's password changes or an admin resets it: a session
    # carrying an older value is over, so every other session of that user
    # ends (main._session_user_id).
    ("users", "session_generation", "INTEGER NOT NULL DEFAULT 0"),
    # Which of the playlists selected for a device it plays: NULL = all of
    # them in one play order (Everything, the default). Cleared when that
    # playlist is no longer selected for the device
    # (selection_store.recompute_device_state) or is deleted (the foreign key).
    ("devices", "play_playlist_id", "INTEGER REFERENCES playlists(id) ON DELETE SET NULL"),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    for table, column, coldef in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
    _post_migration_indexes(conn)
    _backfill_release_date(conn)


def _refresh_norms(conn: sqlite3.Connection) -> None:
    """Fill tracks.artist_norm/title_norm where they're empty, or recompute
    every row when matching.NORM_VERSION differs from the one they were
    computed with. Does not commit."""
    import matching  # db is imported by nearly everything; matching only here.

    version = str(matching.NORM_VERSION)
    where = "" if get_config(conn, "tracks_norm_version") != version else "WHERE artist_norm IS NULL OR title_norm IS NULL"
    rows = conn.execute(f"SELECT id, artist, title FROM tracks {where}").fetchall()
    conn.executemany("UPDATE tracks SET artist_norm = ?, title_norm = ? WHERE id = ?",
                     [(matching.normalize(r["artist"]), matching.normalize(r["title"]), r["id"]) for r in rows])
    set_config(conn, "tracks_norm_version", version)


def _refresh_album_artists(conn: sqlite3.Connection) -> None:
    """Fill tracks.album_artist for the whole library when any live track
    has none yet (a library from before the column). Does not commit."""
    if conn.execute("SELECT 1 FROM tracks WHERE album_artist IS NULL AND deleted_at IS NULL LIMIT 1").fetchone():
        import album_artist  # only here, like matching in _refresh_norms
        album_artist.refresh(conn)


def _backfill_delivered_paths(conn: sqlite3.Connection) -> None:
    """Name the file each device holds where no delivery recorded it
    (device_store.backfill_delivered_paths). Does not commit."""
    if conn.execute("SELECT 1 FROM device_track_state WHERE status = 'downloaded' "
                    "AND delivered_path IS NULL LIMIT 1").fetchone():
        import sync_state  # only here, like album_artist above
        sync_state.backfill_delivered_paths(conn)


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
    # The recording-id tier looks up every playlist entry that carries one,
    # in the file's own id and then in the one AcoustID resolved.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_mbid ON tracks(mbid) WHERE mbid IS NOT NULL"
    )
    # The artist/title matcher looks every entry's artist up by this; its
    # NULL branch (rows nobody normalised) uses the same index.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_artist_norm ON tracks(artist_norm)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_acoustid_mbid ON tracks(acoustid_mbid) "
        "WHERE acoustid_mbid IS NOT NULL"
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
        # Runs as a migration step (_run_migration_step): foreign keys are off,
        # so DROP TABLE playlists doesn't cascade-delete playlist_tracks, and
        # the whole swap is one transaction. A playlists_new left by an
        # older build's interrupted run would otherwise stop the CREATE.
        conn.execute("DROP TABLE IF EXISTS playlists_new")
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
        ("mbid", "TEXT"), ("artist_norm", "TEXT"), ("title_norm", "TEXT"),
        ("album_artist_tag", "TEXT"), ("album_artist", "TEXT"), ("album_artist_read", "INTEGER"),
        ("lossless", "INTEGER"),
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

    # Runs as a migration step (_run_migration_step): foreign keys are off, so
    # DROP TABLE tracks doesn't cascade-delete device_track_state and
    # playlist_tracks, and the whole swap is one transaction.
    conn.execute("DROP TABLE IF EXISTS tracks_new")
    conn.execute(f"CREATE TABLE tracks_new ({ddl}) STRICT")
    conn.execute(f"INSERT INTO tracks_new ({names}) SELECT {select} FROM tracks")
    conn.execute("DROP TABLE tracks")
    conn.execute("ALTER TABLE tracks_new RENAME TO tracks")

    # DROP TABLE took both indexes with it. idx_tracks_artist_album lives in
    # SCHEMA (already executed by now) and idx_tracks_fingerprint in
    # _post_migration_indexes (likewise), so neither would come back on its own.
    # Losing the fingerprint one wouldn't error — it would quietly turn #239's
    # rematch into a full table scan per pushed entry.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_artist_album ON tracks(artist, album)")
    _post_migration_indexes(conn)


def _migrate_unresolved_playlist_tracks_strict(conn: sqlite3.Connection) -> None:
    """#298: convert `unresolved_playlist_tracks` to a STRICT table.

    Named in the issue alongside jobs/device_provenance as one of the "new
    tables STRICT from now on" set, but it already existed by then (unlike
    those two), so an existing DB's copy still needs the same
    create-new/copy/drop/rename rebuild as _migrate_tracks_strict — SQLite
    can't ALTER a table into STRICT. Its columns are SCHEMA's 9 plus
    inferred_album, suggested_track_id, accepted and source_path, which
    _MIGRATIONS adds before this runs.

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
        # Added by _MIGRATIONS, which runs before this rebuild.
        ("inferred_album", "TEXT"), ("suggested_track_id", "INTEGER"), ("accepted", "INTEGER"),
        ("source_path", "TEXT"),
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
        else f"{name} {decl} NOT NULL DEFAULT 0" if name in ("excluded", "accepted")
        else f"{name} {decl} NOT NULL" if name == "position"
        else f"{name} {decl}"
        for name, decl in columns)

    # A migration step, like _migrate_tracks_strict.
    conn.execute("DROP TABLE IF EXISTS unresolved_playlist_tracks_new")
    conn.execute(f"CREATE TABLE unresolved_playlist_tracks_new ({ddl}) STRICT")
    conn.execute(
        f"INSERT INTO unresolved_playlist_tracks_new ({names}) "
        f"SELECT {select} FROM unresolved_playlist_tracks")
    conn.execute("DROP TABLE unresolved_playlist_tracks")
    conn.execute(
        "ALTER TABLE unresolved_playlist_tracks_new RENAME TO unresolved_playlist_tracks")

    # DROP TABLE took both indexes with it; neither creator runs again on a
    # DB past its first init. Losing idx_unresolved_playlist_tracks_identity
    # wouldn't error — unresolved_store.record_unresolved_playlist_tracks would
    # just start inserting duplicate rows on every resync instead of
    # preserving `excluded` across one.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_unresolved_playlist_tracks_playlist "
        "ON unresolved_playlist_tracks(playlist_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_unresolved_playlist_tracks_identity "
        "ON unresolved_playlist_tracks(playlist_id, artist, title, album)")


def _migration_1(conn: sqlite3.Connection) -> None:
    """Every schema change made before versioning: each step below probes
    for what it changes and skips it when it is already done, so this runs
    the same on an empty database and on one from any earlier release."""
    _migrate_api_tokens_table(conn)
    _migrate_playlist_tables(conn)
    conn.executescript(SCHEMA)
    _run_migrations(conn)
    _dedupe_selections(conn)
    # Lidarr requests that failed only because Lidarr didn't answer were
    # once recorded as attempted, so they were never asked for again.
    # They are no longer recorded (lidarr_requests.TRANSIENT_REASONS);
    # clearing the old rows lets the next run retry those albums.
    # Idempotent: nothing writes such rows any more.
    conn.execute(
        "DELETE FROM lidarr_requested_albums WHERE status = 'failed' AND error = 'unreachable'"
    )


def _migrate_users_thanks_and_tidal_scopes(conn: sqlite3.Connection) -> None:
    """Two users columns.

    thanks_card_dismissed: whether this user dismissed the thank-you card the
    web UI shows admins once the server's device syncs reach a milestone. Per
    user: each admin dismisses it for themselves, and it never comes back.

    tidal_scopes: the scopes this user's Tidal link was granted
    (space-separated), so Profile can ask for a reconnect when sharing needs
    playlists.write and the link predates it. NULL for links made before
    this was recorded, which counts as read-only.

    Either may already exist: builds before this migration added them in
    migration 1, which a new database still ran."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "thanks_card_dismissed" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN thanks_card_dismissed INTEGER NOT NULL DEFAULT 0")
    if "tidal_scopes" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN tidal_scopes TEXT")


def _migrate_tidal_shares(conn: sqlite3.Connection) -> None:
    """The tidal_shares table: a copy of one member's Tidal playlist kept
    in another member's own Tidal account (see tidal_shares.py). Ids in it
    are Tidal's: the source playlist, and the copy, recorded when Trobar
    created it and the only playlist on the recipient's side it ever writes
    to. `snapshot` is the merged track list (JSON, Tidal track ids) after the
    last good pass; the two `unavailable_*` lists are tracks Tidal refused
    on that side. One live share per playlist and recipient; ended ones are
    history."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tidal_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_remote_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'ended')),
            two_way INTEGER NOT NULL DEFAULT 0,
            copy_remote_id TEXT,
            copy_title TEXT,
            snapshot TEXT,
            unavailable_owner TEXT,
            unavailable_recipient TEXT,
            last_synced_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            accepted_at TEXT,
            ended_at TEXT,
            CHECK (owner_user_id != recipient_user_id)
        ) STRICT""")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tidal_shares_live
        ON tidal_shares(source_remote_id, recipient_user_id) WHERE status != 'ended'""")


def _migrate_tidal_shares_delete_original(conn: sqlite3.Connection) -> None:
    """When the owner of an ended share asked for their original to be
    deleted, when the sync deleted it, and, when it refused because the
    copy lacks tracks, which ones (JSON list of {"track_id", "position"}).
    Tidal writes happen only in the playlist sync, so the request waits for
    the next pass."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tidal_shares)")}
    if "delete_original_requested_at" not in existing:
        conn.execute("ALTER TABLE tidal_shares ADD COLUMN delete_original_requested_at TEXT")
    if "original_deleted_at" not in existing:
        conn.execute("ALTER TABLE tidal_shares ADD COLUMN original_deleted_at TEXT")
    if "delete_blocked_tracks" not in existing:
        conn.execute("ALTER TABLE tidal_shares ADD COLUMN delete_blocked_tracks TEXT")


def _migrate_playlists_rule(conn: sqlite3.Connection) -> None:
    """A rule playlist's rule (JSON, see rule_playlists.py); NULL for every
    other playlist."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(playlists)")}
    if "rule" not in existing:
        conn.execute("ALTER TABLE playlists ADD COLUMN rule TEXT")


def _migrate_device_plays(conn: sqlite3.Connection) -> None:
    """Plays reported by devices (device_plays.py), and each person's switch
    for keeping them, off until they turn it on. A device's removal leaves its
    plays with their owner; removing the person removes them."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "device_plays_enabled" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN device_plays_enabled INTEGER NOT NULL DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_plays (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
            track_id INTEGER NOT NULL,
            artist TEXT,
            title TEXT,
            album TEXT,
            played_at TEXT NOT NULL,
            UNIQUE (user_id, track_id, played_at)
        ) STRICT""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_device_plays_user_time ON device_plays(user_id, played_at)")


def _migrate_scrobble_credentials(conn: sqlite3.Connection) -> None:
    """Credentials for sending plays, encrypted by credential_store.py."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for column in ("maloja_api_key_enc", "listenbrainz_token_enc"):
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")


def _migrate_play_sends(conn: sqlite3.Connection) -> None:
    """How far each person's plays have gone to each target (play_forward.py).
    That mark is a play id, so device_plays is rebuilt with AUTOINCREMENT: an id
    is never used again after a delete, and a new play always comes after it."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'device_plays'").fetchone()[0]
    if "AUTOINCREMENT" not in sql:
        conn.execute("ALTER TABLE device_plays RENAME TO device_plays_old")
        conn.execute("DROP INDEX IF EXISTS idx_device_plays_user_time")
        conn.execute("""
            CREATE TABLE device_plays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
                track_id INTEGER NOT NULL,
                artist TEXT,
                title TEXT,
                album TEXT,
                played_at TEXT NOT NULL,
                UNIQUE (user_id, track_id, played_at)
            ) STRICT""")
        conn.execute("INSERT INTO device_plays SELECT * FROM device_plays_old")
        conn.execute("DROP TABLE device_plays_old")
        conn.execute("CREATE INDEX idx_device_plays_user_time ON device_plays(user_id, played_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS play_sends (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            target TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            last_play_id INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            rejected INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, target)
        ) STRICT""")


def _migrate_lastfm_session(conn: sqlite3.Connection) -> None:
    """A person's Last.fm session for sending plays, encrypted by
    credential_store.py, and the account name it belongs to."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for column in ("lastfm_session_enc", "lastfm_session_name"):
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")


def _migrate_setup_completed_for_old_installs(conn: sqlite3.Connection) -> None:
    """Setup is the only thing that sets setup_completed, so an install older
    than the setup wizard never got it, and without it scheduled and after-scan
    playlist syncs quietly never run. Such an install has a user and has
    finished a scan. A finished scan's job is kept only for days, so live
    tracks with no scan under way count too. The wizard starts its scan after
    setting the flag, so an install still in the wizard normally has none and
    finishes it; one that scanned outside the wizard and cancelled part-way
    would skip it, a small harm next to old installs never syncing."""
    if get_config(conn, "setup_completed") is not None:
        return
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
        return
    states = {row[0] for row in conn.execute("SELECT DISTINCT state FROM jobs WHERE type = 'library_scan'")}
    tracks = conn.execute("SELECT 1 FROM tracks WHERE deleted_at IS NULL LIMIT 1").fetchone() is not None
    if "done" in states or (tracks and not states & {"queued", "running"}):
        set_config(conn, "setup_completed", "1")


def _migrate_ma_account_owners(conn: sqlite3.Connection) -> None:
    """Which member each Music Assistant streaming account belongs to: its
    playlists become that member's, private to them. An account with no row
    stays everyone's."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ma_account_owners (
            instance_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE
        ) STRICT""")


def _migrate_source_health(conn: sqlite3.Connection) -> None:
    """How each playlist source has fared across syncs (source_health.py):
    failures in a row, since when, the last reason, and whether it alerts."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_health (
            key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            provider TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            subscription_id INTEGER REFERENCES playlist_subscriptions(id) ON DELETE CASCADE,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            failing_since TEXT,
            last_reason TEXT,
            last_success_at TEXT,
            last_seen_at TEXT NOT NULL,
            alerting INTEGER NOT NULL DEFAULT 0
        ) STRICT""")



def _migrate_mirror_overwrites(conn: sqlite3.Connection) -> None:
    """What a mirror write overwrote on its target (mirror_copies.py).

    mirror_copies: each server sink's copy of a playlist as Trobar last
    left it, the baseline that tells an edit made on the target from a
    change Trobar made itself.

    Three mirror_runs columns: how many tracks added on the target the
    write discarded, how many removed there it put back, and those tracks
    (JSON, at most mirror_runs.LIST_MAX)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mirror_copies (
            playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            sink TEXT NOT NULL,
            items TEXT NOT NULL,
            PRIMARY KEY (playlist_id, sink)
        ) STRICT""")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mirror_runs)")}
    if "discarded" not in existing:
        conn.execute("ALTER TABLE mirror_runs ADD COLUMN discarded INTEGER NOT NULL DEFAULT 0")
    if "restored" not in existing:
        conn.execute("ALTER TABLE mirror_runs ADD COLUMN restored INTEGER NOT NULL DEFAULT 0")
    if "overwritten" not in existing:
        conn.execute("ALTER TABLE mirror_runs ADD COLUMN overwritten TEXT NOT NULL DEFAULT '[]'")

def _migrate_subsonic_owners(conn: sqlite3.Connection) -> None:
    """Navidrome playlist owners. A member gives their Navidrome username
    (users.subsonic_username; no password: the library account is a
    Navidrome admin and reads every user's playlists). Each Subsonic
    playlist records its owner there (playlists.source_owner) and whether
    it is private there (playlists.source_private). A private one whose
    owner isn't a member yet is visible to admins only."""
    users = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "subsonic_username" not in users:
        conn.execute("ALTER TABLE users ADD COLUMN subsonic_username TEXT")
    playlists = {row[1] for row in conn.execute("PRAGMA table_info(playlists)")}
    if "source_owner" not in playlists:
        conn.execute("ALTER TABLE playlists ADD COLUMN source_owner TEXT")
    if "source_private" not in playlists:
        conn.execute("ALTER TABLE playlists ADD COLUMN source_private INTEGER NOT NULL DEFAULT 0")
    # Whose account the Subsonic mirror copy is in, when Trobar gave it to
    # its owner (their Navidrome username); NULL is the mirror account.
    if "subsonic_mirror_owner" not in playlists:
        conn.execute("ALTER TABLE playlists ADD COLUMN subsonic_mirror_owner TEXT")
    # One Navidrome user per member, whatever the case: it decides whose
    # private playlists someone receives.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_subsonic_username "
                 "ON users (subsonic_username COLLATE NOCASE) WHERE subsonic_username IS NOT NULL")

# The numbered schema migrations. PRAGMA user_version records how many have
# run; each runs once, in order. Append, never edit or reorder one that has
# shipped: a database that already ran it will not run it again.
#
# 2-4 are the table rebuilds, which used to sit inside migration 1. They list
# columns that migration 1's _MIGRATIONS adds, so they come after it. Each
# checks the table's own CREATE sql first, so on a database migration 1 had
# already rebuilt, and on a fresh one, they do nothing.
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migration_1,
    _migrate_tracks_strict,
    _migrate_unresolved_playlist_tracks_strict,
    _migrate_playlists_composite_key,
    _migrate_users_thanks_and_tidal_scopes,
    _migrate_tidal_shares,
    _migrate_tidal_shares_delete_original,
    _migrate_playlists_rule,
    _migrate_device_plays,
    _migrate_scrobble_credentials,
    _migrate_play_sends,
    _migrate_lastfm_session,
    _migrate_setup_completed_for_old_installs,
    _migrate_source_health,
    _migrate_ma_account_owners,
    _migrate_mirror_overwrites,
    _migrate_subsonic_owners,
)


def _foreign_key_violations(conn: sqlite3.Connection) -> int:
    return len(conn.execute("PRAGMA foreign_key_check").fetchall())


def _run_migration_step(conn: sqlite3.Connection, step: Callable[[sqlite3.Connection], None],
                        version: int | None = None) -> None:
    """One migration after the first: inside BEGIN IMMEDIATE, with foreign
    keys off (a table rebuild's DROP would otherwise cascade-delete the rows
    that reference it, and the pragma is a no-op inside a transaction), and
    committed together with its version. If it raises, nothing it did stays.

    Foreign keys are checked by count, before and after: a step that leaves
    more violations than it found fails. A plain check would also fail on
    orphans the database already had, which real databases collect, and
    stop the start over something no migration did."""
    # The pragma needs no open transaction; nothing should be pending here.
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = _foreign_key_violations(conn)
        step(conn)
        after = _foreign_key_violations(conn)
        if after > before:
            raise RuntimeError(
                f"migration {version}: {after - before} new foreign key violation(s); "
                "nothing was changed")
        if version is not None:
            conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Run the migrations this database has not had yet. Each runs through
    _run_migration_step, so one that is interrupted leaves nothing behind
    and runs again on the next start. Migration 1 is the exception: it
    commits as it goes (executescript), and is safe to run again because
    every step in it checks first. A database from a newer build stops the
    start, since this build cannot know what that build changed."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    known = len(MIGRATIONS)
    if current > known:
        raise SystemExit(
            f"The database is at schema version {current}, but this build of Trobar "
            f"only knows versions up to {known}. Run the newer build again, or restore "
            "the backup taken before upgrading.")
    for version in range(current + 1, known + 1):
        if version == 1:
            MIGRATIONS[0](conn)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        else:
            _run_migration_step(conn, MIGRATIONS[version - 1], version)


def init_db() -> None:
    with connection() as conn:
        _apply_migrations(conn)
        # Maintenance that runs on every start, not once: each step checks
        # whether it has anything to do.
        _refresh_norms(conn)
        _refresh_album_artists(conn)
        _backfill_delivered_paths(conn)
        # device_type taxonomy split (phone/tablet/watch/dap/sdcard)
        # replaced the old 'android' catch-all — reclassify existing rows so
        # they get the right icon instead of silently falling back to phone.
        # Every start, not once: the API accepted 'android' until it left
        # DEVICE_TYPES, so a database past migration 1 can still hold some.
        conn.execute("UPDATE devices SET device_type = 'phone' WHERE device_type = 'android'")
        # #501 (caught in review): basket_items gained a required device
        # dimension via the new basket_item_devices join table, but every
        # basket_items row that predates this feature has zero links in
        # it -- it never had one to have. Left alone, such a row becomes a
        # permanent ghost: no per-device section to render under, and the
        # device-scoped fan-out filters it out of what it considers
        # sendable, so it can never be sent OR cleared by any normal path.
        # A basket is a transient staging list, not data worth preserving
        # across this shape change -- delete them. Every start, not once:
        # replacing a device (device_store.transfer_device) can still leave an
        # item staged only for the old one with no link.
        conn.execute(
            "DELETE FROM basket_items WHERE id NOT IN "
            "(SELECT DISTINCT basket_item_id FROM basket_item_devices)"
        )
        _seed_spotify_experimental_default(conn)
        _seed_playlist_sync_interval_default(conn)
        # Tidal refresh tokens stored in cleartext are encrypted. Every start:
        # one the credential key couldn't seal is tried again.
        import tidal_token  # here, not at the top: it imports credential_store, which imports db
        tidal_token.convert_cleartext(conn)
        conn.commit()


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


def _seed_playlist_sync_interval_default(conn: sqlite3.Connection) -> None:
    """The scheduled playlist sync is on (every 6 h) for a fresh install
    and off for an existing one, which keeps the behaviour it had: an
    upgrade must not start syncing, and so writing to mirrors, on a timer
    nobody set. "Existing" means setup has been completed. Decided once,
    when the key has never been set, like _seed_spotify_experimental_default
    above: an admin's later choice, either way, holds."""
    if get_config(conn, "playlist_sync_interval_hours") is not None:
        return
    set_config(conn, "playlist_sync_interval_hours",
               "0" if get_config(conn, "setup_completed") else "6")


def set_config(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# Successful device syncs reported to this server, across every device of
# every account: one count, kept in app_config, never broken down by device
# or user. It starts at zero on the version that added it.
DEVICE_SYNCS_KEY = "device_syncs_completed"


def record_device_sync(conn: sqlite3.Connection) -> None:
    """One more successful device sync. A single upsert, so two devices
    reporting at once can't lose one."""
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (DEVICE_SYNCS_KEY,),
    )


def device_syncs_completed(conn: sqlite3.Connection) -> int:
    value = get_config(conn, DEVICE_SYNCS_KEY)
    try:
        return int(value) if value is not None else 0
    except ValueError:
        return 0


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


def get_extra_playlist_folder() -> Path | None:
    """One extra folder walked for `.m3u`/`.m3u8` alongside MUSIC_ROOT,
    for players that keep their playlists in a directory of their own
    rather than inside the music library — and equally for a folder the
    user exports playlists into by hand.

    Same override-over-env-var precedence and same "unset means not
    configured" contract as get_mirror_folder() above — this is the read
    side of the pair that one is the write side of. Read side only:
    filesystem_client never writes here.

    One folder rather than a list: it keeps the playlist id derived from
    the folder-relative path alone, which stays stable across an admin
    edit in a way an index into a list would not. Two players are handled
    by mounting both under this one folder.

    get_conn() per call, like get_mirror_folder() — an admin edit takes
    effect on the next sync with no restart."""
    conn = get_conn()
    try:
        override = get_config(conn, "extra_playlist_folder")
    finally:
        conn.close()
    path_str = override or os.environ.get("EXTRA_PLAYLIST_ROOT")
    return Path(path_str) if path_str else None



def get_mirror_share_location() -> str | None:
    """Where the mirror folder sits inside the music share, as a path
    relative to MUSIC_ROOT (e.g. "UserPlaylists"). Inside the container the
    two are separate mounts, so Trobar can't work this out; the admin says
    it, and main._apply_folder_settings() checks it against the files. Set,
    mirrors list tracks relative to the playlist file (mirror.entry_path);
    unset (None), they keep absolute paths."""
    conn = get_conn()
    try:
        return get_config(conn, "mirror_share_location") or None
    finally:
        conn.close()


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


def get_mirror_music_assistant_config() -> tuple[str, str] | None:
    """The Music Assistant mirror target: Music Assistant's one connection
    (music_assistant_url/token), the same one it is read through as a
    provider or an extra source, since its playlists are server-wide.
    None when it isn't configured."""
    conn = get_conn()
    try:
        url = get_config(conn, "music_assistant_url")
        token = get_config(conn, "music_assistant_token")
    finally:
        conn.close()
    if not url or not token:
        return None
    return url, token


def get_mirror_plex_config() -> tuple[str, str] | None:
    """The Plex mirror target: its own server URL and X-Plex-Token
    (mirror_plex_url/mirror_plex_token), a distinct write destination from
    the active-provider plex_url/plex_token even when both point at the
    same server, as for the other server sinks. Plex playlists belong to
    the token's account, so that is where the copies land. None when it
    isn't configured."""
    conn = get_conn()
    try:
        url = get_config(conn, "mirror_plex_url")
        token = get_config(conn, "mirror_plex_token")
    finally:
        conn.close()
    if not url or not token:
        return None
    return url, token


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


def get_lidarr_config(conn: sqlite3.Connection | None = None) -> tuple[str, str, str, int, int] | None:
    """#494: the full connection needed to actually request an album —
    url, api_key, root_folder_path, quality_profile_id, metadata_profile_id.
    Unlike every mirror-target config above (three plain strings, all
    admin-typed), the last two of these five have no safe default: they're
    Lidarr-instance-specific ids with no meaningful value Trobar could
    guess, so they can only be *chosen* from GET /api/admin/lidarr-options'
    live lists, never defaulted. Same "any missing means not configured,
    return None rather than a partial tuple" contract as the mirror
    targets. Reads through `conn` when given (a caller mid-transaction, or
    on a connection of its own), else through a connection of its own."""
    own = conn is None
    if conn is None:
        conn = get_conn()
    try:
        url = get_config(conn, "lidarr_url")
        api_key = get_config(conn, "lidarr_api_key")
        root_folder_path = get_config(conn, "lidarr_root_folder_path")
        quality_profile_id = get_config(conn, "lidarr_quality_profile_id")
        metadata_profile_id = get_config(conn, "lidarr_metadata_profile_id")
    finally:
        if own:
            conn.close()
    if not url or not api_key or not root_folder_path \
            or not quality_profile_id or not metadata_profile_id:
        return None
    try:
        return (url, api_key, root_folder_path,
                int(quality_profile_id), int(metadata_profile_id))
    except ValueError:
        return None
