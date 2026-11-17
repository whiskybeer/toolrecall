# Cross-Agent Failure Ledger (P5)

The Cross-Agent Failure Ledger is a Layer-1 persistence and discovery mechanism for ToolRecall. It tracks recurring tool execution failures across distinct agent sessions, subagents, and tool runs.

When autonomous agents encounter failures (missing files, network timeouts, invalid arguments, permission barriers, or rate limits), they frequently repeat the exact same errors across turns or parallel subagents. The Failure Ledger normalizes error messages into stable, deterministic content-addressed signatures and indexes them so that agents and tooling can identify repeating failure modes and their known resolutions before repeating mistakes.

---

## 1. Data Minimization & Privacy Guarantees

The Failure Ledger is designed around strict privacy hygiene and zero-leakage principles:

1. **Signatures and Counts Only:** No user prompts, tool arguments, or raw response payloads are ever stored.
2. **Aggressive Secret Redaction:** All error messages pass through `_redact_secrets()` prior to normalization and storage. Credentials, Bearer tokens, API keys (`sk-...`), GitHub credentials (`ghp_...`), AWS credentials (`AKIA...`), and PEM private key blocks are stripped and replaced with `<redacted>`.
3. **Deterministic Volatile Token Collapsing:** Dynamic and environment-specific markers are normalized:
   - User home directories (`/home/<user>` or `~`) are collapsed to `~`.
   - UUIDs are collapsed to `<uuid>`.
   - ISO 8601 and Unix epoch timestamps are collapsed to `<ts>`.
   - Hex memory addresses (`0x[0-9a-fA-F]+`) are collapsed to `0x<hex>`.
   - Line numbers (`line \d+` or `:\d+`) are collapsed to `:N`.
   - Whitespace is collapsed, and the normalized error message is capped at 512 characters.
4. **Deterministic Signatures:** Hashing routes through `content_digest` in `toolrecall/keys.py`, computing a 32-character hex signature:
   ```python
   sig_hash = content_digest(f"{tool}\x00{norm_text}")[:32]
   ```
5. **Isolated SQLite Storage:** Ledger records are stored in the `failure_ledger` SQLite table via `toolrecall/_db.py`, indexed with an optional SQLite FTS5 virtual table (`failure_ledger_fts`) when available.

---

## 2. Configuration

Recording of failure signatures is **disabled by default (opt-in)** in `~/.config/toolrecall/toolrecall.toml`. Enable it via the `[cache]` configuration block (or `TOOLRECALL_FAILURE_LEDGER=1`):

```toml
[cache]
failure_ledger = true  # Set to true to opt in to failure signature tracking
```

---

## 3. Schema & Persistence

The underlying SQLite schema consists of:

```sql
CREATE TABLE IF NOT EXISTS failure_ledger (
    signature_hash TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    normalized_error TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    resolution TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS failure_ledger_fts USING fts5(
    signature_hash UNINDEXED,
    tool,
    normalized_error,
    resolution
);
```

When an identical failure signature recurs:
- `count` is incremented.
- `last_seen` is updated to the current timestamp.
- `first_seen` is preserved.
- Any existing `resolution` is retained.

---

## 4. MCP Tools

The daemon provides an MCP tool allowing agents to query known failure signatures and their recorded resolutions before executing risky or previously failing operations:

### `failures_search`

Searches recorded failure signatures using full-text search (FTS5) or prefix matching.

**Parameters:**
- `query` (string, required): Search query matching error text or resolution.
- `tool` (string, optional): Filter by tool name (e.g. `bash`, `git`, `read_file`).
- `limit` (integer, optional, default: 10): Maximum results to return.

**Returns:**
A list of matching failure records sorted by `count DESC, last_seen DESC`, containing:
- `signature_hash`: 32-char hex digest identifying the failure pattern.
- `tool`: Target tool name.
- `normalized_error`: Redacted, normalized error representation.
- `count`: Total occurrences observed.
- `first_seen`: Unix epoch timestamp when first encountered.
- `last_seen`: Unix epoch timestamp when most recently encountered.
- `resolution`: Description of fix or workaround, if recorded.

---

## 5. CLI Commands

The ToolRecall CLI provides commands for inspecting and managing failure signatures:

- **Search recorded failures:**
  ```bash
  toolrecall failures search "connection refused" --tool bash --limit 5
  ```

- **Show ledger statistics:**
  ```bash
  toolrecall failures stats
  ```
  Returns total unique signatures, total failure occurrences, and count of resolved signatures.

- **Record a resolution for a known failure signature:**
  ```bash
  toolrecall failures resolve <signature_hash> --resolution "Ensure redis-server is running on port 6379"
  ```

---

## 6. Offline Discovery Script

The offline discovery script `scripts/discover_failure_signatures.py` provides read-only inspection of failure patterns from a SQLite cache database or raw execution log:

```bash
# Analyze signatures directly from cache.db (read-only mode)
python3 scripts/discover_failure_signatures.py --min-count 2

# Filter for a specific tool
python3 scripts/discover_failure_signatures.py --tool bash --min-count 3

# Parse recurring failures from a log file
python3 scripts/discover_failure_signatures.py --log /path/to/daemon.log --min-count 2

# Output as JSON
python3 scripts/discover_failure_signatures.py --json
```

The script guarantees read-only access (using SQLite URI `?mode=ro`) and never modifies the live database.
