"""ToolRecall Daemon — Central cache process with UDS interface + MCP Multiplexer.

The daemon holds the In-Memory LRU + SQLite and accepts requests
via Unix Domain Socket. All access paths (Python import, MCP, HTTP)
communicate with the daemon — one cache, always warm.

|The MCP Multiplexer manages persistent subprocesses for external MCP
|servers (github, time, fetch, ...). Agents then only need a single
|MCP server in their config (toolrecall mcp) instead of all individually.

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
import logging
import os
import socket
import struct
import subprocess
import sys
import signal
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from toolrecall.cache import (
    cached_read as _cache_read,
    cached_terminal as _cache_terminal,
    cached_skill as _cache_skill,
    cached_write as _cache_write,
    cached_patch as _cache_patch,
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
    """Check requests against configured security rules.
    
    - cached_read: only within allowed_paths
    - cached_terminal: only if allow_terminal=true
    - cache_invalidate: only if allow_invalidate=true
    - mcp_call: only allowed servers
    """
    def __init__(self, cfg):
        self.cfg = cfg
        self.allowed_paths = cfg.mcp_allowed_paths or []
        self.allow_terminal = cfg.mcp_allow_terminal
        self.allowed_terminal_commands = cfg.mcp_allowed_terminal_commands or []
        self.allow_invalidate = cfg.mcp_allow_invalidate
        self.allow_multiplex = cfg.mcp_multiplex_enabled
        self.allowed_servers = [s.lower() for s in (cfg.mcp_multiplex_servers or [])]
        self.read_only_sandbox = cfg.mcp_read_only_sandbox
        self.dangerous_tool_keywords = cfg.mcp_dangerous_tool_keywords or [
            "write", "edit", "delete", "remove", "terminal",
            "bash", "exec", "run", "push", "commit", "update", "create",
            "sudo", "chmod", "chown", "invalidate", "store", "set",
        ]
        self.logger = logging.getLogger(__name__)

    MAX_PATH_LENGTH = 4096  # POSIX PATH_MAX

    def check_read_path(self, path: str) -> str | None:
        """Check if path is allowed to be read. Returns None or error message."""
        # Reject null bytes — can bypass extension checks
        if "\x00" in path:
            return "Path not allowed: contains null byte"

        # Reject excessively long paths — prevents buffer issues
        if len(path) > self.MAX_PATH_LENGTH:
            return "Path not allowed: exceeds maximum length"

        if not self.allowed_paths:
            return None  # All allowed (DANGEROUS — but configured explicitly)

        # Security: Strict symlink resolution to prevent directory traversal escapes
        abs_path = os.path.realpath(os.path.expanduser(path))

        for allowed in self.allowed_paths:
            allowed_abs = os.path.realpath(os.path.expanduser(allowed))
            if abs_path == allowed_abs or abs_path.startswith(allowed_abs + os.sep):
                return None
        # Generic error — never leak the real resolved path to the caller
        return "Path not allowed: access denied"

    def check_terminal(self, cmd: str) -> str | None:
        if not self.allow_terminal:
            return "cached_terminal is disabled. Set mcp.allow_terminal=true in config."
            
        if not self.allowed_terminal_commands:
            # If terminal is allowed but no specific regexes defined, allow all (Binary WAF fallback)
            return None
            
        import re
        for pattern in self.allowed_terminal_commands:
            try:
                if re.search(pattern, cmd):
                    return None
            except re.error as e:
                self.logger.info(f"Warning: Invalid regex in allowed_terminal_commands: '{pattern}' ({e})")
                
        return f"Terminal command not allowed by regex allowlist: {cmd}"

    def check_invalidate(self) -> str | None:
        if not self.allow_invalidate:
            return "cache_invalidate is disabled. Set mcp.allow_invalidate=true in config."
        return None

    def check_mcp_server(self, server: str) -> str | None:
        if not self.allow_multiplex:
            return "MCP multiplexer is disabled."
        if self.allowed_servers and server.lower() not in self.allowed_servers:
            return f"MCP server '{server}' not in allowed_servers."
        return None

    def check_mcp_tool_sandbox(self, tool_name: str) -> str | None:
        """Ultimate Sandbox WAF: If enabled, blocks tools that modify state."""
        if not self.read_only_sandbox:
            return None
        
        t_lower = tool_name.lower()
        for kw in self.dangerous_tool_keywords:
            if kw.lower() in t_lower:
                return f"ToolRecall Sandbox WAF: Execution of modifying tool '{tool_name}' is blocked in read-only mode."
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
        """Start the subprocess with env vars from ~/.toolrecall/.env."""
        full_env = os.environ.copy()
        full_env.update(self.env)
        # Load env vars from ~/.toolrecall/.env for persistent secrets
        env_file = os.path.expanduser("~/.toolrecall/.env")
        if os.path.exists(env_file):
            try:
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, val = line.split("=", 1)
                            key, val = key.strip(), val.strip().strip('"').strip("'")
                            if key and val:
                                full_env[key] = val
            except Exception:
                pass
        # Resolve $AGENT_HOME and $HOME in args for agent-agnostic paths
        resolved_args = []
        agent_home = full_env.get("AGENT_HOME") or full_env.get("HERMES_HOME") or ""
        for arg in self.args:
            arg = arg.replace("$AGENT_HOME", agent_home).replace("${AGENT_HOME}", agent_home)
            arg = arg.replace("$HERMES_HOME", agent_home).replace("${HERMES_HOME}", agent_home)
            arg = os.path.expanduser(arg)
            resolved_args.append(arg)
        self._proc = subprocess.Popen(
            [self.command, *resolved_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            preexec_fn=os.setsid,  # Start in a new process group for clean killing
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
        """Graceful shutdown of entire process group."""
        import signal
        if self._proc is not None:
            pid = self._proc.pid
            try:
                # Kill the entire process group to eradicate Node.js zombie trees
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception:
                    pass
        self._proc = None


class MCPMultiplexer:
    """Manages multiple MCP subprocesses — lazy start, idle timeout, session handover.

    Servers start on the first mcp_call(). They run persistently across sessions.
    Idle servers are automatically shut down after idle_timeout minutes (saving RAM).
    They are restarted on the next call.

    Usage:
        mux = MCPMultiplexer(cfg)
        mux.call("github", "list_issues", {"owner": "whiskybeer", "repo": "toolrecall"})
        # → first call starts github (~2s), subsequent calls are instant
        # → after 15min idle, github shuts down automatically
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._sessions: dict[str, MCPClientSession] = {}
        self._configs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._idle_timeout = cfg.mcp_multiplex_idle_minutes or 15  # minutes
        self._last_use: dict[str, float] = {}
        self._reaper_running = False

    # ─── Discovery ────────────────────────────────────

    def _discover_configs(self):
        """Read server configurations (runs once)."""
        if self._configs:
            return
        servers = self.cfg.mcp_multiplex_servers_config
        if not servers:
            return
        allow_servers = [s.lower() for s in self.cfg.mcp_multiplex_servers]
        for name, srv_config in servers.items():
            name_lower = name.lower()
            if allow_servers and name_lower not in allow_servers:
                continue
            self._configs[name_lower] = {
                "name": name,
                "command": srv_config.get("command", ""),
                "args": srv_config.get("args", []),
                "env": srv_config.get("env", {}),
            }

    # ─── Lazy Server Start ────────────────────────────

    def _ensure_server(self, name_lower: str) -> str | None:
        """Start server if needed. Returns error message or None on success."""
        with self._lock:
            if name_lower in self._sessions:
                session = self._sessions[name_lower]
                if session.running:
                    return None  # Already running
                # Stale session — restart
                self._sessions.pop(name_lower, None)

            config = self._configs.get(name_lower)
            if not config:
                return f"MCP server '{name_lower}' not configured"

            try:
                session = MCPClientSession(
                    name=config["name"],
                    command=config["command"],
                    args=config["args"],
                    env=config["env"],
                )
                init_resp = session.initialize()
                if "error" in init_resp:
                    return f"{name_lower}: init failed — {init_resp['error'].get('message', 'unknown')}"
                # Fetch tools for warmup
                tools = session.list_tools()
                self._sessions[name_lower] = session
                print(f"  ✓ {name_lower}: {len(tools)} tools started on-demand")
            except Exception as e:
                return f"{name_lower}: start failed — {e}"
        return None

    # ─── Idle Timeout (Reaper) ────────────────────────

    def _start_reaper(self):
        """Background thread: stops idle servers."""
        if self._reaper_running:
            return
        self._reaper_running = True

        def _reaper_loop():
            while True:
                time.sleep(60)  # Check every minute
                now = time.time()
                with self._lock:
                    idle_names = []
                    for name, last_used in list(self._last_use.items()):
                        if name not in self._sessions:
                            continue
                        idle_mins = (now - last_used) / 60
                        if idle_mins >= self._idle_timeout:
                            idle_names.append((name, idle_mins))
                    for name, mins in idle_names:
                        session = self._sessions.pop(name, None)
                        if session:
                            try:
                                session.shutdown()
                                print(f"  💤 {name}: idle shutdown ({int(idle_mins)}min)")
                            except Exception:
                                pass
                            self._last_use.pop(name, None)

        t = threading.Thread(target=_reaper_loop, daemon=True, name="mcp-reaper")
        t.start()

    # ─── Public API ───────────────────────────────────

    def start(self):
        """Initialisiert Konfiguration. Startet KEINE Server (lazy)."""
        self._discover_configs()
        self._start_reaper()
        n = len(self._configs)
        if n:
            print(f"  {n} servers configured (lazy start — first call starts each)")

    def shutdown(self):
        """Shutdown all running servers."""
        with self._lock:
            for name, session in list(self._sessions.items()):
                try:
                    session.shutdown()
                    print(f"  ✗ {name}: stopped")
                except Exception:
                    pass
            self._sessions.clear()
            self._last_use.clear()

    def list_servers(self) -> list:
        """List all configured servers — running, idle, or not yet started."""
        self._discover_configs()
        result = []
        with self._lock:
            for name_lower, config in self._configs.items():
                session = self._sessions.get(name_lower)
                if session and session.running:
                    try:
                        tools = session.list_tools()
                        result.append({
                            "name": config["name"],
                            "running": True,
                            "status": "active",
                            "tools": len(tools),
                            "tool_names": [t.get("name") for t in tools],
                        })
                    except Exception:
                        result.append({
                            "name": config["name"],
                            "running": False,
                            "status": "error",
                            "tools": 0,
                            "tool_names": [],
                        })
                else:
                    result.append({
                        "name": config["name"],
                        "running": False,
                        "status": "idle",
                        "tools": 0,
                        "tool_names": [],
                    })
        return result

    def call(self, server: str, tool: str, arguments: dict = None) -> dict:
        """Call a tool — lazy-starts server on first use, tracks idle."""
        server_lower = server.lower()

        # Ensure config known
        self._discover_configs()
        if server_lower not in self._configs:
            return {"error": f"MCP server '{server}' is not configured"}

        # Lazy start
        err = self._ensure_server(server_lower)
        if err:
            return {"error": err}

        # Mark used (for idle timeout)
        self._last_use[server_lower] = time.time()

        # Call
        with self._lock:
            session = self._sessions.get(server_lower)
            if not session:
                return {"error": f"MCP server '{server}' not available"}
                
        # Execute outside global multiplexer lock (session has its own lock)
        resp = session.call_tool(tool, arguments or {})
        
        with self._lock:
            self._last_use[server_lower] = time.time()
            
        return resp.get("result", resp)


# ─── UDS Server ───────────────────────────────────────────


class DaemonServer:
    """Unix Domain Socket Server — ThreadPoolExecutor für Connection-Handling."""

    def __init__(self, socket_path: str = None):
        self.socket_path = socket_path or _default_socket_path()
        self.cfg = load_config()
        self.security = SecurityGate(self.cfg)
        self.multiplexer = MCPMultiplexer(self.cfg)
        self._server = None
        self._running = False
        # Limit worker pool to prevent thread starvation under concurrent agent workflows
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="TRWorker")

    def _run_periodic_gc(self):
        """Runs garbage collection every 4 hours in a background thread."""
        from toolrecall.cache import garbage_collect
        while self._running:
            # Sleep in small increments so we can exit instantly on stop()
            for _ in range(14400):  # 4 hours
                if not self._running:
                    return
                time.sleep(1)
            try:
                garbage_collect()
            except Exception:
                pass

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

        print(f"ToolRecall Daemon v0.3.0")
        print(f"  Socket: {self.socket_path}")
        print(f"  PID: {os.getpid()}")
        print(f"  Path allowlist: {', '.join(self.security.allowed_paths) if self.security.allowed_paths else 'ALL (DANGEROUS)'}")
        print(f"  Terminal: {'ENABLED' if self.security.allow_terminal else 'DISABLED'}")
        print(f"  Invalidate: {'ENABLED' if self.security.allow_invalidate else 'DISABLED'}")

        # Start MCP Multiplexer (lazy — no servers started yet)
        if self.cfg.mcp_multiplex_enabled:
            print(f"\nMCP Multiplexer:")
            self.multiplexer.start()
        print("")

        # Start periodic GC background thread
        self._gc_thread = threading.Thread(target=self._run_periodic_gc, daemon=True)
        self._gc_thread.start()

        while self._running:
            try:
                conn, addr = self._server.accept()
                self._executor.submit(self._handle, conn)
            except OSError:
                break  # Server closed

    def stop(self):
        """Graceful shutdown."""
        self._running = False
        self._executor.shutdown(wait=False)
        self.multiplexer.shutdown()
        try:
            self._server.close()
        except Exception:
            pass
        try:
            os.unlink(self.socket_path)
        except Exception:
            pass
        print("ToolRecall Daemon stopped.")

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

        except (socket.timeout, json.JSONDecodeError, ConnectionResetError, BrokenPipeError):
            # These errors mean the response cannot be sent — that's fine,
            # just close the connection. No send_response attempt needed.
            pass
        except Exception as e:
            # Try to send the error back, but swallow if connection is gone
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
            elif cmd == "cached_write":
                return self._handle_write(request)
            elif cmd == "cached_patch":
                return self._handle_patch(request)
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
        command = req.get("command", "")
        err = self.security.check_terminal(command)
        if err:
            return {"error": err}
        if not command:
            return {"error": "Missing 'command'"}
        ttl = req.get("ttl")
        return _cache_terminal(command, ttl=ttl)

    def _handle_skill(self, req: dict) -> dict:
        name = req.get("name", "")
        if not name:
            return {"error": "Missing 'name'"}
        return _cache_skill(name)

    def _handle_write(self, req: dict) -> dict:
        path = req.get("path", "")
        content = req.get("content", "")
        if not path:
            return {"error": "Missing 'path'"}
        if content is None:
            return {"error": "Missing 'content'"}
        err = self.security.check_read_path(path)
        if err:
            return {"error": err}
        return _cache_write(path, content)

    def _handle_patch(self, req: dict) -> dict:
        path = req.get("path", "")
        old_string = req.get("old_string", "")
        new_string = req.get("new_string", "")
        if not path:
            return {"error": "Missing 'path'"}
        if not old_string:
            return {"error": "Missing 'old_string'"}
        err = self.security.check_read_path(path)
        if err:
            return {"error": err}
        return _cache_patch(path, old_string, new_string)

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

        # Ultimate Sandbox WAF check
        sandbox_err = self.security.check_mcp_tool_sandbox(tool)
        if sandbox_err:
            return {"error": sandbox_err}

        # Check cache (only when transparent cache is enabled)
        cached = {"key": f"{server}:{tool}"}  # default for type checker
        if self.multiplexer.cfg.mcp_multiplex_transparent_cache:
            server_cfg = self.multiplexer.cfg.mcp_multiplex_servers_config.get(server, {})
            ttl = server_cfg.get("ttl", self.multiplexer.cfg.mcp_multiplex_default_ttl)
            cached = _cache_mcp_check(server, tool, arguments, ttl=ttl)
            if cached.get("cached"):
                return {"result": cached["data"], "cached": True}

        # Call the multiplexed server
        result = self.multiplexer.call(server, tool, arguments)

        # Store in cache (only when transparent cache is enabled)
        if self.multiplexer.cfg.mcp_multiplex_transparent_cache:
            import json as _json
            result_json = _json.dumps(result)
            server_cfg = self.multiplexer.cfg.mcp_multiplex_servers_config.get(server, {})
            ttl = server_cfg.get("ttl", self.multiplexer.cfg.mcp_multiplex_default_ttl)
            _cache_mcp_store(cached.get("key", f"{server}:{tool}"), server, tool, arguments, result_json, ttl=ttl)

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
    if _server_instance:
        _server_instance.stop()
    sys.exit(0)


def run_daemon(socket_path: str = None, foreground: bool = False):
    """Start the ToolRecall daemon."""
    global _server_instance

    # Prevent starting multiple daemons (concurrency / socket-stealing guard)
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # Signal 0 tests process existence
            print(f"❌ Error: ToolRecall Daemon is already running (PID: {pid}).")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            # PID file is stale, clean it up
            try:
                os.remove(PID_FILE)
            except Exception:
                pass

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
            
        # Child process: Redirect standard streams to catch low-level crashes
        log_file = os.path.expanduser("~/.toolrecall/daemon.log")
        sys.stdout = open(log_file, "a")
        sys.stderr = sys.stdout

    _server_instance.start()


def stop_daemon():
    """Stop the daemon if running."""
    if not os.path.exists(PID_FILE):
        print("ToolRecall Daemon is not running.")
        return

    with open(PID_FILE) as f:
        pid = int(f.read().strip())

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to Daemon (PID {pid})")
    except ProcessLookupError:
        print(f"Daemon (PID {pid}) not running. Cleaning up PID file.")
    finally:
        os.remove(PID_FILE)
        socket_path = _default_socket_path()
        if os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except Exception:
                pass

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
                    print(f"  Path allowlist: {resp.get('allowed_paths', [])}")
                    print(f"  Terminal enabled: {resp.get('allow_terminal', False)}")
                    print(f"  MCP Multiplex: {'ENABLED' if resp.get('multiplex_enabled') else 'DISABLED'}")
                    servers = resp.get('multiplex_servers', [])
                    if servers:
                        print(f"  MCP Servers: {', '.join(s['name'] for s in servers)}")
        except Exception:
            print("  Status: Unresponsive (Socket error)")

    except ProcessLookupError:
        print("ToolRecall Daemon: DEAD (Stale PID file)")
        print("  → Run 'toolrecall daemon --stop' to clean up")
