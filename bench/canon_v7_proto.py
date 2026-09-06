#!/usr/bin/env python3
"""Warp canonicalization v7 — structural walk, no raw-text bracket scanning.

v6 post-mortem: scanning for '[' on the RAW body text matches ENVELOPE-level
arrays (content:[{...}]) whose spans don't correspond to the embedded skills
array. Parsing an envelope-level block as 'stringified JSON' fails with
'Extra data'. Correct approach: parse the envelope ONCE, walk messages
structurally, and canonicalize each string field's INNER text (where the
embedded JSON blobs live, already one escape level decoded).

Canonical transform per string field (inner text):
  1. UUIDs → placeholder
  2. ISO-8601 timestamps → placeholder
  3. '# Conversation context …' section (Warp session state) → removed
  4. directory_state / shell / operating_system JSON objects → removed
  5. Embedded JSON array-of-{name} objects (skills list, balance-scanned on
     inner text) → fixed placeholder token
Then the body is re-serialized with json.dumps(sort_keys=True) so field
order can't affect the hash either.
"""

import glob
import hashlib
import json
import re

_UUID_SUB = "00000000-0000-4000-8000-canonuuidplaceholder"
_TS_SUB = "1970-01-01T00:00:00Z"

_RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_RE_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\b")


def _strip_ctx_section(text: str) -> str:
    return re.sub(r"# Conversation context.*?(?=\n# )", "", text, flags=re.S)


def _strip_env_objects(text: str) -> str:
    for key in ("directory_state", "shell", "operating_system"):
        text = re.sub(r'"%s"\s*:\s*\{[^{}]*\}\s*,?\s*' % key, "", text)
    text = re.sub(r",(\s*\})", r"\1", text)  # dangling commas
    text = re.sub(r"\{\s*,", "{", text)  # leading commas
    text = re.sub(r":[ \t]+", ": ", text)  # collapse stray spaces after colons
    text = re.sub(r"\n[ \t]+", "\n", text)  # collapse indented empty lines
    return text


def _collapse_named_arrays(text: str) -> str:
    """Replace embedded JSON arrays of {name,...} objects with a placeholder.
    Balance-scan on the INNER text (real newlines, \"-escaped quotes)."""
    out, last = [], 0
    k = 0
    while True:
        j = text.find("[", k)
        if j == -1:
            break
        if '"name"' in text[j : j + 600]:
            depth, p = 0, j
            end = None
            in_str = False
            while p < len(text):
                ch = text[p]
                if in_str:
                    if ch == "\\":
                        p += 2
                        continue
                    if ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "[":
                        depth += 1
                    elif ch == "]":
                        depth -= 1
                        if depth == 0:
                            end = p
                            break
                p += 1
            if end is not None:
                block = text[j : end + 1]
                try:
                    arr = json.loads(block)
                    ok = (
                        isinstance(arr, list)
                        and arr
                        and all(isinstance(x, dict) and "name" in x for x in arr)
                    )
                except Exception:
                    ok = False
                out.append(text[last:j])
                if ok:
                    out.append("[[toolrecall-skills-placeholder]]")
                    last = end + 1
                    k = end + 1
                    continue
                out.append(text[j : end + 1])
                last = end + 1
                k = end + 1
                continue
        k = j + 1
    out.append(text[last:])
    return "".join(out)


def _canon_text(text: str) -> str:
    text = _RE_UUID.sub(_UUID_SUB, text)
    text = _RE_TS.sub(_TS_SUB, text)
    text = _strip_ctx_section(text)
    text = _strip_env_objects(text)
    text = _collapse_named_arrays(text)
    return text


def _walk(node):
    if isinstance(node, dict):
        return {k: _walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(x) for x in node]
    if isinstance(node, str):
        return _canon_text(node)
    return node


def canonicalize_warp(body: bytes) -> bytes | None:
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    envelope = _walk(envelope)
    return json.dumps(envelope, sort_keys=True, ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    files = sorted(glob.glob("/tmp/edge_capture_prod/body_*.json"))
    ds = [open(f, "rb").read() for f in files]
    c = {i: canonicalize_warp(d) for i, d in enumerate(ds)}
    h = lambda b: hashlib.sha256(b).hexdigest()[:12]

    print("== same task, different sessions ==")
    print(
        "0006 (t1 turn0) vs 0008 (t2 turn0): MATCH =",
        c[5] == c[7],
        "|",
        h(ds[5]),
        "vs",
        h(ds[7]),
        "→",
        h(c[5]),
        "/",
        h(c[7]),
    )
    print("== negative controls ==")
    print("task2 turn0 vs turn1 collide:", c[7] == c[8])
    print("different task collide:", c[5] == c[3])
    print("turn1 vs turn2 same task collide:", c[8] == c[9])
