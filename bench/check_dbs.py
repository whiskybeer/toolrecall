#!/usr/bin/env python3
import sqlite3, os, glob

bench_dir = os.path.expanduser("~/.toolrecall/bench-runs")

prefix_dbs = sorted(glob.glob(os.path.join(bench_dir, "526-pilot-prefix*.db")))
toolrecall_dbs = sorted(glob.glob(os.path.join(bench_dir, "526-pilot-toolrecall*.db")))

for name, files in [("prefix", prefix_dbs), ("toolrecall", toolrecall_dbs)]:
    if not files:
        print(f"{name}: NO DB FOUND")
        continue
    db = files[-1]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    turns = con.execute("SELECT COUNT(*) FROM turn_log").fetchone()[0]
    last = con.execute("SELECT turn_index, request_tokens, prompt_tokens, status FROM turn_log ORDER BY turn_index DESC LIMIT 1").fetchone()
    print(f"{name}: {os.path.basename(db)} {turns} turns, last={last[0]}, req_tok={last[1]}, prov_tok={last[2]}, status={last[3]}")
    rows = con.execute("SELECT turn_index, request_tokens, prompt_tokens, ctx_dropped_tokens_cum FROM turn_log ORDER BY turn_index").fetchall()
    for r in rows:
        print(f"  turn {r[0]:>2}: req_tok={r[1]:>6}, prov_tok={r[2]:>6}, dropped_cum={r[3]:>6}")
    con.close()