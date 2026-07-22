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

import os  # noqa: E402 (needed by mark_dirty/mark_read below — see note at file end)
import time
from threading import RLock

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
        — cumulative estimate of tokens dropped from context
        Incremented each time get_dirty()/get_hint() returns clean files.
        Estimated as sum(len(content)/4) for each clean file.
    """

    def __init__(self):
        self._dirty: dict[str, dict] = {}
        self._read_set: dict[str, float] = {}
        self._checkpoint_counter: int = 0
        self._ctx_dropped_tokens: int = 0
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
        self._lock = RLock()

    def set_checkpoint(self, name: str = "") -> dict:
        """Set a checkpoint — everything clean after this point.

        Auto-accumulates ctx_dropped_tokens: files that were read since the
        last checkpoint but never dirtied are estimated as tokens the agent
        "dropped" from context. This happens automatically — no explicit
        get_dirty() call needed from either the agent or the daemon.

        Args:
            name: Optional human-readable label for the checkpoint.

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
            clean_read = [
                p for p in self._read_set
                if p not in self._dirty
            ]
            for p in clean_read:
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                self._ctx_dropped_tokens += size // 4

            result = {
                "checkpoint": self._checkpoint_counter,
                "name": name,
                "dirty_before": dirty_count,
            }
        return result

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

    def mark_read(self, path: str) -> None:
        """Record a file read. Called by daemon on cached_read.

        This lets the tracker know which files the agent has seen.
        A file that is in read_set but NOT in dirty is "clean" —
        safe to drop from context.

        Args:
            path: Absolute path to the file that was read.
        """
        if not path:
            return
        with self._lock:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = time.time()
            abs_path = os.path.abspath(path)
            self._read_set[abs_path] = mtime
            self._seq += 1
            self._read_seq[abs_path] = self._seq

    def get_dirty(self, checkpoint: int | None = None) -> dict:
        """Get dirty and clean files since a checkpoint.

        Args:
            checkpoint: Checkpoint ID to diff against.
                None = use current checkpoint.

        Returns:
            {
                "dirty": ["/abs/path", ...],
                "clean": ["/abs/path", ...],
                "checkpoint": int,
                "total_dirty": int,
                "total_clean": int,
            }
        """
        with self._lock:
            target = checkpoint if checkpoint is not None else self._checkpoint_counter

            # All callers get checkpoint-scoped results:
            # - checkpoint=None → use current checkpoint
            # - checkpoint=N     → scope to checkpoint N
            # - checkpoint=0     → scope to checkpoint 0 (post-reset)
            dirty_list = [
                path for path, info in self._dirty.items()
                if info.get("tick", 0) >= target
            ]
            read_but_not_dirty = [
                p for p in self._read_set
                if p not in self._dirty
                or self._dirty[p].get("tick", 0) < target
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

            self._ctx_dropped_tokens += clean_tokens

            return {
                "dirty": sorted(dirty_list),
                "clean": sorted(clean_list),
                "checkpoint": self._checkpoint_counter,
                "total_dirty": len(dirty_list),
                "total_clean": len(clean_list),
                "ctx_dropped_tokens": clean_tokens,
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
                     "size", "est_tokens", "mtime"}, ...
                ],
                "paths": ["/abs/path", ...],
                "total_stale": int,
                "est_reclaimable_tokens": int,
                "checkpoint": int,
                "seq": int,
            }
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
                entries.append({
                    "path": path,
                    "read_seq": r_seq,
                    "write_seq": w_seq,
                    "size": size,
                    "est_tokens": size // 4,
                    "mtime": mtime,
                })

            entries.sort(key=lambda e: e["path"])
            return {
                "stale": entries,
                "paths": [e["path"] for e in entries],
                "total_stale": len(entries),
                "est_reclaimable_tokens": sum(e["est_tokens"] for e in entries),
                "checkpoint": self._checkpoint_counter,
                "seq": self._seq,
            }

    def get_stats(self) -> dict:
        """Full status of the tracker.

        Returns confirmed cumulative state only — ctx_dropped_tokens_total
        counts files that were actually returned as clean by get_dirty().
        For the current pending estimate, use get_dirty().
        """
        with self._lock:
            dirty_list = list(self._dirty.keys())
            read_but_not_dirty = [
                p for p in self._read_set
                if p not in self._dirty
            ]
            clean_list = list(set(read_but_not_dirty))

            return {
                "dirty": sorted(dirty_list),
                "clean": sorted(clean_list),
                "checkpoint": self._checkpoint_counter,
                "total_dirty": len(dirty_list),
                "total_clean": len(clean_list),
                "total_read": len(self._read_set),
                "ctx_dropped_tokens_total": self._ctx_dropped_tokens,
            }

    def reset(self) -> dict:
        """Clear all checkpoints and dirty state.

        After reset, tracker behaves as if freshly initialized.
        The agent should call set_checkpoint() again before resuming.

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
            return {"reset": True, "checkpoint": 0}

    @property
    def checkpoint(self) -> int:
        """Current checkpoint ID."""
        return self._checkpoint_counter
