"""Tests for TransportClient network-daemon support (A6 Task 2.4).

Covers: legacy behavior unchanged without a token, HMAC handshake when
TOOLRECALL_NETWORK_TOKEN is set, explicit-only remote selection via
TOOLRECALL_TRANSPORT, TLS client mode, and error honesty (no silent
fallback to UDS).
"""

import socket
import subprocess
import threading

import pytest

from toolrecall import network_auth as na
from toolrecall import network_tls as nt
from toolrecall.transport import TransportClient, _parse_tcp


def _serve_once_or_loop(srv: socket.socket, handler, stop: threading.Event):
    srv.settimeout(0.5)
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except (socket.timeout, TimeoutError):
            continue
        except OSError:
            return
        try:
            handler(conn)
        finally:
            try:
                conn.close()
            except OSError:
                pass


class TestLegacyBehavior:
    def test_no_token_no_handshake(self, monkeypatch):
        """Without TOOLRECALL_NETWORK_TOKEN, a LEGACY tcp:// peer (Windows
        loopback, plain framed IPC, no nonce) still works: the client's
        handshake probe reads the FIRST bytes and — because they are not
        a 32-byte nonce (framed v1 message starts with a 4-byte length
        prefix of a small value) — falls through to plain framed mode.
        Against an A6 daemon (nonce first), the probe authenticates or
        reports network_auth_rejected honestly (see test_no_token_against_
        network_daemon)."""
        monkeypatch.delenv("TOOLRECALL_NETWORK_TOKEN", raising=False)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()
        got = {}

        def handler(conn):
            from toolrecall.transport import receive_message, send_message

            req = receive_message(conn)
            got["req"] = req
            send_message(conn, {"pong": True})

        t = threading.Thread(target=_serve_once_or_loop, args=(srv, handler, stop), daemon=True)
        t.start()
        c = TransportClient(f"tcp://127.0.0.1:{port}")
        resp = c.send({"cmd": "ping"})
        stop.set()
        t.join(timeout=3)
        srv.close()
        assert resp.get("pong") is True
        assert got["req"]["cmd"] == "ping"

    def test_no_token_against_network_daemon(self, monkeypatch):
        """No token + A6 daemon (nonce-first): the client must NOT report a
        framing artifact ('Message too large') — it reports
        network_auth_rejected. Found in live testing 2026-09-08."""
        monkeypatch.delenv("TOOLRECALL_NETWORK_TOKEN", raising=False)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()
        got = {"rejected": 0}

        def handler(conn):
            from toolrecall import network_auth as na

            try:
                na.server_handshake(conn, {b"real": "x"})
            except na.AuthError:
                got["rejected"] += 1

        t = threading.Thread(target=_serve_once_or_loop, args=(srv, handler, stop), daemon=True)
        t.start()
        c = TransportClient(f"tcp://127.0.0.1:{port}")
        resp = c.send({"cmd": "ping"})
        stop.set()
        t.join(timeout=3)
        srv.close()
        assert resp.get("error") == "network_auth_rejected", resp
        assert got["rejected"] == 1


class TestNetworkHandshake:
    def test_send_with_token_authenticates(self, monkeypatch):
        """TOOLRECALL_NETWORK_TOKEN set + tcp:// → client performs the
        HMAC handshake before the first request."""
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "tok-1")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()
        got = {}

        def handler(conn):
            from toolrecall.transport import receive_message, send_message

            try:
                token_id = na.server_handshake(conn, {b"tok-1": "ci"})
            except na.AuthError:
                got["auth"] = "rejected"
                return
            got["auth"] = token_id
            req = receive_message(conn)
            got["req"] = req
            send_message(conn, {"pong": True, "served": token_id})

        t = threading.Thread(target=_serve_once_or_loop, args=(srv, handler, stop), daemon=True)
        t.start()
        c = TransportClient(f"tcp://127.0.0.1:{port}")
        resp = c.send({"cmd": "ping"})
        stop.set()
        t.join(timeout=3)
        srv.close()
        assert got.get("auth") == "ci"
        assert resp.get("pong") is True

    def test_wrong_token_reports_auth_rejected(self, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "mine")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def handler(conn):
            try:
                na.server_handshake(conn, {b"server-token": "x"})
            except na.AuthError:
                pass

        t = threading.Thread(target=_serve_once_or_loop, args=(srv, handler, stop), daemon=True)
        t.start()
        c = TransportClient(f"tcp://127.0.0.1:{port}")
        resp = c.send({"cmd": "ping"})
        stop.set()
        t.join(timeout=3)
        srv.close()
        assert resp.get("error") == "network_auth_rejected"


class TestExplicitOnly:
    def test_default_path_is_uds_not_tcp(self, monkeypatch, tmp_path):
        """Setting ONLY TOOLRECALL_NETWORK_TOKEN must NOT flip the default
        transport to TCP — remote selection is TOOLRECALL_TRANSPORT's job."""
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "tok")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("TOOLRECALL_TRANSPORT", raising=False)
        monkeypatch.delenv("TOOLRECALL_UDS_PATH", raising=False)
        c = TransportClient()
        assert c.is_tcp is False

    def test_explicit_tcp_transport_parses(self):
        host, port = _parse_tcp("tcp://cachehost.internal:8570")
        assert host == "cachehost.internal"
        assert port == 8570


class TestTLSClient:
    def test_tls_client_against_tls_server(self, monkeypatch, tmp_path):
        if subprocess.run(["which", "openssl"], capture_output=True).returncode != 0:
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
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "tls-tok")
        monkeypatch.setenv("TOOLRECALL_NETWORK_TLS", "true")
        monkeypatch.setenv("TOOLRECALL_NETWORK_TLS_CA", str(cert))
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()
        sctx = nt.server_context(str(cert), str(key))
        got = {}

        def handler(conn):
            from toolrecall.transport import receive_message, send_message

            try:
                conn = sctx.wrap_socket(conn, server_side=True)
                token_id = na.server_handshake(conn, {b"tls-tok": "ci"})
                got["auth"] = token_id
                receive_message(conn)
                send_message(conn, {"pong": True, "served": token_id})
            except Exception:
                got.setdefault("err", "handshake-failed")

        t = threading.Thread(target=_serve_once_or_loop, args=(srv, handler, stop), daemon=True)
        t.start()
        c = TransportClient(f"tcp://127.0.0.1:{port}")
        resp = c.send({"cmd": "ping"})
        stop.set()
        t.join(timeout=3)
        srv.close()
        assert got.get("auth") == "ci"
        assert resp.get("pong") is True


class TestErrorHonesty:
    def test_connection_refused_is_daemon_unavailable(self, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "t")
        # Bind then close: nothing is listening on this port now.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        c = TransportClient(f"tcp://127.0.0.1:{port}")
        assert c.send({"cmd": "ping"}).get("error") == "daemon_unavailable"
