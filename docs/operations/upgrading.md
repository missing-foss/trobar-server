<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Upgrading

```bash
git pull && docker compose up -d --build
```

Schema migrations run **automatically at startup** — there are no manual
migration steps in the normal case.

!!! tip "Back up `DATA_DIR` first"
    A backup taken *before* upgrading is your rollback point — see
    [Backups & DATA_DIR](backups.md).

Sections below that need something from you say so up front; a section with no
such flag needs nothing.

## Upgrading from a pre-10001 (root) image

!!! warning "Action needed: a one-time `chown`, or the app won't start"
    Only if you're coming from an image old enough to have run as root.

Older images ran as root, so an existing `DATA_DIR` created by them is owned by
root. The current image runs as the unprivileged **uid 10001** and can no longer
write it — on start you'll see:

```
PermissionError: '/data/flask_secret_key'
```

Fix it once:

```bash
docker compose down
sudo chown -R 10001:10001 ./data          # bind mount (reference compose default)
# named volume instead:
#   docker run --rm -v <volume>:/data alpine chown -R 10001:10001 /data
docker compose up -d --build
```

## Client compatibility

The server, Android app, desktop app, and Garmin watch app are released
independently and tagged separately (`vX.Y.Z`, `vX.Y.Z`,
`vX.Y.Z`, `vX.Y.Z`) — see
[Release Cycle & Versioning](../project/releases.md). The sync API is
backward-compatible within a major line, so a slightly older client keeps
working against a newer server; update clients at your own pace. The Garmin
app has no in-watch auto-update (no Connect IQ Store listing yet) — updating
means sideloading a newer `.prg` the same way as the initial install.

## Fingerprints need a scan (2.4.x)

!!! warning "Action needed: run a library scan"
    Nothing here happens on upgrade alone, and device recovery has nothing to
    match against until it does.

Upgrading does not populate audio fingerprints on its own. The pass that computes
them is queued when a **library scan completes**, so an existing install needs a
scan before device recovery has anything to match against — check
**Administration → Background jobs** for `provenance_library_fingerprints`.

Two things gate it, deliberately:

- **At least one enrolled device.** Fingerprints exist to re-identify a device's
  files after a server rebuild or a re-tag, so an install that syncs nothing skips
  the work rather than spending hours of CPU on it.
- **An AcoustID key is not required.** Computing a fingerprint is local. The key
  only adds the lookup that recovers an ISRC for poorly-tagged tracks, and that is
  the only part which talks to the internet.

On a large library the pass is hours of audio decode. It runs in a separate queue
lane, so device syncs stay responsive while it works, and it reports live progress.

## Integration tokens minted by non-admins are revoked (2.9.0)

!!! warning "Action needed: an admin must mint a replacement token"
 Only if a token in use was created by a non-admin — those return `401`
    after this upgrade. Tokens already minted by an admin keep working.

Integration tokens (**Profile → Integrations**) can now only be created
by an admin, and only an admin may hold one. Any token created by a
non-admin user on 2.8.0/2.8.1 is revoked during the upgrade and will
return `401`.

If a Home Assistant instance (or any other integration) stops
authenticating after this upgrade, an **admin** must mint a replacement
token and update the integration's configuration. Tokens that were
already created by an admin keep working and need no action.

## An integration token's owner is re-checked on every use, not just at minting (2.10.0)

Demoting an admin to a regular user now immediately revokes any
integration token (**Profile → Integrations**) they minted while an
admin — previously the mint-time admin check was never re-verified, so
the token kept working indefinitely after demotion.

If a Home Assistant instance (or other integration) starts returning
`401` right after you demote its owner, this is why: mint a fresh token
with a still-admin account and update the integration's configuration.
No action needed otherwise.

## Newly-synced playlists from a personal account now start private, not shared (2.10.0)

!!! warning "Action needed: the owner must click **Shared**"
    Only if your household relies on a per-user link (Tidal, Spotify, a Roon
    profile, a Jellyfin/Emby mapping) reaching everyone automatically. From
    now on those playlists start private, and **nothing does this for you**.

A playlist synced from something personal to one household member — a
linked Tidal or Spotify account, a Roon profile mapping, or the new
per-user Jellyfin/Emby mapping below — now starts **private** (visible
only to its owner and the admin) the first time it's synced, instead of
shared with the whole household by default.

This only affects newly-synced playlists going forward; anything already
synced before the upgrade keeps whatever sharing state it already has —
upgrading does not retroactively privatize your library. If your
household already relies on one of these per-user links and expects
those playlists to reach everyone automatically, the owner needs to
click **Shared** on the playlist row (Playlists tab) after upgrading —
nothing does this for them.

## New: mirror playlists to Subsonic/Navidrome, Jellyfin, or Emby (2.10.0)

Three new mirror-target sinks join the existing folder mirror — see
[Playlists → Mirroring](../using/playlists.md#mirroring-to-a-local-folder-a-subsonicnavidrome-server-a-jellyfin-server-or-an-emby-server)
for what each does and its known quirks (notably: Emby reverts the
mirrored playlist's comment field a few seconds after every write —
its own behavior, outside Trobar's control, and harmless — the name and
track list are unaffected). All are opt-in: nothing mirrors anywhere
until an admin sets a target's URL/credentials in **Administration →
Configuration**, so upgrading changes nothing on its own.

Also new: mapping individual Trobar users to their own Jellyfin or Emby
account (**Administration → Configuration**), so their personal
playlists sync in alongside the household's — same mechanism the
existing Roon profile mapping already used. See the sharing-default
note above for what happens to those playlists' visibility.

## New: request missing albums from Lidarr (2.11.0)

A different kind of sink from the mirrors above: instead of copying a
playlist somewhere, "Request missing albums…" (Playlists tab) asks a
[Lidarr](https://lidarr.audio/) instance to start watching for an album
your library doesn't have — monitor-only, Lidarr's own scheduled search
finds it later. See
[Playlists → Requesting missing albums from Lidarr](../using/playlists.md#requesting-missing-albums-from-lidarr)
for the full behavior, including the "requested once, ever, not per
playlist" dedup and why the button stays disabled on Roon/iTunes
playlists.

Opt-in and inert until an admin completes a two-phase setup in
**Administration → Configuration**: connect a URL/API key, then choose
a root folder/quality profile/metadata profile from Lidarr's own live
lists — so upgrading changes nothing on its own. Once configured, any
household member can toggle it on a playlist they can see, same as the
mirror buttons — and, deliberately, nothing in Trobar can undo a
request already sent; un-toggling only stops *future* ones. If that's
not a household policy you want, leave it unconfigured.

## The device picker replaced the per-row buttons (2.12.0)

Adding a playlist or album to a device is now one button everywhere:
pick the device in the dialog, then either keep browsing or send
straight away. The basket groups what you've staged by device, so you
can queue different things for different devices in one pass. No
action needed — existing selections and devices are unaffected.

## New: mirror health is visible to integrations (2.12.0)

`GET /api/integrations/mirrors` reports how many playlist mirrors are
currently failing, broken down by sink, so a monitoring integration
can alert on a mirror target that has gone away instead of it being
noticed only in **Administration → Playlist mirrors**. See
[the integration API reference](../reference/integration-api.md#mirror-health).
No action needed.

## Anything staged in your basket is cleared by this upgrade (2.12.0)

!!! warning "Action needed: re-add anything you had staged"
    Items staged before upgrading have no device attached and are removed.
    Selections already sent to a device, and the devices themselves, are
    untouched.

The basket now remembers which device each item is staged for, so you
can queue different things for different devices in one pass instead
of two separate basket sessions. Items staged **before** upgrading have
no device attached and are removed by this upgrade — re-add anything
you had queued. Nothing else is affected: selections already sent to a
device, and the devices themselves, are untouched.

## The four mirror buttons collapsed into one picker (2.13.0)

A playlist row's "Mirror to…" / "Mirror to Subsonic…" / "Mirror to
Jellyfin…" / "Mirror to Emby…" buttons are now one **"Mirror…"** button
that opens a picker listing only the sinks an admin has configured.
Already-mirrored sinks show greyed with a checkmark — click again to
turn one off. A playlist mirrored somewhere now shows an icon for each
active sink next to its title instead of a same-row action button; the
icon turns red if that sink's last write failed. No action needed —
every existing mirror keeps mirroring exactly as before, this only
changes how you turn one on or off.
