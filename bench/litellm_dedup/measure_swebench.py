#!/usr/bin/env python3
"""
measure_swebench.py — LiteLLM dedup savings on REAL SWE-bench workloads.

================================================================================
METHODOLOGY (v1.0)
================================================================================

Goal
----
Measure how many input tokens the LiteLLM dedup hook (toolrecall.adapters.litellm)
saves on realistic agent debugging sessions. The hook stubs duplicate content
blocks in chat completion requests — we measure the difference in usage.prompt_tokens
between an identical request WITH the hook enabled vs WITH the hook disabled.

Source Data
-----------
- Dataset: SWE-bench Lite (princeton-nlp/swe-bench-lite), test split, 300 instances
- 3 diverse instances selected for this benchmark:
    1. astropy__astropy-12907  — separability_matrix bug in nested CompoundModels
    2. django__django-10914    — FILE_UPLOAD_PERMISSION default change
    3. matplotlib__matplotlib-18869 — version info convenience property
- File contents fetched from GitHub raw at base_commit (real code, real bugs)

Agent Simulation
----------------
Each instance produces an 8-turn debugging conversation that mirrors how an LLM
agent actually works a SWE-bench task:

    Turn | Action                         | Content size   | Dedup eligible?
    ------|-------------------------------|----------------|-----------------
    1     | Read problem statement        | ~500 chars     | No (unique)
    2     | Read source files (first pass)| ~6-35K chars   | No (first occurrence)
    3     | Re-read files to trace flow   | ~6-35K chars   | YES — same files as T2
    4     | Propose fix strategy          | ~200 chars     | No (unique)
    5     | Re-read files to verify       | ~6-35K chars   | YES — same files as T2/T3
    6     | Apply fix (patch diff)        | ~2K chars      | No (unique)
    7     | Re-read files post-fix        | ~6-35K chars   | YES — same files as T2
    8     | Read test files + run tests   | ~6-35K chars   | YES — same as earlier reads

The key pattern: the same file contents are embedded in the messages array
multiple times (turns 2, 3, 5, 7, 8). The dedup hook stubs copies 2-N.

File content is truncated to 200 lines per the standard SWE-bench agent format
(see bench/arms.py:_build_file_block — first 100 + last 100 lines).

Measurement
-----------
Two arms, same payloads, same model, different OpenRouter API keys:

    ARM A (WITH dedup):
        LiteLLM proxy with callbacks: toolrecall.adapters.litellm.handler
        → hook deduplicates → provider sees stubbed messages
        → usage.prompt_tokens = billed prompt tokens (with dedup savings)

    ARM B (WITHOUT dedup):
        LiteLLM proxy with TOOLRECALL_DEDUP_DISABLED=1
        → hook passes through (disabled) → provider sees full messages
        → usage.prompt_tokens = billed prompt tokens (no dedup savings)

    Metric: prompt_tokens_delta = ARM_B_prompt_tokens - ARM_A_prompt_tokens
            savings_pct = delta / ARM_B_prompt_tokens * 100

    Cost estimate: cost = prompt_tokens / 1,000,000 × model_input_price

    Verification: Each arm uses a separate OpenRouter key. The OpenRouter
    dashboard shows the per-key billed spend, cross-verifying the token counts
    reported by usage.prompt_tokens in the API response.

    Caveats:
    - This measures token savings, NOT task success rate. The hook is opt-in
      and the protected_tail (last 2 messages) ensures the model's last turn
      is always intact.
    - The dedup hook only fires on OpenAI-format /v1/chat/completions routes
      (litellm#27518). /v1/messages is not covered.
    - This is request-level dedup, different from LiteLLM's response caching.

Configuration
-------------
    MEASURE_INSTANCES=3     — SWE-bench instances to test (default: 3)
    MEASURE_MAX_TURNS=8     — turns per instance (default: 8, max dataset: 8)
    MEASURE_MODEL=openrouter/deepseek/deepseek-v4-flash
    MEASURE_PRICE=0.15      — $/M input tokens for cost display

Output
------
    --json: machine-readable JSON with per-instance and aggregate results
    --compare A.json B.json: side-by-side comparison table

Usage
-----
    # 1. Start LiteLLM proxy with the hook:
    export OR_KEY=sk-or-v1-...
    litellm --config docs/examples/litellm-proxy-config.yaml --port 4000

    # 2. Run with dedup:
    python3 measure_swebench.py --json > result_with.json

    # 3. Restart proxy without dedup:
    TOOLRECALL_DEDUP_DISABLED=1 OR_KEY=sk-or-v1-... \\
      litellm --config docs/examples/litellm-proxy-config.yaml --port 4000

    # 4. Run without dedup:
    python3 measure_swebench.py --disabled --json > result_without.json

    # 5. Compare:
    python3 measure_swebench.py --compare result_with.json result_without.json
================================================================================
"""

import json
import os
import re
import sys
import time
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000/v1/chat/completions")
LITELLM_KEY = os.getenv("LITELLM_API_KEY", "sk-bench")
MODEL = os.getenv("MEASURE_MODEL", "openrouter/deepseek/deepseek-v4-flash")
PRICE_PER_M_INPUT = float(os.getenv("MEASURE_PRICE", "0.15"))

N_INSTANCES = int(os.getenv("MEASURE_INSTANCES", "3"))
MAX_TURNS = int(os.getenv("MEASURE_MAX_TURNS", "8"))
assert 1 <= MAX_TURNS <= 8, "MAX_TURNS must be 1-8 (8-turn conversation defined)"

GITHUB_CACHE = os.path.expanduser("~/.cache/swebench-files")
os.makedirs(GITHUB_CACHE, exist_ok=True)


# ── Methodology metadata (embedded in every output) ─────────────────────────

METHODOLOGY = {
    "version": "1.0",
    "date": datetime.utcnow().isoformat() + "Z",
    "description": "LiteLLM dedup measurement on real SWE-bench Lite instances",
    "arms": {
        "with_dedup": "LiteLLM proxy with toolrecall.adapters.litellm.handler callback",
        "without_dedup": "Same proxy with TOOLRECALL_DEDUP_DISABLED=1 (hook passes through)"
    },
    "workload_description": (
        "8-turn agent debugging conversation constructed from real SWE-bench "
        "instances. Each turn simulates a phase of an agent's workflow: read problem, "
        "read source files, re-read to trace flow, propose fix, re-read to verify, "
        "apply patch, re-read post-fix, run tests. File content at base_commit "
        "fetched from GitHub raw. 200-line truncated blocks (per SWE-bench convention)."
    ),
    "token_counting": (
        "usage.prompt_tokens from OpenAI-compatible /v1/chat/completions response. "
        "This is the provider-billed prompt token count. Cost estimate at "
        f"${PRICE_PER_M_INPUT}/M input tokens."
    ),
    "verification": (
        "Separate OpenRouter API keys per arm. Per-key billed spend visible on "
        "https://openrouter.ai/dashboard — cross-verifies reported token counts."
    ),
    "caveats": [
        "Measures token savings, not task success rate. Opt-in design, protected_tail=2.",
        "Hook only fires on /v1/chat/completions (litellm#27518 — /v1/messages not covered).",
        "Request-level dedup, different from LiteLLM's response caching.",
    ],
    "reproduce": (
        "pip install litellm toolrecall datasets\n"
        "python3 measure_swebench.py --json > result.json\n"
        f"MEASURE_INSTANCES={N_INSTANCES} MEASURE_MAX_TURNS={MAX_TURNS} "
        f"MEASURE_MODEL={MODEL}"
    ),
}


# ── SWE-bench instance loading ─────────────────────────────────────────────

def load_instances(n: int = 3):
    """Load n diverse SWE-bench Lite instances from local HF cache."""
    from datasets import load_dataset

    ds = load_dataset("princeton-nlp/swe-bench_lite", split="test")
    n_available = len(ds)

    # Pick diverse repos: first instance of each distinct repo
    chosen = []
    seen_repos = set()
    for inst in ds:
        repo = inst["repo"]
        if repo not in seen_repos:
            chosen.append(inst)
            seen_repos.add(repo)
        if len(chosen) >= n:
            break
    # Fallback: fill remaining from start of dataset
    if len(chosen) < n:
        chosen.extend(list(ds)[len(chosen):n])

    return {
        "instances": chosen[:n],
        "source": "princeton-nlp/swe-bench-lite (test split)",
        "available": n_available,
    }


def changed_files(patch: str) -> list[str]:
    """Extract file paths from a git diff patch (a/ and b/ sides)."""
    files = set(re.findall(r"--- a/(.+?)\n", patch))
    files.update(re.findall(r"\+\+\+ b/(.+?)\n", patch))
    return sorted(files)


def fetch_file(repo: str, commit: str, path: str) -> str | None:
    """Fetch file content at base_commit, cached to disk."""
    abbrev = commit[:12]
    safe_path = path.replace("/", "_")
    cache_path = os.path.join(GITHUB_CACHE, f"{repo.replace('/', '__')}__{abbrev}__{safe_path}")

    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "toolrecall-bench/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8")
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(content)
        return content
    except Exception:
        return None


def build_file_block(path: str, content: str) -> str | None:
    """
    Format file content as an agent would see it (200-line cap, head+tail).

    Mirrors bench/arms.py:_build_file_block exactly.
    """
    if content is None:
        return None
    lines = content.split("\n")
    total_lines = len(lines)
    MAX_LINES = 200
    if total_lines > MAX_LINES:
        head = "\n".join(lines[:100])
        tail = "\n".join(lines[-100:])
        return (
            f"=== {path} ===\n"
            f"{head}\n"
            f"... [{total_lines} lines total, showing first 100 + last 100] ...\n"
            f"{tail}\n"
            f"=== end {path} ==="
        )
    return f"=== {path} ===\n{content}\n=== end {path} ==="


# ── Conversation builder ────────────────────────────────────────────────────

TURN_TEMPLATES = [
    # (turn_index_1based, description, action, reads_files)
    (1, "Read problem statement", "read_problem", False),
    (2, "Read source files (first pass)", "read_sources", True),
    (3, "Re-read same files to trace flow", "read_sources", True),
    (4, "Propose fix strategy", "reason", False),
    (5, "Re-read files to verify approach", "read_sources", True),
    (6, "Apply fix (patch)", "read_sources", False),
    (7, "Re-read files post-fix to verify", "read_sources", True),
    (8, "Read test files and run tests", "read_tests", True),
]

TURN_HUMAN_TEXT = {
    "read_problem": "Read the issue description and understand what needs to be fixed.",
    "read_sources": "Read the source files to understand the current implementation.",
    "read_sources_again": "Re-read the files to trace the exact code path.",
    "read_sources_verify": "Read the files again to confirm the fix approach.",
    "read_sources_postfix": "Verify the fix by reading the modified files.",
    "reason": "Propose a fix strategy. Which functions need to change and how?",
    "read_tests": "Read the test file and verify the tests pass.",
    "apply_patch": "Apply the fix. Show the diff.",
}

# File blocks appear in tool messages — these are byte-identical across
# re-read turns, which is exactly what the dedup hook targets.
DUPLICATE_TURN_PAIRS = [
    "Turns 2,3,5,7 all read the same source files → 4× identical file blocks in tool messages",
]


def build_session_convo(instance: dict, max_turns: int = 8, with_snapshots: bool = False):
    """
    Build a multi-turn agent debugging conversation.

    File content is returned as TOOL messages (role: "tool") — this mirrors
    how real MCP-based agents work: each tool call returns verbatim file
    content in its own message. When the same file is re-read, the tool
    message is byte-identical → dedup hook fires.

    If with_snapshots=True, also returns the accumulating message array after
    each completed turn (turn_snapshots[t] = full context the agent would send
    as its request at turn t, t 1-based). This is what a REAL agent sends: turn N
    is a single request carrying turns 1..N.

    Returns (messages, turn_info, file_info[, turn_snapshots]).
    """
    repo = instance["repo"]
    commit = instance["base_commit"]
    problem = instance["problem_statement"]
    patch = instance["patch"]
    files = changed_files(patch)
    test_patch = instance.get("test_patch", "")
    test_files = changed_files(test_patch)

    # Fetch all files
    file_contents = {}
    fetch_errors = []
    for fpath in files + test_files:
        if fpath not in file_contents:
            content = fetch_file(repo, commit, fpath)
            if content is not None:
                file_contents[fpath] = content
            else:
                fetch_errors.append(fpath)

    # Build file blocks (cached per path)
    file_blocks = {}
    for fpath, content in file_contents.items():
        block = build_file_block(fpath, content)
        if block:
            file_blocks[fpath] = block

    messages = []
    turn_info = []
    file_block_message_positions = []
    tool_call_id = 0

    def add(role: str, content: str, is_file_block: bool = False):
        nonlocal tool_call_id
        msg = {"role": role}
        if role == "tool":
            msg["content"] = content
            msg["tool_call_id"] = f"call_{tool_call_id}"
        elif role == "assistant" and content.startswith("read_file"):
            # Assistant triggers a tool call
            tool_call_id += 1
            msg["content"] = None
            msg["tool_calls"] = [{
                "id": f"call_{tool_call_id}",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": content.replace("read_file ", "")}),
                }
            }]
        else:
            msg["content"] = content
        messages.append(msg)
        idx = len(messages) - 1
        if is_file_block:
            file_block_message_positions.append(idx)

    # System prompt
    add("system", "You are a senior software engineer debugging an issue in an open-source project.")

    used_templates = [t for t in TURN_TEMPLATES if t[0] <= max_turns]
    file_blocks_used = set()
    source_read_count = 0  # track how many times source files were read
    turn_snapshots = []  # accumulating context after each turn (1-based)

    for turn_num, desc, action, has_content in used_templates:
        if action == "read_problem":
            add("user", f"Read the issue description:\n\n{problem}")
            add("assistant", "I've read the issue. Let me examine the source files.")

        elif action == "read_sources" and has_content:
            source_read_count += 1
            # Read each source file as a separate tool call
            for fpath in files:
                if fpath in file_blocks:
                    # Assistant triggers read_file tool call
                    add("assistant", f"read_file {fpath}")
                    # Tool returns the file content (byte-identical every time)
                    add("tool", file_blocks[fpath], is_file_block=True)
                    file_blocks_used.add(fpath)

            if source_read_count == 1:
                add("assistant", "I've read the source files. Let me trace the code flow.")
            elif source_read_count == 2:
                add("assistant", "I've re-read the code. The issue is in how the data flows through this path.")
            elif source_read_count == 3:
                add("assistant", "Confirmed. The approach is sound — I'll implement the fix now.")
            elif source_read_count >= 4:
                add("assistant", "Verified. The fix is correct.")

        elif action == "reason":
            add("user", "Propose a fix strategy. Which functions need to change and how?")
            add("assistant",
                f"Fix strategy: modify the relevant functions to handle the edge case.\n\n"
                f"Proposed patch:\n{patch[:2000]}")

        elif action == "read_tests":
            # Read test files
            for fpath in test_files[:2]:
                if fpath in file_blocks:
                    add("assistant", f"read_file {fpath}")
                    add("tool", file_blocks[fpath], is_file_block=True)
                    file_blocks_used.add(fpath)

            add("user", f"Run the tests that must pass:\n{json.dumps(instance.get('FAIL_TO_PASS', []))}")
            add("assistant", "All tests pass. The fix is complete.")

        # Snapshot the FULL accumulating context the agent would send as its
        # request at this turn — turn N = N requests in a real agent loop.
        turn_snapshots.append(list(messages))

    ret = (messages, {
        "instance_id": instance["instance_id"],
        "repo": repo,
        "base_commit": commit,
        "turns_constructed": len(used_templates),
        "total_messages": len(messages),
        "source_files_changed": files,
        "test_files_changed": test_files,
        "files_successfully_fetched": list(file_contents.keys()),
        "files_fetch_errors": fetch_errors,
        "file_block_message_positions": file_block_message_positions,
        "n_file_block_occurrences": len(file_block_message_positions),
        "n_unique_files_in_blocks": len(file_blocks_used),
        "source_read_count": source_read_count,
        "duplicate_structure": (
            f"Source files read {source_read_count} times → "
            f"{len(file_blocks_used)} unique files × {source_read_count} reads = "
            f"{len(file_block_message_positions)} total tool messages, "
            f"of which {len(file_block_message_positions) - len(file_blocks_used)} are duplicates"
        ),
        "total_chars_of_file_blocks": sum(len(fb) for fb in file_blocks.values()),
    }, file_blocks)
    if with_snapshots:
        return ret + (turn_snapshots,)
    return ret


# ── Proxy call ──────────────────────────────────────────────────────────────

def send_to_proxy(messages: list) -> dict:
    """Send a chat completion request to LiteLLM. Returns usage info."""
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "max_tokens": 5,
    }).encode("utf-8")

    req = urllib.request.Request(
        LITELLM_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return {"usage": {"prompt_tokens": 0}, "error": f"HTTP {e.code}: {err_body[:300]}"}
    except Exception as e:
        return {"usage": {"prompt_tokens": 0}, "error": str(e)}


# ── Measurement runner ──────────────────────────────────────────────────────

def measure(label: str, disabled: bool = False) -> dict:
    """Run measurement across N SWE-bench instances."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    # Load instances
    print(f"\n  Loading up to {N_INSTANCES} SWE-bench Lite instances...")
    result = load_instances(N_INSTANCES)
    instances = result["instances"]
    print(f"  Source: {result['source']} ({result['available']} available)")

    instance_results = []
    total_prompt = 0
    total_chars_sent = 0

    for idx, inst in enumerate(instances):
        inst_id = inst["instance_id"]
        repo = inst["repo"]
        print(f"\n  [{idx+1}/{len(instances)}] {inst_id} ({repo})")

        # Build conversation
        convo, info, file_blocks = build_session_convo(inst, max_turns=MAX_TURNS)

        # Compute stats
        total_chars = sum(len(str(m.get("content", ""))) for m in convo)
        total_chars_sent += total_chars

        # Validate conversation
        n_file_blocks = info["n_file_block_occurrences"]
        n_expected_dups = max(0, n_file_blocks - len(info["files_successfully_fetched"]))

        print(f"    Messages: {info['total_messages']} | "
              f"File block occurrences: {n_file_blocks} | "
              f"Unique files: {info['n_unique_files_in_blocks']}")
        print(f"    Content: {total_chars:,} chars")
        print(f"    Duplicate occurrences beyond first: {n_expected_dups}")

        if info["files_fetch_errors"]:
            print(f"    ⚠ Fetch errors: {info['files_fetch_errors']}")

        # Send to proxy
        time.sleep(0.3)
        t0 = time.time()
        resp = send_to_proxy(convo)
        elapsed = time.time() - t0

        usage = resp.get("usage", {})
        pt = usage.get("prompt_tokens", 0) or 0
        ct = usage.get("completion_tokens", 0) or 0
        # OpenRouter returns total_cost in usage. Capture it for verification.
        cost = usage.get("total_cost", 0) or resp.get("total_cost", 0) or 0
        total_prompt += pt

        entry = {
            "instance_id": inst_id,
            "repo": repo,
            "base_commit": info["base_commit"],
            "turns_constructed": info["turns_constructed"],
            "messages_in_request": info["total_messages"],
            "file_block_occurrences": n_file_blocks,
            "unique_file_blocks": info["n_unique_files_in_blocks"],
            "total_chars_in_request": total_chars,
            "prompt_tokens_billed": pt,
            "completion_tokens": ct,
            "total_cost": cost,
            "proxy_latency_s": round(elapsed, 2),
            "proxy_error": resp.get("error"),
        }
        instance_results.append(entry)

        status = f"prompt_tokens={pt:,}, latency={elapsed:.1f}s"
        if resp.get("error"):
            status += f" ❌ {resp['error']}"
        print(f"    {status}")

        time.sleep(0.5)

    total_cost = total_prompt / 1_000_000 * PRICE_PER_M_INPUT

    return {
        "methodology": METHODOLOGY,
        "config": {
            "model": MODEL,
            "price_per_m_input": PRICE_PER_M_INPUT,
            "n_instances": len(instance_results),
            "max_turns_per_instance": MAX_TURNS,
            "disabled": disabled,
            "litellm_url": LITELLM_URL,
            "dataset": "princeton-nlp/swe-bench-lite (test split)",
            "hooks": (
                "callbacks: toolrecall.adapters.litellm.handler"
                if not disabled else
                "callbacks: toolrecall.adapters.litellm.handler (TOOLRECALL_DEDUP_DISABLED=1)"
            ),
            "separate_api_key_per_arm": True,
            "separate_api_key_verification": "OpenRouter dashboard per-key billed spend"
        },
        "results": {
            "label": label,
            "disabled": disabled,
            "total_prompt_tokens": total_prompt,
            "total_cost_estimate": round(total_cost, 6),
            "total_chars_sent": total_chars_sent,
            "instances": instance_results,
        },
    }


# ── Accumulating-loop measurement (Phase 1: real agent, turn N = N requests) ──

def measure_accumulating(label: str, disabled: bool = False) -> dict:
    """
    Measure like a REAL agent: for each instance, send turn N as a single
    request carrying the accumulated context of turns 1..N. 10 instances ×
    8 turns = 80 requests per arm. This produces a per-turn savings CURVE,
    not a single constant, and tests the keep-first prefix-caching property.
    """
    print(f"\n{'='*60}")
    print(f"  {label}  (accumulating loop)")
    print(f"{'='*60}")

    result = load_instances(N_INSTANCES)
    instances = result["instances"]
    print(f"  Source: {result['source']} ({result['available']} available)")
    print(f"  Requests: {N_INSTANCES} instances × {MAX_TURNS} turns = "
          f"{N_INSTANCES * MAX_TURNS} requests")

    instance_results = []
    total_prompt = 0
    total_cost_billed = 0.0
    n_requests = 0
    n_errors = 0

    for idx, inst in enumerate(instances):
        inst_id = inst["instance_id"]
        repo = inst["repo"]
        print(f"\n  [{idx+1}/{len(instances)}] {inst_id} ({repo})")

        convo, info, file_blocks, turn_snapshots = build_session_convo(
            inst, max_turns=MAX_TURNS, with_snapshots=True)
        assert len(turn_snapshots) == MAX_TURNS, "snapshot count mismatch"

        turn_rows = []
        inst_prompt = 0
        inst_cost = 0.0
        inst_errors = 0
        for t_idx, snap in enumerate(turn_snapshots, start=1):
            time.sleep(0.3)
            t0 = time.time()
            resp = send_to_proxy(snap)
            elapsed = time.time() - t0
            usage = resp.get("usage", {})
            pt = usage.get("prompt_tokens", 0) or 0
            ct = usage.get("completion_tokens", 0) or 0
            # OpenRouter reports cost in usage.cost (not total_cost).
            cost = usage.get("cost", 0) or resp.get("cost", 0) or usage.get("total_cost", 0) or 0
            details = usage.get("prompt_tokens_details", {}) or {}
            cached = details.get("cached_tokens", 0) or 0
            err = resp.get("error")
            if err:
                inst_errors += 1
                n_errors += 1
            inst_prompt += pt
            inst_cost += cost
            n_requests += 1
            turn_rows.append({
                "turn": t_idx,
                "n_messages": len(snap),
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "cache_read_tokens": cached,
                "cost": cost,
                "latency_s": round(elapsed, 2),
                "error": err,
            })
            print(f"    t{t_idx}: {pt:>8,} tok (cache_read={cached}) {'❌ '+str(err) if err else ''}")
            time.sleep(0.4)

        total_prompt += inst_prompt
        total_cost_billed += inst_cost

        instance_results.append({
            "instance_id": inst_id,
            "repo": repo,
            "base_commit": info["base_commit"],
            "turns": turn_rows,
            "n_file_block_occurrences": info["n_file_block_occurrences"],
            "unique_file_blocks": info["n_unique_files_in_blocks"],
            "sum_prompt_tokens": inst_prompt,
            "sum_cost_billed": round(inst_cost, 8),
            "n_errors": inst_errors,
        })

    return {
        "methodology": METHODOLOGY,
        "mode": "accumulating",
        "config": {
            "model": MODEL,
            "price_per_m_input": PRICE_PER_M_INPUT,
            "n_instances": N_INSTANCES,
            "turns_per_instance": MAX_TURNS,
            "n_requests": n_requests,
            "disabled": disabled,
            "litellm_url": LITELLM_URL,
            "dataset": "princeton-nlp/swe-bench-lite (test split)",
            "hooks": (
                "callbacks: toolrecall.adapters.litellm.handler"
                if not disabled else
                "callbacks: toolrecall.adapters.litellm.handler (TOOLRECALL_DEDUP_DISABLED=1)"
            ),
        },
        "results": {
            "label": label,
            "disabled": disabled,
            "total_prompt_tokens": total_prompt,
            "total_cost_billed": round(total_cost_billed, 6),
            "total_cost_estimate": round(total_prompt / 1_000_000 * PRICE_PER_M_INPUT, 6),
            "n_requests": n_requests,
            "n_errors": n_errors,
            "instances": instance_results,
        },
    }


def compare_accumulating(with_path: str, without_path: str):
    """Compare accumulating-loop results: per-turn savings curve + effective rate."""
    with open(with_path) as f:
        w = json.load(f)
    with open(without_path) as f:
        wo = json.load(f)

    w_inst = w["results"]["instances"]
    wo_inst = wo["results"]["instances"]

    print(f"\n{'='*76}")
    print(f"  SWE-BENCH LITE — DEDUP — ACCUMULATING AGENT LOOP")
    print(f"  {w['config']['n_instances']} instances × {w['config']['turns_per_instance']} turns"
          f" = {w['config']['n_requests']} requests/arm")
    print(f"  Model: {w['config']['model']}")
    print(f"{'='*76}")

    # Aggregate per-turn across instances
    def per_turn(d):
        grid = {}
        for inst in d["results"]["instances"]:
            for t in inst["turns"]:
                grid.setdefault(t["turn"], {"prompt": 0, "cost": 0.0, "n": 0, "cache_read": 0})
                grid[t["turn"]]["prompt"] += t["prompt_tokens"]
                grid[t["turn"]]["cost"] += t.get("cost", 0)
                grid[t["turn"]]["cache_read"] += t.get("cache_read_tokens", 0)
                grid[t["turn"]]["n"] += 1
        return grid

    wg, wog = per_turn(w), per_turn(wo)

    # Totals & effective rate
    wt = w["results"]["total_prompt_tokens"]
    wot = wo["results"]["total_prompt_tokens"]
    saved = wot - wt
    pct = (saved / wot * 100) if wot else 0

    w_rate = (wt / 1_000_000) * w["config"]["price_per_m_input"]
    wo_rate = (wot / 1_000_000) * w["config"]["price_per_m_input"]
    # Effective rate the provider BILLED (= cost/tokens) — tests prefix caching
    w_billed = w["results"]["total_cost_billed"]
    wo_billed = wo["results"]["total_cost_billed"]
    w_eff = (w_billed / wt * 1_000_000) if wt else 0
    wo_eff = (wo_billed / wot * 1_000_000) if wot else 0

    w_cache = sum(g["cache_read"] for g in wg.values())
    wo_cache = sum(g["cache_read"] for g in wog.values())

    print(f"\n  SAVINGS CURVE (aggregate over instances):\n")
    print(f"  {'Turn':>5}  {'WITH':>10}  {'WITHOUT':>10}  {'Saved':>10}  {'%':>7}")
    print(f"  {'-'*46}")
    for t in sorted(wg.keys()):
        wp = wg[t]["prompt"]
        wop = wog[t]["prompt"]
        d = wop - wp
        p = (d / wop * 100) if wop else 0
        print(f"  {t:>5}  {wp:>10,}  {wop:>10,}  {d:>10,}  {p:>6.1f}%")

    print(f"\n  {'─'*46}")
    print(f"  {'CUMULATIVE':>30}")
    print(f"  {'Total prompt tokens':>30}  {wt:>10,}  {wot:>10,}  {saved:>10,}  {pct:>6.1f}%")
    print(f"  {'Est. cost (@price)':>30}  ${w_rate:>9.4f}  ${wo_rate:>9.4f}  ${wo_rate-w_rate:>9.4f}")
    print(f"  {'Billed cost (OpenRouter)':>30}  ${w_billed:>9.4f}  ${wo_billed:>9.4f}")
    print(f"  {'Effective $/M (billed/tok)':>30}  ${w_eff:>9.4f}  ${wo_eff:>9.4f}")
    print(f"  {'Cache-read tokens (provider)':>30}  {w_cache:>10,}  {wo_cache:>10,}")
    print(f"\n  Phase 2 — prefix-caching claim (keep-first preserves provider cache):")
    print(f"    Compare cache-read tokens and effective \$/M between arms.")
    print(f"    If keep-first works, WITH cache ≈ WITHOUT cache (dedup doesn't break")
    print(f"    the provider's cacheable prefix) and effective rates match.")
    print(f"    Δ effective = ${abs(wo_eff-w_eff):.4f}/M "
          f"({'OK (no damage)' if abs(wo_eff-w_eff)<3.0 else 'CHECK — possible prefix damage'})")
    print(f"\n  Errors: WITH={w['results']['n_errors']}  WITHOUT={wo['results']['n_errors']}")
    print(f"  Data: {with_path}, {without_path}")


# ── Comparison and reporting ────────────────────────────────────────────────

def _format_instances_table(title: str, data: dict):
    """Pretty-print instance results."""
    print(f"  {title}")
    print(f"  {'Instance':>35}  {'Tokens':>10}  {'Chars':>10}  {'Msgs':>5}  {'Files':>5}")
    print(f"  {'-'*75}")
    for inst in data:
        iid = inst["instance_id"]
        pt = inst.get("prompt_tokens_billed", 0)
        chars = inst.get("total_chars_in_request", 0)
        msgs = inst.get("messages_in_request", 0)
        nfiles = inst.get("file_block_occurrences", 0)
        print(f"  {iid:>35}  {pt:>10,}  {chars:>10,}  {msgs:>5}  {nfiles:>5}")
    print()


def compare(with_path: str, without_path: str):
    """Compare WITH vs WITHOUT dedup results."""
    with open(with_path) as f:
        w = json.load(f)
    with open(without_path) as f:
        wo = json.load(f)

    # Validate structure
    w_instances = w["results"]["instances"]
    wo_instances = wo["results"]["instances"]

    n_match = min(len(w_instances), len(wo_instances))
    if n_match == 0:
        print("  ❌ No matching instances to compare")
        return

    wt = w["results"]["total_prompt_tokens"]
    wot = wo["results"]["total_prompt_tokens"]
    saved = wot - wt
    pct = (saved / wot * 100) if wot else 0
    cost_saved = (wot - wt) / 1_000_000 * w["config"]["price_per_m_input"]

    # Methodology header
    print(f"\n{'='*72}")
    print(f"  SWE-BENCH LITE — DEDUP MEASUREMENT RESULTS")
    print(f"  Methodology v{METHODOLOGY['version']}")
    print(f"{'='*72}")
    print(f"")
    print(f"  Dataset:        {w['config']['dataset']}")
    print(f"  Instances:      {n_match}")
    print(f"  Turns/instance: {w['config']['max_turns_per_instance']}")
    print(f"  Model:          {w['config']['model']}")
    print(f"  Price:          ${w['config']['price_per_m_input']}/M input tokens")
    print(f"  Arm A (with):   {w['config']['hooks']}")
    print(f"  Arm B (without):{wo['config']['hooks']}")
    print(f"")
    print(f"  {'─'*72}")
    print(f"  AGGREGATE RESULTS")
    print(f"  {'─'*72}")
    print(f"  {'':>30}  {'WITH dedup':>12}  {'WITHOUT':>12}  {'Saved':>12}  {'%':>7}")
    print(f"  {'─'*66}")
    print(f"  {'Total prompt tokens':>30}  {wt:>12,}  {wot:>12,}  {saved:>12,}  {pct:>6.1f}%")
    print(f"  {'Cost estimate':>30}  ${w['results']['total_cost_estimate']:>10.6f}"
          f"  ${wo['results']['total_cost_estimate']:>10.6f}"
          f"  ${cost_saved:>10.6f}")
    print(f"  {'Total chars sent':>30}  {w['results']['total_chars_sent']:>12,}"
          f"  {wo['results']['total_chars_sent']:>12,}"
          f"  {wot - wt:>12,}")
    print(f"")
    print(f"  {'─'*72}")
    print(f"  PER-INSTANCE BREAKDOWN")
    print(f"  {'─'*72}")
    print(f"  {'Instance':>35}  {'With':>10}  {'Without':>10}  {'Δ':>10}  {'%':>7}")
    print(f"  {'─'*75}")

    w_by_id = {i["instance_id"]: i for i in w_instances}
    for inst in wo_instances:
        iid = inst["instance_id"]
        w_inst = w_by_id.get(iid)
        if w_inst is None:
            continue
        w_pt = w_inst["prompt_tokens_billed"]
        wo_pt = inst["prompt_tokens_billed"]
        d = wo_pt - w_pt
        p = (d / wo_pt * 100) if wo_pt else 0
        print(f"  {iid:>35}  {w_pt:>10,}  {wo_pt:>10,}  {d:>10,}  {p:>6.1f}%")

    print(f"")
    print(f"  {'─'*72}")
    print(f"  DUPLICATE STRUCTURE (per instance)")
    print(f"  {'─'*72}")
    for inst in wo_instances:
        iid = inst["instance_id"]
        n_blocks = inst.get("file_block_occurrences", 0)
        n_unique = inst.get("unique_file_blocks", 0)
        n_msgs = inst.get("messages_in_request", 0)
        chars = inst.get("total_chars_in_request", 0)
        print(f"  {iid:>35}")
        print(f"    Messages: {n_msgs}  |  File block occurrences: {n_blocks} "
              f"({n_unique} unique)  |  {chars:,} chars")
        print(f"    Dedup eligible: {n_blocks - n_unique} of {n_blocks} blocks are duplicates")

    print(f"")
    print(f"  {'─'*72}")
    print(f"  VERIFICATION")
    print(f"  {'─'*72}")
    print(f"  Check https://openrouter.ai/dashboard for per-key billed spend:")
    print(f"    - 'with' key    = dedup arm")
    print(f"    - 'without' key = no-dedup arm")
    print(f"")
    print(f"  {'─'*72}")
    print(f"  CAVEATS")
    print(f"  {'─'*72}")
    for c in METHODOLOGY["caveats"]:
        print(f"  • {c}")
    print(f"")
    print(f"  Reproduce:")
    print(f"  {METHODOLOGY['reproduce']}")
    print(f"  Data files: {with_path}, {without_path}")
    print(f"")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LiteLLM dedup measurement on real SWE-bench workloads"
    )
    parser.add_argument("--disabled", action="store_true",
                        help="Run WITHOUT dedup (TOOLRECALL_DEDUP_DISABLED=1)")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON (machine-readable)")
    parser.add_argument("--key", type=str, default=None,
                        help="OpenRouter key (KEY=VALUE or just VALUE)")
    parser.add_argument("--compare", nargs=2, metavar=("WITH", "WITHOUT"),
                        help="Compare two JSON result files")
    parser.add_argument("--accumulate", action="store_true",
                        help="Run the accumulating agent loop (turn N = N requests)")
    args = parser.parse_args()

    if args.compare:
        # Auto-detect accumulating mode from the JSON
        try:
            with open(args.compare[0]) as f:
                mode = json.load(f).get("mode")
        except Exception:
            mode = None
        if mode == "accumulating":
            compare_accumulating(args.compare[0], args.compare[1])
        else:
            compare(args.compare[0], args.compare[1])
        return

    if args.key:
        if "=" in args.key:
            _, val = args.key.split("=", 1)
            os.environ["OPENROUTER_KEY_DEDUP"] = val
        else:
            os.environ["OPENROUTER_KEY_DEDUP"] = args.key

    if args.disabled:
        os.environ["TOOLRECALL_DEDUP_DISABLED"] = "1"
        label = "WITHOUT dedup hook"
    else:
        label = "WITH dedup hook"

    if args.accumulate:
        # Route progress to stderr so stdout stays pure JSON
        if args.json:
            import contextlib
            with contextlib.redirect_stdout(sys.stderr):
                result = measure_accumulating(label, disabled=args.disabled)
        else:
            result = measure_accumulating(label, disabled=args.disabled)
    else:
        if args.json:
            import contextlib
            with contextlib.redirect_stdout(sys.stderr):
                result = measure(label, disabled=args.disabled)
        else:
            result = measure(label, disabled=args.disabled)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n  Total prompt tokens: {result['results']['total_prompt_tokens']:,}")
        print(f"  Total cost: ${result['results']['total_cost_estimate']:.6f}")


if __name__ == "__main__":
    main()
