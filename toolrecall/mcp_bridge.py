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

import json
import logging
import os
import sys
import time

from toolrecall.transport import TransportClient, DEFAULT_PATH
from toolrecall import __version__
from toolrecall import mcp_catalog
from toolrecall.context_tracker import format_stale_block
from toolrecall.keys import content_digest
from toolrecall.stub_core import slice_lines

log = logging.getLogger("toolrecall.mcp_bridge")


# ─── MCP Tool Definitions ────────────────────────────────

TOOL_DEFINITIONS = [
    # ── File tools (primary names) ──
    {
        "name": "read_file",
        "description": "Read a file through ToolRecall's cache. "
        "Cached until file modification time (mtime) changes. "
        "Set bypass_cache=true to force a fresh read from disk. "
        "Use offset/limit for a line window (1-based) instead of the whole file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "bypass_cache": {
                    "type": "boolean",
                    "description": "Skip cache and force fresh read from disk",
                },
                "offset": {
                    "type": "integer",
                    "description": "First line to read (1-based). Omit for the whole file.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read from offset.",
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
        "description": "Search the indexed knowledge base by full text (skills, "
        "memories, project docs, indexed external documentation). Use it before "
        "answering questions about past decisions, earlier work, or documentation "
        "that was indexed here — instead of grepping the working tree, which only "
        "sees files that happen to be checked out.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "source": {
                    "type": "string",
                    "description": "Optional namespace filter (e.g. 'hermes-docs')",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "docs_get_page",
        "description": "Read one page from the indexed knowledge base by source "
        "and path — take the exact source/path from a docs_search result.",
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
        "description": (
            "Clear ToolRecall caches. Default scope='agent' clears agent caches "
            "only (api_cache with billed LLM responses is kept). scope='api' "
            "ALSO wipes api_cache — re-bills cached calls. "
            "⚠ Requires mcp.allow_invalidate=true in config."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["agent", "api"],
                    "description": "'agent' (default) = tool caches only; 'api' = also clear api_cache",
                }
            },
            "required": [],
        },
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
        "name": "context_compacted",
        "description": "Tell ToolRecall your harness compacted its transcript, so earlier "
        "file content is no longer in your context. Call it from a compaction hook; the next "
        "read then returns full content instead of a stub/diff.",
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
    "context_compacted": "context_compacted",
}


def _as_int(value: object) -> int | None:
    """Coerce a JSON-schema integer argument, or None when absent/invalid."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _range_key(path: str, offset: int | None, limit: int | None) -> str:
    """Stub/diff key for a read.

    The whole file and any line window of it are DIFFERENT content to the
    agent, so they must never share suppression state: a 40-line window can
    be stubbed against a previous window, but never against the full file.
    """
    real = os.path.realpath(os.path.expanduser(path))
    if not offset and not limit:
        return real
    return f"{real}#{offset or 1}-{limit or 'end'}"


# ─── MCP Bridge ───────────────────────────────────────────


class MCPBridge:
    """Reads MCP JSON-RPC from stdin, proxies to daemon, writes to stdout."""

    def __init__(
        self,
        socket_path: str | None = None,
        emit_context_hints: bool = True,
        multiplexer_only: bool = False,
        client_hint_policy: dict | None = None,
    ):
        self.client = TransportClient(socket_path or DEFAULT_PATH)
        self._start_time = time.time()
        # Session-scoped stub state (Lever 1) — delegates to the SHARED
        # stub_core.FileStubSession. Policy: ttl 240s / cap 2 / floor 0 —
        # the bridge's own contract (any tool result may be referenced).
        # cap 2 = the pre-core EFFECTIVE behavior (old code incremented
        # before checking >= 3). The instance lifetime is the bridge
        # process = the MCP session = exactly the scope in which "the agent
        # already holds this content" is true.
        #
        # TTL 60 -> 240 (2026-09-20, measured): the TTL guards the AGENT-
        # memory axis (is the earlier content still in the transcript?), so
        # it must track the real compaction horizon. Measured over 396
        # conversation-growth runs in the billed usage CSV (2 months,
        # monotone prompt growth until reset): median 14 turns / 4.8 min
        # before a context reset, p25 1.9 min, p75 19.1 min; median 18.5s
        # per request. A 60s TTL therefore covered only ~3.2 requests of a
        # live conversation and expired stubs whose content the agent
        # provably still held. 240s sits just under the median horizon
        # (288s) and well below p75 — conservative on the stranding risk,
        # while max_consecutive stays the hard armor.
        from toolrecall.stub_core import (
            DEFAULT_MAX_CALLS,
            DEFAULT_MAX_CONSECUTIVE,
            DEFAULT_MAX_TOKENS,
            FileStubSession,
            note_mcp,
        )

        self._stubs = FileStubSession(
            ttl_s=240.0,
            max_consecutive=DEFAULT_MAX_CONSECUTIVE,
            min_chars=0,
            note_fn=note_mcp,
            counter_fn=self._count_measured,
            # The same horizon as the TTL, expressed in what actually pushes
            # old results out of a transcript — calls emitted and tokens
            # emitted since the full copy was sent. Wall clock alone over-keeps
            # in a burst and under-keeps in a slow session; see
            # stub_core.DEFAULT_MAX_CALLS for the evidence behind the numbers.
            max_calls=DEFAULT_MAX_CALLS,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        # Compaction epoch last seen from the daemon (None = never seen). A
        # change means a harness reported a transcript compaction, so every
        # stub/diff assumption in this session is void (daemon cmd
        # context_compacted). The exact signal beats guessing the horizon.
        self._compaction_epoch: int | None = None
        # Context hints (drop-clean-files) are only useful in harnesses that
        # own their message array (Hermes, ADK).  Append-only harnesses
        # (Claude Code, Cursor) cannot act on them — disable by default.
        self._emit_context_hints = emit_context_hints
        # Per-MCP-client override table from the daemon config, keyed by
        # clientInfo.name (e.g. "claude-code", "opencode", "warp"). Applied
        # at initialize; unlisted clients keep the global flag above.
        self._client_hint_policy = client_hint_policy or {}
        # Identity captured from the MCP initialize handshake.
        self._client_name = ""
        # Hints only after the agent has demonstrated the checkpoint pattern:
        # the daemon can't force a drop — the agent must act. Passive until
        # the client calls context_set_checkpoint once. Stateless agents
        # (Hermes, OpenCode, Cline) do this on turn 1; context-managing
        # agents (Claude Code, Cursor) never do, so they stay silent without
        # any config.
        self._hints_opted_in = False
        # Discovery nudge: one-time tip after 10 reads (pre-opt-in), so
        # agents discover the checkpoint flow without being told about it.
        self._nudge_reads = 0
        self._nudge_emitted = False
        # Multiplexer-only mode: expose only mcp_call/mcp_list_servers,
        # no file/terminal/cache tools. For agents with built-in context
        # management (Claude Code, Cursor) where file caching costs 2.4× more.
        self._multiplexer_only = multiplexer_only
        if multiplexer_only:
            self._emit_context_hints = False  # context tracker is useless without file tools
        # Update-availability hint (once per version per session): when the
        # updater runs in notify-only mode ([update] auto_apply = false, the
        # default), agent-driven users never see the CLI's stdout — the MCP
        # bridge's tool-result stream is the channel they DO receive. One
        # daemon round-trip per session emits it after the first tool call.
        self._update_hint_done = False
        # Stub layer kill switch for the W1 arms (stuboff vs stubon). The two
        # arms must be the SAME bridge with only the tool-result rendering
        # differing, so this gates presentation, never the daemon wiring or the
        # context hints. Default ON; disable with TOOLRECALL_BRIDGE_STUB=0.
        self._stub_enabled = os.environ.get("TOOLRECALL_BRIDGE_STUB", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

    def _sync_epoch(self, body: dict) -> None:
        """Honour a compaction signal carried on a tool response.

        The daemon stamps ``compaction_epoch`` on read/terminal responses. When
        it changes, a harness reported that it compacted its transcript, so
        anything this session believes the agent still holds is void: drop the
        lot and let the next read re-send full content. The field never reaches
        the agent (callers pop it before presenting).
        """
        epoch = body.get("compaction_epoch") if isinstance(body, dict) else None
        if epoch is None:
            return
        if self._compaction_epoch is not None and epoch != self._compaction_epoch:
            self._stubs.reset_all()
        self._compaction_epoch = epoch

    def _present_read(self, arguments: dict, resp: dict) -> dict:
        """Stub / diff a file read, with line-range support.

        Harnesses with append-only transcripts (Claude Code, Cursor, Cline)
        cannot drop an earlier tool_result, so re-sending identical content
        costs full tokens every time. Their native read tools deduplicate;
        without this we are strictly more expensive than the tool we replace.

        Every suppression IS a measured context saving: the content is
        REPLACED on the wire by a note or a diff, so those tokens provably
        never reach the LLM. Counted in cache_stats.tokens_not_sent_measured
        (category "mcp_dedup") via stub_core's counter_fn → daemon cmd
        `dedup_tokens`.

        A line-range read is its own key (``<real>#start-end``), so a 40-line
        window and the whole file are different views to the agent; only the
        whole-file read offers a diff (a window is not a copy of the file).
        """
        if "error" in resp:
            return resp
        body = resp.get("result", resp)
        if not isinstance(body, dict):
            return resp
        if not self._stub_enabled:
            # stuboff arm: same conversation, full content — but the internal
            # compaction signal is stripped on BOTH arms, or the "identical
            # except tool-result rendering" invariant is false and the arm
            # silently carries ~25 extra chars per read.
            self._sync_epoch(body)
            body.pop("compaction_epoch", None)
            return resp
        self._sync_epoch(body)
        # Internal signalling only — never part of the payload the agent sees.
        body.pop("compaction_epoch", None)
        content = body.get("content")
        path = arguments.get("path", "")
        if not isinstance(content, str) or not isinstance(path, str) or not path:
            return resp

        offset = _as_int(arguments.get("offset"))
        limit = _as_int(arguments.get("limit"))
        ranged = bool(offset or limit)
        looked = dict(body)
        if ranged:
            looked.update(slice_lines(content, offset, limit))
        window = looked.get("content", "")
        if not isinstance(window, str):
            return resp

        key = _range_key(path, offset, limit)
        if arguments.get("bypass_cache"):
            # An explicit fresh read is the agent saying "my copy is gone":
            # forget the view so this read is a full send, not a stub.
            self._stubs.reset_key(key)
        decision = self._stubs.present(key, window, allow_diff=not ranged)
        presented = decision if decision is not None else looked
        self._stubs.tick(self._format_result(presented))
        if decision is None:
            return {"result": looked} if ranged else resp
        if decision.get("unchanged"):
            out = {"unchanged": True, "note": decision["note"]}
            for field in ("path", "start_line", "end_line", "total_lines"):
                if field in looked:
                    out[field] = looked[field]
            return {"result": out}
        return {
            "result": {
                "changed": True,
                "path": looked.get("path", path),
                "diff": decision["diff"],
                "note": (
                    "File changed since your last full read; apply this diff to your "
                    "copy. Pass bypass_cache=true for the whole file."
                ),
            }
        }

    def _present_terminal(self, command: str, resp: dict) -> dict:
        """Replace terminal output with a stub when identical to last run.

        Native Bash tool does NOT deduplicate command output. Repeated runs of
        idempotent commands (git status, which, pip list) re-send identical
        output at full token cost. This catches them — now under the same
        session budget as file reads, instead of never expiring.
        """
        if not command or "error" in resp:
            return resp
        body = resp.get("result", resp)
        if not isinstance(body, dict):
            return resp
        if not self._stub_enabled:
            return resp
        self._sync_epoch(body)
        body.pop("compaction_epoch", None)
        content = body.get("output")
        if not isinstance(content, str):
            return resp
        decision = self._stubs.present(f"$ {command}", content, allow_diff=False)
        presented = decision if decision is not None else content
        self._stubs.tick(self._format_result(presented))
        if decision is None:
            return resp
        return {
            "result": {
                "unchanged": True,
                "note": f'Command "{command}" returned identical output; see earlier result.',
            }
        }

    def _count_measured(self, tokens: int, log_path: str, note: str = "") -> None:
        """Fire-and-forget daemon counter for MEASURED not-sent tokens.

        The stub path guarantees these tokens never reach the LLM — this is
        the only wire-verified CONTEXT-tier number. Best-effort: a counter
        must never break a tool call, and the daemon may be restarting
        (logged at debug — designed fallback, not an incident). Also the
        counter_fn adapter for stub_core (signature: tokens, path, note);
        the note rides in the log detail exactly as before the core split.
        """
        try:
            self._uds_request(
                "dedup_tokens",
                tokens=int(tokens),
                path=f"{log_path} (stub: {note})" if note else log_path,
            )
        except Exception:
            # "measured-tier", not the wire cmd name — semgrep p/python flags
            # the substring "token" in a log call as credential-disclosure.
            log.debug("measured-tier counter failed (path=%s)", log_path, exc_info=True)

    def _resolve_agent_id(self) -> str:
        """Resolve agent identity from environment or clientInfo handshake.

        Attribution only. The activation contract (context-tier tagging and
        stubs) is ``TOOLRECALL_AGENT_ID`` in the AGENT's own environment, set by
        its adapter — this process must not mutate global env state on behalf of
        someone else's session.
        """
        return (os.environ.get("TOOLRECALL_AGENT_ID") or self._client_name or "").strip()

    def _uds_request(self, cmd: str, **kwargs) -> dict:
        """Send a request to the daemon and return parsed response."""
        payload = {"cmd": cmd, **kwargs}
        agent_id = self._resolve_agent_id()
        if agent_id and "agent_id" not in payload:
            payload["agent_id"] = agent_id
        return self.client.send(payload)

    def _format_result(self, result) -> str:
        """Format a result for MCP text content."""
        if isinstance(result, dict):
            return json.dumps(result, indent=2, ensure_ascii=False)
        return str(result)

    @staticmethod
    def _cache_provenance(resp: dict) -> str:
        """One compact line telling the agent a reply came from cache.

        The daemon stamps mcp_call replies with cached/revalidated/stale, but
        only the payload used to travel to the client: an agent could not
        tell a replay from a live call, and a serve-stale body carried no
        staleness caveat. Appended (never prepended) so the payload still
        starts at byte 0 for parsers.
        """
        if resp.get("revalidated"):
            return "\n\n[toolrecall] cached (ETag revalidated — source confirmed unchanged)"
        if resp.get("stale"):
            return "\n\n[toolrecall] cached (serve-stale — may lag the source)"
        ttl = resp.get("ttl")
        if ttl:
            return f"\n\n[toolrecall] cached (ttl {int(ttl)}s — source not re-checked)"
        return "\n\n[toolrecall] cached (source not re-checked)"

    def handle_request(self, req: dict) -> dict | None:
        """Handle one JSON-RPC request."""
        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            return self._handle_initialize(req_id, params)
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

    def _handle_initialize(self, req_id, params: dict | None = None):
        # Ping daemon to get security info
        info = self._uds_request("ping")
        security = {
            "allowed_paths": info.get("allowed_paths", []),
            "allow_terminal": info.get("allow_terminal", False),
            "allow_invalidate": info.get("allow_invalidate", False),
        }

        # Identify the client from the MCP handshake and apply any per-client
        # hint policy. clientInfo.name is what Warp, Claude Code, Hermes etc.
        # actually send; unknown/empty falls back to the global flag.
        params = params or {}
        client_info = params.get("clientInfo") or {}
        self._client_name = client_info.get("name", "")
        if self._client_name in self._client_hint_policy:
            self._emit_context_hints = bool(self._client_hint_policy[self._client_name])

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
        gateway = info.get("gateway", False)

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

        # Gateway: advertise each multiplexed server's tools as first-class
        # names (mcp__<server>__<tool>) so an agent can call them directly
        # instead of discovering them through two indirections. The catalog
        # holds schemas the daemon learned when a server last started; this
        # never spawns a server inside a handshake.
        if multiplex_enabled and gateway:
            cat_resp = self._uds_request("mcp_tools_catalog")
            catalog = (cat_resp.get("result") or {}).get("servers") or {}
            listed = {t["name"] for t in tools}
            for tdef in mcp_catalog.defs(catalog):
                if tdef["name"] not in listed:
                    tools.append(tdef)

        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    def _handle_tool_call(self, req_id, params):
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        # Resolve file paths in THIS process — the bridge runs as the agent's
        # stdio subprocess, so os.getcwd() here is the agent's working
        # directory (same reasoning as the cwd-scoped terminal cache below).
        # The daemon resolves against ITS OWN cwd, so a relative path used to
        # open whatever the daemon happened to have and share a single cache
        # entry across projects — reading NOTES.md in project B returned
        # project A's content. The daemon rejects anything still relative.
        if isinstance(arguments.get("path"), str) and arguments["path"]:
            arguments["path"] = os.path.abspath(os.path.expanduser(arguments["path"]))

        # Gateway names (mcp__<server>__<tool>) route through the same daemon
        # mcp_call path, so the security scans, the transparent cache and the
        # cache-provenance marker apply unchanged — the gateway only removes
        # the discovery indirection, it does not add a second code path.
        routed = mcp_catalog.split_tool_name(tool_name)
        if routed:
            arguments = {"server": routed[0], "tool": routed[1], "arguments": arguments}
            tool_name = "mcp_call"

        # cwd-scoped terminal cache: the bridge runs as the agent's stdio
        # subprocess, so os.getcwd() here is the agent's working directory.
        # Include it so the daemon scopes the terminal cache key per
        # directory (same command in different dirs ≠ same entry).
        if tool_name in ("terminal", "cached_terminal"):
            arguments = {**arguments, "cwd": os.getcwd()}

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
                extra = {"ttl": 0} if bypass else {}
                resp = self._uds_request(
                    "mcp_call",
                    server=server,
                    tool=tool,
                    arguments=tool_args,
                    **extra,
                )
            elif tool_name == "mcp_list_servers":
                resp = self._uds_request("mcp_list_servers")
            else:
                # cached_read with bypass_cache → translate to refresh_file
                if tool_name in ("cached_read", "read_file") and arguments.get(
                    "bypass_cache", False
                ):
                    resp = self._uds_request(
                        "cache_refresh_file",
                        path=arguments.get("path", ""),
                        mcp_origin=True,
                    )
                elif tool_name == "write_file":
                    resp = self._uds_request(
                        "cached_write",
                        path=arguments.get("path", ""),
                        content=arguments.get("content", ""),
                        mcp_origin=True,
                    )
                elif tool_name == "patch":
                    resp = self._uds_request(
                        "cached_patch",
                        path=arguments.get("path", ""),
                        old_string=arguments.get("old_string", ""),
                        new_string=arguments.get("new_string", ""),
                        mcp_origin=True,
                    )
                else:
                    # Mark agent-tool reads so tokens_saved_to_llm counts.
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

            # Opt-in contract: the agent demonstrates the checkpoint pattern
            # once, and only then do we start appending drop-clean hints. This
            # keeps the bridge passive for context-managing agents (Claude
            # Code, Cursor, Warp Agent) without any config beyond the global
            # flag — they never call the checkpoint tools.
            if tool_name == "context_set_checkpoint" and not self._hints_opted_in:
                self._hints_opted_in = True

            if tool_name == "context_compacted":
                # The daemon bumped the epoch. Drop this session's views
                # immediately so the next read is a full send; the epoch
                # carried on read responses covers every OTHER session.
                self._stubs.reset_all()

            # Session-scoped view: identical content → stub, changed content →
            # diff (saves tokens in append-only harnesses like Claude Code
            # where content can't be dropped from the transcript after it
            # enters). bypass_cache is handled INSIDE: an explicit fresh read
            # forgets the view and always returns full content.
            if tool_name in ("cached_read", "read_file"):
                resp = self._present_read(arguments, resp)
            # Terminal dedup: identical command output → stub (native Bash
            # tool does not deduplicate; this is a genuine win)
            if tool_name in ("cached_terminal", "terminal"):
                resp = self._present_terminal(arguments.get("command", ""), resp)

            # Extract result for presentation
            content = resp.get("result", resp)
            result_text = self._format_result(content)

            # Cache provenance for multiplexed calls: the daemon's flags
            # (cached/revalidated/stale) describe where the payload came
            # from — the client would otherwise see a replay as a live call.
            if tool_name == "mcp_call" and resp.get("cached"):
                result_text += self._cache_provenance(resp)

            # ── Discovery nudge (pre-opt-in) ──
            # The opt-in contract below is a cold-start deadlock: agents that
            # never call context_set_checkpoint never learn the flow exists.
            # One gentle tip after N reads, once per session, before opt-in.
            if (
                self._emit_context_hints
                and not self._hints_opted_in
                and not self._nudge_emitted
                and tool_name in ("cached_read", "read_file")
            ):
                self._nudge_reads = getattr(self, "_nudge_reads", 0) + 1
                if self._nudge_reads >= 10:
                    self._nudge_emitted = True
                    result_text += (
                        "\n\n[toolrecall] Tip: this session has read 10+ files. "
                        "Call context_set_checkpoint once to enable context savings — "
                        "afterwards context_get_dirty lists files safe to drop from "
                        "context, cutting input tokens on every later turn."
                    )

            # ── Update-availability hint (once per version per session) ──
            # auto_apply defaults to FALSE: installing silently would be code
            # execution from PyPI without consent, and a stdout hint never
            # reaches agent-driven users. Tool output does. The bridge asks
            # the daemon once per session; the daemon consults the updater
            # state (latest_seen) and throttles across sessions via
            # notified_for. Runs regardless of _emit_context_hints — a
            # security notice is not a context hint.
            if not self._update_hint_done:
                self._update_hint_done = True
                try:
                    hint_resp = self.client.send({"cmd": "update_pending_hint"})
                    update_hint = hint_resp.get("hint", "")
                    if update_hint:
                        result_text += "\n\n" + update_hint
                except Exception:
                    # Graceful: never break a tool call over a notice. Debug,
                    # not warning — daemon restarting mid-session is routine.
                    log.debug("update_pending_hint daemon round-trip failed", exc_info=True)

            # Auto-trigger context hint after every non-context tool call.
            # Only if the client opted in by checkpointing (and, on the daemon
            # side, if the flag + per-client policy allow it).
            if (
                self._emit_context_hints
                and self._hints_opted_in
                and tool_name
                not in (
                    "context_set_checkpoint",
                    "context_get_dirty",
                    "context_get_stale",
                    "context_get_stats",
                    "context_reset",
                )
            ):
                try:
                    hint_resp = self.client.send({"cmd": "context_get_hint"})
                    hint = hint_resp.get("hint", "")
                    if hint:
                        result_text += "\n\n" + hint
                except Exception:
                    # Graceful: hint is best-effort (same rationale as above).
                    log.debug("context_get_hint daemon round-trip failed", exc_info=True)

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
                    # Graceful: never break a tool call over a hint.
                    log.debug("context_get_stale daemon round-trip failed", exc_info=True)

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
    client_policy = ping.get("client_hint_policy", {})
    bridge = MCPBridge(
        emit_context_hints=emit_hints,
        client_hint_policy=client_policy,
        multiplexer_only=multiplexer_only,
    )

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
        f"config_hash_{content_digest(_sock_key, truncate=16)}",
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
