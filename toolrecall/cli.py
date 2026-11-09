"""ToolRecall CLI -- setup, daemon lifecycle, cache inspection, indexing, MCP, proxy.

Usage:
    toolrecall setup           # One-shot setup: config + service + shim + start
    toolrecall restart         # Health check + clean daemon restart
    toolrecall stop            # Stop daemon + revert proxy base-URL wiring
    toolrecall status          # Show cache status
    toolrecall stats           # Detailed statistics (JSON)
    toolrecall trend           # Daily hit-rate trend + worst miss classes
    toolrecall invalidate      # Clear cache
    toolrecall reset-stats     # Reset statistics counters
    toolrecall index           # Index knowledge base
    toolrecall index-memory    # Index agent memory stores
    toolrecall index-dir       # Index a directory into knowledge DB
    toolrecall tool-usage      # Which ToolRecall tools agents actually call
    toolrecall index-remote    # Sync configured remote doc sources (opt-in)
    toolrecall docs-rejects    # What remote syncs skipped or flagged
    toolrecall dedup-stats     # Which tools' outputs request-dedup stubbed
    toolrecall config-set      # Set a config value (section.key = value)
    toolrecall serve           # Start forward proxy (cache API responses)
    toolrecall debug           # Start debug/demo server (test cached_read via curl)
    toolrecall nginx           # Generate nginx config
    toolrecall mcp             # Start MCP Bridge (stdio → Daemon)
    toolrecall daemon          # Start/stop/manage cache daemon
    toolrecall shim            # Install/uninstall transparent cache shim (.pth)
    toolrecall init            # Create default config.toml and .env
"""

import os
import sys


def _stdin_is_interactive() -> bool:
    """True only when a prompt could actually get an answer.

    Non-tty stdin (CI, cron, a closed fd, pytest's capture) cannot answer a
    question, and it makes ``input()`` raise ``OSError`` where callers used to
    catch only ``EOFError`` — which crashed ``toolrecall setup`` on a scripted
    run instead of taking its documented non-interactive default. This is the
    gate for DECIDING whether to offer a prompt; prompt sites additionally
    treat an unreadable stdin as "no answer" (see ``_confirm_install``) so that
    a piped ``yes | toolrecall shim --install`` keeps working.
    """
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):  # detached or closed stdin
        return False


def cmd_init():
    """Create boilerplate config and .env for users with interactive setup."""
    import os
    import sys

    cfg_dir = os.path.expanduser("~/.config/toolrecall")
    os.makedirs(cfg_dir, exist_ok=True)

    cfg_path = os.path.join(cfg_dir, "toolrecall.toml")
    env_path = os.path.join(cfg_dir, ".env")

    # ─── Security banner ───────────────────────────────
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  ⚠️  ToolRecall Security Setup                          ║")
    print("║                                                          ║")
    print("║  ToolRecall uses DEFAULT-DENY path access control.       ║")
    print("║  The agent can ONLY read files under directories you     ║")
    print("║  explicitly allow below.                                ║")
    print("║                                                          ║")
    print("║  ⚠️  IMPORTANT — Consequences of allowing a path:        ║")
    print("║   • Every file under that path becomes readable          ║")
    print("║     through ToolRecall's MCP layer.                      ║")
    print("║   • If the agent is prompt-injected, files under         ║")
    print("║     allowed paths could be exfiltrated.                  ║")
    print("║   • Credential files (.env, .ssh/, .pem, .gitconfig)    ║")
    print("║     are still blocked inside allowed paths.              ║")
    print("║                                                          ║")
    print("║  Best practice: only add directories the agent needs.    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # ─── Interactive path collection (fallback to default if non-TTY) ─
    default_paths = ["/tmp", "~/.toolrecall"]
    paths = []

    if not sys.stdin.isatty():
        # Non-TTY (e.g. CI, Docker, pipe) — use defaults silently
        paths = list(default_paths)
        print("📄 Non-interactive shell detected — using default allowed paths.")
        print(f"   Allowed: {', '.join(default_paths)}")
        print("   Edit config.toml later to add more paths.")
        print()
    else:
        print("Enter the directories your agent should be able to read.")
        print("One path per line. Empty line when done.")
        print(f"Default (press Enter): {', '.join(default_paths)}")
        print()
        print("  ⚠️  Home directory (~/) is NOT in the default allowlist.")
        print("     Add only what the agent needs — keep everything else off-limits.")
        print()

        first = True
        while True:
            prompt = "Path 1: " if first else f"Path {len(paths) + 1}: "
            user_input = input(prompt).strip()
            first = False
            if not user_input:
                if not paths:
                    paths = list(default_paths)
                    print(f"  → Using defaults: {', '.join(default_paths)}")
                break
            expanded = os.path.expanduser(user_input)
            if not os.path.isdir(expanded):
                print(f"  ⚠️  Directory does not exist: {expanded}")
                yn = input("  Add anyway? [y/N] ").strip().lower()
                if yn != "y":
                    continue
            paths.append(user_input)

        print()

    # ─── Cache key normalization toggle ──────────────────
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  🔑 Cache Key Normalization (Track 1 — Semantic Intent) ║")
    print("║                                                          ║")
    print("║  Normalizes tool call arguments before cache key         ║")
    print("║  generation so that semantically identical calls         ║")
    print("║  produce the same cache key:                            ║")
    print("║                                                          ║")
    print("║  • Sorts JSON keys — {b:2, a:1} == {a:1, b:2}          ║")
    print('║  • Strips whitespace — "  /tmp  " == "/tmp"            ║')
    print("║  • Removes noise — timestamps, session IDs, trace IDs   ║")
    print('║  • Lowercases command names — "LS -la" == "ls -la"     ║')
    print("║                                                          ║")
    print("║  ⚠️  Changes existing cache keys — existing entries      ║")
    print("║     become orphans until they expire or are overwritten. ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    norm_enabled = False
    if sys.stdin.isatty():
        resp = input("Enable cache key normalization? [y/N] ").strip().lower()
        norm_enabled = resp == "y"
        if norm_enabled:
            print("  ✅ Normalization enabled — broader cache hits across rephrased queries.")
        else:
            print("  ℹ️  Normalization disabled (default) — can be enabled later via config.")
        print()
    else:
        print("  ℹ️  Non-interactive — normalization disabled (default).")
        print("     Set [norm].enabled = true in config.toml to enable.")
        print()

    norm_toml = f"""
[norm]
# Cache key normalization — deterministic JSON + noise stripping.
# When enabled, tool call arguments are normalized before cache key
# generation: keys sorted, whitespace stripped, non-semantic fields
# (timestamps, session IDs, request IDs, trace IDs, nonces) removed.
# ⚠️ Changing this invalidates existing cache entries.
enabled = {"true" if norm_enabled else "false"}
"""

    # ─── Build config content ──────────────────────────
    paths_toml = ",\n    ".join(f'"{p}"' for p in paths)

    cfg_content = f"""# ToolRecall Configuration
# Created by `toolrecall init`

[storage]
backend = "sqlite"

[cache]
file_ttl = -1          # read_file: until file modification
skill_ttl = -1         # skill_view: until skill update
terminal_default_ttl = 300

[security]
tool_access_control = false
dangerous_tool_keywords = []

[mcp]
# ⚠️ SECURITY: Default-deny file access control.
# The agent can ONLY read files under these directories.
# Add paths the agent needs — nothing more.
# Credential files (.env, .ssh/, .pem) are still blocked inside
# allowed paths as a secondary safety net.
allowed_paths = [
    {paths_toml}
]
allow_terminal = true
allow_invalidate = false

[mcp_multiplex]
enabled = true
default_ttl = 60
# Server names: auto-resolved via built-in registry.
# Built-in (stdlib, no deps): time, github, sequential-thinking
# External (needs uvx): fetch, filesystem, git, memory, brave-search, playwright, slack
servers = ["time", "sequential-thinking"]
idle_minutes = 15

# Custom server overrides (optional — auto-resolve is the default).
# Uncomment to override auto-resolved servers or add custom ones:
# [mcp_multiplex.servers_config]
# github = {{ command = "npx", args = ["-y", "@modelcontextprotocol/server-github"] }}
# fetch = {{ command = "uvx", args = ["mcp-server-fetch"] }}

[forward_proxy]
# ToolRecall Daemon starts the forward proxy on :8569 automatically.
# Set TOOLRECALL_FORWARD_PORT to change the default port.
# Point your API client at http://localhost:8569 to get cached API responses.
{norm_toml}"""

    env_content = """# ToolRecall Secrets
# Loaded safely by the Daemon. Do NOT commit this file.
# Example for GitHub MCP:
GITHUB_PERSONAL_ACCESS_TOKEN=""
"""

    created_cfg = False
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w") as f:
            f.write(cfg_content)
        created_cfg = True
    else:
        print("ℹ️  config.toml already exists — not overwriting.")
        print("   Run `toolrecall config-set mcp.allowed_paths [...]` to update paths.")
        print()

    created_env = False
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(env_content)
        os.chmod(env_path, 0o600)
        created_env = True

    print(f"ToolRecall directory: {cfg_dir}")
    if created_cfg:
        print("✅ Created config.toml")
        print(f"   Allowed paths: {', '.join(paths)}")
    if created_env:
        print("✅ Created .env (API keys — keep secret!)")

    print()
    print("Next steps:")
    print(f"  1. Edit {env_path} to add your API keys (if needed)")


def cmd_status():
    """Show cache status via daemon or directly (always includes recent activity table)."""
    from toolrecall.cache import get_stats

    stats = get_stats()
    print("=" * 50)
    print("  ToolRecall Cache Status")
    print("=" * 50)
    for k, v in stats.items():
        if k == "recent":
            continue
        if isinstance(v, dict):
            saved = v.get("tokens_served_from_cache", v.get("tokens_not_read_from_disk", 0))
            adjusted = v.get(
                "tokens_served_from_cache_adjusted",
                v.get("tokens_not_read_from_disk_adjusted", 0),
            )
            read = v.get("tokens_served_from_disk", v.get("tokens_read_from_disk", 0))
            context = v.get("tokens_not_sent_to_llm", v.get("tokens_saved_to_llm", 0))
            content_tokens = v.get("cached_content_tokens", 0)
            saved_str = f", tokens_served_from_cache={saved:,}" if saved else ""
            adjusted_str = f", adjusted={adjusted:,}" if adjusted and adjusted != saved else ""
            read_str = f", tokens_served_from_disk={read:,}" if read else ""
            # 2026-09-19 rename: this column is the CONTEXT-tier ESTIMATE
            # (per-HIT counter); the MEASURED not-sent tier lives in
            # tokens_not_sent_measured (mcp_dedup). The old human label
            # ("tokens_not_sent_to_llm=") collided with the measured tier's
            # name and read as billed-truth.
            context_str = f", context_estimate={context:,}" if context else ""
            content_str = f", cached_content_tokens={content_tokens:,}" if content_tokens else ""
            # A banked total means the GC reset this row at some point; without
            # showing the lifetime sum, housekeeping would look like "nothing
            # ever happened here" (which is exactly how a 2.02 B io total
            # disappeared unnoticed).
            banked = v.get("banked_io_tokens_saved") or 0
            lifetime_str = (
                f", lifetime_io_saved={v.get('lifetime_io_tokens_saved', 0):,}"
                f" (+{banked:,} banked, {v.get('bank_count', 0)}x)"
                if banked
                else ""
            )
            print(
                f"  {k}: {v['hits']} hits, {v['misses']} misses, "
                f"hit_rate={v['hit_rate']}{read_str}{saved_str}{adjusted_str}"
                f"{context_str}{content_str}{lifetime_str}"
            )
        else:
            print(f"  {k}: {v}")

    # ─── Per-Agent Summary ──────────────────
    try:
        from toolrecall.cache import get_agent_stats

        agent_rows = get_agent_stats()
        if agent_rows:
            print()
            print("  ── Active Agents ──")
            agents = sorted(set(r["agent_id"] for r in agent_rows))
            for aid in agents:
                rows = [r for r in agent_rows if r["agent_id"] == aid]
                a_hits = sum(r["hits"] for r in rows)
                a_misses = sum(r["misses"] for r in rows)
                a_saved = sum(r["tokens_saved"] for r in rows)
                total_a = a_hits + a_misses
                rate_str = f"{a_hits / total_a * 100:.0f}%" if total_a > 0 else "0%"
                saved_str = f", tokens_saved={a_saved:,}" if a_saved else ""
                print(
                    f"  agent {aid}: {a_hits} hits, {a_misses} misses ({rate_str} hit rate){saved_str}"
                )
    except Exception:
        pass

    # Recent activity
    recent = stats.get("recent", [])
    if recent:
        print()
        print("  ── Last 20 accesses ──")
        print(f"  {'ago':>8} {'type':<12} {'tokens':>8} {'cached_at':<26} {'path'}")
        print(f"  {'─' * 8} {'─' * 12} {'─' * 8} {'─' * 26} {'─' * 40}")
        for r in recent:
            icon = "✅" if r["hit"] else "⬇️"
            p = r.get("path", r["category"])
            if len(p) > 40:
                p = "..." + p[-37:]
            tokens_str = f"{r['tokens']:,}" if r.get("tokens", 0) else "-"
            ca = r.get("cached_at", "")[11:19]  # HH:MM:SS from ISO
            print(f"  {r['since_status']:>8} {icon} {tokens_str:>8} {ca:<26} {p}")

    # ─── Terminal cache info ─────────────────
    print()
    print("  Terminal cache (read-only commands):")
    print("    System: hostname(3600s), whoami(3600s), pwd(3600s), uptime(300s), uname(3600s)")
    print("    Files:  ls(60s), cat(30s), head(30s), tail(30s), wc(30s)")
    print("    Search: grep(60s), rg(60s), find(60s), fd(60s)")
    print("    Git:    status(30s), diff(30s), log(30s), branch(300s), stash(300s)")
    print("    Env:    which(3600s), python3 --version(3600s), pip list(600s)")
    print()
    print("  ⚠️  Only read-only commands are cached. Dangerous")
    print("     commands (rm, sudo, git push, kill) are NEVER cached.")
    print()


def _context_flow_snapshot() -> dict:
    """Live context-tracker state for reporters (armed flag + banked ledger).

    Fetched from the daemon over IPC so every reporter (CLI JSON, cron
    script, dashboards) sees the same state the TUI shows, including the
    ``armed`` flag that the bare ``context_estimate`` counter cannot
    convey. Fail-soft: any error degrades to ``{"available": False,
    "reason": ...}`` — a reporter must print WHY state is missing instead
    of implying a dead flow (2026-09-15 cron misread: an armed flow with
    593k banked tokens was reported as inactive because the armed flag
    was invisible in the stats JSON).

    Two tiers, both correct, never merge:
      - cache tier (``context_estimate`` / stats key
        ``tokens_not_sent_to_llm``): per agent-tool HIT. ESTIMATE tier —
        the measured not-sent tier is ``tokens_not_sent_measured``.
      - tracker ledger (``banked_tokens``): per file, once per checkpoint
        epoch (banked-once). Smaller by design.
    """
    out: dict = {
        "available": False,
        "reason": "",
        "armed": False,
        "checkpoint": 0,
        "banked_tokens": 0,
        "read_paths": 0,
        "clean": 0,
        "dirty": 0,
    }
    try:
        from toolrecall.transport import DEFAULT_PATH, TransportClient

        resp = TransportClient(DEFAULT_PATH).send({"cmd": "context_get_stats"}, timeout=5.0)
        if isinstance(resp, dict) and "agent_armed" in resp:
            out.update(
                {
                    "available": True,
                    "armed": bool(resp.get("agent_armed")),
                    "checkpoint": int(resp.get("checkpoint", 0)),
                    "banked_tokens": int(resp.get("ctx_dropped_tokens_total", 0)),
                    "read_paths": int(resp.get("total_read", 0)),
                    "clean": int(resp.get("total_clean", 0)),
                    "dirty": int(resp.get("total_dirty", 0)),
                }
            )
            return out
        out["reason"] = f"unexpected response: {str(resp)[:120]}"
    except Exception as e:  # noqa: BLE001 — reporter state is best-effort
        out["reason"] = str(e)[:120]
    return out


def cmd_stats():
    """Detailed statistics as JSON.

    Flags:
      --by-agent         Show per-agent breakdown from agent_cache_stats
      --agent-id <id>    Show stats for a specific agent only
    """
    if "--by-agent" in sys.argv or "--agent-id" in sys.argv:
        from toolrecall.cache import get_agent_stats

        agent_filter = None
        if "--agent-id" in sys.argv:
            idx = sys.argv.index("--agent-id")
            if idx + 1 < len(sys.argv):
                agent_filter = sys.argv[idx + 1]
        stats = get_agent_stats(agent_id=agent_filter)
        if not stats:
            print("No per-agent stats recorded. Set TOOLRECALL_AGENT_ID to enable tracking.")
            return
        print("Per-Agent Cache Stats")
        print("=" * 50)
        for row in stats:
            print(
                f"{row['agent_id']:<20} {row['category']:<20} "
                f"hits={row['hits']:<5} misses={row['misses']:<5} "
                f"tokens_saved={row['tokens_saved']}"
            )
        return

    from toolrecall.cache import get_stats
    import json as _json

    stats = get_stats()
    # Context-flow verdict for reporters: armed/checkpoint/banked state
    # from the live tracker. Absent this, a bare 0-vs-nonzero counter has
    # been misread as "flow inactive" by downstream summarizers.
    stats["context_flow"] = _context_flow_snapshot()
    print(_json.dumps(stats, indent=2, default=str))


_AGENT_COUNTERS = (
    "hits",
    "misses",
    "tokens_saved",
    "tokens_read_from_disk",
    "context_tokens_saved",
)


def _agent_totals(rows: list[dict]) -> dict:
    """Sum the per-category counters of agent_cache_stats rows."""
    return {k: sum(r.get(k) or 0 for r in rows) for k in _AGENT_COUNTERS}


def cmd_agent():
    """List known agents and show per-agent cache stats.

    Subcommands:
      ls [--json]          List all agents with recorded stats
      show <id> [--json]   Show stats for a specific agent
      prune <id>           Delete stats for a specific agent

    Examples:
      toolrecall agent ls
      toolrecall agent ls --json
      toolrecall agent show hermes --json
      toolrecall agent prune obsolete-agent
    """
    import json as _json

    from toolrecall.cache import get_agent_stats

    raw = sys.argv[2:]
    as_json = "--json" in raw
    args = [a for a in raw if a != "--json"]
    if not args or args[0] in ("--help", "-h"):
        print("Usage: toolrecall agent <subcommand> [--json]")
        print()
        print("Subcommands:")
        print("  ls              List all agents with cache stats")
        print("  show <id>       Show detailed stats for one agent")
        print("  prune <id>      Delete an agent's stats rows")
        print()
        print("Options:")
        print("  --json          Machine-readable output (ls: array, show: object)")
        print()
        print("Examples:")
        print("  toolrecall agent ls")
        print("  toolrecall agent show hermes --json")
        return

    sub = args[0]

    if sub == "ls":
        stats = get_agent_stats()
        agents = sorted(set(r["agent_id"] for r in stats))
        if as_json:
            out = []
            for agent_id in agents:
                agent_rows = [r for r in stats if r["agent_id"] == agent_id]
                out.append(
                    {
                        "agent_id": agent_id,
                        **_agent_totals(agent_rows),
                        "categories": len(agent_rows),
                        "updated_at": max((r.get("updated_at") or 0) for r in agent_rows),
                    }
                )
            print(_json.dumps(out, indent=2))
            return
        if not stats:
            print("No per-agent stats recorded. Set TOOLRECALL_AGENT_ID to enable tracking.")
            return
        print("Known Agents")
        print("=" * 40)
        for agent_id in agents:
            t = _agent_totals([r for r in stats if r["agent_id"] == agent_id])
            print(
                f"  {agent_id:<20}  hits={t['hits']}  misses={t['misses']}  "
                f"tokens_saved={t['tokens_saved']}  tokens_read={t['tokens_read_from_disk']}"
            )

    elif sub == "show":
        if len(args) < 2:
            print("Usage: toolrecall agent show <agent-id> [--json]")
            return
        agent_id = args[1]
        stats = get_agent_stats(agent_id=agent_id)
        if as_json:
            # Unknown agent → empty categories + zero totals, so dashboards
            # always get the same object shape.
            out = {
                "agent_id": agent_id,
                "categories": [
                    {
                        "category": r["category"],
                        **{k: r.get(k) or 0 for k in _AGENT_COUNTERS},
                        "updated_at": r.get("updated_at") or 0,
                    }
                    for r in stats
                ],
                "totals": _agent_totals(stats),
            }
            print(_json.dumps(out, indent=2))
            return
        if not stats:
            print(f"No stats recorded for agent '{agent_id}'.")
            return
        print(f"Agent: {agent_id}")
        print("=" * 50)
        for row in stats:
            print(
                f"  {row['category']:<20}  hits={row['hits']:<5}  misses={row['misses']:<5}  "
                f"tokens_saved={row['tokens_saved']:<6}  tokens_read={row['tokens_read_from_disk']:<6}  "
                f"context_tokens_saved={row['context_tokens_saved']}"
            )
        t = _agent_totals(stats)
        print("=" * 50)
        print(
            f"  TOTAL{'':<16}  hits={t['hits']:<5}  misses={t['misses']:<5}  "
            f"tokens_saved={t['tokens_saved']:<6}  tokens_read={t['tokens_read_from_disk']:<6}  "
            f"context_tokens_saved={t['context_tokens_saved']}"
        )

    elif sub == "prune":
        if len(args) < 2:
            print("Usage: toolrecall agent prune <agent-id>")
            return
        agent_id = args[1]
        try:
            from toolrecall._db import _db

            with _db() as conn:
                conn.execute("DELETE FROM agent_cache_stats WHERE agent_id = ?", (agent_id,))
                conn.execute(
                    "UPDATE access_log SET agent_id = NULL WHERE agent_id = ?", (agent_id,)
                )
            print(f"Deleted stats for agent '{agent_id}'.")
        except Exception as e:
            print(f"Failed to prune stats: {e}")

    else:
        print(f"Unknown subcommand: {sub}")
        print("Available: ls, show, prune")


def cmd_trend():
    """Daily hit-rate trend + worst miss classes (self-serve verification)."""
    args_rest = sys.argv[2:]
    if "--help" in args_rest or "-h" in args_rest:
        print("Usage: toolrecall trend [--days N] [--json]")
        print()
        print("Daily hit-rate trend + worst miss-heavy path classes, computed")
        print("from the access_log rolling window. Self-serve verification of")
        print("how well the cache serves YOUR workload (see README §Verifying).")
        print()
        print("Options:")
        print("  --days N    Window in days (default 7)")
        print("  --json      Machine-readable: {trend, miss_classes, coverage}")
        print()
        print("Daemon-independent: opens the cache DB read-only (WAL-safe).")
        return
    days = 7
    as_json = False
    for arg in args_rest:
        if arg == "--json":
            as_json = True
        elif arg == "--days":
            continue
        elif arg.isdigit() and "--days" in sys.argv:
            days = int(arg)
        elif arg.startswith("--days="):
            raw = arg.split("=", 1)[1]
            if raw.isdigit():
                days = int(raw)
    from toolrecall.trend import trend_report

    try:
        print(trend_report(days=days, as_json=as_json))
    except BrokenPipeError:
        # Piped into a pager/editor that closed early (e.g. `| less`, `q`).
        # Python's shutdown flush would print a spurious traceback — exit
        # cleanly the way well-behaved CLI filters do.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        os._exit(0)


def _cli_arg_value(flag: str) -> str | None:
    """Value of ``--flag value`` from sys.argv (None when absent or bare)."""
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def cmd_invalidate():
    """Clear cache — whole cache, one MCP server, or one site.

    toolrecall invalidate                  # agent caches (api_cache untouched)
    toolrecall invalidate --server fetch   # one MCP server's cached responses
    toolrecall invalidate --url example.com --server fetch
                                           # rows whose arguments contain the substring
    toolrecall invalidate --scope api      # explicit opt-in: ALSO wipe api_cache
    """
    server_flag = _cli_arg_value("--server")
    url_flag = _cli_arg_value("--url")
    scope_flag = _cli_arg_value("--scope")

    if url_flag:
        from toolrecall.cache import invalidate_mcp_url

        server = server_flag or "fetch"
        tool = "fetch_url" if server == "fetch" else "fetch"
        n = invalidate_mcp_url(server, tool, url_flag)
        print(f"Invalidated {n} cached response(s) for '{url_flag}' ({server}).")
        return

    if server_flag:
        from toolrecall.cache import invalidate_mcp_server

        invalidate_mcp_server(server_flag)
        print(f"Invalidated all cached responses for MCP server '{server_flag}'.")
        return

    try:
        from toolrecall.client import cache_invalidate

        print(cache_invalidate(scope=scope_flag or "agent"))
    except Exception:
        from toolrecall.cache import invalidate_all

        invalidate_all()
        print("ToolRecall cache cleared (direct).")


def cmd_reset_stats():
    """Reset the statistics window, moving counters and the trend window into lifetime totals."""
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: toolrecall reset-stats [--yes]")
        print()
        print("Reset the statistics window. Counters and the access_log window are")
        print("moved into banked lifetime totals — nothing is deleted.")
        print("Cache entries are preserved (use `toolrecall invalidate` to clear those).")
        print("Useful before benchmark runs so numbers reflect only that run.")
        print()
        print("Options:")
        print("  --yes     Skip the confirmation prompt (also: TOOLRECALL_NONINTERACTIVE)")
        return
    from toolrecall.cache import reset_stats

    # Changes every reported number, so confirm interactively. Not destructive
    # any more (the stats are banked, not wiped), but an unexplained sudden
    # zero in a benchmark reading is still worth a yes.
    # --yes / TOOLRECALL_NONINTERACTIVE bypass for scripts.
    if "--yes" not in sys.argv and "TOOLRECALL_NONINTERACTIVE" not in os.environ:
        try:
            resp = input(
                "Reset the stats window (counters move to banked lifetime totals,"
                " trend window is compacted)? [y/N] "
            )
        except (EOFError, OSError, KeyboardInterrupt):
            # OSError = non-tty stdin (captured/closed); treat as "no answer"
            # and keep the counters rather than crashing the command.
            print("\naborted")
            return
        if resp.strip().lower() not in ("y", "yes"):
            print("aborted — counters untouched")
            return
    reset_stats()
    print(
        "Statistics window reset. Totals kept as banked lifetime values"
        " (`toolrecall stats` lifetime_*), trend history preserved. Cache entries untouched."
    )


def cmd_index():
    """Index knowledge base. Use --memory to also index agent memory stores."""
    from toolrecall.docs import index_all, index_agent_memory

    print("Indexing knowledge database...")
    total = index_all()
    print(f"Done. {total} pages indexed.")

    if "--memory" in sys.argv:
        print("Indexing agent memory stores...")
        mem_total = index_agent_memory()
        print(f"Done. {mem_total} memory entries indexed.")


def cmd_index_compact():
    """Rebuild the FTS index and VACUUM the knowledge DB (reclaim bloat)."""
    from toolrecall.docs import compact_knowledge_db, get_index_bloat

    fts, content = get_index_bloat()
    print(
        f"FTS index: {fts / 1e6:.1f} MB for {content / 1e6:.1f} MB of content"
        + (" — bloated, compacting" if fts > 5 * content and fts > 50e6 else "")
    )
    result = compact_knowledge_db()
    print(
        f"Done. {result['before'] / 1e6:.1f} MB → {result['after'] / 1e6:.1f} MB "
        f"(reclaimed {result['reclaimed'] / 1e6:.1f} MB)."
    )


def cmd_tool_usage():
    """Which agent-facing ToolRecall tools are actually called, and how often.

    Exists because ToolRecall could not previously answer that about itself:
    `docs_search` and `docs_get_page` had been advertised for months and had
    never been called once (measured 2026-09-30 via the Hermes agent log and the
    session transcripts) while nothing in the product showed it.
    """
    import time as _time

    from toolrecall.cache import TOOL_USAGE_TRACKED, tool_usage_counts

    rows = tool_usage_counts()
    if not rows:
        print("No agent-facing tool calls recorded yet.")
        print()
        print("  counted:     " + ", ".join(sorted(TOOL_USAGE_TRACKED)))
        print("  not counted: file/shim commands (different question, far higher volume)")
        print()
        print("  An empty table means none of the counted tools ran since the counter")
        print("  exists — it is NOT proof that agents do not use ToolRecall.")
        return
    print(f"{'tool':22} {'calls':>8} {'errors':>7}  last call")
    for row in rows:
        last = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(row["last_at"] or 0))
        print(f"{row['tool']:22} {row['calls']:>8} {row['errors']:>7}  {last}")
    print()
    print("  Counted tools only: knowledge base, MCP surface, cache_status.")


def cmd_index_remote():
    r"""Sync remote doc sources ([[sources.remote]]) into the knowledge database.

    Opt-in and local-first: nothing is fetched unless a source is configured,
    and the local content (skills, memories, scanned dirs) is untouched. Every
    skipped or flagged page is recorded — see 'toolrecall docs-rejects'.

    Usage:
        toolrecall index-remote                  # all configured sources
        toolrecall index-remote --source NAME    # one source
        toolrecall index-remote --force          # ignore ETag/Last-Modified
    """
    from toolrecall.docs_sources import configured_sources, sync_all

    sources = configured_sources()
    if not sources and "--source" not in sys.argv:
        print("No remote sources configured — the knowledge base stays local.")
        print()
        print("Remote sources are opt-in. Add to your toolrecall.toml:")
        print()
        print("  [[sources.remote]]")
        print('  name = "hermes-docs"')
        print('  kind = "llms_full"                       # or "urls"')
        print('  url  = "https://example.com/docs/llms-full.txt"')
        print('  base_url = "https://example.com/docs/"')
        print()
        print("Then: toolrecall index-remote  ·  toolrecall docs-rejects")
        return

    only = None
    if "--source" in sys.argv:
        i = sys.argv.index("--source")
        if i + 1 < len(sys.argv):
            only = sys.argv[i + 1]
    force = "--force" in sys.argv

    print(f"Syncing remote sources{' (' + only + ')' if only else ''}...")
    reports = sync_all(only=only, force=force)
    if not reports:
        print(f"  no configured source named '{only}'")
        return

    total = sum(r["indexed"] for r in reports)
    flagged = sum(r["flagged"] for r in reports)
    skipped = sum(sum(r["rejected"].values()) for r in reports)
    print()
    print(f"Done. {total} pages indexed, {flagged} flagged, {skipped} skipped.")
    for r in reports:
        for e in r["errors"]:
            print(f"  ⚠ {r['source']}: {e}")
    if flagged or skipped:
        print()
        print("Why pages were skipped or flagged:  toolrecall docs-rejects")


def cmd_docs_rejects():
    r"""Which remote pages are skipped or flagged most, and for what reason.

    Usage:
        toolrecall docs-rejects                # all sources, top 20
        toolrecall docs-rejects --source NAME
        toolrecall docs-rejects --top 50
    """
    from toolrecall.docs_sources import format_rejection_stats, rejection_stats

    source, top = None, 20
    if "--source" in sys.argv:
        i = sys.argv.index("--source")
        if i + 1 < len(sys.argv):
            source = sys.argv[i + 1]
    if "--top" in sys.argv:
        i = sys.argv.index("--top")
        if i + 1 < len(sys.argv):
            try:
                top = int(sys.argv[i + 1])
            except ValueError:
                print(f"  ignoring non-numeric --top value: {sys.argv[i + 1]!r}")
    print(format_rejection_stats(rejection_stats(source=source, top=top), source=source))


def cmd_dedup_stats():
    """Which tools' outputs the request dedup actually stubbed, and how much.

    The usage CSV carries one dedup_tokens figure per request; this is the
    per-tool breakdown (same chars//4 estimator, so the sums reconcile).
    """
    import time as _time

    from toolrecall.dedup_stats import per_tool

    rows = per_tool()
    if not rows:
        print("No request-dedup stubs recorded yet.")
        print()
        print("  Dedup only fires when a request contains the SAME large block twice")
        print("  (>= 800 chars, byte-identical). Measured 2026-10-01 over 385 sessions:")
        print("  that is rare — about 1 % of large tool outputs, 43 % for skills_list.")
        print("  An empty table is NOT proof that dedup is broken.")
        return
    total = sum(r["tokens_saved"] for r in rows) or 1
    print(f"{'tool':24} {'stubs':>7} {'tokens saved':>13} {'share':>7} {'requests':>9}  last")
    for r in rows:
        last = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(r["last_at"] or 0))
        print(
            f"{r['tool'][:24]:24} {r['blocks']:>7} {r['tokens_saved']:>13,} "
            f"{100 * r['tokens_saved'] / total:>6.1f}% {r['requests']:>9}  {last}"
        )
    print()
    print("  tokens = chars // 4, the same estimator as dedup_tokens in proxy_usage.csv")


def cmd_index_memory():
    """Index agent persistent memory stores (MEMORY.md, USER.md) into knowledge DB.

    Uses AGENT_HOME env var (or HERMES_HOME for backward compat) to locate
    the memories/ directory.
    """
    from toolrecall.docs import index_agent_memory

    # Optional: custom source label via --source
    source = "agent-memory"
    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        if idx + 1 < len(sys.argv):
            source = sys.argv[idx + 1]

    print(f"Indexing agent memory stores (source='{source}')...")
    total = index_agent_memory(source=source)
    print(f"Done. {total} memory entries indexed with FTS5 (source='{source}').")
    print()
    print("Query via: docs_search('<query>', source='<source>')")
    print("Or via MCP: toolrecall docs_search '<query>'")
    print()


def cmd_index_dir():
    r"""Index a directory into the knowledge database.

    Usage:
        toolrecall index-dir ~/Documents/Obsidian\ Vault
        toolrecall index-dir --source my-notes ~/notes
    """
    from toolrecall.docs import index_directory

    # Parse args
    args = [a for a in sys.argv[2:] if not a.startswith("--source")]
    source_override = None
    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        if idx + 1 < len(sys.argv):
            source_override = sys.argv[idx + 1]

    if not args:
        print("Usage: toolrecall index-dir [--source label] <directory> [directory2 ...]")
        print()
        print("Index all .md files from the given directory into the FTS5 knowledge DB.")
        print("Each file becomes a searchable page. Use --source to set a custom label")
        print("(default: basename of the directory).")
        print()
        print("Examples:")
        print("  toolrecall index-dir ~/Documents/Obsidian\\\\ Vault")
        print("  toolrecall index-dir --source my-wiki ~/wiki")
        print("  toolrecall index-dir ~/notes ~/Documents/Obsidian\\\\ Vault")
        return

    total_all = 0
    for dir_arg in args:
        dir_path = os.path.expanduser(dir_arg)
        if not os.path.isdir(dir_path):
            print(f"⚠️  Not a directory: {dir_path}")
            continue

        source = source_override or os.path.basename(dir_path)
        print(f"Indexing '{dir_path}' as source='{source}'...")
        count = index_directory(dir_path, source=source)
        print(f"  → {count} pages indexed")
        total_all += count

    print(f"\nDone. {total_all} total pages indexed.")
    print("Query via: docs_search('<query>', source='<source>')")


def cmd_config_set():
    """Set a config value in config.toml.

    Usage:
        toolrecall config-set proxy.port 9090
        toolrecall config-set mcp.allow_terminal true
        toolrecall config-set mcp.allowed_paths "['/data', '/projects']"
    """
    from toolrecall.config import load_config, save_config

    # save_config uses built-in TOML serializer — no external deps

    args = sys.argv[2:]
    if len(args) < 2 or "--help" in args or "-h" in args:
        print("Usage: toolrecall config-set <section.key> <value>")
        print()
        print("Examples:")
        print("  toolrecall config-set proxy.port 9090")
        print("  toolrecall config-set mcp.allow_terminal true")
        print(
            "  toolrecall config-set security.tool_access_control true  # MCP keyword access control (not OS sandbox)"
        )
        print("  toolrecall config-set mcp.allowed_paths \"['/data', '/projects']\"")
        return

    key = args[0]
    val = args[1]
    parts = key.rsplit(".", 1)

    if len(parts) != 2:
        print(f"❌ Invalid key: '{key}'. Use section.key format (e.g. proxy.port)")
        return

    section, name = parts
    cfg_path = os.path.expanduser("~/.config/toolrecall/toolrecall.toml")
    cfg = load_config(cfg_path)

    # Parse value
    parsed_val = val
    if val.lower() == "true":
        parsed_val = True
    elif val.lower() == "false":
        parsed_val = False
    else:
        try:
            parsed_val = int(val)
        except ValueError:
            try:
                parsed_val = float(val)
            except ValueError:
                # Try as list
                if val.startswith("[") and val.endswith("]"):
                    import ast

                    try:
                        parsed_val = ast.literal_eval(val)
                    except Exception:
                        pass
                # Keep as string

    # Apply
    if section not in cfg._data:
        cfg._data[section] = {}
    cfg._data[section][name] = parsed_val

    if save_config(cfg_path, cfg):
        print(f"✅ Set {key} = {parsed_val!r} in {cfg_path}")
        print("⚠️  Restart the daemon for changes to take effect.")
    else:
        print(f"❌ Failed to write {cfg_path}")


def cmd_serve():
    """Start the forward proxy (caches LLM API responses).

    Note: The daemon already starts the forward proxy automatically.
    This command is only needed if you want a standalone instance
    on a different port while the daemon is NOT running.
    """
    # Parse --port from argv
    port_override = None
    clean_argv = []
    i = 0
    while i < len(sys.argv):
        if sys.argv[i] == "--port" and i + 1 < len(sys.argv):
            port_override = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i].startswith("--port="):
            port_override = int(sys.argv[i].split("=", 1)[1])
            i += 1
        else:
            clean_argv.append(sys.argv[i])
            i += 1
    sys.argv = clean_argv

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: toolrecall serve [--port PORT]")
        print()
        print("Start the forward proxy — caches LLM API responses (OpenAI, Anthropic, etc.)")
        print("before they leave your machine. On cache hit, returns cached response")
        print("without contacting the provider.")
        print()
        print("Note: The forward proxy also starts automatically with `toolrecall daemon`.")
        print("Use `toolrecall serve` only if you need a standalone instance on a different port.")
        print()
        print("Options:")
        print("  --help, -h          Show this help message")
        print("  --port PORT         Override forward proxy port")
        print()
        print(f"Default port: {int(os.environ.get('TOOLRECALL_FORWARD_PORT', '8569'))}")
        print()
        print("Examples:")
        print("  toolrecall serve                    # Forward proxy on :8569")
        print("  toolrecall serve --port 9090        # Forward proxy on :9090")
        print()
        print("Use with:")
        print("  export OPENAI_BASE_URL=http://localhost:8569")
        print("  export ANTHROPIC_BASE_URL=http://localhost:8569")
        return

    # Check if daemon is already running (it starts the proxy on default port)
    from toolrecall.transport import TransportClient

    daemon_check = TransportClient()
    daemon_ping = daemon_check.send({"cmd": "ping"})
    daemon_running = daemon_ping.get("pong", False)

    if daemon_running and (
        port_override is None
        or port_override == int(os.environ.get("TOOLRECALL_FORWARD_PORT", "8569"))
    ):
        print("The daemon already manages the forward proxy on port 8569.")
        print("To run a standalone proxy on a different port, use:")
        print("  toolrecall serve --port <PORT>")
        print("while the daemon is stopped.")
        return

    from toolrecall.proxy import run_forward_proxy

    port = (
        port_override
        if port_override is not None
        else int(os.environ.get("TOOLRECALL_FORWARD_PORT", "8569"))
    )
    run_forward_proxy(port=port)


def cmd_mcp():
    """MCP Bridge & Registry commands.

    Subcommands:
      list    List registered and active MCP servers
      <none>  Start MCP Bridge (stdio -> Daemon)
    """
    if len(sys.argv) >= 3:
        sub = sys.argv[2]
        if sub == "list":
            return cmd_mcp_list()
        elif sub in ("--help", "-h", "help"):
            print("Usage: toolrecall mcp [list]")
            print()
            print("Start the MCP Bridge (stdio → daemon) — connect any MCP agent by")
            print("adding `toolrecall mcp` to its MCP config. The bridge serves")
            print("cached_read / cached_terminal / docs_search / multiplexed MCP tools.")
            print()
            print("Subcommands:")
            print("  list    List registered MCP servers (builtin/external, command, args)")
            print()
            print("Flags:")
            print("  --multiplexer-only    No file/terminal cache tools; multiplexer + docs only")
            print("                        (recommended for Claude Code — see docs/CLI.md)")
            print()
            print("Requires the daemon: `toolrecall daemon`.")
            return

    # Default: start MCP Bridge
    from toolrecall.mcp_bridge import main as bridge_main

    bridge_main()


def cmd_mcp_list():
    """List registered MCP servers with status."""
    from toolrecall.mcp_registry import list_registered_servers, has_uvx

    servers = list_registered_servers()
    if not servers:
        print("No MCP servers registered.")
        return

    print(f"MCP Server Registry  ({len(servers)} total)")
    print(f"{'Name':<25} {'Source':<10} {'Command':<30} {'Args'}")
    print("-" * 100)
    for srv in servers:
        source = srv["source"]
        cmd = srv["command"]
        args = " ".join(srv["args"])
        print(f"{srv['name']:<25} {source:<10} {cmd:<30} {args}")

    # Check uvx
    if not has_uvx():
        print()
        print("⚠️  uvx not found on PATH — external servers (fetch, filesystem, git, ...)")
        print("   will NOT start until uvx is installed.")
        print("   Install: curl -LsSf https://astral.sh/uv/install.sh | sh")

    print()
    print("Active via daemon:")
    print("  Run `toolrecall status` or connect MCP Bridge to see live servers.")


def cmd_debug():
    """Start minimal debug/demo server on :8570."""
    from toolrecall.proxy import run_debug_server

    port_override = None
    for i, arg in enumerate(sys.argv[2:], 2):
        if arg == "--port" and i + 1 < len(sys.argv):
            port_override = int(sys.argv[i + 1])
        elif arg.startswith("--port="):
            port_override = int(arg.split("=", 1)[1])

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: toolrecall debug [--port PORT]")
        print()
        print("Start minimal debug/demo server.")
        print("Endpoints:")
        print("  GET /read?path=X   cached_read demo")
        print("  GET /term?cmd=X    cached_terminal demo")
        print("  GET /stats         cache statistics")
        print("  GET /health        daemon status")
        print()
        print("Example:")
        print("  time curl http://localhost:8570/read?path=README.md")
        return

    run_debug_server(port=port_override or 8570)


def _clean_daemon_env() -> dict:
    """Copy os.environ minus path overrides that must not leak into a daemon.

    Auto-started daemons inherit the caller's environment. When the caller is
    a pytest process (hermetic fixtures set TOOLRECALL_CACHE_DB /
    TOOLRECALL_KNOWLEDGE_DB to per-test temp paths), a daemon spawned from
    that context would silently serve a scratch database — or one that is
    deleted moments later — while looking like the production daemon on the
    production socket. Strip the path-overriding variables so the spawned
    daemon resolves its DB paths from config, exactly as a systemd-started
    daemon would.
    """
    import os

    env = os.environ.copy()
    for var in (
        "TOOLRECALL_CACHE_DB",
        "TOOLRECALL_KNOWLEDGE_DB",
        "TOOLRECALL_UDS_PATH",
        "TOOLRECALL_CONFIG",
    ):
        env.pop(var, None)
    return env


def _ensure_daemon():
    """Auto-start the ToolRecall cache daemon if not running.

    Tries (in order):
    1. systemd --user (Linux with systemd)
    2. Direct fork + run_daemon() (Docker, macOS, Codespaces)
    3. Windows fallback (subprocess.DETACHED_PROCESS)

    Returns True if daemon is running after attempt, False otherwise.

    Note: auto-starts the daemon silently (output goes to DEVNULL). To start
    it explicitly with visible output, run ``toolrecall daemon`` separately.
    """
    from toolrecall.transport import TransportClient, DEFAULT_PATH
    import time

    # Test/ci gate: hermetic runs set TOOLRECALL_NO_AUTOSTART=1 so a ping
    # miss can never start the systemd service or spawn a daemon from
    # inside pytest (the escaped-daemon failure mode pinned in
    # test_daemon_env_hygiene.py).
    if os.environ.get("TOOLRECALL_NO_AUTOSTART") == "1":
        return False

    # ── 1. Already running? ──
    try:
        tc = TransportClient(DEFAULT_PATH)
        resp = tc.send({"cmd": "ping"})
        if resp.get("pong"):
            return True
    except Exception:
        pass

    # ── 2. systemd user service ──
    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", "toolrecall-daemon"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            for _ in range(10):
                time.sleep(0.5)
                try:
                    tc = TransportClient(DEFAULT_PATH)
                    resp = tc.send({"cmd": "ping"})
                    if resp.get("pong"):
                        return True
                except Exception:
                    continue
    except FileNotFoundError:
        pass  # No systemd — fall through

    # ── 3. Direct subprocess (no systemd) ──
    # Use subprocess instead of fork to avoid venv-vs-system conflicts.
    # start_new_session=True detaches from the parent process group so the
    # daemon survives the CLI process exiting (e.g. when opencode ends a session).
    # Important: use the binary (toolrecall daemon --foreground) not python -m,
    # because toolrecall has no __main__.py and `python -m toolrecall` fails.
    import sys as _sys
    import subprocess as _sp
    import shutil as _shutil

    try:
        print(
            "[ToolRecall] Daemon not running — auto-starting silently in background...",
            file=_sys.stderr,
            flush=True,
        )
        _toolrecall_bin = _shutil.which("toolrecall")
        if not _toolrecall_bin:
            # Fallback: locate the installed package's cli module directly
            _toolrecall_bin = _sys.executable
            _sp.Popen(
                [
                    _toolrecall_bin,
                    "-c",
                    "from toolrecall.cli import cmd_daemon; import sys; sys.argv = ['toolrecall', 'daemon', '--foreground']; cmd_daemon()",
                ],
                env=_clean_daemon_env(),
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                start_new_session=True,
            )
        else:
            _sp.Popen(
                [_toolrecall_bin, "daemon", "--foreground"],
                env=_clean_daemon_env(),
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                start_new_session=True,
            )
        for _ in range(10):
            time.sleep(0.5)
            try:
                tc = TransportClient(DEFAULT_PATH)
                resp = tc.send({"cmd": "ping"})
                if resp.get("pong"):
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # ── 4. Windows fallback ──
    import sys as _sys

    if _sys.platform == "win32":
        try:
            import subprocess as _sp

            _sp.Popen(
                ["toolrecall", "daemon", "--foreground"],
                env=_clean_daemon_env(),
                creationflags=_sp.DETACHED_PROCESS,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            for _ in range(10):
                time.sleep(0.5)
                try:
                    tc = TransportClient(DEFAULT_PATH)
                    resp = tc.send({"cmd": "ping"})
                    if resp.get("pong"):
                        return True
                except Exception:
                    continue
        except Exception:
            pass

    return False


def _ensure_shim():
    """Install OS-level shim if not present, then load it into the current process.

    Why this is needed: the tr_shim.pth file auto-imports toolrecall.shim
    on every Python process startup. The .pth file uses a bare
    'import toolrecall.shim' which relies on Python's normal import
    resolution — this works with both editable and regular installs.
    """
    import os
    import shutil
    import sys

    try:
        installed = False
        # Check if shim is already in site-packages
        for p in sys.path:
            if p.endswith("site-packages"):
                pth = os.path.join(p, "tr_shim.pth")
                if os.path.exists(pth):
                    installed = True
                    break

        if not installed:
            # Install the .pth file from the package's bundled copy
            for p in sys.path:
                if p.endswith("site-packages") and os.path.isdir(p):
                    pth_src = os.path.join(os.path.dirname(__file__), "tr_shim.pth")
                    pth_dst = os.path.join(p, "tr_shim.pth")
                    if os.path.exists(pth_src):
                        shutil.copy2(pth_src, pth_dst)
                        print("  ℹ️  Shim auto-installed (tr_shim.pth)")
                        installed = True
                    break

        # Load the shim into the CURRENT process so existing agents benefit immediately
        if installed:
            try:
                import toolrecall.shim

                toolrecall.shim.apply()  # Force apply patches (idempotent)
            except (ImportError, AttributeError):
                pass
    except Exception:
        pass  # Silently ignore — shim is optional


def cmd_daemon():
    """Manage the ToolRecall Cache Daemon.

    Starts cache daemon + MCP bridge + forward proxy (:8569)."""
    from toolrecall.daemon import run_daemon, stop_daemon, daemon_status

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: toolrecall daemon [--foreground] [--stop] [--status]")
        print()
        print("Start the ToolRecall cache daemon.")
        print("  • MCP bridge: agents connect via stdio MCP")
        print("  • Forward proxy: http://127.0.0.1:8569 (caches API responses)")
        print("  • Use --foreground to run in terminal (not daemonized)")
        print()
        print("Then point agents OPENAI_BASE_URL to http://localhost:8569")
        return

    # Accept both --stop (flag) and "stop" (positional subcommand)
    args = sys.argv[2:]  # everything after "toolrecall daemon"
    if "--stop" in args or "stop" in args:
        stop_daemon()
    elif "--status" in args or "status" in args:
        daemon_status()
    elif "--foreground" in args or "foreground" in args:
        run_daemon(foreground=True)
    else:
        run_daemon(foreground=False)


def cmd_proxy():
    """Attach/detach Hermes' LLM traffic to the ToolRecall API proxy.

    proxy attach — point llm.base_url at http://127.0.0.1:8569/v1
                   (health-checked; config backup kept)
    proxy detach — restore the backed-up config
    """
    sub = sys.argv[2:3]
    if sub == ["attach"]:
        ok, msg = _attach_hermes_to_proxy()
        print(("✅ " if ok else "⚠️  ") + msg)
        if ok and "already" not in msg:
            print("   restart Hermes to route LLM traffic through the proxy")
        sys.exit(0 if ok else 1)
    if sub == ["detach"]:
        sys.exit(_detach_hermes_from_proxy())
    print("Usage: toolrecall proxy <attach|detach>")
    print()
    print("  attach   point Hermes' llm.base_url at the local forward proxy")
    print("           (health-gated, backup kept — see toolrecall serve)")
    print("  detach   restore the pre-attach config from backup")
    sys.exit(1)


def cmd_nginx():
    """Generate nginx config."""
    cfg_dir = os.path.expanduser("~/.toolrecall")
    os.makedirs(cfg_dir, exist_ok=True)
    out_path = os.path.join(cfg_dir, "nginx-toolrecall.conf")

    nginx_cfg = """# ToolRecall — Nginx Reverse Proxy Config
# Generated by `toolrecall nginx`
# Place in /etc/nginx/sites-available/toolrecall
# Then: ln -s /etc/nginx/sites-available/toolrecall /etc/nginx/sites-enabled/
# Then: nginx -t && systemctl reload nginx

server {
    listen 80;
    server_name localhost;

    proxy_cache off;
    proxy_no_cache 1;
    proxy_cache_bypass 1;

    location /toolrecall/ {
        proxy_pass http://127.0.0.1:8569/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }

    # Optional: Password protection
    # location /toolrecall/ {
    #     auth_basic "ToolRecall";
    #     auth_basic_user_file /etc/nginx/.htpasswd_toolrecall;
    #     proxy_pass http://127.0.0.1:8569/;
    # }
}

# SSL Version (recommended)
# server {
#     listen 443 ssl;
#     server_name toolrecall.dev;
#     ssl_certificate /etc/letsencrypt/live/toolrecall.dev/fullchain.pem;
#     ssl_certificate_key /etc/letsencrypt/live/toolrecall.dev/privkey.pem;
#     location / {
#         proxy_pass http://127.0.0.1:8569/;
#     }
# }
"""
    with open(out_path, "w") as f:
        f.write(nginx_cfg)
    print(f"Nginx config written to: {out_path}")
    print()
    print("To install:")
    print(f"  sudo cp {out_path} /etc/nginx/sites-available/toolrecall")
    print("  sudo ln -s /etc/nginx/sites-available/toolrecall /etc/nginx/sites-enabled/")
    print("  sudo nginx -t && sudo systemctl reload nginx")


def cmd_shim():
    """Install/uninstall/inspect the transparent cache shim (.pth) agent-agnostically.

    Modes:
      toolrecall shim --install                current venv only (back-compat)
      toolrecall shim --install --venv <path>  target one venv (root or bin/python)
      toolrecall shim --install --all          discover + install into every venv
      toolrecall shim --status [--venv <path> | --all]
      toolrecall shim --uninstall [--venv <path> | --all]
      toolrecall shim --disable             persistent disable (marker file)
      toolrecall shim --enable              clear the persistent disable marker
    Any unrecognized flag prints usage and exits non-zero (no silent swallowing).
    """
    from toolrecall import venvs as venv_mod

    args = sys.argv[2:]

    def usage(exit_code: int = 1):
        print(
            "Usage: toolrecall shim [--install|--uninstall|--status|--disable|--enable] [--venv <path>|--all]"
        )
        print()
        print("  --install            Install .pth shim into a Python env")
        print("  --uninstall          Remove the .pth shim (leaves the package)")
        print("  --status             Report shim state")
        print("  --disable            Persistently disable the shim (marker file)")
        print("  --enable             Re-enable the shim (clear the disable marker)")
        print("  --venv <path>        Target a specific venv (root dir or its bin/python)")
        print("  --all                Apply to every discovered venv (excl. toolrecall's own)")
        print("  --yes / -y           Skip the opt-in confirmation prompt (default is NO)")
        print()
        print("Python-vs-bridge model:")
        print("  Python agents w/ own venv (Hermes, Codex, OpenCode): .pth shim in that venv")
        print("  Non-Python agents (Claude Code, Cursor, Cline): MCP bridge `toolrecall mcp`")
        sys.exit(exit_code)

    action = None
    target = None
    all_flag = False
    yes_flag = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--install", "install"):
            action = "install"
        elif a in ("--uninstall", "uninstall"):
            action = "uninstall"
        elif a in ("--status", "status"):
            action = "status"
        elif a in ("--disable", "disable"):
            action = "disable"
        elif a in ("--enable", "enable"):
            action = "enable"
        elif a in ("--venv", "-v"):
            if i + 1 >= len(args):
                print("Error: --venv requires a path argument")
                usage(2)
            target = args[i + 1]
            i += 1
        elif a in ("--all", "-a"):
            all_flag = True
        elif a in ("--yes", "-y"):
            yes_flag = True
        elif a in ("-h", "--help"):
            usage(0)
        else:
            print(f"Error: unknown shim flag: {a}")
            usage(2)
        i += 1

    if action is None:
        action = "status"  # bare `toolrecall shim` → status (back-compat)

    if action in ("disable", "enable"):
        # Global marker operations — independent of any venv target.
        import toolrecall.shim as shim_mod

        if action == "disable":
            if shim_mod.disable():
                print("✅ Shim DISABLED (persistent).")
                print(
                    "   Existing processes keep running as-is; new Python processes skip the shim."
                )
                print("   Re-enable with: toolrecall shim --enable")
                print(f"   Marker: {shim_mod._marker_path()}")
            else:
                print(f"⚠️  Could not write disable marker: {shim_mod._marker_path()}")
        else:
            if shim_mod.enable():
                print("✅ Shim ENABLED.")
                print("   New Python processes will apply the shim again.")
            else:
                print("⚠️  Could not clear the disable marker.")
        return

    def make_venv(path: str):
        """Resolve `path` (venv root OR its bin/python) to a Venv, or None."""
        p = os.path.expanduser(path)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "pyvenv.cfg")):
            # venv root
            launcher = None
            bindir = os.path.join(p, "bin")
            if os.path.isdir(bindir):
                for name in sorted(os.listdir(bindir)):
                    if name.startswith("python3"):
                        full = os.path.join(bindir, name)
                        if os.access(full, os.X_OK):
                            launcher = full
                            break
            if not launcher:
                print(f"Error: no python launcher found in {p}/bin")
                return None
            sp = venv_mod._site_packages_of(launcher)
            if not sp:
                print(f"Error: could not resolve site-packages for {launcher}")
                return None
            return venv_mod.Venv(root=p, python=launcher, site_packages=sp)
        if os.path.isfile(p) and os.path.basename(p).startswith("python"):
            # direct python path
            root = os.path.dirname(os.path.dirname(p))
            sp = venv_mod._site_packages_of(p)
            if not sp:
                print(f"Error: could not resolve site-packages for {p}")
                return None
            return venv_mod.Venv(root=root, python=p, site_packages=sp)
        print(f"Error: not a venv root or python path: {path}")
        return None

    current_env = None
    for p in sys.path:
        if p.endswith("site-packages") and os.path.isdir(p):
            # current interpreter's own venv
            py = sys.executable
            current_env = venv_mod.Venv(
                root=os.path.dirname(os.path.dirname(p)),
                python=py,
                site_packages=p,
            )
            break

    if target:
        v = make_venv(target)
        if v is None:
            sys.exit(2)
        targets = [v]
    elif all_flag:
        found = venv_mod.discover_python_venvs()
        if not found:
            print("No Python venvs discovered (other than toolrecall's own).")
            return
        print(f"Discovered {len(found)} venv(s):")
        for v in found:
            print(f"  {v.python}")
        targets = found
    else:
        if current_env is None:
            print("Error: could not find the current environment's site-packages")
            sys.exit(2)
        targets = [current_env]

    if action == "status":
        import toolrecall.shim as shim_mod

        if shim_mod._marker_disabled():
            print(f"⚠️  Shim globally DISABLED via marker: {shim_mod._marker_path()}")
            print("    Run `toolrecall shim --enable` to re-enable.\n")
        for v in targets:
            st = venv_mod.shim_status(v)
            ok = st["probe_ok"]
            pth = "present" if st["pth_present"] else "missing"
            pkg = "ok" if st["package_importable"] else "MISSING"
            icon = "✅" if ok else "❌"
            print(f"{icon} {v.root}")
            print(f"     python: {v.python}")
            print(
                f"     package: {pkg} | shim: {pth} | "
                f"applied: {'yes' if st['applied'] else 'no'} | "
                f"probe: {'pass' if ok else 'FAIL'}"
            )
            print(
                f"     version: {st['version_venv'] or '-'} (venv) vs "
                f"{st['version_source'] or '-'} (this install)"
            )
            if st["drift"]:
                print(
                    "     shim modules: ⚠️  DRIFT — differ from this install "
                    "(pre-fix shim behaviour)"
                )
                print(f"     fix: toolrecall shim --install --venv {v.root} --yes")
            else:
                print("     shim modules: in sync")
        return

    if action == "install":
        for v in targets:
            if not yes_flag and not os.environ.get("TOOLRECALL_NONINTERACTIVE"):
                if not _confirm_install(v):
                    print(f"  skipped (opt-out): {v.root}")
                    continue
            if venv_mod.ensure_shim(v):
                print(f"✅ Shim active in {v.root} (verified: import toolrecall.shim OK)")
            else:
                print(f"⚠️  Shim install FAILED in {v.root} — see diagnosis below")
                st = venv_mod.shim_status(v)
                if not st["package_importable"]:
                    print(
                        f"     `toolrecall` not importable in {v.python}; "
                        "install it (pip/uv) or check the venv is writable"
                    )
                elif not st["pth_present"]:
                    print(f"     could not write {v.site_packages}/tr_shim.pth")
                elif st["drift"]:
                    print(
                        "     shim modules still differ from this install "
                        f"(venv version: {st['version_venv'] or 'unknown'}) — "
                        "the venv may be read-only, or its copy was installed "
                        "out of band; reinstall it from this tree: "
                        f"pip install -e {os.path.dirname(os.path.dirname(venv_mod.__file__))}"
                    )
                else:
                    print(f"     import probe failed in {v.python}")
        return

    if action == "uninstall":
        for v in targets:
            if venv_mod.uninstall_shim(v):
                print(f"✅ Shim removed from {v.root}")
            else:
                print(f"⚠️  Could not remove shim from {v.root}")


def _confirm_install(v) -> bool:
    """Show the opt-in prompt for shimming a Python venv. Default DISABLED (N)."""
    try:
        print()
        if v is not None:
            print(f"Detected Python agent venv: {v.root}")
        print("  ToolRecall's .pth shim patches open()/subprocess for EVERY python")
        print("  process in this venv for transparent caching.")
        print()
        print("  Risks:    broad interception of non-agent reads; stale-content caching;")
        print("            behavior changes if daemon is down.")
        print("  Benefits: zero-config cache, ~99% hit rate, terminal dedup, token savings.")
        print()
        resp = input("  Enable shim? [y/N] ").strip().lower()
    except (EOFError, OSError, KeyboardInterrupt):
        # OSError is the non-tty case: "reading from stdin while output is
        # captured" (pytest, CI, a closed fd). Catching it here — rather than
        # gating on isatty() — keeps a piped `yes | toolrecall shim --install`
        # working while never crashing on stdin that cannot answer.
        return False
    return resp in ("y", "yes")


SYSTEMD_SERVICE_CONTENT = """[Unit]
Description=ToolRecall Cache Daemon
After=network.target

[Service]
Type=simple
ExecStart=%s
Restart=on-failure
RestartSec=5s
# Kill the daemon quickly on stop — the daemon's accept() loop has a 1s
# poll timeout and exits cleanly within ~2s.  The default 90s timeout
# leaves a hung daemon blocking restarts for over a minute.
TimeoutStopSec=10

[Install]
WantedBy=default.target
"""


def _host_os() -> str:
    """Classify the host OS: 'macos', 'windows', or 'linux'.

    systemd autostart only applies on Linux; macOS uses launchd, and
    Windows has no per-user service manager we target here.
    """
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


# macOS LaunchAgent plist (equivalent of the Linux systemd user service).
# Loaded at login via RunAtLoad; KeepAlive restarts the daemon if it exits.
LAUNCH_AGENT_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{toolrecall_bin}</string>
        <string>daemon</string>
        <string>--foreground</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""


def _install_macos_launch_agent() -> tuple[bool, str]:
    """Install a macOS LaunchAgent that starts the ToolRecall daemon at login.

    Writes a plist to ~/Library/LaunchAgents and, if possible, loads it
    immediately with launchctl. Even when launchctl is unavailable, the
    RunAtLoad plist guarantees an autostart at the next login.
    """
    import shutil as _shutil
    import subprocess as _sp

    label = "toolrecall-daemon"
    agents_dir = os.path.expanduser("~/Library/LaunchAgents")
    plist_path = os.path.join(agents_dir, label + ".plist")

    # Prefer the on-disk binary and resolve to an absolute path -- launchd
    # does NOT expand "~" inside ProgramArguments.
    toolrecall_bin = _shutil.which("toolrecall") or os.path.expanduser("~/.local/bin/toolrecall")
    if not toolrecall_bin or not os.path.exists(toolrecall_bin):
        return False, "macOS autostart skipped: toolrecall binary not found"

    try:
        os.makedirs(agents_dir, exist_ok=True)
        with open(plist_path, "w") as f:
            f.write(LAUNCH_AGENT_PLIST.format(label=label, toolrecall_bin=toolrecall_bin))
        # Best-effort immediate load; non-fatal, next-login start still applies.
        _sp.run(["launchctl", "load", plist_path], capture_output=True, timeout=10)
        return True, f"macOS autostart: LaunchAgent installed ({plist_path})"
    except Exception:
        return False, "macOS autostart could not be installed"


def _install_windows_scheduled_task() -> tuple[bool, str]:
    """Install a per-user Windows Scheduled Task that starts the daemon at logon.

    Windows equivalent of the macOS LaunchAgent / Linux systemd user unit:
    `schtasks /SC ONLOGSTART` runs at every login, needs no admin rights,
    and is idempotent (/F overwrites an existing task).
    """
    import shutil as _shutil
    import subprocess as _sp

    task_name = "ToolRecall Daemon"
    toolrecall_bin = _shutil.which("toolrecall")
    if not toolrecall_bin:
        return False, "Windows autostart skipped: toolrecall binary not found"
    tr = f'"{toolrecall_bin}" daemon'
    try:
        r = _sp.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                task_name,
                "/TR",
                tr,
                "/SC",
                "ONLOGSTART",
                "/RL",
                "LIMITED",
                "/F",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode == 0:
            return True, f"Windows autostart: scheduled task '{task_name}' created (ONLOGSTART)"
        return False, f"Windows autostart failed: {(r.stderr or r.stdout).strip()[:200]}"
    except FileNotFoundError:
        return False, "Windows autostart skipped: schtasks not found"
    except Exception as e:
        return False, f"Windows autostart could not be installed: {e}"


def cmd_setup():
    """One-shot setup: init config → install autostart service → ensure daemon + shim."""
    import os

    print("=" * 56)
    print("  ToolRecall Setup — one-time installation")
    print("=" * 56)
    print()

    steps_ok = []
    errors = []

    # ─── 1. Config / init ───────────────────────────
    cfg_path = os.path.expanduser("~/.config/toolrecall/toolrecall.toml")
    if not os.path.exists(cfg_path):
        print("📄 No config found — running 'toolrecall init'...")
        cmd_init()
        print()
    else:
        steps_ok.append("config: found")

    # ─── 2. Autostart service (OS-specific) ────────
    if _host_os() == "macos":
        ok, msg = _install_macos_launch_agent()
        (steps_ok if ok else errors).append(msg)
    elif _host_os() == "linux":
        import subprocess

        try:
            systemd_dir = os.path.expanduser("~/.config/systemd/user")
            service_path = os.path.join(systemd_dir, "toolrecall-daemon.service")
            os.makedirs(systemd_dir, exist_ok=True)

            toolrecall_bin = os.path.expanduser("~/.local/bin/toolrecall")
            if not os.path.exists(toolrecall_bin):
                import shutil

                toolrecall_bin = shutil.which("toolrecall") or toolrecall_bin
            service_content = SYSTEMD_SERVICE_CONTENT % (toolrecall_bin + " daemon --foreground")
            with open(service_path, "w") as f:
                f.write(service_content)

            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10
            )
            enabled = subprocess.run(
                ["systemctl", "--user", "enable", "toolrecall-daemon"],
                capture_output=True,
                timeout=10,
            )
            if enabled.returncode == 0:
                steps_ok.append("systemd service: written + enabled")
            else:
                steps_ok.append(
                    "systemd service: written (enable failed — run "
                    "`systemctl --user enable toolrecall-daemon`)"
                )
        except FileNotFoundError:
            # No systemctl on this Linux (e.g. minimal container) — daemon
            # still starts via _ensure_daemon's direct-launch fallback.
            steps_ok.append(
                "systemd service: written (systemctl not found — daemon runs via direct launch)"
            )
    else:  # windows
        ok, msg = _install_windows_scheduled_task()
        (steps_ok if ok else errors).append(msg)

    # ─── 3. Daemon (auto-start) ────────────
    if _ensure_daemon():
        steps_ok.append("daemon: running")
    else:
        errors.append("daemon could not be started")

    # ─── 4. Agent integration ────────────────
    agents = _ensure_agent_integration()
    found_agents = [k for k, v in agents.items() if v]
    if found_agents:
        steps_ok.append(f"agent integration: {', '.join(found_agents)}")
    else:
        steps_ok.append("agent integration: none detected")

    # ─── 5. API proxy attachment (billed-token savings path) ────
    # Only automatic on explicit opt-in (--yes) or interactive yes: pointing
    # an agent's LLM traffic at the proxy changes its failure mode, so this
    # is gated AND health-checked (never attach to a dead proxy).
    import shutil as _shutil_step5

    if _shutil_step5.which("hermes"):
        yes = "--yes" in sys.argv
        # isatty too: without it a scripted/CI run printed an interactive
        # question and then hit the prompt's OSError (2026-09-28). The env
        # var remains the explicit opt-out; this makes the flag honest.
        interactive = "TOOLRECALL_NONINTERACTIVE" not in os.environ and _stdin_is_interactive()
        if yes:
            ok, msg = _attach_hermes_to_proxy()
            (steps_ok if ok else errors).append(f"API proxy: {msg}")
        elif interactive:
            print()
            print("  Attach the detected agent's LLM traffic to the ToolRecall API proxy?")
            print("  This is what enables billed-token savings (api_cache replay")
            print("  + usage capture). Proxy is health-checked; a backup of your")
            print("  config is kept; detach anytime: toolrecall proxy detach")
            if _confirm_install(None):
                ok, msg = _attach_hermes_to_proxy()
                (steps_ok if ok else errors).append(f"API proxy: {msg}")
            else:
                steps_ok.append("API proxy: not attached (opt-in later: toolrecall proxy attach)")
        else:
            steps_ok.append(
                "API proxy: not attached (non-interactive; use --yes or toolrecall proxy attach)"
            )
    else:
        steps_ok.append("API proxy: Hermes not detected — skipped")

    # ─── Summary ──────────────────────────────────
    print()
    for msg in steps_ok:
        print(f"  ✅ {msg}")
    for msg in errors:
        print(f"  ❌ {msg}")
    print()
    print("=" * 56)
    if errors:
        print(f"  ⚠️  Setup finished with {len(errors)} issue(s)")
    else:
        print("  ✅ Setup complete — ToolRecall is ready")
    if not found_agents:
        print()
        print(_MANUAL_AGENT_GUIDE)
    else:
        print()
        print("  ℹ️  ToolRecall MCP tools available on next agent restart")
        print("     Agents must be told to use cached_read/cached_terminal over native tools")
        print("     (ToolRecall instruction snippets appended to agent configs where applicable)")
        print()
        print("  📦 Cached terminal commands (read-only):")
        print("     System: hostname(3600), whoami(3600), pwd(3600), uptime(300), uname(3600)")
        print("     Files:  ls(60), cat(30), head(30), tail(30), wc(30)")
        print("     Search: grep(60), rg(60), find(60), fd(60)")
        print("     Git:    status(30), diff(30), log(30), branch(300), stash(300)")
        print(
            "     Env:    which(3600), python3 --version(3600), pip list(600), node --version(3600)"
        )
        print("     Disk:   du(120), df(120)")
        print()
        print("  ⚠️  Security: only read-only commands are cached. Dangerous commands")
        print("     (rm, sudo, mv, git push, kill) are NEVER cached. Use ttl=0 to bypass.")
    print("=" * 56)


def _ensure_agent_integration():
    """Auto-detect supported agents and wire up ToolRecall access.

    Python agents with their own venv (Hermes, Codex, OpenCode) get the
    transparent cache via a ``.pth`` shim dropped into that venv's
    site-packages. This is **opt-in and default-disabled**: installing a shim
    patches open()/subprocess for EVERY python process in the venv, so we only
    do it after an explicit yes (or ``--yes`` / ``TOOLRECALL_NONINTERACTIVE``
    with ``--yes``). The ✅ is only printed after a probe confirms
    ``import toolrecall.shim`` works in that venv from a neutral cwd.

    Non-Python agents use the MCP bridge instead:
      - Claude Code: prefers `claude mcp add` (automatic), falls back to
        writing ~/.claude.json directly.
      - OpenCode/Crush: writes ~/.opencode/opencode.jsonc.

    Returns dict with keys: 'hermes', 'opencode', 'antigravity', 'claude', 'cline', 'cursor' — bool per agent.
    """
    import os
    import subprocess
    import json
    import shutil

    result = {}

    # ─── Hermes Agent (and any Python agent in its own venv) ───────────────
    # The OS-level .pth shim is agent-agnostic; detection is just "is there a
    # Python env we could shim?". We only auto-install into the Hermes venv on
    # explicit opt-in. This REPLACES the old behavior that printed an
    # unverified "✅ active" just because the `hermes` binary existed on PATH.
    from toolrecall import venvs as venv_mod

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        # Locate the Hermes venv via generic discovery (by venv containing the
        # hermes launcher, or by matching the binary's own environment).
        target = None
        for v in venv_mod.discover_python_venvs():
            launcher_bin = os.path.join(os.path.dirname(v.python), "hermes")
            if os.path.isfile(launcher_bin):
                target = v
                break
        if target is None:
            # Fall back to the current interpreter if it has a `hermes` launcher
            cur_l = os.path.join(os.path.dirname(sys.executable), "hermes")
            if os.path.isfile(cur_l):
                for p in sys.path:
                    if p.endswith("site-packages") and os.path.isdir(p):
                        target = venv_mod.Venv(
                            root=os.path.dirname(os.path.dirname(p)),
                            python=sys.executable,
                            site_packages=p,
                        )
                        break

        if target is not None:
            yes = "--yes" in sys.argv
            interactive = "TOOLRECALL_NONINTERACTIVE" not in os.environ
            if yes:
                # Explicit --yes → auto-install with verification
                done = venv_mod.ensure_shim(target)
                result["hermes"] = done
                if done:
                    print(
                        f"  ✅ Hermes transparent cache active in {target.root} "
                        "(verified: import toolrecall.shim probe passed)"
                    )
                else:
                    print(f"  ⚠️  Hermes shim install FAILED in {target.root}")
                    print(
                        "     `toolrecall` may not be installed in that venv "
                        "or the venv is read-only."
                    )
            elif interactive:
                # Interactive without --yes → show opt-in prompt (default N)
                if not _confirm_install(target):
                    print("  ℹ️  Hermes detected but shim not enabled (opt-in, default off).")
                    result["hermes"] = False
                else:
                    done = venv_mod.ensure_shim(target)
                    result["hermes"] = done
                    if done:
                        print(
                            f"  ✅ Hermes shim active in {target.root} "
                            "(verified: import toolrecall.shim OK)"
                        )
                    else:
                        print(f"  ⚠️  Hermes shim install FAILED in {target.root}")
                        print(
                            "     `toolrecall` may not be installed in that venv "
                            "or the venv is read-only."
                        )
            else:
                # Non-interactive without --yes (e.g. `setup` in CI) → leave off
                result["hermes"] = False
        else:
            result["hermes"] = False

    # ─── OpenCode / Crush ────────────────────────────────────────────────
    OC_DIR = os.path.expanduser("~/.opencode")
    if os.path.isdir(OC_DIR):
        oc_config_path = os.path.join(OC_DIR, "opencode.jsonc")
        config = _prepare_opencode_config(oc_config_path)
        with open(oc_config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"  ✅ OpenCode MCP config written (automatic): {oc_config_path}")
        result["opencode"] = True
    else:
        result["opencode"] = False

    # ─── Google Antigravity (2.0 / IDE / CLI) ───────────────────────────
    # All Antigravity surfaces read ~/.gemini/config/mcp_config.json (global)
    # or .agents/mcp_config.json (workspace). We write the global file —
    # workspace files belong to the project. Merge-preserving: existing
    # servers stay untouched, only the "toolrecall" entry is added.
    ag_home = os.path.expanduser("~/.gemini")
    if os.path.isdir(ag_home):
        ag_config_path = os.path.join(ag_home, "config", "mcp_config.json")
        wrote, warn = _prepare_antigravity_config(ag_config_path)
        if wrote:
            print(f"  ✅ Antigravity MCP config written (automatic): {ag_config_path}")
            result["antigravity"] = True
            _print_antigravity_notice()
        else:
            print(f"  ⚠️  Antigravity MCP config NOT written: {warn}")
            result["antigravity"] = False
    else:
        result["antigravity"] = False

    # ─── Claude Code ────────────────────────────────
    claude_bin = shutil.which("claude")
    # Initialize unconditionally: when the `claude` binary is absent but a
    # ~/.claude.json fallback fires, this variable must still exist below.
    # Default to multiplexer-only: file caching via MCP was tested on Claude
    # Code and showed a 2.4× cost increase (Claude Code manages its own context).
    claude_multiplexer_only = True

    if claude_bin:
        # Prompt user: offer full bridge mode only if explicitly desired.
        if "--yes" not in sys.argv and "TOOLRECALL_NONINTERACTIVE" not in os.environ:
            print()
            print("  ℹ️  Claude Code detected:")
            print(
                "     ToolRecall's MCP multiplexer pools MCP servers host-wide, cutting RAM by 70%."
            )
            print(
                "     (Note: Full MCP file caching increases cost on Claude Code by 2.4× and is not recommended)."
            )
            print()
            print("  Options:")
            print(
                "    m) Multiplexer-only — host-wide MCP pooling, no file stubs (recommended, default)"
            )
            print(
                "    f) Full bridge — file cache + multiplexer (warning: 2.4× cost increase tested)"
            )
            print()
            try:
                claude_choice = input("  Install [M/f]: ").strip().lower()
                if claude_choice == "f":
                    claude_multiplexer_only = False
            except (EOFError, OSError, KeyboardInterrupt):
                pass  # Non-interactive (or non-tty stdin) → default to multiplexer-only
        # Automatic: uses Claude's own CLI to register the MCP server
        mcp_args = ["toolrecall", "mcp"]
        if claude_multiplexer_only:
            mcp_args.append("--multiplexer-only")
        try:
            r = subprocess.run(
                [claude_bin, "mcp", "add", "toolrecall", "-s", "user", "--"] + mcp_args,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.returncode == 0:
                label = "multiplexer-only" if claude_multiplexer_only else "full"
                print(f"  ✅ Claude Code MCP server registered ({label} — via 'claude mcp add')")
                result["claude"] = True
            else:
                print(
                    f"  ⚠️  'claude mcp add' returned exit {r.returncode} — falling back to direct config"
                )
                if r.stderr.strip():
                    print(f"     stderr: {r.stderr.strip()[:200]}")
                _write_claude_json_config(multiplexer_only=claude_multiplexer_only)
                result["claude"] = True
        except FileNotFoundError:
            _write_claude_json_config(multiplexer_only=claude_multiplexer_only)
            result["claude"] = True
        except subprocess.TimeoutExpired:
            print("  ⚠️  'claude mcp add' timed out — falling back to direct config")
            _write_claude_json_config(multiplexer_only=claude_multiplexer_only)
            result["claude"] = True
    else:
        # Fallback: detect via ~/.claude.json
        claude_json = os.path.expanduser("~/.claude.json")
        if os.path.exists(claude_json):
            _write_claude_json_config(multiplexer_only=claude_multiplexer_only)
            label = "multiplexer-only" if claude_multiplexer_only else "full"
            print(f"  ✅ Claude Code MCP config written ({label}, automatic): {claude_json}")
            result["claude"] = True

    # Instruction snippet: tell Claude to prefer cached tools (only for full mode)
    claude_dotdir = os.path.expanduser("~/.claude/claude_dotfiles")
    claude_md = os.path.join(claude_dotdir, "claude.md")
    if result.get("claude") and not claude_multiplexer_only and not os.path.exists(claude_md):
        os.makedirs(claude_dotdir, exist_ok=True)
        try:
            with open(claude_md, "w") as f:
                f.write(_CLAUDE_INSTRUCTION_SNIPPET)
            print(f"  ✅ Claude Code instruction snippet written (automatic): {claude_md}")
        except (OSError, IOError) as e:
            print(f"  ⚠️  Could not write instruction snippet: {e}")
    elif result.get("claude") and not claude_multiplexer_only and os.path.exists(claude_md):
        try:
            with open(claude_md) as f:
                content = f.read()
                if "ToolRecall" not in content:
                    with open(claude_md, "a") as f2:
                        f2.write(_CLAUDE_INSTRUCTION_SNIPPET)
                    print(f"  ✅ Appended ToolRecall instruction to {claude_md}")
        except (OSError, IOError) as e:
            print(f"  ⚠️  Could not check/update instruction snippet: {e}")

    # ─── Cline ───────────────────────────────────────────────────────────
    cline_bin = shutil.which("cline")
    cline_dir = os.path.expanduser("~/.config/cline")
    cline_rules = os.path.expanduser("~/.clinerules")
    vscode_cline_dir = os.path.expanduser(
        "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings"
    )

    cline_target = None
    if os.path.isdir(vscode_cline_dir):
        cline_target = os.path.join(vscode_cline_dir, "cline_mcp_settings.json")
    elif os.path.isdir(cline_dir) or os.path.isfile(cline_rules) or cline_bin:
        cline_target = os.path.join(cline_dir, "mcp_settings.json")

    if cline_target:
        wrote, info = _prepare_cline_config(cline_target)
        if wrote:
            print(f"  ✅ Cline MCP config written (automatic): {cline_target}")
            result["cline"] = True
        else:
            print(f"  ⚠️  Cline MCP config NOT written: {info}")
            result["cline"] = False
    else:
        result["cline"] = False

    # ─── Cursor ──────────────────────────────────────────────────────────
    cursor_bin = shutil.which("cursor")
    cursor_dir = os.path.expanduser("~/.cursor")
    cursor_config_dir = os.path.expanduser("~/.config/Cursor")

    if cursor_bin or os.path.isdir(cursor_dir) or os.path.isdir(cursor_config_dir):
        cursor_target = os.path.join(cursor_dir, "mcp.json")
        wrote, info = _prepare_cursor_config(cursor_target)
        if wrote:
            print(f"  ✅ Cursor MCP config written (multiplexer-only, automatic): {cursor_target}")
            result["cursor"] = True
        else:
            print(f"  ⚠️  Cursor MCP config NOT written: {info}")
            result["cursor"] = False
    else:
        result["cursor"] = False

    return result


_CLAUDE_INSTRUCTION_SNIPPET = """
## ToolRecall Cache
ToolRecall is installed. When reading files, use `cached_read` via MCP instead of `read_file`.
When running terminal commands, use `cached_terminal` instead of `terminal`.
"""


def _proxy_alive(timeout: float = 3.0) -> bool:
    """True when the forward proxy answers on 127.0.0.1:8569.

    Gate for the provider base_url rewrite: we never point an agent's LLM
    traffic at a dead proxy. Any response (even 401 from upstream auth)
    proves the proxy is listening and routing; only connection errors
    count as down.
    """
    import urllib.error
    import urllib.request

    port = os.environ.get("TOOLRECALL_FORWARD_PORT", "8569")
    try:
        # nosemgrep: dynamic-urllib-use-detected — loopback-only probe of our own
        # proxy; port from env, no user URL
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout)  # nosemgrep
        return True
    except urllib.error.HTTPError:
        return True  # proxy answered (upstream auth error) — it's alive
    except Exception:
        return False


def _attach_hermes_to_proxy() -> tuple[bool, str]:
    """Point the detected agent's LLM provider at the ToolRecall proxy.

    Agent-agnostic target resolution (first existing wins):
      1. TOOLRECALL_PROXY_TARGET env — explicit config path
      2. $AGENT_HOME/config.yaml (any agent honoring AGENT_HOME; Hermes
         sets it via its systemd unit)
      3. ~/.hermes/config.yaml (Hermes default)
    The `model:`/`llm:` section base_url line-edit works for any
    OpenAI-compatible YAML config with that shape.

    Writes a <config>.toolrecall-bak backup; `toolrecall proxy detach`
    restores it. Inserts a base_url line when the section carries none.
    Refuses when: no config found, no model:/llm: section to insert into,
    base_url already proxied, or proxy unreachable. Returns (ok, message).
    """
    explicit = os.environ.get("TOOLRECALL_PROXY_TARGET")
    candidates = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    agent_home = os.environ.get("AGENT_HOME") or os.environ.get("TOOLRECALL_AGENT_HOME")
    if agent_home:
        candidates.append(os.path.join(os.path.expanduser(agent_home), "config.yaml"))
    candidates.append(os.path.expanduser("~/.hermes/config.yaml"))
    cfg_path = next((c for c in candidates if os.path.isfile(c)), None)
    if cfg_path is None:
        return False, (
            "no agent config found (tried: "
            + ", ".join(candidates)
            + ") — for other clients set OPENAI_BASE_URL=http://127.0.0.1:"
            + os.environ.get("TOOLRECALL_FORWARD_PORT", "8569")
            + "/v1 manually"
        )

    port = os.environ.get("TOOLRECALL_FORWARD_PORT", "8569")
    proxy_base = f"http://127.0.0.1:{port}/v1"

    with open(cfg_path) as f:
        lines = f.readlines()

    # locate the primary LLM section's base_url line. Hermes uses `model:`
    # as the top-level key (with llm: as legacy alias); also match `llm:`
    # for older configs. The FIRST matching top-level section wins.
    section_keys = ("model:", "llm:")
    in_llm = False
    section_idx = None
    target_idx = None
    current_url = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(line.startswith(k) for k in section_keys) and stripped.endswith(":"):
            if section_idx is None:
                section_idx = i
            in_llm = True
            continue
        if in_llm and line and not line[0].isspace():  # next top-level key
            in_llm = False
            continue
        if in_llm and stripped.startswith("base_url:"):
            target_idx = i
            current_url = stripped.split(":", 1)[1].strip().strip("'\"")
            break

    inserted = False
    if target_idx is None:
        # The LLM section exists but carries no base_url line. A partial
        # revert or a hand edit leaves configs in exactly that state, and a
        # missing line means "not proxied yet" — not "config broken". Insert
        # one as the section's first key instead of refusing.
        if section_idx is None:
            return False, (
                "no model:/llm: section in Hermes config — cannot insert a "
                "base_url line; set OPENAI_BASE_URL=http://127.0.0.1:" + port + "/v1 manually"
            )
        indent = "  "
        for line in lines[section_idx + 1 :]:
            if not line.strip():  # blank line — keep looking for a child key
                continue
            if not line[0].isspace():  # next top-level key: section has no child
                break
            indent = line[: len(line) - len(line.lstrip())]
            break
        target_idx = section_idx + 1
        lines.insert(
            target_idx,
            f"{indent}base_url: {proxy_base}  # toolrecall proxy (detach: toolrecall proxy detach)\n",
        )
        inserted = True
    elif "127.0.0.1" in current_url or "localhost" in current_url:
        return True, f"Hermes already points at a local proxy ({current_url})"

    if not _proxy_alive():
        return False, (
            f"forward proxy not reachable on :{port} — refusing to redirect "
            "LLM traffic to a dead proxy (start the daemon first)"
        )

    backup = cfg_path + ".toolrecall-bak"
    if not os.path.exists(backup):
        import shutil

        shutil.copy2(cfg_path, backup)

    if not inserted:
        indent = lines[target_idx][: len(lines[target_idx]) - len(lines[target_idx].lstrip())]
        lines[target_idx] = (
            f"{indent}base_url: {proxy_base}  # toolrecall proxy (detach: toolrecall proxy detach; was {current_url})\n"
        )
    with open(cfg_path, "w") as f:
        f.writelines(lines)
    return True, (
        f"agent llm.base_url → {proxy_base} "
        f"({'inserted' if inserted else 'updated'}; config: {cfg_path}; backup: {backup})"
    )


def _detach_hermes_from_proxy() -> int:
    """Restore the pre-proxy agent config (reversal of _attach).

    Searches the same candidate list as attach for a config with a
    .toolrecall-bak sibling (agent-agnostic).
    """
    candidates = []
    explicit = os.environ.get("TOOLRECALL_PROXY_TARGET")
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    agent_home = os.environ.get("AGENT_HOME") or os.environ.get("TOOLRECALL_AGENT_HOME")
    if agent_home:
        candidates.append(os.path.join(os.path.expanduser(agent_home), "config.yaml"))
    candidates.append(os.path.expanduser("~/.hermes/config.yaml"))
    for cfg_path in candidates:
        backup = cfg_path + ".toolrecall-bak"
        if os.path.exists(backup):
            import shutil

            shutil.copy2(backup, cfg_path)
            os.remove(backup)
            print(f"✅ restored {cfg_path} from backup — agent no longer uses the proxy")
            print("   restart the agent to pick up the change")
            return 0
    print("no backup found (searched: " + ", ".join(candidates) + ")")
    print("nothing to detach — or already detached")
    return 1


_MANUAL_AGENT_GUIDE = """
No supported agent detected on this machine.

ToolRecall works with any AI coding agent via MCP. To integrate manually:

  Hermes Agent:
    toolrecall shim --install
    → OS-level .pth shim patches open()/subprocess.run() in every Python process.
      No per-agent config needed.

  Claude Code:
    claude mcp add toolrecall -s user -- toolrecall mcp
    → Adds cached_read/cached_terminal as MCP tools

  OpenCode / Crush:
    Add to ~/.opencode/opencode.jsonc under "mcp" key:
    { "toolrecall": { "type": "local", "command": "toolrecall", "args": ["mcp"], "enabled": true } }

  Cursor:
    Add to .cursorrules: "Use cached_read for file reads, cached_terminal for terminal commands"

  Cline / Roo Code:
    Add to .clinerules: same instruction as Cursor

  Any MCP-compatible agent:
    Add an MCP server with command "toolrecall" and args ["mcp"]

After configuring your agent: make sure the ToolRecall daemon is running:
    toolrecall daemon
    toolrecall status
"""


def _write_claude_json_config(multiplexer_only: bool = True):
    """Write toolrecall MCP entry to ~/.claude.json.

    Reads existing config, merges toolrecall into mcpServers, writes back.
    Used as fallback when 'claude mcp add' CLI command is unavailable.
    Defaults to multiplexer_only=True to prevent 2.4× cost increase from file stubs.
    """
    import os
    import json

    claude_json = os.path.expanduser("~/.claude.json")
    config = {}
    if os.path.exists(claude_json):
        try:
            with open(claude_json) as f:
                config = json.load(f)
        except (json.JSONDecodeError, Exception):
            config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}
    args = ["mcp"]
    if multiplexer_only:
        args.append("--multiplexer-only")
    config["mcpServers"]["toolrecall"] = {
        "command": "toolrecall",
        "args": args,
    }

    with open(claude_json, "w") as f:
        json.dump(config, f, indent=2)


def _toolrecall_command_for_mcp() -> list[str]:
    """Command + args that launch the TR MCP bridge from an MCP client.

    Resolves the absolute path of the `toolrecall` executable — MCP servers
    spawned by GUI processes (Antigravity IDE on macOS/Windows especially)
    may not inherit a shell PATH where pipx/uv tool shims live. Absolute path
    keeps it working everywhere; falls back to the bare name if resolution
    fails (client-PATH lookup then applies).
    """
    import os as _os
    import shutil as _shutil

    # shutil.which only returns existing executables — trust it outright
    # (it runs in the user's shell at setup time, where pipx/uv shims are
    # on PATH; Antigravity then launches the recorded absolute path).
    resolved = _shutil.which("toolrecall")
    if resolved:
        return [resolved, "mcp"]
    fallback = _os.path.expanduser("~/.local/bin/toolrecall")
    for candidate in (fallback, fallback + ".exe", fallback + ".cmd", fallback + ".bat"):
        if _os.path.isfile(candidate):
            return [candidate, "mcp"]
    return ["toolrecall", "mcp"]


def _prepare_antigravity_config(config_path: str) -> tuple[bool, str]:
    """Read existing Antigravity mcp_config.json or create a fresh one, then
    merge the toolrecall MCP bridge entry into ``mcpServers``.

    Merge-preserving: user-defined servers in the file are kept verbatim
    (Antigravity schema: stdio uses command/args/env; remote uses serverUrl).
    Writes atomically (tmp file + os.replace) so a crash mid-write can't
    leave a truncated config that would break Antigravity startup.

    Returns:
        (wrote, info): wrote=True → file written/updated (info = "created" or
        "updated"); warn non-empty with wrote=False → reason skipped
        (unreadable/corrupt existing file, schema mismatch).
    """
    import json
    import os
    import tempfile

    config: dict = {}
    existing = False
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
            existing = True
        except (json.JSONDecodeError, OSError) as e:
            # Never clobber a config we cannot parse — user data protection.
            return False, f"existing config unreadable ({e}) — fix or remove it first"

    if not isinstance(config, dict):
        return False, "existing config is not a JSON object"
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return False, '"mcpServers" is not an object'

    cmd_line = _toolrecall_command_for_mcp()
    servers["toolrecall"] = {"command": cmd_line[0], "args": cmd_line[1:]}

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    # Atomic: tmp file in the same directory, then os.replace.
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(config_path), prefix=".mcp_config_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_err:
            # Best-effort temp cleanup — never masks the original write error
            # (re-raised below); logged so the swallow is visible, not silent.
            import logging

            logging.getLogger("toolrecall.cli").debug(
                "antigravity config tmp cleanup failed: %s", cleanup_err
            )
        raise
    return True, "updated" if existing else "created"


def _print_antigravity_notice() -> None:
    """One-time guidance after Antigravity MCP config write.

    Covers the rides-along capabilities the bridge unlocks (replay, FTS
    skills indexing, MCP multiplex opt-in) — the bridge entry alone is what
    was written; these are optional next steps, never auto-enabled.
    """
    print()
    print("  Antigravity + ToolRecall — next steps:")
    print("  • Restart Antigravity so it picks up the new MCP server.")
    print("  • SDK agents: decorate expensive custom tools —")
    print("      from toolrecall.adapters import antigravity")
    print("      @antigravity.cached_tool(ttl=300)")
    print("  • Record a session for offline replay:")
    print("      toolrecall replay record antigravity-session")
    print("  • Make workspace skills/rules searchable (docs_search):")
    print("      toolrecall index-dir ~/.gemini/skills --source antigravity-skills")
    print("  • Optional: fronted third-party MCP servers get transparent")
    print("    response caching — opt in via [mcp_multiplex] in the daemon")
    print("    config (set ttl = 0 on any server with write tools).")


def _prepare_cline_config(config_path: str) -> tuple[bool, str]:
    """Read existing Cline mcp_settings.json or create a fresh one, then
    merge the toolrecall MCP bridge entry into ``mcpServers``.

    Merge-preserving: user-defined servers in the file are kept verbatim.
    Writes atomically (tmp file + os.replace).

    Returns:
        (wrote, info): wrote=True -> "created" or "updated";
        wrote=False -> reason skipped.
    """
    import json
    import os
    import tempfile

    config: dict = {}
    existing = False
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
            existing = True
        except (json.JSONDecodeError, OSError) as e:
            return False, f"existing config unreadable ({e}) — fix or remove it first"

    if not isinstance(config, dict):
        return False, "existing config is not a JSON object"
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return False, '"mcpServers" is not an object'

    cmd_line = _toolrecall_command_for_mcp()
    servers["toolrecall"] = {"command": cmd_line[0], "args": cmd_line[1:]}

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(config_path), prefix=".cline_mcp_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True, "updated" if existing else "created"


def _prepare_cursor_config(config_path: str) -> tuple[bool, str]:
    """Read existing Cursor mcp.json or create a fresh one, then
    merge the toolrecall MCP bridge entry into ``mcpServers`` with --multiplexer-only.

    Cursor manages its own context aggressively; multiplexer-only provides
    host-wide MCP pooling without redundant file stubs.
    Merge-preserving: user-defined servers in the file are kept verbatim.
    Writes atomically (tmp file + os.replace).

    Returns:
        (wrote, info): wrote=True -> "created" or "updated";
        wrote=False -> reason skipped.
    """
    import json
    import os
    import tempfile

    config: dict = {}
    existing = False
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
            existing = True
        except (json.JSONDecodeError, OSError) as e:
            return False, f"existing config unreadable ({e}) — fix or remove it first"

    if not isinstance(config, dict):
        return False, "existing config is not a JSON object"
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return False, '"mcpServers" is not an object'

    cmd_line = _toolrecall_command_for_mcp()
    args = list(cmd_line[1:])
    if "--multiplexer-only" not in args:
        args.append("--multiplexer-only")
    servers["toolrecall"] = {"command": cmd_line[0], "args": args}

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(config_path), prefix=".cursor_mcp_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True, "updated" if existing else "created"


def _prepare_opencode_config(config_path):
    """Read existing opencode config or create fresh one with Crush format.

    Detects Crush (mcp key) vs classic opencode (mcpServers key) and
    writes the appropriate format.
    """
    import json

    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                content = f.read()
            import re

            content = re.sub(r"//.*$", "", content, flags=re.MULTILINE)
            content = re.sub(r",\s*([}\]])$", r"\1", content)
            config = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, Exception):
            config = {}

    is_crush = "mcp" in config and isinstance(config["mcp"], dict)

    toolrecall_entry = {
        "type": "local",
        "command": "toolrecall",
        "args": ["mcp"],
        "enabled": True,
    }

    if is_crush:
        config["mcp"]["toolrecall"] = toolrecall_entry
    else:
        if "mcpServers" not in config:
            config["mcpServers"] = {}
        config["mcpServers"]["toolrecall"] = {
            "command": "toolrecall",
            "args": ["mcp"],
        }
        config.pop("mcp", None)

    if is_crush:
        config.pop("$schema", None)
        config.setdefault("$schema", "https://opencode.ai/config.json")

    return config


def _revert_proxy_wiring_in_text(path_hint: str, text: str) -> str:
    """Strip the exact :8569 base-URL override lines ToolRecall wires.

    Non-destructive: only removes lines that point at localhost/127.0.0.1:8569,
    leaving everything else (including real-host overrides) intact.
    """
    out = []
    for line in text.splitlines(keepends=True):
        if (
            ":" in line
            and "8569" in line
            and ("OPENAI_BASE_URL" in line or "ANTHROPIC_BASE_URL" in line or "base_url" in line)
            and ("localhost" in line or "127.0.0.1" in line)
        ):
            continue  # drop the ToolRecall proxy override
        out.append(line)
    return "".join(out)


_PROXY_WIRING_FILES = [
    os.path.expanduser("~/.bashrc"),
    os.path.expanduser("~/.profile"),
    os.path.expanduser("~/.hermes/config.yaml"),
]


def _revert_proxy_wiring() -> list:
    """Revert agent base-URL pointing at the ToolRecall forward proxy.

    Returns list of human-readable changes made. Backs up each touched file it
    actually edits (timestamped sibling), so the revert is reversible.
    """
    import shutil as _shutil
    import time as _time

    changed = []
    for path in _PROXY_WIRING_FILES:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        new_text = _revert_proxy_wiring_in_text(path, text)
        if new_text == text:
            continue
        backup = f"{path}.toolrecall-bak.{_time.strftime('%Y%m%d-%H%M%S')}"
        _shutil.copy2(path, backup)
        with open(path, "w") as f:
            f.write(new_text)
        changed.append(f"{path}  (backup: {backup})")
    return changed


def cmd_stop():
    """Stop the ToolRecall daemon and revert agent proxy base-URL wiring."""
    from toolrecall.daemon import stop_daemon

    print("=" * 56)
    print("  ToolRecall Stop")
    print("=" * 56)
    stop_daemon()
    print()
    print("  Reverting forward-proxy base-URL wiring (agents → direct provider)...")
    changed = _revert_proxy_wiring()
    if changed:
        for c in changed:
            print(f"    ⚙️  {c}")
    else:
        print("    ℹ️  No :8569 base-URL overrides found — nothing to revert.")
    print()
    print("✅ ToolRecall stopped. Agents now call their providers directly.")
    print("   Restart with: toolrecall daemon")


def cmd_restart():
    """Health check + restart via systemd: config check → systemctl --user restart → verify.
    Auto-installs OS-level shim if not present."""
    if "--help" in sys.argv[2:] or "-h" in sys.argv[2:]:
        print("Usage: toolrecall restart")
        print()
        print("Health check + clean daemon restart:")
        print("  1. config check (allowed_paths, terminal, multiplexer)")
        print("  2. systemctl --user restart toolrecall-daemon")
        print("     (falls back to a direct daemon restart without systemd)")
        print("  3. wait for UDS readiness, print summary")
        print()
        print("Auto-installs the OS-level shim if missing. Takes no options.")
        print("Related: `toolrecall daemon --status`, `toolrecall healthcheck`.")
        return
    import os
    import subprocess
    import time

    print("=" * 56)
    print("  ToolRecall Restart — health check + systemd restart")
    print("=" * 56)
    print()

    # ─── 1. Config check ────────────────────────────
    print("🔍 Checking configuration...")
    cfg_path = os.path.expanduser("~/.config/toolrecall/toolrecall.toml")
    found = []
    errors = []

    if os.path.exists(cfg_path):
        found.append(f"config: {cfg_path}")
        from toolrecall.config import load_config

        cfg = load_config()
        allowed = cfg.mcp_allowed_paths
        if allowed:
            found.append(f"allowed_paths ({len(allowed)} dirs): {', '.join(allowed)}")
            for p in allowed:
                expanded = os.path.expanduser(p) if "~" in p else p
                if not os.path.isdir(expanded):
                    errors.append(f"allowed_path '{p}' → {expanded} does not exist")
        else:
            errors.append("allowed_paths is empty — all file reads blocked!")

        if cfg.mcp_allow_terminal:
            found.append("terminal: ENABLED")
        else:
            found.append("terminal: disabled (default)")

        if cfg.mcp_multiplex_enabled:
            servers = cfg.mcp_multiplex_servers
            found.append(f"MCP multiplexer: enabled ({len(servers)} servers)")
        else:
            found.append("MCP multiplexer: disabled")
    else:
        errors.append(
            f"config not found at {cfg_path} — run 'toolrecall setup' or 'toolrecall init'"
        )

    for msg in found:
        print(f"  ✅ {msg}")
    if errors:
        print()
        for msg in errors:
            print(f"  ❌ {msg}")
        print()

    # ─── 2. systemd restart ─────────────────────────
    # Windows (and systems without systemd) skip straight to the direct
    # fallback below. Previously the unguarded subprocess.run crashed with
    # FileNotFoundError on Windows before the fallback was reachable
    # (handover report 0.8.19, bug 3).
    import shutil

    use_systemd = _host_os() != "windows" and shutil.which("systemctl") is not None
    result = None
    if use_systemd:
        print("🔄 Restarting via systemd --user...")
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", "toolrecall-daemon"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            result = None

    if result is None or result.returncode != 0:
        if result is not None:
            print(f"  ⚠️  systemctl restart returned exit {result.returncode}")
            print("     (exit -15 = SIGTERM = daemon was killed; exit 3 = not running)")
            if result.stderr.strip() and result.returncode not in (-15, 3):
                for line in result.stderr.strip().split("\n"):
                    print(f"     {line}")
        print("  → Falling back to direct daemon restart...")
        print()

        # Fallback: start daemon directly
        from toolrecall.daemon import stop_daemon

        stop_daemon()
        if _ensure_daemon():
            print("  ✅ Daemon started via fallback")
        else:
            print("  ❌ Could not start daemon — try 'toolrecall daemon --foreground'")
            print()
            print("=" * 56)
            return

    print("  ✅ systemd restart issued successfully")

    # ─── 3. Wait for readiness ─────────────────────
    from toolrecall.transport import TransportClient, DEFAULT_PATH

    print("  Waiting for daemon to accept connections...")
    for attempt in range(10):
        time.sleep(0.5)
        try:
            tc = TransportClient(DEFAULT_PATH)
            resp = tc.send({"cmd": "ping"})
            if resp.get("pong"):
                print(
                    f"  ✅ Daemon ready (PID {resp.get('pid')}) — connected (attempt {attempt + 1})"
                )
                break
        except Exception:
            continue
    else:
        print(
            "  ⚠️  Daemon started but not responding after 5s — check 'toolrecall daemon --status'"
        )

    # ─── 4. Summary ─────────────────────────────
    print()
    print("=" * 56)
    if errors:
        print(f"  ⚠️  Restarted with {len(errors)} config issue(s) to fix")
        for msg in errors:
            print(f"     ❌ {msg}")
        print()
        print("  Fix config issues above, then run 'toolrecall restart' again.")
    else:
        print("  ✅ Restart complete — everything looks good")
    print("=" * 56)


def cmd_logs():
    """Tail/filter the rotating ToolRecall log (payload-free, redacted)."""
    import argparse as _ap
    from pathlib import Path

    parser = _ap.ArgumentParser(prog="toolrecall logs")
    parser.add_argument("--tail", "-n", type=int, default=50, help="Lines to show (default 50)")
    parser.add_argument(
        "--level",
        type=str,
        default=None,
        help="Minimum level: DEBUG/INFO/WARNING/ERROR (default: show all)",
    )
    parser.add_argument("--file", type=str, default=None, help="Log file override (rarely needed)")
    args = parser.parse_args(sys.argv[2:])

    log_file = args.file or os.path.expanduser("~/.toolrecall/logs/toolrecall.log")
    log_path = Path(log_file)
    if not log_path.exists():
        print(f"No log file at {log_path} — nothing logged yet (or logging disabled).")
        return

    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    min_rank = order.get((args.level or "").upper(), 0)
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = []
    for line in lines:
        rank = 0
        for name, val in order.items():
            if f" {name} " in line:
                rank = val
                break
        if rank >= min_rank:
            selected.append(line)
    out = selected[-args.tail :] if min_rank else lines[-args.tail :]
    print("\n".join(out) if out else "(no matching lines)")


def cmd_top():
    """`toolrecall top` — live TUI dashboard (curses, zero deps).

    Read-only: per-cache hit rates + token accounting + access-log tail.
    Talks to the daemon over UDS; falls back to read-only SQLite when the
    daemon is down. Non-tty runs print a single plain-text snapshot.
    """
    from toolrecall.tui_top import main as tui_main

    sys.exit(tui_main(sys.argv[2:]))


def cmd_healthcheck():
    """Run the generalized daemon/cache healthcheck.

    Local-only: reuses ToolRecall's own transport/pid/lock paths. Flags:
      --json         machine-readable JSON object (health + warnings)
      --clean-stale  first remove a provably dead daemon.pid and orphaned
                     daemon-*.lck files (flock-verified), then report health

    Exit code:
      0  healthy
      1  daemon down or abnormal pid/lock/socket state
      2  hard failure (e.g. healthcheck module import error) or bad usage
    """
    args = sys.argv[2:]
    unknown = [a for a in args if a not in ("--json", "--clean-stale")]
    if unknown:
        print(f"toolrecall healthcheck: unknown argument(s): {' '.join(unknown)}", file=sys.stderr)
        print("  usage: toolrecall healthcheck [--json] [--clean-stale]", file=sys.stderr)
        sys.exit(2)
    try:
        from toolrecall import healthcheck as _hc

        code = _hc.run(as_json="--json" in args, clean_stale="--clean-stale" in args)
    except Exception as e:  # noqa: BLE001  (surface a hard failure as exit 2)
        print(f"toolrecall healthcheck error: {e}")
        code = 2
    sys.exit(code)


def cmd_context():
    """Context tracker inspection: `toolrecall context <status|stale>`.

    Local-only: talks to the daemon over the UDS. No network, no deps.
    """
    import json as _json
    from toolrecall import client

    args = sys.argv[2:]
    sub = args[0] if args else ""

    if sub in ("", "--help", "-h", "help"):
        print("Usage: toolrecall context <command>")
        print("")
        print("Commands:")
        print("  status              Checkpoint, dirty/clean/stale counts")
        print("  stale               Files read then overwritten (stale in context)")
        print("  compacted           Report a harness transcript compaction (call from a hook)")
        print("")
        print("Options for 'stale':")
        print("  --format json|table   Output format (default: table)")
        print("  --quiet, -q           Bare paths, one per line (pipeable)")
        print("")
        print("Exit codes: 0 = nothing stale, 1 = stale files found, 2 = daemon error")
        return

    def _call(fn):
        try:
            resp = fn()
        except Exception as e:
            print(f"  \u26a0\ufe0f  Could not reach daemon: {e}", file=sys.stderr)
            sys.exit(2)
        if isinstance(resp, dict) and resp.get("error"):
            print(f"  \u26a0\ufe0f  {resp['error']}", file=sys.stderr)
            sys.exit(2)
        return resp

    if sub == "compacted":
        resp = _call(client.context_compacted)
        print(
            f"  recorded transcript compaction (epoch {resp.get('compaction_epoch', '?')}); "
            "bridges re-send full content on their next read"
        )
        return

    if sub == "status":
        stats = _call(client.context_get_stats)
        stale = _call(client.context_get_stale)
        print("=" * 50)
        print("  Context Tracker")
        print("=" * 50)
        print(f"  checkpoint:          {stats.get('checkpoint', 0)}")
        print(f"  files read:          {stats.get('total_read', 0)}")
        print(f"  dirty (written):     {stats.get('total_dirty', 0)}")
        print(f"  clean (droppable):   {stats.get('total_clean', 0)}")
        print(f"  stale (WRONG):       {stale.get('total_stale', 0)}")
        print(f"  reclaimable:         {stale.get('est_reclaimable_tokens', 0):,} tokens")
        print(f"  dropped (cumulative):{stats.get('ctx_dropped_tokens_total', 0):,} tokens")
        return

    if sub == "stale":
        fmt = "table"
        if "--format" in args:
            i = args.index("--format")
            if i + 1 < len(args):
                fmt = args[i + 1]
        if "--quiet" in args or "-q" in args:
            fmt = "quiet"

        data = _call(client.context_get_stale)
        stale = data.get("stale", [])

        if fmt == "json":
            print(_json.dumps(data, indent=2))
        elif fmt == "quiet":
            for e in stale:
                print(e["path"])
        elif not stale:
            print("  \u2705 No stale files \u2014 every file you read is current on disk.")
        else:
            total = data.get("est_reclaimable_tokens", 0)
            print(f"  {len(stale)} stale file(s), ~{total:,} reclaimable tokens:")
            print("")
            for n, e in enumerate(stale, 1):
                print(f"  {n:>3}. {e['path']}")
                print(
                    f"       read at op {e['read_seq']}, overwritten at op "
                    f"{e['write_seq']} \u2014 ~{e['est_tokens']:,} tok"
                )
            print("")
            print("  These copies in your context are out of date. Evict or re-read.")

        sys.exit(1 if stale else 0)

    if sub == "recall":
        rsub = args[1] if len(args) > 1 else ""
        if rsub in ("", "--help", "-h", "help"):
            print("Usage: toolrecall context recall <command>")
            print("")
            print("Commands:")
            print("  store <fingerprint> [content_type]   Persist stdin content, print node_id")
            print("  get   <node_id>                      Restore a stored block")
            print(
                "  status                               Count + tokens in recall cache (if enabled)"
            )
            print("")
            print("Options for 'store':")
            print("  --reproducible    Mark block as deterministically re-fetchable")
            print('  --summary "..."    Optional short semantic pointer')
            print("Options for 'get':")
            print("  --json            Print the full entry as JSON")
            return

        if rsub == "store":
            if len(args) < 3:
                print(
                    "Usage: toolrecall context recall store <fingerprint> [content_type]",
                    file=sys.stderr,
                )
                sys.exit(2)
            fingerprint = args[2]
            content_type = args[3] if len(args) > 3 else "other"
            reproducible = "--reproducible" in args
            summary = ""
            if "--summary" in args:
                i = args.index("--summary")
                if i + 1 < len(args):
                    summary = args[i + 1]
            content = sys.stdin.read()
            if not content:
                print(
                    "  \u26a0\ufe0f  No input on stdin — pipe the content to persist.",
                    file=sys.stderr,
                )
                sys.exit(2)
            resp = _call(
                lambda: client.recall_store(
                    fingerprint=fingerprint,
                    content=content,
                    content_type=content_type,
                    reproducible=reproducible,
                    summary=summary,
                )
            )
            print(resp.get("node_id", ""))
            return

        if rsub == "get":
            if len(args) < 3:
                print("Usage: toolrecall context recall get <node_id>", file=sys.stderr)
                sys.exit(2)
            node_id_ = args[2]
            resp = _call(lambda: client.recall_get(node_id_))
            entry = resp.get("entry")
            if entry is None:
                print(f"  \u274c  No entry for node_id: {node_id_}", file=sys.stderr)
                sys.exit(1)
            if "--json" in args:
                print(_json.dumps(entry, indent=2))
                return
            if entry.get("summary"):
                print(f"# {entry['summary']}")
            print(entry.get("content", ""))
            return

        if rsub == "status":
            resp = _call(client.recall_stats)
            print(f"  recall entries:   {resp.get('total', 0)}")
            print(f"  persisted tokens: {resp.get('tokens', 0):,}")
            return

        print(f"Unknown recall command: {rsub}")
        print("Available: store, get, status")
        sys.exit(2)

    print(f"Unknown context command: {sub}")
    print("Available: status, stale, recall")
    sys.exit(2)


def cmd_update():
    """`toolrecall update` — check PyPI, apply upgrade; --check = no apply;
    --status = show updater state without any network activity."""
    from toolrecall import updater

    args = sys.argv[2:]
    if "--help" in args or "-h" in args:
        print("Usage: toolrecall update [--check] [--status]")
        print()
        print("Check PyPI for a newer toolrecall and apply the upgrade.")
        print("A background update check also runs at CLI entry. By default a")
        print("newer release is ANNOUNCED (terminal + agent tool-output hint),")
        print("never installed silently — set [update] auto_apply = true (or")
        print("TOOLRECALL_UPDATE_AUTO_APPLY=1) to restore install-on-detect.")
        print()
        print("Options:")
        print("  --check, -c     Check only — report newer version, do not apply")
        print("  --status, -s    Show updater state (last check/apply, failures) — no network")
        return
    if "--status" in args or "-s" in args:
        updater.run_status_cli()
        return
    force = "--check" in args or "-c" in args
    updater.run_update_cli(force_check=force, apply=not force)


def _maybe_auto_update():
    """Opportunistic update check at CLI entry (opt-out; see [update] config).

    Never raises, never blocks >1s (PyPI timeout), never runs for the
    daemon/serve paths. Failures are recorded in the updater state file —
    surfaced via `toolrecall update --status`, not swallowed silently.

    Install behavior: [update] auto_apply (default FALSE, review 2026-09 —
    a compromised PyPI account must not mean silent code execution on the
    next CLI run). When False, prints the available version and records it
    in the state file, where the agent-facing hint channel
    (updater.pending_update_hint) picks it up. Installing is then an
    explicit `toolrecall update`.
    """
    # Never in these paths: they are long-lived processes or latency-critical.
    if sys.argv[1:2] in (["daemon"], ["serve"], ["mcp"], ["debug"], ["replay"]):
        return
    # Explicit update/healthcheck commands handle their own state.
    if sys.argv[1:2] in (["update"], ["healthcheck"]):
        return
    try:
        from toolrecall import updater
        from toolrecall import config as _config_mod

        cfg = _config_mod.load_config()
        # Explicit gate before any state I/O: disabled = fully inert.
        if not updater._update_enabled(cfg):
            return
        latest = updater.check_for_update(cfg)
        if latest:
            if cfg.update_auto_apply:
                print(
                    f"  ⬆️  ToolRecall {latest} available — updating "
                    f"(disable: [update] auto_apply = false)"
                )
                if updater.apply_update(latest):
                    print("  ✅ Updated — run `toolrecall restart` to refresh the daemon.")
                else:
                    print("  ⚠️  Auto-update failed — run `toolrecall update` for details.")
            else:
                print(
                    f"  ⬆️  ToolRecall {latest} available — run `toolrecall update` to install "
                    f"(auto-apply is off: [update] auto_apply = true to change)."
                )
    except Exception as e:  # noqa: BLE001 — updater must never break the command
        # Recorded degradation, not a silent swallow: state file + stderr note.
        import warnings

        warnings.warn(f"ToolRecall auto-update check skipped: {type(e).__name__}: {e}")


def cmd_hook():
    """Agent lifecycle hook dispatch."""
    args = sys.argv[2:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: toolrecall hook <target> [options]")
        print("")
        print("Targets:")
        print("  claude          Claude Code hook adapter (stdin JSON or print-config)")
        print("")
        print("Examples:")
        print("  toolrecall hook claude print-config")
        print(
            "  toolrecall hook claude ci-env [--port 8569]   # shell env for repeated headless runs"
        )
        return
    target = args[0]
    if target == "claude":
        from toolrecall.adapters import claude_code

        sys.exit(claude_code.main(args[1:]))
    else:
        print(f"Unknown hook target: {target}")
        sys.exit(1)


def cmd_keytrace():
    """Explain forward-proxy replay misses from a TOOLRECALL_KEY_TRACE_DIR trace."""
    from toolrecall import keytrace

    sys.exit(keytrace.main(sys.argv[2:]))


def cmd_audit():
    """Read-only token audits of agent session transcripts."""
    args = sys.argv[2:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: toolrecall audit <target> [options]")
        print("")
        print("Targets:")
        print(
            "  claude-code     Where Claude Code session tokens go (starting context, parts, shares)"
        )
        print("")
        print("Examples:")
        print("  toolrecall audit claude-code --days 30")
        return
    if args[0] == "claude-code":
        from toolrecall.adapters import claude_code_audit

        sys.exit(claude_code_audit.main(args[1:]))
    print(f"Unknown audit target: {args[0]}")
    sys.exit(1)


def main():
    if sys.platform == "win32":
        # Windows consoles default to legacy code pages (cp1252) that cannot
        # encode the CLI's emoji/box-drawing output — reconfigure to UTF-8
        # with lossy fallback so status/setup never crash mid-output.
        for _stream in (sys.stdout, sys.stderr):
            # hasattr guard (not try/except) keeps the silent-swallow audit
            # frozen: streams without reconfigure (pytest capture, io.StringIO
            # redirect) keep their default encoding and still work.
            if hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8", errors="replace")

    # Auto-update check (opt-out, interval-gated, 1s timeout) — before
    # dispatch so every user-facing command sees it, after the skip-list
    # guard inside _maybe_auto_update() itself.
    _maybe_auto_update()

    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("Usage: toolrecall <command>")
        print()
        print("Commands:")
        print("  setup           One-shot setup: config + systemd service + shim + start")
        print("  restart         Health check + clean daemon restart")
        print("  stop            Stop daemon + revert forward-proxy base-URL wiring")
        print("  init            Create default config.toml and .env")
        print("  status          Cache status and stats")
        print("  stats           Detailed stats (JSON)")
        print("  trend           Daily hit-rate trend + worst miss classes (--days N --json)")
        print("  invalidate      Clear all caches")
        print("  reset-stats     Reset statistics counters (preserves cache entries)")
        print("  index           Build/update knowledge database (--memory to add memory stores)")
        print("  index-memory    Index agent memory stores (MEMORY.md, USER.md)")
        print("  index-compact   Rebuild FTS index + VACUUM knowledge DB (reclaim bloat)")
        print("  index-dir       Index a directory into knowledge DB (e.g. Obsidian vault)")
        print("  config-set      Set a config value (section.key = value)")
        print("  serve           Start forward proxy (cache API responses)")
        print("  debug           Start debug/demo server (test cached_read via curl)")
        print("  nginx           Generate nginx config")
        print("  mcp             Start MCP Bridge (requires daemon); `mcp list` shows servers")
        print("  daemon          Start/stop/manage cache daemon (--foreground --stop --status)")
        print("  logs            Tail the rotating ToolRecall log (--tail N --level X)")
        print("  shim            Install/uninstall transparent cache shim (.pth)")
        print("  context         Context tracker: status, stale files, recall store/get")
        print("  top             Live TUI dashboard (stats/log/trend/context; --once snapshot)")
        print("  proxy           Attach/detach agent LLM traffic to the API proxy (attach|detach)")
        print("  update          Check PyPI for a newer version / apply upgrade (--check/--status)")
        print("  replay          Record/replay tool call scenarios (Replay mode)")
        print("  turso           Turso Cloud sync: init, status")
        print("  agent           Per-agent cache stats: ls, show, prune")
        print("  hook            Agent lifecycle hooks (claude print-config, staleness)")
        print("  audit           Token audit of agent transcripts (claude-code)")
        print("  keytrace        Explain proxy replay misses (needs TOOLRECALL_KEY_TRACE_DIR)")
        print("  healthcheck     One-shot daemon/cache health (--json, --clean-stale; exit 0/1/2)")
        return

    if sys.argv[1] in ("--version", "-V", "-v"):
        from toolrecall import __version__

        print(f"ToolRecall {__version__}")
        return

    cmd = sys.argv[1]

    # Commands that need the daemon running
    _DAEMON_REQUIRED = {
        "status",
        "stats",
        "invalidate",
        "reset-stats",
        "serve",
        "debug",
        "mcp",
        "restart",
        "index",
        "index-memory",
        "index-dir",
        "index-remote",
        "context",
    }
    if cmd in _DAEMON_REQUIRED:
        if not _ensure_daemon():
            print(f"  ⚠️  Could not start daemon — running '{cmd}' in direct mode.", file=sys.stderr)

    commands = {
        "init": cmd_init,
        "setup": cmd_setup,
        "restart": cmd_restart,
        "stop": cmd_stop,
        "status": cmd_status,
        "stats": cmd_stats,
        "trend": cmd_trend,
        "invalidate": cmd_invalidate,
        "reset-stats": cmd_reset_stats,
        "index": cmd_index,
        "index-compact": cmd_index_compact,
        "index-memory": cmd_index_memory,
        "index-dir": cmd_index_dir,
        "tool-usage": cmd_tool_usage,
        "index-remote": cmd_index_remote,
        "docs-rejects": cmd_docs_rejects,
        "dedup-stats": cmd_dedup_stats,
        "config-set": cmd_config_set,
        "serve": cmd_serve,
        "debug": cmd_debug,
        "nginx": cmd_nginx,
        "mcp": cmd_mcp,
        "daemon": cmd_daemon,
        "shim": cmd_shim,
        "update": cmd_update,
        "healthcheck": cmd_healthcheck,
        "logs": cmd_logs,
        "context": cmd_context,
        "top": cmd_top,
        "proxy": cmd_proxy,
        "agent": cmd_agent,
        "hook": cmd_hook,
        "audit": cmd_audit,
        "keytrace": cmd_keytrace,
    }

    if cmd in commands:
        commands[cmd]()
    elif cmd == "replay":
        from toolrecall.replay_cli import cmd_replay

        cmd_replay(sys.argv[2:])
    elif cmd == "turso":
        from toolrecall.turso_cli import cmd_turso

        cmd_turso(sys.argv[2:])
    else:
        print(f"Unknown command: {cmd}")
        print()
        print("Available commands:")
        for name in commands:
            print(f"  {name}")
        print("  replay")
        print("  turso")
        print()
        print("Run `toolrecall --help` for descriptions, or `toolrecall <command> --help`")
        print("for per-command usage.")
