# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Cross-agent failure ledger — capture, normalize, and share failure signatures.

This module provides the persistent Layer-1 failure ledger for ToolRecall:
recording recurring tool execution failures across agents, normalizing error
messages into stable content-addressed signatures (with secret redaction,
path normalization, and volatile token stripping), and providing full-text
and prefix search over known failure signatures and resolutions.

Zero runtime dependencies: stdlib only + SQLite via ``toolrecall._db``.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Any

from toolrecall._db import _db
from toolrecall.keys import content_digest

__all__ = [
    "normalize_error_signature",
    "record_failure",
    "search_failures",
    "resolve_failure",
    "failure_stats",
]

# Patterns for secret redaction
_SECRET_PATTERNS = [
    # Bearer creds (e.g. Bearer sk-..., Bearer eyJ...)
    re.compile(r"(?i)\bBearer\s+[^\s\"'`;,]+"),
    # OpenAI, Anthropic, or general API keys starting with sk-
    re.compile(r"\bsk-[a-zA-Z0-9_\-]{8,}"),
    # GitHub personal access or OAuth creds
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}"),
    # AWS Access Key IDs
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # AWS Secret Keys
    re.compile(r"(?i)\b(?:aws_secret_access_key|aws_session_token)\s*[:=]\s*[^\s\"'`;,]+"),
    # PEM Private Key Blocks
    re.compile(r"-----BEGIN [A-Z0-9_\- ]+KEY-----[^-]*-----END [A-Z0-9_\- ]+KEY-----", re.DOTALL),
    re.compile(r"-----BEGIN [A-Z0-9_\- ]+KEY-----"),
    re.compile(r"-----END [A-Z0-9_\- ]+KEY-----"),
    # Passwords or creds in URLs (e.g. https://user:pass@host)
    re.compile(r"://([^:\s]+):([^@\s]+)@"),
    # Generic key/secret assignments
    re.compile(
        r"(?i)\b((?:api[_-]?key|secret|password|passwd|auth_token)\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"'`;,]+)"
    ),
]

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_UNIX_TS_RE = re.compile(r"\b1[5-9]\d{8}(?:\.\d+)?\b")
_UNIX_TS_MS_RE = re.compile(r"\b1[5-9]\d{11}\b")
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_LINE_NUM_RE = re.compile(r"(?i)\bline\s+\d+\b")
_COLON_NUM_RE = re.compile(r":\d+\b")
_HOME_DIR_RE = re.compile(r"(?:/home|/Users)/[^/\s\"'`;,]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _redact_secrets(text: str) -> str:
    """Scrub sensitive credentials (creds, API keys, private keys) from text."""
    if not isinstance(text, str):
        return ""
    for pat in _SECRET_PATTERNS:
        if pat.pattern.startswith("://"):
            text = pat.sub(r"://\1:<redacted>@", text)
        elif "api[_-]?key" in pat.pattern:
            text = pat.sub(r"\1<redacted>", text)
        else:
            text = pat.sub("<redacted>", text)
    return text


def normalize_error_signature(tool: str, error_text: str) -> tuple[str, str]:
    """Normalize tool error text into a deterministic, stable signature.

    Strips volatile elements (ANSI sequences, secrets, home paths, UUIDs,
    timestamps, hex memory addresses, line numbers) and whitespace, capping
    the normalized text at 512 characters.

    Returns:
        tuple[str, str]: (signature_hash, normalized_text)
    """
    tool_str = str(tool or "")
    text = str(error_text or "")

    # 1. Strip ANSI escape sequences
    text = _ANSI_RE.sub("", text)

    # 2. Redact credentials/secrets
    text = _redact_secrets(text)

    # 3. User home directory -> ~
    home = os.path.expanduser("~")
    if home and home != "/":
        text = text.replace(home, "~")
    text = _HOME_DIR_RE.sub("~", text)

    # 4. UUIDs -> <uuid>
    text = _UUID_RE.sub("<uuid>", text)

    # 5. Timestamps (ISO and Unix epoch) -> <ts>
    text = _ISO_TS_RE.sub("<ts>", text)
    text = _UNIX_TS_RE.sub("<ts>", text)
    text = _UNIX_TS_MS_RE.sub("<ts>", text)

    # 6. Hex addresses -> 0x<hex>
    text = _HEX_ADDR_RE.sub("0x<hex>", text)

    # 7. Line numbers -> :N
    text = _LINE_NUM_RE.sub(":N", text)
    text = _COLON_NUM_RE.sub(":N", text)

    # 8. Collapse whitespace and truncate to 512 chars
    norm_text = _WHITESPACE_RE.sub(" ", text).strip()[:512]

    # 9. Compute deterministic signature hash via content_digest
    sig_hash = content_digest(f"{tool_str}\x00{norm_text}")[:32]
    return (sig_hash, norm_text)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure failure_ledger table and FTS5 virtual table exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS failure_ledger (
            signature_hash TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            normalized_error TEXT NOT NULL,
            agent_id TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 1,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            resolution TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_failure_ledger_tool ON failure_ledger(tool)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_failure_ledger_count ON failure_ledger(count DESC, last_seen DESC)"
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS failure_ledger_fts USING fts5(
                signature_hash UNINDEXED,
                tool,
                normalized_error,
                resolution
            )
            """
        )
    except sqlite3.OperationalError as e:
        logging.getLogger(__name__).debug("fts5 init skipped: %s", e)


def _has_fts5(conn: sqlite3.Connection) -> bool:
    """Check if failure_ledger_fts virtual table exists and is operational."""
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='failure_ledger_fts'"
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """Convert a failure_ledger row tuple into a structured dictionary."""
    return {
        "signature_hash": row[0],
        "tool": row[1],
        "normalized_error": row[2],
        "agent_id": row[3],
        "count": row[4],
        "first_seen": row[5],
        "last_seen": row[6],
        "resolution": row[7],
    }


def record_failure(tool: str, error_text: str, agent_id: str = "") -> dict[str, Any]:
    """Record a tool failure, upserting into the ledger and updating FTS index.

    Upserts into failure_ledger (incrementing count, updating last_seen, keeping
    first_seen). Syncs failure_ledger_fts when FTS5 is available.

    Returns:
        dict: {"signature_hash", "tool", "normalized_error", "count"}
    """
    now = time.time()
    sig_hash, norm_text = normalize_error_signature(tool, error_text)

    with _db() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO failure_ledger (
                signature_hash, tool, normalized_error, agent_id, count, first_seen, last_seen, resolution
            ) VALUES (?, ?, ?, ?, 1, ?, ?, '')
            ON CONFLICT(signature_hash) DO UPDATE SET
                count = failure_ledger.count + 1,
                last_seen = excluded.last_seen,
                agent_id = CASE WHEN excluded.agent_id != '' THEN excluded.agent_id ELSE failure_ledger.agent_id END
            """,
            (sig_hash, tool, norm_text, agent_id, now, now),
        )
        row = conn.execute(
            "SELECT count, resolution FROM failure_ledger WHERE signature_hash = ?",
            (sig_hash,),
        ).fetchone()
        count = row[0] if row else 1
        resolution = row[1] if row else ""

        if _has_fts5(conn):
            try:
                conn.execute(
                    "DELETE FROM failure_ledger_fts WHERE signature_hash = ?",
                    (sig_hash,),
                )
                conn.execute(
                    "INSERT INTO failure_ledger_fts (signature_hash, tool, normalized_error, resolution) VALUES (?, ?, ?, ?)",
                    (sig_hash, tool, norm_text, resolution),
                )
            except sqlite3.OperationalError as e:
                logging.getLogger(__name__).debug("fts5 sync skipped: %s", e)

    return {
        "signature_hash": sig_hash,
        "tool": tool,
        "normalized_error": norm_text,
        "count": count,
    }


def search_failures(query: str, tool: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """Search recorded failure signatures by query and/or tool filter.

    Searches failure_ledger_fts via MATCH (sanitizing query tokens safely) with
    fallback to parameterized LIKE on failure_ledger. Optionally filters by tool.

    Returns:
        list[dict]: List of records sorted by count DESC, last_seen DESC.
    """
    with _db() as conn:
        _ensure_schema(conn)

        clean_tokens = re.findall(r"\w+", query)
        if _has_fts5(conn) and clean_tokens:
            fts_expr = " ".join(f'"{tok}"*' for tok in clean_tokens)
            try:
                cur = conn.execute(
                    """
                    SELECT f.signature_hash, f.tool, f.normalized_error, f.agent_id, f.count, f.first_seen, f.last_seen, f.resolution
                    FROM failure_ledger_fts fts
                    JOIN failure_ledger f ON f.signature_hash = fts.signature_hash
                    WHERE failure_ledger_fts MATCH ?
                      AND (? = '' OR f.tool = ?)
                    ORDER BY f.count DESC, f.last_seen DESC
                    LIMIT ?
                    """,
                    (fts_expr, tool, tool, limit),
                )
                rows = cur.fetchall()
                if rows:
                    return [_row_to_dict(r) for r in rows]
            except sqlite3.OperationalError as e:
                logging.getLogger(__name__).debug("fts5 sync skipped: %s", e)

        # Fallback to parameterized LIKE on failure_ledger
        like_param = f"%{query.strip()}%"
        cur = conn.execute(
            """
            SELECT signature_hash, tool, normalized_error, agent_id, count, first_seen, last_seen, resolution
            FROM failure_ledger
            WHERE (? = '' OR normalized_error LIKE ? OR resolution LIKE ? OR signature_hash LIKE ?)
              AND (? = '' OR tool = ?)
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (query.strip(), like_param, like_param, like_param, tool, tool, limit),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def resolve_failure(signature_hash: str, resolution: str) -> bool:
    """Record or update a resolution for a recurring failure signature.

    Updates resolution in failure_ledger and failure_ledger_fts.
    Returns True if row existed, False otherwise.
    """
    with _db() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "UPDATE failure_ledger SET resolution = ? WHERE signature_hash = ?",
            (resolution, signature_hash),
        )
        if cur.rowcount == 0:
            return False

        if _has_fts5(conn):
            try:
                row = conn.execute(
                    "SELECT tool, normalized_error FROM failure_ledger WHERE signature_hash = ?",
                    (signature_hash,),
                ).fetchone()
                if row:
                    conn.execute(
                        "DELETE FROM failure_ledger_fts WHERE signature_hash = ?",
                        (signature_hash,),
                    )
                    conn.execute(
                        "INSERT INTO failure_ledger_fts (signature_hash, tool, normalized_error, resolution) VALUES (?, ?, ?, ?)",
                        (signature_hash, row[0], row[1], resolution),
                    )
            except sqlite3.OperationalError as e:
                logging.getLogger(__name__).debug("fts5 sync skipped: %s", e)
        return True


def failure_stats() -> dict[str, int]:
    """Return aggregated failure ledger statistics.

    Returns:
        dict: {"unique_signatures": int, "total_failures": int, "resolved_signatures": int}
        (returns zeros if table doesn't exist yet).
    """
    with _db() as conn:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='failure_ledger'"
        )
        if not cur.fetchone():
            return {
                "unique_signatures": 0,
                "total_failures": 0,
                "resolved_signatures": 0,
            }

        row = conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(count), 0),
                COALESCE(SUM(CASE WHEN resolution != '' THEN 1 ELSE 0 END), 0)
            FROM failure_ledger
            """
        ).fetchone()
        if not row:
            return {
                "unique_signatures": 0,
                "total_failures": 0,
                "resolved_signatures": 0,
            }
        return {
            "unique_signatures": int(row[0]),
            "total_failures": int(row[1]),
            "resolved_signatures": int(row[2]),
        }
