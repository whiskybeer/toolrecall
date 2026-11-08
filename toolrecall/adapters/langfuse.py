# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""
Langfuse Adapter — cached tool decorator that reports cache verdicts into
Langfuse traces.

Langfuse instruments the agent (traces, observations, cost per generation).
ToolRecall removes repeated tool work underneath. This adapter composes the
two: a ``@cached_tool`` decorator that

1. checks ToolRecall's cache under the ``langfuse`` namespace before running
   the wrapped function (hit → skip execution, zero re-billing),
2. writes the cache verdict (hit/miss, age, key prefix) into the CURRENT
   Langfuse span's metadata via ``langfuse.get_client().update_current_span()``,
   so the trace shows exactly which observations were served deterministically
   and which were billed.

Usage:
    from toolrecall.adapters.langfuse import cached_tool

    @cached_tool(ttl=300)
    def search_web(query: str) -> str:
        return external_api_call(query)      # runs only on cache miss

    # Inside an active Langfuse trace (e.g. under @langfuse.observe()):
    #   @observe
    #   def agent_turn(...):
    #       search_web("berlin weather")   # span metadata: {"toolrecall": {...}}
    #
    # Works without Langfuse too: verdict reporting is skipped silently.

Architecture:
    Thin wrapper over toolrecall.cache (cached_mcp_check/store). The daemon
    manages the SQLite cache — the adapter never opens a direct DB connection.
    Cache keys are namespaced under the "langfuse" server so they never collide
    with other adapters.

    Cache keys are derived from the BOUND signature of the wrapped function
    (positional and keyword calls with identical effective arguments collide
    into one entry). Parameters whose annotation references the langfuse SDK
    are excluded from the key by annotation-name inspection — no SDK import
    needed.

    Only JSON-round-trip-stable results are cached: on a hit the adapter
    returns ``json.loads(stored_data)``, so anything that would not survive
    that round trip (arbitrary objects) is never stored and always executes
    live. Store failures NEVER break the tool (house pattern, google_adk.py).

Note:
    langfuse is an OPTIONAL dependency and toolrecall carries no langfuse
    dependency at all. Without langfuse installed the decorator still caches;
    only the span-metadata reporting is skipped. No pip extra is required for
    runtime use — langfuse is the application's own dependency:

        pip install langfuse        # the app's choice, not toolrecall's
"""

import functools
import inspect
import json
import logging
import typing
from typing import Optional

from toolrecall.cache import (
    cached_mcp_check,
    cached_mcp_store,
    invalidate_mcp_server,
)
from toolrecall import client as _tr_client

logger = logging.getLogger("toolrecall.adapters.langfuse")

ADAPTER_SERVER = "langfuse"  # Cache namespace — never collides with other adapters

# Langfuse caps the serialized `langfuse.observation.metadata` attribute at
# 9 KB per observation and silently drops excess keys (and the attribute)
# when a payload exceeds it. The adapter therefore size-guards its own
# metadata writes and exposes a merge helper with a fidelity ladder.
OBSERVATION_METADATA_CAP_BYTES = 9216

__all__ = [
    "cached_tool",
    "cache_verdict_metadata",
    "merge_verdict_metadata",
    "invalidate_langfuse_cache",
    "OBSERVATION_METADATA_CAP_BYTES",
    "ADAPTER_SERVER",
]


# ---- Langfuse verdict reporting ---------------------------------------------


def cache_verdict_metadata(verdict: dict) -> dict:
    """Wrap a verdict dict under the namespaced metadata key.

    Kept public so applications can merge it into their own span metadata.
    Note: Langfuse v4 ``update_current_span(metadata=...)`` replaces the whole
    metadata object — merge BEFORE calling (see :func:`merge_verdict_metadata`).
    """
    return {"toolrecall": verdict}


def _json_bytes(payload: dict) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return OBSERVATION_METADATA_CAP_BYTES + 1  # unserializable → over cap


def merge_verdict_metadata(
    existing: Optional[dict],
    verdict: dict,
    cap: int = OBSERVATION_METADATA_CAP_BYTES,
) -> dict:
    """Merge a ToolRecall verdict into user metadata, size-guarded.

    Langfuse v4 silently drops the whole ``langfuse.observation.metadata``
    attribute when its serialized size exceeds ~9 KB per observation — the
    verdict must never be the reason user metadata disappears. Fidelity
    ladder when ``{**existing, **{"toolrecall": verdict}}`` would exceed
    ``cap`` bytes:

    1. Drop oversized string VALUES inside the verdict, keep all keys.
    2. Drop non-essential verdict keys (``stale``, ``bypassed``, ``ttl``),
       keep ``hit``, ``key``, ``age_s``.
    3. Shrink ``key`` to 8 hex chars.
    4. Emit ``{"toolrecall": {"hit": ..., "dropped": true}}`` — a hit verdict
       reduced to its boolean; a miss verdict is never dropped (a miss
       explains a billed call — losing it makes a live call look cached).

    A verdict on its own (existing=None) is always written: the ladder only
    consumes the verdict, never user keys.
    """
    merged = dict(existing or {})
    vr = dict(verdict)
    merged["toolrecall"] = vr
    if _json_bytes(merged) <= cap:
        return merged

    # Ladder 1: drop oversized string values, keep every key.
    for k, v in list(vr.items()):
        if isinstance(v, str) and len(v.encode("utf-8")) > 32:
            vr[k] = v[:32]
    if _json_bytes(merged) <= cap:
        return merged

    # Ladder 2: drop non-essential verdict keys (misses keep essential keys).
    essential = ("hit", "key", "age_s")
    if not vr.get("hit"):
        essential = ("hit", "key", "stale", "bypassed", "ttl")
    for k in list(vr):
        if k not in essential:
            vr.pop(k)
    if _json_bytes(merged) <= cap:
        return merged

    # Ladder 3: shrink key to 8 chars AND drop remaining non-essentials
    # (age_s/ttl/etc. after rung 2 already took the optional fields). The key
    # identifies the call — it must survive to here, not vanish into rung 4.
    if "key" in vr and isinstance(vr["key"], str):
        vr["key"] = vr["key"][:8]
    for k in list(vr):
        if k not in ("hit", "key", "dropped"):
            vr.pop(k)
    if _json_bytes(merged) <= cap:
        return merged

    # Ladder 4: minimal verdict — hits may collapse to the boolean; misses
    # keep a minimal explanatory shape and flag the truncation.
    if vr.get("hit"):
        return {**merged, "toolrecall": {"hit": True, "dropped": True}}
    minimal = {k: vr[k] for k in ("hit", "key") if k in vr}
    minimal["dropped"] = True
    return {**merged, "toolrecall": minimal}


def _report_verdict(verdict: dict) -> None:
    """Best-effort: write the verdict into the current Langfuse span.

    Silent no-op when langfuse is not installed, not initialized, or no span
    is active. Never raises — reporting must not break execution.
    """
    try:
        from langfuse import get_client

        get_client().update_current_span(
            metadata=merge_verdict_metadata(None, verdict)
        )
    except Exception as e:  # noqa: BLE001 — reporting is strictly best-effort
        logger.debug("Langfuse verdict reporting skipped: %s", e)


def _verdict_from_check(result: dict, ttl: Optional[int]) -> dict:
    """Project a cached_mcp_check result onto the reportable verdict shape."""
    v: dict = {"hit": bool(result.get("cached"))}
    key = result.get("key")
    if key:
        v["key"] = str(key)[:12]
    if result.get("stale"):
        v["stale"] = True
    if result.get("bypassed"):
        v["bypassed"] = True
    if "age_s" in result:
        v["age_s"] = round(result["age_s"], 1)
    if ttl is not None:
        v["ttl"] = ttl
    return v


# ---- Key derivation ----------------------------------------------------------


def _canonical_args(func, args: tuple, kwargs: dict) -> dict:
    """Bind call args to the signature so positional/keyword calls collide.

    Parameters whose annotation references the langfuse SDK are excluded from
    the key (SDK-injected stateful context, not request data). Annotation-name
    matching only — the adapter must stay importable without langfuse.
    """
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        params = dict(bound.arguments)
    except TypeError:
        params = dict(kwargs)

    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = dict(getattr(func, "__annotations__", {}) or {})

    for pname in list(params):
        anno = str(hints.get(pname, ""))
        if "langfuse" in anno.lower():
            params.pop(pname, None)

    return params


def _roundtrip_stable(value) -> bool:
    """True iff json.dumps/loads round-trips the value losslessly.

    The cache stores JSON; on a hit the stored bytes are parsed back. A value
    that does not survive that round trip (objects, sets, NaN, …) must never
    be stored — the hit path would return a different object than the live
    call. Conservative: only provably-identical values are cacheable.
    """
    try:
        data = json.dumps(value, ensure_ascii=False, default=str)
        return json.loads(data) == value
    except (TypeError, ValueError):
        return False


# ---- The decorator ------------------------------------------------------------


def cached_tool(ttl: Optional[int] = None, name: Optional[str] = None):
    """Cache a tool function via ToolRecall and report verdicts to Langfuse.

    - Hit (same tool + bound args): cached data returns in ~1ms, function
      NOT executed, verdict metadata attached to the current Langfuse span.
    - Miss: function runs; JSON-round-trip-stable results are stored;
      miss-verdict attached.
    - Daemon down: function executes live (graceful bypass).
    - ttl=0: bypass cache entirely (verdict says bypassed).

    Sync and async functions are both supported (async gets an async wrapper).
    """

    def decorator(func):
        tool_name = name or getattr(func, "__name__", "tool")

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                cargs = _canonical_args(func, args, kwargs)
                if not _tr_client.daemon_running():
                    return await func(*args, **kwargs)
                try:
                    check = cached_mcp_check(ADAPTER_SERVER, tool_name, cargs, ttl=ttl)
                except Exception as e:  # noqa: BLE001 — cache read must not break the tool
                    logger.warning("Cache check failed (executing live): %s", e)
                    return await func(*args, **kwargs)
                if check.get("cached"):
                    logger.info("Cache HIT  langfuse/%s", tool_name)
                    _report_verdict(_verdict_from_check(check, ttl))
                    try:
                        return json.loads(check["data"])
                    except Exception:  # noqa: BLE001 — corrupt row → live value
                        return await func(*args, **kwargs)
                verdict = _verdict_from_check(check, ttl)
                value = await func(*args, **kwargs)
                if _roundtrip_stable(value):
                    try:
                        data = json.dumps(value, ensure_ascii=False, default=str)
                        cached_mcp_store(
                            check["key"], ADAPTER_SERVER, tool_name, cargs, data, ttl=ttl
                        )
                    except Exception as e:  # noqa: BLE001 — store must not break the tool
                        logger.warning("Cache store failed (tool unaffected): %s", e)
                _report_verdict(verdict)
                return value

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cargs = _canonical_args(func, args, kwargs)
            if not _tr_client.daemon_running():
                return func(*args, **kwargs)
            try:
                check = cached_mcp_check(ADAPTER_SERVER, tool_name, cargs, ttl=ttl)
            except Exception as e:  # noqa: BLE001 — cache read must not break the tool
                logger.warning("Cache check failed (executing live): %s", e)
                return func(*args, **kwargs)
            if check.get("cached"):
                logger.info("Cache HIT  langfuse/%s", tool_name)
                _report_verdict(_verdict_from_check(check, ttl))
                try:
                    return json.loads(check["data"])
                except Exception:  # noqa: BLE001 — corrupt row → live value
                    return func(*args, **kwargs)
            verdict = _verdict_from_check(check, ttl)
            value = func(*args, **kwargs)
            if _roundtrip_stable(value):
                try:
                    data = json.dumps(value, ensure_ascii=False, default=str)
                    cached_mcp_store(
                        check["key"], ADAPTER_SERVER, tool_name, cargs, data, ttl=ttl
                    )
                except Exception as e:  # noqa: BLE001 — store must not break the tool
                    logger.warning("Cache store failed (tool unaffected): %s", e)
            _report_verdict(verdict)
            return value

        return wrapper

    return decorator


# ---- Namespace clear ------------------------------------------------------------


def invalidate_langfuse_cache() -> None:
    """Invalidate all ToolRecall entries cached under the langfuse namespace."""
    invalidate_mcp_server(ADAPTER_SERVER)
    logger.info("Cache CLEAR langfuse — all entries invalidated")

