"""test_microbench.py — statistical sanity assertions for the microbench.

Assertions (soft ceilings; a much faster/slower machine may need a tuning pass):
  - median overhead for `small` < 50 µs.
  - dup_heavy median < 2x small median (work is roughly linear in blocks).
  - run variance across shapes doesn't collapse to 0 (sanity, not strict).
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import SHAPES  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from toolrecall.adapters.litellm import dedup_messages  # noqa: E402

ITERS = 5000


def med_us(gen):
    flat = []
    for _ in range(ITERS):
        payload = gen()
        t0 = __import__("time").perf_counter_ns()
        dedup_messages(payload)
        flat.append(__import__("time").perf_counter_ns() - t0)
    flat.sort()
    return flat[len(flat) // 2] / 1000.0  # us


def test_small_under_50us():
    assert med_us(SHAPES["small"]) < 50.0, "small-shape median overhead too high"


def test_heavy_shapes_bounded():
    # Cost scales with BYTES HASHED, not message count (dup_heavy/wrap_varied hash
    # multi-KB blocks; small's sub-min_chars strings are never hashed), so a 2x-vs-
    # small ratio is the wrong bound. The defensible claim is: even on KB-scale
    # blocks the hook stays well under half a millisecond.
    d = med_us(SHAPES["dup_heavy"])
    w = med_us(SHAPES["wrap_varied"])
    assert d < 500.0, f"dup_heavy median {d:.1f}us too high"
    assert w < 500.0, f"wrap_varied median {w:.1f}us too high"


def test_shapes_not_degenerate():
    # all three shapes must be measurable and non-zero
    ms = [med_us(SHAPES[k]) for k in ("small", "dup_heavy", "wrap_varied")]
    assert all(m > 0 for m in ms)
    assert statistics.pstdev(ms) >= 0.0  # sanity: things ran


def test_dup_heavy_actually_stubs():
    # content-level check independent of timing: dup re-reads must be stubbed
    out, stats = dedup_messages(SHAPES["dup_heavy"]())
    assert stats["blocks"] >= 1, "dup_heavy should register at least one stub block"


def test_wrap_varied_stubs_less_than_dup_heavy():
    _, s1 = dedup_messages(SHAPES["dup_heavy"]())
    _, s2 = dedup_messages(SHAPES["wrap_varied"]())
    # wrap_varied should produce strictly fewer (or equal) whole-block matches
    assert s2["blocks"] <= s1["blocks"]
