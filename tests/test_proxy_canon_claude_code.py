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
