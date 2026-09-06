"""TTL policy helpers: SWR tier classification, adaptive TTL, file-TTL resolution, fuzzy TTL.

Pure functions over stdlib types — no I/O, no dependencies. The cache layers in
cache.py call these to decide HOW to serve (fresh / stale-while-revalidate /
expired) and WHAT TTL to store (adaptive extension, per-file/per-command maps).

Design invariants:
- Serving is always exact-key: fuzzy/difflib matching only classifies TTL or
  cacheability, it never serves one entry under another command's key.
- All policies are opt-in; with default config every helper returns values that
  reproduce the pre-existing hard-TTL behavior.
"""

from __future__ import annotations

import fnmatch
import os
import time
from difflib import SequenceMatcher

# Expiry tiers returned by classify_expiry()
SWR_FRESH = "fresh"  # expires_at > now → serve normally
SWR_SERVE_STALE = "serve_stale"  # within SWR window → serve + revalidate in background
SWR_EXPIRED = "expired"  # beyond SWR window (or SWR off) → miss, re-execute


def classify_expiry(expires_at: float, now: float | None = None, swr: float = 0) -> str:
    """Classify a cache row's expiry against now and the SWR window.

    Args:
        expires_at: Row's absolute expiry timestamp (time.time() scale).
        now: Current time; defaults to time.time().
        swr: Stale-while-revalidate window in seconds (0 = off).

    Returns:
        SWR_FRESH, SWR_SERVE_STALE, or SWR_EXPIRED.

    Boundaries: ``expires_at > now`` is fresh; ``now - swr <= expires_at <= now``
    is serve-stale; anything older is expired.
    """
    if now is None:
        now = time.time()
    if expires_at > now:
        return SWR_FRESH
    if swr > 0 and (now - swr) <= expires_at:
        return SWR_SERVE_STALE
    return SWR_EXPIRED


def adaptive_ttl(base_ttl: float, hit_streak: int, factor: float, max_ttl: float) -> float:
    """Effective TTL for a store, given the entry's current hit streak.

    ttl_eff = min(base_ttl * factor**hit_streak, max_ttl). Never exceeds
    max_ttl, never grows below base_ttl, and factor <= 1 disables growth.

    Args:
        base_ttl: The configured/layer TTL in seconds.
        hit_streak: Consecutive unchanged-content hits (>= 0).
        factor: Multiplicative growth per streak (e.g. 2.0 doubles per streak).
        max_ttl: Hard cap in seconds.

    Returns:
        Effective TTL in seconds (float, integral value).
    """
    if base_ttl <= 0:
        return 0.0
    if hit_streak <= 0 or factor <= 1.0:
        return float(min(base_ttl, max_ttl))
    grown = base_ttl * (factor**hit_streak)
    return float(min(grown, max_ttl))


def resolve_file_ttl(path: str, cache_cfg: dict | None) -> int:
    """Resolve the trust-window TTL for a file read.

    Precedence: exact key in ``file_ttls`` → glob match (fnmatch on the
    expanded path) → ``file_ttl`` global → -1.

    Semantics (see cached_read):
        -1  always mtime-validate (default; current behavior)
         0  never cache this file
        >0  within N seconds of caching, serve WITHOUT mtime validation

    Args:
        path: Absolute filesystem path of the file being read.
        cache_cfg: The ``[cache]`` config section dict (or None).

    Returns:
        TTL in seconds as described above.
    """
    cfg = cache_cfg or {}
    file_ttls = cfg.get("file_ttls") or {}
    global_ttl = cfg.get("file_ttl", -1)
    if not isinstance(file_ttls, dict) or not file_ttls:
        try:
            return int(global_ttl)
        except (TypeError, ValueError):
            return -1

    expanded = os.path.abspath(os.path.expanduser(path))

    # 1. Exact match on the raw key or its ~-expanded form
    for key, ttl in file_ttls.items():
        if key == path or key == expanded:
            return _safe_ttl(ttl)
    for key, ttl in file_ttls.items():
        if key.startswith("~") and os.path.expanduser(key) == expanded:
            return _safe_ttl(ttl)

    # 2. Glob match — first (deterministic) hit wins over the global default
    for key, ttl in file_ttls.items():
        pattern = os.path.expanduser(key) if key.startswith("~") else key
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatch(expanded, pattern):
            return _safe_ttl(ttl)

    # 3. Global fallback
    return _safe_ttl(global_ttl)


def _safe_ttl(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def fuzzy_ttl_for(
    command: str, patterns: dict[str, float], threshold: float = 0.85
) -> tuple[float | None, bool]:
    """Fuzzy-match a command against TTL patterns (difflib, classification only).

    Used when a command matches no exact/prefix pattern: the most similar
    pattern at/above ``threshold`` lends its TTL. NEVER grants cache serving
    across different keys — callers must still store/serve under the real
    command's own hash.

    Args:
        command: The command string (should already be normalized).
        patterns: Mapping of pattern → TTL (e.g. terminal_ttls).
        threshold: Minimum SequenceMatcher ratio (0..1]; 1.0 disables fuzz.

    Returns:
        (ttl, matched): inherited TTL and whether a fuzzy match fired.
        For an EXACT pattern hit, returns (ttl, True) too — callers can use
        this as a fallback matcher; exact hit short-circuits scoring.
    """
    if not patterns:
        return None, False
    cmd = (command or "").strip()
    # Exact hit short-circuit (case-insensitive on the whole string; patterns
    # are user config and terminal commands are case-sensitive in practice,
    # so keep it simple: exact or nothing).
    if cmd in patterns:
        return float(patterns[cmd]), True
    if threshold >= 1.0 or not cmd:
        return None, False
    best_ratio = 0.0
    best_ttl: float | None = None
    for pattern, ttl in patterns.items():
        ratio = SequenceMatcher(None, cmd, pattern).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_ttl = float(ttl)
    if best_ratio >= threshold and best_ttl is not None:
        return best_ttl, True
    return None, False
