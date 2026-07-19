"""Default storage backend: stdlib sqlite3. Always available, zero deps.

No sync capability — single-machine by design. See storage/libsql.py
for the optional multi-writer/sync backend.
"""

import sqlite3

SUPPORTS_SYNC = False


def connect(cfg, db_path: str) -> sqlite3.Connection:
    """Open the stdlib sqlite3 connection with ToolRecall's standard pragmas."""
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn
