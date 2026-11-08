"""A/B report: per run, API calls and cost units; a proxied call whose message id
already appeared in an earlier run was replayed from cache (never billed).
Cost units: input 1, cache write 1.25, cache read 0.1, output 5 (ratio of list prices)."""

import json, glob, os, sys, collections

RES = "/tmp/trhook/results"
TDIR = os.path.expanduser("~/.claude/projects/-tmp-trhook-ccwork-task")


def units(u):
    return (
        u.get("input_tokens", 0)
        + 1.25 * u.get("cache_creation_input_tokens", 0)
        + 0.1 * u.get("cache_read_input_tokens", 0)
        + 5 * u.get("output_tokens", 0)
    )


def run(label):
    d = json.load(open(f"{RES}/{label}.json"))
    sid = d["session_id"]
    msgs = collections.OrderedDict()
    for l in open(f"{TDIR}/{sid}.jsonl"):
        e = json.loads(l)
        if e.get("type") == "assistant":
            msgs[e["message"]["id"]] = units(e["message"].get("usage", {}))
    return d, msgs


seen = set()
rows = collections.defaultdict(list)
labels = sorted(
    {os.path.basename(f)[:-5] for f in glob.glob(f"{RES}/*.json")},
    key=lambda s: (s.rstrip("0123456789"), int(s[len(s.rstrip("0123456789")) :])),
)
for lab in labels:
    d, msgs = run(lab)
    arm = lab.rstrip("0123456789")
    replayed = [m for m in msgs if arm == "proxy" and m in seen]
    billed = sum(v for m, v in msgs.items() if m not in replayed)
    if arm == "proxy":
        seen.update(msgs)
    ok = (
        "PASS"
        if "pass" in (d.get("result") or "").lower() or "fix" in (d.get("result") or "").lower()
        else "?"
    )
    rows[arm].append(billed)
    print(
        f"{lab:8s} calls={len(msgs)} replayed={len(replayed)} billed_units={billed:9.0f} cc_reported_usd={d.get('total_cost_usd'):.5f} {ok}"
    )
for arm, v in rows.items():
    print(f"{arm:7s} n={len(v)} mean_billed_units={sum(v) / len(v):9.0f}")
if rows.get("direct") and rows.get("proxy"):
    b = sum(rows["direct"]) / len(rows["direct"])
    p = sum(rows["proxy"]) / len(rows["proxy"])
    print(f"proxy vs direct per run: {p / b - 1:+.1%}  (first proxy run is the cold fill)")
    w = rows["proxy"][1:]
    print(f"warm proxy runs only (2..n): {sum(w) / len(w) / b - 1:+.1%}")
