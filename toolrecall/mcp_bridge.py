"""ToolRecall MCP Bridge — stdio ↔ UDS Protocol Adapter.

The MCP Bridge is a thin adapter: it reads MCP JSON-RPC from stdin,
translates the requests into UDS calls to the ToolRecall Daemon and writes
the responses back to stdout.

It has NO caching logic of its own, NO SQLite, NO In-Memory LRU.
Everything runs through the Daemon. This makes it:
  - Slim (~100 LOC instead of 540)
  - Fast to start (~5ms instead of 200ms)
  - Secure (Security lies in the Daemon, not in the bridge)

Usage:
    toolrecall mcp              # Start Bridge (requires Daemon)

Requires a running ToolRecall Daemon:
    toolrecall daemon &         # Start once
    toolrecall mcp              # Run bridge
"""

import hashlib
import json
import os
import sys
import time

from toolrecall.transport import TransportClient, DEFAULT_PATH
from toolrecall import __version__
from toolrecall.context_tracker import format_stale_block


# ─── MCP Tool Definitions ────────────────────────────────

TOOL_DEFINITIONS = [
    # ── File tools (primary names) ──
    {
        "name": "read_file",
        "description": "Read a file through ToolRecall's cache. "
        "Cached until file modification time (mtime) changes. "
        "Set bypass_cache=true to force a fresh read from disk.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "bypass_cache": {
                    "type": "boolean",
                    "description": "Skip cache and force fresh read from disk",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file, invalidates cache.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to write to"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "patch",
        "description": "Apply a find-and-replace patch, invalidates cache.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to patch"},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find (must be unique)",
                },
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "terminal",
        "description": "Run a terminal command with TTL-based caching. "
        "⚠ Requires mcp.allow_terminal=true in config.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command"},
                "ttl": {"type": "integer", "description": "Cache TTL in seconds (0=bypass)"},
            },
            "required": ["command"],
        },
    },
    # ── Cached variants (explicit aliases — not exposed in tools/list) ──
    # cached_read and cached_terminal are NOT in this list. They remain
    # usable via CMD_TO_MCP mapping — agents that discover them from
    # a prior session or instruction text can still call them.
    # ── Skill & docs ──
    {
        "name": "cached_skill",
        "description": "View an agent skill with caching.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name"}},
            "required": ["name"],
        },
    },
    {
        "name": "docs_search",
        "description": "Full-text search indexed documents (FTS5+BM25). "
        "Hide from tools/list when knowledge DB is empty.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "source": {"type": "string", "description": "Optional namespace filter"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "docs_get_page",
        "description": "Retrieve an indexed document page by source and path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Document source/namespace"},
                "path": {"type": "string", "description": "Document path"},
            },
            "required": ["source", "path"],
        },
    },
    # ── Cache admin ──
    {
        "name": "cache_status",
        "description": "Show cache statistics (hits, misses, tokens).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cache_invalidate",
        "description": "Clear all caches. ⚠ Requires mcp.allow_invalidate=true in config.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cache_refresh_file",
        "description": "Re-read a file from disk (bypasses cache). Safe.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path to refresh"}},
            "required": ["path"],
        },
    },
    # ── MCP multiplex ──
    {
        "name": "mcp_call",
        "description": "Call a tool on a multiplexed MCP server (github, time, fetch). "
        "⚠ Requires mcp_multiplex.enabled=true in config.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "MCP server name (e.g. 'github')"},
                "tool": {"type": "string", "description": "Tool name on that server"},
                "arguments": {"type": "object", "description": "Tool arguments dict"},
                "bypass_cache": {
                    "type": "boolean",
                    "description": "Skip cache and force fresh call",
                },
            },
            "required": ["server", "tool"],
        },
    },
    {
        "name": "mcp_list_servers",
        "description": "List available multiplexed MCP servers and their tools.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Context Tracker tools (hidden when emit_context_hints=false) ──
    {
        "name": "context_set_checkpoint",
        "description": "Mark current file state as a checkpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Optional label"}},
            "required": [],
        },
    },
    {
        "name": "context_get_dirty",
        "description": "Get files dirtied (written) vs clean (read-only) since a checkpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "checkpoint": {
                    "type": "integer",
                    "description": "Checkpoint ID to diff against (default: current)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "context_get_stale",
        "description": "Get files read then later overwritten — your copy is stale.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "context_get_stats",
        "description": "Full context tracker status: dirty/clean files, checkpoint ID.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "context_reset",
        "description": "Reset the context tracker. Call context_set_checkpoint after.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "recall_store",
        "description": "Persist a non-reproducible content block out-of-band and return a "
        "node_id to keep in context. Use for web/API/ephemeral output that cannot "
        "be re-fetched identically. Later recall_get(node_id) restores the raw bytes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fingerprint": {
                    "type": "string",
                    "description": "Stable key identifying this block (drives node_id dedup).",
                },
                "content": {
                    "type": "string",
                    "description": "Raw content to persist out-of-band.",
                },
                "content_type": {
                    "type": "string",
                    "description": "web|api|terminal|file|mcp|browser|other",
                },
                "reproducible": {
                    "type": "boolean",
                    "description": "False for non-reproducible content (the recall tier's target).",
                    "default": False,
                },
                "summary": {
                    "type": "string",
                    "description": "Optional short semantic pointer left with the entry.",
                    "default": "",
                },
            },
            "required": ["fingerprint", "content"],
        },
    },
    {
        "name": "recall_get",
        "description": "Restore a persisted content block by node_id (raw bytes + summary).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "node_id returned by recall_store.",
                }
            },
            "required": ["node_id"],
        },
    },
]

CMD_TO_MCP = {
    "read_file": "cached_read",
    "cached_read": "cached_read",
    "write_file": "cached_write",
    "cached_write": "cached_write",  # not a daemon cmd, special-cased below
    "patch": "cached_patch",
    "cached_patch": "cached_patch",  # special-cased below
    "terminal": "cached_terminal",
    "cached_terminal": "cached_terminal",
    "cached_skill": "cached_skill",
    "docs_search": "docs_search",
    "docs_get_page": "docs_get_page",
    "cache_status": "cache_status",
    "cache_invalidate": "cache_invalidate",
    "cache_refresh_file": "cache_refresh_file",
    "mcp_call": "mcp_call",
    "mcp_list_servers": "mcp_list_servers",
    "context_set_checkpoint": "context_set_checkpoint",
    "context_get_dirty": "context_get_dirty",
    "context_get_stale": "context_get_stale",
    "context_get_stats": "context_get_stats",
    "context_reset": "context_reset",
    "recall_store": "recall_store",
    "recall_get": "recall_get",
}


# ─── MCP Bridge ───────────────────────────────────────────


class MCPBridge:
    """Reads MCP JSON-RPC from stdin, proxies to daemon, writes to stdout."""

    def __init__(
        self,
        socket_path: str | None = None,
        emit_context_hints: bool = True,
        multiplexer_only: bool = False,
    ):
        self.client = TransportClient(socket_path or DEFAULT_PATH)
        self._start_time = time.time()
        # abs path -> sha256 of the content this session has already sent.
        # Per-process: the bridge's lifetime is the agent session, which is
        # exactly the scope we want.  The daemon cache stays shared.
        self._session_reads: dict[str, str] = {}
        # Timestamp of last full send per path — for proactive TTL expiry.
        # Harnesses compact old tool results; after N seconds the agent may
        # no longer have the content even if the hash matches.
        self._last_full_send: dict[str, float] = {}
        # Consecutive stub counter per path — prevents compaction blindness.
        # Claude Code summarizes away old tool results; after 2 consecutive
        # stubs we re-send full content so the agent can recover without
        # guessing bypass_cache=true.
        self._consecutive_stubs: dict[str, int] = {}
        # Terminal output dedup: command string -> sha256 of output.
        # Native Bash tool does NOT deduplicate; repeated identical output
        # (git status, test runs, ls) re-enters context at full cost.
        self._session_terminal: dict[str, str] = {}
        # Context hints (drop-clean-files) are only useful in harnesses that
        # own their message array (Hermes, ADK).  Append-only harnesses
        # (Claude Code, Cursor) cannot act on them — disable by default.
        self._emit_context_hints = emit_context_hints
        # Multiplexer-only mode: expose only mcp_call/mcp_list_servers,
        # no file/terminal/cache tools. For agents with built-in context
        # management (Claude Code, Cursor) where file caching costs 2.4× more.
        self._multiplexer_only = multiplexer_only
        if multiplexer_only:
            self._emit_context_hints = False  # context tracker is useless without file tools

    def _maybe_stub(self, path: str, resp: dict) -> dict:
        """Replace content with a stub when this session already holds it.

        Harnesses with append-only transcripts (Claude Code, Cursor, Cline)
        cannot drop an earlier tool_result, so re-sending identical content
        costs full tokens every time.  Their native read tools deduplicate;
        without this we are strictly more expensive than the tool we replace.
        """
        if not path or "error" in resp:
            return resp
        body = resp.get("result", resp)
        if not isinstance(body, dict):
            return resp
        content = body.get("content")
        if not isinstance(content, str):
            return resp

        key = os.path.realpath(os.path.expanduser(path))
        digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()

        if self._session_reads.get(key) == digest:
            # Proactive TTL expiry: if the last full send was more than
            # STUB_TTL seconds ago, the harness may have compacted the
            # earlier result. Re-send rather than risk a stale stub.
            STUB_TTL = 60  # seconds — conservative per Claude Code's pacing
            last_send = self._last_full_send.get(key, self._start_time)
            if time.time() - last_send > STUB_TTL:
                self._session_reads[key] = digest  # refresh, don't re-arm
                self._last_full_send[key] = time.time()
                return resp
            # Cap consecutive stubs — Claude Code compacts old tool results,
            # so the agent may have lost the earlier content. After 2 stubs
            # we re-send the full content to avoid blind recovery.
            self._consecutive_stubs[key] = self._consecutive_stubs.get(key, 0) + 1
            if self._consecutive_stubs[key] >= 3:
                self._consecutive_stubs[key] = 0  # keep digest, don't re-arm
                return resp
            note = (
                "File unchanged; see earlier read. "
                "If you no longer have it, pass bypass_cache=true."
            )
            return {
                "result": {
                    "unchanged": True,
                    "note": note,
                }
            }

        self._session_reads[key] = digest
        self._last_full_send[key] = time.time()
        return resp

    def _maybe_stub_terminal(self, command: str, resp: dict) -> dict:
        """Replace terminal output with a stub when identical to last run.

        Native Bash tool does NOT deduplicate command output.  Repeated
        runs of idempotent commands (git status, which, pip list) re-send
        identical output at full token cost.  This catches them.
        """
        if not command or "error" in resp:
            return resp
        content = (
            resp.get("result", resp).get("output")
            if isinstance(resp.get("result", resp), dict)
            else None
        )
        if not isinstance(content, str):
            return resp
        digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        if self._session_terminal.get(command) == digest:
            return {
                "result": {
                    "unchanged": True,
                    "note": f'Command "{command}" returned identical output; see earlier result.',
                }
            }
        self._session_terminal[command] = digest
        return resp

    def _uds_request(self, cmd: str, **kwargs) -> dict:
        """Send a request to the daemon and return parsed response."""
        payload = {"cmd": cmd, **kwargs}
        return self.client.send(payload)

    def _format_result(self, result) -> str:
        """Format a result for MCP text content."""
        if isinstance(result, dict):
            return json.dumps(result, indent=2, ensure_ascii=False)
        return str(result)

    def handle_request(self, req: dict) -> dict | None:
        """Handle one JSON-RPC request."""
        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            return self._handle_initialize(req_id)
        elif method == "tools/list":
            return self._handle_tools_list(req_id)
        elif method == "tools/call":
            return self._handle_tool_call(req_id, params)
        elif method == "notifications/initialized":
            return None
        elif method == "close":
            return None
        else:
            return self._error(req_id, -32601, f"Method not found: {method}")

    def _handle_initialize(self, req_id):
        # Ping daemon to get security info
        info = self._uds_request("ping")
        security = {
            "allowed_paths": info.get("allowed_paths", []),
            "allow_terminal": info.get("allow_terminal", False),
            "allow_invalidate": info.get("allow_invalidate", False),
        }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "ToolRecall (Bridge)",
                    "version": __version__,
                    "security": security,
                },
                "instructions": (
                    f"ToolRecall — Tool-Output Cache for LLM Agents (MCP Bridge).\n\n"
                    f"This bridge connects to the ToolRecall daemon. "
                    f"All file read/write tools are transparently cached.\n"
                    f"  read_file: path-allowlisted (bypass_cache=true for fresh read)\n"
                    f"  write_file: write content, invalidates cache\n"
                    f"  patch: find-and-replace, invalidates cache\n"
                    f"  cache_refresh_file: re-read a file from disk (bypasses cache)\n"
                    f"  cache_status: view cache statistics\n"
                    f"  terminal: {'ENABLED' if security['allow_terminal'] else 'DISABLED'}\n"
                    f"  cache_invalidate: {'ENABLED' if security['allow_invalidate'] else 'DISABLED'}\n"
                    + (
                        "  context_set_checkpoint / context_get_dirty / context_get_stats / context_reset:\n"
                        "    Context Tracker — bound your context window.\n"
                        "    Pattern: context_set_checkpoint → read files → work → "
                        "context_get_dirty → drop 'clean' files from context → "
                        "context_set_checkpoint again.\n"
                        if self._emit_context_hints
                        else ""
                    )
                    + "\nStart daemon: toolrecall daemon &"
                ),
            },
        }

    def _handle_tools_list(self, req_id):
        # Ask daemon which tools are actually available (gates)
        info = self._uds_request("ping")
        allow_terminal = info.get("allow_terminal", False)
        allow_invalidate = info.get("allow_invalidate", False)
        multiplex_enabled = info.get("multiplex_enabled", False)
        recall_enabled = info.get("recall_enabled", False)

        tools = []
        for tdef in TOOL_DEFINITIONS:
            name = tdef["name"]
            if name == "terminal" and not allow_terminal:
                continue
            if name == "cache_invalidate" and not allow_invalidate:
                continue
            if name in ("recall_store", "recall_get") and not recall_enabled:
                continue
            if name in ("mcp_call", "mcp_list_servers") and not multiplex_enabled:
                continue
            if self._multiplexer_only and name not in ("mcp_call", "mcp_list_servers"):
                continue
            tools.append(tdef)

        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    def _handle_tool_call(self, req_id, params):
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        uds_cmd = CMD_TO_MCP.get(tool_name)
        if not uds_cmd:
            return self._error(req_id, -32601, f"Unknown tool: {tool_name}")

        try:
            # mcp_call: server, tool, arguments (dict) are top-level params
            if tool_name == "mcp_call":
                server = arguments.get("server", "")
                tool = arguments.get("tool", "")
                tool_args = arguments.get("arguments", {})
                bypass = arguments.get("bypass_cache", False)
                payload = {
                    "cmd": "mcp_call",
                    "server": server,
                    "tool": tool,
                    "arguments": tool_args,
                }
                if bypass:
                    payload["ttl"] = 0
                resp = self.client.send(payload)
            elif tool_name == "mcp_list_servers":
                resp = self.client.send({"cmd": "mcp_list_servers"})
            else:
                # cached_read with bypass_cache → translate to refresh_file
                if tool_name in ("cached_read", "read_file") and arguments.get(
                    "bypass_cache", False
                ):
                    resp = self.client.send(
                        {
                            "cmd": "cache_refresh_file",
                            "path": arguments.get("path", ""),
                            "mcp_origin": True,
                        }
                    )
                elif tool_name == "write_file":
                    resp = self.client.send(
                        {
                            "cmd": "cached_write",
                            "path": arguments.get("path", ""),
                            "content": arguments.get("content", ""),
                            "mcp_origin": True,
                        }
                    )
                elif tool_name == "patch":
                    resp = self.client.send(
                        {
                            "cmd": "cached_patch",
                            "path": arguments.get("path", ""),
                            "old_string": arguments.get("old_string", ""),
                            "new_string": arguments.get("new_string", ""),
                            "mcp_origin": True,
                        }
                    )
                else:
                    # Mark agent-tool reads so context_tokens_saved counts.
                    # Only when context hints are enabled: if the agent isn't
                    # receiving drop instructions, it can't be saving context,
                    # so the counter would be misleading.
                    if tool_name in ("cached_read", "read_file"):
                        source = "agent_tool" if self._emit_context_hints else None
                        resp = self._uds_request(
                            uds_cmd, **arguments, source=source, mcp_origin=True
                        )
                    else:
                        resp = self._uds_request(uds_cmd, **arguments, mcp_origin=True)

            if "error" in resp:
                return self._error(req_id, -32603, resp["error"])

            # Session-scoped dedup: identical content → stub (saves tokens in
            # append-only harnesses like Claude Code where content can't be
            # dropped from the transcript after it enters)
            if tool_name in ("cached_read", "read_file") and not arguments.get(
                "bypass_cache", False
            ):
                resp = self._maybe_stub(arguments.get("path", ""), resp)
            # Terminal dedup: identical command output → stub (native Bash
            # tool does not deduplicate; this is a genuine win)
            if tool_name in ("cached_terminal", "terminal"):
                resp = self._maybe_stub_terminal(arguments.get("command", ""), resp)

            # Extract result for presentation
            content = resp.get("result", resp)
            result_text = self._format_result(content)

            # Auto-trigger context hint after every non-context tool call
            # Only emit if the harness owns its message array (config flag).
            if self._emit_context_hints and tool_name not in (
                "context_set_checkpoint",
                "context_get_dirty",
                "context_get_stale",
                "context_get_stats",
                "context_reset",
            ):
                try:
                    hint_resp = self.client.send({"cmd": "context_get_hint"})
                    hint = hint_resp.get("hint", "")
                    if hint:
                        result_text += "\n\n" + hint
                except Exception:
                    pass  # Graceful: hint is best-effort

                # Machine-parseable stale-file markers. Any agent loop can
                # regex these out without an explicit tool call, regardless
                # of provider. Paths are sanitized and capped by
                # format_stale_block — filenames are attacker-controlled.
                try:
                    stale_resp = self.client.send({"cmd": "context_get_stale"})
                    block = format_stale_block(stale_resp.get("paths", []))
                    if block:
                        result_text += "\n\n" + block
                except Exception:
                    pass  # Graceful: never break a tool call over a hint

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": result_text}]},
            }
        except Exception as e:
            return self._error(req_id, -32603, str(e))

    def _error(self, req_id, code, message):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ─── Entry Point ──────────────────────────────────────────


def main():
    """Start the MCP Bridge (stdio → Daemon ↔ UDS)."""

    # Parse --multiplexer-only from sys.argv (consumed here, not passed to daemon)
    multiplexer_only = "--multiplexer-only" in sys.argv
    if multiplexer_only:
        sys.argv = [a for a in sys.argv if a != "--multiplexer-only"]

    # First ping: get daemon capabilities before constructing bridge
    probe = MCPBridge()
    ping = probe._uds_request("ping")
    if ping.get("error") == "daemon_unavailable":
        print("❌ ToolRecall daemon is not running.", file=sys.stderr)
        print("   Run: toolrecall daemon &", file=sys.stderr)
        print("   Or:  toolrecall mcp --direct   (legacy standalone)", file=sys.stderr)
        sys.exit(1)

    emit_hints = ping.get("emit_context_hints", True)
    bridge = MCPBridge(emit_context_hints=emit_hints, multiplexer_only=multiplexer_only)

    print("ToolRecall MCP Bridge v0.2.0", file=sys.stderr)
    print("  Connected to daemon", file=sys.stderr)
    term = ping.get("allow_terminal", False)
    inv = ping.get("allow_invalidate", False)
    paths = ping.get("allowed_paths", [])
    daemon_hash = ping.get("config_hash", "")
    print(
        f"  cached_read path allowlist: {', '.join(paths) if paths else 'ALL (DANGEROUS)'}",
        file=sys.stderr,
    )
    print(f"  cached_terminal: {'ENABLED' if term else 'DISABLED'}", file=sys.stderr)
    print(f"  cache_invalidate: {'ENABLED' if inv else 'DISABLED'}", file=sys.stderr)
    print(f"  config: #{daemon_hash}", file=sys.stderr)

    # Check if daemon's config hash differs from last known (stale daemon).
    # Key by socket path so users alternating between two projects don't get
    # a false stale-daemon warning on every correct startup.
    _sock_key = str(DEFAULT_PATH)
    _cache_dir = os.environ.get(
        "XDG_CACHE_HOME", os.environ.get("LOCALAPPDATA", os.path.expanduser("~/.cache"))
    )
    _config_hash_store = os.path.join(
        _cache_dir,
        "toolrecall",
        f"config_hash_{hashlib.sha256(_sock_key.encode()).hexdigest()[:16]}",
    )
    _prev_hash = ""
    try:
        with open(_config_hash_store) as _f:
            _prev_hash = _f.read().strip()
    except (FileNotFoundError, OSError, IOError):
        pass
    if _prev_hash and _prev_hash != daemon_hash:
        print(
            f"  ⚠ Config changed since last connection (#{_prev_hash[:16]} → #{daemon_hash}).",
            file=sys.stderr,
        )
        print(
            "    The old daemon may have been stale. Run 'toolrecall daemon stop && toolrecall daemon' to confirm.",
            file=sys.stderr,
        )
    try:
        os.makedirs(os.path.dirname(_config_hash_store), exist_ok=True)
        with open(_config_hash_store, "w") as _f:
            _f.write(daemon_hash)
    except (OSError, IOError):
        pass

    # Warn about env-var mismatch: the bridge delegates to the daemon's config,
    # so TOOLRECALL_* env vars set at bridge launch time have no effect unless
    # the daemon was also started with them in its environment.
    import os as _os

    _ENV_MISMATCH_WARNINGS = []
    for _env_key in (
        "TOOLRECALL_MCP_ALLOWED_PATHS",
        "TOOLRECALL_MCP_ALLOW_TERMINAL",
        "TOOLRECALL_MCP_ALLOW_INVALIDATE",
        "TOOLRECALL_CACHE_DB",
        "TOOLRECALL_MCP_MULTIPLEX_ENABLED",
        "TOOLRECALL_STORAGE_BACKEND",
    ):
        _val = _os.environ.get(_env_key)
        if _val:
            _ENV_MISMATCH_WARNINGS.append(
                f"  ⚠ {_env_key}={_val[:60]} — set at bridge launch, but config is from the daemon.\n"
                f"    Restart the daemon with this env var set, or set it in your config.toml."
            )
    if _ENV_MISMATCH_WARNINGS:
        print("  ── Env-var / daemon config mismatch ──", file=sys.stderr)
        for _w in _ENV_MISMATCH_WARNINGS:
            print(_w, file=sys.stderr)
        print(file=sys.stderr)

    # Read JSON-RPC from stdin line by line
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = bridge.handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
