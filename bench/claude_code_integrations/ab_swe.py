#!/usr/bin/env python3
"""Apples-to-apples A/B: Claude Code native vs Claude Code + ToolRecall on real SWE-bench Lite
django/db/migrations issues, graded with the official hidden tests.

Arm A (native):    claude -p straight to the API, no hooks.
Arm B (toolrecall): same, through an isolated ToolRecall proxy (claude-code profile) + the
                    staleness hook. Dedup off (the recommended Claude Code setup).
Held equal: model, prompt, tool allowlist, fixed checkout path, base commit, PYTHONHASHSEED=0,
pinned MCP. Per task: REPS runs per arm, arm order alternating per rep.

Usage: ab_swe.py <proxy_port> <reps> [task ...]   (results appended to $AB_OUT/runs.jsonl)
"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

L = Path(os.environ.get("SWE_DJ_DIR", "/home/hermes/bench-data/swe-dj"))  # instances, venv, grader
REPO_DJ = L / "django"
PY = L / "env/bin/python"
TR = Path(os.environ.get("TR_REPO", str(Path(__file__).resolve().parents[2])))
OUT = Path(os.environ.get("AB_OUT", "/home/hermes/bench-data/ab-out")); OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(os.environ.get("AB_WORK", "/home/hermes/bench-data/ab-work"))  # fixed path per task (cwd is in the prompt)
MIN_FREE_MB = int(os.environ.get("AB_MIN_FREE_MB", "1000"))
CLAUDE = shutil.which("claude") or "claude"
MODEL = os.environ.get("AB_MODEL", "sonnet")
TASKS = ["django__django-15738", "django__django-15996", "django__django-16595",
         "django__django-16820", "django__django-17087"]
ALLOWED = ",".join([
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(cd:*)", f"Bash(PYTHONPATH=.. {PY} runtests.py:*)", f"Bash({PY} runtests.py:*)",
    "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(ls:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(cat:*)", "Bash(head:*)",
    "Bash(tail:*)", "Bash(sed -n:*)", "Bash(wc:*)",
])


def prompt(iid):
    ps = (L / "inst" / iid / "problem_statement").read_text()
    return ("You are working in a checkout of the django/django repository. Fix the GitHub issue below "
            "by changing the library source code. Do not modify or add tests under tests/ — hidden tests "
            "will grade your fix.\n\nRun tests with\n"
            f"  cd tests && PYTHONPATH=.. {PY} runtests.py --parallel 1 <module labels, e.g. migrations.test_writer>\n\n"
            "When the fix is complete, stop and give a one-paragraph summary.\n\n"
            f"<issue>\n{ps}\n</issue>")


def git(*a, **kw):
    return subprocess.run(["git", "-C", str(REPO_DJ), *a], capture_output=True, text=True, **kw)


def fresh_worktree(path, commit):
    git("worktree", "remove", "--force", str(path))
    shutil.rmtree(path, ignore_errors=True)
    git("worktree", "add", "-f", "--detach", str(path), commit)


def settings_file(arm):
    p = OUT / f"settings-{arm}.json"
    s = {"hooks": {}}
    if arm == "B":
        h = [{"type": "command", "command": f"{sys.executable} -m toolrecall.adapters.claude_code", "timeout": 15}]
        s = {"hooks": {"PostToolUse": [{"matcher": "Read|Bash|Edit|Write|MultiEdit|NotebookEdit", "hooks": h}]}}
    p.write_text(json.dumps(s))
    return p


def run_one(iid, arm, rep, port):
    base = (L / "inst" / iid / "base_commit").read_text().strip()
    w = WORK / iid
    fresh_worktree(w, base)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                        "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS")}
    env["PYTHONHASHSEED"] = "0"
    if arm == "B":
        env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
                   ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: claude-code",
                   PYTHONPATH=str(TR), TOOLRECALL_CC_HOOK_DIR=str(OUT / "hookstate"),
                   TOOLRECALL_TRANSPORT=str(OUT / "no-daemon.sock"), TOOLRECALL_NO_AUTOSTART="1")
    cmd = [CLAUDE, "-p", prompt(iid), "--model", MODEL, "--output-format", "json",
           "--settings", str(settings_file(arm)), "--allowedTools", ALLOWED,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=w, env=env, capture_output=True, text=True, timeout=2400,
                           stdin=subprocess.DEVNULL)
        stdout, stderr = p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:  # recorded as a failed run, never crashes the driver
        stdout, stderr = "", f"TIMEOUT after {e.timeout}s"
    wall = time.time() - t0
    try:
        j = json.loads(stdout)
    except Exception:
        j = {"parse_error": stdout[-300:], "stderr": stderr[-300:]}
    subprocess.run(["git", "-C", str(w), "add", "-A", "-N"], capture_output=True)
    diff = subprocess.run(["git", "-C", str(w), "diff", "--", ".", ":(exclude)tests"],
                          capture_output=True, text=True).stdout
    tag = f"{iid}_{arm}_r{rep}"
    (OUT / f"{tag}.diff").write_text(diff)
    (OUT / f"{tag}.json").write_text(json.dumps(j))
    g = WORK / "grade"
    fresh_worktree(g, base)
    ap = subprocess.run(["git", "-C", str(g), "apply", "--whitespace=nowarn", str(OUT / f"{tag}.diff")],
                        capture_output=True) if diff.strip() else None
    if ap is not None and ap.returncode:
        grade = "diff-apply-fail"
    else:
        grade = subprocess.run([str(PY), str(L / "grade_dj.py"), iid, str(g)], capture_output=True,
                               text=True).stdout.strip().splitlines()[-1:] or ["grade-error"]
        grade = grade[0]
    git("worktree", "remove", "--force", str(g))
    git("worktree", "remove", "--force", str(w))
    row = {"task": iid, "arm": arm, "rep": rep, "wall": round(wall, 1), "grade": grade,
           "resolved": grade == "f2p=0 p2p=0", "timeout": stderr.startswith("TIMEOUT"), "session_id": j.get("session_id"),
           "turns": j.get("num_turns"), "cc_cost_usd": j.get("total_cost_usd"),
           "denials": len(j.get("permission_denials") or []), "is_error": j.get("is_error"),
           "cwd": str(w), "ts": time.time()}
    with open(OUT / "runs.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(f"{tag}: {grade} turns={row['turns']} cc_cost=${row['cc_cost_usd']} wall={row['wall']}s "
          f"denials={row['denials']}", flush=True)


def mem_available_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 10 ** 6


def wait_for_memory():
    """Pause while another workload has the machine short of RAM (swap thrash
    turned a run into a 40-min timeout once); resumes on its own."""
    waited = 0
    while mem_available_mb() < MIN_FREE_MB:
        if waited % 300 == 0:
            print(f"waiting: {mem_available_mb()} MB available < {MIN_FREE_MB} MB", flush=True)
        time.sleep(30)
        waited += 30


def done_runs():
    p = OUT / "runs.jsonl"
    if not p.exists():
        return set()
    return {(r["task"], r["arm"], r["rep"]) for r in map(json.loads, p.read_text().splitlines())}


def main():
    port, reps = int(sys.argv[1]), int(sys.argv[2])
    tasks = sys.argv[3:] or TASKS
    done = done_runs()  # resume: a restart skips runs already recorded
    for rep in range(1, reps + 1):
        for k, iid in enumerate(tasks):
            arms = ["A", "B"] if (rep + k) % 2 else ["B", "A"]
            for arm in arms:
                if (iid, arm, rep) in done:
                    continue
                wait_for_memory()
                run_one(iid, arm, rep, port)


main()
