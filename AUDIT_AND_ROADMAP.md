# Architecture Audit & Roadmap

This document outlines a strict, neutral architectural audit of ToolRecall v0.3.0. It strips away marketing terminology to highlight the exact technical trade-offs, structural risks, and unresolved edge cases. 

These points serve as the **Roadmap for v0.4.0 and Enterprise Readiness**.

---

## 1. The Token Savings Dependency
**The Reality:** ToolRecall *does not manage the LLM context window*. It intercepts and serves tool executions instantly. The massive token savings (e.g., 141M tokens in benchmarking) are entirely dependent on the *Orchestrator* (like Hermes or Claude Code) actively pruning old outputs from its conversational history.
**The Risk:** If ToolRecall is plugged into a naive Agent framework (e.g., a basic LangChain loop) that infinitely appends every response to the prompt, ToolRecall will reduce execution latency by 1000x, but will save zero API tokens.
**Future Improvement:** Document explicit instructions on how Agent frameworks should drop context when using ToolRecall.

## 2. Cache Invalidation Vulnerabilities (State Desync)
**The Reality:** The invalidation logic is susceptible to high-frequency race conditions and external state changes.
- **File System (`mtime`):** Relying on `mtime` is dangerous if an agent writes to a file and reads it back within the sub-second resolution limits of older filesystems.
- **External State (MCP):** If an agent uses a mutation tool (e.g., closing a GitHub issue) but lists issues again within the 60-second TTL window, it receives cached (open) status. The agent will hallucinate that its action failed.
**Future Improvement:** 
- Implement sub-millisecond hashing alongside `mtime`.
- Implement active Cache Invalidation via MCP mutation tracking (if a tool name contains "update", "write", or "delete", automatically purge the cache for that server).

## 3. Single Point of Failure (SPOF) & IPC Fragility
**The Reality:** The Python daemon is a strict SPOF.
- If the daemon crashes (e.g., due to an OOM kill from an underlying Node.js MCP memory leak), the `.sock` file may be orphaned.
- Subsequent startups will fail with `Address already in use` until the `.sock` is manually deleted.
- The UDS server uses basic thread handling, which is not stress-tested for 50+ concurrent multi-agent environments.
**Future Improvement:** 
- Add aggressive socket cleanup and PID-file verification on daemon boot.
- Transition UDS handling to asynchronous event loops (`asyncio`) for enterprise concurrency.

## 4. Security Sandbox Limitations (Binary WAF)
**The Reality:** The "WAF" features are currently binary switches.
- `allow_terminal = true` opens the door entirely. There is no granular parsing to allow `npm run build` but block `rm -rf /`.
- `allowed_paths` relies on string and `abspath` normalization. Edge cases with complex symlinks or Unicode directory traversal remain theoretical escape vectors for Prompt Injections.
**Future Improvement:** 
- Implement Regex-based terminal whitelists (e.g., `^npm run (lint|build|test)$`).
- Strict symlink resolution (`os.path.realpath`) enforced on all file reads.

## 5. Maintenance Burden (Multiplexer Zombie Processes)
**The Reality:** ToolRecall inherits the lifecycle management of arbitrary external MCP servers (`npx`, `uvx`).
- Node.js subprocesses are notoriously difficult to kill cleanly via `SIGTERM`. The 15-minute idle "Reaper" thread might leave orphaned child processes accumulating over weeks of uptime.
**Future Improvement:** 
- Implement OS-level process group termination (`os.killpg`) to ensure entire MCP process trees are eradicated during idle timeouts.
