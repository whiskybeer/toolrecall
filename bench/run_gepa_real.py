#!/usr/bin/env python3
# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
"""run_gepa_real.py — billed A/B: the REAL gepa.optimize() loop with an
LLM proposer, a tool-calling agent, and real tool executions.

================================================================================
What this measures (and what the synthetic fork A/B could not)
================================================================================

bench/run_gepa_fork_ab.py measured the fork mechanic on simulated mutations
with max_tokens=5. This bench runs the optimizer as a user would:

- gepa.optimize() with GEPA's own LLM reflection proposer (the adapter's
  deterministic propose_new_texts is switched off).
- A tool-calling agent (OpenAI tools API) answering code-navigation
  questions about a PINNED git revision of this repo. Tools: grep,
  read_file, list_dir — all served by `git` at that revision, so every
  call is pure in (tool, args) and replay is correct by construction.
- Real completions (max_tokens=400) and a real scorer, so the best
  validation score is a task-quality signal, not noise. The scorer wants a
  final `ANSWER: <path>:<line> <parameter>` line (1/3 per field); the
  question does not state that format, the feedback does — so the seed
  prompt scores low and GEPA has something to learn.
- Task calls run with reasoning disabled, reflection with reasoning effort
  "low" and a large max_tokens: on reasoning models, hidden reasoning can
  otherwise use the whole budget and return empty content.

Arms (same model, same data, same GEPA seed and budget)
-------------------------------------------------------
  default     GEPA defaults (cache_evaluation=False). Tools execute, LLM
              calls go straight to OpenRouter (provider prefix caching
              applies as it would for any user).
  gepa-cache  GEPA's own evaluation cache ON (cache_evaluation=True): a
              repeated (candidate, example) pair is not re-run. This is
              the FAIR baseline — the saving any GEPA user gets for free.
  toolrecall  gepa-cache + ToolRecallGepaAdapter (tool calls through the
              replay fork tree) + task-LM calls through the ToolRecall
              forward proxy (hermetic rig, see bench/gepa_real_rig.sh).

Reflection-LM calls go straight to OpenRouter in every arm and are billed
separately (they are unique prompts; no layer can serve them).

Per arm we record: task-LM calls / billed $ / proxy HITs (with the replayed
cost as cost_avoided, billed $0), tool calls requested vs really executed,
reflection calls / $, metric calls, candidates, best validation score,
wall time. Every request row lands in the JSONL.

Where the toolrecall arm can save
---------------------------------
GEPA re-evaluates the parent candidate on every iteration's minibatch with
capture_traces=True, and its evaluation cache is written there but never
read (gepa 0.1.4, reflective_mutation.py). Identical (prompt, example)
rollouts therefore re-run even with cache_evaluation=True: the proxy serves
their LLM calls and the fork tree their tool calls. Across DIFFERENT
prompts every LLM body differs, so only tool calls can be replayed there.

Honesty limits
--------------
- The tools here are cheap (git reads, ~ms). The tool axis is reported as
  COUNTS of executions avoided; what an avoided execution is worth depends
  on the real tool (API $, rate limits, side effects), not on this bench.
- LLM sampling is not byte-deterministic even at temperature 0, so arms
  explore different trajectories: the per-arm totals carry run-to-run
  variance. Run several seeds before quoting a difference.
- Single OpenRouter key unless KEY_TR differs from KEY: per-request
  usage.cost is the attribution; no dashboard cross-check.

Usage
-----
    bash bench/gepa_real_rig.sh            # hermetic daemon + proxy + edge
    KEY=sk-or-... <venv-with-gepa>/bin/python -u bench/run_gepa_real.py --plan
    KEY=sk-or-... <venv-with-gepa>/bin/python -u bench/run_gepa_real.py --execute --seed 0

Outputs (never overwritten): bench/results/gepa_real_<model>_s<seed>_<UTC>.{json,jsonl}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("BENCH_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The toolrecall arm imports the adapter; its DB must be a bench DB before
# any toolrecall import (replay trees live in TOOLRECALL_CACHE_DB).
_BENCH_DIR = os.environ.get("BENCH_DIR") or tempfile.mkdtemp(prefix="gepa_real_")
os.makedirs(_BENCH_DIR, exist_ok=True)
os.environ.setdefault("TOOLRECALL_CACHE_DB", os.path.join(_BENCH_DIR, "adapter.db"))
os.environ.setdefault("TOOLRECALL_NO_AUTOSTART", "1")
os.environ.setdefault("TOOLRECALL_UPDATE_CHECK", "0")
sys.path.insert(0, REPO)

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_EDGE = "http://127.0.0.1:8583/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
# Corpus: a pinned revision, so every tool result is a pure function of args.
DEFAULT_REV = "v0.8.21"

MAX_STEPS = 6
MAX_TOKENS = 400
GREP_MAX_LINES = 25
READ_MAX_LINES = 80

SEED_PROMPT = "You are a helpful assistant. Answer the question."

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search the repository for a regular expression. "
            "Returns up to 25 matching lines as path:line:text.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read up to 80 lines of a file, starting at start_line (1-based).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries of a directory ('' = repository root).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]


# ─── corpus: pure tools over a pinned git revision ─────────────────────────


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else ""


class Corpus:
    def __init__(self, rev: str):
        self.rev = _git("rev-parse", rev).strip()
        if not self.rev:
            raise SystemExit(f"unknown revision {rev!r}")
        self.executions = 0  # real tool executions (all arms count here)

    def run(self, name: str, args: dict) -> str:
        self.executions += 1
        if name == "grep":
            out = _git("grep", "-n", "-I", "-E", str(args.get("pattern", "")), self.rev)
            lines = [ln.split(":", 1)[1] for ln in out.splitlines() if ":" in ln]
            body = "\n".join(lines[:GREP_MAX_LINES])
            more = len(lines) - GREP_MAX_LINES
            return (body + (f"\n... {more} more" if more > 0 else "")) or "no matches"
        if name == "read_file":
            path = str(args.get("path", "")).lstrip("./")
            text = _git("show", f"{self.rev}:{path}")
            if not text:
                return f"error: no such file {path}"
            start = max(1, int(args.get("start_line") or 1))
            lines = text.splitlines()[start - 1 : start - 1 + READ_MAX_LINES]
            return "\n".join(f"{start + i}: {ln}" for i, ln in enumerate(lines))
        if name == "list_dir":
            path = str(args.get("path", "")).strip("/")
            spec = f"{self.rev}:{path}" if path else self.rev
            out = _git("ls-tree", "--name-only", spec)
            return out or f"error: no such directory {path}"
        return f"error: unknown tool {name}"

    def questions(self, n: int, seed: int) -> list[dict]:
        """Code-navigation questions with exact answers, from the corpus."""
        out = _git("grep", "-n", "-E", r"^def [a-z][a-z0-9_]+\(", self.rev, "--", "toolrecall/*.py")
        defs: dict[str, list] = {}
        for ln in out.splitlines():
            _, path, lineno, text = ln.split(":", 3)
            m = re.match(r"def ([a-z][a-z0-9_]+)\(\s*([a-z_][a-z0-9_]*)?", text)
            if m and m.group(2) and m.group(2) not in ("self", "cls"):
                defs.setdefault(m.group(1), []).append((path, int(lineno), m.group(2)))
        unique = sorted((k, v[0]) for k, v in defs.items() if len(v) == 1 and len(k) > 8)
        rng = random.Random(seed)
        picks = rng.sample(unique, n)
        return [
            {
                "id": f"q{i}",
                "question": f"Where is the function `{fn}` defined, and what is its first parameter?",
                "path": path,
                "line": line,
                "param": param,
            }
            for i, (fn, (path, line, param)) in enumerate(picks)
        ]


ANSWER_FORMAT = "ANSWER: <repo-relative path>:<line> <first parameter name>"


def score_answer(item: dict, output: dict) -> tuple[float, str]:
    """1/3 each for path, definition line, first parameter — graded only from
    a final ``ANSWER: path:line param`` line. The format lives here and in the
    feedback, not in the question: learning it is the optimizer's job."""
    text = str(output.get("answer", "")) if isinstance(output, dict) else ""
    expected = f"{item['path']}:{item['line']} {item['param']}"
    m = re.findall(r"ANSWER:\s*`?([^\s:`]+):(\d+)`?\s+`?([A-Za-z_][A-Za-z0-9_]*)", text)
    if not m:
        return (
            0.0,
            f"Missing the required final line '{ANSWER_FORMAT}'. Expected: ANSWER: {expected}",
        )
    path, line, param = m[-1]
    path = path.lstrip("./")
    parts = {
        "path": path == item["path"],
        "line": int(line) == item["line"],
        "parameter": param == item["param"],
    }
    s = round(sum(parts.values()) / 3, 4)
    if s == 1.0:
        return s, "Correct."
    wrong = ", ".join(k for k, ok in parts.items() if not ok)
    return s, f"Wrong {wrong}. Got: ANSWER: {path}:{line} {param}. Expected: ANSWER: {expected}"


# ─── LLM transport ─────────────────────────────────────────────────────────


class Ledger:
    """Every billed (or replayed) request, one JSONL row each."""

    def __init__(self, arm: str, model: str, cost_cap: float):
        self.arm = arm
        self.model = model
        self.cost_cap = cost_cap
        self.rows: list[dict] = []

    def billed(self, kind: str | None = None) -> float:
        return sum(r["billed_cost"] for r in self.rows if kind is None or r["kind"] == kind)

    def post(self, url: str, key: str, body: dict, kind: str, tag: str) -> dict:
        if self.billed() > self.cost_cap:
            raise RuntimeError(f"cost cap ${self.cost_cap:.2f} reached on arm {self.arm}")
        raw = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=raw,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        t0 = time.perf_counter()
        row = {
            "arm": self.arm,
            "kind": kind,
            "tag": tag,
            "body_sha": hashlib.sha256(raw).hexdigest()[:16],
            "cache": None,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "total_cost": 0.0,
            "billed_cost": 0.0,
            "cost_avoided": 0.0,
            "error": None,
        }
        data: dict = {}
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read().decode())
                    row["cache"] = resp.headers.get("X-ToolRecall-Cache")
                row["error"] = None
                break
            except urllib.error.HTTPError as e:
                row["error"] = f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
            time.sleep(2 ** (attempt + 1))
        row["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        usage = data.get("usage") or {}
        row["prompt_tokens"] = usage.get("prompt_tokens") or 0
        row["completion_tokens"] = usage.get("completion_tokens") or 0
        row["cached_tokens"] = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        cost = float(usage.get("cost") or 0.0)
        row["total_cost"] = cost
        # A HIT replays the original body verbatim, usage.cost included: it
        # was billed once (on the MISS), so the replay bills $0.
        if row["cache"] == "HIT":
            row["cost_avoided"] = cost
        else:
            row["billed_cost"] = cost
        self.rows.append(row)
        if row["error"]:
            raise RuntimeError(row["error"])
        return data


# ─── the agent ─────────────────────────────────────────────────────────────


def run_agent(item, system_prompt, call_tool, llm, model, seed) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": item["question"]},
    ]
    tool_calls = 0
    for step in range(MAX_STEPS):
        body = {
            "model": model,
            "messages": messages,
            "tools": TOOLS,
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "seed": seed,
            "reasoning": {"enabled": False},
        }
        data = llm(body, f"{item['id']}/step{step}")
        msg = ((data.get("choices") or [{}])[0]).get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"answer": msg.get("content") or "", "steps": step + 1, "tool_calls": tool_calls}
        messages.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        )
        for c in calls:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls += 1
            result = call_tool(fn.get("name", ""), args)
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": result})
    return {"answer": "", "steps": MAX_STEPS, "tool_calls": tool_calls, "error": "step limit"}


def reflective_dataset(rollouts, components):
    """Shared by every arm: GEPA's reflection sees the same feedback shape."""
    records = [
        {
            "Inputs": {"question": r["item"]["question"]},
            "Generated Outputs": {"answer": str(r["output"].get("answer", ""))[-600:]},
            "Feedback": r["feedback"],
        }
        for r in rollouts
    ]
    return {c: records for c in components}


class PlainAdapter:
    """default / gepa-cache arms: every tool call executes."""

    propose_new_texts = None

    def __init__(self, corpus, llm, model, seed):
        self.corpus, self.llm, self.model, self.seed = corpus, llm, model, seed
        self.tool_requests = 0
        self._last: list = []

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa.core.adapter import EvaluationBatch

        outs, scores, rollouts = [], [], []
        for item in batch:

            def call_tool(name, args):
                self.tool_requests += 1
                return self.corpus.run(name, args)

            try:
                out = run_agent(
                    item, candidate["system_prompt"], call_tool, self.llm, self.model, self.seed
                )
            except Exception as e:  # noqa: BLE001 — failed rollout scores 0
                out = {"answer": "", "error": str(e)}
            s, fb = score_answer(item, out)
            outs.append(out)
            scores.append(s)
            rollouts.append({"item": item, "output": out, "feedback": fb})
        self._last = rollouts
        return EvaluationBatch(
            outputs=outs, scores=scores, trajectories=rollouts if capture_traces else None
        )

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        return reflective_dataset(eval_batch.trajectories or self._last, components_to_update)


def make_toolrecall_adapter(corpus, llm, model, seed):
    from toolrecall.adapters.gepa import (
        ToolRecallGepaAdapter,
        intercept_tool_call,
        record_tool_response,
    )

    counters = {"tool_requests": 0, "replayed": 0}

    def task(item, ctx):
        def call_tool(name, args):
            counters["tool_requests"] += 1
            hit = intercept_tool_call(name, args, ctx["tree_id"], ctx["node_id"])
            if hit["replay_hit"]:
                counters["replayed"] += 1
                return hit["data"]
            result = corpus.run(name, args)
            record_tool_response(name, args, result, ctx["tree_id"], ctx["node_id"])
            return result

        return run_agent(item, ctx["candidate"]["system_prompt"], call_tool, llm, model, seed)

    def scorer(item, out):
        return score_answer(item, out)[0]

    class Adapter(ToolRecallGepaAdapter):
        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            rollouts = []
            for r in eval_batch.get("rollouts") or self._last_results:
                item = r.task_input
                rollouts.append(
                    {"item": item, "output": r.output, "feedback": score_answer(item, r.output)[1]}
                )
            return reflective_dataset(rollouts, components_to_update)

    adapter = Adapter(task_input_fn=task, scorer=scorer, branch_prefix="real")
    adapter.propose_new_texts = None  # GEPA's LLM reflection, not the e2e stub
    return adapter, counters


# ─── one arm ───────────────────────────────────────────────────────────────


def run_arm(arm, args, corpus, train, val, key) -> dict:
    import gepa

    ledger = Ledger(arm, args.model, args.cost_cap)
    task_url = args.edge if arm == "toolrecall" else OPENROUTER

    def llm(body, tag):
        return ledger.post(task_url, key, body, "task", tag)

    def reflection_lm(prompt):
        msgs = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
        data = ledger.post(
            OPENROUTER,
            key,
            {
                "model": args.model,
                "messages": msgs,
                # reasoning models spend hidden tokens first; 1500 left GEPA's
                # long reflection prompt with empty content
                "max_tokens": 6000,
                "temperature": 0.7,
                "reasoning": {"effort": "low"},
            },
            "reflection",
            "reflect",
        )
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    execs_before = corpus.executions
    if arm == "toolrecall":
        adapter, counters = make_toolrecall_adapter(corpus, llm, args.model, args.seed)
    else:
        adapter, counters = PlainAdapter(corpus, llm, args.model, args.seed), None

    t0 = time.time()
    result = gepa.optimize(
        seed_candidate={"system_prompt": SEED_PROMPT},
        trainset=train,
        valset=val,
        adapter=adapter,
        reflection_lm=reflection_lm,
        max_metric_calls=args.budget,
        reflection_minibatch_size=3,
        cache_evaluation=(arm != "default"),
        seed=args.seed,
        raise_on_exception=False,
        display_progress_bar=False,
    )
    wall = time.time() - t0

    tool_requests = counters["tool_requests"] if counters else adapter.tool_requests
    task_rows = [r for r in ledger.rows if r["kind"] == "task"]
    best = max(range(len(result.candidates)), key=lambda i: result.val_aggregate_scores[i])
    summary = {
        "arm": arm,
        "model": args.model,
        "seed": args.seed,
        "budget_metric_calls": args.budget,
        "metric_calls_used": getattr(result, "total_metric_calls", None),
        "candidates": len(result.candidates),
        "seed_val_score": round(result.val_aggregate_scores[0], 4),
        "best_val_score": round(result.val_aggregate_scores[best], 4),
        "best_prompt": result.candidates[best]["system_prompt"],
        "task_llm_calls": len(task_rows),
        "task_llm_hits": sum(1 for r in task_rows if r["cache"] == "HIT"),
        "task_billed_usd": round(ledger.billed("task"), 6),
        "task_cost_avoided_usd": round(sum(r["cost_avoided"] for r in task_rows), 6),
        "task_prompt_tokens": sum(r["prompt_tokens"] for r in task_rows),
        "task_cached_tokens": sum(r["cached_tokens"] for r in task_rows),
        "reflection_calls": sum(1 for r in ledger.rows if r["kind"] == "reflection"),
        "reflection_billed_usd": round(ledger.billed("reflection"), 6),
        "total_billed_usd": round(ledger.billed(), 6),
        "tool_calls_requested": tool_requests,
        "tool_executions": corpus.executions - execs_before,
        "tool_calls_replayed": counters["replayed"] if counters else 0,
        "errors": sum(1 for r in ledger.rows if r["error"]),
        "wall_s": round(wall, 1),
    }
    if arm == "toolrecall":
        adapter.cleanup()
    return {"summary": summary, "rows": ledger.rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=os.environ.get("BENCH_MODEL", DEFAULT_MODEL))
    ap.add_argument("--rev", default=DEFAULT_REV)
    ap.add_argument("--edge", default=DEFAULT_EDGE)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--questions", type=int, default=12, help="half train, half val")
    ap.add_argument("--budget", type=int, default=40, help="GEPA max_metric_calls per arm")
    ap.add_argument("--arms", default="default,gepa-cache,toolrecall")
    ap.add_argument("--cost-cap", type=float, default=0.40, help="USD abort per arm")
    ap.add_argument("--out", default=None)
    ap.add_argument("--plan", action="store_true", help="print the plan, spend nothing")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    corpus = Corpus(args.rev)
    qs = corpus.questions(args.questions, seed=1000 + args.seed)
    half = len(qs) // 2
    train, val = qs[:half], qs[half:]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    print(f"corpus {args.rev} @ {corpus.rev[:10]} · model {args.model} · seed {args.seed}")
    print(f"{len(train)} train / {len(val)} val questions · budget {args.budget} metric calls/arm")
    print(f"arms {arms} · cost cap ${args.cost_cap:.2f}/arm · bench dir {_BENCH_DIR}")
    for q in qs[:3]:
        print(f"  e.g. {q['path']} {q['param']} ← {q['question'][:90]}…")
    if args.plan or not args.execute:
        print("PLAN ONLY — re-run with --execute to spend.")
        return 0

    key = os.environ.get("KEY", "").strip()
    if not key:
        raise SystemExit("KEY (OpenRouter) not set")

    results = []
    rows: list = []
    for arm in arms:
        print(f"\n=== arm {arm} ===", flush=True)
        r = run_arm(arm, args, corpus, train, val, key)
        print(json.dumps(r["summary"], indent=2), flush=True)
        results.append(r["summary"])
        rows.extend(r["rows"])

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = args.model.split("/")[-1]
    base = args.out or os.path.join(
        REPO, "bench", "results", f"gepa_real_{slug}_s{args.seed}_{stamp}"
    )
    if os.path.exists(base + ".json"):
        raise SystemExit(f"refusing to overwrite {base}.json")
    with open(base + ".json", "w") as f:
        json.dump(
            {
                "bench": "gepa_real",
                "model": args.model,
                "rev": corpus.rev,
                "seed": args.seed,
                "questions": qs,
                "max_steps": MAX_STEPS,
                "max_tokens": MAX_TOKENS,
                "arms": results,
            },
            f,
            indent=2,
        )
    with open(base + ".jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(f"\nwrote {base}.json / .jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
