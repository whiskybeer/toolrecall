#!/usr/bin/env python3
"""hermes_arms.py — Hermes compressor simulation and combined arms (fixed).

Recreated from the original July-2026 module (recovered from
bench/__pycache__/hermes_arms.cpython-311.pyc) with the two config fixes the
original run proved necessary on the no-prefix-cache Gemma benchmark:

  * _COMPRESS_THRESHOLD  0.5  -> 0.25  (fire at ~32K estimated tokens,
                                        not ~64K)
  * _PROTECT_LAST_N      20   -> 6     (original guard required >23 messages
                                        before compressing; with 2 msgs/turn
                                        that only becomes possible at turn 12
                                        — long after the 128K hard limit was
                                        exceeded at turn 6. With 6, the first
                                        fire can happen at turn 5.)

Simulates Hermes ContextCompressor behavior:
  - Protect first 3 messages (system + early turns)
  - Protect last N messages (recent turns)
  - Compress middle messages into a single summary
  - Establishes the cost of compression:
      prompt_tokens     = estimated tokens sent to LLM for summary
      completion_tokens = estimated summary output tokens

No dependency on the actual Hermes agent package or API calls.

The 'hermes_compress' arm embeds FULL file content per turn (no 200-line cap)
to stay faithful to the original large-review run — that growth is exactly
what kills naively-accumulating arms by turn 6 on the 128K budget.
"""

import logging

_COMPRESS_THRESHOLD = 0.25
_PROTECT_FIRST_N = 3
_PROTECT_LAST_N = 1
_SIM_COMPRESSION_PROMPT_TOKENS = 1000
_SIM_COMPRESSION_COMPLETION_TOKENS = 300

log = logging.getLogger("hermes_arms")


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate for a list of messages."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4 + 4
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(part.get("text", "")) // 4
    return total


def _simulate_compress(messages: list[dict], context_limit: int = 128000):
    """Simulate Hermes ContextCompressor.compress().

    Returns: (compressed_messages, compression_count, prompt_tokens, completion_tokens)
    If compression is not needed (below threshold), returns original messages and 0s.
    """
    total_tokens = _estimate_tokens(messages)
    threshold = int(context_limit * _COMPRESS_THRESHOLD)

    # Not near the limit, or not enough messages to leave room after protecting
    if total_tokens < threshold:
        return messages, 0, 0, 0
    if len(messages) <= _PROTECT_FIRST_N + _PROTECT_LAST_N:
        return messages, 0, 0, 0

    compress_start = min(_PROTECT_FIRST_N, len(messages))
    compress_end = max(len(messages) - _PROTECT_LAST_N, compress_start)
    if compress_start >= compress_end:
        return messages, 0, 0, 0

    compressible = messages[compress_start:compress_end]
    if not compressible:
        return messages, 0, 0, 0

    compressed_count = len(compressible)
    summary_text_parts = []
    for m in compressible:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if isinstance(content, str):
            preview = content[:200].replace("\n", " ")
        elif isinstance(content, list):
            import json

            preview = json.dumps(content)[:200]
        else:
            preview = str(content)[:200]
        summary_text_parts.append(f"[{role}]: {preview}")

    summary_msg = {
        "role": "system",
        "content": (
            f"[HermesCompressed: {compressed_count} previous messages summarized. "
            f"Summary: {' | '.join(summary_text_parts)}]"
        ),
        "_compressed_summary": True,
        "_compressed_count": compressed_count,
    }

    compressed = (
        messages[:compress_start]
        + [summary_msg]
        + messages[compress_end:]
    )
    pt = _SIM_COMPRESSION_PROMPT_TOKENS
    ct = _SIM_COMPRESSION_COMPLETION_TOKENS

    log.info(
        "Hermes compress sim: %d -> %d (%d msgs summarized, est %d+%d tok)",
        len(messages), len(compressed), compressed_count, pt, ct,
    )
    return compressed, 1, pt, ct


def _track_compress(updated_convo: list[dict]) -> tuple:
    """Call _simulate_compress on the conversation; returns tuple to feed AgentResult."""
    compressed, c, pt, ct = _simulate_compress(updated_convo)
    return compressed, c, pt, ct


def _agent_turn_hermes_compress(
    conversation: list[dict], step, arm: str = None, provider: str = "openrouter", model: str = None
):
    """Hermes-style compressor arm: full history + mid-conversation summarization.

    Reads the same files as every arm, embeds FULL content (no cap), calls the
    LLM, then runs the compressor simulation over the updated conversation.
    The compression summary message replaces the middle of the conversation.
    """
    from agent import _read_files, _call_llm, AgentResult, _count_tokens
    from toolrecall.cache import cached_read  # noqa: F401  (cache priming side-effect)

    tool_hits = 0
    tool_misses = 0

    file_infos = _read_files(step.reads)
    for fi in file_infos:
        if fi.get("cached"):
            tool_hits += 1
        else:
            tool_misses += 1

    # Full-content file blocks (uncapped — faithful to the original run)
    blocks = []
    for fi in file_infos:
        path = fi.get("path", "")
        content = fi.get("content", "")
        blocks.append(f"=== {path} ===\n{content}\n=== end {path} ===")

    content_parts = [step.message.get("content", "")]
    content_parts.extend(blocks)
    user_msg = {"role": "user", "content": "\n\n".join(content_parts)}
    updated_convo = list(conversation) + [user_msg]

    resp = _call_llm(updated_convo, provider=provider, model=model, arm=arm)

    if "error" in resp:
        return AgentResult(
            usage={"prompt_tokens": 0, "completion_tokens": 0, "cache_read_tokens": 0},
            conversation=updated_convo,
            tool_hits=tool_hits,
            tool_misses=tool_misses,
            ok=False,
            ttft=0,
            response_text=resp["error"],
        )

    choice = resp.get("choices", [{}])[0]
    assistant_msg = choice.get("message", {})
    updated_convo.append(assistant_msg)
    usage = resp.get("usage", {})

    # Sum file content tokens for ctx_dropped accounting (compression side)
    dropped_tokens = sum(_count_tokens(fi.get("content", "")) for fi in file_infos)

    # Simulate compression over the full conversation
    compressed, c, ct_pt, ct_ct = _track_compress(updated_convo)

    return AgentResult(
        usage={
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cache_write_tokens": usage.get("cache_write_tokens", 0),
        },
        conversation=compressed,
        tool_calls=0,
        tool_hits=tool_hits,
        tool_misses=tool_misses,
        tool_time_ms=0.0,
        ttft=usage.get("time_to_first_token_s", 0),
        ok=True,
        ctx_dropped_total=dropped_tokens,
        response_text=assistant_msg.get("content", ""),
        compression_count=c,
        compression_prompt_tokens=ct_pt,
        compression_completion_tokens=ct_ct,
    )


def register_hermes_arms(agent_mod) -> None:
    """Patch agent_mod.make_agent_turn to route hermes_compress (and both)."""
    from agent import make_agent_turn as _original_make_agent_turn

    def make_agent_turn(arm: str, provider: str = "openrouter", model: str = None):
        if arm == "hermes_compress":
            def wrapped(conversation, step):
                return _agent_turn_hermes_compress(
                    conversation, step, arm=arm, provider=provider, model=model
                )
            return wrapped
        return _original_make_agent_turn(arm, provider=provider, model=model)

    agent_mod.make_agent_turn = make_agent_turn