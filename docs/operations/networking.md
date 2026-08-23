<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Networking & Reverse Proxy

Trobar speaks plain HTTP on its container port and relies on a
**TLS-terminating reverse proxy** for HTTPS. **The container port must never be
exposed directly to the internet.**

## Reverse proxy

Any proxy works — Traefik, Caddy, nginx. The `docker-compose.yaml` in this repo
binds the app only to `127.0.0.1:5000` and expects your own reverse-proxy config
in front of it; it doesn't ship configuration for any specific proxy. The app
honours `X-Forwarded-Proto` / `-Host` from the proxy and reads `X-Forwarded-For`
for per-IP rate limiting, so the proxy must set those headers.

In [`forward` auth mode](../getting-started/authentication.md#forward) the proxy
carries an even heavier responsibility: it must authenticate and gate **every**
route, because the app performs no auth of its own in that mode.

## Trusted proxy and rate limiting

Trusting `X-Forwarded-For` only makes sense if a trusted proxy is the one
setting it — `TROBAR_TRUSTED_PROXY` (default `*`, meaning "any peer") is what
tells waitress which one that is.

**This same trust is what the per-IP brute-force backoff** (login, device
pairing, device-token auth) **relies on to know who a request is really
from.** If this container's port is reachable by anything other than your
reverse proxy, an attacker can send their own `X-Forwarded-For` and rotate it
every request, and the rate limiter never accumulates a failure against any
one bucket. It looks identical to a limiter that's simply never been
triggered; nothing about it is loud.

**`*` is the correct, sufficient default for the `docker-compose.yaml` this
repo actually ships** — it binds the container port to `127.0.0.1:5000` and
expects your own reverse proxy in front of it. That loopback bind is a real
barrier (nothing on the network, only a process on the host itself, can
reach the port), just a narrower one than a fully internal-only Docker
network with no published port at all — an alternative topology you can set
up yourself. Don't treat pinning as a hardening step to reach for by default
on either topology; it's for a deployment that can't get *some* form of that
isolation in the first place (the port genuinely reachable from elsewhere on
the LAN or the internet).

!!! warning "A pin that doesn't match is worse than leaving it at `*`"
 When it fails to match, waitress strips `X-Forwarded-For` from *every*
 request rather than trusting none of it selectively — `REMOTE_ADDR` then
    stays the proxy's own address for every real user behind it, and they all
    collapse into **one** shared rate-limit bucket. One household member
 mistyping a password, or one bot hitting `/login`, can lock out everyone
    else for the backoff window. It fails in the confusing direction:
    everything works until someone gets a 429 they can't explain.

??? tip "If you do need to pin it: getting the value right"

 **Do not use `127.0.0.1`.** It looks like the obvious value for the
    loopback-bound port above, but it's wrong: Docker's port publishing
    doesn't preserve the connecting address as seen by the container. Behind a
 `127.0.0.1:<port>:<port>` mapping, a request to `127.0.0.1:5000` on the
    host arrives inside the container from a Docker bridge gateway address
 (something in the `172.x.0.1` shape), never from `127.0.0.1`. waitress
    matches by raw string equality against the connecting socket peer — never
    a hostname, container name, DNS lookup, or "the address I dialed" — so
 `127.0.0.1` here never matches anything, ever.

    The value that actually matches is your own deployment's Docker bridge
    gateway — find it with:

    ```bash
    docker network inspect $(docker compose ps -q trobar \
      | xargs docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}') \
      --format '{{ (index .IPAM.Config 0).Gateway }}'
    ```

 It's stable across `docker compose up --force-recreate` (the network
    itself isn't recreated, only the container), but it's assigned by Docker
    per project/host, not a fixed value you can copy from these docs — re-run
 the command above and update `TROBAR_TRUSTED_PROXY` if you ever fully tear
 the network down (`docker compose down`) rather than just recreating the
    container.

    If you're instead running a genuinely internal-network-only topology (your
    own reverse proxy joined to a Docker network with no published port at
    all), pin to that proxy's container IP on that network the same way — the
    same "exact match, verify it, re-check after a full network teardown"
    rules apply.

## Split-horizon DNS (recommended)

If the app is reachable through a public hostname (proxy + TLS), make LAN
clients resolve that hostname **directly to the server's LAN IP**:

- AdGuard Home / Pi-hole: add a DNS rewrite for `trobar.example.com` → LAN IP.
- Or your router's DNS override, if it has one.

Same hostname, same TLS certificate, zero app configuration — but sync traffic
at home goes straight to the server instead of out and back through your WAN.

!!! warning "Private DNS bypasses this"
    A phone using Private DNS (DoT/DoH) bypasses your local resolver and keeps
    using the public route. Either disable Private DNS on the home Wi-Fi or
    accept the detour.
