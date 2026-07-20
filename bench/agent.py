"""agent.py — arm-specific agent turn functions for the three-arm benchmark.

Each arm has a different strategy for context management:

  naive      — full conversation history every turn (no dropping)
  prefix     — same as naive; relies on provider prefix caching for savings
  toolrecall — uses ToolRecall daemon's context tracker + cached_read

All three arms read files via toolrecall.client.cached_read() so the
comparison is fair (same file content in all cases). Only the context
management strategy differs.

The naive/prefix arms accumulate full history. The toolrecall arm uses
context_set_checkpoint/context_get_dirty to determine which file content
is "clean" (read but not written) and drops it from the conversation
before the next turn, and uses cached_read to report cache-hit savings.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error

# OpenRouter API — read key from .hermes/.env
ENV_FILE = os.path.expanduser("~/.hermes/.env")
MODEL = "deepseek/deepseek-v4-flash"
API_URL = "https://openrouter.ai/api/v1/chat/completions"


def _read_api_key() -> str:
    """Read the last OPENROUTER_API_KEY from .env."""
    with open(ENV_FILE) as f:
        lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    aks = [l for l in lines if "OPENROUTER_API_KEY" in l]
    raw = aks[-1] if aks else lines[-1]
    return raw.split("=", 1)[1].strip().strip('"').strip("'")


API_KEY = _read_api_key()


def _call_llm(messages: list[dict]) -> dict:
    """Make an OpenRouter API call. Returns the parsed response dict.

    Sets stream=False so the response includes usage.prompt_tokens.
    Timeout: 180s.
    """
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 512,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=180).read())
        return resp
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"error": str(e)}


# ── Agent Result ──────────────────────────────────────────────


class AgentResult:
    """Result of a single agent turn."""

    def __init__(self, usage: dict = None, conversation: list[dict] = None,
                 tool_calls: int = 0, tool_hits: int = 0, tool_misses: int = 0,
                 tool_time_ms: float = 0.0, ttft: float = 0.0,
                 ok: bool = True, ctx_dropped_total: int = 0,
                 response_text: str = "",
                 context_tracker_ok: bool = True):
        self.usage = usage or {}
        self.conversation = conversation or []
        self.tool_calls = tool_calls
        self.tool_hits = tool_hits
        self.tool_misses = tool_misses
        self.tool_time_ms = tool_time_ms
        self.ttft = ttft
        self.ok = ok
        self._ctx_dropped_total = ctx_dropped_total
        self.response_text = response_text
        self.context_tracker_ok = context_tracker_ok

    def ctx_dropped_total(self) -> int:
        return self._ctx_dropped_total


# ── File reading (common to all arms) ─────────────────────────


def _read_files(reads: list[str]) -> list[dict]:
    """Read files via toolrecall.client.cached_read().

    Returns list of {"path": str, "content": str, "cached": bool}.
    Resolves relative paths against the repo root set by the workload.
    """
    from toolrecall.client import cached_read

    results = []
    for path in reads:
        resp = cached_read(path)
        if "error" in resp:
            results.append({"path": path, "content": f"<error: {resp['error']}>",
                            "cached": False})
        else:
            results.append({
                "path": resp.get("path", path),
                "content": resp.get("content", ""),
                "cached": resp.get("cached", False),
            })
    return results


def _build_file_block(file_info: dict) -> str:
    """Build a formatted file content block for insertion into a message.

    Capped at ~50 lines (roughly 500-800 tokens) to keep naive/prefix
    arms viable for hundreds of turns while still representing realistic
    LLM agent file reads.
    """
    path = file_info["path"]
    content = file_info["content"]
    cached_mark = " [cached]" if file_info.get("cached") else ""
    lines = content.split("\n")
    MAX_LINES = 200
    if len(lines) > MAX_LINES:
        head = "\n".join(lines[:100])
        tail = "\n".join(lines[-100:])
        return (
            f"=== {path} ===\n"
            f"{head}\n"
            f"... [{len(lines)} lines total, showing first 100 + last 100] ...\n"
            f"{tail}\n"
            f"=== end {path} ==={cached_mark}"
        )
    return f"=== {path} ===\n{content}\n=== end {path} ==={cached_mark}"


def _strip_file_blocks(messages: list[dict], file_paths: set[str]) -> list[dict]:
    """Remove file content blocks for given paths from ALL messages (user + assistant).

    File content is injected into user messages (as part of the turn instruction).
    This function strips those blocks from every message in the conversation,
    preserving the message structure (roles, order).

    Returns a new message list with blocks removed.
    """
    result = []
    for msg in messages:
        content = msg.get("content", "")
        if not content:
            result.append(msg)
            continue

        for fp in file_paths:
            start_marker = f"=== {fp} ===\n"
            end_marker = f"\n=== end {fp} ==="
            while start_marker in content:
                start_idx = content.index(start_marker)
                if end_marker in content[start_idx:]:
                    end_idx = content.index(end_marker, start_idx)
                    content = content[:start_idx] + content[end_idx + len(end_marker):]
                else:
                    content = content[:start_idx]

        result.append({**msg, "content": content})

    return result


def _count_tokens(text: str) -> int:
    """Rough token estimate (chars/4)."""
    return len(text) // 4


# ── Arm-specific agent turn factories ────────────────────────


def make_agent_turn(arm: str):
    """Return the agent_turn function for the given arm.

    The returned function has signature:
        fn(conversation: list[dict], step: WorkloadStep) -> AgentResult
    """
    if arm == "naive":
        return _agent_turn_naive
    elif arm == "prefix":
        return _agent_turn_prefix
    elif arm == "toolrecall":
        return _agent_turn_toolrecall
    raise ValueError(f"Unknown arm: {arm}")


def _agent_turn_naive(conversation: list[dict], step) -> AgentResult:
    """Full history, no dropping. Reads files via cached_read but accumulates everything."""
    from toolrecall.client import cached_read

    t0 = time.time()
    tool_hits = 0
    tool_misses = 0

    # Read files for this step — all arms do the same reads
    file_infos = _read_files(step.reads)
    file_blocks = [_build_file_block(fi) for fi in file_infos]

    # Count cache hits/misses
    for fi in file_infos:
        if fi.get("cached"):
            tool_hits += 1
        else:
            tool_misses += 1

    # Build user message — instruction + file content
    content_parts = [step.message.get("content", "")]
    content_parts.extend(file_blocks)
    user_msg = {"role": "user", "content": "\n\n".join(content_parts)}

    updated_convo = list(conversation) + [user_msg]

    # Call the LLM
    resp = _call_llm(updated_convo)
    elapsed = time.time() - t0

    if "error" in resp:
        return AgentResult(
            usage={"prompt_tokens": 0, "completion_tokens": 0,
                   "cache_read_tokens": 0},
            conversation=updated_convo,
            tool_hits=tool_hits, tool_misses=tool_misses,
            ok=False, ttft=0,
            response_text=resp["error"],
        )

    choice = resp.get("choices", [{}])[0]
    assistant_msg = choice.get("message", {})
    updated_convo.append(assistant_msg)

    usage = resp.get("usage", {})

    return AgentResult(
        usage={
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cache_write_tokens": usage.get("cache_write_tokens", 0),
        },
        conversation=updated_convo,
        tool_calls=0, tool_hits=tool_hits, tool_misses=tool_misses,
        tool_time_ms=0.0, ttft=usage.get("time_to_first_token_s", 0),
        ok=True,
        response_text=assistant_msg.get("content", ""),
    )


def _agent_turn_prefix(conversation: list[dict], step) -> AgentResult:
    """Full history, same as naive. Provider prefix caching is the savings mechanism.

    This arm exists so we can compare real provider-reported prompt_tokens against
    the toolrecall arm. If DeepSeek/OpenRouter prefix caching works well, this arm
    will show lower provider prompt_tokens than request_tokens (self-counted).
    """
    return _agent_turn_naive(conversation, step)


def _agent_turn_toolrecall(conversation: list[dict], step) -> AgentResult:
    """ToolRecall arm: uses the real daemon for context tracking + file caching.

    Before each turn:
      1. Calls context_set_checkpoint() to mark current state
      2. Reads files via cached_read (gets cache-hit info)
      3. Builds message with file content

    After the LLM responds:
      4. Calls context_get_dirty() to find clean files
      5. Strips clean file content from assistant messages
      6. Tracks ctx_dropped_tokens for the report
    """
    from toolrecall.client import cached_read, context_set_checkpoint, context_get_dirty

    t0 = time.time()
    tool_hits = 0
    tool_misses = 0

    # Step 1: Set checkpoint before reading
    context_tracker_ok = True
    try:
        context_set_checkpoint("turn_start")
    except Exception as e:
        logging.warning(f"Context tracker checkpoint failed: {e}")
        context_tracker_ok = False

    # Step 2: Read files via daemon's cached_read
    file_infos = _read_files(step.reads)

    # Count hits/misses from cached_read responses
    for fi in file_infos:
        if fi.get("cached"):
            tool_hits += 1
        else:
            tool_misses += 1

    # Step 3: Build user message with file content
    content_parts = [step.message.get("content", "")]
    content_parts.extend(_build_file_block(fi) for fi in file_infos)
    user_msg = {"role": "user", "content": "\n\n".join(content_parts)}

    updated_convo = list(conversation) + [user_msg]

    # Call the LLM
    resp = _call_llm(updated_convo)
    elapsed = time.time() - t0

    if "error" in resp:
        return AgentResult(
            usage={"prompt_tokens": 0, "completion_tokens": 0,
                   "cache_read_tokens": 0},
            conversation=updated_convo,
            tool_hits=tool_hits, tool_misses=tool_misses,
            ok=False, ttft=0,
            response_text=resp["error"],
        )

    choice = resp.get("choices", [{}])[0]
    assistant_msg = choice.get("message", {})
    updated_convo.append(assistant_msg)

    usage = resp.get("usage", {})

    # Step 4: Get dirty/clean from context tracker
    clean_paths = set()
    dropped_tokens = 0
    try:
        ctx = context_get_dirty()
        dirty_set = set(ctx.get("dirty", []))
        clean_set = set(ctx.get("clean", []))
        # Files in step.writes are "dirty" — don't drop them
        write_paths = set(getattr(step, "writes", []))
        clean_paths = clean_set - dirty_set - write_paths

        # Estimate token count of content being dropped
        for fi in file_infos:
            if fi["path"] in clean_paths:
                dropped_tokens += _count_tokens(fi.get("content", ""))
    except Exception as e:
        logging.warning(f"Context tracker dirty check failed: {e}")
        context_tracker_ok = False

    # Step 5: Strip clean file content from assistant messages
    if clean_paths:
        updated_convo = _strip_file_blocks(updated_convo, clean_paths)

    return AgentResult(
        usage={
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cache_write_tokens": usage.get("cache_write_tokens", 0),
        },
        conversation=updated_convo,
        tool_calls=0, tool_hits=tool_hits, tool_misses=tool_misses,
        tool_time_ms=0.0, ttft=usage.get("time_to_first_token_s", 0),
        ok=True,
        ctx_dropped_total=dropped_tokens,
        response_text=assistant_msg.get("content", ""),
        context_tracker_ok=context_tracker_ok,
    )