#!/usr/bin/env python3
"""Task definitions, prompts, and hidden/suite graders for the crossover benchmark.

Matrix:
  2 repos (django, toolrecall) x 2 workloads (short, long) + null_qa (Gate 1).
  Every task pins: repo path, base commit, allowed tools, max_turns, and grader.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TR_REPO = Path(os.environ.get("TR_REPO", str(Path(__file__).resolve().parents[2])))
SWE_DJ_DIR = Path(os.path.expanduser(os.environ.get("SWE_DJ_DIR", "~/bench-data/swe-dj")))
DJ_REPO = SWE_DJ_DIR / "django"
DJ_PY = SWE_DJ_DIR / "env" / "bin" / "python"
DJ_TEST = SWE_DJ_DIR / "bin" / "dj-test"

SHORT_DJ_TASKS = [
    "django__django-15738",
    "django__django-15996",
    "django__django-16595",
    "django__django-16820",
    "django__django-17087",
]

DJ_HOT_FILES = [
    "django/db/migrations/autodetector.py",
    "django/db/migrations/serializer.py",
    "django/db/migrations/operations/fields.py",
    "django/db/migrations/operations/models.py",
    "django/db/migrations/writer.py",
    "django/db/migrations/questioner.py",
    "django/db/migrations/loader.py",
    "django/db/migrations/executor.py",
]

TR_HOT_FILES = [
    "toolrecall/config.py",
    "toolrecall/shim.py",
    "toolrecall/client.py",
    "toolrecall/stub_core.py",
    "toolrecall/keys.py",
    "toolrecall/shim_stub.py",
    "toolrecall/dedup.py",
    "toolrecall/proxy.py",
]


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    repo: str            # "null" | "django" | "toolrecall"
    workload: str        # "null" | "short" | "long"
    repo_dir: Path
    base_commit: str
    prompt: str
    allowed_tools: str
    max_turns: int


def _git_rev(repo_dir: Path, ref: str = "HEAD") -> str:
    p = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", ref],
        capture_output=True,
        text=True,
    )
    return p.stdout.strip() if p.returncode == 0 else ref


def _dj_allowed_tools() -> str:
    return ",".join([
        "Read", "Edit", "Write", "Glob", "Grep",
        f"Bash({DJ_TEST}:*)",
        f"Bash(PYTHONPATH=.. {DJ_PY} runtests.py:*)",
        f"Bash({DJ_PY} runtests.py:*)",
        "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)",
        "Bash(ls:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(cat:*)",
        "Bash(head:*)", "Bash(tail:*)", "Bash(sed -n:*)", "Bash(wc:*)",
    ])


def _tr_allowed_tools() -> str:
    py = sys.executable
    return ",".join([
        "Read", "Edit", "Write", "Glob", "Grep",
        f"Bash({py} -m pytest:*)",
        "Bash(pytest:*)",
        "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)",
        "Bash(ls:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(cat:*)",
        "Bash(head:*)", "Bash(tail:*)", "Bash(sed -n:*)", "Bash(wc:*)",
    ])


def build_tasks(
    repo_filter: str = "all",
    workload_filter: str = "all",
    long_max_turns: int = 200,
) -> list[TaskSpec]:
    """Return the list of TaskSpec objects matching the requested repo/workload."""
    tasks: list[TaskSpec] = []

    if workload_filter in ("null", "all") and repo_filter in ("null", "all"):
        tasks.append(
            TaskSpec(
                task_id="null_qa",
                repo="null",
                workload="null",
                repo_dir=TR_REPO,
                base_commit=_git_rev(TR_REPO, "main"),
                prompt=(
                    "Answer in one short paragraph: what is the difference between a POSIX "
                    "process and a POSIX thread? Do not read, search, or modify any files."
                ),
                allowed_tools="",
                max_turns=5,
            )
        )

    if repo_filter in ("django", "all"):
        if workload_filter in ("short", "all"):
            for iid in SHORT_DJ_TASKS:
                base_file = SWE_DJ_DIR / "inst" / iid / "base_commit"
                ps_file = SWE_DJ_DIR / "inst" / iid / "problem_statement"
                base = base_file.read_text().strip() if base_file.exists() else "HEAD"
                ps = ps_file.read_text() if ps_file.exists() else f"(missing {iid})"
                prompt = (
                    "You are working in a checkout of the django/django repository. Fix the GitHub issue below "
                    "by changing the library source code. Do not modify or add tests under tests/ — hidden tests "
                    "will grade your fix.\n\nRun tests with\n"
                    f"  {DJ_TEST} <module labels, e.g. migrations.test_writer>\n\n"
                    "When the fix is complete, stop and give a one-paragraph summary.\n\n"
                    f"<issue>\n{ps}\n</issue>"
                )
                tasks.append(
                    TaskSpec(
                        task_id=iid,
                        repo="django",
                        workload="short",
                        repo_dir=DJ_REPO,
                        base_commit=base,
                        prompt=prompt,
                        allowed_tools=_dj_allowed_tools(),
                        max_turns=50,
                    )
                )

        if workload_filter in ("long", "all"):
            hot_list = "\n".join(f"  - {f}" for f in DJ_HOT_FILES)
            base = _git_rev(DJ_REPO, "HEAD")
            prompt = (
                "You are working in a checkout of django/django. Perform a thorough, incremental "
                "cross-module audit and deterministic-output hardening pass across ONLY these "
                "migration modules (do not modify or add files under tests/):\n"
                f"{hot_list}\n\n"
                "Requirements (complete one module at a time, running the test suite after EVERY edit):\n"
                "1. Read all 8 modules above to map how operation deconstruction, serialization, "
                "autodetection, and writer formatting interact.\n"
                "2. Add explicit `__repr__` / deterministic ordering / defensive validation helpers "
                "where missing or inconsistent across `operations/fields.py`, `operations/models.py`, "
                "`serializer.py`, `autodetector.py`, `writer.py`, `questioner.py`, `loader.py`, and `executor.py`, "
                "preserving 100% backward compatibility with Django's migrations test suite.\n"
                "3. After EACH individual module edit, re-read the affected module(s) and run:\n"
                f"  {DJ_TEST} migrations.test_operations migrations.test_autodetector migrations.test_writer "
                "migrations.test_loader migrations.test_executor migrations.test_questioner\n"
                "4. Fix any test failures immediately, re-read the modified files, and re-run the suite "
                "until all 6 test modules pass with zero failures.\n"
                "5. Finally, inspect `git diff` across all 8 modules and run the full `migrations` test suite "
                "one final time to confirm everything is green."
            )
            tasks.append(
                TaskSpec(
                    task_id="django__migrations_hot8_long",
                    repo="django",
                    workload="long",
                    repo_dir=DJ_REPO,
                    base_commit=base,
                    prompt=prompt,
                    allowed_tools=_dj_allowed_tools(),
                    max_turns=long_max_turns,
                )
            )

    if repo_filter in ("toolrecall", "all"):
        tr_base = os.environ.get("TR_BASE_COMMIT") or _git_rev(TR_REPO, "main")
        if workload_filter in ("short", "all"):
            prompt = (
                "You are working in a checkout of toolrecall. Inspect `toolrecall/keys.py` and "
                "`tests/test_keys.py`, verify that canonical key generation handles nested dicts "
                "with mixed key types deterministically, add a concise docstring/edge-case guard "
                "if needed without breaking any existing behavior, and run:\n"
                f"  {sys.executable} -m pytest tests/test_keys.py -q\n"
                "Do not modify tests/. Summarize your findings and test result in one paragraph."
            )
            tasks.append(
                TaskSpec(
                    task_id="toolrecall__keys_short",
                    repo="toolrecall",
                    workload="short",
                    repo_dir=TR_REPO,
                    base_commit=tr_base,
                    prompt=prompt,
                    allowed_tools=_tr_allowed_tools(),
                    max_turns=40,
                )
            )

        if workload_filter in ("long", "all"):
            hot_list = "\n".join(f"  - {f}" for f in TR_HOT_FILES)
            prompt = (
                "You are working in a checkout of toolrecall. Perform a thorough, incremental "
                "cross-module reliability and zero-deps hardening pass across ONLY these core modules "
                "(do not modify tests/):\n"
                f"{hot_list}\n\n"
                "Requirements (complete one module at a time, running pytest after EVERY edit):\n"
                "1. Read all 8 modules above to map the request/stub/key/dedup lifecycle.\n"
                "2. Add or tighten defensive input validation, clear docstrings, and boundary guards "
                "in each module while keeping 100% stdlib-only imports and preserving all existing APIs.\n"
                "3. After EACH individual module edit, re-read the affected module(s) and run:\n"
                f"  {sys.executable} -m pytest tests/test_keys.py tests/test_dedup.py tests/test_config.py -q\n"
                "4. Fix any regressions immediately, re-read the modified files, and re-run the suite "
                "until all tests pass.\n"
                "5. Finally, inspect `git diff` across all 8 modules and run the unit test suite "
                "one final time to confirm everything is green."
            )
            tasks.append(
                TaskSpec(
                    task_id="toolrecall__shim_hot8_long",
                    repo="toolrecall",
                    workload="long",
                    repo_dir=TR_REPO,
                    base_commit=tr_base,
                    prompt=prompt,
                    allowed_tools=_tr_allowed_tools(),
                    max_turns=long_max_turns,
                )
            )

    return tasks


def grade_task(task: TaskSpec, work_dir: Path, diff_path: Path | None) -> tuple[str, bool]:
    """Grade a completed run on an isolated verification tree. Returns (grade_str, resolved)."""
    if task.workload == "null":
        return "null-ok", True

    if task.repo == "django" and task.workload == "short":
        grade_py = SWE_DJ_DIR / "grade_dj.py"
        if not grade_py.exists():
            return "grader-missing", False
        p = subprocess.run(
            [str(DJ_PY), str(grade_py), task.task_id, str(work_dir)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        lines = p.stdout.strip().splitlines()
        grade = lines[-1] if lines else "grade-error"
        return grade, (grade == "f2p=0 p2p=0")

    if task.repo == "django" and task.workload == "long":
        p = subprocess.run(
            [
                str(DJ_PY),
                "runtests.py",
                "--parallel",
                "1",
                "--noinput",
                "migrations.test_operations",
                "migrations.test_autodetector",
                "migrations.test_writer",
                "migrations.test_loader",
                "migrations.test_executor",
                "migrations.test_questioner",
            ],
            cwd=str(work_dir / "tests"),
            env={**os.environ, "PYTHONPATH": str(work_dir)},
            capture_output=True,
            text=True,
            timeout=1800,
        )
        ok = p.returncode == 0
        return ("dj-mig=pass" if ok else f"dj-mig=fail({p.returncode})"), ok

    if task.repo == "toolrecall":
        tests = ["tests/test_keys.py"] if task.workload == "short" else [
            "tests/test_keys.py", "tests/test_dedup.py", "tests/test_config.py"
        ]
        p = subprocess.run(
            [sys.executable, "-m", "pytest", *tests, "-q"],
            cwd=str(work_dir),
            env={**os.environ, "PYTHONPATH": str(work_dir)},
            capture_output=True,
            text=True,
            timeout=600,
        )
        ok = p.returncode == 0
        return ("pytest=pass" if ok else f"pytest=fail({p.returncode})"), ok

    return "unknown-task", False
