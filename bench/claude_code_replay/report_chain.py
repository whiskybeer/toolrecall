"""Long-chain replay report. One chain = one Claude Code session over 5 issues (resumed).
Per chain and step: calls, calls replayed (message id seen in an earlier chain), usage billed
vs what the same calls would have cost. Divergence = first canonical-differing block vs the
earlier chain with the longest common prefix."""
import json, glob, os, re, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from toolrecall.proxy import _hermes_canon_text, _RE_CANON_DURATION
canon = lambda s: _RE_CANON_DURATION.sub(r"\1 <duration>", _hermes_canon_text(s))
RES = "/tmp/trhook/long/results"; TD = os.path.expanduser("~/.claude/projects/" + re.sub(r"[^A-Za-z0-9]", "-", "/tmp/trhook/long/work/chain"))
def units(u): return u.get("input_tokens",0)+1.25*u.get("cache_creation_input_tokens",0)+0.1*u.get("cache_read_input_tokens",0)+5*u.get("output_tokens",0)
def chain(rep):
    sids = []
    for k in range(5):
        f = f"{RES}/r{rep}_s{k}.json"
        if os.path.exists(f):
            try: sid = json.load(open(f)).get("session_id")
            except Exception: sid = None
            if sid and sid not in sids: sids.append(sid)
    msgs = collections.OrderedDict(); step_of = {}; blocks = []; step = -1
    for sid in sids:
        p = f"{TD}/{sid}.jsonl"
        if not os.path.exists(p): continue
        for l in open(p):
            e = json.loads(l)
            if e.get("isSidechain"): continue
            if e.get("type") == "user" and isinstance(e["message"].get("content"), str) and "Fix the GitHub issue" in e["message"]["content"]:
                step += 1
            if e.get("type") == "assistant":
                mid = e["message"]["id"]
                if mid not in msgs: msgs[mid] = units(e["message"].get("usage", {})); step_of[mid] = step
            if e.get("type") in ("assistant", "user") and isinstance(e["message"].get("content"), list):
                for b in e["message"]["content"]: blocks.append(canon(json.dumps(b, sort_keys=True)))
    return msgs, step_of, blocks
seen = set(); prev = []; rows = []
for rep in range(1, 10):
    if not os.path.exists(f"{RES}/r{rep}_s0.json"): break
    msgs, step_of, blocks = chain(rep)
    ids = list(msgs); pre = 0
    while pre < len(ids) and ids[pre] in seen: pre += 1
    per = collections.defaultdict(lambda: [0, 0, 0.0, 0.0])
    for m in ids:
        s = per[step_of[m]]; s[0] += 1; s[3] += msgs[m]
        if m in seen: s[1] += 1
        else: s[2] += msgs[m]
    grades = [" ".join(open(f"{RES}/r{rep}_s{k}.grade").read().split()) if os.path.exists(f"{RES}/r{rep}_s{k}.grade") else "-" for k in range(5)]
    billed = sum(v for m, v in msgs.items() if m not in seen); full = sum(msgs.values())
    print(f"chain r{rep}: calls={len(ids)} replayed={sum(1 for m in ids if m in seen)} prefix={pre} billed={billed:,.0f} of {full:,.0f} ({billed/full-1:+.0%})")
    for k in sorted(per): c, r, b, f = per[k]; print(f"   step {k}: calls {c:3d} replayed {r:3d}  billed {b:9,.0f} / {f:9,.0f}   grade {grades[k] if 0 <= k < 5 else ''}")
    if prev and pre < len(ids):
        best = max(prev, key=lambda pb: next((i for i, (x, y) in enumerate(zip(pb, blocks)) if x != y), min(len(pb), len(blocks))))
        i = next((i for i, (x, y) in enumerate(zip(best, blocks)) if x != y), None)
        if i is not None:
            s, t = best[i], blocks[i]; j = next((q for q, (a, b) in enumerate(zip(s, t)) if a != b), min(len(s), len(t)))
            print(f"   diverges at block {i}/{len(blocks)}: …{s[max(0,j-90):j+40]!r}\n                         vs …{t[max(0,j-90):j+40]!r}")
    rows.append((rep, billed, full)); seen.update(ids); prev.append(blocks)
if len(rows) > 1:
    cold = rows[0][1]; w = rows[1:]
    print(f"\nwarm chains vs cold chain: {sum(b for _,b,_ in w)/len(w)/cold-1:+.1%};  vs own unreplayed: {sum(b for _,b,_ in w)/sum(f for _,_,f in w)-1:+.1%}")
