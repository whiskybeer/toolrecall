# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests: Incremental Reads (P1) — verify_append, IncrementalReadSession, and MCPBridge integration."""

from __future__ import annotations

import os
import unittest

from toolrecall.incremental import IncrementalReadSession, verify_append
from toolrecall.mcp_bridge import MCPBridge


class TestVerifyAppend(unittest.TestCase):
    def test_verify_append_exact(self):
        prev = b"log line 1\nlog line 2\n"
        curr = b"log line 1\nlog line 2\nlog line 3\n"
        res = verify_append(prev, curr)
        self.assertTrue(res["append_only"])
        self.assertEqual(res["mode"], "exact")
        self.assertEqual(res["unchanged_prefix_bytes"], len(prev))
        self.assertEqual(res["tail_bytes"], b"log line 3\n")
        self.assertEqual(res["total_bytes"], len(curr))

    def test_verify_append_json_tail(self):
        prev = b'{"items": [1, 2]}\n'
        curr = b'{"items": [1, 2, 3]}\n'
        res = verify_append(prev, curr, allow_closing_trim=32)
        self.assertTrue(res["append_only"])
        self.assertEqual(res["mode"], "json_tail")
        self.assertEqual(res["tail_bytes"], b", 3]}\n")
        self.assertEqual(res["unchanged_prefix_bytes"] + res["replaced_suffix_bytes"], len(prev))
        self.assertEqual(res["unchanged_prefix_bytes"] + len(res["tail_bytes"]), len(curr))

    def test_verify_append_json_tail_whitespace_and_commas(self):
        prev = b'[\n  {"id": 1}\n]\n'
        curr = b'[\n  {"id": 1},\n  {"id": 2}\n]\n'
        res = verify_append(prev, curr, allow_closing_trim=32)
        self.assertTrue(res["append_only"])
        self.assertEqual(res["mode"], "json_tail")
        self.assertIn(b'{"id": 2}', res["tail_bytes"])

    def test_verify_append_rejects(self):
        # Empty prev
        self.assertFalse(verify_append(b"", b"hello")["append_only"])
        # Curr shorter
        self.assertFalse(verify_append(b"hello world", b"hello")["append_only"])
        # Curr equal length
        self.assertFalse(verify_append(b"hello world", b"hello world")["append_only"])
        # Edit in middle / prefix
        self.assertFalse(verify_append(b"prefix1 tail", b"prefix2 tail append")["append_only"])
        # Truncation
        self.assertFalse(verify_append(b"hello world 12345", b"hello world")["append_only"])
        # CRLF conversion in prefix
        prev_lf = b"line1\nline2\n"
        curr_crlf = b"line1\r\nline2\r\nline3\r\n"
        self.assertFalse(verify_append(prev_lf, curr_crlf)["append_only"])
        # Closing trim exceeded (> allow_closing_trim)
        prev_pad = b'{"items": [' + (b" " * 40) + b"]}\n"
        curr_pad = b'{"items": [' + b"1, 2]}\n"
        self.assertFalse(verify_append(prev_pad, curr_pad, allow_closing_trim=32)["append_only"])


class TestIncrementalReadSession(unittest.TestCase):
    def test_disabled_by_default(self):
        s = IncrementalReadSession()
        p = "/tmp/test_disabled.log"
        content1 = "a" * 70000
        content2 = content1 + "b" * 1000
        self.assertIsNone(s.present(p, content1))
        self.assertIsNone(s.present(p, content2))

    def test_min_bytes_guard(self):
        s = IncrementalReadSession()
        p = "/tmp/test_min_bytes.log"
        content1 = "small content"
        content2 = content1 + " and more"
        self.assertIsNone(s.present(p, content1, enabled=True, min_bytes=65536))
        self.assertIsNone(s.present(p, content2, enabled=True, min_bytes=65536))

    def test_allow_paths_filter(self):
        s = IncrementalReadSession()
        p_allowed = "/tmp/allowed/app.log"
        p_blocked = "/tmp/blocked/app.log"
        c1 = "x" * 1000
        c2 = c1 + "y" * 500
        allow = ["/tmp/allowed/*"]

        # Blocked path never returns incremental
        self.assertIsNone(s.present(p_blocked, c1, enabled=True, min_bytes=100, allow_paths=allow))
        self.assertIsNone(s.present(p_blocked, c2, enabled=True, min_bytes=100, allow_paths=allow))

        # Allowed path: first read full, second returns incremental tail
        self.assertIsNone(s.present(p_allowed, c1, enabled=True, min_bytes=100, allow_paths=allow))
        res = s.present(p_allowed, c2, enabled=True, min_bytes=100, allow_paths=allow)
        self.assertIsNotNone(res)
        self.assertTrue(res["incremental"])
        self.assertEqual(res["tail"], "y" * 500)

    def test_first_read_full_second_read_tail(self):
        s = IncrementalReadSession()
        p = "/tmp/test_flow.log"
        c1 = "line 1\n" * 10000
        c2 = c1 + "tail 2\n" * 500

        res1 = s.present(p, c1, enabled=True, min_bytes=1000)
        self.assertIsNone(res1)  # Fail-closed first read

        res2 = s.present(p, c2, enabled=True, min_bytes=1000)
        self.assertIsNotNone(res2)
        self.assertTrue(res2["incremental"])
        self.assertEqual(res2["mode"], "exact")
        self.assertEqual(res2["tail"], "tail 2\n" * 500)
        self.assertEqual(res2["unchanged_prefix_bytes"], len(c1.encode("utf-8")))
        self.assertEqual(res2["tokens_avoided"], res2["unchanged_prefix_bytes"] // 4)

    def test_non_append_edit_falls_back_to_full(self):
        s = IncrementalReadSession()
        p = "/tmp/test_edit.log"
        c1 = "line 1\n" * 10000
        s.present(p, c1, enabled=True, min_bytes=1000)

        # An edit in the middle
        c_edited = "line 0\n" + ("line 1\n" * 9999) + "tail\n"
        res = s.present(p, c_edited, enabled=True, min_bytes=1000)
        self.assertIsNone(res)

    def test_reset_path_and_reset_all(self):
        s = IncrementalReadSession()
        p = "/tmp/test_resets.log"
        c1 = "line 1\n" * 10000
        c2 = c1 + "tail\n"

        s.present(p, c1, enabled=True, min_bytes=1000)
        s.reset_path(p)
        self.assertIsNone(s.present(p, c2, enabled=True, min_bytes=1000))

        s.present(p, c1, enabled=True, min_bytes=1000)
        s.reset_all()
        self.assertIsNone(s.present(p, c2, enabled=True, min_bytes=1000))

    def test_lru_eviction(self):
        # Bound session bytes to 2500 bytes
        s = IncrementalReadSession(max_session_bytes=2500)
        p1 = "/tmp/f1.log"
        p2 = "/tmp/f2.log"
        c1 = "a" * 1500
        c2 = "b" * 1500

        s.present(p1, c1, enabled=True, min_bytes=100)
        s.present(p2, c2, enabled=True, min_bytes=100)
        # Total bytes = 3000 > 2500, p1 was evicted and p2 was kept
        res2 = s.present(p2, c2 + "tail", enabled=True, min_bytes=100)
        self.assertIsNotNone(res2)
        self.assertTrue(res2["incremental"])

        # p1 was evicted earlier, so presenting it returns None (first read)
        res1 = s.present(p1, c1 + "tail", enabled=True, min_bytes=100)
        self.assertIsNone(res1)


class TestMCPBridgeIncrementalIntegration(unittest.TestCase):
    def setUp(self):
        self.bridge = MCPBridge(socket_path="/nonexistent-incremental.sock")
        self.counted = []
        self.bridge._uds_request = lambda cmd, **kw: (
            self.counted.append((cmd, kw)) or ({"result": {}} if cmd == "ping" else {})
        )
        self.bridge._incremental_enabled = True
        self.bridge._incremental_min_bytes = 100

    def test_bridge_incremental_read_flow(self):
        path = "/tmp/bridge_inc.log"
        c1 = "hello world\n" * 50  # 600 bytes
        c2 = c1 + "appended tail\n" * 10

        # First read: cached=False from disk
        resp1 = {"result": {"content": c1, "cached": False, "path": path}}
        p1 = self.bridge._present_read({"path": path}, resp1)
        self.assertEqual(p1.get("result", {}).get("content"), c1)

        # Second read with append: returns incremental dict
        resp2 = {"result": {"content": c2, "cached": False, "path": path}}
        p2 = self.bridge._present_read({"path": path}, resp2)
        res2 = p2.get("result", {})
        self.assertTrue(res2.get("incremental"))
        self.assertEqual(res2.get("tail"), "appended tail\n" * 10)
        self.assertEqual(res2.get("tokens_avoided"), len(c1.encode("utf-8")) // 4)

        # Verify dedup_tokens call with category="incremental_reads"
        inc_reqs = [
            kw
            for cmd, kw in self.counted
            if cmd == "dedup_tokens" and kw.get("category") == "incremental_reads"
        ]
        self.assertEqual(len(inc_reqs), 1)
        self.assertEqual(inc_reqs[0]["tokens"], len(c1.encode("utf-8")) // 4)

    def test_bridge_incremental_bypass_cache_forces_full(self):
        path = "/tmp/bridge_bypass.log"
        c1 = "hello world\n" * 50
        c2 = c1 + "appended tail\n" * 10

        resp1 = {"result": {"content": c1, "cached": False, "path": path}}
        self.bridge._present_read({"path": path}, resp1)

        # Reread with bypass_cache=True
        resp2 = {"result": {"content": c2, "cached": False, "path": path}}
        p2 = self.bridge._present_read({"path": path, "bypass_cache": True}, resp2)
        self.assertFalse(p2.get("result", {}).get("incremental", False))

    def test_bridge_compaction_epoch_resets_incremental(self):
        path = "/tmp/bridge_compact.log"
        c1 = "hello world\n" * 50
        c2 = c1 + "appended tail\n" * 10

        resp1 = {"result": {"content": c1, "cached": False, "path": path, "compaction_epoch": 1}}
        self.bridge._present_read({"path": path}, resp1)

        # Compaction epoch changes to 2
        resp2 = {"result": {"content": c2, "cached": False, "path": path, "compaction_epoch": 2}}
        p2 = self.bridge._present_read({"path": path}, resp2)
        # Epoch changed -> reset -> full send
        self.assertFalse(p2.get("result", {}).get("incremental", False))

    def test_bridge_write_file_resets_incremental(self):
        path = os.path.abspath("/tmp/bridge_write.log")
        c1 = "hello world\n" * 50
        c2 = c1 + "appended tail\n" * 10

        resp1 = {"result": {"content": c1, "cached": False, "path": path}}
        self.bridge._present_read({"path": path}, resp1)

        # Agent writes to file
        self.bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "write_file", "arguments": {"path": path, "content": "fresh"}},
            }
        )

        # Next read cannot be incremental against the pre-write base
        resp2 = {"result": {"content": c2, "cached": False, "path": path}}
        p2 = self.bridge._present_read({"path": path}, resp2)
        self.assertFalse(p2.get("result", {}).get("incremental", False))
