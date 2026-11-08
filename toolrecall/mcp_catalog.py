"""MCP gateway: expose multiplexed servers' tools as first-class bridge tools.

Why this exists
---------------
The bridge can only reach multiplexed servers through the generic ``mcp_call``
tool. Measured 2026-09-26: that produced ZERO organic traffic — an agent must
first call ``mcp_list_servers``, learn the server/tool names, then take two
indirections, and no model does that spontaneously. The servers were, in
practice, unreachable (``hermes-docs/search_docs`` returned real hits when
called by hand and never once by an agent).

The gateway lists every configured server's tools under a
``mcp__<server>__<tool>`` name (the same namespacing Hermes uses) and routes
those calls through the existing daemon ``mcp_call`` path, so security scans,
the transparent cache and the cache-provenance marker apply unchanged.

Catalog semantics
-----------------
The catalog is DISCOVERY data only: names, descriptions, schemas — persisted so
``tools/list`` never has to spawn a server (a cold ``uv run`` costs seconds and
must not sit in an MCP handshake). A *call* routes by name and needs no catalog
entry: a tool that is not listed yet simply is not advertised yet, it never
fails to run. That asymmetry is deliberate — it keeps discovery cheap and
makes a missing/stale catalog a non-event.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("toolrecall.mcp_catalog")

# Bridge-visible prefix for a multiplexed tool name. Chosen to match the
# `<vendor>__<server>__<tool>` shape MCP clients already display for their own
# connectors, and to be collision-free with ToolRecall's native tools
# (read_file, terminal, mcp_call, ...) — none of them start with `mcp__`.
PREFIX = "mcp__"


def tool_name(server: str, tool: str) -> str:
    """Bridge-visible name for a multiplexed tool."""
    return f"{PREFIX}{server}__{tool}"


def split_tool_name(name: str) -> tuple[str, str] | None:
    """Return ``(server, tool)`` for a gateway name, else ``None``.

    Splits on the FIRST ``__`` after the prefix, so a tool name may itself
    contain ``__`` (e.g. ``mcp__fetch__fetch_url__batch`` → ("fetch",
    "fetch_url__batch")). Server names come from config keys and are simple
    slugs, so they never contain ``__``.
    """
    if not name.startswith(PREFIX):
        return None
    rest = name[len(PREFIX) :]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


def catalog_path(cfg) -> str:
    """Where the persisted catalog lives (next to the cache DB)."""
    db = os.path.expanduser(cfg.cache_db)
    return os.path.join(os.path.dirname(db), "mcp_tools_catalog.json")


def load(path: str) -> dict:
    """Read the persisted catalog. Never raises — discovery data, not truth.

    A missing file (first run) and a corrupt one are both logged at debug and
    treated as "nothing known yet": the catalog is rebuilt whenever a server
    starts, so the worst case is a tool being advertised one session later.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.debug("mcp catalog unreadable (%s): %s", path, e)
        return {}
    if not isinstance(data, dict):
        log.debug("mcp catalog has unexpected shape (%s): %s", path, type(data).__name__)
        return {}
    return data


def store(path: str, server: str, tools: list) -> None:
    """Persist one server's tool list, atomically.

    Atomic because the daemon rewrites this while bridge processes read it:
    a torn write would advertise a truncated tool set until the next refresh.
    """
    data = load(path)
    data[server] = {"tools": tools, "updated_at": time.time()}
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("mcp catalog write failed (%s): %s", path, e)


def defs(catalog: dict) -> list[dict]:
    """Build MCP tool definitions from the catalog.

    Descriptions are prefixed with the server name: with several servers in one
    flat tool list, "search docs" alone does not tell an agent WHICH corpus it
    searches.
    """
    out: list[dict] = []
    for server in sorted(catalog):
        entry = catalog.get(server) or {}
        for tool in entry.get("tools") or []:
            tname = tool.get("name")
            if not tname:
                continue
            desc = (tool.get("description") or "").strip()
            out.append(
                {
                    "name": tool_name(server, tname),
                    "description": f"[{server}] {desc}" if desc else f"[{server}] {tname}",
                    "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                }
            )
    return out
