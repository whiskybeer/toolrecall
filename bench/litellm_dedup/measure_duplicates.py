#!/usr/bin/env python3
"""measure_duplicates.py — standalone duplicate-block ratio reporter.

The buyer's request: a script that runs ENTIRELY inside their perimeter, sends
NOTHING out, needs no gateway change / no LiteLLM / no daemon / no approvals.

For each chat-completion request body (opened-Messages format) it reports how much
byte-identical duplicate content ToolRecall's keep-first dedup could stub, at a few
min_chars thresholds. This answers "what is *our* duplicate ratio?" before anyone
commits to a gateway pilot.

Input:  a file of request bodies. Supported formats (newline-delimited):
  --format jsonl    each line is a full OpenAI chat.completions request {"messages":[...]}
  --format msgs     each line is just a {"role":..,"content":..} message array

Output (stdout only — nothing leaves the machine):
  per-request and aggregate duplicate-block count, chars stubbable, est. tokens,
  and % of request token-volume that is stubbable.

Pure stdlib. Only dependency: the `dedup_messages` pure function, loaded from
toolrecall/adapters/litellm.py WITHOUT importing litellm (guarded import).

Usage:
  python3 measure_duplicates.py requests.jsonl --format jsonl --min-chars 400 600 800
  python3 measure_duplicates.py < requests.jsonl --format jsonl   (reads stdin)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import importlib.util
from typing import Dict, List


def load_dedup(min_chars_default: int):
    """Import dedup_messages as a PURE function. Never imports litellm.

    We load the module file by path and call the function directly. The module's
    litellm import is guarded, so this works with litellm absent.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # 1) toolrecall installed/checked-out
    for cand in [
        os.path.join(here, "toolrecall", "adapters", "litellm.py"),
        os.path.expanduser("~/toolrecall/toolrecall/adapters/litellm.py"),
        os.path.expanduser("~/toolrecall-dev/toolrecall/toolrecall/adapters/litellm.py"),
    ]:
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("_tr_litellm_pure", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.dedup_messages
    raise SystemExit(
        "Could not locate toolrecall/adapters/litellm.py. Set TOOLRECALL_SRC or pass "
        "--module PATH."
    )


def to_tokens(chars: int) -> int:
    """Rough token estimate: 4 chars/token (BPE-like). For ratio only."""
    return int(chars) // 4


def load_requests(path: str, fmt: str) -> List[Dict]:
    reqs: List[Dict] = []
    f = sys.stdin if path in (None, "-") else open(path, encoding="utf-8", errors="replace")
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if fmt == "msgs" and isinstance(obj, list):
                obj = {"messages": obj}
            msgs = obj.get("messages") if isinstance(obj, dict) else None
            if isinstance(msgs, list):
                reqs.append(obj)
    return reqs


def text_content(msg: Dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for part in c:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "\n".join(out)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", default="-", help="path or '-' for stdin")
    ap.add_argument("--format", choices=["jsonl", "msgs"], default="jsonl")
    ap.add_argument("--module", default=os.environ.get("TOOLRECALL_SRC", ""),
                    help="path to toolrecall/adapters/litellm.py (override auto-detect)")
    ap.add_argument("--min-chars", type=int, nargs="+", default=[400, 600, 800],
                    help="min_chars thresholds to report")
    ap.add_argument("--max-tokens-ratio", action="store_true",
                    help="report % of message volume stubbable")
    args = ap.parse_args()

    dedup = load_dedup(0)

    reqs = load_requests(args.input, args.format)
    if not reqs:
        print("No valid request bodies found.", file=sys.stderr)
        return 1

    # (min_chars) -> per-request stats lists
    statz: Dict[int, Dict] = {mc: {"blocks": [], "chars": [], "tokens": [], "total_chars": []}
                              for mc in args.min_chars}

    for req in reqs:
        msgs = req.get("messages", [])
        total_chars = sum(len(text_content(m)) for m in msgs)
        for mc in args.min_chars:
            _, stats = dedup(msgs, min_chars=mc, protect_last=0)
            s = statz[mc]
            s["blocks"].append(stats.get("blocks", 0))
            s["chars"].append(stats.get("chars_saved", 0))
            s["tokens"].append(to_tokens(stats.get("chars_saved", 0)))
            s["total_chars"].append(total_chars)

    # ---- Report ----
    print("=" * 74)
    print("ToolRecall duplicate-ratio scan  (runs 100% in-perimeter; sends nothing)")
    print("=" * 74)
    print(f"  requests scanned : {len(reqs)}")
    print(f"  object used      : dedup_messages (pure, protect_last=0)")
    print()
    for mc in args.min_chars:
        s = statz[mc]
        n = len(s["blocks"])
        tot_blocks = sum(s["blocks"])
        tot_chars = sum(s["chars"])
        tot_tokens = sum(s["tokens"])
        tot_vol = sum(s["total_chars"]) or 1
        pct_vol = tot_chars / tot_vol * 100
        reqs_with = sum(1 for b in s["blocks"] if b > 0)
        print(f"--- min_chars = {mc} ---")
        print(f"  requests with any stubbable dup : {reqs_with}/{n} ({reqs_with/max(n,1)*100:.0f}%)")
        print(f"  total duplicate blocks stubbable : {tot_blocks}")
        print(f"  total chars stubbable            : {tot_chars:,}  (~{tot_tokens:,} tokens)")
        print(f"  % of message volume stubbable    : {pct_vol:.1f}%")
        if args.max_tokens_ratio:
            nonempty = [s["total_chars"][i] for i in range(n) if s["total_chars"][i] > 0]
        print()
    print("Note: % of volume stubbable = duplicate content you'd stop re-sending.")
    print("It is NOT billed $ savings — real savings depend on cache-hit economics,")
    print("which this in-perimeter scan correctly does NOT project.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
