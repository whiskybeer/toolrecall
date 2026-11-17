"""Integration tests verifying config, daemon IPC, client, and CLI wiring."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading

from toolrecall.cache import _init, get_stats
from toolrecall.config import Config, ENV_MAP, load_config
from toolrecall.context_tracker import ContextTracker
from toolrecall.daemon import DaemonServer, _count_tool_call


class _DummyMux:
    def __init__(self, sessions=None):
        self._sessions = sessions or {}
        self.calls = []

    def call_tool(self, server, tool, arguments):
        self.calls.append((server, tool, arguments))
        return {"ok": True, "server": server, "tool": tool, "arguments": arguments}

    def is_cacheable(self, server, tool):
        return False


class _AllowAllSecurity:
    allowed_paths = []
    allow_terminal = True
    allowed_terminal_commands = []
    allow_invalidate = True
    allow_multiplex = True

    def check_read_path_for(self, path, client=None, remote=False):
        return None

    def check_mcp_server(self, server):
        return None

    def check_mcp_tool_access(self, tool):
        return None

    def cognitive_scan_arguments(self, arguments):
        return None

    def check_ast_injection(self, arguments):
        return None


def _make_bare_server(monkeypatch, *, failure_ledger: bool = False, egress_mode: str = "off"):
    _init()
    if failure_ledger:
        monkeypatch.setenv("TOOLRECALL_FAILURE_LEDGER", "1")
    else:
        monkeypatch.setenv("TOOLRECALL_FAILURE_LEDGER", "0")
    monkeypatch.setenv("TOOLRECALL_EGRESS_MODE", egress_mode)

    srv = DaemonServer.__new__(DaemonServer)
    srv.cfg = load_config()
    srv.security = _AllowAllSecurity()
    srv.multiplexer = _DummyMux({"github": object()})
    srv._context = ContextTracker()
    srv._compaction_epoch = 0
    srv._compaction_lock = threading.Lock()
    import logging

    srv.logger = logging.getLogger("toolrecall.daemon.test")
    return srv


def test_config_schema_and_env_overrides(monkeypatch):
    """New P1-P6 keys are registered in Config._KNOWN and coerce via ENV_MAP."""
    for sec, key in (
        ("cache", "incremental_reads"),
        ("cache", "incremental_paths"),
        ("cache", "incremental_min_bytes"),
        ("cache", "delta_reads"),
        ("cache", "delta_min_saving_ratio"),
        ("cache", "failure_ledger"),
        ("tracker", "post_compaction_window"),
        ("forward_proxy", "stabilize_prefix"),
        ("security", "egress_mode"),
        ("security", "egress_allow_rules"),
    ):
        assert key in Config._KNOWN[sec]

    assert ENV_MAP["TOOLRECALL_STABILIZE_PREFIX"] == ("forward_proxy", "stabilize_prefix")
    monkeypatch.setenv("TOOLRECALL_INCREMENTAL_READS", "true")
    monkeypatch.setenv("TOOLRECALL_INCREMENTAL_PATHS", "*.log, *.jsonl")
    monkeypatch.setenv("TOOLRECALL_DELTA_MIN_SAVING_RATIO", "0.35")
    monkeypatch.setenv("TOOLRECALL_FAILURE_LEDGER", "1")
    monkeypatch.setenv("TOOLRECALL_STABILIZE_PREFIX", "1")
    monkeypatch.setenv("TOOLRECALL_EGRESS_MODE", "redact")
    monkeypatch.setenv("TOOLRECALL_EGRESS_ALLOW_RULES", "slack_token")

    cfg = load_config()
    assert cfg.get("cache", "incremental_reads") is True
    assert cfg.get("cache", "incremental_paths") == ["*.log", "*.jsonl"]
    assert cfg.get("cache", "delta_min_saving_ratio") == 0.35
    assert cfg.get("cache", "failure_ledger") is True
    assert cfg.get("forward_proxy", "stabilize_prefix") is True
    assert cfg.get("security", "egress_mode") == "redact"
    assert cfg.get("security", "egress_allow_rules") == ["slack_token"]


def test_daemon_ping_and_dedup_and_api_check(monkeypatch, tmp_path):
    """ping exposes failure_ledger_enabled; dedup_tokens and api_check route by category."""
    srv = _make_bare_server(monkeypatch, failure_ledger=True)
    pong = srv._route({"cmd": "ping"})
    assert pong["pong"] is True
    assert pong["failure_ledger_enabled"] is True

    d_resp = srv._route(
        {
            "cmd": "dedup_tokens",
            "tokens": 420,
            "path": "/tmp/a.py",
            "category": "incremental_reads",
        }
    )
    assert d_resp["ok"] is True
    assert d_resp["category"] == "incremental_reads"

    stats = get_stats()
    assert stats["incremental_reads"]["hits"] == 1
    assert stats["incremental_reads"]["tokens_not_sent_measured"] == 420

    # Invalid category falls back cleanly to mcp_dedup
    d_bad = srv._route({"cmd": "dedup_tokens", "tokens": 50, "category": "bogus"})
    assert d_bad["category"] == "mcp_dedup"

    # api_check routes llm_passthrough separately from api_cache
    api_miss = srv._route(
        {"cmd": "cached_api_check", "request_hash": "h123", "category": "llm_passthrough"}
    )
    assert api_miss["cached"] is False
    stats2 = get_stats()
    assert stats2["llm_passthrough"]["misses"] == 1
    assert stats2["llm_passthrough"].get("non_replayable_by_design") is True


def test_daemon_compaction_manifest_anchors_and_attribution(monkeypatch, tmp_path):
    """_handle_read records anchors & post_compaction reads; attribute_turn updates hints."""
    monkeypatch.setenv("TOOLRECALL_POST_COMPACTION_WINDOW", "2")
    srv = _make_bare_server(monkeypatch)

    sample = tmp_path / "module.py"
    sample.write_text(
        ("alpha_line = 1\n" * 40) + "UNIQUE_MID_ANCHOR_XYZ_789\n" + ("omega = 2\n" * 40)
    )

    r1 = srv._handle_read({"path": str(sample), "source": "agent_tool"})
    assert r1.get("content")
    anchors_resp = srv._route({"cmd": "context_get_anchors"})
    assert str(sample) in anchors_resp["anchors"]

    # Compaction builds manifest and arms post-compaction window
    comp = srv._route({"cmd": "context_compacted"})
    assert comp["compacted"] is True
    assert comp["manifest_files"] == 1

    man = srv._route({"cmd": "context_get_manifest"})
    assert man["compaction_count"] == 1
    assert man["total_files"] == 1
    assert "content" not in man["manifest"][0]

    # Next 2 reads land in post_compaction category, 3rd does not
    srv._handle_read({"path": str(sample), "source": "agent_tool"})
    srv._handle_read({"path": str(sample), "source": "agent_tool"})
    srv._handle_read({"path": str(sample), "source": "agent_tool"})
    st = get_stats()
    assert st["post_compaction"]["hits"] + st["post_compaction"]["misses"] == 2

    # Attribute turn using body_text containing midpoint anchor
    anchor_str = anchors_resp["anchors"][str(sample)]["anchor"]
    attr = srv._route(
        {
            "cmd": "context_attribute_turn",
            "body_text": f"prefix {anchor_str} suffix",
            "prompt_tokens": 1000,
            "cost_usd": 0.10,
        }
    )
    assert attr["matched_files"] == 1
    assert attr["attributed_cost_usd"] > 0.0

    hint_resp = srv._route({"cmd": "context_get_hint"})
    assert "Top clean context cost attribution" in hint_resp["hint"]


def test_daemon_failure_ledger_and_egress_guard(monkeypatch):
    """_count_tool_call records failures when enabled; MCP & proxy egress guard records/blocks."""
    srv = _make_bare_server(monkeypatch, failure_ledger=True, egress_mode="block")

    def _failing_handler(_req):
        return {"error": "connection timed out for " + "/home/alice/work"}

    res = _count_tool_call("mcp_call", _failing_handler, {"client": "agent-1"})
    assert "error" in res

    found = srv._route({"cmd": "failures_search", "query": "connection timed out"})
    assert len(found["failures"]) == 1
    sig = found["failures"][0]["signature_hash"]

    resolved = srv._route(
        {
            "cmd": "failures_resolve",
            "signature_hash": sig,
            "resolution": "Increase timeout",
        }
    )
    assert resolved["resolved"] is True

    # Construct fake Anthropic key dynamically (gitleaks-safe)
    fake_secret = "sk-ant-" + "api03-" + ("A" * 24)
    blocked = srv._handle_mcp_call(
        {"server": "github", "tool": "create_issue", "arguments": {"body": fake_secret}}
    )
    assert "error" in blocked
    assert "Egress guard blocked" in blocked["error"]
    assert srv.multiplexer.calls == []

    # Direct egress_event route from proxy
    ev = srv._route({"cmd": "egress_event", "blocked": True, "rules": ["anthropic_api_key"]})
    assert ev["ok"] is True
    st = get_stats()
    assert st["egress_guard"]["hits"] >= 2


def test_cli_stats_context_and_failures(monkeypatch, tmp_path):
    """CLI cmd_stats, cmd_context (manifest/cost), and cmd_failures end-to-end."""
    _init()
    csv_file = tmp_path / "usage.csv"
    csv_file.write_text(
        "timestamp,cache_status,target_host,target_path,request_hash,"
        "prompt_tokens,completion_tokens,cache_read_tokens,cache_write_tokens,"
        "cost_usd,dedup_tokens,cache_discount_usd,prefix_hash,cache_bust_tokens\n"
        "1700000000,STREAM,api.openai.com,/v1/chat/completions,h1,500,50,100,0,0.01,0,0.0,p1,250\n"
    )
    monkeypatch.setenv("TOOLRECALL_PROXY_USAGE_CSV", str(csv_file))

    from toolrecall.cli import cmd_context, cmd_failures, cmd_stats

    def _run_capture(fn, argv):
        monkeypatch.setattr(sys, "argv", argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    data = json.loads(_run_capture(cmd_stats, ["toolrecall", "stats"]))
    assert data["prefix_busts"]["total_bust_tokens"] == 250
    assert data["non_replayable"]["llm_passthrough_rows"] == 1

    # Mock client for context manifest & cost
    monkeypatch.setattr(
        "toolrecall.client.context_get_manifest",
        lambda: {
            "compaction_count": 2,
            "total_files": 1,
            "total_tokens": 100,
            "manifest": [{"path": "/tmp/x.py", "est_tokens": 100, "dirty": False, "stale": False}],
        },
    )
    out_man = _run_capture(cmd_context, ["toolrecall", "context", "manifest"])
    assert "Compactions: 2" in out_man

    monkeypatch.setattr(
        "toolrecall.client.context_get_stats",
        lambda: {"attributed_billed_tokens": 900, "attributed_cost_usd": 0.05},
    )
    monkeypatch.setattr(
        "toolrecall.client.context_get_dirty",
        lambda: {
            "cost_ranking": [
                {
                    "status": "clean",
                    "path": "/tmp/x.py",
                    "billed_cost_usd": 0.05,
                    "billed_tokens": 900,
                    "turns_billed": 3,
                }
            ]
        },
    )
    out_cost = _run_capture(cmd_context, ["toolrecall", "context", "cost"])
    assert "$0.0500" in out_cost

    # Exercise failures CLI directly against isolated temp SQLite DB
    from toolrecall.failures import record_failure

    rec = record_failure("bash", "Permission denied reading /tmp/locked")
    st_out = json.loads(_run_capture(cmd_failures, ["toolrecall", "failures", "stats", "--json"]))
    assert st_out["unique_signatures"] == 1

    out_res = _run_capture(
        cmd_failures,
        ["toolrecall", "failures", "resolve", rec["signature_hash"], "--resolution", "Check perms"],
    )
    assert "Resolved" in out_res

    out_search = _run_capture(
        cmd_failures, ["toolrecall", "failures", "search", "Permission denied"]
    )
    assert "Check perms" in out_search
