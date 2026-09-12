"""network_tls — optional TLS for the shared network daemon (A6 2.2b).

stdlib ``ssl`` only (constitution II: zero runtime dependencies). TLS is
independent of the HMAC auth layer and composes with it:

    accept() → wrap_server() [TLS] → server_handshake() [HMAC] → framed IPC

Threat-model placement (SECURITY.md §network-daemon):

- HMAC alone authenticates the client but does NOT encrypt — cache
  contents (file reads, terminal output, MCP payloads) cross the wire in
  the clear. Fine on loopback or inside a VPN/SSH tunnel.
- TLS encrypts the channel; with ca-pinned verification it also
  authenticates the SERVER to the client (mitM resistance).
- On an open network: TLS is REQUIRED (the config comment says so; the
  daemon prints a warning when bind != loopback and tls = false).

Client verification modes:
- verify=False (default for tcp:// bootstrap): trusts any cert. Still
  prevents passive eavesdropping; vulnerable to active mitM. The HMAC
  token remains the real authentication.
- verify=True + ca_path: full server verification (pin the team daemon's
  self-signed cert or use a real CA chain).
"""

from __future__ import annotations

import os
import ssl
import socket


class TLSConfigError(Exception):
    """TLS configured wrongly (missing cert/key, unreadable files)."""


def server_context(cert_path: str | None, key_path: str | None) -> ssl.SSLContext:
    """Build the listener-side SSLContext.

    Client certificates are NOT required — client identity is the HMAC
    token (A4 policy binds the token-id). CERT_NONE keeps cert management
    off the team's critical path.
    """
    if not cert_path or not key_path:
        raise TLSConfigError("network.tls=true requires network.tls_cert and network.tls_key")
    cert_path = os.path.expanduser(cert_path)
    key_path = os.path.expanduser(key_path)
    if not os.path.isfile(cert_path) or not os.path.isfile(key_path):
        raise TLSConfigError(f"TLS cert/key not found: cert={cert_path!r} key={key_path!r}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    # Client auth is HMAC-based; we do not verify client certs.
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def client_context(verify: bool = False, ca_path: str | None = None) -> ssl.SSLContext:
    """Build the client-side SSLContext.

    verify=False: encrypt but trust any server cert (TOOLRECALL_NETWORK_TLS
    without TOOLRECALL_NETWORK_TLS_CA). Passive-eavesdrop safe.
    verify=True: require a verifiable server cert chain; ca_path pins a
    self-signed team cert or points at a CA bundle.
    """
    if verify:
        ctx = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=os.path.expanduser(ca_path) if ca_path else None
        )
        # ca_path pinning IS the server identity (self-signed team cert):
        # check_hostname stays off so connecting by IP works. The pin
        # already authenticates the server; hostname checks only matter
        # for real CA chains, where callers pass server_hostname for SNI.
        ctx.check_hostname = False
        return ctx
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def wrap_server(raw: socket.socket, ctx: ssl.SSLContext) -> ssl.SSLSocket:
    """Server side: wrap an accepted socket into TLS. Raises on TLS-level
    failure (plaintext client, bad handshake) — caller treats as reject."""
    return ctx.wrap_socket(raw, server_side=True)


def wrap_client(
    raw: socket.socket, ctx: ssl.SSLContext, server_hostname: str | None = None
) -> ssl.SSLSocket:
    """Client side: wrap a connected socket into TLS before the HMAC
    handshake. server_hostname is passed for SNI when verify=True."""
    return ctx.wrap_socket(raw, server_hostname=server_hostname)
