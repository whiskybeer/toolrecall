"""Optional storage backend: libSQL (libsql-experimental).

OPTIONAL in every sense:
- Selected only via [storage].backend = "libsql" (default is sqlite)
- Dependency installed only via pip install toolrecall[libsql]
- This module always imports cleanly WITHOUT the dependency -- only
  connect() raises, with an install hint, if it's missing
- Turso Cloud sync is a further opt-in on top (sync_enabled, default
  false) -- see sync_configured()

The wrapper classes adapt libSQL's interface to sqlite3's so the 38
call sites above the singleton never know which backend they got.
"""

try:
    import libsql_experimental as libsql
    _HAS_LIBSQL = True
except ImportError:
    libsql = None
    _HAS_LIBSQL = False

SUPPORTS_SYNC = True


def sync_configured(cfg) -> bool:
    """Full opt-in policy: explicit enable (default false) AND credentials."""
    return bool(
        cfg.libsql_sync_enabled
        and cfg.libsql_sync_url
        and cfg.libsql_sync_token
    )


def stats_info(cfg) -> dict:
    """Backend block for get_stats()."""
    return {
        "sync_enabled": cfg.libsql_sync_enabled,
        "sync_url": cfg.libsql_sync_url or "not configured",
        "sync_interval": cfg.libsql_sync_interval,
    }


def connect(cfg, db_path: str):
    """Open a libSQL connection (embedded replica iff sync is fully opted in)."""
    if not _HAS_LIBSQL:
        raise ImportError(
            "storage_backend='libsql' requires libsql-experimental.\n"
            "  Install: pip install toolrecall[libsql]"
        )
    if sync_configured(cfg):
        # Embedded replica: local file that can push/pull to Turso Cloud.
        conn = libsql.connect(
            db_path,
            sync_url=cfg.libsql_sync_url,
            auth_token=cfg.libsql_sync_token,
        )
    else:
        conn = libsql.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return _LibSQLConnection(conn)


class _LibSQLRow:
    """Thin wrapper: make libSQL cursor rows subscriptable by column name.

    sqlite3.Row supports both row[0] and row["colname"]. libSQL returns plain
    tuples. This wrapper uses cursor.description to map column names to indices,
    so callers accessing row["column_name"] work the same way.
    """

    __slots__ = ("_row", "_cols")

    def __init__(self, row: tuple, description: list):
        self._row = row
        self._cols = {d[0]: i for i, d in enumerate(description)} if description else {}

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._row[self._cols[key]]
        return self._row[key]

    def __len__(self):
        return len(self._row)

    def __repr__(self):
        return repr(self._row)


class _LibSQLConnection:
    """Wraps a libSQL Connection so execute() returns cursors with row-like results.

    libSQL's C extension types (Connection, Cursor) are immutable and cannot
    have attributes set. This proxy wraps execute() to return a cursor whose
    fetchone()/fetchall() return _LibSQLRow objects supporting both int and
    string key access (matching sqlite3.Row's interface).
    """

    __slots__ = ("_conn", "_changes")

    def __init__(self, conn):
        self._conn = conn
        self._changes = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    @property
    def total_changes(self) -> int:
        """Track INSERT/UPDATE/DELETE row counts (sqlite3.Connection compatibility)."""
        return self._changes

    def execute(self, sql, parameters=None):
        c = self._conn.execute(sql) if parameters is None else self._conn.execute(sql, parameters)
        # Track rowcount for total_changes compatibility (best-effort).
        # libSQL returns rowcount=1 for SELECT (unlike sqlite3 which
        # returns -1), so we must skip non-modifying statements.
        try:
            rc = c.rowcount
            if rc is not None and rc > 0:
                stmt = sql.lstrip().upper()
                # Only count actual DML, not reads or pragmas
                if stmt.startswith("INSERT") or stmt.startswith("UPDATE") or stmt.startswith("DELETE") or stmt.startswith("REPLACE"):
                    self._changes += rc
                elif rc == -1 and stmt.startswith("INSERT"):
                    self._changes += 1
        except Exception:
            pass
        return _LibSQLCursor(c)

    def executemany(self, sql, seq):
        c = self._conn.executemany(sql, seq)
        try:
            rc = c.rowcount
            if rc is not None and rc > 0:
                self._changes += rc
        except Exception:
            pass
        return _LibSQLCursor(c)

    def cursor(self):
        return _LibSQLCursor(self._conn.cursor())

    def close(self):
        self._conn.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def sync(self):
        """Push/pull the embedded replica against Turso Cloud.

        Only meaningful when the connection was opened with sync_url +
        auth_token; raises on a plain local connection.
        """
        return self._conn.sync()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Match sqlite3.Connection semantics: commit on success, rollback
        # on exception, and DO NOT close -- the connection stays usable.
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        except Exception:
            pass
        return False


class _LibSQLCursor:
    """Wraps a libSQL Cursor so fetchone/fetchall return _LibSQLRow objects."""

    __slots__ = ("_cursor", "_description")

    def __init__(self, cursor):
        self._cursor = cursor
        self._description = cursor.description

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    @property
    def description(self):
        return self._description

    def fetchone(self):
        row = self._cursor.fetchone()
        return _LibSQLRow(row, self._description) if row is not None else None

    def fetchmany(self, size: int = 1):
        rows = self._cursor.fetchmany(size)
        return [_LibSQLRow(r, self._description) for r in rows]

    def fetchall(self):
        return [_LibSQLRow(r, self._description) for r in self._cursor.fetchall()]

    def __iter__(self):
        return _LibSQLRowIter(self._cursor, self._description)


class _LibSQLRowIter:
    """Iterator for _LibSQLCursor -- makes `for row in cursor` work like sqlite3."""

    __slots__ = ("_cursor", "_description")

    def __init__(self, cursor, description):
        self._cursor = cursor
        self._description = description

    def __next__(self):
        row = self._cursor.fetchone()
        if row is None:
            raise StopIteration
        return _LibSQLRow(row, self._description)