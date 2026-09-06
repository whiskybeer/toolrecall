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
import os
from typing import Any


# Keys that are non-semantic noise — stripping them broadens cache hits
# across different agent invocations, session IDs, and request tracing.
NON_SEMANTIC_KEYS = frozenset(
    {
        "timestamp",
        "request_id",
        "session_id",
        "nonce",
        "trace_id",
        "span_id",
        "correlation_id",
        "_t",
        "_r",
    }
)

# Config values — set by normalize_tool_args() or overridable defaults.
# These are module-level so they can be set once per process but also
# overridden per-call.
_SORT_LISTS = True
_STRIP_STRINGS = True


def configure(*, sort_lists: bool | None = None, strip_strings: bool | None = None):
    """Set normalization defaults for the module.

    Args:
        sort_lists: If True, sort lists of primitive types (str, int, float, bool, None).
            Default True. Set False to preserve argument order (e.g. for positional args).
        strip_strings: If True, strip leading/trailing whitespace from string values.
            Default True. Set False to preserve significant whitespace.
    """
    global _SORT_LISTS, _STRIP_STRINGS
    if sort_lists is not None:
        _SORT_LISTS = sort_lists
    if strip_strings is not None:
        _STRIP_STRINGS = strip_strings


def normalize_json(
    obj: Any, *, sort_lists: bool | None = None, strip_strings: bool | None = None
) -> str:
    """Normalize any JSON-serializable object into a deterministic string.

    Args:
        obj: JSON-serializable object to normalize.
        sort_lists: Override the module default for list sorting.
        strip_strings: Override the module default for string stripping.

    Rules:
    - Sorts dict keys alphabetically
    - Strips leading/trailing whitespace from string values (configurable)
    - Sorts list elements of primitive types (str, int, float, bool, None) (configurable)
    - Uses compact JSON output (no extra whitespace)

    >>> normalize_json({"b": 2, "a": 1})
    '{"a":1,"b":2}'
    >>> normalize_json({"name": "  hello  ", "tags": ["z", "a"]})
    '{"name":"hello","tags":["a","z"]}'
    """
    _sort = sort_lists if sort_lists is not None else _SORT_LISTS
    _strip = strip_strings if strip_strings is not None else _STRIP_STRINGS
    normalized = _normalize_value(obj, sort_lists=_sort, strip_strings=_strip)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _normalize_value(value: Any, *, sort_lists: bool, strip_strings: bool) -> Any:
    """Recursively normalize a JSON value for deterministic hashing."""
    if isinstance(value, dict):
        return {
            k: _normalize_value(v, sort_lists=sort_lists, strip_strings=strip_strings)
            for k, v in sorted(value.items())
        }
    elif isinstance(value, list):
        items = [
            _normalize_value(v, sort_lists=sort_lists, strip_strings=strip_strings) for v in value
        ]
        # Sort lists of primitives so order doesn't matter
        # (configurable — set sort_lists=False to preserve argument order)
        if (
            sort_lists
            and items
            and all(isinstance(x, (str, int, float, bool, type(None))) for x in items)
        ):
            try:
                items.sort(key=str)
            except TypeError:
                pass
        return items
    elif isinstance(value, str):
        return value.strip() if strip_strings else value
    return value


def normalize_tool_args(
    args: dict,
    strip_noise: bool = True,
    *,
    sort_lists: bool | None = None,
    strip_strings: bool | None = None,
) -> str:
    """Normalize tool call arguments into a deterministic cache key.

    Strips non-semantic fields (timestamps, session IDs, etc.) before
    normalization for broader cross-session cache hits.

    Args:
        args: The tool call arguments dict.
        strip_noise: If True, removes known non-semantic keys (default).
        sort_lists: Override the module default for list sorting.
        strip_strings: Override the module default for string stripping.

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
    return normalize_json(args, sort_lists=sort_lists, strip_strings=strip_strings)


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


def canonical_command(cmd: str) -> str:
    """Semantic normalization for cache keys: flag-cluster sorting, path and
    quote canonicalization on top of :func:`normalize_command`.

    Semantically identical commands produce the SAME key, so ``ls -al`` and
    ``ls -la`` share one cache entry. Serving stays exact-key — identical
    keys are the only way entries are shared (no cross-key poisoning).

    Conservative by design:
    - Only contiguous flag clusters (tokens starting with ``-``) sort; a
      cluster's flags are assumed order-independent. Anything else keeps
      positional order (``head -n 5`` ≠ ``head -5``).
    - ``~`` expands, duplicate slashes and trailing slashes on path-like
      tokens collapse.
    - Shell metacharacters abort canonicalization (compound commands are
      never cacheable anyway) — returns the base normalization.
    - shlex failures fall back to the base normalization, never raise.
    """
    base = normalize_command(cmd)
    if not base:
        return base

    # Compound / redirected commands: never cacheable, don't pretend.
    if any(ch in base for ch in (";", "&", "|", "`", "$(", ">", "<", "\n")):
        return base

    try:
        import shlex

        parts = shlex.split(base)
    except ValueError:
        return base
    if not parts:
        return base

    home = os.path.expanduser("~")

    def _canon_token(tok: str, is_first: bool) -> str:
        if is_first:
            return tok.lower()
        if tok.startswith("-"):
            return tok
        # Path-like canonicalization (skip bare "-" style tokens)
        if "/" in tok:
            tok = tok.replace("~", home) if tok.startswith("~") else tok
            while "//" in tok:
                tok = tok.replace("//", "/")
            if len(tok) > 1:
                tok = tok.rstrip("/")
            return tok or "/"
        return tok

    parts = [_canon_token(p, i == 0) for i, p in enumerate(parts)]

    # Sort each contiguous run of flag tokens (order-independent within a
    # run). Combined short flags split into chars first: '-al' ≡ '-la' ≡
    # '-a -l'. Long flags ('--oneline') and valued flags ('-n', '-5') stay
    # whole — only pure short-flag bundles decompose.
    def _flag_sort_key(tok: str):
        if (
            tok.startswith("--")
            or not tok.startswith("-")
            or len(tok) <= 2
            or not tok[1:].isalnum()
        ):
            return (1, tok)  # long flags / valued: sort as whole tokens
        return (0, "".join(sorted(tok[1:])))  # '-al' → 'al' sorted

    def _flag_norm(tok: str) -> str:
        if tok.startswith("--") or len(tok) <= 2 or not tok[1:].isalnum():
            return tok
        return "-" + "".join(sorted(tok[1:]))

    out: list[str] = []
    cluster: list[str] = []
    for p in parts:
        if p.startswith("-") and p != "-":
            cluster.append(p)
        else:
            if cluster:
                out.extend(sorted((_flag_norm(c) for c in cluster)))
                cluster = []
            out.append(p)
    if cluster:
        merged: list[str] = []
        for c in sorted(_flag_norm(c) for c in cluster):
            # Merge pure short flags into one bundle: '-a','-l' → '-al'
            if (
                len(c) == 2
                and c.startswith("-")
                and c[1].isalnum()
                and merged
                and len(merged[-1]) >= 2
                and merged[-1].startswith("-")
                and not merged[-1].startswith("--")
                and merged[-1][1:].isalnum()
            ):
                merged[-1] = "-" + "".join(sorted(merged[-1][1:] + c[1]))
            else:
                merged.append(c)
        out.extend(merged)

    return " ".join(out)
