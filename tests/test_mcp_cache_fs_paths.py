"""cached_read_file / cached_write_file / cached_patch_file must send ABSOLUTE paths.

The daemon opens a path against its OWN working directory and keys its cache on
the path *string* (see cache._resolve_cache_path), so a relative path used to
open whatever the daemon happened to have — and every caller using the same
relative name shared one cache entry across projects (reading NOTES.md in
project B returned project A's content).

The MCP cache-fs server runs as the agent's own subprocess, so it is the only
place that knows the caller's cwd. It must therefore resolve the path before
handing it to the daemon. Absolute paths are unaffected.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import toolrecall.mcp_cache_fs as mcp_fs


class _CapturedSend:
    """Stand-in for _daemon_send that records the payload it was handed."""

    def __init__(self, response=None):
        self.payload = {}
        self._response = response if response is not None else {"cached": False}

    def __call__(self, cmd):
        self.payload = dict(cmd)
        return self._response


class TestMcpCacheFsAbsolutePaths(unittest.TestCase):
    """A relative path must be resolved in-process before it reaches the daemon."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self.tmpdir = tempfile.mkdtemp()
        os.chdir(self.tmpdir)
        with open(os.path.join(self.tmpdir, "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("hello\n")
        self.expected = os.path.join(self.tmpdir, "notes.md")

    def tearDown(self):
        os.chdir(self._orig_cwd)

    def test_read_resolves_relative_path_before_sending(self):
        sent = _CapturedSend({"content": "hello\n", "cached": False})
        with patch.object(mcp_fs, "_daemon_send", sent):
            mcp_fs._cached_read("notes.md")
        self.assertEqual(sent.payload.get("path"), self.expected, sent.payload)

    def test_write_resolves_relative_path_before_sending(self):
        sent = _CapturedSend({"cached": False})
        with patch.object(mcp_fs, "_daemon_send", sent):
            mcp_fs._cached_write("notes.md", "new content")
        self.assertEqual(sent.payload.get("path"), self.expected, sent.payload)

    def test_patch_resolves_relative_path_before_sending(self):
        sent = _CapturedSend({"cached": False})
        with patch.object(mcp_fs, "_daemon_send", sent):
            mcp_fs._cached_patch("notes.md", "hello", "bye")
        self.assertEqual(sent.payload.get("path"), self.expected, sent.payload)

    def test_absolute_path_is_unchanged(self):
        """The resolution must be a no-op for a path that is already absolute."""
        sent = _CapturedSend({"content": "hello\n", "cached": False})
        with patch.object(mcp_fs, "_daemon_send", sent):
            mcp_fs._cached_read(self.expected)
        self.assertEqual(sent.payload.get("path"), self.expected, sent.payload)

    def test_abs_helper_expands_tilde(self):
        """~ must be expanded AND absolutised — the daemon cannot expand it."""
        resolved = mcp_fs._abs("~/notes.md")
        self.assertTrue(os.path.isabs(resolved), resolved)
        self.assertNotIn("~", resolved)


if __name__ == "__main__":
    unittest.main()
