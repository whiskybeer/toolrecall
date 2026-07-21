"""workloads.py — 3 benchmark workloads, 400+ turns each.

Each workload returns a Workload instance where every turn specifies:
  - user message (the instruction)
  - files the agent should read
  - files the agent may write
  - clean_files: files safe to drop from context (for toolrecall arm)

The read/write metadata is what the toolrecall arm uses to decide
which file content to keep vs drop from conversation history.
"""

import itertools
import os

# Root of the ToolRecall codebase — used as the file pool
REPO = os.path.expanduser("~/toolrecall")

# Source files rotated through by all workloads
SOURCE_FILES = [
    "toolrecall/cache.py",
    "toolrecall/client.py",
    "toolrecall/daemon.py",
    "toolrecall/config.py",
    "toolrecall/cli.py",
    "toolrecall/shim.py",
    "toolrecall/context_tracker.py",
    "toolrecall/proxy.py",
    "toolrecall/_db.py",
    "toolrecall/replay.py",
    "toolrecall/normalizer.py",
    "toolrecall/mcp_bridge.py",
    "toolrecall/transport.py",
    "toolrecall/mcp_registry.py",
    "toolrecall/mcp_fetch.py",
    "toolrecall/mcp_time.py",
    "toolrecall/replay_cli.py",
]

DOC_FILES = [
    "docs/ARCHITECTURE.md",
    "docs/CONTEXT_TRACKER.md",
    "docs/BENCHMARK.md",
    "docs/FORWARD_PROXY.md",
    "docs/CLI.md",
    "docs/CONFIG_REFERENCE.md",
    "docs/TROUBLESHOOTING.md",
    "docs/HOW_IT_WORKS.md",
    "docs/SECURITY.md",
    "README.md",
]

TEST_FILES = [
    "tests/test_cache_safety.py",
    "tests/test_client.py",
    "tests/test_context_tracker.py",
    "tests/test_shim.py",
    "tests/test_e2e_proxy.py",
    "tests/test_libsql_sync.py",
]

# ── Core files for repeated-read workloads ──────────────────
# These are the key modules reviewed in every "session".
CORE_FILES = [
    "toolrecall/cache.py",
    "toolrecall/daemon.py",
    "toolrecall/config.py",
    "toolrecall/client.py",
]


class WorkloadStep:
    """A single turn: instruction + file I/O metadata."""

    def __init__(self, message: dict, reads: list[str] = None,
                 writes: list[str] = None, clean_files: list[str] = None,
                 check: str = None):
        self.message = message
        self.reads = reads or []
        self.writes = writes or []
        self.clean_files = clean_files or []
        self.check = check  # human-readable expected outcome for validation

    def __repr__(self):
        return f"<WorkloadStep reads={len(self.reads)} writes={len(self.writes)}>"


class Workload:
    """A scripted workload — sequence of steps plus initial conversation."""

    def __init__(self, workload_id: str, steps: list[WorkloadStep],
                 initial_messages: list[dict] = None,
                 repo_root: str = REPO):
        self.id = workload_id
        self.steps = steps
        self._initial = initial_messages or []
        self.repo_root = repo_root

    def step(self, turn_index: int) -> WorkloadStep | None:
        idx = turn_index - 1  # 1-based
        if idx < len(self.steps):
            return self.steps[idx]
        return None

    def initial_messages(self) -> list[dict]:
        return list(self._initial)

    def total_turns(self) -> int:
        return len(self.steps)


def _make_msg(action: str, file_label: str, detail: str = "") -> dict:
    """Build a user message from a template."""
    content = f"{action} {file_label}."
    if detail:
        content += f" {detail}"
    return {"role": "user", "content": content}


def _rotate(files: list[str], start: int, count: int, repo: str = REPO) -> list[str]:
    """Get a contiguous slice from a cycled file list, returning absolute paths."""
    cycled = itertools.cycle(files)
    all_items = list(itertools.islice(cycled, start, start + count))
    return [os.path.join(repo, f) for f in all_items]


# ── Workload 1: Debugging (bugfix) ─────────────────────────

def workload_bugfix(seed: int = 42) -> Workload:
    """Find and fix bugs in source files.

    Pattern per turn: read a source file → identify a potential issue →
    write a fix. High write ratio — many cache invalidations.
    Total turns: 450+
    """
    import random
    rng = random.Random(seed)
    turns = 0
    steps = []

    def step(msg, reads, writes, clean=None):
        nonlocal turns
        turns += 1
        steps.append(WorkloadStep(
            message=msg, reads=reads, writes=writes,
            clean_files=clean or [],
            check=f"review {os.path.basename(reads[0]) if reads else 'file'}",
        ))

    system_msg = [{"role": "system",
                   "content": "You are a senior Python developer debugging a codebase."}]

    # Phase 1: Read-only inspection (50 turns — warms the cache)
    for i in range(50):
        files = _rotate(SOURCE_FILES, start=i, count=2)
        step(
            _make_msg("Review", os.path.basename(files[0]),
                      "Look for potential None-safety issues, missing error handling, or type mismatches."),
            reads=files, writes=[],
            clean=files,  # only read, safe to drop
        )

    # Phase 2: Fix bugs with writes (200 turns — high cache invalidation)
    for i in range(200):
        src_file = _rotate(SOURCE_FILES, start=i, count=1)[0]
        ref_file = _rotate(DOC_FILES, start=i, count=1)[0] if rng.random() < 0.3 else None
        reads = [src_file] + ([ref_file] if ref_file else [])
        step(
            _make_msg("Fix", os.path.basename(src_file),
                      "There is a type annotation issue in a function signature. "
                      "Fix it by adding proper Optional/Union types."),
            reads=reads, writes=[src_file],
            clean=reads,  # we read ref files but only mutate src_file
        )

    # Phase 3: Add error handling (100 turns — reads + writes)
    for i in range(100):
        src_file = _rotate(SOURCE_FILES, start=i + 200, count=1)[0]
        step(
            _make_msg("Harden", os.path.basename(src_file),
                      "Add try/except wrappers around file I/O operations. "
                      "Log warnings instead of crashing."),
            reads=[src_file], writes=[src_file],
            clean=[src_file],
        )

    # Phase 4: Cross-file investigation (100 turns — reads across multiple files)
    for i in range(100):
        files = _rotate(SOURCE_FILES, start=i + 300, count=2)
        doc_ref = _rotate(DOC_FILES, start=i, count=1)[0]
        step(
            _make_msg("Trace", os.path.basename(files[0]),
                      f"Trace how data flows between {os.path.basename(files[0])} "
                      f"and {os.path.basename(files[1])}. Check {os.path.basename(doc_ref)} for architecture context."),
            reads=files + [doc_ref], writes=[],
            clean=files + [doc_ref],
        )

    return Workload("bugfix", steps, initial_messages=system_msg, repo_root=REPO)


# ── Workload 2: Feature Development (feature) ──────────────

def workload_feature(seed: int = 42) -> Workload:
    """Add small features across the codebase.

    Pattern per turn: read 1-3 source files → write a new function or modify
    existing code. Write ratio ~50% — moderate invalidation rate.
    Total turns: 450+
    """
    import random
    rng = random.Random(seed + 1000)
    turns = 0
    steps = []

    def step(msg, reads, writes, clean=None):
        nonlocal turns
        turns += 1
        steps.append(WorkloadStep(
            message=msg, reads=reads, writes=writes,
            clean_files=clean or [],
            check=f"feature {os.path.basename(reads[0]) if reads else 'file'}",
        ))

    system_msg = [{"role": "system",
                   "content": "You are a senior Python developer adding features to a codebase."}]

    # Phase 1: Read architecture docs (50 turns)
    for i in range(50):
        doc = _rotate(DOC_FILES, start=i, count=1)[0]
        src = _rotate(SOURCE_FILES, start=i, count=1)[0]
        step(
            _make_msg("Study", os.path.basename(doc),
                      f"Read {os.path.basename(doc)} and {os.path.basename(src)} "
                      f"to understand the architecture before making changes."),
            reads=[doc, src], writes=[],
            clean=[doc, src],
        )

    # Phase 2: Add logging/verbose flags (150 turns — writes with reads)
    for i in range(150):
        src_file = _rotate(SOURCE_FILES, start=i + 50, count=1)[0]
        step(
            _make_msg("Add verbose logging", os.path.basename(src_file),
                      "Add structured logging (import logging, set up logger, "
                      "log key operations at DEBUG level)."),
            reads=[src_file], writes=[src_file],
            clean=[src_file],
        )

    # Phase 3: Add new utility functions (150 turns — reads multiple, writes one)
    for i in range(150):
        files = _rotate(SOURCE_FILES, start=i + 200, count=2)
        step(
            _make_msg("Add helper", os.path.basename(files[1]),
                      f"Add a helper function to {os.path.basename(files[1])} "
                      f"that uses types from {os.path.basename(files[0])}. "
                      "Add proper docstring and type hints."),
            reads=files, writes=[files[1]],
            clean=files,
        )

    # Phase 4: Write test coverage (100 turns — reads src, writes test)
    for i in range(100):
        src_file = _rotate(SOURCE_FILES, start=i + 350, count=1)[0]
        test_file = _rotate(TEST_FILES, start=i, count=1)[0]
        step(
            _make_msg("Add tests", os.path.basename(src_file),
                      f"Read {os.path.basename(src_file)} and add test coverage "
                      f"to {os.path.basename(test_file)}. Add parametrized tests."),
            reads=[src_file, test_file], writes=[test_file],
            clean=[src_file, test_file],
        )

    return Workload("feature", steps, initial_messages=system_msg, repo_root=REPO)


# ── Workload 3: Data Analysis (analysis) ───────────────────

def workload_analysis(seed: int = 42) -> Workload:
    """Read log/data files, compute statistics, report.

    Pattern per turn: read files → compute → write analysis output.
    Low write ratio but heavy read volume on each turn.
    Total turns: 400+
    """
    import random
    rng = random.Random(seed + 2000)
    turns = 0
    steps = []

    def step(msg, reads, writes, clean=None):
        nonlocal turns
        turns += 1
        steps.append(WorkloadStep(
            message=msg, reads=reads, writes=writes,
            clean_files=clean or [],
            check=f"analyze {os.path.basename(reads[0]) if reads else 'data'}",
        ))

    system_msg = [{"role": "system",
                   "content": "You are a data analyst working with code metrics and logs."}]

    # Phase 1: Read and summarize files (100 turns)
    for i in range(100):
        files = _rotate(SOURCE_FILES, start=i, count=3)
        step(
            _make_msg("Summarize", f"{len(files)} files",
                      f"Read {', '.join(os.path.basename(f) for f in files)}. "
                      "Summarize the purpose of each module and how they interact."),
            reads=files, writes=[],
            clean=files,
        )

    # Phase 2: Count patterns and report (100 turns)
    for i in range(100):
        files = _rotate(SOURCE_FILES, start=i + 100, count=2)
        step(
            _make_msg("Count patterns", os.path.basename(files[0]),
                      f"Read {os.path.basename(files[0])} and {os.path.basename(files[1])}. "
                      "Count how many functions, classes, and TODO comments exist in each."),
            reads=files, writes=[],
            clean=files,
        )

    # Phase 3: Compare versions / trace changes (100 turns)
    for i in range(100):
        files = _rotate(SOURCE_FILES, start=i + 200, count=2)
        step(
            _make_msg("Compare", f"{os.path.basename(files[0])} vs {os.path.basename(files[1])}",
                      f"Compare {os.path.basename(files[0])} and {os.path.basename(files[1])}. "
                      "Identify shared patterns and differences in error handling approach."),
            reads=files, writes=[],
            clean=files,
        )

    # Phase 4: Audit security patterns (100 turns — reads, rare writes)
    for i in range(100):
        files = _rotate(SOURCE_FILES, start=i + 300, count=2)
        sec_doc = _rotate(DOC_FILES, start=i, count=1)[0]
        step(
            _make_msg("Audit", os.path.basename(sec_doc),
                      f"Read {os.path.basename(sec_doc)} and check {os.path.basename(files[0])} "
                      f"and {os.path.basename(files[1])} against the security guidelines. "
                      "List any violations found."),
            reads=files + [sec_doc], writes=[],
            clean=files + [sec_doc],
        )

    return Workload("analysis", steps, initial_messages=system_msg, repo_root=REPO)


# ── Workload 4: Daily Code Review (review) ──────────────────

def _count_tokens(path: str) -> int:
    """Rough token estimate of a file's content (chars/4)."""
    full_path = os.path.join(REPO, path)
    try:
        with open(full_path) as f:
            return len(f.read()) // 4
    except FileNotFoundError:
        return 2000  # fallback


def workload_review(seed: int = 42) -> Workload:
    """Daily code review — same core files every turn across multiple sessions.

    Simulates a developer who opens the same key modules repeatedly (e.g.
    daily standup prep, code review, bug triage). Maximizes ToolRecall's
    advantage: repeated reads of the same files with no writes.

    Structure:
      - Phase 1: 50 turns reading CORE_FILES (same files every turn)
        → ToolRecall: cached_read hits after first read, drops clean content
        → Naive: re-reads and re-sends full file content every turn
      - Phase 2: 50 turns reading CORE_FILES + occasional reference docs
        → Still heavy overlap, tests context dropping with some unique content
      - Phase 3: 50 turns of CORE_FILES + light edits
        → Writes invalidate clean status — tests dirty tracking
      - Phase 4: 50 turns of pure CORE_FILES reads again
        → After edits in Phase 3, files are dirty — tests re-caching

    Total turns: 200
    """
    import random
    rng = random.Random(seed + 3000)
    turns = 0
    steps = []

    def step(msg, reads, writes, clean=None):
        nonlocal turns
        turns += 1
        steps.append(WorkloadStep(
            message=msg, reads=reads, writes=writes,
            clean_files=clean or [],
            check=f"review {os.path.basename(reads[0]) if reads else 'file'}",
        ))

    system_msg = [{"role": "system",
                   "content": "You are a senior developer doing a daily code review of a Python project."}]

    # Resolve core file paths
    core_paths = [os.path.join(REPO, f) for f in CORE_FILES]

    # Phase 1: Pure repeated reads (50 turns)
    # Every turn reads the same 4 core files. No writes.
    # ToolRecall: after turn 1, all content is cached + clean → dropped from conversation
    for i in range(50):
        variation = i % 4  # rotate the focus question
        questions = [
            "Review each file for potential race conditions in async code. Note any unprotected shared state.",
            "Check all files for missing type annotations on public APIs. List function signatures that need fixing.",
            "Audit error handling patterns. Which functions swallow exceptions without logging?",
            "Review docstring coverage. Which public functions lack documentation?",
        ]
        step(
            _make_msg("Daily review", f"core ({CORE_FILES[variation]})",
                      questions[variation]),
            reads=core_paths, writes=[],
            clean=core_paths,  # all clean — only read, never written
        )

    # Phase 2: Core + reference docs (50 turns)
    # Still reads core files every turn, plus one reference doc per turn
    for i in range(50):
        ref_file = _rotate(DOC_FILES, start=i, count=1)[0]
        question = f"Read the core modules and {os.path.basename(ref_file)}. How does the architecture doc align with the current implementation?"
        step(
            _make_msg("Architecture review", os.path.basename(ref_file), question),
            reads=core_paths + [ref_file], writes=[],
            clean=core_paths + [ref_file],
        )

    # Phase 3: Core + writes (50 turns)
    # Each turn reads all core files, makes a small change to ONE file
    for i in range(50):
        target = core_paths[i % len(core_paths)]
        basename = os.path.basename(target)
        edits = [
            f"Add a TODO comment at the top of {basename} noting the need for async-safe patterns.",
            f"Add a debug log entry at the start of the main function in {basename}.",
            f"Add a type: ignore comment to the first type annotation in {basename}.",
            f"Add a FIXME comment in {basename} marking the error handling section for refactoring.",
        ]
        step(
            _make_msg("Edit", basename, edits[i % len(edits)]),
            reads=core_paths, writes=[target],
            clean=core_paths,  # read all, but one is dirty from write
        )

    # Phase 4: Pure repeated reads again (50 turns)
    for i in range(50):
        variation = i % 4
        questions = [
            "Review all modules for compatibility with Python 3.12 features. Any deprecated APIs in use?",
            "Check import organization. Are there circular import risks between these modules?",
            "Audit logging consistency. Do all modules log at appropriate levels?",
            "Review test coverage. Which edge cases are missing from the current tests?",
        ]
        step(
            _make_msg("Final review", f"core ({CORE_FILES[variation]})",
                      questions[variation]),
            reads=core_paths, writes=[],
            clean=core_paths,
        )

    return Workload("review", steps, initial_messages=system_msg, repo_root=REPO)


# ── Registry ────────────────────────────────────────────────

WORKLOADS = {
    "bugfix": workload_bugfix,
    "feature": workload_feature,
    "analysis": workload_analysis,
    "review": workload_review,
}

# ── Runner convenience ─────────────────────────────────────

def load_workload(name: str, seed: int = 42) -> Workload:
    if name in WORKLOADS:
        return WORKLOADS[name](seed=seed)
    raise ValueError(f"Unknown workload: {name}. Choices: {list(WORKLOADS.keys())}")


if __name__ == "__main__":
    for name, fn in WORKLOADS.items():
        w = fn()
        print(f"{name:12s}: {w.total_turns():>4} turns, {len(w.initial_messages())} system msgs")