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
