#!/usr/bin/env python3
"""Live End-to-End Benchmark of Claude Code with ToolRecall Hooks & Dedup.

Executes real Claude Code CLI (v2.1.306) processes against a real testbed project:
- Track 1 (Staleness Protection): Compares Claude Code with vs without ToolRecall's
  PostToolUse drift alert hook on an out-of-band file mutation task.
- Captures real Claude Code JSON output, turn counts, tool call sequences, and
  verbatim provider token usage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or os.path.expanduser("~/.local/bin/claude")
BENCH_ROOT = Path("/tmp/tr_real_claude_bench")


def setup_workspace(target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Base calculator module
    calc = target_dir / "calculator.py"
    calc.write_text(
        "def compute(values):\n"
        "    # Original implementation\n"
        "    total = sum(values)\n"
        "    return total\n",
        encoding="utf-8",
    )

    # 2. Out-of-band mutator script (simulating code generator or migration)
    mutator = target_dir / "mutate.py"
    mutator.write_text(
        "from pathlib import Path\n"
        "p = Path('calculator.py')\n"
        "p.write_text(\n"
        "    'import math\\n\\n'\n"
        "    'def compute(values, scale=1.0, debug=False):\\n'\n"
        "    '    # Schema updated by build step\\n'\n"
        "    '    if debug:\\n'\n"
        "    '        print(f\"Scaling {values} by {scale}\")\\n'\n"
        "    '    total = sum(values) * scale\\n'\n"
        "    '    return math.floor(total)\\n',\n"
        "    encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )

    # 3. Test file
    test_f = target_dir / "test_calculator.py"
    test_f.write_text(
        "from calculator import compute\n\n"
        "def test_compute():\n"
        "    assert compute([10, 20, 30]) >= 60\n",
        encoding="utf-8",
    )


def run_claude_session(work_dir: Path, with_hook: bool) -> Dict[str, Any]:
    claude_dot = work_dir / ".claude"
    claude_dot.mkdir(parents=True, exist_ok=True)

    # Configure hook if enabled
    if with_hook:
        hook_cmd = f"{sys.executable} -m toolrecall.adapters.claude_code"
        settings = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Read|Bash|Edit|Write",
                        "hooks": [{"type": "command", "command": hook_cmd, "timeout": 15}],
                    }
                ]
            }
        }
        (claude_dot / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    else:
        # Explicit empty hooks
        (claude_dot / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    prompt = (
        "TASK INSTRUCTIONS:\n"
        "1. First, use the Read tool to read calculator.py.\n"
        "2. Next, run the shell command `python3 mutate.py` using the Bash tool.\n"
        "3. Inspect the state and update calculator.py so that compute() returns the total plus 5.\n"
        "4. Finally, run `pytest test_calculator.py` using Bash to verify tests pass.\n"
        "When finished, give a 1-sentence completion summary."
    )

    env = os.environ.copy()
    env["PATH"] = f"{os.path.dirname(CLAUDE_BIN)}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO_ROOT)
    # Unset Claude Code nested-session variables
    for var in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"]:
        env.pop(var, None)

    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--model",
        "haiku",
    ]

    t0 = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(work_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    wall = time.time() - t0

    out = proc.stdout
    err = proc.stderr

    data = {}
    try:
        data = json.loads(out)
    except Exception:
        data = {"raw_output": out, "error_output": err}

    return {
        "returncode": proc.returncode,
        "wall_seconds": round(wall, 2),
        "data": data,
        "raw_err": err,
    }


def main():
    print("=" * 70)
    print("  LIVE Claude Code Benchmark: Context Staleness & Out-of-Band Drift")
    print("=" * 70)

    # Arm A: Baseline
    dir_a = BENCH_ROOT / "arm_a_baseline"
    setup_workspace(dir_a)
    print("\n[1/2] Running Arm A (Baseline: No Staleness Hook)...")
    res_a = run_claude_session(dir_a, with_hook=False)
    print(f"  Returncode: {res_a['returncode']} (wall: {res_a['wall_seconds']}s)")

    # Arm B: Treatment
    dir_b = BENCH_ROOT / "arm_b_treatment"
    setup_workspace(dir_b)
    print("\n[2/2] Running Arm B (Treatment: ToolRecall PostToolUse Hook Enabled)...")
    res_b = run_claude_session(dir_b, with_hook=True)
    print(f"  Returncode: {res_b['returncode']} (wall: {res_b['wall_seconds']}s)")

    summary = {
        "arm_a_baseline": res_a,
        "arm_b_treatment": res_b,
        "timestamp": time.time(),
    }

    out_json = Path("bench/results/live_claude_code_benchmark.json")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n✅ Live benchmark results saved to: {out_json}")


if __name__ == "__main__":
    main()
