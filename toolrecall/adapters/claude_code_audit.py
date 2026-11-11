# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.adapters.claude_code_audit — where a Claude Code session's tokens go.

Reads Claude Code session transcripts (``~/.claude/projects/*/*.jsonl``) and
reports, per session and in total:

- the fixed starting context: the exact token count of the first API call
  (``input + cache_creation + cache_read``) — system prompt, tool definitions,
  instruction files, skill/MCP/agent listings. It is re-read on every call;
- what that starting context is made of, from the transcript's attachments
  (instruction files such as CLAUDE.md / AGENTS.md, skill listing, MCP
  instructions, deferred-tool and agent listings). Component sizes are
  chars/4 estimates; the starting-context total is exact;
- each component's share of session cost, weighted by list-price ratios
  (input 1, cache write 1.25, cache read 0.1, output 5) — the same weighting
  used in ``bench/claude_code_replay``. Not invoice-verified.

Read-only: never writes to the transcripts. Run via
``toolrecall audit claude-code [--json] [--days N] [--projects DIR] [--include-tmp]``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

W_IN, W_CW, W_CR, W_OUT = 1.0, 1.25, 0.1, 5.0

# Attachment types that make up the starting context (seen before the first call).
_LISTING_TYPES = {
    "skill_listing": "skill listing",
    "mcp_instructions_delta": "MCP server instructions",
    "deferred_tools_delta": "deferred tool names",
    "agent_listing_delta": "agent type listing",
    "session_context": "session context",
}


def _tok(chars: int) -> int:
    return chars // 4


def _attachment_parts(a: Dict[str, Any]) -> List[tuple]:
    """(label, chars) for the starting-context parts an attachment carries."""
    t = a.get("type")
    if t == "instructions":
        return [
            (f"instructions: {f.get('path', '?')}", len(f.get("content") or ""))
            for f in a.get("files") or []
            if isinstance(f, dict)
        ]
    if t in _LISTING_TYPES:
        if t == "mcp_instructions_delta":
            return [
                (
                    _LISTING_TYPES[t],
                    sum(len(b) for b in a.get("addedBlocks") or [] if isinstance(b, str)),
                )
            ]
        if t == "skill_listing":
            return [(_LISTING_TYPES[t], len(a.get("content") or ""))]
        return [(_LISTING_TYPES[t], len(json.dumps(a)))]
    return []


def analyse_session(path: str) -> Optional[Dict[str, Any]]:
    """Return per-session numbers, or None for a session without API calls."""
    seen_uuid = set()
    seen_msg = set()
    calls = 0
    cost = 0.0
    out_cost = 0.0
    baseline = None  # exact tokens of the first call's prompt
    parts: Dict[str, int] = defaultdict(int)
    compactions = 0
    cwd = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            u = d.get("uuid")
            if u is not None:
                if u in seen_uuid:
                    continue  # duplicated transcript line (resume artifact)
                seen_uuid.add(u)
            cwd = cwd or d.get("cwd")
            typ = d.get("type")
            if typ == "system" and d.get("subtype") == "compact_boundary":
                compactions += 1
            if typ == "attachment" and baseline is None:
                for label, chars in _attachment_parts(d.get("attachment") or {}):
                    parts[label] += chars
            if typ != "assistant" or d.get("isSidechain"):
                continue
            m = d.get("message") or {}
            mid = m.get("id")
            if not mid or mid in seen_msg:
                continue
            seen_msg.add(mid)
            us = m.get("usage") or {}
            i, cw, cr, o = (
                int(us.get(k) or 0)
                for k in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                )
            )
            calls += 1
            cost += i * W_IN + cw * W_CW + cr * W_CR + o * W_OUT
            out_cost += o * W_OUT
            if baseline is None:
                baseline = i + cw + cr
    if not calls or baseline is None:
        return None
    # A block present from call 1 is written once and read on every later call.
    carry = W_CW + W_CR * (calls - 1)
    return {
        "path": path,
        "cwd": cwd,
        "calls": calls,
        "cost_units": cost,
        "output_cost_units": out_cost,
        "baseline_tokens": baseline,
        "baseline_cost_units": baseline * carry,
        "parts": {
            k: {"tokens_est": _tok(v), "cost_units": _tok(v) * carry} for k, v in parts.items()
        },
        "compactions": compactions,
    }


def audit(projects: str, days: Optional[float] = None, include_tmp: bool = False) -> Dict[str, Any]:
    files = glob.glob(os.path.join(projects, "*", "*.jsonl"))
    if not include_tmp:
        # Workdirs under /tmp are almost always benchmarks and fixtures.
        files = [f for f in files if not os.path.basename(os.path.dirname(f)).startswith("-tmp-")]
    if days is not None:
        cutoff = time.time() - days * 86400
        files = [f for f in files if os.path.getmtime(f) >= cutoff]
    sessions = [s for s in (analyse_session(f) for f in sorted(files)) if s]
    tot_cost = sum(s["cost_units"] for s in sessions)
    parts: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"cost_units": 0.0, "tokens_est_max": 0, "sessions": 0}
    )
    for s in sessions:
        for k, v in s["parts"].items():
            p = parts[k]
            p["cost_units"] += v["cost_units"]
            p["tokens_est_max"] = max(p["tokens_est_max"], v["tokens_est"])
            p["sessions"] += 1
    base_cost = sum(s["baseline_cost_units"] for s in sessions)
    baselines = sorted(s["baseline_tokens"] for s in sessions)
    return {
        "sessions": len(sessions),
        "calls": sum(s["calls"] for s in sessions),
        "cost_units": tot_cost,
        "output_share": (sum(s["output_cost_units"] for s in sessions) / tot_cost)
        if tot_cost
        else 0.0,
        "baseline_tokens_median": baselines[len(baselines) // 2] if baselines else 0,
        "baseline_share": (base_cost / tot_cost) if tot_cost else 0.0,
        "compactions": sum(s["compactions"] for s in sessions),
        "parts": sorted(
            (
                {
                    "name": k,
                    "share": (v["cost_units"] / tot_cost) if tot_cost else 0.0,
                    "tokens_est_max": v["tokens_est_max"],
                    "sessions": v["sessions"],
                }
                for k, v in parts.items()
            ),
            key=lambda x: -x["share"],
        ),
        "per_1k_always_loaded": (
            sum(1000 * (W_CW + W_CR * (s["calls"] - 1)) for s in sessions) / tot_cost
            if tot_cost
            else 0.0
        ),
    }


def format_report(r: Dict[str, Any]) -> str:
    if not r["sessions"]:
        return "No Claude Code sessions with API calls found."
    lines = [
        f"Claude Code token audit — {r['sessions']} sessions, {r['calls']} API calls",
        "(cost = list-price-weighted tokens: input 1, cache write 1.25, cache read 0.1, output 5)",
        "",
        f"Fixed starting context: median {r['baseline_tokens_median']:,} tokens per session, "
        f"re-read on every call = {100 * r['baseline_share']:.1f}% of cost",
        f"Output tokens: {100 * r['output_share']:.1f}% of cost   Compactions: {r['compactions']}",
        f"Every 1,000 tokens of always-loaded context = {100 * r['per_1k_always_loaded']:.2f}% of your cost",
        "",
        "Starting-context parts you control (size is a chars/4 estimate):",
    ]
    for p in r["parts"]:
        lines.append(
            f"  {100 * p['share']:5.2f}%  ~{p['tokens_est_max']:>6,} tok  in {p['sessions']:>3} sessions  {p['name']}"
        )
    rest = r["baseline_share"] - sum(p["share"] for p in r["parts"])
    lines.append(
        f"  {100 * max(rest, 0.0):5.2f}%  rest: Claude Code system prompt + tool definitions "
        "(shrinks only by disabling tools / MCP servers)"
    )
    lines += [
        "",
        "To cut cost: shorten or scope the largest instruction files (move rarely needed",
        "detail into files loaded on demand), disable MCP servers and skills a project",
        "does not use. Unchanged re-reads and repeated tool output are already handled",
        "by Claude Code itself; there is nothing to gain from caching those.",
        "",
        "Note on ToolRecall: Interactive Claude Code sessions do not benefit from file",
        "caching (Claude manages context natively). ToolRecall savings apply to headless",
        "regression test loops (claude -p in CI) and local stdio MCP pooling.",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="toolrecall audit claude-code",
        description="Where Claude Code session tokens go (read-only).",
    )
    ap.add_argument(
        "--projects", default=os.path.expanduser(os.path.join("~", ".claude", "projects"))
    )
    ap.add_argument(
        "--days", type=float, default=None, help="only sessions modified in the last N days"
    )
    ap.add_argument(
        "--include-tmp", action="store_true", help="include sessions whose workdir is under /tmp"
    )
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = audit(a.projects, a.days, a.include_tmp)
    print(json.dumps(r, indent=2) if a.json else format_report(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
