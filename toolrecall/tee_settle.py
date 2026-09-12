# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tee-settle registry: closes the stream-cache store race in-process.

Race (measured 2026-09-11, hermes profile e2e): request 2 fired immediately
after request 1's SSE stream ended could reach the daemon's
``cached_api_check`` BEFORE request 1's tee store committed. The relay loop
hands the response to the client at upstream's final chunk, but
``_store_stream_tee`` runs afterwards — SSE reconstruction + UDS round-trip
+ SQLite commit — a millisecond-scale window that widens under daemon load.
Result: a guaranteed-cacheable duplicate went LIVE to the provider (double
billing risk on retries).

Fix: the relay thread registers its request_hash BEFORE streaming and
signals completion AFTER the store commit. A racing request 2 that misses
the cache only waits (bounded ~50ms) when its exact hash is mid-store —
no sleep on the normal path, no cross-request coupling beyond the hash
key. Single-process only: the proxy runs inside the daemon process
(ThreadedHTTPServer), so threads see the same registry. If ToolRecall
ever splits the proxy out of process, this must move into the daemon
(next to cached_api_store) or be dropped with the race re-documented.

Memory: entries are removed on signal; a crashed relay thread leaks one
small entry per crash — bounded by the sweep (entries older than 10s are
treated as settled by waiters and pruned).
"""

from __future__ import annotations

import threading
import time

# Wait cap for a racing request 2. The store path is SSE parse + one UDS
# round-trip + commit: milliseconds healthy; 2s covers pathological daemon
# load without hanging a client that could just go live instead.
_WAIT_BUDGET_S = 2.0
# A registry entry older than this is stale (relay thread died mid-store);
# waiters stop waiting and prunes remove it.
_STALE_S = 10.0

_lock = threading.Lock()
_pending: dict[str, float] = {}  # request_hash -> registration monotonic time


def register(hash_hex: str) -> None:
    """Mark ``hash_hex`` as tee-store-in-flight (call BEFORE streaming)."""
    with _lock:
        _prune_locked()
        _pending[hash_hex] = time.monotonic()


def settle(hash_hex: str) -> None:
    """Mark the store committed (or abandoned) — wake any waiters."""
    with _lock:
        _pending.pop(hash_hex, None)


def wait_settled(hash_hex: str, budget: float = _WAIT_BUDGET_S) -> bool:
    """Block until ``hash_hex`` is no longer tee-store-in-flight.

    Returns True immediately when the hash is not pending (normal path:
    no sleep). Bounded otherwise — never hangs past ``budget``. Stale
    registrations (relay crashed before settle) age out after _STALE_S.
    """
    deadline = time.monotonic() + budget
    while True:
        with _lock:
            _prune_locked()
            started = _pending.get(hash_hex)
            if started is None:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _prune_locked() -> None:
    """Drop stale entries (relay thread died before settle). Caller holds _lock."""
    now = time.monotonic()
    stale = [h for h, t in _pending.items() if now - t > _STALE_S]
    for h in stale:
        _pending.pop(h, None)