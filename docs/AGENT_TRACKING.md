# Agent Tracking — Per-Agent Cache Statistics

ToolRecall can optionally track cache hits, misses, and token savings **per agent identity**. This gives you visibility into which agent frameworks benefit most from caching, across machines when Turso sync is enabled.

## How it works

Agent identity is **stats-only metadata** — it is NOT part of cache keys. This means:

- All agents share one cache (agent A warms the cache for agent B)
- Deterministic replay works regardless of which agent_id was recorded
- Existing cache entries with NULL agent_id continue to work unchanged
- Backward compatible — no migration needed

## Setting up

Set the `TOOLRECALL_AGENT_ID` environment variable before starting your agent:

```bash
export TOOLRECALL_AGENT_ID=hermes
# or
export TOOLRECALL_AGENT_ID=claude-code
# or
export TOOLRECALL_AGENT_ID=antigravity
# or any custom identifier
export TOOLRECALL_AGENT_ID=my-dev-machine
```

Or pass it explicitly per call in code:

```python
from toolrecall.client import cached_read
result = cached_read("file.py", agent_id="my-script")
```

Or pass via forward proxy header:

```bash
curl -H "X-ToolRecall-Agent: claude-code" http://127.0.0.1:8569/v1/chat/completions ...
```

## Recommended agent_id values

| Agent framework | Recommended TOOLRECALL_AGENT_ID |
|----------------|-------------------------------|
| Hermes Agent | `hermes` |
| Claude Code | `claude-code` |
| Antigravity | `antigravity` |
| Codex CLI | `codex` |
| Aider | `aider` |
| Cursor | `cursor` |
| Generic shell | `shell` |

## Viewing stats

### Per-agent breakdown

```bash
toolrecall stats --by-agent
```

Shows all agents with their hits, misses, and token savings by category.

### Single agent

```bash
toolrecall stats --agent-id hermes
```

### Agent management

```bash
toolrecall agent ls              # List all known agents
toolrecall agent show hermes     # Detailed per-category stats
toolrecall agent prune hermes    # Delete an agent's stats (keeps cache entries)
```

### JSON output (monitoring / dashboards)

`agent ls` and `agent show` accept `--json` for machine-readable output on stdout.
Counters are integers; `updated_at` is a Unix timestamp (float, seconds).

`toolrecall agent ls --json` — array of per-agent totals, sorted by `agent_id`;
`[]` when no stats are recorded:

```json
[
  {
    "agent_id": "hermes",
    "hits": 42,
    "misses": 7,
    "tokens_saved": 18350,
    "tokens_read_from_disk": 2210,
    "context_tokens_saved": 9100,
    "categories": 3,
    "updated_at": 1793876543.21
  }
]
```

`categories` is the number of cache categories the agent has rows for;
`updated_at` is the latest update across them.

`toolrecall agent show hermes --json` — one object with per-category rows and totals:

```json
{
  "agent_id": "hermes",
  "categories": [
    {
      "category": "file_cache",
      "hits": 40,
      "misses": 5,
      "tokens_saved": 18000,
      "tokens_read_from_disk": 2000,
      "context_tokens_saved": 9100,
      "updated_at": 1793876543.21
    }
  ],
  "totals": {
    "hits": 40,
    "misses": 5,
    "tokens_saved": 18000,
    "tokens_read_from_disk": 2000,
    "context_tokens_saved": 9100
  }
}
```

An unknown agent returns the same shape with `"categories": []` and all-zero
`totals`, so consumers never need a separate "not found" branch. Both commands
exit 0 either way.

## Cross-host aggregation with Turso

When Turso Cloud sync is enabled, `agent_cache_stats` is a regular table in the synced database. You can query across all machines:

```sql
-- Which agent saves the most tokens across all hosts?
SELECT agent_id, SUM(tokens_saved) AS total_saved
FROM agent_cache_stats
GROUP BY agent_id
ORDER BY total_saved DESC;

-- Hit rate per agent per category
SELECT agent_id, category,
       CAST(hits AS REAL) / NULLIF(hits + misses, 0) AS hit_rate
FROM agent_cache_stats
ORDER BY hit_rate DESC;
```

## Schema

```sql
CREATE TABLE IF NOT EXISTS agent_cache_stats (
    agent_id TEXT NOT NULL,
    category TEXT NOT NULL,
    hits INTEGER DEFAULT 0,
    misses INTEGER DEFAULT 0,
    tokens_read_from_disk INTEGER DEFAULT 0,
    tokens_saved INTEGER DEFAULT 0,
    context_tokens_saved INTEGER DEFAULT 0,
    updated_at REAL DEFAULT 0,
    PRIMARY KEY (agent_id, category)
);
```

The `access_log` table also has an `agent_id TEXT` column (nullable) for per-operation traces.

## Principles

| Principle | How agent tracking respects it |
|-----------|-------------------------------|
| **Local** | Agent stats stay in the local DB until sync is explicitly triple-opted-in |
| **Secure** | Agent_id doesn't bypass security — all path/terminal checks apply regardless |
| **Agnostic** | Agent_id is a free-form string, set via env var, works with any framework |
| **0-deps** | Pure SQLite schema additions — zero new Python dependencies |
| **Deterministic** | Agent_id is excluded from ALL cache keys — same input = same key |
| **Replayable** | Replay operates on cache keys, not agent_id — unaffected |
| **Zero-trust** | Agent_id is caller-provided metadata, not authenticated — not a security boundary |
| **Fail-closed** | Unreachable daemon → no stats recording. Cache ops fail closed as before |
| **Transparent** | Agent_id is set explicitly via env var or IPC param, never auto-harvested |
