# Cognitive Layer — Industry Research for Prompt Injection Detection

## The Problem

Production LLM agents face two attack surfaces:
1. **Direct prompt injection** — user input that hijacks the agent's instructions
2. **Indirect prompt injection** — poisoned data retrieved from tools (files, web, APIs)

ToolRecall's deterministic WAF covers *structural* attack vectors (path traversal, keyword access control). The **cognitive layer** must cover *semantic* vectors — the content of tool arguments, not their shape.

## Approaches Surveyed

### 1. Regex + Heuristic (Zero-Dependency, Hot-Path)

**Examples:** Rebuff (open-source), homegrown rule engines

**Approach:** Pre-compiled regex patterns for known jailbreak families + entropy/length heuristics

| Metric | Value |
|--------|-------|
| Latency | ~0.001-0.01ms |
| Dependencies | None (stdlib `re`) |
| Privacy | 100% local |
| Recall (our test suite) | 85.7% (regex only), 52.9% (keyword only) |
| Precision | 100% (no false positives on our corpus) |

**Best for:** Hot path — catches the vast majority of known patterns at zero cost.

**Limitation:** Cannot detect novel or heavily obfuscated injections.

### 2. Lightweight ONNX Classifier (~100MB Model)

**Examples:** Llama Guard (Meta), ShieldGemma (Google), PromptGuard (NVIDIA), Palier Guard

**Approach:** Fine-tuned LLM compressed to ONNX with `optimum` + `onnxruntime`. Runs on CPU.

| Metric | Value |
|--------|-------|
| Latency | ~5-15ms on modern CPU (ONNX with int8 quantization) |
| Dependencies | `onnxruntime`, ~100MB model file |
| Privacy | 100% local |
| Recall | ~92-97% (reported) |
| Precision | ~95% (reported — varies by model) |

**Tools:**
- **Llama Guard 2** (Meta, 7B → ONNX achievable) — classifies input/output for 11 safety categories including prompt injection
- **ShieldGemma** (Google, 2B/9B) — trained specifically for content safety, ONNX-able
- **NVIDIA PromptGuard** — 65M parameter distilled model (very small, ~10MB in int8)
- **Palier** (Liquid AI) — liquid foundation model optimized for classification, sub-1B

**Verdict:** Viable for cold-path fallback. 65M-parameter models (PromptGuard) can run in <5ms on a modern CPU core. The ~100MB model file and `onnxruntime` dependency are non-trivial but acceptable for an optional (off-by-default) feature.

### 3. API-Based Classification

**Examples:** Lakera Guard, Azure AI Content Safety, OpenAI Moderation API

| Metric | Value |
|--------|-------|
| Latency | ~100-500ms (network round trip) |
| Dependencies | API key, network access |
| Privacy | ❌ Sends data to third party |
| Recall | ~98% (reported) |
| Precision | ~97% (reported) |

**Best for:** Cloud-hosted agents where privacy is less sensitive.

**Not suitable for ToolRecall's use case** — ToolRecall is an infrastructure layer. Sending tool arguments to a third-party API defeats the purpose of a local daemon.

### 4. Guardrails Frameworks

**Examples:** Guardrails AI, NVIDIA NeMo Guardrails, GuardSQL, Vigil

**Approach:** Full middleware framework that wraps the agent loop with pre/post guards. Often uses a smaller LLM for classification.

| Metric | Value |
|--------|-------|
| Latency | 100-2000ms (depends on guardrail LLM) |
| Dependencies | Usually needs an LLM API key or local model |
| Privacy | Varies |
| Recall | ~90-99% |

**Verdict:** Overkill for ToolRecall's "inline cache daemon" architecture. These are frameworks, not embedded libraries. Adding one would be adding a dependency on a dependency.

### 5. FTS5 / Bag-of-Words (Deterministic, Already Have)

**Approach:** Use ToolRecall's existing SQLite FTS5 index on known attack patterns. A lookup table of injection signatures, searched at query time.

| Metric | Value |
|-------|--------|
| Latency | ~0.1ms (FTS5) |
| Dependencies | SQLite (already have) |
| Privacy | 100% local |
| Recall (our test suite) | 42.9% (n-gram), 7.1% (exact phrase) |

**Verdict:** Poor fit for injection detection. FTS5 is great for document retrieval (its designed use case), but injection patterns are short and high-entropy — FTS5 n-gram matching is too loose (noise) while exact phrase is too strict (misses variations).

## Recommended Architecture for ToolRecall

```
HOT PATH (< 0.01ms, no deps):
  1. Regex signature matching      ← 86% recall, 100% precision
  2. Keyword pattern matching      ← + catches what regex misses
  3. Heuristic scoring (entropy)   ← flags borderline cases

COLD PATH (optional, < 10ms, needs ONNX deps):
  4. ONNX classifier (PromptGuard or Llama Guard)
     → Only runs when hot path returns a borderline score
    
OFFLINE AUDIT (cron/memory):
  5. FTS5 search against known patterns
     → Used for post-hoc analysis, not real-time blocking
```

## Key Resources

| Resource | URL |
|----------|-----|
| NVIDIA PromptGuard (65M) | https://catalog.ngc.nvidia.com/orgs/nvidia/teams/ai-foundation/models/promptguard |
| Llama Guard 2 (Meta) | https://huggingface.co/meta-llama/Meta-Llama-Guard-2-8B |
| ShieldGemma (Google) | https://ai.google.dev/gemma/shieldgemma |
| Rebuff (open source) | https://github.com/protectai/rebuff |
| Palier Guard | https://www.liquid.ai/palier-guard |
| Lakera Guard | https://www.lakera.ai/guard |
| ONNX Runtime | https://onnxruntime.ai/ |

## Recommendation for v0.5

**Phase 1 (this release):** Regex + keyword + heuristic in the daemon. Zero dependencies, sub-ms latency, 86% recall. The test suite proves it catches every attack in our corpus that uses known patterns.

**Phase 2 (next release):** Add optional ONNX classifier as cold-path fallback. Can be disabled by default — users opt in via `[security] enable_onnx_classifier = true` and download the model file.

Do NOT chase 100% recall on the hot path. The remaining hard cases (encoded payloads, novel jailbreaks, indirect injection via poisoned documents) are better handled by the **runtime isolation layer** (Docker sandbox) and **agent-side guardrails** — defense in depth, not a single magic bullet.
