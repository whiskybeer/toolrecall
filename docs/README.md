# ToolRecall Documentation Directory

Welcome to the ToolRecall technical library. This directory contains the architectural, financial, and strategic documentation required to understand, scale, and pitch the ToolRecall in-memory cache infrastructure.

## 1. Hard Data & Benchmarks
- **[`BENCHMARK.md`](BENCHMARK.md)**: The raw $O(N^2)$ mitigation data. Hit rates, latency, and timing from a real 13-hour session. *(Token count corrected for double-counting bug, see file for details.)*
- **[`LATENCY_PITCH.md`](LATENCY_PITCH.md)**: Explains the math behind dropping tool execution latency from ~1.5s (end-to-end) down to ~0.6ms (daemon UDS hit) on cached calls, eliminating ~85 minutes of wait time per developer/day.

## 2. Business & Enterprise Value
- **[`ROI_AND_SAVINGS.md`](ROI_AND_SAVINGS.md)**: CFO-friendly financial projections. Breaks down exact dollar savings across API tokens (forcing the 90% cloud discount), engineering salaries, and AWS RAM reductions.
- **[`PITCH_SUMMARY.md`](PITCH_SUMMARY.md)**: A one-page executive summary of the framework's value proposition.

## 3. Engineering & Architecture
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)**: Deep dive into the Daemon (IPC), SQLite FTS5 Micro-RAG, and the exact Unix Domain Socket routing mechanisms.
- **[`OSI_LAYERS.md`](OSI_LAYERS.md)**: Where ToolRecall sits in the agent tool execution stack — and why layers 1-3 are entirely bypassed on cache hits.
- **[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)**: A developer FAQ for fixing common issues (e.g., Access Denied errors, Node.js zombie processes).

## 4. Vision & Roadmap
- **[`VISION.md`](VISION.md)**: Use cases beyond agent caching, emergent architectural wins (air-gapped agents, attention profiling, zero-cost SFT datasets), roadmap to v0.6.0, and long-term vision (multi-tenant gateway, synthetic data flywheel, A2A swarm multiplier).

## 5. Operations & Security
- **[`SECURITY_AUDIT.md`](SECURITY_AUDIT.md)**: Detailed security audit covering WAF, path canonicalization, sensitive file blocklist, and OWASP coverage.

## 6. Integrations
- **[`BROWSER_CACHE.md`](BROWSER_CACHE.md)**: How the ToolRecall Browser Cache Extension integrates with LLM agents that use browser tools (browser_navigate, browser_snapshot). Covers data flow, change detection, proxy port discovery, and troubleshooting.
- **[`BROWSER_CACHE_SECURITY.md`](BROWSER_CACHE_SECURITY.md)**: Security analysis of the browser extension — threat model, permissions, network isolation, content size limits, OWASP LLM Top 10 audit, and unmitigated risks.