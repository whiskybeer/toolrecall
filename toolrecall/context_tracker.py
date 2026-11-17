"""
Context Tracker — in-memory checkpoint-based dirty-file tracking.

Enables agents to safely drop clean file content from their context
window, keeping context bounded and breaking O(n²) attention cost growth.

Architecture:
  - Purely in-memory (no SQLite — state resets on daemon restart)
  - Thread-safe via Lock
  - Checkpoint = monotonically increasing integer ID
  - Dirty = files written via cached_write/cached_patch since checkpoint
  - Clean = files read but not written since checkpoint

The agent pattern:
  1. context_set_checkpoint("start")  → records "everything is clean"
  2. Agent reads files (no dirty tracking)
  3. Agent writes/patches files → marked dirty
  4. context_get_dirty(checkpoint)    → {dirty: [...], clean: [...]}
  5. Agent drops clean files from context
  6. Agent sets new checkpoint
  7. Repeat

Thread safety:
  All public methods acquire self._lock. The daemon calls
  mark_dirty() from the ThreadPoolExecutor, so multi-thread safety
  is essential.
"""

import fnmatch
import inspect
import json
import os  # noqa: E402 (needed by mark_dirty/mark_read below — see note at file end)
import time
import warnings
from threading import RLock
from typing import Any

# ─── Hint emission safety ─────────────────────────────────
# The stale-file list is injected into the agent's context by the MCP
# bridge. Paths are attacker-influenceable: a repo can contain a file
# whose *name* carries newlines or the closing marker, letting it break
# out of the block and inject instructions into the model's context.
# POSIX permits every byte except NUL and '/' in a filename.
#
# Everything below exists so that ToolRecall never becomes the delivery
# mechanism for a prompt injection it was asked to protect against.
STALE_MARKER_OPEN = "=== stale-files ==="
STALE_MARKER_CLOSE = "=== end stale-files ==="

# Hard caps. An unbounded hint appended to *every* tool result would
# grow context — the exact failure this feature exists to prevent.
MAX_HINT_PATHS = 20
MAX_HINT_PATH_LEN = 512


def sanitize_path_for_hint(path: str) -> str | None:
    """Make a path safe to embed in agent-visible text.

    Returns None if the path cannot be safely represented, in which
    case the caller must omit it rather than emit it raw.

    Rejects:
      - control characters (newlines, CR, NUL, ANSI escapes)
      - either stale-file marker appearing inside the path
      - absurdly long paths (context-flooding)
    """
    if not path or len(path) > MAX_HINT_PATH_LEN:
        return None
    # Any C0/C1 control char lets a filename forge line structure.
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in path):
        return None
    if STALE_MARKER_OPEN in path or STALE_MARKER_CLOSE in path:
        return None
    return path


def _is_sensitive(path: str) -> bool:
    """Reuse the cache layer's sensitive-file blocklist.

    Lazy import keeps context_tracker importable standalone (and keeps
    the zero-dependency promise: _db is stdlib-only). Fails closed on
    any error — an unknown path is treated as sensitive rather than
    leaked into agent-visible output.
    """
    try:
        from toolrecall._db import _is_sensitive_path

        return _is_sensitive_path(path)
    except Exception:
        return True


def format_stale_block(paths: list[str]) -> str:
    """Render the machine-parseable stale-files block.

    Caps the list and drops unsafe names. Returns "" for an empty or
    fully-rejected list so callers can skip appending entirely.
    """
    safe = [p for p in (sanitize_path_for_hint(p) for p in paths) if p]
    if not safe:
        return ""
    shown, hidden = safe[:MAX_HINT_PATHS], len(safe) - MAX_HINT_PATHS
    lines = [STALE_MARKER_OPEN, *shown]
    if hidden > 0:
        lines.append(f"... and {hidden} more (call context_get_stale for the full list)")
    lines.append(STALE_MARKER_CLOSE)
    return "\n".join(lines)


class ContextTracker:
    """In-memory checkpoint-based dirty-file tracker.

    Tracked files:
      self._dirty: {path: {mtime: float, tick: int}}
        — files written via cached_write/cached_patch since last checkpoint

      self._read_set: {path: mtime}
        — files that have been read (via cached_read) since last checkpoint
        Helps determine "clean" = was read but never written

      self._checkpoint_counter: int
        — monotonically increasing checkpoint ID

      self._ctx_dropped_tokens: int
        — BANKED-ONCE estimate of tokens dropped from context.
        Each clean file is added exactly ONCE per checkpoint epoch: a file
        already counted is NOT re-counted on later get_dirty() calls, so
        the total never inflates with polling. Per-call contribution of
        the CURRENT clean set is returned by get_dirty() as
        ctx_dropped_tokens (per-call view, never double-added).

      self._banked: set[str]
        — clean files already added to the cumulative counter; guards
        against re-counting the same file on every poll.

      self._ignore_prefixes: tuple[str, ...]
        — absolute path prefixes NEVER tracked as reads. Daemon-side
        serve-through reads (agent session transcripts, agent config /
        cache files) were never present in any agent context and can
        never be dropped from one — counting them poisoned the clean
        set and inflated every potential-savings figure (verified
        2026-09-10: 303 of 334 tracked files were session JSONs,
        ~380MB — orders of magnitude overstatement).
    """

    def __init__(
        self,
        ignore_prefixes: list[str] | tuple[str, ...] | None = None,
        branch_id: str | None = None,
    ):
        self._dirty: dict[str, dict] = {}
        self._read_set: dict[str, float] = {}
        self._checkpoint_counter: int = 0
        self._ctx_dropped_tokens: int = 0
        self._agent_armed: bool = False
        self._banked: set[str] = set()
        # ── Branch scoping (G4) ─────────────────────────────
        # None = legacy behavior (shared singleton row in tracker_state).
        # A branch id namespaces persistence into tracker_state_branch so
        # concurrent tree branches snapshot/restore independently and a
        # branch's clean/dirty state never leaks into another's view.
        self._branch_id: str | None = branch_id
        self._ignore_prefixes: tuple[str, ...] = tuple(
            os.path.expanduser(p) for p in (ignore_prefixes or ())
        )
        self._ignore_globs: tuple[str, ...] = tuple(
            p for p in self._ignore_prefixes if any(c in p for c in "*?[")
        )
        # Literal prefixes are anchored to an absolute base; glob patterns
        # stay raw and are fnmatch-tested against the absolute path.
        # Anchor EVERY literal prefix at a directory boundary (always append
        # os.sep): abspath() strips a trailing separator, so testing the
        # ORIGINAL p for endswith(os.sep) was always False for slash-terminated
        # prefixes (every [tracker].ignore_read_prefixes default) and the
        # anchor silently vanished — "/tmp/sessions/" degraded to the
        # unanchored "/tmp/sessions", which also matched "/tmp/sessions_extra".
        self._ignore_prefixes = tuple(
            # Root "/" special case: abspath + os.sep would yield "//",
            # which startswith() never matches — keep the root prefix as-is
            # ("ignore everything" stays "ignore everything").
            (os.path.abspath(p) if os.path.abspath(p) == os.sep else os.path.abspath(p) + os.sep)
            for p in self._ignore_prefixes
            if not any(c in p for c in "*?[")
        )
        # ── Staleness ordering (v0.8.15) ──────────────────
        # Monotonic operation counter. Incremented on every read and
        # every write. Lets us answer a question the checkpoint model
        # cannot: did the write happen *after* the read?
        #
        # A file that was written but never read has no stale copy in
        # the agent's context — there is nothing to evict. Only a file
        # that was read, and *then* overwritten, is provably stale.
        self._seq: int = 0
        self._read_seq: dict[str, int] = {}
        self._write_seq: dict[str, int] = {}
        self._last_compaction_manifest: list[dict] = []
        self._compaction_count: int = 0
        self._post_compaction_reads_remaining: int = 0
        self._anchors: dict[str, dict] = {}  # abs_path -> {"anchor": str, "file_tokens": int}
        self._cost_ledger: dict[
            str, dict
        ] = {}  # abs_path -> {"turns_billed": int, "billed_tokens": int, "billed_cost_usd": float}
        self._lock = RLock()

    # ─── Persistence (option A): snapshot / restore via _db() ──────
    #
    # The tracker was purely in-memory — every daemon restart reset the
    # checkpoint flow and the agent had to re-arm it. Snapshot state is
    # persisted into the same SQLite DB the caches use (tracker_state
    # table, singleton row, JSON payload) through the shared _db() lock;
    # restore on boot is gated by a TTL and re-validates read-set mtimes.

    def snapshot(self) -> dict:
        """Serialize tracker state into the shared DB (tracker_state row).

        Called on graceful daemon shutdown and periodically (crash
        insurance). Best-effort by design: any DB failure logs a warning
        and returns {"saved": False, ...} — persistence must never break
        the daemon's stop path. The JSON payload carries everything
        restore() needs: read set (path → mtime at read time), dirty map,
        read/write sequence numbers, banked-once guard set, banked token
        total, checkpoint counter, and the armed flag.

        Returns:
            {"saved": bool, "paths": int, "dirty": int, "checkpoint": int}
        """
        with self._lock:
            payload: dict = {
                "checkpoint_counter": self._checkpoint_counter,
                "armed": self._agent_armed,
                "ctx_dropped_tokens": self._ctx_dropped_tokens,
                "seq": self._seq,
                # Sorted for deterministic payloads (easier diffing in tests).
                "read_set": {p: self._read_set[p] for p in sorted(self._read_set)},
                "dirty": {p: self._dirty[p] for p in sorted(self._dirty)},
                "read_seq": {p: self._read_seq[p] for p in sorted(self._read_seq)},
                "write_seq": {p: self._write_seq[p] for p in sorted(self._write_seq)},
                "banked": sorted(self._banked),
                "compaction_count": self._compaction_count,
                "last_compaction_manifest": [dict(e) for e in self._last_compaction_manifest],
                "cost_ledger": {
                    p: dict(self._cost_ledger[p])
                    for p in sorted(self._cost_ledger)
                    if not _is_sensitive(p)
                },
            }
        saved_at = time.time()
        try:
            from toolrecall._db import _db

            with _db() as conn:
                if self._branch_id is None:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS tracker_state ("
                        "id INTEGER PRIMARY KEY CHECK (id = 1),"
                        "payload TEXT NOT NULL,"
                        "saved_at REAL NOT NULL)"
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO tracker_state (id, payload, saved_at) VALUES (1, ?, ?)",
                        (json.dumps(payload), saved_at),
                    )
                else:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS tracker_state_branch ("
                        "branch_id TEXT PRIMARY KEY,"
                        "payload TEXT NOT NULL,"
                        "saved_at REAL NOT NULL)"
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO tracker_state_branch "
                        "(branch_id, payload, saved_at) VALUES (?, ?, ?)",
                        (self._branch_id, json.dumps(payload), saved_at),
                    )
            read_n = len(payload["read_set"])
            dirty_n = len(payload["dirty"])
            return {
                "saved": True,
                "paths": read_n,
                "dirty": dirty_n,
                "checkpoint": payload["checkpoint_counter"],
            }
        except Exception as e:  # noqa: BLE001 — persistence must never break shutdown
            warnings.warn(f"ToolRecall: tracker snapshot failed: {e}")
            return {
                "saved": False,
                "paths": len(payload["read_set"]),
                "dirty": len(payload["dirty"]),
                "checkpoint": payload["checkpoint_counter"],
            }

    def restore(self, ttl: float = 21600.0) -> dict:
        """Restore tracker state from the DB row written by snapshot().

        Gates:
          - TTL (seconds): a read set from yesterday describes files the
            agent saw in a PREVIOUS session — restoring it across sessions
            is wrong. Default 6h; 0 disables restore.
          - Sensitive paths: never restored (defense in depth — cached_read
            blocks them, but the snapshot predates the current blocklist).
          - Read-set mtime re-validation: a file whose mtime changed after
            the snapshot was written is no longer the content the agent
            saw; it is dropped from the read set rather than reported
            clean (which would invite a wrong drop).

        Never arms the flow implicitly: the daemon's startup checkpoint
        (armed=False) stays distinct from an agent-driven one, so the TUI
        can still tell "restored, agent never checkpointed this boot" from
        a live flow. If the snapshot says the flow WAS armed, the armed
        flag is restored with it — that's exactly what persistence means.

        Returns:
            {"restored": bool, "reason": str, "paths": int, "dirty": int}
            reason: "ok" | "no-state" | "ttl-expired" | "disabled" | "error"
        """
        if ttl <= 0:
            return {"restored": False, "reason": "disabled", "paths": 0, "dirty": 0}
        try:
            from toolrecall._db import _db, _is_sensitive_path

            with _db() as conn:
                if self._branch_id is None:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS tracker_state ("
                        "id INTEGER PRIMARY KEY CHECK (id = 1),"
                        "payload TEXT NOT NULL,"
                        "saved_at REAL NOT NULL)"
                    )
                    row = conn.execute(
                        "SELECT payload, saved_at FROM tracker_state WHERE id = 1"
                    ).fetchone()
                else:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS tracker_state_branch ("
                        "branch_id TEXT PRIMARY KEY,"
                        "payload TEXT NOT NULL,"
                        "saved_at REAL NOT NULL)"
                    )
                    row = conn.execute(
                        "SELECT payload, saved_at FROM tracker_state_branch WHERE branch_id = ?",
                        (self._branch_id,),
                    ).fetchone()
        except Exception as e:  # noqa: BLE001 — a broken DB must not break boot
            warnings.warn(f"ToolRecall: tracker restore failed: {e}")
            return {"restored": False, "reason": "error", "paths": 0, "dirty": 0}
        if row is None:
            return {"restored": False, "reason": "no-state", "paths": 0, "dirty": 0}
        try:
            payload = json.loads(row["payload"])
            saved_at = float(row["saved_at"])
        except (ValueError, TypeError, KeyError):
            return {"restored": False, "reason": "error", "paths": 0, "dirty": 0}
        if time.time() - saved_at > ttl:
            return {"restored": False, "reason": "ttl-expired", "paths": 0, "dirty": 0}

        read_set: dict[str, float] = {}
        for p, mtime in (payload.get("read_set") or {}).items():
            if _is_sensitive_path(p):
                continue
            # Ignore-prefix filter on restore too: a snapshot from before a
            # prefix was added (or written by an older build without the
            # gate) must not resurrect poisoned entries (2026-09-12: infra
            # reads like ~/.toolrecall/proxy_usage.csv poisoned the set).
            if self._ignore_prefixes or self._ignore_globs:
                skip = False
                for pref in self._ignore_prefixes:
                    if p.startswith(pref):
                        skip = True
                        break
                if not skip:
                    for pat in self._ignore_globs:
                        if fnmatch.fnmatch(p, pat):
                            skip = True
                            break
                if skip:
                    continue
            try:
                cur = os.path.getmtime(p)
            except OSError:
                continue  # file deleted since snapshot — not readable, not clean
            if abs(cur - float(mtime)) > 1e-6:
                continue  # modified after snapshot — content is new, drop it
            read_set[p] = float(mtime)

        dirty: dict[str, dict] = {}
        for p, info in (payload.get("dirty") or {}).items():
            if _is_sensitive_path(p):
                continue
            # Dirty entries are kept even when the file vanished: the
            # write DID happen, and get_stale() already renders deleted
            # files as size-0 stale entries.
            dirty[p] = {
                "mtime": float(info.get("mtime", 0.0)),
                "tick": int(info.get("tick", 0)),
            }

        compaction_count = max(0, int(payload.get("compaction_count", 0)))
        raw_manifest = payload.get("last_compaction_manifest") or []
        manifest: list[dict] = []
        for entry in raw_manifest:
            if isinstance(entry, dict):
                p = entry.get("path")
                if p and not _is_sensitive_path(p):
                    manifest.append(dict(entry))

        cost_ledger: dict[str, dict] = {}
        for p, cinfo in (payload.get("cost_ledger") or {}).items():
            if _is_sensitive_path(p):
                continue
            if isinstance(cinfo, dict):
                cost_ledger[p] = {
                    "turns_billed": int(cinfo.get("turns_billed", 0)),
                    "billed_tokens": int(cinfo.get("billed_tokens", 0)),
                    "billed_cost_usd": float(cinfo.get("billed_cost_usd", 0.0)),
                }

        with self._lock:
            self._checkpoint_counter = max(0, int(payload.get("checkpoint_counter", 0)))
            self._agent_armed = bool(payload.get("armed", False))
            self._ctx_dropped_tokens = max(0, int(payload.get("ctx_dropped_tokens", 0)))
            self._seq = max(0, int(payload.get("seq", 0)))
            self._read_set = read_set
            self._dirty = dirty
            self._read_seq = {
                p: int(v) for p, v in (payload.get("read_seq") or {}).items() if p in read_set
            }
            self._write_seq = {
                p: int(v) for p, v in (payload.get("write_seq") or {}).items() if p in dirty
            }
            # Banked-once guard set: restored as-is so tokens already
            # banked pre-restart are never re-added post-restart.
            self._banked = {
                p for p in (payload.get("banked") or []) if isinstance(p, str) and p in read_set
            }
            self._compaction_count = compaction_count
            self._last_compaction_manifest = manifest
            self._cost_ledger = cost_ledger
        return {
            "restored": True,
            "reason": "ok",
            "paths": len(read_set),
            "dirty": len(dirty),
        }

    def clear_persisted(self) -> bool:
        """Delete the persisted snapshot row (companion to reset()).

        Branch-scoped trackers (branch_id set) clear their own
        tracker_state_branch row; legacy trackers clear the singleton
        tracker_state row exactly as before.

        Returns True when a row was removed, False otherwise (best-effort).
        A missing tracker_state table is the normal never-snapshotted case
        — handled silently, not warned about.
        """
        try:
            from toolrecall._db import _db

            with _db() as conn:
                if self._branch_id is None:
                    cur = conn.execute("DELETE FROM tracker_state WHERE id = 1")
                    return bool(cur.rowcount)
                cur = conn.execute(
                    "DELETE FROM tracker_state_branch WHERE branch_id = ?",
                    (self._branch_id,),
                )
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001 — reset must not fail on a broken DB
            if "no such table" not in str(e).lower():
                warnings.warn(f"ToolRecall: tracker persisted-state clear failed: {e}")
            return False

    def set_checkpoint(self, name: str = "", armed: bool = True) -> dict:
        """Set a checkpoint — everything clean after this point.

        Auto-accumulates ctx_dropped_tokens: files that were read since the
        last checkpoint but never dirtied are estimated as tokens the agent
        "dropped" from context. BANKED-ONCE (2026-09-10): each clean file is
        counted exactly once; repeated calls do not re-count already-banked
        files.

        Args:
            name: Optional human-readable label for the checkpoint.
            armed: Mark the flow as agent-driven. The daemon's startup
                auto-checkpoint passes armed=False so the TUI can
                distinguish "daemon restarted, agent never drove the flow"
                from a real agent checkpoint.

        Returns:
            {"checkpoint": int, "name": str, "dirty_before": int}
        """
        with self._lock:
            self._checkpoint_counter += 1
            dirty_count = len(self._dirty)

            # Auto-accumulate ctx_dropped_tokens: files read but never
            # dirtied since the last checkpoint have been "dropped" from
            # the agent's context. Estimate their token cost so the
            # healthcheck metric is always live.
            #
            # BANKED-ONCE GUARD (fix 2026-09-10): each clean file is added
            # exactly once. Previously every checkpoint/get_dirty call
            # re-added the ENTIRE current clean set to the cumulative
            # counter — N polls inflated the total N× the same set
            # (proven live: 82.87M → 165.73M after ONE get_dirty call).
            for p in self._read_set:
                if p in self._dirty or p in self._banked:
                    continue
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                self._ctx_dropped_tokens += size // 4
                self._banked.add(p)

            # An agent checkpoint (not the daemon-start one) arms the
            # flow — the TUI distinguishes "armed, nothing dropped yet"
            # from "never checkpointed".
            if armed:
                self._agent_armed = True

            result = {
                "checkpoint": self._checkpoint_counter,
                "name": name,
                "dirty_before": dirty_count,
            }
        return result

    def arm(self, source: str = "") -> dict:
        """Mark the flow agent-driven WITHOUT banking a checkpoint.

        ``set_checkpoint(armed=True)`` arms as a side effect of banking clean
        files; arming on its own needs no banking and must not move the
        checkpoint counter. Transport surfaces call this on the first request
        of a session (MCP bridge handshake, forward proxy request), so the flag
        reflects reality for ANY agent and does not depend on the agent calling
        ``context_set_checkpoint`` every turn — a convention no shipped
        harness follows (measured 2026-11-11: agent_armed=false after 61 days,
        233 clean files staged, nothing dropped, while the benchmark harness
        that does follow it reported 7.4×).

        Idempotent: repeated calls keep the flow armed and report
        ``already=True`` so callers can log the first transition only.

        Returns:
            {"armed": True, "already": bool, "armed_by": str}
        """
        with self._lock:
            already = self._agent_armed
            self._agent_armed = True
            return {"armed": True, "already": already, "armed_by": source}

    def is_armed(self) -> bool:
        """Whether an agent-driven flow has been seen (cheap, lock-guarded)."""
        with self._lock:
            return self._agent_armed

    def mark_dirty(self, path: str) -> None:
        """Record a file write. Called by daemon handlers.

        Args:
            path: Absolute path to the file that was written/patched.
        """
        if not path:
            return
        with self._lock:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = time.time()
            abs_path = os.path.abspath(path)
            self._dirty[abs_path] = {
                "mtime": mtime,
                "tick": self._checkpoint_counter,
            }
            self._seq += 1
            self._write_seq[abs_path] = self._seq

    def mark_read(self, path: str, content: str | None = None) -> None:
        """Record a file read. Called by daemon on cached_read.

        This lets the tracker know which files the agent has seen.
        A file that is in read_set but NOT in dirty is "clean" —
        safe to drop from context.

        Reads under _ignore_prefixes are NOT tracked (fix 2026-09-10):
        agent session transcripts and agent config/cache files are read
        daemon-side on serve-through but are never present in any agent
        context — counting them poisoned the clean set (~380MB of
        session JSONs) and inflated potential-savings figures.

        Args:
            path: Absolute path to the file that was read.
            content: Optional file content string for midpoint anchor indexing.
        """
        if not path:
            return
        abs_path = os.path.abspath(os.path.expanduser(path))
        if self._ignore_prefixes or self._ignore_globs:
            for pref in self._ignore_prefixes:
                if abs_path.startswith(pref):
                    return
            for pat in self._ignore_globs:
                if fnmatch.fnmatch(abs_path, pat):
                    return
        with self._lock:
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                mtime = time.time()
            self._read_set[abs_path] = mtime
            self._seq += 1
            self._read_seq[abs_path] = self._seq
            if (
                content is not None
                and isinstance(content, str)
                and len(content) >= 64
                and not _is_sensitive(abs_path)
            ):
                mid = len(content) // 2
                raw_slice = content[max(0, mid - 32) : mid + 32]
                anchor = json.dumps(raw_slice)[1:-1]
                self._anchors[abs_path] = {
                    "anchor": anchor,
                    "file_tokens": max(1, len(content) // 4),
                }

    # Preserve (self, path) for inspect.signature (regression guard in test_context_source_gate).
    mark_read.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter("path", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ]
    )

    def _externally_modified(self) -> list[dict]:
        """Read-set paths whose on-disk mtime differs from the recorded one.

        Caller MUST hold ``self._lock`` (both query methods call this while
        holding it; ``threading.Lock`` is not reentrant).

        Closes the native-write blindness: ``mark_dirty`` only fires for
        writes through the daemon (cached_write/cached_patch) — an agent
        writing via its own native tools (Hermes write_file/patch, any
        ``open(path, 'w')`` in-process) never touched ``_dirty``, so the
        tracker kept reporting such files as "clean" although the context
        copy was provably outdated.

        The read set stores the mtime AT READ TIME (mark_read); comparing
        it against the live mtime detects any out-of-band modification
        without knowing who wrote it. restore() has re-validated read-set
        mtimes exactly this way since persistence shipped — same logic,
        moved to query time. Pure at query time: no state mutation, so
        repeated polls are stable and a later mark_read naturally clears
        the flag (it refreshes the stored mtime).

        Exclusions (all deliberate):
          - paths with write_seq > read_seq: the DAEMON wrote these after
            the read (cached_write/patch) — already covered by the normal
            dirty/stale paths, not "external".
          - paths that have disappeared: deletion is not a content change
            we can validate — the context copy was correct at read time
            and a re-read errors loudly on its own; temp-file cleanup
            churn would make deleted-file reports noise. (get_stale's
            deleted-file OSError branch covers only files the daemon
            wrote before deletion — those iterate via write_seq.)
          - sensitive paths: defense in depth, same egress rule as
            get_stale (results are echoed to a possibly-injected agent).
        """
        out: list[dict] = []
        for path, r_mtime in self._read_set.items():
            r_seq = self._read_seq.get(path, 0)
            w_seq = self._write_seq.get(path, 0)
            if w_seq > r_seq:
                continue  # daemon wrote it — normal dirty/stale path
            try:
                cur = os.path.getmtime(path)
            except OSError:
                continue  # deleted — get_stale's OSError branch reports it
            if _is_sensitive(path):
                # Defense in depth, same rule as get_stale: this result is
                # echoed to a possibly-injected agent; re-check at egress
                # rather than trust upstream filters.
                continue
            if abs(cur - float(r_mtime)) > 1e-6:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                out.append(
                    {
                        "path": path,
                        "read_mtime": float(r_mtime),
                        "current_mtime": cur,
                        "size": size,
                        "est_tokens": size // 4,
                    }
                )
        return out

    def get_dirty(self, checkpoint: int | None = None) -> dict:
        """Get dirty and clean files since a checkpoint.

        Args:
            checkpoint: Checkpoint ID to diff against.
                None = use current checkpoint.

        Returns:
            {
                "dirty": ["/abs/path", ...],
                "clean": ["/abs/path", ...],
                "externally_modified": ["/abs/path", ...],
                "externals_detail": [{path, read_mtime, current_mtime,
                                      size, est_tokens}, ...],
                "total_externally_modified": int,
                "checkpoint": int,
                "total_dirty": int,
                "total_clean": int,
            }

        ``externally_modified``: read-set files whose on-disk mtime moved
        after the read — rewritten OUTSIDE the daemon (agent native tools,
        other processes). The context copy is outdated exactly as if the
        write had come through cached_write; treat them as dirty. They are
        listed separately (the "dirty" contract stays daemon-write-scoped)
        and re-reading the path clears the flag (mark_read refreshes the
        stored mtime).
        """
        with self._lock:
            target = checkpoint if checkpoint is not None else self._checkpoint_counter

            # All callers get checkpoint-scoped results:
            # - checkpoint=None → use current checkpoint
            # - checkpoint=N     → scope to checkpoint N
            # - checkpoint=0     → scope to checkpoint 0 (post-reset)
            dirty_list = [
                path for path, info in self._dirty.items() if info.get("tick", 0) >= target
            ]
            read_but_not_dirty = [
                p
                for p in self._read_set
                if p not in self._dirty or self._dirty[p].get("tick", 0) < target
            ]
            clean_list = list(set(read_but_not_dirty))

            # Estimate tokens for clean files (content length / 4 chars-per-token)
            clean_tokens = 0
            for p in clean_list:
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                clean_tokens += size // 4

            # BANKED-ONCE GUARD (fix 2026-09-10): previously every
            # get_dirty() call re-added the ENTIRE current clean set to
            # the cumulative counter — N polls inflated the total N× the
            # same set (proven live: 82.87M → 165.73M after ONE call).
            # Now each clean file is banked exactly once.
            for p in clean_list:
                if p in self._banked:
                    continue
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                self._ctx_dropped_tokens += size // 4
                self._banked.add(p)

            # Native-write detection: read-set paths whose on-disk mtime
            # moved after the read were rewritten OUTSIDE the daemon (agent
            # native tools, other processes). These were invisible to
            # mark_dirty and used to hide in "clean" with outdated context
            # copies. Reported separately — not merged into "dirty", whose
            # checkpoint contract stays daemon-write-scoped; the full list
            # rides in the response (cap on hint TEXT happens daemon-side).
            externals = self._externally_modified()
            return {
                "dirty": sorted(dirty_list),
                "clean": sorted(clean_list),
                "externally_modified": sorted((e["path"] for e in externals), key=str),
                "externals_detail": externals,
                "total_externally_modified": len(externals),
                "checkpoint": self._checkpoint_counter,
                "total_dirty": len(dirty_list),
                "total_clean": len(clean_list),
                "ctx_dropped_tokens": clean_tokens,
                "agent_armed": self._agent_armed,
                "cost_ranking": self.get_cost_ranking(),
            }

    def get_stale(self) -> dict:
        """Files that were read and *later* overwritten.

        The content of these files as it appears in the agent's
        conversation history is provably out of date: the agent saw
        version A, then something wrote version B to disk. Any copy of
        version A still sitting in the context window is wrong, and
        every subsequent turn re-sends those wrong bytes to the model.

        This is deliberately narrower than ``get_dirty()``:

          - ``dirty``  = written since checkpoint (may never have been
                         read — nothing stale in context)
          - ``clean``  = read, never written (safe to drop, but the
                         content is still *correct* — dropping is an
                         optimisation)
          - ``stale``  = read, then written (the content is *wrong* —
                         evicting is a correctness fix)

        A re-read after the write clears staleness, because the agent
        has then seen the current bytes.

        Returns:
            {
                "stale": [
                    {"path", "read_seq", "write_seq",
                     "size", "est_tokens", "mtime", "external"}, ...
                ],
                "paths": ["/abs/path", ...],
                "total_stale": int,
                "est_reclaimable_tokens": int,
                "externally_modified": ["/abs/path", ...],
                "checkpoint": int,
                "seq": int,
            }

        Entries written OUTSIDE the daemon (native agent tools) carry
        ``external: True`` and ``write_seq: 0`` — their staleness is proven
        by the mtime-vs-read-set comparison instead of the write sequence.
        """
        with self._lock:
            entries = []
            for path, w_seq in self._write_seq.items():
                r_seq = self._read_seq.get(path)
                if r_seq is None:
                    # Written but never read — no stale copy in context.
                    continue
                if r_seq >= w_seq:
                    # Most recent operation was a read: agent has seen
                    # the current bytes. Not stale.
                    continue
                if _is_sensitive(path):
                    # Defense in depth. These paths should never have
                    # been tracked (cached_read blocks them), but this
                    # result is echoed to a possibly-injected agent, so
                    # we re-check at egress rather than trust upstream.
                    continue
                try:
                    size = os.path.getsize(path)
                    mtime = os.path.getmtime(path)
                except OSError:
                    # File was deleted after being read. The context
                    # copy is still stale — arguably more so.
                    size, mtime = 0, None
                entries.append(
                    {
                        "path": path,
                        "read_seq": r_seq,
                        "write_seq": w_seq,
                        "size": size,
                        "est_tokens": size // 4,
                        "mtime": mtime,
                    }
                )

            # Native-write staleness: same mtime-vs-read_set comparison as
            # get_dirty, surfaced in the SAME shape as daemon-write staleness
            # so a native `open('w')` after a read reports exactly like a
            # cached_write after a read. A path appears in BOTH lists only
            # when it was deleted-and-recreated (the OSError branch above
            # keeps the deleted entry; the recreated file shows up here with
            # a new mtime) — union semantics, both true.
            externals = self._externally_modified()
            for e in externals:
                entries.append(
                    {
                        "path": e["path"],
                        "read_seq": self._read_seq.get(e["path"], 0),
                        "write_seq": 0,  # no daemon write — external one
                        "size": e["size"],
                        "est_tokens": e["est_tokens"],
                        "mtime": e["current_mtime"],
                        "external": True,
                    }
                )
            entries.sort(key=lambda e: str(e["path"]))
            return {
                "stale": entries,
                "paths": [e["path"] for e in entries],
                "total_stale": len(entries),
                "est_reclaimable_tokens": sum(e["est_tokens"] for e in entries),
                "externally_modified": sorted((e["path"] for e in externals), key=str),
                "checkpoint": self._checkpoint_counter,
                "seq": self._seq,
                "cost_ranking": self.get_cost_ranking(),
            }

    # ─── P3: Compaction Manifest & Post-Compaction Window ─────────

    def on_compacted(self, window_reads: int = 10) -> dict:
        """Record context compaction event and build handoff manifest.

        Builds a manifest containing STRICTLY metadata (never file content)
        for all non-sensitive paths in the current read set, arms a post-compaction
        read window, and increments the compaction counter.
        """
        with self._lock:
            manifest = []
            for p in sorted(self._read_set.keys()):
                if _is_sensitive(p) or sanitize_path_for_hint(p) is None:
                    continue
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                dirty = p in self._dirty
                stale = self._write_seq.get(p, 0) > self._read_seq.get(p, 0)
                manifest.append(
                    {
                        "path": p,
                        "mtime": float(self._read_set[p]),
                        "size": size,
                        "est_tokens": size // 4,
                        "dirty": dirty,
                        "stale": stale,
                    }
                )
            self._last_compaction_manifest = manifest
            self._compaction_count += 1
            self._post_compaction_reads_remaining = max(0, int(window_reads))
            return self.get_manifest()

    def get_manifest(self) -> dict:
        """Return the most recent compaction handoff manifest."""
        with self._lock:
            return {
                "compaction_count": self._compaction_count,
                "post_compaction_reads_remaining": self._post_compaction_reads_remaining,
                "manifest": [dict(e) for e in self._last_compaction_manifest],
                "total_files": len(self._last_compaction_manifest),
                "total_tokens": sum(
                    int(e.get("est_tokens", 0)) for e in self._last_compaction_manifest
                ),
            }

    def consume_post_compaction_window(self) -> bool:
        """Consume one read from the post-compaction window if available."""
        with self._lock:
            if self._post_compaction_reads_remaining > 0:
                self._post_compaction_reads_remaining -= 1
                return True
            return False

    # ─── P4.5: Wire-Verified Context-Cost Attribution ─────────────

    def get_active_anchors(self) -> dict[str, dict]:
        """Return midpoint anchors for active, non-sensitive read-set paths."""
        with self._lock:
            return {
                p: dict(v)
                for p, v in self._anchors.items()
                if p in self._read_set and not _is_sensitive(p)
            }

    def attribute_turn(
        self,
        body_text: str | bytes = "",
        prompt_tokens: int = 0,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
        matched_paths: list[str] | None = None,
    ) -> dict:
        """Attribute prompt tokens and cost to tracked files present in the prompt.

        If matched_paths is given, uses those paths directly (filtered to current
        read set and non-sensitive). Otherwise, verifies wire presence by searching
        for each active file's anchor in the request body text.
        """
        with self._lock:
            if matched_paths is not None:
                candidates = [os.path.abspath(os.path.expanduser(p)) for p in matched_paths if p]
                seen: set[str] = set()
                matched: list[str] = []
                for p in candidates:
                    if p not in seen and p in self._read_set and not _is_sensitive(p):
                        seen.add(p)
                        matched.append(p)
                matched.sort()
            else:
                if isinstance(body_text, bytes):
                    text = body_text.decode("utf-8", errors="replace")
                else:
                    text = str(body_text or "")
                matched = [
                    p
                    for p in sorted(self._read_set.keys())
                    if p in self._anchors
                    and not _is_sensitive(p)
                    and self._anchors[p]["anchor"] in text
                ]

            sum_tok = 0
            sum_cost = 0.0
            p_tokens = int(prompt_tokens or 0)
            c_usd = float(cost_usd or 0.0)

            for p in matched:
                ft = self._anchors.get(p, {}).get("file_tokens")
                if not ft:
                    try:
                        ft = os.path.getsize(p) // 4
                    except OSError:
                        ft = 0
                file_tokens = int(ft or 0)
                tok_attr = min(file_tokens, p_tokens) if p_tokens > 0 else file_tokens
                cost_attr = (
                    (c_usd * (tok_attr / p_tokens)) if (p_tokens > 0 and c_usd > 0.0) else 0.0
                )

                entry = self._cost_ledger.setdefault(
                    p,
                    {"turns_billed": 0, "billed_tokens": 0, "billed_cost_usd": 0.0},
                )
                entry["turns_billed"] += 1
                entry["billed_tokens"] += int(tok_attr)
                entry["billed_cost_usd"] = round(float(entry["billed_cost_usd"]) + cost_attr, 8)

                sum_tok += int(tok_attr)
                sum_cost += cost_attr

            return {
                "matched_files": len(matched),
                "paths": matched,
                "attributed_tokens": sum_tok,
                "attributed_cost_usd": round(sum_cost, 8),
            }

    def get_cost_ranking(self) -> list[dict]:
        """Rank files by attributed billed context cost."""
        with self._lock:
            ranking: list[dict[str, Any]] = []
            for p, info in self._cost_ledger.items():
                if _is_sensitive(p):
                    continue
                if p in self._dirty:
                    status = "dirty"
                elif p in self._read_set:
                    status = "clean"
                else:
                    status = "evicted"

                ft = self._anchors.get(p, {}).get("file_tokens", 0)
                if not ft:
                    try:
                        ft = os.path.getsize(p) // 4
                    except OSError:
                        ft = 0

                ranking.append(
                    {
                        "path": p,
                        "status": status,
                        "turns_billed": int(info.get("turns_billed", 0)),
                        "billed_tokens": int(info.get("billed_tokens", 0)),
                        "billed_cost_usd": float(info.get("billed_cost_usd", 0.0)),
                        "file_tokens": int(ft or 0),
                    }
                )

            ranking.sort(
                key=lambda e: (
                    -float(e["billed_cost_usd"]),
                    -int(e["billed_tokens"]),
                    str(e["path"]),
                )
            )
            return ranking

    def get_stats(self) -> dict:
        """Full status of the tracker.

        ctx_dropped_tokens_total is BANKED-ONCE: each clean file's size is
        added exactly once (per checkpoint epoch) when first returned by
        set_checkpoint() or get_dirty(). Polling get_dirty() no longer
        inflates the total (N polls used to add the same clean set N×).

        Per-call state (current clean set, its token estimate) comes from
        get_dirty(), not from this method.
        """
        with self._lock:
            dirty_list = list(self._dirty.keys())
            read_but_not_dirty = [p for p in self._read_set if p not in self._dirty]
            clean_list = list(set(read_but_not_dirty))

            total_billed_tokens = sum(
                int(e.get("billed_tokens", 0))
                for p, e in self._cost_ledger.items()
                if not _is_sensitive(p)
            )
            total_billed_cost = sum(
                float(e.get("billed_cost_usd", 0.0))
                for p, e in self._cost_ledger.items()
                if not _is_sensitive(p)
            )

            return {
                "dirty": sorted(dirty_list),
                "clean": sorted(clean_list),
                "checkpoint": self._checkpoint_counter,
                "total_dirty": len(dirty_list),
                "total_clean": len(clean_list),
                "total_read": len(self._read_set),
                "ctx_dropped_tokens_total": self._ctx_dropped_tokens,
                "agent_armed": self._agent_armed,
                "attributed_billed_tokens": total_billed_tokens,
                "attributed_cost_usd": round(total_billed_cost, 8),
                "compaction_count": self._compaction_count,
            }

    def reset(self) -> dict:
        """Clear all checkpoints and dirty state.

        After reset, tracker behaves as if freshly initialized.
        The agent should call set_checkpoint() again before resuming.

        Also clears the persisted snapshot row: reset means "forget
        everything", and a later daemon restart must not resurrect
        state the agent explicitly dropped.

        Returns:
            {"reset": True, "checkpoint": 0}
        """
        with self._lock:
            self._dirty.clear()
            self._read_set.clear()
            self._read_seq.clear()
            self._write_seq.clear()
            self._seq = 0
            self._checkpoint_counter = 0
            self._ctx_dropped_tokens = 0
            self._banked.clear()
            self._agent_armed = False
            self._last_compaction_manifest = []
            self._compaction_count = 0
            self._post_compaction_reads_remaining = 0
            self._anchors.clear()
            self._cost_ledger.clear()
            self.clear_persisted()
            return {"reset": True, "checkpoint": 0}

    @property
    def checkpoint(self) -> int:
        """Current checkpoint ID."""
        return self._checkpoint_counter
