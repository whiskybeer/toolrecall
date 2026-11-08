"""Per task: calls, replayed calls (message id seen in an earlier run of the same task),
usage units billed, and for partial replays the first diverging tool output."""
import json, glob, os, re, collections
RES = "/tmp/trhook/real/results"
def units(u): return u.get("input_tokens",0)+1.25*u.get("cache_creation_input_tokens",0)+0.1*u.get("cache_read_input_tokens",0)+5*u.get("output_tokens",0)
def load(task, rep):
    d = json.load(open(f"{RES}/{task}_r{rep}.json"))
    tdir = os.path.expanduser("~/.claude/projects/" + re.sub(r"[^A-Za-z0-9]", "-", f"/tmp/trhook/real/work/{task}"))
    msgs = collections.OrderedDict(); blocks = []
    for l in open(f"{tdir}/{d['session_id']}.jsonl"):
        e = json.loads(l)
        if e.get("type") == "assistant": msgs[e["message"]["id"]] = units(e["message"].get("usage", {}))
        if e.get("type") in ("assistant", "user") and isinstance(e["message"].get("content"), list):
            for b in e["message"]["content"]: blocks.append(json.dumps(b, sort_keys=True))
    return d, msgs, blocks
tot = collections.Counter()
for task in sorted({os.path.basename(f).rsplit("_r", 1)[0] for f in glob.glob(f"{RES}/*.json")}):
    seen = set(); prev_blocks = []; cold = None; warm = []
    for rep in range(1, 20):
        if not os.path.exists(f"{RES}/{task}_r{rep}.json"): break
        d, msgs, blocks = load(task, rep)
        ids = list(msgs); rp = [m for m in ids if m in seen]
        billed = sum(v for m, v in msgs.items() if m not in seen); full = sum(msgs.values())
        prefix = 0
        while prefix < len(ids) and ids[prefix] in seen: prefix += 1
        why = ""
        if rp and len(rp) < len(ids):
            best = max(prev_blocks, key=lambda pb: next((i for i, (x, y) in enumerate(zip(pb, blocks)) if x != y), min(len(pb), len(blocks))))
            i = next((i for i, (x, y) in enumerate(zip(best, blocks)) if x != y), None)
            if i is not None:
                s, t = best[i], blocks[i]; j = next(k for k, (a, b) in enumerate(zip(s, t)) if a != b)
                why = f"diverges at block {i}: …{s[max(0,j-50):j+30]!r} vs …{t[max(0,j-50):j+30]!r}"
        grade = open(f"{RES}/{task}_r{rep}.grade").read().split()
        ok = "RESOLVED" if grade == ["f2p=0", "p2p=0"] else " ".join(grade)
        print(f"{task[-12:]} r{rep} calls={len(ids):2d} replayed={len(rp):2d} (prefix {prefix}) billed={billed:8.0f}/{full:8.0f} {ok}")
        if why: print("      ", why[:260])
        if rep == 1: cold = billed
        else: warm.append(billed)
        seen.update(ids); prev_blocks.append(blocks)
        tot["calls"] += len(ids); tot["replayed"] += len(rp); tot["billed"] += billed; tot["full"] += full
    if warm: print(f"   -> warm runs vs cold: {sum(warm)/len(warm)/cold-1:+.1%}  (cold {cold:.0f})")
print(f"ALL: calls {tot['calls']}, replayed {tot['replayed']} ({tot['replayed']/tot['calls']:.0%}); billed {tot['billed']:.0f} of {tot['full']:.0f} units ({tot['billed']/tot['full']-1:+.1%} vs no replay)")
