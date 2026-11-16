#!/usr/bin/env python3
"""Crossover analyzer and 5-gate verifier for ToolRecall A/B benchmarks.

Reports per turn-budget bucket and per (repo, workload):
  - net $ per solved task = (dedup avoided) - (cache-creation penalty) - (completion delta)
  - dedup avoided and cache-creation penalty separately (opposite signs)
  - crossover turn count where cumulative net $ turns positive
  - paired median + 95% bootstrap CI
  - 5-gate verification sequence before any public number can be quoted.

Usage:
  python3 bench/crossover/analyze_crossover.py ~/bench-data/crossover-out
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics as st
from collections import defaultdict
from pathlib import Path

BUCKETS = [(1, 20), (21, 40), (41, 80), (81, 150), (151, 250), (251, 10**9)]

# Default Anthropic Sonnet 4 list prices ($ per token)
DEFAULT_RATES = {
    "input": 3.00 / 1_000_000,
    "cache_read": 0.30 / 1_000_000,
    "cache_write": 3.75 / 1_000_000,
    "output": 15.00 / 1_000_000,
}


def bucket_of(n_turns: int) -> str:
    for lo, hi in BUCKETS:
        if lo <= n_turns <= hi:
            return f"{lo}-{hi if hi < 10**9 else '+'}"
    return "?"


def bootstrap_median_ci(
    values: list[float],
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Return (median, ci_low, ci_high) via deterministic percentile bootstrap."""
    if not values:
        return 0.0, 0.0, 0.0
    med = st.median(values)
    if len(values) == 1:
        return med, med, med
    rng = random.Random(seed)
    n = len(values)
    boots = sorted(st.median(rng.choices(values, k=n)) for _ in range(n_resamples))
    lo_idx = max(0, int((alpha / 2.0) * n_resamples))
    hi_idx = min(n_resamples - 1, int((1.0 - alpha / 2.0) * n_resamples))
    return med, boots[lo_idx], boots[hi_idx]


def decompose_pair(a: dict, b: dict, rates: dict[str, float] = DEFAULT_RATES) -> dict:
    """Exact algebraic decomposition of cost(A) - cost(B) into:
    net_usd = dedup_avoided - cache_creation_penalty - completion_delta.
    Positive net_usd means Arm B (ToolRecall) saved money vs Arm A (native).
    When provider-reported `cost_usd` > 0 is present on both runs, `billed_net_usd`
    gives the exact provider-billed difference (`cost_A - cost_B`).
    """
    dedup_avoided = (
        (a.get("input_tokens", 0) - b.get("input_tokens", 0)) * rates["input"]
        + (a.get("cache_read", 0) - b.get("cache_read", 0)) * rates["cache_read"]
    )
    cc_penalty = (b.get("cache_creation", 0) - a.get("cache_creation", 0)) * rates["cache_write"]
    comp_delta = (b.get("output_tokens", 0) - a.get("output_tokens", 0)) * rates["output"]
    net_usd = dedup_avoided - cc_penalty - comp_delta
    ca = float(a.get("cost_usd") or 0.0)
    cb = float(b.get("cost_usd") or 0.0)
    billed_net = (ca - cb) if (ca > 0 and cb > 0) else net_usd
    return {
        "dedup_avoided_usd": dedup_avoided,
        "cache_creation_penalty_usd": cc_penalty,
        "completion_delta_usd": comp_delta,
        "net_usd": billed_net,
        "list_net_usd": net_usd,
    }


def _load_turns(out_dir: Path, row: dict) -> list[dict]:
    tag = f"{row['repo']}__{row['task']}__{row['workload']}__{row['arm']}_r{row['rep']}"
    p = out_dir / f"{tag}.turns.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def find_crossover_turn(
    pairs: list[tuple[dict, dict]],
    out_dir: Path,
    rates: dict[str, float] = DEFAULT_RATES,
) -> int | None:
    """Find the earliest turn t where mean cumulative net_usd(1..t) > 0 and stays >= 0 at end.

    Uses provider-reported `cost_usd` per turn when present (> 0), otherwise
    falls back to token-rate decomposition.
    """
    cum_nets_by_turn: defaultdict[int, list[float]] = defaultdict(list)
    for a, b in pairs:
        ta = _load_turns(out_dir, a)
        tb = _load_turns(out_dir, b)
        if not ta or not tb:
            continue
        n = min(len(ta), len(tb))
        use_billed = any(t.get("cost_usd", 0.0) > 0 for t in ta[:n]) and any(
            t.get("cost_usd", 0.0) > 0 for t in tb[:n]
        )
        cum_cost_a = cum_cost_b = 0.0
        cum_a = {"input_tokens": 0, "cache_read": 0, "cache_creation": 0, "output_tokens": 0}
        cum_b = {"input_tokens": 0, "cache_read": 0, "cache_creation": 0, "output_tokens": 0}
        for i in range(n):
            if use_billed:
                cum_cost_a += float(ta[i].get("cost_usd") or 0.0)
                cum_cost_b += float(tb[i].get("cost_usd") or 0.0)
                cum_nets_by_turn[i + 1].append(cum_cost_a - cum_cost_b)
            else:
                for k in cum_a:
                    cum_a[k] += ta[i].get(k, 0)
                    cum_b[k] += tb[i].get(k, 0)
                cum_nets_by_turn[i + 1].append(decompose_pair(cum_a, cum_b, rates)["net_usd"])

    if not cum_nets_by_turn:
        return None
    sorted_turns = sorted(cum_nets_by_turn)
    means = {t: st.mean(cum_nets_by_turn[t]) for t in sorted_turns}
    if means[sorted_turns[-1]] <= 0:
        return None
    # Last turn where cumulative mean <= 0; crossover is the next turn.
    last_neg = 0
    for t in sorted_turns:
        if means[t] <= 0:
            last_neg = t
    for t in sorted_turns:
        if t > last_neg and means[t] > 0:
            return t
    return sorted_turns[0] if means[sorted_turns[0]] > 0 else None


def evaluate_gates(rows: list[dict], pairs: list[tuple[dict, dict]]) -> dict[str, dict]:
    """Evaluate the 5-gate sequence before any public number can be quoted."""
    gates: dict[str, dict] = {}

    # Gate 1: null arm ≈ 0% (95% CI contains 0)
    null_pairs = [(a, b) for a, b in pairs if a.get("workload") == "null"]
    if not null_pairs:
        gates["gate1_null_arm"] = {"status": "PENDING", "detail": "no null_qa pairs"}
    else:
        nets = [decompose_pair(a, b)["net_usd"] for a, b in null_pairs]
        med, lo, hi = bootstrap_median_ci(nets)
        ok = lo <= 0.0 <= hi
        gates["gate1_null_arm"] = {
            "status": "PASS" if ok else "FAIL",
            "detail": f"n={len(null_pairs)} median=${med:.5f} 95%CI=[${lo:.5f}, ${hi:.5f}]",
        }

    # Gate 2: equal work (hidden/suite tests pass in both arms on same task set)
    work_pairs = [(a, b) for a, b in pairs if a.get("workload") != "null"]
    if not work_pairs:
        gates["gate2_equal_work"] = {"status": "PENDING", "detail": "no workload pairs"}
    else:
        both_ok = sum(1 for a, b in work_pairs if a.get("resolved") and b.get("resolved"))
        res_a = sum(1 for a, _ in work_pairs if a.get("resolved"))
        res_b = sum(1 for _, b in work_pairs if b.get("resolved"))
        ok = both_ok == len(work_pairs)
        gates["gate2_equal_work"] = {
            "status": "PASS" if ok else "FAIL",
            "detail": f"both_resolved={both_ok}/{len(work_pairs)} (A={res_a}, B={res_b})",
        }

    # Gate 3: arm verified from its own data (not label)
    long_or_rep_b = [r for r in rows if r.get("arm") == "B" and (r.get("repeat_share") or 0) > 0.05]
    arm_a_stubs = [r for r in rows if r.get("arm") == "A" and (r.get("stub_bytes_served") or 0) > 0]
    if not long_or_rep_b:
        gates["gate3_arm_verified"] = {"status": "PENDING", "detail": "no Arm B runs with repeat_share > 0.05"}
    else:
        b_active = sum(1 for r in long_or_rep_b if (r.get("stub_bytes_served") or 0) > 0)
        ok = (b_active == len(long_or_rep_b)) and (len(arm_a_stubs) == 0)
        gates["gate3_arm_verified"] = {
            "status": "PASS" if ok else "FAIL",
            "detail": f"B_with_stubs={b_active}/{len(long_or_rep_b)}, A_with_stubs={len(arm_a_stubs)}",
        }

    # Gate 4: order counterbalanced
    orders = [a.get("arm_order") for a, _ in pairs if a.get("arm_order")]
    if not orders:
        gates["gate4_counterbalanced"] = {"status": "PENDING", "detail": "no arm_order recorded"}
    else:
        ab = sum(1 for o in orders if o == "AB")
        ba = sum(1 for o in orders if o == "BA")
        ok = ab > 0 and ba > 0 and abs(ab - ba) <= max(1, int(0.2 * len(orders)))
        gates["gate4_counterbalanced"] = {
            "status": "PASS" if ok else "FAIL",
            "detail": f"AB={ab}, BA={ba}",
        }

    # Gate 5: paired median + bootstrap CI computed
    if not work_pairs:
        gates["gate5_paired_ci"] = {"status": "PENDING", "detail": "no workload pairs"}
    else:
        nets = [decompose_pair(a, b)["net_usd"] for a, b in work_pairs]
        med, lo, hi = bootstrap_median_ci(nets)
        ok = len(work_pairs) >= 3
        gates["gate5_paired_ci"] = {
            "status": "PASS" if ok else "PENDING",
            "detail": f"n={len(work_pairs)} median=${med:.4f} 95%CI=[${lo:.4f}, ${hi:.4f}]",
        }

    return gates


def analyze(out_dir: Path, rates: dict[str, float] = DEFAULT_RATES) -> dict:
    runs_path = out_dir / "runs.jsonl"
    if not runs_path.exists():
        raise FileNotFoundError(f"No runs.jsonl found at {runs_path}")
    rows = [json.loads(line) for line in runs_path.read_text().splitlines() if line.strip()]

    by_key: dict[tuple[str, str, str, int], dict] = {}
    for r in rows:
        by_key[(r.get("repo", ""), r["task"], r.get("workload", ""), int(r["rep"]))] = r

    # Pair A and B by (repo, task, workload, rep)
    grouped: defaultdict[tuple[str, str, str, int], dict[str, dict]] = defaultdict(dict)
    for r in rows:
        grouped[(r.get("repo", ""), r["task"], r.get("workload", ""), int(r["rep"]))][r["arm"]] = r

    pairs: list[tuple[dict, dict]] = []
    for key in sorted(grouped):
        arms = grouped[key]
        if "A" in arms and "B" in arms:
            pairs.append((arms["A"], arms["B"]))

    # Report per turn-budget bucket
    by_bucket: defaultdict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for a, b in pairs:
        if a.get("workload") == "null":
            continue
        t_ref = max(a.get("turns") or a.get("n_turns") or 0, b.get("turns") or b.get("n_turns") or 0)
        by_bucket[bucket_of(t_ref)].append((a, b))

    # Report per (repo, workload)
    by_cell: defaultdict[tuple[str, str], list[tuple[dict, dict]]] = defaultdict(list)
    for a, b in pairs:
        by_cell[(a.get("repo", ""), a.get("workload", ""))].append((a, b))

    gates = evaluate_gates(rows, pairs)
    crossover_turn = find_crossover_turn([p for p in pairs if p[0].get("workload") != "null"], out_dir, rates)

    return {
        "n_rows": len(rows),
        "n_pairs": len(pairs),
        "by_bucket": by_bucket,
        "by_cell": by_cell,
        "crossover_turn": crossover_turn,
        "gates": gates,
    }


def print_report(res: dict, out_dir: Path, rates: dict[str, float] = DEFAULT_RATES) -> None:
    print(f"Crossover Analysis ({out_dir}): {res['n_rows']} runs, {res['n_pairs']} paired runs")
    print(f"Estimated crossover turn count (cumulative net > $0): {res['crossover_turn']}")

    print("\n## Per Turn-Budget Bucket (paired B vs A; positive net = ToolRecall saves $)")
    print(
        f"  {'bucket':>9} {'pairs':>5} {'dedup_avoid':>12} {'cc_penalty':>11} "
        f"{'comp_delta':>11} {'med_net$':>10} {'95%_CI':>21} {'repeat_share':>13}"
    )
    for lo, hi in BUCKETS:
        b = f"{lo}-{hi if hi < 10**9 else '+'}"
        plist = res["by_bucket"].get(b, [])
        if not plist:
            continue
        decomps = [decompose_pair(x, y, rates) for x, y in plist]
        med, ci_lo, ci_hi = bootstrap_median_ci([d["net_usd"] for d in decomps])
        da = st.median(d["dedup_avoided_usd"] for d in decomps)
        cp = st.median(d["cache_creation_penalty_usd"] for d in decomps)
        cd = st.median(d["completion_delta_usd"] for d in decomps)
        rs = st.median(y.get("repeat_share", 0.0) for _, y in plist)
        print(
            f"  {b:>9} {len(plist):>5} ${da:>10.4f} ${cp:>9.4f} "
            f"${cd:>9.4f} ${med:>8.4f} [${ci_lo:>7.4f}, ${ci_hi:>7.4f}] {rs:>12.3f}"
        )

    print("\n## Per (Repo, Workload) Cell")
    print(
        f"  {'cell':>20} {'pairs':>5} {'dedup_avoid':>12} {'cc_penalty':>11} "
        f"{'med_net$':>10} {'95%_CI':>21} {'xover_turn':>11}"
    )
    for (repo, wl), plist in sorted(res["by_cell"].items()):
        decomps = [decompose_pair(x, y, rates) for x, y in plist]
        med, ci_lo, ci_hi = bootstrap_median_ci([d["net_usd"] for d in decomps])
        da = st.median(d["dedup_avoided_usd"] for d in decomps)
        cp = st.median(d["cache_creation_penalty_usd"] for d in decomps)
        xt = find_crossover_turn(plist, out_dir, rates) if wl != "null" else None
        print(
            f"  {f'{repo}/{wl}':>20} {len(plist):>5} ${da:>10.4f} ${cp:>9.4f} "
            f"${med:>8.4f} [${ci_lo:>7.4f}, ${ci_hi:>7.4f}] {str(xt or '-'):>11}"
        )

    print("\n## 5-Gate Verification Sequence")
    all_pass = True
    for gname, ginfo in res["gates"].items():
        if ginfo["status"] != "PASS":
            all_pass = False
        print(f"  [{ginfo['status']:7s}] {gname}: {ginfo['detail']}")
    print(f"\nQuotable public result: {'YES (all 5 gates PASS)' if all_pass else 'NO (gates incomplete/failed)'}")

    # Optional per-bug cumulative crossover when endurance per_bug arrays are present
    for (repo, wl), plist in sorted(res["by_cell"].items()):
        a0, b0 = plist[0]
        if a0.get("per_bug") and b0.get("per_bug"):
            print(f"\n## Per-Bug Cumulative Crossover ({repo}/{wl}, rep {a0['rep']})")
            print(f"  {'bug':>8} {'cum_A_$':>9} {'cum_B_$':>9} {'cum_net_$':>10} {'cum_pct':>8}")
            ca = cb = 0.0
            for ba, bb in zip(a0["per_bug"], b0["per_bug"]):
                ca += float(ba.get("cost_usd") or 0.0)
                cb += float(bb.get("cost_usd") or 0.0)
                net = ca - cb
                pct = (100.0 * net / ca) if ca > 0 else 0.0
                print(f"  {ba['bug']:>8} ${ca:>8.4f} ${cb:>8.4f} ${net:>+9.4f} {pct:>+7.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze crossover benchmark runs and check 5 gates")
    ap.add_argument("out_dir", nargs="?", type=Path, default=Path(os.path.expanduser("~/bench-data/crossover-out")))
    ap.add_argument("--ingest-endurance", type=Path, default=None, help="Convert run_*_endurance.py JSON first")
    ap.add_argument("--data-dir", type=Path, default=Path("bench/data"), help="Directory containing usage-*.csv")
    ap.add_argument("--repo", default="django")
    ap.add_argument("--harness", default="hermes")
    ap.add_argument("--arm-b", default="tr_delta_dedup", choices=["tr_default", "tr_delta_dedup", "tr_optin"])
    args = ap.parse_args()
    if args.ingest_endurance:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import instrument as inst
        inst.ingest_endurance_results(
            args.ingest_endurance,
            args.data_dir,
            args.out_dir,
            repo=args.repo,
            harness_filter=args.harness,
            arm_b=args.arm_b,
        )
    res = analyze(args.out_dir)
    print_report(res, args.out_dir)


if __name__ == "__main__":
    main()
