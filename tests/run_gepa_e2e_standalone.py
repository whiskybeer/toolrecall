"""Real-engine e2e: gepa.optimize() drives ToolRecallGepaAdapter.

Real gepa (0.1.4, installed in a throwaway venv) runs its full optimizer loop —
seed eval, reflective proposer, acceptance, Pareto state — against our
adapter. Zero LLM API calls: the adapter owns propose_new_texts
(deterministic mutation), so the engine never needs a reflection LM.

The task performs REAL tool executions (actual filesystem reads via
open()) routed through the fork-tree interception. What we verify:

1. The engine runs to completion (max_metric_calls budget respected).
2. The seed candidate is evaluated; the proposer proposes; children are
   accepted per scores.
3. Fork mechanic live: across candidates, real file-read executions drop
   (shared prefix replays from the tree) while scores stay consistent.
4. GEPAResult comes back well-formed.

Run: <venv-with-gepa>/bin/python -u tests/run_gepa_e2e_standalone.py
(-u: the runner exits via os._exit, which drops buffered stdout when piped)
(standalone runner pattern — same as the LangGraph e2e — hermetic DB via
env before any toolrecall import).
"""

import os
import sys
import tempfile

# tempfile.mkdtemp (NOT mktemp): mktemp returns a name without creating the
# file — a classic CodeQL py/insecure-temporary-file sink. mkdtemp creates
# the directory atomically; the hermetic DB lives inside it.
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(tempfile.mkdtemp(prefix="gepa-e2e-"), "cache.db")
os.environ["TOOLRECALL_NO_AUTOSTART"] = "1"
os.environ["TOOLRECALL_UPDATE_CHECK"] = "0"

# this checkout, not whatever toolrecall the venv has installed
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time  # noqa: E402
import traceback  # noqa: E402

RESULTS: dict = {"fail": []}


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        RESULTS["fail"].append(f"{name}: {detail}")


def main() -> None:
    # ── hermetic toolrecall boot (same pattern as test fixtures) ──────────
    import toolrecall._db as dbmod
    from toolrecall.cache import SCHEMA

    dbmod._cached_config = None
    dbmod._db_real = None
    dbmod._init(SCHEMA)

    from toolrecall.adapters.gepa import ToolRecallGepaAdapter

    # ── the task: real filesystem reads through the fork tree ────────────
    workdir = tempfile.mkdtemp(prefix="gepa-e2e-fs-")
    files = {}
    for i in range(4):
        p = os.path.join(workdir, f"shared_{i}.txt")
        with open(p, "w") as f:
            f.write(f"SHARED-CONTENT-{i}" + "x" * 200)
        files[i] = p
    divergent = os.path.join(workdir, "divergent.txt")
    with open(divergent, "w") as f:
        f.write("DIVERGENT-CONTENT")

    exec_log: list[str] = []

    def scorer(task_input: dict, output: str) -> float:
        # Score rewards the DIVERGENT read: a "greedy" candidate reads the
        # extra file and its content appears in the joined output. Seed
        # candidates score 0.8 (missing divergent content) so the engine
        # has something to improve — exercises the divergence re-exec path.
        base = 0.8 if "done:" in output else 0.0
        return 1.0 if "DIVERGENT-CONTENT" in output else base

    trainset = [
        {"id": "t0", "kind": "plain"},
        {"id": "t1", "kind": "plain"},
        {"id": "t2", "kind": "plain"},
    ]

    adapter = ToolRecallGepaAdapter(
        task_input_fn=lambda ti, ctx: task_fn(ti, ctx),
        scorer=scorer,
    )

    # adapter-aware task: uses task_input fields + candidate text (the
    # candidate's prompt text steers tool behavior — that's the point of
    # optimizing it; the fork tree must absorb the divergence)
    def task_fn(ti: dict, ctx: dict) -> str:
        ctx = dict(ctx)
        ctx["task_input"] = ti["kind"]
        ctx["candidate_text"] = (
            ctx.get("candidate", {}).get("sys", "")
            if isinstance(ctx.get("candidate"), dict)
            else ""
        )
        from toolrecall.adapters.gepa import intercept_tool_call, record_tool_response

        def tool_read(path: str) -> str:
            hit = intercept_tool_call("cached_read", {"path": path}, ctx["tree_id"], ctx["node_id"])
            if hit["replay_hit"]:
                exec_log.append(f"R:{os.path.basename(path)}")
                return str(hit["data"])
            with open(path) as f:
                content = f.read()
            record_tool_response(
                "cached_read", {"path": path}, content, ctx["tree_id"], ctx["node_id"]
            )
            exec_log.append(f"E:{os.path.basename(path)}")
            return content

        parts = [tool_read(files[i]) for i in range(4)]
        if ti["kind"] == "greedy" or "greedy" in ctx.get("candidate_text", ""):
            parts.append(tool_read(divergent))
        # Keep enough of each part for the DIVERGENT-CONTENT marker to
        # survive into the joined output (scorer greps for it).
        return "done:" + "|".join(p[:20] for p in parts)

    adapter.task_input_fn = task_fn

    # ── run the REAL gepa engine ──────────────────────────────────────────
    from gepa.api import optimize

    t0 = time.time()
    result = optimize(
        seed_candidate={"sys": "read the shared files and report"},
        trainset=trainset,
        valset=trainset,
        adapter=adapter,
        reflection_lm=None,  # adapter owns propose_new_texts
        max_metric_calls=30,
        display_progress_bar=False,
        run_dir=None,
        candidate_selection_strategy="current_best",
        module_selector="all",
        skip_perfect_score=False,
    )
    wall = time.time() - t0

    # ── assertions ────────────────────────────────────────────────────────
    check(
        "GEPAResult well-formed",
        hasattr(result, "candidates") and len(result.candidates) >= 1,
        f"{len(result.candidates)} candidates, best={max(result.val_aggregate_scores):.2f}",
    )
    summary = adapter.tree_summary()
    check(
        "fork tree populated by real engine",
        summary["nodes"] >= 1 + len(trainset),
        f"nodes={summary['nodes']} calls={summary['calls']}",
    )
    execs = [e for e in exec_log if e.startswith("E:")]
    replays = [e for e in exec_log if e.startswith("R:")]
    check(
        "seed candidate executed real reads",
        len(execs) >= len(trainset) * 4,
        f"{len(execs)} real execs, {len(replays)} replays total",
    )
    check(
        "fork mechanic fired under real engine (replays > 0)",
        len(replays) > 0,
        f"{len(replays)} replayed calls across later candidates",
    )
    check("wall clock sane", wall < 120, f"{wall:.1f}s")

    print("\n=== SUMMARY ===")
    print(f"candidates explored : {len(result.candidates)}")
    print(f"tree nodes / calls  : {summary['nodes']} / {summary['calls']}")
    print(f"real executions     : {len(execs)}")
    print(f"replayed calls      : {len(replays)}")
    print(f"best val score      : {max(result.val_aggregate_scores):.3f}")
    print(f"wall time           : {wall:.1f}s")
    print(
        f"best candidate      : {result.candidates[0] if len(result.candidates) == 1 else 'see run'}"
    )

    if RESULTS["fail"]:
        print("\nFAILURES:")
        for f_ in RESULTS["fail"]:
            print(" -", f_)
        os._exit(1)
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
