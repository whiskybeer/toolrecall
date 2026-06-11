# ToolRecall Documentation Directory

Welcome to the ToolRecall technical library. This directory contains the architectural, financial, and strategic documentation required to understand, scale, and pitch the ToolRecall in-memory cache infrastructure.

## 1. Hard Data & Benchmarks
- **[`BENCHMARK.md`](BENCHMARK.md)**: The raw $O(N^2)$ mitigation data. Hit rates, latency, and timing from a real 13-hour session. *(Token count corrected for double-counting bug, see file for details.)*
- **[`LATENCY_PITCH.md`](LATENCY_PITCH.md)**: Explains the math behind dropping tool execution latency from ~1.5s (end-to-end) down to ~0.6ms (daemon UDS hit) on cached calls, eliminating ~85 minutes of wait time per developer/day.

## 2. Business & Enterprise Value
- **[`ROI_AND_SAVINGS.md`](ROI_AND_SAVINGS.md)**: CFO-friendly financial projections. Breaks down exact dollar savings across API tokens (forcing the 90% cloud discount), engineering salaries, and AWS RAM reductions.
- **[`DATA_CENTER_SCALE.md`](DATA_CENTER_SCALE.md)**: The macro-economic view. Explains why forcing deterministic OS payloads locally saves hyperscalers (OpenAI/Anthropic) massive amounts of GPU VRAM and physical megawatt grid power.
- **[`ENTERPRISE_SCALE.md`](ENTERPRISE_SCALE.md)**: The "Iron Triangle" pitch, the "gzip for AI context" metaphor (Jevons Paradox), and the Zero-Trust WAF security model that cages prompt-injected agents.
- **[`PITCH_SUMMARY.md`](PITCH_SUMMARY.md)**: A one-page executive summary of the framework's value proposition.

## 3. Engineering & Architecture
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)**: Deep dive into the Daemon (IPC), SQLite FTS5 Micro-RAG, and the exact Unix Domain Socket routing mechanisms.
- **[`AUDIT_AND_ROADMAP.md`](AUDIT_AND_ROADMAP.md)**: A brutally honest systems-engineering audit. Details current systemic limits (Phantom Bugs, Real-Time Blindness) and the technical roadmap for v0.6.0.
- **[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)**: A developer FAQ for fixing common issues (e.g., Access Denied errors, Node.js zombie processes).

## 4. Emergent Capabilities (The Future)
- **[`BYPRODUCTS.md`](BYPRODUCTS.md)**: Documents accidental architectural wins, such as Zero-Penalty Context Switching, Agent Attention Profiling, and Air-Gapped execution.
- **[`STEALTH_VISION.md`](STEALTH_VISION.md)**: The end-game vision. Discusses the transition to a "Swarm OS" (shared cache across multiple agents) and passive generation of high-fidelity RLHF/DPO trajectories to train local L0 models.

## 5. Operations & Security
- **[`SERVER_SECURITY_NONROOT_NGINX.md`](server-security-nonroot-nginx.md)**: How ToolRecall's production web server runs entirely with user privileges — no root required. Covers authbind architecture, nginx as a user service, SSL certificate renewal pipeline, and the ki-game-api shutdown that eliminated its last remaining service dependency.

## 6. Integrations
- **[`BROWSER_CACHE.md`](BROWSER_CACHE.md)**: How the ToolRecall Browser Cache Extension integrates with LLM agents that use browser tools (browser_navigate, browser_snapshot). Covers data flow, change detection, proxy port discovery, and troubleshooting.
- **[`BROWSER_CACHE_SECURITY.md`](BROWSER_CACHE_SECURITY.md)**: Security analysis of the browser extension — threat model, permissions, network isolation, content size limits, OWASP LLM Top 10 audit, and unmitigated risks.