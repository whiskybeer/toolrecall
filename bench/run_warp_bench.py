#!/usr/bin/env python3
# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
"""run_warp_bench.py — ToolRecall response-cache benefit on real SWE-bench
workloads, through the Warp edge, billing-verified per-key.

================================================================================
METHODOLOGY (v1.0)
================================================================================

Question
--------
Does the ToolRecall response cache (forward proxy + api_cache) cut real billed
cost when an agent harness re-runs the same work — the Warp agent-fleet /
Factories-replay scenario?

Workload
--------
Identical to bench/litellm_dedup/measure_swebench.py (proven methodology):
- Real SWE-bench Lite instances (astropy-12907, django-10914, matplotlib-18869)
- Real file contents fetched from GitHub at base_commit (disk-cached)
- 8-turn agent-simulated debugging conversation per instance
- Requests sent through the TR forward proxy via the Warp edge

Arms (4 OpenRouter keys — one per arm, dashboard-verified per key)
------------------------------------------------------------------
    Arm A  key_A  via TR proxy, cache ON   (TOOLRECALL_API_TTL=3600)
    Arm B  key_B  via TR proxy, cache OFF  (TTL=0 → store skipped → no reuse)
    Arm C  key_C  via TR proxy, cache ON   (same as A — the "fleet re-run" arm:
           pass 1 bills, passes 2-3 must be near-$0)
    Arm D  key_D  via TR proxy, cache OFF  (baseline for C)

Wait — A and C identical? No: A runs instances 1-3; C re-runs the SAME
instances AFTER A warmed the cache... but each arm uses its own key. The
cache key is (method, host, path, body-hash) — body includes "model", so
arm C uses a DIFFERENT MODEL ID on the same provider (deepseek-v4-flash vs
deepseek-v4-flash via a different model alias), giving the cross-model arm.
If a second model alias is unavailable, C falls back to same-model and the
cross-model claim is measured as cache sharing across keys (see results
meta). NOTE: provider-side billing follows the key, TR-side caching follows
the body — two different models never share a TR cache entry.

Passes
------
Each arm runs each instance 3 times (pass 1 / 2 / 3):
    pass 1 = cold (billed, populates cache where ON)
    pass 2 = warm (HIT where cache ON → $0)
    pass 3 = warm again (stability check)

Metrics
-------
    per arm: billed_tokens_p1/p2/p3, hit_ratio_p2/p3, est_cost per pass
    headline: cache ON vs OFF total billed cost (p1+p2+p3), and
              hit_ratio = 1 - billed_p2/p1 (warm-pass cost retention)

Verification
------------
- Per-key OpenRouter dashboard spend cross-checks usage.prompt_tokens sums
- X-ToolRecall-Cache response header recorded per request (MISS/HIT)

Honest limits
-------------
- max_tokens=5: measures INPUT token economics + cache hit behavior, not
  task success (same caveat as the litellm dedup benchmark)
- Simulation replays a fixed conversation; a real agent's pass 2 would
  diverge (different tool results → different bodies → fewer hits). The
  fleet-replay scenario (identical re-runs) is the honest target here.
- 3 instances × 8 turns × 3 passes × 4 arms = 288 requests, ~$0.05-0.15
  total at DeepSeek V4 Flash pricing ($0.15/M input)

Claims under test (each maps to tests in this benchmark — "include all
the points as tests" so every pitch claim has a measured row)
------------------------------------------------------------------
  C1  Same-task re-run saves cost           → arm A vs B, passes 2-3 HIT
  C2  Savings scale with fleet size         → endurance mode, s0→sN curve
  C3  Savings survive across sessions       → endurance with session gap
  C4  TTL expiry works as documented        → endurance with TTL=300, gap>300
  C5  Correctness: HIT replays byte-exact   → content_sha drift check
  C6  Correctness under concurrency         → --concurrent racing arms
  C7  Works across models (routing)         → arms C/D (glm-5.3-flash)
  C8  Latency: HIT faster than MISS         → latency_ms_mean p1 vs p2
  C9  Cost is billing-verified, not est.    → total_cost per request + dashboard
  C10 Provider prefix cache tracked apart   → cached_tokens field per request
  C11 Energy: tokens not recomputed         → energy block (labeled estimate)
  C12 Solo-user traffic ≈ 0 repeatable      → tr-warp-stats (done: 0.38%/0.22%)
  C13 README/file-cache layer claim         → shared-context mode (PENDING,
                                            see handoff item 2)

Usage
-----
    export KEY_A=sk-or-v1-... KEY_B=sk-or-v1-...
    bash bench/setup_warp_bench.sh          # rig: proxy :8572 + edge :8573
    python3 bench/run_warp_bench.py --smoke # gate first (~$0.001)
    python3 bench/run_warp_bench.py         # full: writes results/*.json+jsonl
    python3 bench/run_warp_bench.py --arms A,C --endurance 4 --session-gap 150
    python3 bench/run_warp_bench.py --concurrent
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse the proven workload machinery from the litellm dedup benchmark.
from bench.litellm_dedup.measure_swebench import (  # noqa: E402
    build_session_convo,
    load_instances,
)

DEFAULT_EDGE = "http://127.0.0.1:8573/v1/chat/completions"  # bench edge (setup_warp_bench.sh)
N_PASSES = 3

# ── Shared-context preamble (v2, claim C13) ─────────────────────────────────
# Identical doc-read messages prepended to EVERY session of EVERY arm.
# This models the fleet hypothesis: same repo, different issues, shared doc
# preamble (README/CONTRIBUTING re-read by every agent). The TR FILE cache is
# the layer that should absorb this — same-repo-diff-issues api_cache measured
# 0.0% reuse, so this mode is the last plausible path to fleet-scale savings.
# Preamble content is fetched from GitHub at each instance's base_commit
# (disk-cached by fetch_file), so it is byte-identical across arms.
SHARED_CONTEXT_FILES = ["README.md", "CONTRIBUTING.md", "docs/developers/index.rst"]

# Arm definitions: (name, env key name, model id, cache_on)
# v2: 2 keys, 4 arms. A/C share KEY_A (cache-ON on two models), B/D share
# KEY_B (cache-OFF baseline on the same two models). TR cache keys on
# (method, host, path, body-hash) and body includes "model", so the two
# cache-ON arms never share TR entries across models.
ARMS = [
    ("A_cache_on", "KEY_A", "deepseek/deepseek-v4-flash", True),
    ("B_cache_off", "KEY_B", "deepseek/deepseek-v4-flash", False),
    ("C_cache_on_glm", "KEY_A", "z-ai/glm-5.3-flash", True),
    ("D_cache_off_glm", "KEY_B", "z-ai/glm-5.3-flash", False),
]


def build_shared_preamble(instance: dict) -> list:
    """Build the identical doc-read preamble messages for shared-context mode.

    Fetches SHARED_CONTEXT_FILES from the instance's repo at base_commit
    (disk-cached by fetch_file) and returns them as system+user messages that
    are byte-identical across every arm/session — exactly what a fleet of
    agents working the same repo would re-send every session.
    """
    from bench.litellm_dedup.measure_swebench import fetch_file

    repo, commit = instance["repo"], instance["base_commit"]
    msgs = [
        {
            "role": "system",
            "content": "You are a senior software engineer debugging an issue "
            "in an open-source project. Project documentation follows.",
        }
    ]
    total_chars = 0
    for fpath in SHARED_CONTEXT_FILES:
        content = fetch_file(repo, commit, fpath)
        if content is None:
            continue
        total_chars += len(content)
        msgs.append({"role": "user", "content": f"Project docs — {fpath}:\n\n{content}"})
        msgs.append({"role": "assistant", "content": f"I've reviewed {fpath}."})
    print(
        f"    shared-context preamble: {len(msgs) - 1} messages, "
        f"{total_chars / 1024:.1f} KiB docs from {repo}",
        flush=True,
    )
    return msgs


def classify_error(error: str | None) -> str | None:
    """Error taxonomy: what failed matters more than how often."""
    if not error:
        return None
    e = error.lower()
    if "timeout" in e or "timed out" in e:
        return "timeout"
    if "connection refused" in e or "errno 111" in e:
        return "conn_refused"
    if "http 4" in e:
        return "http_4xx"  # auth/validation — our side or key issue
    if "http 5" in e:
        return "http_5xx"  # provider/upstream — their side
    return "other"


def _median(xs: list) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def send_through_edge(
    url: str, token: str, model: str, messages: list, no_cache: bool = False
) -> dict:
    """POST one chat completion through the Warp edge.

    Returns usage + cache header + latency_ms + response_content_hash.
    The content hash lets the analysis detect correctness drift: a HIT body
    must be byte-identical to the MISS body for the same request (or to a
    previously seen identical-hash body). Latency: HIT expected ~ms, MISS ~s.

    no_cache=True sends X-ToolRecall-No-Cache — the baseline (cache-OFF)
    arms bill every pass instead of riding the shared TR cache namespace.
    """
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
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as je:
                # Corruption forensics: dump the malformed body so the failure
                # is diagnosable instead of silent.
                import os
                dbg = f"/tmp/hit_corrupt_{int(time.time())}_{os.getpid()}.txt"
                with open(dbg, "w") as df:
                    df.write(raw)
                print(f"    [corrupt body] saved {dbg} ({len(raw)} bytes): {je}", flush=True)
                raise
            usage = data.get("usage", {})
            out["prompt_tokens"] = usage.get("prompt_tokens", 0)
            out["completion_tokens"] = usage.get("completion_tokens", 0)
            # billing-verified cost from OpenRouter (not an estimate).
            # Field is `cost` (not total_cost) on current OpenRouter usage.
            out["total_cost"] = usage.get("cost")
            # provider prefix-cache tokens — tracked SEPARATELY so TR savings
            # are never conflated with provider-side prefix caching
            ptd = usage.get("prompt_tokens_details") or {}
            out["cached_tokens"] = ptd.get("cached_tokens")
            out["cache"] = resp.headers.get("X-ToolRecall-Cache")
            # correctness tracking: hash the assistant content (choices[0])
            choice = (data.get("choices") or [{}])[0]
            content = str(choice.get("message", {}).get("content", ""))
            out["content_sha"] = hashlib.sha256(content.encode()).hexdigest()[:16]
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def run_arm(
    arm_name: str,
    key: str,
    model: str,
    edge_url: str,
    token: str,
    instances: list,
    max_turns: int,
    jsonl_rows: list,
    shared_preamble: list | None = None,
    no_cache: bool = False,
) -> dict:
    """Run one arm: instances × passes. Returns per-instance/pass usage.

    Request shape mirrors a real accumulating agent loop: turn N sends the
    full conversation so far (build_session_convo's turn_snapshots) — so
    turn k+1's body differs from turn k's, but a RE-RUN of the same turn
    produces a byte-identical body → TR cache HIT.

    shared_preamble: identical doc-read messages prepended to every request
    of every session (shared-context mode / claim C13). Byte-identical
    across arms so the TR file-cache layer is what absorbs the re-sends.
    """
    results = {"arm": arm_name, "model": model, "instances": []}

    for inst in instances:
        inst_id = inst["instance_id"]
        # accumulating snapshots: turn t = full message list the agent sends.
        # with_snapshots=True returns (messages, info, file_blocks, snapshots).
        built = build_session_convo(inst, max_turns=max_turns, with_snapshots=True)
        turn_snapshots = built[3] if len(built) > 3 else [built[0]]
        if shared_preamble:
            # Prepend the byte-identical doc preamble to each turn snapshot.
            # The system message from build_session_convo stays inside the
            # snapshot; the preamble carries its own system message first.
            turn_snapshots = [list(shared_preamble) + list(s) for s in turn_snapshots]

        inst_result = {"instance_id": inst_id, "passes": []}

        for p in range(1, N_PASSES + 1):
            pass_tokens = 0
            hits = 0
            misses = 0
            errors = 0
            latencies = []
            content_shas = {}  # request index -> content hash
            completion_tokens = {}  # request index -> completion token count
            pass_cost = 0.0
            pass_cost_avoided = 0.0
            pass_cached_tokens = 0
            for ti, msgs in enumerate(turn_snapshots):
                r = send_through_edge(edge_url, key, model, msgs, no_cache=no_cache)
                # JSONL raw row: one line per request — survives crashes,
                # feeds the results site, enables per-request analysis
                jsonl_rows.append(
                    {
                        "arm": arm_name,
                        "model": model,
                        "instance_id": inst_id,
                        "pass": p,
                        "turn": ti,
                        "cache": r["cache"],
                        "prompt_tokens": r["prompt_tokens"],
                        "completion_tokens": r.get("completion_tokens"),
                        "total_cost": r["total_cost"],
                        "billed_cost": 0.0 if r["cache"] == "HIT" else (r["total_cost"] or 0.0),
                        "cost_avoided": (r["total_cost"] or 0.0) if r["cache"] == "HIT" else 0.0,
                        "cached_tokens": r["cached_tokens"],
                        "latency_ms": r["latency_ms"],
                        "content_sha": r["content_sha"],
                        "error": r["error"],
                        "error_class": classify_error(r["error"]),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )
                if r["error"]:
                    errors += 1
                    print(f"    [{arm_name}/{inst_id} p{p}] ERROR: {r['error']}", flush=True)
                else:
                    # Billing honesty: a TR cache HIT never reaches the provider.
                    # The proxy replays the stored body INCLUDING its original
                    # usage block, so r["total_cost"] on a HIT is the cost the
                    # request WOULD have incurred, not what was billed. Record
                    # billed cost only on cache-ineligible/MISS responses; keep
                    # the replayed figure as cost_avoided_usd on the row.
                    is_tr_hit = r["cache"] == "HIT"
                    row_cost = 0.0 if is_tr_hit else (r["total_cost"] or 0.0)
                    pass_cost += row_cost
                    if is_tr_hit and r["total_cost"]:
                        pass_cost_avoided += r["total_cost"]
                    pass_tokens += r["prompt_tokens"]
                    latencies.append(r["latency_ms"])
                    if r["cached_tokens"]:
                        pass_cached_tokens += r["cached_tokens"]
                    if r["cache"] == "HIT":
                        hits += 1
                    elif r["cache"] == "MISS":
                        misses += 1
                    if r["content_sha"]:
                        content_shas[ti] = r["content_sha"]
                    if r.get("completion_tokens") is not None:
                        completion_tokens[ti] = r["completion_tokens"]
            inst_result["passes"].append(
                {
                    "pass": p,
                    "billed_prompt_tokens": pass_tokens,
                    "billed_cost_usd": round(pass_cost, 6),
                    "cost_avoided_usd": round(pass_cost_avoided, 6),
                    "provider_cached_tokens": pass_cached_tokens,
                    "hits": hits,
                    "misses": misses,
                    "errors": errors,
                    "latency_ms_median": _median(latencies),
                    "latency_ms_mean": round(sum(latencies) / len(latencies))
                    if latencies
                    else None,
                    "content_shas": content_shas,
                    "completion_tokens": completion_tokens,
                }
            )
            print(
                f"  [{arm_name}] {inst_id} pass {p}: {pass_tokens} tok, "
                f"{hits}H/{misses}M/{errors}E",
                flush=True,
            )
        results["instances"].append(inst_result)
    return results


def summarize(results: list, price_per_m: float) -> dict:
    """Aggregate: per-arm totals, hit ratios, cost, latency, correctness."""
    summary = []
    for arm in results:
        p_tokens = [0, 0, 0]
        p_hits = [0, 0, 0]
        p_reqs = [0, 0, 0]
        p_lat = [0.0, 0.0, 0.0]  # mean latency per pass
        n_lat = [0, 0, 0]
        billed_total = 0.0
        avoided_total = 0.0
        # correctness: per (instance, turn) the set of content hashes seen
        # across passes. Cache correctness = exactly ONE distinct hash per
        # request slot across all passes (HIT must replay the MISS content).
        sha_slots: dict = {}
        # token-level output identity: TR is input-side, so completion tokens
        # per request slot should be ~identical across arms. Divergence in the
        # cache-OFF arm (sampling) vs cache-ON (replay) is itself a signal.
        completion_slots: dict = {}
        for inst in arm["instances"]:
            for p in inst["passes"]:
                idx = p["pass"] - 1
                p_tokens[idx] += p["billed_prompt_tokens"]
                p_hits[idx] += p["hits"]
                p_reqs[idx] += p["hits"] + p["misses"]
                billed_total += p.get("billed_cost_usd", 0.0) or 0.0
                avoided_total += p.get("cost_avoided_usd", 0.0) or 0.0
                if p.get("latency_ms_mean"):
                    p_lat[idx] += p["latency_ms_mean"] * len(p.get("content_shas", {}))
                    n_lat[idx] += len(p.get("content_shas", {}))
                for ti, sha in p.get("content_shas", {}).items():
                    sha_slots.setdefault((inst["instance_id"], ti), set()).add(sha)
                for ti, ct in p.get("completion_tokens", {}).items():
                    completion_slots.setdefault((inst["instance_id"], ti), []).append(ct)

        drift_slots = sum(1 for shas in sha_slots.values() if len(shas) > 1)
        ct_values = [c for slots in completion_slots.values() for c in slots]
        summary.append(
            {
                "arm": arm["arm"],
                "model": arm["model"],
                "tokens_p1": p_tokens[0],
                "tokens_p2": p_tokens[1],
                "tokens_p3": p_tokens[2],
                "tokens_total": sum(p_tokens),
                "completion_tokens_total": sum(ct_values),
                "completion_tokens_per_slot_stable": all(
                    len(set(cts)) == 1 for cts in completion_slots.values()
                )
                if completion_slots
                else None,
                "hit_ratio_p2": round(p_hits[1] / p_reqs[1], 3) if p_reqs[1] else 0.0,
                "hit_ratio_p3": round(p_hits[2] / p_reqs[2], 3) if p_reqs[2] else 0.0,
                "latency_ms_mean_p1": round(p_lat[0] / n_lat[0]) if n_lat[0] else None,
                "latency_ms_mean_p2": round(p_lat[1] / n_lat[1]) if n_lat[1] else None,
                "correctness": {
                    "request_slots_tracked": len(sha_slots),
                    "slots_with_content_drift": drift_slots,
                    "verdict": "PASS — every HIT replayed the original content"
                    if drift_slots == 0
                    else f"FAIL — {drift_slots} slots served different content across passes",
                },
                "est_cost_total_usd": round(sum(p_tokens) / 1e6 * price_per_m, 4),
                "billed_cost_usd": round(billed_total, 4),
                "cost_avoided_usd": round(avoided_total, 4),
                **_energy_metrics(
                    hits_total=sum(p_hits), tokens_cached=_cached_tokens(p_tokens, p_hits, p_reqs)
                ),
            }
        )
    return summary


# ── Energy conversion (assumption-bound, labeled) ───────────────────────────
# Flash-class MoE serving (DeepSeek V4 Flash ≈ 37B active params): 8–20K
# tok/s/GPU at 400–700W draw, PUE 1.2 → 5–25 Wh per 1K tokens. Generic
# frontier dense models run 22–117 Wh/1K. We report the flash-class range
# for flash models and mark it as an estimate, never billing-verified.
FLASH_WH_PER_1K_TOK_LOW = 5.0
FLASH_WH_PER_1K_TOK_HIGH = 25.0
CO2_KG_PER_KWH = 0.4  # global grid average


def _cached_tokens(p_tokens: list, p_hits: list, p_reqs: list) -> int:
    """Tokens NOT recomputed = tokens that would have been billed on warm
    passes but were served from cache. Approximation: (hit ratio × pass
    tokens) summed over warm passes."""
    saved = 0
    for idx in (1, 2):  # passes 2,3 are warm
        if p_reqs[idx]:
            saved += int(p_tokens[idx] * p_hits[idx] / p_reqs[idx])
    return saved


def _energy_metrics(hits_total: int, tokens_cached: int) -> dict:
    if tokens_cached <= 0:
        return {
            "energy": {
                "tokens_not_recomputed": 0,
                "kwh_avoided": [0.0, 0.0],
                "kg_co2e_avoided": [0.0, 0.0],
            }
        }
    kwh_low = tokens_cached * (FLASH_WH_PER_1K_TOK_LOW / 1000) / 1000
    kwh_high = tokens_cached * (FLASH_WH_PER_1K_TOK_HIGH / 1000) / 1000
    return {
        "energy": {
            "tokens_not_recomputed": tokens_cached,
            "assumptions": f"flash-MoE {FLASH_WH_PER_1K_TOK_LOW}-{FLASH_WH_PER_1K_TOK_HIGH} Wh/1K tok, PUE 1.2, "
            f"{CO2_KG_PER_KWH} kg CO2/kWh — estimate, not billing-verified",
            "kwh_avoided": [round(kwh_low, 4), round(kwh_high, 4)],
            "kg_co2e_avoided": [
                round(kwh_low * CO2_KG_PER_KWH, 4),
                round(kwh_high * CO2_KG_PER_KWH, 4),
            ],
        }
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--edge", default=DEFAULT_EDGE, help="Edge chat-completions URL")
    ap.add_argument(
        "--auth-token",
        default=os.environ.get("TOOLRECALL_EDGE_TOKEN", ""),
        help="Edge bearer token (arm key used as Authorization)",
    )
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    ap.add_argument("--max-turns", type=int, default=int(os.getenv("MEASURE_MAX_TURNS", "8")))
    ap.add_argument(
        "--price",
        type=float,
        default=float(os.getenv("MEASURE_PRICE", "0.15")),
        help="$/M input tokens for cost estimates",
    )
    ap.add_argument("--arms", default="A,B,C,D", help="Comma-separated arm subset")
    ap.add_argument(
        "--out",
        default=None,
        help="Output JSON path (default: bench/results/warp_bench_<UTC>.json + .jsonl)",
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke gate: 1 instance x 2 turns x 2 passes x 4 arms (~$0.001). "
        "Verify each datapoint before the full run.",
    )
    ap.add_argument(
        "--endurance",
        type=int,
        default=0,
        metavar="N_SESSIONS",
        help="Session-endurance mode: N sequential sessions (each = full pass over "
        "the instances) spaced --session-gap seconds apart. Tests whether cache "
        "hits survive across session boundaries (fleet re-use) and whether the "
        "chain stays error-free over a long horizon. Session 0 populates the "
        "cache; sessions 1..N-1 measure the decay curve. Requires cache-ON arms.",
    )
    ap.add_argument(
        "--session-gap",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Wall-clock gap between endurance sessions (default 0). Use e.g. 300 "
        "to probe TTL expiry (TOOLRECALL_API_TTL=300 → session 2+ should MISS).",
    )
    ap.add_argument(
        "--concurrent",
        action="store_true",
        help="Run selected arms simultaneously (fleet simulation). Tests cache "
        "correctness under racing identical requests. Requires ≥2 keys.",
    )
    ap.add_argument(
        "--shared-context",
        action="store_true",
        help="Shared-context mode (claim C13): prepend identical doc-read "
        "messages (~README/CONTRIBUTING per instance repo) to EVERY request "
        "of EVERY arm — models a fleet working the same repo on different "
        "issues. The TR file-cache layer is what should absorb the re-sends.",
    )
    ap.add_argument(
        "--retry-probe",
        action="store_true",
        help="Retry-semantics probe (claim C14): after the main runs, resend "
        "one request twice and assert the cached response replays "
        "byte-identically (content_sha equal). Tests identical-retry behavior.",
    )
    ap.add_argument(
        "--instances",
        type=int,
        default=10,
        metavar="N",
        help="Number of SWE-bench instances (default 10 — matches the "
        "LiteLLM dedup run; 3 cannot separate signal from instance noise).",
    )
    args = ap.parse_args()

    # Edge auth: the ARM KEY is what the provider must see. The edge token is
    # separate — if set, prepend it via TOOLRECALL_EDGE_TOKEN on the edge side;
    # here we send the arm key as Authorization (which the edge relays).
    # NOTE: when edge auth is enabled, edge auth and provider key conflict on
    # the same Authorization header. For the benchmark the edge runs with auth
    # DISABLED and bound to loopback (tunnel not used). Document this.
    loaded = load_instances(args.instances)
    instances = loaded["instances"]
    n_available = sum(1 for i in instances if i.get("patch"))
    print(f"Loaded {n_available}/{len(instances)} SWE-bench instances", flush=True)

    if args.smoke:
        instances = instances[:1]
        args.max_turns = 2
        print("SMOKE MODE: 1 instance, 2 turns, 2 passes per arm", flush=True)

    shared_preamble = None
    if args.shared_context:
        # Built once from the FIRST instance's repo — byte-identical preamble
        # for every session/arm in this run (that's the point: same fleet).
        shared_preamble = build_shared_preamble(instances[0])

    jsonl_rows = []
    selected = [a.strip()[0] for a in args.arms.split(",")]
    results = []
    for arm_name, key_env, model, cache_on in ARMS:
        letter = arm_name[0]
        if letter not in selected:
            continue
        key = os.environ.get(key_env, "").strip()
        if not key:
            print(f"!! {key_env} not set — skipping arm {letter}", flush=True)
            continue
        print(f"\n=== Arm {letter}: {arm_name} (model={model}) ===", flush=True)
        if args.concurrent:
            # Concurrent mode: all selected arms run simultaneously — mirrors a
            # real fleet (multiple agents hitting the cache at once) and tests
            # cache correctness under racing identical requests.
            selected_specs = [
                (an, ke, m, co)
                for an, ke, m, co in ARMS
                if an[0] in selected and os.environ.get(ke, "").strip()
            ]
            if len(selected_specs) > 1:
                import threading

                def _arm_worker(name, kenv, mdl, nc, out_list):
                    out_list.append(
                        run_arm(
                            name,
                            os.environ.get(kenv, "").strip(),
                            mdl,
                            args.edge,
                            "",
                            instances,
                            args.max_turns,
                            jsonl_rows,
                            shared_preamble=shared_preamble,
                            no_cache=nc,
                        )
                    )

                print(f"CONCURRENT mode: {len(selected_specs)} arms in parallel", flush=True)
                threads = [
                    threading.Thread(target=_arm_worker, args=(an, ke, m, not co, results))
                    for an, ke, m, co in selected_specs
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                break  # all arms already ran
        results.append(
            run_arm(
                arm_name,
                key,
                model,
                args.edge,
                "",
                instances,
                args.max_turns,
                jsonl_rows,
                shared_preamble=shared_preamble,
                no_cache=not cache_on,
            )
        )
    endurance_summary = None
    if args.endurance > 0:
        print(
            f"\n### ENDURANCE: {args.endurance} sessions, gap {args.session_gap}s "
            f"(cache-ON arms only) ###",
            flush=True,
        )
        endurance_results = []
        for arm_name, key_env, model, cache_on in ARMS:
            if not cache_on or arm_name[0] not in selected:
                continue
            key = os.environ.get(key_env, "").strip()
            if not key:
                continue
            sessions = []
            for s in range(args.endurance):
                if s > 0 and args.session_gap > 0:
                    print(f"  [endurance/{arm_name}] gap {args.session_gap}s...", flush=True)
                    time.sleep(args.session_gap)
                print(f"  [endurance/{arm_name}] session {s}", flush=True)
                # one session = one full pass over all instances/turns,
                # tagged with the session index in the JSONL rows
                before = len(jsonl_rows)
                run_arm(
                    f"{arm_name}_s{s}",
                    key,
                    model,
                    args.edge,
                    "",
                    instances,
                    args.max_turns,
                    jsonl_rows,
                    shared_preamble=shared_preamble,
                )
                for row in jsonl_rows[before:]:
                    row["endurance_session"] = s
                hits = sum(1 for r in jsonl_rows[before:] if r["cache"] == "HIT")
                misses = sum(1 for r in jsonl_rows[before:] if r["cache"] == "MISS")
                errs = sum(1 for r in jsonl_rows[before:] if r["error"])
                cost = sum(r["total_cost"] or 0 for r in jsonl_rows[before:])
                sessions.append(
                    {
                        "session": s,
                        "hits": hits,
                        "misses": misses,
                        "errors": errs,
                        "hit_ratio": round(hits / (hits + misses), 3) if hits + misses else 0.0,
                        "billed_cost_usd": round(cost, 6),
                    }
                )
                print(
                    f"  [endurance/{arm_name}] session {s}: "
                    f"{hits}H/{misses}M/{errs}E cost=${cost:.6f}",
                    flush=True,
                )
            endurance_results.append({"arm": arm_name, "model": model, "sessions": sessions})
        endurance_summary = endurance_results

    # ── Retry-semantics probe (claim C14, identical-success variant) ────────
    retry_probe = None
    if args.retry_probe and results:
        print("\n### RETRY PROBE: identical request ×2 → replay must be byte-exact ###", flush=True)
        arm_name, key_env, model, _ = next(
            (a for a in ARMS if a[0] in results and os.environ.get(a[1], "").strip()), ARMS[0]
        )
        # arm_name like "A_cache_on" — results entries use the same name
        key = os.environ.get(key_env, "").strip()
        probe = build_session_convo(instances[0], max_turns=args.max_turns, with_snapshots=True)
        probe_snapshots = probe[3] if len(probe) > 3 else [probe[0]]
        if shared_preamble:
            probe_snapshots = [list(shared_preamble) + list(s) for s in probe_snapshots]
        msgs = probe_snapshots[-1]  # largest turn = the "failed agent run" retry
        attempts = []
        for attempt in (1, 2):
            r = send_through_edge(args.edge, key, model, msgs)
            attempts.append(
                {
                    "attempt": attempt,
                    "cache": r["cache"],
                    "content_sha": r["content_sha"],
                    "latency_ms": r["latency_ms"],
                    "total_cost": r["total_cost"],
                    "error": r["error"],
                }
            )
            print(
                f"  attempt {attempt}: cache={r['cache']} sha={r['content_sha']} err={r['error']}",
                flush=True,
            )
        ok = (
            attempts[0]["error"] is None
            and attempts[1]["error"] is None
            and attempts[0]["content_sha"] is not None
            and attempts[0]["content_sha"] == attempts[1]["content_sha"]
        )
        retry_probe = {
            "claim": "C14 identical-success retry replays byte-exact",
            "model": model,
            "arm": arm_name,
            "attempts": attempts,
            "verdict": "PASS" if ok else "FAIL",
        }
        print(f"  C14 verdict: {retry_probe['verdict']}", flush=True)
        jsonl_rows.append(
            {
                "arm": arm_name,
                "model": model,
                "instance_id": instances[0]["instance_id"],
                "pass": "retry_probe",
                "turn": -1,
                "cache": attempts[1]["cache"],
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_cost": sum(a["total_cost"] or 0 for a in attempts),
                "cached_tokens": None,
                "latency_ms": None,
                "content_sha": attempts[1]["content_sha"],
                "error": None if ok else "retry probe content mismatch",
                "error_class": None if ok else "other",
                "ts": datetime.now(timezone.utc).isoformat(),
                "retry_probe": True,
            }
        )

    if not results:
        print("No arms ran — set KEY_A..KEY_D", flush=True)
        return 1

    summary = summarize(results, args.price)

    payload = {
        "meta": {
            "date": datetime.now(timezone.utc).isoformat(),
            "edge": args.edge,
            "dataset": "princeton-nlp/swe-bench-lite (test split)",
            "instances": [i["instance_id"] for i in instances],
            "passes_per_instance": N_PASSES,
            "smoke": args.smoke,
            "price_per_m_input_usd": args.price,
            "note_auth": "edge on loopback, auth disabled; arms isolated by per-arm keys",
            "note_cost": "total_cost is OpenRouter-billed per request; est_cost is "
            "tokens*price fallback. cached_tokens = provider prefix cache (separate).",
        },
        "summary": summary,
        "endurance": endurance_summary,
        "retry_probe": retry_probe,
        "shared_context": bool(args.shared_context),
        "raw": results,
    }

    # Named output files (never overwrite): JSON summary + JSONL raw rows
    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = args.out or os.path.join(out_dir, f"warp_bench_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump(payload, f, indent=2)
    with open(base + ".jsonl", "w") as f:
        for row in jsonl_rows:
            f.write(json.dumps(row) + "\n")
    print(f"\nWrote {base}.json (+{len(jsonl_rows)} raw rows in .jsonl)", flush=True)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("\n" + "=" * 100)
        print(
            f"{'Arm':<22}{'tok p1':>9}{'tok p2':>9}{'tok p3':>9}{'total':>9}"
            f"{'hit% p2':>8}{'lat p1':>8}{'lat p2':>8}{'drift':>7}"
            f"{'billed $':>9}{'avoided $':>10}"
        )
        for s in summary:
            lat1 = s["latency_ms_mean_p1"]
            lat2 = s["latency_ms_mean_p2"]
            print(
                f"{s['arm']:<22}{s['tokens_p1']:>9,}{s['tokens_p2']:>9,}"
                f"{s['tokens_p3']:>9,}{s['tokens_total']:>9,}"
                f"{s['hit_ratio_p2'] * 100:>7.0f}%"
                f"{(str(lat1) + 'ms') if lat1 else 'n/a':>8}"
                f"{(str(lat2) + 'ms') if lat2 else 'n/a':>8}"
                f"{s['correctness']['slots_with_content_drift']:>7}"
                f"{s.get('billed_cost_usd', 0):>9.4f}"
                f"{s.get('cost_avoided_usd', 0):>10.4f}"
            )
        print("\nCorrectness verdicts:")
        for s in summary:
            print(
                f"  {s['arm']}: {s['correctness']['verdict']} "
                f"({s['correctness']['request_slots_tracked']} slots tracked)"
            )
        if endurance_summary:
            print("\nSession endurance (cache-ON arms):")
            for arm in endurance_summary:
                curve = " → ".join(
                    f"s{sess['session']}:{sess['hit_ratio'] * 100:.0f}%H"
                    f"/${sess['billed_cost_usd']:.4f}"
                    for sess in arm["sessions"]
                )
                total_err = sum(sess["errors"] for sess in arm["sessions"])
                print(f"  {arm['arm']}: {curve} | total errors: {total_err}")

        # ── Claims verdict table: every pitch claim → measured row ─────────
        def _verdict():
            v = {}
            a = next((s for s in summary if s["arm"].startswith("A")), None)
            b = next((s for s in summary if s["arm"].startswith("B")), None)
            c = next((s for s in summary if s["arm"].startswith("C")), None)
            # C1 same-task re-run savings — billing-verified: cache-ON arm must
            # bill LESS than the equal-work cache-OFF baseline (not just fewer
            # tokens; provider prefix cache discounts confuse token counts).
            if a and b:
                a_billed = a.get("billed_cost_usd", 0)
                b_billed = b.get("billed_cost_usd", 0)
                v["C1 same-task re-run saves cost"] = (
                    f"PASS — billed ${a_billed:.4f} vs baseline ${b_billed:.4f} "
                    f"({(1 - a_billed / b_billed) * 100:.0f}% saved, billing-verified)"
                    if (a_billed < b_billed and a["hit_ratio_p2"] > 0.5)
                    else "FAIL"
                )
            # C2/C3 endurance
            if endurance_summary:
                arm0 = endurance_summary[0]
                s_ratio = [sess["hit_ratio"] for sess in arm0["sessions"]]
                v["C2/C3 savings persist across sessions"] = (
                    "PASS" if s_ratio and s_ratio[-1] > 0.5 else "FAIL"
                )
            else:
                v["C2/C3 savings persist across sessions"] = "NOT RUN (no --endurance)"
            # C4 TTL expiry
            if endurance_summary and args.session_gap > 0:
                v["C4 TTL expiry (see gap config)"] = "MEASURED — inspect decay curve"
            else:
                v["C4 TTL expiry"] = "NOT RUN (no --session-gap)"
            # C5 correctness
            if a:
                v["C5 HIT replays byte-exact"] = (
                    "PASS" if a["correctness"]["slots_with_content_drift"] == 0 else "FAIL"
                )
            # C6 concurrency
            v["C6 concurrency correctness"] = (
                "MEASURED" if args.concurrent else "NOT RUN (no --concurrent)"
            )
            # C7 cross-model
            v["C7 works across models"] = (
                "MEASURED (arm C)" if c else "NOT RUN (arm C not selected)"
            )
            # C8 latency
            if a and a["latency_ms_mean_p2"] and a["latency_ms_mean_p1"]:
                v["C8 HIT faster than MISS"] = (
                    "PASS" if a["latency_ms_mean_p2"] < a["latency_ms_mean_p1"] else "FAIL"
                )
            # C9/C10 billing & prefix separation — structural, always on
            v["C9 billing-verified cost"] = "RECORDED (total_cost per request)"
            v["C10 prefix cache tracked apart"] = "RECORDED (cached_tokens per request)"
            # C11 energy
            if a:
                kwh = a["energy"]["kwh_avoided"]
                v["C11 energy not recomputed"] = f"ESTIMATE {kwh[0]}-{kwh[1]} kWh"
            # C12/C13/C14
            v["C12 solo-user repeatable ≈ 0"] = "DONE (tr-warp-stats: 0.38%/0.22%)"
            if getattr(args, "shared_context", False):
                v["C13 README/file-cache claim"] = (
                    "MEASURED — shared-context mode (see per-arm cost delta)"
                )
            else:
                v["C13 README/file-cache claim"] = "NOT RUN (no --shared-context)"
            rp = getattr(args, "_retry_probe_result", None)
            v["C14 retry: identical-success replays byte-exact"] = (
                rp["verdict"] if rp else "NOT RUN (no --retry-probe)"
            )
            return v

        print("\n" + "=" * 100)
        args._retry_probe_result = retry_probe  # feed C14 verdict row
        print("  CLAIMS VERDICT (every pitch claim → measured row)")
        print("=" * 100)
        for claim, verdict in _verdict().items():
            print(f"  {claim:<42} {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
