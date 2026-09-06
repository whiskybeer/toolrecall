# Resilience: Coalescing, Retry, Circuit Breaker

ToolRecall's resilience layer (`toolrecall/resilience.py`, pure stdlib:
`threading`, `time`, `random`, `collections`) hardens the hot paths against
thundering herds, flaky upstreams, and outages. **Everything is opt-in** —
with the default config every primitive is inert and behavior is
byte-identical to the pre-resilience code.

## Request Coalescing (`Coalescer`)

**Problem:** N agents (or N threads) requesting the same uncached key at the
same moment execute the same subprocess / MCP fetch / upstream call N times.

**Mechanism:** the first caller for a key becomes the *winner* and executes;
concurrent callers register on a `threading.Event` and wait (bounded by
`coalescing_window`, default 30 s). The winner's return value **or
exception** is shared with all waiters — no cache-row dependency, no
re-execution. A waiter that times out executes on its own (degraded, never
stuck).

```
8 concurrent `hostname` misses, coalescing ON  → 1 subprocess
8 concurrent `hostname` misses, coalescing OFF → 8 subprocesses
```

**Applied to:** terminal cache miss path (`cache.cached_terminal`), one-shot
MCP fetch (`cached_mcp`), proxy forward (wired where the producer is a
callable).

## Retry (`retry_call` / proxy `_forward_with_retry`)

**Problem:** transient upstream failures (connection resets, 429 rate
limits, 502/503/504) fail requests that would succeed 100 ms later.

**Mechanism:** exponential backoff with **full jitter**
(`sleep = base * 2**attempt + uniform(0, base)`) — jitter prevents
synchronized retry storms. `Retry-After` headers are honored (capped at 5 s
so a hostile header can't stall the proxy).

**Hard guards:**

- A response whose **body was already consumed is never retried** — the
  request may have been billed (LLM completions). Only transport-level
  failures and pre-body status codes qualify.
- **Streaming responses are never retried** — a partially relayed SSE stream
  cannot be replayed safely.
- HTTP 404/400/etc. are never retried (`is_retryable_status`: 429/502/503/504).

## Circuit Breaker (`CircuitBreaker`)

**Problem:** when an upstream is down, every request pays the full
timeout before failing — and hammers the dead service.

**State machine (per process, all allowlisted hosts):**

```
CLOSED ── threshold failures in window ──→ OPEN
OPEN ── open_seconds elapsed ──→ HALF_OPEN ── probe success ──→ CLOSED
                                  └── probe failure ──→ OPEN
```

- **CLOSED:** all requests pass; each 429/5xx records a failure in a
  sliding `cb_window` (default 60 s). At `cb_failure_threshold` (default 5)
  the breaker trips.
- **OPEN:** requests fast-fail **503** with `Retry-After` and
  `{"error":{"code":"circuit_open","host":...,"retry_after":N}}` — no
  upstream contact, no timeout wait.
- **HALF_OPEN:** exactly one probe request is admitted; success closes the
  breaker, failure reopens it.

**Accounting detail:** 429/5xx responses are *outcomes*, not exceptions —
the proxy raises an internal `_UpstreamFailure` inside `breaker.call()` so
the failure routes through the breaker's exception path (the success path
would clear the failure history). `record_failure()`/`record_success()` are
public for other outcome-based callers.

## Config

See [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) — the `[resilience]` section
and the `TOOLRECALL_*` env overrides. Quick enable:

```toml
[resilience]
coalescing = true
retry = true
circuit_breaker = true
cb_failure_threshold = 5
cb_open_seconds = 30
```

## Zero dependencies

All primitives are stdlib-only: `threading` (coalescer, breaker lock),
`time`/`random` (backoff + jitter), `collections.deque` (sliding window).
`pyproject.toml` `dependencies = []` remains untouched — this layer ships in
the core package.

## Tests

`tests/test_resilience.py` (19 unit tests: single-flight, shared outcome,
windowed fallthrough, backoff shaping, non-retryable pass-through, full
state machine with injected clocks) and `tests/test_proxy_resilience.py`
(3 e2e tests against a mock upstream: retry recovery, retry-off
passthrough, breaker trip + fast-fail).
