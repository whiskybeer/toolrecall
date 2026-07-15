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
        self._lock = RLock()

    def set_checkpoint(self, name: str = "") -> dict:
        """Set a checkpoint — everything clean after this point.

        Args:
            name: Optional human-readable label for the checkpoint.

        Returns:
            {"checkpoint": int, "name": str, "dirty_before": int}
        """
        with self._lock:
            self._checkpoint_counter += 1
            dirty_count = len(self._dirty)
            # Don't clear dirty/read — they accumulate until the agent
            # asks. The checkpoint is just a timestamp reference.
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
            self._dirty[os.path.abspath(path)] = {
                "mtime": mtime,
                "tick": self._checkpoint_counter,
            }

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
            self._read_set[os.path.abspath(path)] = mtime

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
            target = checkpoint if checkpoint else self._checkpoint_counter

            # Filter dirty files: if a specific checkpoint is given, only
            # return files dirtied at or after that checkpoint.
            # If checkpoint is None (use current), return ALL dirty files
            # (everything written since the last reset).
            if checkpoint:
                dirty_list = [
                    path for path, info in self._dirty.items()
                    if info.get("tick", 0) >= target
                ]
                read_but_not_dirty = [
                    p for p in self._read_set
                    if p not in self._dirty
                    or self._dirty[p].get("tick", 0) < target
                ]
            else:
                dirty_list = list(self._dirty.keys())
                # Clean = was read but never written
                read_but_not_dirty = [
                    p for p in self._read_set
                    if p not in self._dirty
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

    def get_stats(self) -> dict:
        """Full status of the tracker.

        Returns:
            {
                "dirty": [...],
                "clean": [...],
                "checkpoint": int,
                "total_dirty": int,
                "total_clean": int,
                "total_read": int,
                "ctx_dropped_tokens_total": int,
            }
        """
        with self._lock:
            # Directly compute from internal state without calling get_dirty()
            # to avoid double-counting ctx_dropped_tokens.
            target = self._checkpoint_counter
            dirty_list = list(self._dirty.keys())
            read_but_not_dirty = [
                p for p in self._read_set
                if p not in self._dirty
            ]
            clean_list = list(set(read_but_not_dirty))
            return {
                "dirty": sorted(dirty_list),
                "clean": sorted(clean_list),
                "checkpoint": target,
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
            self._checkpoint_counter = 0
            self._ctx_dropped_tokens = 0
            return {"reset": True, "checkpoint": 0}

    @property
    def checkpoint(self) -> int:
        """Current checkpoint ID."""
        return self._checkpoint_counter
