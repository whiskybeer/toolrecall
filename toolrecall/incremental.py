# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.incremental — session-scoped append-only incremental file read suppression.

Provides byte-level append verification and an LRU-bounded session cache for
serving only newly appended tails of growing files (e.g. logs, trace outputs,
append-only JSON arrays). Fail-closed on first read and on any non-append edit.
"""

from __future__ import annotations

import collections
import fnmatch
import os
import threading

from toolrecall.keys import content_digest

_JSON_CLOSING_BYTES = frozenset(b"]},\r\n\t ")


def verify_append(prev: bytes, curr: bytes, allow_closing_trim: int = 32) -> dict:
    """Verify whether curr is an append-only extension of prev.

    Returns a dict with verification details:
      - append_only: bool
      - mode: "exact" | "json_tail" | "none"
      - unchanged_prefix_bytes: int
      - tail_bytes: bytes
      - total_bytes: int
      - replaced_suffix_bytes: int (only when mode is "json_tail")
    """
    if isinstance(prev, str):
        prev = prev.encode("utf-8", "replace")
    if isinstance(curr, str):
        curr = curr.encode("utf-8", "replace")

    if not prev or len(curr) <= len(prev):
        return {
            "append_only": False,
            "mode": "none",
            "unchanged_prefix_bytes": 0,
            "tail_bytes": curr,
            "total_bytes": len(curr),
        }

    if curr.startswith(prev):
        return {
            "append_only": True,
            "mode": "exact",
            "unchanged_prefix_bytes": len(prev),
            "tail_bytes": curr[len(prev) :],
            "total_bytes": len(curr),
        }

    if allow_closing_trim > 0:
        k = len(os.path.commonprefix([prev, curr]))
        trim_len = len(prev) - k
        if 0 < trim_len <= allow_closing_trim and set(prev[k:].strip()) <= _JSON_CLOSING_BYTES:
            return {
                "append_only": True,
                "mode": "json_tail",
                "unchanged_prefix_bytes": k,
                "replaced_suffix_bytes": trim_len,
                "tail_bytes": curr[k:],
                "total_bytes": len(curr),
            }

    return {
        "append_only": False,
        "mode": "none",
        "unchanged_prefix_bytes": 0,
        "tail_bytes": curr,
        "total_bytes": len(curr),
    }


class IncrementalReadSession:
    """Session-scoped incremental read suppression.

    Tracks append-only file growth across read operations. When a large file
    grows strictly via append, only the newly appended tail is sent over the wire,
    avoiding redundant tokens.
    """

    def __init__(
        self,
        max_session_bytes: int = 64 * 1024 * 1024,
        allow_closing_trim: int = 32,
    ) -> None:
        self.max_session_bytes = max(0, int(max_session_bytes))
        self.allow_closing_trim = max(0, int(allow_closing_trim))
        self._lock = threading.Lock()
        self._session: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._total_bytes: int = 0

    def _store(self, key: str, curr: bytes) -> None:
        """Store or update key with curr bytes, maintaining LRU and byte bounds."""
        if key in self._session:
            old_entry = self._session.pop(key)
            self._total_bytes -= old_entry.get("total_bytes", 0)

        entry = {
            "bytes": curr,
            "sha256": content_digest(curr),
            "total_bytes": len(curr),
        }
        self._session[key] = entry
        self._total_bytes += len(curr)

        while self._total_bytes > self.max_session_bytes and len(self._session) > 1:
            _k, old = self._session.popitem(last=False)
            self._total_bytes -= old.get("total_bytes", 0)

    def present(
        self,
        path: str,
        content: str,
        *,
        enabled: bool = False,
        min_bytes: int = 65536,
        allow_paths: list[str] | None = None,
    ) -> dict | None:
        """Evaluate if content can be served incrementally as a tail append."""
        if not enabled:
            return None
        if not path or not isinstance(content, str):
            return None

        curr = content.encode("utf-8", "replace")
        if len(curr) < min_bytes:
            return None

        if allow_paths:
            expanded_path = os.path.expanduser(path)
            matched = False
            for pat in allow_paths:
                expanded_pat = os.path.expanduser(pat)
                if fnmatch.fnmatch(expanded_path, expanded_pat) or fnmatch.fnmatch(
                    path, expanded_pat
                ):
                    matched = True
                    break
            if not matched:
                return None

        key = os.path.realpath(os.path.expanduser(path))

        with self._lock:
            prev_entry = self._session.get(key)
            if prev_entry is None:
                self._store(key, curr)
                return None

            prev_bytes = prev_entry["bytes"]
            res = verify_append(prev_bytes, curr, allow_closing_trim=self.allow_closing_trim)
            if not res["append_only"]:
                self._store(key, curr)
                return None

            self._store(key, curr)
            out = {
                "incremental": True,
                "path": path,
                "mode": res["mode"],
                "unchanged_prefix_bytes": res["unchanged_prefix_bytes"],
                "tail": res["tail_bytes"].decode("utf-8", "replace"),
                "total_bytes": res["total_bytes"],
                "tokens_avoided": res["unchanged_prefix_bytes"] // 4,
                "note": (
                    "prefix verified unchanged; showing appended tail only "
                    "(pass bypass_cache=true for full file)"
                ),
            }
            if "replaced_suffix_bytes" in res:
                out["replaced_suffix_bytes"] = res["replaced_suffix_bytes"]
            return out

    def reset_path(self, path: str) -> None:
        """Clear state for a path."""
        if not path:
            return
        key = os.path.realpath(os.path.expanduser(path))
        with self._lock:
            old = self._session.pop(key, None)
            if old is not None:
                self._total_bytes -= old.get("total_bytes", 0)

    def reset_all(self) -> None:
        """Clear all session state."""
        with self._lock:
            self._session.clear()
            self._total_bytes = 0
