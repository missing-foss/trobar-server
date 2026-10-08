#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""url_guard: which addresses are refused, the save-time check, and the
request-time session against real HTTP servers on 127.0.0.1 (ephemeral
ports). Name resolution is patched where a test needs a hostname; nothing
leaves the machine.

Every refusal test has a control beside it, with the guard relaxed, that
shows the same request would otherwise have reached the server.

    python3 -m unittest test_url_guard -v      # from app/
"""
import http.server
import json
import os
import socket
import threading
import unittest
import unittest.mock as mock

import requests

import url_guard


class _Server:
    """An HTTP server on 127.0.0.1 that records the paths it is asked for.
    `routes` maps a path to (status, headers, body)."""

    def __init__(self, routes=None):
        self.hits: list[str] = []
        self.routes = routes or {}
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the stdlib's name
                path = self.path.split("?")[0]
                server.hits.append(path)
                status, headers, body = server.routes.get(path, (200, {}, b"{}"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def resolving(name, address):
    """Patch name resolution so `name` resolves to `address` and nothing
    else resolves."""
    real = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host == name:
            return real(address, port, *args, **kwargs)
        raise socket.gaierror(f"{host} not in the test resolver")
    return mock.patch("socket.getaddrinfo", side_effect=fake)


def relaxed():
    """The guard switched off, for the controls and the redirect tests."""
    return mock.patch.object(url_guard, "blocked_reason", return_value=None)


class BlockedReasonTests(unittest.TestCase):
    def test_each_blocked_range(self):
        cases = {
            "127.0.0.1": "loopback", "127.255.0.9": "loopback", "::1": "loopback",
            "169.254.169.254": "link-local", "169.254.0.1": "link-local", "fe80::1": "link-local",
            "fe80::1%lo": "link-local",
            "0.0.0.0": "unspecified", "::": "unspecified",
            "224.0.0.1": "multicast", "239.255.255.250": "multicast", "ff02::1": "multicast",
        }
        self.assertEqual({a: url_guard.blocked_reason(a) for a in cases}, cases)

    def test_ipv4_mapped_forms_count_as_the_ipv4_address(self):
        self.assertEqual(url_guard.blocked_reason("::ffff:127.0.0.1"), "loopback")
        self.assertEqual(url_guard.blocked_reason("::ffff:169.254.169.254"), "link-local")
        self.assertEqual(url_guard.blocked_reason("::ffff:0.0.0.0"), "unspecified")
        self.assertEqual(url_guard.blocked_reason("::ffff:224.0.0.1"), "multicast")
        self.assertIsNone(url_guard.blocked_reason("::ffff:192.168.1.10"))

    def test_lan_and_public_addresses_are_allowed(self):
        for address in ("10.0.0.1", "172.16.0.1", "172.31.255.254", "192.168.1.10", "fd00::1", "fc00::1",
                        "192.0.2.10", "2001:db8::1"):
            with self.subTest(address=address):
                self.assertIsNone(url_guard.blocked_reason(address))

    def test_every_kind_returned_is_listed(self):
        kinds = {url_guard.blocked_reason(a) for a in ("127.0.0.1", "169.254.0.1", "0.0.0.0", "224.0.0.1")}
        self.assertEqual(kinds, set(url_guard.KINDS))


class CheckUrlTests(unittest.TestCase):
    def test_a_literal_address_needs_no_resolver(self):
        with mock.patch("socket.getaddrinfo") as resolve:
            self.assertEqual(url_guard.check_url("http://169.254.169.254/latest/"), "link-local")
            self.assertEqual(url_guard.check_url("https://[::1]:42010"), "loopback")
            self.assertIsNone(url_guard.check_url("http://192.168.1.10:42010"))
        resolve.assert_not_called()

    def test_forms_only_the_resolver_reads_are_resolved(self):
        # "127.1" and "0x7f000001" aren't addresses to ipaddress, but are to
        # the resolver, and so to requests.
        self.assertEqual(url_guard.check_url("http://127.1:42010"), "loopback")
        self.assertEqual(url_guard.check_url("http://0x7f000001/"), "loopback")

    def test_a_hostname_is_refused_if_any_address_it_resolves_to_is_blocked(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 80)),
                  (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80, 0, 0))]
        with mock.patch("socket.getaddrinfo", return_value=answer) as resolve:
            self.assertEqual(url_guard.check_url("https://maloja.example.invalid/"), "loopback")
        self.assertEqual(resolve.call_args.args[:2], ("maloja.example.invalid", 443))

    def test_a_hostname_that_does_not_resolve_passes(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
            self.assertIsNone(url_guard.check_url("http://maloja.example.invalid:42010"))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.server = _Server()
        self.addCleanup(self.server.close)

    def test_a_loopback_peer_is_refused_before_anything_is_sent(self):
        with url_guard.session() as s, self.assertRaises(url_guard.BlockedAddress) as raised:
            s.get(self.server.url + "/x", timeout=5)
        self.assertEqual(raised.exception.kind, "loopback")
        self.assertEqual(self.server.hits, [])
        with relaxed(), url_guard.session() as s:  # control
            self.assertEqual(s.get(self.server.url + "/x", timeout=5).status_code, 200)
        self.assertEqual(self.server.hits, ["/x"])

    def test_https_is_checked_at_tcp_connect_before_tls(self):
        # The server speaks plain HTTP: unchecked, the TLS handshake would
        # fail with an SSL error instead.
        with url_guard.session() as s, self.assertRaises(url_guard.BlockedAddress):
            s.get(f"https://127.0.0.1:{self.server.port}/x", timeout=5)
        with relaxed(), url_guard.session() as s, self.assertRaises(requests.exceptions.SSLError):
            s.get(f"https://127.0.0.1:{self.server.port}/x", timeout=5)

    def test_the_address_connected_to_is_checked_not_the_url(self):
        # Saved while the name pointed somewhere harmless; it now resolves to
        # loopback.
        url = f"http://maloja.example.invalid:{self.server.port}/x"
        with resolving("maloja.example.invalid", "192.168.1.10"):
            self.assertIsNone(url_guard.check_url(url))
        with resolving("maloja.example.invalid", "127.0.0.1"):
            with url_guard.session() as s, self.assertRaises(url_guard.BlockedAddress):
                s.get(url, timeout=5)
            self.assertEqual(self.server.hits, [])
            with relaxed(), url_guard.session() as s:  # control
                s.get(url, timeout=5)
        self.assertEqual(self.server.hits, ["/x"])

    def test_a_redirect_is_not_followed_even_to_an_allowed_address(self):
        target = _Server()
        self.addCleanup(target.close)
        self.server.routes["/x"] = (302, {"Location": target.url + "/inside"}, b"")
        with relaxed(), url_guard.session() as s, self.assertRaises(requests.HTTPError) as raised:
            s.get(self.server.url + "/x", timeout=5)
        response = raised.exception.response
        self.assertEqual(response.status_code if response is not None else None, 302)
        self.assertEqual(target.hits, [])
        with relaxed():  # control: plain requests would have followed it
            requests.get(self.server.url + "/x", timeout=5)
        self.assertEqual(target.hits, ["/inside"])

    def test_an_environment_proxy_is_not_used(self):
        proxy = _Server()
        self.addCleanup(proxy.close)
        with relaxed(), mock.patch.dict(os.environ, {"HTTP_PROXY": proxy.url, "http_proxy": proxy.url,
                                                     "NO_PROXY": "", "no_proxy": ""}):
            with url_guard.session() as s:
                s.get(self.server.url + "/x", timeout=5)
            self.assertEqual((self.server.hits, proxy.hits), (["/x"], []))
            requests.get(self.server.url + "/y", timeout=5)  # control
        self.assertEqual(len(proxy.hits), 1)

    def test_a_json_body_comes_through(self):
        self.server.routes["/j"] = (200, {"Content-Type": "application/json"}, json.dumps({"a": 1}).encode())
        with relaxed(), url_guard.session() as s:
            self.assertEqual(s.get(self.server.url + "/j", timeout=5).json(), {"a": 1})


if __name__ == "__main__":
    unittest.main()
