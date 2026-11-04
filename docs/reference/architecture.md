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

- **Library providers** — the active library source is one of eight:
  `filesystem_client.py`, `roon_client.py`, `jellyfin_client.py`,
  `emby_client.py`, `plex_client.py`, `lms_client.py`, `subsonic_client.py` and
  `music_assistant_client.py`. The Jellyfin and Emby clients share most of
  their code in `mediabrowser_client.py`.
- **Other playlist sources** — a member's own account through `tidal_client.py`
  or `spotify_client.py`, and a public playlist by link through
  `ytmusic_client.py` or `spotify_public_client.py`. `tidal_shares.py` keeps a
  copy of one member's Tidal playlist in another member's Tidal account, on
  each playlist sync.
- **Library** — `scanner.py` (tag/folder scanning), `matching.py` (resolving
  playlist entries to local files).
- **Suggestions & history** — `suggestions.py`, `lastfm.py`, `listenbrainz.py`.
- **Playlists** — `playlist_sync.py`; mirrors in `mirror.py` (the `.m3u`
  folder) and `mirror_subsonic.py`, `mirror_jellyfin.py`, `mirror_emby.py`,
  `mirror_plex.py`, `mirror_music_assistant.py` (the Jellyfin and Emby sinks
  share `mirror_mediabrowser.py`, and `mirror_sinks.py` lists all six for
  the code that writes, deletes or toggles them); missing-album requests in
  `lidarr_requests.py` and `lidarr_client.py`.
- **Images** — `covers.py`, `artist_images.py`, `audiodb_client.py`.
- **Sync/transcode** — `selection_store.py` (selections and the device state
  they add up to), `basket_store.py` (what's staged for devices before it's
  sent), `token_store.py` (device, pairing-code and integration tokens),
  `device_store.py` (devices: what each holds and is told to change, and
  where each track lands on it), `unresolved_store.py` (playlist entries that
  matched no library track), `autofit.py` (filling a device's storage budget
  with its owner's most-played albums), `sync_state.py` (re-exports all of
  these for one release), `transcode.py`.
- **Data** — `db.py`; schema migrations run at startup. They are numbered (`db.MIGRATIONS`), each runs once, and `PRAGMA user_version` records how many have run. A new schema change is a new function appended to that list, never an edit to one that has shipped.

## The device sync protocol

Devices authenticate with a per-device **Bearer token**. The client normally
gets it by redeeming a short pairing code (`POST /api/enrollment/redeem`; see
[Device tokens](../clients/index.md#device-tokens)), and the token isn't
shown. Two cases show it: **Add device** falls back to showing the raw token
when it can't create a pairing code, and **Older desktop app? Get a config
file instead** issues a new token for a desktop build that predates codes.
Sync is **server-driven**:

1. The client asks what it's missing (`GET /api/device/changes`); the server
   diffs the device's selections against what the device has acknowledged.
   The answer also carries `playlists`: one `.m3u8` per playlist selected for
   the device, listing only the tracks that are or will be on it, with the
   paths the device knows them by. A playlist with none of its tracks on the
   device is left out, which tells the client to delete its file. Beside
   it, `play_playlist` is the `filename` of the one playlist the device
   should play, chosen per device, or `null` to play everything on it, as
   before. It is also `null` when the chosen playlist has no file on the
   device. A client that does not play music ignores it.
2. The client downloads each track (`GET /api/device/file/<id>`), writing
   **atomically**, and **acknowledges** it with the real byte count written.
3. On each sync the client **verifies** that files the server believes are
   present still exist, and reports removals — the server stops re-queuing tracks
   the user chose to leave deleted.
4. The Android and desktop clients also upload the playlists a person made
   on the device (`POST /api/device/playlists`): every `.m3u`/`.m3u8` they
   find, except the ones Trobar wrote. Each becomes a playlist of the
   device's owner, and one missing from a later upload is removed. See
   [Playlists made on a device](../using/playlists.md#playlists-made-on-a-device).

Every route under `/api/device/` takes the device's Bearer token:

| Route | What it is for |
|---|---|
| `GET /api/device/info` | The device's own record as the server holds it (its name, limit and settings), so a client can show what was set in the web UI. |
| `GET /api/device/changes` | What to download and delete, the playlist files, and `play_playlist` (step 1). |
| `GET /api/device/file/<id>` | One track, transcoded on the fly when the device's format is set (step 2). |
| `POST /api/device/ack` | A track downloaded or removed, with the bytes written; ignored if the track's state changed since `changes` said so. |
| `POST /api/device/missing-tracks` | Tracks the client found missing from disk: `redownload` queues them again, `exclude` stops them being queued again (step 3). |
| `POST /api/device/storage` | Free and total space of the device's storage, and the size of music in its folder that Trobar didn't put there. |
| `POST /api/device/sync-completed` | A sync finished with no error. It only adds one to a count for the whole server, which decides when administrators see the thank-you card; nothing about the device is kept. |
| `PATCH /api/device/limit` | The device sets its own storage limit, as the web UI can. |
| `PATCH /api/device/source-of-truth` | The device sets *Keep device content* (`"device"`) or not (`"server"`). |
| `POST /api/device/manifest` | The paths the device already holds, after pairing again: matching tracks are marked downloaded instead of fetched again, and the rest are recorded as its unknown tracks. The Android and desktop apps send it only then, after turning *Keep device content* on. |
| `GET /api/device/fingerprints` | The server's audio fingerprint for each track the device holds or will hold, paged, for the client's own record of where its files came from. |
| `POST /api/device/provenance` | That record sent back (track id, fingerprint, path), so a fresh server database can recognise the device's files by fingerprint. |
| `POST /api/device/plays` | Plays on the device, stored only if its owner keeps a play history. |
| `POST /api/device/playlists` | The playlists made on the device (step 4). |
| `GET /api/device/artist-image` | An artist's picture for the device's folders, `?size=small` for a downscaled one. |

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

## App API

The per-device token above now opens a **second route group**,
`/api/app/*`, for the Android app's own screens — library browsing, the
basket, the Home dashboard — so that what a browser can do on the road can
be done from the phone. No new credential: the token the phone holds from
pairing is resolved to the device, then to the device's **owner**, and
every route acts as that account, with admin re-checked on every request.

What it can do is bounded by what the owner can do in a browser: stage to
their own and pinned devices, send the basket, read the selections and
devices they can see, read and narrowly write their own dashboard
preferences. What it cannot do is listed as deliberately as what it can —
no direct selection creation, no whole-profile write, no admin listings
(counts only). The sync protocol under `/api/device/*` is untouched and
stays its own contract; a breaking change under the App API prefix, or a
new section a client gates a whole screen on, bumps the `app_api` level
that `/api/device/info` advertises, which is what a client checks — never
the server version.

Full route-by-route reference, the compatibility rule, and the shapes the
two prefixes share and do not share: [App API](app-api.md).

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
