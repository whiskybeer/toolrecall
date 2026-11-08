# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.adapters.openevolve — work-ledger memo for OpenEvolve evaluators.

OpenEvolve (and AlphaEvolve-shaped loops generally) evaluate a candidate program
by calling a user-supplied ``evaluate(program_path)`` in the eval script, once
per generated child. The child's bytes are the whole input to that call, so the
evaluation is a pure function of the candidate — and therefore reusable. This
adapter wraps the eval script's entry points so a repeat of an already-paid unit
is served from the work ledger instead of being paid for again::

    from toolrecall.adapters import openevolve as tr_oe

    tr_oe.wrap_evaluator(globals())          # inside the eval script
    # OpenEvolve imports `evaluate` from here — the wrap is invisible to it.

Duck-typed on purpose: no ``openevolve`` import appears anywhere in this file,
the framework stays an opt-in client-side dependency, and this module remains
stdlib + in-core ToolRecall. Wrapping is idempotent; calling it twice is safe.

PRIOR ART — read this before making any claim about the file
------------------------------------------------------------
Memoizing an evaluation by the identity of the candidate is **not new**:

* **Fitness caching** is a research field of its own — e.g. *Fitness Caching:
  From a Minor Mechanism to Major Evolutionary Mechanism* (IEEE 2021), *The
  influence of fitness caching on modern evolutionary computation* (GECCO 2018
  companion), and for program search specifically *Implementing a Software
  Cache for Genetic Programming* (2014), which already covers invalidation.
* **Disk-persistent, content-addressed memoization** is the standard form of
  ``joblib.Memory`` (cache a function's result on disk, invisible to the caller,
  invalidated when the function's code changes), and of ``diskcache`` /
  ``cachier``.
* **Putting the environment into a content-addressed key** is routine in build
  systems: Bazel's action cache hashes the command, the arguments, the
  ``--action_env`` variables and the input digests.
* **OpenEvolve itself** caches a derived diversity metric by ``code_hash``
  (``database.py``, in-memory LRU, per process) and carries an open issue about
  evaluating *before* the novelty check (`#439 <https://github.com/
  algorithmicsuperintelligence/openevolve/issues/439>`_).

What this adapter contributes is **placement, not mechanism**: the key comes from
ToolRecall's Layer-0 contract (``keys.work_key``) rather than being bound to a
Python function (joblib) or a build action (Bazel), so the same unit of work is
addressable from *another harness, another process, another run, another
machine*; the ledger persists and counts it under its own ``work_cache``
category, so reuse is auditable and shows up in ``top``/healthcheck beside every
other tier; and the reuse policy is an explicit parameter instead of an accident.

NOT claimed: a new primitive, the first evaluation cache for evolutionary search,
or that memoizing leaves a stochastic search untouched. The EA literature
documents that fitness caching perturbs the search — measured here as well (an
A/B with this memo flipped changed the run's best score), which is exactly why
the policy below exists.

Reuse policy
------------
``allow_reuse(attempts)`` — or the convenience ``samples=K`` — decides whether a
recalled result may be served, *before* it is served. A key is paid for K times
and served from attempt K+1 on: a deliberate re-roll is not redundancy.
**Verified for the harness we target:** OpenEvolve 0.3.2 — and ``main``, whose
relevant line numbers are identical — has no deliberate re-roll knob at all.
``num_islands`` and ``parallel_evaluations`` control breadth and concurrency, not
repetition, and each child is evaluated once, so ``samples=1`` is the honest
policy there and a repeated candidate is a regeneration, not a sample.
``samples=K`` exists for samplers that do re-roll deliberately (GEPA-style loops,
our own benches): pass K explicitly, or derive it from your own config with
``policy_from_config``, which reads a field name *you* name — it is not a claim
about any framework's field names.

Why this is not a detail: the ceiling of the whole mechanism **is** the hit rate,
and the hit rate is a property of the sampling design, not of the ledger.
Measured in a 100-iteration A/B with the memo flipped (M4, circle_packing): K=1
saved **21.8 %** of evaluator spend, K=3 fell to **12.1 %**. Serving more repeats
than the sampler's K is not "more saving", it is a different search.

The decision is **logged, never silent**: a hit is served at INFO as ``HIT``, and
a hit withheld by the policy is logged as ``WITHHELD`` with the attempt count and
the threshold, so deliberate re-rolls are visible in the run log. ``mode="shadow"``
goes further and never serves at all: every evaluation is paid, would-be hits are
counted (``shadow_hits`` in the ``stats`` dict) — the honest way to pick K from
data before any behaviour changes.

Environment dependence (documented option — deliberately NOT implemented)
------------------------------------------------------------------------
A result is only reusable if the evaluator is a function of the unit alone. A
sandbox evaluator (SWE-bench style) also depends on its environment, so the same
code can legitimately score differently. Callers have two levers today:

* ``params=<fingerprint>`` — fold the environment/eval configuration into the key
  (this is what Bazel does with ``--action_env``);
* ``ttl=<seconds>`` — bound how long a result may be served.

An *automatic* environment fingerprint is not built: it would have to decide
which parts of the environment matter, and guessing wrong yields a silently
wrong score. An evaluator that is not a pure function of the candidate must be
declared with ``impure=True``, and the adapter then refuses to wrap it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from toolrecall import client as _client
from toolrecall.keys import work_key as _work_key

__all__ = [
    "wrap_evaluator",
    "wrap_llm_feedback",
    "admit_unique",
    "reuse_after",
    "never_reuse",
    "policy_from_config",
    "MEMO_ENV_VAR",
]

#: Opt-in switch. Without it wrapping is a pass-through (no behaviour change).
MEMO_ENV_VAR = "TOOLRECALL_OPENEVOLVE_MEMO"

#: ``serve`` — a policy-approved hit is returned without re-evaluating.
#: ``shadow`` — never serve; count would-be hits so K can be chosen from data.
MODE_SERVE = "serve"
MODE_SHADOW = "shadow"
MODES = (MODE_SERVE, MODE_SHADOW)

#: Entry points an OpenEvolve eval script may expose. Only what exists is wrapped.
EVALUATOR_STAGES = (
    "evaluate",
    "evaluate_stage1",
    "evaluate_stage2",
    "evaluate_stage3",
    "evaluate_stage4",
)

log = logging.getLogger("toolrecall.adapters.openevolve")


def reuse_after(samples: int) -> Callable[[int], bool]:
    """Policy factory: pay for the first ``samples`` evaluations, then reuse.

    ``samples`` is the sampler's own K — the number of times a candidate may be
    deliberately re-rolled before a repeat is provably redundant rather than
    deliberate. ``samples=1`` means "one paid evaluation, then reuse" (the
    default policy). The threshold rides on the returned callable so the run log
    can name it when a hit is withheld.
    """
    if samples < 1:
        raise ValueError("samples must be >= 1")

    def _allow(attempts: int) -> bool:
        return attempts >= samples

    setattr(_allow, "__reuse_threshold__", samples)
    return _allow


def never_reuse(_attempts: int) -> bool:
    """Control arm: pay for every evaluation and reuse nothing.

    Use with ``mode="shadow"`` to measure the addressable hit rate without
    changing behaviour — or without it as the honest baseline of an A/B.
    """
    return False


def policy_from_config(
    config: Any, *, field: str = "num_samples", default_samples: int = 1
) -> Callable[[int], bool]:
    """Derive the reuse boundary from a sampler's own re-roll count.

    The hit rate is the ceiling of the whole mechanism, and K is a property of the
    sampling design — so read it from the config instead of guessing. ``field``
    names what YOUR harness calls it: OpenEvolve 0.3.2 exposes no such field
    (verified against the installed package and ``main``) and needs no re-roll
    allowance, because it evaluates each child once. Accepts a config object or a
    mapping; falls back to ``default_samples`` with a warning when the field is
    absent — a silent default here would quietly change which repeats are served.
    """
    samples = config.get(field) if isinstance(config, dict) else getattr(config, field, None)
    if samples is None:
        log.warning(
            "openevolve memo: no %r in config; assuming K=%d for the reuse policy",
            field,
            default_samples,
        )
        return reuse_after(default_samples)
    return reuse_after(int(samples))


def _default_allow_reuse(attempts: int) -> bool:
    return attempts >= 1


setattr(_default_allow_reuse, "__reuse_threshold__", 1)


def _bump(stats: dict[str, int] | None, name: str) -> None:
    """Count one event into a caller-owned stats dict, if one was passed."""
    if stats is not None:
        stats[name] = stats.get(name, 0) + 1


def _metrics_summary(metrics: Any, limit: int = 200) -> str:
    """Bounded, sorted rendering of a metrics dict for the audit log.

    The log is the authoritative record of what a run served, so a served value
    must stay comparable with the ledger's stored value afterwards — without
    letting an evaluator's large payload flood the log.
    """
    try:
        text = json.dumps(metrics, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(metrics)
    return text if len(text) <= limit else text[:limit] + "..."


def _program_bytes(program_path: Any) -> bytes | None:
    """Read a candidate's exact bytes — the unit of work identity is built on.

    Returns None when the candidate cannot be read; the caller then evaluates
    live rather than risk keying an entry on the wrong bytes.
    """
    if isinstance(program_path, (bytes, bytearray)):
        return bytes(program_path)
    if isinstance(program_path, (str, os.PathLike)):
        try:
            with open(program_path, "rb") as fh:
                return fh.read()
        except OSError as exc:
            log.warning("openevolve memo: cannot read candidate %s (%s)", program_path, exc)
            return None
    return None


def _wrap_one(
    real: Callable,
    stage: str,
    *,
    allow_reuse: Callable[[int], bool],
    ttl: float,
    params: Any,
    is_enabled: Callable[[], bool],
    mode: str,
    stats: dict[str, int] | None,
) -> Callable:
    """Wrap one evaluator entry point with the ledger memo.

    ``stats`` (optional) is a plain dict owned by the caller; it receives the
    counters ``hits``, ``withheld``, ``shadow_hits`` and ``evaluated``, shared by
    every wrapped stage.
    """
    threshold = getattr(allow_reuse, "__reuse_threshold__", None)

    def memoized(program_path: Any = None, *args: Any, **kwargs: Any) -> Any:
        if not is_enabled() or program_path is None:
            return real(program_path, *args, **kwargs)

        code = _program_bytes(program_path)
        if code is None:
            return real(program_path, *args, **kwargs)

        key = _work_key(code, stage=stage, params=params)
        try:
            record = _client.work_lookup(key)
        except Exception as exc:  # a memo must never break a paid run
            log.warning("openevolve memo: ledger lookup failed (%s); evaluating live", exc)
            record = None

        if record is not None:
            attempts = int(record.get("attempts", 0))
            if allow_reuse(attempts):
                if mode == MODE_SERVE:
                    _bump(stats, "hits")
                    metrics = dict(record["metrics"])
                    log.info(
                        "openevolve memo: HIT %s stage=%s attempts=%s metrics=%s",
                        key[:12],
                        stage,
                        attempts,
                        _metrics_summary(metrics),
                    )
                    return metrics
                _bump(stats, "shadow_hits")
                log.info(
                    "openevolve memo: SHADOW-HIT %s stage=%s attempts=%s metrics=%s "
                    "— not served (mode=shadow)",
                    key[:12],
                    stage,
                    attempts,
                    _metrics_summary(record["metrics"]),
                )
            else:
                _bump(stats, "withheld")
                log.info(
                    "openevolve memo: WITHHELD %s stage=%s attempts=%s (policy serves from attempt %s)",
                    key[:12],
                    stage,
                    attempts,
                    threshold if threshold is not None else "policy",
                )

        _bump(stats, "evaluated")
        started = time.perf_counter()
        result = real(program_path, *args, **kwargs)
        elapsed = time.perf_counter() - started

        if isinstance(result, dict):
            try:
                _client.work_store(key, result, eval_time=elapsed, ttl=ttl)
            except Exception as exc:  # recording is best-effort, never blocking
                log.warning("openevolve memo: ledger store failed (%s)", exc)
        else:
            log.warning(
                "openevolve memo: %s returned %s, not a metrics dict; not recorded",
                stage,
                type(result).__name__,
            )
        return result

    memoized.__name__ = getattr(real, "__name__", stage)
    memoized.__doc__ = f"ToolRecall work-ledger memo around the OpenEvolve `{stage}` evaluator."
    # Markers via setattr: mypy rejects attribute assignment on a nested function.
    setattr(memoized, "__toolrecall_memo__", True)
    setattr(memoized, "__wrapped_evaluator__", real)
    return memoized


def _namespace_get(namespace: Any, name: str) -> Any:
    """Read a member from a module-like OR dict-like namespace.

    ``wrap_evaluator(globals())`` is the documented call inside an eval script,
    and ``globals()`` is a dict — ``getattr`` would find nothing there and the
    adapter would silently do nothing. Both shapes are supported explicitly.
    """
    if isinstance(namespace, dict):
        return namespace.get(name)
    return getattr(namespace, name, None)


def _namespace_set(namespace: Any, name: str, value: Any) -> None:
    if isinstance(namespace, dict):
        namespace[name] = value
    else:
        setattr(namespace, name, value)


def wrap_evaluator(
    module: Any,
    *,
    allow_reuse: Callable[[int], bool] | None = None,
    samples: int | None = None,
    mode: str = MODE_SERVE,
    stats: dict[str, int] | None = None,
    ttl: float = 0.0,
    params: Any = None,
    impure: bool = False,
    enabled: bool | None = None,
) -> list[str]:
    """Wrap every evaluator entry point on ``module`` with a work-ledger memo.

    ``module`` is typically the eval script's ``globals()`` (or the module object
    itself) — whatever namespace OpenEvolve will pull ``evaluate`` from. Returns
    the names that were wrapped; names not present are skipped, and a name
    already wrapped by this adapter is left alone (idempotent).

    **Reuse policy** — pass exactly one of ``samples=K`` (a key is paid for K
    times, served from attempt K+1; K is a re-roll count of *your* sampler —
    OpenEvolve evaluates each child once, so K=1 there — or use
    :func:`policy_from_config` with your own field name) or ``allow_reuse`` (a
    callable of the attempt count, e.g. :func:`reuse_after` / :func:`never_reuse`).
    Passing both raises: there is no silent precedence between two answers to the
    same question. ``mode="shadow"`` never serves anything — it counts would-be
    hits into ``stats`` so K can be chosen from data before behaviour changes.

    ``enabled=None`` follows the opt-in env var (:data:`MEMO_ENV_VAR`), read per
    call so it can be flipped at runtime. ``enabled=False`` still installs the
    wrappers but passes every call straight through — no behaviour change.

    ``impure=True`` refuses to memoize (raises ``ValueError``) — see the module
    docstring on environment dependence.
    """
    if impure:
        raise ValueError(
            "openevolve memo: refusing to memoize an evaluator declared impure. "
            "An environment-dependent result would be served as if it were a "
            "function of the candidate alone. Fold the environment into `params` "
            "or pass a `ttl` instead."
        )
    if allow_reuse is not None and samples is not None:
        raise ValueError(
            "openevolve memo: pass either `samples` or `allow_reuse`, not both "
            "(two answers to the same question is one too many)"
        )
    if mode not in MODES:
        raise ValueError(f"openevolve memo: mode must be one of {MODES}, got {mode!r}")

    if allow_reuse is not None:
        allow = allow_reuse
    elif samples is not None:
        allow = reuse_after(samples)
    else:
        allow = _default_allow_reuse
    if enabled is None:
        is_enabled = lambda: bool(os.environ.get(MEMO_ENV_VAR))  # noqa: E731
    else:
        is_enabled = lambda: bool(enabled)  # noqa: E731

    wrapped: list[str] = []
    for stage in EVALUATOR_STAGES:
        real = _namespace_get(module, stage)
        if real is None or not callable(real):
            continue
        if getattr(real, "__toolrecall_memo__", False):
            continue  # already wrapped — idempotent
        wrapper = _wrap_one(
            real,
            stage,
            allow_reuse=allow,
            ttl=ttl,
            params=params,
            is_enabled=is_enabled,
            mode=mode,
            stats=stats,
        )
        _namespace_set(module, stage, wrapper)
        wrapped.append(stage)

    if not wrapped:
        log.warning(
            "openevolve memo: no evaluator entry point found (%s) — nothing wrapped; "
            "call wrap_evaluator AFTER defining evaluate() in the eval script",
            ", ".join(EVALUATOR_STAGES),
        )
    else:
        log.info("openevolve memo: wrapped %s (enabled=%s)", ", ".join(wrapped), is_enabled())
    return wrapped


# ─── llm_feedback judge ───────────────────────────────────────────────────────
#
# ``evaluator.use_llm_feedback`` makes OpenEvolve call an LLM judge for every
# child, from ``Evaluator.evaluate_program`` — *outside* the user's ``evaluate()``
# that ``wrap_evaluator`` memoizes. A memo hit on the evaluator therefore still
# bills the judge again (measured: 3 evaluations of 2 distinct candidates = 2
# evaluator runs but 3 judge calls, identically on openevolve 0.3.2 and 0.4.0;
# see workspace/openevolve-bench/compat_040/). This hook closes that gap.

#: Stage name of the judge in the work ledger (disjoint from every evaluator stage).
JUDGE_STAGE = "llm_feedback"

#: Attributes of an OpenEvolve model config that change what the judge answers.
#: ``api_base`` / ``api_key`` / ``timeout`` are deployment, not identity.
_JUDGE_MODEL_FIELDS = ("name", "provider", "weight", "temperature", "top_p", "max_tokens")
#: Attributes of the evaluator's prompt config that change the judge's prompt.
_JUDGE_PROMPT_FIELDS = (
    "evaluator_system_message",
    "template_dir",
    "include_artifacts",
    "max_artifact_bytes",
)


def _judge_identity(evaluator: Any) -> dict[str, Any]:
    """What the judge *is*, read from the live Evaluator — part of the ledger key.

    A verdict is only reusable for the same judge. Reading the model list and the
    evaluator prompt config off the instance means a changed model, temperature
    or system message can never be served an older verdict. Anything this cannot
    see (custom template files, a custom ``init_client``) must go through the
    caller's ``params=``.
    """
    ensemble = getattr(evaluator, "llm_ensemble", None)
    models = []
    for cfg in getattr(ensemble, "models_cfg", None) or []:
        models.append({f: getattr(cfg, f, None) for f in _JUDGE_MODEL_FIELDS})
    sampler_cfg = getattr(getattr(evaluator, "prompt_sampler", None), "config", None)
    prompt = {f: getattr(sampler_cfg, f, None) for f in _JUDGE_PROMPT_FIELDS}
    return {"models": models, "prompt": prompt}


def _result_parts(result: Any) -> tuple[dict, dict] | None:
    """Split a judge result into ``(metrics, artifacts)``, or None if unusable.

    OpenEvolve returns ``{}`` for any parse / API failure inside the judge. That
    is a *failed attempt*, not an answer: recording it would freeze one transient
    error as the permanent verdict for that program.
    """
    metrics = getattr(result, "metrics", result)
    artifacts = getattr(result, "artifacts", None) or {}
    if not isinstance(metrics, dict) or not metrics:
        return None
    if not isinstance(artifacts, dict):
        return None
    return metrics, artifacts


def _rebuild_result(metrics: dict, artifacts: dict) -> Any:
    """Re-create what the judge originally returned (an ``EvaluationResult``).

    Looked up in ``sys.modules`` rather than imported: this module never imports
    openevolve. Falls back to the bare metrics dict, which OpenEvolve's
    ``_process_evaluation_result`` accepts as well.
    """
    import sys

    module = sys.modules.get("openevolve.evaluation_result")
    cls = getattr(module, "EvaluationResult", None)
    if cls is None:
        return dict(metrics)
    return cls(metrics=dict(metrics), artifacts=dict(artifacts))


def wrap_llm_feedback(
    target: Any = None,
    *,
    allow_reuse: Callable[[int], bool] | None = None,
    samples: int | None = None,
    mode: str = MODE_SHADOW,
    stats: dict[str, int] | None = None,
    ttl: float = 0.0,
    params: Any = None,
    enabled: bool | None = None,
) -> bool:
    """Memoize OpenEvolve's ``llm_feedback`` judge on the work ledger.

    Call it from the eval script, next to :func:`wrap_evaluator`. The eval script
    is imported by ``Evaluator.__init__`` inside each worker process, so by then
    ``openevolve.evaluator`` is already in ``sys.modules`` and ``target=None``
    patches ``Evaluator._llm_evaluate`` on the class (found there, not imported —
    the adapter keeps no openevolve import). Pass a class or an instance to patch
    that one explicitly. Returns True when something was wrapped; idempotent.

    **Default is ``mode="shadow"`` — deliberately not ``serve``.** The judge is a
    *measurement* by a stochastic model, the same kind of stage the serving
    policy never serves (a served timing stage froze a 22.3x "speedup" for a real
    1.97x). Shadow mode pays every judge call and counts the would-be hits in
    ``stats["judge_shadow_hits"]`` — the data to decide; ``mode="serve"`` is an explicit
    opt-in, and ``samples=K`` then keeps the first K verdicts paid (a K-sample
    judge average is a better measurement than one).

    The key is the program bytes + the judge's identity (models, weights,
    temperature, evaluator prompt config — see :func:`_judge_identity`) + the
    caller's ``params``. A failed judge call (OpenEvolve returns ``{}``) is never
    recorded.
    """
    if allow_reuse is not None and samples is not None:
        raise ValueError(
            "openevolve memo: pass either `samples` or `allow_reuse`, not both "
            "(two answers to the same question is one too many)"
        )
    if mode not in MODES:
        raise ValueError(f"openevolve memo: mode must be one of {MODES}, got {mode!r}")
    allow = (
        allow_reuse
        if allow_reuse is not None
        else reuse_after(samples)
        if samples is not None
        else _default_allow_reuse
    )
    threshold = getattr(allow, "__reuse_threshold__", None)
    if enabled is None:
        is_enabled: Callable[[], bool] = lambda: bool(os.environ.get(MEMO_ENV_VAR))  # noqa: E731
    else:
        is_enabled = lambda: bool(enabled)  # noqa: E731

    if target is None:
        import sys

        module = sys.modules.get("openevolve.evaluator")
        target = getattr(module, "Evaluator", None)
        if target is None:
            log.warning(
                "openevolve memo: openevolve.evaluator is not loaded — llm_feedback "
                "not wrapped; call wrap_llm_feedback from the eval script (imported by "
                "the Evaluator) or pass the Evaluator class/instance explicitly"
            )
            return False
    real = getattr(target, "_llm_evaluate", None)
    if real is None or not callable(real):
        log.warning("openevolve memo: %r has no _llm_evaluate — llm_feedback not wrapped", target)
        return False
    if getattr(real, "__toolrecall_memo__", False):
        return False  # already wrapped — idempotent

    is_class = isinstance(target, type)

    async def _memoized(evaluator: Any, call_real: Callable, program_code: Any, *a, **kw) -> Any:
        if not is_enabled() or not isinstance(program_code, str):
            return await call_real(program_code, *a, **kw)
        code = program_code.encode("utf-8")
        key = _work_key(
            code,
            stage=JUDGE_STAGE,
            params={"judge": _judge_identity(evaluator), "user": params},
        )
        try:
            record = _client.work_lookup(key)
        except Exception as exc:  # a memo must never break a paid run
            log.warning("openevolve memo: judge ledger lookup failed (%s); judging live", exc)
            record = None

        if record is not None:
            attempts = int(record.get("attempts", 0))
            stored = record.get("metrics") or {}
            if allow(attempts) and isinstance(stored.get("metrics"), dict):
                if mode == MODE_SERVE:
                    _bump(stats, "judge_hits")
                    log.info(
                        "openevolve memo: HIT %s stage=%s attempts=%s metrics=%s",
                        key[:12],
                        JUDGE_STAGE,
                        attempts,
                        _metrics_summary(stored["metrics"]),
                    )
                    return _rebuild_result(stored["metrics"], stored.get("artifacts") or {})
                _bump(stats, "judge_shadow_hits")
                log.info(
                    "openevolve memo: SHADOW-HIT %s stage=%s attempts=%s metrics=%s "
                    "— not served (mode=shadow)",
                    key[:12],
                    JUDGE_STAGE,
                    attempts,
                    _metrics_summary(stored["metrics"]),
                )
            elif not allow(attempts):
                _bump(stats, "judge_withheld")
                log.info(
                    "openevolve memo: WITHHELD %s stage=%s attempts=%s "
                    "(policy serves from attempt %s)",
                    key[:12],
                    JUDGE_STAGE,
                    attempts,
                    threshold if threshold is not None else "policy",
                )

        _bump(stats, "judge_calls")
        started = time.perf_counter()
        result = await call_real(program_code, *a, **kw)
        elapsed = time.perf_counter() - started
        parts = _result_parts(result)
        if parts is None:
            _bump(stats, "judge_unrecorded")
            log.warning(
                "openevolve memo: judge returned no usable metrics for %s; not recorded", key[:12]
            )
            return result
        try:
            _client.work_store(
                key, {"metrics": parts[0], "artifacts": parts[1]}, eval_time=elapsed, ttl=ttl
            )
        except Exception as exc:  # recording is best-effort, never blocking
            log.warning("openevolve memo: judge ledger store failed (%s)", exc)
        return result

    if is_class:

        async def memoized(self: Any, program_code: Any = None, *a: Any, **kw: Any) -> Any:
            async def call_real(code: Any, *ra: Any, **rkw: Any) -> Any:
                return await real(self, code, *ra, **rkw)

            return await _memoized(self, call_real, program_code, *a, **kw)

    else:

        async def memoized(program_code: Any = None, *a: Any, **kw: Any) -> Any:  # type: ignore[misc]
            return await _memoized(target, real, program_code, *a, **kw)

    memoized.__name__ = getattr(real, "__name__", "_llm_evaluate")
    memoized.__doc__ = "ToolRecall work-ledger memo around OpenEvolve's `_llm_evaluate` judge."
    setattr(memoized, "__toolrecall_memo__", True)
    setattr(memoized, "__wrapped_evaluator__", real)
    setattr(target, "_llm_evaluate", memoized)
    log.info(
        "openevolve memo: wrapped llm_feedback judge (mode=%s, enabled=%s)", mode, is_enabled()
    )
    return True


# ─── duplicate admission guard ────────────────────────────────────────────────


def admit_unique(
    *, stats: dict[str, int] | None = None, enabled: bool | None = None
) -> Callable[[Any, Any, int], bool]:
    """``population_strategy.admit`` hook: refuse an exact copy of an island member.

    OpenEvolve 0.4.0 filters near-duplicates with an embedding plus an LLM
    "is this novel?" judge — both inside ``ProgramDatabase.add``, i.e. *after* the
    child was evaluated. A byte-identical repeat has cosine similarity 1.0, so it
    always reaches the judge, which is billed on the **mutation ensemble**. The
    ``admit`` hook runs just before that check, so refusing the exact copy there
    skips the embedding call and the judge call::

        from openevolve.population import PopulationStrategy

        strategy = PopulationStrategy(admit=tr_oe.admit_unique(stats=stats))
        OpenEvolve(..., population_strategy=strategy)

    Duck-typed on the snapshot (``programs``, ``islands``) and candidate (``id``,
    ``code``): no openevolve import. Comparison is exact string equality *within
    the target island* — the same notion of "duplicate" ``ProgramDatabase``
    itself uses for migrants. It deliberately does **not** consult the work
    ledger: a program evaluated in some earlier run is not a member of this
    population, and refusing it would starve the search.

    This changes what enters the population (duplicates stop being added), so it
    is not byte-for-byte the same search as without it — an opt-in with a
    measurable effect, not a free lunch. It saves nothing when
    ``database.embedding_model`` is unset (there is then no novelty check to
    skip). The seed program is never refused.
    """
    is_enabled: Callable[[], bool]
    if enabled is None:
        is_enabled = lambda: bool(os.environ.get(MEMO_ENV_VAR))  # noqa: E731
    else:
        is_enabled = lambda: bool(enabled)  # noqa: E731

    def admit(snapshot: Any, candidate: Any, island_idx: int) -> bool:
        if not is_enabled():
            return True
        try:
            programs = snapshot.programs
            if not programs:
                return True  # the seed program: OpenEvolve forbids refusing it
            members = snapshot.islands[island_idx % len(snapshot.islands)]
            code = candidate.code
            for pid in members:
                if pid == candidate.id:
                    continue
                other = programs.get(pid)
                if other is not None and other.code == code:
                    _bump(stats, "duplicates_refused")
                    log.info(
                        "openevolve memo: DUPLICATE refused island=%s candidate=%s same as %s "
                        "(embedding + novelty judge skipped)",
                        island_idx,
                        candidate.id,
                        pid,
                    )
                    return False
        except Exception as exc:  # a guard must never break a paid run
            log.warning("openevolve memo: admit_unique failed (%s); admitting", exc)
        return True

    return admit
