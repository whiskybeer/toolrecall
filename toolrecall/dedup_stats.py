# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Per-tool attribution of request-dedup savings.

The forward proxy records one ``dedup_tokens`` figure per billed request (the
usage CSV). That answers "how much did dedup remove" but not "from WHICH tool's
output" — so a question like "does skills_list get stubbed?" had no answer.
This module keeps a cumulative per-tool ledger next to the cache stats.

Same estimator as the usage CSV (chars // 4), so the per-tool sums reconcile
with ``dedup_tokens`` instead of being a second, incompatible number.

The table is created lazily here rather than in ``cache.SCHEMA``: it is written
only by the proxy, only when a request actually had something stubbed (a small
fraction of requests), and keeping it self-contained means it can never block
the main schema migration.

Never raises. A ledger write that fails is logged, not swallowed — "the counter
is broken" and "nothing was stubbed" must not look the same.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

log = logging.getLogger("toolrecall.dedup_stats")

_TABLE = """
CREATE TABLE IF NOT EXISTS dedup_by_tool (
    tool        TEXT PRIMARY KEY,
    blocks      INTEGER NOT NULL DEFAULT 0,
    chars_saved INTEGER NOT NULL DEFAULT 0,
    requests    INTEGER NOT NULL DEFAULT 0,
    first_at    REAL,
    last_at     REAL
)
"""


def record(by_tool: Dict[str, Dict[str, int]]) -> None:
    """Add one request's per-tool dedup savings to the ledger."""
    if not by_tool:
        return
    try:
        from toolrecall._db import _db

        now = time.time()
        with _db() as conn:
            conn.execute(_TABLE)
            for tool, slot in by_tool.items():
                conn.execute(
                    "INSERT INTO dedup_by_tool (tool, blocks, chars_saved, requests, first_at, last_at)"
                    " VALUES (?, ?, ?, 1, ?, ?)"
                    " ON CONFLICT(tool) DO UPDATE SET"
                    "   blocks = blocks + excluded.blocks,"
                    "   chars_saved = chars_saved + excluded.chars_saved,"
                    "   requests = requests + 1,"
                    "   last_at = excluded.last_at",
                    (
                        str(tool),
                        int(slot.get("blocks", 0)),
                        int(slot.get("chars_saved", 0)),
                        now,
                        now,
                    ),
                )
    except Exception as e:
        log.warning("dedup_by_tool ledger write failed: %s", e)


def per_tool() -> List[Dict[str, Any]]:
    """Ledger rows, largest saving first. ``tokens_saved`` = chars // 4."""
    try:
        from toolrecall._db import _db

        with _db() as conn:
            conn.execute(_TABLE)
            rows = conn.execute(
                "SELECT tool, blocks, chars_saved, requests, first_at, last_at"
                " FROM dedup_by_tool ORDER BY chars_saved DESC, tool"
            ).fetchall()
    except Exception as e:
        log.warning("dedup_by_tool ledger read failed: %s", e)
        return []
    return [
        {
            "tool": r[0],
            "blocks": int(r[1] or 0),
            "chars_saved": int(r[2] or 0),
            "tokens_saved": int(r[2] or 0) // 4,
            "requests": int(r[3] or 0),
            "first_at": r[4],
            "last_at": r[5],
        }
        for r in rows
    ]
