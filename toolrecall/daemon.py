"""ToolRecall Daemon — Zentraler Cache-Prozess mit UDS-Interface + MCP Multiplexer.

Der Daemon hält In-Memory LRU + SQLite und akzeptiert Requests
über Unix Domain Socket. Alle Zugänge (Python-Import, MCP, HTTP)
sprechen mit dem Daemon — ein Cache, immer warm.

Der MCP Multiplexer managt persistente Subprozesse für externe MCP
Server (github, time, fetch, ...). Hermes braucht dann nur noch
einen MCP Server in der Config (toolrecall mcp) statt alle einzeln.

Usage:
    toolrecall daemon [--socket PATH] [--foreground]

Architektur:
    ┌─────────────────────────────────┐
    │  ToolRecall Daemon              │
    │                                 │
    │  ┌──────────┐  ┌────────────┐   │
    │  │ LRU Cache│  │ SQLite     │   │
    │  │ (warm)   │  │ (WAL mode) │   │
    │  └──────────┘  └────────────┘   │
    │  ┌──────────────────────────┐   │
    │  │ MCP Multiplexer          │   │
    │  │ ├── Server A (stdio)     │   │
    │  │ ├── Server B (stdio)     │   │
    │  │ └── ...                  │   │
    │  └──────────────────────────┘   │
    └─────────────────────────────────┘
"""

import json
import os
import socket
import struct
import subprocess
import sys
import signal
import threading
import time
from pathlib import Path

from toolrecall.cache import (
    cached_read as _cache_read,
    cached_terminal as _cache_terminal,
    cached_skill as _cache_skill,
    cached_mcp_check as _cache_mcp_check,
    cached_mcp_store as _cache_mcp_store,
    invalidate_all,
    get_stats,
)
from toolrecall.docs import docs_search as _docs_search, docs_get_page as _docs_get_page
from toolrecall.config import load_config

# ─── Defaults ─────────────────────────────────────────────

PID_FILE = os.path.expanduser("~/.toolrecall/daemon.pid")


def _default_socket_path():
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "toolrecall.sock")
    home = Path.home() / ".toolrecall"
    home.mkdir(parents=True, exist_ok=True)
    return str(home / "toolrecall.sock")


# ─── Security Gates ───────────────────────────────────────


class SecurityGate:
    """Prüft Requests gegen die konfigurierten Sicherheitsregeln.

    - cached_read: nur in allowed_paths
    - cached_terminal: nur wenn allow_terminal=true
    - cache_invalidate: nur wenn allow_invalidate=true
    - mcp_call: nur erlaubte Server
    """

    def __init__(self, cfg):
        self.allowed_paths = cfg.mcp_allowed_paths
        self.allow_terminal = cfg.mcp_allow_terminal
        self.allow_invalidate = cfg.mcp_allow_invalidate
        self.allow_multiplex = cfg.mcp_multiplex_enabled
        self.allowed_servers = [s.lower() for s in cfg.mcp_multiplex_servers]

    def check_read_path(self, path: str) -> str:
        """Prüft ob path gelesen werden darf. Gibt None oder Fehlermeldung."""
        if not self.allowed_paths:
            return None  # Alles erlaubt (DANGEROUS — aber konfiguriert)
        abs_path = os.path.abspath(os.path.expanduser(path))
        for allowed in self.allowed_paths:
            allowed_abs = os.path.abspath(os.path.expanduser(allowed))
            if abs_path == allowed_abs or abs_path.startswith(allowed_abs + os.sep):
                return None
        return f"Path not allowed: {path}"

    def check_terminal(self) -> str:
        if not self.allow_terminal:
            return "cached_terminal is disabled. Set mcp.allow_terminal=true in config."
        return None

    def check_invalidate(self) -> str:
        if not self.allow_invalidate:
            return "cache_invalidate is disabled. Set mcp.allow_invalidate=true in config."
        return None

    def check_mcp_server(self, server: str) -> str:
        if not self.allow_multiplex:
            return "MCP multiplexer is disabled."
        if self.allowed_servers and server.lower() not in self.allowed_servers:
            return f"MCP server '{server}' not in allowed_servers."
        return None


# ─── MCP Multiplexer ──────────────────────────────────────


class MCPClientSession:
    """Manages a persistent MCP subprocess over stdio JSON-RPC.

    Handles connect, keepalive, reconnect (max 3 attempts),
    and proper shutdown.
    """

    def __init__(self, name: str, command: str, args: list, env: dict = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._reconnect_count = 0
        self._max_reconnects = 3
        self._req_id = 0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self):
        """Start the subprocess."""
        full_env = os.environ.copy()
        full_env.update(self.env)
        self._proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
        )

    def _send_raw(self, payload: dict) -> dict:
        """Send JSON-RPC line, read response line. Thread-safe."""
        with self._lock:
            if not self.running:
                self._start()
            self._req_id += 1
            payload["id"] = self._req_id
            line = json.dumps(payload) + "\n"
            try:
                self._proc.stdin.write(line.encode("utf-8"))
                self._proc.stdin.flush()
                resp_line = self._proc.stdout.readline()
                if not resp_line:
                    raise ConnectionError("Empty response from subprocess")
                return json.loads(resp_line.decode("utf-8"))
            except Exception as e:
                # Attempt reconnect once
                if self._reconnect_count < self._max_reconnects:
                    self._reconnect_count += 1
                    self._proc = None
                    self._start()
                    return self._send_raw(payload)
                raise

    def initialize(self) -> dict:
        """Send initialize handshake."""
        return self._send_raw({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "toolrecall", "version": "0.3.0"},
            },
        })

    def list_tools(self) -> list:
        """Fetch tools/list from the subprocess."""
        resp = self._send_raw({
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
        })
        result = resp.get("result", {})
        return result.get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict = None) -> dict:
        """Call a tool on the subprocess."""
        resp = self._send_raw({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
            },
        })
        return resp

    def shutdown(self):
        """Graceful shutdown."""
        if self.running:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
        self._proc = None


class MCPMultiplexer:
    """Manages multiple persistent MCP subprocesses.

    Reads Hermes-style MCP server configs from ToolRecall config
    and starts each as a persistent subprocess. Responses are
    cached via ToolRecall's shared LRU+SQLite.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._sessions: dict[str, MCPClientSession] = {}
        self._initialized = False

    def _start_servers(self):
        """Start and initialize all configured MCP servers."""
        servers = self.cfg.mcp_multiplex_servers_config
        if not servers:
            return

        for name, srv_config in servers.items():
            name_lower = name.lower()
            if self.cfg.mcp_multiplex_servers:
                if name_lower not in [s.lower() for s in self.cfg.mcp_multiplex_servers]:
                    continue
            try:
                session = MCPClientSession(
                    name=name,
                    command=srv_config.get("command", ""),
                    args=srv_config.get("args", []),
                    env=srv_config.get("env", {}),
                )
                # Initialize handshake
                init_resp = session.initialize()
                if "error" in init_resp:
                    print(f"  ⚠ {name}: init failed — {init_resp['error'].get('message', 'unknown')}", file=sys.stderr)
                    continue
                # Fetch available tools
                tools = session.list_tools()
                print(f"  ✓ {name}: {len(tools)} tools available", file=sys.stderr)
                self._sessions[name_lower] = session
            except Exception as e:
                print(f"  ⚠ {name}: start failed — {e}", file=sys.stderr)
        self._initialized = True

    def shutdown(self):
        """Shutdown all managed subprocesses."""
        for name, session in self._sessions.items():
            try:
                session.shutdown()
                print(f"  ✗ {name}: stopped", file=sys.stderr)
            except Exception:
                pass
        self._sessions.clear()

    def list_servers(self) -> list:
        """List connected servers with tool counts."""
        result = []
        for name, session in self._sessions.items():
            try:
                tools = session.list_tools()
                result.append({
                    "name": name,
                    "running": session.running,
                    "tools": len(tools),
                    "tool_names": [t.get("name") for t in tools],
                })
            except Exception as e:
                result.append({
                    "name": name,
                    "running": False,
                    "tools": 0,
                    "tool_names": [],
                    "error": str(e),
                })
        return result

    def call(self, server: str, tool: str, arguments: dict = None) -> dict:
        """Call a tool on a multiplexed server."""
        server_lower = server.lower()
        session = self._sessions.get(server_lower)
        if not session:
            return {"error": f"MCP server '{server}' not connected"}
        resp = session.call_tool(tool, arguments or {})
        return resp.get("result", resp)


# ─── UDS Server ───────────────────────────────────────────


class DaemonServer:
    """Unix Domain Socket Server — ein Thread pro Connection."""

    def __init__(self, socket_path: str = None):
        self.socket_path = socket_path or _default_socket_path()
        self.cfg = load_config()
        self.security = SecurityGate(self.cfg)
        self.multiplexer = MCPMultiplexer(self.cfg)
        self._server = None
        self._running = False

    def start(self):
        """Start the UDS server (blocking)."""
        # Remove stale socket
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(10)
        os.chmod(self.socket_path, 0o700)  # Nur Owner

        self._running = True

        print(f"ToolRecall Daemon v0.3.0", file=sys.stderr)
        print(f"  Socket: {self.socket_path}", file=sys.stderr)
        print(f"  PID: {os.getpid()}", file=sys.stderr)
        print(f"  Path whitelist: {', '.join(self.security.allowed_paths) if self.security.allowed_paths else 'ALL (DANGEROUS)'}", file=sys.stderr)
        print(f"  Terminal: {'ENABLED' if self.security.allow_terminal else 'DISABLED'}", file=sys.stderr)
        print(f"  Invalidate: {'ENABLED' if self.security.allow_invalidate else 'DISABLED'}", file=sys.stderr)

        # Start MCP Multiplexer
        if self.cfg.mcp_multiplex_enabled:
            print(f"\nMCP Multiplexer:", file=sys.stderr)
            self.multiplexer._start_servers()
        print(file=sys.stderr)

        while self._running:
            try:
                conn, addr = self._server.accept()
                t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
                t.start()
            except OSError:
                break  # Server closed

    def stop(self):
        """Graceful shutdown."""
        self._running = False
        self.multiplexer.shutdown()
        try:
            self._server.close()
        except Exception:
            pass
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        print("ToolRecall Daemon stopped.", file=sys.stderr)

    def _handle(self, conn: socket.socket):
        """Handle one client connection."""
        try:
            conn.settimeout(30.0)
            # Read 4-byte length prefix + payload
            raw_len = self._recv_exact(conn, 4)
            if not raw_len:
                conn.close()
                return
            msg_len = struct.unpack("!I", raw_len)[0]
            if msg_len > 1024 * 1024:  # Max 1MB request
                self._send_response(conn, {"error": "Request too large"})
                conn.close()
                return

            raw_data = self._recv_exact(conn, msg_len)
            if not raw_data:
                conn.close()
                return

            request = json.loads(raw_data.decode("utf-8"))
            response = self._route(request)
            self._send_response(conn, response)

        except (socket.timeout, json.JSONDecodeError, ConnectionResetError, BrokenPipeError) as e:
            try:
                self._send_response(conn, {"error": str(e)})
            except Exception:
                pass
        except Exception as e:
            try:
                self._send_response(conn, {"error": str(e)})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _recv_exact(self, conn: socket.socket, n: int) -> bytes:
        """Receive exactly n bytes."""
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = conn.recv(min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_response(self, conn: socket.socket, data: dict):
        """Send length-prefixed JSON response."""
        payload = json.dumps(data).encode("utf-8")
        conn.sendall(struct.pack("!I", len(payload)) + payload)

    def _route(self, request: dict) -> dict:
        """Route a request to the appropriate handler."""
        cmd = request.get("cmd", "")

        try:
            if cmd == "cached_read":
                return self._handle_read(request)
            elif cmd == "cached_terminal":
                return self._handle_terminal(request)
            elif cmd == "cached_skill":
                return self._handle_skill(request)
            elif cmd == "docs_search":
                return self._handle_docs_search(request)
            elif cmd == "docs_get_page":
                return self._handle_docs_get_page(request)
            elif cmd == "cache_status":
                return self._handle_status(request)
            elif cmd == "cache_invalidate":
                return self._handle_invalidate(request)
            elif cmd == "mcp_call":
                return self._handle_mcp_call(request)
            elif cmd == "mcp_list_servers":
                return self._handle_mcp_list_servers(request)
            elif cmd == "ping":
                return self._handle_ping(request)
            else:
                return {"error": f"Unknown command: {cmd}"}

        except Exception as e:
            return {"error": str(e)}

    def _handle_ping(self, req: dict) -> dict:
        return {
            "pong": True,
            "pid": os.getpid(),
            "allowed_paths": self.security.allowed_paths,
            "allow_terminal": self.security.allow_terminal,
            "allow_invalidate": self.security.allow_invalidate,
            "multiplex_enabled": self.security.allow_multiplex,
            "multiplex_servers": list(self.multiplexer._sessions.keys()),
        }

    def _handle_read(self, req: dict) -> dict:
        path = req.get("path", "")
        if not path:
            return {"error": "Missing 'path'"}
        err = self.security.check_read_path(path)
        if err:
            return {"error": err}
        return _cache_read(path)

    def _handle_terminal(self, req: dict) -> dict:
        err = self.security.check_terminal()
        if err:
            return {"error": err}
        command = req.get("command", "")
        if not command:
            return {"error": "Missing 'command'"}
        ttl = req.get("ttl")
        return _cache_terminal(command, ttl=ttl)

    def _handle_skill(self, req: dict) -> dict:
        name = req.get("name", "")
        if not name:
            return {"error": "Missing 'name'"}
        return _cache_skill(name)

    def _handle_docs_search(self, req: dict) -> dict:
        query = req.get("query", "")
        if not query:
            return {"error": "Missing 'query'"}
        source = req.get("source")
        result = _docs_search(query, source=source)
        return {"result": result}

    def _handle_docs_get_page(self, req: dict) -> dict:
        source = req.get("source", "")
        path = req.get("path", "")
        if not source or not path:
            return {"error": "Missing 'source' or 'path'"}
        result = _docs_get_page(source, path)
        return {"result": result}

    def _handle_status(self, req: dict) -> dict:
        stats = get_stats()
        return {"result": stats}

    def _handle_invalidate(self, req: dict) -> dict:
        err = self.security.check_invalidate()
        if err:
            return {"error": err}
        invalidate_all()
        return {"result": "All ToolRecall caches cleared"}

    def _handle_mcp_call(self, req: dict) -> dict:
        """Call a tool on a multiplexed MCP server.

        Request: {"cmd": "mcp_call", "server": "github", "tool": "list_issues", "arguments": {...}}
        """
        err = self.security.check_mcp_server(req.get("server", ""))
        if err:
            return {"error": err}

        server = req.get("server", "")
        tool = req.get("tool", "")
        arguments = req.get("arguments", {})

        if not server:
            return {"error": "Missing 'server'"}
        if not tool:
            return {"error": "Missing 'tool'"}

        # Check cache first
        cached = _cache_mcp_check(server, tool, arguments)
        if cached.get("cached"):
            return {"result": cached["data"], "cached": True}

        # Call the multiplexed server
        result = self.multiplexer.call(server, tool, arguments)

        # Store in cache
        import json as _json
        result_json = _json.dumps(result)
        _cache_mcp_store(cached["key"], result_json)

        return {"result": result, "cached": False}

    def _handle_mcp_list_servers(self, req: dict) -> dict:
        """List available multiplexed MCP servers with their tools."""
        err = self.security.check_mcp_server("_any")
        if err:
            return {"error": err}
        servers = self.multiplexer.list_servers()
        return {"result": servers}


# ─── Entry Points ─────────────────────────────────────────

_server_instance = None


def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _server_instance
    if _server_instance:
        _server_instance.stop()
    sys.exit(0)


def run_daemon(socket_path: str = None, foreground: bool = False):
    """Start the ToolRecall daemon."""
    global _server_instance

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    _server_instance = DaemonServer(socket_path)

    if not foreground:
        # Daemonize: fork, exit parent
        pid = os.fork()
        if pid > 0:
            # Write PID file
            os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
            with open(PID_FILE, "w") as f:
                f.write(str(pid))
            print(f"ToolRecall Daemon started (PID: {pid})")
            print(f"  Socket: {_server_instance.socket_path}")
            sys.exit(0)

    _server_instance.start()


def stop_daemon():
    """Stop a running daemon via PID file."""
    if not os.path.exists(PID_FILE):
        print("No PID file found. Is the daemon running?")
        return

    with open(PID_FILE) as f:
        pid = int(f.read().strip())

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent stop signal to PID {pid}")
    except ProcessLookupError:
        print(f"Daemon (PID {pid}) not running. Cleaning up PID file.")
    finally:
        os.unlink(PID_FILE)


def daemon_status():
    """Print daemon status."""
    if not os.path.exists(PID_FILE):
        print("ToolRecall Daemon: NOT RUNNING")
        return

    with open(PID_FILE) as f:
        pid = int(f.read().strip())

    try:
        os.kill(pid, 0)  # Test if process exists
        print(f"ToolRecall Daemon: RUNNING (PID {pid})")
        print(f"  Socket: {_default_socket_path()}")

        # Try to ping the daemon
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(_default_socket_path())
            data = json.dumps({"cmd": "ping"}).encode("utf-8")
            sock.sendall(struct.pack("!I", len(data)) + data)
            raw_len = sock.recv(4)
            if raw_len:
                msg_len = struct.unpack("!I", raw_len)[0]
                resp = json.loads(sock.recv(msg_len).decode("utf-8"))
                sock.close()
                if resp.get("pong"):
                    print(f"  PID from socket: {resp.get('pid')}")
                    print(f"  Path whitelist: {resp.get('allowed_paths', [])}")
                    print(f"  Terminal enabled: {resp.get('allow_terminal', False)}")
                    print(f"  MCP Multiplex: {'ENABLED' if resp.get('multiplex_enabled') else 'DISABLED'}")
                    servers = resp.get('multiplex_servers', [])
                    if servers:
                        print(f"  Multiplex servers: {', '.join(servers)}")
                return
            sock.close()
        except Exception:
            print("  ⚠ PID file exists but daemon is not responding")
            print("  → Run 'toolrecall daemon --stop' to clean up")
    except ProcessLookupError:
        print("ToolRecall Daemon: PID FILE STALE (process not found)")
        print("  → Run 'toolrecall daemon --stop' to clean up")
