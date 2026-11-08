# Agent Compatibility

Not all agents benefit equally from ToolRecall. This document explains **who wins, who loses, and how to configure each one correctly**.

---

## Decision Table

Pick your agent and integration layer. The table tells you what value to expect and any caveats.

| Agent | MCP Bridge | Forward Proxy | Shim | Value | Notes |
|-------|-----------|---------------|------|-------|-------|
| **Hermes** | ✅ | ✅ | ✅ | **High** | Optimized for Hermes — stateless, small context = biggest win. Context Tracker emits drop-clean hints once the agent checkpoints (Hermes does so on turn 1). |
| **OpenCode** | ✅ | ✅ | ❌ N/A (Node.js) | **High** | MCP multiplex is the killer feature. |
| **Cline** | ✅ | ✅ | ✅ | **High** | Benefits from both MCP bridge and shim. |
| **Aider** | ✅ Via `--mcp-toolrecall` | ✅ | ✅ | **Medium** | Diff-patch based, fewer tool re-reads. |
| **Google ADK** | ✅ | ✅ | ✅ | **High** | Python SDK, no built-in tool caching; shim catches `open()` in tools. |
| **Claude Code** | ❌ Not for file cache — use multiplexer + proxy only | ✅ | ❌ | **Selective** | Tested: file caching via MCP increases cost 2.4× (real billed API usage, n=2, adoption forced, edit-heavy — directional). Context hints now silent by default: emitted only after a client calls `context_set_checkpoint`, which Claude Code never does → zero hint overhead, no config needed. Distinct from provider prefix caching, which complements TR on stateless agents. |
| **Codex CLI** | ⚠️ Multiplex only | ✅ | ❌ N/A (Node.js) | **Selective** | MCP bridge for static tool multiplexing only. |
| **Cursor** | ⚠️ Optional | ✅ | ⚠️ Safe but redundant | **Low** | Cursor manages its own tool state. |
| **Warp** | ❌ N/A | ✅ via [warp adapter](#warp-⚠️-selective--strong-for-platform-workloads) | ❌ N/A (Rust) | **Selective** | Strong for platform workloads (agent fleets, Factories replay, cross-model evals); skip the file cache — Warp manages its own context. Requires a public HTTPS endpoint (Warp's backend calls your endpoint; localhost is rejected). |
| **Antigravity** | ✅ Bridge + replay + FTS | ❌ N/A (no endpoint override) | ⚠️ SDK only, opt-in | **Selective** | SDK tools are plain callables → `@cached_tool` dedups expensive external calls; bridge unlocks replay + `docs_search` + MCP multiplex. No LLM caching ever (inference is Google-hosted, no base-URL). Do NOT enable MCP file caching — stateful edit-heavy agent, same 2.4× trap as Claude Code. Contrast with [Google ADK](#google-adk--✅-high-value): same vendor, opposite end of the statefulness spectrum — ADK is the stateless case (shim + proxy apply), Antigravity the stateful one. Both adapters can share one daemon (separate namespaces). |

---

## Hermes (Nous Research) — ✅ Best-in-class

ToolRecall is **built for Hermes** — the tools `read_file`, `terminal`, `mcp_call` are available directly (native MCP names). The internal daemon commands are `cached_read`, `cached_terminal`, etc. — both names work in the MCP bridge.

**Why it works:**
- Hermes is a stateless agent with limited context budget
- Repeated file reads and terminal calls inflate prompt size fast
- Context Tracker provides per-turn hints for which files to drop

**Config:**
```bash
pipx install toolrecall && toolrecall setup
# or: uv tool install toolrecall && toolrecall setup
# Tools available natively in Hermes — no extra config needed.
```

> **⚠️  Shim venv:** If you used `pipx` or `uv tool install`, `toolrecall setup` auto-detects
> the Hermes venv and installs the `.pth` shim there. If this fails, or you skipped `setup`,
> run manually:
> ```bash
> toolrecall shim --install --venv ~/.hermes/hermes-agent/venv
> ```

---

## OpenCode — ✅ High value

OpenCode is a Node.js agent (shim doesn't apply), but the MCP bridge is transformative.

**Why it works:**
- OpenCode has no built-in MCP multiplexing — TR provides shared server subprocesses
- Lazy-loading avoids ~1.7s per-server startup on every session boot
- Shared daemon means GitHub / Postgres / etc. servers persist across OpenCode sessions

**Config:**
```jsonc
// ~/.opencode/opencode.jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "toolrecall": {
      "type": "local",
      "command": "toolrecall",
      "args": ["mcp"],
      "enabled": true
    }
  }
}
```

`toolrecall setup` writes this automatically.

---

## Cline — ✅ High value

Cline is a Python agent that benefits from both the shim and MCP bridge.

**Config:** Add to `~/.config/cline/mcp_settings.json`:
```json
{
  "mcpServers": {
    "toolrecall": {
      "command": "toolrecall",
      "args": ["mcp"]
    }
  }
}
```

The `.pth` shim auto-caches `open()`, `subprocess.run()`, and `subprocess.Popen()` in every Cline session.

---

## Aider — ✅ Medium value

Aider's architecture is diff-patch based: it reads files, produces diffs, applies them. Fewer repeated tool calls than agents that iterate via tool loops.

**Config:**
```bash
aider --mcp-toolrecall
```

**Shim benefit:** The Python shim catches `read_file` calls from Aider's tool execution layer. Worth enabling for projects with large files read multiple times.

---

## Google ADK — ✅ High value

Google's Agent Development Kit (ADK) is a Python framework with no built-in tool-output caching. Every tool call runs through `run_async()` fresh — even repeated reads of the same file.

**Why it works:**
- ADK tools are plain Python functions, so the `.pth` shim transparently caches `open()`, `subprocess.run()`, and `subprocess.Popen()` calls inside them.
- The MCP Bridge gives ADK access to shared, persistent MCP server subprocesses (time, fetch, GitHub, etc.).
- For deepest integration, wrap tools with `cached_read`, `cached_write`, etc.

**Config:**
```bash
pipx install toolrecall && toolrecall setup
# No per-agent config needed for the shim.
```

For detailed ADK-specific patterns, see [ToolRecall + Google ADK](google-adk.md).

---

## Claude Code — ❌ File caching costs more than it saves

| Feature | Verdict | Evidence |
|---------|---------|----------|
| **Forward Proxy** | ✅ Verified — saves real cost | Orthogonal to tool loop |
| **Request-body dedup** (proxy) | ⚠️ Mechanism-verified — no billed run on this path yet | Keep-first, prefix-stable stub of repeated `tool_result` payloads — the one compatible cost lever, see below |
| **MCP Multiplexer** | ✅ Verified — shares subprocesses across sessions | Works as documented |
| **File/terminal cache via MCP** | ❌ Tested: **2.4× cost increase** | Numbers and methodology in "The Numbers" and "Root causes" below |
| **Context tracker** | ❌ No benefit — but now silent by default (see below) | Same report; opt-in mechanism in `c66b16c` |

### The Numbers

A controlled A/B test (Claude Code Sonnet 5, edit-heavy task, n=2 per arm, adoption forced) showed:

| Metric | Native | With TR file cache | Δ |
|--------|-------:|-------------------|---:|
| Turns | 32 | 56 | **1.8×** |
| Cost (USD) | $0.57 | $1.34 | **2.4×** |
| Wall time | 61 s | 187 s | **3.1×** |

Cost is **real billed API usage** (`total_cost_usd` from each run's Claude API JSON; means of 2 runs/arm: A=$0.644/$0.490, B=$1.122/$1.555). n=2 per arm, one model (Sonnet 5), one edit-heavy task, adoption forced — adequate for a directional 2.4× result, not for effect-size precision.

> **Scope note — this is a different result from the prefix-caching benchmark.** The 2.4× is about *ToolRecall's file cache being routed through MCP as tools inside Claude Code*, an append-only harness that can't drop context. The provider-prefix-caching finding is an *API-layer* result on stateless agents where the Context Tracker can drop clean files — there, provider prefix caching is orthogonal and complements TR. One does not contradict the other; they describe different cache layers for different harnesses.

**Root causes:**
1. **`patch` has no `replace_all`** — a 29-site rename became 58 sequential single-occurrence patch calls vs. a few native `replace_all` edits. Each extra turn re-bills the full growing context under Anthropic prompt caching.
2. **Edits fight the cache** — every `patch` invalidates the file's entry; verification re-reads mostly missed (21% hit rate).
3. **Stub savings are structurally small** — ~15K tokens avoided vs ~690K extra context tokens billed (~45×).
4. **Native Read is preferred** — when both native `Read` and MCP `cached_read` are available, Claude Code uses native Read exclusively. Forcing it to use MCP tools via `--disallowedTools Read` makes sessions more expensive.

### Why File Caching Doesn't Help Append-Only Harnesses

Claude Code's transcript is **append-only from the model's side**. Every turn appends to the message array — there's no harness-level compaction mechanism exposed to the model. Adding a file cache that returns stubs saves ~15K tokens of file content but adds dozens of MCP tool round-trips plus the full re-billing of prompt-cached context on each extra turn.

This means:
- **The 7.4× endurance figure does NOT transfer.** Context grows unboundedly — same as baseline.
- Adding ToolRecall's MCP file-caching tools means **your sessions cost 2.4× more and run 3.1× slower.**

### Context hints are now silent by default

Since `c66b16c` (per-client context-hint policy + checkpoint opt-in), the
bridge only appends 🧹 drop-clean hints **after the client has called
`context_set_checkpoint` once**. Claude Code never calls the checkpoint
tools, so:

- **No hint text is ever appended** to Claude Code tool results — the old
  default emitted ~60 B + a stale-file block after every non-context call,
  which append-only transcripts re-billed at full token cost. That overhead
  is gone with zero configuration.
- The per-client table (`[mcp.clients."claude-code"] emit_context_hints = false`)
  makes the silence explicit in mixed-agent daemons (e.g. Warp hosting
  Claude Code alongside Hermes), but it is **not required** — the default
  behavior is already silent for any client that never checkpoints.

The tracker still provides **no benefit** to Claude Code: an append-only
harness cannot act on drop-clean instructions, so hints would be inert even
if emitted. The gain here is removal of overhead, not a new capability.

### Why there is no fix from ToolRecall's side (verified against the platform docs)

The tracker's clean/dirty decision engine is sound; the blocker is that no
external surface can act on it in Claude Code:

- **Hooks cannot prune retroactively.** Every Claude Code hook is
  forward-looking or observational: `PreToolUse` can modify a tool *input*,
  `PostToolUse` can only *add* context via `additionalContext` (the wrong
  direction). No hook can remove or rewrite a `tool_result` from an earlier
  turn.
- **API context editing is harness-owned.** Anthropic's `clear_tool_uses_20250919`
  (context-management beta) does clear old tool results server-side — but only
  the API caller (Claude Code itself) can set it, and selection is
  threshold / keep-N (`trigger`, `keep`, `clear_at_least`, `exclude_tools`);
  it deliberately accepts no per-file clean/dirty signal.

So the only layer that can cut the token bill in an append-only harness —
retroactive transcript pruning — belongs to the harness/API and takes no
external input. **If you want this class of savings in Claude Code, turn on
Anthropic's context editing / let Claude Code compact** — that is the native
version of what the tracker provides, and it is the better tool there. The
tracker's value stays in harnesses where the agent prunes its own context
(e.g. Hermes). See also [CONTEXT_STALE.md](CONTEXT_STALE.md) for the
file-level stale signal ToolRecall exposes to harnesses that *can* use it.

### What to Use Instead

- **Forward proxy** — cache API responses via `:8569`. This is orthogonal to the tool loop and saves real money on repeat API calls in dev loops.
- **Request-body dedup** — the one cost lever that is compatible with an append-only harness (see below).
- **MCP multiplexer only** — if you run 5+ MCP servers, TR's multiplexer shares one subprocess per server across all sessions. Add TR as the single MCP entry point, but **don't route file tools through it**.

### The one cost lever that is compatible: request-body dedup

Request-body dedup (`[forward_proxy] dedup`, default **off**) rewrites
duplicate large text blocks *inside the request body* — the same file coming
back as a `tool_result` again — to a short stub, before the provider sees it.
It is compatible with an append-only harness because it never asks the model
or the harness to drop anything retroactively; it only shortens what is sent
*this* turn:

- **Keep-first, stub-later** — within a request, earlier messages are never
  rewritten. Across turns the byte-prefix stays stable only if the duplicate is
  stubbed on its *first* send, so run with `TOOLRECALL_DEDUP_PROTECT_LAST=0`:
  with the default (`2`) a duplicate arriving inside the protected tail goes out
  in full, is cached, and is stubbed one turn later — rewriting already-cached
  bytes and forcing the suffix to be cache-written again (~1.25×) instead of read
  (~0.1×). Keep-first keeps the full copy either way, so nothing is lost
  (`tests/test_dedup_anthropic.py::TestProtectedTailVsCache`).
- **Marker-preserving** — blocks are shallow-copied and only the text leaf is
  replaced, so `cache_control`, `tool_use_id` and every other key survive
  byte-exact. This matters: a gateway that *strips* `cache_control` while
  returning success makes the entire conversation history bill as uncached
  input on every turn. ToolRecall never touches those markers.
- **Tail-protected** — `TOOLRECALL_DEDUP_PROTECT_LAST` (default 2) keeps the
  newest messages full, so the block carrying Claude Code's cache breakpoint
  is never stubbed.

**Honest scope — read before quoting a number.** The mechanism is tested on
the Anthropic wire shape (`/v1/messages` dedup, prefix stability,
`cache_control` preservation in `tests/test_dedup_anthropic.py`). The sibling
chat-completions path is billing-verified at **−32.3% prompt tokens /
−30.0% billed** (SWE-bench Lite × 8 turns, LiteLLM/OpenRouter). The
`/v1/messages` path has **no billed measurement yet** — treat its saving as
unquantified until an A/B run on your own Claude Code workload produces one.

### Config (multiplexer + proxy only, no file caching)

```json
// ~/.claude/settings.json — add toolrecall for multiplexing ONLY
{
  "mcpServers": {
    "toolrecall": {
      "command": "toolrecall",
      "args": ["mcp"]
    }
  }
}
```

Then set `OPENAI_BASE_URL=http://localhost:8569/v1` for API response caching.

Claude Code's own endpoint is Anthropic-shaped, so point `ANTHROPIC_BASE_URL`
at the same proxy and opt in to request-body dedup:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8569
export TOOLRECALL_FORWARD_DEDUP=1   # or [forward_proxy] dedup = true in toolrecall.toml
```

For an A/B baseline arm, per-request opt-out is the header
`X-ToolRecall-No-Dedup: 1`.

> **Bottom line:** Add TR **only** for the forward proxy, request-body dedup
> and MCP multiplex. File caching through MCP has been empirically tested and
> makes Claude Code sessions **2.4× more expensive**. Do not route file tools
> through ToolRecall with Claude Code. Context hints need no action: they're
> silent by default (checkpoint opt-in), so a stock TR install no longer
> appends any hint text to Claude Code sessions. Request-body dedup is the one
> lever that is compatible with the append-only transcript — measure it on
> your own workload before trusting a number.

---

## Codex CLI — ⚠️ Use selectively

Codex CLI is Node.js (shim N/A). The MCP bridge is useful for multiplexing static tool servers.

**Config:** Use the MCP config format Codex CLI expects. ToolRecall acts as a multiplexing endpoint for read-only tools (time, fetch).

**Avoid:** Do not route file reading/editing through ToolRecall — Codex manages its own file state.

---

## Cursor — ⚠️ Low value

Cursor has its own tool-execution plumbing. The shim is safe (Python process) but largely redundant — Cursor manages its own state aggressively.

**Recommended:** Skip ToolRecall for Cursor sessions. The forward proxy is the only feature that adds value (API cost savings).

---

## Warp — ⚠️ Selective — strong for platform workloads

[Warp](https://www.warp.dev/) routes agent inference through its own backend, so ToolRecall integrates via the **custom inference endpoint** surface — not env vars, not the shim (Warp is Rust). The `warp` adapter exposes a small public HTTPS edge that fronts the ToolRecall forward proxy.

**Where it wins (platform layer):**

| Warp capability | Why ToolRecall helps |
|---|---|
| Agent fleets / orchestration | Identical calls across fleet runs are served from cache — $0 repeats, additive to provider prefix caching |
| Factories (self-improvement loops) | Recorded-and-replayed tool/API results make runs deterministic instead of re-crawling a changed web |
| Cross-model evals & routing | Every model arm sees identical tool results — score models, not tool noise. The dedup hook is model-agnostic, so savings survive routing |
| **File cache** | ❌ Skip it — Warp manages its own context (same category as Claude Code/Cursor) |

**Honest limits:** the response cache does not survive model switches (a different model is a different request by definition). Custom inference endpoints don't apply to Warp Cloud Agents. Requests carry your provider API key in-flight through Warp's backend and your edge — the adapter suppresses all access logging.

**Setup:**

```bash
# 1. Start the edge (proxies to the TR forward proxy on 127.0.0.1:8569)
tr-warp-edge --provider api.openai.com --port 8571 --auth-token "$(openssl rand -hex 32)"

# 2. Publish the edge at a public HTTPS URL — Warp's backend must be able to
#    reach it; localhost/private addresses are rejected by Warp.
cloudflared tunnel --url http://127.0.0.1:8571

# 3. Register the public URL in Warp: Settings > inference endpoint,
#    model identifier(s), and your provider API key (stored on-device).
```

> **⚠ Always set `--auth-token` (or `TOOLRECALL_EDGE_TOKEN`) when the edge is
> reachable beyond loopback.** A quick-tunnel URL (`*.trycloudflare.com`) is
> public-by-obscurity, not private: anyone who learns the URL can POST through
> the edge. The token makes the edge reject unauthenticated requests with 401
> before any relay. On loopback only, auth is optional.

Verified locally: identical request twice through edge → proxy → daemon = one upstream call, second response served with `X-ToolRecall-Cache: HIT` (see `tests/test_warp_fullchain.py`).

---

## Antigravity — ⚠️ Selective — bridge value, no LLM path

Google Antigravity (2.0 / IDE / CLI) consumes MCP config externally
(`~/.gemini/config/mcp_config.json` global, `.agents/mcp_config.json`
workspace) — the same integration shape as OpenCode, and unlike ADK the
bridge CAN sit between Antigravity and other MCP servers. The
`google-antigravity` SDK registers custom tools as plain callables, so
[`@cached_tool`](antigravity.md#path-2-antigravity-sdk-decorator-python-agents)
works exactly like the ADK adapter.

**Works:** SDK tool caching (web/GitHub/DB lookups), session replay via the
bridge (`toolrecall replay record`), FTS over skills/rules
(`toolrecall index-dir ~/.gemini/skills`), opt-in MCP multiplex response
cache (`ttl = 0` on write-tool servers).

**Doesn't:** No forward proxy / API caching — Antigravity has no base-URL
override on any surface; inference is Google-hosted by design. Do NOT enable
MCP file caching: Antigravity manages its own context (stateful, edit-heavy)
— the same measured 2.4× cost trap as Claude Code.

[Full docs →](antigravity.md)

## Integration Layer Reference

| Layer | What it does | Requires | Best for |
|-------|-------------|----------|----------|
| **MCP Bridge** (`toolrecall mcp`) | Single MCP entry point → daemon → multiplexed servers + caching | MCP-compatible agent | Any agent. The default. |
| **Forward Proxy** (`:8569`) | Caches API responses by body hash | SDK pointed at `http://localhost:8569` | Any agent making API calls. Saves $ in dev loops. |
| **Python Shim** (`.pth` file) | Transparently caches `open()`, `subprocess.run()`, and `subprocess.Popen()`, auto-strips agent shell wrappers | Python agent, pipx or `toolrecall shim --install` | Python agents without native TR support. Marked experimental. |
| **Go Client** (`tr` binary) | Direct UDS connection to daemon | `go build` in `go-client/` | Shell scripts, CI/CD, non-Python agents, herdr panes. |