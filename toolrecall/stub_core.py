# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.stub_core — session-scoped unchanged-file stub state machine.

ONE implementation of the Lever-1 "unchanged-file stub" contract, shared by
the two surfaces that serve file content to an agent:

  - the MCP bridge (``mcp_bridge.MCPBridge._maybe_stub``) — tool-result reads
  - the shim (``shim_stub.py``) — native ``open()`` reads in agent processes

Both used to carry private copies of the same state machine. The constants
looked drifted (TTL 300 vs 60, "cap" 2 vs 3, floor 800 vs 0) but the cap was
an illusion: the bridge incremented before checking ``>= 3``, the shim
checked before incrementing with 2 — both surfaces effectively allowed TWO
consecutive stubs before a full send. The real policy differences are TTL
and floor (bridge serves Claude Code, which paces compaction faster and can
reference any tool result; the shim serves Hermes native reads). The
contract now lives here; each surface instantiates it with its own policy
and its own recovery note.

Contract (identical on both surfaces)
-------------------------------------
A stub replaces byte-identical content that the agent's harness ALREADY
holds, with a one-line note. Wire-verified: the replaced tokens provably
never reach the LLM, counted in the measured ``mcp_dedup`` tier via the
caller's ``counter_fn``. Safety rails:

  - only when the caller says the read is agent-context-shaped
    (``is_agent``) and byte-lossless (``lossless``)
  - byte floor (``min_chars``) — a stub must be strictly cheaper
  - TTL (``ttl_s``): after this long since the last FULL send, serve full
    content again. The mtime+size cache gate proves the DISK content is
    current; the TTL is the only guard for the AGENT-memory axis: harnesses
    compact old tool results away, so a stub older than the compaction
    horizon references content the agent no longer holds (a stranded
    recovery turn costs more than the duplicate re-send). 0 disables
    stubbing entirely.
  - consecutive cap (``max_consecutive``): after N consecutive stubs, one
    full send (compaction armor — bounds any stranding inside a hot loop)
  - changed digest (file content differs) → full send, state re-arms
  - ``reset_path`` on write/invalidation (process-local best effort; the
    mtime gate covers cross-process writes)

Per-instance state, thread-safe (lock). stdlib only.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from toolrecall.keys import content_digest

log = logging.getLogger("toolrecall.stub_core")


# ─── Recovery notes ──────────────────────────────────────────────────────────
# The note must tell the agent how to get the content back if the harness no
# longer holds it. The escape hatch differs per surface, so each surface picks
# its builder; both live here so the wording stays in one reviewable place.


def note_agent(full_sent_at: float) -> str:
    """Note for the shim surface (native agent reads — Hermes, Codex, …).

    Native read tools have no ``bypass_cache`` argument; the honest recovery
    hint is "re-read bypassing the cache" via whatever the harness offers
    (Hermes: the MCP ``cache_refresh_file`` tool; other harnesses: their own
    fresh-read path).
    """
    return (
        f"[toolrecall] File unchanged since {time.strftime('%H:%M:%S', time.gmtime(full_sent_at))} UTC; "
        "content withheld (agent holds it from the earlier read). "
        "If you no longer have it, re-read the file bypassing the cache "
        "(e.g. the MCP cache_refresh_file tool or your read tool's fresh-read option)."
    )


def note_mcp(full_sent_at: float) -> str:
    """Note for the MCP bridge surface (byte-identical to the pre-core text)."""
    return "File unchanged; see earlier read. If you no longer have it, pass bypass_cache=true."


class FileStubSession:
    """Session-scoped stub state: path → (digest, last full send, streak).

    One instance per serving surface per process:
      - bridge: an instance attribute (lifetime = the MCP session — exactly
        the scope in which "the agent already holds this" is true)
      - shim: a module-global in ``shim_stub`` (lifetime = the agent process)
    """

    def __init__(
        self,
        *,
        ttl_s: float = 300.0,
        max_consecutive: int = 2,
        min_chars: int = 800,
        note_fn=None,
        counter_fn=None,
    ) -> None:
        self.ttl_s = float(ttl_s)
        self.max_consecutive = max(0, int(max_consecutive))
        self.min_chars = max(0, int(min_chars))
        self._note_fn = note_fn or note_agent
        self._counter_fn = counter_fn
        self._lock = threading.Lock()
        # key -> {"digest": str, "full_sent_at": float, "consecutive": int}
        self._session: dict[str, dict] = {}

    # ── public API ──

    def maybe_stub(
        self,
        path: str,
        content: str,
        *,
        is_agent: bool = True,
        lossless: bool = True,
    ) -> str | None:
        """Return the stub note if this read should be stubbed, else None.

        None means "serve the content as usual" — and (on full sends)
        registers/refreshes the stub state for this path. Called with the
        FULL content the surface was about to serve.
        """
        if self.ttl_s <= 0:  # 0 = stubbing disabled (see module docstring)
            return None
        if not is_agent or not lossless:
            return None
        if len(content) < self.min_chars:
            return None
        if not path:
            return None

        key = os.path.realpath(os.path.expanduser(path))
        digest = content_digest(content)
        now = time.time()
        with self._lock:
            state = self._session.get(key)
            if state is None or state["digest"] != digest:
                # first sight (or file changed): full send, arm the stub
                self._session[key] = {
                    "digest": digest,
                    "full_sent_at": now,
                    "consecutive": 0,
                }
                return None
            # seen before, same digest — stub candidate
            if now - state["full_sent_at"] > self.ttl_s:
                # TTL expiry: the harness may have compacted the earlier
                # copy since the full send. Serve full content, restart the
                # window AND the streak — a streak that predates the gap
                # says nothing about what the agent holds now.
                state["full_sent_at"] = now
                state["consecutive"] = 0
                return None
            if state["consecutive"] >= self.max_consecutive:
                # compaction armor: force one full send before more stubs
                state["consecutive"] = 0
                state["full_sent_at"] = now
                return None
            state["consecutive"] += 1
            tokens = len(content) // 4
            note = self._note_fn(state["full_sent_at"])
        self._count_measured(tokens, path, note)
        return note

    def reset_path(self, path: str) -> None:
        """Clear stub state for a path (write/invalidation in THIS process)."""
        if not path:
            return
        key = os.path.realpath(os.path.expanduser(path))
        with self._lock:
            self._session.pop(key, None)

    def reset_all(self) -> None:
        with self._lock:
            self._session.clear()

    # ── internals ──

    def _count_measured(self, tokens: int, path: str, note: str = "") -> None:
        """Fire-and-forget daemon counter (measured mcp_dedup tier).

        Best-effort with a logged swallow: a counter failure must never
        break a read, and daemon-down is a designed fallback — but it is
        logged at debug so daemon-restart storms stay visible in the log
        rail without becoming warnings.
        """
        if self._counter_fn is None:
            return
        try:
            self._counter_fn(tokens, path, note)
        except Exception:  # noqa: BLE001 — counter never breaks a read
            log.debug("stub counter failed (path=%s)", path, exc_info=True)
