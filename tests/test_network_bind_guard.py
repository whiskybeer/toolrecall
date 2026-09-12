"""Tests for the local-networks-only bind guard (A6 hardening).

User requirement (2026-09-08): the network daemon must bind LOCAL networks
only — loopback, RFC1918 private ranges, link-local, ULA — and refuse
public IPs by default. A public bind is an explicit escape hatch
(`[network] allow_public = true`), because some deployments legitimately
need it (dedicated cache host behind its own firewall).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.daemon import _is_local_network_bind


class TestLocalBindClassification:
    def test_loopback_allowed(self):
        assert _is_local_network_bind("127.0.0.1")
        assert _is_local_network_bind("127.0.0.5")

    def test_rfc1918_allowed(self):
        assert _is_local_network_bind("10.164.0.2")
        assert _is_local_network_bind("172.16.0.1")
        assert _is_local_network_bind("172.31.255.254")
        assert _is_local_network_bind("192.168.1.10")

    def test_link_local_and_ula_allowed(self):
        assert _is_local_network_bind("169.254.1.1")
        assert _is_local_network_bind("::1")
        assert _is_local_network_bind("fe80::1")
        assert _is_local_network_bind("fd00::1")

    def test_wildcard_blocked_by_default(self):
        # 0.0.0.0 binds ALL interfaces incl. public ones — not local-only.
        assert not _is_local_network_bind("0.0.0.0")

    def test_public_ip_blocked(self):
        assert not _is_local_network_bind("8.8.8.8")
        assert not _is_local_network_bind("1.2.3.4")
        assert not _is_local_network_bind("172.32.0.1")  # just outside RFC1918
        assert not _is_local_network_bind("11.0.0.1")  # just outside 10/8

    def test_edge_boundaries(self):
        # 10/8 boundaries: 10.255.255.255 in, 11.0.0.0 out.
        assert _is_local_network_bind("10.255.255.255")
        assert not _is_local_network_bind("11.0.0.0")
        # 172.16-31 only: 172.15 and 172.32 are public.
        assert not _is_local_network_bind("172.15.255.255")
        assert _is_local_network_bind("172.16.0.0")
        assert _is_local_network_bind("172.31.255.255")
        assert not _is_local_network_bind("172.32.0.0")
        # 192.168/16 only.
        assert not _is_local_network_bind("192.169.0.1")
        assert _is_local_network_bind("192.168.255.255")

    def test_special_ranges_NOT_local(self):
        """is_private=True is NOT the contract: documentation/benchmark
        ranges (RFC 5737/2544) are not local networks. Live-test finding
        2026-09-08: 203.0.113.7 slipped through via is_private."""
        assert not _is_local_network_bind("203.0.113.7")  # TEST-NET-3
        assert not _is_local_network_bind("192.0.2.1")  # TEST-NET-1
        assert not _is_local_network_bind("198.51.100.1")  # TEST-NET-2
        assert not _is_local_network_bind("198.18.0.1")  # benchmark net

    def test_hostname_resolved_then_classified(self):
        # A hostname that resolves loopback = local.
        assert _is_local_network_bind("localhost")
        # A hostname resolving public = blocked.
        assert not _is_local_network_bind("dns.google")  # 8.8.8.8/8.8.4.4
