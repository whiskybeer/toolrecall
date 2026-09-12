"""bench_driver.py — Stage-3 media-dedup benchmark driver.

Isolated venvs (real pip installs of this repo), per-arm keys, interleaved
seed execution (A1,B1,A2,B2,A3,B3) to cancel temporal provider artifacts.
Per-key /auth/key usage deltas before/after as the independent billing
cross-check. Tripwires: toolrecall arm applies exactly the expected stub
count per seed (computed OFFLINE from the same workload); naive 0.

Usage: .venv/bin/python bench/bench_driver.py [--seeds 3]
Output: bench/media-bench/results.json + per-arm JSONL files.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH_DIR)
sys.path.insert(0, BENCH_DIR)

from bench_common import PAGES, STEPS_PER_PAGE, build_session  # noqa: E402

MODEL = "z-ai/glm-5.3-flash"
COST_ABORT = 2.00


def key_fp(source: str) -> str:
    if source == "edge":
        text = open(os.path.expanduser("~/.config/systemd/user/toolrecall-edge.service")).read()
    else:
        text = open(os.path.expanduser("~/.hermes/.env")).read()
    m = re.search(r"sk-or-v1-[0-9a-f]{64}", text)
    import hashlib

    return hashlib.sha256(m.group(0).encode()).hexdigest()[:12]


def or_usage(fp_label: str) -> dict:
    if fp_label == "edge":
        text = open(os.path.expanduser("~/.config/systemd/user/toolrecall-edge.service")).read()
    else:
        text = open(os.path.expanduser("~/.hermes/.env")).read()
    key = re.search(r"sk-or-v1-[0-9a-f]{64}", text).group(0)
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)["data"]
    return {"usage": d.get("usage"), "limit": d.get("limit")}


def offline_stubs(seed: int) -> int:
    """Tripwire value computed offline from the identical workload."""
    from toolrecall.adapters.litellm import dedup_messages

    session = build_session(seed)
    total = 0
    for t in range(1, len(session)):
        _, stats = dedup_messages(
            json.loads(json.dumps(session[: 1 + t])),
            min_chars=800,
            protect_last=2,
            media=True,
        )
        total += stats["blocks"]
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, help="run BOTH arms for one seed (append to JSONL)")
    ap.add_argument("--aggregate", action="store_true", help="aggregate JSONLs into results.json")
    args = ap.parse_args()
    if args.seed is None and not args.aggregate:
        ap.error("need --seed N or --aggregate")

    outdir = os.path.join(BENCH_DIR, "media-bench")
    os.makedirs(outdir, exist_ok=True)

    # 0. isolated venvs — real installs, never PYTHONPATH
    venvs = {}
    for arm in ("naive", "toolrecall"):
        vdir = os.path.join(outdir, f"venv-{arm}")
        if not os.path.exists(os.path.join(vdir, "bin", "python")):
            subprocess.run([sys.executable, "-m", "venv", vdir], check=True)
            subprocess.run([os.path.join(vdir, "bin", "pip"), "install", "-q", REPO], check=True)
        venvs[arm] = vdir
        ver = subprocess.run(
            [
                os.path.join(vdir, "bin", "python"),
                "-c",
                "import toolrecall, sys; print(toolrecall.__version__, sys.executable)",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        print(f"venv-{arm}: toolrecall {ver}")

    # 1. per-key billing snapshots BEFORE (only when running a seed)
    if args.seed is not None:
        before = {
            arm: or_usage("edge" if arm == "toolrecall" else "main")
            for arm in ("naive", "toolrecall")
        }
        print("before:", json.dumps(before))

    # 2. run: for the given seed, naive then toolrecall (interleaved across
    # seeds by invoking --seed 0,1,2 in order). Arms already fully recorded
    # for this seed are skipped (crash-resume without double billing).
    jsonl = {arm: os.path.join(outdir, f"{arm}.jsonl") for arm in ("naive", "toolrecall")}
    n_steps = len(build_session(0)) - 1
    spent = 0.0
    if args.seed is not None:
        for arm in ("naive", "toolrecall"):
            done = 0
            if os.path.exists(jsonl[arm]):
                done = sum(
                    1
                    for line in open(jsonl[arm])
                    if json.loads(line).get("seed") == args.seed
                )
            if done >= n_steps:
                print(
                    f"[driver] seed {args.seed} arm {arm}: already recorded ({done} reqs) — skipping"
                )
                continue
            print(f"=== seed {args.seed} arm {arm} ===", flush=True)
            subprocess.run(
                [
                    os.path.join(venvs[arm], "bin", "python"),
                    os.path.join(BENCH_DIR, "arm_worker.py"),
                    arm,
                    str(args.seed),
                    jsonl[arm],
                ],
                check=True,
                cwd=REPO,
            )
            spent = 0.0
            for a in ("naive", "toolrecall"):
                if not os.path.exists(jsonl[a]):
                    continue  # arm not yet run
                for line in open(jsonl[a]):
                    spent += (json.loads(line).get("usage") or {}).get("cost") or 0.0
            print(f"[driver] cumulative spend so far: ${spent:.4f}", flush=True)
            if spent > COST_ABORT:
                print(f"ABORT: ${spent:.2f} > ${COST_ABORT:.2f}")
                return 2

    # 3. per-key billing snapshots AFTER + cross-check (persisted across
    # per-seed invocations; --aggregate mode reads them from disk)
    if args.seed is not None:
        after = {
            arm: or_usage("edge" if arm == "toolrecall" else "main")
            for arm in ("naive", "toolrecall")
        }
        snap_path = os.path.join(outdir, "key-snapshots.json")
        snaps = json.load(open(snap_path)) if os.path.exists(snap_path) else {}
        snaps[str(args.seed)] = {
            "before": before,
            "after": after,
            "delta_usd": {
                arm: round(after[arm]["usage"] - before[arm]["usage"], 6) for arm in before
            },
        }
        json.dump(snaps, open(snap_path, "w"), indent=2)
        print(
            f"[driver] seed {args.seed} complete. Re-run with --aggregate when all seeds are done."
        )
        return 0

    # ── aggregate mode ──
    snaps = json.load(open(os.path.join(outdir, "key-snapshots.json")))
    seeds_done = sorted(int(s) for s in snaps)
    dashboard_total = round(
        sum(snaps[s]["delta_usd"]["naive"] + snaps[s]["delta_usd"]["toolrecall"] for s in snaps), 6
    )

    # 4. aggregate
    results = {"model": MODEL, "seeds": seeds_done, "arms": {}}
    trip_ok = True
    expected_stubs = {s: offline_stubs(s) for s in seeds_done}
    for arm in ("naive", "toolrecall"):
        recs = [json.loads(line) for line in open(jsonl[arm])]
        prompts = sum((r["usage"].get("prompt_tokens") or 0) for r in recs)
        cached = sum((r["usage"].get("prompt_cached_tokens") or 0) for r in recs)
        comps = sum((r["usage"].get("completion_tokens") or 0) for r in recs)
        cost = sum((r["usage"].get("cost") or 0.0) for r in recs)
        chars = sum(r["chars_sent"] for r in recs)
        stubs = sum(r["stubs"] for r in recs)
        results["arms"][arm] = {
            "requests": len(recs),
            "prompt_tokens": prompts,
            "prompt_cached_tokens": cached,
            "completion_tokens": comps,
            "billed_cost_usd": round(cost, 6),
            "chars_sent": chars,
            "stubs": stubs,
        }
        if arm == "naive" and stubs != 0:
            trip_ok = False
        if arm == "toolrecall" and stubs != sum(expected_stubs.values()):
            trip_ok = False

    results["dashboard_cross_check"] = {
        "key_fingerprints": {"naive": key_fp("main"), "toolrecall": key_fp("edge")},
        "per_seed_delta_usd": {s: snaps[s]["delta_usd"] for s in snaps},
        "billed_delta_usd_total": dashboard_total,
        "matches_response_usage": abs(
            dashboard_total
            - results["arms"]["naive"]["billed_cost_usd"]
            - results["arms"]["toolrecall"]["billed_cost_usd"]
        )
        < 0.01,
    }
    results["tripwires_pass"] = trip_ok
    results["total_cost_usd"] = dashboard_total

    off, on = results["arms"]["naive"], results["arms"]["toolrecall"]
    if off["prompt_tokens"]:
        results["delta"] = {
            "prompt_tokens_saved": off["prompt_tokens"] - on["prompt_tokens"],
            "prompt_tokens_saved_pct": round(
                100 * (off["prompt_tokens"] - on["prompt_tokens"]) / off["prompt_tokens"], 1
            ),
            "billed_cost_saved_usd": round(off["billed_cost_usd"] - on["billed_cost_usd"], 6),
            "uncached_tokens_saved": (off["prompt_tokens"] - off["prompt_cached_tokens"])
            - (on["prompt_tokens"] - on["prompt_cached_tokens"]),
        }
    results["workload"] = (
        f"{len(PAGES)} pages x {STEPS_PER_PAGE} steps x {len(seeds_done)} seeds, 1024x1024 PNGs, full-history requests"
    )

    with open(os.path.join(outdir, "results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(json.dumps(results, indent=2))
    ok = trip_ok and results["dashboard_cross_check"]["matches_response_usage"]
    print("BENCH:", "COMPLETE" if ok else "COMPLETE-WITH-TRIPWIRE-FAILURES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
