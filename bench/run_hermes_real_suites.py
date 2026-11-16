#!/usr/bin/env python3
"""run_hermes_real_suites.py — Hermes (stateless) benchmark suites:
  1. null_qa         — Gate 1 null check (no repeats, 1 turn Q&A)
  2. unfamiliar_repo — Fresh 8-module Python repo (`ledgerflow`) with symptom-only prompts
                       and normal offset/limit reads (zero model memorization)
  3. mcp_blob        — MCP whole-blob workflow (`ci_pipeline.log`, `service_schema.json`,
                       `run_pytest`) routed through MCPBridge vs direct execution

Reuses IsolatedEnvironment, call_openrouter_via_proxy, evict_clean_files_hermes, and
load_openrouter_key from bench/run_django_endurance.py.

Usage:
    python3 bench/run_hermes_real_suites.py --dry-run
    python3 bench/run_hermes_real_suites.py --suites null_qa unfamiliar_repo mcp_blob --seeds 3
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "bench"))

import run_django_endurance as rde  # noqa: E402

RESULTS_DIR = REPO_ROOT / "bench" / "results"
DEFAULT_MODEL = rde.DEFAULT_MODEL

# ---------------------------------------------------------------------------
# Unfamiliar Repo (`ledgerflow` — 8 modules, symptom-only bugs)
# ---------------------------------------------------------------------------

LEDGERFLOW_FILES: dict[str, str] = {
    "ledgerflow/__init__.py": '"""LedgerFlow — internal multi-currency settlement engine."""\n__version__ = "0.4.2"\n',
    "ledgerflow/config.py": (
        '"""Configuration and fee schedule for LedgerFlow."""\n\n'
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True)\n"
        "class FeeSchedule:\n"
        "    base_bps: int = 25\n"
        "    fx_spread_bps: int = 40\n"
        "    min_fee_cents: int = 100\n"
        "    max_fee_cents: int = 50000\n\n"
        "    def compute_fee(self, amount_cents: int, cross_currency: bool = False) -> int:\n"
        "        if amount_cents <= 0:\n"
        "            return 0\n"
        "        bps = self.base_bps + (self.fx_spread_bps if cross_currency else 0)\n"
        "        raw = (amount_cents * bps) // 10000\n"
        "        return max(self.min_fee_cents, min(self.max_fee_cents, raw))\n\n"
        + "\n".join(f"# config_audit_rule_{i}: reserved policy hook {i}" for i in range(1, 140))
        + "\n"
    ),
    "ledgerflow/rates.py": (
        '"""FX rate table and deterministic rounding."""\n\n'
        "DEFAULT_RATES = {\n"
        '    ("USD", "USD"): 1.0,\n'
        '    ("EUR", "USD"): 1.10,\n'
        '    ("GBP", "USD"): 1.25,\n'
        '    ("JPY", "USD"): 0.0067,\n'
        "}\n\n"
        "def convert_cents(amount_cents: int, src: str, dst: str, rates: dict | None = None) -> int:\n"
        "    table = rates or DEFAULT_RATES\n"
        "    if src == dst:\n"
        "        return amount_cents\n"
        "    if (src, dst) in table:\n"
        "        # BUG_1: multiplies instead of dividing? No, (src, dst) rate is dst per src.\n"
        "        return int(round(amount_cents / table[(src, dst)]))\n"
        "    if (dst, src) in table:\n"
        "        return int(round(amount_cents / table[(dst, src)]))\n"
        "    raise KeyError(f'Missing FX pair {src}->{dst}')\n\n"
        + "\n".join(f"# fx_compliance_note_{i}: settlement precision rule {i}" for i in range(1, 140))
        + "\n"
    ),
    "ledgerflow/validator.py": (
        '"""Entry validation for LedgerFlow batches."""\n\n'
        "REQUIRED_FIELDS = ('tx_id', 'account', 'currency', 'amount_cents', 'direction')\n\n"
        "def validate_entry(entry: dict) -> list[str]:\n"
        "    errors = []\n"
        "    for k in REQUIRED_FIELDS:\n"
        "        if k not in entry or entry[k] in (None, ''):\n"
        "            errors.append(f'missing:{k}')\n"
        "    amt = entry.get('amount_cents')\n"
        "    # BUG_2: rejects valid positive ints if == 0 check inverted\n"
        "    if isinstance(amt, int) and amt >= 0:\n"
        "        errors.append('non_positive_amount')\n"
        "    direction = entry.get('direction')\n"
        "    if direction not in ('DEBIT', 'CREDIT'):\n"
        "        errors.append('invalid_direction')\n"
        "    return errors\n\n"
        + "\n".join(f"# validator_invariant_{i}: check code V{i:03d}" for i in range(1, 140))
        + "\n"
    ),
    "ledgerflow/reconciler.py": (
        '"""Netting and batch reconciliation."""\n\n'
        "from ledgerflow.config import FeeSchedule\n"
        "from ledgerflow.rates import convert_cents\n\n"
        "def net_account_balances(entries: list[dict], base_ccy: str = 'USD', fees: FeeSchedule | None = None) -> dict[str, int]:\n"
        "    sched = fees or FeeSchedule()\n"
        "    balances: dict[str, int] = {}\n"
        "    for e in entries:\n"
        "        acct = e['account']\n"
        "        ccy = e['currency']\n"
        "        usd_cents = convert_cents(e['amount_cents'], ccy, base_ccy)\n"
        "        fee = sched.compute_fee(usd_cents, cross_currency=(ccy != base_ccy))\n"
        "        # BUG_3: adds CREDIT and subtracts DEBIT inverted\n"
        "        delta = -(usd_cents - fee) if e['direction'] == 'CREDIT' else +(usd_cents + fee)\n"
        "        balances[acct] = balances.get(acct, 0) + delta\n"
        "    return balances\n\n"
        + "\n".join(f"# reconciler_rule_{i}: clearing window {i}" for i in range(1, 140))
        + "\n"
    ),
    "ledgerflow/store.py": (
        '"""In-memory idempotency journal."""\n\n'
        "class Journal:\n"
        "    def __init__(self):\n"
        "        self._seen: dict[str, dict] = {}\n\n"
        "    def record(self, entry: dict) -> bool:\n"
        "        tid = entry['tx_id']\n"
        "        # BUG_4: returns True on duplicate and False on new\n"
        "        if tid in self._seen:\n"
        "            return True\n"
        "        self._seen[tid] = dict(entry)\n"
        "        return False\n\n"
        "    def count(self) -> int:\n"
        "        return len(self._seen)\n\n"
        + "\n".join(f"# journal_checkpoint_{i}: replay marker {i}" for i in range(1, 120))
        + "\n"
    ),
    "ledgerflow/exporter.py": (
        '"""Deterministic CSV summary exporter."""\n\n'
        "def format_summary_csv(balances: dict[str, int]) -> str:\n"
        "    lines = ['account,balance_cents']\n"
        "    # BUG_5: unsorted keys breaks deterministic export\n"
        "    for acct, bal in balances.items():\n"
        "        lines.append(f'{acct},{bal}')\n"
        "    return '\\n'.join(lines)\n\n"
        + "\n".join(f"# exporter_spec_{i}: column formatting rule {i}" for i in range(1, 120))
        + "\n"
    ),
    "tests/test_ledgerflow.py": (
        "from ledgerflow.config import FeeSchedule\n"
        "from ledgerflow.exporter import format_summary_csv\n"
        "from ledgerflow.rates import convert_cents\n"
        "from ledgerflow.reconciler import net_account_balances\n"
        "from ledgerflow.store import Journal\n"
        "from ledgerflow.validator import validate_entry\n\n"
        "def test_case_1_fx_conversion():\n"
        "    assert convert_cents(10000, 'EUR', 'USD') == 11000\n"
        "    assert convert_cents(10000, 'USD', 'USD') == 10000\n\n"
        "def test_case_2_validation():\n"
        "    assert validate_entry({'tx_id': 't1', 'account': 'A', 'currency': 'USD', 'amount_cents': 500, 'direction': 'CREDIT'}) == []\n"
        "    assert 'non_positive_amount' in validate_entry({'tx_id': 't2', 'account': 'A', 'currency': 'USD', 'amount_cents': 0, 'direction': 'CREDIT'})\n\n"
        "def test_case_3_reconciler():\n"
        "    fees = FeeSchedule(base_bps=0, fx_spread_bps=0, min_fee_cents=0)\n"
        "    entries = [\n"
        "        {'tx_id': '1', 'account': 'alice', 'currency': 'USD', 'amount_cents': 1000, 'direction': 'CREDIT'},\n"
        "        {'tx_id': '2', 'account': 'alice', 'currency': 'USD', 'amount_cents': 300, 'direction': 'DEBIT'},\n"
        "    ]\n"
        "    assert net_account_balances(entries, fees=fees) == {'alice': 700}\n\n"
        "def test_case_4_journal_idempotency():\n"
        "    j = Journal()\n"
        "    assert j.record({'tx_id': 'x1'}) is True\n"
        "    assert j.record({'tx_id': 'x1'}) is False\n"
        "    assert j.count() == 1\n\n"
        "def test_case_5_sorted_csv_export():\n"
        "    out = format_summary_csv({'zoe': 10, 'amy': 20, 'bob': 5})\n"
        "    assert out == 'account,balance_cents\\namy,20\\nbob,5\\nzoe,10'\n"
    ),
}


def seed_ledgerflow(ws: Path) -> None:
    for rel, txt in LEDGERFLOW_FILES.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")


# ---------------------------------------------------------------------------
# MCP Blob Suite (large CI log + OpenAPI/config schema blobs, no offset/limit)
# ---------------------------------------------------------------------------

def seed_mcp_blob_workspace(ws: Path) -> None:
    seed_ledgerflow(ws)
    schema = {
        "service": "ledgerflow-settlement",
        "version": "2.4.0",
        "endpoints": [
            {"path": f"/v2/settlements/batch_{i}", "method": "POST", "timeout_ms": 5000, "retries": 3}
            for i in range(1, 90)
        ],
        "currencies": ["USD", "EUR", "GBP", "JPY"],
        "rules": {"credit_sign": "+", "debit_sign": "-", "sort_export_keys": True},
    }
    (ws / "service_schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
    # Initial CI log (~10 KB)
    ci_lines = [f"[2026-11-16T13:00:{i:02d}Z] INFO worker-{i%4} healthcheck OK" for i in range(1, 140)]
    ci_lines.extend([
        "=== PYTEST FAILURE SUMMARY ===",
        "FAILED tests/test_ledgerflow.py::test_case_1_fx_conversion - AssertionError: 9091 != 11000",
        "FAILED tests/test_ledgerflow.py::test_case_2_validation - AssertionError: ['non_positive_amount'] != []",
        "FAILED tests/test_ledgerflow.py::test_case_3_reconciler - AssertionError: {'alice': -700} != {'alice': 700}",
        "FAILED tests/test_ledgerflow.py::test_case_4_journal_idempotency - AssertionError: False is not True",
        "FAILED tests/test_ledgerflow.py::test_case_5_sorted_csv_export - AssertionError: unsorted rows",
    ])
    (ws / "ci_pipeline.log").write_text("\n".join(ci_lines) + "\n", encoding="utf-8")


def run_pytest_ws(ws: Path) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_ledgerflow.py", "-v", "--tb=short"],
        cwd=str(ws),
        env={**os.environ, "PYTHONPATH": str(ws)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    return p.returncode, p.stdout + p.stderr


def run_suite_instance(
    *,
    suite: str,
    arm: str,
    seed: int,
    arm_order: str,
    model: str,
    temperature: float,
    dry_run: bool,
) -> dict:
    run_tag = f"{suite}-hermes-{arm}-s{seed}"
    ws_dir = Path(tempfile.mkdtemp(prefix=f"tr-suite-{run_tag}-"))
    if suite == "unfamiliar_repo":
        seed_ledgerflow(ws_dir)
    elif suite == "mcp_blob":
        seed_mcp_blob_workspace(ws_dir)

    env_mgr = rde.IsolatedEnvironment(
        run_tag=run_tag, workspace=ws_dir, harness="hermes", arm=arm, seed=seed
    )
    t0 = time.time()
    try:
        env_mgr.start()
        if dry_run:
            return {
                "run_tag": run_tag,
                "suite": suite,
                "arm": arm,
                "arm_order": arm_order,
                "seed": seed,
                "dry_run": True,
                "wall_s": round(time.time() - t0, 2),
            }

        api_key = rde.load_openrouter_key()

        if suite == "null_qa":
            messages = [
                {"role": "system", "content": f"<!-- {run_tag} --> Answer concisely."},
                {"role": "user", "content": "In one sentence, what is the difference between a POSIX process and thread?"},
            ]
            rde.call_openrouter_via_proxy(
                proxy_port=env_mgr.proxy_port,
                api_key=api_key,
                model=model,
                messages=messages,
                tools=[],
                temperature=temperature,
            )
            time.sleep(0.3)
            u = env_mgr.read_usage_summary()
            return {
                "run_tag": run_tag,
                "suite": suite,
                "harness": "hermes",
                "arm": arm,
                "arm_order": arm_order,
                "seed": seed,
                "model": model,
                "resolved": True,
                "usage": u,
                "wall_s": round(time.time() - t0, 2),
            }

        # Build tools for unfamiliar_repo vs mcp_blob
        if suite == "unfamiliar_repo":
            tools = rde.build_openai_tools(arm, full_reads=False)
            prompts = [
                (
                    "Run `run_pytest` to inspect the 5 failing tests in `tests/test_ledgerflow.py`. "
                    "Do not modify `tests/test_ledgerflow.py`. Fix test_case_1 and test_case_2 first, "
                    "then verify with `run_pytest`."
                ),
                "Now fix test_case_3 and test_case_4, and verify with `run_pytest`.",
                "Finally fix test_case_5 and confirm all 5 tests pass with `run_pytest`.",
            ]
        else:
            # mcp_blob: MCP tools that return whole blobs without offset/limit
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "get_ci_logs",
                        "description": "Fetch full CI pipeline log blob (no line slicing).",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "get_service_schema",
                        "description": "Fetch full service_schema.json contract blob.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a source file.",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "patch",
                        "description": "Find and replace an exact string in a file.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                            },
                            "required": ["path", "old_string", "new_string"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_pytest",
                        "description": "Run pytest on tests/test_ledgerflow.py and append output to ci_pipeline.log.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                },
            ]
            prompts = [
                "Call `get_ci_logs` and `get_service_schema` to understand the service contract and failures, then fix test_case_1 and test_case_2 (do not edit tests/). Run `run_pytest` after fixing.",
                "Call `get_ci_logs` and `get_service_schema` again to verify current state, fix test_case_3 and test_case_4, and run `run_pytest`.",
                "Call `get_ci_logs` and `get_service_schema` once more, fix test_case_5, and run `run_pytest` until all 5 tests pass.",
            ]

        if env_mgr.bridge is not None:
            env_mgr.bridge.handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "context_set_checkpoint", "arguments": {}}}
            )

        messages: list[dict] = [
            {
                "role": "system",
                "content": f"<!-- {run_tag} --> You are working in `{ws_dir}`. Follow instructions and verify with `run_pytest`.",
            }
        ]
        call_seq = 10
        for step_prompt in prompts:
            messages.append({"role": "user", "content": step_prompt})
            for _ in range(12):
                resp = rde.call_openrouter_via_proxy(
                    proxy_port=env_mgr.proxy_port,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                )
                choice = resp["choices"][0]["message"]
                messages.append(choice)
                tcs = choice.get("tool_calls") or []
                if not tcs:
                    break
                for tc in tcs:
                    call_seq += 1
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        fn_args = {}
                    if fn_name == "get_ci_logs":
                        out_text, _ = rde.execute_tool(
                            name="read_file",
                            args={"path": "ci_pipeline.log"},
                            workspace=ws_dir,
                            env_mgr=env_mgr,
                            log_path=ws_dir / "ci_pipeline.log",
                            call_idx=call_seq,
                            full_reads=True,
                        )
                    elif fn_name == "get_service_schema":
                        out_text, _ = rde.execute_tool(
                            name="read_file",
                            args={"path": "service_schema.json"},
                            workspace=ws_dir,
                            env_mgr=env_mgr,
                            log_path=ws_dir / "ci_pipeline.log",
                            call_idx=call_seq,
                            full_reads=True,
                        )
                    elif fn_name == "run_pytest":
                        rc_t, out_t = run_pytest_ws(ws_dir)
                        with open(ws_dir / "ci_pipeline.log", "a", encoding="utf-8") as lfh:
                            lfh.write(f"\n--- pytest rc={rc_t} ---\n{out_t[-1500:]}\n")
                        out_text = json.dumps({"returncode": rc_t, "output": out_t})
                    else:
                        try:
                            out_text, _ = rde.execute_tool(
                                name=fn_name,
                                args=fn_args,
                                workspace=ws_dir,
                                env_mgr=env_mgr,
                                log_path=ws_dir / "ci_pipeline.log",
                                call_idx=call_seq,
                                full_reads=(suite == "mcp_blob"),
                            )
                        except KeyError:
                            out_text = json.dumps({"error": f"Tool {fn_name} failed on {fn_args}"})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out_text})
            if env_mgr.bridge is not None:
                rde.evict_clean_files_hermes(messages, env_mgr)

        time.sleep(0.3)
        final_rc, _ = run_pytest_ws(ws_dir)
        return {
            "run_tag": run_tag,
            "suite": suite,
            "harness": "hermes",
            "arm": arm,
            "arm_order": arm_order,
            "seed": seed,
            "model": model,
            "resolved": final_rc == 0,
            "usage": env_mgr.read_usage_summary(),
            "daemon_stats": env_mgr.read_daemon_stats(),
            "wall_s": round(time.time() - t0, 2),
        }
    finally:
        env_mgr.stop()
        shutil.rmtree(ws_dir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--suites",
        nargs="+",
        default=["null_qa", "unfamiliar_repo", "mcp_blob"],
        choices=["null_qa", "unfamiliar_repo", "mcp_blob"],
    )
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    results: list[dict] = []
    for k, suite in enumerate(args.suites):
        for seed in range(1, args.seeds + 1):
            arms = ["baseline", "tr_optin"] if (seed + k) % 2 else ["tr_optin", "baseline"]
            arm_order = "AB" if arms[0] == "baseline" else "BA"
            for arm in arms:
                print(f"[run] suite={suite} arm={arm} ({arm_order}) seed={seed} dry_run={args.dry_run}", flush=True)
                res = run_suite_instance(
                    suite=suite,
                    arm=arm,
                    seed=seed,
                    arm_order=arm_order,
                    model=args.model,
                    temperature=args.temperature,
                    dry_run=args.dry_run,
                )
                results.append(res)
                if not args.dry_run:
                    u = res["usage"]
                    print(
                        f"  -> resolved={res['resolved']} rows={u['rows']} pt={u['prompt_tokens']} "
                        f"peak={u['max_single_turn_pt']} cost=${u['cost_usd']:.5f} bust={u['cache_bust_tokens']}",
                        flush=True,
                    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"hermes_real_suites_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Saved results to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
