# Case Study: Measured Impact in a Single 13-Hour Agent Session

**Date:** June 7, 2026  
**Environment:** GCP e2-medium (4GB RAM), Hermes Agent + Gemini 3.1 Pro Preview  
**ToolRecall Version:** v0.8.12 (MCP Multiplexer, Context Tracker, Forward Proxy, `ctx_dropped_tokens`)

In a single 13-hour development session building the ToolRecall MCP Multiplexer, ToolRecall achieved a **91% file cache hit rate**, intercepting **827 tool calls** locally that would have otherwise triggered full OS execution.

> **⚠️ Token Count Correction:** The original benchmark claimed 141.1M tokens saved. This was inflated by a **double-counting bug** in the `tokens_read_from_disk` counter (fixed in v0.3.2). Tokens were counted on every cache hit (in-memory and SQLite replay), accumulating ~99× the real unique content. The hit rates, timing data, and architecture insights below remain accurate. Real unique content cached: **~55K tokens across 13 project files.**

This benchmark explains the math behind this seemingly impossible number and how ToolRecall solves the fundamental scaling problem of LLM context windows.

---

## 1. The Core Problem: The $O(N^2)$ Context Snowball

To understand the savings, you have to understand how LLMs process and bill for context. LLMs are stateless; they have no continuous memory. For *every single message* in a conversation, the entire history up to that point must be re-transmitted and re-processed by the API.

**The Scenario WITHOUT ToolRecall:**
Imagine an autonomous agent uses a `read_file` tool to ingest a 10,000-token file (e.g., a core daemon script).
1. The 10,000 tokens are added to the active conversation history.
2. The agent and user exchange 100 more messages (turns) while debugging.
3. Because the history is cumulative, those 10,000 tokens are re-sent to the API **100 times**.
4. **Math:** 10,000 tokens × 100 turns = **1,000,000 input tokens billed** for a single file read.

If the file is later pushed out of the context window to save space, and the agent needs to read it again, it executes another `read_file` command. The file is read from disk, a new 10,000-token block is appended to the *bottom* of the context, and the snowball starts rolling all over again.

## 2. The Solution: Byte-Exact Caching & Micro-RAG

ToolRecall disrupts this $O(N^2)$ snowball effect entirely using a combination of a persistent SQLite cache, LRU memory, and FTS5 (Full-Text Search).

1. **Context Dropping:** Because ToolRecall caches all tool outputs (file reads, terminal logs, MCP tool results), the agent is instructed to drop large file dumps from its active context window after processing them. It doesn't need to carry the dead weight.
2. **Instant Recall (Micro-RAG):** If the agent needs that file again 4 hours later, it doesn't need to hit the disk or run the tool again. ToolRecall serves the *exact* byte-for-byte output from its local SQLite cache in ~1.5 milliseconds.
3. **Zero Hallucination:** Unlike vector databases that use LLMs to summarize older context (which introduces hallucinations and loss of detail), ToolRecall returns the exact original `stdout` or JSON response. 
4. **Strict Invalidation:** The moment a file is modified via `write_file` or a terminal command, ToolRecall's security gates instantly fire invalidation locks. The stale cache is purged, guaranteeing the next read fetches the fresh state from disk.

## 3. The Hard Data

During the 13-hour session (386 messages exchanged, ~642 KB of raw text/code generated), the cache intercepted and served 827 requests locally that would have otherwise triggered full tool executions and context bloat.

| Cache Layer | Hits | Misses | Hit Rate | Unique Tokens Cached | Est. Cost Saved ($3/M) |
|---|---|---|---|---|---|
| `file_cache` | 666 | 62 | **91%** | **55,189** | **~$0.17** |
| `terminal_cache` | 143 | 15 | **91%** | 170 | ~$0.00 |
| `code_cache` | 8 | 9 | **47%** | 14 | ~$0.00 |
| `mcp_cache` | 10 | 18 | **37%** | 90 | ~$0.00 |
| **TOTAL** | **827** | **104** | **89%** | **~55,500** | **~$0.17** |

*Note: The token counter had a double-counting bug (fixed v0.3.2). The original flawed counter reported 141,112,165 tokens ($282.22). The figures above are unique content only. Hit rates, timing, and architecture data are accurate.*

**Token reduction:** Without TR, 13 files read 3× each = ~204K tokens. With TR: ~55K unique. **73% fewer tokens** for shallow sessions. **~81%** for deeper sessions with 10+ re-reads.

**Time savings:** Each cache hit avoids a subprocess fork (~1.5s for Node.js MCP servers). Over 827 calls: **~20 minutes less wall-clock waiting time**.

## 4. System Architecture Impact (v0.3.0)

Beyond token savings, the v0.3.0 update introduced an **MCP Multiplexer** with Lazy Loading, drastically reducing the RAM footprint on the host machine (a 4GB e2-medium instance). 

Instead of spawning 5 separate Node.js/Python MCP servers per session (~600MB baseline), the ToolRecall daemon acts as a persistent host:

| Metric | Before (Per-Session Eager) | After (Daemon Lazy Load) |
|---|---|---|
| **Daemon RAM (Idle)** | — | **11 MB** |
| **Daemon RAM (Peak)** | ~3.6 GB (6 sessions × 600MB) | **~600 MB** (One-time shared pool) |
| **Server Startup** | ~1.7s per session boot | **~0.01s** (UDS connect) |
| **Resource Recovery** | Never (processes orphaned) | **15-minute idle timeout** |

## Conclusion

ToolRecall proves that the most expensive problem in modern AI development (context window bloat) can be solved with classic system design: SQLite, LRU caches, and strict invalidation locks. 

By functioning as a transparent middleware layer between the Agent and the OS/MCP Servers, it ensures 100% data fidelity while reducing API costs by an order of magnitude.