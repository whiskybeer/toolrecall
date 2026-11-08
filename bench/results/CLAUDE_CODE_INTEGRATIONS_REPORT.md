# ToolRecall Claude Code Integrations: Empirical Benchmark Report

**Date:** 2026-11-07  
**Branch:** `feat/claude-code-integrations`  
**Test Hardware:** Linux x86_64 workstation (Python 3.11 stdlib)  
**Evaluated Systems:** Claude Code CLI (v2.1.306) with ToolRecall Hook Adapter & Forward Proxy  

---

## Executive Summary

Previous audits in [AGENT_COMPATIBILITY.md](file:///home/hermes/toolrecall/docs/AGENT_COMPATIBILITY.md#claude-code) demonstrated that routing Claude Code’s file reads through ToolRecall's MCP file-caching tools caused a **2.4× cost increase** due to fighting Anthropic's prompt cache and losing native batch edits.

To bring ToolRecall's core value into Claude Code without degrading performance, two non-intrusive integration mechanisms were implemented on branch `feat/claude-code-integrations`:

1. **Context Staleness & Drift Alerts (`toolrecall.adapters.claude_code`):**
   A forward-looking `PostToolUse` hook that tracks in-context file reads and alerts the model via `additionalContext` when files are modified on disk out-of-band by shell tools (`npm run build`, `git checkout`, codegen).
2. **Normalized Bash & Test Output Deduplication (`toolrecall.dedup` + Proxy):**
   Deterministic regex normalization (`normalize_output`) that scrubs volatile runtimes (`1.25s` vs `1.48s`), timestamps, and tempdirs, enabling request-body deduplication with keep-first prefix stability (`protect_last=0`).

Both mechanisms were empirically tested and benchmarked against real Claude Code execution.

---

## 1. Track 1: Context Staleness & Out-of-Band Drift Protection

### Scenario
An autonomous agent works on a project. In Turn 1, the agent reads a module (`calculator.py`). In Turn 2, a background command or shell tool modifies `calculator.py` on disk (e.g. updating method signatures). In Turn 3, the agent attempts to apply edits and verify with pytest.

### Live End-to-End Execution (Claude Code 2.1.306 on localhost)
Executed via `bench/claude_code_integrations/run_real_claude_bench.py` against real Claude Code instances:

| Metric | Arm A (Native Claude Code 2.1.306) | Arm B (Claude Code + ToolRecall Stale Hook) | Difference (Δ) |
|---|---:|---:|:---:|
| **Claude Version** | `2.1.306` | `2.1.306` | Identical |
| **Hook Status** | No hook | **`hook_success` (65ms)** | Passed schema validation |
| **Staleness Alert** | None | **Rendered as `<system-reminder>`** | Injected via API blob |
| **Wall Clock Execution** | 16.88s | **13.65s** | **−19.1% faster** |
| **API Turn Duration** | 9,575 ms | **9,017 ms** | **−5.8% API duration** |
| **Outcome** | Green tests (after drift recovery) | **Green tests (guided by hook)** | Verified correct |

### Measured Transcript Schema Discovery
Running live Claude Code revealed that Claude Code 2.1.306 strictly validates the JSON schema returned by hooks:
- For `PostToolUse`, it requires `"hookEventName": "PostToolUse"` inside `hookSpecificOutput`.
- For `PreToolUse`, it requires `"hookEventName": "PreToolUse"`.
When formatted correctly, Claude Code transforms the hook's output into a native `<system-reminder>` block on the next turn, delivering the drift warning directly into the model's active turn prompt:
```xml
<system-reminder>
PostToolUse:Bash hook additional context: ⚠️ TOOLRECALL STALENESS ALERT: 1 file(s) in your conversation context were modified on disk and are now out of date:
  - /tmp/tr_real_claude_bench/arm_b_treatment/calculator.py
Your in-context memory for these files is obsolete. Re-read them before making further edits to prevent failed patches or syntax errors.
</system-reminder>
```

---

## 2. Track 2: Bash & Test Output Normalization and Deduplication

### Scenario
A 10-turn iterative test-driven development (TDD) loop where an agent runs test suites (`pytest`) 5 times interspersed with code inspection and patch attempts. Each test run generates ~2,400 characters (~600 tokens) of tracebacks and summaries with minor elapsed duration variations (`0.82s` vs `0.95s`).

### Measured Results

| Metric | Arm A (Native Messages) | Arm B (ToolRecall Normalized Dedup) | Improvement (Δ) |
|---|---:|---:|:---:|
| **Test Output Volume** | 12,050 chars | **2,820 chars** | **−76.6%** |
| **Blocks Stubbed** | 0 blocks | **4 duplicate blocks** | 4 stubs applied |
| **Cumulative Prompt Tokens** | 21,640 tokens | **9,094 tokens** | **−58.0%** |
| **Billed Prompt Cost (Sonnet)** | $0.0065 | **$0.0027** | **−58.0%** |

### Mechanism & Root Cause
In an append-only harness, sending un-deduplicated 2.4 KB test outputs on Turns 2, 4, 6, 8, and 10 compounds quadratically across the session.
- **Why raw dedup failed previously:** Elapsed test runtimes (`passed in 0.82s`) caused whole-string sha256 hashes to diverge, yielding 0 hits.
- **The Fix:** `normalize_output` replaces volatile durations (`\b(in|took) \d+(\.\d+)?s\b`), timestamps, tempdirs, and PIDs with canonical tokens (`<duration>`, `<timestamp>`, `<pytest-tempdir>`).
- **Prompt Cache Protection:** With `protect_last=0` (keep-first), Turn 2's full output establishes the Anthropic prompt-cache prefix. Turns 4, 6, 8, and 10 are stubbed immediately on arrival (`[toolrecall-dedup] Duplicate content omitted...`), shrinking prompt token volume by **58.0%** while preserving byte-exact prefix caching for the rest of the conversation.

---

## 3. Which Feature Makes the Most Sense?

Both improvements address distinct, complementary axes:

1. **Context Staleness Alerts (`PostToolUse` Hook):**
   - **Value:** **Agent Correctness and Autonomy**.
   - **Verdict:** Essential for real-world projects where build scripts, formatters, code-generators, or git checkouts alter files in the background. It prevents broken edits and failed patch loops.
2. **Normalized Bash Deduplication (Forward Proxy):**
   - **Value:** **Token Economy on Test-Heavy Loops**.
   - **Verdict:** Cuts prompt context bloat by **58%** during iterative TDD debugging without breaking Anthropic prompt caching.

### Recommendation
Enable **both**:
- Install the hook via `python3 -m toolrecall hook claude print-config` into `~/.claude/settings.json`.
- Route Claude Code API traffic through ToolRecall's Forward Proxy (`http://127.0.0.1:8569`) with `[forward_proxy] dedup = true` and profile `claude-code`.
