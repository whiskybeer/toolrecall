"""Recall tier — persist non-reproducible content out-of-band, restore on demand.

This is the complementary mechanism for the one content class ToolRecall
cannot serve losslessly today: content that lacks a deterministic re-fetch
key (web / API / ephemeral output). Such content currently *must* stay in
the agent's live context because re-fetching yields different bytes.

The recall tier lets the agent:
  1. ``recall.store(...)`` → persists the raw content out-of-band and returns
     a deterministic ``node_id`` (a tiny pointer to leave in context),
  2. ``recall.get(node_id)`` → restores the raw bytes on demand.

Everything reproducible (file / terminal / api / mcp / browser, keyed) keeps
the existing lossless byte-identical semantics — the lossy tier fires only
for the non-reproducible tail.

Zero runtime deps: pure stdlib + SQLite via ``toolrecall._db``.
"""

from __future__ import annotations

import time

from toolrecall._db import _db, _hash
from toolrecall.cache import _estimate_tokens

# Content classes with a stable re-fetch key are reproducible: re-fetching
# yields identical bytes, so the existing deterministic cache already covers
# them. Everything else is non-reproducible and needs the recall tier.
_REPRODUCIBLE_KINDS = frozenset({"file", "terminal", "script", "code", "api", "mcp", "browser"})


def reproducible(content_type: str, key: str) -> bool:
    """True if content can be deterministically re-fetched (byte-identical).

    A content type is reproducible only if it is a known keyed kind AND it
    carries a stable key (path / command hash / request hash). Reproducible
    content stays in the lossless cache; non-reproducible content is the
    recall tier's target.
    """
    if content_type not in _REPRODUCIBLE_KINDS:
        return False
    return bool(key)


def node_id(fingerprint: str) -> str:
    """Deterministic id derived from the fingerprint (not an RNG).

    Idempotent: re-storing the same fingerprint yields the same node_id, so
    a block persisted twice dedups to a single row.
    """
    return _hash(str(fingerprint))


def store(
    *,
    fingerprint: str,
    content: str,
    content_type: str,
    reproducible: bool,
    summary: str = "",
) -> str:
    """Persist a content block out-of-band and return its node_id pointer.

    Args:
        fingerprint: stable key identifying the block (drives node_id dedup).
        content: raw bytes to persist.
        content_type: one of file|terminal|script|code|api|mcp|browser|web|other.
        reproducible: whether this block is deterministically re-fetchable.
        summary: optional short semantic pointer left with the entry.

    Returns:
        The node_id to store in-context; later passed to :func:`get`.
    """
    nid = node_id(fingerprint)
    tokens = _estimate_tokens(content)
    with _db() as conn:
        conn.execute(
            "INSERT INTO recall_cache (node_id, fingerprint, content, content_type, "
            "reproducible, summary, tokens, cached_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET "
            "content=excluded.content, summary=excluded.summary, "
            "tokens=excluded.tokens, cached_at=excluded.cached_at",
            (
                nid,
                fingerprint,
                content,
                content_type,
                1 if reproducible else 0,
                summary,
                tokens,
                time.time(),
            ),
        )
    return nid


def get(node_id_: str) -> dict | None:
    """Restore a persisted block by node_id.

    Returns a dict with ``content``, ``summary``, ``content_type``,
    ``reproducible`` and ``tokens``; None if never stored.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT content, summary, content_type, reproducible, tokens "
            "FROM recall_cache WHERE node_id=?",
            (node_id_,),
        ).fetchone()
    if not row:
        return None
    return {
        "content": row[0],
        "summary": row[1],
        "content_type": row[2],
        "reproducible": bool(row[3]),
        "tokens": row[4],
    }


def stats() -> dict:
    """Aggregate recall-cache totals (entry count + persisted tokens)."""
    with _db() as conn:
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM recall_cache").fetchone()
    return {"total": int(row[0]), "tokens": int(row[1])}
