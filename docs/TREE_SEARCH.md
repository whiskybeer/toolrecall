# Tree Search over Agent Executions — design note

> **Status: design note + shipped primitives + one billed A/B.** The cost-model
> numbers on this page are MODELED. The replay fork tree and the GEPA adapter
> are shipped (0.8.21; see "What ships today" below). The first billed fork A/B
> is in [`bench/results/GEPA_FORK_AB_RESULTS.md`](../bench/results/GEPA_FORK_AB_RESULTS.md):
> 79% lower billed cost than naive re-exploration on one synthetic workload,
> with 25/25 cache hits byte-exact. Repeats on two models reproduce it at
> 82–83%, but that workload uses scripted mutations. The real `gepa.optimize()`
> loop, with LLM-proposed prompts, a tool-calling agent and a real scorer, is in
> [`bench/results/GEPA_REAL_RESULTS.md`](../bench/results/GEPA_REAL_RESULTS.md):
> with both arms replaying one GEPA run's exact evaluation schedule (equal
> work), agent LLM cost fell 45–78% on deepseek and 21–72% on mistral (6 runs),
> and 25–59% of tool calls were replayed, with no loss in rollout score.
> The LangGraph fork/resume primitive is real-graph verified: see
> [langgraph-adapter.md](langgraph-adapter.md) (5/5 scenarios, including
> time-travel rewind with the tool executed exactly once).

## The use case

Mutation-loop tree search over agent executions: run an agent to a
checkpoint, apply a small variation (a "mutation" — different tool order,
different parameter, different prompt branch), and re-explore **from exactly
that checkpoint** instead of starting from zero. Repeat across pathways to
find the optimal route to a solution.

This is how prompt optimizers (GEPA/DSPy), agent RL rollouts, eval harnesses
(N-pass scoring), and any "try B variants and keep the best" workflow already
think. Today every one of them re-pays the shared prefix — in tokens, in
attention compute, and in real tool executions — once per branch.

## The cost model (modeled — see status banner)

Three separable axes for B branches diverging d steps after a shared prefix C:

| Axis | Naive re-exploration | Fork from checkpoint | Mechanism |
|---|---|---|---|
| **Billed input tokens** | `B·k·(C+d)²/2` | `k·C²/2·[1+(B−1)·δ] + B·k·d²/2`, δ≈0.1 | Provider prefix cache re-prices the shared bytes at cache-read rate; only divergence bills full |
| **Attention pairs** (decode compute, context wall) | `Σ_{t=1}^{C+d}(t·k)²` per branch — dies at the wall | Branch-local context ≈ `d·k` after clean-file drops | Context Tracker bounds each branch (the README's modeled 90% table is exactly this workload) |
| **Real tool executions** | `B·(C+d)` calls (API $, rate limits, side effects, flakiness) | `C + B·d` | Replay serves the shared prefix from record at $0; only mutation steps execute |

All three ratios improve simultaneously; the multiplication is the point.

**What this is NOT:** a claim that any of these formulas has been measured.
The one billed A/B (status banner) measured the billed-dollar axis on a
single synthetic workload; the attention and tool-execution axes are unmeasured.
The README's 9.5×/7.4× Context Tracker figures remain the only measured
endurance results; the "idealized re-read-heavy agent" behind the modeled 90%
ceiling is realized concretely by the search workload described here — which
is why that table lives next to this note, not as a standalone claim.

## Why this layer, why now (prior-art gap)

| Layer | Existing solution | What it misses |
|---|---|---|
| Serving engine (self-hosted) | RadixAttention/SGLang KV prefix reuse | Owns KV/billing only for self-hosted models; nothing for tool re-execution or agent context growth |
| Agent framework | LangGraph checkpointing / time travel | Rewind is a **single rewind point**; resuming **re-executes tools**; no tool-result record |
| Workflow engines | Temporal event-sourced replay | Workflow-level, not agent-loop-level; no workspace restore |
| Research | ACRFence (replay-vs-fork semantics at tool boundaries) | Semantics analysis, no shipped cost mechanism |

**Nobody ships: deterministic tool-record + fork-from-node + workspace
restore for API-model agents.** ToolRecall already owns the ingredients:
tool-call-granular recording (`ReplayManager`), canonicalization (stable keys
across non-deterministic harnesses), a cache with per-key TTL, the Context
Tracker (branch-context bounding), and one shipped fork primitive.

## What ships today (Phase 1 — done)

The [LangGraph adapter](langgraph-adapter.md) provides the harness-level
fork/resume primitive, verified against real langgraph 1.2.11:

- `get_tuple(checkpoint_id=...)` restores an exact node in a thread's history
- Rewind + re-run works, and `@cached_tool` serves repeated node work from
  cache — a replayed branch paid zero tool executions for its shared prefix
- Checkpoints survive restarts; parent chains walkable; subgraph namespaces
  scoped per `<node>:<task_id>`

Limitations of Phase 1 (honest): fork = same-thread rewind (cross-thread
forking needs state copy), no workspace restore, no branch orchestration,
no per-branch session isolation in the daemon.

Since 0.8.21 the replay layer ships the tool-record half of Phase 2/3:

- **Fork tree** in `toolrecall/replay.py` — `fork_tree`, `fork_node`,
  `record_call_at`, `replay_at`, plus `toolrecall replay tree|fork|at`
  ([REPLAY_MODE.md](REPLAY_MODE.md#fork-trees)). A node replays through its
  ancestor chain; calls are keyed by `(tree_id, node_id)`, so concurrent
  branches are isolated by construction.
- **Scoped replay modes** — `scope=` on the flat record/replay API gives each
  branch its own mode (the per-branch isolation prerequisite below).
- **GEPA adapter** — `toolrecall/adapters/gepa.py`
  ([adapters README](../toolrecall/adapters/README.md#gepa--evaluate-adapter-over-the-replay-fork-tree)),
  e2e-verified against the real `gepa.optimize()` loop.

Still design-only: workspace snapshot/restore and the daemon-side MCP tools.

## The contract (Phase 2/3 — design)

Daemon-side MCP tools (Phase 2):

```
record_step(session_id, tool_name, tool_args, observation,
            meta{tokens, wall_ms, seed}?) -> {node_id, seq}
fork_branch(session_id, node_id) -> {branch_id, base_node_id, snapshot_id}
replay_to_node(session_id, node_id, mode: 'verify'|'dry',
               from_node_id?) -> {events, checksum_ok, diverged_at_seq}
```

Workspace snapshot/restore (Phase 3, adapter-declared capabilities):

```
snapshot_workspace(session_id, branch_id, path?) -> {snapshot_id, bytes, file_count}
restore_workspace(session_id, snapshot_id) -> {restored, mtime_fidelity}
```

Client-side (stays outside the daemon): spawning B branches, mutation
application, scoring, UCB1/Pareto selection, budget accounting.

**Workspace contract** (from the git-worktree research): a worktree is
created from commits — uncommitted/untracked state needs a carry pattern
(stash-apply, temp-branch commit, or tar+hardlink COW for non-git dirs).
mtime restoration is contractual (make/ninja treat every fork as full
invalidation otherwise); adapters that cannot restore mtimes MUST declare
`mtime_fidelity: false`. Explicitly out of scope, enumerated per adapter:
running processes, env vars, network state.

**Per-branch session isolation** (prereq for Phase 2): replay mode is solved
(scoped modes + stateless tree calls, see "What ships today"). The Context
Tracker is still daemon-global, so concurrent branches cross-contaminate
dirty/clean sets. Branch scoping keys on the existing per-request client concept.

## GEPA integration point (first-party caller, Phase 3)

Verified against gepa 0.1.4 source (`gepa/core/adapter.py`): GEPA executes
candidates through an adapter protocol — `evaluate`/`batch_evaluate`
(`BatchEvaluateFn` → `EvaluationBatch` with honest `num_metric_calls`;
default `batch_evaluate` falls back to sequential `evaluate` calls), budget
stopped via `max_metric_calls`. A ToolRecall adapter implements
`batch_evaluate` to run each candidate against a forked, replayed workspace
(shared prefix served from the tool record; only the mutated steps
execute). GEPA keeps candidate selection, acceptance, Pareto bookkeeping,
and budget stopping. This also gives the Recall Tier its first first-party
caller (non-reproducible tool output restored on demand at branch re-entry).

## What NOT to claim

- No general fork-vs-re-exploration savings number. The 79–83% is one synthetic
  workload (C=8 shared turns, d=1 divergent turn, simulated mutations); savings
  scale with C/d and do not transfer to other shapes. The real GEPA loop ranged
  from 21% to 78% per run (equal-work design)
- No task-quality claim: the pass@1 A/B gap from the dedup benchmark is
  open; tree search does not change that
- No comparison against self-hosted RadixAttention stacks on the token axis
  (they win there by construction; ToolRecall's axes are tools + harness
  context, see [AGENT_COMPATIBILITY.md](AGENT_COMPATIBILITY.md))
- No simple-truncation baseline arm yet — same gap as the three-arm
  benchmark; the fork A/B should ship with one from the start

## Verification plan (Phase 4)

Billed 2-key A/B on the existing warp rig: arm A = B mutations re-explored
naively from zero; arm B = same mutations forked from the recorded prefix.
Measure per-axis: billed dollars (provider dashboard, separate keys), turns
to context wall, tool-execution count. Gate: pre-researched execution plan,
named DBs, per-datapoint verification before the full run — same protocol as
every benchmark on this repo.

Status: the synthetic fork A/B
([GEPA_FORK_AB_RESULTS.md](../bench/results/GEPA_FORK_AB_RESULTS.md)) is
repeated 5× on two models at 82–83%, with and without provider prefix caching.
The real GEPA loop ([GEPA_REAL_RESULTS.md](../bench/results/GEPA_REAL_RESULTS.md),
equal-work design) saves 21–78% of agent LLM cost. The saving tracks how often
GEPA re-runs identical parent rollouts, which its own cache does not serve;
across different prompts only tool calls replay. Open: a second key for the dashboard cross-check, paid or slow
tools (the tool axis is measured only as counts), and more task types.
