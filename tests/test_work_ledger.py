# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Work-unit ledger — the persistence half of work-unit identity.

``keys.work_key`` names a unit of work; this ledger remembers what it cost to
learn that unit's result, so a later run can decide whether to pay again. The
reuse POLICY stays with the caller (see the evaluator adapter); the ledger only
stores, counts, and reports — in its own ``work_cache`` category, so reuse is
visible in ``top``/healthcheck and can never borrow another tier's label.
"""

import time
import unittest

from toolrecall.cache import _init, get_stats, work_lookup, work_store


class TestWorkLedger(unittest.TestCase):
    def setUp(self):
        # The isolated test env starts empty; create the schema first (house
        # pattern, cf. tests/test_file_cache.py) so the ledger tables and
        # cache_stats exist. Without it every read degrades to None and the
        # tests would "pass" on a missing table.
        _init()

    def test_store_then_lookup_roundtrip(self):
        self.assertTrue(work_store("k1", {"combined_score": 0.5}, eval_time=1.2))
        got = work_lookup("k1")
        self.assertIsNotNone(got)
        self.assertEqual(got["metrics"], {"combined_score": 0.5})
        self.assertAlmostEqual(got["eval_time"], 1.2, places=3)

    def test_lookup_missing_key_returns_none(self):
        self.assertIsNone(work_lookup("no-such-key"))

    def test_store_never_overwrites_a_known_result(self):
        """First known result wins — a re-roll must not rewrite history."""
        self.assertTrue(work_store("k2", {"combined_score": 0.1}))
        self.assertFalse(work_store("k2", {"combined_score": 0.9}))
        self.assertEqual(work_lookup("k2")["metrics"], {"combined_score": 0.1})

    def test_attempts_count_paid_evaluations(self):
        """attempts is the reuse-policy input: how often did we PAY for this unit?"""
        work_store("k3", {"s": 1})
        self.assertEqual(work_lookup("k3")["attempts"], 1)
        work_store("k3", {"s": 2})  # a re-roll: paid again, metrics unchanged
        got = work_lookup("k3")
        self.assertEqual(got["attempts"], 2)
        self.assertEqual(got["metrics"], {"s": 1})

    def test_ttl_expiry_drops_the_entry(self):
        work_store("k4", {"s": 1}, ttl=0.05)
        self.assertIsNotNone(work_lookup("k4"))
        time.sleep(0.08)
        self.assertIsNone(work_lookup("k4"))

    def test_ttl_zero_never_expires(self):
        work_store("k5", {"s": 1}, ttl=0)
        time.sleep(0.02)
        self.assertIsNotNone(work_lookup("k5"))

    def test_work_cache_category_reports_hits_and_misses(self):
        work_store("k6", {"s": 1})
        work_lookup("k6")  # hit
        work_lookup("absent-key")  # miss
        cat = get_stats()["work_cache"]
        self.assertEqual(cat["hits"], 1)
        self.assertEqual(cat["misses"], 1)

    def test_rejects_empty_key(self):
        with self.assertRaises(ValueError):
            work_store("", {"s": 1})
        with self.assertRaises(ValueError):
            work_lookup("")

    def test_rejects_non_dict_metrics(self):
        with self.assertRaises(TypeError):
            work_store("k7", "not-a-dict")


if __name__ == "__main__":
    unittest.main()
