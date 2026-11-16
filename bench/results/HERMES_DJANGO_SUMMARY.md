# Hermes Django 5.1.4 Endurance Benchmark Summary (Option A & Option B)

- **Harness:** Hermes (stateless loop)
- **Model:** `deepseek/deepseek-v4.1-flash-1017` (`temperature=0.0`)
- **Repo:** Django 5.1.4 (20 sequential bug fixes in one continuous session)
- **Arms:** `baseline` (no ToolRecall) vs `tr_delta_dedup` (`tr_optin`: `incremental_reads`, `mcp_delta`, `mcp_dedup`, `stabilize_prefix`, clean-file eviction)
- **Prefix-cache busts (`cache_bust_tokens`):** `0` across all 12 runs

## Option A — Realistic `offset`/`limit` Reads (`bench/results/django_endurance_20261116T105841Z.json`)

| Seed | `baseline` Solved | `tr_delta_dedup` Solved | `baseline` Peak Turn | `tr_delta_dedup` Peak → Last | `baseline` Cost | `tr_delta_dedup` Cost | Saved (`$`, `%`) |
|---:|---|---|---:|---:|---:|---:|---:|
| `s1` | `20/20` | `20/20` | `106,723` | `100,124` → `74,347` | `$0.13614` | `$0.11321` | `+$0.02293` (`16.8%`) |
| `s2` | `20/20` | `20/20` | `103,315` | `64,321` → `59,289` | `$0.11290` | `$0.09939` | `+$0.01351` (`12.0%`) |
| `s3` | `0/20` | `0/20` | `102,735` | `66,941` → `66,941` | `$0.13660` | `$0.08023` | `+$0.05637` (`41.3%`) |

- **Paired median saving:** `+$0.0229` (`95% CI: [$0.0135, $0.0564]`)
- **Crossover turn:** ~Turn 33 (`bug_9` on `s1`)

## Option B — Heavy-Context Upper Bound (`--full-reads`, `bench/results/django_endurance_20261116T114441Z.json`)

| Seed | `baseline` Solved | `tr_delta_dedup` Solved | `baseline` Peak Turn | `tr_delta_dedup` Peak → Last | `baseline` Cost | `tr_delta_dedup` Cost | Saved (`$`, `%`) |
|---:|---|---|---:|---:|---:|---:|---:|
| `s1` | `20/20` | `20/20` | `321,146` | `61,666` → `35,915` | `$0.19837` | `$0.04307` | `+$0.15530` (`78.3%`) |
| `s2` | `20/20` | `20/20` | `325,484` | `61,773` → `35,979` | `$0.18108` | `$0.04245` | `+$0.13863` (`76.6%`) |
| `s3` | `20/20` | `20/20` | `358,402` | `61,619` → `42,302` | `$0.20992` | `$0.04915` | `+$0.16077` (`76.6%`) |

- **Paired median saving:** `+$0.1553` (`95% CI: [$0.1386, $0.1608]`)
- **Crossover turn:** Turn 7 (`bug_5` on `s1`)

## Additional Hermes Suites (`bench/results/hermes_real_suites_20261116T133052Z.json`, 3 seeds, `0` cache busts)

| Suite | Turns | `baseline` Cost (s1/s2/s3) | `tr_delta_dedup` Cost (s1/s2/s3) | Peak Context (`Base → TR`) | Takeaway |
|---|---:|---|---|---|----|
| `null_qa` (Gate 1) | 1 | `$0.00004 / $0.00005 / $0.00006` | `$0.00006 / $0.00006 / $0.00005` | `64 → 65` | Gate 1 PASS (`~0%` overhead) |
| `unfamiliar_repo` (`ledgerflow`, `offset`/`limit`) | 12–14 | `$0.00446 / $0.00423 / $0.00472` | `$0.00423 / $0.00509 / $0.00477` | `20K–21K → 24K–27K` | Too short (`<33` turns): break-even / slight overhead |
| `mcp_blob` (whole CI log + schema blobs) | 14 | `$0.00892 / $0.00750 / $0.00854` | `$0.00790 / $0.00842 / $0.00759` | **`46K → 36K` (`-21%`)** | Cuts peak context ~21% every seed; saves ~11% on s1 & s3 |

## 60-Bug (~300–357 Turn) Neutral-Repo Endurance (`--rounds 3`, `offset`/`limit` enabled, `s1`, `0` cache busts)

- **`baseline`** (`357` calls, `bench/data/usage-django60-hermes-baseline-s1.csv`): `60/60` solved, `36,405,716` prompt tokens, `peak_turn=159,839` (`last_turn=159,839`), **`$0.44616`**
- **`tr_delta_dedup`** (`299` calls, `bench/results/django_endurance_20261116T140000Z.json` + `bench/data/usage-django60-hermes-tr_optin-s1.csv`): `60/60` solved, `14,940,136` prompt tokens (**`-59.0%`**), `peak_turn=94,898` → `last_turn=79,924` (**`-50.0%`** final context), **`$0.21596`** (**`-51.6%` billed cost, `+$0.2302` saved**)


