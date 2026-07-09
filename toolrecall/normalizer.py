"""Deterministic JSON normalization for cache key generation.

Ensures that semantically identical tool arguments produce the same
cache key regardless of JSON key ordering, whitespace, formatting, or
non-semantic fields (timestamps, session IDs, request IDs).

This is the core of Track 1 (Semantic Intent Caching) — it's pure stdlib,
zero dependencies, and makes the first cache hit broader without any
embedding model.

Usage:
    >>> from toolrecall.normalizer import normalize_tool_args
    >>> key = normalize_tool_args({"path": " /tmp/file ", "flags": ["-l", "-a"]})
    '{"flags":["-a","-l"],"path":"/tmp/file"}'
"""

import json
from typing import Any


# Keys that are non-semantic noise — stripping them broadens cache hits
# across different agent invocations, session IDs, and request tracing.
NON_SEMANTIC_KEYS = frozenset({
    "timestamp", "request_id", "session_id", "nonce", "trace_id",
    "span_id", "correlation_id", "_t", "_r",
})


def normalize_json(obj: Any) -> str:
    """Normalize any JSON-serializable object into a deterministic string.

    Rules:
    - Sorts dict keys alphabetically
    - Strips leading/trailing whitespace from string values
    - Sorts list elements of primitive types (str, int, float, bool, None)
    - Uses compact JSON output (no extra whitespace)

    >>> normalize_json({"b": 2, "a": 1})
    '{"a":1,"b":2}'
    >>> normalize_json({"name": "  hello  ", "tags": ["z", "a"]})
    '{"name":"hello","tags":["a","z"]}'
    """
    normalized = _normalize_value(obj)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _normalize_value(value: Any) -> Any:
    """Recursively normalize a JSON value for deterministic hashing."""
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in sorted(value.items())}
    elif isinstance(value, list):
        items = [_normalize_value(v) for v in value]
        # Sort lists of primitives so order doesn't matter
        if items and all(isinstance(x, (str, int, float, bool, type(None))) for x in items):
            try:
                items.sort(key=str)
            except TypeError:
                pass
        return items
    elif isinstance(value, str):
        return value.strip()
    return value


def normalize_tool_args(args: dict, strip_noise: bool = True) -> str:
    """Normalize tool call arguments into a deterministic cache key.

    Strips non-semantic fields (timestamps, session IDs, etc.) before
    normalization for broader cross-session cache hits.

    Args:
        args: The tool call arguments dict.
        strip_noise: If True, removes known non-semantic keys (default).

    Returns:
        Compact normalized JSON string suitable for hashing into a cache key.

    Example:
        >>> normalize_tool_args({"path": "/tmp/file", "flags": ["-l"]})
        '{"flags":["-l"],"path":"/tmp/file"}'
        >>> normalize_tool_args({"path": "/tmp/file", "timestamp": "12345"})
        '{"path":"/tmp/file"}'
    """
    if strip_noise:
        args = {k: v for k, v in args.items() if k not in NON_SEMANTIC_KEYS}
    return normalize_json(args)


def normalize_command(cmd: str) -> str:
    """Normalize a shell command string for cache keying.

    Strips leading/trailing whitespace, collapses multiple spaces,
    and lowercases the command name (but not arguments).

    >>> normalize_command("  LS   -la  ")
    'ls -la'
    >>> normalize_command("ECHO hello")
    'echo hello'
    """
    cmd = cmd.strip()
    parts = cmd.split()
    if parts:
        parts[0] = parts[0].lower()
    return " ".join(parts)