# Architecture Audit & Roadmap (Path to v0.6.0)

This document outlines a strict, neutral architectural audit of ToolRecall. It strips away marketing terminology to highlight the exact technical trade-offs, structural risks, and unresolved edge cases. 

These four systemic bottlenecks serve as the **Development Roadmap for v0.6.0**.

---

## 1. The "Phantom Bug" Trap (Cache Invalidation)
**The Reality:** Cache invalidation is the hardest problem in computer science. ToolRecall freezes the world for the agent. If an agent queries a GitHub issue via an MCP server, ToolRecall caches the response for the `TTL` duration (e.g., 60s). If a human closes that issue at second 5, the agent remains blind to this reality for the remaining 55 seconds.
**The Consequence:** The agent operates on stale data and might attempt to fix bugs that no longer exist on the server.
**v0.6.0 Solution:** 
- Implement active Cache Invalidation via MCP mutation tracking. If an MCP tool name implies mutation (contains "update", "write", "delete", "close"), ToolRecall will automatically purge the cache for that specific server.

## 2. Real-Time Blindness (The Polling Problem)
**The Reality:** Agents often need to wait for external processes (e.g., polling a CI pipeline every 10 seconds until it returns "SUCCESS").
**The Consequence:** Because ToolRecall is deterministic, it intercepts the status check. If the first check returned "PENDING", ToolRecall will instantly return "PENDING" for all subsequent checks within the TTL. The agent spins 50 loops in a millisecond, assumes the pipeline is stuck, and fails the task.
**v0.6.0 Solution:**
- Document and implement `ttl=0` overrides for specific dynamic tools (status checks, CI logs) to bypass the cache and preserve the agent's sense of time.

## 3. The "Lazy Agent" Illusion (Context Window Bloat)
**The Reality:** ToolRecall optimizes I/O latency and guarantees deterministic *Forced Cache Hits* at the cloud provider level. However, it *does not* manage the LLM context window itself.
**The Consequence:** If the agent framework (e.g., a naive LangChain loop) fails to drop read files from its conversational history, the JSON payload sent to Anthropic/OpenAI will still grow infinitely. ToolRecall fixes I/O latency, but it cannot fix bad prompt management by the agent.
**v0.6.0 Solution:**
- Build strict context-management guidelines into the documentation. 
- (Exploratory) Expose an MCP tool `toolrecall_drop_context` to actively help agents manage their token footprints.

## 4. Single Point of Failure (SPOF)
**The Reality:** Before ToolRecall, a failing `subprocess.run()` only crashed one tool execution. Now, every file read, terminal command, and MCP server request is routed through a single background process: the ToolRecall Python Daemon.
**The Consequence:** If this daemon hangs or crashes (e.g., a Node.js MCP server memory leak dragging the Python process down), the Unix Socket dies. The entire AI agent becomes instantly deaf and blind until the daemon is manually restarted.
**v0.6.0 Solution:**
- Transition the daemon's UDS handling to a robust `asyncio` event loop.
- Implement aggressive socket cleanup, heartbeat monitoring, and OS-level process group termination (`os.killpg`) to eradicate zombie MCP processes during idle timeouts.

---

*Update (v0.3.0/v0.5.0 patches applied):*
* Security: The OOM vulnerability caused by agents attempting to `cached_read` massive payloads (e.g., 2GB `/var/log/syslog`) was patched with a hard 5MB read limit.*
* Stability: SQLite Garbage Collection (`toolrecall gc`) and robust Daemon logging (`daemon.log`) were implemented to prevent WAL bloat and blind crashes.*
