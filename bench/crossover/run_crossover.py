#!/usr/bin/env python3
"""Unified crossover benchmark driver (Null arm + Tier 2 short + Tier 3 long).

Matrix:
  2 repos (django, toolrecall) x 2 workloads (short, long) x 2 arms (A, B) x 3 reps
  + null_qa (Gate 1).

Per task:
  - Arm A (native):    claude -p straight to the API, no ToolRecall hooks/proxy.
  - Arm B (toolrecall): claude -p routed through isolated ToolRecall proxy (dedup=true)
                        + Claude Code PostToolUse staleness hook.
  - Arm order alternated per (task, rep) for counterbalance (Gate 4).
  - Separate API keys supported via AB_API_KEY_A / AB_API_KEY_B.
  - Per-turn instrumentation saved to `<tag>.turns.jsonl` and summarized in `runs.jsonl`.

Usage:
  python3 bench/crossover/run_crossover.py --dry-run
  python3 bench/crossover/run_crossover.py --repo django --workload long --reps 3 --port 8595
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import instrument as inst  # noqa: E402
import tasks as tmod  # noqa: E402

OUT = Path(os.path.expanduser(os.environ.get("AB_OUT", "~/bench-data/crossover-out")))
WORK = Path(os.path.expanduser(os.environ.get("AB_WORK", "~/bench-data/crossover-work")))
MIN_FREE_MB = int(os.environ.get("AB_MIN_FREE_MB", "1000"))
CLAUDE = shutil.which("claude") or "claude"


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True)


def _fresh_worktree(repo_dir: Path, path: Path, commit: str) -> None:
    _git(repo_dir, "worktree", "remove", "--force", str(path))
    shutil.rmtree(path, ignore_errors=True)
    res = _git(repo_dir, "worktree", "add", "-f", "--detach", str(path), commit)
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed for {repo_dir}@{commit}: {res.stderr}")


def _settings_file(out_dir: Path, arm: str, hook: bool = True) -> Path:
    p = out_dir / f"settings-{arm}{'' if hook else '-nohook'}.json"
    s: dict = {"hooks": {}}
    if arm == "B" and hook:
        h = [{"type": "command", "command": f"{sys.executable} -m toolrecall.adapters.claude_code", "timeout": 15}]
        s = {"hooks": {"PostToolUse": [{"matcher": "Read|Bash|Edit|Write|MultiEdit|NotebookEdit", "hooks": h}]}}
    p.write_text(json.dumps(s))
    return p


def _mem_available_mb() -> int:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 10**6


def _wait_for_memory() -> None:
    waited = 0
    while _mem_available_mb() < MIN_FREE_MB:
        if waited % 300 == 0:
            print(f"waiting: {_mem_available_mb()} MB available < {MIN_FREE_MB} MB", flush=True)
        time.sleep(30)
        waited += 30


def _done_runs(out_dir: Path) -> set[tuple[str, str, str, int]]:
    p = out_dir / "runs.jsonl"
    if not p.exists():
        return set()
    done: set[tuple[str, str, str, int]] = set()
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        done.add((r["task"], r["workload"], r["arm"], int(r["rep"])))
    return done


def _arm_env(arm: str, port: int, out_dir: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_SESSION_ID",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
        )
    }
    env["PYTHONHASHSEED"] = "0"
    key_override = os.environ.get(f"AB_API_KEY_{arm}") or os.environ.get(f"ANTHROPIC_API_KEY_{arm}")
    if key_override:
        env["ANTHROPIC_API_KEY"] = key_override
    if arm == "B":
        env.update(
            ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
            ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: " + os.environ.get("AB_PROFILE", "claude-code"),
            PYTHONPATH=str(tmod.TR_REPO),
            TOOLRECALL_CC_HOOK_DIR=str(out_dir / "hookstate"),
            TOOLRECALL_TRANSPORT=str(out_dir / "no-daemon.sock"),
            TOOLRECALL_NO_AUTOSTART="1",
        )
    return env


def run_one(
    task: tmod.TaskSpec,
    arm: str,
    rep: int,
    arm_order: str,
    port: int,
    model: str,
    out_dir: Path,
    work_dir: Path,
    usage_csv: Path | None = None,
    hook: bool = True,
) -> None:
    w = work_dir / f"{task.repo}_{task.task_id}"
    _fresh_worktree(task.repo_dir, w, task.base_commit)
    env = _arm_env(arm, port, out_dir)
    cmd = [
        CLAUDE,
        "-p",
        task.prompt,
        "--model",
        model,
        "--max-turns",
        str(task.max_turns),
        "--output-format",
        "json",
        "--settings",
        str(_settings_file(out_dir, arm, hook)),
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    if task.allowed_tools:
        cmd.extend(["--allowedTools", task.allowed_tools])

    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            cwd=w,
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,
            stdin=subprocess.DEVNULL,
        )
        stdout, stderr = p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        stdout, stderr = "", f"TIMEOUT after {e.timeout}s"
    t1 = time.time()
    wall = t1 - t0

    try:
        j = json.loads(stdout)
    except Exception:
        j = {"parse_error": stdout[-300:], "stderr": stderr[-300:]}

    subprocess.run(["git", "-C", str(w), "add", "-A", "-N"], capture_output=True)
    diff = subprocess.run(
        ["git", "-C", str(w), "diff", "--", ".", ":(exclude)tests"],
        capture_output=True,
        text=True,
    ).stdout

    tag = f"{task.repo}__{task.task_id}__{task.workload}__{arm}_r{rep}"
    diff_path = out_dir / f"{tag}.diff"
    diff_path.write_text(diff)
    (out_dir / f"{tag}.json").write_text(json.dumps(j))

    if j.get("is_error"):
        print(f"ABORT {tag}: status={j.get('api_error_status')} {str(j.get('result'))[:120]}", flush=True)
        sys.exit(3)

    if task.workload == "null":
        grade, resolved = "null-ok", True
    else:
        g = work_dir / "grade"
        _fresh_worktree(task.repo_dir, g, task.base_commit)
        ap = (
            subprocess.run(["git", "-C", str(g), "apply", "--whitespace=nowarn", str(diff_path)], capture_output=True)
            if diff.strip()
            else None
        )
        if ap is not None and ap.returncode:
            grade, resolved = "diff-apply-fail", False
        else:
            grade, resolved = tmod.grade_task(task, g, diff_path)
        _git(task.repo_dir, "worktree", "remove", "--force", str(g))

    _git(task.repo_dir, "worktree", "remove", "--force", str(w))

    row = {
        "task": task.task_id,
        "repo": task.repo,
        "workload": task.workload,
        "arm": arm,
        "arm_order": arm_order,
        "rep": rep,
        "model": model,
        "hook": hook,
        "wall": round(wall, 1),
        "grade": grade,
        "resolved": resolved,
        "timeout": stderr.startswith("TIMEOUT"),
        "session_id": j.get("session_id"),
        "turns": j.get("num_turns"),
        "cc_cost_usd": j.get("total_cost_usd"),
        "denials": len(j.get("permission_denials") or []),
        "is_error": j.get("is_error"),
        "cwd": str(w),
        "ts_start": t0,
        "ts": t1,
    }
    summary = inst.instrument_run(row, out_dir, tag, usage_csv=usage_csv, ts_start=t0, ts_end=t1)
    row.update(summary)

    with open(out_dir / "runs.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(
        f"{tag}: {grade} turns={row['turns']} cost=${row['cc_cost_usd']} "
        f"repeat_share={row['repeat_share']:.3f} stub_bytes={row['stub_bytes_served']} wall={row['wall']}s",
        flush=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Crossover A/B benchmark driver")
    ap.add_argument("--repo", choices=["null", "django", "toolrecall", "all"], default="all")
    ap.add_argument("--workload", choices=["null", "short", "long", "all"], default="all")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--port", type=int, default=int(os.environ.get("AB_PORT", "8595")))
    ap.add_argument("--model", default=os.environ.get("AB_MODEL", "sonnet"))
    ap.add_argument("--long-max-turns", type=int, default=int(os.environ.get("AB_LONG_MAX_TURNS", "200")))
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--work", type=Path, default=WORK)
    ap.add_argument("--usage-csv", type=Path, default=None, help="Path to Arm B rig proxy_usage.csv")
    ap.add_argument("--no-hook", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Print planned matrix & verify paths without API calls")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    tasks = tmod.build_tasks(args.repo, args.workload, long_max_turns=args.long_max_turns)
    done = _done_runs(args.out)

    plan = []
    for rep in range(1, args.reps + 1):
        for k, task in enumerate(tasks):
            arms = ["A", "B"] if (rep + k) % 2 else ["B", "A"]
            arm_order = "".join(arms)
            for arm in arms:
                if (task.task_id, task.workload, arm, rep) in done:
                    continue
                plan.append((task, arm, rep, arm_order))

    if args.dry_run:
        print(f"DRY-RUN: {len(plan)} runs planned across {len(tasks)} tasks, reps={args.reps}, model={args.model}")
        est_usd = sum(0.25 if t.workload == "long" else (0.05 if t.workload == "short" else 0.005) for t, _, _, _ in plan)
        print(f"Estimated max cost: ~${est_usd:.2f}")
        for t, arm, rep, order in plan:
            print(
                f"  rep={rep} arm={arm} (order={order}) repo={t.repo:10s} workload={t.workload:5s} "
                f"max_turns={t.max_turns:3d} task={t.task_id} base={t.base_commit[:10]}"
            )
        return

    for task, arm, rep, arm_order in plan:
        _wait_for_memory()
        run_one(
            task=task,
            arm=arm,
            rep=rep,
            arm_order=arm_order,
            port=args.port,
            model=args.model,
            out_dir=args.out,
            work_dir=args.work,
            usage_csv=args.usage_csv,
            hook=not args.no_hook,
        )


if __name__ == "__main__":
    main()
