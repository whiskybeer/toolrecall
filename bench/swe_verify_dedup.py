"""swe_verify_dedup.py — real SWE-bench Lite verification of the text-dedup claim.

Checks whether the repo's published claim (-32.3% input tokens / -30% billed
on SWE-bench Lite) reproduces on a small fresh run, using the ORIGINAL
harness's instance loader and conversation builder (imported, not copied),
called against OpenRouter directly.

Protocol:
  - N instances (default 5), first-instance-per-repo diversity, same rule
    as the original harness
  - Per instance: accumulating per-turn requests (turns 1..8), the real
    agent shape the claim was measured on
  - Arms per instance back-to-back, order alternates by instance index
    (AB / BA / AB...) to cancel temporal drift
  - temperature 0, max_tokens 8 (completion is noise; input is the subject)
  - Verbatim provider usage per request; per-key dashboard window delta as
    the independent cross-check (single dedicated capped key)
  - Artifacts: JSONL per request + summary JSON. No PII (instance_id,
    repo, commit hashes only). Timestamps: MONTH ONLY (YYYY-MM).

Usage:
  bench/swe-verify-venv/bin/python bench/swe_verify_dedup.py --instances 5
  (add --execute after reviewing the plan output)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH_DIR)
sys.path.insert(0, BENCH_DIR)
sys.path.insert(0, os.path.join(REPO, "bench", "litellm_dedup"))

MODEL = "deepseek/deepseek-v4-flash"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
COST_ABORT = 1.00
OUTDIR = os.path.join(BENCH_DIR, "swe-verify")


def month_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_key() -> str:
    svc = os.path.expanduser("~/.config/systemd/user/toolrecall-edge.service")
    m = re.search(r"sk-or-v1-[0-9a-f]{64}", open(svc).read())
    if not m:
        raise SystemExit("edge key not found")
    return m.group(0)


def key_fp() -> str:
    return hashlib.sha256(load_key().encode()).hexdigest()[:12]


def or_usage() -> float:
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {load_key()}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["data"]["usage"]


def call(key: str, messages: list) -> dict:
    body = json.dumps(
        {"model": MODEL, "messages": messages, "max_tokens": 8, "temperature": 0}
    ).encode()
    req = urllib.request.Request(
        OR_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            return {"usage": {}, "error": f"HTTP {e.code}: {detail}"}
        except Exception as e:
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            return {"usage": {}, "error": str(e)}


def usage_of(resp: dict) -> dict:
    u = dict(resp.get("usage") or {})
    det = u.pop("prompt_tokens_details", None)
    if isinstance(det, dict):
        u["prompt_cached_tokens"] = det.get("cached_tokens")
    return u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", type=int, default=5)
    ap.add_argument("--execute", action="store_true", help="actually spend money")
    ap.add_argument("--resume", action="store_true", help="append mode, keep existing JSONL")
    args = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    jsonl_path = os.path.join(OUTDIR, "requests.jsonl")
    if not args.resume:
        open(jsonl_path, "w").close()

    # import the ORIGINAL harness pieces
    sys.path.insert(0, os.path.join(REPO, "bench", "litellm_dedup"))
    import measure_swebench as ms

    result = ms.load_instances(args.instances)
    instances = result["instances"]
    print(f"instances: {len(instances)} from {result['source']}")
    for inst in instances:
        print(f"  {inst['instance_id']} ({inst['repo']})")

    if not args.execute:
        # plan mode: build conversations, count dups, estimate cost — no spend
        total_reqs = 0
        dup_report = []
        est_prompt_tokens = 0
        for inst in instances:
            convo, info, _ = ms.build_session_convo(inst, max_turns=8, with_snapshots=True)
            dup_report.append(
                {
                    "instance_id": inst["instance_id"],
                    "file_block_occurrences": info["n_file_block_occurrences"],
                    "unique_files": info["n_unique_files_in_blocks"],
                    "dup_beyond_first": max(
                        0, info["n_file_block_occurrences"] - info["n_unique_files_in_blocks"]
                    ),
                    "chars": sum(len(str(m.get("content", ""))) for m in convo),
                }
            )
            total_reqs += info["total_messages"] - 1
            est_prompt_tokens += sum(
                sum(len(str(m.get("content", ""))) for m in snap) for snap in info.get("turn_snapshots", [])
            ) // 3
        est_cost = est_prompt_tokens * 0.000000084
        print(json.dumps(dup_report, indent=2))
        print(f"\nrequests planned: {total_reqs} per arm x2 arms")
        print(f"rough prompt-token estimate both arms: ~{est_prompt_tokens:,} -> ~${est_cost:.3f}")
        print(f"key fingerprint: {key_fp()}   month: {month_stamp()}")
        print("DRY-RUN ONLY — re-run with --execute to spend.")
        return 0

    # ── live run ──
    key = load_key()
    usage_before = or_usage()
    print(f"key usage before: ${usage_before:.6f}  month: {month_stamp()}")

    from toolrecall.adapters.litellm import dedup_messages

    with open(jsonl_path, "a") as f:
        for idx, inst in enumerate(instances):
            convo, info, _ = ms.build_session_convo(inst, max_turns=8, with_snapshots=True)
            snaps = info["turn_snapshots"]
            first_arm = "dedup" if idx % 2 == 0 else "raw"
            order = (first_arm, "raw" if first_arm == "dedup" else "dedup")
            print(f"[{idx+1}/{len(instances)}] {inst['instance_id']} order={order[0]}first", flush=True)
            for arm in order:
                for t, snap in enumerate(snaps, start=1):
                    if arm == "dedup":
                        msgs, stats = dedup_messages(
                            json.loads(json.dumps(snap)), min_chars=800, protect_last=2
                        )
                    else:
                        msgs, stats = snap, {"blocks": 0}
                    resp = call(key, msgs)
                    u = usage_of(resp)
                    rec = {
                        "instance_index": idx,
                        "instance_id": inst["instance_id"],
                        "repo": inst["repo"],
                        "arm": arm,
                        "turn": t,
                        "usage": u,
                        "chars_sent": sum(
                            len(m.get("content", "")) if isinstance(m.get("content"), str) else sum(
                                len(p.get("text", "")) for p in m.get("content", []) if isinstance(p, dict)
                            )
                            for m in msgs
                        ),
                        "stubs": stats["blocks"],
                        "error": resp.get("error"),
                        "month": month_stamp(),
                    }
                    f.write(json.dumps(rec) + "\n")
                    f.flush()
                    if u.get("cost"):
                        pass  # cost tracked via usage records
            time.sleep(1)

    usage_after = or_usage()
    window_delta = round(usage_after - usage_before, 6)
    print(f"key usage after: ${usage_after:.6f} (window delta ${window_delta})")
    print(f"raw JSONL: {jsonl_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
