"""
O(n²) Context Window Scalability Benchmark

Measures how context window growth scales with agent turns,
and whether ToolRecall mitigates the O(n²) attention cost.

Key insight: ToolRecall caches OS-level I/O (file reads, terminal commands)
but does NOT prevent the agent from appending tool output to context.
The O(n²) attention cost is at the LLM API level — ToolRecall enables
deterministic payloads for server-side prefix caching (90% discount),
but the context window still grows linearly with turns.

Three scenarios:
  1. Baseline (no cache) — every turn reads files from disk
  2. ToolRecall (cache hits) — repeat reads served from cache
  3. ToolRecall + prompt caching — deterministic payloads unlock provider discount

Each scenario simulates N agents, each doing T turns of "read file → produce output"
"""
import os, sys, time, json, math, statistics, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Configuration ──────────────────────────────────────────────
REPO = os.path.expanduser("~/toolrecall")
AGENT_COUNTS = [1, 2, 5, 10, 20]          # Number of sequential agents
TURNS_PER_AGENT = [5, 10, 20, 30]          # Turns each agent takes
SHARED_FILES = 5                           # Files all agents read (high cache value)
UNIQUE_FILES_PER_AGENT = 2                 # Files only one agent reads (cold misses)

# Test files (real project files)
ALL_FILES = [
    "README.md", "pyproject.toml",
    "toolrecall/cache.py", "toolrecall/daemon.py",
    "toolrecall/cli.py", "toolrecall/config.py",
    "toolrecall/client.py", "toolrecall/docs.py",
]

def get_file_sizes():
    """Get content size in tokens (tiktoken-compatible estimate: ~4 chars/token)."""
    sizes = {}
    for fname in ALL_FILES:
        fpath = os.path.join(REPO, fname)
        if os.path.exists(fpath):
            with open(fpath) as f:
                content = f.read()
            sizes[fname] = max(1, len(content) // 4)  # rough token count
    return sizes

FILE_SIZES = get_file_sizes()
print(f"File sizes (tokens): {json.dumps(FILE_SIZES, indent=2)}")
total_token_base = sum(FILE_SIZES.values())
print(f"Total base tokens: {total_token_base:,}")

# ── Simulation Engine ─────────────────────────────────────────

class AgentSimulator:
    """Simulates an agent's context window growth across turns."""

    def simulate_no_cache(self, agents, turns_per_agent):
        """
        Baseline: Every turn reads ALL files fresh from disk.
        Context window grows unbounded — every read appends tokens.
        
        Context growth per turn:
          Turn 1: read 5 shared + 2 unique = 7 files → 7*avg_file_size tokens
          Turn 2: read 7 files AGAIN → 7*avg_file_size MORE tokens
          Turn T: context = T * 7 * avg_file_size → O(N) growth
        
        Agent 1 ends with T * 7 * avg_file_size tokens in context
        Agent 2 starts FRESH but reads same shared files → same growth
        """
        results = {}
        for n_agents in agents:
            for t in turns_per_agent:
                scenario_key = f"A{n_agents:02d}_T{t:02d}"
                
                # Per-agent context size totals
                agent_context_tokens = []
                
                for agent_idx in range(n_agents):
                    context_tokens = 0
                    
                    for turn in range(t):
                        # Each turn: read shared files + this agent's unique files
                        files_this_turn = SHARED_FILES + UNIQUE_FILES_PER_AGENT
                        tokens_this_turn = 0
                        
                        for fi in range(files_this_turn):
                            fname = ALL_FILES[fi % len(ALL_FILES)]
                            tokens_this_turn += FILE_SIZES.get(fname, 100)
                        
                        # Agent also produces output tokens per turn
                        output_tokens = 200  # ~800 chars of reasoning + tool result
                        
                        # APPEND to context window (the O(n) growth)
                        context_tokens += tokens_this_turn + output_tokens
                    
                    agent_context_tokens.append(context_tokens)
                
                # Context window PER agent (what the LLM pays attention over)
                max_context = max(agent_context_tokens)
                avg_context = statistics.mean(agent_context_tokens)
                
                # O(n²): attention cost ~ context²
                # Total cost across all agents
                total_attention_cost = sum(c * c for c in agent_context_tokens)
                
                results[scenario_key] = {
                    "max_context_per_agent": max_context,
                    "avg_context_per_agent": avg_context,
                    "total_context_all_agents": sum(agent_context_tokens),
                    "total_attention_cost_O_n2": total_attention_cost,
                    "per_agent_contexts": agent_context_tokens,
                }
        
        return results

    def simulate_with_tr(self, agents, turns_per_agent):
        """
        With ToolRecall: repeat reads are cache hits.
        Context window still grows, BUT:
        - Turn 1 (miss): reads all 7 files → append tokens
        - Turn 2+ (hits): reads from cache → append tokens (still in context!)
        
        WAIT — ToolRecall doesn't prevent context bloat!
        The cache hit means "don't hit disk", but the AGENT still
        appends the result to context because it doesn't know it's cached.
        
        So how does TR help O(n²)?
        
        Answer: TR enables SERVER-SIDE PROMPT CACHING.
        If the tool output is byte-identical (deterministic),
        the LLM provider's prefix cache matches → 90% discount
        on the cached prefix portion.
        """
        results = {}
        for n_agents in agents:
            for t in turns_per_agent:
                scenario_key = f"A{n_agents:02d}_T{t:02d}"
                agent_context_tokens = []
                agent_prompt_cache_savings = []
                
                for agent_idx in range(n_agents):
                    context_tokens = 0
                    prompt_cache_hits = 0
                    prompt_cache_misses = 0
                    
                    for turn in range(t):
                        files_this_turn = SHARED_FILES + UNIQUE_FILES_PER_AGENT
                        tokens_this_turn = 0
                        
                        for fi in range(files_this_turn):
                            fname = ALL_FILES[fi % len(ALL_FILES)]
                            
                            # First turn → cold miss. Subsequent → warm hit.
                            if turn == 0:
                                tokens_this_turn += FILE_SIZES.get(fname, 100)
                                prompt_cache_misses += FILE_SIZES.get(fname, 100)
                            else:
                                # Cache hit: deterministic content → prefix cache match
                                # But tokens still enter context window
                                tokens_this_turn += FILE_SIZES.get(fname, 100)
                                prompt_cache_hits += FILE_SIZES.get(fname, 100)
                        
                        output_tokens = 200
                        context_tokens += tokens_this_turn + output_tokens
                    
                    agent_context_tokens.append(context_tokens)
                    agent_prompt_cache_savings.append(prompt_cache_hits)
                
                max_context = max(agent_context_tokens)
                avg_context = statistics.mean(agent_context_tokens)
                total_attention = sum(c * c for c in agent_context_tokens)
                
                # With 90% server-side prefix caching:
                # The first N tokens of each turn get 90% discount
                # But the O(n²) attention is still computed server-side
                # Only the INPUT TOKEN COST is reduced, not the compute
                total_prefix_savings = sum(agent_prompt_cache_savings)
                effective_token_cost_reduction = total_prefix_savings * 0.9
                
                results[scenario_key] = {
                    "max_context_per_agent": max_context,
                    "avg_context_per_agent": avg_context,
                    "total_attention_cost_O_n2": total_attention,
                    "total_prefix_cache_hits": total_prefix_savings,
                    "effective_token_discount": effective_token_cost_reduction,
                    "per_agent_contexts": agent_context_tokens,
                }
        
        return results

    def simulate_with_tr_and_drop(self, agents, turns_per_agent, drop_every=5):
        """
        The ACTUAL solution to O(n²): drop old context.
        
        With ToolRecall + "drop old context":
        - Agent reads files, works on them
        - Every `drop_every` turns, old file content is dropped from context
        - When needed again → re-read from cache (instant, 0 disk I/O)
        
        This bounds context growth: agent keeps only current active files.
        Context doesn't grow indefinitely — it oscillates.
        
        THIS is the real O(1) solution.
        """
        results = {}
        for n_agents in agents:
            for t in turns_per_agent:
                scenario_key = f"A{n_agents:02d}_T{t:02d}"
                agent_context_tokens = []
                total_misses = 0
                
                for agent_idx in range(n_agents):
                    context_tokens = 0
                    
                    for turn in range(t):
                        files_this_turn = SHARED_FILES + UNIQUE_FILES_PER_AGENT
                        tokens_this_turn = 0
                        
                        for fi in range(files_this_turn):
                            fname = ALL_FILES[fi % len(ALL_FILES)]
                            tokens_this_turn += FILE_SIZES.get(fname, 100)
                        
                        output_tokens = 200
                        
                        # Context drop: every N turns, reset to just current turn
                        if turn > 0 and turn % drop_every == 0:
                            context_tokens = tokens_this_turn + output_tokens
                            total_misses += 1
                        else:
                            context_tokens += tokens_this_turn + output_tokens
                    
                    agent_context_tokens.append(context_tokens)
                
                max_context = max(agent_context_tokens)
                avg_context = statistics.mean(agent_context_tokens)
                total_attention = sum(c * c for c in agent_context_tokens)
                
                results[scenario_key] = {
                    "max_context_per_agent": max_context,
                    "avg_context_per_agent": avg_context,
                    "total_attention_cost_O_n2": total_attention,
                    "re_reads_from_cache": total_misses,
                    "per_agent_contexts": agent_context_tokens,
                }
        
        return results

    def simulate_with_context_tracker(self, agents, turns_per_agent, drop_every=5, dirty_fraction=0.2):
        """
        Context Tracker model: dirty files kept, clean files dropped.
        
        More realistic than the blunt drop — the agent knows which files
        it edited (dirty) and which it just read (clean). Only clean files
        are dropped. Dirty files accumulate across turns until checkpoint.
        
        - dirtied_fraction: what fraction of files are written (made dirty)
          in each round. 0.2 means 1 in 5 files is written per turn.
        
        After each drop, agent re-reads clean files from cache (0 cost)."""
        results = {}
        for n_agents in agents:
            for t in turns_per_agent:
                scenario_key = f"A{n_agents:02d}_T{t:02d}"
                agent_context_tokens = []
                total_re_reads = 0
                
                for agent_idx in range(n_agents):
                    context_tokens = 0
                    dirty_set = set()
                    read_set = set()
                    
                    for turn in range(t):
                        files_this_turn = SHARED_FILES + UNIQUE_FILES_PER_AGENT
                        tokens_this_turn = 0
                        
                        for fi in range(files_this_turn):
                            fname = ALL_FILES[fi % len(ALL_FILES)]
                            tokens = FILE_SIZES.get(fname, 100)
                            
                            # Record read
                            read_set.add(fname)
                            
                            # Some files get written (become dirty)
                            hash_val = (agent_idx * 1000 + turn * 10 + fi)
                            if hash_val % int(1 / dirty_fraction) == 0:
                                dirty_set.add(fname)
                                # Actually read the file first (miss first time)
                                if fname not in read_set:
                                    pass  # first read
                            else:
                                pass  # just read, not written
                            
                            tokens_this_turn += tokens
                        
                        output_tokens = 200
                        
                        # Every drop_every turns: drop clean files
                        if turn > 0 and turn % drop_every == 0:
                            # Clean = read but not dirty
                            clean = read_set - dirty_set
                            # Keep only dirty files + current turn content
                            context_tokens = tokens_this_turn + output_tokens
                            # Add back dirty files that are still in context
                            for df in dirty_set:
                                context_tokens += FILE_SIZES.get(df, 100)
                            total_re_reads += len(clean)  # all clean files re-read from cache
                        else:
                            context_tokens += tokens_this_turn + output_tokens
                    
                    agent_context_tokens.append(context_tokens)
                
                max_context = max(agent_context_tokens)
                avg_context = statistics.mean(agent_context_tokens)
                total_attention = sum(c * c for c in agent_context_tokens)
                
                results[scenario_key] = {
                    "max_context_per_agent": max_context,
                    "avg_context_per_agent": avg_context,
                    "total_attention_cost_O_n2": total_attention,
                    "re_reads_from_cache": total_re_reads,
                    "per_agent_contexts": agent_context_tokens,
                }
        
        return results

    def run_full_benchmark(self):
        """Run all three scenarios and compare O(n²) scaling."""
        
        print("\n" + "=" * 70)
        print("  O(n²) CONTEXT WINDOW SCALABILITY BENCHMARK")
        print("=" * 70)
        print(f"\n  Configuration:")
        print(f"  - Agents: {AGENT_COUNTS}")
        print(f"  - Turns/agent: {TURNS_PER_AGENT}")
        print(f"  - Shared files: {SHARED_FILES}")
        print(f"  - Unique files/agent: {UNIQUE_FILES_PER_AGENT}")
        print(f"  - Turn output tokens: 200")
        
        all_results = {}
        
        for scenario_name, sim_fn in [
            ("no_cache", self.simulate_no_cache),
            ("with_tr", self.simulate_with_tr),
            ("with_tr_and_drop", self.simulate_with_tr_and_drop),
            ("with_context_tracker", self.simulate_with_context_tracker),
        ]:
            print(f"\n  ── Running: {scenario_name} ──")
            t0 = time.time()
            all_results[scenario_name] = sim_fn(AGENT_COUNTS, TURNS_PER_AGENT)
            elapsed = time.time() - t0
            print(f"  Done in {elapsed:.3f}s")
        
        # ── Report ────────────────────────────────────────────────
        self.print_report(all_results)
        
        # ── Summary ────────────────────────────────────────────────
        self.print_summary(all_results)
        
        return all_results

    def print_report(self, all_results):
        """Print detailed report for one key scenario: 10 agents × 20 turns."""
        print("\n" + "=" * 70)
        print("  DETAILED: 10 Agents × 20 Turns")
        print("=" * 70)
        
        key = "A10_T20"
        
        header = f"{'Scenario':<20} {'Max ctx/agent':>15} {'Total O(n²)':>18} {'O(n²) factor':>14}"
        print(f"\n  {header}")
        print(f"  {'─'*67}")
        
        baseline = all_results.get("no_cache", {}).get(key, {})
        baseline_O2 = baseline.get("total_attention_cost_O_n2", 1)
        
        for scenario_name, label in [
            ("no_cache", "Baseline (no cache)"),
            ("with_tr", "With TR (cache)"),
            ("with_tr_and_drop", "TR + blunt drop"),
            ("with_context_tracker", "TR + Context Tracker"),
        ]:
            r = all_results.get(scenario_name, {}).get(key, {})
            if not r:
                continue
            max_ctx = r.get("max_context_per_agent", 0)
            o2 = r.get("total_attention_cost_O_n2", 0)
            factor = o2 / baseline_O2 if baseline_O2 else 1
            print(f"  {label:<20} {max_ctx:>15,} {o2:>18,} {factor:>13.2f}x")
        
        print(f"\n  {'─'*67}")
        print(f"  {'TR + context drop solves O(n²)':<20} {'→ bounded context':>15} {'→ O(1) per agent':>18}")

    def print_summary(self, all_results):
        """Print scaling table across agent counts."""
        print("\n" + "=" * 70)
        print("  SCALING SUMMARY: 20 Turns, Varying Agents")
        print("=" * 70)
        
        t = 20  # fixed turns
        
        baseline = all_results.get("no_cache", {})
        tr = all_results.get("with_tr", {})
        tr_drop = all_results.get("with_tr_and_drop", {})
        tr_ct = all_results.get("with_context_tracker", {})
        
        header = (f"{'Agents':<8} | "
                  f"{'Baseline O(n²)':>16} | "
                  f"{'TR O(n²)':>14} | "
                  f"{'Drop O(n²)':>14} | "
                  f"{'CT O(n²)':>14} | "
                  f"{'TR saves':>10} | "
                  f"{'CT saves':>10}")
        print(f"\n  {header}")
        print(f"  {'─'*8}─┼─{'─'*16}─┼─{'─'*14}─┼─{'─'*14}─┼─{'─'*14}─┼─{'─'*10}─┼─{'─'*10}──")
        
        for n in AGENT_COUNTS:
            key = f"A{n:02d}_T{t:02d}"
            b = baseline.get(key, {}).get("total_attention_cost_O_n2", 0)
            tr_v = tr.get(key, {}).get("total_attention_cost_O_n2", 0)
            td = tr_drop.get(key, {}).get("total_attention_cost_O_n2", 0)
            ct = tr_ct.get(key, {}).get("total_attention_cost_O_n2", 0)
            
            tr_save = (b - tr_v) / b * 100 if b else 0
            ct_save = (b - ct) / b * 100 if b else 0
            
            print(f"  {n:<8} | {b:>16,} | {tr_v:>14,} | {td:>14,} | {ct:>14,} | {tr_save:>8.1f}% | {ct_save:>8.1f}%")
        
        print(f"\n  {'─'*80}")
        print(f"  {'NOTE: TR alone does NOT reduce O(n²) — it only enables server-side':>78}")
        print(f"  {'prefix caching (90% token discount). Context drop is the real fix.':>78}")


# ── Run ────────────────────────────────────────────────────────
if __name__ == "__main__":
    sim = AgentSimulator()
    results = sim.run_full_benchmark()
    
    # Save results
    out_path = os.path.join(os.path.dirname(__file__), "benchmark_on2_results.json")
    serializable = {}
    for scenario, data in results.items():
        serializable[scenario] = {
            k: {
                mk: mv for mk, mv in v.items()
                if mk != "per_agent_contexts"
            }
            for k, v in data.items()
        }
    with open(out_path, "w") as f:
        json.dump(serializable, f, indent=2)
    print(f"\n  Results saved to: {out_path}")