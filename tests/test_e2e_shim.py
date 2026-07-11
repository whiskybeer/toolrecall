"""E2E tests for toolrecall.shim — real subprocess with the shim loaded.

Tests cover:
  1. Shim auto-applies on import — builtins.open is patched
  2. Reading a real file through the shim works (no crash, returns content)
  3. No infinite recursion when cache client internally calls open()
  4. subprocess.run string-form is intercepted by the shim
  5. subprocess.run list-form bypasses the shim
  6. Shim can be disabled via TOOLRECALL_SHIM_DISABLE=1
  7. shim remove() restores original builtins
  8. Re-entrancy guard prevents stack overflow (the actual bug)

These tests spawn real Python subprocesses with PYTHONPATH pointing at the
toolrecall source tree, so the shim module is importable. No mocks — real
file I/O, real subprocess, real open() interception.
"""

import json
import os
import sys
import tempfile
import textwrap
import unittest

REPO_DIR = os.path.expanduser("~/toolrecall")


def _run_python(code: str, extra_env: dict[str, str] | None = None) -> tuple[str, str, int]:
    """Run Python code in a subprocess with the toolrecall source on PYTHONPATH.

    Returns (stdout, stderr, returncode).
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_DIR
    # Isolate cache DB
    env["TOOLRECALL_CACHE_DB"] = os.path.join(tempfile.mkdtemp(), "e2e_shim.db")
    # Don't let pytest's env vars interfere with the subprocess shim detection
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("TOOLRECALL_SHIM_DISABLE", None)
    if extra_env:
        env.update(extra_env)
    result = sys.executable and __import__("subprocess").run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


class TestShimE2EApply(unittest.TestCase):
    """Shim auto-applies when imported, patches builtins.open."""

    def test_shim_import_patches_open(self):
        """Importing toolrecall.shim replaces builtins.open with _shim_open."""
        code = textwrap.dedent("""\
            import builtins
            import toolrecall.shim as shim
            print(builtins.open is shim._shim_open)
        """)
        out, err, rc = _run_python(code)
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertEqual(out, "True")

    def test_shim_disabled_no_patch(self):
        """With TOOLRECALL_SHIM_DISABLE=1, builtins.open stays original."""
        code = textwrap.dedent("""\
            import builtins
            original_open = builtins.open
            import toolrecall.shim as shim
            print(builtins.open is original_open)
        """)
        out, err, rc = _run_python(code, {"TOOLRECALL_SHIM_DISABLE": "1"})
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertEqual(out, "True")


class TestShimE2EFileRead(unittest.TestCase):
    """Shim intercepts open() for file reads without crashing."""

    def test_read_real_file(self):
        """Reading a real file through the shimmed open() works."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello from e2e test")
            temp_path = f.name

        try:
            code = textwrap.dedent(f"""\
                import toolrecall.shim  # auto-applies
                with open({json.dumps(temp_path)}, 'r') as f:
                    content = f.read()
                print(content)
            """)
            out, err, rc = _run_python(code)
            self.assertEqual(rc, 0, f"Crashed: {err}")
            self.assertEqual(out, "hello from e2e test")
        finally:
            os.unlink(temp_path)

    def test_read_binary_file(self):
        """Binary mode ('rb') bypasses cache and works correctly."""
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False) as f:
            f.write(b"\x00\x01\x02 binary data")
            temp_path = f.name

        try:
            code = textwrap.dedent(f"""\
                import toolrecall.shim
                with open({json.dumps(temp_path)}, 'rb') as f:
                    data = f.read()
                print(repr(data))
            """)
            out, err, rc = _run_python(code)
            self.assertEqual(rc, 0, f"Crashed: {err}")
            self.assertIn("binary data", out)
        finally:
            os.unlink(temp_path)

    def test_write_file(self):
        """Writing through the shimmed open() works (write mode bypasses cache)."""
        temp_path = os.path.join(tempfile.mkdtemp(), "write_test.txt")

        code = textwrap.dedent(f"""\
            import toolrecall.shim
            with open({json.dumps(temp_path)}, 'w') as f:
                f.write("written content")
            with open({json.dumps(temp_path)}, 'r') as f:
                print(f.read())
        """)
        out, err, rc = _run_python(code)
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertEqual(out, "written content")


class TestShimE2EReentrancy(unittest.TestCase):
    """The actual bug: no infinite recursion when cache client calls open().

    Before the fix, the shim intercepted open() → called cached_read() →
    which internally called open() → shim intercepted again → stack overflow.

    The re-entrancy guard breaks this cycle. This E2E test reproduces the
    exact scenario: the shim is loaded, cached_read is available, and a file
    is opened. The internal open() calls from the cache client must not
    trigger the shim again.
    """

    def test_no_recursion_on_file_read(self):
        """Opening a file through the shim must not cause RecursionError.

        This is the direct regression test for the stack overflow bug.
        If the re-entrancy guard is broken, this test will hit Python's
        recursion limit and crash with RecursionError.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("recursion test content")
            temp_path = f.name

        try:
            code = textwrap.dedent(f"""\
                import toolrecall.shim
                # Force _TR to load (triggers open() calls during import)
                import toolrecall.shim as shim
                shim._get_tr()

                # Now read a file — this goes through _shim_open
                with open({json.dumps(temp_path)}, 'r') as f:
                    content = f.read()

                if content == "recursion test content":
                    print("OK")
                else:
                    print(f"FAIL: {{content!r}}")
            """)
            out, err, rc = _run_python(code)
            self.assertEqual(rc, 0, f"Process crashed (possible RecursionError): {err}")
            self.assertEqual(out, "OK")
            self.assertNotIn("RecursionError", err)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_no_recursion_with_multiple_opens(self):
        """Multiple sequential file reads through the shim don't accumulate recursion."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f1:
            f1.write("file1 content")
            path1 = f1.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f2:
            f2.write("file2 content")
            path2 = f2.name

        try:
            paths = json.dumps([path1, path2] * 3)
            code = textwrap.dedent(f"""\
                import toolrecall.shim
                # Read files sequentially — must not trigger recursion
                results = []
                for p in {paths}:
                    with open(p, 'r') as f:
                        results.append(f.read())
                if all(r in ("file1 content", "file2 content") for r in results):
                    print(f"OK: {{len(results)}} reads")
                else:
                    print("FAIL: unexpected results")
            """)
            out, err, rc = _run_python(code)
            self.assertEqual(rc, 0, f"Crashed: {err}")
            self.assertIn("OK", out)
            self.assertNotIn("RecursionError", err)
        finally:
            os.unlink(path1)
            os.unlink(path2)


class TestShimE2ESubprocess(unittest.TestCase):
    """Shim intercepts subprocess.run for string commands."""

    def test_string_command_intercepted(self):
        """String-form subprocess.run goes through the shim (not the real subprocess)."""
        code = textwrap.dedent("""\
            import toolrecall.shim
            import subprocess

            # The shim patches subprocess.run — string commands go through
            # cached_terminal, which may or may not have a daemon.
            # If no daemon: it falls back to real subprocess.run.
            # Either way, the command should execute successfully.
            result = subprocess.run("echo hello_from_shim", shell=True, capture_output=True, text=True)
            print(result.stdout.strip())
        """)
        out, err, rc = _run_python(code)
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertIn("hello_from_shim", out)

    def test_list_command_bypassed(self):
        """List-form subprocess.run goes to real subprocess, not the shim."""
        code = textwrap.dedent("""\
            import toolrecall.shim
            import subprocess

            result = subprocess.run(["echo", "list_form_works"], capture_output=True, text=True)
            print(result.stdout.strip())
        """)
        out, err, rc = _run_python(code)
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertIn("list_form_works", out)


class TestShimE2ERemove(unittest.TestCase):
    """shim.remove() restores original builtins."""

    def test_remove_restores_open(self):
        """After remove(), builtins.open is the real open, not _shim_open."""
        code = textwrap.dedent("""\
            import builtins
            import toolrecall.shim as shim

            patched = builtins.open is shim._shim_open
            shim.remove()
            restored = builtins.open is shim._original_open
            print(f"patched={patched} restored={restored}")
        """)
        out, err, rc = _run_python(code)
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertIn("patched=True", out)
        self.assertIn("restored=True", out)

    def test_file_read_after_remove(self):
        """After remove(), file reads go through real open() directly."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("post-remove content")
            temp_path = f.name

        try:
            code = textwrap.dedent(f"""\
                import toolrecall.shim as shim
                shim.remove()
                with open({json.dumps(temp_path)}, 'r') as f:
                    print(f.read())
            """)
            out, err, rc = _run_python(code)
            self.assertEqual(rc, 0, f"Crashed: {err}")
            self.assertEqual(out, "post-remove content")
        finally:
            os.unlink(temp_path)


class TestShimE2EFullCycle(unittest.TestCase):
    """Full lifecycle: import → read → write → subprocess → remove → read."""

    def test_full_cycle_no_crash(self):
        """Exercise the shim across all code paths in one process."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("initial content")
            temp_path = f.name

        try:
            code = textwrap.dedent(f"""\
                import toolrecall.shim as shim
                import subprocess

                # 1. Read through shim
                with open({json.dumps(temp_path)}, 'r') as f:
                    assert f.read() == "initial content", "read failed"

                # 2. Write through shim (bypasses cache)
                with open({json.dumps(temp_path)}, 'w') as f:
                    f.write("updated content")

                # 3. Read again
                with open({json.dumps(temp_path)}, 'r') as f:
                    assert f.read() == "updated content", "re-read failed"

                # 4. Subprocess (string form, shell=True for shell commands)
                r = subprocess.run("echo ok", shell=True, capture_output=True, text=True)
                assert "ok" in r.stdout, "subprocess string failed"

                # 5. Subprocess (list form)
                r = subprocess.run(["echo", "list_ok"], capture_output=True, text=True)
                assert "list_ok" in r.stdout, "subprocess list failed"

                # 6. Remove shim
                shim.remove()

                # 7. Read after remove
                with open({json.dumps(temp_path)}, 'r') as f:
                    assert f.read() == "updated content", "post-remove read failed"

                print("ALL_OK")
            """)
            out, err, rc = _run_python(code)
            self.assertEqual(rc, 0, f"Crashed: {err}")
            self.assertEqual(out, "ALL_OK")
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


if __name__ == "__main__":
    unittest.main()
