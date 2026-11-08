# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""OpenEvolve evaluator memo — invisible when off, provably sparing when on.

The contract under test is the one that makes the adapter shippable: with the
memo disabled nothing about the evaluator's behaviour changes, and with it
enabled a repeat of an already-paid unit never reaches the real evaluator
(asserted with a call counter inside a fake evaluator), while a byte-different
candidate always does.
"""

import importlib.util
import os
import shutil
import tempfile
import time
import types
import unittest

from toolrecall.adapters import openevolve as oe_adapter
from toolrecall.cache import _init


class _FakeEval:
    """A deterministic evaluator namespace with a per-stage call counter."""

    def __init__(self, result=None):
        self.calls = {"evaluate": 0, "evaluate_stage1": 0}
        self._result = result

    def as_module(self):
        module = types.ModuleType("fake_openevolve_eval")

        def evaluate(program_path, **kwargs):
            with open(program_path) as fh:
                head = fh.read(16)
            self.calls["evaluate"] += 1
            if self._result is not None:
                return self._result
            return {"combined_score": 0.5, "head": head}

        def evaluate_stage1(program_path, **kwargs):
            self.calls["evaluate_stage1"] += 1
            return {"combined_score": 0.5}

        module.evaluate = evaluate
        module.evaluate_stage1 = evaluate_stage1
        return module


class TestOpenEvolveMemo(unittest.TestCase):
    def setUp(self):
        _init()  # ledger tables (house pattern, cf. tests/test_work_ledger.py)
        self.tmp = tempfile.mkdtemp()
        self.prog = os.path.join(self.tmp, "cand.py")
        self.write_program("def solve():\n    return 1\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_program(self, text: str) -> None:
        with open(self.prog, "w") as fh:
            fh.write(text)

    # ── the two claims that matter ───────────────────────────────

    def test_second_identical_call_never_reaches_the_evaluator(self):
        ev = _FakeEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True)

        first = module.evaluate(self.prog)
        second = module.evaluate(self.prog)

        self.assertEqual(ev.calls["evaluate"], 1, "identical candidate was re-evaluated")
        self.assertEqual(first, second)

    def test_byte_different_candidate_is_always_evaluated(self):
        ev = _FakeEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True)

        module.evaluate(self.prog)
        self.write_program("def solve():\n    return 2\n")
        module.evaluate(self.prog)

        self.assertEqual(ev.calls["evaluate"], 2)

    def test_documented_integration_shape_with_a_real_eval_script(self):
        """OpenEvolve imports the eval file as a module and calls
        ``module.evaluate(program_path)`` positionally (``evaluator.py``: imports
        the file, then ``run_in_executor(None, module.evaluate, program_path)``).
        This drives exactly that shape: the script wraps its own ``globals()``.
        """
        script = os.path.join(self.tmp, "openEvolve_eval.py")
        with open(script, "w") as fh:
            fh.write(
                "from toolrecall.adapters import openevolve as tr_oe\n"
                "CALLS = {'n': 0}\n"
                "\n"
                "def evaluate(program_path):\n"
                "    CALLS['n'] += 1\n"
                "    with open(program_path) as fh:\n"
                "        head = fh.read(16)\n"
                "    return {'combined_score': 0.5, 'head': head}\n"
                "\n"
                "tr_oe.wrap_evaluator(globals(), enabled=True)\n"
            )

        spec = importlib.util.spec_from_file_location("openEvolve_eval", script)
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)

        self.assertTrue(getattr(loaded.evaluate, "__toolrecall_memo__", False))
        first = loaded.evaluate(self.prog)
        second = loaded.evaluate(self.prog)

        self.assertEqual(loaded.CALLS["n"], 1, "the import path lost the memo")
        self.assertEqual(first, second)

    # ── pass-through and refusals ────────────────────────────────

    def test_disabled_wrapper_is_a_pure_pass_through(self):
        ev = _FakeEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=False)

        module.evaluate(self.prog)
        module.evaluate(self.prog)

        self.assertEqual(ev.calls["evaluate"], 2, "disabled memo changed the call pattern")

    def test_env_var_gates_the_memo(self):
        ev = _FakeEval()
        module = ev.as_module()
        os.environ.pop(oe_adapter.MEMO_ENV_VAR, None)
        oe_adapter.wrap_evaluator(module)  # enabled=None → follows the env var

        module.evaluate(self.prog)  # memo off: evaluates, records nothing
        self.assertEqual(ev.calls["evaluate"], 1)

        os.environ[oe_adapter.MEMO_ENV_VAR] = "1"
        try:
            module.evaluate(self.prog)  # now on: miss → evaluates + records
            module.evaluate(self.prog)  # hit → served
        finally:
            os.environ.pop(oe_adapter.MEMO_ENV_VAR, None)

        self.assertEqual(ev.calls["evaluate"], 2, "env var must be read per call, not at wrap time")

    def test_impure_evaluator_is_refused(self):
        ev = _FakeEval()
        module = ev.as_module()
        with self.assertRaises(ValueError):
            oe_adapter.wrap_evaluator(module, enabled=True, impure=True)

    def test_reuse_after_k_keeps_deliberate_rolls_paid(self):
        ev = _FakeEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True, allow_reuse=oe_adapter.reuse_after(2))

        module.evaluate(self.prog)  # paid 1
        module.evaluate(self.prog)  # paid 2 — deliberate re-roll stays paid
        module.evaluate(self.prog)  # served

        self.assertEqual(ev.calls["evaluate"], 2)

    def test_reuse_after_rejects_zero(self):
        with self.assertRaises(ValueError):
            oe_adapter.reuse_after(0)

    # ── key composition ──────────────────────────────────────────

    def test_stages_are_keyed_separately(self):
        ev = _FakeEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True)

        module.evaluate(self.prog)
        module.evaluate_stage1(self.prog)
        module.evaluate(self.prog)
        module.evaluate_stage1(self.prog)

        self.assertEqual(ev.calls["evaluate"], 1)
        self.assertEqual(ev.calls["evaluate_stage1"], 1)

    def test_params_fingerprint_separates_environments(self):
        """Same bytes, different eval params → different unit (the env story)."""
        plain = _FakeEval()
        plain_module = plain.as_module()
        oe_adapter.wrap_evaluator(plain_module, enabled=True, params={"eval": "v1"})
        plain_module.evaluate(self.prog)

        other = _FakeEval()
        other_module = other.as_module()
        oe_adapter.wrap_evaluator(other_module, enabled=True, params={"eval": "v2"})
        other_module.evaluate(self.prog)

        self.assertEqual(plain.calls["evaluate"], 1)
        self.assertEqual(other.calls["evaluate"], 1, "params must participate in the key")

    def test_unreadable_candidate_falls_through(self):
        ev = _FakeEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True)

        with self.assertRaises(FileNotFoundError):
            module.evaluate(os.path.join(self.tmp, "missing.py"))
        self.assertEqual(ev.calls["evaluate"], 0)
        self.assertEqual(module.evaluate(self.prog)["combined_score"], 0.5)

    def test_non_dict_result_is_returned_but_not_recorded(self):
        ev = _FakeEval(result=0.75)
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True)

        self.assertEqual(module.evaluate(self.prog), 0.75)
        self.assertEqual(module.evaluate(self.prog), 0.75)
        self.assertEqual(ev.calls["evaluate"], 2, "non-dict results are not reusable")

    # ── ttl and idempotence ──────────────────────────────────────

    def test_ttl_expiry_re_evaluates(self):
        ev = _FakeEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True, ttl=0.05)

        module.evaluate(self.prog)
        module.evaluate(self.prog)
        self.assertEqual(ev.calls["evaluate"], 1)
        time.sleep(0.08)
        module.evaluate(self.prog)
        self.assertEqual(ev.calls["evaluate"], 2)

    def test_wrapping_twice_is_idempotent(self):
        ev = _FakeEval()
        module = ev.as_module()

        first = oe_adapter.wrap_evaluator(module, enabled=True)
        second = oe_adapter.wrap_evaluator(module, enabled=True)

        self.assertEqual(first, ["evaluate", "evaluate_stage1"])
        self.assertEqual(second, [])
        module.evaluate(self.prog)
        module.evaluate(self.prog)
        self.assertEqual(ev.calls["evaluate"], 1)

    def test_absent_entry_points_are_skipped(self):
        module = types.ModuleType("no_evaluators")
        self.assertEqual(oe_adapter.wrap_evaluator(module, enabled=True), [])


def _score_for_text(text: str) -> float:
    """Deterministic score derived from the candidate's text.

    Deriving the score from the content is what gives the score-identity gate
    teeth: a wrong key cannot accidentally produce the same number.
    """
    return round((sum(text.encode()) % 997) / 997.0, 6)


class _ContentScoredEval:
    """Evaluator whose score is a function of the candidate's content."""

    def __init__(self):
        self.calls = {"evaluate": 0}

    def expected_score(self, text: str) -> float:
        return _score_for_text(text)

    def as_module(self):
        module = types.ModuleType("fake_content_scored_eval")

        def evaluate(program_path, **kwargs):
            with open(program_path) as fh:
                text = fh.read()
            self.calls["evaluate"] += 1
            return {"combined_score": _score_for_text(text)}

        module.evaluate = evaluate
        return module


class TestOpenEvolveReusePolicy(unittest.TestCase):
    """The honest part: when a repeat may be served, and whether serving it
    returns the same score the evaluator produced."""

    def setUp(self):
        _init()
        self.tmp = tempfile.mkdtemp()
        self.prog = os.path.join(self.tmp, "cand.py")
        self.prog_b = os.path.join(self.tmp, "cand_b.py")
        self.text = "def solve():\n    return 41\n"
        self.text_b = "def solve():\n    return 42\n"
        for path, text in ((self.prog, self.text), (self.prog_b, self.text_b)):
            with open(path, "w") as fh:
                fh.write(text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_samples_three_pays_three_attempts_then_serves(self):
        ev = _ContentScoredEval()
        module = ev.as_module()
        stats: dict[str, int] = {}
        oe_adapter.wrap_evaluator(module, enabled=True, samples=3, stats=stats)

        results = [module.evaluate(self.prog) for _ in range(6)]

        self.assertEqual(ev.calls["evaluate"], 3, "attempts 1-3 must be paid")
        self.assertEqual(stats["withheld"], 2)
        self.assertEqual(stats["hits"], 3, "attempts 4-6 must be served")
        # per-key score identity: every served score equals the paid score
        expected = ev.expected_score(self.text)
        for result in results:
            self.assertEqual(result["combined_score"], expected)

    def test_served_score_never_bleeds_across_candidates(self):
        ev = _ContentScoredEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True)

        first_a = module.evaluate(self.prog)
        first_b = module.evaluate(self.prog_b)
        served_a = module.evaluate(self.prog)
        served_b = module.evaluate(self.prog_b)

        self.assertEqual(first_a["combined_score"], ev.expected_score(self.text))
        self.assertEqual(first_b["combined_score"], ev.expected_score(self.text_b))
        self.assertNotEqual(first_a["combined_score"], first_b["combined_score"])
        self.assertEqual(served_a, first_a)
        self.assertEqual(served_b, first_b)
        self.assertEqual(ev.calls["evaluate"], 2)

    def test_shadow_mode_never_serves_but_counts_would_be_hits(self):
        ev = _ContentScoredEval()
        module = ev.as_module()
        stats: dict[str, int] = {}
        oe_adapter.wrap_evaluator(
            module, enabled=True, samples=1, mode=oe_adapter.MODE_SHADOW, stats=stats
        )

        for _ in range(3):
            module.evaluate(self.prog)

        self.assertEqual(ev.calls["evaluate"], 3, "shadow mode must pay for everything")
        self.assertEqual(stats["shadow_hits"], 2, "calls 2 and 3 would have been served")
        self.assertEqual(stats["evaluated"], 3)
        self.assertNotIn("hits", stats)

    def test_withheld_hit_is_logged_with_the_threshold(self):
        ev = _ContentScoredEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True, samples=3)

        module.evaluate(self.prog)
        with self.assertLogs("toolrecall.adapters.openevolve", level="INFO") as captured:
            module.evaluate(self.prog)

        self.assertTrue(
            any("WITHHELD" in line and "attempt 3" in line for line in captured.output),
            captured.output,
        )

    def test_samples_and_allow_reuse_together_are_rejected(self):
        module = _ContentScoredEval().as_module()
        with self.assertRaises(ValueError):
            oe_adapter.wrap_evaluator(
                module, enabled=True, samples=2, allow_reuse=oe_adapter.never_reuse
            )

    def test_unknown_mode_is_rejected(self):
        module = _ContentScoredEval().as_module()
        with self.assertRaises(ValueError):
            oe_adapter.wrap_evaluator(module, enabled=True, mode="serve_everything")

    def test_never_reuse_pays_for_every_call(self):
        ev = _ContentScoredEval()
        module = ev.as_module()
        stats: dict[str, int] = {}
        oe_adapter.wrap_evaluator(
            module, enabled=True, allow_reuse=oe_adapter.never_reuse, stats=stats
        )

        module.evaluate(self.prog)
        module.evaluate(self.prog)

        self.assertEqual(ev.calls["evaluate"], 2)
        self.assertEqual(stats.get("hits", 0), 0)
        self.assertEqual(stats["withheld"], 1)

    def test_served_hit_logs_the_metrics_it_returned(self):
        ev = _ContentScoredEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True)

        module.evaluate(self.prog)
        with self.assertLogs("toolrecall.adapters.openevolve", level="INFO") as captured:
            module.evaluate(self.prog)

        expected = ev.expected_score(self.text)
        self.assertTrue(
            any("metrics=" in line and str(expected) in line for line in captured.output),
            captured.output,
        )

    def test_shadow_hit_logs_the_metrics_it_withheld(self):
        ev = _ContentScoredEval()
        module = ev.as_module()
        oe_adapter.wrap_evaluator(module, enabled=True, mode=oe_adapter.MODE_SHADOW, samples=1)

        module.evaluate(self.prog)
        with self.assertLogs("toolrecall.adapters.openevolve", level="INFO") as captured:
            module.evaluate(self.prog)

        expected = ev.expected_score(self.text)
        self.assertTrue(
            any(
                "SHADOW-HIT" in line and "metrics=" in line and str(expected) in line
                for line in captured.output
            ),
            captured.output,
        )

    def test_policy_from_config_reads_the_named_field(self):
        self.assertEqual(oe_adapter.policy_from_config({"num_samples": 3})(3), True)
        self.assertEqual(oe_adapter.policy_from_config({"num_samples": 3})(2), False)
        self.assertEqual(
            oe_adapter.policy_from_config({"reuse_samples": 5}, field="reuse_samples")(5), True
        )
        self.assertEqual(
            oe_adapter.policy_from_config({"reuse_samples": 5}, field="reuse_samples")(4), False
        )

        class _Config:
            num_samples = 4

        self.assertEqual(oe_adapter.policy_from_config(_Config())(4), True)
        self.assertEqual(oe_adapter.policy_from_config(_Config())(3), False)

    def test_policy_from_config_warns_and_falls_back(self):
        with self.assertLogs("toolrecall.adapters.openevolve", level="WARNING") as captured:
            policy = oe_adapter.policy_from_config({}, default_samples=2)

        self.assertEqual(policy(2), True)
        self.assertEqual(policy(1), False)
        self.assertTrue(any("no 'num_samples' in config" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
