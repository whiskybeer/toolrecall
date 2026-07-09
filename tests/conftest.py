"""pytest conftest — reset the singleton DB connection between test modules.

Each test module sets TOOLRECALL_CACHE_DB to its own temp DB path at module level.
Without this conftest, the _db_real singleton connection from module A remains
open, and module B inherits a connection pointing to A's DB path. This causes
write-cache, file-cache, and regression tests to fail in full-suite runs while
passing when run alone.

The fix: close the old connection, clear the singleton, and remove stale WAL/SHM
files before each test, so the next _db() call re-opens with whatever
TOOLRECALL_CACHE_DB is currently set.
"""
import os
import pytest
from toolrecall._db import _db_lock, _db_real
import toolrecall._db as _db_mod


@pytest.fixture(autouse=True)
def _reset_db_before_test():
    """Reset the singleton DB connection and remove stale WAL/SHM files."""
    _db_lock.acquire()
    try:
        if _db_real is not None:
            db_path = None
            try:
                cur = _db_real.execute("PRAGMA database_list")
                row = cur.fetchone()
                if row:
                    db_path = row[2]
            except Exception:
                pass
            try:
                _db_real.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            _db_real.close()
            # Remove stale WAL/SHM files if we know the path
            if db_path:
                for suffix in ("-wal", "-shm"):
                    p = db_path + suffix
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
        _db_mod._db_real = None
    finally:
        _db_lock.release()
    yield