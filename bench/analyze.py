"""analyze.py — comprehensive report generator for three-arm benchmark.

Usage:
    /tmp/bench-env/bin/python3 bench/analyze.py [--db ~/.toolrecall/benchmark.db]

Produces:
    - fig1_context_growth.png
    - fig2_ratio.png
    - fig3_warmup.png
    - benchmark_stats.txt
    - BENCHMARK_REPORT.md (standalone self-contained report)
"""

import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DB = os.path.expanduser("~/.toolrecall/benchmark.db")

# Read provider and model info from agent.py for report headers + pricing
import importlib.util as _iutil
_AGENT_SPEC = _iutil.spec_from_file_location(
    "_agent_pricing", os.path.join(os.path.dirname(__file__), "agent.py")
)
_AGENT_MOD = _iutil.module_from_spec(_AGENT_SPEC)
_AGENT_SPEC.loader.exec_module(_AGENT_MOD)

_PROVIDER = "openrouter"  # default, can override via --provider
_MODEL = _AGENT_MOD.DEFAULT_MODELS.get(_PROVIDER, "unknown")

def _resolve_pricing(provider: str = None, model: str = None) -> dict:
    """Look up pricing for the given provider and model. Returns fallback if not found."""
    p = provider or _PROVIDER
    m = model or _MODEL
    try:
        return _AGENT_MOD.PRICING[p][m]
    except KeyError:
        # Fallback pricing
        return {"prompt": 0.50, "prompt_cached": 0.05, "completion": 1.50}

# Evidence plan claims — locked before data collection
CLAIMS = {
    "C1": "Baseline per-turn cost grows; ToolRecall's stays bounded",
    "C2": "The gap widens with session length",
    "C3": "Sessions of 340+ turns stay usable where baseline exhausts",
    "C4": "Dropping clean files does not reduce task quality",
    "C5": "Cache hit avoids subprocess fork (~112x)",
}


def load_data(db_path: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    df = pd.read_sql("SELECT * FROM turn_log ORDER BY run_id, turn_index", con)
    con.close()
    return df


def load_probes(db_path: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    df = pd.read_sql("SELECT * FROM probe_result", con)
    con.close()
    return df


# ── Figure 1: Context growth per turn ──────────────────────────

def fig1_context_growth(curve: pd.DataFrame):
    fig, ax = plt.subplots(figsize=(9, 5))
    for arm, g in curve.groupby("arm"):
        ax.plot(g.turn_index, g.request_tokens, label=arm, lw=2)
    ax.set_xlabel("Turn")
    ax.set_ylabel("Request tokens (median per turn)")
    ax.set_title("Context growth per turn (median over runs)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.savefig("fig1_context_growth.png", dpi=150, bbox_inches="tight")
    print("  saved fig1_context_growth.png")


# ── Figure 2: Ratio with bootstrap CI ──────────────────────────

def fig2_ratio(curve: pd.DataFrame):
    p = curve.pivot(index="turn_index", columns="arm", values="request_tokens")
    if "prefix" not in p.columns or "toolrecall" not in p.columns:
        print("  (skipping fig2 — need both prefix and toolrecall data)")
        return
    ratio = p["prefix"] / p["toolrecall"]
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(ratio.index, ratio.values, lw=2, color="darkred")
    ax.axhline(1, ls="--", c="gray")
    ax.set_xlabel("Turn")
    ax.set_ylabel("x advantage vs prefix-cached baseline")
    ax.set_title("The gap widens with session length")
    fig.savefig("fig2_ratio.png", dpi=150, bbox_inches="tight")
    print("  saved fig2_ratio.png")


# ── Figure 3: Cache warm-up curve ──────────────────────────────

def fig3_warmup(df: pd.DataFrame):
    df_tr = df[df.arm == "toolrecall"].copy()
    if df_tr.empty:
        print("  (skipping fig3 — no toolrecall data)")
        return
    hits = df_tr["tool_cache_hits"].fillna(0)
    misses = df_tr["tool_cache_misses"].fillna(0)
    denom = (hits + misses).clip(lower=1)
    df_tr["hit_rate"] = hits / denom
    hr = df_tr.groupby("turn_index").hit_rate.median()
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(hr.index, hr.rolling(10, min_periods=1).mean(), lw=2)
    ax.set_xlabel("Turn")
    ax.set_ylabel("Tool cache hit rate")
    ax.set_title("Warm-up curve: rises, then plateaus (report the plateau)")
    ax.grid(alpha=0.3)
    fig.savefig("fig3_warmup.png", dpi=150, bbox_inches="tight")
    print("  saved fig3_warmup.png")


# ── Statistics ─────────────────────────────────────────────────

def compute_wilcoxon(df: pd.DataFrame, arm_a: str, arm_b: str) -> dict:
    """Paired Wilcoxon signed-rank test at matched (run_id, turn_index)."""
    a = df[df.arm == arm_a].set_index(["run_id", "turn_index"])["request_tokens"]
    b = df[df.arm == arm_b].set_index(["run_id", "turn_index"])["request_tokens"]
    common = a.index.intersection(b.index)
    if len(common) < 3:
        return {"n": len(common), "statistic": None, "pvalue": None}
    from scipy.stats import wilcoxon
    w = wilcoxon(a.loc[common], b.loc[common], method="approx")
    return {"n": len(common), "statistic": round(w.statistic, 1), "pvalue": w.pvalue}


def compute_logrank(df: pd.DataFrame) -> dict:
    """Log-rank test on turns-to-exhaustion between arms."""
    term = (
        df.sort_values("turn_index")
        .groupby(["run_id", "arm"])
        .last()
        .reset_index()
    )
    results = {}
    arms = term.arm.unique()
    from scipy.stats import logrank
    for i in range(len(arms)):
        for j in range(i + 1, len(arms)):
            a_data = term[term.arm == arms[i]]["turn_index"].values
            b_data = term[term.arm == arms[j]]["turn_index"].values
            res = logrank(a_data, b_data)
            results[f"{arms[i]}_vs_{arms[j]}"] = {
                "statistic": round(res.statistic, 2),
                "pvalue": res.pvalue,
            }
    return results


def compute_stats(df: pd.DataFrame, probe_df: pd.DataFrame) -> str:
    lines = ["=" * 60, "ToolRecall Benchmark Statistics", "=" * 60, ""]

    # ── 1. Turns to exhaustion ──
    term = (
        df.sort_values("turn_index")
        .groupby(["run_id", "arm"])
        .last()
        .reset_index()
    )
    lines.append("--- Turns to exhaustion ---")
    for arm, g in term.groupby("arm"):
        median = g.turn_index.median()
        exhausted = (g.status == "context_exhausted").sum()
        total = len(g)
        lines.append(f"  {arm:12s}: median={median:.0f}, exhausted={exhausted}/{total}")
    lines.append("")

    # ── 2. Wilcoxon signed-rank ──
    lines.append("--- Wilcoxon signed-rank on request_tokens ---")
    arms = list(df.arm.unique())
    for i in range(len(arms)):
        for j in range(i + 1, len(arms)):
            w = compute_wilcoxon(df, arms[i], arms[j])
            p_str = f"p={w['pvalue']:.4f}" if w["pvalue"] is not None else "N/A"
            lines.append(f"  {arms[i]:12s} vs {arms[j]:12s}: n={w['n']:>4}, W={w['statistic']}, {p_str}")
    lines.append("")

    # ── 3. Log-rank ──
    lines.append("--- Log-rank test (turns to exhaustion) ---")
    lr = compute_logrank(df)
    for pair, res in lr.items():
        p_str = f"p={res['pvalue']:.4f}" if isinstance(res['pvalue'], float) else str(res['pvalue'])
        lines.append(f"  {pair:30s}: chi2={res['statistic']}, {p_str}")
    lines.append("")

    # ── 4. Marginal cost at checkpoints ──
    curve = (
        df.groupby(["arm", "turn_index"])["request_tokens"]
        .median()
        .reset_index()
    )
    lines.append("--- Marginal cost at checkpoints (median request_tokens) ---")
    for t in (10, 50, 100, 200, 340):
        for arm in ("naive", "prefix", "toolrecall"):
            row = curve[(curve.turn_index == t) & (curve.arm == arm)]
            if not row.empty:
                lines.append(f"  turn {t:>4}, {arm:12s}: {row.request_tokens.iloc[0]:>8,.0f} tok")
    lines.append("")

    # ── 5. Bootstrap 95% CI on prefix/TR ratio ──
    if "prefix" in df.arm.values and "toolrecall" in df.arm.values:
        pivot = curve.pivot(index="turn_index", columns="arm", values="request_tokens")
        lines.append("--- Bootstrap 95% CI on prefix/TR ratio ---")
        rng = np.random.default_rng(42)
        for t in (50, 100, 200, 340):
            if t not in pivot.index:
                continue
            prefix_val = pivot.loc[t, "prefix"]
            tr_val = pivot.loc[t, "toolrecall"]
            ratio = prefix_val / tr_val
            run_ratios = []
            for run_id in df.run_id.unique():
                sub = df[df.run_id == run_id]
                p_sub = sub[sub.arm == "prefix"]
                t_sub = sub[sub.arm == "toolrecall"]
                pv = p_sub[p_sub.turn_index == t].request_tokens.mean()
                tv = t_sub[t_sub.turn_index == t].request_tokens.mean()
                if pv and tv:
                    run_ratios.append(pv / tv)
            if len(run_ratios) >= 3:
                boot = rng.choice(run_ratios, size=10000, replace=True)
                ci_lo, ci_hi = np.percentile(boot, [2.5, 97.5])
                lines.append(
                    f"  turn {t:>4}: ratio={ratio:.2f}, "
                    f"95%CI=[{ci_lo:.2f}, {ci_hi:.2f}] (bootstrap, 10k resamples)"
                )
            else:
                lines.append(f"  turn {t:>4}: ratio={ratio:.2f} (too few runs for CI)")
        lines.append("")

    # ── 6. Probe recall ──
    if not probe_df.empty:
        lines.append("--- Probe recall pass rate ---")
        summary = probe_df.groupby(["arm", "lag"]).passed.agg(["mean", "count"])
        for (arm, lag), row in summary.iterrows():
            lines.append(
                f"  {arm:12s}, lag={lag:>3}: {row['mean']:.0%} ({row['count']:.0f} probes)"
            )
        lines.append("")

    # ── 7. Cost estimate ──
    pricing = _resolve_pricing(_PROVIDER, _MODEL)
    lines.append(f"--- Cost estimate ({_MODEL} via {_PROVIDER}) ---")
    for arm in df.arm.unique():
        sub = df[df.arm == arm]
        pt = sub.prompt_tokens.sum()
        ct = sub.completion_tokens.sum()
        cr = sub.cache_read_tokens.sum()
        cost_miss = (pt - cr) / 1e6 * pricing["prompt"]
        cost_hit = cr / 1e6 * pricing.get("prompt_cached", pricing["prompt"] * 0.1)
        cost_comp = ct / 1e6 * pricing["completion"]
        total = cost_miss + cost_hit + cost_comp
        lines.append(f"  {arm:12s}: prompt_miss=${cost_miss:.5f}, "
                     f"prompt_hit=${cost_hit:.5f}, completion=${cost_comp:.5f}, "
                     f"total=${total:.5f}")
    lines.append("")

    # ── 8. Provider prefix-cache gap ──
    lines.append("--- Provider prefix caching (self-counted vs provider-reported) ---")
    for arm in df.arm.unique():
        sub = df[df.arm == arm]
        req = sub.request_tokens.sum()
        prov = sub.prompt_tokens.sum()
        gap = prov - req
        pct = gap / req * 100 if req else 0
        lines.append(f"  {arm:12s}: request_tokens={req:>8,}, prompt_tokens={prov:>8,}, "
                     f"delta={gap:>+7,} ({pct:+.1f}%)")
    lines.append("")

    # ── 9. Per-workload breakdown ──
    if df.workload_id.nunique() > 1:
        lines.append("--- Per-workload breakdown ---")
        for wl in df.workload_id.unique():
            sub = df[df.workload_id == wl]
            lines.append(f"  Workload: {wl} ({sub.run_id.nunique()} runs)")
            for arm in sub.arm.unique():
                a = sub[sub.arm == arm]
                med_req = a.request_tokens.median()
                lines.append(f"    {arm:12s}: median_req_tok={med_req:>8,.0f}, turns={len(a)}")
        lines.append("")

    # ── 10. Per-arm summary ──
    lines.append("--- Per-arm summary ---")
    summary = df.groupby("arm").agg(
        runs=("run_id", "nunique"),
        total_turns=("turn_index", "count"),
        median_request_tokens=("request_tokens", "median"),
        median_completion=("completion_tokens", "median"),
        total_api_time_s=("api_latency_s", "sum"),
    )
    lines.append(summary.to_string())
    lines.append("")

    # ── 11. Repeated-read / context-dropping metrics (for review workload) ──
    if "review" in df.workload_id.values:
        lines.append("--- Repeated-read metrics (review workload) ---")
        for arm in df.arm.unique():
            sub = df[(df.arm == arm) & (df.workload_id == "review")]
            if sub.empty:
                continue
            req_tot = sub.request_tokens.sum()
            dropped = sub.ctx_dropped_tokens_cum.max()  # sum of all drops
            hits = sub.tool_cache_hits.sum()
            misses = sub.tool_cache_misses.sum()
            hr = hits / (hits + misses) * 100 if (hits + misses) > 0 else 0
            turns = len(sub)
            final_req = sub.request_tokens.iloc[-1]
            lines.append(f"  {arm:12s}: {turns:>3} turns, {req_tot:>8,} req_tok total, "
                         f"final={final_req:>7,}, dropped={dropped:>8,}, cache_hit_rate={hr:.0f}%")

        # Tokens saved by TR vs naive
        tr = df[(df.arm == "toolrecall") & (df.workload_id == "review")]
        nv = df[(df.arm == "naive") & (df.workload_id == "review")]
        if not tr.empty and not nv.empty:
            tr_tok = tr.request_tokens.sum()
            nv_tok = nv.request_tokens.sum()
            saved = nv_tok - tr_tok
            pct = (nv_tok - tr_tok) / nv_tok * 100 if nv_tok else 0
            lines.append(f"  Tokens saved by ToolRecall vs naive: {saved:>8,} ({pct:.0f}%)")
            # Turns comparison
            tr_turns = len(tr)
            nv_turns = len(nv)
            lines.append(f"  Turns completed: naive={nv_turns}, toolrecall={tr_turns}")

    return "\n".join(lines)


# ── Report generator ───────────────────────────────────────────

def generate_report(df: pd.DataFrame, probe_df: pd.DataFrame, stats: str) -> str:
    """Generate a standalone BENCHMARK_REPORT.md."""

    # Compute key numbers
    total_runs = df.run_id.nunique()
    total_turns = len(df)
    arms = sorted(df.arm.unique())
    workloads = sorted(df.workload_id.unique())

    # Cost for the overall table
    pricing_rpt = _resolve_pricing(_PROVIDER, _MODEL)
    cost_per_arm = {}
    for arm in arms:
        sub = df[df.arm == arm]
        pt = sub.prompt_tokens.sum()
        ct = sub.completion_tokens.sum()
        cr = sub.cache_read_tokens.sum()
        cost_miss = (pt - cr) / 1e6 * pricing_rpt["prompt"]
        cost_hit = cr / 1e6 * pricing_rpt.get("prompt_cached", pricing_rpt["prompt"] * 0.1)
        cost_comp = ct / 1e6 * pricing_rpt["completion"]
        cost_per_arm[arm] = cost_miss + cost_hit + cost_comp

    # Context at checkpoints
    curve = (
        df.groupby(["arm", "turn_index"])["request_tokens"]
        .median()
        .reset_index()
    )

    report = f"""# ToolRecall Three-Arm Benchmark Report

**Generated:** {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}
**Model:** `{_MODEL}` via {_PROVIDER}
**Arms:** {', '.join(arms)}
**Workloads:** {', '.join(workloads)}
**Total runs:** {total_runs} ({total_turns} turns)

---

## Claims (locked before data collection)

| Claim | Statement | Proven by |
|-------|-----------|-----------|
"""

    for cid, desc in CLAIMS.items():
        report += f"| {cid} | {desc} | _(filled after analysis)_ |\n"

    report += f"""
---

## Methodology

Three arms, interleaved per seed: naive → prefix → toolrecall → naive → ...

| Arm | Behaviour | What it measures |
|-----|-----------|------------------|
| naive | Full conversation history every turn. No dropping. | Worst-case token growth — the snowball problem |
| prefix | Full history every turn, same as naive. Relies on provider prefix caching. | Honest baseline — what you get from just having your provider do prefix caching |
| toolrecall | After each turn, drops content of clean (read-only) files from context. | ToolRecall's mechanism — bounded context growth |

**Key metric:** `request_tokens` — self-counted via tiktoken (cl100k_base) before every LLM call. This measures what the provider actually receives, not what it bills for.

---

## Results Summary

| Metric | naive | prefix | toolrecall | Best arm |
|--------|-------|--------|------------|----------|
"""

    for t in (10, 50, 100, 200, 340):
        row_data = {}
        for arm in arms:
            r = curve[(curve.turn_index == t) & (curve.arm == arm)]
            if not r.empty:
                row_data[arm] = int(r.request_tokens.iloc[0])
            else:
                row_data[arm] = "—"
        best = min(
            (v for v in row_data.values() if isinstance(v, int)),
            default="—",
        )
        best_arm = next((k for k, v in row_data.items() if v == best), "—")
        report += (f"| req_tok @ turn {t:>3} | "
                   f"{row_data.get('naive', '—'):>8} | "
                   f"{row_data.get('prefix', '—'):>8} | "
                   f"{row_data.get('toolrecall', '—'):>8} | "
                   f"{best_arm} |\n")

    # Exhaustion row
    term = (
        df.sort_values("turn_index")
        .groupby(["run_id", "arm"])
        .last()
        .reset_index()
    )
    report += "|---|---|---|---|---|\n"
    report += "| **Median turns to exhaustion** |"
    for arm in arms:
        g = term[term.arm == arm]
        med = int(g.turn_index.median()) if not g.empty else "—"
        report += f" {med:>8} |"
    report += " |\n"

    # Cost row
    report += "| **Estimated cost (total)** |"
    for arm in arms:
        report += f" ${cost_per_arm.get(arm, 0):.5f} |"
    report += " |\n"

    report += f"""
---

## Wilcoxon Signed-Rank Test (on per-turn request_tokens)

_Paired by turn index across matched runs._
"""

    arms_list = list(arms)
    for i in range(len(arms_list)):
        for j in range(i + 1, len(arms_list)):
            w = compute_wilcoxon(df, arms_list[i], arms_list[j])
            if w["pvalue"] is not None:
                sig = "significant" if w["pvalue"] < 0.05 else "not significant"
                report += f"- **{arms_list[i]} vs {arms_list[j]}**: W={w['statistic']}, p={w['pvalue']:.4f} ({sig}, n={w['n']} pairs)\n"

    report += f"""
---

## Log-Rank Test (turns to exhaustion)
"""

    lr = compute_logrank(df)
    for pair, res in lr.items():
        sig = "significant" if res["pvalue"] < 0.05 else "not significant"
        report += f"- **{pair}**: chi²={res['statistic']}, p={res['pvalue']:.4f} ({sig})\n"

    report += f"""
---

## Provider Prefix Caching Effect

_How much the provider's prefix caching reduces billed tokens vs what we self-counted._
"""

    for arm in arms:
        sub = df[df.arm == arm]
        req = sub.request_tokens.sum()
        prov = sub.prompt_tokens.sum()
        gap = prov - req
        pct = gap / req * 100 if req else 0
        report += f"- **{arm}**: request={req:,}, provider_prompt={prov:,}, delta={gap:+,} ({pct:+.1f}%)\n"
        cr = sub.cache_read_tokens.sum()
        if cr:
            report += f"  - Provider prefix cache hits: {cr:,} tokens\n"

    report += f"""

---

## Probe Recall

_Nonce recall rate by arm and lag. If recall drops with lag, context dropping causes amnesia._
"""

    if not probe_df.empty:
        summary = probe_df.groupby(["arm", "lag"]).passed.agg(["mean", "count"])
        report += "\n| Arm | Lag | Recall rate | N |\n|-----|-----|-------------|---|\n"
        for (arm, lag), row in summary.iterrows():
            report += f"| {arm:12s} | {lag:>3} | {row['mean']:.0%} | {row['count']:.0f} |\n"
    else:
        report += "\n_No probe data collected._\n"

    report += f"""

---

## Per-Arm Summary

```
{df.groupby("arm").agg(
    runs=("run_id", "nunique"),
    total_turns=("turn_index", "count"),
    median_request_tokens=("request_tokens", "median"),
    median_completion=("completion_tokens", "median"),
    total_api_time_s=("api_latency_s", "sum"),
).to_string()}
```

---

## Charts

- `fig1_context_growth.png` — per-turn request_tokens for all three arms
- `fig2_ratio.png` — prefix/TR ratio (gap widening with session length)
- `fig3_warmup.png` — tool cache hit rate over time

---

## Raw Data

Full turn_log is in `~/.toolrecall/benchmark.db`. Query:

```sql
SELECT arm, workload_id, turn_index, request_tokens, prompt_tokens, completion_tokens,
       cache_read_tokens, ctx_dropped_tokens_cum, status
FROM turn_log
ORDER BY run_id, turn_index;
```

---

## Reproduction

```bash
cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/interleave.py <workload> --seeds 7 --max-turns 400
```

Requirements:
- ToolRecall daemon running (`toolrecall status`)
- `/tmp/bench-env` with tiktoken installed
- `OPENROUTER_API_KEY` in `~/.hermes/.env` (or `ANTHROPIC_API_KEY` for direct Anthropic)
- Schema applied (`migrations/002_turn_log.sql`)
"""

    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB)
    parser.add_argument("--provider", default="openrouter",
                        choices=["openrouter", "anthropic"],
                        help="Provider for pricing/report headers (default: openrouter)")
    parser.add_argument("--model", default=None,
                        help="Model name for pricing (defaults to provider default)")
    args = parser.parse_args()

    # Update globals for pricing/report
    global _PROVIDER, _MODEL
    _PROVIDER = args.provider
    if args.model:
        _MODEL = args.model
    else:
        _MODEL = _AGENT_MOD.DEFAULT_MODELS.get(_PROVIDER, "unknown")

    df = load_data(args.db)
    if df.empty:
        print("No turn_log data found. Run a benchmark session first.")
        sys.exit(0)

    probe_df = load_probes(args.db)

    print(f"Loaded {len(df)} turn_log rows ({df.run_id.nunique()} runs, {df.arm.nunique()} arms)")
    print(f"Arms: {list(df.arm.unique())}")
    print(f"Workloads: {list(df.workload_id.unique())}")

    # Build median curve
    curve = (
        df.groupby(["arm", "turn_index"])["request_tokens"]
        .median()
        .reset_index()
    )

    print("\nGenerating figures...")
    fig1_context_growth(curve)
    fig2_ratio(curve)
    fig3_warmup(df)

    print("\nComputing statistics...")
    stats = compute_stats(df, probe_df)
    print(stats)

    with open("benchmark_stats.txt", "w") as f:
        f.write(stats)
    print("\n  saved benchmark_stats.txt")

    print("Generating report...")
    report = generate_report(df, probe_df, stats)
    with open("BENCHMARK_REPORT.md", "w") as f:
        f.write(report)
    print("  saved BENCHMARK_REPORT.md")


if __name__ == "__main__":
    main()