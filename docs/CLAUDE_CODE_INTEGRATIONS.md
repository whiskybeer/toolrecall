# Claude Code Integrations Architecture & Roadmap

This document outlines the five architectural integration paths between **ToolRecall** and **Claude Code**, detailing their mechanisms, configuration, prompt-cache interactions, and benchmarking protocols.

---

## 1. Architectural Foundations: The Claude Code Contract

Integrating ToolRecall with Claude Code requires respecting two non-negotiable platform invariants:

1. **The Transcript is Append-Only:** External tools cannot retroactively drop, rewrite, or prune previous turns from Claude Code’s active in-memory session.
2. **Anthropic Prompt Caching Sensitivity:** Modifying an earlier prefix block in a request invalidates Anthropic's 90% prompt-cache discount and re-bills the entire history as a cache write (at 1.25× base rate).

> [!WARNING]
> Routing Claude Code’s file reads through ToolRecall’s MCP cached read tools (`cached_read`) resulted in a measured **2.4× cost increase** ($0.57 → $1.34) and a **3.1× latency slowdown** (see [AGENT_COMPATIBILITY.md](AGENT_COMPATIBILITY.md#claude-code)). Claude Code already stubs unchanged file reads natively via `file_unchanged`. Therefore, ToolRecall must never replace native file reads in Claude Code.

Instead, all valid integrations operate on three non-destructive layers:
- **Client-Side Hooks** (`~/.claude/settings.json`): Intercepting tool inputs (`PreToolUse`) or appending forward guidance (`PostToolUse`).
- **Wire-Level Forward Proxy** (`ANTHROPIC_BASE_URL`): Transparent request dedup and response caching.
- **Additive MCP Tools** (`~/.claude.json`): Exposing search, multiplexing, and inspection without overriding native file primitives.

---

## 2. The 5 Improvements: Detailed Analysis

```
┌────────────────────────────────────────────────────────────────────────┐
│ Claude Code Execution Stack                                            │
│                                                                        │
│  [Hook System]                        [MCP Gateway]                    │
│   • PreToolUse (WAF Security)          • FTS5 Knowledge Search         │
│   • PostToolUse (Staleness Alert)      • Multiplexer-Only Bridge       │
│        │                                    │                          │
└────────┼────────────────────────────────────┼──────────────────────────┘
         ▼                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ ToolRecall Daemon (:8569 / UDS)                                        │
│  - context_tracker (read_seq < write_seq monotonic staleness)          │
│  - waf (Layer-4 path allowlist & command blackhole)                    │
│  - mcp_bridge (--multiplexer-only subprocess pooling)                  │
└────────────────────────────────┬───────────────────────────────────────┘
                                 │ Forward Proxy
                                 ▼ (ANTHROPIC_BASE_URL)
┌────────────────────────────────────────────────────────────────────────┐
│ Upstream Provider                                                      │
│  - Replay Cache (claude-code canonical profile for headless runs)      │
│  - Request-Body Dedup (keep-first bash/test output stubbing)           │
└────────────────────────────────────────────────────────────────────────┘
```

---

### Improvement 1: Context Tracker Drift & Staleness Alerts (`PostToolUse` Hook)

* **The Problem:** When Claude Code runs shell commands (`Bash` running `git checkout`, `git stash`, `npm run build`, `make`, or code-generators) or modifies code via external scripts, files previously read into Claude's context are modified on disk. Claude continues reasoning on its outdated in-context memory, causing hallucinated patch locations, broken edits, and extra debugging turns.
* **The Mechanism:**
  ToolRecall’s Context Tracker maintains a monotonic sequence clock per file:
  $$\text{stale} \iff \text{read\_seq}[\text{path}] < \text{write\_seq}[\text{path}]$$
  A lightweight Claude Code `PostToolUse` hook triggers after `Bash`, `Edit`, or `Write` tools. It queries `context_get_stale` via Unix Domain Socket and, if any in-context file was overwritten, emits structured `additionalContext`:
  ```json
  {
    "hookSpecificOutput": {
      "additionalContext": "⚠️ TOOLRECALL DRIFT ALERT: [src/app.py] was modified on disk after your previous read. Your in-context content is stale. Re-read the file before applying further edits."
    }
  }
  ```
* **Prompt Cache Impact:** **Zero.** The alert is purely forward-looking; it appends to the current turn without altering earlier prompt prefixes.
* **Implementation Path:**
  1. Add `toolrecall context stale --hook-json` command returning Claude Code hook JSON output.
  2. Configure `PostToolUse` in `.claude/settings.json`.

---

### Improvement 2: Terminal / Bash Output Deduplication & Log Truncation

* **The Problem:** Iterative test-driven loops (`pytest`, `npm test`, `cargo test`) repeatedly output large, redundant tracebacks or build logs into Claude Code’s append-only transcript, rapidly swelling context costs.
* **The Mechanism:**
  - **Option A (Proxy Dedup):** ToolRecall's Forward Proxy (`[forward_proxy] dedup = true`) normalizes stdout blocks (stripping dynamic test runtimes, timestamps, and PIDs) and replaces subsequent byte-identical occurrences with short stubs pointing to the first occurrence.
  - **Rule for Prefix Stability:** Must operate with `TOOLRECALL_DEDUP_PROTECT_LAST=0` (keep-first): the first output is sent in full to establish the provider prompt cache breakpoint, and identical future repeats are stubbed immediately.
  - **Option B (PostToolUse Hook):** A hook inspects `tool_response` from `Bash` calls; if stdout exceeds 2,000 characters and matches a previous command output, it collapses the output with a reference pointer before it enters the transcript.
* **Prompt Cache Impact:** Neutral if using keep-first; harmful if using delayed tail protection (which rewrites cached prefixes).

---

### Improvement 3: Workstation Security & WAF Gate (`PreToolUse` Hook)

* **The Problem:** Unattended Claude Code execution (`claude -p` or permissions auto-granted) risks accidental data exfiltration (reading `.env`, `.aws/credentials`, SSH keys) or destructive filesystem modification (`rm -rf`, unexpected curl-to-bash pipelines).
* **The Mechanism:**
  A `PreToolUse` hook intercepts `Bash`, `Write`, and `Edit` commands, evaluating the target paths and shell tokens against ToolRecall’s Layer-4 security engine:
  - Path allowlist enforcement.
  - `.env` and secret key air-gapping.
  - Command blackholing (`rm -rf /`, piping unverified URLs to sh).
  If a rule is violated, the hook exits with non-zero status or outputs:
  ```json
  {
    "hookSpecificOutput": {
      "permissionDecision": "deny",
      "permissionDecisionReason": "ToolRecall WAF: Attempted access to sensitive path (.env) blocked by local security policy."
    }
  }
  ```
* **Prompt Cache Impact:** **Zero.** The command is blocked before execution and Claude receives a compact refusal message.

---

### Improvement 4: Codebase Knowledge & Fast FTS5 Search (MCP Tool)

* **The Problem:** Navigating large or unfamiliar codebases via broad `grep` or `find` commands requires multiple round-trips and bloats context with raw directory listings.
* **The Mechanism:**
  Expose ToolRecall’s local SQLite FTS5 full-text search index as an MCP tool (`search_knowledge` / `codebase_fts`) registered in `~/.claude.json`.
  Instruct Claude via `CLAUDE.md`:
  > *"For wide codebase searches or finding definitions across unfamiliar repos, use `search_knowledge` first to locate exact file and line offsets."*
* **Prompt Cache Impact:** **Zero.** Standard MCP tool request/response.

---

### Improvement 5: Multiplexer-Only Integration via `toolrecall setup`

* **The Problem:** Running `toolrecall setup` or adding ToolRecall to Claude Code without flags registers file-caching tools (`cached_read`, `cached_patch`), which triggers the measured 2.4× cost disaster.
* **The Mechanism:**
  Update `toolrecall/cli.py` and `_write_claude_json_config()` to default `multiplexer_only=True` for Claude Code.
  This allows Claude Code to share a single daemon and process pool across multiple MCP servers (Postgres, GitHub, Puppeteer, custom servers) with 15-minute idle timeouts, slashing memory overhead without touching file tools.
* **Prompt Cache Impact:** **Zero.**

---

## 3. Evaluation & Suitability Matrix

| Feature | Primary Benefit | Harness Fit | Prompt Cache Risk | Benchmark Suitability | Rank |
|---|---|---|---|---|:---:|
| **1. Staleness Alerts** | Prevents drift bugs & hallucinated edits | Native (`PostToolUse` `additionalContext`) | **Zero** | **Highest** (Turns, Accuracy, Cost) | **#1** |
| **2. Bash Output Dedup** | Cuts redundant test log tokens | Proxy / `PostToolUse` | Medium (Requires keep-first) | High (Token volume on test loops) | **#2** |
| **3. WAF Security Gate** | Workstation protection & air-gapping | Native (`PreToolUse` deny) | **Zero** | Low (Pass/Fail security audit) | **#3** |
| **4. FTS5 Search MCP** | Fast cross-repo search | Additive MCP tool | **Zero** | Medium (Tool selection variance) | **#4** |
| **5. Multiplexer Setup** | Eliminates MCP process bloat | `~/.claude.json` config | **Zero** | Low (System RAM benchmark) | **#5** |

---

## 4. Empirical Benchmark Results

Full methodology and raw receipts in [bench/results/CLAUDE_CODE_INTEGRATIONS_REPORT.md](../bench/results/CLAUDE_CODE_INTEGRATIONS_REPORT.md).

### Track 1: Context Staleness & Out-of-Band Drift Protection
Evaluates multi-turn refactoring where an intermediate build step mutates source files out-of-band:
- **Baseline Claude Code:** 6 turns, 1 failed patch attempt, 15,200 prompt tokens ($0.0310).
- **Claude Code + ToolRecall Stale Hook:** 4 turns, 0 failed patch attempts, 9,400 prompt tokens ($0.0188).
- **Delta:** **−33.3% turns**, **−38.2% prompt tokens**, 100% elimination of failed patch attempts.

### Track 2: Bash & Test Output Normalization and Deduplication
Evaluates a 10-turn iterative TDD debugging loop with 5 test executions (~2.4 KB each):
- **Native Claude Code:** 21,640 cumulative prompt tokens ($0.0065).
- **ToolRecall Normalized Dedup (`protect_last=0`):** 9,094 cumulative prompt tokens ($0.0027) with 4 duplicate output blocks stubbed.
- **Delta:** **−58.0% prompt token reduction** across the session without breaking Anthropic prompt-cache prefix stability.
