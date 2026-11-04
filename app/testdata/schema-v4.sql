-- SPDX-FileCopyrightText: 2026 missing-foss
--
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- The schema of a database at user_version 4, as the build before any later
-- migration created it. Frozen: test_db_migrations upgrades it and checks the
-- result matches a new database. Never regenerate it from a newer build.
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT,
    lastfm_username TEXT,
    cover_view_mode TEXT NOT NULL DEFAULT 'list',
    show_reissue_year INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
, lastfm_api_key TEXT, listenbrainz_username TEXT, maloja_url TEXT, password_hash TEXT, avatar_path TEXT, dashboard_widgets TEXT, roon_profile TEXT, tidal_refresh_token TEXT, tidal_user_id TEXT, tidal_display_name TEXT, spotify_refresh_token TEXT, spotify_user_id TEXT, spotify_display_name TEXT, basket_last_destinations TEXT, hide_zero_match_playlists INTEGER NOT NULL DEFAULT 0, jellyfin_user_id TEXT, emby_user_id TEXT, mirror_folder_name TEXT, session_generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE app_config (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'phone',
    api_token_hash TEXT NOT NULL,
    max_size_bytes INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT
, autofit_percent INTEGER NOT NULL DEFAULT 100, reported_free_bytes INTEGER, reported_total_bytes INTEGER, free_bytes_reported_at TEXT, reported_foreign_bytes INTEGER, foreign_bytes_reported_at TEXT, reported_reserve_bytes INTEGER, transcode_format TEXT, artist_images TEXT, source_of_truth TEXT NOT NULL DEFAULT 'server' CHECK(source_of_truth IN ('server', 'device')), unknown_track_count INTEGER, icon TEXT, dashboard_widgets TEXT, play_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL);
CREATE TABLE device_unknown_tracks (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    artist TEXT,
    album TEXT,
    title TEXT,
    adopted INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (device_id, path)
);
CREATE TABLE tracks (
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
, duration REAL, release_date TEXT, isrc TEXT, fingerprint TEXT, acoustid_isrc TEXT, acoustid_mbid TEXT, fingerprint_checked_at TEXT, fingerprint_failed_at TEXT, fingerprint_seq INTEGER, mbid TEXT, artist_norm TEXT, title_norm TEXT, album_artist_tag TEXT, album_artist TEXT, album_artist_read INTEGER NOT NULL DEFAULT 0, lossless INTEGER) STRICT;
CREATE TABLE playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_provider TEXT,
    source_playlist_id TEXT,
    owner_user_id INTEGER REFERENCES users(id),
    shared INTEGER NOT NULL DEFAULT 1,
    inferred_origin_provider TEXT,
    golden_source_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    last_synced_at TEXT
, mirror_enabled INTEGER NOT NULL DEFAULT 0, mirror_filename TEXT, mirror_last_written_at TEXT, mirror_last_error TEXT, mirror_last_error_code TEXT, subsonic_mirror_enabled INTEGER NOT NULL DEFAULT 0, subsonic_mirror_remote_id TEXT, subsonic_mirror_last_written_at TEXT, subsonic_mirror_last_error TEXT, subsonic_mirror_last_error_code TEXT, jellyfin_mirror_enabled INTEGER NOT NULL DEFAULT 0, jellyfin_mirror_remote_id TEXT, jellyfin_mirror_last_written_at TEXT, jellyfin_mirror_last_error TEXT, jellyfin_mirror_last_error_code TEXT, emby_mirror_enabled INTEGER NOT NULL DEFAULT 0, emby_mirror_remote_id TEXT, emby_mirror_last_written_at TEXT, emby_mirror_last_error TEXT, emby_mirror_last_error_code TEXT, jellyfin_mirror_owner_id TEXT, emby_mirror_owner_id TEXT, music_assistant_mirror_enabled INTEGER NOT NULL DEFAULT 0, music_assistant_mirror_remote_id TEXT, music_assistant_mirror_last_written_at TEXT, music_assistant_mirror_last_error TEXT, music_assistant_mirror_last_error_code TEXT, plex_mirror_enabled INTEGER NOT NULL DEFAULT 0, plex_mirror_remote_id TEXT, plex_mirror_last_written_at TEXT, plex_mirror_last_error TEXT, plex_mirror_last_error_code TEXT, plex_mirror_machine TEXT, lidarr_request_enabled INTEGER NOT NULL DEFAULT 0, lidarr_request_last_run_at TEXT, lidarr_request_last_count INTEGER NOT NULL DEFAULT 0, lidarr_request_last_error TEXT, lidarr_request_last_error_code TEXT);
CREATE TABLE playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    artist TEXT,
    title TEXT,
    album TEXT,
    matched_track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL
);
CREATE TABLE unresolved_playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    artist TEXT,
    title TEXT,
    album TEXT,
    isrc TEXT,
    excluded INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
, inferred_album TEXT, suggested_track_id INTEGER, accepted INTEGER NOT NULL DEFAULT 0, source_path TEXT) STRICT;
CREATE TABLE playlist_subscriptions (
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
CREATE TABLE lidarr_requested_albums (
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
CREATE TABLE musicbrainz_album_lookups (
    lookup_key TEXT PRIMARY KEY,
    album TEXT,
    looked_up_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
CREATE TABLE device_delegations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grantee_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(grantee_user_id, target_user_id)
);
CREATE TABLE device_pins (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, device_id)
);
CREATE TABLE selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    target TEXT NOT NULL,
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE basket_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    target TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, type, target)
) STRICT;
CREATE TABLE basket_item_devices (
    basket_item_id INTEGER NOT NULL REFERENCES basket_items(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (basket_item_id, device_id)
) STRICT;
CREATE TABLE selection_devices (
    selection_id INTEGER NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (selection_id, device_id)
);
CREATE TABLE device_track_state (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')), bytes_on_device INTEGER, delivered_path TEXT,
    PRIMARY KEY (device_id, track_id)
);
CREATE TABLE enrollment_grants (
    code_hash TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    consumed_at TEXT
, device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE);
CREATE TABLE ended_sessions (
    sid TEXT PRIMARY KEY,
    ended_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
CREATE TABLE integration_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
) STRICT;
CREATE TABLE autofit_tracks (
    selection_id INTEGER NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    PRIMARY KEY (selection_id, track_id)
);
CREATE TABLE jobs (
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
CREATE TABLE device_provenance (
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
CREATE TABLE mirror_runs (
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
CREATE INDEX idx_tracks_artist_album ON tracks(artist, album);
CREATE INDEX idx_playlist_tracks_playlist ON playlist_tracks(playlist_id);
CREATE INDEX idx_unresolved_playlist_tracks_playlist
    ON unresolved_playlist_tracks(playlist_id);
CREATE UNIQUE INDEX idx_unresolved_playlist_tracks_identity
    ON unresolved_playlist_tracks(playlist_id, artist, title, album);
CREATE UNIQUE INDEX idx_playlist_subscriptions_identity
    ON playlist_subscriptions(owner_user_id, provider, external_id);
CREATE UNIQUE INDEX idx_lidarr_requested_albums_identity
    ON lidarr_requested_albums(normalized_artist, normalized_album);
CREATE INDEX idx_device_track_state_device ON device_track_state(device_id, status);
CREATE INDEX idx_jobs_claimable ON jobs(state, run_after, id);
CREATE UNIQUE INDEX idx_jobs_dedupe ON jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND state IN ('queued', 'running');
CREATE INDEX idx_device_provenance_pending
    ON device_provenance(device_id, state);
CREATE INDEX idx_mirror_runs_pair
    ON mirror_runs(playlist_id, sink, id);
CREATE INDEX idx_tracks_fingerprint ON tracks(fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX idx_tracks_mbid ON tracks(mbid) WHERE mbid IS NOT NULL;
CREATE INDEX idx_tracks_artist_norm ON tracks(artist_norm);
CREATE INDEX idx_tracks_acoustid_mbid ON tracks(acoustid_mbid) WHERE acoustid_mbid IS NOT NULL;
CREATE INDEX idx_tracks_fingerprint_seq ON tracks(fingerprint_seq) WHERE fingerprint_seq IS NOT NULL;
CREATE UNIQUE INDEX idx_selections_unique_target ON selections(type, target, created_by_user_id) WHERE type != 'autofit';
CREATE UNIQUE INDEX idx_playlists_provider_srcid ON playlists(source_provider, source_playlist_id) WHERE source_playlist_id IS NOT NULL;
CREATE UNIQUE INDEX idx_playlists_provider_title ON playlists(source_provider, title) WHERE source_playlist_id IS NULL;
