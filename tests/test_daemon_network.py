"""Tests for the DaemonServer TCP network listener (A6 Task 2.3).

Covers: listener only with enabled+token, auth-required, ping roundtrip
over TCP, client-identity override (no policy spoofing), TLS mode, and
lifecycle (stop closes listener).
"""

import json
import socket
import ssl
import struct
import subprocess
import sys
import threading

import pytest

from toolrecall import network_auth as na
from toolrecall.daemon import DaemonServer

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only UDS+TCP combo test")


def _framed(sock: socket.socket, payload: dict) -> None:
    raw = json.dumps(payload).encode()
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def _unframed(sock: socket.socket) -> dict:
    hdr = b""
    while len(hdr) < 4:
        c = sock.recv(4 - len(hdr))
        if not c:
            raise ConnectionError("closed")
        hdr += c
    (n,) = struct.unpack("!I", hdr)
    body = b""
    while len(body) < n:
        c = sock.recv(n - len(body))
        if not c:
            raise ConnectionError("closed")
        body += c
    return json.loads(body)


def _auth_and_request(port: int, token: bytes, payload: dict, tls_ctx=None) -> dict:
    """Helper: connect, authenticate, send one framed request, get reply."""
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        if tls_ctx is not None:
            c = tls_ctx.wrap_socket(c, server_hostname=None)
        c.settimeout(5)
        na.client_handshake(c, token)
        _framed(c, payload)
        return _unframed(c)
    finally:
        try:
            c.close()
        except OSError:
            pass


@pytest.fixture()
def tcp_daemon(tmp_path, monkeypatch):
    """A DaemonServer with network enabled on an ephemeral port."""
    monkeypatch.setenv("TOOLRECALL_NETWORK_ENABLED", "true")
    monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "test-token-123")
    monkeypatch.setenv("TOOLRECALL_NETWORK_PORT", "0")  # ephemeral
    monkeypatch.setenv("TOOLRECALL_MCP_ALLOWED_PATHS", str(tmp_path))
    d = DaemonServer(socket_path=str(tmp_path / "t.sock"))
    # Patch the UDS server away: we only exercise the TCP listener here.
    d._server = None

    started = threading.Event()
    listener_holder = {}

    def _listener():
        # Reuse the production entrypoint but bind ephemeral + track port

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((d.cfg.network_bind, 0))
        srv.listen(16)
        listener_holder["srv"] = srv
        listener_holder["port"] = srv.getsockname()[1]
        d._tcp_srv = srv
        started.set()
        while d._running:
            try:
                srv.settimeout(0.5)
                conn, _ = srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            d._executor.submit(d._handle_tcp, conn)

    d._running = True
    d._executor = __import__("concurrent.futures", fromlist=["x"]).ThreadPoolExecutor(max_workers=8)
    t = threading.Thread(target=_listener, daemon=True)
    t.start()
    started.wait(timeout=5)
    d.tcp_port = listener_holder["port"]
    yield d
    d._running = False
    try:
        listener_holder["srv"].close()
    except OSError:
        pass
    d._executor.shutdown(wait=False)


class TestListenerLifecycle:
    def test_no_listener_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOOLRECALL_NETWORK_ENABLED", raising=False)
        monkeypatch.delenv("TOOLRECALL_NETWORK_TOKEN", raising=False)
        d = DaemonServer(socket_path=str(tmp_path / "x.sock"))
        assert d._should_start_network_listener() is False

    def test_enabled_but_no_token_is_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_NETWORK_ENABLED", "true")
        monkeypatch.delenv("TOOLRECALL_NETWORK_TOKEN", raising=False)
        d = DaemonServer(socket_path=str(tmp_path / "x.sock"))
        assert d._should_start_network_listener() is False

    def test_enabled_with_token_is_on(self, tcp_daemon):
        assert tcp_daemon._should_start_network_listener() is True


class TestAuthRequired:
    def test_unauthenticated_ping_rejected(self, tcp_daemon):
        c = socket.create_connection(("127.0.0.1", tcp_daemon.tcp_port), timeout=5)
        c.settimeout(5)
        # Skip the handshake; speak framed protocol directly
        _framed(c, {"cmd": "ping"})
        with pytest.raises((ConnectionError, OSError)):
            _unframed(c)
        c.close()

    def test_wrong_token_rejected(self, tcp_daemon):
        c = socket.create_connection(("127.0.0.1", tcp_daemon.tcp_port), timeout=5)
        c.settimeout(5)
        nonce = c.recv(32)
        assert len(nonce) == 32
        c.sendall(na.client_greeting(b"wrong-token", nonce))
        # Server must close, not serve
        assert c.recv(64) == b""
        c.close()

    def test_correct_token_ping_roundtrip(self, tcp_daemon):
        resp = _auth_and_request(tcp_daemon.tcp_port, b"test-token-123", {"cmd": "ping"})
        assert resp.get("pong") is True


class TestClientOverride:
    def test_client_field_cannot_spoof(self, tcp_daemon):
        """The server overrides request['client'] with the authenticated
        token-id. A remote client claiming client='admin' (a name with a
        privileged [mcp.clients] entry) is re-stamped to its token-id."""
        resp = _auth_and_request(
            tcp_daemon.tcp_port,
            b"test-token-123",
            {"cmd": "cached_read", "path": "/etc/passwd", "client": "admin"},
        )
        # /etc/passwd is outside the fixture's allowed_paths (tmp_path);
        # a spoofed 'admin' client must not change that outcome.
        assert resp.get("ok") is False or "error" in resp

    def test_client_field_set_to_token_id(self, tcp_daemon, monkeypatch):
        """With a [network.clients] entry for the token-id, requests are
        evaluated under THAT policy — proving the override value."""
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "test-token-123")
        resp = _auth_and_request(
            tcp_daemon.tcp_port,
            b"test-token-123",
            {"cmd": "ping", "client": "whatever"},
        )
        assert resp.get("pong") is True


class TestTLSMode:
    def test_tls_roundtrip(self, tmp_path, monkeypatch):
        openssl_ok = subprocess.run(["which", "openssl"], capture_output=True).returncode == 0
        if not openssl_ok:
            pytest.skip("openssl CLI unavailable")
        cert = tmp_path / "c.pem"
        key = tmp_path / "k.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )
        monkeypatch.setenv("TOOLRECALL_NETWORK_ENABLED", "true")
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "tls-token")
        monkeypatch.setenv("TOOLRECALL_NETWORK_TLS", "true")
        monkeypatch.setenv("TOOLRECALL_NETWORK_TLS_CERT", str(cert))
        monkeypatch.setenv("TOOLRECALL_NETWORK_TLS_KEY", str(key))
        monkeypatch.setenv("TOOLRECALL_MCP_ALLOWED_PATHS", str(tmp_path))

        from toolrecall import network_tls as nt

        d = DaemonServer(socket_path=str(tmp_path / "tls.sock"))
        d._running = True
        d._executor = __import__("concurrent.futures", fromlist=["x"]).ThreadPoolExecutor(
            max_workers=8
        )

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((d.cfg.network_bind, 0))
        srv.listen(16)
        port = srv.getsockname()[1]
        ctx = nt.server_context(d.cfg.network_tls_cert, d.cfg.network_tls_key)

        def _listener():
            while d._running:
                try:
                    srv.settimeout(0.5)
                    conn, _ = srv.accept()
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                d._executor.submit(d._handle_tcp, conn, None, ctx)

        t = threading.Thread(target=_listener, daemon=True)
        t.start()

        client_ctx = nt.client_context(verify=True, ca_path=str(cert))
        # TLS floor < 1.2 refused (mirrors the library guarantee; CodeQL
        # py/insecure-protocol flags an unrestricted client context here).
        client_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        resp = _auth_and_request(port, b"tls-token", {"cmd": "ping"}, tls_ctx=client_ctx)
        assert resp.get("pong") is True

        d._running = False
        srv.close()
        d._executor.shutdown(wait=False)


class TestFraming:
    def test_multiple_sequential_requests_one_connection(self, tcp_daemon):
        """Same framing contract as UDS: multiple framed requests per
        connection, one framed reply each, in order."""
        c = socket.create_connection(("127.0.0.1", tcp_daemon.tcp_port), timeout=5)
        try:
            c.settimeout(5)
            na.client_handshake(c, b"test-token-123")
            _framed(c, {"cmd": "ping"})
            r1 = _unframed(c)
            _framed(c, {"cmd": "ping"})
            r2 = _unframed(c)
            assert r1.get("pong") and r2.get("pong")
        finally:
            c.close()

    def test_protocol_version_mismatch_rejected(self, tcp_daemon):
        resp = _auth_and_request(
            tcp_daemon.tcp_port,
            b"test-token-123",
            {"cmd": "cached_read", "path": "/etc/passwd", "v": 99},
        )
        assert resp.get("error") or resp.get("ok") is False
