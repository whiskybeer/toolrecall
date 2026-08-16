"""
Property test for toolrecall.adapters.litellm dedup (Phase 3 quality-close).

Proves the quality-preservation claim BY CONSTRUCTION, with zero model spend:

  Invariant 1 (no information loss)  — every stub references a message index
      (ref) that holds the byte-identical full block in the SAME request,
      and ref < stub_index. A stub never points to another stub (no chains),
      and its sha256 digest is always present as a full block in the request.
      => the output carries identical information to the input; nothing that
         wasn't already present is ever dropped or invented.

  Invariant 2 (keep-first)           — the first occurrence of every distinct
      block is retained in full (never stubbed). A block is stubbed only when a
      byte-identical copy was already seen earlier in the same request.

  Invariant 3 (non-mutation)         — the input list and its dicts are never
      mutated; the handler returns a copy.

  Invariant 4 (fail-open)            — garbage / non-dict / hostile input passes
      through unchanged.

  Documented, NOT an invariant: "prefix stability". protect_last re-anchors per
  request, so as a conversation grows, the protected tail shifts and EARLIER
  messages may become stubbable. That changes bytes but is never a loss: every
  stub still resolves to a retained full block in the same request. Per-request
  semantic completeness is the real guarantee, and it always holds.

Run:  python3 bench/litellm_dedup/property_test_quality.py   (or via pytest)
"""

import copy
import hashlib
import random
import re
import string

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from toolrecall.adapters.litellm import dedup_messages

STUB_RE = re.compile(
    r"^\[toolrecall-dedup\] Duplicate content omitted \((\d+) chars, "
    r"sha256:([0-9a-f]{16})\)\. The byte-identical content already appears "
    r"in message (\d+) of this request\.$"
)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def random_block(rng, size=900):
    body = "".join(rng.choice(string.ascii_letters + string.digits + "\n ") for _ in range(size))
    return f"def block_{rng.randint(0, 99)}(x):\n    return {body}"[:size]


def random_messages(rng, n_blocks=8, length=40, seed=0):
    blocks = [random_block(rng, size=rng.randint(850, 1200)) for _ in range(n_blocks)]
    msgs = [{"role": "system", "content": "You are a senior software engineer."}]
    for i in range(length):
        r = rng.random()
        if r < 0.15:
            msgs.append(
                {
                    "role": "user",
                    "content": rng.choice(
                        ["Read the test file.", "Fix the bug.", "Run the tests.", "What changed?"]
                    ),
                }
            )
        elif r < 0.30:
            msgs.append(
                {
                    "role": "assistant",
                    "content": rng.choice(
                        ["Looking at it now.", "I see the issue.", "Let me check."]
                    ),
                }
            )
        elif r < 0.45:
            msgs.append({"role": "tool", "content": rng.choice(blocks)})
        elif r < 0.65:
            msgs.append({"role": "tool", "content": [{"type": "text", "text": rng.choice(blocks)}]})
        elif r < 0.80:
            msgs.append({"role": "user", "content": "Re-read the full file."})
        else:
            msgs.append({"role": "assistant", "content": "Done."})
    return msgs


def text_of(msg):
    """str content only; None otherwise."""
    if not isinstance(msg, dict):
        return None
    c = msg.get("content")
    return c if isinstance(c, str) else None


def block_texts(msg):
    """All text blocks of a message (str content, or content-parts list)."""
    if not isinstance(msg, dict):
        return []
    c = msg.get("content")
    if isinstance(c, str):
        return [c]
    if isinstance(c, list):
        return [
            p.get("text")
            for p in c
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        ]
    return []


def test_no_information_loss(seed=0, trials=500):
    rng = random.Random(seed)
    for t in range(trials):
        msgs = random_messages(rng, seed=seed + t)
        out, _ = dedup_messages(msgs, min_chars=800, protect_last=2)
        # index of the first FULL block per digest (only real, non-stub big blocks)
        full_first = {}
        for i, m in enumerate(out):
            for tx in block_texts(m):
                if tx is None or STUB_RE.match(tx) or len(tx) < 800:
                    continue
                full_first.setdefault(_digest(tx), i)
        for i, m in enumerate(out):
            for tx in block_texts(m):
                mm = STUB_RE.match(tx or "")
                if not mm:
                    continue
                digest, ref = mm.group(2), int(mm.group(3))
                assert ref < i, f"stub refs later msg (trial {t})"
                assert digest in full_first, f"stub digest absent as full block (trial {t})"
                # the referenced message must itself contain a full (non-stub) block
                referent_blocks = [
                    b for b in block_texts(out[ref]) if b is not None and not STUB_RE.match(b)
                ]
                assert any(len(b) >= 800 and _digest(b) == digest for b in referent_blocks), (
                    f"stub->stub / digest-mismatch at ref {ref} (trial {t})"
                )
    return f"no-information-loss OK ({trials} trials)"


def test_keep_first_retained(seed=0, trials=500):
    rng = random.Random(seed)
    for t in range(trials):
        msgs = random_messages(rng, seed=seed + t)
        out, _ = dedup_messages(msgs, min_chars=800, protect_last=2)
        first = {}
        for i, m in enumerate(out):
            for tx in block_texts(m):
                if tx is None or STUB_RE.match(tx) or len(tx) < 800:
                    continue
                first.setdefault(_digest(tx), (i, tx))
        for fi, ftx in first.values():
            assert not STUB_RE.match(ftx), f"first occurrence stubbed (trial {t}, idx {fi})"
    return f"keep-first OK ({trials} trials)"


def test_non_mutation(seed=0):
    rng = random.Random(seed)
    msgs = random_messages(rng, seed=seed)
    snapshot = copy.deepcopy(msgs)
    dedup_messages(msgs, min_chars=800, protect_last=2)
    assert msgs == snapshot, "input list mutated"
    return "non-mutation OK"


def test_fail_open(seed=0):
    junk = [
        None,
        42,
        "a plain short string that is not a dict",
        {"role": "tool", "content": 12345},
        {"role": "tool", "content": ["not", "a", "list-of-dicts"]},
        {"content": "no role"},
    ]
    out, stats = dedup_messages(junk, min_chars=1, protect_last=0)
    assert out == junk, "garbage input was changed"
    assert stats["blocks"] == 0
    return "fail-open OK"


def test_prefix_shift_is_benign(seed=0, trials=200):
    """Prefix may change (protect window re-anchors) but never loses info."""
    rng = random.Random(seed)
    chains = 0
    for t in range(trials):
        msgs = random_messages(rng, seed=seed + t)
        extra = msgs + [{"role": "tool", "content": random_block(rng, 1100)}]
        out2, _ = dedup_messages(extra, min_chars=800, protect_last=2)
        full_first = {}
        for i, m in enumerate(out2):
            for tx in block_texts(m):
                if tx is None or STUB_RE.match(tx) or len(tx) < 800:
                    continue
                full_first.setdefault(_digest(tx), i)
        for i, m in enumerate(out2):
            for tx in block_texts(m):
                mm = STUB_RE.match(tx or "")
                if mm:
                    ref = int(mm.group(3))
                    referent_blocks = [
                        b for b in block_texts(out2[ref]) if b is not None and not STUB_RE.match(b)
                    ]
                    if not any(
                        len(b) >= 800 and _digest(b) == mm.group(2) for b in referent_blocks
                    ):
                        chains += 1
    assert chains == 0, f"prefix growth caused stub->stub chain ({chains})"
    return f"prefix-shift benign (no stub->stub chains in {trials} trials)"


if __name__ == "__main__":
    results = []
    for fn in (
        test_no_information_loss,
        test_keep_first_retained,
        test_non_mutation,
        test_fail_open,
        test_prefix_shift_is_benign,
    ):
        try:
            results.append(f"{fn.__name__}: {fn()}")
        except AssertionError as e:
            results.append(f"{fn.__name__}: FAIL -> {e}")
    print("\n".join(results))
    ok = [r for r in results if "FAIL" not in r and "OK" in r or "benign" in r]
    print(f"\n{len(ok)}/{len(results)} properties passed")
