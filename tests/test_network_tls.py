"""Tests for network_tls (A6 2.2b) — stdlib-ssl wrapper for the TCP listener.

Zero-deps (constitution II): `ssl` is stdlib. The TLS layer is independent
of the HMAC layer — TLS encrypts the channel, HMAC authenticates the
client. Both apply to one connection: wrap first, then handshake.

Uses self-signed certs generated on the fly (openssl CLI is present in dev
environments; tests skip cleanly if it is not).
"""

import socket
import ssl
import subprocess
import threading

import pytest

from toolrecall import network_auth as na
from toolrecall import network_tls as nt

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "openssl"], capture_output=True).returncode != 0,
    reason="openssl CLI not available for self-signed test certs",
)


@pytest.fixture()
def cert_paths(tmp_path):
    """Generate a self-signed cert/key pair for tests."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
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
            "/CN=toolrecall-test",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


def _listen():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    return srv, srv.getsockname()[1]


class TestServerContext:
    def test_build_requires_cert_and_key(self):
        with pytest.raises(nt.TLSConfigError):
            nt.server_context(cert_path=None, key_path=None)

    def test_build_with_cert_and_key(self, cert_paths):
        cert, key = cert_paths
        ctx = nt.server_context(cert_path=cert, key_path=key)
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_NONE  # client auth is HMAC, not certs

    def test_missing_cert_file_raises(self, tmp_path):
        with pytest.raises(nt.TLSConfigError):
            nt.server_context(
                cert_path=str(tmp_path / "nope.pem"),
                key_path=str(tmp_path / "nope2.pem"),
            )


class TestClientContext:
    def test_insecure_mode_skips_verify(self):
        ctx = nt.client_context(verify=False)
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_verify_mode_uses_ca(self, cert_paths):
        cert, _ = cert_paths
        ctx = nt.client_context(verify=True, ca_path=cert)
        assert ctx.verify_mode == ssl.CERT_REQUIRED


class TestTLSRoundtrip:
    def test_tls_socket_carries_hmac_handshake(self, cert_paths):
        """Full stack: TLS wrap → HMAC handshake → framed ping, all over
        one connection. The layers compose and the client verifies via
        CA pinning (self-signed cert passed as ca_path)."""
        cert, key = cert_paths
        srv, port = _listen()
        token = b"tok"
        got = {}

        def serve():
            raw, _ = srv.accept()
            try:
                tls = nt.wrap_server(raw, nt.server_context(cert, key))
                tls.settimeout(5)
                tid = na.server_handshake(tls, {token: "ci"})
                got["id"] = tid
                tls.sendall(b"hello-over-tls")
            except Exception:
                pass
            finally:
                raw.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()

        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        tls = nt.wrap_client(raw, nt.client_context(verify=True, ca_path=cert))
        tls.settimeout(5)
        na.client_handshake(tls, token)
        reply = tls.recv(32)
        tls.close()
        t.join(timeout=5)
        srv.close()

        assert got.get("id") == "ci"
        assert reply == b"hello-over-tls"

    def test_plaintext_client_to_tls_server_fails(self, cert_paths):
        """A non-TLS client hitting a TLS listener cannot authenticate —
        the server's TLS unwrap fails before HMAC ever runs."""
        cert, key = cert_paths
        srv, port = _listen()
        got = {}

        def serve():
            raw, _ = srv.accept()
            try:
                nt.wrap_server(raw, nt.server_context(cert, key))
                got["tls_ok"] = True
            except Exception:
                got["tls_ok"] = False
            finally:
                raw.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        raw.sendall(b"\x01" + b"\x00" * 32)  # plaintext greeting at TLS listener
        raw.settimeout(3)
        try:
            raw.recv(64)
        except OSError:
            pass
        raw.close()
        t.join(timeout=5)
        srv.close()
        assert got.get("tls_ok") is False
