#!/usr/bin/env python3
"""measure_handoff.py — cross-role duplicate-reuse reporter (the Kimchi pitch).

The in-perimeter tool for the handoff claim. Where measure_duplicates.py finds
duplicates WITHIN one request, this finds the same file content re-sent ACROSS
different roles in a multi-agent session — the thing Kimchi's 5-role routing does
(explorer reads a file, hands to planner, delegates to builder; each re-carries the
bytes because provider prefix caching is per-provider).

For each request body tagged with a session + role, it:
  - extracts large text blocks (>= min_chars) from messages,
  - tracks which (session, role) first carried each block,
  - counts a block as CROSS-ROLE REUSE when the same bytes reappear in a DIFFERENT
    role within the same session (and as within-role reuse when same role repeats it).

Runs 100% in-perimeter: reads a JSONL slice of YOUR request bodies, sends nothing
out, no keys, no gateway, no network, no daemon, no litellm. Stdlib only.

Input (JSONL, one request per line), with role/session metadata. Accepts either:
  --format full    {"session":..,"role":..,"model":..,"messages":[{role,content},..]}
  --format msgs    {"messages":[...]} with role inferred from the FIRST message or
                   a "role"/"session" top-level key if present

Usage:
  python3 measure_handoff.py traffic.jsonl --min-chars 400 800
  cat traffic.jsonl | python3 measure_handoff.py - --min-chars 800
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Tuple


def to_tokens(chars: int) -> int:
    return int(chars) // 4


def text_content(part) -> str:
    """Extract text from a content string or OpenAI content-parts list."""
    if isinstance(part, str):
        return part
    if isinstance(part, list):
        out = []
        for p in part:
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                out.append(p["text"])
        return "\n".join(out)
    return ""


def extract_blocks(req: Dict, min_chars: int) -> List[Tuple[str, str]]:
    """Return list of (role, content) for large text blocks in this request."""
    blocks = []
    for msg in req.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "?")
        c = text_content(msg.get("content"))
        if isinstance(c, str) and len(c) >= min_chars:
            blocks.append((role, c))
    return blocks


def dedup_key(text: str) -> str:
    """Fingerprint for byte-identical reuse. Exact hash — NOT substring/formatting-insensitive.
    This deliberately mirrors ToolRecall's whole-block matching: if two harnesses wrap
    the same file differently (line numbers, path headers), it will NOT match — which is
    itself a finding worth reporting (see --report-wrap-mismatch)."""
    import hashlib
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", default="-", help="path or '-' for stdin")
    ap.add_argument("--format", choices=["full", "msgs"], default="full")
    ap.add_argument("--min-chars", type=int, nargs="+", default=[400, 800])
    args = ap.parse_args()

    f = sys.stdin if args.input in (None, "-") else open(args.input, encoding="utf-8", errors="replace")
    reqs = []
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if args.format == "msgs" and isinstance(o, list):
                o = {"messages": o}
            if isinstance(o, dict) and isinstance(o.get("messages"), list):
                reqs.append(o)

    if not reqs:
        print("No valid request bodies found.", file=sys.stderr)
        return 1

    print("=" * 74)
    print("ToolRecall cross-role handoff reuse scan  (100% in-perimeter, sends nothing)")
    print("=" * 74)
    print(f"  requests            : {len(reqs)}")
    print(f"  roles detected      : {sorted({r.get('role') or r.get('session') or '?' for r in reqs})}")
    print()

    for mc in args.min_chars:
        # per-block fingerprint -> set of (session, role) that carried it
        seen: Dict[str, Dict[str, set]] = {}   # session -> {fingerprint: set(roles)}
        cross = 0        # bytes re-sent to a different role in same session
        within = 0       # bytes repeated by same role
        cross_reqs = set()
        norm_missed = 0  # wrap-mismatch byte-exact missed (normalized fn finds, exact does not)

        def norm(s: str) -> str:
            return re.sub(r"\s+", " ", s.strip()).lower()

        for r in reqs:
            session = r.get("session", r.get("session_id", "_"))
            role = r.get("role", "?")
            role = ":".join(
                (role,) if isinstance(role, str) else tuple(str(x) for x in (role or []))
            )
            for mrole, c in extract_blocks(r, mc):
                dk = dedup_key(c)
                s = seen.setdefault(session, {})
                roles_for = s.get(dk)
                if roles_for is None:
                    s[dk] = {role}
                    continue
                if role in roles_for:
                    within += len(c)
                else:
                    cross += len(c)
                    cross_reqs.add(session)
                    roles_for.add(role)

        print(f"--- min_chars = {mc} ---")
        print(f"  cross-role reuse (bytes re-sent to a different role): {cross:,}  (~{to_tokens(cross):,} tokens)")
        print(f"  within-role repeat (bytes re-sent to same role)      : {within:,}  (~{to_tokens(within):,} tokens)")
        print(f"  sessions showing cross-role reuse                    : {len(cross_reqs)}")
        print()

    print("Note: these are VOLUME figures (bytes/tokens re-sent across roles), NOT billed-$")
    print("savings. Real savings depend on cache-read pricing and are what a follow-up")
    print("cost-per-resolved-instance pass would establish. This scan stays in-perimeter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
