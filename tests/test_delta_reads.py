# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests: Delta Reads (P2) — roundtrip diff application, category routing, ratio guard, record_agent_write, armor."""

from __future__ import annotations

import os
import random
import unittest

from toolrecall.mcp_bridge import MCPBridge
from toolrecall.stub_core import FileStubSession


def apply_diff(base_str: str, diff_str: str) -> str:
    """Apply a unified diff (ignoring machine comments) to base_str."""
    diff_lines = diff_str.splitlines(keepends=True)
    # Strip any comment lines like "# delta from ..."
    diff_lines = [line for line in diff_lines if not line.startswith("#")]
    i = 0
    while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
        i += 1
    base_lines = base_str.splitlines(keepends=True)
    out_lines = []
    base_idx = 0
    while i < len(diff_lines):
        line = diff_lines[i]
        if line.startswith("@@"):
            parts = line.split("@@")[1].strip().split(" ")
            old_part = parts[0]
            old_start = int(old_part[1:].split(",")[0])
            target_idx = old_start - 1 if old_start > 0 else 0
            while base_idx < target_idx and base_idx < len(base_lines):
                out_lines.append(base_lines[base_idx])
                base_idx += 1
            i += 1
            continue
        if line.startswith("+"):
            out_lines.append(line[1:])
            i += 1
        elif line.startswith("-"):
            base_idx += 1
            i += 1
        elif line.startswith(" "):
            out_lines.append(base_lines[base_idx])
            base_idx += 1
            i += 1
        else:
            i += 1
    while base_idx < len(base_lines):
        out_lines.append(base_lines[base_idx])
        base_idx += 1
    return "".join(out_lines)


class TestDeltaRoundtrip(unittest.TestCase):
    def test_100_randomized_edit_roundtrips(self):
        rng = random.Random(1337)
        for trial in range(100):
            session = FileStubSession(min_chars=0, diff_ratio=0.5)
            path = f"/tmp/test_delta_{trial}.py"

            # Generate 80-120 base lines with sufficient padding so small edits stay under 50% diff ratio
            num_lines = rng.randint(80, 120)
            base_lines = [
                f'def line_{i}(): return "value_{rng.randint(100000, 999999)}_padding_string_content"\n'
                for i in range(num_lines)
            ]
            base_content = "".join(base_lines)

            # First read: full send primes the base
            decision1 = session.present(path, base_content)
            self.assertIsNone(decision1, f"Trial {trial}: first read must be full send")

            # Make 1-2 targeted modifications
            curr_lines = list(base_lines)
            num_edits = rng.randint(1, 2)
            for _ in range(num_edits):
                action = rng.choice(["insert", "delete", "replace"])
                idx = rng.randint(0, len(curr_lines) - 1)
                if action == "insert":
                    curr_lines.insert(idx, f"# inserted comment {rng.randint(1000, 9999)}\n")
                elif action == "delete" and len(curr_lines) > 20:
                    del curr_lines[idx]
                else:
                    curr_lines[idx] = f"# replaced {rng.randint(1000, 9999)}\n"

            curr_content = "".join(curr_lines)

            # Second read: delta presentation
            decision2 = session.present(path, curr_content)
            self.assertIsNotNone(decision2, f"Trial {trial}: small edit should emit delta")
            self.assertTrue(decision2.get("changed"))
            self.assertEqual(decision2.get("kind"), "delta")

            diff = decision2.get("diff", "")
            self.assertTrue(
                diff.startswith("# delta from "),
                f"Trial {trial}: diff header missing machine readable prefix",
            )
            self.assertIn("base_sha256", decision2)
            self.assertIn("full_tokens", decision2)
            self.assertIn("diff_tokens", decision2)

            # Apply diff and verify exact reconstruction
            reconstructed = apply_diff(base_content, diff)
            self.assertEqual(
                reconstructed,
                curr_content,
                f"Trial {trial}: reconstructed text does not match curr_content",
            )


class TestDeltaVsStubRouting(unittest.TestCase):
    def test_counter_fn_four_arg_routing(self):
        calls = []

        def counter_fn(tokens: int, path: str, note: str, kind: str):
            calls.append((tokens, path, note, kind))

        session = FileStubSession(min_chars=0, counter_fn=counter_fn)
        path = "/tmp/routing_test.txt"
        base = "line 1\nline 2\n" + ("line 3\n" * 50)
        modified = "line 1 modified\nline 2\n" + ("line 3\n" * 50)

        # 1. First send: primes base
        session.present(path, base)

        # 2. Unchanged send: stub -> kind="stub"
        session.present(path, base)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[-1][3], "stub")

        # 3. Small change: diff -> kind="delta"
        session.present(path, modified)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1][3], "delta")

    def test_counter_fn_three_arg_backward_compatibility(self):
        calls = []

        def legacy_counter_fn(tokens: int, path: str, note: str = ""):
            calls.append((tokens, path, note))

        session = FileStubSession(min_chars=0, counter_fn=legacy_counter_fn)
        path = "/tmp/legacy_test.txt"
        base = "hello world\n" * 50
        modified = "hello world modified\n" + ("hello world\n" * 49)

        # Should not raise TypeError when called with 3-arg counter_fn
        session.present(path, base)
        session.present(path, base)
        self.assertEqual(len(calls), 1)

        session.present(path, modified)
        self.assertEqual(len(calls), 2)

    def test_mcp_bridge_category_routing(self):
        bridge = MCPBridge(socket_path="/nonexistent-routing.sock")
        uds_calls = []
        bridge._uds_request = lambda cmd, **kw: (
            uds_calls.append((cmd, kw)) or ({"result": {}} if cmd == "ping" else {})
        )

        # Delta kind maps to mcp_delta
        bridge._count_measured(tokens=42, log_path="/tmp/file.py", kind="delta")
        self.assertEqual(uds_calls[-1][0], "dedup_tokens")
        self.assertEqual(uds_calls[-1][1]["tokens"], 42)
        self.assertEqual(uds_calls[-1][1]["category"], "mcp_delta")

        # Stub kind maps to mcp_dedup
        bridge._count_measured(tokens=84, log_path="/tmp/file.py", kind="stub")
        self.assertEqual(uds_calls[-1][0], "dedup_tokens")
        self.assertEqual(uds_calls[-1][1]["tokens"], 84)
        self.assertEqual(uds_calls[-1][1]["category"], "mcp_dedup")

        # Incremental kind maps to incremental_reads
        bridge._count_measured(tokens=128, log_path="/tmp/file.py", kind="incremental")
        self.assertEqual(uds_calls[-1][0], "dedup_tokens")
        self.assertEqual(uds_calls[-1][1]["tokens"], 128)
        self.assertEqual(uds_calls[-1][1]["category"], "incremental_reads")

    def test_mcp_bridge_diff_forwarding_fields(self):
        bridge = MCPBridge(socket_path="/nonexistent-fields.sock")
        bridge._uds_request = lambda cmd, **kw: {"result": {}} if cmd == "ping" else {}
        path = "/tmp/forward_fields.txt"
        c1 = "alpha\n" * 100
        c2 = "beta\n" + ("alpha\n" * 99)

        # Prime with full send
        bridge._present_read({"path": path}, {"result": {"content": c1, "cached": False}})

        # Second read with small change: emits diff
        resp = bridge._present_read({"path": path}, {"result": {"content": c2, "cached": False}})
        res = resp.get("result", {})
        self.assertTrue(res.get("changed"))
        self.assertIn("base_sha256", res)
        self.assertIn("full_tokens", res)
        self.assertIn("diff_tokens", res)
        self.assertIn("diff", res)


class TestRatioGuard(unittest.TestCase):
    def test_ratio_guard_falls_back_to_full(self):
        # 50% ratio threshold
        session = FileStubSession(min_chars=0, diff_ratio=0.5)
        path = "/tmp/ratio_test.txt"
        base = "line\n" * 100
        # Complete rewrite: 100 lines rewritten
        rewrite = "completely different content\n" * 100

        session.present(path, base)
        # Exceeds 50% diff ratio -> returns None (full send)
        decision = session.present(path, rewrite)
        self.assertIsNone(decision)


class TestRecordAgentWrite(unittest.TestCase):
    def test_record_agent_write_arms_session(self):
        session = FileStubSession(min_chars=0)
        path = "/tmp/write_arm.txt"
        content_written = "original line 1\noriginal line 2\n" + ("pad\n" * 50)
        content_read = "original line 1 modified\noriginal line 2\n" + ("pad\n" * 50)

        # Simulate agent write
        session.record_agent_write(path, content_written)

        # Subsequent read with minor edit against written content emits diff
        decision = session.present(path, content_read)
        self.assertIsNotNone(decision)
        self.assertTrue(decision.get("changed"))
        self.assertEqual(decision.get("kind"), "delta")

    def test_bridge_write_file_arms_and_serves_diff(self):
        bridge = MCPBridge(socket_path="/nonexistent-write.sock")
        bridge._uds_request = lambda cmd, **kw: {"result": {}} if cmd == "ping" else {}
        path = os.path.abspath("/tmp/bridge_write_diff.txt")
        content_written = "line 1\nline 2\n" + ("pad\n" * 50)
        content_read = "line 1 modified\nline 2\n" + ("pad\n" * 50)

        # 1. Agent calls write_file
        res = bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "write_file",
                    "arguments": {"path": path, "content": content_written},
                },
            }
        )
        self.assertNotIn("error", res)

        # 2. Agent reads slightly changed file
        read_resp = bridge._present_read(
            {"path": path}, {"result": {"content": content_read, "cached": False}}
        )
        res_body = read_resp.get("result", {})
        self.assertTrue(res_body.get("changed"))
        self.assertIn("diff", res_body)


class TestConsecutiveArmorAndCompaction(unittest.TestCase):
    def test_consecutive_armor_forces_full_send(self):
        # max_consecutive = 3
        session = FileStubSession(min_chars=0, max_consecutive=3)
        path = "/tmp/armor_test.txt"
        base = "line 1\n" + ("pad\n" * 50)
        mod = "line 1 mod\n" + ("pad\n" * 50)

        # 0: primes base
        session.present(path, base)

        # Suppressions 1, 2, 3
        for i in range(1, 4):
            decision = session.present(path, mod)
            self.assertIsNotNone(decision, f"Suppression {i} should succeed")

        # 4th suppression blocked by compaction armor
        self.assertIsNone(session.present(path, mod))

    def test_compaction_reset_all_forces_full_send(self):
        session = FileStubSession(min_chars=0)
        path = "/tmp/compaction_test.txt"
        base = "line 1\n" + ("pad\n" * 50)
        mod = "line 1 mod\n" + ("pad\n" * 50)

        session.present(path, base)
        session.reset_all()

        # After reset_all, next read must be full send
        self.assertIsNone(session.present(path, mod))
