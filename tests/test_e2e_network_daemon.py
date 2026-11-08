"""E2E: shared network daemon (A6 Task 2.7).

Real daemon subprocess with [network] enabled (ephemeral TCP port):
- local client writes via UDS, "remote" client reads the same entry via
  TCP with the token -> cache HIT shared across transports
- remote client without a token is rejected
- ping over TCP carries the same protocol version
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import network_auth as na
from toolrecall.transport import TransportClient


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _tcp_request(port: int, token: bytes, payload: dict) -> dict:
    """Minimal remote client: TLS-less TCP + HMAC + framed JSON."""
    from toolrecall.transport import send_message, receive_message

    c = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        c.settimeout(10)
        na.client_handshake(c, token)
        send_message(c, payload)
        return receive_message(c)
    finally:
        c.close()


class NetworkDaemonE2E:
    """Boot a daemon subprocess with [network] enabled on an ephemeral port."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr_net_e2e_")
        tmp = self._tmp.name
        self.port = _free_port()
        self.token = "e2e-shared-token"
        self.cfg_path = os.path.join(tmp, "toolrecall.toml")
        from tests.toml_helpers import toml_str, write_toml_config

        write_toml_config(
            self.cfg_path,
            f"""
[paths]
cache_db = {toml_str(os.path.join(tmp, "cache.db"))}
knowledge_db = {toml_str(os.path.join(tmp, "knowledge.db"))}

[mcp]
allowed_paths = [{toml_str(tmp)}]

[network]
enabled = true
bind = "127.0.0.1"
port = {self.port}
token = {toml_str(self.token)}

[network.clients.network-client]
allowed_paths = [{toml_str(tmp)}]
allow_terminal = false
""",
        )
        env = dict(os.environ)
        env["TOOLRECALL_CONFIG"] = self.cfg_path
        env.pop("TOOLRECALL_NETWORK_TOKEN", None)
        # Daemon reads TOOLRECALL_TRANSPORT for its own socket; keep UDS local
        self.sock_path = os.path.join(tmp, "uds.sock")
        env["TOOLRECALL_UDS_PATH"] = self.sock_path
        env["XDG_RUNTIME_DIR"] = tmp
        from toolrecall.daemon import DaemonServer

        # Run the daemon in-process (same pattern as test_daemon_pid_guard):
        # subprocess would not inherit the patched config path cleanly.
        env_overrides = {
            "TOOLRECALL_CONFIG": self.cfg_path,
            "TOOLRECALL_UDS_PATH": self.sock_path,
            "XDG_RUNTIME_DIR": tmp,
        }
        for k, v in env_overrides.items():
            os.environ[k] = v
        self.daemon = DaemonServer(socket_path=self.sock_path)
        self.thread = threading.Thread(target=self.daemon.start, daemon=True)
        self.thread.start()
        # Wait for the TCP listener
        deadline = time.time() + 15
        while time.time() < deadline:
            tcp = getattr(self.daemon, "_tcp_port", None)
            if tcp:
                self.port = tcp
                break
            time.sleep(0.1)
        # Wait for UDS too
        deadline = time.time() + 15
        while time.time() < deadline:
            if os.path.exists(self.sock_path):
                break
            time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        try:
            self.daemon.stop()
        except Exception:
            pass
        for k in ("TOOLRECALL_CONFIG", "TOOLRECALL_UDS_PATH", "XDG_RUNTIME_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()


def test_shared_cache_local_write_remote_read():
    """Write via local UDS client; read the SAME entry via remote TCP."""
    from toolrecall.cache import _file_cache, _init as _cache_init

    with NetworkDaemonE2E() as nd:
        # A6-7 namespacing tests share this process: drop any cross-test
        # in-memory entries so the shared-cache flow starts cold.
        _file_cache.clear()
        _cache_init()
        # Local client over UDS (no token — local path needs none)
        local = TransportClient(nd.sock_path)
        test_file = os.path.join(nd._tmp.name, "shared.txt")
        with open(test_file, "w") as f:
            f.write("team-content-1234")
        r1 = local.send({"cmd": "cached_read", "path": test_file})
        assert "content" in r1, r1

        # Remote client over TCP with the token reads the SAME path.
        # Same machine -> same content by construction (the honest case:
        # shared filesystem / identical checkout, see ARCHITECTURE §3.3).
        r2 = _tcp_request(nd.port, nd.token.encode(), {"cmd": "cached_read", "path": test_file})
        assert "error" not in r2, r2
        assert r2.get("content") == "team-content-1234"
        assert r2.get("cached") is True, r2  # served from the shared cache


def test_remote_without_token_rejected():
    with NetworkDaemonE2E() as nd:
        c = socket.create_connection(("127.0.0.1", nd.port), timeout=5)
        try:
            c.settimeout(5)
            # speak framed protocol without authenticating
            from toolrecall.transport import send_message

            send_message(c, {"cmd": "ping"})
            # daemon must refuse to serve. The client is speaking framed
            # JSON but the server is in HMAC-handshake state: the client's
            # frame reads back as handshake garbage (or the connection is
            # reset). The INVARIANT is: no valid {pong: true} reply.
            data = b""
            try:
                while len(data) < 4096:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except (ConnectionResetError, OSError):
                pass
            ponged = False
            try:
                from toolrecall.transport import _MAX_MSG_SIZE

                if len(data) >= 4:
                    import struct as _s

                    (n,) = _s.unpack("!I", data[:4])
                    if 0 < n <= _MAX_MSG_SIZE and len(data) >= 4 + n:
                        ponged = json.loads(data[4 : 4 + n]).get("pong") is True
            except (ValueError, json.JSONDecodeError):
                ponged = False
            assert not ponged
        finally:
            c.close()


def test_remote_ping_roundtrip():
    with NetworkDaemonE2E() as nd:
        resp = _tcp_request(nd.port, nd.token.encode(), {"cmd": "ping"})
        assert resp.get("pong") is True
        assert resp.get("protocol_version") == 1


def test_remote_wrong_token_rejected():
    with NetworkDaemonE2E() as nd:
        import pytest

        c = socket.create_connection(("127.0.0.1", nd.port), timeout=5)
        try:
            c.settimeout(5)
            with pytest.raises(Exception):
                na.client_handshake(c, b"not-the-token")
        finally:
            c.close()
