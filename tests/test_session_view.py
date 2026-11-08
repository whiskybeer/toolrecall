# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Session view: stubs, diffs, line ranges, budgets and the compaction signal.

The state machine lives in stub_core (one contract, both surfaces — the shim
keeps the stub-only ``maybe_stub``; the bridge uses ``present``, which adds line
ranges and diff presentation). These tests pin the properties that make a
suppression SAFE:

  * the diff is always computed against the agent's last FULL copy — never
    against a diff we previously sent. This module cannot know whether a
    harness applied a diff, so it must never assume it: a wrong assumption
    compounds (the agent's copy silently diverges from ours).
  * freshness is the TTL AND the session budget (calls / tokens emitted since
    the full send) AND the armor (N consecutive suppressions → one full send),
    so a suppression can never outlive the agent's copy.
  * every suppression is a MEASURED saving (counter_fn), stubs and diffs alike.
"""

from __future__ import annotations

import unittest
from unittest import mock

from toolrecall import stub_core
from toolrecall.mcp_bridge import MCPBridge, _range_key
from toolrecall.stub_core import FileStubSession, slice_lines

FILE = "".join(f"line {i}\n" for i in range(1, 201))
NOTE_MCP = "File unchanged; see earlier read. If you no longer have it, pass bypass_cache=true."


def _session(**kw) -> FileStubSession:
    kw.setdefault("ttl_s", 600.0)
    kw.setdefault("max_consecutive", 2)
    kw.setdefault("min_chars", 0)
    kw.setdefault("note_fn", stub_core.note_mcp)
    return FileStubSession(**kw)


class TestPresentBasics(unittest.TestCase):
    def test_first_present_is_full_then_unchanged(self) -> None:
        s = _session()
        assert s.present("k", FILE) is None, "the first read must send full content"
        assert s.present("k", FILE) == {"unchanged": True, "note": NOTE_MCP}

    def test_differing_key_has_its_own_view(self) -> None:
        s = _session()
        assert s.present("a", FILE) is None
        assert s.present("b", FILE) is None, "a different key must not inherit 'a'"
        assert s.present("a", FILE) == {"unchanged": True, "note": NOTE_MCP}

    def test_empty_content_is_never_suppressed(self) -> None:
        s = _session()
        assert s.present("k", "") is None
        assert s.present("k", "") is None

    def test_ttl_zero_disables_suppression(self) -> None:
        s = _session(ttl_s=0.0)
        assert s.present("k", FILE) is None
        assert s.present("k", FILE) is None, "ttl_s=0 must disable the view entirely"


class TestDiffPresenter(unittest.TestCase):
    def test_small_edit_returns_an_applicable_diff(self) -> None:
        s = _session()
        assert s.present("k", FILE) is None
        changed = FILE.replace("line 100\n", "line 100 EDITED\n")
        out = s.present("k", changed)
        assert out is not None and out.get("changed") is True, out
        diff = out["diff"]
        assert "-line 100\n" in diff and "+line 100 EDITED\n" in diff, diff

    def test_large_rewrite_sends_full(self) -> None:
        s = _session()
        assert s.present("k", FILE) is None
        out = s.present("k", "".join(f"other {i}\n" for i in range(1, 400)))
        assert out is None, f"a rewrite must not be a diff: {out}"

    def test_diff_is_computed_against_the_last_full_copy(self) -> None:
        """The core safety property: state advances only on FULL sends.

        A → B sends diff(A→B). If the harness did NOT apply it and the file
        moves to C, the next response must still diff against A (the copy we
        know the agent has) — not against B, which we only ever sent as a
        patch.
        """
        s = _session(max_consecutive=99)
        a = FILE
        b = FILE.replace("line 5\n", "line 5 B\n")
        c = FILE.replace("line 7\n", "line 7 C\n")
        assert s.present("k", a) is None
        d_ab = s.present("k", b)
        assert d_ab and d_ab.get("changed"), d_ab
        d_ac = s.present("k", c)
        assert d_ac and d_ac.get("changed"), d_ac
        assert "+line 7 C\n" in d_ac["diff"], d_ac["diff"]
        # The diff is A→C: it must NOT contain B's edit (that would mean we
        # based it on a patch the harness may never have applied).
        assert "line 5 B" not in d_ac["diff"], d_ac["diff"]

    def test_diff_not_offered_when_disallowed(self) -> None:
        s = _session()
        assert s.present("k", FILE) is None
        changed = FILE.replace("line 100\n", "line 100 EDITED\n")
        assert s.present("k", changed, allow_diff=False) is None

    def test_diff_honours_max_consecutive_armor(self) -> None:
        s = _session(max_consecutive=1)
        assert s.present("k", FILE) is None
        changed = FILE.replace("line 100\n", "line 100 EDITED\n")
        first = s.present("k", changed)
        assert first is not None, "one suppression is allowed before the armor"
        assert s.present("k", changed) is None, "the armor must force a full send"

    def test_diff_does_not_extend_freshness(self) -> None:
        """A diff is not a full send: it must not reset the TTL/budget window."""
        s = _session(ttl_s=100.0, max_consecutive=99)
        changed = FILE.replace("line 100\n", "line 100 EDITED\n")
        with mock.patch.object(stub_core.time, "time", lambda: 1000.0):
            assert s.present("k", FILE) is None
            assert s.present("k", changed) is not None
        with mock.patch.object(stub_core.time, "time", lambda: 1000.0 + 101):
            # Window expired: even though the last response was a diff, the
            # agent's full copy is now presumed gone → full send.
            assert s.present("k", changed) is None


class TestSessionBudget(unittest.TestCase):
    def test_expires_after_call_budget(self) -> None:
        """The window is N calls of freshness; call N+1 forces a full send."""
        s = _session(max_calls=3, max_consecutive=99)
        assert s.present("k", FILE) is None
        for _ in range(3):
            assert s.present("k", FILE) is not None
        for _ in range(4):
            s.tick("x")
        assert s.present("k", FILE) is None, "the call budget must force a full send"

    def test_expires_after_token_budget(self) -> None:
        s = _session(max_tokens=100, max_consecutive=99)
        assert s.present("k", FILE) is None
        s.tick("y" * 500)  # 500//4 = 125 tokens > 100
        assert s.present("k", FILE) is None, "the token budget must force a full send"

    def test_budget_axes_off_by_default(self) -> None:
        """The shim gets no session clock; the budget must stay inert."""
        s = _session(max_consecutive=99)
        assert s.present("k", FILE) is None
        s.tick("z" * 10_000)
        assert s.present("k", FILE) is not None

    def test_full_send_rearms_the_budget(self) -> None:
        s = _session(max_calls=1, max_consecutive=99)
        assert s.present("k", FILE) is None
        s.tick("x")
        s.tick("x")  # 2 calls > 1 → the window is gone
        assert s.present("k", FILE) is None  # full send, re-armed
        assert s.present("k", FILE) is not None, "a full send must reset the window"


class TestReset(unittest.TestCase):
    def test_reset_key_forgets_one_view(self) -> None:
        s = _session()
        assert s.present("k", FILE) is None
        s.reset_key("k")
        assert s.present("k", FILE) is None

    def test_reset_all_forgets_everything(self) -> None:
        s = _session()
        assert s.present("a", FILE) is None
        assert s.present("b", FILE) is None
        s.reset_all()
        assert s.present("a", FILE) is None
        assert s.present("b", FILE) is None

    def test_reset_path_clears_ranged_views_too(self) -> None:
        import os
        import tempfile

        s = _session()
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "f.txt")
            with open(p, "w") as f:
                f.write(FILE)
            real = os.path.realpath(p)
            assert s.present(real, FILE) is None
            assert s.present(f"{real}#1-20", FILE[:100]) is None
            s.reset_path(p)
            assert s.present(real, FILE) is None
            assert s.present(f"{real}#1-20", FILE[:100]) is None


class TestMeasuredCounting(unittest.TestCase):
    """Every suppression must be counted — stubs AND diffs (wire-verified)."""

    def test_stub_and_diff_are_counted(self) -> None:
        counted: list[tuple[str, int]] = []
        s = _session(counter_fn=lambda tokens, path, note="": counted.append((path, tokens)))
        assert s.present("stub", FILE) is None
        assert s.present("stub", FILE) is not None
        assert counted, "a stub must be counted as a measured saving"
        assert counted[-1][1] > 0

        counted.clear()
        assert s.present("diff", FILE) is None
        out = s.present("diff", FILE.replace("line 100\n", "line 100 EDITED\n"))
        assert out and out.get("changed")
        assert counted, "a diff must be counted as a measured saving"
        assert counted[-1][1] > 0, f"diff saving not positive: {counted[-1]}"

    def test_full_send_is_not_counted(self) -> None:
        counted: list = []
        s = _session(counter_fn=lambda *a, **k: counted.append(a))
        assert s.present("k", FILE) is None
        assert counted == [], f"a full send saves nothing: {counted}"

    def test_unreadably_small_diff_is_not_a_saving(self) -> None:
        """A diff almost as large as the content must not inflate the count."""
        counted: list = []
        s = _session(counter_fn=lambda tokens, path, note="": counted.append(tokens))
        assert s.present("k", FILE) is None
        # Change most lines: the diff exceeds the ratio → full send, no count.
        s.present("k", "".join(f"x {i}\n" for i in range(1, 201)))
        assert counted == [], f"nothing was suppressed but we counted: {counted}"


class TestMaybeStubUnchanged(unittest.TestCase):
    """The shim surface keeps its stub-only contract (no diffs)."""

    def test_stub_only_and_note_string(self) -> None:
        s = _session(note_fn=stub_core.note_agent)
        assert s.maybe_stub("/tmp/x", FILE, is_agent=True, lossless=True) is None
        note = s.maybe_stub("/tmp/x", FILE, is_agent=True, lossless=True)
        assert isinstance(note, str) and "File unchanged" in note

    def test_never_stubs_non_agent_or_lossy(self) -> None:
        s = _session()
        assert s.maybe_stub("/tmp/x", FILE, is_agent=False, lossless=True) is None
        assert s.maybe_stub("/tmp/x", FILE, is_agent=False, lossless=True) is None
        assert s.maybe_stub("/tmp/y", FILE, is_agent=True, lossless=False) is None

    def test_changed_content_never_returns_a_diff(self) -> None:
        s = _session()
        assert s.maybe_stub("/tmp/x", FILE, is_agent=True, lossless=True) is None
        changed = FILE.replace("line 100\n", "line 100 EDITED\n")
        assert s.maybe_stub("/tmp/x", changed, is_agent=True, lossless=True) is None


class TestSliceLines(unittest.TestCase):
    def test_range(self) -> None:
        out = slice_lines(FILE, 10, 3)
        assert out["content"] == "line 10\nline 11\nline 12\n", out["content"]
        assert (out["start_line"], out["end_line"], out["total_lines"]) == (10, 12, 200)

    def test_offset_only_reads_to_end(self) -> None:
        out = slice_lines(FILE, 199, None)
        assert out["content"] == "line 199\nline 200\n", out["content"]
        assert out["end_line"] == 200

    def test_limit_only_reads_from_start(self) -> None:
        out = slice_lines(FILE, None, 2)
        assert out["content"] == "line 1\nline 2\n"
        assert out["start_line"] == 1

    def test_past_end_is_empty(self) -> None:
        out = slice_lines(FILE, 500, 10)
        assert out["content"] == ""
        assert out["start_line"] > out["end_line"], out

    def test_no_window_is_the_whole_file(self) -> None:
        assert slice_lines(FILE, None, None)["content"] == FILE


def _bridge() -> MCPBridge:
    with mock.patch.object(MCPBridge, "_count_measured"):
        return MCPBridge("/tmp/does-not-exist.sock")


class TestBridgeReadPresentation(unittest.TestCase):
    def test_repeat_read_is_stubbed_with_the_pinned_shape(self) -> None:
        b = _bridge()
        resp = {"content": FILE, "cached": True}
        first = b._present_read({"path": "/tmp/f.py"}, resp)
        assert first is resp, "the first read must pass through untouched"
        second = b._present_read({"path": "/tmp/f.py"}, {"content": FILE, "cached": True})
        assert second == {"result": {"unchanged": True, "note": NOTE_MCP}}, second

    def test_line_range_is_its_own_view(self) -> None:
        b = _bridge()
        full = b._present_read({"path": "/tmp/f.py"}, {"content": FILE})
        assert full == {"content": FILE}
        windowed = b._present_read(
            {"path": "/tmp/f.py", "offset": 10, "limit": 5}, {"content": FILE}
        )
        assert windowed["result"]["content"] == "".join(f"line {i}\n" for i in range(10, 15))
        assert windowed["result"]["start_line"] == 10
        # The range did not stub against the whole-file view.
        assert "unchanged" not in windowed["result"]
        # And repeating the same range stubs against the RANGE.
        again = b._present_read({"path": "/tmp/f.py", "offset": 10, "limit": 5}, {"content": FILE})
        assert again["result"].get("unchanged") is True
        assert again["result"]["start_line"] == 10

    def test_edited_file_returns_a_diff(self) -> None:
        b = _bridge()
        b._present_read({"path": "/tmp/f.py"}, {"content": FILE})
        out = b._present_read(
            {"path": "/tmp/f.py"}, {"content": FILE.replace("line 3\n", "line 3 X\n")}
        )
        assert out["result"].get("changed") is True, out
        assert "+line 3 X\n" in out["result"]["diff"]

    def test_bypass_always_returns_full(self) -> None:
        b = _bridge()
        b._present_read({"path": "/tmp/f.py"}, {"content": FILE})
        out = b._present_read({"path": "/tmp/f.py", "bypass_cache": True}, {"content": FILE})
        assert out == {"content": FILE}, "bypass_cache must forget the view"

    def test_error_response_passes_through(self) -> None:
        b = _bridge()
        err = {"error": "no such file"}
        assert b._present_read({"path": "/tmp/f.py"}, err) is err

    def test_bad_arguments_do_not_suppress(self) -> None:
        b = _bridge()
        assert b._present_read({}, {"content": FILE}) == {"content": FILE}
        assert b._present_read({"path": ""}, {"content": FILE}) == {"content": FILE}
        assert b._present_read({"path": "/tmp/f.py", "offset": "abc"}, {"content": FILE}) == {
            "content": FILE
        }

    def test_compaction_epoch_never_reaches_the_agent(self) -> None:
        b = _bridge()
        out = b._present_read({"path": "/tmp/f.py"}, {"content": FILE, "compaction_epoch": 3})
        assert out == {"content": FILE}, out


class TestTerminalPresentation(unittest.TestCase):
    def test_repeat_terminal_is_stubbed(self) -> None:
        b = _bridge()
        resp = {"output": "same output", "exit_code": 0}
        assert b._present_terminal("git status", resp) is resp
        out = b._present_terminal("git status", {"output": "same output", "exit_code": 0})
        assert out == {
            "result": {
                "unchanged": True,
                "note": 'Command "git status" returned identical output; see earlier result.',
            }
        }, out

    def test_changed_output_is_never_a_diff(self) -> None:
        b = _bridge()
        assert b._present_terminal("ls", {"output": "a\n"}) is not None
        out = b._present_terminal("ls", {"output": "b\n"})
        assert out == {"output": "b\n"}, out

    def test_distinct_commands_have_distinct_views(self) -> None:
        b = _bridge()
        assert b._present_terminal("ls", {"output": "x"}) is not None
        out = b._present_terminal("pwd", {"output": "x"})
        assert out == {"output": "x"}, out


class TestCompactionEpoch(unittest.TestCase):
    def test_epoch_change_resets_the_view(self) -> None:
        b = _bridge()
        b._present_read({"path": "/tmp/f.py"}, {"content": FILE, "compaction_epoch": 1})
        # Same content, same epoch → stub.
        stubbed = b._present_read({"path": "/tmp/f.py"}, {"content": FILE, "compaction_epoch": 1})
        assert stubbed["result"].get("unchanged") is True
        # The harness compacted: full content again.
        out = b._present_read({"path": "/tmp/f.py"}, {"content": FILE, "compaction_epoch": 2})
        assert out == {"content": FILE}, out

    def test_stable_epoch_does_not_reset(self) -> None:
        b = _bridge()
        b._present_read({"path": "/tmp/f.py"}, {"content": FILE, "compaction_epoch": 7})
        out = b._present_read({"path": "/tmp/f.py"}, {"content": FILE, "compaction_epoch": 7})
        assert out["result"].get("unchanged") is True, out


class TestRangeKey(unittest.TestCase):
    def test_whole_file_key_is_the_realpath(self) -> None:
        import os

        assert _range_key("/tmp/x.py", None, None) == os.path.realpath("/tmp/x.py")

    def test_window_key_is_distinct(self) -> None:
        assert _range_key("/tmp/x.py", 1, 50) != _range_key("/tmp/x.py", None, None)
        assert _range_key("/tmp/x.py", 1, 50) != _range_key("/tmp/x.py", 1, 60)


if __name__ == "__main__":
    unittest.main()
