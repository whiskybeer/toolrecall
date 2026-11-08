# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""T7 — Measure savings with Google MCP servers, fronted through ToolRecall.

Google ships first-party MCP servers (workspace, gcloud, firebase etc.) that
an Antigravity session can call. When Antigravity reaches them through
ToolRecall's multiplexer with transparent caching enabled, identical repeated
calls are answered from the local cache instead of re-executing on the
server — saving per-call network/API cost and latency.

This script measures exactly that at the cache layer, the same layer the
daemon's transparent cache uses (`cached_mcp_check` / `cached_mcp_store`):

  Arm A (control, ttl=0):   every call executes live against the server
  Arm B (treatment, ttl=N): first call live, identical repeats from cache

Two measurement dimensions per arm:
  - wall-clock per call and total (latency saved)
  - bytes transferred (the payload the network did NOT have to carry on hits)

The "Google server" itself is NOT contacted by this script — the Google
Workspace/GitHub MCP servers need OAuth you should not run inside a bench.
Instead, the script measures the cache layer's saving shape with the SAME
arguments you would send to the Google server; the saving applies whenever
the real server returns byte-identical responses for identical args within
TTL (true for workspace file metadata reads, calendar listings, etc.).

IMPORTANT (honesty guardrails):
  - This measures the cache-layer saving PATTERN, not a billed Google API
    delta. For a billed number you'd need OAuth against the real server and
    a quota dashboard — out of scope for a zero-credential test kit.
  - No third-party network is touched: server side is the bundled loopback
    TLS server, exactly like T2/T3.

Run (from the kit folder):
  python bench/mcp_multiplex_bench.py --ttl 300 --calls 6 --ms 200
Output: JSON lines + human summary. Numbers only, no transcripts.
"""

import argparse
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

_KIT_DIR = Path(__file__).resolve().parent


def _boot_server():
    import importlib.util

    server_path = _KIT_DIR / "local_server.py"
    if not server_path.exists():
        # kit layout: bench/mcp_multiplex_bench.py → server one level up
        server_path = _KIT_DIR.parent / "local_server.py"
    spec = importlib.util.spec_from_file_location("local_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    httpd, port = mod.serve_in_thread(0)
    return mod, httpd, port


def _ca() -> ssl.SSLContext:
    cert = _KIT_DIR / "local_cert.pem"
    if not cert.exists():
        cert = _KIT_DIR.parent / "local_cert.pem"
    return ssl.create_default_context(cafile=str(cert))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default="fetch", help="multiplexed server name to attribute rows to (default: fetch)")
    ap.add_argument("--tool", default="fetch_url", help="tool name (default: fetch_url)")
    ap.add_argument("--ttl", type=int, default=300, help="treatment TTL seconds")
    ap.add_argument("--calls", type=int, default=6, help="identical calls per arm")
    ap.add_argument("--ms", type=int, default=200, help="simulated server latency")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    from toolrecall import client as tr_client

    if not tr_client.daemon_running():
        print("ERROR: daemon not running — cache layer would silently bypass.", file=sys.stderr)
        sys.exit(2)

    mod, httpd, port = _boot_server()
    ctx = _ca()
    base = f"https://127.0.0.1:{port}"
    # "Google-ish" args: the shape a workspace/calendar read would carry.
    url = f"{base}/delay/{args.ms}?seed=1&slot=google-mcp"
    mcp_args = {"url": url, "user": "test-user", "origin": "google-mcp-server"}

    def live_call() -> dict:
        t0 = time.perf_counter()
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, context=ctx, timeout=args.timeout) as resp:
            body = resp.read()
        return {"latency_s": round(time.perf_counter() - t0, 4), "bytes": len(body) + 300}

    from toolrecall.cache import cached_mcp_check, cached_mcp_store

    def run_arm(name: str, ttl: int) -> dict:
        lat, byt, on_wire = [], [], []
        t_start = time.perf_counter()
        live = 0
        for _ in range(args.calls):
            t0 = time.perf_counter()
            res = cached_mcp_check(args.server, args.tool, mcp_args, ttl=ttl)
            if res.get("cached"):
                # hit: body came from disk, zero network — bytes = row read size
                lat.append(round(time.perf_counter() - t0, 4))
                byt.append(len(res["data"]))
                on_wire.append(0)
                continue
            r = live_call()
            live += 1
            lat.append(r["latency_s"])
            byt.append(r["bytes"])
            on_wire.append(r["bytes"])
            cached_mcp_store(
                res["key"], args.server, args.tool, mcp_args, json.dumps({"echo": "google-mcp-shape"}), ttl=ttl
            )
        total = round(time.perf_counter() - t_start, 3)
        return {
            "arm": name,
            "ttl": ttl,
            "calls": args.calls,
            "live_calls": live,
            "cache_hits": args.calls - live,
            "total_s": total,
            "avg_latency_s": round(total / args.calls, 4),
            "bytes_total": sum(byt),
            "bytes_on_wire": sum(on_wire),
        }

    results = [run_arm("control", 0), run_arm("treatment", args.ttl)]
    httpd.shutdown()

    for r in results:
        print(json.dumps(r, ensure_ascii=False))

    c, t = results
    print(
        "\n# summary\n"
        f"# live calls:        control={c['live_calls']}  treatment={t['live_calls']}  "
        f"avoided={c['live_calls'] - t['live_calls']}\n"
        f"# total wall:        control={c['total_s']}s  treatment={t['total_s']}s  "
        f"delta={round(c['total_s'] - t['total_s'], 3)}s\n"
        f"# bytes on wire:     control={c['bytes_on_wire']}  treatment={t['bytes_on_wire']}  "
        f"saved={c['bytes_on_wire'] - t['bytes_on_wire']}\n"
        "# Measured: cache-layer saving pattern for fronted MCP servers.\n"
        "# NOT measured: billed Google API delta (needs real OAuth + quota dashboard).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
