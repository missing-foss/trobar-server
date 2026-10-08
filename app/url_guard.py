#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbound requests to a URL a user typed, as opposed to one an admin set.

Any logged-in account can save such a URL, and the server then fetches it,
so it must not be a way to reach what only the server can reach: itself
(loopback), link-local addresses (which include cloud metadata services),
the unspecified address (which Linux connects to as loopback) and
multicast. IPv4-mapped IPv6 forms count as the IPv4 address they carry.

LAN addresses (10/8, 172.16/12, 192.168/16, fc00::/7) are allowed on
purpose: a self-hosted service on the home network is the main use.

Two checks, both needed:
- check_url(), when the URL is saved: a literal address, or every address
  the hostname resolves to right now. It gives the user a clear refusal.
- session(), for every request: the address checked is the one the socket
  actually connected to, after DNS, so a hostname that resolves somewhere
  harmless at save time and to 127.0.0.1 later is still refused. Nothing
  is sent before the check. Redirects are never followed, since a
  redirect would make the server connect to an address the user picked
  without going through check_url(); a 3xx answer is an error. Proxies
  from the environment are ignored, since the proxy would then be the
  peer checked.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

# What blocked_reason() can return, for callers that word a message per kind.
KINDS = ("loopback", "link-local", "unspecified", "multicast")


class BlockedAddress(Exception):
    """A user-supplied URL led to an address the server won't contact.

    Not an OSError, on purpose: urllib3 wraps (and retries) OSErrors as
    connection failures, and callers need to tell this one apart, to log it
    without the URL."""

    def __init__(self, kind: str):
        super().__init__(f"{kind} address refused")
        self.kind = kind


def blocked_reason(address) -> str | None:
    """The kind of blocked address this is (one of KINDS), or None if it is
    allowed. `address` is an IP address as text or an ipaddress object;
    anything else raises ValueError."""
    ip = ipaddress.ip_address(address)
    # Python 3.13+ already answers is_* for the IPv4 address a mapped one
    # carries; unwrapping says so here instead of relying on it.
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast:
        return "multicast"
    return None


def check_url(url: str) -> str | None:
    """The kind of blocked address the URL's host is, or resolves to, or
    None. A host that doesn't resolve right now passes: it can't be
    contacted either, and session() checks again on every request."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return None
    try:
        return blocked_reason(host)
    except ValueError:
        pass  # a hostname, or an address in a form only the resolver reads
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return None
    for info in infos:
        kind = blocked_reason(info[4][0])
        if kind:
            return kind
    return None


def _check_peer(sock: socket.socket) -> None:
    kind = blocked_reason(sock.getpeername()[0])
    if kind:
        sock.close()
        raise BlockedAddress(kind)


class _GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self) -> socket.socket:
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


class _GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        sock = super()._new_conn()  # TCP only: TLS starts after this returns
        _check_peer(sock)
        return sock


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}


def _refuse_redirect(resp: requests.Response, *_args, **_kwargs) -> None:
    # A response hook runs before requests looks at the redirect, so raising
    # here means it is never followed, whatever allow_redirects says.
    if 300 <= resp.status_code < 400:
        raise requests.HTTPError(f"{resp.status_code} redirect not followed", response=resp)


class GuardedSession(requests.Session):
    """A requests session that checks every peer and follows no redirect."""

    def __init__(self):
        super().__init__()
        self.trust_env = False
        self.hooks["response"].append(_refuse_redirect)
        adapter = _GuardedAdapter()
        self.mount("http://", adapter)
        self.mount("https://", adapter)


def session() -> GuardedSession:
    return GuardedSession()
