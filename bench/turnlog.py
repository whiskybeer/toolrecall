"""turnlog.py — one row per agent turn, append-only to own benchmark DB."""

import sqlite3
import time
import uuid
import os

BENCH_DB = os.path.expanduser("~/.toolrecall/benchmark.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS turn_log (
    run_id                 TEXT    NOT NULL,
    arm                    TEXT    NOT NULL,
    workload_id            TEXT    NOT NULL,
    turn_index             INTEGER NOT NULL,
    ts                     REAL    NOT NULL,
    request_tokens         INTEGER,
    prompt_tokens          INTEGER,
    completion_tokens      INTEGER,
    cache_read_tokens      INTEGER DEFAULT 0,
    cache_write_tokens     INTEGER DEFAULT 0,
    ctx_dropped_tokens_cum INTEGER DEFAULT 0,
    tool_calls             INTEGER DEFAULT 0,
    tool_cache_hits        INTEGER DEFAULT 0,
    tool_cache_misses      INTEGER DEFAULT 0,
    tool_time_ms           REAL    DEFAULT 0,
    ttft_s                 REAL,
    api_latency_s          REAL,
    status                 TEXT,
    error                  TEXT,
    context_tracker_ok     INTEGER DEFAULT 1,
    PRIMARY KEY (run_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_turn_arm ON turn_log(arm, workload_id, turn_index);
CREATE TABLE IF NOT EXISTS probe_result (
    run_id       TEXT    NOT NULL,
    arm          TEXT    NOT NULL,
    probe_id     TEXT    NOT NULL,
    planted_turn INTEGER NOT NULL,
    asked_turn   INTEGER NOT NULL,
    lag          INTEGER NOT NULL,
    passed       INTEGER NOT NULL,
    answer       TEXT,
    PRIMARY KEY (run_id, probe_id, asked_turn)
);
"""


class TurnLogger:
    """One row per agent turn. Cheap, synchronous, append-only."""

    def __init__(self, arm: str, workload_id: str, db_path: str = BENCH_DB):
        self.run_id = str(uuid.uuid4())
        self.arm = arm
        self.workload_id = workload_id
        self.turn = 0
        self.con = sqlite3.connect(db_path, timeout=30)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(_SCHEMA_SQL)

    def log(self, **kw) -> int:
        self.turn += 1
        cols = dict(
            run_id=self.run_id,
            arm=self.arm,
            workload_id=self.workload_id,
            turn_index=self.turn,
            ts=time.time(),
            status="ok",
        )
        cols.update(kw)
        keys = ",".join(cols)
        marks = ",".join("?" * len(cols))
        self.con.execute(
            f"INSERT OR REPLACE INTO turn_log ({keys}) VALUES ({marks})",
            tuple(cols.values()),
        )
        self.con.commit()
        return self.turn

    def close(self):
        self.con.close()