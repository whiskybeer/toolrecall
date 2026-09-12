# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""
LangGraph Adapter — ToolRecallCheckpointSaver + @cached_tool for graph nodes.

Two integration points for LangGraph (langgraph is an OPTIONAL dependency —
the core of this module is framework-free and importable without it):

1. Checkpoint persistence via ``ToolRecallCheckpointSaver`` (bound lazily to
   ``langgraph.checkpoint.base.BaseCheckpointSaver`` — see ``get_saver()``).
   Graph thread state survives process restarts and is shared through the
   ToolRecall cache — enabling fork/resume workflows where a tree-search
   orchestrator restarts a graph from any prior checkpoint.

2. ``@cached_tool`` decorator for node/tool callables: identical-argument
   calls return the recorded result instead of re-executing (API calls,
   searches, DB lookups — anything the interpreter shim cannot see).

Usage (checkpoints):
    from langgraph.graph import StateGraph
    from toolrecall.adapters.langgraph import get_saver

    graph = StateGraph(MyState).compile(
        checkpointer=get_saver()
    )

Usage (tool cache):
    from toolrecall.adapters.langgraph import cached_tool

    @cached_tool(ttl=3600)
    def search_web(query: str) -> str:
        return external_api_call(query)  # runs only on cache miss

Architecture:
    Same pattern as the google_adk adapter: thin wrappers around
    toolrecall.cache.cached_mcp_check / cached_mcp_store. The adapter
    "server" namespace is "langgraph", so keys never collide with other
    frameworks. Graceful bypass: daemon-down → tools execute live,
    checkpoints are dropped, lookups return None.

    Checkpoint storage (mcp_cache rows under the "langgraph" namespace):
        lg:<thread_id>:<ns>:<checkpoint_id> -> {ckpt, meta, parent, pending}
        lgidx:<thread_id>:<ns>              -> JSON list of checkpoint ids,
                                               oldest first (thread index)

Honest scoping (MVP):
    - Payloads serialize with LangGraph's JsonPlusSerializer when langgraph
      is installed; without it, a stdlib JSON fallback handles plain-dict
      state only (complex channel values require langgraph or a custom
      serde passed to the constructor).
    - put_writes() records pending writes for interrupt/resume; there is no
      dedicated writes table — they live inside the checkpoint row.
    - No workspace/filesystem restore: forking a coding agent's repo state
      is an integration-boundary concern (git worktree contract), covered
      by the tree-search design (docs/TREE_SEARCH.md), not by this adapter.

Note:
    langgraph is optional. Import the saver only when it is actually used;
    the cached_tool decorator works without langgraph installed.
"""

import base64
import functools
import inspect
import json
import logging
from typing import Any, Callable, Optional

from toolrecall.cache import cached_mcp_check, cached_mcp_store
from toolrecall import client as _tr_client

logger = logging.getLogger("toolrecall.adapters.langgraph")

ADAPTER_SERVER = "langgraph"  # Cache namespace for LangGraph checkpoints/tools

_KEY_PREFIX = "lg"
_IDX_PREFIX = "lgidx"


# ---- Serde (framework-free fallback + lazy LangGraph binding) ---------------


class _StdlibSerde:
    """Minimal serde for plain-dict state when langgraph is not installed.

    Matches the JsonPlusSerializer method names (dumps_typed / loads_typed)
    so saver code is identical in both modes. Raises on non-JSON types
    instead of silently degrading them.
    """

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        try:
            return "json", json.dumps(obj, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise TypeError(
                "State is not JSON-serializable and langgraph is not "
                "installed. Install langgraph (or pass a custom serde) to "
                "checkpoint complex channel values."
            ) from e

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        type_, raw = data
        if type_ == "json":
            return json.loads(raw.decode("utf-8"))
        raise ValueError(
            f"Cannot deserialize checkpoint of type '{type_}' without "
            "langgraph installed (JsonPlusSerializer required)."
        )


def _default_serde() -> Any:
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        return JsonPlusSerializer()
    except ImportError:
        return _StdlibSerde()


# ---- Tool cache (framework-free decorator) -----------------------------------


def _tool_key(tool_name: str, args: tuple, kwargs: dict) -> str:
    """Deterministic cache key from tool name + call arguments."""
    return f"{tool_name}:{args!r}:{sorted(kwargs.items())!r}"


def cached_tool(func=None, *, ttl: Optional[int] = None, name: Optional[str] = None):
    """Decorator: cache graph-node/tool function results via ToolRecall.

    Works with or without arguments, sync or async::

        @cached_tool           # default TTL
        @cached_tool(ttl=300)  # custom TTL in seconds

    On daemon-down, the function executes live (graceful bypass).
    """

    def decorate(fn: Callable) -> Callable:
        tool_name = name or str(getattr(fn, "__name__", "tool") or "tool")

        def _lookup(args: tuple, kwargs: dict) -> Optional[Any]:
            if not _tr_client.daemon_running():
                return None
            key = _tool_key(tool_name, args, kwargs)
            result = cached_mcp_check(ADAPTER_SERVER, tool_name, {"key": key}, ttl=ttl)
            if not result.get("cached"):
                return None
            try:
                return json.loads(result["data"])
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Tool cache deserialization error for %s: %s", tool_name, e)
                return None

        def _store(args: tuple, kwargs: dict, value: Any) -> None:
            if not _tr_client.daemon_running():
                return
            key = _tool_key(tool_name, args, kwargs)
            try:
                serialized = json.dumps(value, default=repr)
            except (TypeError, ValueError) as e:
                logger.warning("Failed to serialize tool result for %s: %s", tool_name, e)
                return
            try:
                cached_mcp_store(
                    _SaverCore._request_hash(tool_name, key),
                    ADAPTER_SERVER,
                    tool_name,
                    {"key": key},
                    serialized,
                    ttl=ttl,
                )
            except Exception as e:  # cache must never break the tool
                logger.warning("Failed to store tool result for %s: %s", tool_name, e)
                return
            logger.info("Cache STORE langgraph/tool:%s - %d chars", tool_name, len(serialized))

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                hit = _lookup(args, kwargs)
                if hit is not None:
                    logger.info("Cache HIT   langgraph/tool:%s", tool_name)
                    return hit
                value = await fn(*args, **kwargs)
                _store(args, kwargs, value)
                return value

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            hit = _lookup(args, kwargs)
            if hit is not None:
                logger.info("Cache HIT   langgraph/tool:%s", tool_name)
                return hit
            value = fn(*args, **kwargs)
            _store(args, kwargs, value)
            return value

        return wrapper

    if func is not None:
        return decorate(func)
    return decorate


# ---- Checkpoint saver core (framework-free, unit-testable) -------------------


class _SaverCore:
    """Framework-free checkpoint persistence logic.

    Key layout (mcp_cache rows under the "langgraph" namespace):
        lg:<thread_id>:<ns>:<checkpoint_id>  -> {ckpt, meta, parent, pending}
        lgidx:<thread_id>:<ns>               -> JSON list of checkpoint ids,
                                                oldest first (thread index)
    """

    def __init__(self, ttl: Optional[int] = None, serde: Optional[Any] = None):
        self._ttl = ttl
        self._serde = serde if serde is not None else _default_serde()

    # -- key helpers -------------------------------------------------------

    @staticmethod
    def _row_key(thread_id: str, ns: str, checkpoint_id: str) -> str:
        return f"{_KEY_PREFIX}:{thread_id}:{ns}:{checkpoint_id}"

    @staticmethod
    def _index_key(thread_id: str, ns: str) -> str:
        return f"{_IDX_PREFIX}:{thread_id}:{ns}"

    # -- cache plumbing ------------------------------------------------------

    @staticmethod
    def _request_hash(tool: str, key: str) -> str:
        """Mirror cached_mcp_check's key derivation so stores land on the
        same request_hash the check reads. Needed because cached_mcp_check
        returns ``key`` only on the miss path. Guarded by roundtrip tests."""
        import json as _json

        from toolrecall._db import _hash

        args_json = _json.dumps({"key": key}, sort_keys=True)
        return _hash(f"{ADAPTER_SERVER}://{tool}?{args_json}")

    def _check(self, key: str) -> Optional[str]:
        if not _tr_client.daemon_running():
            return None
        try:
            result = cached_mcp_check(ADAPTER_SERVER, "checkpoint", {"key": key}, ttl=self._ttl)
        except Exception as e:
            logger.warning("Checkpoint cache check failed: %s", e)
            return None
        return result["data"] if result.get("cached") else None

    def _store_row(self, key: str, payload_json: str) -> None:
        if not _tr_client.daemon_running():
            return
        try:
            cached_mcp_store(
                self._request_hash("checkpoint", key),
                ADAPTER_SERVER,
                "checkpoint",
                {"key": key},
                payload_json,
                ttl=self._ttl,
            )
        except Exception as e:
            logger.warning("Checkpoint cache store failed: %s", e)

    # -- index management ------------------------------------------------

    def _read_index(self, thread_id: str, ns: str) -> list[str]:
        raw = self._check(self._index_key(thread_id, ns))
        if not raw:
            return []
        try:
            ids = json.loads(raw)
            return ids if isinstance(ids, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _write_index(self, thread_id: str, ns: str, ids: list[str]) -> None:
        self._store_row(self._index_key(thread_id, ns), json.dumps(ids))

    def _append_index(self, thread_id: str, ns: str, checkpoint_id: str) -> None:
        ids = self._read_index(thread_id, ns)
        if checkpoint_id not in ids:
            ids.append(checkpoint_id)
        self._write_index(thread_id, ns, ids)

    # -- payload serde -----------------------------------------------------

    def _serialize(self, obj: Any) -> dict:
        type_, blob = self._serde.dumps_typed(obj)
        return {"t": type_, "d": base64.b64encode(blob).decode("ascii")}

    def _deserialize(self, payload: dict) -> Any:
        return self._serde.loads_typed((payload["t"], base64.b64decode(payload["d"])))

    # -- row read/write (pure dict-in/dict-out, unit-testable) -------------

    def build_row(
        self,
        checkpoint: Any,
        metadata: Optional[dict],
        parent_checkpoint: Optional[Any],
        pending_writes_json: str,
    ) -> str:
        """Serialize one checkpoint row (pure function for tests)."""
        row = {
            "ckpt": self._serialize(checkpoint),
            "meta": json.dumps(metadata or {}, default=str),
            "parent": None,
            "pending": pending_writes_json,
        }
        if parent_checkpoint is not None:
            row["parent"] = self._serialize(parent_checkpoint)
        return json.dumps(row)

    def parse_row(self, raw: str) -> Optional[dict]:
        """Deserialize one checkpoint row -> {checkpoint, metadata, parent,
        pending_writes}; None on corruption (logged, never raised)."""
        try:
            row = json.loads(raw)
            out = {
                "checkpoint": self._deserialize(row["ckpt"]),
                "metadata": json.loads(row["meta"]) if row.get("meta") else {},
                "parent": self._deserialize(row["parent"]) if row.get("parent") else None,
                "pending": json.loads(row["pending"]) if row.get("pending") else [],
            }
            return out
        except (json.JSONDecodeError, TypeError, KeyError, ValueError) as e:
            logger.warning("Checkpoint row deserialization error: %s", e)
            return None

    # -- public core operations ---------------------------------------------

    def put(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint: Any,
        metadata: Optional[dict],
        parent_checkpoint: Optional[Any],
    ) -> str:
        """Store a checkpoint row + append to the thread index.

        Parent fallback: real graph runs don't always pass the previous
        checkpoint_id in config to put(), so parent_checkpoint can be None
        even mid-thread. When it is, link to the thread's current tip —
        matching how MemorySaver chains checkpoints in practice.
        """
        if parent_checkpoint is None:
            ids = self._read_index(thread_id, checkpoint_ns)
            if ids:
                prev = self.get(thread_id, checkpoint_ns, ids[-1])
                if prev is not None:
                    parent_checkpoint = prev["checkpoint"]
        row = self.build_row(checkpoint, metadata, parent_checkpoint, "[]")
        self._store_row(self._row_key(thread_id, checkpoint_ns, checkpoint["id"]), row)
        self._append_index(thread_id, checkpoint_ns, checkpoint["id"])
        return checkpoint["id"]

    def put_pending_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        writes: list[list],
        task_id: str,
    ) -> None:
        """Record pending writes for an interrupted checkpoint."""
        raw = self._check(self._row_key(thread_id, checkpoint_ns, checkpoint_id))
        if raw is None:
            logger.warning(
                "put_writes: checkpoint %s not found for thread %s", checkpoint_id, thread_id
            )
            return
        parsed = self.parse_row(raw)
        if parsed is None:
            return
        pending = parsed["pending"]
        # Write values are arbitrary objects — serde them like checkpoints.
        ser = [[chan, self._serialize(val)] for chan, val in writes]
        pending.append({"task_id": task_id, "writes": ser})
        row = self.build_row(
            parsed["checkpoint"], parsed["metadata"], parsed["parent"], json.dumps(pending)
        )
        self._store_row(self._row_key(thread_id, checkpoint_ns, checkpoint_id), row)

    def get(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: Optional[str]
    ) -> Optional[dict]:
        """Load one checkpoint dict (or the latest if checkpoint_id is None)."""
        cid = checkpoint_id
        if cid is None:
            ids = self._read_index(thread_id, checkpoint_ns)
            if not ids:
                return None
            cid = ids[-1]
        raw = self._check(self._row_key(thread_id, checkpoint_ns, cid))
        if raw is None:
            return None
        parsed = self.parse_row(raw)
        if parsed is None:
            return None
        parsed["checkpoint_id"] = cid
        return parsed

    def list_ids(
        self, thread_id: str, checkpoint_ns: str, limit: Optional[int] = None
    ) -> list[str]:
        """Checkpoint ids for a thread, oldest first."""
        ids = self._read_index(thread_id, checkpoint_ns)
        if limit is not None:
            ids = ids[-limit:] if limit > 0 else []
        return ids

    def delete_thread_rows(self, thread_id: str, checkpoint_ns: str) -> int:
        """Clear the thread index (rows age out via TTL). Returns ids removed."""
        ids = self._read_index(thread_id, checkpoint_ns)
        self._write_index(thread_id, checkpoint_ns, [])
        return len(ids)


# ---- Lazily-bound BaseCheckpointSaver subclass --------------------------------

_SaverCore_shared = None  # per-get_saver() instance; module-level only for tests
_BaseCheckpointSaver = None
_ToolRecallCheckpointSaverImpl = None


def get_saver(ttl: Optional[int] = None, serde: Optional[Any] = None):
    """Return a ToolRecallCheckpointSaver bound to langgraph's ABC.

    Requires langgraph; raises ImportError with install instructions
    otherwise. ``ToolRecallCheckpointSaver`` (the bound class) is also
    importable after the first ``get_saver()`` call.
    """
    global _BaseCheckpointSaver, _ToolRecallCheckpointSaverImpl

    if _ToolRecallCheckpointSaverImpl is not None:
        return _ToolRecallCheckpointSaverImpl(ttl=ttl, serde=serde)

    try:
        from langchain_core.runnables import RunnableConfig  # noqa: F401 (type surface)
        from langgraph.checkpoint.base import BaseCheckpointSaver
    except ImportError as e:
        raise ImportError(
            "langgraph is required for ToolRecallCheckpointSaver. "
            "Install it with: pip install toolrecall[langgraph] "
            "(or: pip install langgraph)"
        ) from e

    _BaseCheckpointSaver = BaseCheckpointSaver

    class ToolRecallCheckpointSaverImpl(BaseCheckpointSaver):
        """BaseCheckpointSaver backed by ToolRecall's cache.

        Thread state survives restarts; get_tuple() with an older
        checkpoint_id restores exactly that node — the fork/resume
        primitive tree-search orchestrators branch from.
        """

        def __init__(self, ttl=None, serde=None):
            super().__init__(serde=serde)
            self._core = _SaverCore(ttl=ttl, serde=serde if serde is not None else self.serde)
            self._ttl = self._core._ttl

        # -- sync API ------------------------------------------------------

        def get_tuple(self, config):
            from langgraph.checkpoint.base import CheckpointTuple

            thread_id = config["configurable"]["thread_id"]
            ns = config["configurable"].get("checkpoint_ns", "")
            cid = config["configurable"].get("checkpoint_id")
            parsed = self._core.get(thread_id, ns, cid)
            if parsed is None:
                return None
            chk = parsed["checkpoint"]
            cfg = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": parsed["checkpoint_id"],
                }
            }
            parent_cfg = None
            if parsed["parent"] is not None:
                parent_cfg = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": parsed["parent"]["id"],
                    }
                }
            pending = (
                [
                    (w["task_id"], chan, self._core._deserialize(pv))
                    for w in parsed["pending"]
                    for chan, pv in w["writes"]
                ]
                if parsed["pending"]
                else None
            )
            return CheckpointTuple(
                config=cfg,
                checkpoint=chk,
                metadata=parsed["metadata"],
                parent_config=parent_cfg,
                pending_writes=pending,
            )

        def list(self, config, *, filter=None, before=None, limit=None):

            thread_id = config["configurable"]["thread_id"] if config else None
            ns = (
                (config or {}).get("configurable", {}).get("checkpoint_ns", "") if thread_id else ""
            )
            ids = self._core.list_ids(thread_id, ns) if thread_id else []
            # Contract: newest FIRST (SqliteSaver semantics). list_ids is
            # oldest-first (append order) — reverse, then apply limit on the
            # newest end. `before` cuts everything newer than it.
            if before is not None:
                before_id = before["configurable"].get("checkpoint_id")
                if before_id in ids:
                    ids = ids[: ids.index(before_id)]
            ids = list(reversed(ids))
            if limit is not None:
                ids = ids[:limit]
            for cid in ids:
                tuple_ = self.get_tuple(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": ns,
                            "checkpoint_id": cid,
                        }
                    }
                )
                if tuple_ is not None:
                    if filter is not None:
                        meta = tuple_.metadata or {}
                        if not all(
                            meta.get(k) == v
                            or (isinstance(meta.get(k), dict) and v in (meta.get(k) or {}))
                            for k, v in filter.items()
                        ):
                            continue
                    yield tuple_

        def put(self, config, checkpoint, metadata, new_versions):
            thread_id = config["configurable"]["thread_id"]
            ns = config["configurable"].get("checkpoint_ns", "")
            parent = self._core.get(thread_id, ns, config["configurable"].get("checkpoint_id"))
            parent_ckpt = parent["checkpoint"] if parent else None
            self._core.put(thread_id, ns, checkpoint, dict(metadata or {}), parent_ckpt)
            return {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint["id"],
                }
            }

        def put_writes(self, config, writes, task_id, task_path=""):
            thread_id = config["configurable"]["thread_id"]
            ns = config["configurable"].get("checkpoint_ns", "")
            cid = config["configurable"].get("checkpoint_id")
            if cid is None:
                return
            self._core.put_pending_writes(thread_id, ns, cid, list(writes), task_id)

        def delete_thread(self, thread_id):
            self._core.delete_thread_rows(thread_id, "")

        # -- async: delegate to sync (SQLite via daemon is sync anyway) -----

        async def aget_tuple(self, config):
            return self.get_tuple(config)

        async def alist(self, config, *, filter=None, before=None, limit=None):
            for t in self.list(config, filter=filter, before=before, limit=limit):
                yield t

        async def aput(self, config, checkpoint, metadata, new_versions):
            return self.put(config, checkpoint, metadata, new_versions)

        async def aput_writes(self, config, writes, task_id, task_path=""):
            self.put_writes(config, writes, task_id, task_path)

        async def adelete_thread(self, thread_id):
            self.delete_thread(thread_id)

    _ToolRecallCheckpointSaverImpl = ToolRecallCheckpointSaverImpl
    return ToolRecallCheckpointSaverImpl(ttl=ttl, serde=serde)


def ToolRecallCheckpointSaver(ttl: Optional[int] = None, serde: Optional[Any] = None):
    """Factory function: returns a bound BaseCheckpointSaver instance.

    Function (not class) because the BaseCheckpointSaver subclass only
    exists after langgraph imports succeed — see get_saver().
    """
    return get_saver(ttl=ttl, serde=serde)


__all__ = [
    "ADAPTER_SERVER",
    "ToolRecallCheckpointSaver",
    "get_saver",
    "cached_tool",
]
