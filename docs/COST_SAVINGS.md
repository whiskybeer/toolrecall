## "Without and Any Model" — Model-Agnostic Savings

This is the critical point. ToolRecall's cost savings do **not** depend on which model or provider you use:

| Provider | Does prefix caching? | TR benefit |
|----------|-------------------|------------|
| DeepSeek | ✅ Yes (automatic) | Sends fewer total tokens → less input billed, even with a lower cache hit rate |
| OpenAI | ✅ Yes (automatic) | Same logic — volume beats rate |
| Anthropic | ✅ Yes (automatic) | Same logic |
| Gemini | ❌ No (standard tier) | TR sends fewer tokens where prefix caching can't help at all |
| OpenRouter | ✅ Yes (for supported models) | Same logic as upstream provider |

The context tracker works at the **token-count layer** (how many tokens does the agent send per turn?), not the **billing-rate layer** (how much does each token cost after discount?). Since every provider charges by token volume, sending fewer tokens saves money on **any** provider.

## Honest Caveats

- **Stateless agents only.** The context tracker requires the agent to own its message loop (Hermes, Cline, ADK). Agents with built-in context management (Claude Code, Cursor) don't benefit from the tracker — they get the forward proxy only.
- **Workload-dependent.** Sessions that read the same files repeatedly save the most. A session reading 17 unique files once saves less. The numbers above are from controlled benchmarks on real file pools.
- **Completion tokens vary.** On some workloads TR produced +150% more completion tokens (the analysis workload). This could offset some savings and is under investigation.
- **Dollar amounts are from one billing period.** Prices change. The relationships (sending fewer tokens = paying less) are structural.
- **Shared network daemon (A6): not every cache category shares equally.** File-read entries are path-keyed — cross-machine hits are only *correct* on a shared filesystem or identical checkouts (details in ARCHITECTURE.md §3.3 Network Daemon). Terminal entries are cwd-scoped. API-proxy and MCP entries are hash-keyed and benefit fully. When deploying a team daemon, expect the shared hit rate to concentrate in API/MCP categories unless the team shares a filesystem — do not project single-machine file-cache hit rates onto the network deployment.

## Honest Metric Tiers & Non-Replayable Accounting

`toolrecall stats` separates cacheable replay tiers from non-replayable traffic so aggregate hit rates stay accurate:

| Tier / Category | Included in headline `hit_rate`? | Meaning |
|---|---|---|
| `api_cache` | ✅ Yes | Replayable HTTP GET/metadata endpoints (e.g. `/models`) |
| `llm_passthrough` | ❌ No (`non_replayable_by_design`) | Conversational `POST */chat/completions` and `*/messages` turns (unique history per turn; reported alongside `non_replayable` summary in `toolrecall stats`) |
| `file_cache` | ✅ Yes | File reads originating from `agent_tool` |
| `file_cache_internal` | ❌ No (`non_replayable_by_design`) | Internal / shim `ttl=0` serve-through reads (`source != "agent_tool"`) |
| `mcp_dedup` / `mcp_delta` / `incremental_reads` | ❌ No (`measured_tier`) | Wire-verified tokens avoided via stubs, unified diffs, or append-only tail suppression |
| `post_compaction` / `egress_guard` | ❌ No | Compaction-window re-warm counters and outbound credential guard events |

