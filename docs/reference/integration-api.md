<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Integration API

An API for external tools that want device/sync/server status, and to
trigger a library rescan, without holding a browser session or posing as a
sync device — Home Assistant, Grafana, a shell script, an uptime monitor.
Shipped in **2.8.0**; its field types settled in **2.8.1** —
**2.8.1 is the practical minimum**, since 2.8.0 emits `is_own`/`is_pinned`
as `0`/`1` rather than real booleans. A second endpoint, server-wide
metrics, arrived in **2.9.0**. A rescan-triggering action arrived
the same release, which went through two shapes before merging:
an initial version split read and write into two separate credential
types, revised before merge to the single, admin-only-minted token
described below. **2.9.0 is required**
for both the server-metrics endpoint and the action route; the original
devices endpoint alone still only needs 2.8.1. A fourth endpoint, mirror
health, arrived later — device sync already had a monitoring surface
here; the three newer sinks (plus the pre-existing filesystem one)
didn't.

See [Architecture & Sync Protocol](architecture.md#integration-api)
for how this token fits alongside the browser session and per-device
tokens
for the design rationale behind treating Home Assistant as a
monitoring/automation surface rather than a sync target.

## What it is — and isn't

- **Four endpoints, one credential, admin-minted.** `/api/integrations/devices`,
 `/api/integrations/server`, `/api/integrations/mirrors`,
 and `/api/integrations/actions/scan` are all authenticated by
  the same **integration token**. Only an admin can mint one (`POST
  /api/integration-tokens` requires an admin session) — that's the
  property that keeps a rescan trigger safe now, not a second credential
  type. A household realistically has one admin (whoever installed the
  server); everyone else uses a phone or watch, not their own
 integrations. `/api/integrations/devices` is per-viewer (it returns
  exactly what the token's owner would see in the web UI's device list —
  in practice, since the owner is always an admin, that's every device);
 `/api/integrations/server` and `/api/integrations/mirrors` are both
  instance-wide — the same numbers every logged-in user's own dashboard
  or admin panel already shows them, so neither is scoped per caller at
  all.
- **A token cannot create or revoke other tokens** — that's a
  session-only, admin-only route. And it can only ever do the four things
  above: it is wired into its own authenticator
 (`_authenticated_integration_token`), never into the one every
  session-authenticated route (including every mutating one) uses, so it
  cannot reach anything outside these four routes regardless of what's
  added elsewhere later.
- The token grants **nothing an ordinary admin session couldn't already
  do** — starting a library scan has never required anything beyond being
  logged in, admin or not, in the web UI. What minting requires *is*
  admin, though: that's what replaced the earlier read-only/action split.
  Tokens are separately revocable per name: turning off one automation's
  token never touches another's.
- **Admin status is checked on every use, not just at minting.**
  If the account that minted a token stops being an admin, its tokens
 stop authenticating immediately, the same `401` as an unknown token —
  a demoted user's integration shouldn't be able to tell "revoked" from
  "never existed." Deleting the account revokes its tokens too (a plain
  foreign-key cascade, unaffected by this).
- It is **not** a general-purpose public API. The device API
 (`/api/device/*`) is for the sync clients and is a different, unrelated
  contract — a different credential, different rate-limit bucket, and not
  meant for external tools at all.

## Creating and revoking a token

**Profile → Integrations** in the web UI, visible to admins only: create
it, name it, and the value is **shown once** — copy it before closing the
dialog, the same as a device token. Revoke from the same place;
revocation is immediate.

Worth stating up front rather than in a footnote: this credential is
long-lived and typically ends up pasted into another system's
configuration (a `secrets.yaml`, a Grafana data source, a cron job). Knowing
where the off switch is matters more than it does for a password you type
and forget.

## Devices

```
GET /api/integrations/devices
Authorization: Bearer <token>
```

```sh
curl -H "Authorization: Bearer <YOUR_TOKEN>" \
  https://trobar.example.com/api/integrations/devices
```

<!-- Editors: keep the angle brackets around <YOUR_TOKEN> in the curl example
     above. A bare YOUR_TOKEN trips gitleaks' curl-auth-header rule in CI,
     regardless of the placeholder's actual entropy. -->

### Errors

| Status | Meaning |
|---|---|
| `401` | Missing `Authorization` header, or the token is wrong / revoked. |
| `404` | This server predates 2.8.0 — the endpoint doesn't exist yet. |
| `429` | Rate-limited — see below. |

Every `/api/*` error responds `{"error": "..."}` with the matching status
code, not an HTML error page — safe to `jq -r .error` in a script.

## The response

A JSON array, one object per device the token's owner can see (their own,
plus anything pinned to them; an admin's token sees every device, matching
what an admin session already sees):

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Stable identifier — use this, not `name`, as your own key. Names are user-editable. |
| `name` | string | |
| `device_type` | string | Currently one of `phone`, `tablet`, `watch`, `dap`, `sdcard`, `folder` — don't treat this as a closed set. |
| `owner_user_id` | integer | |
| `owner_username` | string | |
| `is_own` | boolean | `true` if this is the token owner's own device. |
| `is_pinned` | boolean | `true` if visible to the token owner via delegation without being theirs. A household's token can see other members' devices this way — decide deliberately whether to distinguish them in whatever you build. |
| `max_size_bytes` | integer or `null` | The configured storage cap; `null` means no limit. **Can exceed `reported_free_bytes`** — pick your "percent full" denominator deliberately and expect it can read over 100%. |
| `reported_free_bytes` | integer or `null` | See [null semantics](#null-semantics) below. |
| `reported_total_bytes` | integer or `null` | Same caveat as `reported_free_bytes`. |
| `free_bytes_reported_at` | string or `null` | Timestamp of the last storage report; `null` alongside the two fields above. |
| `created_at` | string | |
| `last_seen_at` | string or `null` | |
| `source_of_truth` | string | `"server"` or `"device"` — which side's manifest wins on a mismatch. |
| `transcode_format` | string or `null` | `null` means **sync originals**, not "unset". Otherwise one of `mp3_320`, `mp3_256`, `mp3_192`, `mp3_128`. |
| `artist_images` | string or `null` | `null` means off. Otherwise `"small"` or `"full"`. |
| `unknown_track_count` | integer or `null` | `null` ≠ `0` — see below. |
| `autofit` | object | Shape depends on `enabled` — see below. |
| `sync_status` | object | `{"pending_count": integer, "last_synced_at": string or null}`. |

Timestamps are the server's own format — SQLite's `datetime('now')`, i.e.
`YYYY-MM-DD HH:MM:SS`, always UTC but without a `T` separator or an
explicit offset. Attach UTC yourself rather than assuming your parser
infers it.

### `autofit`'s two shapes

```json
{"enabled": false, "percent": 100}
```

```json
{
  "enabled": true,
  "percent": 80,
  "period": "6_months",
  "albums": 42,
  "tracks": 511,
  "bytes": 2147483648
}
```

`percent`, `period`, `albums`, `tracks`, and `bytes` only mean something
when `enabled` is `true` — while disabled, the object is just the two
fields above, not the full shape with zeros filled in.

### Null semantics

Three genuinely different things, easy to collapse into one "missing data"
bucket by mistake:

- **`sync_status.last_synced_at: null`** — enrolled but never synced. Not
  an error; treat it as "never," a real answer.
- **`reported_free_bytes` / `reported_total_bytes` / `free_bytes_reported_at`
 all `null`, permanently, on some devices** — the Garmin watch client only
 ever calls `/api/device/changes`, `/api/device/file`, and
 `/api/device/ack`; it never calls `/api/device/storage`, so a watch
  reports no storage data at all, ever. Treat this as **unavailable by
  design**, not as data that hasn't arrived yet — a consumer that waits
  for it will wait forever.
- **`unknown_track_count: null`** — the count only exists once a device
  has completed the re-enrollment manifest handshake, in which it uploads
  a list of what's actually on it. The desktop and Android clients both do
  this, but only on the first sync after (re-)enrolling into a folder that
  already holds a library; the Garmin client never does, and a device
 enrolled into an empty folder never has cause to. `null` here means
  "never uploaded one," not zero unknown tracks.

!!! warning "On a Garmin watch, two of these three are permanent — not pending"
 A consumer that reads `null` as "hasn't arrived yet" and keeps polling
 will wait forever: the watch never calls `/api/device/storage`, and never
 uploads a manifest. Only `last_synced_at` fills itself in, once the device
    syncs.

## Server metrics

```
GET /api/integrations/server
Authorization: Bearer <token>
```

```sh
curl -H "Authorization: Bearer <YOUR_TOKEN>" \
  https://trobar.example.com/api/integrations/server
```

Same errors and rate limiting as [Devices](#devices) above — one shared
credential type and one shared failure bucket, not a separate contract per
route.

**Not `/api/library/stats`, and deliberately narrower than it.** That
endpoint reads every non-deleted track's full row to aggregate codec and
decade breakdowns in Python — fine for a dashboard opened occasionally, not
for something polled every few minutes indefinitely. This endpoint answers
only what a cheap, indexable aggregate can: no `by_codec`, no `by_decade` —
open the web UI's Home dashboard for those.

A single JSON object, one snapshot of the whole instance — not scoped to
the calling token's owner:

| Field | Type | Notes |
|---|---|---|
| `version` | string | Same value as `GET /api/about`. |
| `track_count` | integer | Non-deleted tracks, library-wide. |
| `total_bytes` | integer | Sum of `size` over the same set. `0` on an empty library, never `null`. |
| `scan_running` | boolean | A library scan is queued or actively running right now. |
| `last_scan_at` | string or `null` | When the most recent scan (success or failure) finished. `null` before the first scan ever runs, and while one is in progress — see below. |

No `online` field: a response arriving at all *is* that signal. An
unreachable server can't answer this request to say it's unreachable, so a
field that could only ever read `true` would be noise, not information —
infer reachability from whether the poll itself succeeded, the same way
 does.

`last_scan_at` follows the same [null semantics](#null-semantics) pattern
as the devices endpoint's own timestamps: `null` here means "no completed
scan to report on right now," not "unknown" — while `scan_running` is
`true`, `last_scan_at` reports `null` even if a previous scan finished
earlier, so a client can't mistake an in-progress scan for a stale
"finished" timestamp.

## Mirror health

```
GET /api/integrations/mirrors
Authorization: Bearer <token>
```

```sh
curl -H "Authorization: Bearer <YOUR_TOKEN>" \
  https://trobar.example.com/api/integrations/mirrors
```

Same errors and rate limiting as [Devices](#devices) above.

Filled a gap the other three routes didn't cover: playlist mirroring
(filesystem, Subsonic, Jellyfin, Emby) is unattended background
work with a documented, per-sink failure state
(`unset_target`/`unreachable`/`no_target_matches`/`write_failed`, or
filesystem's own `unset_folder`/`not_writable`/`bad_filename`/
`marker_unsafe`), but that state was previously only visible in the
session-authenticated web UI (`GET /api/provider/playlists`, and
**Administration → Mirrors** for admins). Device sync already had this
kind of surface — this is the mirrors equivalent.

A single JSON object, instance-wide like [Server metrics](#server-metrics)
above, not scoped to the calling token's owner:

| Field | Type | Notes |
|---|---|---|
| `mirrors_failing` | integer | Total currently-failing playlist × sink pairs, across every sink. Exact — never affected by `failing`'s cap below. The simplest possible alert: `> 0`. |
| `by_sink` | object | One entry per sink — `filesystem`, `subsonic`, `jellyfin`, `emby` — each `{"enabled": integer, "failing": integer}`. Counts playlist × sink **pairs**, not playlists: a playlist mirrored to both Subsonic and Jellyfin counts once in each. `by_sink.*.failing` summed across all four sinks always equals `mirrors_failing`. |
| `failing` | array | One entry per currently-failing pair — a worklist, not an inventory, capped at 50 entries (see `failing_truncated`). Each entry: `{"playlist_id": integer, "title": string, "sink": string, "error_code": string, "last_written_at": string or null}`. |
| `failing_truncated` | boolean | `true` if more pairs are failing than `failing` has room for — `mirrors_failing`/`by_sink` are still exact in that case, only the array is short. A single dead mirror target can fail every playlist pointed at it, so this can happen on a real install, not just synthetically. |

`error_code` is the raw machine-readable code, the same one
`GET /api/admin/mirrors` exposes alongside its own rendered message — this
endpoint deliberately omits the message (these codes are
language-independent so a client renders its own strings rather than
displaying server-side English). `unset_target` counts as failing like
any other code: a mirror target cleared out from under playlists still
enabled against it is exactly the silent-drift case this endpoint exists
to surface.

## Actions

```
POST /api/integrations/actions/scan
Authorization: Bearer <token>
Content-Type: application/json

{"force": false}
```

```sh
curl -X POST -H "Authorization: Bearer <YOUR_TOKEN>" \
  https://trobar.example.com/api/integrations/actions/scan
```

**The same token as [Devices](#devices), [Server metrics](#server-metrics),
and [Mirror health](#mirror-health)** — see
[What it is — and isn't](#what-it-is-and-isnt) above for why one
admin-minted credential covers all four routes rather than a separate
write-capable token.

Triggers the same background library scan as the web UI's own "Scan
library" button (`POST /api/library/scan`), and answers with the identical
shape:

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status": "started", "job_id": <int>}` | Scan started in the background — poll `scan_running`/`last_scan_at` on [`/api/integrations/server`](#server-metrics) for completion. |
| `409` | `{"error": "..."}` | A scan is already running — this call did not start a second one. |

`force` (optional, default `false`) is read from the JSON body: `false`
skips files whose mtime hasn't changed since the last scan, `true`
re-reads everything. Same meaning as the web UI's own force-rescan option.

There is currently no other action — provider-playlist refresh was
considered and deliberately left out of scope; ask if
you need that and it still doesn't exist.

## Rate limiting

**30 failed authentication attempts per 5 minutes, per IP**, in its own
bucket, shared by all four routes above — separate from the login rate
limiter, so a misconfigured integration retrying a stale token can
neither trip nor be tripped by a brute-force login lockout. Only failures
count against it; a stream of successful polls never approaches the
limit. Every **successful** request also updates the token's
`last_used_at`, shown next to it in **Profile → Integrations** — so a
working poller shows up there, and a lockout after 30 failures tells you
the token needs replacing, not that this endpoint is flaky.

## Recipes

Sync itself is slow-moving — the Android client syncs on a **6-hour**
period — so polling every 30 seconds fetches an identical payload hundreds
of times between real changes. **A few minutes is plenty**; trobar-ha
itself polls every 5 minutes.

### Home Assistant, native

The turnkey path: [trobar-ha](https://github.com/missing-foss/trobar-ha),
covered on its own page — see
[Home Assistant Integration](home-assistant.md).

### Home Assistant, no custom component

Point HA's built-in [`rest` sensor](https://www.home-assistant.io/integrations/rest/)
at this endpoint directly — no installation, at the cost of one sensor
definition per value you want and no device registry grouping. This is
also **Phase 0** of the integration's design: the zero-install fallback
for anyone who'd rather not add a custom repository.

```yaml
# configuration.yaml
sensor:
  - platform: rest
    name: "Trobar living room phone — pending tracks"
    resource: https://trobar.example.com/api/integrations/devices
    method: GET
    headers:
      Authorization: !secret trobar_api_token   # store as "Bearer <token>"
    value_template: >-
      {{ (value_json | selectattr('id', 'equalto', 1) | first).sync_status.pending_count }}
    scan_interval: 300   # 5 minutes -- see the note above on poll frequency
```

Store the full header value in `secrets.yaml`, not just the token:

```yaml
# secrets.yaml
trobar_api_token: "Bearer <YOUR_TOKEN>"
```

[Mirror health](#mirror-health) is a flat object rather than an array, so
its `value_template` skips the `selectattr` filter entirely:

```yaml
# configuration.yaml
sensor:
  - platform: rest
    name: "Trobar — mirrors failing"
    resource: https://trobar.example.com/api/integrations/mirrors
    method: GET
    headers:
      Authorization: !secret trobar_api_token
    value_template: "{{ value_json.mirrors_failing }}"
    json_attributes:
      - by_sink
      - failing
    scan_interval: 300
```

`json_attributes` carries `by_sink` and `failing` onto the sensor's own
attributes — HA's sensor *state* is length-limited, but attributes hold
structured data fine, so an automation can alert on the bare count while
a dashboard card still shows which sink and playlist.

### Grafana

Via the [Infinity datasource](https://grafana.github.io/grafana-infinity-datasource/)
(JSON, no plugin-specific server-side code needed):

1. Add an Infinity data source, Auth type **Bearer Token**, paste the raw
 token (no `Bearer ` prefix — Infinity adds it).
2. New panel → query type **JSON**, URL
 `https://trobar.example.com/api/integrations/devices`, Parser
   **Backend**, root selector empty (the response is already the array).
3. Columns: `name` (string), `reported_free_bytes` (number),
 `reported_total_bytes` (number) — a bar gauge over
 `reported_free_bytes` grouped by `name` gives free space per device in
   one panel.
4. Set the panel/dashboard refresh interval to a few minutes, matching the
   note above — Infinity re-polls on every refresh, same as any other
   data source.

### Shell / uptime monitors

```sh
#!/bin/sh
# Alert if any device has tracks still pending sync.
resp=$(curl -sf -H "Authorization: Bearer $TROBAR_TOKEN" \
  https://trobar.example.com/api/integrations/devices) || exit 1

stuck=$(echo "$resp" | jq '[.[] | select(.sync_status.pending_count > 0)] | length')
if [ "$stuck" -gt 0 ]; then
  echo "ALERT: $stuck device(s) with tracks still pending"
  exit 1
fi
```

Run this every few minutes via cron, or point an uptime monitor with a
JSON-body-assertion feature (Uptime Kuma's "Json Query" monitor type, for
one) directly at the same `jq` expression instead of a separate script.

`mirrors_failing` is built for exactly this shape of check — no `jq`
filter needed, just the field:

```sh
#!/bin/sh
# Alert if any playlist mirror is currently failing.
resp=$(curl -sf -H "Authorization: Bearer $TROBAR_TOKEN" \
  https://trobar.example.com/api/integrations/mirrors) || exit 1

failing=$(echo "$resp" | jq '.mirrors_failing')
if [ "$failing" -gt 0 ]; then
  echo "ALERT: $failing mirror(s) failing — $(echo "$resp" | jq -c '.by_sink')"
  exit 1
fi
```

## See also

- [Devices & Storage](../using/devices.md) — the same fields, from the web
  UI's perspective.
- [Home Assistant Integration](home-assistant.md) — the native client for
  this API.
- [Architecture & Sync Protocol](architecture.md#integration-api)
  — how this token fits alongside the others.
