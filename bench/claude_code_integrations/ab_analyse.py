#!/usr/bin/env python3
"""Analyse ab_swe.py runs: billed cost per run (replayed calls = 0), resolution, turns, wall.

Billed USD per run = Claude Code's own total_cost_usd scaled by billed/total weighted tokens of the
main-chain calls (weights = Anthropic list-price ratios: input 1, 5m cache write 1.25,
1h cache write 2, cache read 0.1, output 5). A call is "replayed" when its message id already
appeared in an earlier run of the same task in arm B (the proxy served it; it was never billed).
"""
import json, os, re, statistics as st, sys
from collections import defaultdict

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/bench-data/ab-out")
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
    order = [k in rep for k in m]  # per call, in order: replayed?
    r["calls"], r["replayed"] = len(m), len(rep)
    r["replay_prefix"] = next((i for i, x in enumerate(order) if not x), len(order))
    r["usd"] = (r["cc_cost_usd"] or 0) * (billed / full) if full else (r["cc_cost_usd"] or 0)


# Map each task and arm to its first run (rep 1) resolved outcome
base_outcomes = {(r["task"], r["arm"]): r["resolved"] for r in rows if r["rep"] == 1 and r.get("scenario", "base") == "base"}


def summ(sel, label, is_rerun=False):
    out = {}
    for arm in ("A", "B"):
        rs = [r for r in sel if r["arm"] == arm]
        if not rs:
            continue
        frozen = sum(1 for r in rs if r["resolved"] == base_outcomes.get((r["task"], r["arm"])))
        out[arm] = dict(n=len(rs), usd=sum(r["usd"] for r in rs), mean=st.mean(r["usd"] for r in rs),
                        res=sum(r["resolved"] for r in rs), frozen=frozen, turns=st.mean(r["turns"] or 0 for r in rs),
                        wall=st.mean(r["wall"] for r in rs), calls=sum(r["calls"] for r in rs),
                        rep=sum(r["replayed"] for r in rs))
    if "A" in out and "B" in out:
        a, b = out["A"], out["B"]
        print(f"\n## {label}")
        if is_rerun:
            print(f"{'':10} {'runs':>4} {'outcome frozen':>16} {'total $':>9} {'mean $/run':>11} {'turns':>6} {'wall s':>7} {'calls replayed':>15}")
            print(f"{'native':10} {a['n']:>4} {'(sampled live)':>16} {a['usd']:>9.3f} {a['mean']:>11.4f} {a['turns']:>6.1f} {a['wall']:>7.1f} {a['rep']:>7}/{a['calls']:<7}")
            b_froz = f"{b['frozen']}/{b['n']} ({100*b['frozen']/b['n']:.0f}%)"
            print(f"{'toolrecall':10} {b['n']:>4} {b_froz:>16} {b['usd']:>9.3f} {b['mean']:>11.4f} {b['turns']:>6.1f} {b['wall']:>7.1f} {b['rep']:>7}/{b['calls']:<7}")
        else:
            print(f"{'':10} {'runs':>4} {'resolved':>9} {'total $':>9} {'mean $/run':>11} {'turns':>6} {'wall s':>7} {'calls replayed':>15}")
            for arm, nm in (("A", "native"), ("B", "toolrecall")):
                x = out[arm]
                print(f"{nm:10} {x['n']:>4} {x['res']:>4}/{x['n']:<4} {x['usd']:>9.3f} {x['mean']:>11.4f} {x['turns']:>6.1f} "
                      f"{x['wall']:>7.1f} {x['rep']:>7}/{x['calls']:<7}")
        print(f"Δ cost B vs A: {100 * (b['usd'] - a['usd']) / a['usd']:+.1f}%   "
              f"Δ wall: {100 * (b['wall'] - a['wall']) / a['wall']:+.1f}%")
        if is_rerun:
            print("  * Note: Reruns test cost and outcome freezing under replay. Quality must be evaluated on first runs.")
    return out


print(f"{len(rows)} runs")
if any(r.get("scenario", "legacy") != "legacy" for r in rows):
    LABEL = {("base", 1): "cold run", ("base", 2): "identical rerun", ("base", 3): "identical rerun 2", ("codecommit", 1): "rerun, code commit", ("newcommit", 1): "rerun, new commit",
             ("promptedit", 1): "rerun, prompt edited", ("shuffle", 1): "cold run, --shuffle",
             ("shuffle", 2): "rerun, --shuffle (volatile output)"}
    print("\n## Repeat scenarios: native vs ToolRecall (5 tasks each)")
    print(f"  {'scenario':36} {'native $':>9} {'TR $':>8} {'Δ':>7}  {'TR calls replayed':>18}  {'TR outcome':>11}  replay held through (per task)")
    for key, label in LABEL.items():
        sel = [r for r in rows if (r.get("scenario"), r["rep"]) == key]
        a = [r for r in sel if r["arm"] == "A"]
        b = [r for r in sel if r["arm"] == "B"]
        if not a or not b:
            continue
        ua, ub = sum(r["usd"] for r in a), sum(r["usd"] for r in b)
        held = ", ".join(("all" if r["replay_prefix"] == r["calls"] else f"{r['replay_prefix']}/{r['calls']}")
                         for r in sorted(b, key=lambda r: r["task"]))
        scen_name, scen_rep = key
        if scen_rep == 1 and scen_name in ("base", "shuffle"):
            tr_out = f"{sum(r['resolved'] for r in b)}/{len(b)} solved"
        else:
            match_base = sum(1 for r in b if r["resolved"] == base_outcomes.get((r["task"], "B")))
            tr_out = f"{match_base}/{len(b)} frozen"
        print(f"  {label:36} {ua:>9.3f} {ub:>8.3f} {100 * (ub - ua) / ua:>+6.0f}%  "
              f"{sum(r['replayed'] for r in b):>8}/{sum(r['calls'] for r in b):<9}  {tr_out:>11}  {held}")
    if "--json" in sys.argv:
        json.dump(rows, open(f"{OUT}/analysed.json", "w"), indent=1)
    sys.exit(0)
summ([r for r in rows if r["rep"] == 1], "One-off use (rep 1: independent attempts — quality & baseline cost)")
summ([r for r in rows if r["rep"] > 1], "Repeated use (reps 2+: same task again — cost & outcome fidelity)", is_rerun=True)
summ(rows, "All runs (reference)")

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

# ── Turns: when does replay pay off? ────────────────────────────────────────
reruns_b = [r for r in rows if r["arm"] == "B" and r["rep"] > 1]
print("\n## Replay per rerun (ToolRecall arm): calls replayed before the first live call")
for r in sorted(reruns_b, key=lambda r: (r["task"], r["rep"])):
    held = "all" if r["replay_prefix"] == r["calls"] else f"first {r['replay_prefix']}"
    print(f"  {r['task'][-5:]} run {r['rep']}: {r['turns']:>3} turns, {r['calls']:>3} calls, replayed {held}")

if reruns_b:
    kmax = max(r["calls"] for r in reruns_b)
    print("\n## Survival: share of reruns still replaying at call k")
    marks = [k for k in (1, 2, 3, 5, 8, 13, 21, 34) if k <= kmax]
    print("  " + "  ".join(f"k={k}: {sum(1 for r in reruns_b if r['calls'] >= k and r['replay_prefix'] >= k)}/"
                         f"{sum(1 for r in reruns_b if r['calls'] >= k)}" for k in marks))

print("\n## Savings by session length (task length = native mean turns)")
print(f"  {'task':6} {'native turns':>12} {'native $/rerun':>15} {'TR $/rerun':>11} {'saving':>8} {'1st run TR vs native':>21}")
for t in sorted({r["task"] for r in rows}, key=lambda t: st.mean(r["turns"] or 0 for r in rows if r["task"] == t and r["arm"] == "A")):
    a_all = [r for r in rows if r["task"] == t and r["arm"] == "A"]
    a = [r["usd"] for r in a_all if r["rep"] > 1]
    b = [r["usd"] for r in rows if r["task"] == t and r["arm"] == "B" and r["rep"] > 1]
    a1 = [r["usd"] for r in a_all if r["rep"] == 1]
    b1 = [r["usd"] for r in rows if r["task"] == t and r["arm"] == "B" and r["rep"] == 1]
    if a and b and a1 and b1:
        print(f"  {t[-5:]:6} {st.mean(r['turns'] or 0 for r in a_all):>12.1f} {st.mean(a):>15.4f} {st.mean(b):>11.4f} "
              f"{100 * (st.mean(b) - st.mean(a)) / st.mean(a):>+7.0f}% {100 * (b1[0] - a1[0]) / a1[0]:>+20.0f}%")

print("\n## Break-even: cumulative cost after n runs of the same task (all 5 tasks)")
reps = sorted({r["rep"] for r in rows})
ca = cb = 0.0
for n in reps:
    ca += sum(r["usd"] for r in rows if r["arm"] == "A" and r["rep"] == n)
    cb += sum(r["usd"] for r in rows if r["arm"] == "B" and r["rep"] == n)
    print(f"  after {n} run(s): native ${ca:.3f}  ToolRecall ${cb:.3f}  ({100 * (cb - ca) / ca:+.1f}%)")

if "--json" in sys.argv:
    json.dump(rows, open(f"{OUT}/analysed.json", "w"), indent=1)
