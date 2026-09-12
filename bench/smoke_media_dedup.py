"""smoke_media_dedup.py — offline gate-eval smoke test for image-part dedup.

Simulates a browser-agent session (the Stage-3 workload shape) WITHOUT any
API calls: an 8-step session over 2 pages; the screenshot tool returns
byte-identical images while the page is unchanged. Each turn's request
carries the full accumulated history (what a real agent sends), so identical
screenshots re-appear — and would be re-billed — every step.

Arms (same messages, only the hook differs):
  OFF  — TOOLRECALL_DEDUP_MEDIA unset (current default)
  ON   — media dedup active

Metrics: per-request and cumulative character cost, stub count, and the
invariants that must hold before any LIVE gate eval is attempted:
  I1  media OFF ⇒ zero modification (bit-identical passthrough)
  I2  distinct pages' images are never stubbed
  I3  unchanged-page repeats stub down to a short reference
  I4  protected tail (protect_last) always keeps full copies
  I5  corrupt/https image refs pass through untouched

This is a MECHANISM smoke test — characters, not dollars. No savings claim
comes out of this file; billed numbers only from the Stage-3 live harness.

Usage:
    .venv/bin/python bench/smoke_media_dedup.py
Exit 0 = all invariants hold. Writes bench/smoke-media-last.json.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH_DIR)
sys.path.insert(0, REPO)

from toolrecall.adapters.litellm import dedup_messages  # noqa: E402

# ── Synthetic workload ────────────────────────────────────────────────
# Distinct 9 KB "screenshots" per page (deterministic bytes), re-sent
# byte-identical while the page is unchanged. base64 inflates ~4/3.

PAGES = ["dashboard", "checkout"]  # steps 1-4 page A, 5-8 page B
STEPS_PER_PAGE = 4
TEXT_OBS = "Page {page}: nav OK, 47 elements visible, cursor at ({x},{y})."


def _page_image(page: str) -> bytes:
    h = hashlib.sha256(f"tr-smoke-{page}".encode()).digest()
    return b"\x89PNG\r\n\x1a\n" + h * 288  # ~9.2 KB distinct per page


def _data_url(page: str) -> str:
    return "data:image/png;base64," + base64.b64encode(_page_image(page)).decode()


def build_session() -> list:
    """One turn per step; tool message = text observation + screenshot."""
    msgs = [{"role": "system", "content": "You are a browser agent."}]
    for i, page in enumerate(PAGES):
        for step in range(STEPS_PER_PAGE):
            msgs.append(
                {
                    "role": "tool",
                    "content": [
                        {"type": "text", "text": TEXT_OBS.format(page=page, x=10 + step, y=20 + i)},
                        {
                            "type": "image_url",
                            "image_url": {"url": _data_url(page), "detail": "high"},
                        },
                    ],
                }
            )
    return msgs


def request_view(msgs: list, upto: int) -> list:
    """What the agent sends at turn `upto` (1-based): turns 1..upto."""
    return msgs[: 1 + upto]


def cost(messages: list) -> int:
    """Total characters across all content parts (proxy for billed tokens)."""
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    n += len(part.get("text", ""))
                elif part.get("type") == "image_url":
                    n += len(part["image_url"]["url"])
    return n


def run_arm(media: bool) -> dict:
    session = build_session()
    total = 0
    stubs = 0
    per_turn = []
    for t in range(1, 1 + len(PAGES) * STEPS_PER_PAGE):
        view = request_view(session, t)
        out, stats = dedup_messages(
            [json.loads(json.dumps(m)) for m in view],
            min_chars=800,
            protect_last=2,
            media=media,
        )
        total += cost(out)
        stubs += stats["blocks"]
        per_turn.append({"turn": t, "chars": cost(out), "stubs": stats["blocks"]})
    return {"total_chars": total, "stubs": stubs, "per_turn": per_turn}


def invariants() -> dict:
    """I1-I5 must all hold before the live gate eval is attempted."""
    session = build_session()
    res = {}

    # I1: media OFF ⇒ bit-identical passthrough
    out_off, stats_off = dedup_messages(
        json.loads(json.dumps(session)), min_chars=800, protect_last=2, media=False
    )
    res["I1_media_off_passthrough"] = stats_off["blocks"] == 0 and cost(out_off) == cost(session)

    # I2: distinct images never stubbed (2 distinct pages → 2 survivors)
    out_on, _ = dedup_messages(
        json.loads(json.dumps(session)), min_chars=800, protect_last=0, media=True
    )
    kept = sum(
        1
        for m in out_on
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:")
    )
    res["I2_distinct_kept"] = kept == len(PAGES)

    # I3: repeats stub to short references
    stubbed = [
        p.get("text", "")
        for m in out_on
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p.get("type") == "text" and p.get("text", "").startswith("[toolrecall-dedup]")
    ]
    res["I3_repeats_stubbed"] = len(stubbed) > 0 and all(len(s) < 200 for s in stubbed)

    # I4: protected tail keeps full copies (last 2 messages unstubbed).
    # Must use protect_last=2 — out_on above was computed with 0.
    out_prot, _ = dedup_messages(
        json.loads(json.dumps(session)), min_chars=800, protect_last=2, media=True
    )
    tail = out_prot[-2:]
    tail_full = all(
        p["image_url"]["url"].startswith("data:")
        for m in tail
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p.get("type") == "image_url"
    )
    res["I4_tail_protected"] = tail_full

    # I5: https + corrupt refs pass through untouched
    weird = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}],
        },
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!"}}],
        },
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}],
        },
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!"}}],
        },
    ]
    out_w, stats_w = dedup_messages(weird, min_chars=1, protect_last=0, media=True)
    res["I5_weird_passthrough"] = (
        stats_w["blocks"] == 0
        and out_w[0]["content"][0]["image_url"]["url"] == "https://x/y.png"
        and out_w[3]["content"][0]["image_url"]["url"] == "data:image/png;base64,!!!"
    )
    return res


def main() -> int:
    off = run_arm(media=False)
    on = run_arm(media=True)
    checks = invariants()

    delta = off["total_chars"] - on["total_chars"]
    pct = (delta / off["total_chars"] * 100) if off["total_chars"] else 0.0

    report = {
        "workload": "8-step browser-agent session, 2 pages, byte-identical re-screenshots",
        "arm_off": {"total_chars": off["total_chars"], "stubs": off["stubs"]},
        "arm_on": {"total_chars": on["total_chars"], "stubs": on["stubs"]},
        "chars_saved": delta,
        "chars_saved_pct": round(pct, 1),
        "invariants": checks,
        "note": "MECHANISM smoke only — characters, not dollars. No savings claim.",
    }
    print(json.dumps(report, indent=2))
    ok = all(checks.values())
    with open(os.path.join(BENCH_DIR, "smoke-media-last.json"), "w") as f:
        json.dump(report, f, indent=2)
    print("SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
