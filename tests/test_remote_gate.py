"""Tests for the remote security gate (A6 Task 2.5).

Remote = untrusted-ish: policy comes ONLY from [network.clients], never
from the global [mcp] settings. A token-id with no entry is denied
everything. Local UDS behavior is unchanged.
"""

import socket
import threading


from toolrecall import network_auth as na
from toolrecall.config import Config
from toolrecall.daemon import DaemonServer, SecurityGate


class FakeCfg:
    """Minimal config double for gate-level tests."""

    def __init__(self, network_clients=None, allowed_paths=None, allow_terminal=True):
        self.mcp_allowed_paths = allowed_paths or []
        self.mcp_allow_terminal = allow_terminal
        self.mcp_allowed_terminal_commands = []
        self.mcp_allow_invalidate = False
        self.mcp_multiplex_enabled = False
        self.mcp_multiplex_servers = []
        self.mcp_client_policy = lambda: {}
        self.mcp_emit_context_hints = False
        self.mcp_client_hint_policy = {}
        self.mcp_tool_access_control = False
        self.mcp_dangerous_tool_keywords = None
        self.mcp_cognitive_check_enabled = False
        self.mcp_ast_check_enabled = False
        self.network_clients = network_clients or {}


class TestRemoteGateUnit:
    def test_remote_default_deny(self):
        g = SecurityGate(FakeCfg(allowed_paths=["/home"], allow_terminal=True))
        g._enter_remote_mode()
        eff = g.effective_policy("unknown-token")
        assert eff["allowed_paths"] == []
        assert eff["allow_terminal"] is False

    def test_remote_scoped_to_policy(self):
        g = SecurityGate(FakeCfg(network_clients={"ci": {"allowed_paths": ["/repo"]}}))
        g._enter_remote_mode()
        eff = g.effective_policy("ci")
        assert eff["allowed_paths"] == ["/repo"]
        assert eff["allow_terminal"] is False  # not overridden -> denied

    def test_remote_terminal_needs_explicit(self):
        g = SecurityGate(FakeCfg(network_clients={"ci": {"allow_terminal": True}}))
        g._enter_remote_mode()
        assert g.effective_policy("ci")["allow_terminal"] is True
        # another token-id still denied
        assert g.effective_policy("other")["allow_terminal"] is False

    def test_local_mode_unchanged(self):
        g = SecurityGate(FakeCfg(allowed_paths=["/home"], allow_terminal=True))
        eff = g.effective_policy(None)
        assert eff["allowed_paths"] == ["/home"]
        assert eff["allow_terminal"] is True

    def test_remote_read_path_denied(self):
        g = SecurityGate(
            FakeCfg(
                allowed_paths=["/home"],
                allow_terminal=True,
                network_clients={"ci": {"allowed_paths": ["/repo"]}},
            )
        )
        g._enter_remote_mode()
        assert g.check_read_path_for("/home/x/y", "unknown-token") is not None
        assert g.check_read_path_for("/repo/src/a.py", "ci") is None

    def test_remote_terminal_denied(self):
        g = SecurityGate(FakeCfg(allowed_paths=["/home"], allow_terminal=True))
        g._enter_remote_mode()
        assert g.check_terminal_for("ls", "unknown-token") is not None
        assert g.check_terminal_for("ls", "ci") is not None  # no override -> denied


def _net_daemon(tmp_path, monkeypatch, toml_body: str):
    """Boot the real TCP listener with [network] config from a TOML file."""
    cfg_file = tmp_path / "toolrecall.toml"
    cfg_file.write_text(toml_body)
    import toolrecall.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "load_config", lambda: Config(str(cfg_file)))
    d = DaemonServer(socket_path=str(tmp_path / "t.sock"))
    d._server = None
    d._running = True
    from concurrent.futures import ThreadPoolExecutor

    d._executor = ThreadPoolExecutor(max_workers=8)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((d.cfg.network_bind, 0))
    srv.listen(16)
    port = srv.getsockname()[1]
    d.security._enter_remote_mode()

    def _listener():
        while d._running:
            srv.settimeout(0.5)
            try:
                conn, _ = srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            d._executor.submit(d._handle_tcp, conn)

    t = threading.Thread(target=_listener, daemon=True)
    t.start()
    return d, srv, port


def _remote_request(port, token=b"tok-123", payload=None):
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        c.settimeout(5)
        na.client_handshake(c, token)
        from toolrecall.transport import send_message, receive_message

        send_message(c, payload or {"cmd": "cached_terminal", "command": "ls"})
        return receive_message(c)
    finally:
        c.close()


def _stop(d, srv):
    d._running = False
    srv.close()
    d._executor.shutdown(wait=False)


class TestRemoteGateE2E:
    """Note on config path: DaemonServer reads env overrides on top of the
    package-default config; a temp TOML is injected via TOOLRECALL_CONFIG
    so [network.clients] reaches the SecurityGate."""

    def test_remote_terminal_denied_by_default(self, tmp_path, monkeypatch):
        # Global policy: terminal ALLOWED (proves remote mode ignores it)
        toml = """
[mcp]
allowed_paths = ["/"]
allow_terminal = true

[network]
enabled = true
token = "tok-123"
"""
        d, srv, port = _net_daemon(tmp_path, monkeypatch, toml)
        try:
            resp = _remote_request(port)
            # Remote default-deny: no [network.clients] entry -> terminal refused
            assert resp.get("error") or resp.get("ok") is False
        finally:
            _stop(d, srv)

    def test_remote_scoped_to_policy(self, tmp_path, monkeypatch):
        toml = """
[mcp]
allowed_paths = ["/nonexistent-locked-path"]
allow_terminal = false

[network]
enabled = true
token = "tok-123"

[network.clients.ci]
allowed_paths = ["/tmp"]
allow_terminal = true
"""
        d, srv, port = _net_daemon(tmp_path, monkeypatch, toml)
        try:
            resp = _remote_request(port, payload={"cmd": "ping"})
            assert resp.get("pong") is True
            # remote token-id resolves under the network policy, and the
            # global lock-down (allow_terminal=false, no paths) does NOT apply
            g = d.security
            eff = g.effective_policy("ci")
            assert eff["allow_terminal"] is True
            assert eff["allowed_paths"] == ["/tmp"]
        finally:
            _stop(d, srv)

    def test_remote_denied_without_clients_entry(self, tmp_path, monkeypatch):
        toml = """
[network]
enabled = true
token = "tok-123"
"""
        d, srv, port = _net_daemon(tmp_path, monkeypatch, toml)
        try:
            resp = _remote_request(port, payload={"cmd": "cached_read", "path": "/etc/hostname"})
            assert resp.get("error") or resp.get("ok") is False
        finally:
            _stop(d, srv)
