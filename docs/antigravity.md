# ToolRecall + Google Antigravity

> Integration for Google Antigravity 2.0, Antigravity IDE, and Antigravity CLI.
> One MCP config entry unlocks transparent tool caching, session replay, and
> full-text search over skills — no changes inside Antigravity itself.

## Table of Contents

- [Overview](#overview)
- [What works — and what does not](#what-works--and-what-does-not)
- [Path 1: MCP Bridge (`--multiplexer-only` recommended)](#path-1-mcp-bridge---multiplexer-only-recommended)
- [Path 2: Antigravity SDK decorator & Subagent Pooling](#path-2-antigravity-sdk-decorator--subagent-pooling)
- [Path 3: Lifecycle Hooks (`hooks.json` WAF Gate & Drift Detection)](#path-3-lifecycle-hooks-hooksjson-waf-gate--drift-detection)
- [Path 4: Transcript Auditor & Environment Doctor](#path-4-transcript-auditor--environment-doctor)
- [Path 5: FTS5 Auto-Indexing (`index-antigravity`)](#path-5-fts5-auto-indexing-index-antigravity)
- [Rides-along: replay, forward proxy, MCP multiplex](#rides-along-replay-forward-proxy-mcp-multiplex)
- [Antigravity vs Google ADK — stateful vs stateless](#antigravity-vs-google-adk--stateful-vs-stateless)
- [Freshness: how cached web content stays correct](#freshness-how-cached-web-content-stays-correct)
- [Install per platform](#install-per-platform)
- [Windows testing checklist](#windows-testing-checklist)
- [Troubleshooting](#troubleshooting)

---

## Overview

Antigravity consumes MCP configuration **externally** (`mcp_config.json`), which
puts ToolRecall in a position it cannot reach with most Python SDKs (ADK, for
example, brings its own MCP client the bridge can't intercept): the bridge can
sit **between Antigravity and every other MCP server**.

ToolRecall registers by default with `--multiplexer-only`, stripping out
stateful file tools (`cached_read`, `cached_write`) so Antigravity relies
on its own stateful workspace engine while gaining 70% RAM savings from
MCP server pooling, 0.6ms BM25 skills search, session replay, and deterministic
WAF/drift lifecycle hooks.

The `google-antigravity` SDK additionally registers custom tools as plain
Python callables — the [`@cached_tool`](#path-2-antigravity-sdk-decorator--subagent-pooling)
decorator wraps them, with subagent role tagging (`tag_agent_role`) and Recall Tier
pointer compression (`store_pointer`, `@recall_pointer`).

## What works — and what does not

| Layer | Verdict | Why |
|---|---|---|
| MCP bridge (`toolrecall mcp --multiplexer-only`) | ✅ default | External MCP config — multiplexes external servers without adding file tool schema bloat |
| Transparent MCP response cache (fronted servers) | ✅ opt-in | `[mcp_multiplex]` in the daemon config; RFC 8785 request keys |
| Lifecycle hooks (`toolrecall hook antigravity`) | ✅ | WAF gate blocking sensitive reads / dangerous commands, out-of-band disk drift detection, and context alert injection |
| Session replay (VCR for tool calls) | ✅ | Bridge records full MCP responses (`toolrecall replay record`) |
| FTS over skills, plugins & artifacts (`docs_search`) | ✅ | `toolrecall index-antigravity` auto-discovers builtin skills, workspace skills, plugins, and artifacts |
| SDK custom-tool caching (`@cached_tool`) | ✅ | Plain callables — no wrapper class needed; subagent role attribution & shared cache pooling |
| Recall Tier pointers (`@recall_pointer`) | ✅ | Compresses verbose tool output into lightweight pointer nodes with on-demand retrieval (`fetch_pointer`) |
| Environment Doctor & Auditor | ✅ | `toolrecall doctor antigravity` checks setup health; `toolrecall audit antigravity` audits transcript tokens |
| Forward proxy / API caching for **Antigravity's own inference** | ❌ impossible (interactive) / ✅ opt-in (headless SDK/eval) | Interactive IDE/CLI chat has no base-URL override (Google-hosted). Headless SDK runs and eval pipelines replay repeat runs at $0 using `X-ToolRecall-Canonicalize: antigravity`. |
| File cache / Python shim (`cached_read`, context tracker) | ❌ do not enable | Antigravity manages its own context (stateful agent line). Exposing file caching into MCP measurably increases cost (2.4× trap). |

> **Rating: Selective.** Strong for MCP multiplexing + lifecycle hooks + SDK tool caching + transcript auditing + skills FTS.
> Skip the file-cache tools entirely (`--multiplexer-only`).

### Surface Breakdown: Desktop (IDE) vs. CLI (agy) vs. SDK

| Surface | ToolRecall Role | What It Does & What To Avoid |
|---|:---:|---|
| **Antigravity Desktop (IDE Chat)** | MCP Multiplexer, Hooks & FTS Search | Connects via `~/.gemini/config/mcp_config.json`. Cuts background tool RAM by 70%, adds `.agents/hooks.json` WAF/drift security gates, and enables 0.6ms skills search (`docs_search`). Interactive inference is Google-hosted (no proxy base URL). Do not route file tools through MCP. |
| **Antigravity CLI (`agy` interactive)** | MCP Multiplexer, Hooks, Audit & `tr` wrapper | Shares global `mcp_config.json`. Fast local skills retrieval, transcript token auditing (`toolrecall audit antigravity`), and repeatable expensive terminal commands via `tr`. |
| **Antigravity Headless CI / Evals** | Forward Proxy Replay | Use `export TOOLRECALL_CANONICALIZE=antigravity` with the forward proxy. Normalizes volatile `<ADDITIONAL_METADATA>` timestamps, conversation UUIDs, `<USER_SETTINGS_CHANGE>`, and parallel tool-result ordering across OpenAI and Gemini wire formats to replay identical test suites at $0. |
| **Antigravity Python SDK (`google-antigravity`)** | Custom Tool Caching & Swarm Pooling | Custom agent tools decorated with `@antigravity.cached_tool(ttl=300, freshness=True)` return cached API/DB lookups without re-billing. Shared daemon cache pool deduplicates work across subagents and swarm workers. |

---

## Path 1: MCP Bridge (`--multiplexer-only` recommended)

One entry covers Antigravity 2.0, the IDE, and the CLI — they share the same
config format:

- **Global:** `~/.gemini/config/mcp_config.json` (what `toolrecall setup` writes)
- **Workspace:** `.agents/mcp_config.json` (project-specific — setup never touches this)

```bash
toolrecall setup
```

writes (merge-preserving, atomic; your existing servers are kept verbatim):

```json
{
  "mcpServers": {
    "toolrecall": {
      "command": "/home/you/.local/bin/toolrecall",
      "args": ["mcp", "--multiplexer-only"]
    }
  }
}
```

The absolute `command` path matters: Antigravity IDE is a GUI app and on
macOS/Windows its spawned MCP processes may not inherit your shell PATH.
(Windows pipx shims — `toolrecall.exe` / `.cmd` — are detected by the writer's
fallback.) Restart Antigravity afterwards so it picks up the server.

### Why `--multiplexer-only` is standard

Antigravity is an edit-heavy, stateful agent with its own context management and
compaction engine. Exposing `cached_read` and `cached_write` into its MCP tool catalog:
1. Bloats the system prompt with tool schema tokens on every turn.
2. Risks the agent choosing `cached_read` over native file-viewing tools, which can cause
   tool ping-pong and up to 2.4× cost inflation (the same measured trap observed in Claude Code).

With `--multiplexer-only`, ToolRecall strips out file tools while retaining:
- Unified multiplexing and connection pooling for all third-party MCP servers (saving 70% RAM).
- Transparent RFC 8785 response caching for fronted servers.
- Deterministic session replay (`toolrecall replay record`).
- Local BM25 full-text search over skills, plugins, and conversation artifacts (`docs_search`).

### Fronting third-party MCP servers (transparent response cache)

Point Antigravity at `toolrecall` instead of N separate servers; fronted
servers are configured in the daemon config (`[mcp_multiplex] servers = [...]`)
and get transparent response caching keyed by RFC 8785 request hashes.

```toml
[mcp_multiplex]
enabled = true
transparent_cache = true
default_ttl = 300

[mcp_multiplex.servers.my-expensive-api]
# server with read-only tools → safe to cache

[mcp_multiplex.servers.my-writable-api]
ttl = 0   # ⚠️ MANDATORY for any server exposing write tools:
          # identical-args writes would otherwise return cached responses
```

Check whether this pays: `toolrecall stats` → `mcp_hits` / `mcp_missed`. If the
`mcp_cache` table stays empty, disable the multiplexer
(`[mcp_multiplex] enabled = false`) — enabled-but-unused costs nothing (lazy
start + idle reaper), but the tool surface stays leaner without it.

---

## Path 2: Antigravity SDK decorator & Subagent Pooling

The SDK's `Agent`/`LocalAgentConfig` accept custom tools as plain callables —
wrap them with the decorator and hand over the wrapped callable. No wrapper
class, no registration changes:

```python
from toolrecall.adapters import antigravity

@antigravity.cached_tool(ttl=300)          # ttl=0 → opt out per tool
async def search_web(query: str) -> str:   # sync tools work too
    return await external_api_call(query)  # runs only on cache miss

config = LocalAgentConfig(tools=[search_web])   # plain callable — done
```

Semantics:

- **Namespace `antigravity`** — keys never collide with other adapters
- **ToolContext excluded:** the SDK injects a stateful `ToolContext` into tools
  that declare one; it is conversation state and never enters the cache key
  (signature inspection, mirroring the SDK's own `_find_context_param`)
- **Positional or kwarg calls collide into one entry** (signature-bound keys —
  the SDK's runner invokes kwarg-only, direct calls may be positional)
- **Errors never cached** — an exception propagates live, nothing stored
- **Graceful bypass** — daemon not running → executes live, no crash
- **Hit = JSON roundtrip** — dict/list/str return intact; rich objects
  (pydantic/dataclass) come back stringified. The model consumes strings
  anyway.

### Subagent & Swarm Shared Cache Pooling

When parent agents invoke subagents (`invoke_subagent`) or run coordinated swarms
(`swarm`), subagents start with separate conversation contexts but frequently perform
duplicate searches, documentation looks up, or API queries.

ToolRecall pools tool cache rows across all subagents and swarms running on the machine:

```python
from toolrecall.adapters import antigravity

# Tag role in subagent lifecycle for per-agent cost attribution
antigravity.tag_agent_role("codebase-researcher")

@antigravity.cached_tool(ttl=1800)
def read_api_spec(endpoint: str) -> str:
    return fetch_spec(endpoint)
```

1. **Shared Cache Pool:** When Worker 1 calls `read_api_spec("/v1/models")`, the result is stored in the daemon. When Worker 2 calls the identical function minutes later, it gets an instant cache hit ($0, 0ms).
2. **Attribution:** `antigravity.tag_agent_role("researcher")` sets the role metadata on subsequent calls, allowing `toolrecall stats` to attribute hit/miss rates to specific subagent roles.

### Recall Tier Pointer Compression (`@recall_pointer`)

Large non-reproducible tool payloads (database dumps, massive JSON responses) can easily exhaust the subagent context window. Decorate heavy tools with `@antigravity.recall_pointer`:

```python
from toolrecall.adapters import antigravity

@antigravity.recall_pointer(threshold_chars=4000, summary_len=200)
def query_large_dataset(query: str) -> str:
    return execute_heavy_query(query)
```

- When the result length exceeds `threshold_chars`, ToolRecall automatically persists the raw content in the Recall Tier daemon store and returns a compact pointer:
  `[RECALL_POINTER:node_4a9b...:20000_chars:json] Summary preview...`
- The agent retains only the pointer and summary in its context history.
- To restore full content when needed:
  ```python
  raw_content = antigravity.fetch_pointer("node_4a9b...")
  ```

---

## Path 3: Lifecycle Hooks (`hooks.json` WAF Gate & Drift Detection)

Antigravity executes workspace commands and edits files across long trajectories. ToolRecall provides a deterministic lifecycle hook adapter via `toolrecall hook antigravity`.

### Setting up Hooks

Generate the `.agents/hooks.json` configuration snippet:

```bash
toolrecall hook antigravity print-config
```

Outputs:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "command": "/home/you/.local/bin/toolrecall hook antigravity pre-tool",
        "description": "ToolRecall WAF security gate"
      }
    ],
    "PostToolUse": [
      {
        "command": "/home/you/.local/bin/toolrecall hook antigravity post-tool",
        "description": "ToolRecall disk drift detection"
      }
    ],
    "PreInvocation": [
      {
        "command": "/home/you/.local/bin/toolrecall hook antigravity pre-invocation",
        "description": "ToolRecall staleness alerts"
      }
    ],
    "SessionStart": [
      {
        "command": "/home/you/.local/bin/toolrecall hook antigravity session-start",
        "description": "ToolRecall session state reset"
      }
    ]
  }
}
```

Add these hooks to `.agents/hooks.json` in your workspace.

### Hook Capabilities

1. **`PreToolUse` (WAF Security Gate):**
   - Blocks attempts to read sensitive secrets: `.env`, `.aws/credentials`, `id_rsa`, `id_ed25519`, `credentials.json`.
   - Blocks dangerous shell commands: `rm -rf /`, `mkfs`, fork bombs (`:(){ :|:& };:`), raw device destruction (`dd if=... of=/dev/...`).
   - Returns a structured denial error without executing the tool. Can be bypassed per project by setting `TOOLRECALL_WAF=0`.
2. **`PostToolUse` (Out-of-band Disk Drift Detection):**
   - Tracks file paths modified by `write_file`, `edit_file`, and files read by `read_file`.
   - When `run_command` executes build or migration scripts (e.g., `git checkout`, `npm install`, `alembic upgrade`), it detects files modified on disk out-of-band that have already been read into agent memory.
3. **`PreInvocation` (Context Staleness Alerts):**
   - Injects a high-priority system reminder if modified files on disk have drifted from previously read versions, preventing hallucinated edits on stale file contents.
4. **`SessionStart`:**
   - Cleans up ephemeral turn trackers and initializes session state.

---

## Path 4: Transcript Auditor & Environment Doctor

### `toolrecall doctor antigravity`

Validates your Antigravity environment and configuration integrity:

```bash
toolrecall doctor antigravity
```

Checks:
- Binary resolution: confirms `agy` or Antigravity binary is reachable.
- MCP configuration: checks `~/.gemini/config/mcp_config.json` and `.agents/mcp_config.json` for `--multiplexer-only` flag.
- Lifecycle hooks: checks whether `.agents/hooks.json` is configured.
- FTS5 Index: reports indexed skill count and timestamp.
- Daemon connection: verifies the background daemon is healthy.

### `toolrecall audit antigravity`

Analyzes local Antigravity brain transcripts to reveal where tokens, turns, and money go:

```bash
toolrecall audit antigravity --days 7
```

Scans `<appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl`:
- **Steps & Turns:** Total interactions, planner responses, user inputs, and errors.
- **Token Accounting:** Measures prompt input tokens, cache read tokens, and output generation tokens.
- **Tool Usage:** Ranks the most heavily invoked tools (e.g. `read_file`, `run_command`).
- **Redundant Reads:** Identifies files repeatedly read across the session, highlighting optimization targets.
- **Subagents:** Audits subagent and swarm invocations.
- Outputs JSON report with `--json`.

---

## Path 5: FTS5 Auto-Indexing (`index-antigravity`)

Antigravity ships with built-in skills, workspace skills, and plugin directories. ToolRecall indexes these locally into SQLite FTS5 for 0.6ms BM25 keyword search:

```bash
toolrecall index-antigravity   # or toolrecall index --all
```

> [!NOTE]
> `toolrecall setup` runs this automatically upon detecting Antigravity! When installed, `docs_search` and `docs_get_page` are immediately available in Antigravity's toolset with all skills pre-indexed.

Auto-discovers:
- **Builtin Skills:** `~/.gemini/antigravity-cli/builtin/skills/`
- **Workspace Skills:** `.agents/skills/`, `.gemini/skills/`, `skills/`
- **Plugin Skills:** `~/.gemini/config/plugins/**/skills/`
- **Conversation Artifacts:** `<appDataDir>/brain/<conversation-id>/*.md` (excluding internal logs)

Once indexed, any agent can query documentation instantly via the bridge's `docs_search` tool:
```python
results = docs_search("Chrome DevTools accessibility audit")
```
Re-run `toolrecall index-antigravity` (or `toolrecall index --all`) whenever new skills or plugins are installed.

---

## Rides-along: replay, forward proxy, MCP multiplex

### Session replay (zero adapter code)

The bridge records full MCP tool responses on the first run and replays them
deterministically afterwards — VCR.py for tool calls:

```bash
toolrecall replay record antigravity-session   # start recording, then use Antigravity
toolrecall replay stop
toolrecall replay list                         # scenarios stored in the cache DB
```

Use cases: reproduce a failing IDE/CLI session deterministically; demo without
API keys; CI replay of a recorded scenario (SDK headless mode). See
[REPLAY_MODE.md](REPLAY_MODE.md).

### Forward-Proxy Replay (`antigravity` profile)

When routing Antigravity LLM calls through ToolRecall's forward proxy (for headless CI runs, evaluation pipelines, or custom SDK agents), Antigravity injects volatile metadata that would otherwise break cache-key matching across identical runs:
- Volatile local time strings in `<ADDITIONAL_METADATA>`
- Ephemeral conversation UUIDs and `/brain/<uuid>` artifact paths
- Dynamic `<USER_SETTINGS_CHANGE>` notifications
- Test runner wall-clock durations (`in 0.45s`)
- Non-deterministic ordering of parallel tool calls and responses

The `antigravity` canonicalization profile normalizes all of these deterministically across both OpenAI/Anthropic wire format (`messages`) and Google Gemini wire format (`contents` and `parts`):

```bash
export OPENAI_BASE_URL=http://localhost:8569/v1  # or target provider
export TOOLRECALL_CANONICALIZE=antigravity
```

With this profile enabled, repeated identical headless benchmark or CI runs replay at 100% cache hit ($0 billed cost, 0ms model latency).

### CI Failure Recovery (`replay forget`)

If an Antigravity benchmark or CI job fails, replaying the failed run reproduces the failure on every retry. Tag the run with `TOOLRECALL_RUN_ID` and evict on failure:

```bash
export TOOLRECALL_RUN_ID="ci-$JOB_ID"
# Run Antigravity agent:
run_antigravity_task.sh || { toolrecall replay forget "$TOOLRECALL_RUN_ID"; exit 1; }
```

This clears all cached responses created by the failed attempt, allowing the next attempt to query the model live.

### Full-text search over skills and rules

```bash
toolrecall index-antigravity   # auto-discovers builtin, workspace, plugins, artifacts
# or target individual directories manually:
toolrecall index-dir ~/.gemini/skills --source antigravity-skills
```

`docs_search` is then available to Antigravity through the bridge (BM25,
Porter stemming, no embeddings/GPU). Re-index after editing skills.

### `tr` binary (CLI terminal only)

Inside the Antigravity CLI's terminal, `tr` wraps repeatable/expensive
commands (`tr run "cargo build --release"`) — herdr Path-1 pattern. One line
in your Antigravity rules file if you use it.

### Recall Tier (advanced, optional)

Non-reproducible tool output (one-shot API results, live snapshots) can be
stored out-of-band and referenced by a tiny pointer node — the agent drops the
verbose block and restores on demand:

```
1. recall_store(fingerprint, content, content_type, reproducible=False)
2. Agent drops the verbose raw block, keeps the pointer
3. recall_get(node_id) → restores the raw bytes
```

This only pays if Antigravity *follows* the drop-keep-pointer instruction
reliably — ship it as a suggested Workflows rule, measure before claiming
anything. Off by default (`[recall] enabled = true`). See RECALL_TIER.md.

---

## Antigravity vs Google ADK — stateful vs stateless

Both are Google agent frameworks, and both work with ToolRecall — but they
sit at **opposite ends of the statefulness spectrum**, which flips which
mechanisms apply:

| | **Google ADK** | **Antigravity (2.0 / IDE / CLI / SDK)** |
|---|---|---|
| Context management | **Stateless** — every `run_async()` sends the full history fresh; no app-side context manager | **Stateful** — Antigravity manages its own context (compaction, artifact memory) |
| File cache / Python shim | ✅ Applies — `open()`/`subprocess` inside ADK tools are cached transparently (`.pth` shim) | ❌ Do not pitch — stateful edit-heavy harness, same measured 2.4× trap as Claude Code |
| `@cached_tool` decorator | ✅ (`toolrecall.adapters.google_adk`) | ✅ (`toolrecall.adapters.antigravity`, plus `freshness=True`) |
| Forward proxy / LLM API caching | ✅ ADK respects `GEMINI_BASE_URL` → point it at `toolrecall serve` — **billed-token savings are measurable here** | ❌ Interactive N/A (no endpoint override); ✅ Headless/eval only via `X-ToolRecall-Canonicalize: antigravity` |
| MCP bridge value | Limited: ADK brings its own MCP client, the bridge can't intercept it | ✅ Full: Antigravity consumes `mcp_config.json` externally → bridge, multiplex, replay, `docs_search`, `fetch_revalidate` |
| Value story | Token savings (proxy + file cache) | External-call avoidance + replay + freshness |

**Running both together:** fully supported and common. Both adapters share
one daemon but keep **separate cache namespaces** (`adk` vs `antigravity`) —
an identical web call from an ADK tool and an Antigravity SDK tool is cached
twice, once per namespace (no cross-collision, by design). Shared
*infrastructure-level* caching (fetch/GitHub rows via the bridge's
transparent cache, `fetch_revalidate` rows) is shared across all agents on
the machine, ADK and Antigravity alike.

**One ADK caution that does not transfer to Antigravity:** ADK's file-cache
wins assume the stateless harness. If you embed ADK *inside* a stateful
Antigravity SDK agent, follow the Antigravity rules for file caching (off)
and the ADK rules for the proxy (on) — the harness that owns the context
window decides, not the tool framework.

## Freshness: how cached web content stays correct

The cache is a deterministic key→value store — it never knows a site changed;
freshness is a time contract you control. Four complementary levers:

### 1. TTL sizing (the main lever)

| Content | TTL |
|---|---|
| Docs, changelogs, reference pages | hours–days |
| News front pages, dashboards | 30–60 s |
| Prices, personalized views, logged-in pages | `ttl = 0` — do not cache |

### 2. ETag conditional revalidation (opt-in)

```toml
[mcp_multiplex]
transparent_cache = true
fetch_revalidate = true        # default OFF
```

When a cached `fetch_url` row expires, the next lookup sends ONE conditional
request (`If-None-Match`) instead of refetching the body:

- **304 Not Modified** → the old body stands (proven byte-identical), only
  timestamps refresh. No body transfer, no tokens.
- **200** → the row is replaced with the fresh body + new validator.
- **network error** → the stale row is left untouched.

Works only for sites that send `ETag`/`Last-Modified`; the first fetch stores
the validator automatically. Rows without one fall back to plain refetch.
Equivalent tool for SDK agents: `fetch_revalidate(url, etag)`.

### 3. Projection at the tool boundary (ads/dynamic blobs)

Ads, banners, embedded timestamps never enter the cache if the tool extracts
the stable core and caches only that (article text, table, JSON). Cache the
*answer*, not the *page* — see `bench/antigravity_win.py` for the pattern.

### 4. `freshness=True` metadata (SDK agents)

```python
@antigravity.cached_tool(ttl=300, freshness=True)
def fetch_page(url: str) -> str: ...

result, meta = fetch_page(url)   # meta = {"cached": bool, "age_s": float|None}
```

The agent can reason: "this result is 2 400 s old — revalidate before
quoting." `age_s` is the cache row's age on a hit, `None` on a live call.

### Precision invalidation

```bash
toolrecall invalidate --url example.com          # one site (fetch server)
toolrecall invalidate --server fetch             # all fetched content
toolrecall invalidate                            # agent caches (api_cache untouched)
toolrecall invalidate --scope api                # explicit opt-in: also api_cache
```

**Not a freshness tool:** invalidation removes *wrong or poisoned* content.
For volatility, use TTL/projection/ETag above.

## Install per platform

### Linux

```bash
pipx install toolrecall          # or: uv tool install toolrecall
toolrecall setup                 # config + systemd user service + daemon + Antigravity MCP entry
# restart Antigravity
```

### macOS

Same two commands as Linux. `toolrecall setup` installs a LaunchAgent instead
of systemd. Notes:

- The LaunchAgent starts the daemon in your GUI session — Antigravity IDE
  (GUI-spawned) can reach the UDS socket.
- On the **Apple-silicon default zsh**: run `toolrecall setup` from your
  normal shell so `which toolrecall` resolves the pipx/uv shim and the
  **absolute path** lands in `mcp_config.json`.
- If Antigravity runs under a different user account than the daemon: the
  socket is per-user; run the daemon for that user too.

### Windows

> Daemon transport on Windows is **TCP localhost:8568** (experimental) instead
> of the Unix socket — the daemon locks via msvcrt and installs a scheduled
> task for autostart. Everything else behaves the same.

```powershell
pipx install toolrecall          # shim: %USERPROFILE%\.local\bin\toolrecall.exe
toolrecall setup                 # config + scheduled task + daemon + Antigravity MCP entry
# restart Antigravity (Antigravity IDE: fully quit from the tray, then relaunch)
```

- The setup writer records the **absolute shim path** (`...\toolrecall.exe`)
  — pipx shims are not on the GUI app's PATH by default.
- Path length: long `~/.toolrecall` paths are handled (PATH_MAX-safe layout);
  no long-path opt-in needed for the default location.
- **Run `toolrecall setup` in PowerShell, not cmd** — same reason as macOS
  zsh: `which`/`Get-Command` must resolve the shim.

---

## Windows testing checklist

A ready-made verification script ships with the repo:
[`scripts/verify_antigravity_windows.ps1`](../scripts/verify_antigravity_windows.ps1)
— read-only (never writes config), checks PATH/shim, daemon health, TCP
transport, the `mcp_config.json` entry (absolute path?), and does a live MCP
handshake against `toolrecall mcp`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_antigravity_windows.ps1
```

For manual verification on a Windows machine (any Windows 10/11 with
Python 3.11+ and Antigravity installed):

```powershell
# 1. Install + setup
pipx install toolrecall
toolrecall setup
toolrecall healthcheck            # exit 0 = healthy; exit 2 = error → Troubleshooting
toolrecall status                 # daemon running, transport: TCP

# 2. Verify the MCP config write
type $env:USERPROFILE\.gemini\config\mcp_config.json
# → mcpServers.toolrecall.command must be an absolute ...\toolrecall.exe path

# 3. Verify the bridge answers as an MCP server
#    (Antigravity CLI or IDE → MCP Servers → refresh → toolrecall listed, green)

# 4. Exercise the cache from Antigravity
#    Ask the agent to call a fronted/cached tool twice with identical args;
#    then check stats:
toolrecall stats                  # mcp_hits > 0 proves end-to-end caching

# 5. Replay smoke
toolrecall replay record ag-win-smoke
#    ... use the agent for one prompt with tool calls ...
toolrecall replay stop
toolrecall replay list            # scenario present

# 6. SDK decorator smoke (optional, needs google-antigravity)
pip install google-antigravity
python -c "from toolrecall.adapters import antigravity; print(antigravity.ADAPTER_SERVER)"
```

Send back: steps 1–5 outputs (text is fine) plus `toolrecall healthcheck`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Antigravity doesn't list `toolrecall` under MCP servers | Config path wrong — must be `~/.gemini/config/mcp_config.json` (global) or `.agents/mcp_config.json` (workspace). Restart Antigravity after writing. |
| Server listed but red / fails to start | Run `~/.local/bin/toolrecall mcp` (or the Windows shim) manually — you should see MCP handshake logs or an immediate error. Check `toolrecall healthcheck`. |
| `toolrecall: command not found` inside Antigravity | The config entry must contain the **absolute** path (re-run `toolrecall setup` from your normal shell). |
| Windows: server won't start, works in PowerShell | GUI apps start with a reduced PATH — confirm the config's `command` is the absolute `toolrecall.exe` path, not a bare name. |
| Cached response for a write-operation server | Set `ttl = 0` for that server in `[mcp_multiplex]` and invalidate: `toolrecall invalidate` (clears cached MCP rows). |
| `mcp_cache` stats stay at 0 despite use | Transparent cache is opt-in — enable `[mcp_multiplex] transparent_cache = true`; the multiplexer must list the server. |
| Replay returns nothing | Recording captures MCP tool calls through the bridge — check the session actually routed via `toolrecall mcp`, not direct config. |
