"""Run memory-index tests in isolated subprocess to avoid Config singleton conflicts.

The toolrecall Config is a process-global singleton. Other tests initialize it
with default env vars before test_memory_index.py's TOOLRECALL_KNOWLEDGE_DB 
takes effect. This runner spawns a fresh Python process, ensuring the env var
is set before Config is first loaded anywhere.

Usage:
    python3 tests/run_memory_tests.py
    python3 -m pytest tests/ -k 'not memory'  # all other tests normally
"""

import subprocess
import sys
import os
import tempfile

def main():
    test_file = os.path.join(os.path.dirname(__file__), "test_memory_index.py")

    # SECURITY: use mkstemp, NOT the deprecated/insecure tempfile.mktemp
    # (py/insecure-temporary-file). mkstemp creates the file exclusively and
    # returns the fd, eliminating the TOCTOU race where an attacker could
    # pre-create a file at the mktemp name before we open it.
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="toolrecall_memory_")
    os.close(fd)

    env = os.environ.copy()
    env["TOOLRECALL_KNOWLEDGE_DB"] = db_path

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"],
            env=env,
            capture_output=True,
            text=True,
        )

        print(result.stdout)
        if result.stderr:
            print(result.stderr)

        sys.exit(result.returncode)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

if __name__ == "__main__":
    main()