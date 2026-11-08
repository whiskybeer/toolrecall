#!/usr/bin/env python3
"""Replay request-body dedup (with and without normalize_output) over real Claude Code
transcripts. For every tool_result that would be stubbed, count chars saved x number of later
API calls that carry it (keep-first stubbing is deterministic, so a stub persists from arrival).
Usage: python3 offline_dedup.py <repo> <projects_dir>"""
import glob, json, os, sys
from collections import Counter

sys.path.insert(0, sys.argv[1])
from toolrecall.dedup import dedup_messages, normalize_output  # noqa: E402

PROJ = sys.argv[2]
W = {"in": 1.0, "cw": 1.25, "cr": 0.1, "out": 5.0}  # list-price ratios


def load(path):
    msgs, calls, seen_ids = [], [], set()
    usage_cost = 0.0
    seen_uuid = set()
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("uuid") in seen_uuid:
            continue  # duplicated transcript line (resume artifact), never re-sent on the wire
        seen_uuid.add(d.get("uuid"))
        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            msgs = []  # context restarts after compaction; calls index into the new list
            calls = [c - 10**9 for c in calls]  # earlier calls never see later blocks
        if d.get("isSidechain") or d.get("type") not in ("user", "assistant"):
            continue
        m = d.get("message") or {}
        if d["type"] == "assistant":
            mid = m.get("id")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                calls.append(len(msgs))  # API call issued with msgs[:len(msgs)]
                u = m.get("usage") or {}
                usage_cost += (u.get("input_tokens", 0) * W["in"] + u.get("cache_creation_input_tokens", 0) * W["cw"]
                               + u.get("cache_read_input_tokens", 0) * W["cr"] + u.get("output_tokens", 0) * W["out"])
            msgs.append({"role": "assistant", "content": m.get("content")})
        else:
            c = m.get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                msgs.append({"role": "user", "content": c})
            elif isinstance(c, (str, list)):
                msgs.append({"role": "user", "content": c})
    return msgs, calls, usage_cost


def tr_text(b):
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def analyse(msgs, calls, normalizer):
    new, stats = dedup_messages(msgs, min_chars=800, protect_last=0, normalizer=normalizer)
    saved_weighted = 0.0
    stubs = []
    for i, (a, b) in enumerate(zip(msgs, new)):
        if a is b or a["role"] != "user" or not isinstance(a["content"], list):
            continue
        for x, y in zip(a["content"], b["content"]):
            if x is y or not isinstance(x, dict) or x.get("type") != "tool_result":
                continue
            saved = (len(tr_text(x)) - len(tr_text(y))) / 4  # tokens
            later = sum(1 for c in calls if c > i)
            if later:
                # one cache write when it arrives, cache reads on every later call
                saved_weighted += saved * W["cw"] + saved * W["cr"] * (later - 1)
            stubs.append(tr_text(x)[:80].replace("\n", " | "))
    return stats["blocks"], saved_weighted, stubs


def main():
    files = [f for f in glob.glob(os.path.join(PROJ, "*", "*.jsonl"))
             if "-tmp-" not in os.path.basename(os.path.dirname(f))]  # skip bench/tmp workdirs
    tot = Counter()
    examples = []
    sessions_hit = {"raw": 0, "norm": 0}
    for f in files:
        msgs, calls, cost = load(f)
        if not calls:
            continue
        tot["sessions"] += 1
        tot["calls"] += len(calls)
        tot["cost_units"] += cost
        tot["tool_results"] += sum(1 for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                                   for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
        for tag, nz in (("raw", None), ("norm", normalize_output)):
            blocks, sw, stubs = analyse(msgs, calls, nz)
            tot[f"{tag}_blocks"] += blocks
            tot[f"{tag}_saved_units"] += sw
            if blocks:
                sessions_hit[tag] += 1
            if tag == "norm":
                examples += [(os.path.basename(f)[:8], s) for s in stubs]
    print(json.dumps({k: round(v, 1) if isinstance(v, float) else v for k, v in tot.items()}, indent=1))
    print("sessions with >=1 stub:", sessions_hit)
    for tag in ("raw", "norm"):
        print(f"{tag}: saved {tot[tag + '_saved_units']:.0f} of {tot['cost_units']:.0f} weighted units "
              f"= {100 * tot[tag + '_saved_units'] / max(tot['cost_units'], 1):.3f}% of session cost")
    print("example stubbed tool results (normalized):")
    for e in examples[:25]:
        print(" ", e)


main()
