#!/usr/bin/env python3
"""Per-turn instrumentation for the crossover benchmark.

Extracts per-turn token, prefix-cache, block-repeat, and stub metrics from
Claude Code session JSONL files (and optionally ToolRecall's proxy_usage.csv
for Arm B).

Per-turn fields emitted:
  turn, message_id, prompt_tokens, input_tokens, cache_read, cache_creation,
  output_tokens, distinct_block_bytes, repeated_block_bytes, repeat_share,
  stub_bytes_served, reread_ratio, unique_files_read, total_file_reads
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

MIN_CHARS = int(os.environ.get("TOOLRECALL_DEDUP_MIN_CHARS", "800"))
PROTECT_LAST = int(os.environ.get("TOOLRECALL_DEDUP_PROTECT_LAST", "2"))
STUB_LEN = 120
DEFAULT_PROJ_DIR = Path(os.path.expanduser("~/.claude/projects"))


def _session_jsonl_path(cwd: str, session_id: str, proj_dir: Path = DEFAULT_PROJ_DIR) -> Path | None:
    if not session_id:
        return None
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    p = proj_dir / slug / f"{session_id}.jsonl"
    return p if p.exists() else None


def _extract_text_blocks(msg_obj: dict) -> list[str]:
    """Extract text blocks from a Claude Code JSONL user/tool_result entry."""
    message = msg_obj.get("message") or {}
    content = message.get("content")
    out: list[str] = []
    if isinstance(content, str):
        if len(content) >= MIN_CHARS:
            out.append(content)
        return out
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "text":
                txt = item.get("text") or ""
                if len(txt) >= MIN_CHARS:
                    out.append(txt)
            elif itype == "tool_result":
                tc = item.get("content")
                if isinstance(tc, str) and len(tc) >= MIN_CHARS:
                    out.append(tc)
                elif isinstance(tc, list):
                    for sub in tc:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            txt = sub.get("text") or ""
                            if len(txt) >= MIN_CHARS:
                                out.append(txt)
    return out


def _extract_read_paths(msg_obj: dict) -> list[str]:
    """Extract file_path targets from assistant Read tool_use blocks."""
    message = msg_obj.get("message") or {}
    content = message.get("content")
    paths: list[str] = []
    if not isinstance(content, list):
        return paths
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use" and item.get("name") == "Read":
            inp = item.get("input") or {}
            fp = inp.get("file_path") or inp.get("path")
            if isinstance(fp, str) and fp:
                paths.append(fp)
    return paths


def load_proxy_dedup_tokens(
    usage_csv: Path | None,
    ts_start: float | None = None,
    ts_end: float | None = None,
) -> list[int]:
    """Load per-call dedup_tokens from proxy_usage.csv within [ts_start, ts_end]."""
    if usage_csv is None or not usage_csv.exists():
        return []
    tokens: list[int] = []
    with open(usage_csv, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = float(row.get("timestamp") or 0.0)
            except ValueError:
                continue
            if ts_start is not None and ts < ts_start:
                continue
            if ts_end is not None and ts > ts_end:
                continue
            path = row.get("target_path") or ""
            if "/messages" not in path and "/chat/completions" not in path:
                continue
            try:
                tokens.append(int(float(row.get("dedup_tokens") or 0)))
            except ValueError:
                tokens.append(0)
    return tokens


def extract_session_turns(
    jsonl_path: Path,
    arm: str = "A",
    proxy_dedup_tokens: list[int] | None = None,
    min_chars: int = MIN_CHARS,
    protect_last: int = PROTECT_LAST,
) -> list[dict]:
    """Parse a Claude Code session JSONL into per-turn instrumentation records."""
    if not jsonl_path.exists():
        return []

    seen_msg_ids: set[str] = set()
    # Cumulative block history across messages up to each assistant turn.
    # Each entry is (sha256_digest, byte_len).
    blocks_so_far: list[tuple[bytes, int]] = []
    files_read: list[str] = []
    turns: list[dict] = []

    with open(jsonl_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("isSidechain"):
                continue

            etype = entry.get("type")
            if etype == "user":
                for txt in _extract_text_blocks(entry):
                    if len(txt) >= min_chars:
                        b = txt.encode("utf-8", "replace")
                        dg = hashlib.sha256(b).digest()
                        blocks_so_far.append((dg, len(b)))
                continue

            if etype != "assistant":
                continue

            msg = entry.get("message") or {}
            mid = msg.get("id")
            if not mid or mid in seen_msg_ids:
                continue
            seen_msg_ids.add(mid)

            for fp in _extract_read_paths(entry):
                files_read.append(fp)

            usage = msg.get("usage") or {}
            inp = int(usage.get("input_tokens") or 0)
            cr = int(usage.get("cache_read_input_tokens") or 0)
            cc = int(usage.get("cache_creation_input_tokens") or 0)
            out = int(usage.get("output_tokens") or 0)
            pt = inp + cr + cc

            # Compute block-repeat statistics over the stubbable prefix.
            # Blocks in the last `protect_last` entries are never stubbed.
            stubbable_end = max(0, len(blocks_so_far) - protect_last)
            counts: Counter[bytes] = Counter()
            distinct_sizes: dict[bytes, int] = {}
            repeated_bytes = 0
            simulated_stub_bytes = 0

            for idx, (dg, sz) in enumerate(blocks_so_far):
                if dg not in distinct_sizes:
                    distinct_sizes[dg] = sz
                if idx < stubbable_end:
                    counts[dg] += 1
                    if counts[dg] > 1:
                        repeated_bytes += sz
                        simulated_stub_bytes += max(0, sz - STUB_LEN)
                else:
                    # Still count repeats in protected tail for repeat_share
                    counts[dg] += 1
                    if counts[dg] > 1:
                        repeated_bytes += sz

            distinct_bytes = sum(distinct_sizes.values())
            repeat_share = (repeated_bytes / distinct_bytes) if distinct_bytes > 0 else 0.0

            turn_idx = len(turns) + 1
            if arm == "B":
                if proxy_dedup_tokens and turn_idx - 1 < len(proxy_dedup_tokens):
                    stub_bytes = proxy_dedup_tokens[turn_idx - 1] * 4
                else:
                    stub_bytes = simulated_stub_bytes
            else:
                stub_bytes = 0

            uniq_files = len(set(files_read))
            total_reads = len(files_read)
            reread_ratio = (total_reads / turn_idx) if turn_idx > 0 else 0.0

            turns.append(
                {
                    "turn": turn_idx,
                    "message_id": mid,
                    "prompt_tokens": pt,
                    "input_tokens": inp,
                    "cache_read": cr,
                    "cache_creation": cc,
                    "output_tokens": out,
                    "distinct_block_bytes": distinct_bytes,
                    "repeated_block_bytes": repeated_bytes,
                    "repeat_share": round(repeat_share, 6),
                    "stub_bytes_served": stub_bytes,
                    "unique_files_read": uniq_files,
                    "total_file_reads": total_reads,
                    "reread_ratio": round(reread_ratio, 4),
                }
            )

    return turns


def summarize_turns(turns: list[dict]) -> dict:
    """Compute session-level summary over per-turn instrumentation records."""
    if not turns:
        return {
            "n_turns": 0,
            "prompt_tokens": 0,
            "input_tokens": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "output_tokens": 0,
            "stub_bytes_served": 0,
            "distinct_block_bytes": 0,
            "repeated_block_bytes": 0,
            "repeat_share": 0.0,
            "unique_files_read": 0,
            "total_file_reads": 0,
            "reread_ratio": 0.0,
        }
    last = turns[-1]
    return {
        "n_turns": len(turns),
        "prompt_tokens": sum(t["prompt_tokens"] for t in turns),
        "input_tokens": sum(t["input_tokens"] for t in turns),
        "cache_read": sum(t["cache_read"] for t in turns),
        "cache_creation": sum(t["cache_creation"] for t in turns),
        "output_tokens": sum(t["output_tokens"] for t in turns),
        "stub_bytes_served": sum(t["stub_bytes_served"] for t in turns),
        "distinct_block_bytes": last["distinct_block_bytes"],
        "repeated_block_bytes": last["repeated_block_bytes"],
        "repeat_share": last["repeat_share"],
        "unique_files_read": last["unique_files_read"],
        "total_file_reads": last["total_file_reads"],
        "reread_ratio": last["reread_ratio"],
    }


def instrument_run(
    row: dict,
    out_dir: Path,
    tag: str,
    proj_dir: Path = DEFAULT_PROJ_DIR,
    usage_csv: Path | None = None,
    ts_start: float | None = None,
    ts_end: float | None = None,
) -> dict:
    """Instrument one completed run, writing `<tag>.turns.jsonl` and returning summary."""
    p = _session_jsonl_path(row.get("cwd", ""), row.get("session_id", ""), proj_dir=proj_dir)
    if p is None:
        return summarize_turns([])
    dedup_toks = load_proxy_dedup_tokens(usage_csv, ts_start=ts_start, ts_end=ts_end) if row.get("arm") == "B" else None
    turns = extract_session_turns(p, arm=row.get("arm", "A"), proxy_dedup_tokens=dedup_toks)
    turns_path = out_dir / f"{tag}.turns.jsonl"
    with open(turns_path, "w") as fh:
        for t in turns:
            fh.write(json.dumps(t) + "\n")
    return summarize_turns(turns)


def extract_proxy_csv_turns(usage_csv: Path) -> list[dict]:
    """Parse a ToolRecall proxy_usage.csv (used by Hermes/stateless runs) into per-turn records."""
    if not usage_csv.exists():
        return []
    turns: list[dict] = []
    with open(usage_csv, newline="") as fh:
        for row in csv.DictReader(fh):
            pt = int(float(row.get("prompt_tokens") or 0))
            ct = int(float(row.get("completion_tokens") or 0))
            crt = int(float(row.get("cache_read_tokens") or 0))
            cwt = int(float(row.get("cache_write_tokens") or 0))
            dedup_tok = int(float(row.get("dedup_tokens") or 0))
            bust_tok = int(float(row.get("cache_bust_tokens") or 0))
            cost = float(row.get("cost_usd") or 0.0)
            discount = float(row.get("cache_discount_usd") or 0.0)
            inp = max(0, pt - crt - cwt)
            turns.append(
                {
                    "turn": len(turns) + 1,
                    "prompt_tokens": pt,
                    "input_tokens": inp,
                    "cache_read": crt,
                    "cache_creation": cwt,
                    "output_tokens": ct,
                    "cost_usd": cost,
                    "cache_discount_usd": discount,
                    "dedup_tokens": dedup_tok,
                    "stub_bytes_served": dedup_tok * 4,
                    "cache_bust_tokens": bust_tok,
                }
            )
    return turns


def ingest_endurance_results(
    result_json: Path,
    data_dir: Path,
    out_dir: Path,
    repo: str = "django",
    workload: str = "long",
    harness_filter: str = "hermes",
    arm_b: str = "tr_delta_dedup",
) -> Path:
    """Convert a run_*_endurance.py result JSON + usage-*.csv files into crossover runs.jsonl."""
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_out = out_dir / "runs.jsonl"
    data = json.loads(result_json.read_text())
    rows: list[dict] = []
    arm_b_aliases = {"tr_delta_dedup": ("tr_delta_dedup", "tr_optin"), "tr_optin": ("tr_delta_dedup", "tr_optin")}.get(
        arm_b, (arm_b,)
    )

    for r in data:
        if harness_filter and r.get("harness") != harness_filter:
            continue
        arm_raw = r.get("arm", "")
        if arm_raw == "baseline":
            arm = "A"
        elif arm_raw in arm_b_aliases:
            arm = "B"
        else:
            continue

        run_tag = r["run_tag"]
        usage_csv = data_dir / f"usage-{run_tag}.csv"
        turns = extract_proxy_csv_turns(usage_csv)
        seed = int(r.get("seed", 1))
        task_id = f"{repo}__endurance_{r.get('total_bugs', 0)}bugs"
        tag = f"{repo}__{task_id}__{workload}__{arm}_r{seed}"
        if turns:
            with open(out_dir / f"{tag}.turns.jsonl", "w") as fh:
                for t in turns:
                    fh.write(json.dumps(t) + "\n")

        u = r.get("usage") or {}
        pt = sum(t["prompt_tokens"] for t in turns) if turns else int(u.get("prompt_tokens") or 0)
        cr = sum(t["cache_read"] for t in turns) if turns else int(u.get("cache_read_tokens") or 0)
        cc = sum(t["cache_creation"] for t in turns) if turns else 0
        out_t = sum(t["output_tokens"] for t in turns) if turns else int(u.get("completion_tokens") or 0)
        inp = sum(t["input_tokens"] for t in turns) if turns else max(0, pt - cr - cc)
        cost = sum(t["cost_usd"] for t in turns) if turns else float(u.get("cost_usd") or 0.0)
        stub_bytes = sum(t["stub_bytes_served"] for t in turns)
        bust_tok = sum(t["cache_bust_tokens"] for t in turns) if turns else int(u.get("cache_bust_tokens") or 0)

        rows.append(
            {
                "task": task_id,
                "repo": repo,
                "workload": workload,
                "harness": r.get("harness"),
                "arm_raw": arm_raw,
                "arm": arm,
                "arm_order": "AB" if seed % 2 else "BA",
                "rep": seed,
                "model": r.get("model"),
                "resolved": bool(r.get("all_passed")),
                "turns": len(turns) if turns else int(u.get("rows") or 0),
                "prompt_tokens": pt,
                "input_tokens": inp,
                "cache_read": cr,
                "cache_creation": cc,
                "output_tokens": out_t,
                "cost_usd": cost,
                "stub_bytes_served": stub_bytes,
                "cache_bust_tokens": bust_tok,
                "repeat_share": 0.0,
                "per_bug": r.get("per_bug") or [],
            }
        )

    with open(runs_out, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return runs_out

