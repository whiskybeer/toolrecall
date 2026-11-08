# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.keytrace — explain why forward-proxy replay stopped.

Reads a key-trace directory written by the forward proxy when
``TOOLRECALL_KEY_TRACE_DIR`` is set (canonical bodies + ``trace.jsonl``).
For every cache MISS it finds the closest earlier request (longest run of
identical leading messages, same system/tools) and prints the first field
that differs, with a short excerpt of both values — e.g. an ``ls -l`` mtime,
a new commit hash in the environment block, or a tool description that
changed. Read-only, stdlib only, works for any canonicalization profile.

Usage: ``toolrecall keytrace <dir> [--json]``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_EXCERPT = 60


def _first_diff(a: Any, b: Any, path: str = "") -> Optional[Tuple[str, Any, Any]]:
    """First differing leaf as (path, a_value, b_value), or None if equal."""
    if type(a) is not type(b):
        return path or "$", a, b
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}", a.get(k, "<missing>"), b.get(k, "<missing>")
            d = _first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        for i in range(min(len(a), len(b))):
            d = _first_diff(a[i], b[i], f"{path}[{i}]")
            if d:
                return d
        if len(a) != len(b):
            return f"{path}.length", len(a), len(b)
        return None
    return None if a == b else (path or "$", a, b)


def _excerpt(a: Any, b: Any) -> Tuple[str, str]:
    """Excerpts of two values centred on their first differing character."""
    sa = a if isinstance(a, str) else json.dumps(a)
    sb = b if isinstance(b, str) else json.dumps(b)
    i = 0
    while i < min(len(sa), len(sb)) and sa[i] == sb[i]:
        i += 1
    lo = max(0, i - _EXCERPT // 2)

    def cut(s: str) -> str:
        head = "…" if lo else ""
        tail = "…" if len(s) > lo + _EXCERPT else ""
        return head + s[lo : lo + _EXCERPT].replace("\n", "\\n") + tail

    return cut(sa), cut(sb)


def _common_messages(a: Dict[str, Any], b: Dict[str, Any]) -> int:
    ma, mb = a.get("messages") or [], b.get("messages") or []
    n = 0
    while n < min(len(ma), len(mb)) and ma[n] == mb[n]:
        n += 1
    return n


def _same_frame(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in set(a) | set(b) if k != "messages")


def explain(trace_dir: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: List[Dict[str, Any]] = []
    path = os.path.join(trace_dir, "trace.jsonl")
    with open(path, encoding="utf-8") as fh:
        entries = [json.loads(line) for line in fh if line.strip()]
    for n, e in enumerate(entries):
        try:
            with open(os.path.join(trace_dir, f"{e['request_hash']}.json"), encoding="utf-8") as fh:
                body = json.load(fh)
        except (OSError, ValueError, KeyError):
            body = None
        row: Dict[str, Any] = {
            "n": n,
            "hit": bool(e.get("hit")),
            "messages": len((body or {}).get("messages") or []),
            "request_hash": e.get("request_hash"),
        }
        if not row["hit"] and isinstance(body, dict):
            if any(s == body for s in seen):
                row["why"] = "identical canonical body was seen before (entry expired or evicted)"
            elif not seen:
                row["why"] = "first request seen (nothing to replay from)"
            elif not any(_common_messages(x, body) for x in seen):
                row["why"] = "first request of this conversation (nothing to replay from)"
            elif row["messages"] > max(
                (
                    len(x.get("messages") or [])
                    for x in seen
                    if _common_messages(x, body) and _same_frame(x, body)
                ),
                default=row["messages"],
            ):
                # Longer than anything seen from the same start with the same
                # system/tools: a step no earlier run reached, so a live call is
                # expected (agents move cache markers between turns, so a
                # prefix diff would be noise).
                row["why"] = "new step: no earlier request this long (nothing to replay)"
            else:
                n_msgs = len(body.get("messages") or [])
                best = max(
                    seen,
                    key=lambda s: (
                        _common_messages(s, body),  # same conversation first
                        _same_frame(s, body),
                        -abs(len(s.get("messages") or []) - n_msgs),  # same step of an earlier run
                    ),
                )
                # System prompt / tools / model before messages: a changed tool
                # description breaks every call and must be named as such.
                frame = {k: v for k, v in best.items() if k != "messages"}
                d = _first_diff(frame, {k: v for k, v in body.items() if k != "messages"})
                if d is None:
                    d = _first_diff(best, body)
                if d is not None:
                    p, va, vb = d
                    xa, xb = _excerpt(va, vb)
                    row.update(
                        why="differs from closest earlier request", path=p, earlier=xa, now=xb
                    )
        if isinstance(body, dict):
            seen.append(body)
        rows.append(row)
    return rows


def format_rows(rows: List[Dict[str, Any]]) -> str:
    hits = sum(r["hit"] for r in rows)
    out = [
        f"{len(rows)} traced requests: {hits} replayed (HIT), {len(rows) - hits} live (MISS)",
        "",
    ]
    for r in rows:
        status = "HIT " if r["hit"] else "MISS"
        line = f"#{r['n']:<4} {status} msgs={r['messages']:<4}"
        if r.get("path"):
            out.append(f"{line} first difference at {r['path']}")
            out.append(f"        earlier: {r['earlier']}")
            out.append(f"        now:     {r['now']}")
        else:
            out.append(f"{line} {r.get('why', '')}".rstrip())
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="toolrecall keytrace", description="Explain forward-proxy replay misses."
    )
    ap.add_argument("dir", help="directory given as TOOLRECALL_KEY_TRACE_DIR to the proxy")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.exists(os.path.join(a.dir, "trace.jsonl")):
        print(f"No trace.jsonl in {a.dir} — start the proxy with TOOLRECALL_KEY_TRACE_DIR={a.dir}")
        return 1
    rows = explain(a.dir)
    print(json.dumps(rows, indent=2) if a.json else format_rows(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
