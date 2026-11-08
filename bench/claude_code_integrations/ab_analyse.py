#!/usr/bin/env python3
"""Analyse ab_swe.py runs: billed cost per run (replayed calls = 0), resolution, turns, wall.

Billed USD per run = Claude Code's own total_cost_usd scaled by billed/total weighted tokens of the
main-chain calls (weights = Anthropic list-price ratios: input 1, 5m cache write 1.25,
1h cache write 2, cache read 0.1, output 5). A call is "replayed" when its message id already
appeared in an earlier run of the same task in arm B (the proxy served it; it was never billed).
"""
import json, os, re, statistics as st, sys
from collections import defaultdict

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/trverify/ab/out"
PROJ = os.path.expanduser("~/.claude/projects")


def units(u):
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens")
    w5m = cc.get("ephemeral_5m_input_tokens")
    cw = u.get("cache_creation_input_tokens", 0)
    if w1h is None and w5m is None:
        w5m, w1h = cw, 0
    return (u.get("input_tokens", 0) + 1.25 * (w5m or 0) + 2.0 * (w1h or 0)
            + 0.1 * u.get("cache_read_input_tokens", 0) + 5 * u.get("output_tokens", 0))


def calls(row):
    slug = re.sub(r"[^A-Za-z0-9]", "-", row["cwd"])
    p = f"{PROJ}/{slug}/{row['session_id']}.jsonl"
    msgs = {}
    if not os.path.exists(p):
        return msgs
    for line in open(p):
        d = json.loads(line)
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        m = d.get("message") or {}
        if m.get("id") and m["id"] not in msgs:
            msgs[m["id"]] = units(m.get("usage") or {})
    return msgs


rows = [json.loads(l) for l in open(f"{OUT}/runs.jsonl")]
rows.sort(key=lambda r: r["ts"])
seen_b = defaultdict(set)
for r in rows:
    m = calls(r)
    full = sum(m.values())
    if r["arm"] == "B":
        rep = [k for k in m if k in seen_b[r["task"]]]
        seen_b[r["task"]] |= set(m)
    else:
        rep = []
    billed = sum(v for k, v in m.items() if k not in rep)
    r["calls"], r["replayed"] = len(m), len(rep)
    r["usd"] = (r["cc_cost_usd"] or 0) * (billed / full) if full else (r["cc_cost_usd"] or 0)


def summ(sel, label):
    out = {}
    for arm in ("A", "B"):
        rs = [r for r in sel if r["arm"] == arm]
        if not rs:
            continue
        out[arm] = dict(n=len(rs), usd=sum(r["usd"] for r in rs), mean=st.mean(r["usd"] for r in rs),
                        res=sum(r["resolved"] for r in rs), turns=st.mean(r["turns"] or 0 for r in rs),
                        wall=st.mean(r["wall"] for r in rs), calls=sum(r["calls"] for r in rs),
                        rep=sum(r["replayed"] for r in rs))
    if "A" in out and "B" in out:
        a, b = out["A"], out["B"]
        print(f"\n## {label}")
        print(f"{'':10} {'runs':>4} {'resolved':>9} {'total $':>9} {'mean $/run':>11} {'turns':>6} {'wall s':>7} {'calls replayed':>15}")
        for arm, nm in (("A", "native"), ("B", "toolrecall")):
            x = out[arm]
            print(f"{nm:10} {x['n']:>4} {x['res']:>4}/{x['n']:<4} {x['usd']:>9.3f} {x['mean']:>11.4f} {x['turns']:>6.1f} "
                  f"{x['wall']:>7.1f} {x['rep']:>7}/{x['calls']:<7}")
        print(f"Δ cost B vs A: {100 * (b['usd'] - a['usd']) / a['usd']:+.1f}%   "
              f"Δ wall: {100 * (b['wall'] - a['wall']) / a['wall']:+.1f}%")
    return out


print(f"{len(rows)} runs")
summ(rows, "All runs")
summ([r for r in rows if r["rep"] == 1], "One-off use (rep 1: B's cache is cold)")
summ([r for r in rows if r["rep"] > 1], "Repeated use (reps 2+: same task again)")

print("\n## Per task (USD per run, A | B; resolved)")
for t in sorted({r["task"] for r in rows}):
    line = [t.replace("django__django-", "dj-")]
    for rep in sorted({r["rep"] for r in rows}):
        cell = []
        for arm in ("A", "B"):
            x = [r for r in rows if r["task"] == t and r["rep"] == rep and r["arm"] == arm]
            cell.append(f"{x[0]['usd']:.3f}{'✓' if x[0]['resolved'] else '✗'}" if x else "  -  ")
        line.append(f"r{rep} {cell[0]} | {cell[1]}")
    print("  ".join(line))
if "--json" in sys.argv:
    json.dump(rows, open(f"{OUT}/analysed.json", "w"), indent=1)
