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
    # No auto-checkpoint on commit. SQLite's default (wal_autocheckpoint=1000
    # pages) folds a checkpoint into whichever write happens to fill the WAL, so
    # one cache write can inherit a page flush + fsync of a large DB — while
    # holding the caller's DB lock, and every other request queues behind it.
    # Measured (430 KB body, copy of the live 1.19 GB DB): commit max 114.8 ms at
    # the default vs 4.7 ms at 0 (p50 2.19 vs 1.82 ms); after the expired-row
    # reclaim, max 74.7 vs 13.1 ms. Safe because the WAL is bounded elsewhere:
    # the daemon checkpoints it every 60 s (daemon._run_periodic_checkpoint,
    # truncate=True) and on shutdown, and SQLite checkpoints when the last
    # connection closes, so short-lived CLI connections still leave no WAL.
    conn.execute("PRAGMA wal_autocheckpoint=0;")
    conn.row_factory = sqlite3.Row
    return conn
