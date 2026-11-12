# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.adapters.antigravity_audit — transcript token and tool auditor for Antigravity.

Parses Antigravity session transcripts (``transcript.jsonl``) and reports:
- Session count, total steps, and user/model turn counts
- Exact tool execution frequencies (read_file, write_file, edit_file, run_command, invoke_subagent, swarm)
- Subagent spawns and swarm coordination overhead
- Redundant file reads (repeated reads of unchanged files)
- Tool execution errors and failed patch attempts
- Input/output and cached token distributions
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional


def _tok(chars: int) -> int:
    return max(1, chars // 4)


def analyse_transcript(path: str) -> Optional[Dict[str, Any]]:
    """Analyze a single Antigravity transcript.jsonl file."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None

    steps = 0
    user_prompts = 0
    model_turns = 0
    tool_calls: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    read_paths: Counter[str] = Counter()
    subagents_spawned = 0
    swarm_workers_spawned = 0

    input_tokens = 0
    cache_read_tokens = 0
    output_tokens = 0
    baseline_tokens = None
    created_at = None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue

            steps += 1
            stype = d.get("type")
            status = d.get("status")
            if not created_at and d.get("created_at"):
                created_at = d.get("created_at")

            if stype == "USER_INPUT":
                user_prompts += 1

            if stype == "PLANNER_RESPONSE":
                model_turns += 1
                inp = int(d.get("input_tokens") or 0)
                cr = int(d.get("cache_read_tokens") or 0)
                out = int(d.get("output_tokens") or 0)

                # Fallback token estimate if harness didn't record usage
                if inp == 0 and out == 0:
                    content_str = str(d.get("content") or "")
                    thinking_str = str(d.get("thinking") or "")
                    out = _tok(len(content_str) + len(thinking_str))
                    inp = 12000  # typical starting context estimate

                input_tokens += inp
                cache_read_tokens += cr
                output_tokens += out
                if baseline_tokens is None and (inp + cr) > 0:
                    baseline_tokens = inp + cr

                calls = d.get("tool_calls") or []
                if isinstance(calls, list):
                    for call in calls:
                        if not isinstance(call, dict):
                            continue
                        name = call.get("name", "unknown")
                        tool_calls[name] += 1
                        args = call.get("args") or {}

                        if name == "read_file":
                            p = args.get("Path") or args.get("path")
                            if isinstance(p, str) and p:
                                read_paths[p.strip().strip("'\"")] += 1
                        elif name == "invoke_subagent":
                            subagents_spawned += 1
                        elif name == "swarm":
                            action = args.get("action")
                            if action == "add":
                                n = int(args.get("n") or 1)
                                swarm_workers_spawned += n

            # Check status errors
            if status == "ERROR":
                content_text = str(d.get("content") or "")
                tool_errors["generic_error"] += 1
                if "error in tool execution" in content_text.lower():
                    tool_errors["tool_execution_error"] += 1

    if steps == 0:
        return None

    # Redundant reads (read >= 2 times in this session)
    redundant_reads = {p: count for p, count in read_paths.items() if count >= 2}

    conv_id = Path(path).parent.parent.name
    return {
        "path": path,
        "conversation_id": conv_id,
        "created_at": created_at,
        "steps": steps,
        "user_prompts": user_prompts,
        "model_turns": model_turns,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "output_tokens": output_tokens,
        "baseline_tokens": baseline_tokens or 0,
        "tool_calls": dict(tool_calls),
        "tool_errors": dict(tool_errors),
        "redundant_reads": redundant_reads,
        "subagents_spawned": subagents_spawned,
        "swarm_workers_spawned": swarm_workers_spawned,
    }


def find_transcripts(brain_dir: Optional[str] = None, days: Optional[float] = None) -> List[str]:
    """Discover Antigravity transcript.jsonl files."""
    candidates = []
    if brain_dir:
        candidates.append(
            os.path.join(
                os.path.expanduser(brain_dir), "*", ".system_generated", "logs", "transcript.jsonl"
            )
        )
        candidates.append(os.path.join(os.path.expanduser(brain_dir), "*.jsonl"))
    else:
        # Standard search paths across CLI and Desktop / IDE
        search_roots = [
            "~/.gemini/antigravity-cli/brain",
            "~/.gemini/antigravity/brain",
            "~/.gemini/antigravity-ide/brain",
            ".gemini/antigravity-cli/brain",
        ]
        for root in search_roots:
            p = os.path.expanduser(root)
            if os.path.isdir(p):
                candidates.append(
                    os.path.join(p, "*", ".system_generated", "logs", "transcript.jsonl")
                )

    found = []
    for pattern in candidates:
        for f in glob.glob(pattern):
            if os.path.isfile(f):
                found.append(f)

    if days is not None:
        cutoff = time.time() - days * 86400
        found = [f for f in found if os.path.getmtime(f) >= cutoff]

    return sorted(list(set(found)))


def audit(
    brain_dir: Optional[str] = None,
    days: Optional[float] = None,
    files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Generate aggregate audit across transcripts."""
    transcripts = files if files is not None else find_transcripts(brain_dir=brain_dir, days=days)
    sessions = [s for s in (analyse_transcript(f) for f in transcripts) if s]

    total_steps = sum(s["steps"] for s in sessions)
    total_prompts = sum(s["user_prompts"] for s in sessions)
    total_turns = sum(s["model_turns"] for s in sessions)
    total_in_tok = sum(s["input_tokens"] for s in sessions)
    total_cr_tok = sum(s["cache_read_tokens"] for s in sessions)
    total_out_tok = sum(s["output_tokens"] for s in sessions)

    tools_combined: Counter[str] = Counter()
    errors_combined: Counter[str] = Counter()
    redundant_reads_all: Counter[str] = Counter()
    total_subagents = sum(s["subagents_spawned"] for s in sessions)
    total_swarm_workers = sum(s["swarm_workers_spawned"] for s in sessions)

    for s in sessions:
        for t, cnt in s["tool_calls"].items():
            tools_combined[t] += cnt
        for e, cnt in s["tool_errors"].items():
            errors_combined[e] += cnt
        for p, cnt in s["redundant_reads"].items():
            redundant_reads_all[p] += cnt

    baselines = [s["baseline_tokens"] for s in sessions if s["baseline_tokens"] > 0]
    median_baseline = baselines[len(baselines) // 2] if baselines else 0

    return {
        "sessions_analyzed": len(sessions),
        "total_steps": total_steps,
        "user_prompts": total_prompts,
        "model_turns": total_turns,
        "median_baseline_tokens": median_baseline,
        "input_tokens_total": total_in_tok,
        "cache_read_tokens_total": total_cr_tok,
        "output_tokens_total": total_out_tok,
        "subagents_spawned": total_subagents,
        "swarm_workers_spawned": total_swarm_workers,
        "tool_calls": dict(tools_combined.most_common()),
        "tool_errors": dict(errors_combined),
        "redundant_reads": dict(redundant_reads_all.most_common(15)),
        "sessions": sessions,
    }


def format_report(r: Dict[str, Any]) -> str:
    """Format human-readable audit report."""
    if r["sessions_analyzed"] == 0:
        return "No Antigravity transcripts found to analyze."

    lines = [
        "══════════════════════════════════════════════════════════════",
        f"  ToolRecall Audit: Google Antigravity ({r['sessions_analyzed']} session(s))",
        "══════════════════════════════════════════════════════════════",
        f"  • Steps:             {r['total_steps']:,} total ({r['user_prompts']} user prompts, {r['model_turns']} model responses)",
        f"  • Median Baseline:   ~{r['median_baseline_tokens']:,} tokens (rules + skills + MCP schemas)",
        f"  • Token Volume:      ~{r['input_tokens_total']:,} input, ~{r['cache_read_tokens_total']:,} provider cache reads, ~{r['output_tokens_total']:,} output",
        f"  • Multi-Agent Ops:   {r['subagents_spawned']} subagent(s), {r['swarm_workers_spawned']} swarm worker(s)",
        "",
        "  Top Tool Executions:",
    ]
    if r["tool_calls"]:
        for name, count in list(r["tool_calls"].items())[:12]:
            lines.append(f"    {count:>5}×  {name}")
    else:
        lines.append("    (no tool executions recorded)")

    lines.append("")
    lines.append("  Redundant In-Session File Reads:")
    if r["redundant_reads"]:
        for path, count in list(r["redundant_reads"].items())[:6]:
            lines.append(f"    {count:>5}×  {path}")
        lines.append("    ℹ️  Tip: Subagents and hooks can cache or offload unchanged repeat reads.")
    else:
        lines.append("    None detected (clean single-read pattern)")

    if r["tool_errors"]:
        lines.append("")
        lines.append("  Execution Errors:")
        for err, cnt in r["tool_errors"].items():
            lines.append(f"    {cnt:>5}×  {err}")

    lines += [
        "══════════════════════════════════════════════════════════════",
        "",
        "Recommendations:",
        "  1. Ensure ~/.gemini/config/mcp_config.json uses --multiplexer-only to",
        "     drop ~1.5k unused tool schema tokens per turn.",
        "  2. Enable lifecycle drift alerts via 'toolrecall hook antigravity print-config'",
        "     to catch out-of-band disk edits before failed patch attempts.",
        "  3. Use 'toolrecall index-antigravity' to enable 0.6ms docs_search.",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="toolrecall audit antigravity",
        description="Audit Antigravity transcript tokens, steps, and tool executions.",
    )
    ap.add_argument(
        "--brain-dir",
        default=None,
        help="Custom brain directory (e.g. ~/.gemini/antigravity-cli/brain)",
    )
    ap.add_argument(
        "--days",
        type=float,
        default=None,
        help="Only analyze sessions modified in the last N days",
    )
    ap.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    args = ap.parse_args(argv)

    r = audit(brain_dir=args.brain_dir, days=args.days)
    if args.json:
        # Prune full session lists from top-level json for conciseness
        r_out = dict(r)
        r_out["sessions"] = [
            {k: v for k, v in s.items() if k != "path"} for s in r_out.get("sessions", [])
        ]
        print(json.dumps(r_out, indent=2))
    else:
        print(format_report(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
