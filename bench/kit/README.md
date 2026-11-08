# ToolRecall × Antigravity — One-Click Windows Test Kit

Everything needed to install ToolRecall on a Windows machine with Antigravity
and measure the benefits — **fully automated, logged, no manual steps**.

## What's in the zip

| File | Purpose |
|---|---|
| `toolrecall-0.8.22+ag1-py3-none-any.whl` | ToolRecall build with the Antigravity adapter (install on the Windows machine) |
| `run_ag_tests.ps1` | **The one-click script.** Installs, sets up, runs all tests, logs everything |
| `verify_antigravity_windows.ps1` | Read-only environment verification (called by the runner; standalone use fine) |
| `bench/antigravity_win.py` | The A/B benchmark (called by the runner; standalone use fine) |
| `README.md` | This file |

## How to run (one click)

1. Unzip anywhere (e.g. `C:\Users\<you>\Downloads\toolrecall-ag-kit\`)
2. Open PowerShell in that folder
3. Run:

```powershell
powershell -ExecutionPolicy Bypass -File run_ag_tests.ps1
```

That's it. The script:

- Checks Python + Antigravity presence
- Installs the wheel (`pip install --user`)
- Runs `toolrecall setup` (daemon as scheduled task + Antigravity MCP config)
- Verifies daemon health + MCP config entry
- Runs the read-only verify script
- **T1** — MCP bridge handshake (does Antigravity's config actually reach our server?)
- **T2** — live fetch + ETag 304 revalidation against httpbin.org (proves
  conditional-revalidation saves the full body transfer)
- **T3** — cached_tool A/B benchmark (5 seeds × 2 passes × 3 slots): control
  (no cache) vs treatment (TTL 300), reporting live-call counts avoided and
  warm-vs-cold latency
- **T4/T4b** — replay record → replay list → offline replay lookup
- **T5** — freshness metadata (`cached=True, age_s=...` on the second call)
- **T6** — scoped invalidation (`--url`, `--server`)

Every step writes to a timestamped run log + a JSON results file under
`%TEMP%\toolrecall-ag-test-<stamp>\`. The final summary prints pass/fail and
tells you where the files are.

## Exit codes

- `0` — everything passed
- `1` — at least one test failed (check the log; the summary names them)

## What the results mean (honest interpretation)

- **T3 is the headline number**: "control made N live HTTP calls, treatment
  made M" — the difference is what ToolRecall avoided. Warm latency ~5ms vs
  ~1400ms live.
- **T2 proves the ETag story**: a 304 revalidation means the server confirmed
  content-unchanged without sending the body again.
- **Token savings are NOT measured here** — Antigravity's inference is
  Google-hosted with no endpoint override, so billed LLM tokens cannot be
  intercepted. What IS measured: external calls avoided, wall-clock, and
  bandwidth. For a real billed-token number, the companion ADK proxy A/B
  (stateless case, `GEMINI_BASE_URL` → `toolrecall serve`) is the way —
  different adapter, documented in docs/antigravity.md.
- **Free tier is fine**: none of these tests consume Antigravity's model quota
  meaningfully; the bench traffic goes to httpbin.org, not Google.

## Requirements

- Windows 10/11, Python 3.11+ on PATH, Antigravity (any surface) installed
- Internet for T2/T3 (httpbin.org); everything else is local
- ~3–5 minutes runtime, ~50 MB disk

## Troubleshooting

- `toolrecall not found` after install → open a NEW PowerShell window (PATH
  refresh), or use `python -m toolrecall` instead.
- Antigravity not installed → tests still run; only T1 (MCP config in
  Antigravity's config file) will show a warning.
- `httpbin.org` unreachable → T2/T3 fail but T1/T4–T6 still validate the
  local plumbing; re-run when online.
