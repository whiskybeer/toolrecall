"""ToolRecall Daemon — Central cache process with IPC interface + MCP Multiplexer.

The daemon holds the In-Memory LRU + SQLite and accepts requests
via Unix Domain Socket (POSIX) or TCP (Windows fallback).
All access paths (Python import, MCP, HTTP)
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

import atexit
import json
import weakref
import logging
import itertools
import os
import socket
import subprocess
import sys
import signal
import threading
import warnings
import time
from concurrent.futures import ThreadPoolExecutor

from toolrecall import __version__
from toolrecall import mcp_catalog
from toolrecall.keys import content_digest
from toolrecall.cache import (
    _record as _cache_record,
    record_tool_call,
    work_store as _work_store,
    work_lookup as _work_lookup,
    cached_read as _cache_read,
    cached_terminal as _cache_terminal,
    cached_skill as _cache_skill,
    cached_write as _cache_write,
    cached_patch as _cache_patch,
    cached_mcp_check as _cache_mcp_check,
    cached_mcp_serve as _cache_mcp_serve,
    cached_mcp_store as _cache_mcp_store,
    mcp_tool_is_cacheable as _mcp_tool_is_cacheable,
    invalidate_mcp_server as _cache_invalidate_mcp_server,
    _heal_poisoned_mcp_row as _heal_poisoned_mcp_row,
    revalidate_mcp_entry as _revalidate_mcp_entry,
    cached_browser_check as _cache_browser_check,
    cached_browser_store as _cache_browser_store,
    cached_api_check as _cache_api_check,
    cached_api_store as _cache_api_store,
    _is_sensitive_path,
    invalidate_all,
    invalidate_api_cache,
    get_stats,
)
from toolrecall.transport import (
    TransportClient,
    create_socket,
    bind_socket,
    send_message,
    PROTOCOL_VERSION,
    receive_message,
    IS_WINDOWS,
)
from toolrecall.docs import docs_search as _docs_search, docs_get_page as _docs_get_page
from toolrecall.config import load_config
from toolrecall.context_tracker import (
    ContextTracker,
    sanitize_path_for_hint as _sanitize_path_for_hint,
    MAX_HINT_PATHS as _MAX_HINT_PATHS,
)

# ─── Defaults ─────────────────────────────────────────────

PID_FILE = os.path.expanduser("~/.toolrecall/daemon.pid")


def _should_manage_pid_file(socket_path: str | None) -> bool:
    """True when this daemon owns the PRODUCTION socket.

    Only the production daemon may read/write/remove the shared
    ``~/.toolrecall/daemon.pid``. Hermetic daemons (pytest, e2e rigs) bind
    sockets under the system temp dir, and on 2026-09-10 one of them — a
    ``/tmp/toolrecall_e2e_*/toolrecall.sock`` daemon that then lost the global
    ``:8569`` bind race and exited — still overwrote the production PID file
    with its own (soon dead) PID. ``toolrecall healthcheck`` and
    ``daemon --stop`` then pointed at a non-existent process.

    Mirrors the tempdir exemption used by the production-path guard in
    ``tests/conftest.py``: the classification must NOT go through
    ``transport._default_socket_path()``, which honors ``TOOLRECALL_TRANSPORT``
    and would therefore call a test socket "production".

    Non-path transports (``tcp://`` on Windows) keep the legacy behavior —
    there the PID file is the stop mechanism.
    """
    if not socket_path:
        return False
    if socket_path.startswith("tcp://"):
        return True
    import tempfile

    try:
        real = os.path.realpath(socket_path)
        tmp = os.path.realpath(tempfile.gettempdir())
    except OSError:
        return False
    return real != tmp and not real.startswith(tmp + os.sep)


def _instance_lock_path(socket_path: str) -> str:
    """Return a lock-file path for a given socket path.

    The lock is keyed on the socket path so that daemons bound to
    *different* sockets (e.g. e2e tests using a temp socket) each get
    their own lock, while multiple attempts to start a daemon on the
    *same* socket serialize and only one wins.

    Hermetic daemons bound under temp directories keep their lock file
    in their own temp directory (next to the socket) rather than polluting
    ~/.toolrecall (which may be read-only or owned by production).
    """
    import hashlib

    # nosemgrep: insecure-hash-algorithm-md5 — non-crypto lockfile name, socket path only
    digest = hashlib.md5(socket_path.encode("utf-8")).hexdigest()[:16]  # nosemgrep
    if not _should_manage_pid_file(socket_path) and os.path.isabs(socket_path):
        # Hermetic test daemon: place lock file sibling to test socket or in tempdir
        lock_dir = os.path.dirname(os.path.realpath(socket_path))
        return os.path.join(lock_dir, f"daemon-{digest}.lck")
    return os.path.join(os.path.dirname(PID_FILE), f"daemon-{digest}.lck")


def _acquire_instance_lock(socket_path: str):
    """Atomically claim a single-instance lock for this socket.

    Returns an open file object held for the daemon's lifetime, or None
    if another process already holds the lock for this socket.

    Uses fcntl.flock on POSIX / msvcrt.locking on Windows — the OS releases
    the lock automatically when the holding process exits/crashes, so a
    stale lock file can never permanently block a restart (the classic
    pid-file trap).

    NOTE: The returned file object MUST be kept referenced for the
    lifetime of the daemon — the lock is tied to the open file
    description/handle; if the object is garbage collected, the lock is
    silently released (true on both POSIX and Windows).
    """
    if IS_WINDOWS:
        # Windows: byte-range lock via msvcrt.locking instead of fcntl.flock.
        # Previously this branch returned None unconditionally, so every
        # start attempt looked like "already running" and the daemon could
        # never start on Windows (fixed 2026-09-04, handover report 0.8.19).
        import msvcrt

        lock_path = _instance_lock_path(socket_path)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        # Append mode so we never truncate the file (and thus erase another
        # holder's PID) before we've actually won the lock. msvcrt.locking
        # with LK_NBLCK is non-blocking and raises OSError when held.
        fh = open(lock_path, "a+")
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]  # Windows-only module; no stubs on POSIX
        except OSError:
            fh.close()
            return None
        # Record the owning PID so humans/debuggers can see who holds it.
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    import fcntl

    lock_path = _instance_lock_path(socket_path)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    # Use append mode so we never truncate the file (and thus erase another
    # holder's PID) before we've actually won the lock. We only truncate+write
    # our own PID after a successful flock.
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    # Record the owning PID so humans/debuggers can see who holds it.
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _default_socket_path():
    """Default IPC path: UDS on POSIX, TCP on Windows."""
    from toolrecall.transport import _default_socket_path as _tsp

    return _tsp()


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
        self.tool_access_control = cfg.mcp_tool_access_control
        # F1 symlink policy: lexical in-allowed-dir symlinks (path_utils
        # Layer B). Default False (strict realpath containment, review
        # 2026-09); distro/venv layouts opt in with allow_symlink_escapes
        # = true.
        self.allow_symlink_escapes = getattr(cfg, "mcp_allow_symlink_escapes", False)
        self.dangerous_tool_keywords = cfg.mcp_dangerous_tool_keywords or [
            "write",
            "edit",
            "delete",
            "remove",
            "terminal",
            "bash",
            "exec",
            "run",
            "push",
            "commit",
            "update",
            "create",
            "sudo",
            "chmod",
            "chown",
            "invalidate",
            "store",
            "set",
        ]
        self.cognitive_check = cfg.mcp_cognitive_check_enabled
        self.ast_check = cfg.mcp_ast_check_enabled
        # Per-agent policy table from [mcp.clients."<name>"], keyed by the MCP
        # clientInfo.name the bridge reports on each request. Unlisted agents
        # (or direct non-MCP callers with no client field) get the global
        # [mcp] settings — default-deny semantics are unchanged.
        self.client_policy = cfg.mcp_client_policy()
        # A6: policy table for REMOTE (TCP) callers, keyed by token-id from
        # [network.clients."<id>"]. Remote = untrusted-ish: the base policy
        # is empty-deny and ONLY a [network.clients] entry can grant — the
        # global [mcp] settings never leak to remote callers.
        # getattr: third-party/legacy configs (tests, embedders) may predate
        # A6 and not carry the attribute — empty table = remote default-deny.
        self.remote_policy = getattr(cfg, "network_clients", {})
        self.logger = logging.getLogger(__name__)

    def effective_policy(self, client: str | None, remote: bool = False) -> dict:
        """Resolve the effective [mcp] policy for one agent request.

        Local (UDS) requests: per-client overrides from
        [mcp.clients."<name>"] win; everything the client doesn't override
        falls back to the global [mcp] values.

        Remote (TCP) requests (remote=True): policy comes ONLY from
        [network.clients."<token-id>"] — nothing falls back to the global
        [mcp] settings. A token-id with no entry is denied everything
        (compromised token = exactly the scoped policy, nothing more).

        The remote flag travels WITH THE REQUEST (set by the TCP handler),
        never on shared gate state — the daemon serves UDS and TCP
        concurrently, and a shared mode flag would let the network
        listener deny or scramble LOCAL requests.
        """
        if remote:
            entry = self.remote_policy.get(client or "", {})
            return {
                "allowed_paths": entry.get("allowed_paths", []),
                "allow_terminal": entry.get("allow_terminal", False),
                "allowed_terminal_commands": entry.get("allowed_terminal_commands", []),
            }
        eff = {
            "allowed_paths": self.allowed_paths,
            "allow_terminal": self.allow_terminal,
            "allowed_terminal_commands": self.allowed_terminal_commands,
        }
        entry = self.client_policy.get(client or "", {})
        for key in eff:
            if key in entry:
                eff[key] = entry[key]
        return eff

    def check_read_path_for(
        self, path: str, client: str | None = None, remote: bool = False
    ) -> str | None:
        """check_read_path with per-agent allowed_paths (A4).

        remote=True (A6): policy comes from [network.clients] only.
        Thread-safe: resolves the effective allowlist without mutating any
        shared state. All layers still apply — Layer 1 uses the agent's
        allowlist, Layer 2 (sensitive-file blocklist) and the null-byte /
        length checks always use the global rules.
        """
        eff = self.effective_policy(client, remote=remote)
        if eff["allowed_paths"] == self.allowed_paths:
            return self.check_read_path(path)

        # Layer 0 (shared): null bytes, length
        if "\x00" in path:
            return "Path not allowed: contains null byte"
        if len(path) > self.MAX_PATH_LENGTH:
            return "Path not allowed: exceeds maximum length"

        # Layer 1: agent-specific allowlist (default-deny preserved)
        agent_paths = eff["allowed_paths"]
        if not agent_paths:
            return (
                "Path not allowed: no allowed paths configured for this agent. "
                f'Add allowed_paths to [mcp.clients."{client}"] in your config.toml.'
            )
        from toolrecall.path_utils import check_path_allowed

        if not check_path_allowed(path, agent_paths, self.allow_symlink_escapes):
            self.logger.warning("Blocked path not in allowed_paths for agent %r: %s", client, path)
            return "Path not allowed: access denied"

        # Layer 2: sensitive-file blocklist (global, always applies)
        if _is_sensitive_path(path):
            print(
                f"[ToolRecall] Blocked read of sensitive file: {path} (Layer 2: sensitive file blocklist)"
            )
            return "Path not allowed: path matches a sensitive file pattern"

        return None

    def check_terminal_for(
        self, cmd: str, client: str | None = None, remote: bool = False
    ) -> str | None:
        """check_terminal with per-agent allow_terminal / command allowlist (A4).

        remote=True (A6): policy comes from [network.clients] only.
        Thread-safe: evaluates the effective policy locally, no shared-state
        mutation. The global check_terminal is reused when the agent has no
        overrides so log messages stay identical.
        """
        eff = self.effective_policy(client, remote=remote)
        if (
            eff["allow_terminal"] == self.allow_terminal
            and eff["allowed_terminal_commands"] == self.allowed_terminal_commands
        ):
            return self.check_terminal(cmd)

        if not eff["allow_terminal"]:
            print(
                f"[ToolRecall] Blocked terminal command for agent {client!r}: {cmd[:80]} (terminal disabled for this agent)"
            )
            return (
                f"cached_terminal is disabled for agent '{client}'. "
                f'Set allow_terminal = true in [mcp.clients."{client}"] in config.toml.'
            )

        agent_cmds = eff["allowed_terminal_commands"]
        if not agent_cmds:
            return None  # Binary WAF fallback, same as global

        import re

        for pattern in agent_cmds:
            try:
                if re.search(pattern, cmd):
                    return None
            except re.error as e:
                self.logger.info(
                    f"Warning: Invalid regex in [mcp.clients.\"{client}\"].allowed_terminal_commands: '{pattern}' ({e})"
                )

        return f"Terminal command not allowed by regex allowlist for agent '{client}': {cmd}"

    MAX_PATH_LENGTH = (
        4096  # POSIX PATH_MAX (260 on Windows without long-path support; 4096 is safe on both)
    )

    def check_read_path(self, path: str) -> str | None:
        """Check if path is allowed to be read. Returns None or error message."""
        # Reject null bytes — can bypass extension checks
        if "\x00" in path:
            return "Path not allowed: contains null byte"

        # Reject excessively long paths — prevents buffer issues
        if len(path) > self.MAX_PATH_LENGTH:
            return "Path not allowed: exceeds maximum length"

        # ═══ Layer 1: Allowlist ═══
        # Default-deny: if allowed_paths is empty, NO paths are readable.
        # The user MUST explicitly configure paths in mcp.allowed_paths.
        if not self.allowed_paths:
            return (
                "Path not allowed: no allowed paths configured. "
                "Add directories to mcp.allowed_paths in your config.toml "
                "(e.g. allowed_paths = ['~/projects']). "
                "See: toolrecall init for interactive setup."
            )

        from toolrecall.path_utils import check_path_allowed

        if not check_path_allowed(path, self.allowed_paths, self.allow_symlink_escapes):
            # Generic error — never leak the real resolved path to the caller
            self.logger.warning("Blocked path not in allowed_paths: %s", path)
            return "Path not allowed: access denied"

        # ═══ Layer 2: Sensitive file blocklist ═══
        # Even within allowed paths, block known sensitive files — .env, .ssh,
        # credentials, etc.  This is a safety net, not a primary control.
        # The allowlist defines trust; the blocklist prevents accidental
        # disclosure of credential files within trusted directories.
        if _is_sensitive_path(path):
            print(
                f"[ToolRecall] Blocked read of sensitive file: {path} (Layer 2: sensitive file blocklist)"
            )
            return "Path not allowed: path matches a sensitive file pattern"

        return None

    def check_terminal(self, cmd: str) -> str | None:
        if not self.allow_terminal:
            print(
                f"[ToolRecall] Blocked terminal command: {cmd[:80]} (terminal disabled in config)"
            )
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
                self.logger.info(
                    f"Warning: Invalid regex in allowed_terminal_commands: '{pattern}' ({e})"
                )

        return f"Terminal command not allowed by regex allowlist: {cmd}"

    def check_invalidate(self) -> str | None:
        if not self.allow_invalidate:
            return "cache_invalidate is disabled. Set mcp.allow_invalidate=true in config."
        return None

    def check_mcp_server(self, server: str) -> str | None:
        if not self.allow_multiplex:
            print(f"[ToolRecall] MCP multiplexer is disabled, server '{server}' blocked")
            return "MCP multiplexer is disabled."
        # "_any" is the multiplexer-wide sentinel used by the discovery handlers
        # (mcp_list_servers, mcp_tools_catalog): they enumerate the allowlist
        # themselves, so the allowlist must not reject them. Live bug until
        # 2026-09-28: with servers = ["fetch", "hermes-docs"], mcp_list_servers
        # answered {"error": "MCP server '_any' not in allowed_servers."} — every
        # discovery path was broken for anyone who configured an allowlist.
        if self.allowed_servers and server != "_any" and server.lower() not in self.allowed_servers:
            print(f"[ToolRecall] Blocked MCP server '{server}' not in allowed_servers list")
            return f"MCP server '{server}' not in allowed_servers."
        return None

    def check_mcp_tool_access(self, tool_name: str) -> str | None:
        """MCP Keyword Access Control: blocks tools whose name contains a dangerous substring.

        This is NOT an OS sandbox. There is no process isolation, no container,
        no cgroups. It's a simple substring match on tool names — tools whose names
        happen to not match any keyword (e.g. 'post_to_slack', 'run_migration')
        are NOT blocked. For real sandboxing, combine with Docker/gVisor.
        """
        if not self.tool_access_control:
            return None

        t_lower = tool_name.lower()
        for kw in self.dangerous_tool_keywords:
            if kw.lower() in t_lower:
                print(f"[ToolRecall] Blocked MCP tool access: {tool_name} (matches keyword '{kw}')")
                return f"ToolRecall MCP Access Control: tool '{tool_name}' blocked (matches keyword '{kw}')."
        return None

    # ─── Cognitive Semantic Scan ────────────────────────────

    # Compiled lazily on first call to cognitive_scan_arguments()
    _cog_override_pat = None
    _cog_role_pat = None
    _cog_fish_pat = None
    _cog_jailbreak_pat = None
    _cog_overflow_pat = None
    _cog_encode_pat = None
    _cog_exfil_domain_pat = None
    _cog_exfil_ip_pat = None

    @classmethod
    def _compile_cognitive_patterns(cls):
        """Lazy-compile cognitive scan regexes (measured: ~0.001ms each on first call)."""
        if cls._cog_override_pat is None:
            import re

            cls._cog_override_pat = re.compile(
                r"ignore\s+(?:all\s+)?(?:prior|previous|your)?\s*(?:\w+\s+)?(?:instructions|directives|rules|commands)",
                re.IGNORECASE,
            )
            cls._cog_role_pat = re.compile(
                r"(?:you\s+are\s+now|act\s+as\s+(?:if\s+)?|new\s+role)",
                re.IGNORECASE,
            )
            cls._cog_fish_pat = re.compile(
                r"(?:reveal\s+(?:your\s+)?(?:system\s+)?prompt|show\s+(?:your\s+)?secret|dump\s+(?:your\s+)?internal)",
                re.IGNORECASE,
            )
            cls._cog_jailbreak_pat = re.compile(
                r"\b(?:DAN|STAN|GODMODE|god\s*mode|unlocked\s*mode)\b",
                re.IGNORECASE,
            )
            cls._cog_overflow_pat = re.compile(
                r"(?:context\s+overflow|token\s+(?:consumption|limit|window))",
                re.IGNORECASE,
            )
            cls._cog_encode_pat = re.compile(
                r"(?:base64|%[0-9a-fA-F]{2}%[0-9a-fA-F]{2}%[0-9a-fA-F]{2})",
            )
            cls._cog_exfil_domain_pat = re.compile(
                r"(?:evil-|exfil|malware|phishing|steal|leak).*\.(?:com|org|net|io|xyz|ru)",
                re.IGNORECASE,
            )
            cls._cog_exfil_ip_pat = re.compile(
                r"https?://(?:127\.0\.0\.1|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+)(?::\d+)?/(?:exfil|steal|leak|upload)",
                re.IGNORECASE,
            )
        return (
            cls._cog_override_pat,
            cls._cog_role_pat,
            cls._cog_fish_pat,
            cls._cog_jailbreak_pat,
            cls._cog_overflow_pat,
            cls._cog_encode_pat,
            cls._cog_exfil_domain_pat,
            cls._cog_exfil_ip_pat,
        )

    def cognitive_scan_arguments(self, arguments: dict) -> str | None:
        """Scan tool arguments for prompt injection, jailbreak, and exfiltration patterns.

        Scans every string value in the arguments dict, including those nested
        inside dicts and lists. Returns an error message on match, or None on pass.

        Performance: ~0.001ms per call after cache warmup (lazy pattern compilation
        adds ~0.08ms on the very first call).
        """
        if not self.cognitive_check:
            return None

        patterns = self._compile_cognitive_patterns()
        ovrd, role, fish, jail, overf, enc, exf_dom, exf_ip = patterns

        # Recursively extract all string values from nested dicts/lists
        def _extract_strings(obj, key_path=""):
            if isinstance(obj, str):
                yield key_path, obj
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    yield from _extract_strings(v, f"{key_path}.{k}" if key_path else k)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    yield from _extract_strings(v, f"{key_path}[{i}]")

        for key, val in _extract_strings(arguments):
            if not isinstance(val, str):
                continue  # Skip non-string values silently

            # Override instructions
            if ovrd.search(val):
                print(
                    f"[ToolRecall] Cognitive scan blocked: argument '{key}' matches override instruction pattern"
                )
                return f"Cognitive scan blocked: argument '{key}' matches override instruction pattern."

            # Role hijacking
            if role.search(val):
                print(
                    f"[ToolRecall] Cognitive scan blocked: argument '{key}' matches role hijack pattern"
                )
                return f"Cognitive scan blocked: argument '{key}' matches role hijack pattern."

            # Credential fishing
            if fish.search(val):
                print(
                    f"[ToolRecall] Cognitive scan blocked: argument '{key}' matches credential fishing pattern"
                )
                return (
                    f"Cognitive scan blocked: argument '{key}' matches credential fishing pattern."
                )

            # Jailbreak tags
            if jail.search(val):
                print(
                    f"[ToolRecall] Cognitive scan blocked: argument '{key}' matches jailbreak tag pattern"
                )
                return f"Cognitive scan blocked: argument '{key}' matches jailbreak tag pattern."

            # Context overflow
            if overf.search(val):
                print(
                    f"[ToolRecall] Cognitive scan blocked: argument '{key}' matches context overflow pattern"
                )
                return f"Cognitive scan blocked: argument '{key}' matches context overflow pattern."

            # Encoding evasion
            if enc.search(val):
                print(
                    f"[ToolRecall] Cognitive scan blocked: argument '{key}' matches encoding evasion pattern"
                )
                return f"Cognitive scan blocked: argument '{key}' matches encoding evasion pattern."

            # Exfiltration URL (domain-based)
            if exf_dom.search(val):
                print(
                    f"[ToolRecall] Cognitive scan blocked: argument '{key}' matches exfiltration url pattern"
                )
                return f"Cognitive scan blocked: argument '{key}' matches exfiltration url pattern."

            # Exfiltration URL (raw IP + exfil path)
            if exf_ip.search(val):
                print(
                    f"[ToolRecall] Cognitive scan blocked: argument '{key}' matches exfiltration url pattern"
                )
                return f"Cognitive scan blocked: argument '{key}' matches exfiltration url pattern."

        return None

    # ─── AST Structural Validation ──────────────────────────

    _AST_DANGEROUS_CALLS = frozenset(
        {
            "exec",
            "eval",
            "compile",
            "__import__",
        }
    )
    _AST_MIN_LENGTH = 10  # Skip strings shorter than this

    def check_ast_injection(self, arguments: dict) -> str | None:
        """Check tool arguments for Python code injection using AST parsing.

        Tries to parse every string value >=10 chars as Python code.
        If it parses as a valid Python AST, it checks for dangerous
        constructs: exec(), eval(), compile(), __import__(), import
        statements, and function definitions.

        Non-string values and strings <10 chars are silently skipped.

        Performance: ~0.01ms per call for non-code (fast AST.parse error),
        ~0.05ms per call for actual code (full AST walk).
        """
        if not self.ast_check:
            return None

        import ast

        for key, val in arguments.items():
            if not isinstance(val, str):
                continue
            if len(val) < self._AST_MIN_LENGTH:
                continue

            try:
                tree = ast.parse(val, mode="exec")
            except SyntaxError:
                # Not valid Python — harmless
                continue

            # Walk the AST for dangerous constructs
            for node in ast.walk(tree):
                # exec(), eval(), compile() calls
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in self._AST_DANGEROUS_CALLS:
                        return f"AST injection blocked: argument '{key}' contains dangerous '{node.func.id}()' call."

                # __import__() calls (as attribute)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "__import__":
                        return (
                            f"AST injection blocked: argument '{key}' contains '__import__()' call."
                        )

                # import statements
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                    return f"AST injection blocked: argument '{key}' contains import statement (imports: {', '.join(names)})."

                # from ... import statements
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    names = [a.name for a in node.names]
                    return f"AST injection blocked: argument '{key}' contains from-import statement (from {module} import {', '.join(names)})."

                # Function definitions (def / async def)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return f"AST injection blocked: argument '{key}' contains function definition '{node.name}'."

        return None


# ─── MCP Multiplexer ──────────────────────────────────────


# ─── Live MCP session registry (leak guard) ───────────────────────────────
# Every started MCP subprocess is tracked here so it can be reaped when this
# process goes away, even if no caller ever invoked shutdown(). Without it, any
# short-lived owner (tests, CLIs, a daemon killed before teardown) orphans its
# servers: one pytest run left 84 `uvx mcp-server-sqlite` processes running
# (168 with their uv wrappers), which starved the host of memory and made every
# unrelated service on the box look "slow".
# WeakSet, deliberately: the registry must observe sessions, never own them.
# A strong set would pin every session for the life of the process, so
# ``MCPClientSession.__del__`` could never fire and an abandoned session's
# server would live on — the leak this guard exists to prevent, inverted.
_LIVE_MCP_SESSIONS: "weakref.WeakSet" = weakref.WeakSet()
_LIVE_MCP_SESSIONS_LOCK = threading.Lock()


def shutdown_all_mcp_sessions() -> int:
    """Best-effort shutdown of every live MCP session in this process.

    Safe to call from test teardown, atexit, or a signal handler. Returns the
    number of sessions that were stopped.
    """
    with _LIVE_MCP_SESSIONS_LOCK:
        sessions = list(_LIVE_MCP_SESSIONS)
    stopped = 0
    for session in sessions:
        try:
            session.shutdown()
            stopped += 1
        except Exception:
            pass
    return stopped


atexit.register(shutdown_all_mcp_sessions)


class MCPClientSession:
    """Manages a persistent MCP subprocess over stdio JSON-RPC.

    Handles connect, keepalive, reconnect (max 3 attempts),
    and proper shutdown.
    """

    def __init__(self, name: str, command: str, args: list, env: dict | None = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self._proc: subprocess.Popen | None = None
        # SECURITY: RLock (not Lock) to prevent deadlock when _send_raw
        # recursively calls itself on reconnect — a non-reentrant Lock
        # would deadlock the calling thread forever on the first reconnect.
        self._lock = threading.RLock()
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
            preexec_fn=os.setsid
            if os.name != "nt"
            else None,  # POSIX only; new process group for clean killing
        )
        with _LIVE_MCP_SESSIONS_LOCK:
            _LIVE_MCP_SESSIONS.add(self)

    def __del__(self):
        # Leak guard: a session dropped without an explicit shutdown() would
        # leave its server process running with no remaining owner.
        try:
            if getattr(self, "_proc", None) is not None:
                self.shutdown()
        except Exception:
            pass

    def _send_raw(self, payload: dict) -> dict:
        """Send JSON-RPC line, read response line. Thread-safe."""
        with self._lock:
            if not self.running:
                self._start()
            self._req_id += 1
            payload["id"] = self._req_id
            line = json.dumps(payload) + "\n"
            try:
                proc = self._proc
                assert proc is not None and proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
                # Read response with timeout to prevent indefinite blocking
                # on hung MCP subprocesses (common with Node.js on startup).
                # Without this, a single hung server exhausts the thread pool.
                import select

                stdout_fd = proc.stdout.fileno()
                readable, _, _ = select.select([stdout_fd], [], [], 30)
                if not readable:
                    raise ConnectionError("MCP subprocess response timeout (30s)")
                resp_line = proc.stdout.readline()
                if not resp_line:
                    raise ConnectionError("Empty response from subprocess")
                return json.loads(resp_line.decode("utf-8"))
            except Exception:
                # Attempt reconnect once
                if self._reconnect_count < self._max_reconnects:
                    self._reconnect_count += 1
                    self._proc = None
                    self._start()
                    return self._send_raw(payload)
                raise

    def initialize(self) -> dict:
        """Send initialize handshake."""
        return self._send_raw(
            {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "toolrecall", "version": __version__},
                },
            }
        )

    def list_tools(self) -> list:
        """Fetch tools/list from the subprocess."""
        resp = self._send_raw(
            {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {},
            }
        )
        result = resp.get("result", {})
        return result.get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
        """Call a tool on the subprocess."""
        resp = self._send_raw(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments or {},
                },
            }
        )
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
        try:
            with _LIVE_MCP_SESSIONS_LOCK:
                _LIVE_MCP_SESSIONS.discard(self)
        except Exception:
            pass


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
        # Tool annotations per server (server → tool_lower → annotations), captured
        # from tools/list when the server starts. Lets the transparent cache decide
        # read vs write from the server's own readOnlyHint instead of guessing from
        # the tool name. Empty for a server that has never started.
        self._annotations: dict[str, dict[str, dict]] = {}
        self._reaper_running = False
        # Gateway discovery data (server → tools) lives outside the process so
        # an MCP tools/list never has to spawn a server; see mcp_catalog.
        self._catalog_path = mcp_catalog.catalog_path(cfg)

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
                self._annotations[name_lower] = {
                    t["name"].lower(): t.get("annotations") or {}
                    for t in tools
                    if isinstance(t, dict) and t.get("name")
                }
                self._sessions[name_lower] = session
                # A running server is the only place tool schemas can come
                # from (the registry carries command lines, not schemas) —
                # persist them so the gateway can advertise them later
                # without paying this cold start inside tools/list.
                mcp_catalog.store(self._catalog_path, config["name"], tools)
                print(f"  ✓ {name_lower}: {len(tools)} tools started on-demand")
            except Exception as e:
                return f"{name_lower}: start failed — {e}"
        return None

    def tool_annotations(self, server: str, tool: str) -> dict | None:
        """Annotations for a tool from the last tools/list of ``server``.

        None when the server has not started (or does not list the tool) —
        the caller then falls back to name-based classification. Never starts
        a server: a cache check must not pay a cold start to decide
        cacheability (a HIT is supposed to avoid that start entirely).
        """
        with self._lock:
            per_server = self._annotations.get(server.lower())
        if not per_server:
            return None
        return per_server.get(tool.lower())

    # ─── Gateway Catalog ──────────────────────────────

    @property
    def gateway_enabled(self) -> bool:
        """True when the bridge advertises this multiplexer's tools by name.

        The warm-up below is only worth its price in that mode: without it
        nothing consumes the catalog.
        """
        return bool(self.cfg.mcp_multiplex_enabled and self.cfg.mcp_multiplex_gateway)

    def warm_catalog(self) -> dict:
        """Start the configured servers once so the gateway has tools to list.

        Why this exists: the gateway advertises tools from the persisted
        catalog, and that catalog is only written when a server starts. On a
        fresh install (or after the catalog is removed) nothing has started →
        nothing is advertised → no agent calls a tool it cannot see → nothing
        ever starts. One best-effort pass breaks that deadlock.

        Callers run it in a background thread: a cold ``uv run`` takes seconds
        and must never delay daemon startup. Failures are reported, not
        raised — the gateway keeps listing whatever else it has.
        """
        self._discover_configs()
        started: list[str] = []
        failed: dict[str, str] = {}
        for name_lower in list(self._configs):
            try:
                err = self._ensure_server(name_lower)
            except Exception as e:  # a warm-up must never kill its thread silently
                failed[name_lower] = f"{type(e).__name__}: {e}"
                continue
            if err:
                failed[name_lower] = err
            else:
                started.append(name_lower)
        return {"started": started, "failed": failed}

    def catalog(self) -> dict:
        """Tool catalog for the gateway: fresh for running servers, disk for the rest.

        Never starts a server — discovery must not pay a cold ``uv run`` inside
        an MCP handshake. A tool absent from the catalog is merely not
        advertised yet: a call routes by name and still works.
        """
        self._discover_configs()
        with self._lock:
            running = [(n, s) for n, s in self._sessions.items() if s.running]
        for name_lower, session in running:
            try:
                tools = session.list_tools()
            except Exception as e:
                logging.getLogger("toolrecall.daemon").debug(
                    "mcp catalog refresh failed for %s: %s", name_lower, e
                )
                continue
            mcp_catalog.store(self._catalog_path, self._configs[name_lower]["name"], tools)
        return mcp_catalog.visible(
            mcp_catalog.load(self._catalog_path),
            exclude_servers=self.cfg.mcp_multiplex_gateway_exclude,
            exclude_tools=self.cfg.mcp_multiplex_gateway_exclude_tools,
            # A server dropped from the config must stop being advertised: the
            # catalog file is only rewritten when a server STARTS, so a removed
            # server's entry would linger forever and offer tools that cannot
            # route anywhere (e.g. `hermes-docs` after the switch to the
            # in-repo `docs` server).
            only_servers={e["name"] for e in self._configs.values()},
        )

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
                                # BUGFIX: was `idle_mins` (loop var from the first
                                # for-loop), which always printed the last item's
                                # idle time. Now uses `mins` — the current item.
                                print(f"  💤 {name}: idle shutdown ({int(mins)}min)")
                            except Exception:
                                pass
                            self._last_use.pop(name, None)

        t = threading.Thread(target=_reaper_loop, daemon=True, name="mcp-reaper")
        t.start()

    # ─── Public API ───────────────────────────────────

    def start(self):
        """Initialize configuration. Starts NO servers (lazy — first call starts each)."""
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

    def __del__(self):
        # Leak guard: reaps every server this multiplexer started.
        try:
            self.shutdown()
        except Exception:
            pass

    def list_servers(self) -> list:
        """List all configured servers — running, idle, or not yet started."""
        self._discover_configs()
        # Collect session references under lock, then call list_tools()
        # outside the lock to avoid blocking all multiplexer.call() calls
        # while doing blocking I/O with each subprocess.
        with self._lock:
            config_snapshot = [
                (name_lower, dict(config)) for name_lower, config in self._configs.items()
            ]

        result = []
        for name_lower, config in config_snapshot:
            session = self._sessions.get(name_lower)
            if session and session.running:
                try:
                    tools = session.list_tools()
                    result.append(
                        {
                            "name": config["name"],
                            "running": True,
                            "status": "active",
                            "tools": len(tools),
                            "tool_names": [t.get("name") for t in tools],
                        }
                    )
                except Exception:
                    result.append(
                        {
                            "name": config["name"],
                            "running": False,
                            "status": "error",
                            "tools": 0,
                            "tool_names": [],
                        }
                    )
            else:
                result.append(
                    {
                        "name": config["name"],
                        "running": False,
                        "status": "idle",
                        "tools": 0,
                        "tool_names": [],
                    }
                )
        return result

    def call(self, server: str, tool: str, arguments: dict | None = None) -> dict:
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

        # Mark used (for idle timeout) — inside lock to prevent
        # race with reaper thread reading _last_use concurrently.
        with self._lock:
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


# ─── IPC Server ──────────────────────────────────────────


# ─── A6 bind guard: local-networks-only (default), public = explicit opt-in ──


# ─── A6 remote command allow-list (A6-3, review 2026-09-08) ────
# The ONLY commands a network (TCP) client may invoke, enforced in
# _handle_tcp BEFORE routing. Cache-relevant operations that the
# per-token [network.clients] policy then path-gates individually.
# Deliberately EXCLUDED (local-only): docs_* (knowledge-DB disclosure,
# no allowlist), recall_* (cross-member blob store), mcp_call/mcp_*
# (runs the OPERATOR's multiplexed servers with operator credentials —
# privilege escalation), cache_invalidate/cache_status (cache
# management/info), context_* (per-agent tracker), shutdown/restart
# (daemon lifecycle), cached_browser_*/cached_api_* (store primitives
# — a remote caller can poison shared entries unbound by any path
# policy; local-only until a policy surface exists for them).
REMOTE_ALLOWED_COMMANDS = frozenset(
    {
        "ping",
        "cached_read",
        "cached_shell_exec",
        "cached_terminal",
        "cached_skill",
        "cached_write",  # A6-1 fix: routed through the remote-aware gate
        "cached_patch",  # A6-1 fix: routed through the remote-aware gate
        "cache_refresh_file",
        # Read-only, validator-gated MCP result serving (2026-09-20).
        # Safe to expose where the store primitives are not, because the
        # handler structurally cannot write and cannot reach private rows:
        #   - read-only: no write path exists, so poisoning is impossible
        #     (the store primitives stay local-only for exactly that reason);
        #   - shared namespace ONLY: the command derives its key with
        #     scope="shared", so a local/private row is unreachable;
        #   - validator required: rows without an ETag are refused loudly
        #     rather than served on a clock guess. Phase C measured ~29%
        #     wrong deliveries for clock-based serving vs 0% for
        #     validate-before-serve on a mutating origin, and a wrong result
        #     handed to another agent is the failure mode this design exists
        #     to prevent.
        # Data disclosure is bounded by what the operator explicitly stored in
        # the shared namespace. Rate limiting and per-token policy remain the
        # transport layer's job (see docs/MCP_CACHE_POLICY.md).
        "cached_mcp_serve",
    }
)

# Backwards-compat alias for the import in _handle_tcp.
_REMOTE_ALLOWED_COMMANDS = REMOTE_ALLOWED_COMMANDS


def _is_local_network_bind(host: str) -> bool:
    """True when `host` is a LOCAL network address (A6 hardening).

    Allowed: loopback (127/8, ::1), RFC1918 private (10/8, 172.16/12,
    192.168/16), link-local (169.254/16, fe80::/10), IPv6 ULA (fc00::/7),
    and hostnames that RESOLVE to those (localhost, a VM's internal name).
    Refused: public IPs, wildcard 0.0.0.0/:: (binds every interface incl.
    public), unresolvable names — fail-closed.

    Note: `ipaddress.is_private` is NOT used alone — it also flags
    documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24),
    benchmark nets (198.18/15) and IANA special blocks, which are NOT
    local networks. Explicit RFC1918/ULA/link-local/loopback membership
    is the contract.

    Rationale (user requirement 2026-09-08): the shared daemon is a team
    cache; a stray config value must not expose it to the internet. A
    public bind stays possible via [network] allow_public = true —
    explicit and logged at startup.
    """
    import ipaddress as _ip
    import socket as _s

    _LOCAL_NETS = [
        _ip.ip_network("127.0.0.0/8"),
        _ip.ip_network("10.0.0.0/8"),
        _ip.ip_network("172.16.0.0/12"),
        _ip.ip_network("192.168.0.0/16"),
        _ip.ip_network("169.254.0.0/16"),
        _ip.ip_network("::1/128"),
        _ip.ip_network("fe80::/10"),
        _ip.ip_network("fc00::/7"),
    ]

    def _local(ip) -> bool:
        return any(ip in net for net in _LOCAL_NETS)

    hostname = host.strip().lower()
    if hostname in ("", "0.0.0.0", "::", "*"):
        return False  # wildcard = every interface, not "local only"
    try:
        _ip.ip_address(hostname)
        candidates = [hostname]
    except ValueError:
        # Hostname: resolve it. Split-DNS safe — EVERY address must be local.
        try:
            candidates = [str(ai[4][0]) for ai in _s.getaddrinfo(hostname, None)]
        except OSError:
            return False
        if not candidates:
            return False
    for a in candidates:
        try:
            ip = _ip.ip_address(a)
        except ValueError:
            return False
        if not _local(ip):
            return False
    return True


def _fetch_revalidate_once(url: str, etag: str) -> dict | None:
    """One conditional HTTP GET for ETag revalidation (stdlib, no subprocess).

    Mirrors mcp_fetch's security posture exactly: same URL validation
    (SSRF), no redirects, same size cap. Returns the mcp_fetch-style result
    dict — {status: 304, revalidated: True} on unchanged content,
    {status: 200, content, etag} on changed content, None on network
    failure (revalidate_mcp_entry leaves the stale row untouched).
    """
    # Import inside: keeps the daemon's module-import surface unchanged and
    # avoids a daemon↔fetch-tool circular import.
    import importlib.util
    import socket
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, build_opener

    spec = importlib.util.find_spec("toolrecall.mcp_fetch")
    if spec is None or spec.origin is None or spec.loader is None:
        return None
    mcp_fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp_fetch)  # stdlib-only module; safe to load twice

    try:
        # The fetch server's own SSRF validator (F14) — same posture as the
        # real fetch tool so revalidation can never reach somewhere the
        # plain fetch could not.
        err = mcp_fetch._validate_url(url)
        if err:
            return {"status": 0, "error": err}
        req = Request(url, method="GET")
        req.add_header("User-Agent", "toolrecall-fetch-mcp/0.1")
        req.add_header("If-None-Match", etag)
        opener = build_opener(mcp_fetch._no_redirects())
        try:
            with opener.open(req, timeout=mcp_fetch.TIMEOUT_SECONDS) as resp:
                headers = dict(resp.headers)
                raw = resp.read(mcp_fetch.MAX_CONTENT_BYTES)
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw.decode("latin-1", errors="replace")
                return {
                    "status": resp.status,
                    "content": content,
                    "content_type": headers.get("Content-Type", ""),
                    "etag": headers.get("ETag"),
                }
        except HTTPError as e:
            if e.code == 304:
                return {
                    "status": 304,
                    "revalidated": True,
                    "etag": etag,
                }
            return {"status": e.code, "error": str(e.reason)}
    except (URLError, socket.timeout, ConnectionError, OSError):
        return None


def _count_tool_call(tool, handler, request):
    """Run an agent-facing handler and count the call — success or failure.

    Counting failures too is the point: a tool that is called and always errors
    must not look identical to a tool that nobody calls.

    The counter is best-effort by construction (record_tool_call logs and never
    raises), so instrumenting a handler can never turn a working tool call into
    a failed one.
    """
    try:
        result = handler(request)
    except Exception:
        record_tool_call(tool, ok=False)
        raise
    ok = not (isinstance(result, dict) and result.get("error"))
    record_tool_call(tool, ok=ok)
    return result


def _warm_gateway_catalog(mux, logger) -> None:
    """Thread target for the gateway warm-up: log the outcome, never raise.

    The pass itself stays synchronous and testable in MCPMultiplexer; this
    wrapper only absorbs the threading consequences. Every outcome is logged —
    there is no silent-swallow path here.
    """
    try:
        outcome = mux.warm_catalog()
    except Exception as e:
        logger.warning("gateway catalog warm-up failed: %s", e)
        return
    logger.info(
        "gateway catalog warm-up: %s started, %s failed",
        len(outcome["started"]),
        len(outcome["failed"]),
    )
    if outcome["failed"]:
        logger.debug("gateway warm-up failures: %s", outcome["failed"])


class DaemonServer:  # noqa: E302 — placed immediately before its section
    """IPC server — Unix Domain Socket (POSIX) or TCP (Windows).

    Uses ThreadPoolExecutor for connection handling.
    The transport.py layer handles platform selection automatically.
    """

    def __init__(self, socket_path: str | None = None):
        # Central logging rail (payload-free, redacting, daily rotation).
        # setup_logging() is idempotent and fails soft — safe at every entry.
        from toolrecall.logging_setup import setup_logging

        setup_logging()
        self.logger = logging.getLogger("toolrecall.daemon")
        self.socket_path = socket_path or _default_socket_path()
        self.cfg = load_config()
        self.security = SecurityGate(self.cfg)
        self.multiplexer = MCPMultiplexer(self.cfg)
        # Gateway discovery needs one server start before it has anything to
        # advertise: the catalog persists across restarts, but a fresh
        # install (or a removed catalog) would otherwise list nothing, never
        # be called, and therefore never start anything. Background + best
        # effort, only while the gateway mode is on: startup latency is
        # untouched and a failure only costs that server's catalog entry.
        if self.multiplexer.gateway_enabled:
            threading.Thread(
                target=_warm_gateway_catalog,
                args=(self.multiplexer, self.logger),
                daemon=True,
                name="tr-mcp-warmup",
            ).start()
        self._context = ContextTracker(ignore_prefixes=self.cfg.tracker_ignore_read_prefixes)
        # Compaction epoch: a monotone counter bumped when a harness tells us
        # it compacted its transcript (cmd "context_compacted" — e.g. a
        # PreCompact hook). Bridges read it on their next tool response and
        # drop their session-scoped "the agent already holds this" state: the
        # signal is exact, where the stub TTL/call budget can only guess at
        # the compaction horizon. Daemon-wide on purpose — one bridge's
        # compaction invalidates every bridge's assumption.
        self._compaction_epoch = 0
        self._compaction_lock = threading.Lock()
        # Restore persisted tracker state (option A): keeps the checkpoint
        # flow alive across daemon restarts instead of resetting it. TTL
        # gated ([tracker].persist_ttl, default 6h — a read set from a
        # previous session must not describe this session's context) and
        # mtime re-validated inside restore(). Fail-soft: any problem
        # leaves the fresh in-memory tracker as before.
        try:
            _restored = self._context.restore(ttl=self.cfg.tracker_persist_ttl)
            if _restored.get("restored"):
                self.logger.info(
                    "tracker restored from persisted state: %s paths, %s dirty, cp=%s",
                    _restored.get("paths", 0),
                    _restored.get("dirty", 0),
                    _restored.get("reason", "?"),
                )
        except Exception as e:  # noqa: BLE001 — restore is an optimization, never a boot gate
            warnings.warn(f"ToolRecall: tracker state restore skipped: {e}")
        # Startup checkpoint: banks nothing (read set is empty) and does
        # NOT arm the flow — armed only becomes true on an agent-driven
        # checkpoint, so the TUI can distinguish "daemon restarted" from
        # "flow actively driven". AFTER restore(): the restored state keeps
        # its checkpoint counter/armed flag, and this call banks nothing
        # extra (a restored read set that was already banked is guarded by
        # the restored _banked set; a restored-but-unbanked read set banks
        # exactly once here, same as it would have on the pre-restart side).
        self._context.set_checkpoint(name="daemon_start", armed=False)
        self._server = None
        self._running = False
        # ⚠ ThreadPoolExecutor is NOT created here — it must be created AFTER
        # fork() to avoid corrupted locks in the child process.
        # See _init_post_fork() which is called from start().
        self._executor = None
        # Monotonic request counter for the per-request trace log (req=N).
        self._req_counter = itertools.count(1)

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
            except Exception as e:  # noqa: BLE001 — never break the loop, but never hide it
                # A silently failing GC is how dead rows accumulate unnoticed: the
                # api_cache backlog this function now purges went unreported for weeks
                # (12,281 expired rows, 1,103 MB of a 1,190 MB file).
                self.logger.warning("periodic cache GC failed: %s", e)

    def _run_periodic_checkpoint(self):
        """Checkpoint the SQLite WAL into the main DB every 60 seconds.

        WAL mode keeps committed frames in ``-wal`` until merged into the
        main ``.db`` file. If the daemon dies without a checkpoint — or
        ``-wal``/``-shm`` sidecars were unlinked by a competing process —
        every entry still in the WAL is lost. A 60s checkpoint bounds that
        loss to the last minute; the shutdown checkpoint in ``stop()``
        makes normal stops lossless.
        """
        from toolrecall._db import db_checkpoint

        while self._running:
            for _ in range(60):  # 60 seconds
                if not self._running:
                    return
                time.sleep(1)
            # Tracker snapshot piggybacks on this cadence (crash insurance):
            # a SIGKILL/OOM only loses the last 60s of read/dirty tracking,
            # not the whole flow. Must run BEFORE db_checkpoint so the row
            # is inside the WAL that's about to be merged.
            try:
                self._context.snapshot()
            except Exception as e:  # noqa: BLE001 — insurance, never break the loop
                self.logger.debug("periodic tracker snapshot failed: %s", e)
            try:
                db_checkpoint(truncate=True)
            except Exception as e:  # noqa: BLE001 — never break the loop, but never hide it
                # Visibility matters more than usual here: the commit path no
                # longer auto-checkpoints (storage/sqlite.py sets
                # wal_autocheckpoint=0), so THIS thread is what bounds the WAL.
                # A silently failing checkpoint would mean unbounded -wal growth
                # with no signal at all.
                self.logger.warning("periodic WAL checkpoint failed: %s", e)

    def _start_sync_worker(self):
        """Start the storage-sync background thread (backend-agnostic).

        Whether sync applies at all is the storage layer's call — the
        daemon only asks storage.sync_configured(cfg), which encodes the
        full opt-in policy (capability AND explicit enable, default off,
        AND credentials). The daemon never imports a backend module.
        """
        from toolrecall.storage import sync_configured

        cfg = self.cfg
        if not sync_configured(cfg):
            return  # backend has no sync, or user hasn't opted in
        interval = cfg.libsql_sync_interval
        if interval <= 0:
            return

        def _sync_loop():
            """Periodically sync the embedded replica via the singleton connection."""
            from toolrecall._db import db_sync

            logger = logging.getLogger("toolrecall.daemon.sync")
            backoff = interval

            while self._running:
                try:
                    if db_sync():
                        backoff = interval  # reset after success
                except Exception as e:
                    logger.warning(
                        "storage sync failed (%s: %.120s) — retrying in %ds",
                        type(e).__name__,
                        str(e),
                        backoff,
                    )
                    backoff = min(backoff * 2, 3600)  # exponential backoff, cap 1h
                # Sleep in 1s slices so daemon shutdown isn't delayed
                # by up to a full sync interval.
                slept = 0
                while self._running and slept < backoff:
                    time.sleep(1)
                    slept += 1

        thread = threading.Thread(target=_sync_loop, daemon=True, name="Storage-Sync")
        thread.start()
        print(f"  storage sync: enabled, every {interval}s")

    # ─── Network listener (A6: shared team daemon, opt-in) ───

    def _should_start_network_listener(self) -> bool:
        """A6 gate: TCP listener starts ONLY on explicit opt-in + auth.

        enabled=false → never. enabled=true but NO token anywhere (neither
        shared [network].token nor any [network.clients.*].token) → never
        (a network daemon without auth must not exist); start() prints
        why. Everything defaults off, so a config without [network] is
        byte-identical to pre-A6 behavior.
        """
        if not self.cfg.network_enabled:
            return False
        if self.cfg.network_token:
            return True
        # A6-4: per-member tokens count as auth.
        return any(
            isinstance(e, dict) and e.get("token") for e in self.cfg.network_clients.values()
        )

    def _network_tokens(self) -> dict:
        """{token-bytes: token-id} for the auth handshake.

        A6-4 (review): per-member tokens. Each [network.clients."<id>"]
        entry may carry its own `token`; a token in the members table
        SHADOWS the single shared [network].token (removing a member's
        entry revokes them). With no member tokens configured, the
        single shared token maps to the "network-client" id (back-compat).
        """
        tokens: dict[bytes, str] = {}
        for member_id, entry in self.cfg.network_clients.items():
            t = entry.get("token") if isinstance(entry, dict) else None
            if t:
                tokens[t.encode() if isinstance(t, str) else t] = str(member_id)
        if tokens:
            return tokens
        # Single-token deployments: one shared token, one id.
        return {self.cfg.network_token: "network-client"}

    def _member_ns(self, token_id: str) -> str:
        """A6-7 cache namespace for a remote member's WRITES.

        - private_writes=true  → member-level ns ("m:<id>"): full isolation,
          the member's writes/reads never touch any other member's entries.
        - default              → scope-level ns ("s:<sha256(allowed_paths)>"):
          members with an IDENTICAL scope share a namespace (they already
          trust each other's writes — same paths, same policy), while a
          member with a different scope can never collide with them. The
          GLOBAL namespace stays reserved for local (operator) traffic,
          which is the trusted warming source for the team cache.
        """
        entry = self.cfg.network_clients.get(token_id, {})
        if not isinstance(entry, dict):
            return ""
        if entry.get("private_writes"):
            return f"m:{token_id}"
        scope = entry.get("allowed_paths", [])
        import hashlib as _hashlib

        scope_json = json.dumps(sorted(scope), sort_keys=True).encode()
        return "s:" + _hashlib.sha256(scope_json).hexdigest()[:16]

    def _run_tcp_listener(self, host: str, port: int):
        """Accept loop for the TCP listener. Runs in its own thread.

        One bad connection must never take the listener down: every
        per-connection failure is caught, logged (outcome only, payload-
        free), and the loop continues.
        """
        from toolrecall import network_tls as nt

        # Local-networks-only guard (A6 hardening, default ON): refuse a
        # public bind unless [network] allow_public = true. Loopback,
        # RFC1918, link-local, ULA always pass.
        if not self.cfg.network_allow_public and not _is_local_network_bind(host):
            print(
                f"  ⚠️  Network daemon REFUSED to bind {host}: not a local "
                f"network address (loopback/RFC1918/link-local/ULA only). "
                f"To bind a public address deliberately, set "
                f"[network] allow_public = true — and pair it with "
                f"tls = true (SECURITY.md §9)."
            )
            self.logger.warning(
                "network listener refused: bind=%s not local, allow_public=false", host
            )
            return

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((host, port))
        except OSError as e:
            print(f"  ⚠️  Network listener failed to bind {host}:{port}: {e}")
            return
        srv.listen(16)
        self._tcp_srv = srv
        actual_port = srv.getsockname()[1]
        tls_desc = "TLS" if self.cfg.network_tls else "HMAC-only"
        print(
            f"  Network daemon: tcp://{host}:{actual_port} ({tls_desc}, "
            f"token auth — remote requests run under the daemon's policy)"
        )
        # A6-2 fail-closed (review 2026-09-08): a non-loopback bind without
        # TLS serves cache CONTENTS in cleartext over the LAN. HMAC protects
        # the token, not the data. Refuse rather than warn.
        if not self.cfg.network_tls and host not in ("127.0.0.1", "::1", "localhost"):
            print(
                f"  ⚠️  Network daemon SHUTTING DOWN: tls=false on a non-loopback "
                f"bind ({host}). HMAC authenticates but does NOT encrypt — "
                f"cache contents would cross the LAN in cleartext. Set "
                f"[network] tls = true (+ tls_cert/tls_key), or bind 127.0.0.1."
            )
            self.logger.warning("network listener refused: non-loopback bind %s without TLS", host)
            try:
                srv.close()
            except OSError:
                pass
            self._tcp_srv = None  # type: ignore[assignment]  # shutdown reads it via getattr(..., None)
            return
        self._tcp_port = actual_port

        tokens = self._network_tokens()
        tls_ctx = None
        if self.cfg.network_tls:
            try:
                tls_ctx = nt.server_context(self.cfg.network_tls_cert, self.cfg.network_tls_key)
            except nt.TLSConfigError as e:
                print(f"  ⚠️  Network TLS disabled: {e}")
                tls_ctx = None

        while self._running:
            try:
                srv.settimeout(1.0)
                conn, addr = srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break  # listener closed
            if self._executor is not None:
                self._executor.submit(self._handle_tcp, conn, tokens, tls_ctx)
        try:
            srv.close()
        except OSError:
            pass

    def _handle_tcp(self, conn: socket.socket, tokens: dict | None = None, tls_ctx=None):
        """Handle one remote connection: TLS wrap (optional) → HMAC auth →
        same framed request loop as the UDS path.

        Identity: after auth, EVERY request's "client" field is over-
        written with the authenticated token-id — a remote caller cannot
        inherit another agent's [mcp.clients] policy by asserting its
        name (A4 spoof-proofing at the transport boundary).
        """
        from toolrecall import network_auth as na

        if tokens is None:
            tokens = self._network_tokens()

        request = None
        try:
            if tls_ctx is not None:
                conn = tls_ctx.wrap_socket(conn, server_side=True)
            token_id = na.server_handshake(conn, tokens)
        except Exception:
            # Auth failure / TLS failure / mid-handshake disconnect:
            # outcome-only log, no payload, connection dropped.
            self.logger.info("net outcome=auth_rejected")
            try:
                conn.close()
            except OSError:
                pass
            return

        try:
            conn.settimeout(30.0)
            while True:
                request = receive_message(conn)
                if request is None:
                    break
                # ── Identity override (A6 security boundary) ──
                request["client"] = token_id
                # Mark as remote: policy resolves from [network.clients]
                # only (per-request flag — the UDS path is unaffected)
                request["_remote"] = True
                # A6-7: the member's write namespace travels with every
                # request — cache writes land under their scope (or
                # member-level for private_writes=true), never global.
                request["_ns"] = self._member_ns(token_id)
                # ── Remote command allow-list (A6-3, review 2026-09-08) ──
                # One choke point BEFORE routing: a remote caller may only
                # invoke the cache-relevant commands. Everything else —
                # docs_*, recall_*, mcp_* (operator credentials!), cache
                # management, context tracker, shutdown/restart — is
                # local-only. Default-deny: a NEW handler added to _route
                # is NOT remote-reachable until listed here deliberately.
                if request.get("cmd") not in _REMOTE_ALLOWED_COMMANDS:
                    self.logger.info(
                        "net cmd=%s outcome=remote_denied (not in remote allow-list)",
                        request.get("cmd", "?"),
                    )
                    send_message(
                        conn,
                        {
                            "error": (
                                f"remote_denied: '{request.get('cmd')}' is not "
                                "available to network clients (local-only command). "
                                "See SECURITY.md §9.2."
                            )
                        },
                    )
                    continue
                # Protocol version gate — identical to the UDS path.
                req_v = request.get("v", 1)
                try:
                    req_v = int(req_v)
                except (TypeError, ValueError):
                    req_v = 1
                if req_v > PROTOCOL_VERSION and request.get("cmd") != "ping":
                    send_message(
                        conn,
                        {
                            "error": (
                                f"protocol_version_mismatch: client wire v{req_v} > "
                                f"daemon v{PROTOCOL_VERSION}. Upgrade the daemon: "
                                f"pip install --upgrade toolrecall"
                            ),
                            "daemon_protocol_version": PROTOCOL_VERSION,
                        },
                    )
                    break
                request_id = next(self._req_counter)
                t0 = time.perf_counter()
                response = self._route(request)
                _dt_ms = (time.perf_counter() - t0) * 1000
                if "error" in response:
                    self.logger.info(
                        "net req=%d cmd=%s outcome=error dur=%.1fms err=%s",
                        request_id,
                        request.get("cmd", "?"),
                        _dt_ms,
                        str(response.get("error", ""))[:120],
                    )
                else:
                    self.logger.info(
                        "net req=%d cmd=%s outcome=ok dur=%.1fms",
                        request_id,
                        request.get("cmd", "?"),
                        _dt_ms,
                    )
                send_message(conn, response)
        except (
            socket.timeout,
            json.JSONDecodeError,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
        ):
            pass
        except Exception:
            self.logger.exception(
                "net req cmd=%s handler crashed", request.get("cmd", "?") if request else "?"
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def start(self):
        """Start the IPC server (blocking). Must be called AFTER fork()."""
        # Lazy-init ThreadPoolExecutor AFTER fork — avoids corrupted locks
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="TRWorker")

        try:
            self._server = create_socket(self.socket_path)
            bind_socket(self._server, self.socket_path)
            self._server.listen(10)

            self._running = True

            transport_type = "TCP" if IS_WINDOWS else "UDS"
            print(f"ToolRecall Daemon v{__version__}")
            print(f"  Transport: {transport_type} at {self.socket_path}")
            print(f"  PID: {os.getpid()}")
            print(
                f"  Path allowlist: {', '.join(self.security.allowed_paths) if self.security.allowed_paths else 'ALL (DANGEROUS)'}"
            )
            print(f"  Terminal: {'ENABLED' if self.security.allow_terminal else 'DISABLED'}")
            print(f"  Invalidate: {'ENABLED' if self.security.allow_invalidate else 'DISABLED'}")

            # Start MCP Multiplexer (lazy — no servers started yet)
            if self.cfg.mcp_multiplex_enabled:
                print("\nMCP Multiplexer:")
                self.multiplexer.start()
            print("")

            # Start forward proxy (caches API responses) in daemon thread
            try:
                from toolrecall.proxy import run_forward_proxy

                fp_thread = threading.Thread(
                    target=run_forward_proxy,
                    daemon=True,
                    name="ForwardProxy",
                )
                fp_thread.start()
                print("  Forward proxy: http://127.0.0.1:8569 (caches API responses)")
            except Exception as e:
                print(f"  ⚠️  Forward proxy failed to start: {e}")
                print("     API response caching on :8569 is unavailable.")

            # Start shared network daemon (A6) — opt-in, default OFF.
            if self._should_start_network_listener():
                self._tcp_thread = threading.Thread(
                    target=self._run_tcp_listener,
                    args=(self.cfg.network_bind, self.cfg.network_port),
                    daemon=True,
                    name="NetworkListener",
                )
                self._tcp_thread.start()
            elif self.cfg.network_enabled and not self._should_start_network_listener():
                print(
                    "  ⚠️  [network] enabled but no token — network daemon NOT "
                    "started. Set TOOLRECALL_NETWORK_TOKEN (or [network].token), "
                    'or give a [network.clients."<id>"] entry its own token. '
                    "A network daemon without auth must not exist."
                )

            # Start periodic GC background thread
            self._gc_thread = threading.Thread(target=self._run_periodic_gc, daemon=True)
            self._gc_thread.start()

            # Durability: periodically merge the WAL into the main DB so a
            # crash loses at most the last minute of cache writes.
            self._checkpoint_thread = threading.Thread(
                target=self._run_periodic_checkpoint, daemon=True
            )
            self._checkpoint_thread.start()

            # Start storage sync background worker (backend decides; opt-in)
            self._start_sync_worker()

            # Update refresher: keeps latest_seen current so the agent-facing
            # hint channel (update_pending_hint) has data even when the user
            # never runs the CLI. Interval-gated by the updater state file.
            if load_config().update_enabled:
                self._update_thread = threading.Thread(
                    target=self._run_update_refresher, daemon=True, name="Update-Refresher"
                )
                self._update_thread.start()

            # Set a accept timeout so the main loop periodically checks
            # self._running instead of blocking forever on accept().
            # Without this, SIGTERM → stop() → _server.close() does NOT
            # reliably unblock accept() on Linux (closing a socket from
            # another thread doesn't interrupt a blocked accept syscall).
            # The daemon then hangs until systemd's TimeoutStopSec (90s)
            # expires and SIGKILLs it, leaving the service in "failed" state.
            self._server.settimeout(1.0)

            while self._running:
                try:
                    conn, addr = self._server.accept()
                    self._executor.submit(self._handle, conn)
                except socket.timeout:
                    continue  # No connection — loop back and check _running
                except OSError:
                    break  # Server closed

        except BaseException:
            # Ensure cleanup on any crash so we don't leave stale socket
            self.stop()
            raise

    def stop(self):
        """Graceful shutdown."""
        self._running = False
        if self._executor:
            self._executor.shutdown(wait=False)
        # Tracker snapshot (option A) BEFORE the WAL checkpoint: the row is
        # written through _db(), and the truncate-checkpoint below merges
        # those WAL frames into the main DB — so the tracker state survives
        # even if -wal/-shm sidecars are later unlinked by a competitor.
        try:
            _snap = self._context.snapshot()
            if _snap.get("saved"):
                self.logger.info(
                    "tracker snapshot saved: %s read paths, %s dirty, cp=%s",
                    _snap.get("paths", 0),
                    _snap.get("dirty", 0),
                    _snap.get("checkpoint", 0),
                )
        except Exception as e:  # noqa: BLE001 — shutdown must never abort on snapshot failure
            warnings.warn(f"ToolRecall: tracker snapshot failed during shutdown: {e}")
        # Durability: merge WAL frames into the main DB before we unlink
        # anything or exit, so every committed cache entry survives the
        # daemon's shutdown — even if the -wal/-shm sidecars were unlinked
        # by a competing process that opened the same DB.
        try:
            from toolrecall._db import db_checkpoint

            db_checkpoint(truncate=True)
        except Exception as e:  # noqa: BLE001 — shutdown must never abort on checkpoint failure
            warnings.warn(f"ToolRecall: shutdown checkpoint failed: {e}")
        self.multiplexer.shutdown()
        try:
            self._server.close()
        except Exception as e:  # noqa: BLE001 — shutdown must never abort mid-teardown
            warnings.warn(f"ToolRecall: server close failed during shutdown: {e}")
        # A6: close the TCP listener too (it loops on _running, but close()
        # unblocks a pending accept() immediately).
        tcp_srv = getattr(self, "_tcp_srv", None)
        if tcp_srv is not None:
            try:
                tcp_srv.close()
            except Exception:
                pass
        # Clean up socket file (UDS only — TCP sockets aren't files)
        if not IS_WINDOWS:
            try:
                os.unlink(self.socket_path)
            except Exception:
                pass
        print("ToolRecall Daemon stopped.")

    def _handle(self, conn: socket.socket):
        """Handle one client connection."""
        try:
            conn.settimeout(30.0)
            request = receive_message(conn)
            if request is None:
                conn.close()
                return
            # Protocol version gate (constitution VI): reject requests from a
            # NEWER major wire version with a clear error instead of
            # misparsing unknown fields. Same/older versions pass — backward
            # compatibility is the contract. "ping" bypasses the gate so a
            # client can always discover the daemon's version.
            req_v = request.get("v", 1)
            try:
                req_v = int(req_v)
            except (TypeError, ValueError):
                req_v = 1
            if req_v > PROTOCOL_VERSION and request.get("cmd") != "ping":
                send_message(
                    conn,
                    {
                        "error": (
                            f"protocol_version_mismatch: client wire v{req_v} > "
                            f"daemon v{PROTOCOL_VERSION}. Upgrade the daemon: "
                            f"pip install --upgrade toolrecall"
                        ),
                        "daemon_protocol_version": PROTOCOL_VERSION,
                    },
                )
                conn.close()
                return
            request_id = next(self._req_counter)
            t0 = time.perf_counter()
            response = self._route(request)
            _dt_ms = (time.perf_counter() - t0) * 1000
            # One payload-free INFO line per request: outcome + duration only.
            # Args/response bodies are never logged (sensitive-data no-go).
            if "error" in response:
                self.logger.info(
                    "req=%d cmd=%s outcome=error dur=%.1fms err=%s",
                    request_id,
                    request.get("cmd", "?"),
                    _dt_ms,
                    str(response.get("error", ""))[:120],
                )
            else:
                self.logger.info(
                    "req=%d cmd=%s outcome=ok dur=%.1fms",
                    request_id,
                    request.get("cmd", "?"),
                    _dt_ms,
                )
            send_message(conn, response)
        except (socket.timeout, json.JSONDecodeError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            # Unexpected handler crash: respond to the client, and record the
            # traceback in the log (previously silently swallowed).
            self.logger.exception(
                "req cmd=%s handler crashed", request.get("cmd", "?") if request else "?"
            )
            try:
                send_message(conn, {"error": str(e)})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _route(self, request: dict) -> dict:
        """Route a request to the appropriate handler."""
        cmd = request.get("cmd", "")

        try:
            if cmd == "cached_read":
                return self._handle_read(request)
            elif cmd == "cached_shell_exec":
                return self._handle_shell_exec(request)
            elif cmd == "cached_terminal":
                return self._handle_terminal(request)
            elif cmd == "cached_skill":
                return self._handle_skill(request)
            elif cmd == "cached_write":
                return self._handle_write(request)
            elif cmd == "cached_patch":
                return self._handle_patch(request)
            elif cmd == "docs_search":
                return _count_tool_call(cmd, self._handle_docs_search, request)
            elif cmd == "docs_get_page":
                return _count_tool_call(cmd, self._handle_docs_get_page, request)
            elif cmd == "cache_status":
                return _count_tool_call(cmd, self._handle_status, request)
            elif cmd == "cache_invalidate":
                return self._handle_invalidate(request)
            elif cmd == "dedup_tokens":
                return self._handle_dedup_tokens(request)
            elif cmd == "work_store":
                return self._handle_work_store(request)
            elif cmd == "work_lookup":
                return self._handle_work_lookup(request)
            elif cmd == "cache_refresh_file":
                return self._handle_refresh_file(request)
            elif cmd == "mcp_call":
                return _count_tool_call(cmd, self._handle_mcp_call, request)
            elif cmd == "mcp_list_servers":
                return _count_tool_call(cmd, self._handle_mcp_list_servers, request)
            elif cmd == "mcp_tools_catalog":
                return self._handle_mcp_tools_catalog(request)
            elif cmd == "cached_mcp_check":
                return self._handle_mcp_check(request)
            elif cmd == "cached_mcp_serve":
                return self._handle_mcp_serve(request)
            elif cmd == "cached_mcp_store":
                return self._handle_mcp_store(request)
            elif cmd == "cached_browser_check":
                return self._handle_browser_check(request)
            elif cmd == "cached_browser_store":
                return self._handle_browser_store(request)
            elif cmd == "cached_api_check":
                return self._handle_api_check(request)
            elif cmd == "cached_api_store":
                return self._handle_api_store(request)
            elif cmd == "ping":
                return self._handle_ping(request)
            elif cmd == "shutdown":
                # Graceful shutdown — clean up PID, socket, then exit
                self._handle_shutdown()
                return {"result": "Shutting down"}
            elif cmd == "restart":
                # Restart — forks a new daemon, then exits
                return self._handle_restart(request)
            elif cmd == "context_set_checkpoint":
                return self._handle_context_set_checkpoint(request)
            elif cmd == "context_get_dirty":
                return self._handle_context_get_dirty(request)
            elif cmd == "context_get_hint":
                return self._handle_context_get_hint(request)
            elif cmd == "update_pending_hint":
                return self._handle_update_pending_hint(request)
            elif cmd == "context_get_stale":
                return self._handle_context_get_stale(request)
            elif cmd == "context_get_stats":
                return self._handle_context_get_stats(request)
            elif cmd == "context_reset":
                return self._handle_context_reset(request)
            elif cmd == "context_compacted":
                return self._handle_context_compacted(request)
            elif cmd == "recall_store":
                return self._handle_recall_store(request)
            elif cmd == "recall_get":
                return self._handle_recall_get(request)
            elif cmd == "recall_stats":
                return self._handle_recall_stats(request)
            else:
                return {"error": f"Unknown command: {cmd}"}

        except Exception as e:
            return {"error": str(e)}

    def _handle_ping(self, req: dict) -> dict:
        ctx = self._context.get_stats()
        # Config fingerprint — compare this across process restarts
        config_hash = (
            str(self.security.allowed_paths)
            + str(self.security.allow_terminal)
            + str(self.security.allowed_terminal_commands)
            + str(self.security.allow_invalidate)
            + str(self.security.allow_multiplex)
            + str(self.cfg.mcp_emit_context_hints)
            + str(self.cfg.mcp_client_hint_policy)
        )
        config_hash = content_digest(config_hash, truncate=16)
        return {
            "pong": True,
            "pid": os.getpid(),
            "protocol_version": PROTOCOL_VERSION,
            "config_hash": config_hash,
            "allowed_paths": self.security.allowed_paths,
            "allow_terminal": self.security.allow_terminal,
            "allow_invalidate": self.security.allow_invalidate,
            "multiplex_enabled": self.security.allow_multiplex,
            "emit_context_hints": self.cfg.mcp_emit_context_hints,
            "client_hint_policy": self.cfg.mcp_client_hint_policy,
            "recall_enabled": self.cfg.recall_enabled,
            "multiplex_servers": list(self.multiplexer._sessions.keys()),
            "gateway": bool(self.cfg.mcp_multiplex_gateway),
            "context_tracker": {
                "checkpoint": ctx.get("checkpoint", 0),
                "dirty": ctx.get("total_dirty", 0),
                "clean": ctx.get("total_clean", 0),
                "total_read": ctx.get("total_read", 0),
                "ctx_dropped_tokens": ctx.get("ctx_dropped_tokens_total", 0),
                "agent_armed": ctx.get("agent_armed", False),
            },
        }

    def _handle_shutdown(self) -> None:
        """Graceful shutdown: clean PID, socket, then exit."""
        # Clean up PID file — only if this daemon owns the production socket.
        # A hermetic daemon never wrote it; deleting the production daemon's
        # PID file on its way out is the same defect in reverse.
        if _should_manage_pid_file(getattr(self, "socket_path", None)) and os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
        # Clean up socket
        if hasattr(self, "socket_path") and self.socket_path:
            try:
                os.unlink(self.socket_path)
            except Exception:
                pass
        # Stop multiplexer
        if hasattr(self, "multiplexer"):
            try:
                self.multiplexer.shutdown()
            except Exception:
                pass
        # Exit in a thread-safe way
        threading.Thread(target=self._do_exit, daemon=True).start()

    def _do_exit(self) -> None:
        # Use os.kill(SIGTERM) instead of sys.exit(0) or os._exit(0).
        # sys.exit(0) in a daemon thread only kills the thread (SystemExit
        # is caught by the thread runner), leaving the process as a zombie.
        # os._exit(0) skips finally blocks, atexit handlers, and __del__,
        # leaving MCP subprocesses running and SQLite WAL uncommitted.
        # os.kill(SIGTERM) triggers the registered signal handler which
        # sets _running = False and calls stop() in the main thread,
        # doing proper cleanup (multiplexer, socket, PID file).
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    def _handle_restart(self, req: dict) -> dict:
        """Restart: spawn a new daemon, then shut down."""
        foreground = req.get("foreground", False)
        # Spawn new daemon process
        python = sys.executable
        args = [python, "-m", "toolrecall", "daemon"]
        if foreground:
            args.append("--foreground")
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Clean up and exit
        self._handle_shutdown()
        return {"result": "Restarting"}

    def _handle_read(self, req: dict) -> dict:
        path = req.get("path", "")
        if not path:
            return {"error": "Missing 'path'"}
        # Per-agent policy (A4): agent-specific allowed_paths when set.
        # A6: `_remote=True` marks a TCP-authenticated request — policy
        # comes from [network.clients] only.
        err = self.security.check_read_path_for(
            path, req.get("client"), remote=bool(req.get("_remote"))
        )
        if err:
            return {"error": err}
        bypass = req.get("bypass_cache", False)
        if bypass:
            from toolrecall.cache import invalidate_file

            invalidate_file(path, ns=req.get("_ns", ""))
        # If source is "agent_tool", pass it through so context tokens are tracked
        source = req.get("source", "")
        agent_id = req.get("agent_id") or req.get("client")
        result = _cache_read(
            path,
            source=source,
            ns=req.get("_ns", ""),
            client=req.get("client", ""),
            agent_id=agent_id,
        )
        if result and not result.get("error"):
            # Echo the source tag back: the shim's stub gate needs to know
            # whether THIS read was agent-visible (2026-09-18 live incident —
            # a stub served to the gateway's own config re-read corrupted
            # its YAML parse; the shim must only stub agent_tool reads).
            result["source"] = source
            # Context-tracker gate (2026-09-12): track ONLY agent-tool reads.
            # The shim tags infra reads "shim" (its size/binary/library gates),
            # and gateway processes (cron scripts, TUI, healthcheck, the Hermes
            # gateway's own caches — models_dev_cache.json alone is 1.15M tok)
            # funnel through the shim into cached_read. Tracking those
            # poisoned the tracker's clean set: the "ctx dropped" number was
            # 70% infra files that are never in any LLM context. Mirrors the
            # tokens_saved_to_llm rule — only agent-tool reads can drop
            # content from context.
            if source == "agent_tool":
                self._context.mark_read(path)
            # Stamp the compaction epoch: the bridge drops its session stub/diff
            # state when this differs from the value it last saw (see __init__).
            # getattr, not self._compaction_epoch: this is auxiliary signalling
            # and must never turn a file read into an error. Handlers are also
            # driven from partially-built servers (tests construct one via
            # __new__), where an AttributeError here would fail the read itself
            # — the same "a counter must never break a tool call" rule that
            # _count_measured follows.
            result["compaction_epoch"] = getattr(self, "_compaction_epoch", 0)
        return result

    def _handle_shell_exec(self, req: dict) -> dict:
        """Route a wrapped shell command through the same terminal security gate.

        Mirrors _handle_terminal: the wrapper-stripped inner command is checked
        against the SecurityGate (allow_terminal + allowed_terminal_commands
        regex allowlist) BEFORE execution. Prevents the cached_shell_exec path
        from bypassing the terminal policy that cached_terminal enforces.
        """
        command = req.get("command", "")
        if not command:
            return {"error": "Missing 'command'"}
        from toolrecall.cache import _strip_shell_wrapper

        inner = _strip_shell_wrapper(command) or command
        # Per-agent policy (A4): same gate as _handle_terminal; A6 remote flag
        err = self.security.check_terminal_for(
            inner, req.get("client"), remote=bool(req.get("_remote"))
        )
        if err:
            return {"error": err}
        from toolrecall.cache import cached_shell_exec

        result = cached_shell_exec(command, cwd=req.get("cwd"))
        # Record mcp_cache stats when request originates from MCP bridge
        if req.get("mcp_origin"):
            _cache_record("mcp_cache", hit=result.get("cached", False), path=inner)
        return result

    def _handle_terminal(self, req: dict) -> dict:
        command = req.get("command", "")
        # Per-agent policy (A4): the MCP bridge stamps "client" (clientInfo.name)
        # onto every request; direct UDS callers without it get global policy.
        # A6: `_remote=True` marks a TCP-authenticated request — policy comes
        # from [network.clients] only.
        client = req.get("client")
        err = self.security.check_terminal_for(command, client, remote=bool(req.get("_remote")))
        if err:
            return {"error": err}
        if not command:
            return {"error": "Missing 'command'"}
        agent_id = req.get("agent_id") or client
        ttl = req.get("ttl")
        # Pass the client-reported cwd through so the terminal cache key is
        # directory-scoped. The daemon never fabricates its own cwd — it may
        # run as a service with a different working directory than the caller.
        result = _cache_terminal(command, ttl=ttl, cwd=req.get("cwd"), agent_id=agent_id)
        # Record mcp_cache stats when request originates from MCP bridge
        if req.get("mcp_origin"):
            _cache_record("mcp_cache", hit=result.get("cached", False), path=command)
        # Compaction epoch (see __init__): same signal as the read path, so a
        # terminal-only turn cannot miss a compaction. Defensive read for the
        # same reason as the read stamp.
        if isinstance(result, dict) and not result.get("error"):
            result["compaction_epoch"] = getattr(self, "_compaction_epoch", 0)
        return result

    def _handle_skill(self, req: dict) -> dict:
        name = req.get("name", "")
        if not name:
            return {"error": "Missing 'name'"}
        agent_id = req.get("agent_id") or req.get("client")
        return _cache_skill(name, agent_id=agent_id)

    def _handle_write(self, req: dict) -> dict:
        path = req.get("path", "")
        content = req.get("content", "")
        if not path:
            return {"error": "Missing 'path'"}
        if content is None:
            return {"error": "Missing 'content'"}
        # A6-1 fix: remote callers resolve write access from [network.clients]
        # ONLY (same gate as reads) — the global [mcp] allowlist never applies
        # remotely, so a default-deny remote token cannot write anywhere.
        err = self.security.check_read_path_for(
            path, req.get("client"), remote=bool(req.get("_remote"))
        )
        if err:
            return {"error": err}
        result = _cache_write(path, content, ns=req.get("_ns", ""), client=req.get("client", ""))
        # Track write as dirty for context tracker
        if result and not result.get("error"):
            self._context.mark_dirty(path)
        # Record mcp_cache stats when request originates from MCP bridge
        if req.get("mcp_origin"):
            _cache_record("mcp_cache", hit=result.get("unchanged", False), path=path)
        return result

    def _handle_patch(self, req: dict) -> dict:
        path = req.get("path", "")
        old_string = req.get("old_string", "")
        new_string = req.get("new_string", "")
        if not path:
            return {"error": "Missing 'path'"}
        if not old_string:
            return {"error": "Missing 'old_string'"}
        # A6-1 fix: same remote-aware gate as write (see above).
        err = self.security.check_read_path_for(
            path, req.get("client"), remote=bool(req.get("_remote"))
        )
        if err:
            return {"error": err}
        result = _cache_patch(
            path,
            old_string,
            new_string,
            ns=req.get("_ns", ""),
            client=req.get("client", ""),
        )
        # Track patch as dirty for context tracker
        if result and not result.get("error"):
            self._context.mark_dirty(path)
        # Record mcp_cache stats when request originates from MCP bridge
        if req.get("mcp_origin"):
            _cache_record("mcp_cache", hit=result.get("unchanged", False), path=path)
        return result

    def _handle_recall_store(self, req: dict) -> dict:
        """Persist a non-reproducible content block; return a node_id pointer."""
        if not self.cfg.recall_enabled:
            return {"error": "recall tier disabled (enable [recall].enabled)"}
        fingerprint = req.get("fingerprint", "")
        content = req.get("content", "")
        if not fingerprint or not content:
            return {"error": "Missing 'fingerprint' or 'content'"}
        from toolrecall import recall

        nid = recall.store(
            fingerprint=fingerprint,
            content=content,
            content_type=req.get("content_type", "other"),
            reproducible=bool(req.get("reproducible")),
            summary=req.get("summary", ""),
            ttl=self.cfg.recall_ttl,
        )
        # Accounting: store = a disk-write miss for the recall sink.
        _cache_record("recall", hit=False, tokens_read=max(1, len(content) // 3))
        return {"node_id": nid}

    def _handle_recall_get(self, req: dict) -> dict:
        """Restore a persisted block by node_id."""
        if not self.cfg.recall_enabled:
            return {"error": "recall tier disabled (enable [recall].enabled)"}
        node_id_ = req.get("node_id", "")
        if not node_id_:
            return {"error": "Missing 'node_id'"}
        from toolrecall import recall

        entry = recall.get(node_id_)
        if entry is not None:
            # Accounting: a get hit = tokens served from the recall sink
            # that would have been re-sent into the context.
            tokens = int(entry.get("tokens") or 0)
            _cache_record("recall", hit=True, io_tokens_saved=tokens, context_tokens=tokens)
        return {"entry": entry}

    def _handle_recall_stats(self, req: dict) -> dict:
        """Return recall-cache aggregate totals."""
        if not self.cfg.recall_enabled:
            return {"error": "recall tier disabled (enable [recall].enabled)"}
        from toolrecall import recall

        return recall.stats()

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
        result = _docs_get_page(path, source)
        return {"result": result}

    def _handle_status(self, req: dict) -> dict:
        stats = get_stats()
        return {"result": stats}

    def _handle_dedup_tokens(self, req: dict) -> dict:
        """Count MEASURED not-sent tokens from bridge dedup stubs.

        The bridge calls this when it replaced file/terminal content with a
        stub — at that moment the tokens provably never reach the LLM. This
        is the only wire-verified CONTEXT-tier number; it lands in
        cache_stats under its own category ('mcp_dedup') so estimate tiers
        can never contaminate it, and in access_log for the per-day trend.
        """
        tokens = req.get("tokens", 0)
        path = req.get("path", "")
        try:
            tokens = int(tokens)
        except (TypeError, ValueError):
            tokens = 0
        if tokens > 0:
            _cache_record(
                "mcp_dedup",
                hit=False,
                path=path,
                tokens_not_sent_measured=tokens,
            )
        return {"ok": True, "counted": tokens}

    def _handle_work_store(self, req: dict) -> dict:
        """Record one PAID evaluation of a work unit (identity: keys.work_key).

        Fail-closed on a malformed request: an empty key or a non-dict metrics
        payload is refused, never coerced. The reuse POLICY stays with the
        caller — the daemon only stores the result and counts attempts.

        The key is caller-supplied and trusted only as a memo *address*, never
        as an authorization claim (§2 Zero-trust): whoever calls this can only
        pollute its own memo lookups, and the row carries no path or privilege.
        """
        key = req.get("key")
        if not isinstance(key, str) or not key.strip():
            return {"error": "Missing or empty 'key'"}
        metrics = req.get("metrics")
        if not isinstance(metrics, dict):
            return {"error": "'metrics' must be an object"}
        try:
            created = _work_store(
                key,
                metrics,
                eval_time=float(req.get("eval_time") or 0.0),
                ttl=float(req.get("ttl") or 0.0),
            )
        except (TypeError, ValueError) as e:
            return {"error": str(e)}
        return {"result": {"stored": created, "key": key}}

    def _handle_work_lookup(self, req: dict) -> dict:
        """Read a work unit's recorded result — ``null`` when the unit is unknown."""
        key = req.get("key")
        if not isinstance(key, str) or not key.strip():
            return {"error": "Missing or empty 'key'"}
        try:
            record = _work_lookup(key)
        except (TypeError, ValueError) as e:
            return {"error": str(e)}
        return {"result": record}

    def _handle_invalidate(self, req: dict) -> dict:
        err = self.security.check_invalidate()
        if err:
            return {"error": err}
        scope = req.get("scope", "agent")
        if scope not in ("agent", "api"):
            return {"error": f"unknown scope: {scope!r} (expected 'agent' or 'api')"}
        if scope == "api":
            # Opt-in wipe of billed LLM responses — the agent caches are
            # cleared too, so callers get the full table sweep they asked
            # for, with api row counts reported (money-tier visibility).
            invalidate_all()
            api_rows = invalidate_api_cache()
            return {
                "result": (
                    f"All ToolRecall caches cleared (scope=api): agent caches + "
                    f"{api_rows} api_cache entries"
                ),
                "api_entries_cleared": api_rows,
            }
        invalidate_all()
        return {
            "result": (
                "All ToolRecall agent caches cleared (api_cache untouched — "
                "resend with scope=api to include it)"
            )
        }

    def _handle_refresh_file(self, req: dict) -> dict:
        """Invalidate and re-read a single file. Safe — no security gate needed.
        Only re-reads from disk, does not destroy other cache entries.
        Returns same shape as _handle_read().
        """
        # Delegate to _handle_read bypass — it handles invalidation + fresh read
        # through the daemon's own cache path, avoiding SQLite lock contention.
        req["bypass_cache"] = True
        return self._handle_read(req)

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

        # MCP Keyword Access Control check
        sandbox_err = self.security.check_mcp_tool_access(tool)
        if sandbox_err:
            return {"error": sandbox_err}

        # ═══ Security scan: cognitive + AST injection ═══
        # Both scans are config-gated (cognitive_check_enabled, ast_check_enabled).
        # They run BEFORE the cache lookup so cached results also pass security.
        cog_err = self.security.cognitive_scan_arguments(arguments)
        if cog_err:
            print(f"[ToolRecall] Cognitive scan blocked MCP call: {server}/{tool}")
            return {"error": cog_err}
        ast_err = self.security.check_ast_injection(arguments)
        if ast_err:
            print(f"[ToolRecall] AST injection blocked MCP call: {server}/{tool}")
            return {"error": ast_err}

        # Cache eligibility: transparent cache ON *and* the tool is a read.
        # A side-effecting tool is never served from (or written to) the cache:
        # a repeated create_issue inside the TTL used to return the stored
        # reply and never reach the server. Classification prefers the server's
        # own readOnlyHint annotation and falls back to read/write verbs in the
        # tool name (cache.mcp_tool_is_cacheable).
        cached = {"key": f"{server}:{tool}"}  # default for type checker
        cache_enabled = self.multiplexer.cfg.mcp_multiplex_transparent_cache
        server_cfg = self.multiplexer.cfg.mcp_multiplex_servers_config.get(server, {})
        ttl = server_cfg.get("ttl", self.multiplexer.cfg.mcp_multiplex_default_ttl)
        # A per-request TTL wins over the server config. The bridge sends
        # ttl=0 for bypass_cache; the daemon used to read only the server
        # config, so "bypass" was silently ignored and a fresh read was
        # served from cache with no way for the caller to tell.
        req_ttl = req.get("ttl")
        if isinstance(req_ttl, (int, float)) and not isinstance(req_ttl, bool):
            ttl = int(req_ttl)
        cacheable = cache_enabled and self._mcp_tool_cacheable(server, tool)
        if cacheable:
            cached = _cache_mcp_check(server, tool, arguments, ttl=ttl)
            if cached.get("cached"):
                # Forward the staleness flag alongside the hit: the bridge
                # surfaces it to the client (SWR serve-stale bodies may lag
                # the source), otherwise the flag dies here.
                return {
                    "result": cached["data"],
                    "cached": True,
                    "stale": bool(cached.get("stale")),
                    # Policy TTL: the bound on how old this body may be. The
                    # bridge states it in the provenance marker — "cached" is
                    # only actionable if the client knows the staleness bound.
                    "ttl": int(ttl) if isinstance(ttl, (int, float)) else None,
                }

        # ETag revalidation (opt-in, fetch server): a cache MISS that has an
        # EXPIRED row with a stored validator → ONE conditional request
        # (If-None-Match) instead of a full refetch. 304 = content unchanged
        # → serve the old body (no body transfer). 200 → row already replaced
        # with the fresh body. Any other outcome → fall through to the
        # normal live call + store below.
        if (
            cacheable
            and cached.get("key")
            and server == "fetch"
            and tool == "fetch_url"
            and isinstance(arguments, dict)
            and self.multiplexer.cfg.mcp_multiplex_fetch_revalidate
        ):
            url = arguments.get("url", "")
            if not isinstance(url, str) or not url:
                return {
                    "result": json.dumps({"error": "fetch_revalidate: URL required"}),
                    "cached": False,
                }
            # Narrowed for the closure — mypy can't carry isinstance-narrowing
            # into the lambda below.
            revalidate_url: str = url
            outcome = _revalidate_mcp_entry(
                "fetch",
                "fetch_url",
                arguments,
                fetch_fn=lambda etag: _fetch_revalidate_once(revalidate_url, etag),
            )
            if outcome["action"] in ("revalidated", "replaced"):
                # refresh_mcp_etag / the replaced store both bumped the row
                # to fresh — read it back through the normal check path so
                # hit accounting stays consistent.
                refreshed = _cache_mcp_check(server, tool, arguments, ttl=ttl)
                if refreshed.get("cached"):
                    return {
                        "result": refreshed["data"],
                        "cached": True,
                        "revalidated": outcome["action"] == "revalidated",
                        "ttl": int(ttl) if isinstance(ttl, (int, float)) else None,
                    }
            # failed / not_found / no_etag / read-back miss → normal path

        # Call the multiplexed server
        result = self.multiplexer.call(server, tool, arguments)

        # Store in cache (only when transparent cache is enabled)
        # EXCEPT: never cache errors. A failed tool call is not an answer —
        # caching it would pin a transient failure (network blip, crashed
        # subprocess, permission error) for the whole TTL and *replay* it to
        # every identical call, converting a retryable miss into a dead key.
        # Found live 2026-09-22: a daemon-env fetch failure was stored and
        # then served from cache on the second call. Poisoned rows from
        # before this fix are healed below (delete + re-stamp stats).
        if cache_enabled:
            # Re-evaluate eligibility AFTER the call: the live call is what
            # starts a cold server, and a running server is what carries the
            # tool annotations. Without this, the first call to a write tool
            # on a cold server would be classified by name alone and stored.
            if not self._mcp_tool_cacheable(server, tool):
                # A side-effecting tool: its reply is not an answer to reuse,
                # and the call may have changed state the cache still
                # describes from before it. Drop this server's cached reads
                # (this also clears any legacy row for the tool itself, held
                # over from when writes were cached). Invalidating after a
                # FAILED write costs only a refetch — the safe direction.
                _cache_invalidate_mcp_server(server)
            elif self._mcp_result_is_error(result):
                # The poisoned row (if any) must not survive its TTL as
                # "truth". StaleWhileRevalidate would keep serving it
                # otherwise.
                _heal_poisoned_mcp_row(cached.get("key", f"{server}:{tool}"))
            else:
                import json as _json

                result_json = _json.dumps(result)
                _cache_mcp_store(
                    cached.get("key", f"{server}:{tool}"),
                    server,
                    tool,
                    arguments,
                    result_json,
                    ttl=ttl,
                )

        return {"result": result, "cached": False}

    @staticmethod
    def _mcp_result_is_error(result: object) -> bool:
        """True when a multiplexer result is an error, not an answer.

        Two shapes arrive at this boundary:
          * the multiplexer's own failure dict {"error": ...}
          * a tool's JSON-encoded {"error": ...} payload (stdlib servers
            return results as JSON strings)
        Deliberately narrow: a result that merely MENTIONS error (a body
        quoting an error message) is content, not a failure — only a
        top-level error key counts.
        """
        import json as _json

        if isinstance(result, dict) and "error" in result:
            return True
        if isinstance(result, str):
            try:
                parsed = _json.loads(result)
            except (ValueError, TypeError):
                return False
            if isinstance(parsed, dict) and "error" in parsed:
                return True
        return False

    def _mcp_tool_cacheable(self, server: str, tool: str) -> bool:
        """True when this tool's result may be cached (see _handle_mcp_call).

        Single classification point: the pre-check (may we SERVE a stored
        row?) and the post-call decision (may we STORE this reply?) must
        never disagree, or a write could still be pinned by the other path.
        Annotations come from the running server's tools/list; a cold server
        has none yet, so the call is name-classified until it has started —
        which is why the store decision re-runs this after the live call.
        """
        return _mcp_tool_is_cacheable(tool, self.multiplexer.tool_annotations(server, tool))

    def _handle_mcp_list_servers(self, req: dict) -> dict:
        """List available multiplexed MCP servers with their tools."""
        err = self.security.check_mcp_server("_any")
        if err:
            return {"error": err}
        servers = self.multiplexer.list_servers()
        return {"result": servers}

    def _handle_mcp_tools_catalog(self, req: dict) -> dict:
        """Gateway catalog: every multiplexed server's tools (with schemas).

        Discovery metadata only. The bridge appends these to its tools/list as
        ``mcp__<server>__<tool>`` so agents can call a multiplexed tool
        directly instead of discovering it through two indirections.
        """
        err = self.security.check_mcp_server("_any")
        if err:
            return {"error": err}
        return {"result": {"servers": self.multiplexer.catalog()}}

    # ─── MCP Cache IPCs (key-value check/store) ──────────

    def _handle_mcp_check(self, req: dict) -> dict:
        """Generic MCP cache check by key.

        Args in request:
            key (str): cache key to look up
            scope (str, optional): namespace, default "local". Pass "shared"
                to read the shared tier; the same scope must be used at
                store time or the row lands in a different key space.

        Returns cached content or miss indicator.
        """
        key = req.get("key", "")
        if not key:
            return {"error": "Missing 'key' parameter"}
        scope = req.get("scope") or "local"
        return _cache_mcp_check("_generic", "_lookup", {"key": key}, scope=scope)

    def _handle_mcp_serve(self, req: dict) -> dict:
        """Read-only, validator-gated serve from the SHARED namespace.

        Args in request:
            key (str): exact row key (generic IPC key-value facade mode)
            server (str): MCP server name (server/tool mode)
            tool (str): tool name
            arguments (dict, optional): tool arguments
            scope (str, optional): namespace, default "shared"

        The only MCP cache command reachable by remote callers. It is
        read-only (no write primitive, so cache poisoning is structurally
        impossible) and only addresses the shared namespace, so a caller can
        never read a private/local row. Rows without a validator are refused
        loudly (``reason="unvalidated"``) rather than served on a clock guess:
        Phase C measured ~29% wrong deliveries for clock-based serving vs 0%
        for validate-before-serve. Callers receive ``etag`` and are expected
        to revalidate against the origin before using ``data``.
        """
        key = req.get("key") or None
        server = req.get("server") or None
        tool = req.get("tool") or None
        if not key and not (server and tool):
            return {"error": "Provide 'key', or both 'server' and 'tool'"}
        arguments = req.get("arguments") or {}
        scope = req.get("scope") or "shared"
        if key:
            # Row keys written through the generic IPC facade live in that
            # facade's key space; re-derive it exactly as check/store do.
            key = _cache_mcp_check("_generic", "_lookup", {"key": key}, scope=scope)["key"]
        return _cache_mcp_serve(server=server, tool=tool, arguments=arguments, scope=scope, key=key)

    def _handle_mcp_store(self, req: dict) -> dict:
        """Generic MCP cache store by key.

        Args in request:
            key (str): cache key
            content (str): data to store
            etag (str, optional): HTTP validator captured from the live
                response. Enables validate-before-serve: without it a row
                can never be revalidated and freshness can only be guessed
                from the clock. Measured (Phase C, 2026-09-20): clock-only
                policies delivered ~29% wrong results; validate-before-serve
                delivered 0%.
            ttl (int, optional): row TTL in seconds
            scope (str, optional): namespace, default "local". Use "shared"
                for rows that the read-only serve tier may hand to other
                agents. The scope must match at check time.
            url (str, optional): source URL
            contentType (str, optional): content type
        """
        key = req.get("key", "")
        content = req.get("content", "")
        if not key:
            return {"error": "Missing 'key' parameter"}
        etag = req.get("etag") or None
        ttl = req.get("ttl")
        scope = req.get("scope") or "local"
        # Address the row the CHECK reads: _handle_mcp_check looks up
        # cached_mcp_check("_generic", "_lookup", {"key": <sent key>}, scope=…),
        # whose row key is _mcp_request_key of the WRAPPED dict — not the raw
        # sent key. Storing under the raw key stranded every IPC store where
        # the check could never reach it (found by the 2026-09-19 daemon-in-
        # the-loop replay bench). Derivation lives in ONE place (cache.py) per
        # its contract; never re-derive this formula at a call site.
        _cache_mcp_store(
            _cache_mcp_check("_generic", "_lookup", {"key": key}, scope=scope)["key"],
            "_generic",
            "_store",
            {"key": key},
            content,
            ttl=ttl if isinstance(ttl, int) else None,
            etag=etag,
        )
        return {"stored": True, "validated": etag is not None, "scope": scope}

    # ─── Browser Cache IPCs ────────────────────────────────

    def _handle_browser_check(self, req: dict) -> dict:
        """Check if browser page content is cached.

        Args:
            cache_key (str): e.g. ``browser:page:https_example_com:snapshot``
        """
        cache_key = req.get("cache_key", req.get("key", ""))
        if not cache_key:
            return {"error": "Missing 'cache_key' parameter"}
        return _cache_browser_check(cache_key)

    def _handle_browser_store(self, req: dict) -> dict:
        """Store browser page content in cache.

        Args:
            cache_key (str): unique key
            content (str): page content
            url (str): original URL
            content_type (str): ``html``, ``text``, or ``snapshot``
            title (str, optional): page title
            content_hash (str, optional): for change detection
        """
        cache_key = req.get("cache_key", req.get("key", ""))
        content = req.get("content", "")
        if not cache_key:
            return {"error": "Missing 'cache_key' parameter"}
        return _cache_browser_store(
            cache_key=cache_key,
            content=content,
            url=req.get("url", ""),
            content_type=req.get("content_type", req.get("contentType", "snapshot")),
            title=req.get("title", ""),
            content_hash=req.get("content_hash", ""),
        )

    # ─── API Cache IPCs ────────────────────────────────────

    def _handle_api_check(self, req: dict) -> dict:
        """Check if an API response is cached by request hash."""
        request_hash = req.get("request_hash", req.get("key", ""))
        if not request_hash:
            return {"error": "Missing 'request_hash' parameter"}
        agent_id = req.get("agent_id") or req.get("client")
        return _cache_api_check(request_hash, agent_id=agent_id)

    def _handle_api_store(self, req: dict) -> dict:
        """Store an API response in cache."""
        request_hash = req.get("request_hash", req.get("key", ""))
        if not request_hash:
            return {"error": "Missing 'request_hash' parameter"}
        return _cache_api_store(
            request_hash=request_hash,
            method=req.get("method", "POST"),
            host=req.get("host", ""),
            path=req.get("path", ""),
            request_body_hash=req.get("request_body_hash", ""),
            response_status=req.get("response_status", 200),
            response_headers=req.get("response_headers", {}),
            response_body=req.get("response_body", ""),
            ttl=req.get("ttl", None),
        )

    # ─── Context Tracker IPCs ───────────────────────────────

    def _handle_context_set_checkpoint(self, req: dict) -> dict:
        """Set a checkpoint — mark current state as clean.

        Args in request:
            name (str, optional): human-readable label

        Returns:
            {"checkpoint": int, "name": str, "dirty_before": int}
        """
        name = req.get("name", "")
        return self._context.set_checkpoint(name=name)

    def _handle_context_get_dirty(self, req: dict) -> dict:
        """Get dirty and clean files since a checkpoint.

        Args in request:
            checkpoint (int, optional): Checkpoint ID. None = current.

        Returns:
            {dirty: [...], clean: [...], checkpoint: int, ...,
             _agent_hint: str}
        """
        checkpoint = req.get("checkpoint")
        result = self._context.get_dirty(checkpoint=checkpoint)
        result["_agent_hint"] = self._format_context_hint(result)
        return result

    def _handle_update_pending_hint(self, req: dict) -> dict:
        """Agent-facing update notice (once per version).

        Backs the bridge's tool-result hint channel: with
        [update] auto_apply = false (the default), the CLI's stdout notice
        never reaches agent-driven users — tool output does. The daemon
        consults the updater state file (latest_seen, refreshed by the CLI
        hook OR the daemon's own periodic refresher) and marks the version
        notified so a long-lived session doesn't repeat it.

        Never runs a network check here — the request path stays offline.
        """
        try:
            from toolrecall import updater

            hint = updater.pending_update_hint()
            if hint:
                updater.mark_update_notified(updater._load_state().get("latest_seen") or "")
            return {"hint": hint}
        except Exception:  # noqa: BLE001 — a notice must never break a tool call
            return {"hint": ""}

    def _run_update_refresher(self):
        """Periodic PyPI check in a background thread (interval-gated).

        The CLI hook only runs when a user types a command — agent-driven
        setups can go weeks without one, and with auto_apply = false the
        notify-only path then has nothing to notify about. This keeps
        latest_seen fresh so the hint channel has current data. Honors the
        same [update] enabled gate; failures just mean a stale latest_seen.
        First attempt 60s after start (let the daemon boot cleanly); the
        updater's own interval gate makes every attempt cheap — real
        network happens at most once per check_interval_hours.
        """
        from toolrecall import updater

        first = True
        while self._running:
            wait = 60 if first else 3600
            first = False
            # Sleep in 1s slices so daemon shutdown isn't delayed.
            for _ in range(wait):
                if not self._running:
                    return
                time.sleep(1)
            try:
                cfg = load_config()
                if updater._update_enabled(cfg):
                    updater.check_for_update(cfg)
            except Exception:  # noqa: BLE001 — refresher is best-effort
                pass

    def _handle_context_get_hint(self, req: dict) -> dict:
        """Get a context hint without fetching the full dirty/clean list.

        Returns:
            {hint: str, has_dirty: bool, has_clean: bool}
        """
        result = self._context.get_dirty()
        hint = self._format_context_hint(result)
        return {
            "hint": hint,
            "has_dirty": bool(result.get("dirty")),
            "has_clean": bool(result.get("clean")),
        }

    def _format_context_hint(self, result: dict) -> str:
        """Format a context management hint from dirty/clean data."""
        dirty = result.get("dirty", [])
        clean = result.get("clean", [])
        if not dirty and not clean:
            return ""
        lines = []
        if clean:
            lines.append("🧹 Drop these clean files from context (re-read from cache if needed):")
            for p in clean:
                lines.append(f"  - {p}")
        if dirty:
            lines.append("📝 Keep dirty files (you edited them):")
            for p in dirty:
                lines.append(f"  - {p}")
        return "\n".join(lines)

    def _handle_context_get_stale(self, req: dict) -> dict:
        """Files read and later overwritten — provably stale in context.

        Provider-agnostic: returns file paths. The caller decides how to
        act on them (Anthropic context editing, harness compaction,
        manual eviction, or nothing at all).

        Returns:
            {stale: [...], paths: [...], total_stale: int,
             est_reclaimable_tokens: int, _agent_hint: str}
        """
        result = self._context.get_stale()
        result["_agent_hint"] = self._format_stale_hint(result)
        return result

    def _format_stale_hint(self, result: dict) -> str:
        """Format an eviction hint from stale-file data.

        Paths are sanitized and capped: this string is injected into an
        agent's context, and filenames are attacker-influenceable.
        """
        entries = result.get("stale", [])
        if not entries:
            return ""
        safe = [(_sanitize_path_for_hint(e["path"]), e) for e in entries]
        safe = [(p, e) for p, e in safe if p]
        if not safe:
            return ""
        tokens = result.get("est_reclaimable_tokens", 0)
        lines = [
            "♻️  These files changed on disk after you read them. "
            "The copies in your context are out of date — "
            f"evict or re-read them (~{tokens:,} tokens):"
        ]
        for path, e in safe[:_MAX_HINT_PATHS]:
            lines.append(f"  - {path} (~{e['est_tokens']:,} tok)")
        if len(safe) > _MAX_HINT_PATHS:
            lines.append(f"  ... and {len(safe) - _MAX_HINT_PATHS} more")
        return "\n".join(lines)

    def _handle_context_get_stats(self, req: dict) -> dict:
        """Full status of the context tracker.

        Returns:
            {dirty: [...], clean: [...], checkpoint: int, total_read: int, ...}
        """
        return self._context.get_stats()

    def _handle_context_reset(self, req: dict) -> dict:
        """Clear all checkpoints and dirty state.

        Returns:
            {reset: True, checkpoint: 0}
        """
        return self._context.reset()

    def _handle_context_compacted(self, req: dict) -> dict:
        """Record that a harness compacted its transcript (see __init__).

        Bumps the epoch; every bridge that reads it afterwards drops its
        session-scoped stub/diff state and re-sends full content once. Called
        by a harness PreCompact hook (`toolrecall context compacted`) or by
        the MCP tool `context_compacted`. Idempotent per call: N compactions
        are N new epochs, which is exactly what the bridges want.

        Returns:
            {compacted: True, compaction_epoch: N}
        """
        with self._compaction_lock:
            self._compaction_epoch += 1
            epoch = self._compaction_epoch
        return {"compacted": True, "compaction_epoch": epoch}


# ─── Entry Points ─────────────────────────────────────────

_server_instance = None
# Keeps the flock'd single-instance lock file object referenced for the
# daemon's lifetime. If this went out of scope / got GC'd, the lock would
# be silently released and a duplicate daemon could start. On POSIX the
# fd is inherited across fork() by the daemonized child (which is the one
# that keeps running), so the child holds it for the whole process life.
_instance_lock_fh = None


def _release_instance_lock() -> None:
    """Unlink this daemon's instance-lock file and close the fd.

    flock is released by the OS on any exit (clean or crash), but the lock
    FILE is left behind — keyed per-socket-path, so e2e test daemons on temp
    sockets each accumulate one orphaned file in ~/.toolrecall. Graceful
    shutdown (SIGTERM/SIGINT) unlinks its own file; crash leftovers are
    harmless (the flock is gone) and cleaned by `rm ~/.toolrecall/daemon-*.lck`.
    """
    global _instance_lock_fh
    if _instance_lock_fh is None:
        return
    log = logging.getLogger(__name__)
    try:
        os.unlink(_instance_lock_fh.name)
    except OSError as e:
        # Already gone / raced with a cleanup — expected during shutdown, not fatal.
        log.debug("instance-lock unlink skipped: %s", e)
    try:
        _instance_lock_fh.close()
    except OSError as e:
        log.debug("instance-lock close failed: %s", e)
    _instance_lock_fh = None


def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    if _server_instance:
        _server_instance.stop()
    _release_instance_lock()
    sys.exit(0)


def _warn_on_env_path_overrides() -> None:
    """Print a warning when TOOLRECALL_* path overrides point at missing files.

    Catches the escaped-daemon failure mode: a daemon launched from a test
    (or any foreign context) inherits env overrides like
    TOOLRECALL_KNOWLEDGE_DB pointing at a temp file that no longer exists.
    The daemon then starts "successfully" but every knowledge-tier call
    reports "No knowledge database found". Visible at startup beats silent.
    """
    for var, label in (
        ("TOOLRECALL_CACHE_DB", "cache DB"),
        ("TOOLRECALL_KNOWLEDGE_DB", "knowledge DB"),
        ("TOOLRECALL_CONFIG", "config"),
    ):
        val = os.environ.get(var)
        if val and not os.path.exists(os.path.expanduser(val)):
            print(
                f"[toolrecall] WARNING: {var}={val} is set but the file does "
                f"not exist — the {label} will be unavailable. If this daemon "
                f"was auto-started from a test or another project's "
                f"environment, restart it from a clean shell or systemd.",
                file=sys.stderr,
                flush=True,
            )


def _sweep_stale_lock_files() -> int:
    """Unlink orphaned daemon-*.lck files from dead holders (startup sweep).

    ``_acquire_instance_lock`` keys the file on the socket path and the OS
    releases the flock when the holder dies — but SIGKILL/crash (and e2e
    kill-path teardown) leaves the FILE behind forever. Healthcheck then
    warns on exactly those orphans. The holder PID is written inside each
    file after a successful flock, so at startup we can safely unlink any
    lock whose recorded PID is no longer alive: a live holder's file is
    never touched, and a file whose flock we can still take is by
    definition unheld (belt-and-braces: flock it non-blocking first).

    Returns the number of files removed. Runs BEFORE our own lock is
    acquired, so a failure here never blocks daemon start.

    Safety rule (fixed 2026-09-08): a lock file is only removed when its
    flock can actually be taken non-blocking. The recorded-PID check alone
    is insufficient — e.g. the holder's /proc entry can be unreadable in
    other mount namespaces (containers), or the file can have been
    recreated after a manual `rm daemon-*.lck` while the true holder still
    flocks the original (now orphaned) inode. A file we can flock is by
    definition unheld; a file we cannot flock has a live owner and MUST be
    left alone, regardless of what its recorded PID says.
    """
    import fcntl as _fcntl
    import glob as _glob

    lock_dir = os.path.dirname(PID_FILE)
    log = logging.getLogger(__name__)
    removed = 0
    for path in _glob.glob(os.path.join(lock_dir, "daemon-*.lck")):
        # Belt-and-braces: try to flock the file non-blocking first. If the
        # flock succeeds the file is unheld — safe to remove. If it raises,
        # a live process holds it: never touch the file, whatever the
        # recorded PID says (PID reuse and cross-namespace reads lie; the
        # kernel lock cannot).
        try:
            fh = open(path, "a+")
        except OSError as e:
            log.debug("lock-file sweep skipped %s: %s", path, e)
            continue
        try:
            try:
                _fcntl.flock(fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError:
                continue  # held by a live process — leave it alone
            try:
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                pass  # raced with another sweeper — that's the success case
            except OSError as e:
                log.debug("lock-file sweep skipped %s: %s", path, e)
        finally:
            fh.close()
    return removed


def run_daemon(socket_path: str | None = None, foreground: bool = False):
    """Start the ToolRecall daemon."""
    global _server_instance, _instance_lock_fh

    # Strip local source tree from sys.path — ensures we import from
    # site-packages (pip-installed) when running inside a venv whose
    # cwd/pyvenv.cfg leaks the project dir ahead of site-packages.
    # Uses __file__ to find the package dir dynamically (not hardcoded).
    import sys as _sys

    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    _sys.path = [p for p in _sys.path if _pkg_dir not in p]

    # Surface inherited-but-dead env overrides early (escaped-daemon guard)
    _warn_on_env_path_overrides()

    if not socket_path:
        socket_path = _default_socket_path()

    # ── Stale lock-file sweep ─────────────────────────────────
    # SIGKILL/crash orphans daemon-*.lck files keyed on dead sockets; the
    # flock is gone but the file accumulates. Sweep (best-effort, never
    # fatal) BEFORE acquiring our own lock.
    try:
        _swept = _sweep_stale_lock_files()
        if _swept:
            _sweep_log = logging.getLogger(__name__)
            _sweep_log.info("startup sweep removed %d stale lock file(s)", _swept)
    except OSError as e:
        logging.getLogger(__name__).warning("startup lock-file sweep failed (continuing): %s", e)

    # ── Single-instance guard (atomic, flock-based) ──────────
    # flock is an atomic OS lock that (a) serializes two racing start
    # attempts so only one wins, and (b) is auto-released when the holder
    # dies — no stale-lock trap. Without this, repeated `daemon` calls can
    # stack orphaned instances: each rebinds the socket path while older
    # processes keep running on fd 6. Keyed on the socket path, so e2e
    # tests binding a distinct temp socket get their own independent lock.
    _instance_lock_fh = _acquire_instance_lock(socket_path)
    if _instance_lock_fh is None:
        lock_path = _instance_lock_path(socket_path)
        try:
            with open(lock_path) as _lf:
                holder = _lf.read().strip()
        except OSError:
            holder = "?"
        print(
            f"ToolRecall Daemon already running (lock held by PID {holder}) — refusing duplicate."
        )
        sys.exit(0)

    # ── Live-socket ownership check ──────────────────────────
    # The flock guard is inode-based: if a previous holder's lock FILE was
    # deleted while it ran (manual `rm daemon-*.lck`, runbook line), the old
    # daemon keeps listening on the live socket while we successfully flock a
    # freshly created lock file. bind_socket() below would then UNLINK the
    # live socket and steal the path, leaving TWO serving daemons — the exact
    # 3-proc leak seen 2026-09-08 (pipx copy on orphaned socket inode).
    # Defense: if the socket path already has a reachable live listener, do
    # NOT rebind. Exit cleanly so a systemd `Restart=always` unit settles
    # instead of crash-looping: whichever copy starts first owns the socket,
    # later copies attach-detect and stand down (self-heal by converging).
    if not IS_WINDOWS and os.path.exists(socket_path):
        from toolrecall.client import _probe_socket_alive

        if _probe_socket_alive(socket_path):
            print(
                f"ToolRecall Daemon already serving on {socket_path} (live listener detected) "
                "— standing down instead of stealing the socket."
            )
            sys.exit(0)

    if os.path.exists(socket_path) and not IS_WINDOWS:
        try:
            os.unlink(socket_path)
        except OSError:
            pass

    # Disable file-read shim to prevent infinite recursion:
    # _shim_open -> client.cached_read -> daemon -> cached_read -> open -> _shim_open -> ...
    try:
        from toolrecall.shim import remove as _shim_remove

        _shim_remove()
    except ImportError:
        pass

    # Enable faulthandler so segfaults/aborts produce tracebacks
    import faulthandler

    faulthandler.enable()

    # Structured startup banner — replaces unstructured print()s; same info
    # (version, socket, PID, policy) now lands in the rotating log as well.
    from toolrecall.logging_setup import setup_logging as _setup_logging

    _setup_logging()
    _banner_log = logging.getLogger("toolrecall.daemon")
    _banner_log.info(
        "daemon starting v%s socket=%s pid=%d foreground=%s",
        __version__,
        socket_path,
        os.getpid(),
        foreground,
    )

    # Register signal handlers (POSIX only)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

    _server_instance = DaemonServer(socket_path)

    if not foreground and not IS_WINDOWS:
        # Daemonize: fork, exit parent (POSIX only)
        pid = os.fork()
        if pid > 0:
            print(f"ToolRecall Daemon started (PID: {pid})")
            print(f"  Socket: {_server_instance.socket_path}")
            # ⚠ MUST be os._exit(), NOT sys.exit(). The SQLite connection was
            # opened at import time (toolrecall.cache module init), so parent
            # and child share the same WAL-mode connection across the fork.
            # sys.exit(0) runs interpreter teardown in the parent →
            # sqlite3.close() on the shared connection → the -wal/-shm
            # sidecars get unlinked while the child is still using them.
            # Every write then lands in orphaned inodes and is lost on daemon
            # exit (durability bug). os._exit skips teardown entirely; the OS
            # releases the parent's fds and flock automatically. Flush stdout
            # first — os._exit won't flush buffered stdio.
            sys.stdout.flush()
            os._exit(0)

        # Child process: Redirect standard streams, write PID file
        log_file = os.path.expanduser("~/.toolrecall/daemon.log")
        sys.stdout = open(log_file, "a")
        sys.stderr = sys.stdout
        if _should_manage_pid_file(socket_path):
            os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
            with open(PID_FILE, "w") as f:
                f.write(str(os.getpid()))
        else:
            _banner_log.info(
                "hermetic socket %s — not writing production PID file %s",
                socket_path,
                PID_FILE,
            )
    elif not foreground and IS_WINDOWS:
        # Windows: use multiprocessing instead of fork
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        p = ctx.Process(target=_server_instance.start)
        p.start()
        print(f"ToolRecall Daemon started (PID: {p.pid})")
        print(f"  Transport: {_server_instance.socket_path}")
        sys.exit(0)
    else:
        # Foreground mode (incl. systemd-managed, which runs --foreground):
        # write the PID file so `daemon --status`/`daemon --stop` reflect the
        # live daemon instead of a stale value from a previous background run.
        # Hermetic (tempdir) sockets are excluded — see _should_manage_pid_file.
        if not IS_WINDOWS:
            try:
                if _should_manage_pid_file(socket_path):
                    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
                    with open(PID_FILE, "w") as f:
                        f.write(str(os.getpid()))
                else:
                    _banner_log.info(
                        "hermetic socket %s — not writing production PID file %s",
                        socket_path,
                        PID_FILE,
                    )
            except OSError:
                pass  # PID file is best-effort; socket ping is authoritative

    try:
        _server_instance.start()
    except SystemExit:
        raise  # Let SystemExit (incl. SIGTERM handler's sys.exit(0)) propagate cleanly
    except BaseException:
        # If start() crashes (e.g. socket bind failure, executor init failure),
        # print a traceback BEFORE exiting so logs show the root cause.
        import traceback

        traceback.print_exc()
        sys.exit(1)
    # start() returned normally (server shut down without a signal) — still
    # unlink the lock file so no orphan is left behind.
    _release_instance_lock()


def stop_daemon():
    """Stop the daemon via systemd, or by PID file on Windows."""
    import subprocess as _sp

    # Try systemd first (Linux)
    if not IS_WINDOWS:
        result = _sp.run(
            ["systemctl", "--user", "stop", "toolrecall-daemon"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            print("ToolRecall Daemon stopped (via systemd).")
            return
        # Fall through to PID file if systemd not available
    # PID file fallback (Windows / no-systemd)
    if not os.path.exists(PID_FILE):
        print("ToolRecall Daemon is not running.")
        return
    with open(PID_FILE) as f:
        pid = int(f.read().strip())
    try:
        if IS_WINDOWS:
            _sp.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            print(f"Sent kill to Daemon (PID {pid})")
        else:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to Daemon (PID {pid})")
    except ProcessLookupError:
        print(f"Daemon (PID {pid}) not running. Cleaning up PID file.")
    finally:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        socket_path = _default_socket_path()
        if os.path.exists(socket_path) and not IS_WINDOWS:
            try:
                os.unlink(socket_path)
            except Exception:
                pass


def daemon_status():
    """Print daemon status via socket, then systemd, then PID file."""
    import subprocess as _sp

    # Socket is the source of truth: if the daemon answers a ping, it's
    # running regardless of what systemd or the PID file claims. This avoids
    # the false "DEAD (Stale PID file)" report when systemd is unreachable
    # from this shell (no user bus) but the daemon is actually alive.
    # NOTE: TransportClient.send() never raises — it returns
    # {"error": "daemon_unavailable"} (or an error string) on failure, so
    # no except-handler is needed here; the pong check below covers it.
    client = TransportClient(_default_socket_path())
    resp = client.send({"cmd": "ping"})
    if resp.get("pong"):
        pid = resp.get("pid", "?")
        print(f"ToolRecall Daemon: RUNNING (PID {pid})")
        print(f"  Transport: {_default_socket_path()}")
        print(f"  Path allowlist: {resp.get('allowed_paths', [])}")
        print(f"  Terminal enabled: {resp.get('allow_terminal', False)}")
        print(f"  MCP Multiplex: {'ENABLED' if resp.get('multiplex_enabled') else 'DISABLED'}")
        servers = resp.get("multiplex_servers", [])
        if servers:
            names = [s["name"] if isinstance(s, dict) else s for s in servers]
            print(f"  MCP Servers: {', '.join(names)}")
        ctx = resp.get("context_tracker", {})
        if ctx:
            print(
                f"  Context Tracker: checkpoint={ctx.get('checkpoint')}, dirty={ctx.get('dirty')}, clean={ctx.get('clean')}, total_read={ctx.get('total_read')}, ctx_dropped={ctx.get('ctx_dropped_tokens', 0)}"
            )
        return

    # Try systemd first (Linux)
    if not IS_WINDOWS:
        result = _sp.run(
            ["systemctl", "--user", "is-active", "toolrecall-daemon"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        active = result.stdout.strip()
        if active == "active":
            try:
                client = TransportClient(_default_socket_path())
                resp = client.send({"cmd": "ping"})
                pid = resp.get("pid", "?")
                print(f"ToolRecall Daemon: RUNNING (PID {pid})")
                print(f"  Transport: {_default_socket_path()}")
                print(f"  Path allowlist: {resp.get('allowed_paths', [])}")
                print(f"  Terminal enabled: {resp.get('allow_terminal', False)}")
                print(
                    f"  MCP Multiplex: {'ENABLED' if resp.get('multiplex_enabled') else 'DISABLED'}"
                )
                servers = resp.get("multiplex_servers", [])
                if servers:
                    names = [s["name"] if isinstance(s, dict) else s for s in servers]
                    print(f"  MCP Servers: {', '.join(names)}")
                ctx = resp.get("context_tracker", {})
                if ctx:
                    print(
                        f"  Context Tracker: checkpoint={ctx.get('checkpoint')}, dirty={ctx.get('dirty')}, clean={ctx.get('clean')}, total_read={ctx.get('total_read')}, ctx_dropped={ctx.get('ctx_dropped_tokens', 0)}"
                    )
                status = _sp.run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        "-P",
                        "ActiveEnterTimestamp",
                        "toolrecall-daemon",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if status.returncode == 0 and status.stdout.strip():
                    print(f"  Since: {status.stdout.strip()}")
            except Exception:
                print("ToolRecall Daemon: RUNNING (via systemctl)")
                print(f"  Transport: {_default_socket_path()}")
            return
        elif active == "inactive":
            print("ToolRecall Daemon: NOT RUNNING")
            return
        # fall through to PID file check
    # PID file fallback (Windows / no-systemd)
    if not os.path.exists(PID_FILE):
        print("ToolRecall Daemon: NOT RUNNING")
        return
    with open(PID_FILE) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 0)
        print(f"ToolRecall Daemon: RUNNING (PID {pid})")
        print(f"  Transport: {_default_socket_path()}")
    except ProcessLookupError:
        print("ToolRecall Daemon: DEAD (Stale PID file)")
        print("  The PID file is out of date. Clean it up with:")
        print("    toolrecall healthcheck --clean-stale")
        print("  or restart the daemon: systemctl --user restart toolrecall-daemon")
