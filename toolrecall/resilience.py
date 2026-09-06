"""Resilience primitives: request coalescing, retry with jitter, circuit breaker.

Pure stdlib (threading, time, random, collections) — zero runtime deps, per
ToolRecall's hard identity. All pieces are opt-in at the call-site/config
level; defaults preserve existing behavior.

Coalescer   — N concurrent callers on the same key share ONE execution.
Retry       — exponential backoff + full jitter for transient failures.
CircuitBreaker — per-host state machine (CLOSED → OPEN → HALF_OPEN) that
              fast-fails when an upstream is down instead of hanging.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque

__all__ = ["Coalescer", "retry_call", "is_retryable_status", "CircuitBreaker", "CircuitBreakerOpen"]


class CircuitBreakerOpen(Exception):
    """Raised when a call is fast-failed because the breaker is OPEN (or a
    probe is already in flight in HALF_OPEN)."""

    def __init__(self, host: str = "", retry_after: float = 0.0):
        self.host = host
        self.retry_after = retry_after
        msg = f"circuit_open: {host}" if host else "circuit_open"
        if retry_after:
            msg += f" (retry after {retry_after:g}s)"
        super().__init__(msg)


class Coalescer:
    """In-flight request dedup: same key → one execution, shared result.

    The first caller for a key executes the producer; concurrent callers
    wait (bounded by ``window`` seconds) and receive the winner's return
    value or the winner's exception. After completion the key is reusable
    immediately.
    """

    def __init__(self, window: float = 30.0):
        self._window = window
        self._lock = threading.Lock()
        # key → _Flight (event + shared outcome)
        self._inflight: dict[str, "_Flight"] = {}

    def run(self, key: str, producer):
        """Execute ``producer`` coalesced by ``key``.

        Waiters whose winner does not finish within ``window`` seconds fall
        through to executing their own producer (no result sharing, bounded
        latency coupling).
        """
        with self._lock:
            flight = self._inflight.get(key)
            if flight is None:
                flight = _Flight()
                self._inflight[key] = flight
                winner = True
            else:
                winner = False

        if winner:
            try:
                result = producer()
                flight.result = result
                return result
            except Exception as e:
                flight.error = e
                raise
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                flight.done.set()

        # Waiter path: share the winner's outcome
        if flight.done.wait(timeout=self._window):
            if flight.error is not None:
                raise flight.error
            return flight.result
        # Timed out — fly solo
        return producer()


class _Flight:
    __slots__ = ("done", "result", "error")

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error: Exception | None = None


def is_retryable_status(status: int) -> bool:
    """HTTP statuses worth retrying: rate-limit + bad-gateway class.

    200/4xx (except 429) are NOT retried — the request itself is wrong or
    the caller would be double-billed for a completed LLM call.
    """
    return status in (429, 502, 503, 504)


def retry_call(
    fn,
    *,
    max_attempts: int = 3,
    retryable: tuple[type[Exception], ...] = (ConnectionError, TimeoutError, OSError),
    backoff_base: float = 0.25,
    sleep_fn=time.sleep,
):
    """Call ``fn`` up to ``max_attempts`` times with exponential backoff + jitter.

    Sleep before attempt N (N>=2): ``backoff_base * 2**(N-2) + uniform(0, backoff_base)``
    (full jitter). Non-retryable exceptions propagate immediately. On
    exhaustion the last exception is re-raised.

    ``sleep_fn`` is injectable for tests (and for callers that want global
    sleep suppression).
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except retryable as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            delay = backoff_base * (2**attempt) + random.uniform(0, backoff_base)
            sleep_fn(delay)
    # Unreachable (raise above), but keeps type-checkers happy:
    raise last_exc  # type: ignore[misc]


class CircuitBreaker:
    """Per-host circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED.

    - CLOSED: calls pass through; failures within ``window`` seconds are
      counted; at ``failure_threshold`` the breaker OPENs.
    - OPEN: every call fast-fails with CircuitBreakerOpen (no producer call)
      until ``open_seconds`` elapse.
    - HALF_OPEN: exactly one probe call is admitted; success → CLOSED,
      failure → OPEN again. Additional callers during the probe fast-fail.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        window: float = 60.0,
        open_seconds: float = 30.0,
        clock=time.time,
    ):
        self._threshold = failure_threshold
        self._window = window
        self._open_seconds = open_seconds
        self._clock = clock

        self._state = "closed"
        self._failures: deque[float] = deque()
        self._opened_at = 0.0
        self._probe_inflight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    def _current_state(self) -> str:
        """Caller must hold _lock."""
        if self._state == "open" and (self._clock() - self._opened_at) >= self._open_seconds:
            self._state = "half_open"
        return self._state

    def call(self, fn):
        """Execute ``fn`` under breaker semantics."""
        with self._lock:
            state = self._current_state()
            if state == "open":
                raise CircuitBreakerOpen(retry_after=self._open_seconds)
            if state == "half_open":
                if self._probe_inflight:
                    raise CircuitBreakerOpen(retry_after=self._open_seconds)
                self._probe_inflight = True
                probe = True
            else:
                probe = False
        try:
            result = fn()
        except Exception:
            with self._lock:
                if probe:
                    # Failed probe reopens immediately
                    self._trip()
                    self._probe_inflight = False
                else:
                    self._record_failure_locked()
            raise
        else:
            with self._lock:
                if probe:
                    self._probe_inflight = False
                # Success closes (and clears the failure history)
                self._state = "closed"
                self._failures.clear()
            return result

    def _record_failure_locked(self) -> None:
        """Caller must hold _lock."""
        now = self._clock()
        self._failures.append(now)
        while self._failures and (now - self._failures[0]) > self._window:
            self._failures.popleft()
        if len(self._failures) >= self._threshold:
            self._trip()

    def record_failure(self) -> None:
        """Report a failure WITHOUT raising — for outcome-based callers.

        The proxy counts HTTP 429/5xx responses as failures even though
        they return normally (a tuple, not an exception). Half-open probes
        that report failure reopen the breaker immediately.
        """
        with self._lock:
            state = self._current_state()
            if state == "half_open" and self._probe_inflight:
                self._probe_inflight = False
                self._trip()
                return
            if state == "half_open":
                self._trip()
                return
            self._record_failure_locked()

    def record_success(self) -> None:
        """Report a success for outcome-based callers (resets the breaker)."""
        with self._lock:
            self._probe_inflight = False
            self._state = "closed"
            self._failures.clear()

    def _trip(self) -> None:
        """Caller must hold _lock."""
        self._state = "open"
        self._opened_at = self._clock()
        self._failures.clear()
