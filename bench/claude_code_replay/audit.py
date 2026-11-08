"""Replay safety audit. For every replayed call (assistant message id first produced by an
earlier run), compare the RAW conversation blocks that preceded it with the producing run's
blocks before the same message. Every difference must be one the profile intends to ignore
(test durations, UUIDs, ISO timestamps); anything else is a FALSE MATCH."""
import json, os, re, sys, glob, collections, difflib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from toolrecall.proxy import _hermes_canon_text, _RE_CANON_DURATION
canon = lambda s: _RE_CANON_DURATION.sub(r"\1 <duration>", _hermes_canon_text(s))
PROJ = os.path.expanduser("~/.claude/projects/")
def tdir(cwd): return PROJ + re.sub(r"[^A-Za-z0-9]", "-", cwd)
def load(result_json, cwd):
    d = json.load(open(result_json)); p = f"{tdir(cwd)}/{d['session_id']}.jsonl"
    seq = []  # ("msg", id) or ("blk", raw)
    seen = set()
    for l in open(p):
        e = json.loads(l)
        if e.get("isSidechain"): continue
        t = e.get("type")
        if t == "assistant" and e["message"]["id"] not in seen:
            seen.add(e["message"]["id"]); seq.append(("msg", e["message"]["id"]))
        if t in ("assistant", "user"):
            c = e["message"].get("content")
            for b in (c if isinstance(c, list) else [{"type": "text", "text": c}]):
                seq.append(("blk", json.dumps(b, sort_keys=True)))
    return seq
def before(seq, mid):
    """Blocks preceding the first block of message mid (the request context for that call)."""
    out = []
    for k, v in seq:
        if k == "msg" and v == mid: return out
        if k == "blk": out.append(v)
    return None
INTENDED = [re.compile(r"\b(?:in|took) \d+(?:\.\d+)?\s?(?:ms|s|sec|seconds)\b"),
            re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
            re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")]
def classify(a, b):
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False); kinds = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal": continue
        ctx_a, ctx_b = a[max(0, i1 - 40):i2 + 40], b[max(0, j1 - 40):j2 + 40]
        if any(r.search(ctx_a) and r.search(ctx_b) for r in INTENDED): kinds.append("intended")
        else: kinds.append(f"OTHER: {a[max(0,i1-60):i2+30]!r} vs {b[max(0,j1-60):j2+30]!r}")
    return kinds
def audit(name, runs):
    """runs: ordered list of (label, result_json, cwd)."""
    producer = {}; seqs = {}; n_rep = 0; issues = []; kinds = collections.Counter()
    for label, rj, cwd in runs:
        if not os.path.exists(rj): continue
        try: seq = load(rj, cwd)
        except Exception as ex: issues.append(f"{label}: load failed {ex}"); continue
        seqs[label] = seq
        for k, mid in seq:
            if k != "msg": continue
            if mid in producer:
                n_rep += 1; src = producer[mid]
                a, b = before(seqs[src], mid), before(seq, mid)
                if a is None or b is None or len(a) != len(b):
                    issues.append(f"{label}: replayed {mid[:12]} with different context length ({None if a is None else len(a)} vs {None if b is None else len(b)})"); continue
                for x, y in zip(a, b):
                    if x != y:
                        for kd in classify(x, y):
                            kinds["intended" if kd == "intended" else "OTHER"] += 1
                            if kd != "intended": issues.append(f"{label} call {mid[:12]}: {kd[:300]}")
            else: producer[mid] = label
    print(f"[{name}] replayed calls audited: {n_rep}; raw differences: {dict(kinds)}; FALSE-MATCH issues: {len(issues)}")
    for i in issues[:8]: print("   ", i)
    return n_rep, issues
if __name__ == "__main__":
    C = "/tmp/trhook/ctl/results"; total = []
    for T in ("15996", "17087"):
        runs = [(f"populate_{T}", f"{C}/populate_{T}.json", "/tmp/trhook/ctl/wA")]
        runs += [(f"replay_{T}_{a}", f"{C}/replay_{T}_{a}.json", "/tmp/trhook/ctl/wA") for a in (1, 2)]
        runs += [(f"cwdB_{T}", f"{C}/cwdB_{T}.json", "/tmp/trhook/ctl/wB"), (f"edited_{T}", f"{C}/edited_{T}.json", "/tmp/trhook/ctl/wA"), (f"haiku_{T}", f"{C}/haiku_{T}.json", "/tmp/trhook/ctl/wA"),
                 (f"check_{T}_1", f"{C}/check_{T}_1.json", "/tmp/trhook/ctl/wA"), (f"check_{T}_2", f"{C}/check_{T}_2.json", "/tmp/trhook/ctl/wA"),
                 (f"edhidden_{T}", f"{C}/edhidden_{T}.json", "/tmp/trhook/ctl/wA")]
        total.append(audit(f"controls {T}", runs))
    R = "/tmp/trhook/real/results"
    for T in ("pytest-dev__pytest-11148", "pytest-dev__pytest-8906"):
        total.append(audit(f"short {T[-5:]}", [(f"r{r}", f"{R}/{T}_r{r}.json", f"/tmp/trhook/real/work/{T}") for r in range(1, 6)]))
    LR = "/tmp/trhook/long/results"
    total.append(audit("long chains", [(f"chain r{r}", f"{LR}/r{r}_s4.json", "/tmp/trhook/long/work/chain") for r in range(1, 5)]))
    print(f"TOTAL replayed calls audited: {sum(n for n,_ in total)}; false-match issues: {sum(len(i) for _,i in total)}")
