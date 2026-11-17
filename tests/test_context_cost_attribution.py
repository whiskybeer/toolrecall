"""Tests for P4.5: Wire-Verified Context-Cost Attribution.

Covers:
  - Midpoint anchor generation and active anchor indexing
  - Wire verification: only files present in request body text are attributed
  - Proportional cost attribution math (cost_usd * tok_attr / prompt_tokens)
  - Zero cost (0.0) stays 0.0
  - Eviction stops attribution immediately
  - get_cost_ranking sorting: highest cost, highest tokens, path
  - Exclusion of sensitive paths from anchors, attribution, and ranking
  - cost_ranking presence in get_dirty() and get_stale()
  - attributed_billed_tokens, attributed_cost_usd, and compaction_count in get_stats()
  - Persistence round-trip and reset() behavior
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.context_tracker import ContextTracker


def _point_db(path) -> None:
    """Point the _db() singleton at `path` for isolated SQLite persistence tests."""
    from toolrecall import _db as db_mod

    os.environ["TOOLRECALL_CACHE_DB"] = str(path)
    cfg = db_mod._get_cached_config()
    cfg._data.setdefault("paths", {})["cache_db"] = str(path)
    if db_mod._db_real is not None:
        db_mod._db_real.close()
    db_mod._db_real = None
    db_mod._db_path_cached = None


@pytest.fixture()
def fresh_db(monkeypatch, tmp_path):
    """Isolated cache DB for persistence tests."""
    monkeypatch.setenv("TOOLRECALL_DOCS_INDEX_TTL", "0")
    db_file = tmp_path / "cost-attr-test.db"
    _point_db(db_file)
    yield db_file
    from toolrecall import _db as db_mod

    if db_mod._db_real is not None:
        db_mod._db_real.close()
        db_mod._db_real = None
        db_mod._db_path_cached = None


def _create_file(path: str, content: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestContextCostAttribution:
    """Tests for P4.5 wire-verified cost attribution and cost ranking."""

    def test_anchor_generation_and_active_anchors(self, tmp_path):
        """mark_read computes midpoint anchor for files with content >= 64 chars."""
        ct = ContextTracker()

        content_a = "alpha_" * 20  # 120 chars
        content_short = "short"  # < 64 chars

        pa = _create_file(str(tmp_path / "a.py"), content_a)
        ps = _create_file(str(tmp_path / "s.py"), content_short)

        ct.mark_read(pa, content=content_a)
        ct.mark_read(ps, content=content_short)

        active = ct.get_active_anchors()
        assert os.path.abspath(pa) in active
        assert os.path.abspath(ps) not in active

        anchor_info = active[os.path.abspath(pa)]
        assert "anchor" in anchor_info
        assert "file_tokens" in anchor_info
        assert anchor_info["file_tokens"] == len(content_a) // 4

        # Midpoint anchor slice check
        mid = len(content_a) // 2
        expected_slice = content_a[max(0, mid - 32) : mid + 32]
        expected_anchor = json.dumps(expected_slice)[1:-1]
        assert anchor_info["anchor"] == expected_anchor

    def test_wire_verified_matching_in_body_text(self, tmp_path):
        """Only files whose anchors are actually in body_text get attributed."""
        ct = ContextTracker()

        content_1 = (
            "function_one_implementation_details_with_enough_padding_chars_here_to_exceed_64"
        )
        content_2 = (
            "function_two_implementation_details_with_enough_padding_chars_here_to_exceed_64"
        )

        p1 = _create_file(str(tmp_path / "f1.py"), content_1)
        p2 = _create_file(str(tmp_path / "f2.py"), content_2)

        ct.mark_read(p1, content=content_1)
        ct.mark_read(p2, content=content_2)

        active = ct.get_active_anchors()
        anchor_1 = active[os.path.abspath(p1)]["anchor"]

        # Turn 1: Wire payload contains anchor for f1 only
        prompt_payload = json.dumps(
            {"messages": [{"role": "user", "content": f"code: {content_1}"}]}
        )
        assert anchor_1 in prompt_payload
        attr_res = ct.attribute_turn(
            body_text=prompt_payload,
            prompt_tokens=1000,
            cost_usd=0.02,
        )

        assert attr_res["matched_files"] == 1
        assert attr_res["paths"] == [os.path.abspath(p1)]
        f1_tok = active[os.path.abspath(p1)]["file_tokens"]
        assert attr_res["attributed_tokens"] == f1_tok

        ranking = ct.get_cost_ranking()
        assert len(ranking) == 1
        assert ranking[0]["path"] == os.path.abspath(p1)
        assert ranking[0]["turns_billed"] == 1

        # Turn 2: Wire payload as UTF-8 bytes containing both files
        prompt_payload_bytes = json.dumps(
            {"messages": [{"role": "user", "content": f"{content_1} and {content_2}"}]}
        ).encode("utf-8")

        attr_res_2 = ct.attribute_turn(
            body_text=prompt_payload_bytes,
            prompt_tokens=2000,
            cost_usd=0.04,
        )

        assert attr_res_2["matched_files"] == 2
        assert os.path.abspath(p1) in attr_res_2["paths"]
        assert os.path.abspath(p2) in attr_res_2["paths"]

        ranking_2 = ct.get_cost_ranking()
        by_path = {e["path"]: e for e in ranking_2}
        assert by_path[os.path.abspath(p1)]["turns_billed"] == 2
        assert by_path[os.path.abspath(p2)]["turns_billed"] == 1

    def test_proportional_cost_math_and_zero_cost(self, tmp_path):
        """Cost is attributed proportionally to file_tokens / prompt_tokens; 0.0 stays 0.0."""
        ct = ContextTracker()

        content = "X" * 400  # 100 tokens
        p = _create_file(str(tmp_path / "math.py"), content)
        ct.mark_read(p, content=content)

        # 1. Proportional math
        # prompt_tokens=1000, file_tokens=100 (10%), cost_usd=0.10 -> attributed cost = 0.01
        res = ct.attribute_turn(
            matched_paths=[p],
            prompt_tokens=1000,
            cost_usd=0.10,
        )
        assert res["attributed_tokens"] == 100
        assert res["attributed_cost_usd"] == 0.01

        ranking = ct.get_cost_ranking()
        assert ranking[0]["billed_cost_usd"] == 0.01
        assert ranking[0]["billed_tokens"] == 100

        # 2. Zero cost stays 0.0
        res_zero = ct.attribute_turn(
            matched_paths=[p],
            prompt_tokens=1000,
            cost_usd=0.0,
        )
        assert res_zero["attributed_cost_usd"] == 0.0
        # Cumulative cost in ledger unchanged: 0.01 + 0.0 = 0.01
        assert ct.get_cost_ranking()[0]["billed_cost_usd"] == 0.01
        assert ct.get_cost_ranking()[0]["turns_billed"] == 2

        # 3. Zero prompt tokens -> 0.0 cost
        res_no_tok = ct.attribute_turn(
            matched_paths=[p],
            prompt_tokens=0,
            cost_usd=0.05,
        )
        assert res_no_tok["attributed_cost_usd"] == 0.0

    def test_eviction_stops_attribution_immediately(self, tmp_path):
        """Once a file is evicted from read_set, attribution stops and status is evicted."""
        ct = ContextTracker()

        content = "evict_me_code_sample_with_sufficient_padding_for_anchor_testing_12345"
        p = _create_file(str(tmp_path / "evictable.py"), content)
        ct.mark_read(p, content=content)

        # Attributed once while clean
        res1 = ct.attribute_turn(
            body_text=content,
            prompt_tokens=500,
            cost_usd=0.05,
        )
        assert res1["matched_files"] == 1
        ranking1 = ct.get_cost_ranking()
        assert ranking1[0]["status"] == "clean"

        # Simulate eviction: file is removed from tracker read set
        with ct._lock:
            del ct._read_set[os.path.abspath(p)]

        # Turn 2: same content in body_text, but file is evicted
        res2 = ct.attribute_turn(
            body_text=content,
            prompt_tokens=500,
            cost_usd=0.05,
        )
        assert res2["matched_files"] == 0
        assert res2["attributed_tokens"] == 0
        assert res2["attributed_cost_usd"] == 0.0

        # Status in ranking reflects 'evicted'
        ranking2 = ct.get_cost_ranking()
        assert len(ranking2) == 1
        assert ranking2[0]["status"] == "evicted"
        assert ranking2[0]["turns_billed"] == 1

        # Also matched_paths override honors eviction
        res3 = ct.attribute_turn(
            matched_paths=[p],
            prompt_tokens=500,
            cost_usd=0.05,
        )
        assert res3["matched_files"] == 0

    def test_cost_ranking_sorting_order(self, tmp_path):
        """get_cost_ranking sorts descending by cost, descending by tokens, ascending by path."""
        ct = ContextTracker()

        p_a = _create_file(str(tmp_path / "aaa.py"), "A" * 400)
        p_b = _create_file(str(tmp_path / "bbb.py"), "B" * 200)
        p_c = _create_file(str(tmp_path / "ccc.py"), "C" * 800)
        p_d = _create_file(str(tmp_path / "ddd.py"), "D" * 100)

        ct.mark_read(p_a, content="A" * 400)
        ct.mark_read(p_b, content="B" * 200)
        ct.mark_read(p_c, content="C" * 800)
        ct.mark_read(p_d, content="D" * 100)

        # Attribute different costs:
        # p_c: highest cost $0.05
        ct.attribute_turn(matched_paths=[p_c], prompt_tokens=1000, cost_usd=0.05)
        # p_a: medium cost $0.02, 100 tokens
        ct.attribute_turn(matched_paths=[p_a], prompt_tokens=1000, cost_usd=0.02)
        # p_b: medium cost $0.02, 50 tokens (same cost as p_a, fewer tokens)
        ct.attribute_turn(matched_paths=[p_b], prompt_tokens=1000, cost_usd=0.02)
        # p_d: lowest cost $0.00
        ct.attribute_turn(matched_paths=[p_d], prompt_tokens=1000, cost_usd=0.00)

        ranking = ct.get_cost_ranking()
        paths_ordered = [e["path"] for e in ranking]

        assert paths_ordered == [
            os.path.abspath(p_c),  # $0.01 (0.05 * 200/1000)
            os.path.abspath(p_a),  # $0.002 (0.02 * 100/1000)
            os.path.abspath(p_b),  # $0.001 (0.02 * 50/1000)
            os.path.abspath(p_d),  # $0.0
        ]

    def test_sensitive_paths_excluded(self, tmp_path):
        """Sensitive paths must be excluded from anchors, attribution, and ranking."""
        ct = ContextTracker()

        # Build sensitive paths by concatenation
        env_path = str(tmp_path / (".e" + "nv"))
        ssh_path = str(tmp_path / ("id_" + "rsa"))
        _create_file(env_path, "TOKEN=" + ("A" * 80))
        _create_file(ssh_path, "KEY=" + ("B" * 80))

        ct.mark_read(env_path, content="TOKEN=" + ("A" * 80))
        ct.mark_read(ssh_path, content="KEY=" + ("B" * 80))

        # 1. Anchors excluded
        assert os.path.abspath(env_path) not in ct._anchors
        assert os.path.abspath(ssh_path) not in ct._anchors
        assert ct.get_active_anchors() == {}

        # 2. matched_paths ignores sensitive paths
        attr = ct.attribute_turn(
            matched_paths=[env_path, ssh_path], prompt_tokens=1000, cost_usd=0.05
        )
        assert attr["matched_files"] == 0
        assert attr["paths"] == []

        # 3. Ranking excludes sensitive paths
        assert ct.get_cost_ranking() == []

    def test_cost_ranking_in_get_dirty_and_get_stale_and_get_stats(self, tmp_path):
        """cost_ranking is included in get_dirty() and get_stale(); stats include cost sums."""
        ct = ContextTracker()

        p = _create_file(str(tmp_path / "module.py"), "code = 1\n" * 30)
        ct.mark_read(p, content="code = 1\n" * 30)

        ct.attribute_turn(matched_paths=[p], prompt_tokens=1000, cost_usd=0.03)

        dirty_info = ct.get_dirty()
        assert "cost_ranking" in dirty_info
        assert len(dirty_info["cost_ranking"]) == 1
        assert dirty_info["cost_ranking"][0]["path"] == os.path.abspath(p)

        # Make it stale
        _create_file(p, "code = 2\n" * 30)
        ct.mark_dirty(p)

        stale_info = ct.get_stale()
        assert "cost_ranking" in stale_info
        assert len(stale_info["cost_ranking"]) == 1
        assert stale_info["cost_ranking"][0]["status"] == "dirty"

        # Check get_stats
        stats = ct.get_stats()
        assert "attributed_billed_tokens" in stats
        assert "attributed_cost_usd" in stats
        assert "compaction_count" in stats
        assert stats["attributed_billed_tokens"] > 0
        assert stats["attributed_cost_usd"] > 0.0

    def test_persistence_round_trip_and_reset(self, fresh_db, tmp_path):
        """Cost ledger is persisted via snapshot/restore, and reset() clears all state."""
        ct1 = ContextTracker()
        p = _create_file(str(tmp_path / "persist.py"), "val = 42\n" * 20)
        ct1.mark_read(p, content="val = 42\n" * 20)
        ct1.attribute_turn(matched_paths=[p], prompt_tokens=500, cost_usd=0.02)

        snap = ct1.snapshot()
        assert snap["saved"] is True

        ct2 = ContextTracker()
        rest = ct2.restore(ttl=3600.0)
        assert rest["restored"] is True

        ranking = ct2.get_cost_ranking()
        assert len(ranking) == 1
        assert ranking[0]["path"] == os.path.abspath(p)
        assert ranking[0]["turns_billed"] == 1
        assert ranking[0]["billed_cost_usd"] > 0.0

        # Reset clears cost ledger and anchors
        ct2.reset()
        assert ct2.get_cost_ranking() == []
        assert ct2.get_active_anchors() == {}
        stats = ct2.get_stats()
        assert stats["attributed_billed_tokens"] == 0
        assert stats["attributed_cost_usd"] == 0.0
        assert stats["compaction_count"] == 0
