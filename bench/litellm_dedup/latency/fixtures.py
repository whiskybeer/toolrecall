"""fixtures.py — request-shape generators for the dedup_messages microbench.

Mirrors the bimodal real profile: whole-block matches are harness-formatting
dependent. Shapes:
  - small(n_msgs):      2-6 messages, no duplicate blocks (baseline, near-zero work).
  - dup_heavy(...):     30-80% of messages are byte-identical re-reads (the target).
  - wrap_varied(...):   same file bytes re-wrapped with different prefixes per
                        message (line numbers / paths) -> produces FEWER whole-block
                        matches, capturing the harness-mismatch reality.
"""

from __future__ import annotations

import random

# matches the toolrecall default dedup min length; blocks below this never stub
MIN_CHARS = 800


def _file_block(n_chars: int, seed: int) -> str:
    """Deterministic file-like text of ~n_chars, newline-delimited so re-wrapping
    at different line-number prefixes is possible."""
    rng = random.Random(seed)
    lines = []
    while sum(len(line) for line in lines) < n_chars:
        n = rng.randint(20, 90)
        lines.append("".join(rng.choice("abcdefghijklmnopqrstuvwxyz _-(),.") for _ in range(n)))
    return "\n".join(lines) + "\n"


def small(n_msgs: int = 6) -> list[dict]:
    """2-6 short, unique messages — below MIN_CHARS, nothing dedup-eligible."""
    n_msgs = max(2, min(6, n_msgs))
    out = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(n_msgs):
        # short and unique -> never reaches min_chars
        out.append({"role": "user", "content": f"question number {i} about xyz"})
    out.append({"role": "assistant", "content": "short answer"})
    return out


def dup_heavy(n_msgs: int = 8, block_chars: int = 3000) -> list[dict]:
    """A few unique large blocks, then N-2 messages re-reading the SAME bytes
    (byte-identical -> whole-block match -> stub eligible)."""
    roles = ("user", "assistant")
    src = _file_block(block_chars, seed=1)
    out = [{"role": "system", "content": "You are a coding agent. Debug the bug."}]
    # first pass: unique fix-strategy turns (short), plus one long read
    for i in range(max(1, n_msgs // 3)):
        out.append({"role": roles[i % 2], "content": f"Strategy step {i}: inspect trace."})
    out.append({"role": "user", "content": src})  # first occurrence (registered)
    while len(out) < n_msgs:
        out.append({"role": roles[len(out) % 2], "content": src})  # re-read -> dup
    return out


def wrap_varied(n_msgs: int = 8, block_chars: int = 3000) -> list[dict]:
    """Same file bytes re-wrapped per message with a different prefix (line
    numbers / path). Whole block differs -> fewer matches — the CTO's point."""
    roles = ("user", "assistant")
    src = _file_block(block_chars, seed=2)
    out = [{"role": "system", "content": "You are a coding agent."}]
    lines = src.split("\n")
    for i in range(n_msgs):
        n = len(lines)
        prefix = f"=== path/to/file_{i % 3}.py (lines {i * n}-{(i + 1) * n}) ===\n" + "\n".join(
            f"{ln:5d}  {t}" for ln, t in enumerate(lines)
        )
        out.append({"role": roles[i % 2], "content": prefix})
    return out


# registry for the microbench driver
SHAPES = {
    "small": small,
    "dup_heavy": dup_heavy,
    "wrap_varied": wrap_varied,
}
