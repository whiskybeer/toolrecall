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

### Iteration +3: Empirical AI Alignment via Deterministic Trajectories
The AI safety and alignment community currently suffers from a severe data drought. Researchers often debate theoretical alignment because they lack massive, high-fidelity datasets of autonomous agents operating, failing, and being corrected in real operating systems.
Because ToolRecall passively captures byte-for-byte exact pairs of `[Intent -> Action -> Deterministic OS Observation -> Human Correction]`, it inadvertently generates the perfect empirical dataset for alignment research. By exporting these trajectories, safety researchers can apply DPO to mathematically align models against destructive OS behaviors, anchoring AI alignment in empirical systems engineering rather than philosophy.

### Iteration +4: High-Speed RL (The AlphaGo Paradigm for OS Agents)
Currently, ToolRecall is purely an inference optimizer. However, it inadvertently solves the largest bottleneck in Reinforcement Learning (RL) for AI agents: **Physical OS Latency.**
To train an agent via RL, it must attempt a task tens of thousands of times. If it operates against a real OS, `npm install` takes 10 seconds, and API calls take 2 seconds. The training loop chokes on physical time.
By acting as a frozen, deterministic simulator, ToolRecall allows an agent in training to play against the cache. An API response doesn't take 2 seconds; it takes 0.0001 seconds from RAM. ToolRecall effectively becomes the "Matrix" for AI agents—a high-speed simulation environment where models can iterate through millions of OS failures in minutes, radically accelerating the timeline from raw data to a fully trained local model.


## 3. The A2A Swarm Multiplier (Agent-to-Agent Synchronization)

Until now, ToolRecall has been viewed through a "Human $\rightarrow$ Agent" lens. However, when applied to **Multi-Agent Systems (Swarms)**—where a lead orchestrator delegates tasks to multiple sub-agents (e.g., Research, Coding, QA)—the value of ToolRecall explodes geometrically. It becomes the missing operating system for A2A communication.

### Eradicating "Cascading Hallucinations" (Shared Ground Truth)
The primary failure mode in A2A swarms today is state desynchronization. 
* **The Problem:** Agent A reads a log file at 14:00:01 (Status: Pending). Agent B reads the same log at 14:00:02 (Status: Failed). The orchestrator agent receives conflicting reports and enters an endless hallucination loop trying to resolve the contradiction.
* **The ToolRecall Fix:** By freezing the OS state, ToolRecall provides a **Deterministic Ground Truth**. Agent A triggers the snapshot. Milliseconds later, Agent B sees a byte-for-byte identical universe. The swarm synchronizes perfectly because the environment does not shift beneath their feet.

### Geometric Cost Reduction ($N \times M$ Savings)
When an orchestrator spawns 4 sub-agents, standard frameworks force all 4 agents to re-read the same 100,000-token codebase to establish context.
* **Without L1 Cache:** $4 \times 100,000$ tokens = $400,000$ redundant tokens.
* **With ToolRecall:** The daemon's SQLite database runs in WAL (Write-Ahead Logging) mode, allowing massive concurrency. Agent 1 reads the codebase (Cache Miss). Agents 2, 3, and 4 request the same path milliseconds later and receive the payload in $<0.1$ms from RAM. You don't just solve the $O(N^2)$ loop of a single agent; you instantly eradicate **80% of the swarm's baseline context cost**. They physically share the same L1 "brain".

### IPC as the "Corpus Callosum"
Traditionally, agents must communicate via complex JSON APIs or WebSockets. With ToolRecall, the operating system itself becomes a zero-latency message queue:
* Agent A executes `write_file("plan.md")`.
* ToolRecall instantly invalidates the cache for that specific path.
* Agent B executes `read_file("plan.md")` and gets the fresh state.
* The filesystem, wrapped by the L1 Cache, becomes the native, high-speed communication bus for the entire swarm.

## 4. Protocol Agnosticism & Enterprise Multi-Tenancy

ToolRecall uses `stdio` to achieve zero-config, firewall-immune local deployments. However, the architecture is fully protocol-agnostic.

* **Swapping `stdio` for `HTTP/SSE`:** The heavy lifting is isolated in the Python Daemon (via Unix Sockets). The `stdio` bridge is merely a 100-line adapter. Deploying a network-wide Team Gateway is as simple as swapping the adapter to an `HTTP/SSE` bridge.
* **Namespace Isolation:** While a global Swarm Cache is brilliant for cooperating agents, enterprises require strict data isolation. By introducing a `tenant_id` string during the MCP handshake, ToolRecall partitions cache hits at the database level. Agent A (Project Alpha) and Agent B (Project Beta) share the infrastructure, but their knowledge bases remain cryptographically isolated.
