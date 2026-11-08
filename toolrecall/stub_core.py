# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.stub_core — session-scoped "content the agent already holds" state.

ONE implementation of the Lever-1 contract, shared by the surfaces that serve
file content to an agent:

  - the MCP bridge (``mcp_bridge.MCPBridge``) — tool-result reads
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
Two presentations, ONE state machine:

  - ``maybe_stub`` — the original stub-only surface used by the shim: returns
    the replacement note, or None to serve the content.
  - ``present`` — the richer surface used by the bridge: returns
    ``{"unchanged": True, "note": ...}``, ``{"changed": True, "diff": ...}``,
    or None to serve the content. Adds caller-chosen keys (line ranges) and
    diff presentation.

A presentation REPLACES content the agent's harness ALREADY holds, with a
one-line note or a unified diff. Wire-verified: the replaced tokens provably
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
  - session budget (``max_calls`` / ``max_tokens``): the same guard expressed
    in what actually pushes old results out of a transcript — tool calls and
    tokens emitted since the full copy was sent. Wall-clock alone miscounts:
    it over-keeps in a slow conversation and under-keeps in a fast one.
    ``None`` disables the axis (the shim's behaviour, which has no session
    clock). Values are grounded in the same measurement as the TTL
    (see ``DEFAULT_MAX_CALLS``).
  - consecutive cap (``max_consecutive``): after N consecutive SUPPRESSIONS
    (a stub or a diff), one full send (compaction armor — bounds any
    stranding inside a hot loop)
  - changed digest (file content differs) → a diff against the agent's last
    FULL copy when that diff is small enough, else a full send
  - ``reset_path`` / ``reset_key`` on write/invalidation, ``reset_all`` on a
    harness compaction signal (process-local best effort; the mtime gate
    covers cross-process writes)

The diff is ALWAYS computed against the agent's last FULL copy, never against
a diff this module previously sent: it cannot know whether the harness applied
that diff, and assuming it would compound — a silently diverging copy is
exactly the failure mode the validator policy exists to prevent. State
therefore advances only on full sends; diffs are recomputed from the same base
and the armor bounds how many are sent before a full re-send.

Per-instance state, thread-safe (lock). stdlib only.
"""

from __future__ import annotations

import difflib
import logging
import os
import threading
import time

from toolrecall.keys import content_digest

log = logging.getLogger("toolrecall.stub_core")

# Session-budget defaults, same evidence as the bridge's TTL: measured over
# 396 conversation-growth runs in the billed usage CSV, a live conversation
# ran a median 14 turns / 18.5 s per request before a context reset. The TTL
# (240 s) sits just under the median horizon (288 s); expressed in requests
# that horizon is 240 / 18.5 ≈ 13 calls, so the call budget mirrors the TTL
# rather than inventing a second, unrelated number.
DEFAULT_MAX_CALLS = 13
# Token budget as a share of the context the harness must keep results in:
# ~30% of a 200k window. Emitted tokens are what displace older tool results,
# so this is the context-pressure counterpart of the call budget.
DEFAULT_MAX_TOKENS = 60_000
# Send a diff only when it is at most this fraction of the full content:
# a diff that rewrites most of the file is harder to apply than a re-read.
DEFAULT_DIFF_RATIO = 0.5
# Diff bases are held in memory; files larger than this still stub/diff-free
# (they always take the full-send path, and no base is retained for them).
MAX_DIFF_BASE_BYTES = 512 * 1024
# Token estimate divisor, matching the rest of the cache layer (bytes // 3 is
# cache.py's DAST estimate; //4 is the conservative context estimate here).
_TOKEN_DIVISOR = 4


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


def _unified_diff(base: str, current: str) -> str:
    """Unified diff of ``base`` → ``current`` (empty when identical)."""
    return "".join(
        difflib.unified_diff(
            base.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile="previous",
            tofile="current",
        )
    )


def slice_lines(content: str, offset: int | None, limit: int | None) -> dict:
    """1-based line window of ``content`` (``offset``/``limit`` may be None).

    Returns the window plus its position in the full file, so a caller can
    tell the agent both what it received and where it sits. An ``offset``
    past EOF yields empty content with ``start_line > end_line`` — an honest
    "nothing there" rather than a silently clamped window.
    """
    lines = content.splitlines(keepends=True)
    total = len(lines)
    start = max(1, int(offset)) if offset else 1
    end = total if not limit else min(total, start + max(0, int(limit)) - 1)
    return {
        "content": "".join(lines[start - 1 : end]),
        "start_line": start,
        "end_line": end,
        "total_lines": total,
    }


class FileStubSession:
    """Session-scoped suppression state: key → (digest, last full send, streak).

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
        max_calls: int | None = None,
        max_tokens: int | None = None,
        diff_ratio: float = DEFAULT_DIFF_RATIO,
    ) -> None:
        self.ttl_s = float(ttl_s)
        self.max_consecutive = max(0, int(max_consecutive))
        self.min_chars = max(0, int(min_chars))
        self.max_calls = None if max_calls is None else max(0, int(max_calls))
        self.max_tokens = None if max_tokens is None else max(0, int(max_tokens))
        # 0 rails diffs off entirely (full send instead).
        self.diff_ratio = max(0.0, float(diff_ratio))
        self._note_fn = note_fn or note_agent
        self._counter_fn = counter_fn
        self._lock = threading.Lock()
        # key -> {"digest", "full_sent_at", "consecutive", "base",
        #         "at_call", "at_tokens"}
        self._session: dict[str, dict] = {}
        # Session clock: advanced by the caller's tick() with what it emitted.
        self._calls = 0
        self._tokens = 0

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
        registers/refreshes the state for this path. Called with the FULL
        content the surface was about to serve. The stub-only legacy surface
        (the shim); diffs are declined here by construction.
        """
        if not is_agent or not lossless:
            return None
        key = os.path.realpath(os.path.expanduser(path)) if path else ""
        body = self._view(
            key, content, allow_diff=False, min_chars=self.min_chars, note_fn=self._note_fn
        )
        if body is None or not body.get("unchanged"):
            return None
        return body["note"]

    def present(
        self,
        key: str,
        content: str,
        *,
        allow_diff: bool = True,
        min_chars: int | None = None,
        lossless: bool = True,
    ) -> dict | None:
        """Decide how to present ``content`` for ``key``.

        ``key`` is opaque to this module (the bridge passes a realpath, or
        ``realpath#start-end`` for a line-range read). Returns None when the
        full content must be sent (and records it), otherwise the replacement
        body: ``{"unchanged": True, "note": ...}`` or
        ``{"changed": True, "diff": ...}``.
        """
        if not lossless:
            return None
        return self._view(
            key,
            content,
            allow_diff=allow_diff,
            min_chars=self.min_chars if min_chars is None else min_chars,
            note_fn=self._note_fn,
        )

    def tick(self, emitted_text: str = "") -> None:
        """Advance the session clock by one tool response of ``emitted_text``.

        What counts is what ENTERED the agent's context, so the caller passes
        the text it actually sent (a stub/diff is cheap, which is the point:
        suppressed tokens do not displace anything).
        """
        with self._lock:
            self._calls += 1
            if emitted_text:
                self._tokens += len(emitted_text) // _TOKEN_DIVISOR

    def reset_path(self, path: str) -> None:
        """Clear state for a path (write/invalidation in THIS process).

        Ranged views of the same file are keyed ``<real>#start-end``, so
        invalidating the file must invalidate every window of it too.
        """
        if not path:
            return
        real = os.path.realpath(os.path.expanduser(path))
        with self._lock:
            self._session.pop(real, None)
            for key in [k for k in self._session if k.startswith(real + "#")]:
                self._session.pop(key, None)

    def reset_key(self, key: str) -> None:
        """Clear state for an opaque key (already resolved by the caller)."""
        if not key:
            return
        with self._lock:
            self._session.pop(key, None)

    def reset_all(self) -> None:
        with self._lock:
            self._session.clear()

    # ── internals ──

    def _fresh(self, state: dict, now: float) -> bool:
        """True while the agent can be assumed to still hold the full copy."""
        if now - state["full_sent_at"] > self.ttl_s:
            return False
        if self.max_calls is not None and self._calls - state["at_call"] > self.max_calls:
            return False
        if self.max_tokens is not None and self._tokens - state["at_tokens"] > self.max_tokens:
            return False
        return True

    def _arm(self, key: str, content: str, digest: str, now: float) -> None:
        """Register a FULL send: the agent now holds exactly ``content``."""
        self._session[key] = {
            "digest": digest,
            "full_sent_at": now,
            "consecutive": 0,
            "base": content if len(content) <= MAX_DIFF_BASE_BYTES else None,
            "at_call": self._calls,
            "at_tokens": self._tokens,
        }

    def _view(self, key, content, *, allow_diff, min_chars, note_fn):
        """Shared decision: None = serve full content, else the replacement body."""
        if self.ttl_s <= 0:  # 0 = stubbing disabled (see module docstring)
            return None
        if not key:
            return None
        # Empty content is never a saving: the stub note would COST more tokens
        # than the empty body it replaces.
        if not content or len(content) < min_chars:
            return None

        digest = content_digest(content)
        now = time.time()
        with self._lock:
            state = self._session.get(key)
            if state is None or not self._fresh(state, now):
                # First sight, file changed, or the freshness window lapsed:
                # full send, and restart the window AND the streak — a streak
                # that predates the gap says nothing about what the agent
                # holds now.
                self._arm(key, content, digest, now)
                return None
            if state["consecutive"] >= self.max_consecutive:
                # Compaction armor: force one full send before more suppression.
                self._arm(key, content, digest, now)
                return None
            if state["digest"] == digest:
                state["consecutive"] += 1
                body = {"unchanged": True, "note": note_fn(state["full_sent_at"])}
            elif allow_diff and state["base"] is not None:
                diff = _unified_diff(state["base"], content)
                if not diff or len(diff) > self.diff_ratio * len(content):
                    # No base to diff from, or a rewrite the agent could not
                    # usefully patch: re-send the file. NOTE the state is
                    # advanced to the NEW content here — that IS the copy the
                    # agent is about to hold.
                    self._arm(key, content, digest, now)
                    return None
                # Deliberately NOT advancing digest/base/full_sent_at: whether
                # the harness applied this diff is unknowable, so the next diff
                # is computed from the same last FULL copy (see module docstring).
                state["consecutive"] += 1
                body = {"changed": True, "diff": diff}
            else:
                self._arm(key, content, digest, now)
                return None

            tokens = len(content) // _TOKEN_DIVISOR
            if body.get("changed"):
                tokens -= len(body["diff"]) // _TOKEN_DIVISOR

        if tokens > 0:
            self._count_measured(tokens, key, body.get("note", ""))
        return body

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
