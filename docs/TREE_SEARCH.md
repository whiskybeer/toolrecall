# Tree Search over Agent Executions — design note

> **Status: design + one shipped primitive. Everything numeric here is MODELED —
> no billed A/B exists yet, so nothing on this page is a benchmark claim.**
> The fork/resume primitive this note builds on IS shipped and real-graph
> verified: see [langgraph-adapter.md](langgraph-adapter.md) (5/5 scenarios,
> including time-travel rewind with the tool executed exactly once).

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

**What this is NOT:** a claim that any of these numbers has been measured.
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

**Per-branch session isolation** (prereq for Phase 2): the Context Tracker
and replay mode are daemon-global singletons today; concurrent branches
cross-contaminate dirty/clean sets. Branch scoping keys on the existing
per-request client concept.

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

## What NOT to claim (until the billed A/B exists)

- No measured fork-vs-re-exploration savings number of any kind
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
