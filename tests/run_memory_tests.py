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

def main():
    test_file = os.path.join(os.path.dirname(__file__), "test_memory_index.py")
    
    env = os.environ.copy()
    import tempfile
    env["TOOLRECALL_KNOWLEDGE_DB"] = tempfile.mktemp(suffix=".db", prefix="toolrecall_memory_")
    
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

if __name__ == "__main__":
    main()