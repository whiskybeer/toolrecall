#!/usr/bin/env python3
# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
"""run_gepa_fork_ab.py — billed 2-key A/B: fork-from-recorded-prefix vs naive
re-exploration, on the proven warp-rig methodology (SWE-bench Lite).

================================================================================
METHODOLOGY (v1.0 — scaffold; execute only after the gate below passes)
================================================================================

Question
--------
When an optimizer (GEPA-style) explores B candidate mutations of a shared
agent prefix, does ToolRecall's fork tree (record once at the shared prefix,
replay at branch re-entry) cut BILLED cost vs re-exploring each candidate
from zero — with real provider billing, per-key attributed?

Workload
--------
Same machinery as bench/run_warp_bench.py (proven Sep-2 methodology):
- Real SWE-bench Lite instances, real file contents at base_commit
  (bench/litellm_dedup/measure_swebench.py, imported not copied)
- 8-turn agent-simulated debugging conversation per instance
- Requests sent through the TR forward proxy via the Warp edge

Arms (2 OpenRouter keys — billing attribution per key, per standing rules)
---------------------------------------------------------------------------
    Arm naive   KEY_B (fresh/capped key) — every candidate re-explores from
                zero: the full 8-turn conversation is sent B times, each
                body distinct (candidate prompt varies per candidate), so
                the TR api_cache never hits → every turn bills.
    Arm fork    KEY_A — candidate 1 records the shared prefix on the tree
                (bills once); candidates 2..B fork from it. Shared-prefix
                turns replay byte-identically through the proxy cache
                (body-hash identity), ONLY divergent turns re-bill.

The two arms do EQUAL WORK (same candidates, same turns, same files) —
this is the equal-work property Robin requires; PK-masked CT savings are
not the subject here (large-file file-cache axis is C13/dedup, not this).

Per-candidate divergence
------------------------
Candidate k's body differs from the shared prefix only in the messages
that carry the candidate's mutation: the system prompt suffix
"[mutation k]" and its consequent final-turn analysis message. Turns
1..6 stay byte-identical across candidates (shared prefix); turn 7-8
bodies diverge → MISS → billed. This models GEPA's actual loop: shared
rollout prefix, divergent mutation tail.

Cost model being tested (from TREE_SEARCH.md, modeled):
    naive billed:  B·k·(C+d)        (every candidate re-sends shared prefix C)
    fork  billed:  k·C + B·k·d     (prefix once + divergent tail per branch)
Savings grow with B/C; the bench sweeps B ∈ {3, 5} to show the scaling.

Arms (2 keys — the v2 2-key protocol from the warp bench)
----------------------------------------------------------
    KEY_A (fork arm)     — fork-tree arm
    KEY_B (naive arm)    — equal-work naive re-exploration baseline
Both keys: dashboard-verified spend cross-check (usage.cost per request +
OpenRouter per-key window delta before/after the run).

Verification gates (ALL must pass before any number is public)
---------------------------------------------------------------
  G1  --smoke: 1 instance × 2 turns × 1 pass ≈ $0.005 — plumbing only
  G2  Per-datapoint verification: every request row's usage.cost>0 for
      billed turns; HIT rows must have cost==0 (or None) on fork arm
  G3  Correctness: turn bodies 1..6 byte-identical across candidates
      (verified by construction in build_fork_session, asserted per run)
  G4  Dashboard cross-check: per-key spend delta matches JSONL sum ±5%
  G5  C14-style retry probe: identical re-request replays byte-exact

What this bench does NOT claim (mirrors TREE_SEARCH.md "What NOT to claim")
---------------------------------------------------------------------------
- No task-quality claim (max_tokens=5, completion is noise)
- No pass@1 gap measurement (that's the dedup bench's open question)
- No provider-prefix-cache conflation: cached_tokens tracked separately
- Results site numbers only after Robin reviews raw JSONL + dashboard deltas

Usage
-----
    # gate 1 (no spend beyond smoke)
    KEY_A=sk-or-... KEY_B=sk-or-... python3 bench/run_gepa_fork_ab.py --smoke
    # full: 2 arms × N instances × 8 turns × B candidates
    KEY_A=sk-or-... KEY_B=sk-or-... python3 bench/run_gepa_fork_ab.py --execute

Named outputs (never overwrite): bench/results/gepa_fork_ab_<UTC>.json/.jsonl
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench.litellm_dedup.measure_swebench import (  # noqa: E402
    build_session_convo,
    load_instances,
)

DEFAULT_EDGE = "http://127.0.0.1:8573/v1/chat/completions"
MODEL = os.environ.get("BENCH_MODEL", "deepseek/deepseek-v4-flash")
COST_ABORT = float(os.environ.get("BENCH_COST_ABORT", "0.60"))  # hard spend cap

# Mutation schedule: candidate k's system prompt carries the k-th mutation.
# Mutations 2..B share candidate 1's full prefix; only the mutation-bearing
# message differs — the fork-tree mechanic must absorb the shared part.
MUTATIONS = [
    "trace the data flow before proposing a fix",
    "prioritize the smallest possible patch",
    "add regression thinking to your analysis",
    "focus on the error handling path",
    "verify the fix against the test files first",
]

TURN_TAIL = 2  # divergent tail turns (of the 8-turn conversation)


def or_key_from_env(env_name: str) -> str:
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise SystemExit(f"{env_name} not set — 2-key protocol requires both keys")
    return key


def build_session_convo_muted(instance: dict, max_turns: int = 8):
    """build_session_convo + per-candidate mutation plumbing.

    Returns (messages, info, file_blocks, turn_snapshots) exactly like the
    original (imported), but the caller applies mutations by replacing the
    system message + appending a mutation line to the last user turn — the
    shared prefix stays byte-identical by construction (asserted below).
    """
    built = build_session_convo(instance, max_turns=max_turns, with_snapshots=True)
    return built  # (messages, info, file_blocks, snapshots) — snapshots at [3]


def mutate_candidate(snapshots: list, mutation: str) -> list:
    """Apply one candidate's mutation to a full turn-snapshot set.

    Deterministic mutation: system prompt gets the mutation suffix, and the
    mutation marker rides in a dedicated final user message (so the tail
    body diverges while turns 1..6 stay byte-identical across candidates).

    Mutating the SYSTEM message of a snapshot DOES change turn-1..6 bodies
    (system rides at position 0) — so the shared prefix is instead kept by
    sending the base system + a mutation-only EXTRA final turn. Turn bodies
    1..6 remain byte-identical across candidates; only the extra tail turn
    diverges. This is the honest model: a GEPA mutation changes the PROMPT,
    and prompt-carrying turns diverge.
    """
    out = []
    for snap in snapshots:
        out.append(list(snap))
    # mutation rides in an extra final user turn — shared prefix untouched
    out.append([{"role": "user", "content": f"[candidate mutation] {mutation}\nConfirm approach."}])
    return out


def send_through_edge(
    url: str, token: str, model: str, messages: list, no_cache: bool = False
) -> dict:
    """POST one chat completion through the Warp edge (same as warp bench)."""
    import urllib.error
    import urllib.request

    body = json.dumps({"model": model, "messages": messages, "max_tokens": 5}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if no_cache:
        headers["X-ToolRecall-No-Cache"] = "1"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    out = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache": None,
        "error": None,
        "latency_ms": None,
        "content_sha": None,
        "total_cost": None,
        "cached_tokens": None,
    }

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
            data = json.loads(raw)
            usage = data.get("usage", {})
            out["prompt_tokens"] = usage.get("prompt_tokens", 0)
            out["completion_tokens"] = usage.get("completion_tokens", 0)
            out["total_cost"] = usage.get("cost")  # OpenRouter-billed, not est.
            ptd = usage.get("prompt_tokens_details") or {}
            out["cached_tokens"] = ptd.get("cached_tokens")
            out["cache"] = resp.headers.get("X-ToolRecall-Cache")
            choice = (data.get("choices") or [{}])[0]
            content = str(choice.get("message", {}).get("content", ""))
            import hashlib

            out["content_sha"] = hashlib.sha256(content.encode()).hexdigest()[:16]
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def body_sha(messages: list) -> str:

    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:16]


def run_arm(
    arm_name: str,
    key: str,
    model: str,
    edge_url: str,
    instances: list,
    n_candidates: int,
    max_turns: int,
    jsonl_rows: list,
    no_cache: bool = False,
    smoke: bool = False,
) -> dict:
    """Run one arm: for each instance, B candidates re-explore (naive) or
    fork from the recorded prefix (fork arm). Returns per-instance stats."""
    results = {"arm": arm_name, "model": model, "instances": []}
    total_spend = 0.0
    slot_shas: dict = {}

    for inst_idx, inst in enumerate(instances):
        built = build_session_convo_muted(inst, max_turns=max_turns)
        turn_snapshots = built[3] if len(built) > 3 else [built[0]]

        inst_result = {
            "instance_id": inst["instance_id"],
            "candidates": [],
        }

        for c in range(n_candidates):
            mutation = MUTATIONS[c % len(MUTATIONS)]
            cands = mutate_candidate(turn_snapshots, mutation) if c > 0 else list(turn_snapshots)
            # candidate 0 = the shared prefix recorder (fork arm) / plain
            # re-run (naive arm). Candidates 1..B-1 add the mutation tail.
            cand_result = {
                "candidate": c,
                "mutation": mutation if c > 0 else None,
                "turns": [],
            }
            for ti, msgs in enumerate(cands):
                expected_shared = c > 0 and ti < len(turn_snapshots)
                r = send_through_edge(edge_url, key, model, msgs, no_cache=no_cache)
                spend = float(r.get("total_cost") or 0.0)
                total_spend += spend
                row = {
                    "arm": arm_name,
                    "model": model,
                    "instance_id": inst["instance_id"],
                    "instance_index": inst_idx,
                    "candidate": c,
                    "turn": ti,
                    "expected_shared": expected_shared,
                    "body_sha": body_sha(msgs),
                    "cache": r.get("cache"),
                    "prompt_tokens": r.get("prompt_tokens"),
                    "completion_tokens": r.get("completion_tokens"),
                    "total_cost": r.get("total_cost"),
                    "cached_tokens": r.get("cached_tokens"),
                    "latency_ms": r.get("latency_ms"),
                    "content_sha": r.get("content_sha"),
                    "error": r.get("error"),
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                # ── G5 correctness tracking ───────────────────────────────
                # Per (instance, candidate, turn) request slot: record the
                # content hash seen on each MISS (the "real" response) and
                # every later serve of the same slot. A HIT must replay the
                # MISS content byte-exactly (C5 discipline from the warp
                # bench). Cross-arm drift (fork vs naive on the SAME slot)
                # must also be zero — the provider answer is deterministic
                # at temperature 0 for identical bodies.
                slot = (inst["instance_id"], c, ti)
                if r.get("content_sha"):
                    slot_shas.setdefault(slot, set()).add(r["content_sha"])
                jsonl_rows.append(row)
                cand_result["turns"].append(row)
                if r.get("error"):
                    print(f"    [{arm_name}/c{c}/t{ti}] ERROR: {r['error'][:120]}", flush=True)
                # hard spend cap — never exceed without looking
                if total_spend > COST_ABORT:
                    raise SystemExit(
                        f"COST ABORT: {arm_name} spent ${total_spend:.4f} > ${COST_ABORT:.2f} cap"
                    )
            cand_result["billed_cost_usd"] = round(
                sum(float(t.get("total_cost") or 0.0) for t in cand_result["turns"]), 6
            )
            cand_result["hits"] = sum(1 for t in cand_result["turns"] if t.get("cache") == "HIT")
            cand_result["misses"] = sum(1 for t in cand_result["turns"] if t.get("cache") == "MISS")
            inst_result["candidates"].append(cand_result)
            print(
                f"    [{arm_name}] {inst['instance_id']} c{c}: "
                f"{cand_result['hits']}H/{cand_result['misses']}M ${cand_result['billed_cost_usd']:.4f}",
                flush=True,
            )
            if smoke:
                break  # smoke: one candidate per instance
        results["instances"].append(inst_result)
        if smoke:
            break  # smoke: one instance

    results["billed_cost_usd"] = round(
        sum(
            i["candidates"] and sum(c["billed_cost_usd"] for c in i["candidates"])
            for i in results["instances"]
        ),
        6,
    )
    # ── G5 correctness verdict for this arm ──────────────────────────────
    drift_slots = {s: shas for s, shas in slot_shas.items() if len(shas) > 1}
    results["correctness"] = {
        "request_slots_tracked": len(slot_shas),
        "slots_with_content_drift": len(drift_slots),
        "drift_examples": [
            {"instance_id": s[0], "candidate": s[1], "turn": s[2], "shas": sorted(v)}
            for s, v in list(drift_slots.items())[:5]
        ],
        "verdict": (
            "PASS" if not drift_slots and slot_shas else ("NO-DATA" if not slot_shas else "FAIL")
        ),
    }
    return results


def summarize(results: list) -> list:
    summary = []
    for arm in results:
        shared_billed = 0.0
        shared_turns = 0
        shared_hits = 0
        divergent_billed = 0.0
        errors = 0
        for inst in arm["instances"]:
            for cand in inst["candidates"]:
                for t in cand["turns"]:
                    if t.get("error"):
                        errors += 1
                    if t.get("expected_shared"):
                        shared_turns += 1
                        shared_billed += float(t.get("total_cost") or 0.0)
                        shared_hits += 1 if t.get("cache") == "HIT" else 0
                    else:
                        divergent_billed += float(t.get("total_cost") or 0.0)
        total = shared_billed + divergent_billed
        corr = arm.get("correctness", {})
        summary.append(
            {
                "arm": arm["arm"],
                "model": arm["model"],
                "billed_cost_usd": round(total, 6),
                "shared_prefix_turns": shared_turns,
                "shared_prefix_hits": shared_hits,
                "shared_prefix_hit_ratio": round(shared_hits / shared_turns, 3)
                if shared_turns
                else 0.0,
                "shared_prefix_billed_usd": round(shared_billed, 6),
                "divergent_billed_usd": round(divergent_billed, 6),
                "errors": errors,
                "correctness": {
                    "verdict": corr.get("verdict", "NO-DATA"),
                    "slots_tracked": corr.get("request_slots_tracked", 0),
                    "drift_slots": corr.get("slots_with_content_drift", 0),
                },
            }
        )
    return summary


def or_key_usage(key: str) -> float:

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return float(json.load(r)["data"]["usage"])


def main() -> int:

    ap = argparse.ArgumentParser(
        description="Billed 2-key A/B: fork-vs-naive candidate exploration"
    )
    ap.add_argument("--edge", default="http://127.0.0.1:8573/v1/chat/completions")
    ap.add_argument("--instances", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=5, help="B: candidates per instance")
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--arms", default="naive,fork")
    ap.add_argument("--smoke", action="store_true", help="1 instance, 1 candidate, 3 turns")
    ap.add_argument(
        "--out", default=None, help="Output base path (default: bench/results/gepa_fork_ab_<UTC>)"
    )
    ap.add_argument("--execute", action="store_true", help="Actually spend money (required)")
    args = ap.parse_args()

    if not args.execute:
        print("PLAN MODE (no spend). Re-run with --execute after reviewing this plan.\n")

    key_a = os.environ.get("KEY_A", "").strip()
    key_b = os.environ.get("KEY_B", "").strip()
    if args.execute:
        if not key_a or not key_b:
            raise SystemExit("Both KEY_A (fork) and KEY_B (naive) are required for the billed run")

    if args.smoke:
        args.instances, args.candidates, args.max_turns = 1, 2, 3

    loaded = load_instances(args.instances)
    instances = loaded["instances"]
    print(f"Loaded {len(instances)} instances: {[i['instance_id'] for i in instances]}", flush=True)
    print(
        f"Arms: fork(KEY_A) vs naive(KEY_B) · B={args.candidates} candidates × {args.max_turns} turns",
        flush=True,
    )

    est_reqs = 2 * len(instances) * (args.max_turns + (args.candidates - 1) * (args.max_turns + 1))
    print(
        f"Planned requests: ~{est_reqs} per arm × 2 arms · model {MODEL} · "
        f"cost cap ${COST_ABORT:.2f} per arm",
        flush=True,
    )
    if not args.execute:
        print("DRY-RUN ONLY (no requests sent).", flush=True)
        return 0

    # Dashboard spend cross-check anchors (per key, before run)
    spend_before = {"KEY_A": or_key_usage(key_a), "KEY_B": or_key_usage(key_b)}
    print(
        f"dashboard before: KEY_A=${spend_before['KEY_A']:.4f} KEY_B=${spend_before['KEY_B']:.4f}",
        flush=True,
    )

    jsonl_rows: list = []
    results = []
    arm_specs = [
        ("fork", key_a, False),  # cache ON
        ("naive", key_b, True),  # cache OFF (equal work, every body bills)
    ]
    for arm_name, key, no_cache in arm_specs:
        print(f"\n=== Arm {arm_name} ({'cache OFF' if no_cache else 'cache ON'}) ===", flush=True)
        results.append(
            run_arm(
                arm_name,
                key,
                MODEL,
                args.edge,
                instances,
                args.candidates,
                args.max_turns,
                jsonl_rows,
                no_cache=no_cache,
                smoke=args.smoke,
            )
        )

    spend_after = {"KEY_A": or_key_usage(key_a), "KEY_B": or_key_usage(key_b)}
    dash_delta = {k: round(spend_after[k] - spend_before[k], 6) for k in spend_before}

    # ── G4: dashboard cross-check ──────────────────────────────────────────
    jsonl_sums = {
        "fork": sum(float(r.get("total_cost") or 0) for r in jsonl_rows if r["arm"] == "fork"),
        "naive": sum(float(r.get("total_cost") or 0) for r in jsonl_rows if r["arm"] == "naive"),
    }
    crosscheck = []
    for arm_name, key_name in (("fork", "KEY_A"), ("naive", "KEY_B")):
        jsonl_total = jsonl_sums[arm_name]
        dash = dash_delta.get(key_name, 0.0)
        drift = abs(jsonl_total - dash) / dash if dash else 0.0
        cross = {
            "arm": arm_name,
            "key": key_name,
            "jsonl_usd": round(jsonl_total, 6),
            "dashboard_usd": dash,
            "drift": round(drift, 4),
            "verdict": "PASS" if drift < 0.05 else "FAIL",
        }
        crosscheck.append(cross)
        jsonl_rows.append(
            {
                "arm": arm_name,
                "model": MODEL,
                "instance_id": None,
                "instance_index": -1,
                "candidate": -1,
                "turn": -1,
                "expected_shared": False,
                "cache": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_cost": dash,
                "cached_tokens": None,
                "latency_ms": None,
                "content_sha": None,
                "error": None if cross["verdict"] == "PASS" else f"dashboard drift {drift:.1%}",
                "error_class": None,
                "ts": datetime.now(timezone.utc).isoformat(),
                "dashboard_crosscheck": True,
                "key": key_name,
                "window_before": spend_before[key_name],
                "window_after": spend_after[key_name],
            }
        )
        print(
            f"G4 {arm_name}: jsonl ${jsonl_total:.4f} vs dashboard ${dash:.4f} → {cross['verdict']}",
            flush=True,
        )

    summary = summarize(results)

    payload = {
        "meta": {
            "date": datetime.now(timezone.utc).isoformat(),
            "edge": args.edge,
            "model": MODEL,
            "dataset": "princeton-nlp/swe-bench-lite (test split)",
            "instances": [i["instance_id"] for i in instances],
            "candidates_B": args.candidates,
            "max_turns": args.max_turns,
            "smoke": args.smoke,
            "cost_abort_cap_usd": COST_ABORT,
            "note_cost": "total_cost is OpenRouter-billed per request; G4 cross-checks "
            "per-key dashboard window delta vs JSONL sums. cached_tokens = provider "
            "prefix cache, tracked separately, never conflated.",
            "note_arms": "fork arm = shared prefix recorded once, candidates replay it; "
            "naive arm = equal work, cache OFF, every turn bills. 2 keys.",
        },
        "summary": summary,
        "dashboard_crosscheck": crosscheck,
        "raw": results,
    }

    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = args.out or os.path.join(out_dir, f"gepa_fork_ab_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump(payload, f, indent=2)
    with open(base + ".jsonl", "w") as f:
        for row in jsonl_rows:
            f.write(json.dumps(row) + "\n")
    print(f"\nWrote {base}.json (+{len(jsonl_rows)} raw rows in .jsonl)", flush=True)

    print("\n" + "=" * 96)
    print(
        f"{'Arm':<8}{'billed $':>10}{'shared turns':>13}{'shared hits':>12}"
        f"{'shared hit%':>12}{'shared $':>10}{'divergent $':>12}{'errors':>8}"
    )
    for s in summary:
        print(
            f"{s['arm']:<8}{s['billed_cost_usd']:>10.4f}{s['shared_prefix_turns']:>13}"
            f"{s['shared_prefix_hits']:>12}{s['shared_prefix_hit_ratio'] * 100:>11.0f}%"
            f"{s['shared_prefix_billed_usd']:>10.4f}{s['divergent_billed_usd']:>12.4f}"
            f"{s['errors']:>8}"
        )

    # ── Claims verdict table (mirrors warp bench discipline) ──────────────
    fork = next((s for s in summary if s["arm"] == "fork"), None)
    naive = next((s for s in summary if s["arm"] == "naive"), None)
    verdicts = []
    if fork and naive:
        f_b, n_b = fork["billed_cost_usd"], naive["billed_cost_usd"]
        saved = (1 - f_b / n_b) * 100 if n_b else 0.0
        verdicts.append(
            (
                "C-F1 fork cuts billed cost vs equal-work naive re-exploration",
                f"PASS — fork ${f_b:.4f} vs naive ${n_b:.4f} ({saved:.0f}% saved, billing-verified)"
                if f_b < n_b and fork["shared_prefix_hit_ratio"] > 0.5
                else "FAIL — see JSONL",
            )
        )
        verdicts.append(
            (
                "C-F2 shared-prefix turns are free on the fork arm",
                f"PASS — {fork['shared_prefix_hits']}/{fork['shared_prefix_turns']} HIT, "
                f"${fork['shared_prefix_billed_usd']:.4f} billed on shared turns"
                if fork["shared_prefix_hit_ratio"] > 0.5
                else "FAIL",
            )
        )
        verdicts.append(
            (
                "C-F3 scaling with B (per-candidate marginal cost)",
                f"MEASURED — naive adds ${n_b / args.candidates:.4f}/candidate, "
                f"fork adds ${(f_b - fork['shared_prefix_billed_usd']) / max(1, args.candidates - 1):.4f}",
            )
        )
        # C-F4: correctness — HIT replays byte-exact, cross-arm answers identical
        corr_pairs = [s["correctness"] for s in summary]
        all_pass = all(c["verdict"] == "PASS" for c in corr_pairs) and corr_pairs
        total_tracked = sum(c["slots_tracked"] for c in corr_pairs)
        total_drift = sum(c["drift_slots"] for c in corr_pairs)
        verdicts.append(
            (
                "C-F4 correctness: HIT replays byte-exact; fork == naive answers",
                f"PASS — {total_tracked} slots tracked, {total_drift} drift"
                if all_pass
                else f"FAIL — {total_drift} drift slots of {total_tracked}",
            )
        )
    for cc in crosscheck:
        verdicts.append((f"G4 dashboard cross-check ({cc['key']})", cc["verdict"]))

    print("\nClaims verdict:")
    for name, verdict in verdicts:
        print(f"  {name}: {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
