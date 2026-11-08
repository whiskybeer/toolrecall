# Claude Code Integrations

What ToolRecall does for **Claude Code**, what it measurably saves, and what it
does not. Every number below comes from live Claude Code runs or from replaying
real local transcripts; methodology and raw figures are in
[bench/results/CLAUDE_CODE_INTEGRATIONS_REPORT.md](../bench/results/CLAUDE_CODE_INTEGRATIONS_REPORT.md).

> **Verdict.** ToolRecall saves tokens for Claude Code in one situation: **the same
> headless task runs again** (CI re-runs, eval/benchmark iteration, agent regression
> tests). Forward-proxy replay then serves repeated calls from cache: identical warm
> reruns cost **−96%** vs the cold run on a short task (n=8, 7 of 8 fully replayed).
> On real SWE-bench tasks (Sonnet, official grading) reruns cost **−58.8%** vs native
> Claude Code (n=10 per arm; 8 of 10 fully replayed), −30.9% including first runs.
> For one-off and interactive sessions it saves ~0%: Claude Code already stubs
> unchanged re-reads, and repeated tool output does not occur in real sessions
> (0 of 3,015 tool results in 193 local sessions).

---

## 1. The Claude Code contract

1. **The transcript is append-only.** External tools cannot drop or rewrite earlier
   turns of a Claude Code session.
2. **Prompt caching is prefix-exact.** Changing an earlier block invalidates the
   provider cache and re-bills the history as a cache write (1.25×).
3. **Claude Code already handles redundant reads.** An unchanged re-`Read` returns a
   short `file_unchanged` stub, and an `Edit` to a file changed since it was read is
   rejected with "File has been modified since read" — a stale edit is never applied.

> [!WARNING]
> Routing Claude Code's file reads through ToolRecall's MCP cached-read tools
> (`cached_read`) measured a **2.4× cost increase** and a 3.1× slowdown (see
> [AGENT_COMPATIBILITY.md](AGENT_COMPATIBILITY.md#claude-code)). ToolRecall must never
> replace native file tools in Claude Code; `toolrecall setup` therefore registers
> Claude Code as multiplexer-only.

---

## 2. Repeated headless runs — forward-proxy replay (the saving)

Claude Code streams every request. With the `claude-code` canonicalization profile
the proxy keys requests without per-run identifiers (session metadata, test-runner
durations) and stores each completed stream; an identical later request is replayed
from cache and never reaches the provider.

```bash
toolrecall hook claude ci-env > tr-ci.sh   # proxy URL, profile header, PYTHONHASHSEED=0, pinned MCP
source tr-ci.sh
claude -p "<task>" "${TR_CLAUDE_FLAGS[@]}"
```

Replay holds only while the requests stay identical:

- **Same commit.** Claude Code sends recent commits and git status with the first
  request; a new commit changes call 1 and nothing replays.
- **Deterministic tool output.** The first output that differs (an `ls -l` mtime, a
  random port, unordered set printing) ends replay for the rest of the session.
- **Not for evals that sample the model:** a replayed call returns the earlier answer.
- **Handled by the profile:** Claude Code's `Bash` tool schema varies per session
  (background-timeout default 600000 vs 1800000 ms) and parallel tool results arrive in
  completion order; the `claude-code` profile keys both order- and variant-independently.
- **A replayed rerun returns the cached run's outcome**, right or wrong: it is a cheap
  repeat, not a new attempt.

**Why did replay stop?** Start the proxy with `TOOLRECALL_KEY_TRACE_DIR=<dir>` and run
`toolrecall keytrace <dir>`. For each miss it prints the first differing field against
the closest earlier request, e.g.:

```
#10   MISS msgs=4    first difference at .messages[3].content[0].content
        earlier: … 4096 Jan  1 10:00 .\ndrwxr-xr-x 3 …
        now:     … 4096 Jan  1 10:01 .\ndrwxr-xr-x 3 …
```

The trace holds prompts and tool output: it is local, opt-in and never changes keys.

> **Correction.** Stream caching for Anthropic (Claude Code) traffic only works since
> the `message_stop` fix: the stream tee used to accept only OpenAI-style `[DONE]`
> streams and discarded every Claude Code stream. The earlier replay results in
> [bench/claude_code_replay/RESULTS.md](../bench/claude_code_replay/RESULTS.md) were
> served from non-streaming fallback retries, not from the stream cache.

---

## 3. Where interactive-session tokens go — `toolrecall audit claude-code`

```bash
toolrecall audit claude-code --days 30
```

Read-only report from `~/.claude/projects/*/*.jsonl`: the exact fixed starting context
per session (system prompt, tool definitions, instruction files, skill/MCP/agent
listings), its share of list-price-weighted cost, and the parts you control ranked by
cost share. On the reference machine (25 sessions, 2,428 calls) the starting context
was 13.7% of cost; the controllable parts (skill listing, instruction files, MCP
instructions) were ~2.3%, and each 1,000 always-loaded tokens cost ~0.3%. The audit
shows what trimming would buy before anyone spends effort on it.

---

## 4. Hook adapter (`toolrecall.adapters.claude_code`)

```bash
toolrecall hook claude print-config   # settings.json snippet
```

### Staleness alert (`PostToolUse`)

Records each file Claude reads or edits. After a `Bash` call it checks those files
and, if a shell command changed one on disk, adds `additionalContext` telling Claude
to re-read it. The agent's own `Edit`/`Write` never counts as drift. The daemon's
global stale list (shared across agents) is merged only with
`TOOLRECALL_CC_DAEMON_STALE=1`.

Measured (Haiku, n=6 per arm): when a build step silently rewrites a file Claude has
read, the hook saves the rejected-edit round trip (**−10% cost**, 6 vs 8 turns);
when the prompt already tells Claude to inspect the file, it is neutral (−1.2%, within
noise). It is a small correctness aid, not a general token saver.

### WAF gate (`PreToolUse`, opt-in `TOOLRECALL_CC_WAF=1`)

Denies `Bash`/`Write`/`Edit` calls touching `.env` files or SSH keys and a few
destructive command patterns (`rm -rf /`, `curl … | sh`, fork bomb). Not cost-related;
not benchmarked.

---

## 5. Request-body dedup with output normalization (not recommended for Claude Code)

`[forward_proxy] dedup = true` plus the `claude-code` profile (or
`TOOLRECALL_DEDUP_NORMALIZE=1`) stubs tool outputs that repeat an earlier one apart from
timings, timestamps, temp paths or addresses (keep-first, so cached prefixes stay
stable). The stub keeps the output's own last line.

Measured: on a task built to repeat identical pytest output three times it is
**cost-neutral** (+0.7%, n=6; the model writes more output about the collapsed runs),
and replaying 193 real sessions found **nothing to stub**. Leave it off for Claude Code.

---

## 6. MCP: multiplexer only

`toolrecall setup` registers Claude Code with `--multiplexer-only`: Claude Code shares
one daemon and process pool across MCP servers without ToolRecall's file tools.
`docs_search` is available for indexed documentation corpora. Neither is a token
saving; not benchmarked here.
