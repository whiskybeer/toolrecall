# ToolRecall Crossover Benchmark (3-Tier, 2×2 Matrix + 5-Gate Verification)

Measures the **crossover curve** between provider prefix caching (Arm A: native) and ToolRecall dedup + staleness tracking (Arm B: isolated ToolRecall proxy + hook) across both a **neutral OSS repo (`django`)** and **ToolRecall (`toolrecall`)**.

## Design

- **Sweet-spot signature**: `≥100` turns, `≤10` hot files, `≥2` re-reads per request, large repeated blocks.
- **Matrix**: `2 repos (django, toolrecall) × 2 workloads (short graded, ≥150-turn hot-file) × 2 arms × 3 reps = 24 runs` + `null_qa` (Gate 1).
- **Isolation**: Arm B runs its own `HOME`, socket, SQLite DB, and `proxy_usage.csv` via `rig.sh`, and supports separate API keys (`AB_API_KEY_A`, `AB_API_KEY_B`). Live `~/.toolrecall/` is never touched.
- **Per-turn instrumentation (`instrument.py`)**: `prompt_tokens`, `cache_read`, `cache_creation`, `stub_bytes_served`, `distinct_block_bytes`, `repeat_share = repeated_block_bytes / distinct_block_bytes`.
- **Component decomposition (`analyze_crossover.py`)**:
  `net_usd = dedup_avoided_usd - cache_creation_penalty_usd - completion_delta_usd`
  reported separately per turn-budget bucket alongside the crossover turn count and paired median + 95% bootstrap CI.

## Usage

### 1. Tier 1 (Free, $0 — replay before spending)

```bash
python3 bench/crossover/tier1_replay.py
python3 bench/crossover/tier1_replay.py --claude-dir ~/.claude/projects
```

### 2. Dry-Run (Validate matrix, worktrees, and cost estimate)

```bash
python3 bench/crossover/run_crossover.py --dry-run
```

### 3. Start Isolated Arm B Rig

```bash
bash bench/crossover/rig.sh start
# When finished:
bash bench/crossover/rig.sh stop
```

### 4. Run Tier 2 / Tier 3 / Null Arm

```bash
export AB_MODEL=sonnet
# Optional separate billing keys:
# export AB_API_KEY_A=...
# export AB_API_KEY_B=...

python3 bench/crossover/run_crossover.py \
  --reps 3 --port 8595 --usage-csv ~/bench-data/crossover-rig/proxy_usage.csv
```

### 5. Analyze & Check 5-Gate Sequence

```bash
python3 bench/crossover/analyze_crossover.py ~/bench-data/crossover-out
```

Gates checked before any number can be quoted:
1. **Null arm**: `null_qa` paired net $ 95% bootstrap CI contains `0`.
2. **Equal work**: verification/hidden tests pass in both arms on the same task set.
3. **Arm verified from data**: Arm B shows `stub_bytes_served > 0` when `repeat_share > 0.05`; Arm A has `0`.
4. **Order counterbalanced**: `AB` and `BA` run orders balanced across `(task, rep)`.
5. **Paired median + bootstrap CI**: computed over paired runs.
