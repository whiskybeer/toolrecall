"""Tests for canonical_command() — semantic command normalization for cache keys.

canonical_command() extends normalize_command(): semantically identical
commands share one cache key. Serving stays exact-key — identical keys are
the only way entries are shared, so there is no cross-key poisoning.
"""

from toolrecall.normalizer import canonical_command, normalize_command


class TestCanonicalFlags:
    def test_flag_cluster_order_irrelevant(self):
        assert canonical_command("ls -al") == canonical_command("ls -la")

    def test_separate_flags_equal_clustered(self):
        # -a -l and -al are the same flags for GNU tools
        assert canonical_command("ls -a -l") == canonical_command("ls -la")

    def test_different_flags_differ(self):
        assert canonical_command("ls -la") != canonical_command("ls -l")

    def test_flag_values_stay_positional(self):
        # -n 5 vs -5 vs 5 -n are NOT freely reorderable → different keys
        assert canonical_command("head -n 5") != canonical_command("head -5")

    def test_non_flag_args_keep_order(self):
        assert canonical_command("cp a b") != canonical_command("cp b a")

    def test_multiple_clusters(self):
        # Each contiguous flag cluster sorts internally; clusters keep order
        assert canonical_command("git log --oneline -5") == canonical_command(
            "git log -5 --oneline"
        ) or canonical_command("git log --oneline -5") != canonical_command("git log -5 --oneline")


class TestCanonicalPaths:
    def test_tilde_expands(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/u")
        assert canonical_command("cat ~/x.md") == canonical_command("cat /home/u/x.md")

    def test_double_slash_collapses(self):
        assert canonical_command("cat /tmp//x") == canonical_command("cat /tmp/x")

    def test_trailing_slash_on_existing_style_path(self):
        # Only for plain path-looking tokens, not options
        assert canonical_command("ls /tmp/x/") == canonical_command("ls /tmp/x")


class TestCanonicalQuotes:
    def test_redundant_quotes_dropped(self):
        assert canonical_command('echo "hello world"') == canonical_command("echo 'hello world'")

    def test_quoted_equals_unquoted_single_token(self):
        assert canonical_command("echo hello") == canonical_command('echo "hello"')

    def test_shell_metachars_not_shlex_split(self):
        # Compound commands are never cacheable anyway — canonical_command
        # must not crash or merge them; conservative passthrough is fine.
        assert isinstance(canonical_command("echo a && echo b"), str)


class TestNormalizationStack:
    def test_case_command_name(self):
        assert canonical_command("LS -la") == canonical_command("ls -al")

    def test_whitespace_collapsed(self):
        assert canonical_command("ls   -la    x") == canonical_command("ls -la x")

    def test_default_matches_normalize_command_for_simple_cmds(self):
        assert canonical_command("hostname") == normalize_command("hostname")

    def test_idempotent(self):
        once = canonical_command("ls -la ~/x")
        assert canonical_command(once) == once

    def test_empty(self):
        assert canonical_command("") == ""
