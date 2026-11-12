"""antigravity canonicalization profile: key stability across identical runs."""

import json

from toolrecall.proxy import _canonicalize_body


def _ag_body(conv_id, time_str, duration="0.12s", settings="", extra=""):
    system_text = (
        f"<ADDITIONAL_METADATA>\n"
        f"The current local time is: {time_str}\n"
        f"Conversation ID: {conv_id}\n"
        f"Artifact Directory Path: /home/hermes/.gemini/antigravity-cli/brain/{conv_id}\n"
        f"</ADDITIONAL_METADATA>\n"
        f"{settings}"
        f"You are Antigravity agent.{extra}"
    )
    user_tool_result = f"3 passed in {duration}"
    return json.dumps(
        {
            "model": "gemini-2.5-pro",
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": "run tests"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": user_tool_result,
                        }
                    ],
                },
            ],
        }
    ).encode("utf-8")


def test_metadata_and_durations_do_not_fork_key():
    b1 = _ag_body(
        conv_id="11111111-1111-1111-1111-111111111111",
        time_str="2026-11-12T10:00:00Z",
        duration="0.15s",
    )
    b2 = _ag_body(
        conv_id="22222222-2222-2222-2222-222222222222",
        time_str="2026-11-12T15:30:22+00:00",
        duration="1.82s",
    )
    assert _canonicalize_body("antigravity", b1) == _canonicalize_body("antigravity", b2)


def test_user_settings_change_normalized():
    settings = "<USER_SETTINGS_CHANGE>\n[15:20] Model changed to flash\n</USER_SETTINGS_CHANGE>\n"
    b_normal = _ag_body(
        conv_id="11111111-1111-1111-1111-111111111111",
        time_str="2026-11-12T10:00:00Z",
        settings="",
    )
    b_settings = _ag_body(
        conv_id="11111111-1111-1111-1111-111111111111",
        time_str="2026-11-12T10:00:00Z",
        settings=settings,
    )
    assert _canonicalize_body("antigravity", b_normal) == _canonicalize_body(
        "antigravity", b_settings
    )


def test_real_prompt_changes_fork_key():
    b1 = _ag_body(
        conv_id="11111111-1111-1111-1111-111111111111",
        time_str="2026-11-12T10:00:00Z",
        extra=" extra prompt instruction",
    )
    b2 = _ag_body(
        conv_id="11111111-1111-1111-1111-111111111111",
        time_str="2026-11-12T10:00:00Z",
    )
    assert _canonicalize_body("antigravity", b1) != _canonicalize_body("antigravity", b2)


def test_parallel_tool_results_openai_order_normalized():
    res_a = {"type": "tool_result", "tool_use_id": "call_aaa", "content": "res A"}
    res_b = {"type": "tool_result", "tool_use_id": "call_bbb", "content": "res B"}

    msg_ab = {"role": "user", "content": [res_a, res_b]}
    msg_ba = {"role": "user", "content": [res_b, res_a]}

    body1 = json.dumps({"messages": [msg_ab]}).encode()
    body2 = json.dumps({"messages": [msg_ba]}).encode()

    assert _canonicalize_body("antigravity", body1) == _canonicalize_body("antigravity", body2)


def test_gemini_contents_parallel_function_responses_normalized():
    resp_read = {
        "functionResponse": {"name": "read_file", "response": {"content": "def foo(): pass"}}
    }
    resp_command = {"functionResponse": {"name": "run_command", "response": {"output": "ok"}}}

    # Gemini API wire format has 'contents' instead of 'messages'
    body1 = json.dumps(
        {"contents": [{"role": "user", "parts": [resp_read, resp_command]}]}
    ).encode()
    body2 = json.dumps(
        {"contents": [{"role": "user", "parts": [resp_command, resp_read]}]}
    ).encode()

    assert _canonicalize_body("antigravity", body1) == _canonicalize_body("antigravity", body2)
