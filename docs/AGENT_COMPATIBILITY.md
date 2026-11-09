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
| **Claude Code (CLI)** | ❌ Not for file cache — use multiplexer + proxy only | ⚠️ Headless repeats only (`claude-code` profile) | ❌ | **Selective** | Interactive sessions: no measurable token saving — Claude Code already dedups unchanged re-reads natively, and 76 local transcripts showed 0 redundant reads/outputs. Headless repeats (CI re-runs): proxy replay with the `claude-code` canonical profile: −69% usage on warm repeats of short real SWE-bench tasks that run tests, −18% on a 5-issue session (replay stops at the first non-deterministic tool output). Tested: file caching via MCP increases cost 2.4× (real billed API usage, n=2, adoption forced, edit-heavy — directional). Context hints now silent by default: emitted only after a client calls `context_set_checkpoint`, which Claude Code never does → zero hint overhead, no config needed. Distinct from provider prefix caching, which complements TR on stateless agents. |
| **Claude Desktop (App)** | ✅ Multiplexer only (`claude_desktop_config.json`) | ❌ N/A (no proxy base URL) | ❌ | **Low** | MCP server multiplexing (shares subprocesses to cut RAM) and local skill/doc FTS5 search (`docs_search`) only. Electron app with internal auth does not support proxy routing or CLI lifecycle hooks. Like Claude Code, it is an append-only chat interface — do not route file tools through MCP. |
| **Codex CLI** | ⚠️ Multiplex only (static tools) | ✅ | ❌ N/A (Node.js) | **Selective** | MCP bridge for static tool multiplexing only; skip file caching. |
| **Cursor** | ✅ Multiplexer only (`~/.cursor/mcp.json`) | ✅ | ⚠️ Safe but redundant | **Low** | Cursor manages context natively. `toolrecall setup` configures `~/.cursor/mcp.json` with `--multiplexer-only` to pool MCP servers host-wide, plus :8569 for proxy dedup. |
| **Warp** | ❌ N/A | ✅ via [warp adapter](#warp-⚠️-selective--strong-for-platform-workloads) | ❌ N/A (Rust) | **Selective** | Strong for platform workloads (agent fleets, Factories replay, cross-model evals); skip the file cache — Warp manages its own context. Requires a public HTTPS endpoint (Warp's backend calls your endpoint; localhost is rejected). |
| **Antigravity** | ✅ Bridge + replay + FTS | ❌ N/A interactive (Google-hosted); ✅ headless/eval | ⚠️ SDK only, opt-in | **Selective** | SDK tools are plain callables → `@cached_tool` dedups expensive external calls; bridge unlocks replay + `docs_search` + MCP multiplex. Interactive CLI/IDE has no base-URL override; proxy canonical profile (`antigravity`) works for headless SDK runs and eval pipelines. Do NOT enable MCP file caching — stateful edit-heavy agent, same 2.4× trap as Claude Code. Contrast with [Google ADK](#google-adk--✅-high-value): same vendor, opposite end of the statefulness spectrum — ADK is the stateless case (shim + proxy apply), Antigravity the stateful one. Both adapters can share one daemon (separate namespaces). |

---

## Hermes (Nous Research) — ✅ Best-in-class

ToolRecall is **built for Hermes** — the tools `read_file`, `terminal`, `mcp_call` are available directly (native MCP names). The internal daemon commands are `cached_read`, `cached_terminal`, etc. — both names work in the MCP bridge.

**Why it works:**
- Hermes is a stateless agent with limited context budget
- Repeated file reads and terminal calls inflate prompt size fast
- Context Tracker provides per-turn hints for which files to drop
- Compaction signal: Hermes calls `context_compacted` from its compression boundary, so the MCP bridge never serves a dangling `"File unchanged; see earlier read"` stub after a summary. This is the harness-side half of the contract — the daemon/bridge stay agent-agnostic (see [CONTEXT_TRACKER.md](CONTEXT_TRACKER.md))

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

`toolrecall setup` writes this automatically.

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

<a id="claude-code"></a>

## Claude Code — ⚠️ Headless repeats only; no saving in interactive sessions

> [!NOTE]
> **Who benefits?** If you use Claude Code solely for **interactive terminal chat with remote HTTP MCP servers**, ToolRecall saves ~0% (Claude already has native prompt prefix caching and `file_unchanged` stubs). ToolRecall is designed for Claude Code in **automated CI / eval regression test loops** (`claude -p`, saving −45% to −96%), **multi-session local STDIO MCP pooling** (−70% RAM), and **multi-agent environments** (unified rate-limiting and `toolrecall top` telemetry).

| Feature | Verdict | Evidence |
|---------|---------|----------|
| **Forward Proxy replay** | ✅ Headless repeats only — needs the `claude-code` profile | Identical warm reruns **−96%** vs cold (short task, n=8, 7/8 fully replayed). Real SWE-bench tasks (Sonnet): every identical rerun replayed fully (3–21 turns, $0); cumulative −45% after 2 runs, −72% after 4 (n=20/arm); one-off runs save nothing. Requires the Anthropic stream-cache fix (`message_stop`); see the correction under [Measured](#measured-where-toolrecall-can-and-cannot-save). Setup: `toolrecall hook claude ci-env`; misses explained by `toolrecall keytrace` |
| **Request-body dedup** (proxy, with `normalize_output`) | ⚠️ Cost-neutral — nothing to stub on real workloads | 0 stubs in 3,015 tool results from 193 local sessions; +0.7% (noise) on a task built to repeat pytest output |
| **Staleness hook** (`PostToolUse`, `toolrecall hook claude`) | ⚠️ Small correctness aid | −10% cost when a build step silently rewrites a read file (saves the rejected-edit round trip); neutral otherwise (n=6/arm) |
| **Read gate / unchanged re-read stub** | ❌ Redundant | Claude Code returns a native `file_unchanged` result for unchanged re-reads |
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

### Measured: where ToolRecall can and cannot save

Full numbers and method: [bench/claude_code_replay/RESULTS.md](../bench/claude_code_replay/RESULTS.md).

**Interactive sessions — no saving available.** Across 76 local Claude Code
transcripts (1,307 API calls), 68% of cost was cache reads of the ~100k-token
context and only ~15% came from tool results at all. There were 0 unchanged
re-reads (Claude Code answers those natively with a ~90-char `file_unchanged`
result), 0 identical large Bash outputs, and 1 compaction. The largest fixed
cost is the session baseline (system prompt + tools + instruction files, 30% of
all context tokens; 13.7% of weighted cost per `toolrecall audit claude-code`) —
that is trimmed in your own config, not by a cache.

**Headless repeats — replay works with the `claude-code` profile.** Same task,
8 runs through a hermetic proxy:

| Profile | Calls replayed | Runs fully replayed | Usage vs cold run |
|---|---:|---:|---:|
| `hermes` (generic) | 6 / 24 | 0 / 8 | −24% |
| `claude-code` | 16 / 24 | 4 / 8 | **−62%** |

On real tasks (Sonnet, official hidden-test grading), the saving shrinks with
session length:

| Workload (warm repeats) | Usage saved |
|---|---:|
| Short task, agent only reads and edits (3–4 calls) | −100% |
| Short task, agent runs tests (5–8 calls) | −69% |
| 5-issue backlog in one session (33–38 calls) | **−18%** |

> **Correction (stream cache).** The replay figures in this section were served from
> Claude Code's *non-streaming fallback* retries: the proxy's stream tee only accepted
> OpenAI-style `[DONE]` streams and discarded every Anthropic stream, which ends with
> `message_stop`. After the fix, streamed calls are cached and replayed directly:
> identical warm reruns of a short task cost −96% vs the cold run (n=8, 7 of 8 fully
> replayed, 0 first-call misses; the partial run broke on an `ls -l` minute rollover,
> found with `toolrecall keytrace`). The longer-session figures above have not been
> re-measured on the stream path. Details:
> [bench/results/CLAUDE_CODE_INTEGRATIONS_REPORT.md](../bench/results/CLAUDE_CODE_INTEGRATIONS_REPORT.md).

Every request carries the whole conversation, so the first non-deterministic
tool output (pytest temp-dir numbers, `grep -r` order, Python `set` order) ends
replay for the **rest of the session**. Replay saves the prefix before that point.

Two conditions decide whether replay pays off, both measured: **same commit**
(Claude Code sends the 5 most recent commits with the first message, so any new
commit — even an empty one — gets 0 replay; reruns of that commit replay fully),
and **deterministic tool output** (with `PYTHONHASHSEED=0`, the 5-issue session
went from −18% to −65%; 2 of 3 repeats replayed all 38 calls).

Safety controls for the profile passed (other checkout dir, edited source,
other model → no false replay; 79 replayed calls audited, 0 false matches), and a
same-proxy no-cache baseline gives −61% / −67% on two short django tasks.

Limits, all measured:
- **Replay returns the earlier answer.** Right for CI re-runs on unchanged
  inputs, harness regression tests, demos. Wrong for evals that sample the model
  (pass@k, variance) — replay hides the variance being measured.
- **The first run is always paid in full.** Savings over N runs approach
  1 − 1/N only when every warm run replays completely; quote warm-run figures.
- **Tool-output determinism sets the ceiling.** Any time-dependent byte in a
  tool result (`ls -l` mtimes in a fresh checkout) forks the chain from that call.
- **Pin MCP servers** (`--strict-mcp-config`): claude.ai connector tools load
  asynchronously and change the tool list between runs.
- Claude Code's `total_cost_usd` counts replayed calls as billed; read the
  proxy's `HIT` rows instead.

### What to Use Instead

- **Forward proxy replay for headless repeats** — CI re-runs and regression runs of `claude -p` with the `claude-code` profile (config below). Not for interactive sessions and not for sampling evals.
- **Request-body dedup** — the one cost lever that is compatible with an append-only harness (see below).
- **MCP multiplexer only** — if you run 5+ MCP servers, TR's multiplexer shares one subprocess per server across all sessions. Add TR as the single MCP entry point, but **don't route file tools through it**.
- **Hook-based integrations (WAF, Staleness alerts)** — see [CLAUDE_CODE_INTEGRATIONS.md](CLAUDE_CODE_INTEGRATIONS.md) for the roadmap of five safe integration mechanisms that respect prompt caching.

### Request-body dedup: compatible, but little to stub

Measured on 76 local transcripts: 0 identical large tool outputs, and Claude
Code already stubs unchanged re-reads itself — expect a near-zero saving in
interactive sessions. The mechanism below stays correct for workloads that do
repeat large payloads.

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
`/v1/messages` path is now measured on Claude Code and removed nothing — see
"Measured 2026-10" below.

### Measured 2026-10: Claude Code A/B on SWE-bench (Sonnet 5.5, headless)

Headless `claude -p` fixed real SWE-bench Lite issues, graded by the official
FAIL_TO_PASS / PASS_TO_PASS tests. Costs are Claude Code's own
`total_cost_usd` (list-price equivalent; subscription auth).

- **Single issues** (3 pytest issues × 3 runs per arm): native $0.043 vs full
  ToolRecall setup (proxy + dedup, MCP server, context-tracker CLAUDE.md)
  $0.055 per task, bugs fixed 8/9 vs 9/9; sessions of 3–8 turns. Dedup removed
  0 tokens, and Claude Code never called a ToolRecall MCP tool.
- **Backlog sessions** (one resumed session fixes five django/db/migrations
  issues in a row, 34–50 turns, 2 sessions per arm): native $0.54, proxy +
  dedup only $0.54, `docs_search` MCP + CLAUDE.md $0.65, the same with
  built-in search blocked $0.67. Dedup again removed 0 tokens: test output
  differs in its timing line and file reads differ in range. Claude Code called
  `docs_search` 1–6 times per session even when told to and with grep blocked,
  switching to `sed`/`cat`/`python -c` instead; the extra tool schemas and
  instructions grew peak context (74–88k vs 56–68k tokens).

The first run of this A/B also exposed a proxy bug: the streaming passthrough
forwarded `Accept-Encoding`, so Claude Code received gzip bytes it could not
parse and re-sent every turn non-streaming (billed twice, reported once).
Fixed in v0.8.22 — do not point `ANTHROPIC_BASE_URL` at an
older proxy.

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

For headless repeat runs (CI, regression), route Claude Code through the proxy
with the `claude-code` canonicalization profile and pinned MCP servers:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8569
export ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: claude-code"
claude -p "..." --strict-mcp-config --mcp-config '{"mcpServers":{}}'
```

Without the header, Claude Code's (always streaming) requests are relayed but
never cached.

Claude Code's own endpoint is Anthropic-shaped, so point `ANTHROPIC_BASE_URL`
at the same proxy and opt in to request-body dedup:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8569
export TOOLRECALL_FORWARD_DEDUP=1   # or [forward_proxy] dedup = true in toolrecall.toml
```

For an A/B baseline arm, per-request opt-out is the header
`X-ToolRecall-No-Dedup: 1`.

> **Bottom line:** For interactive Claude Code sessions ToolRecall does not
> reduce token cost — Claude Code already handles redundant reads itself, and
> measured transcripts had nothing left to remove. Its Claude Code value is
> **headless repeat runs** (forward-proxy replay with the `claude-code`
> profile: −69% on warm repeats of short real tasks that run tests, but
> only −18% on a 5-issue session — savings stop at the first
> non-deterministic tool output), **MCP multiplexing**, and
> **caching expensive external tools**. File caching through MCP has been
> empirically tested and makes sessions **2.4× more expensive** — do not route
> file tools through ToolRecall with Claude Code. Context hints are silent by
> default (checkpoint opt-in).

### Claude Code (CLI) vs. Claude Desktop (GUI App)

Anthropic provides two distinct client interfaces: the terminal-based **Claude Code CLI** (`claude`) and the desktop GUI app **Claude Desktop** (`Claude.app`). They interact with ToolRecall very differently:

| Dimension | Claude Code (CLI) | Claude Desktop (GUI App) |
|---|---|---|
| **Interface** | Terminal CLI agent for autonomous coding | Desktop chat GUI for human conversation |
| **Layer 3: MCP Multiplexing** | ✅ Yes (`~/.claude.json` / `settings.json`) | ✅ Yes (`claude_desktop_config.json`) |
| **Local Docs / Skill Search** | ✅ Yes (`docs_search` FTS5 in 0.6ms) | ✅ Yes (`docs_search` FTS5 in 0.6ms) |
| **Layer 4: Forward Proxy Replay** | ✅ Yes (`ANTHROPIC_BASE_URL=http://localhost:8569`) | ❌ No (closed Electron networking, direct to api.anthropic.com) |
| **Test Output Deduplication** | ✅ Yes (normalizes pytest durations/tempdirs) | ❌ N/A (no terminal test runner loop) |
| **Lifecycle Hooks (Drift Alerts)** | ✅ Yes (`toolrecall hook claude` / `PostToolUse`) | ❌ No (no hook ecosystem) |
| **File Caching Tools (`cached_read`)** | ❌ **Do NOT use** (causes 2.4× cost increase) | ❌ **Do NOT use** (append-only chat context) |

#### Why Claude Desktop cannot use the Forward Proxy or Hooks:
1. **No Proxy Base-URL Override:** Claude Desktop is a compiled Electron app that connects directly to Anthropic's hosted infrastructure using internal session authentication. Unlike the CLI, it does not respect `ANTHROPIC_BASE_URL` for LLM completions.
2. **Interactive Chat vs. Autonomous Loops:** Claude Desktop is driven by human chat prompts. It does not run multi-turn automated bash/test loops where repeated test tracebacks and background file modifications occur.
3. **No Lifecycle Hook API:** Claude Code 2.1+ exposes native `PreToolUse` and `PostToolUse` event hooks. Claude Desktop provides an MCP client runtime but lacks agent lifecycle hooks.

#### Configuring Claude Desktop with ToolRecall:
To use ToolRecall as a shared MCP multiplexer and local doc search engine in Claude Desktop, register it in:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

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
This saves host RAM by sharing back-end MCP server subprocesses across desktop sessions and enables fast FTS5 retrieval over indexed documentation.

---

## Codex CLI — ⚠️ Use selectively

Codex CLI is Node.js (shim N/A). The MCP bridge is useful for multiplexing static tool servers.

**Config:** Use the MCP config format Codex CLI expects. ToolRecall acts as a multiplexing endpoint for read-only tools (time, fetch).

**Avoid:** Do not route file reading/editing through ToolRecall — Codex manages its own file state.

---

## Cursor — ⚠️ Low value

Cursor has its own tool-execution plumbing. The shim is safe (Python process) but largely redundant — Cursor manages its own state aggressively.

**Recommended:** Skip file caching for Cursor sessions. The forward proxy adds value (API cost savings), and `toolrecall setup` configures `~/.cursor/mcp.json` automatically with `--multiplexer-only` to pool MCP servers host-wide without redundant file stubs.

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

### Antigravity Surface Breakdown: IDE (Desktop) vs. CLI (agy) vs. SDK

| Surface | ToolRecall Value | Why & Recommended Setup |
|---|:---:|---|
| **Antigravity Desktop (IDE Chat)** | ⚠️ **Selective** | No LLM base-URL override. Do not enable MCP file caching. **Use:** MCP Multiplexing (−70% RAM across windows) and 0.6ms local FTS5 skills search (`docs_search`). |
| **Antigravity CLI (`agy` interactive)** | ⚠️ **Selective** | Same Google-hosted inference path as IDE chat. Shares `~/.gemini/config/mcp_config.json`. **Use:** MCP Multiplexer, skills search, and `tr` terminal tool wrapper. |
| **Antigravity Headless / Eval Loops** | 🟢 **High** | Uses `export TOOLRECALL_CANONICALIZE=antigravity` to strip volatile timestamps (`<ADDITIONAL_METADATA>`) and session UUIDs for 100% warm replay in CI. |
| **Antigravity Python SDK (`google-antigravity`)** | 🟢 **High** | Wrap expensive tools with `@antigravity.cached_tool(ttl=300, freshness=True)`. Eliminates duplicate external tool calls during subagent handovers. |

**Works:** SDK tool caching (web/GitHub/DB lookups), session replay via the
bridge (`toolrecall replay record`), FTS over skills/rules
(`toolrecall index-dir ~/.gemini/skills`), opt-in MCP multiplex response
cache (`ttl = 0` on write-tool servers).

**Doesn't:** No forward proxy / API caching for interactive chat — Antigravity has no base-URL
override on its IDE/CLI chat surfaces; inference is Google-hosted by design. (The proxy's
`antigravity` canonical profile applies only to headless eval harnesses and SDK pipelines).
Do NOT enable MCP file caching: Antigravity manages its own context (stateful, edit-heavy)
— the same measured 2.4× cost trap as Claude Code.

[Full docs →](antigravity.md)

---

## Multi-Agent Workspaces & Fleet Attribution — ✅ Universal

When running multiple agents on the same machine (for example, Hermes in a terminal, Claude Code for edits, and Antigravity in the IDE), all agents can safely share the same running ToolRecall daemon.

**How it works:**
- **Shared Cache:** Cache keys are pure content hashes (`keys.py`), independent of agent identity. When Agent A reads a file or warms an API response, Agent B immediately gets a 100% cache hit.
- **Per-Agent Attribution:** Set `TOOLRECALL_AGENT_ID=<slug>` in each agent's environment before launch. ToolRecall records hits, misses, and token savings in `agent_cache_stats` without splitting cache keys.
- **Monitoring & Auditing:** Run `toolrecall agent ls` or `toolrecall stats --by-agent` to see the exact token savings and hit rates broken down per agent framework.

See [Agent Tracking](AGENT_TRACKING.md) for full setup, schema details, and cross-machine Turso sync.

## Integration Layer Reference

| Layer | What it does | Requires | Best for |
|-------|-------------|----------|----------|
| **MCP Bridge** (`toolrecall mcp`) | Single MCP entry point → daemon → multiplexed servers + caching | MCP-compatible agent | Any agent. The default. |
| **Forward Proxy** (`:8569`) | Caches API responses by body hash | SDK pointed at `http://localhost:8569` | Any agent making API calls. Saves $ in dev loops. |
| **Python Shim** (`.pth` file) | Transparently caches `open()`, `subprocess.run()`, and `subprocess.Popen()`, auto-strips agent shell wrappers | Python agent, pipx or `toolrecall shim --install` | Python agents without native TR support. Marked experimental. |
| **Go Client** (`tr` binary) | Direct UDS connection to daemon | `go build` in `go-client/` | Shell scripts, CI/CD, non-Python agents, herdr panes. |