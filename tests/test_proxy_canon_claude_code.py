"""claude-code canonicalization profile: key stability across identical headless runs."""

import json

from toolrecall.proxy import _canonicalize_body


def _body(session, duration, extra=""):
    return json.dumps(
        {
            "model": "m",
            "metadata": {"user_id": json.dumps({"session_id": session})},
            "messages": [
                {"role": "user", "content": "fix the tests" + extra},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": f"1 failed, 1 passed in {duration}",
                        }
                    ],
                },
            ],
        }
    ).encode()


def test_session_metadata_and_durations_do_not_fork_key():
    a = _canonicalize_body("claude-code", _body("s-1", "0.25s"))
    b = _canonicalize_body("claude-code", _body("s-2", "0.54s"))
    assert a == b


def test_counts_and_content_still_fork_key():
    base = _canonicalize_body("claude-code", _body("s", "0.25s"))
    assert _canonicalize_body("claude-code", _body("s", "0.25s", " now")) != base
    other = _body("s", "0.25s").replace(b"1 failed, 1 passed", b"2 passed")
    assert _canonicalize_body("claude-code", other) != base


def test_other_durations_units():
    for d in ("120ms", "3 seconds", "0.003s"):
        assert _canonicalize_body("claude-code", _body("s", d)) == _canonicalize_body(
            "claude-code", _body("s", "1s")
        )


def test_hermes_profile_unchanged_by_new_rules():
    a = _canonicalize_body("hermes", _body("s", "0.25s"))
    b = _canonicalize_body("hermes", _body("s", "0.54s"))
    assert a != b


def _tool_body(default_ms, text="run it"):
    return json.dumps(
        {
            "model": "m",
            "tools": [
                {
                    "name": "Bash",
                    "input_schema": {
                        "properties": {
                            "run_in_background": {
                                "description": f"stopped (default {default_ms} ms, max 7200000 ms)."
                            }
                        }
                    },
                }
            ],
            "messages": [{"role": "user", "content": text}],
        }
    ).encode()


def test_per_session_tool_default_does_not_fork_key():
    # Measured: Claude Code's Bash schema says 600000 or 1800000 ms per session.
    a = _canonicalize_body("claude-code", _tool_body(600000))
    b = _canonicalize_body("claude-code", _tool_body(1800000))
    assert a == b
    assert b"max 7200000 ms" in a  # only the per-session default is normalized


def test_tool_default_normalization_stays_out_of_messages():
    a = _canonicalize_body("claude-code", _tool_body(600000, "sleep default 5 ms"))
    b = _canonicalize_body("claude-code", _tool_body(600000, "sleep default 6 ms"))
    assert a != b


def test_other_profiles_keep_tool_text():
    a = _canonicalize_body("hermes", _tool_body(600000))
    b = _canonicalize_body("hermes", _tool_body(1800000))
    assert a != b


def _parallel_body(order, marker_last=True):
    results = {
        "toolu_A": {"type": "tool_result", "tool_use_id": "toolu_A", "content": "read output"},
        "toolu_B": {"type": "tool_result", "tool_use_id": "toolu_B", "content": "grep output"},
    }
    blocks = [dict(results[k]) for k in order]
    if marker_last:
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return json.dumps(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "task"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {}},
                        {"type": "tool_use", "id": "toolu_B", "name": "Grep", "input": {}},
                    ],
                },
                {"role": "user", "content": blocks},
            ],
        }
    ).encode()


def test_parallel_tool_result_order_does_not_fork_key():
    # Measured: Claude Code returns parallel tool results in completion order.
    a = _canonicalize_body("claude-code", _parallel_body(["toolu_A", "toolu_B"]))
    b = _canonicalize_body("claude-code", _parallel_body(["toolu_B", "toolu_A"]))
    assert a == b


def test_parallel_tool_result_content_still_keyed():
    a = _canonicalize_body("claude-code", _parallel_body(["toolu_A", "toolu_B"]))
    c = _parallel_body(["toolu_A", "toolu_B"]).replace(b"grep output", b"other output")
    assert a != _canonicalize_body("claude-code", c)


def test_hermes_profile_keeps_block_order():
    a = _canonicalize_body("hermes", _parallel_body(["toolu_A", "toolu_B"], marker_last=False))
    b = _canonicalize_body("hermes", _parallel_body(["toolu_B", "toolu_A"], marker_last=False))
    assert a != b


def _git_body(commit_sha: str, email: str, prompt: str = "solve issue"):
    text = (
        f"<system-reminder>\n"
        f"# gitStatus\n"
        f"Current branch: main\n"
        f"Recent commits:\n"
        f"{commit_sha} commit title\n\n"
        f"# userEmail\n"
        f"{email}\n\n"
        f"Claude Code attached\n"
        f"</system-reminder>\n"
        f"{prompt}"
    )
    return json.dumps(
        {
            "model": "m",
            "messages": [{"role": "user", "content": text}],
        }
    ).encode()


def test_git_context_normalization_opt_in():
    b1 = _git_body("aaaaaaa", "user1@example.com")
    b2 = _git_body("bbbbbbb", "user2@example.com")

    # Default claude-code profile preserves git status: distinct keys
    assert _canonicalize_body("claude-code", b1) != _canonicalize_body("claude-code", b2)

    # With ignore_git=True or claude-code-nogit profile: identical keys
    assert _canonicalize_body("claude-code", b1, ignore_git=True) == _canonicalize_body(
        "claude-code", b2, ignore_git=True
    )
    assert _canonicalize_body("claude-code-nogit", b1) == _canonicalize_body(
        "claude-code-nogit", b2
    )

    # Actual prompt changes still produce different keys
    b3 = _git_body("aaaaaaa", "user1@example.com", prompt="different prompt")
    assert _canonicalize_body("claude-code-nogit", b1) != _canonicalize_body(
        "claude-code-nogit", b3
    )
