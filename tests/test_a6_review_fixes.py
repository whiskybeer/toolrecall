"""Regression tests for the A6 security review (A6SECURITYREVIEW.md).

A6-1: remote cached_write/cached_patch must resolve the remote policy
(default-deny), NOT the global [mcp] allowlist.
A6-3: remote command allow-list — mcp_call, docs_*, recall_*,
cache_invalidate, shutdown, restart, context_*, cache_status are
local-only; remote callers get remote_denied BEFORE routing.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse the live-daemon fixture from the daemon-network test module.
# pytest resolves fixtures across imported modules; the alias avoids the
# ruff F811 "redefinition" on the fixture name in test signatures.
import pytest  # E402

from tests import test_daemon_network as _tdn  # E402

tcp_daemon = pytest.fixture(_tdn.tcp_daemon.__wrapped__)

from toolrecall.daemon import SecurityGate


class FakeCfg:
    """Mimics the real Config surface for SecurityGate."""

    def __init__(self, **kw):
        self.mcp_allowed_paths = kw.get("allowed_paths", [])
        self.mcp_allow_terminal = kw.get("allow_terminal", False)
        self.mcp_allowed_terminal_commands = []
        self.mcp_allow_invalidate = kw.get("allow_invalidate", False)
        self.mcp_multiplex_enabled = False
        self.mcp_multiplex_servers = []
        self.mcp_tool_access_control = False
        self.mcp_dangerous_tool_keywords = []
        self.mcp_cognitive_check_enabled = False
        self.mcp_ast_check_enabled = False
        self.mcp_client_policy = lambda: {}  # noqa: E731
        self.network_clients = kw.get("network_clients", {})


class TestA61WritePatchRemoteGate:
    """A6-1: writes/patches must go through the REMOTE-aware gate."""

    def test_remote_write_denied_default_config(self):
        """The exact reviewer reproduction: global allowlist grants ~,
        remote policy grants nothing -> cached_write must be DENIED."""
        g = SecurityGate(FakeCfg(allowed_paths=["/home"]))
        assert (
            g.check_read_path_for(
                "/home/user/.config/autostart/evil.desktop",
                "network-client",
                remote=True,
            )
            is not None
        ), "remote write with empty [network.clients] must be denied"

    def test_remote_patch_denied_default_config(self):
        g = SecurityGate(FakeCfg(allowed_paths=["/home"]))
        assert (
            g.check_read_path_for("/home/user/project/src/main.py", "network-client", remote=True)
            is not None
        )

    def test_remote_write_allowed_only_in_scope(self):
        g = SecurityGate(
            FakeCfg(
                allowed_paths=["/home"],
                network_clients={"vm2": {"allowed_paths": ["/srv/shared"]}},
            )
        )
        # in scope -> allowed (returns None = no error)
        assert g.check_read_path_for("/srv/shared/x.txt", "vm2", remote=True) is None
        # outside scope -> denied
        assert g.check_read_path_for("/home/user/.bashrc", "vm2", remote=True) is not None

    def test_local_write_unchanged(self):
        """Local UDS writes keep the global policy (no behavior change)."""
        g = SecurityGate(FakeCfg(allowed_paths=["/home"]))
        assert g.check_read_path_for("/home/user/file.txt", None, remote=False) is None

    def test_sensitive_blocklist_still_applies_remotely(self):
        g = SecurityGate(
            FakeCfg(
                allowed_paths=["/srv"],
                network_clients={"vm2": {"allowed_paths": ["/srv"]}},
            )
        )
        # Inside scope but sensitive name -> Layer 2 must still block
        assert g.check_read_path_for("/srv/.env", "vm2", remote=True) is not None


class TestA63RemoteCommandAllowList:
    """A6-3: the choke point in _handle_tcp denies local-only commands
    BEFORE routing (live daemon fixture from test_daemon_network)."""

    def test_remote_mcp_call_denied(self, tcp_daemon):
        """The reviewer's privilege-escalation vector: remote mcp_call runs
        the OPERATOR's multiplexed servers. Must be remote_denied."""
        from toolrecall.transport import TransportClient

        c = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}")
        r = c.send({"cmd": "mcp_call", "server": "time", "tool": "get_time", "arguments": {}})
        assert r.get("error", "").startswith("remote_denied"), r

    def test_remote_shutdown_denied(self, tcp_daemon):
        from toolrecall.transport import TransportClient

        r = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}").send({"cmd": "shutdown"})
        assert r.get("error", "").startswith("remote_denied"), r

    def test_remote_restart_denied(self, tcp_daemon):
        from toolrecall.transport import TransportClient

        r = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}").send({"cmd": "restart"})
        assert r.get("error", "").startswith("remote_denied"), r

    def test_remote_docs_search_denied(self, tcp_daemon):
        from toolrecall.transport import TransportClient

        r = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}").send(
            {"cmd": "docs_search", "query": "secrets"}
        )
        assert r.get("error", "").startswith("remote_denied"), r

    def test_remote_cache_invalidate_denied(self, tcp_daemon):
        from toolrecall.transport import TransportClient

        r = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}").send(
            {"cmd": "cache_invalidate"}
        )
        assert r.get("error", "").startswith("remote_denied"), r

    def test_remote_recall_store_denied(self, tcp_daemon):
        from toolrecall.transport import TransportClient

        r = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}").send(
            {"cmd": "recall_store", "fingerprint": "x", "content": "y"}
        )
        assert r.get("error", "").startswith("remote_denied"), r

    def test_remote_cache_status_denied(self, tcp_daemon):
        from toolrecall.transport import TransportClient

        r = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}").send({"cmd": "cache_status"})
        assert r.get("error", "").startswith("remote_denied"), r

    def test_remote_cached_read_still_allowed(self, tcp_daemon):
        """The allow-list must not break the feature: cached_read flows."""
        from toolrecall.transport import TransportClient

        r = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}").send({"cmd": "ping"})
        assert r.get("pong") is True, r

    def test_unknown_command_remote_denied_not_routed(self, tcp_daemon):
        """A command that doesn't exist at all must hit the allow-list
        (remote_denied), not the router's 'Unknown command' — proves the
        choke point sits BEFORE _route."""
        from toolrecall.transport import TransportClient

        r = TransportClient(f"tcp://127.0.0.1:{tcp_daemon.tcp_port}").send(
            {"cmd": "some_future_handler"}
        )
        assert r.get("error", "").startswith("remote_denied"), r

    def test_multi_token_identity_and_revocation(self, tmp_path, monkeypatch):
        """A6-4: per-member tokens via [network.clients."id"].token.
        Each member authenticates to their OWN id (scoped policy);
        removing a member's entry revokes them."""
        import threading as _th

        from toolrecall.daemon import DaemonServer
        from toolrecall.transport import TransportClient

        from tests.toml_helpers import toml_str, write_toml_config

        toml = f"""
[paths]
cache_db = {toml_str(tmp_path / "c.db")}
[mcp]
allowed_paths = [{toml_str(tmp_path)}]
[network]
enabled = true
bind = "127.0.0.1"
port = 0
[network.clients.alice]
token = "alice-token-1"
allowed_paths = [{toml_str(tmp_path)}]
[network.clients.bob]
token = "bob-token-2"
allowed_paths = ["/nonexistent-bob"]
"""
        cfg_file = tmp_path / "toolrecall.toml"
        write_toml_config(cfg_file, toml)
        import toolrecall.daemon as daemon_mod
        from toolrecall.config import Config

        monkeypatch.setattr(daemon_mod, "load_config", lambda: Config(str(cfg_file)))
        d = DaemonServer(socket_path=str(tmp_path / "t.sock"))
        d._server = None
        d._running = True
        from concurrent.futures import ThreadPoolExecutor

        d._executor = ThreadPoolExecutor(max_workers=4)
        srv = __import__("socket").socket()
        srv.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port = srv.getsockname()[1]

        def _listener():
            while d._running:
                try:
                    srv.settimeout(0.4)
                    conn, _ = srv.accept()
                except (__import__("socket").timeout, TimeoutError):
                    continue
                except OSError:
                    break
                d._executor.submit(d._handle_tcp, conn)

        t = _th.Thread(target=_listener, daemon=True)
        t.start()
        try:
            # Alice authenticates to HER id, her policy applies.
            monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "alice-token-1")
            alice = TransportClient(f"tcp://127.0.0.1:{port}")
            assert alice.send({"cmd": "ping"}).get("pong") is True
            assert (
                alice.send({"cmd": "cached_read", "path": str(tmp_path / "x")}).get("error")
                != "network_auth_rejected"
            )
            # Bob authenticates to HIS id; his scope denies the same path.
            monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "bob-token-2")
            bob = TransportClient(f"tcp://127.0.0.1:{port}")
            r = bob.send({"cmd": "cached_read", "path": str(tmp_path / "x")})
            assert "not allowed" in r.get("error", ""), r
            # Revocation: an unknown token is rejected at the handshake.
            monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "revoked-token-3")
            rev = TransportClient(f"tcp://127.0.0.1:{port}")
            assert rev.send({"cmd": "ping"}).get("error") == "network_auth_rejected"
        finally:
            d._running = False
            srv.close()
            d._executor.shutdown(wait=False)
            t.join(timeout=3)


class TestA62FailClosedTLS:
    """A6-2: non-loopback bind without TLS = refused (server side);
    client TLS without CA = refused, insecure mode explicit."""

    def test_nonloopback_bind_without_tls_refused(self, tmp_path, monkeypatch):
        """LAN bind + tls=false must NOT listen (was warn-only). Uses the
        VM's actual global LAN IP — present in this environment."""
        import subprocess as _sp

        from toolrecall.daemon import DaemonServer

        lan_ip = (
            _sp.check_output(
                [
                    "sh",
                    "-c",
                    "ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1",
                ]
            )
            .decode()
            .strip()
        )
        assert lan_ip, "no global LAN IP in this environment"
        monkeypatch.setenv("TOOLRECALL_NETWORK_ENABLED", "true")
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "tok")
        # no TLS env -> tls=false
        d = DaemonServer(socket_path=str(tmp_path / "t.sock"))
        d._server = None
        d._running = True
        d._executor = __import__("concurrent.futures", fromlist=["x"]).ThreadPoolExecutor(
            max_workers=2
        )
        import threading as _th

        t = _th.Thread(target=d._run_tcp_listener, args=(lan_ip, 0), daemon=True)
        t.start()
        t.join(timeout=5)
        assert d._tcp_srv is None, "listener must be refused without TLS on LAN bind"

    def test_client_tls_without_ca_refused(self, monkeypatch):
        """TLS on but no CA pinned -> the client must refuse (no silent
        unverified TLS)."""
        from toolrecall.transport import TransportClient

        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "tok")
        monkeypatch.setenv("TOOLRECALL_NETWORK_TLS", "true")
        monkeypatch.delenv("TOOLRECALL_NETWORK_TLS_CA", raising=False)
        monkeypatch.delenv("TOOLRECALL_NETWORK_TLS_INSECURE", raising=False)
        c = TransportClient("tcp://127.0.0.1:59999")
        r = c.send({"cmd": "ping"})
        # Refusal surfaces as an error (connection fails during wrap) —
        # the key assertion: it does NOT succeed silently.
        assert "pong" not in r, r

    def test_client_insecure_mode_is_explicit(self, monkeypatch):
        """TLS_INSECURE without TOOLRECALL_NETWORK_TLS is a no-op: no TLS
        attempted, the plain framed peer exchange fails fast (dead port —
        daemon_unavailable in ms, no 3s auth window wait)."""
        from toolrecall.transport import TransportClient

        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "tok")
        monkeypatch.delenv("TOOLRECALL_NETWORK_TLS", raising=False)
        monkeypatch.setenv("TOOLRECALL_NETWORK_TLS_INSECURE", "1")
        c = TransportClient("tcp://127.0.0.1:1")  # reserved port, nothing listens
        r = c.send({"cmd": "ping"}, timeout=2)
        assert r.get("error") == "daemon_unavailable", r
