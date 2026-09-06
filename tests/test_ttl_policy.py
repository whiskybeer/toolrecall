"""Tests for toolrecall.ttl_policy — SWR tiers, adaptive TTL, file-TTL resolution, fuzzy TTL.

Zero-dep policy helpers: all pure functions over stdlib types.
"""

import pytest

from toolrecall.ttl_policy import (
    SWR_FRESH,
    SWR_EXPIRED,
    SWR_SERVE_STALE,
    adaptive_ttl,
    classify_expiry,
    fuzzy_ttl_for,
    resolve_file_ttl,
)


class TestClassifyExpiry:
    def test_fresh(self):
        now = 1000.0
        assert classify_expiry(now + 60, now, 0) == SWR_FRESH
        assert classify_expiry(now + 60, now, 300) == SWR_FRESH

    def test_stale_within_swr_window(self):
        now = 1000.0
        # expired 10s ago, SWR window 60s → serve stale
        assert classify_expiry(now - 10, now, 60) == SWR_SERVE_STALE

    def test_stale_beyond_swr_window(self):
        now = 1000.0
        # expired 120s ago, SWR window 60s → expired
        assert classify_expiry(now - 120, now, 60) == SWR_EXPIRED

    def test_swr_off_means_immediate_expiry(self):
        now = 1000.0
        assert classify_expiry(now - 0.001, now, 0) == SWR_EXPIRED

    def test_boundary_exactly_expired_with_swr(self):
        now = 1000.0
        # exactly at expiry → stale (expires_at > now is fresh; == now is not)
        assert classify_expiry(now, now, 60) == SWR_SERVE_STALE

    def test_boundary_at_swr_window_edge(self):
        now = 1000.0
        # expired exactly swr seconds ago → at edge, still within (inclusive)
        assert classify_expiry(now - 60, now, 60) == SWR_SERVE_STALE

    def test_expired_beyond_window(self):
        now = 1000.0
        assert classify_expiry(now - 60.001, now, 60) == SWR_EXPIRED


class TestAdaptiveTtl:
    def test_streak_zero_is_base(self):
        assert adaptive_ttl(300, 0, 2.0, 86400) == 300

    def test_growth(self):
        assert adaptive_ttl(300, 1, 2.0, 86400) == 600
        assert adaptive_ttl(300, 2, 2.0, 86400) == 1200

    def test_cap_enforced(self):
        assert adaptive_ttl(300, 20, 2.0, 86400) == 86400

    def test_factor_one_is_constant(self):
        assert adaptive_ttl(300, 5, 1.0, 86400) == 300

    def test_factor_below_one_never_grows(self):
        assert adaptive_ttl(300, 5, 0.5, 86400) == 300

    def test_negative_streak_treated_as_zero(self):
        assert adaptive_ttl(300, -3, 2.0, 86400) == 300

    def test_zero_base_ttl(self):
        assert adaptive_ttl(0, 5, 2.0, 86400) == 0


class TestResolveFileTtl:
    @pytest.fixture
    def cfg(self):
        return {
            "file_ttls": {
                "/exact/path.py": 3600,
                "~/notes/*.md": 600,
                "/tmp/zero.txt": 0,
            },
            "file_ttl": -1,
        }

    def test_exact_match_wins(self, cfg):
        assert resolve_file_ttl("/exact/path.py", cfg) == 3600

    def test_glob_match(self, cfg, monkeypatch):
        monkeypatch.setenv("HOME", "/home/u")
        assert resolve_file_ttl("/home/u/notes/a.md", cfg) == 600

    def test_global_fallback(self, cfg):
        assert resolve_file_ttl("/other/file.c", cfg) == -1

    def test_zero_never_cache(self, cfg):
        assert resolve_file_ttl("/tmp/zero.txt", cfg) == 0

    def test_missing_keys_default_negative_one(self):
        assert resolve_file_ttl("/any", {}) == -1

    def test_empty_config_default(self):
        assert resolve_file_ttl("/any", None) == -1

    def test_glob_beats_global(self, cfg, monkeypatch):
        monkeypatch.setenv("HOME", "/home/u")
        cfg["file_ttl"] = 30
        assert resolve_file_ttl("/home/u/notes/b.md", cfg) == 600

    def test_exact_beats_glob(self, cfg):
        cfg["file_ttls"]["/home/u/notes/a.md"] = 5
        assert resolve_file_ttl("/home/u/notes/a.md", cfg) == 5

    def test_tilde_expansion_in_keys(self, cfg, monkeypatch):
        monkeypatch.setenv("HOME", "/home/u")
        # key with ~ matched against absolute path
        assert resolve_file_ttl("/home/u/notes/x.md", cfg) == 600


class TestFuzzyTtlFor:
    PATTERNS = {"git status": 30, "ls -la": 300, "whoami": 3600}

    def test_exact_match_first(self):
        ttl, matched = fuzzy_ttl_for("git status", self.PATTERNS, 0.85)
        assert ttl == 30 and matched is True

    def test_near_match_inherits_ttl(self):
        # difflib ratio("git status --short", "git status") ≈ 0.714 → needs
        # threshold below that; 0.85 default intentionally rejects it.
        ttl, matched = fuzzy_ttl_for("git status --short", self.PATTERNS, 0.7)
        assert matched is True
        assert ttl == 30

    def test_default_threshold_rejects_extension(self):
        ttl, matched = fuzzy_ttl_for("git status --short", self.PATTERNS)  # 0.85 default
        assert matched is False and ttl is None

    def test_below_threshold_not_matched(self):
        ttl, matched = fuzzy_ttl_for("docker run --rm -it alpine sh", self.PATTERNS, 0.85)
        assert matched is False and ttl is None

    def test_high_threshold_disables_fuzzy(self):
        ttl, matched = fuzzy_ttl_for("git status --short", self.PATTERNS, 1.0)
        assert matched is False and ttl is None

    def test_empty_patterns(self):
        ttl, matched = fuzzy_ttl_for("anything", {}, 0.85)
        assert matched is False and ttl is None


class TestSwrConstants:
    def test_tiers_distinct(self):
        assert len({SWR_FRESH, SWR_EXPIRED, SWR_SERVE_STALE}) == 3
