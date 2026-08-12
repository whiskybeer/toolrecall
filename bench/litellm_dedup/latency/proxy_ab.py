#!/usr/bin/env python3
"""proxy_ab.py — end-to-end local-path latency: WITH dedup vs WITHOUT.

Measures the request-path latency delta of the dedup hook on an accumulating
agent session, reporting p50/p95/p99 per arm. Two arms, identical payloads:

    ARM A (WITH):    handler runs dedup_messages, then posts stubbed payload
    ARM B (WITHOUT): payload passed through unchanged (hook disabled)

The "proxy" is a local stdlib OpenAI-compatible stub on 127.0.0.1 — no network
to a provider, so the per-request cost is hook + serialization + HTTP dispatch
only (the dedup-attributable delta we control). Total LLM round-trip back to a
real provider is NOT measured here (no key); it is provider-dominated and
discussed, not fabricated.

Usage:
    python3 proxy_ab.py --iters 50 --out proxy_ab_results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from toolrecall.adapters.litellm import dedup_messages  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# Accumulating agent session (deterministic, local synthetic file content)
# ──────────────────────────────────────────────────────────────────────────
def _file_block(n_lines: int = 45, seed: int = 1) -> str:
    import random
    rng = random.Random(seed)
    lines = []
    for i in range(n_lines):
        n = rng.randint(15, 70)
        lines.append("".join(rng.choice("abcdefghijklmnopqrstuvwxyz _(),.")
                             for _ in range(n)))
    return "\n".join(lines) + "\n"


def build_accumulating_session(n_turns: int = 8, n_files: int = 3):
    """Return list of snapshots; snapshots[t] = full messages the agent would
    send as its request at turn t (1-based; carries turns 1..t). Pattern mirrors
    the real agent: re-reads the SAME file blocks across turns -> dedup fires."""
    files = {f"src/mod{i}.py": _file_block(seed=i) for i in range(n_files)}
    snapshots = []
    msgs = [{"role": "system",
             "content": "You are a senior software engineer debugging a repo."}]

    msgs.append({"role": "user",
                 "content": "Trace the bug. Read the relevant modules."})
    for fpath, content in files.items():
        msgs.append({"role": "tool", "content": content})  # first read

    # turn 1 = initial reads
    snapshots.append(list(msgs))

    for t in range(2, n_turns + 1):
        msgs.append({"role": "assistant",
                     "content": f"Analysis {t}: re-inspecting files to verify flow."})
        for fpath, content in files.items():
            msgs.append({"role": "tool", "content": content})  # RE-READ -> dup
        msgs.append({"role": "user",
                     "content": f"Continue debugging, turn {t}."})
        snapshots.append(list(msgs))
    return snapshots, {}

# ──────────────────────────────────────────────────────────────────────────
# Local OpenAI-compatible stub
# ──────────────────────────────────────────────────────────────────────────
class StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"role": "assistant",
                                                    "content": "ok"}}],
                           "usage": {"prompt_tokens": 0, "completion_tokens": 1}})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass  # silence


def _post(url: str, payload: dict) -> float:
    """POST payload to stub, return wall microseconds for the round-trip."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Authorization": "Bearer test-key"})
    t0 = time.perf_counter_ns()
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    return (time.perf_counter_ns() - t0) / 1000.0  # us


# ──────────────────────────────────────────────────────────────────────────
# Arms
# ──────────────────────────────────────────────────────────────────────────
def run_arm(url: str, snapshots: list, with_dedup: bool, iters: int) -> dict:
    hook_samples: list[float] = []
    path_samples: list[float] = []

    # warm-up / wake the stub thread
    _post(url, {"messages": [{"role": "user", "content": "warm"}]})

    for _ in range(iters):
        for snap in snapshots:
            if with_dedup:
                t0 = time.perf_counter_ns()
                payload, stats = dedup_messages(snap)
                hook_samples.append((time.perf_counter_ns() - t0) / 1000.0)
            else:
                payload, stats = snap, None
            path_samples.append(_post(url, {"model": "stub",
                                            "messages": payload}))
    return {"hook_samples": hook_samples, "path_samples": path_samples}


def pct(sorted_us: list[float], q: float) -> float:
    if not sorted_us:
        return 0.0
    return sorted_us[int(q * (len(sorted_us) - 1))]


def summarize(vals: list[float], label: str) -> dict:
    s = sorted(vals)
    return {
        label + "_n": len(s),
        label + "_mean_us": round(statistics.mean(s), 2),
        label + "_p50_us": round(pct(s, 0.50), 2),
        label + "_p95_us": round(pct(s, 0.95), 2),
        label + "_p99_us": round(pct(s, 0.99), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=50)
    ap.add_argument("--n-turns", type=int, default=8)
    ap.add_argument("--out", default="proxy_ab_results.json")
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/v1/chat/completions"

    snapshots, _ = build_accumulating_session(args.n_turns)

    print(f"turns={args.n_turns} snapshots={len(snapshots)} iters={args.iters}")
    arm_b = run_arm(url, snapshots, with_dedup=False, iters=args.iters)
    arm_a = run_arm(url, snapshots, with_dedup=True, iters=args.iters)
    srv.shutdown()

    out = {}
    out["meta"] = {"n_turns": args.n_turns, "iters": args.iters,
                   "stub": "local stdlib OpenAI-compatible, no provider network",
                   "note": "local-path only (hook+dispatch). Total provider "
                           "round-trip not measured here (no key)."}
    out["WITHOUT"] = {**summarize(arm_b["path_samples"], "path")}
    out["WITH"] = {**summarize(arm_a["hook_samples"], "hook"),
                   **summarize(arm_a["path_samples"], "path")}

    # delta table
    d_p50 = out["WITH"]["path_p50_us"] - out["WITHOUT"]["path_p50_us"]
    d_p99 = out["WITH"]["path_p99_us"] - out["WITHOUT"]["path_p99_us"]
    out["delta"] = {"path_p50_us_delta": round(d_p50, 2),
                    "path_p99_us_delta": round(d_p99, 2),
                    "hook_p50_us": out["WITH"]["hook_p50_us"],
                    "hook_p99_us": out["WITH"]["hook_p99_us"]}

    res = Path(args.out)
    res.parent.mkdir(parents=True, exist_ok=True)
    res.write_text(json.dumps(out, indent=2) + "\n")

    print(f"\nWITHOUT (raw passthrough)  path p50={out['WITHOUT']['path_p50_us']}µs "
          f"p99={out['WITHOUT']['path_p99_us']}µs")
    print(f"WITH    (dedup enabled)    hook p50={out['WITH']['hook_p50_us']}µs "
          f"p99={out['WITH']['hook_p99_us']}µs | "
          f"path p50={out['WITH']['path_p50_us']}µs p99={out['WITH']['path_p99_us']}µs")
    print(f"DELTA   path p50={out['delta']['path_p50_us_delta']}µs "
          f"p99={out['delta']['path_p99_us_delta']}µs")
    print(f"saved -> {res}")


if __name__ == "__main__":
    main()
