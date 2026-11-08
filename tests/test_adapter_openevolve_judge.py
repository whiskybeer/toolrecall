# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""OpenEvolve llm_feedback judge memo + duplicate admission guard.

The judge runs in ``Evaluator.evaluate_program``, outside the user's
``evaluate()`` — so the evaluator memo cannot reach it. These tests pin the
contract of the hook that does, against a fake ``Evaluator`` with the same
shape as openevolve's (async ``_llm_evaluate``, ``llm_ensemble.models_cfg``,
``prompt_sampler.config``). The real class is exercised by
``workspace/openevolve-bench/compat_040/probe.py`` (openevolve is not a
dependency of this repo).
"""

import asyncio
import sys
import types
import unittest
import uuid
from dataclasses import dataclass, field

from toolrecall.adapters import openevolve as oe_adapter
from toolrecall.cache import _init


@dataclass
class _Model:
    name: str = "judge-model"
    provider: str | None = None
    weight: float = 1.0
    temperature: float | None = 0.7
    top_p: float | None = None
    max_tokens: int | None = None


@dataclass
class _PromptCfg:
    evaluator_system_message: str = "evaluator_system_message"
    template_dir: str | None = None
    include_artifacts: bool = True
    max_artifact_bytes: int = 20480


@dataclass
class _Ensemble:
    models_cfg: list = field(default_factory=lambda: [_Model()])


@dataclass
class _Sampler:
    config: _PromptCfg = field(default_factory=_PromptCfg)


@dataclass
class _Result:
    metrics: dict
    artifacts: dict = field(default_factory=dict)


def _make_evaluator_class(script=None):
    """A fake Evaluator class. ``script`` = list of results, consumed per real call."""

    class FakeEvaluator:
        calls = 0
        queue = list(script) if script is not None else None

        def __init__(self):
            self.llm_ensemble = _Ensemble()
            self.prompt_sampler = _Sampler()

        async def _llm_evaluate(self, program_code, program_id=""):
            type(self).calls += 1
            if type(self).queue:
                return type(self).queue.pop(0)
            return _Result({"readability": 0.8}, {"note": "ok"})

    return FakeEvaluator


def _code() -> str:
    return f"def f():\n    return '{uuid.uuid4().hex}'\n"


def _run(coro):
    return asyncio.run(coro)


class _FrameworkType:
    """Expose a fake ``openevolve.evaluation_result.EvaluationResult`` to the adapter.

    Without openevolve installed the adapter's ``_rebuild_result`` deliberately falls
    back to a bare metrics dict (which OpenEvolve's ``_process_evaluation_result``
    also accepts). Served-vs-live *type* and artifact handling are only observable
    when the framework's class is importable, so tests that assert on them install
    this stand-in — the same seam ``test_served_result_is_rebuilt_as_the_frameworks_type``
    uses.
    """

    def __enter__(self):
        mod = types.ModuleType("openevolve.evaluation_result")
        mod.EvaluationResult = _Result  # type: ignore[attr-defined]
        self._prior = sys.modules.get("openevolve.evaluation_result")
        sys.modules["openevolve.evaluation_result"] = mod
        return self

    def __exit__(self, *exc):
        if self._prior is None:
            sys.modules.pop("openevolve.evaluation_result", None)
        else:
            sys.modules["openevolve.evaluation_result"] = self._prior


def _metrics(result):
    """Metrics of a judge result, whether it was rebuilt into an object or fell back to a dict."""
    return getattr(result, "metrics", result)


class TestJudgeMemo(unittest.TestCase):
    def setUp(self):
        _init()

    def test_default_mode_is_shadow_and_never_serves(self):
        cls = _make_evaluator_class()
        stats: dict = {}
        self.assertTrue(oe_adapter.wrap_llm_feedback(cls, enabled=True, stats=stats))
        ev, code = cls(), _code()
        _run(ev._llm_evaluate(code))
        _run(ev._llm_evaluate(code))
        self.assertEqual(cls.calls, 2, "shadow mode must pay every judge call")
        self.assertEqual(stats.get("judge_shadow_hits"), 1)
        self.assertNotIn("judge_hits", stats)

    def test_serve_mode_repeat_never_reaches_the_judge(self):
        cls = _make_evaluator_class()
        stats: dict = {}
        oe_adapter.wrap_llm_feedback(cls, mode="serve", enabled=True, stats=stats)
        ev, code = cls(), _code()
        first = _run(ev._llm_evaluate(code))
        second = _run(ev._llm_evaluate(code))
        self.assertEqual(cls.calls, 1, "identical program was judged twice")
        self.assertEqual(_metrics(first), _metrics(second))
        self.assertEqual(stats.get("judge_hits"), 1)

    def test_byte_different_program_is_always_judged(self):
        cls = _make_evaluator_class()
        oe_adapter.wrap_llm_feedback(cls, mode="serve", enabled=True)
        ev = cls()
        _run(ev._llm_evaluate(_code()))
        _run(ev._llm_evaluate(_code()))
        self.assertEqual(cls.calls, 2)

    def test_served_result_keeps_its_artifacts(self):
        with _FrameworkType():
            cls = _make_evaluator_class([_Result({"readability": 0.9}, {"why": "clear"})])
            oe_adapter.wrap_llm_feedback(cls, mode="serve", enabled=True)
            ev, code = cls(), _code()
            _run(ev._llm_evaluate(code))
            served = _run(ev._llm_evaluate(code))
        self.assertEqual(served.metrics, {"readability": 0.9})
        self.assertEqual(served.artifacts, {"why": "clear"})

    def test_served_result_is_rebuilt_as_the_frameworks_type(self):
        fake_mod = types.ModuleType("openevolve.evaluation_result")
        fake_mod.EvaluationResult = _Result  # type: ignore[attr-defined]
        prior = sys.modules.get("openevolve.evaluation_result")
        sys.modules["openevolve.evaluation_result"] = fake_mod
        try:
            cls = _make_evaluator_class()
            oe_adapter.wrap_llm_feedback(cls, mode="serve", enabled=True)
            ev, code = cls(), _code()
            _run(ev._llm_evaluate(code))
            served = _run(ev._llm_evaluate(code))
        finally:
            if prior is None:
                sys.modules.pop("openevolve.evaluation_result", None)
            else:
                sys.modules["openevolve.evaluation_result"] = prior
        self.assertIsInstance(served, _Result)

    def test_failed_judge_call_is_never_recorded(self):
        # OpenEvolve returns {} on any parse/API failure inside the judge.
        cls = _make_evaluator_class([{}, _Result({"readability": 0.7})])
        stats: dict = {}
        oe_adapter.wrap_llm_feedback(cls, mode="serve", enabled=True, stats=stats)
        ev, code = cls(), _code()
        self.assertEqual(_run(ev._llm_evaluate(code)), {})
        retry = _run(ev._llm_evaluate(code))  # must go live again, not serve {}
        self.assertEqual(cls.calls, 2)
        self.assertEqual(retry.metrics, {"readability": 0.7})
        self.assertEqual(stats.get("judge_unrecorded"), 1)
        _run(ev._llm_evaluate(code))  # now recorded → served
        self.assertEqual(cls.calls, 2)

    def test_changed_judge_identity_is_a_miss(self):
        cls = _make_evaluator_class()
        oe_adapter.wrap_llm_feedback(cls, mode="serve", enabled=True)
        code = _code()
        a = cls()
        _run(a._llm_evaluate(code))
        b = cls()
        b.llm_ensemble.models_cfg[0].temperature = 0.1
        _run(b._llm_evaluate(code))
        self.assertEqual(cls.calls, 2, "a different judge was served an old verdict")
        c = cls()
        c.prompt_sampler.config.evaluator_system_message = "other"
        _run(c._llm_evaluate(code))
        self.assertEqual(cls.calls, 3, "a different judge prompt was served an old verdict")

    def test_samples_k_pays_k_judge_calls_before_serving(self):
        cls = _make_evaluator_class()
        oe_adapter.wrap_llm_feedback(cls, mode="serve", samples=2, enabled=True)
        ev, code = cls(), _code()
        for _ in range(3):
            _run(ev._llm_evaluate(code))
        self.assertEqual(cls.calls, 2)

    def test_wrapping_is_idempotent_and_disabled_is_pass_through(self):
        cls = _make_evaluator_class()
        self.assertTrue(oe_adapter.wrap_llm_feedback(cls, mode="serve", enabled=False))
        self.assertFalse(oe_adapter.wrap_llm_feedback(cls, mode="serve", enabled=False))
        ev, code = cls(), _code()
        _run(ev._llm_evaluate(code))
        _run(ev._llm_evaluate(code))
        self.assertEqual(cls.calls, 2)

    def test_instance_target_is_supported(self):
        cls = _make_evaluator_class()
        ev = cls()
        self.assertTrue(oe_adapter.wrap_llm_feedback(ev, mode="serve", enabled=True))
        code = _code()
        _run(ev._llm_evaluate(code))
        _run(ev._llm_evaluate(code))
        self.assertEqual(cls.calls, 1)

    def test_target_none_resolves_the_class_from_sys_modules(self):
        cls = _make_evaluator_class()
        fake_mod = types.ModuleType("openevolve.evaluator")
        fake_mod.Evaluator = cls  # type: ignore[attr-defined]
        prior = sys.modules.get("openevolve.evaluator")
        sys.modules["openevolve.evaluator"] = fake_mod
        try:
            self.assertTrue(oe_adapter.wrap_llm_feedback(mode="serve", enabled=True))
        finally:
            if prior is None:
                sys.modules.pop("openevolve.evaluator", None)
            else:
                sys.modules["openevolve.evaluator"] = prior
        self.assertTrue(getattr(cls._llm_evaluate, "__toolrecall_memo__", False))

    def test_target_none_without_openevolve_loaded_does_nothing(self):
        prior = sys.modules.pop("openevolve.evaluator", None)
        try:
            self.assertFalse(oe_adapter.wrap_llm_feedback(mode="serve", enabled=True))
        finally:
            if prior is not None:
                sys.modules["openevolve.evaluator"] = prior

    def test_contradictory_arguments_raise(self):
        cls = _make_evaluator_class()
        with self.assertRaises(ValueError):
            oe_adapter.wrap_llm_feedback(
                cls, samples=1, allow_reuse=oe_adapter.never_reuse, enabled=True
            )
        with self.assertRaises(ValueError):
            oe_adapter.wrap_llm_feedback(cls, mode="bogus", enabled=True)

    def test_judge_and_evaluator_keys_do_not_collide(self):
        # same program bytes under the evaluator stage and the judge stage
        from toolrecall.keys import work_key

        code = _code().encode()
        self.assertNotEqual(
            work_key(code, stage="evaluate"),
            work_key(code, stage=oe_adapter.JUDGE_STAGE, params={"judge": {}, "user": None}),
        )


@dataclass
class _Prog:
    id: str
    code: str


@dataclass
class _Snap:
    programs: dict
    islands: tuple


class TestAdmitUnique(unittest.TestCase):
    def _snap(self, *islands):
        programs = {}
        sets = []
        for members in islands:
            ids = set()
            for pid, code in members:
                programs[pid] = _Prog(pid, code)
                ids.add(pid)
            sets.append(frozenset(ids))
        return _Snap(programs, tuple(sets))

    def test_refuses_an_exact_copy_in_the_same_island(self):
        stats: dict = {}
        admit = oe_adapter.admit_unique(stats=stats, enabled=True)
        snap = self._snap([("a", "X")], [("b", "Y")])
        self.assertFalse(admit(snap, _Prog("new", "X"), 0))
        self.assertEqual(stats["duplicates_refused"], 1)

    def test_admits_the_same_code_in_a_different_island(self):
        admit = oe_adapter.admit_unique(enabled=True)
        snap = self._snap([("a", "X")], [("b", "Y")])
        self.assertTrue(admit(snap, _Prog("new", "X"), 1))

    def test_admits_a_byte_different_candidate(self):
        admit = oe_adapter.admit_unique(enabled=True)
        snap = self._snap([("a", "X")])
        self.assertTrue(admit(snap, _Prog("new", "X "), 0))

    def test_never_refuses_the_seed_program(self):
        admit = oe_adapter.admit_unique(enabled=True)
        self.assertTrue(admit(_Snap({}, (frozenset(),)), _Prog("seed", "X"), 0))

    def test_disabled_admits_everything(self):
        admit = oe_adapter.admit_unique(enabled=False)
        snap = self._snap([("a", "X")])
        self.assertTrue(admit(snap, _Prog("new", "X"), 0))

    def test_a_broken_snapshot_admits_rather_than_breaking_the_run(self):
        admit = oe_adapter.admit_unique(enabled=True)
        self.assertTrue(admit(object(), _Prog("new", "X"), 0))

    def test_island_index_wraps_like_openevolve(self):
        admit = oe_adapter.admit_unique(enabled=True)
        snap = self._snap([("a", "X")], [("b", "Y")])
        self.assertFalse(admit(snap, _Prog("new", "Y"), 3))  # 3 % 2 == 1

    def test_does_not_refuse_itself(self):
        admit = oe_adapter.admit_unique(enabled=True)
        snap = self._snap([("a", "X")])
        self.assertTrue(admit(snap, _Prog("a", "X"), 0))


if __name__ == "__main__":
    unittest.main()
