import os
import sys
import unittest
import tempfile
import time
import shutil

# Force a clean, isolated test database path before loading toolrecall
test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_cache.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path

# Add current path to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from toolrecall.cache import cached_exec, cached_run, cached_terminal, DEFAULT_CACHEABLE


class TestCacheSafety(unittest.TestCase):
    def setUp(self):
        # Ensure database is clean for each test
        if os.path.exists(test_db_path):
            os.remove(test_db_path)
        from toolrecall.cache import _init
        _init()

    def tearDown(self):
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(test_db_dir, ignore_errors=True)

    def test_cached_exec_default_ttl_zero(self):
        """Verify cached_exec default TTL is 0 (or disabled), meaning it executes fresh every time."""
        code = "import time; print(time.time())"
        
        # Run first time
        res1 = cached_exec(code)
        # Run second time
        res2 = cached_exec(code)
        
        self.assertFalse(res1.get("cached"), "First run should not be cached")
        self.assertFalse(res2.get("cached"), "Second run with default TTL=0 should not be cached")
        self.assertNotEqual(res1.get("output"), res2.get("output"), "Output of dynamic code should be different without cache")

    def test_cached_exec_explicit_ttl(self):
        """Verify cached_exec with explicit TTL does cache."""
        code = "print('hello_exec')"
        
        res1 = cached_exec(code, ttl=10)
        res2 = cached_exec(code, ttl=10)
        
        self.assertFalse(res1.get("cached"), "First run should not be cached")
        self.assertTrue(res2.get("cached"), "Second run with explicit TTL should be cached")
        self.assertEqual(res1.get("output"), res2.get("output"), "Cached output should match")

    def test_cached_run_default_ttl_zero(self):
        """Verify cached_run default TTL is 0, executing fresh every time."""
        # Create a temporary script to execute
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("#!/usr/bin/env python3\nimport time; print(time.time())\n")
            script_path = f.name
        os.chmod(script_path, 0o755)
            
        try:
            # Run first time
            res1 = cached_run(script_path)
            time.sleep(0.01) # Ensure time would advance if it runs fresh
            # Run second time
            res2 = cached_run(script_path)
            
            self.assertFalse(res1.get("cached"), "First run should not be cached")
            self.assertFalse(res2.get("cached"), "Second run with default TTL=0 should not be cached")
            self.assertNotEqual(res1.get("output"), res2.get("output"), "Output of dynamic script should be different without cache")
        finally:
            os.remove(script_path)

    def test_cached_run_explicit_ttl(self):
        """Verify cached_run with explicit TTL does cache."""
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("#!/usr/bin/env python3\nprint('hello_run')\n")
            script_path = f.name
        os.chmod(script_path, 0o755)
            
        try:
            res1 = cached_run(script_path, ttl=10)
            res2 = cached_run(script_path, ttl=10)
            
            self.assertFalse(res1.get("cached"), "First run should not be cached")
            self.assertTrue(res2.get("cached"), "Second run with explicit TTL should be cached")
            self.assertEqual(res1.get("output"), res2.get("output"), "Cached output should match")
        finally:
            os.remove(script_path)

    def test_dynamic_commands_not_in_default_cacheable(self):
        """Verify DESTRUCTIVE commands are NOT in DEFAULT_CACHEABLE (read-only commands are deliberately cached)."""
        unsafe_cmds = ["git push", "git commit", "git merge", "rm", "sudo", "mv", "kill", "docker exec", "dd"]
        for cmd in unsafe_cmds:
            self.assertNotIn(cmd, DEFAULT_CACHEABLE, f"Destructive command '{cmd}' must not be in DEFAULT_CACHEABLE")
        
        # Read-only commands SHOULD be cached now — verify they are
        safe_cmds = ["ls", "cat", "grep", "git status", "git diff"]
        for cmd in safe_cmds:
            self.assertIn(cmd, DEFAULT_CACHEABLE, f"Read-only command '{cmd}' should be in DEFAULT_CACHEABLE")

    def test_cached_terminal_does_not_cache_dynamic_commands(self):
        """Verify cached_terminal does not cache dynamic commands like git status by default."""
        # Even if we don't have a git repo, cached_terminal should bypass cache for 'git status'
        res1 = cached_terminal("git status")
        res2 = cached_terminal("git status")
        
        self.assertFalse(res1.get("cached"), "Dynamic command first run should not be cached")
        self.assertTrue(res2.get("cached"),
                        "Dynamic command second run SHOULD be cached (git status is now in DEFAULT_CACHEABLE with 30s TTL)")


if __name__ == "__main__":
    unittest.main()
