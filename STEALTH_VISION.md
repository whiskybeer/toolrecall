# ToolRecall: Stealth Vision & Iterative Roadmap

## Current State (v0.3/v0.4)
- **Local L1 Cache & MCP Multiplexer**
- Drastically reduces latency (<1ms) and breaks the O(N^2) context snowball locally.
- Forces deterministic cache-hits for cloud LLM providers (Anthropic/OpenAI).

## Is Encryption Needed? (Security Analysis)
**Currently: No.**
* The Daemon uses Unix Domain Sockets (`/run/user/1000/toolrecall.sock`). By Linux kernel design, this is restricted to the current user via `chmod 700`. No other user on the system can intercept the traffic.
* The SQLite DB (`~/.toolrecall/cache.db`) and API keys (`.env`) are also protected by OS file permissions.
* **When it becomes needed:** If deployed to a corporate laptop with strict MDM (Mobile Device Management) policies, caching sensitive data (PII, trade secrets) in a plain SQLite file might violate compliance. At that stage, implementing **SQLCipher** (transparent AES-256 encryption for SQLite) will be necessary. If the Daemon ever exposes a TCP port (e.g., `127.0.0.1:8080`), TLS and API-Key authentication become mandatory.

## Thinking Two Iterations Ahead

### Iteration +1: The Multi-Tenant Team Gateway
ToolRecall moves from the local laptop to a shared Team VPC (Virtual Private Cloud).
* **The Shared Cache:** Developer A asks an agent to read and analyze a massive 500k-token legacy repository. The gateway caches the results. Ten minutes later, Developer B's agent queries the same repository. The gateway serves the result instantly to Developer B.
* **Global Rate Limit Management:** Instead of 50 developers hitting GitHub's API and getting rate-limited, the Team Gateway multiplexes and caches all MCP requests, acting as a massive corporate proxy for AI Agents.

### Iteration +2: The Synthetic Data Flywheel (L0 AI)
ToolRecall is currently secretly exporting trajectories (`export-dataset`).
* **The Flywheel:** Every time a human corrects an agent, or an agent successfully completes a complex multi-step debugging task, ToolRecall has the *exact* frozen state of the OS and the exact actions taken.
* **Self-Distillation:** These logs aren't just a cache—they are high-quality DPO (Direct Preference Optimization) training data. ToolRecall can automatically use this data to fine-tune a small, local open-source model (like Llama 3 8B).
* **The Endgame:** Eventually, the local model becomes so good at handling the company's specific codebase and API quirks that it intercepts the prompt *before* it even goes to Claude or DeepSeek. ToolRecall transitions from an L1 Data Cache to an **L0 Reasoning Engine**.


## Bonus: Protocol Agnosticism & Swarm Architecture

The current setup utilizes `stdio` for a very specific reason: It is the zero-config gold standard for local agents, completely bypassing firewalls and port conflicts. However, the architecture is designed to be fully protocol-agnostic.

### 1. Swapping `stdio` for `HTTP/SSE`
Because the heavy lifting is completely isolated in the background Python Daemon (communicating via Unix Sockets), the `stdio` bridge is merely a 100-line adapter. Deploying a network-wide Team Gateway in the future is as simple as swapping the `stdio` adapter for an `HTTP/SSE` bridge. The core caching engine remains untouched.

### 2. The Local "Swarm Cache"
Because ToolRecall uses SQLite in WAL (Write-Ahead Logging) mode, it effortlessly handles high-concurrency access. If a developer runs Cursor, Aider, and Claude Code simultaneously, they all share the exact same physical "brain" (`cache.db`). If Agent A pays the latency/token cost to fetch a massive API payload, Agent B gets the cache hit 2 seconds later.

### 3. Namespace Isolation (Enterprise Multi-Tenancy)
While a global Swarm Cache is brilliant for a single developer, enterprises need strict data isolation. The architecture is perfectly suited for this: By simply introducing a `namespace` or `tenant_id` string during the MCP handshake, ToolRecall can partition cache hits at the database level. Agent A (working on Project Alpha) will never bleed data or context into Agent B (working on Project Beta), while both still benefit from the exact same highly-optimized L1 infrastructure.
