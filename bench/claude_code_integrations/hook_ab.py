#!/usr/bin/env python3
"""Staleness-hook A/B (live Claude Code). Usage: hook_ab.py <start_rep> <n_reps>.
staleness hook A/B, N reps per arm per prompt, interleaved. Isolated from live daemon."""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])
BENCH = Path(os.environ.get("BENCH_DIR", "/tmp/tr-cc-bench"))
ROOT = BENCH / "hook_ab"
CLAUDE = shutil.which("claude") or "claude"
ALLOWED = "Read,Edit,Bash(python3 build.py),Bash(python3 mutate.py),Bash(pytest*),Bash(python3 -m pytest*)"
MUT = ("from pathlib import Path\np = Path('calculator.py')\np.write_text(\n"
       "    'import math\\n\\n'\n    'def compute(values, scale=1.0, debug=False):\\n'\n"
       "    '    # Schema updated by build step\\n'\n    '    if debug:\\n'\n"
       "    '        print(f\"Scaling {values} by {scale}\")\\n'\n"
       "    '    total = sum(values) * scale\\n'\n    '    return math.floor(total)\\n',\n"
       "    encoding='utf-8'\n)\n")
PROMPTS = {
    # verbatim prompt of the branch's live harness
    "guided": ("TASK INSTRUCTIONS:\n1. First, use the Read tool to read calculator.py.\n"
               "2. Next, run the shell command `python3 mutate.py` using the Bash tool.\n"
               "3. Inspect the state and update calculator.py so that compute() returns the total plus 5.\n"
               "4. Finally, run `pytest test_calculator.py` using Bash to verify tests pass.\n"
               "When finished, give a 1-sentence completion summary."),
    # no hint that the build step touches the file: most favourable case for the hook
    "neutral": ("Read calculator.py. Then run the build step `python3 build.py`. Then change compute() "
                "so it returns the total plus 5, and run `pytest -q`. Reply with one sentence."),
}


def setup(d, variant):
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "calculator.py").write_text(
        "def compute(values):\n    # Original implementation\n    total = sum(values)\n    return total\n")
    (d / ("mutate.py" if variant == "guided" else "build.py")).write_text(MUT)
    (d / "test_calculator.py").write_text(
        "from calculator import compute\n\ndef test_compute():\n    assert compute([10, 20, 30]) >= 60\n")


def run(d, variant, hook, hookdir):
    (d / ".claude").mkdir(exist_ok=True)
    s = {"hooks": {}}
    if hook:
        s = {"hooks": {"PostToolUse": [{"matcher": "Read|Bash|Edit|Write", "hooks": [
            {"type": "command", "command": f"{sys.executable} -m toolrecall.adapters.claude_code", "timeout": 15}]}]}}
    (d / ".claude" / "settings.json").write_text(json.dumps(s))
    env = os.environ.copy()
    for v in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"]:
        env.pop(v, None)
    env.update(PYTHONPATH=REPO, TOOLRECALL_CC_HOOK_DIR=str(hookdir),
               TOOLRECALL_TRANSPORT=str(BENCH / "no-daemon.sock"), TOOLRECALL_NO_AUTOSTART="1",
               TOOLRECALL_UPDATE_CHECK="0")
    t0 = time.time()
    p = subprocess.run([CLAUDE, "-p", PROMPTS[variant], "--output-format", "json",
                        "--allowedTools", ALLOWED, "--model", "haiku",
                        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
                       cwd=d, capture_output=True, text=True, env=env, timeout=300, stdin=subprocess.DEVNULL)
    wall = time.time() - t0
    try:
        j = json.loads(p.stdout)
    except Exception:
        j = {"raw": p.stdout[-500:], "err": p.stderr[-500:]}
    ok = subprocess.run(["python3", "-c", "from calculator import compute;import sys;"
                         "sys.exit(0 if compute([10,20,30])==65 else 1)"], cwd=d,
                        capture_output=True).returncode == 0
    return {"variant": variant, "hook": hook, "wall": round(wall, 2), "correct": ok,
            "cost": j.get("total_cost_usd"), "turns": j.get("num_turns"), "usage": j.get("usage"),
            "session_id": j.get("session_id"), "cwd": str(d), "is_error": j.get("is_error"),
            "denials": j.get("permission_denials"), "raw": j.get("raw"), "err": j.get("err")}


def main():
    start = int(sys.argv[1]); n = int(sys.argv[2])
    out = open(BENCH / "hook_ab_results.jsonl", "a")
    for i in range(start, start + n):
        for variant in PROMPTS:
            for hook in ([False, True] if i % 2 == 0 else [True, False]):
                d = ROOT / f"{variant}-{'hook' if hook else 'base'}-{i}"
                setup(d, variant)
                r = run(d, variant, hook, ROOT / f"hookstate-{variant}-{i}-{hook}")
                r["rep"] = i
                out.write(json.dumps(r) + "\n"); out.flush()
                print(variant, "hook" if hook else "base", i, r["cost"], r["turns"], r["correct"], flush=True)


main()
