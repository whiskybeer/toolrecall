"""Tests for toolrecall.resilience — coalescer, retry with jitter, circuit breaker.

All stdlib: threading (coalescer/CB), time+random (retry backoff). Tests
monkeypatch time.sleep / time.time to stay fast and deterministic.
"""

import threading
import time
import unittest
from unittest import mock

from toolrecall.resilience import CircuitBreaker, CircuitBreakerOpen, Coalescer, retry_call


class TestCoalescer(unittest.TestCase):
    def test_n_callers_one_execution(self):
        c = Coalescer()
        calls = []

        def producer():
            calls.append(1)
            time.sleep(0.05)  # widen the race window
            return "result"

        results = []
        lock = threading.Lock()

        def caller():
            r = c.run("key1", producer)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=caller) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(calls), 1, "producer must execute exactly once")
        self.assertEqual(results, ["result"] * 10)

    def test_different_keys_execute_separately(self):
        c = Coalescer()
        calls = []

        def producer_a():
            calls.append("a")
            return "A"

        def producer_b():
            calls.append("b")
            return "B"

        self.assertEqual(c.run("k-a", producer_a), "A")
        self.assertEqual(c.run("k-b", producer_b), "B")
        self.assertEqual(calls, ["a", "b"])

    def test_waiter_timeout_falls_through(self):
        c = Coalescer(window=0.1)

        started = threading.Event()
        release = threading.Event()

        def slow_producer():
            started.set()
            release.wait(timeout=5)
            return "slow"

        winner = threading.Thread(target=lambda: c.run("k", slow_producer))
        winner.start()
        started.wait(timeout=5)

        # This caller should time out waiting and execute its own producer
        result = c.run("k", lambda: "fallback")
        self.assertEqual(result, "fallback")
        release.set()
        winner.join()

    def test_exception_in_producer_propagates_to_all(self):
        c = Coalescer()
        errors = []

        def bad():
            raise ValueError("boom")

        def caller():
            try:
                c.run("k", bad)
            except ValueError as e:
                errors.append(str(e))

        threads = [threading.Thread(target=caller) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 5, "all waiters must see the producer's error")

    def test_keys_are_reusable_after_completion(self):
        c = Coalescer()
        self.assertEqual(c.run("k", lambda: 1), 1)
        self.assertEqual(c.run("k", lambda: 2), 2)  # not coalesced with the old flight


class TestRetryCall(unittest.TestCase):
    def test_success_first_try(self):
        calls = []
        r = retry_call(lambda: (calls.append(1), "ok")[1], max_attempts=3, sleep_fn=lambda s: None)
        self.assertEqual(r, "ok")
        self.assertEqual(len(calls), 1)

    def test_retries_on_retryable_exception(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("reset")
            return "ok"

        r = retry_call(flaky, max_attempts=3, sleep_fn=lambda s: None)
        self.assertEqual(r, "ok")
        self.assertEqual(len(calls), 3)

    def test_non_retryable_raises_immediately(self):
        calls = []

        def bad():
            calls.append(1)
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            retry_call(bad, max_attempts=3, retryable=(ConnectionError,), sleep_fn=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_exhaustion_raises_last_error(self):
        calls = []

        def always_fail():
            calls.append(1)
            raise ConnectionError("down")

        with self.assertRaises(ConnectionError):
            retry_call(
                always_fail, max_attempts=3, retryable=(ConnectionError,), sleep_fn=lambda s: None
            )
        self.assertEqual(len(calls), 3)

    def test_backoff_growth_and_jitter_bounds(self):
        sleeps = []

        def fail_twice():
            if len(sleeps) < 2:
                raise ConnectionError("x")
            return "ok"

        # Capture base * 2**attempt without the random part, then check bounds
        with mock.patch("toolrecall.resilience.random.uniform", return_value=0.05):
            retry_call(
                fail_twice,
                max_attempts=3,
                backoff_base=0.25,
                retryable=(ConnectionError,),
                sleep_fn=sleeps.append,
            )
        # attempt 0 → 0.25*1 + 0.05; attempt 1 → 0.25*2 + 0.05
        self.assertAlmostEqual(sleeps[0], 0.30)
        self.assertAlmostEqual(sleeps[1], 0.55)

    def test_status_code_retryable_check(self):
        from toolrecall.resilience import is_retryable_status

        self.assertTrue(is_retryable_status(429))
        self.assertTrue(is_retryable_status(502))
        self.assertTrue(is_retryable_status(503))
        self.assertTrue(is_retryable_status(504))
        self.assertFalse(is_retryable_status(200))
        self.assertFalse(is_retryable_status(400))
        self.assertFalse(is_retryable_status(404))


class TestCircuitBreaker(unittest.TestCase):
    def test_closed_allows_calls(self):
        cb = CircuitBreaker(failure_threshold=3, window=60, open_seconds=30, clock=time.time)
        self.assertEqual(cb.call(lambda: "ok"), "ok")
        self.assertEqual(cb.state, "closed")

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, window=60, open_seconds=30, clock=time.time)

        def fail():
            raise ConnectionError("down")

        for _ in range(3):
            with self.assertRaises(ConnectionError):
                cb.call(fail)
        self.assertEqual(cb.state, "open")

    def test_open_fast_fails_without_calling_producer(self):
        cb = CircuitBreaker(failure_threshold=1, window=60, open_seconds=30, clock=time.time)

        def fail():
            raise ConnectionError("down")

        with self.assertRaises(ConnectionError):
            cb.call(fail)
        self.assertEqual(cb.state, "open")

        called = []

        def never_runs():
            called.append(1)
            return "x"

        with self.assertRaises(CircuitBreakerOpen) as ctx:
            cb.call(never_runs)
        self.assertEqual(called, [], "open breaker must fast-fail without executing")
        self.assertIn("circuit_open", str(ctx.exception))

    def test_half_open_after_open_seconds(self):
        t = {"now": 1000.0}
        cb = CircuitBreaker(failure_threshold=1, window=60, open_seconds=30, clock=lambda: t["now"])

        def fail():
            raise ConnectionError("down")

        with self.assertRaises(ConnectionError):
            cb.call(fail)
        self.assertEqual(cb.state, "open")

        t["now"] += 31  # past open_seconds
        self.assertEqual(cb.state, "half_open")

        # One probe allowed; success closes
        self.assertEqual(cb.call(lambda: "probe-ok"), "probe-ok")
        self.assertEqual(cb.state, "closed")

    def test_half_open_failure_reopens(self):
        t = {"now": 1000.0}
        cb = CircuitBreaker(failure_threshold=1, window=60, open_seconds=30, clock=lambda: t["now"])

        with self.assertRaises(ConnectionError):
            cb.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        t["now"] += 31
        self.assertEqual(cb.state, "half_open")

        with self.assertRaises(ConnectionError):
            cb.call(lambda: (_ for _ in ()).throw(ConnectionError("still down")))
        self.assertEqual(cb.state, "open", "failed probe must reopen")

    def test_half_open_admits_only_one_probe(self):
        t = {"now": 1000.0}
        cb = CircuitBreaker(failure_threshold=1, window=60, open_seconds=30, clock=lambda: t["now"])
        with self.assertRaises(ConnectionError):
            cb.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        t["now"] += 31
        self.assertEqual(cb.state, "half_open")

        probe_running = threading.Event()
        probe_release = threading.Event()

        def slow_probe():
            probe_running.set()
            probe_release.wait(timeout=5)
            return "ok"

        probe = threading.Thread(target=lambda: cb.call(slow_probe))
        probe.start()
        probe_running.wait(timeout=5)

        # Second caller while probe in flight → fast-fail (still half_open)
        with self.assertRaises(CircuitBreakerOpen):
            cb.call(lambda: "never")

        probe_release.set()
        probe.join()
        self.assertEqual(cb.state, "closed")

    def test_failures_outside_window_do_not_count(self):
        t = {"now": 1000.0}
        cb = CircuitBreaker(failure_threshold=2, window=60, open_seconds=30, clock=lambda: t["now"])

        def fail():
            raise ConnectionError("down")

        with self.assertRaises(ConnectionError):
            cb.call(fail)
        t["now"] += 120  # outside the 60s window
        with self.assertRaises(ConnectionError):
            cb.call(fail)
        self.assertEqual(cb.state, "closed", "old failures must expire from the window")

    def test_retry_after_header_value(self):
        cb = CircuitBreaker(failure_threshold=1, window=60, open_seconds=42, clock=time.time)
        with self.assertRaises(ConnectionError):
            cb.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        with self.assertRaises(CircuitBreakerOpen) as ctx:
            cb.call(lambda: "x")
        self.assertEqual(ctx.exception.retry_after, 42)


if __name__ == "__main__":
    unittest.main()
