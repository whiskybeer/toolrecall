# Applications — Where ToolRecall's Architecture Fits

ToolRecall is a hybrid in-memory + SQLite cache layer with mtime-based invalidation, FTS5 full-text search, MCP multiplexing, and a zero-trust WAF. These are the domains where this combination solves real problems.

---

## CI/CD Pipelines

Build steps are largely idempotent: lint, test, type-check, format. Same inputs → same outputs. Yet most CI systems re-run every step on every commit.

**What ToolRecall brings:** mtime-based caching means a step that reads unchanged files returns cached results in <1ms instead of seconds. The WAF is irrelevant here (no untrusted LLM), but the hybrid cache + byte-exact replay maps directly to cache keys = (command hash + input file mtimes).

**Effect:** 10–50× faster cache-hit steps. No Redis, no memcached — just SQLite.

---

## LLM Serving / Inference Platforms (vLLM, TGI, llama.cpp)

These systems load the same model configs, tokenizer files, template files, and system prompts on every startup and every request. Repetitive file I/O from disk.

**What ToolRecall brings:** in-memory LRU cache for hot files (~0.001ms lookup), SQLite persistence for warm files (~7ms). Once a config is loaded, it stays in memory until it changes.

**Effect:** Eliminates redundant disk reads for configuration files. Tokenizer merges and vocab files that don't change between model versions stay cached across restarts.

---

## ETL / Data Pipelines

Transformations whose inputs rarely change (dimensional models, lookup tables, reference data) are re-computed on every pipeline run.

**What ToolRecall brings:** cached_read() with mtime auto-invalidation — if the source file hasn't changed, return the cached result. No cache invalidation logic to maintain.

**Effect:** Pipeline stages that process static reference data run in <1ms instead of seconds.

---

## Static Site Generators / Documentation Builds

Building 1000 Markdown pages every time, even when only 3 changed. Same problem, same pattern.

**What ToolRecall brings:** per-file mtime check → only parse files that actually changed. The FTS5 knowledge base also helps: full-text search across your docs without running a separate search service.

**Effect:** Incremental builds without implementing a custom build cache.

---

## Microservice API Response Caching

Services with expensive database queries that return data changing infrequently (user profiles, product catalogs, reference data).

**What ToolRecall brings:** TTL-based caching with SQLite persistence. Same pattern as in-memory caches (Redis, memcached) but zero infrastructure — the cache is a local SQLite file.

**Trade-off:** Single-node, not distributed. Fine for per-service caching or single-instance deployments.

---

## IDE / Editor Plugin Caches

LSP servers, syntax highlighters, and completion engines read the same files repeatedly. Every keystroke triggers file reads that produce the same parse trees.

**What ToolRecall brings:** in-memory LRU with mtime invalidation means the parse tree lives in memory until the file actually changes. The zero-dependency requirement matters here — plugins avoid vendoring heavy cache libraries.

**Effect:** Sub-millisecond cache hits for unchanged files during active editing.

---

## Why These Domains Share the Same Pattern

All of them have:
1. **Repeated reads of unchanged data** — files, configs, query results.
2. **mtime as the right invalidation signal** — the file system already tracks change.
3. **No need for distributed consensus** — single-node caching is sufficient.
4. **Zero tolerance for cache bugs** — stale data is worse than no cache.

ToolRecall doesn't invent a new caching strategy. It packages the correct one (mtime + hybrid memory/SQLite + security gate) into a single zero-dependency library that happens to also do MCP multiplexing and FTS5 search — but those are optional. The cache core is the part that generalizes.
