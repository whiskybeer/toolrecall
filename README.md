# ToolRecall: The L1 Cache for LLM Agents

**An $O(N^2)$ Context Mitigation & MCP Multiplexer Middleware**

ToolRecall is a deterministic middleware layer (API Gateway/WAF) for autonomous AI agents like Claude Code, Cursor, Aider, and Hermes. It sits between the agent and the operating system, caching tool executions and managing external MCP servers via Unix Domain Sockets (IPC).

The core value proposition: **It breaks the $O(N^2)$ context snowball effect.** 
In a recent benchmark, ToolRecall saved **141.1 million input tokens (~$282)** in a single 13-hour session by serving tool results from a local SQLite FTS5 database in 1.5ms.

👉 **[Read the 141M Token Benchmark Case Study](BENCHMARK.md)**

---

## The Core Problem: The Context Snowball

LLM context windows are stateless. Every time an agent reads a 10,000-token file to debug an issue, those 10,000 tokens are added to the history. If the session continues for 100 turns, those same 10,000 tokens are re-transmitted to the API 100 times, costing 1,000,000 billed input tokens.

**The ToolRecall Solution (Micro-RAG):**
1. Agents read the file once.
2. The agent is instructed to *drop* the file dump from its active context window to save space.
3. If the agent needs the file again hours later, ToolRecall serves the *exact byte-for-byte output* from its local SQLite cache instantly.
4. If the file is modified (`write_file`), ToolRecall's locking mechanism instantly invalidates the cache.

**Zero Hallucination:** Unlike Vector-DBs that use LLMs to summarize older context (which introduces hallucinations), ToolRecall returns raw `stdout` and JSON data deterministically.

---

## Architecture & Security (The "Armor")

ToolRecall is not just a cache; it is a **Security Sandbox (WAF)** for LLM agents to mitigate Prompt Injections.

1. **Daemon-Based (IPC):** ToolRecall runs as a persistent daemon. Agents communicate with it via Unix Domain Sockets (`/run/user/1004/toolrecall.sock`). There are no open TCP ports (immune to SSRF and port scanning).
2. **Hard Allowlist:** Instead of trusting the LLM's system prompt to "not read passwords," ToolRecall enforces a hard Python-level allowlist (e.g., `["~/projects", "~/.hermes"]`). If a prompt injection tricks the agent into reading `~/.ssh/id_rsa`, the middleware blocks it with `Access Denied` before the file is ever touched.
3. **Terminal Gating:** By default, `allow_terminal = false`. Read-operations are allowed, but shell commands are filtered unless explicitly whitelisted by the developer.

---

## Features (v0.3.0)

### 1. Byte-Exact Tool Caching
- **File Cache:** Invalidates instantly based on file modifications (`mtime`) and internal invalidation locks.
- **Terminal Cache:** Caches read-only shell commands based on TTLs (e.g., `git status` for 30s).

### 2. The MCP Multiplexer
Running 5 different MCP Servers (GitHub, Time, Fetch, etc.) per session wastes RAM (~600MB) and startup time.
- ToolRecall acts as a persistent host for your MCP servers.
- **Lazy Loading:** Servers boot in 0.01s only when a tool is requested.
- **Idle Timeout:** Servers are killed after 15 minutes of inactivity to recover RAM (dropping daemon footprint from 490MB to 11MB).
- Agents only connect to **ONE** server: `toolrecall mcp`.

---

## Installation & Quickstart

**Requirements:** Python 3.10+, standard SQLite.

```bash
# 1. Install via pip
pip install toolrecall[mcp]

# 2. Initialize default config and .env (Creates ~/.toolrecall/)
toolrecall init

# 3. Start the Daemon in the background
toolrecall daemon &
```

### Usage: Claude Code (Drop-in Replacement)
To instantly make Claude Code 80% cheaper and faster, route it through ToolRecall:
```bash
claude mcp add toolrecall -- uv run python -m toolrecall.mcp_server
```

### Configuration & Secrets
Settings are managed in `~/.toolrecall/config.toml`.
Secrets (like `GITHUB_PERSONAL_ACCESS_TOKEN`) should be placed in `~/.toolrecall/.env`. The daemon securely loads them before launching subprocesses. No secrets are ever stored in Git or passed via the LLM context.

```toml
[mcp]
allowed_paths = ["~/projects", "~/.hermes/skills"]
allow_terminal = false # Security: Prevents Prompt Injection executions
default_ttl = 60 # Default TTL for MCP requests in seconds

[mcp_multiplex]
enabled = true
idle_minutes = 15

[mcp_multiplex.servers_config]
github = { command = "npx", args = ["-y", "@modelcontextprotocol/server-github"], ttl = 60 }
# Set ttl = 0 to entirely bypass caching for dynamic endpoints (like CI logs or real-time data)
```

## Cross-Platform Support
ToolRecall uses **Unix Domain Sockets (IPC)** for daemon communication, making it highly secure.
- **Linux:** Uses `XDG_RUNTIME_DIR` (e.g., `/run/user/1000/toolrecall.sock`).
- **macOS / Windows 10+:** Falls back automatically to `~/.toolrecall/toolrecall.sock`. Windows Named Pipes/AF_UNIX are supported.

## Status
**Experimental.** ToolRecall is currently used in heavy autonomous agent workflows. Before deploying it in production CI/CD environments, ensure your allowlist is strictly scoped.

---

# Appendix: Scientific Whitepaper

*The following section details the theoretical foundation, architecture, and empirical findings of ToolRecall, formatted for academic review in the context of Large Language Model (LLM) agent infrastructure.*

## Mitigating $O(N^2)$ Context Scaling in Autonomous LLM Agents via Operating System-Level Middleware Caching

### Abstract
Autonomous Large Language Model (LLM) agents interact with their environments by executing tools (e.g., reading files, running shell commands, querying APIs). In persistent sessions, the stateless nature of LLM context windows forces the redundant transmission of historical tool outputs at each conversational turn. Due to the quadratic computational complexity ($O(N^2)$) of the Transformer attention mechanism, this context accumulation exponentially increases Time-To-First-Token (TTFT) latency and API inference costs. We present **ToolRecall**, a deterministic, OS-level caching middleware utilizing Unix Domain Sockets (IPC) and SQLite. By serving redundant tool calls from a local exact-match cache (1.5ms latency), we enable aggressive context pruning. Empirical benchmarks from a 13-hour agentic coding session demonstrate a 91% cache hit rate, saving 141.1 million input tokens and eliminating 85 minutes of compounded network and execution latency. Furthermore, the architecture passively yields high-fidelity trajectory datasets suitable for Direct Preference Optimization (DPO) and Supervised Fine-Tuning (SFT).

### 1. Introduction
The current paradigm of agentic AI relies on continuous iterative loops (Observe → Reason → Act). When an agent observes its environment via a tool (e.g., `cat main.py`), the output is appended to the context window. As the session progresses, the context grows linearly, but the computational cost of the attention mechanism scales quadratically. This leads to the "Context Snowball" effect: a single 10,000-token log file read at turn $T_1$ will be redundantly re-transmitted to the API for all subsequent turns $T_2 \dots T_N$, incurring massive financial cost and increasing TTFT latency by tens of seconds.

Current solutions focus on server-side *Prompt Caching* (which reduces compute costs but still requires the agent to locally execute the tool and transmit the data) or *Vector-Database RAG* (which relies on lossy, non-deterministic semantic embeddings that are prone to hallucination). We propose an alternative: an OS-level middleware that intercepts tool executions and returns byte-exact responses.

### 2. Architecture
ToolRecall operates as an independent Daemon process bridging the LLM agent and the host operating system (including MCP—Model Context Protocol servers).

**2.1. IPC Middleware via Unix Domain Sockets:**
To ensure security and bypass network overhead, communication occurs exclusively over Unix Domain Sockets (`AF_UNIX`). This prevents Server-Side Request Forgery (SSRF) and restricts access strictly to the host user.

**2.2. Deterministic State Caching:**
The system employs a two-tier architecture:
1.  *In-Memory LRU Cache:* For highly frequent file reads, achieving $< 0.002$ ms lookups.
2.  *Persistent SQLite FTS5 Database:* Acts as a "Micro-RAG" store for terminal commands and API responses. Unlike semantic RAG, it enforces exact-hash matching. If the state of the environment diverges (detected via `mtime` or write-locks), the cache is strictly invalidated to prevent stale-data hallucinations.

**2.3. Sandboxing & Threat Mitigation:**
By migrating execution logic from the agent's prompts to a deterministic Python middleware, ToolRecall acts as a Web Application Firewall (WAF) for the LLM. It enforces hard path allowlists and command gating, neutralizing Prompt Injections designed to exfiltrate unauthorized data (e.g., `~/.ssh/`).

### 3. Empirical Results
A benchmark was conducted using the DeepSeek-v4-Flash model operating autonomously via the Hermes agent framework during a 13-hour software engineering task.

**Table 1: Cache Efficiency and Token Mitigation**
| Cache Layer | Hits | Misses | Hit Rate | Tokens Mitigated |
| :--- | :--- | :--- | :--- | :--- |
| File I/O | 666 | 62 | 91% | 141,105,842 |
| Terminal / OS | 143 | 15 | 91% | 1,220 |
| MCP Network | 10 | 18 | 37% | 254 |
| **Total** | **827** | **104** | **89%** | **141,112,165** |

**Latency Analysis:**
Redundant executions bypassing the OS fork (`subprocess`) and Node.js MCP server boot overhead reduced local tool execution time from $\sim 1.5$ seconds to $0.0015$ seconds ($1000\times$ speedup). Combined with the reduction in API context bloat, total waiting time mitigated was calculated at $\sim 85$ minutes.

### 4. Discussion: Beyond Latency
**4.1. The Necessity of Determinism in Agents:**
Agentic workflows frequently fail due to environmental stochasticity (e.g., fluctuating network latency, changing API timestamps). By serving frozen, exact-byte cache hits for identical requests within a session, ToolRecall enforces determinism. This converts unstable LLM routines into predictable, testable pipelines analogous to mock-testing in traditional software engineering.

**4.2. Passive Generation of Trajectory Data:**
A significant bottleneck in training open-weight models for agentic tasks is the scarcity of high-quality trajectory data. As a byproduct of OS-level interception, ToolRecall's SQLite database naturally records pristine pairs of (Action $\rightarrow$ State Observation), including failed executions and subsequent corrections. This yields a zero-cost pipeline for generating JSONL datasets suitable for RLHF (Reinforcement Learning from Human Feedback) and DPO.

### 5. Conclusion
Addressing context inflation at the model architecture level ignores the redundant I/O operations occurring on the host machine. Operating System-level middleware proxy caching represents a critical infrastructure layer necessary for the economical and deterministic scaling of autonomous AI agents.