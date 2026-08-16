"""Regression tests for the forward proxy's SSRF allowlist guard (py/full-ssrf).

The proxy is a localhost HTTP forward proxy. Its contract is to relay requests
to known, trusted LLM API hosts ONLY (FORWARD_HOSTS). Before this fix, a caller
could set Host / X-Target-Host to an arbitrary value (e.g. a cloud-metadata or
internal service) and the proxy would connect to it — a classic SSRF. These
tests assert that non-allowlisted hosts are rejected before any connection.

These are pure unit tests — no daemon or outbound connection required.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.proxy import _host_allowed, FORWARD_HOSTS


class TestHostAllowlist(unittest.TestCase):
    """The _host_allowed guard must reject non-trusted hosts and allow known ones."""

    def test_all_known_hosts_allowed(self):
        for host in FORWARD_HOSTS:
            with self.subTest(host=host):
                self.assertTrue(_host_allowed(host), f"{host} must be allowed")

    def test_host_with_port_allowed(self):
        self.assertTrue(_host_allowed("api.openai.com:443"))
        self.assertTrue(_host_allowed("openrouter.ai:8443"))

    def test_cloud_metadata_rejected(self):
        # Classic SSRF targets — must never be reachable through the proxy.
        for host in (
            "169.254.169.254",  # AWS/GCP metadata
            "metadata.google.internal",
            "169.254.170.2",  # AWS ECS credentials
            "localhost",
            "127.0.0.1",
            "::1",
            "internal.corp.example",
            "10.0.0.1",
        ):
            with self.subTest(host=host):
                self.assertFalse(_host_allowed(host), f"{host} must be blocked")

    def test_scheme_embedded_not_bypassed(self):
        # The split handles a bare hostname-with-port; a value with a scheme
        # is not in the allowlist and must be rejected.
        self.assertFalse(_host_allowed("https://api.openai.com"))


class TestForwardRejectsNonAllowlisted(unittest.TestCase):
    """_forward must refuse to connect to a non-allowlisted host at the sink."""

    def test_forward_returns_403_for_metadata_host(self):
        from toolrecall.proxy import ForwardProxyHandler

        # Build a bare handler instance — _forward's SSRF guard fires before
        # any use of self.headers/connection, so no HTTP plumbing is needed.
        handler = object.__new__(ForwardProxyHandler)
        status, headers, body = handler._forward(
            "GET",
            "169.254.169.254",
            "/latest/meta-data/",
            "https",
            b"",
        )
        self.assertEqual(status, 403)
        self.assertIn(b"Forbidden", body)
        # No content-length / connection established — it's a hard reject.

    def test_forward_allows_known_host_guard_pass(self):
        # The guard must NOT block a legitimate allowlisted host (we only
        # assert the allowlist check here — no real connection is attempted).
        self.assertTrue(_host_allowed("api.openai.com"))


if __name__ == "__main__":
    unittest.main()
