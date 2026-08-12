#!/usr/bin/env python3
"""overnight_claude_bench.py — one-shot overnight driver.

Runs the 4-arm Claude benchmark (prefix/toolrecall/hermes_compress/both) on the
large-review workload into an isolated named DB, then builds the 3-version static
website (naive, compress-only, ct-only) from the resulting DB.

Every website number is pulled directly from the DB — no hand-written figures.

Usage (set BENCH_DB_NAME for a unique, non-overwritten DB):
    BENCH_DB_NAME=claude-pk-large <python> overnight_claude_bench.py

Exit 0 on success. Prints a short summary + site paths to stdout.
"""

import os
import sqlite3
import subprocess
import sys
import time

BENCH_DIR = os.path.expanduser("~/.toolrecall/bench-runs")
APPROOT = os.path.expanduser("~/toolrecall/bench")
PY = "/tmp/bench-env/bin/python3"
SITE_DIR = os.path.expanduser("~/workspace/sites-claude")
DB_NAME = os.environ.get("BENCH_DB_NAME", "claude-pk-large")
DB = os.path.join(BENCH_DIR, DB_NAME + ".db")

# Claude Sonnet 4 pricing (matches arms.PRICING)
PRICE = {"prompt": 3.00, "cached": 0.30, "completion": 15.00}
MAX_TURNS = int(os.environ.get("BENCH_MAX_TURNS", "40"))
MODEL = os.environ.get("BENCH_MODEL", "claude-sonnet-5")
CTX = int(os.environ.get("BENCH_CONTEXT_LIMIT", "200000"))


def run_bench():
    env = dict(os.environ)
    env["BENCH_DB_NAME"] = DB_NAME
    env["BENCH_CONTEXT_LIMIT"] = str(CTX)
    env["PYTHONPATH"] = os.path.expanduser("/home/hermes/toolrecall") + ":" + env.get("PYTHONPATH", "")
    cmd = [
        PY, os.path.join(APPROOT, "interleave.py"),
        "large-review",
        "--seeds", "1",
        "--max-turns", str(MAX_TURNS),
        "--provider", "anthropic",
        "--model", MODEL,
        "--delay", "0.3",
    ]
    print("RUN: " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, env=env, cwd=APPROOT, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def db_rows():
    if not os.path.exists(DB):
        return None, []
    con = sqlite3.connect(DB)
    # Each arm writes under its OWN run_id in this DB. Aggregate across ALL
    # run_ids, keeping prompt_tokens>0 (real turns) — drops 401/404/0-token rows.
    rows = con.execute(
        "SELECT arm, turn_index, request_tokens, prompt_tokens, completion_tokens, "
        "cache_read_tokens, ctx_dropped_tokens_cum, status "
        "FROM turn_log WHERE prompt_tokens>0 ORDER BY arm, turn_index",
    ).fetchall()
    con.close()
    return DB, rows


def fmt(n):
    return f"{n:,}"


def cost(rows):
    """row tuple is (t, req, pt, ct_, cr, dropped, status) — indices 2,3,4."""
    p = sum(r[2] for r in rows)   # prompt_tokens
    c = sum(r[3] for r in rows)   # completion_tokens
    cr = sum(r[4] for r in rows)  # cache_read_tokens
    miss = max(p - cr, 0)
    return (miss / 1e6 * PRICE["prompt"]) + (cr / 1e6 * PRICE["cached"]) + (c / 1e6 * PRICE["completion"])


ARMS = {"prefix": "naive", "hermes_compress": "compress-only", "toolrecall": "ct-only"}
ARM_ORDER = {"naive": 1, "compress-only": 2, "ct-only": 3}


def build_sites(rows):
    os.makedirs(SITE_DIR, exist_ok=True)
    data = {}
    for arm, t, req, pt, ct_, cr, dropped, status in rows:
        name = ARMS.get(arm, arm)
        data.setdefault(name, []).append((t, req, pt, ct_, cr, dropped, status))

    # Pull a per-turn view ordered by turn for the naive baseline + ct comparison table
    turns = {}
    for name, lst in data.items():
        for (t, req, pt, ct_, cr, dropped, status) in lst:
            turns.setdefault(t, {})[name] = (req, pt, ct_, cr, dropped, status)

    def results_table(title, label):
        lst = data.get(label, [])
        if not lst:
            return f"<p>No data for {label}.</p>"
        n_turns = len(lst)
        last = lst[-1]
        tot = cost(lst)
        html = [f"<h2>{title}</h2>"]
        html.append(f"<table><tr><th>Turn</th><th>Request tok</th><th>Prompt tok</th>"
                    f"<th>Completion</th><th>Cache-read</th><th>Dropped (cum)</th><th>Status</th></tr>")
        for t, req, pt, ct_, cr, dropped, status in lst:
            html.append(f"<tr><td>{t}</td><td>{fmt(req)}</td><td>{fmt(pt)}</td>"
                        f"<td>{fmt(ct_)}</td><td>{fmt(cr)}</td><td>{fmt(dropped)}</td><td>{status}</td></tr>")
        html.append("</table>")
        html.append(f"<p><strong>{n_turns} turns completed</strong> · last request_tokens={fmt(last[1])} · "
                    f"est. cost=${tot:.4f}</p>")
        return "".join(html)

    # Build a comparison table across arms per turn
    comp = ["<h2>Per-turn comparison (request_tokens)</h2>",
            "<table><tr><th>Turn</th><th>naive</th><th>compress-only</th><th>ct-only</th></tr>"]
    for t in sorted(turns):
        row = turns[t]
        def val(name):
            v = row.get(name)
            return fmt(v[0]) if v else "-"
        comp.append(f"<tr><td>{t}</td><td>{val('naive')}</td><td>{val('compress-only')}</td>"
                    f"<td>{val('ct-only')}</td></tr>")
    comp.append("</table>")
    comp_html = "".join(comp)

    model_label = MODEL.replace("claude-sonnet-5", "Claude Sonnet 5")
    methodology = (
        "<h2>Methodology</h2>"
        f"<p><b>Workload:</b> large-review (3 large files, ~32K est tok each), seed 42, "
        f"{MAX_TURNS} max turns. <b>Model:</b> {model_label}, "
        "direct Anthropic API, prefix caching via <code>cache_control: ephemeral</code>. "
        "<b>Context limit:</b> 200K tokens. <b>Arms:</b> naive (full history), "
        "compress-only (Hermes-style compressor), ToolRecall CT-only (context tracker strips "
        "clean file blocks), ct+compress.</p>"
        "<p>Cost from DB: <code>(prompt−cache_read)×$3/M + cache_read×$0.30/M + "
        "completion×$15/M</code> per arm. All figures pulled from the run DB.</p>"
    )

    shell = """<html><head><meta charset="utf-8"><title>""" + "%s" + """</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:960px;padding:0 1rem}
table{border-collapse:collapse;margin:1rem 0;font-size:.9rem}th,td{border:1px solid #ccc;padding:4px 8px;text-align:right}
th{background:#eee}tr td:first-child,tr th:first-child{text-align:left}
h1{font-size:1.6rem}a{color:#0645ad}</style></head><body>%s</body></html>"""

    pages = {
        "index": ("ToolRecall Claude Benchmark — Results", methodology + comp_html
                  + "<h2>Links</h2><p><a href='naive.html'>naive (baseline)</a> · "
                  "<a href='compress.html'>compress-only</a> · <a href='ct.html'>ct-only (ToolRecall)</a></p>"),
        "naive": ("ToolRecall Benchmark: Naive baseline",
                  methodology + results_table("Naive (full history, provider prefix caching)", "naive")),
        "compress": ("ToolRecall Benchmark: Compress-only",
                     methodology + results_table("Compress-only", "compress-only")),
        "ct": ("ToolRecall Benchmark: CT-only (ToolRecall)",
               methodology + results_table("ToolRecall CT-only", "ct-only")),
    }
    paths = {}
    for name, (title, body) in pages.items():
        p = os.path.join(SITE_DIR, name + ".html")
        with open(p, "w") as f:
            f.write(shell % (title, body))
        paths[name] = p
    return paths


def main():
    t0 = time.time()
    # Skip re-running if a completed DB already has real (ok) data for all 4 arms
    _, rows = db_rows()
    if rows:
        ok_arms = {r[0] for r in rows if r[7] == "ok"}
        if len(ok_arms) >= 4:
            print(f"DB {DB_NAME} already has ok data for {sorted(ok_arms)} — skipping benchmark, rebuilding sites.")
            rc = 0
        else:
            print(f"DB {DB_NAME} partial/empty ok arms ({sorted(ok_arms)}) — running fresh benchmark.")
            rc = run_bench()
    else:
        rc = run_bench()

    _, rows = db_rows()
    if not rows:
        print("NO ROWS in DB after run — benchmark likely failed. rc=%s" % rc, flush=True)
        return 1

    paths = build_sites(rows)
    n_arms = {r[0] for r in rows}
    print("\n=== OVERNIGHT BENCH DONE ===", flush=True)
    print("DB:", DB, flush=True)
    print("Arms present:", sorted(n_arms), flush=True)
    print("Sites built:", flush=True)
    for k, p in paths.items():
        print("  ", k, "->", p, flush=True)
    print("Elapsed: %.0fs" % (time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
