# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Stub an unchanged-file re-read in an agent process — Lever 1 (shim surface).
The bridge stub (``mcp_bridge`` via ``stub_core``) only covers MCP-tool reads.
The HIGHEST-frequency re-read path on agent boxes is the shim (Hermes native
``read_file`` → ``open('rb')``). This module binds the SHARED stub state
machine (``stub_core.FileStubSession`` — one contract, one test surface) to
the shim process: second+ native read of an UNCHANGED file within TTL →
caller gets a one-line stub instead of full content, counted via the daemon's
``dedup_tokens`` counter (mcp_dedup category — same measured tier as the
bridge stub).

TTL resolution (env > config > default, same order as the rest of the config
contract): ``TOOLRECALL_SHIM_STUB_TTL`` env var → ``[shim].stub_ttl`` in
toolrecall.toml (loaded lazily on first stub decision — the shim imports at
every Python process start via ``tr_shim.pth`` and must not pay a config
parse for processes that never read an agent file) → 300s default. The TTL
guards the AGENT-memory axis (harness compaction), not disk staleness — the
cache's mtime+size gate already proves disk content current. See
``stub_core`` module docstring for the full rationale.

Safety contract (identical to the bridge — enforced in stub_core):
  - only in agent processes (``is_agent`` gate, same AND-gate as tagging)
  - only read-only modes, only cache HITs, only byte-lossless content
  - TTL default 300s: after that, re-send full content — a harness may have
    compacted the earlier copy; blind stubs strand the agent
  - max 2 consecutive stubs per path, then 1 full re-send (compaction armor)
  - changed content (digest) → full send, state re-arms
  - opt-out: ``TOOLRECALL_SHIM_STUB=0`` env kills it (default ON for agents)
  - write/invalidation reset: ``cache.invalidate_file`` calls ``reset_path``
    IN THE CALLING PROCESS (daemon-side resets cannot reach this process —
    process-local state by design). A native write is covered regardless:
    changed mtime → cache miss → fresh read → digest compare re-arms.
  - thread-safe (lock), process-local state (per-process like the bridge)
  - counting is fire-and-forget via daemon cmd ``dedup_tokens`` (never breaks
    a read; daemon down => stub still works, count lost, logged at debug)

NOT stubbed: writes (w/a/x/+), non-agent processes, skip-prefix paths,
infra reads (excluded by the same gates as serving).
"""

from __future__ import annotations

import logging
import os
import threading

from toolrecall.stub_core import FileStubSession, note_agent

log = logging.getLogger("toolrecall.shim_stub")

# ─── Config ──────────────────────────────────────────────────────────────────

_DISABLE_VALUES = ("0", "false", "no", "off")
# Kept for introspection/back-compat; the authoritative disable check runs at
# call time in maybe_stub (a kill switch set after process start must work).
_ENABLED = os.environ.get("TOOLRECALL_SHIM_STUB", "1") not in _DISABLE_VALUES

_DEFAULT_TTL_S = 300.0
_MAX_CONSECUTIVE = 2  # 3rd read in a row gets full content (compaction armor)
_MIN_CHARS = 800  # same floor as request-dedup min_chars

_lock = threading.Lock()
_instance: FileStubSession | None = None


def _counter(tokens: int, path: str, note: str = "") -> None:
    """Measured-tier counter: fire-and-forget daemon cmd (stub_core calls this)."""
    from toolrecall.transport import DEFAULT_PATH as _dp
    from toolrecall.transport import TransportClient as _tc

    _tc(_dp).send(
        {"cmd": "dedup_tokens", "tokens": int(tokens), "path": path},
        timeout=2.0,
    )


def _resolve_ttl() -> float:
    """env > [shim].stub_ttl config > default. Invalid values fall back."""
    raw = (os.environ.get("TOOLRECALL_SHIM_STUB_TTL") or "").strip()
    if raw:
        try:
            v = float(raw)
            if v >= 0:
                return v
        except ValueError:
            log.debug("TOOLRECALL_SHIM_STUB_TTL=%r is not a number", raw)
    try:
        # Lazy import + lazy load: this module is imported by the shim in
        # EVERY Python process (tr_shim.pth); a config parse must only be
        # paid by processes that actually reach a stub decision.
        from toolrecall.config import load_config

        v = float(load_config().shim_stub_ttl)
        if v >= 0:
            return v
    except Exception:  # noqa: BLE001 — config is optional for the shim
        log.debug("stub TTL config resolution failed", exc_info=True)
    return _DEFAULT_TTL_S


# Re-entrancy kill-switch (per-thread, mirroring shim.py's own guard): True
# while this thread is inside a stub decision. A stub decision must NEVER run
# code that calls open() again — if builtins.open is shim-patched, that open
# re-enters maybe_stub. The lock-only fix in instance() is not sufficient
# alone: after instance() is built, the counter_fn sends over the transport
# and future code inside FileStubSession could open files too. This flag
# makes the recursion structurally impossible. Thread-local (not global):
# another thread's in-flight stub window must not suppress independent
# agent reads; a re-entrant open happens on the SAME thread by construction.
_reentrant = threading.local()


def _reentrancy_active() -> bool:
    return getattr(_reentrant, "active", False)


def instance() -> FileStubSession:
    """The process-global shim stub session (lazy: built on first decision).

    Re-entrancy safe: lazy init calls load_config(), which opens the config
    file — and if builtins.open is shim-patched (full suites run shim tests
    in one process), that open() re-enters maybe_stub. The per-thread guard
    short-circuits that recursion to "serve content, no stub state" BEFORE
    the lock is touched, so a non-reentrant self-deadlock is impossible and
    direct instance() callers (reset_path, tests) are covered too. The lock
    guards only the check-and-set; TTL resolution happens outside it.
    """
    global _instance
    if _reentrancy_active():
        # We are inside our own open() during lazy init: never recurse.
        # Build the instance from the DEFAULT TTL; the outermost window
        # REPLACES it with the config-resolved one below (the default
        # accumulates no state meanwhile — re-entrant reads never arm).
        if _instance is None:
            _instance = FileStubSession(
                ttl_s=0.0 if not _ENABLED else _DEFAULT_TTL_S,
                max_consecutive=_MAX_CONSECUTIVE,
                min_chars=_MIN_CHARS,
                note_fn=note_agent,
                counter_fn=_counter,
            )
        return _instance
    # Outermost window: resolve the TTL (may re-enter instance() via the
    # patched open — that builds a default placeholder, see above), then
    # UNCONDITIONALLY install the resolved instance. The inner None-check
    # that used to live here let the placeholder WIN, silently dropping the
    # configured TTL (caught by test_reentrant_config_load_cannot_deadlock:
    # 300.0 != 111.0). Last-writer-wins is safe: racing outers resolve the
    # same config, and the placeholder holds no armed state.
    resolved = 0.0 if not _ENABLED else _resolve_ttl()
    with _lock:
        _instance = FileStubSession(
            # TOOLRECALL_SHIM_STUB=0 → ttl 0 → core no-op (belt);
            # maybe_stub's call-time check is the braces.
            ttl_s=resolved,
            max_consecutive=_MAX_CONSECUTIVE,
            min_chars=_MIN_CHARS,
            note_fn=note_agent,
            counter_fn=_counter,
        )
    return _instance


def maybe_stub(path: str, content: str, is_agent: bool, lossless: bool) -> str | None:
    """Return a stub string if this read should be stubbed, else None.

    Called with the FULL content the shim was about to serve. None means
    "serve the content as usual" (and registers it as a full send).
    Kill switch ``TOOLRECALL_SHIM_STUB`` is honored at CALL time (a disable
    set after process start must take effect — the value is read per call).
    """
    if os.environ.get("TOOLRECALL_SHIM_STUB", "1") in _DISABLE_VALUES:
        return None
    if _reentrancy_active():
        # We are inside our own open() (config load, transport send…):
        # serve the content, touch no stub state. The re-entrant read is
        # by definition toolrecall's own infrastructure, never agent traffic.
        return None
    _reentrant.active = True
    try:
        return instance().maybe_stub(path, content, is_agent=is_agent, lossless=lossless)
    finally:
        _reentrant.active = False


def reset_path(path: str) -> None:
    """Clear stub state for a path (called when the file is written/patched)."""
    instance().reset_path(path)


def reset_all() -> None:
    instance().reset_all()
