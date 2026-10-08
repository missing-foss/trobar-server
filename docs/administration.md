<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The Administration panel

**Profile → Administration.** Only the `ADMIN_USERNAME` account sees it;
everything else in the app (own profile, own devices, own selections) is
per-user and needs no admin.

This page is the admin-panel reference. Related admin-managed topics have their
own pages: [Devices & Storage](using/devices.md),
[Delegated Management](using/delegation.md), and the per-provider setup under
[Providers](providers/index.md).

## Configuration

The **Configuration** tab is grouped into collapsible sections. Each header
names its section and summarises it in one line (the active provider and its
state, how many playlist sources are set up, and so on), so with every section
closed the page is still an overview. On a first visit only **Library** is
open. After that, which sections are open is remembered in this browser.
**Open all** and **Close all** sit above the sections.

- A **warning** shows on its section's header whether the section is open or
  closed, and the full message stays inside the section: a library mounted
  writable, a disconnected provider or mirror target, Lidarr connected without
  its profiles chosen, a missing break-glass password.
- A section with an edit not yet saved says **Unsaved change** on its header,
  and the bar under the page lists the sections that have one.
- **Save** stays at the bottom of the page whatever is closed, and saves every
  section but **Security**, whose break-glass password has its own button. When
  the server rejects a setting, the section holding it opens and the field is
  focused, with the server's message beside **Save**.

| Section | What's in it |
|---|---|
| Library | The library source, automatic rescan, artist images (TheAudioDB) |
| Playlist sources | Local playlist sources, Music Assistant, Tidal, Spotify |
| Playlists | Playlist track matching (AcoustID), playlist mirroring, Lidarr: missing-album requests |
| Listening | The default Last.fm key, listening history sources |
| Devices | Transcoding (CPU controls) |
| Maintenance | Background job history |
| Security | The sign-in mode, and under OIDC the break-glass admin password |

### Library

#### Provider connection

The card shows the active provider (Roon / Jellyfin / Emby / Plex / LMS /
Subsonic / Filesystem), its status, and its connection details, which are
editable here, **live — no container restart**. Changing the Roon host/port
re-pairs immediately, reusing the existing pairing token. A new host or a
rotated key is not a switch: it saves with the page's **Save**.
Per-provider details: [Providers](providers/index.md).

#### Changing the library source

Switching to a different provider goes through **Change library source…**,
never through **Save**. It's a dialog in four steps, and nothing is written
before the last one: **Cancel** at any step leaves everything as it was.

1. **Choose** the new provider. The active one isn't offered.
2. **Connect**: enter its connection details and **Test connection**. You
   can't go on until the test passes. Filesystem needs nothing. Roon can only
   be checked as reachable at this point: after the switch, approve Trobar in
   Roon (Settings → Extensions).
3. **Review and confirm**: what the switch does, counted on your server at
   that moment:
    - **Carried over**: each of the old provider's playlists that the new
      provider also has, under the same name (case and spacing aside), for
      the same owner, with most of the same tracks: at least 3 in common,
      and at least 60% of the smaller of the two. It keeps its row, so its
      device selections, mirroring, Lidarr requests, shared/private choice
      and excluded gaps stay, and its tracks are refreshed from the new
      provider on the first sync. Two playlists with one name and owner on
      either side are left unmatched rather than guessed. The switch runs
      the same matching against the new provider as it is then.
      Nothing is carried over to Roon, which can't list its playlists until
      Trobar is approved in Roon, nor to Filesystem or Music Assistant, whose
      playlists are already in Trobar as playlists of their own (both are
      listed under every provider).
    - **Removed**: the old provider's other playlists, with their per-playlist
      settings (mirroring, the shared/private choice, excluded gaps). A
      mirrored playlist's mirror copies are removed with it. Also the
      artist-image cache, which is fetched again from the new provider.
    - **Devices** with one of those playlists selected: the selection is
      removed with the playlist, and the device's next sync removes that
      playlist's file and any tracks no other selection keeps on it. Select
      the new provider's playlists for it afterwards.
    - **Untouched**: playlists from every other source (linked Tidal and
      Spotify accounts, YouTube Music and Spotify playlist links, the `.m3u` folder and
      iTunes, Music Assistant) with all their settings, and the library,
      users, devices, artist/album/track selections and listening history.
      Switching away from Filesystem removes nothing: its `.m3u` folder is
      read whichever provider is active.

   The confirm button names the target, e.g. **Switch to Jellyfin**.
4. **Done**: the switch re-tests the connection before changing anything,
   then starts the first playlist sync from the new provider and shows its
   result. A switch is refused while a playlist sync is already running.

First-run setup keeps its own provider picker: there is nothing to lose yet.

#### API keys

Two keys live in different sections:

- **Default Last.fm API key** (**Listening → Listening history**) — used for suggestions and listening stats for any
  user who hasn't set a personal key in their own profile.
- **TheAudioDB API key** (**Library → Artist images**) — when set, artist pictures are fetched
  from [TheAudioDB](https://www.theaudiodb.com) (whose API terms are written for
  exactly this use) instead of the active provider, with the provider and an
 `artist.jpg`-style folder image as fallbacks. Get a free key from their site;
  entering or changing it clears the image cache so everything re-fetches. Leave
  it empty to keep provider-sourced images — fine for private use, but recommended
  to set for anything public-facing, since provider imagery (Roon especially) is
  licensed for display inside that provider's own products.


### Playlist sources

**Local playlist sources** is the one exception to "one provider at a time".
Both fields there are layered on top of the `.m3u`/`.m3u8` discovery every
provider already gets, and both are editable and effective regardless of
which provider is active:

- **Extra playlist folder** — one folder outside your music library, walked
  for `.m3u`/`.m3u8` exactly as the library is, for players that keep their
  playlists in a directory of their own. Read-only; it must not overlap
  your music folder or the mirror output folder. See
  [Filesystem](providers/filesystem.md#playlists-your-player-keeps-somewhere-else).
- **iTunes/Apple Music Library.xml path** — an exported library file. See
  [iTunes / Apple Music](providers/itunes.md).

**Music Assistant** has one card for all it does: enter its URL and a
long-lived token, and its playlists are added to the next sync whichever
provider is active. The same connection is used when Music Assistant is the
library provider (the Library source card then links here rather than
repeating the fields) and when a playlist is mirrored into it. Clear both
fields and save to disconnect. See
[Music Assistant](providers/music-assistant.md).

#### Streaming accounts (OAuth registration)

Personal streaming accounts are linked per-user, but the admin registers each
OAuth app **once** here under **Configuration** so those logins have something to
authenticate against:

- **[Tidal](providers/tidal.md)** — client ID/secret; three developer-console
  permissions must be enabled or the login fails at Tidal's own screen.
- **[Spotify](providers/spotify.md)** — client ID/secret (**validation
  pending**; Dev-Mode Premium + 5-user limits apply).

Each household member then connects their own account from **Profile → Streaming
accounts**.

### Playlists

- **Playlist track matching**: an optional AcoustID API key. With it, each
  track's fingerprint is also sent to acoustid.org, and MusicBrainz is asked,
  to recover an ISRC for poorly tagged tracks, which playlist entries can then
  match on. Without it nothing here goes to the internet.
- **Playlist mirroring**: the targets a playlist can be mirrored to (an
  `.m3u` output folder, and Subsonic, Jellyfin, Emby and Plex servers; Music
  Assistant mirrors through its own card's connection). Each user
  turns mirroring on per playlist, with **Mirror…** on the Playlists tab.
- **Lidarr: missing-album requests**: the Lidarr connection, and then its root
  folder, quality profile and metadata profile. It is not a mirror target:
  Lidarr is asked for albums missing from a playlist.

### Listening

#### Listening history sources

Suggestions (top-played / recently-played) read from Last.fm, ListenBrainz
and/or Maloja — **read-only**, nothing here ever submits a scrobble. Maloja is
set per user, on their Profile (see [Suggestions](using/suggestions.md#maloja)),
so any user can point the server at an address on your LAN; loopback,
link-local, unspecified and multicast addresses are refused, and Maloja
requests ignore the proxy environment variables (`HTTP_PROXY` and the like).
Last.fm and ListenBrainz default to the real services; two admin fields point
them at a self-hosted alternative instead, live, no restart:

- **Last.fm API base URL** — overrides where Last.fm reads go. **Libre.fm** is
  the closest drop-in (a genuinely Last.fm-API-compatible free-software
  alternative; test after switching, as not every method is independently
  verified). Don't point it at Maloja: its Last.fm-compatible endpoints only
  accept scrobbles; Maloja has its own field on each user's Profile instead.
- **ListenBrainz API base URL** — self-hosted ListenBrainz is the same software
  as the public instance, so just the URL changes.

Leave either blank to use the default (the real service, or the
`LASTFM_API_BASE` / `LISTENBRAINZ_API_BASE` env var if set). See
[Suggestions](using/suggestions.md).

### Devices

**Transcoding (CPU controls)**: how many transcodes run at once, and at what
priority (nice level).

### Maintenance

**Background job history**: how many days finished background jobs are kept
in full.

### Security

The sign-in mode, set by the `AUTH_MODE` environment variable rather than here.
Under OIDC, the **break-glass admin password**: an emergency local login for
when the identity provider can't be reached.

## Playlist mirrors

The **Playlist mirrors** tab shows where playlists are mirrored to, and how
that is going. It has two collapsible sections, like **Configuration**, with
a summary and any warnings on each header:

- **Targets**: the mirror targets that are set up (the `.m3u` folder,
  Subsonic, Jellyfin, Emby, Music Assistant, Plex), each with its state and a link to its settings.
  A server's state comes from a live check, the same one **Configuration**
  makes when it loads; the folder's, from whether it can be written. A
  target that is reachable but where a playlist's last write failed shows a
  warning with that failure. Targets not set up are named on one line, "Also
  available: …". Lidarr is listed apart, under **Requests**: a check when it
  is ready, otherwise what needs attention. When Lidarr is the only thing set
  up, the section says no mirror target is, and links to set one up.
- **Playlists**: each mirrored or Lidarr-enabled playlist, one line per
  target, each naming it.

With nothing set up at all, the tab explains what mirroring is and links to
**Configuration**.

## Users

The **Users** tab holds the accounts and the delegations between them, in two
collapsible sections styled like Configuration's. Each header summarises its
section (how many users and admins, and under `oidc` whether the break-glass
account exists; how many delegations). Both are open on a first visit; after
that, which are open is remembered in this browser. **Open all** and **Close
all** sit above them. Every action here takes effect at once, so there is no
Save button.

### Accounts

- `local` mode: create household accounts here (username + password).
- `oidc` / `forward` modes: accounts appear automatically at first login; this
  section gives the overview and lets you provision extra local-only accounts
  alongside. Under `oidc` the header warns when there is no break-glass
  account.
- Each user's row also says whom they manage and who manages them, when a
  delegation links them.
- Deleting a user who still owns devices or selections fails with a clear
  message — reassign or remove those first. Deleting one removes their
  delegations too.

### Delegations

Choose who manages the devices of whom, and revoke a delegation from its row.
What a delegation allows: [Delegated Management](using/delegation.md).
Delegations used to have a tab of their own; a browser that last had it open
now opens **Users** with this section expanded.

!!! tip "Locked out of the admin account?"
    Lost the admin's local password, or need to provision one without a
 browser? Run `flask --app main.py create-admin <username>` inside the
    container — it creates the account as admin, or, if the account already
    exists, grants admin and resets its password. Prompts for the password if
 `--password` isn't given.
