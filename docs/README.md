# ToolRecall Documentation Directory

Welcome to the ToolRecall technical library. This directory contains the architectural, financial, and strategic documentation required to understand, scale, and pitch the ToolRecall L1-Cache infrastructure.

## 1. Hard Data & Benchmarks
- **[`BENCHMARK.md`](BENCHMARK.md)**: The raw $O(N^2)$ mitigation data. Proves how 141.1 million input tokens were intercepted locally in a single 13-hour session.
- **[`LATENCY_PITCH.md`](LATENCY_PITCH.md)**: Explains the math behind dropping tool execution latency from ~1.5s down to <0.1ms, eliminating ~85 minutes of wait time per developer/day.

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