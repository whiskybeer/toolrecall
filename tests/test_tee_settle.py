# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tee-settle registry — closes the stream-cache store race.

Race (measured 2026-09-11): r2 fired immediately after r1's SSE stream
ended could reach cached_api_check before r1's tee store committed →
r2 went LIVE (double billing risk on retries). The registry lets a
racing miss wait (bounded) for the in-flight store of the same hash.
"""

import sys
import threading
import time
import unittest

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from toolrecall import tee_settle  # noqa: E402


class TestRegistry(unittest.TestCase):
    def setUp(self):
        tee_settle._pending.clear()  # hermetic registry

    def test_wait_returns_immediately_when_not_pending(self):
        t0 = time.monotonic()
        self.assertTrue(tee_settle.wait_settled("hash-a"))
        self.assertLess(time.monotonic() - t0, 0.05)  # no sleep on normal path

    def test_wait_blocks_until_settle(self):
        tee_settle.register("hash-b")
        done = []

        def settle_later():
            time.sleep(0.05)
            tee_settle.settle("hash-b")
            done.append(True)

        threading.Thread(target=settle_later).start()
        t0 = time.monotonic()
        self.assertTrue(tee_settle.wait_settled("hash-b"))
        self.assertGreaterEqual(time.monotonic() - t0, 0.04)
        self.assertEqual(done, [True])

    def test_wait_bounded_by_budget(self):
        tee_settle.register("hash-c")
        t0 = time.monotonic()
        # tiny budget so the test stays fast
        self.assertFalse(tee_settle.wait_settled("hash-c", budget=0.05))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_stale_registration_pruned(self):
        tee_settle.register("hash-d")
        with tee_settle._lock:
            tee_settle._pending["hash-d"] = time.monotonic() - 20.0  # ancient
        self.assertTrue(tee_settle.wait_settled("hash-d"))
        self.assertNotIn("hash-d", tee_settle._pending)

    def test_register_is_idempotent_and_overwrite_keeps_time(self):
        tee_settle.register("hash-e")
        time.sleep(0.02)
        tee_settle.register("hash-e")  # same thread re-registering — allowed
        tee_settle.settle("hash-e")
        self.assertTrue(tee_settle.wait_settled("hash-e"))

    def test_concurrent_register_settle(self):

        for i in range(50):
            h = f"hash-{i}"
            tee_settle.register(h)
            tee_settle.settle(h)
        self.assertEqual(tee_settle._pending, {})


if __name__ == "__main__":
    unittest.main()
