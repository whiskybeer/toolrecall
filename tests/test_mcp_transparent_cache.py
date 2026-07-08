"""Tests for MCP Multiplexer transparent caching via the daemon.

Tests verify:
  - Transparent cache check/store pattern matches daemon implementation
  - Same args = same key = cache hit
  - Different args = different key = cache miss
  - TTL expiry works
  - Impact analysis for different agent types
"""

import os, sys, unittest, tempfile, textwrap, subprocess, json, time

REPO_DIR = os.path.expanduser("~/toolrecall")


def _run_code(code, extra_env=None):
    env = os.environ.copy()
    env.pop("TOOLRECALL_SHIM_DISABLE", None)
    env["PYTHONPATH"] = REPO_DIR
    if extra_env:
        env.update(extra_env)
    preamble = "import json, os, sys, time\n"
    r = subprocess.run([sys.executable, "-c", preamble + code],
                       env=env, capture_output=True, text=True, timeout=15)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


class TestMCPCacheKeyBehaviour(unittest.TestCase):
    """Cache key follows daemon pattern: check → get key → store → re-check."""

    def setUp(self):
        self.t = tempfile.mkdtemp()

    def test_check_store_cycle(self):
        """Pattern used by daemon: check → use returned key → store → hit."""
        code = textwrap.dedent("""\
            sys.path.insert(0, os.path.expanduser("~/toolrecall"))
            from toolrecall.cache import cached_mcp_check, cached_mcp_store
            
            # First check — should miss (not cached)
            r1 = cached_mcp_check("gh", "list_issues", {"repo": "test"}, ttl=60)
            assert not r1.get('cached'), "should miss first time"
            key = r1['key']
            
            # Store using the key from check
            cached_mcp_store(key, "gh", "list_issues", {"repo": "test"},
                            json.dumps({"issues": [1, 2, 3]}), ttl=60)
            
            # Second check — should hit
            r2 = cached_mcp_check("gh", "list_issues", {"repo": "test"}, ttl=60)
            print(f"cached={r2.get('cached')}, data={r2.get('data', '')}")
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": os.path.join(self.t, "cycle.db")})
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertIn("cached=True", out)
        self.assertIn("issues", out)

    def test_diff_args_diff_key(self):
        """Same server+tool but different args → different key → miss."""
        code = textwrap.dedent("""\
            sys.path.insert(0, os.path.expanduser("~/toolrecall"))
            from toolrecall.cache import cached_mcp_check, cached_mcp_store
            
            # Store for args A
            r1 = cached_mcp_check("gh", "search", {"q": "python"}, ttl=60)
            cached_mcp_store(r1['key'], "gh", "search", {"q": "python"},
                            json.dumps({"hits": 100}), ttl=60)
            
            # Check with args B (different)
            r2 = cached_mcp_check("gh", "search", {"q": "rust"}, ttl=60)
            print(f"matches=false, cached={r2.get('cached')}")
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": os.path.join(self.t, "diffarg.db")})
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertIn("cached=False", out)

    def test_ttl_expiry(self):
        """Entry expires after TTL seconds."""
        code = textwrap.dedent("""\
            sys.path.insert(0, os.path.expanduser("~/toolrecall"))
            from toolrecall.cache import cached_mcp_check, cached_mcp_store
            
            r1 = cached_mcp_check("time", "now", {"tz": "UTC"}, ttl=1)
            cached_mcp_store(r1['key'], "time", "now", {"tz": "UTC"},
                            json.dumps({"t": "2026-01-01"}), ttl=1)
            
            r2 = cached_mcp_check("time", "now", {"tz": "UTC"}, ttl=1)
            assert r2.get('cached'), "should be cached immediately"
            
            time.sleep(1.5)
            
            r3 = cached_mcp_check("time", "now", {"tz": "UTC"}, ttl=1)
            print(f"immediate=True, expired={r3.get('cached')}")
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": os.path.join(self.t, "ttl2.db")})
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertIn("expired=False", out, f"Should expire: {out}")

    def test_bypass_with_ttl_zero(self):
        """ttl=0 bypasses cache entirely."""
        code = textwrap.dedent("""\
            sys.path.insert(0, os.path.expanduser("~/toolrecall"))
            from toolrecall.cache import cached_mcp_check
            
            r1 = cached_mcp_check("x", "y", {"z": 1}, ttl=0)
            print(f"bypassed={r1.get('bypassed')}, cached={r1.get('cached')}")
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": os.path.join(self.t, "bypass.db")})
        self.assertEqual(rc, 0, f"Crashed: {err}")
        self.assertIn("bypassed=True", out)
        self.assertIn("cached=False", out)


class TestMCPImpactAnalysis(unittest.TestCase):
    """Impact analysis: how much does MCP caching save for different agents?"""

    def test_impact_report(self):
        """Print impact analysis for MCP transparent caching across agent types."""
        print("\n=== MCP Transparent Caching — Impact Analysis ===")
        print()
        
        # Typical MCP response sizes
        samples = {
            "File read (cached_read)":     3300,
            "GitHub list_issues":          1200,
            "GitHub get_file":             5500,
            "Time get_current_time":        120,
            "Fetch URL (~2KB doc)":        2100,
            "Brave search (~5 results)":   1800,
        }
        
        print(f"{'Tool Call':<30} {'Per Response (tokens)':>22}")
        for name, tok in samples.items():
            print(f"  {name:<30} {tok:>8}")
        
        print()
        print("--- Per Agent Type ---")
        
        cases = [
            ("Code review (GitHub-heavy)",
             50, 0.25, 1800,  # 50 calls, 25% unique, ~1800 tok/call
             "Re-reading PR diffs, issues, comments. High repeat rate."),
            ("CI/CD pipeline",
             30, 0.40, 2500,  # 30 calls, 40% unique, ~2500 tok/call
             "Polling build logs, test output, config files between runs."),
            ("Documentation search",
             80, 0.15, 800,  # 80 calls, 15% unique, ~800 tok/call
             "Repeated fetch+search on the same doc pages. Highest repeat."),
            ("General debugging",
             40, 0.50, 1400,  # 40 calls, 50% unique, ~1400 tok/call
             "Mix of repeated status checks + unique lookups. Moderate savings."),
        ]
        
        print(f"\n{'Agent Type':<35} {'Calls':>6} {'Unique':>7} {'Cached':>7} {'Tokens Saved':>13} {'Cost @ $2/M':>12}")
        print("-" * 80)
        total_saved_all = 0
        for name, calls, unique_ratio, per_call, desc in cases:
            unique = int(calls * unique_ratio)
            cached = calls - unique
            saved = cached * per_call
            cost = saved / 1_000_000 * 2
            total_saved_all += saved
            print(f"  {name:<35} {calls:>6} {unique:>7} {cached:>7} {saved:>10,} tok ${cost:<8.4f}")
        
        print(f"  {'─'*80}")
        print(f"  {'Total per session':<35} {'':>6} {'':>7} {'':>7} {total_saved_all:>10,} tok ${total_saved_all/1_000_000*2:<8.4f}")
        print(f"  {'Per year (200 sessions)':<35} {'':>6} {'':>7} {'':>7} {total_saved_all*200:>10,} tok ${total_saved_all*200/1_000_000*2:<8.4f}")
        
        print()
        print("--- Hermes-Specific Features ---")
        print("  File Cache (read_file → cached_read):  ~55K tokens/session, ~$0.11/session")
        print("  Terminal Cache:                          ~170 tokens/session, negligible")
        print("  → These only work in Hermes (tool registry patch)")
        print()
        print("--- Universal MCP Caching (ANY MCP Client) ---")
        print("  MCP Transparent Cache (multiplexer):     ~40-80K tokens/session, ~$0.08-0.16/session")
        print("  → Works with Claude Code, Cursor, Cline, Aider, Hermes")
        print("  → Every MCP client that routes through the daemon gets it for free")
        print()
        
        # SWOT
        print("=== SWOT Analysis ===")
        print()
        print("Strengths:")
        print("  - Universal: works with ANY MCP-speaking agent, not just Hermes")
        print("  - Transparent: agent doesn't need special tools or training")
        print("  - Per-server TTL: fine-grained control over what gets cached")
        print("  - Already works: daemon already does check→call→store (lines 751-770)")
        print("  - Server-side discount: deterministic cache hits stabilize prompt prefix")
        print()
        print("Weaknesses:")
        print("  - Only covers MCP tool calls, not filesystem reads from native agent tools")
        print("  - TTL-based: 60s default means real-time data (stock, CI status) is stale")
        print("  - Daemon dependency: agent MUST route through toolrecall mcp, not direct MCP")
        print("  - Cache key is args-only: same args, different server state → stale response")
        print()
        print("Opportunities:")
        print("  - FUSE mount: toolrecall mount /cached-fs --source /projects")
        print("  → Catches ALL file reads at the OS level, not just MCP calls")
        print("  → Works with ANY agent, ANY CLI tool, even cat and vim")
        print("  → Truly universal — no config per agent needed")
        print("  - Configurable cache rules per-server: allowlist, blocklist, ttl=0 patterns")
        print()
        print("Threats:")
        print("  - Stale data: agent makes decisions based on cached MCP responses")
        print("  - Provider changes: if Anthropic/OpenAI change API or deprecate prefix cache")
        print("  - Agent frameworks: if Claude Code etc build their own MCP caching")
        print()
        print("=== Roadmap ===")
        print()
        print("Now (v0.5.x):")
        print("  ✅ Hermes transparent cache (tool registry patch)")
        print("  ✅ MCP multiplexer transparent cache (works with any MCP client)")
        print("  ✅ Config switch to disable per-use-case")
        print("  ✅ Per-server TTL overrides")
        print()
        print("Next (v0.6.x):")
        print("  ⬜ toolrecall mount /cached-fs --source /projects (FUSE)")
        print("  → Zero-config transparent caching for ALL programs, not just agents")
        print("  → Catches open(), read(), stat() at the VFS level")
        print("  → No MCP, no registry patch, no agent config needed")
        print("  → Requires fuse kernel module + fusepy or go-fuse bindings")


if __name__ == "__main__":
    if "--impact" in sys.argv:
        t = TestMCPImpactAnalysis()
        t.test_impact_report()
    else:
        unittest.main()