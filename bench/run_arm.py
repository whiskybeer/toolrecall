"""run_arm.py — run a single benchmark arm.

Usage:
    python3 bench/run_arm.py <arm> <workload> [--seed N] [--max-turns 500]

Arms:
    naive      — no caching, full history each turn
    prefix     — provider prefix caching on, full history each turn
    toolrecall — TR daemon + context tracker + drop clean content

The agent turn function is selected automatically based on the arm.
Workload is loaded from bench/workloads.py.
"""

import argparse
import os
import sys

# Ensure bench/ is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from turnlog import TurnLogger
from probes import ProbeSet, record_probe
from agent import make_agent_turn, AgentResult
from workloads import load_workload

# Tunable constants
CONTEXT_LIMIT = 128_000   # exhaustion threshold
PROBE_INTERVAL = 25       # plant a probe every N turns


def request_tokens(messages: list[dict]) -> int:
    """Count tokens in the messages payload before sending.

    Uses tiktoken (cl100k_base) if available — consistent across all three
    arms, so the *comparison* is valid even if absolute counts differ from
    DeepSeek's native tokenizer. Falls back to char/4 estimate.
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
            dry_run: bool = False):
    """Run a single arm of the benchmark.

    Args:
        arm: 'naive', 'prefix', or 'toolrecall'
        workload_id: name from workloads.WORKLOADS
        seed: random seed for probe generation
        max_turns: max turns before forced stop
        dry_run: if True, skip LLM calls (use dummy agent)

    Returns:
        run_id (str)
    """
    # Load the workload
    workload = load_workload(workload_id, seed=seed)

    # Get the appropriate agent turn function
    if dry_run:
        agent_turn_fn = lambda convo, step: _dummy_turn(convo, step)
    else:
        agent_turn_fn = make_agent_turn(arm)

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

        # Check for due probes — ask the LLM, don't silently record UNANSWERED
        due_pids = probes.due(turn)
        for pid in due_pids:
            question = probes.question(pid)
            if question:
                convo.append({"role": "user", "content": question})

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
        import time
        t0 = time.time()
        result = agent_turn_fn(convo, step)
        elapsed = time.time() - t0

        ctx_dropped_cum += result.ctx_dropped_total()

        # Record probe answers from the LLM response
        import re
        for pid in due_pids:
            ans_text = result.response_text or ""
            answer_match = re.search(rf"BUILD_TOKEN_{pid}\s*=\s*(\S+)", ans_text)
            if answer_match:
                ans_text = answer_match.group(1)
            import sqlite3
            con = sqlite3.connect(os.path.expanduser("~/.toolrecall/benchmark.db"))
            record_probe(con, log.run_id, arm, pid, probes, turn, ans_text)
            con.close()

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
            context_tracker_ok=getattr(result, "context_tracker_ok", True),
        )

        convo = result.conversation

        # Print progress every 50 turns
        if turn % 50 == 0:
            pt = result.usage.get("prompt_tokens", 0)
            print(f"  turn {turn:>4} | req_tok={req_tokens:>7} | "
                  f"prov_tok={pt:>7} | ok={result.ok}", flush=True)

    log.close()
    return log.run_id


def _dummy_turn(convo: list[dict], step) -> AgentResult:
    """Dummy agent turn for dry-run testing — no LLM call, no cost."""
    import time
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
    parser.add_argument("arm", choices=["naive", "toolrecall"])
    parser.add_argument("workload", nargs="?", default="bugfix",
                        help="Workload name: bugfix, feature, analysis")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-turns", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip LLM calls — use dummy agent for testing")
    args = parser.parse_args()

    print(f"Arm: {args.arm}")
    print(f"Workload: {args.workload}")
    print(f"Seed: {args.seed}")
    print(f"Max turns: {args.max_turns}")
    print(f"Dry run: {args.dry_run}")
    print()

    run_id = run_arm(
        arm=args.arm,
        workload_id=args.workload,
        seed=args.seed,
        max_turns=args.max_turns,
        dry_run=args.dry_run,
    )
    print(f"\nRun complete: {run_id}")
    print(f"To rerun: python3 bench/run_arm.py {args.arm} {args.workload} "
          f"--seed {args.seed} --max-turns {args.max_turns}")


if __name__ == "__main__":
    main()