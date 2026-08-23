<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Architecture & Sync Protocol

A short orientation for contributors. This is intentionally high-level; the code
is the authority.

## Server

Trobar's server is a **Flask** app backed by **SQLite**, served by the
[waitress](https://pypi.org/project/waitress/) WSGI server, packaged as a single
Docker image running as an unprivileged user. The main pieces live under `app/`:

- **Providers** — `filesystem_client.py`, `roon_client.py`, `jellyfin_client.py`,
 `subsonic_client.py`, plus the per-user streaming clients `tidal_client.py` and
 `spotify_client.py`.
- **Library** — `scanner.py` (tag/folder scanning), `matching.py` (resolving
  playlist entries to local files).
- **Suggestions & history** — `suggestions.py`, `lastfm.py`, `listenbrainz.py`.
- **Playlists** — `playlist_sync.py`.
- **Images** — `covers.py`, `artist_images.py`, `audiodb_client.py`.
- **Sync/transcode** — `sync_state.py`, `transcode.py`.
- **Data** — `db.py`; schema migrations run at startup.

## The device sync protocol

Devices authenticate with a per-device **Bearer token** (created in the web UI,
shown once). Sync is **server-driven**:

1. The client asks what it's missing; the server diffs the device's selections
   against what the device has acknowledged.
2. The client downloads each track (`GET /api/device/file/<id>`), writing
   **atomically**, and **acknowledges** it with the real byte count written.
3. On each sync the client **verifies** that files the server believes are
   present still exist, and reports removals — the server stops re-queuing tracks
   the user chose to leave deleted.

Transcoding to MP3 happens **on the server**, on the fly at
`GET /api/device/file/<id>` for any device whose format is set — never cached
server-side; the clients (Android, desktop, and Garmin) never transcode —
Garmin is the most storage-constrained of the three, so this matters most
there. See [Devices & Storage → Transcoding](../using/devices.md#transcoding).

## Integration API

A third credential, alongside the browser session and the per-device Bearer
token above: an **admin-minted Bearer token** for external tools — Home
Assistant, Grafana, an uptime monitor, a shell script — that need
device/sync/server status, and the ability to trigger a library rescan,
without holding a browser session or posing as a device. Create one in
**Profile → Integrations** (admin-only, shown once like a device token;
only its hash is stored).

```
GET /api/integrations/devices
Authorization: Bearer <token>
```

Returns the same shape `GET /api/devices` does for the token's owner — every
device that owner can see (their own, plus anything pinned/delegated to
them; an admin's token sees every device, matching what an admin session
already sees — in practice every token, since only an admin can mint one),
including each device's `sync_status`. The same token also authenticates
`GET /api/integrations/server` and `POST /api/integrations/actions/scan`
 — one credential covers all three, never folded into the browser
session's own authentication, so it can't reach anything beyond this trio
regardless of what's added elsewhere later. What keeps the rescan action
safe is *who was allowed to mint the token*, not a separate read-only
credential type — see [Integration API](integration-api.md) for the full
reasoning. A wrong token backs off per IP, in its own bucket — never shared
with the login rate limiter, so a misconfigured integration polling with a
stale token can't lock out a real login attempt.

Full field-by-field reference (types, null semantics, rate-limit details)
and worked recipes for Home Assistant, Grafana, and shell monitors:
[Integration API](integration-api.md).

## Database-loss recovery

A device can upload its **manifest** and the server re-matches it against the
library, rebuilding sync state — so a lost server database doesn't force a
from-scratch re-sync. This underpins the [backup](../operations/backups.md)
story.

Manifest matching is **path-based**, which is its limitation: it only works
while the device's folder layout still matches what the server would compute
today. Track **provenance** (below) is the sturdier identity that backs it up.

## Track provenance

Alongside the audio, the server hands each device the **fingerprint it computed
for that track** — an acoustic fingerprint of the source audio — and the client
stores it in a small local database next to the file it wrote. That record says
*"this file came from Trobar, and here is the identity the server itself gave
it."*

**Clients never compute fingerprints.** They only store what the server sends.
That is deliberate: a watch has no audio-decode path at all, and on a phone
decoding a whole library would be a real battery cost. The expensive half stays
on the server, which is already reading the audio anyway.

Two things this makes possible:

- Tracks Trobar itself put on a device stop being reported back as
  *unknown* just because the client's naming changed between versions — the
  device can prove where they came from instead of the server inferring it
  from a path.
- After a database loss, a device can hand its provenance records back and the
  server re-identifies the files **by audio** rather than by filename, so the
  match survives a library reorganisation or a re-tagging that path matching
  would miss.

Fingerprints are computed as devices sync (never on the request path — it's real
audio decoding, done in the background) so they fill in progressively; a client
polls for the ones not ready yet. The fingerprint shipped is always the
**source** file's, even to a device holding a transcoded copy, because that is
the form the server can re-derive from its own library when re-identifying.

Requires no AcoustID key — computing a fingerprint is entirely local. A key only
matters for the separate [ISRC lookup](../administration.md) feature.

### Handing it back

A device can push its provenance records back to the server, which then works
out which library track each one is **by matching audio fingerprints**, not
filenames.

That is the difference that matters. Manifest matching compares the file's name
against a name the server recomputes from the track's tags, track number and
format — so re-tagging an album, or a change in how a client names files, makes
Trobar stop recognising files it wrote itself. Fingerprints don't care: the audio
is the same audio.

So the two problems this fixes:

- Tracks listed as *unknown, please adopt* despite Trobar having put them there
  are recognised again, and quietly stop being flagged.
- After a database loss, sync state is rebuilt from what devices actually hold —
  surviving a library reorganisation or a re-tag that filename matching couldn't.

**A pushed fingerprint is treated as a hint, never as proof.** The server uses it
only to find a candidate track, then re-reads that file and fingerprints it again
before believing anything. A client can't talk the server into associating a
track by asserting it.

Matching runs as a background job, not during the push, because verifying each
record means decoding audio. It works in batches and continues on each following
sync, so a large recovery drains steadily without the client having to manage it.
Progress and any failures appear under
[Administration → Background jobs](../administration.md).
