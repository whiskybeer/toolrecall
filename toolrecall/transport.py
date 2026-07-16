"""ToolRecall Transport — platform-agnostic IPC (UDS on POSIX, TCP on Windows).

Provides TransportClient and TransportServer that abstract away the difference
between Unix Domain Sockets (Linux/macOS) and TCP sockets (Windows fallback).

On POSIX (Linux, macOS): uses AF_UNIX for best performance (~4µs round-trip).
On Windows: falls back to TCP on 127.0.0.1 (Windows has no AF_UNIX in Python 3.11).

Usage:
    server = TransportServer()
    server.start()  # blocking

    client = TransportClient()
    client.send({"cmd": "ping"})  # returns dict
"""

import json
import os
import socket
import struct
import sys
from pathlib import Path

# ─── Platform detection ───────────────────────────────────

IS_WINDOWS = sys.platform == "win32"


def _default_socket_path() -> str:
    """Determine the default transport path.

    Env var priority:
      1. TOOLRECALL_TRANSPORT (read by both client and daemon)
      2. TOOLRECALL_UDS_PATH (alias for backward compat with Docker/docs)
      3. Platform default: ~/.toolrecall/toolrecall.sock (POSIX) or
         tcp://127.0.0.1:8568 (Windows)

    POSIX: Unix Domain Socket at ~/.toolrecall/toolrecall.sock
    Windows: TCP on 127.0.0.1:port

    Returns string: file path on POSIX, "tcp://127.0.0.1:PORT" on Windows.
    """
    if IS_WINDOWS:
        port = int(os.environ.get("TOOLRECALL_PORT", "8568"))
        return f"tcp://127.0.0.1:{port}"

    # TOOLRECALL_TRANSPORT is the canonical env var — read by both client
    # and daemon. TOOLRECALL_UDS_PATH is kept as an alias for Docker/docs
    # backward compatibility (deprecated).
    env_path = os.environ.get("TOOLRECALL_TRANSPORT") or os.environ.get("TOOLRECALL_UDS_PATH")
    if env_path:
        return env_path

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        # Verify that XDG_RUNTIME_DIR matches the actual user UID.
        # Hermes sessions may inherit a wrong XDG_RUNTIME_DIR (e.g.
        # from a container/gateway at UID 1000 while the real user
        # is UID 1004). When the env var doesn't match os.getuid(),
        # prefer the correct path to avoid talking to a different
        # daemon's socket.
        expected_dir = f"/run/user/{os.getuid()}"
        if xdg != expected_dir and os.path.exists(expected_dir):
            return os.path.join(expected_dir, "toolrecall.sock")
        return os.path.join(xdg, "toolrecall.sock")
    home = Path.home() / ".toolrecall"
    home.mkdir(parents=True, exist_ok=True)
    return str(home / "toolrecall.sock")


def _is_tcp(path: str) -> bool:
    """Check if the path is a TCP endpoint (tcp://host:port)."""
    return path.startswith("tcp://")


def _parse_tcp(path: str) -> tuple:
    """Parse tcp://host:port into (host, port)."""
    assert path.startswith("tcp://"), f"Not a TCP path: {path}"
    rest = path[6:]
    host, _, port_str = rest.rpartition(":")
    return host or "127.0.0.1", int(port_str)


# ─── Transport Functions ──────────────────────────────────

def create_socket(path: str) -> socket.socket:
    """Create a socket appropriate for the transport type."""
    if _is_tcp(path):
        return socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


def bind_socket(sock: socket.socket, path: str):
    """Bind a socket to its path/address."""
    if _is_tcp(path):
        host, port = _parse_tcp(path)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    else:
        # Remove stale socket file
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sock.bind(path)
        os.chmod(path, 0o700)


def connect_socket(sock: socket.socket, path: str):
    """Connect a socket to its peer."""
    if _is_tcp(path):
        host, port = _parse_tcp(path)
        sock.connect((host, port))
    else:
        sock.connect(path)


# ─── Framed Message Protocol ──────────────────────────────
# 4-byte big-endian length prefix + JSON payload
# Same format for both UDS and TCP.

_MAX_MSG_SIZE = 1024 * 1024  # 1 MB


def send_message(sock: socket.socket, data: dict):
    """Send a framed JSON message over a connected socket."""
    payload = json.dumps(data).encode("utf-8")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from a socket."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_message(sock: socket.socket) -> dict | None:
    """Receive a framed JSON message. Returns None on connection close."""
    raw_len = recv_exact(sock, 4)
    if not raw_len or len(raw_len) < 4:
        return None
    msg_len = struct.unpack("!I", raw_len)[0]
    if msg_len > _MAX_MSG_SIZE:
        # SECURITY: Drain the oversized payload from the socket before returning.
        # Previously returned an error dict without reading msg_len bytes,
        # leaving the socket poisoned — the next receive_message would read
        # the unread payload as a new length prefix, cascading errors.
        recv_exact(sock, msg_len)
        return {"error": "Message too large"}
    raw_data = recv_exact(sock, msg_len)
    if not raw_data:
        return None
    return json.loads(raw_data.decode("utf-8"))


# ─── Client ───────────────────────────────────────────────

class TransportClient:
    """Platform-agnostic IPC client (UDS on POSIX, TCP on Windows).
    
    Stateless — connects per request.
    """
    
    def __init__(self, path: str = None):
        self._path = path or _default_socket_path()
    
    @property
    def path(self) -> str:
        return self._path
    
    @property
    def is_tcp(self) -> bool:
        return _is_tcp(self._path)

    def close(self):
        """No-op — TransportClient is stateless (connects per request).
        
        Added for API compatibility: client.py's atexit cleanup calls
        _client.close(), which previously raised AttributeError because
        TransportClient had no close() method.
        """
        pass
    
    def send(self, payload: dict, timeout: float = 5.0) -> dict:
        """Send a request and receive response."""
        try:
            sock = create_socket(self._path)
            sock.settimeout(timeout)
            connect_socket(sock, self._path)
            send_message(sock, payload)
            resp = receive_message(sock)
            sock.close()
            if resp is None:
                return {"error": "Empty response"}
            return resp
        except (ConnectionRefusedError, FileNotFoundError,
                socket.timeout, OSError, ConnectionResetError,
                BrokenPipeError):
            return {"error": "daemon_unavailable"}
        except Exception as e:
            return {"error": str(e)}
    
    def ping(self, timeout: float = 1.0) -> bool:
        """Fast connectivity check."""
        try:
            sock = create_socket(self._path)
            sock.settimeout(timeout)
            connect_socket(sock, self._path)
            sock.close()
            return True
        except Exception:
            return False


# ─── Default socket path (module-level, configurable) ─────

DEFAULT_PATH = os.environ.get("TOOLRECALL_TRANSPORT", _default_socket_path())
