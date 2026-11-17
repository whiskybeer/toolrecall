"""Egress credential and secret guard.

Scans outbound LLM API payloads for accidentally leaked credentials,
API keys, and private keys before they leave the host.
"""

from __future__ import annotations

import re
from typing import Any

# High-precision patterns: strictly anchored with provider-specific prefixes
# to avoid false positives on hex SHAs, UUIDs, or normal source code.
EGRESS_RULES: dict[str, re.Pattern[str]] = {
    "anthropic_api_key": re.compile(r"\bsk-ant-(?:api03|admin01)-[A-Za-z0-9_-]{20,}\b"),
    "openai_api_key": re.compile(r"\bsk-(?:proj|svcacct|None)-[A-Za-z0-9_-]{20,}\b"),
    "openrouter_api_key": re.compile(r"\bsk-or-v1-[a-f0-9]{32,}\b"),
    "github_token": re.compile(
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"
    ),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private_key_pem": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    "slack_token": re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
}


def scan_egress(
    payload: bytes | str,
    mode: str = "off",
    allow_rules: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Scan payload for sensitive credentials according to egress guard policy.

    Args:
        payload: String or UTF-8 bytes to scan.
        mode: Policy mode ("off", "warn", "redact", "block"). Unknown modes fall back to "off".
        allow_rules: Set or list of rule names to bypass.

    Returns:
        dict with keys:
            allowed (bool): Whether the request is permitted to proceed.
            mode (str): Effective egress policy mode.
            matches (list[dict]): [{"rule": rule_name, "count": count}, ...] (NO secrets).
            redacted_payload (bytes | str): Original or redacted payload (same type as input).
    """
    valid_modes = {"off", "warn", "redact", "block"}
    effective_mode = mode if mode in valid_modes else "off"

    if effective_mode == "off" or not payload:
        return {
            "allowed": True,
            "mode": effective_mode,
            "matches": [],
            "redacted_payload": payload,
        }

    if isinstance(payload, bytes):
        is_bytes = True
        text = payload.decode("utf-8", errors="replace")
    else:
        is_bytes = False
        text = payload

    allowed_set = set(allow_rules) if allow_rules else set()
    matches: list[dict[str, Any]] = []
    redacted_text: str = text

    for rule_name, pattern in EGRESS_RULES.items():
        if rule_name in allowed_set:
            continue
        found = pattern.findall(text)
        if found:
            matches.append({"rule": rule_name, "count": len(found)})
            if effective_mode == "redact":
                redacted_text = pattern.sub(f"[TOOLRECALL_REDACTED:{rule_name}]", redacted_text)

    redacted_payload: bytes | str
    if effective_mode == "warn":
        allowed = True
        redacted_payload = payload
    elif effective_mode == "redact":
        allowed = True
        if matches:
            redacted_payload = redacted_text.encode("utf-8") if is_bytes else redacted_text
        else:
            redacted_payload = payload
    elif effective_mode == "block":
        allowed = len(matches) == 0
        redacted_payload = payload
    else:
        allowed = True
        redacted_payload = payload

    return {
        "allowed": allowed,
        "mode": effective_mode,
        "matches": matches,
        "redacted_payload": redacted_payload,
    }


def egress_mode_from_config() -> tuple[str, set[str]]:
    """Read egress mode and allow rules from configuration without raising.

    Returns:
        (mode, allow_rules_set)
    """
    try:
        from toolrecall.config import load_config

        cfg = load_config()
        mode = str(cfg.get("security", "egress_mode", default="off")).strip().lower()
        if mode not in ("off", "warn", "redact", "block"):
            mode = "off"
        raw_allows = cfg.get("security", "egress_allow_rules", default=[])
        if isinstance(raw_allows, (list, tuple, set)):
            allow_rules = {str(r).strip() for r in raw_allows if str(r).strip()}
        elif isinstance(raw_allows, str):
            allow_rules = {r.strip() for r in raw_allows.split(",") if r.strip()}
        else:
            allow_rules = set()
        return mode, allow_rules
    except Exception:
        return "off", set()
