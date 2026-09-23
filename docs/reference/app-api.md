<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# App API

The routes the Android app's own screens use — library browsing, the
basket, the Home dashboard — so that everything a browser can do on the
road can be done from the phone without one. Every route lives under
`/api/app/` and is called with the **device token the phone already holds
from pairing**, acting as the device's **owner**.

Not in any release as of **2.14.0**; it is on the development branch and
ships with the next release cut after it. A client does not check the
server version: it checks the **`app_api` level** (below), which is what
the server advertises and what a breaking change bumps.

See [Architecture & Sync Protocol](architecture.md#app-api) for how this
credential use fits beside the browser session, the per-device sync
token and the integration token.

## Authentication and who you act as

```
GET /api/app/whoami
Authorization: Bearer <device token>
```

- The token is the device's own, the same one `/api/device/*` takes. A
  wrong or revoked token is `401` and counts against the device-token
  rate-limit bucket for the caller's IP, the same one the sync routes use.
- Every route resolves the token to the device, then to the device's
  **owner**, and acts as that account: what the owner could do from a
  browser — stage to their own and their pinned devices, see the
  selections they can see, read their own preferences — the phone can do,
  and nothing else. A pinned (delegated) device therefore acts as its
  **owner**, not as the person it is pinned to.
- **Admin is re-checked on every request** from the owner's current flag.
  Admin-only routes answer `403` to a non-admin owner; demoting the owner
  takes effect on the next call.
- The prefix is exempt from the browser login gate and from the
  cross-site mutation guard, which is why every route authenticates
  before it does anything at all — including before validating input, so
  an anonymous caller learns nothing from a `400`.

`whoami` returns:

```json
{"user_id": 1, "username": "alice", "is_admin": false,
 "device_id": 3, "device_name": "Phone", "app_api": 1}
```

## Versioning: the `app_api` level

`GET /api/device/info` carries `"app_api": <integer>` beside the fields it
always had. It is the level of this contract the server speaks —
**1** today. A server from before the App API sends no such field.

The rule for a client: read it as an integer and as nothing else — absent,
`0` or anything that is not a number means *no App API*, and the screens
that need it show an "update the server" state rather than an empty list.
The Android app does exactly this; the sync screens keep working against
any server, since they never touch this prefix.

The rule for the server: **a breaking change to any route under the
prefix bumps the level.** Adding a route or a field is not breaking;
removing or renaming one, or changing a field's type or meaning, is. A
client written against level *n* refuses a server below *n* and works on
any server at or above it.

The consequence of reserving bumps for breaking changes: **a client
cannot tell from the level whether a route added later exists** on the
level-*n* server in front of it — the answer is a `404` at call time. So
the client's rule is two rules: refuse a server below its level, and
treat a `404` on a route it can live without as *not here*, never as an
error to surface.

## Library

| Route | Returns |
|---|---|
| `GET /api/app/library/artists` | `[{artist, track_count, album_count}]`, by name |
| `GET /api/app/library/albums?artist=` | `[{album, year, reissue_year, track_count}]`, newest first; the two years are `null` when no track carries one |
| `GET /api/app/library/similar-artists?artist=` | `["Name", …]` — artists the similarity source names that are also in this library, up to eight; `[]` when the source is not configured or nothing matches |
| `GET /api/app/library/cover?artist=&album=` | the album's cover image bytes (`Content-Type` is the image's); `404` when the album has none |
| `GET /api/app/library/artist-image?artist=[&size=small]` | the artist picture; `size=small` is the phone-grid variant; `404` when none is available |

Same reads as the browser's library, behind the device token. Image
responses carry `Cache-Control: public, max-age=86400`, and a `404` is
the normal outcome for many artists, not an error to surface.

## The basket and the staging loop

The basket is **server-side and per account**: the phone and the browser
see one basket. Staging is choosing a destination — an item always names
at least one device — and *sending* (fan-out) turns the basket's sections
for the chosen devices into selections, which the next sync then acts on.

| Route | Does |
|---|---|
| `GET /api/app/basket` | the items, in staging order (shape below) |
| `POST /api/app/basket` `{type, target, device_ids}` | stages one target; returns `{"id"}`. `400` with no devices; `403` for a device the owner may not use |
| `DELETE /api/app/basket/<id>` | removes one item — the owner's own; another account's id is a no-op |
| `DELETE /api/app/basket/<id>/devices/<device_id>` | takes one device off an item; the item goes with its last device |
| `DELETE /api/app/basket` | clears the owner's basket |
| `POST /api/app/basket/fan-out` `{device_ids}` | creates selections from the items staged for those devices; returns `{"status": "ok", "count", "skipped"}`. Items staged only for other devices stay. `400` with no devices |
| `PATCH /api/app/basket/last-destination` `{surface, device_ids}` | remembers the devices last chosen from one surface of the app, so its picker can preselect them; the browser keeps its own surfaces |

A basket item:

```json
{"id": 5, "type": "album", "target": "Alpha||X", "added_at": "2026-09-01 10:00:00",
 "device_ids": [1, 2], "title": "X", "source_provider": null, "missing": false}
```

`type` is one of `artist`, `album`, `playlist`, `track`; `target` is the
server's own key for it (`Artist||Album` for an album, an id for a
playlist or track) and goes back to the server unchanged. `title` is
resolved for display; for a playlist or track whose target no longer
exists it is `null` and `missing` is `true`.

`fan-out` evaluates visibility against the acting owner, per item: an item
staged for two devices, sent to one, is created for that one and stays in
the basket for the other. `skipped` counts rows the server could not act
on (a type or target that predates today's validation) — reported rather
than failing the whole send.

## Selections and devices

| Route | Returns |
|---|---|
| `GET /api/app/selections` | the selections the owner can see: their own, plus any linked to a device they own or have pinned; every selection for an admin |
| `GET /api/app/devices` | the devices the owner may stage to, each with `sync_status` and `autofit` — the same shape as the browser's device list, `is_own`/`is_pinned` as real booleans |
| `GET /api/app/devices/<id>/usage` | the device's usage; `403` for a device the owner may not use |

**One name, two shapes.** `device_ids` is a JSON list on every basket
route and a **comma-joined string** on `/api/app/selections` —
`"device_ids": "1,2"`, and `null` for a selection with no device link.
That is the shape the browser's own code splits; a client must accept
both, and the selections route and the browser change together or not at
all.

There is **no `POST /api/app/selections`**. The browser can create a
selection directly; the phone stages through the basket and sends. The
asymmetry is deliberate: one write path on the phone, and it is the one
that always names its devices.

Usage:

```json
{"used_bytes": 900, "max_size_bytes": 1000, "track_count": 2,
 "over_limit": false, "folder_over_limit": true,
 "reported_free_bytes": 100, "reported_total_bytes": 2000, "device_used_bytes": 1900,
 "reported_foreign_bytes": 300, "folder_used_bytes": 1200,
 "free_bytes_reported_at": "…", "foreign_bytes_reported_at": "…",
 "limit_exceeds_physical_capacity": false}
```

`used_bytes` is Trobar's own share; every `reported_*` figure is what the
device last sent and is **`null` when it never has** — a folder nobody
measured is not an empty folder, and the two `*_reported_at` timestamps
say when a figure was true.

## The owner's display preferences

| Route | Returns |
|---|---|
| `GET /api/app/profile` | `{"cover_view_mode": "list" \| "grid", "show_reissue_year": bool, "basket_last_destinations": {surface: [device ids]}}` |

The three things the phone's screens follow from the profile and nothing
else — no usernames, no keys. `basket_last_destinations` is what
`PATCH /api/app/basket/last-destination` stores, keyed by surface, so a
picker can preselect the devices last chosen from that surface; the
browser's surfaces are in the same map and are the browser's to read.
There is no whole-profile write on this prefix (see below).

## The Home dashboard

| Route | Returns |
|---|---|
| `GET /api/app/dashboard/catalog` | `[{id, admin_only}]` — the widget catalog in default order, the same list the web client renders from |
| `GET /api/app/dashboard/widgets` | the owner's preferences: `{disabled, order, settings}` |
| `PATCH /api/app/dashboard/widgets` | the narrow write: `disabled` and `order` replace when present, `settings` merges key by key; returns the stored, normalized preferences. Malformed input is `400` with the field named |
| `GET /api/app/library/stats` | `{total_tracks, total_duration_seconds, by_codec: {ext: {tracks, bytes}}, by_decade: {"1990s": n, …}}` |
| `GET /api/app/library/recently-added?months=` | `[{artist, album, added_at, library_artist, library_album, image_url, source}]` |
| `GET /api/app/library/recently-released?months=` | as above with `released_at` |
| `GET /api/app/suggestions?period=` | `{"configured": bool, "items": [...]}` |
| `GET /api/app/suggestions/most-played?period=&limit=` | `{"configured": bool, "items": [{artist, album, playcount, image_url, source}]}`, ranked by playcount, `limit` 1–50 (default 8) |
| `GET /api/app/admin/counts` | `{"users", "delegations"}` — `403` for a non-admin owner |

The **catalog** is served, not embedded, so the phone is not a third
hand-synced copy of the widget list: it governs order, the admin-only
flag and how an unknown id is handled. A client still needs rendering
code per id, and should skip an id it does not know rather than fail.

**Which widgets to show** is decided by the catalog's `admin_only` flag
against `is_admin` from `whoami`, then by the stored `disabled` list —
in that order. The stored list is not the source of truth for the
admin-only widget: a write always adds it to a non-admin's list, but a
read does not, so an owner demoted after their last write still has it
absent from `disabled`. Filter on the flag and the client is right
whether or not it ever writes.

**Settings**: `cover_limit` is one of `15, 30, 45, 60`; `recently_added`
and `recently_released` each take `{"months": n}` with `n` from 1 to 24.
Anything else under `settings` is `400` on the PATCH.

**`configured`** on the two feeds says whether the owner has a Last.fm or
ListenBrainz username; without one, `suggestions` still returns the
library's own recently-added candidates and `most-played` is empty. The
browser's session routes return the bare list; the envelope is the App
API's, so an empty list never has to mean two different things. `period`
is one of `overall, 7day, 1month, 3month, 6month, 12month`; anything else
reads as `6month`.

The administration numbers are **counts only**. The full user and
delegation listings stay behind the admin routes and do not reach a
phone.

## What the browser has that the phone does not

Listed so a client author does not assume them away:

- `POST /api/selections` — the phone stages and sends instead.
- The whole-profile `PUT /api/profile` — the phone reads three display
  preferences and has the narrow widget-preferences PATCH only, so a
  phone toggling a widget can never overwrite a scrobble username the
  browser just saved.
- The admin listings — counts only, above.
- The session routes' `device_ids` shape on selections is a string; the
  phone must accept both shapes, above.
