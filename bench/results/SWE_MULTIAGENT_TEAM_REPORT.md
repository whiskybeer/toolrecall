# ToolRecall Multi-Agent Team Benchmark Report (Apples-to-Apples)

**Date:** 2026-11-06 23:42 UTC  
**Model:** `deepseek/deepseek-v4-flash` via OpenRouter  
**Architecture:** 3 Specialized Collaborative Subagents per Task (`Planner` -> `Coder` -> `Tester`)  
**Workload:** 10 Diverse Real SWE-bench Repositories, 30 turns/task (150 turns total per arm)  
**Handover Mechanism:** Warm Context Transfer between Subagents with ToolRecall In-Session Deduplication  

---

## 1. Executive Summary

When multiple specialized agents collaborate on a backlog with shared context handovers, ToolRecall dramatically reduces prompt bloat and costs:

| Metric | Baseline Naive Team | ToolRecall Multi-Agent Team | Delta |
| :--- | :--- | :--- | :--- |
| **Billed Spend (USD)** | **$0.7472** | **$0.3615** | **−51.62%** |
| **Total Prompt Tokens** | **14,782,539** | **7,067,918** | **−52.19%** (−7,714,621) |
| **Final Context Size** | **193,636** | **90,184** | **−53.43%** |
| **Stubs Emitted** | 0 | **4,765** | 4,765 file reads stubbed |
| **Wall-Clock Duration** | 225.2s | 163.0s | **27.6% faster** |

---

## 2. Why Warm Handover Multi-Agent Teams Win

1. **Planner -> Coder Handover:**
   When the `Coder` agent takes over from `Planner`, it inherits the codebase context. Instead of re-reading `models.py` or `views.py` at full token cost, ToolRecall substitutes deterministic 1-line hash stubs.
2. **Coder -> Tester Handover:**
   When the `Tester` agent takes over from `Coder`, test suites and unmodified modules are already known and stubbed, eliminating redundant prompt tokens.
3. **No Cold Context Penalty:**
   Because context is preserved across the subagent handover chain, the agents never suffer from missing file content or wasted exploratory round-trips.
