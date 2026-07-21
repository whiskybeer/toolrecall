"""run_arm.py — run a single benchmark arm.

Usage:
    python3 bench/run_arm.py <arm> <workload> [--seed N] [--max-turns 500] [--provider PROVIDER] [--model MODEL]

Arms:
    naive      — no caching, full history each turn
    prefix     — provider prefix caching on, full history each turn
    toolrecall — TR daemon + context tracker + drop clean content

Providers:
    openrouter — OpenAI-compatible API via OpenRouter (default)
    anthropic  — Direct Anthropic API

The agent turn function is selected automatically based on the arm.
Workload is loaded from bench/workloads.py.
"""

import argparse
import os
import re
import sqlite3
import sys
import time

# Ensure bench/ is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from turnlog import TurnLogger
from probes import ProbeSet, record_probe
from agent import make_agent_turn, AgentResult, _call_llm
from workloads import load_workload

# Tunable constants
CONTEXT_LIMIT = 1_048_576   # exhaustion threshold
PROBE_INTERVAL = 25       # plant a probe every N turns


def request_tokens(messages: list[dict]) -> int:
    """Count tokens in the messages payload before sending.

    Uses tiktoken (cl100k_base) if available — consistent across all three
    arms, so the *comparison* is valid even if absolute counts differ from
    the LLM provider's native tokenizer. Falls back to char/4 estimate.
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
    except ImportError:
        total = sum(len(str(m)) for m in messages)
        return total // 4

    tokens_per_message = 3  # <|im_start|>role\ncontent<|im_end|>
    tokens_per_name = 1

    num_tokens = 0
    for msg in messages:
        num_tokens += tokens_per_message
        for key, value in msg.items():
            if isinstance(value, str):
                num_tokens += len(enc.encode(value))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("type") == "text":
                        num_tokens += len(enc.encode(item["text"]))
            if key == "name":
                num_tokens += tokens_per_name
    num_tokens += 3  # <|im_start|>assistant — for the reply
    return num_tokens


def run_arm(arm: str, workload_id: str, seed: int = 42, max_turns: int = 500,
            dry_run: bool = False, provider: str = "openrouter",
            model: str = None, delay: float = 0.0):
    """Run a single arm of the benchmark.

    Args:
        arm: 'naive', 'prefix', or 'toolrecall'
        workload_id: name from workloads.WORKLOADS
        seed: random seed for probe generation
        max_turns: max turns before forced stop
        dry_run: if True, skip LLM calls (use dummy agent)
        provider: 'openrouter' or 'anthropic'
        model: model name override
        delay: seconds to wait between turns (avoids rate limits)

    Returns:
        run_id (str)
    """
    # Load the workload
    workload = load_workload(workload_id, seed=seed)

    # Get the appropriate agent turn function
    if dry_run:
        agent_turn_fn = lambda convo, step: _dummy_turn(convo, step)
    else:
        agent_turn_fn = make_agent_turn(arm, provider=provider, model=model)

    log = TurnLogger(arm, workload.id)
    probes = ProbeSet(seed)
    convo = workload.initial_messages()
    ctx_dropped_cum = 0

    # Reset context tracker for toolrecall arm — clears pre-existing
    # read/dirty state from daemon's normal operation before benchmark
    if arm == "toolrecall":
        from toolrecall.client import context_reset
        context_reset()

    for turn in range(1, max_turns + 1):
        # Plant probe at interval
        if turn % PROBE_INTERVAL == 0:
            pid, msg = probes.plant(turn)
            convo.append({"role": "user", "content": msg})

        # Check for due probes — make a separate mini-LLM call for each
        # so the LLM answers the probe directly instead of merging into the
        # workload instruction (which LLMs ignore in favor of complex tasks).
        due_pids = probes.due(turn)
        for pid in due_pids:
            question = probes.question(pid)
            if not question:
                continue
            # Isolated call: just the probe question, no workload noise.
            # Only runs when dry_run=False to avoid wasting API calls.
            if dry_run:
                ans_text = f"[dry-run] BUILD_TOKEN_{pid} = ANSWERED"
            else:
                probe_convo = list(convo)
                probe_convo.append({"role": "user", "content": question})
                probe_resp = _call_llm(probe_convo, provider=provider, model=model, arm=arm)
                ans_text = ""
                if "error" not in probe_resp:
                    ans_text = probe_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                    # Add to main conversation for context continuity
                    convo.append({"role": "user", "content": question})
                    convo.append({"role": "assistant", "content": ans_text})
            con = sqlite3.connect(os.path.expanduser("~/.toolrecall/benchmark.db"))
            record_probe(con, log.run_id, arm, pid, probes, turn, ans_text)
            con.close()

        # Get the workload step for this turn
        step = workload.step(turn)
        if step is None:
            break  # workload finished

        # Count request tokens BEFORE sending
        req_tokens = request_tokens(convo + [step.message])

        # Check context exhaustion
        if req_tokens > CONTEXT_LIMIT:
            log.log(
                request_tokens=req_tokens,
                prompt_tokens=0,
                completion_tokens=0,
                status="context_exhausted",
            )
            print(f"  CONTEXT EXHAUSTED at turn {turn} ({req_tokens} tokens)", flush=True)
            break

        # Execute the turn
        t0 = time.time()
        result = agent_turn_fn(convo, step)
        elapsed = time.time() - t0

        ctx_dropped_cum += result.ctx_dropped_total()

        # Log error text if turn failed
        error_text = ""
        if not result.ok and result.response_text:
            error_text = result.response_text[:200]

        log.log(
            request_tokens=req_tokens,
            prompt_tokens=result.usage.get("prompt_tokens", 0),
            completion_tokens=result.usage.get("completion_tokens", 0),
            cache_read_tokens=result.usage.get("cache_read_tokens", 0),
            cache_write_tokens=result.usage.get("cache_write_tokens", 0),
            ctx_dropped_tokens_cum=ctx_dropped_cum,
            tool_calls=result.tool_calls,
            tool_cache_hits=result.tool_hits,
            tool_cache_misses=result.tool_misses,
            tool_time_ms=result.tool_time_ms,
            ttft_s=result.ttft,
            api_latency_s=elapsed,
            status="ok" if result.ok else "error",
            error=error_text or None,
        )

        convo = result.conversation

        # Print progress every 10 turns (or on error)
        if turn % 10 == 0 or not result.ok:
            pt = result.usage.get("prompt_tokens", 0)
            err_suffix = f" err={error_text[:60]}" if error_text else ""
            print(f"  turn {turn:>4} | req_tok={req_tokens:>7} | "
                  f"prov_tok={pt:>7} | ok={result.ok}{err_suffix}", flush=True)

        # Rate-limit avoidance delay
        if delay > 0 and not dry_run:
            time.sleep(delay)

    log.close()
    return log.run_id


def _dummy_turn(convo: list[dict], step) -> AgentResult:
    """Dummy agent turn for dry-run testing — no LLM call, no cost."""
    time.sleep(0.01)
    new_convo = list(convo) + [step.message]
    new_convo.append({
        "role": "assistant",
        "content": f"[dry-run] processed: {step.message.get('content', '')[:50]}..."
    })
    return AgentResult(
        usage={"prompt_tokens": 100, "completion_tokens": 20,
               "cache_read_tokens": 0, "cache_write_tokens": 0},
        conversation=new_convo,
        ok=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Run a single benchmark arm")
    parser.add_argument("arm", choices=["naive", "prefix", "toolrecall"])
    parser.add_argument("workload", nargs="?", default="bugfix",
                        help="Workload name: bugfix, feature, analysis")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-turns", type=int, default=500)
    parser.add_argument("--provider", default="openrouter",
                        choices=["openrouter", "anthropic"],
                        help="LLM provider (default: openrouter)")
    parser.add_argument("--model", default=None,
                        help="Model name override (uses provider default if unset)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip LLM calls — use dummy agent for testing")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to wait between turns (default: 0.0)")
    args = parser.parse_args()

    print(f"Arm: {args.arm}")
    print(f"Workload: {args.workload}")
    print(f"Seed: {args.seed}")
    print(f"Max turns: {args.max_turns}")
    print(f"Provider: {args.provider}")
    print(f"Model: {args.model or '(default)'}")
    print(f"Dry run: {args.dry_run}")
    print()

    run_id = run_arm(
        arm=args.arm,
        workload_id=args.workload,
        seed=args.seed,
        max_turns=args.max_turns,
        dry_run=args.dry_run,
        provider=args.provider,
        model=args.model,
        delay=args.delay,
    )
    print(f"\nRun complete: {run_id}")
    print(f"To rerun: python3 bench/run_arm.py {args.arm} {args.workload} "
          f"--seed {args.seed} --max-turns {args.max_turns} "
          f"--provider {args.provider}"
          + (f" --model {args.model}" if args.model else "")
          + (f" --delay {args.delay}" if args.delay else ""))


if __name__ == "__main__":
    main()