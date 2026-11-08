#!/usr/bin/env python3
# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Empirical Antigravity harness: replay canonicalization, dedup, and replay forget.

Evaluates ToolRecall's deterministic execution and caching layer under simulated
Antigravity agent execution dynamics:
  1. Replay under Antigravity canonicalization:
     Antigravity injects volatile metadata (<ADDITIONAL_METADATA> timestamps,
     conversation UUIDs, brain artifact paths, and test duration numbers).
     - Cold run: MISS (upstream called and cached).
     - Warm rerun (new session, new timestamp, new UUIDs): HIT (0 upstream calls, replayed).
  2. Control arm (no canonicalization):
     - Warm rerun misses due to volatile timestamps and UUIDs.
  3. Normalized Request-Body Dedup:
     - Repetitive multi-turn tool output with fluctuating execution timings is
       normalized and stubbed, avoiding redundant token re-billing.
  4. CI Failure Recovery (replay forget):
     - A failed run tagged with a run_id is evicted via `replay forget <run_id>`.
     - Retrying the run bypasses cache and goes live again against upstream.

Usage:
    python3 bench/antigravity_replay/bench_antigravity.py
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import uuid

# Ensure worktree toolrecall is imported
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import toolrecall.proxy as proxy_mod  # noqa: E402
from toolrecall.cache import _init, cached_api_check, cached_api_store, forget_run  # noqa: E402


class MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Mock upstream LLM provider endpoint."""

    request_count = 0
    received_bodies: list[bytes] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        MockUpstreamHandler.request_count += 1
        MockUpstreamHandler.received_bodies.append(body)

        response_body = json.dumps(
            {
                "id": f"chatcmpl-mock-{MockUpstreamHandler.request_count}",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "gemini-2.5-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"Mock answer for request {MockUpstreamHandler.request_count}",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 15,
                    "total_tokens": 135,
                },
            }
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format, *args):
        pass


class DirectCacheClient:
    """In-process cache client backed by test-isolated SQLite DB."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def send(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "cached_api_check":
            return cached_api_check(msg["request_hash"], agent_id=msg.get("agent_id"))
        elif cmd == "cached_api_store":
            return cached_api_store(
                msg["request_hash"],
                msg["method"],
                msg["host"],
                msg["path"],
                msg["request_body_hash"],
                msg["response_status"],
                msg["response_headers"],
                msg["response_body"],
                ttl=msg.get("ttl"),
                run_id=msg.get("run_id"),
            )
        elif cmd == "cached_api_forget":
            return forget_run(msg.get("run_id") or msg.get("target"))
        return {}


def _make_antigravity_prompt(
    timestamp: str,
    conv_id: str,
    brain_dir: str,
    test_duration: str,
    tool_text: str = "",
    task: str = "run pytest on test suite",
) -> dict:
    """Simulate an Antigravity multi-turn payload with dynamic metadata."""
    content = (
        f"<ADDITIONAL_METADATA>\n"
        f"The current local time is: {timestamp}\n"
        f"Conversation ID: {conv_id}\n"
        f"Brain directory: {brain_dir}\n"
        f"</ADDITIONAL_METADATA>\n\n"
        f"Task: {task}\n"
        f"Result: 12 passed in {test_duration}\n"
    )
    if tool_text:
        content += f"\nTool output:\n{tool_text}"

    return {
        "model": "gemini-2.5-flash",
        "messages": [
            {"role": "user", "content": content},
        ],
    }


def run_benchmark():
    print("=" * 70)
    print("ToolRecall Empirical Antigravity Replay & Dedup Harness")
    print("=" * 70)

    # 1. Isolated temporary test environment
    temp_dir = tempfile.mkdtemp(prefix="tr_bench_ag_")
    test_db = os.path.join(temp_dir, "toolrecall-antigravity-bench-s42.db")
    test_usage = os.path.join(temp_dir, "usage.csv")

    orig_db = os.environ.get("TOOLRECALL_CACHE_DB")
    orig_usage = proxy_mod._USAGE_LOG_PATH
    orig_allowed = proxy_mod._host_allowed
    orig_connect = http.client.HTTPSConnection.connect

    os.environ["TOOLRECALL_CACHE_DB"] = test_db
    _init()
    proxy_mod._USAGE_LOG_PATH = test_usage
    proxy_mod._host_allowed = lambda h: h.startswith("api.gemini.invalid")

    # Start mock upstream server
    upstream = http.server.HTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    upstream_port = upstream.server_port

    def redirected_connect(self):
        self.sock = socket.create_connection(("127.0.0.1", upstream_port), self.timeout)

    http.client.HTTPSConnection.connect = redirected_connect

    # Hermetic proxy handler wired to DirectCacheClient
    class HermeticProxyHandler(proxy_mod.ForwardProxyHandler):
        def setup(self):
            super().setup()
            self._client = DirectCacheClient(test_db)

    # Start forward proxy
    proxy_server = http.server.HTTPServer(("127.0.0.1", 0), HermeticProxyHandler)
    proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
    proxy_thread.start()
    proxy_port = proxy_server.server_port

    try:

        def send_request(body: dict, headers: dict | None = None) -> tuple[int, dict, str]:
            h = {
                "Host": "api.gemini.invalid",
                "Content-Type": "application/json",
            }
            if headers:
                h.update(headers)
            body_bytes = json.dumps(body).encode("utf-8")
            conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
            conn.request("POST", "/v1/chat/completions", body=body_bytes, headers=h)
            resp = conn.getresponse()
            cache_status = resp.headers.get("X-ToolRecall-Cache", "MISS")
            data = resp.read()
            conn.close()
            return resp.status, json.loads(data.decode("utf-8")), cache_status

        # -------------------------------------------------------------
        # Part 1: Antigravity Canonical Replay (Dynamic Metadata Isolation)
        # -------------------------------------------------------------
        print("\n--- Part 1: Replay with Antigravity Canonical Profile ---")
        run_1_conv = str(uuid.uuid4())
        body_run_1 = _make_antigravity_prompt(
            timestamp="2026-11-08T09:15:00.123456Z",
            conv_id=run_1_conv,
            brain_dir=f"/home/user/.gemini/antigravity-cli/brain/{run_1_conv}",
            test_duration="0.34s",
            task="fix test failure in test_cache.py",
        )

        upstream_before = MockUpstreamHandler.request_count
        status1, resp1, cache1 = send_request(
            body_run_1,
            headers={
                "X-ToolRecall-Canonicalize": "antigravity",
                "X-ToolRecall-Run-Id": "run-ag-1",
            },
        )
        upstream_after1 = MockUpstreamHandler.request_count
        print(
            f"Run 1 (Cold attempt)      : Status {status1}, Cache: {cache1}, Upstream calls: {upstream_after1 - upstream_before}"
        )
        assert cache1 == "MISS", f"Expected MISS on cold run, got {cache1}"
        assert upstream_after1 - upstream_before == 1, "Expected 1 upstream call on cold run"

        # Run 2: New session! Timestamp changed, UUID changed, duration changed
        run_2_conv = str(uuid.uuid4())
        body_run_2 = _make_antigravity_prompt(
            timestamp="2026-11-08T16:42:19.987654Z",
            conv_id=run_2_conv,
            brain_dir=f"/home/user/.gemini/antigravity-cli/brain/{run_2_conv}",
            test_duration="1.89s",
            task="fix test failure in test_cache.py",
        )

        upstream_before = MockUpstreamHandler.request_count
        status2, resp2, cache2 = send_request(
            body_run_2,
            headers={
                "X-ToolRecall-Canonicalize": "antigravity",
                "X-ToolRecall-Run-Id": "run-ag-2",
            },
        )
        upstream_after2 = MockUpstreamHandler.request_count
        print(
            f"Run 2 (Warm rerun, new IDs): Status {status2}, Cache: {cache2}, Upstream calls: {upstream_after2 - upstream_before}"
        )
        assert cache2 == "HIT", (
            f"Expected HIT on warm rerun under antigravity profile, got {cache2}"
        )
        assert upstream_after2 - upstream_before == 0, "Expected 0 upstream calls on cached rerun"
        print("  -> Verified: Antigravity metadata normalized, 100% tokens saved on rerun!")

        # -------------------------------------------------------------
        # Part 2: Control Arm (Standard Byte-Exact Hashing)
        # -------------------------------------------------------------
        print("\n--- Part 2: Control Arm (Standard / No Canonicalization) ---")
        run_ctrl_1_conv = str(uuid.uuid4())
        body_ctrl_1 = _make_antigravity_prompt(
            timestamp="2026-11-08T09:15:00.123456Z",
            conv_id=run_ctrl_1_conv,
            brain_dir=f"/home/user/.gemini/antigravity-cli/brain/{run_ctrl_1_conv}",
            test_duration="0.34s",
            task="control arm evaluation",
        )
        upstream_before = MockUpstreamHandler.request_count
        s_c1, _, c_c1 = send_request(body_ctrl_1)
        assert c_c1 == "MISS"

        run_ctrl_2_conv = str(uuid.uuid4())
        body_ctrl_2 = _make_antigravity_prompt(
            timestamp="2026-11-08T16:42:19.987654Z",
            conv_id=run_ctrl_2_conv,
            brain_dir=f"/home/user/.gemini/antigravity-cli/brain/{run_ctrl_2_conv}",
            test_duration="1.89s",
            task="control arm evaluation",
        )
        s_c2, _, c_c2 = send_request(body_ctrl_2)
        upstream_after_c = MockUpstreamHandler.request_count
        print(f"Control Run 1 (Cold)     : Cache: {c_c1}")
        print(
            f"Control Run 2 (Warm Rerun): Cache: {c_c2}, Upstream calls: {upstream_after_c - upstream_before}"
        )
        assert c_c2 == "MISS", (
            "Expected MISS without canonicalization due to timestamp and UUID differences"
        )
        print("  -> Verified: Without profile, metadata differences fork the key (0% hit rate).")

        # -------------------------------------------------------------
        # Part 3: Repetitive Tool Output Dedup under Antigravity
        # -------------------------------------------------------------
        print("\n--- Part 3: Normalized Request-Body Dedup ---")
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        large_tool_block_1 = (
            "pytest run: 50 tests passed\n"
            + ("Log entry line: [INFO] processing step...\n" * 40)
            + "Finished in 0.42s"
        )
        large_tool_block_2 = (
            "pytest run: 50 tests passed\n"
            + ("Log entry line: [INFO] processing step...\n" * 40)
            + "Finished in 1.15s"
        )

        multi_turn_body = {
            "model": "gemini-2.5-flash",
            "messages": [
                {"role": "system", "content": "You are a coding assistant."},
                {"role": "user", "content": "Run tests"},
                {"role": "tool", "content": large_tool_block_1},
                {"role": "assistant", "content": "Tests passed, running next check."},
                {"role": "user", "content": "Re-run verify"},
                {"role": "tool", "content": large_tool_block_2},
                {"role": "assistant", "content": "Done."},
                {"role": "user", "content": "Final check."},
            ],
        }

        # Clear MockUpstreamHandler received bodies
        MockUpstreamHandler.received_bodies.clear()
        s_dedup, _, _ = send_request(
            multi_turn_body,
            headers={
                "X-ToolRecall-Canonicalize": "antigravity",
                "X-ToolRecall-No-Cache": "1",  # bypass cache to inspect forwarded body
            },
        )
        assert len(MockUpstreamHandler.received_bodies) == 1
        forwarded_envelope = json.loads(MockUpstreamHandler.received_bodies[0].decode("utf-8"))
        stubs_found = [
            m["content"]
            for m in forwarded_envelope["messages"]
            if isinstance(m.get("content"), str) and "[toolrecall-dedup]" in m["content"]
        ]
        print(f"Forwarded messages count: {len(forwarded_envelope['messages'])}")
        print(f"Dedup stubs inserted    : {len(stubs_found)}")
        assert len(stubs_found) >= 1, (
            "Expected at least 1 dedup stub for normalized tool output duplicate"
        )
        print(f"  -> Verified: Repetitive output stubbed: '{stubs_found[0][:60]}...'")

        # -------------------------------------------------------------
        # Part 4: CI Failure Recovery (`toolrecall replay forget <run_id>`)
        # -------------------------------------------------------------
        print("\n--- Part 4: CI Failure Recovery (replay forget) ---")
        failed_run_id = "failed-ci-attempt-101"
        failed_conv_id = str(uuid.uuid4())
        failed_prompt = _make_antigravity_prompt(
            timestamp="2026-11-08T18:00:00.000000Z",
            conv_id=failed_conv_id,
            brain_dir=f"/home/user/.gemini/antigravity-cli/brain/{failed_conv_id}",
            test_duration="2.50s",
            task="investigate crash in module xyz",
        )

        upstream_before = MockUpstreamHandler.request_count
        s_f1, _, c_f1 = send_request(
            failed_prompt,
            headers={
                "X-ToolRecall-Canonicalize": "antigravity",
                "X-ToolRecall-Run-Id": failed_run_id,
            },
        )
        print(f"Initial attempt for {failed_run_id}: Cache: {c_f1}")
        assert c_f1 == "MISS"

        # Verify it cached
        s_f2, _, c_f2 = send_request(
            failed_prompt,
            headers={"X-ToolRecall-Canonicalize": "antigravity"},
        )
        assert c_f2 == "HIT", "Expected HIT before eviction"
        print(f"Immediate repeat without forget      : Cache: {c_f2} (would reproduce failure)")

        # Evict failed run!
        evicted = forget_run(failed_run_id)
        print(
            f"toolrecall replay forget {failed_run_id} -> Evicted {evicted['forgotten']} cache entries"
        )
        assert evicted["forgotten"] >= 1

        # Now retry attempt must go LIVE against upstream (MISS)
        upstream_before_retry = MockUpstreamHandler.request_count
        s_f3, _, c_f3 = send_request(
            failed_prompt,
            headers={
                "X-ToolRecall-Canonicalize": "antigravity",
                "X-ToolRecall-Run-Id": "retry-ci-attempt-102",
            },
        )
        upstream_after_retry = MockUpstreamHandler.request_count
        print(
            f"Retry after forget (fresh attempt)    : Cache: {c_f3}, Upstream calls: {upstream_after_retry - upstream_before_retry}"
        )
        assert c_f3 == "MISS", f"Expected MISS on retry after forget, got {c_f3}"
        assert upstream_after_retry - upstream_before_retry == 1, (
            "Expected retry to call upstream live"
        )
        print("  -> Verified: Failed run evicted; retry executed live without freezing failure!")

        print("\n" + "=" * 70)
        print("ALL ANTIGRAVITY EMPIRICAL BENCHMARKS PASSED SUCCESSFULLY!")
        print("=" * 70)

    finally:
        os.environ.pop("TOOLRECALL_FORWARD_DEDUP", None)
        if orig_db is not None:
            os.environ["TOOLRECALL_CACHE_DB"] = orig_db
        else:
            os.environ.pop("TOOLRECALL_CACHE_DB", None)
        proxy_mod._USAGE_LOG_PATH = orig_usage
        proxy_mod._host_allowed = orig_allowed
        http.client.HTTPSConnection.connect = orig_connect
        proxy_server.shutdown()
        upstream.shutdown()


if __name__ == "__main__":
    run_benchmark()
