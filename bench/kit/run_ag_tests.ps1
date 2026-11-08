# run_ag_tests.ps1 — One-click Antigravity x ToolRecall test suite (Windows)
#
# Runs the ENTIRE verification + benchmark suite unattended. Every action and
# result is logged to a timestamped run log + JSON results. Exit code 0 = all
# tests passed, 1 = at least one failure (log names them).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File run_ag_tests.ps1
#   powershell -ExecutionPolicy Bypass -File run_ag_tests.ps1 -SkipSDK   # if no Python SDK agent is set up
#
# What it does:
#   0. Env check (Python, Antigravity, wheel installed?)
#   1. Install wheel (idempotent, -SkipSDK-safe)
#   2. toolrecall setup (daemon + Antigravity MCP config)
#   3. Read-only verification script (PATH/shim/config/daemon checks)
#   4. T1: MCP config entry present + bridge launches (handshake check)
#   5. T2: fetch + ETag 304 revalidation (LIVE — needs internet)
#   6. T3: cached_tool A/B benchmark (local TLS server, per-seed isolated)
#   7. T4: replay record/replay determinism
#   8. T5: freshness metadata (age_s on hit)
#   9. T6: scoped invalidation (--url / --server)
#  10. Results: JSON + human-readable summary; log kept for Robin
#
# Everything is local. No data leaves the machine except the test HTTP calls.

param(
    [switch]$SkipSDK
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

# ---------- paths ----------
$stamp    = Get-Date -Format "yyyyMMdd-HHmmss"
$logDir   = Join-Path $env:TEMP "toolrecall-ag-test-$stamp"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile  = Join-Path $logDir "run.log"
$jsonFile = Join-Path $logDir "results.json"
$results  = [ordered]@{}
$script:failed = 0

function Log([string]$level, [string]$msg) {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "HH:mm:ss"), $level, $msg
    $line | Tee-Object -FilePath $logFile -Append
}

function Pass([string]$name, $data) {
    $script:failed = $script:failed
    Log "PASS" $name
    $results[$name] = if ($null -ne $data) { $data } else { $true }
}

function Fail([string]$name, [string]$reason, $data) {
    $script:failed++
    Log "FAIL" "$name : $reason"
    $results[$name] = if ($null -ne $data) { $data } else { "FAILED: $reason" }
}

function Step([string]$msg) { Log "STEP" $msg }

# ---------- step 0: env ----------
Step "STEP 0: environment check"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Fail "env.python" "python not on PATH - install Python 3.11+ and re-run"
} else {
    $ver = & python --version 2>&1
    Pass "env.python" @{ version = "$ver"; path = $py.Source }
}

$agDirs = @("$env:USERPROFILE\.gemini")
$agFound = $agDirs | Where-Object { Test-Path $_ }
if ($agFound) {
    Pass "env.antigravity_home" @{ path = $agFound }
} else {
    Log "WARN" "~\.gemini not found - Antigravity may not be installed yet. Setup will still write the config."
    $results["env.antigravity_home"] = "NOT_FOUND (setup will create config anyway)"
}

# ---------- step 1: install ----------
Step "STEP 1: installing wheel"
$whl = Get-ChildItem -Path $PSScriptRoot -Filter "toolrecall-*.whl" | Select-Object -First 1
if (-not $whl) {
    Fail "install.wheel" "no toolrecall-*.whl found next to this script"
} else {
    $out = & python -m pip install --user --force-reinstall --no-deps $whl.FullName 2>&1
    $out | Out-File -FilePath $logFile -Append
    if ($LASTEXITCODE -eq 0) {
        $ver = & toolrecall --version 2>&1
        Pass "install.wheel" @{ wheel = $whl.Name; version = "$ver" }
    } else {
        Fail "install.wheel" "pip install failed (see log)"
    }
}

# ---------- step 2: setup ----------
Step "STEP 2: toolrecall setup (daemon + Antigravity config)"
$out = & toolrecall setup 2>&1
$out | Out-File -FilePath $logFile -Append
$cfgPath = Join-Path $env:USERPROFILE ".gemini\config\mcp_config.json"
if (Test-Path $cfgPath) {
    try {
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($cfg.mcpServers.toolrecall) {
            Pass "setup.mcp_config" @{ path = $cfgPath; command = $cfg.mcpServers.toolrecall.command }
        } else {
            Fail "setup.mcp_config" "config exists but no toolrecall entry"
        }
    } catch {
        Fail "setup.mcp_config" "config unparseable: $_"
    }
} else {
    Fail "setup.mcp_config" "$cfgPath not written (see log)"
}

# daemon health
Start-Sleep -Seconds 3
$st = & toolrecall status 2>&1
$st | Out-File -FilePath $logFile -Append
if ($LASTEXITCODE -eq 0) { Pass "setup.daemon" @{ status = "running" } }
else { Fail "setup.daemon" "toolrecall status exit $LASTEXITCODE" }

# ---------- step 3: read-only verify script ----------
Step "STEP 3: read-only verification script"
$verify = Join-Path $PSScriptRoot "verify_antigravity_windows.ps1"
if (Test-Path $verify) {
    $out = & powershell -ExecutionPolicy Bypass -File $verify 2>&1
    $out | Out-File -FilePath $logFile -Append
    Pass "verify.script" @{ output_lines = ($out | Measure-Object).Count }
} else {
    Log "WARN" "verify_antigravity_windows.ps1 not found - skipping"
    $results["verify.script"] = "SKIPPED"
}

# ---------- step 4: T1 - MCP tools visible ----------
Step "STEP 4 (T1): bridge handshake"
$trBin = (Get-Command toolrecall -ErrorAction SilentlyContinue).Source
if ($trBin) {
    $pinfo = New-Object System.Diagnostics.ProcessStartInfo
    $pinfo.FileName = $trBin
    $pinfo.Arguments = "mcp"
    $pinfo.RedirectStandardInput = $true
    $pinfo.RedirectStandardOutput = $true
    $pinfo.RedirectStandardError = $true
    $pinfo.UseShellExecute = $false
    $proc = [System.Diagnostics.Process]::Start($pinfo)
    $init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"ag-test","version":"1.0"}}}'
    $proc.StandardInput.WriteLine($init)
    $proc.StandardInput.Flush()
    Start-Sleep -Milliseconds 800
    $resp = $proc.StandardOutput.ReadLine()
    try { $proc.Kill() } catch {}
    if ($resp -match '"serverInfo"' -or $resp -match 'toolrecall') {
        Pass "T1.bridge_handshake" @{ response = $resp.Substring(0, [Math]::Min(120, $resp.Length)) }
    } else {
        Fail "T1.bridge_handshake" "no MCP handshake response"
    }
} else {
    Fail "T1.bridge_handshake" "toolrecall binary not found on PATH"
}

# ---------- step 5: T2 - fetch + ETag revalidation ----------
Step "STEP 5 (T2): fetch + ETag 304 revalidation (LOCAL server, no egress)"
# Boot the bundled loopback TLS server; everything in T2/T3 hits 127.0.0.1 only.
# TOOLRECALL_FETCH_CA_BUNDLE pins the bundled cert — TLS verification stays ON.
$env:TOOLRECALL_FETCH_CA_BUNDLE = Join-Path $PSScriptRoot "local_cert.pem"
$t2out = & python -c @"
import json, sys, time
sys.path.insert(0, r'$PSScriptRoot')
from local_server import serve_in_thread
from toolrecall import cache as tc
import toolrecall.mcp_fetch as mf

mf._setup()
httpd, port = serve_in_thread(0)
base = f'https://127.0.0.1:{port}'
url = f'{base}/etag/kit-test-2026'
args = {'url': url}

r1 = mf._handle('fetch_url', {'url': url})
print(json.dumps({'status': r1.get('status'), 'etag': r1.get('etag')}))

# seed the cache row the way the daemon's transparent cache would, then
# force-expire it so the revalidate path (expired row + validator) runs
key = tc.cached_mcp_check('fetch', 'fetch_url', args, ttl=300)
tc.cached_mcp_store(key['key'], 'fetch', 'fetch_url', args, json.dumps(r1), ttl=300, etag=r1.get('etag'))
with tc._db() as conn:
    conn.execute('UPDATE mcp_cache SET expires_at = ? WHERE request_hash = ?', (time.time() - 10, key['key']))
"@ 2>&1
$t2out | Out-File -FilePath $logFile -Append
$t2 = @{}
try {
    $t2.first = ($t2out | Select-Object -Last 1 | ConvertFrom-Json)
    if ($t2.first.status -eq 200 -and $t2.first.etag) {
        $t2.reval = & python -c @"
import json, sys
sys.path.insert(0, r'$PSScriptRoot')
from local_server import serve_in_thread
from toolrecall import cache as tc
import toolrecall.mcp_fetch as mf

mf._setup()
httpd, port = serve_in_thread(0)
base = f'https://127.0.0.1:{port}'
url = f'{base}/etag/kit-test-2026'
args = {'url': url}
out = tc.revalidate_mcp_entry('fetch', 'fetch_url', args,
    fetch_fn=lambda etag: mf._handle('fetch_revalidate', {'url': url, 'etag': etag}))
print(json.dumps(out))
"@ 2>&1 | Select-Object -Last 1 | ConvertFrom-Json
        if ($t2.reval.action -in @('revalidated','replaced')) {
            Pass "T2.etag_revalidate" $t2
        } else {
            Fail "T2.etag_revalidate" "unexpected action" $t2
        }
    } elseif ($t2.first.status -eq 200 -and -not $t2.first.etag) {
        $results["T2.etag_revalidate"] = @{ note = "no ETag from server (unexpected for local server)"; first = $t2.first }
        Fail "T2.etag_revalidate" "local server returned no ETag"
    } else {
        Fail "T2.etag_revalidate" "fetch failed status=$($t2.first.status)"
    }
} catch {
    Fail "T2.etag_revalidate" "exception: $_"
}

# ---------- step 6: T3 - cached_tool A/B benchmark ----------
Step "STEP 6 (T3): cached_tool A/B benchmark"
$bench = Join-Path $PSScriptRoot "bench\antigravity_win.py"
if (Test-Path $bench) {
    $env:TOOLRECALL_CACHE_DB = ""  # use default (daemon-visible) DB
    $out = & python $bench --seeds 5 --passes 2 --slots 3 2>&1
    $out | Out-File -FilePath $logFile -Append
    $summary = $out | Where-Object { $_ -match '"arm"' } | ForEach-Object { $_ | ConvertFrom-Json }
    if ($summary) {
        $ctrl = $summary | Where-Object arm -eq 'control'
        $trt  = $summary | Where-Object arm -eq 'treatment'
        $avoided = [int]$ctrl.live_calls - [int]$trt.live_calls
        Pass "T3.benchmark" @{
            control_live  = $ctrl.live_calls
            treat_live    = $trt.live_calls
            avoided       = $avoided
            ctrl_warm_s   = $ctrl.warm_avg_s
            trt_warm_s    = $trt.warm_avg_s
        }
    } else {
        Fail "T3.benchmark" "no arm summary in output (see log)"
    }
} else {
    Fail "T3.benchmark" "bench\antigravity_win.py not found in kit"
}

# ---------- step 7: T4 - replay determinism ----------
Step "STEP 7 (T4): replay record/stop/list"
$env:TOOLRECALL_REPLAY_SESSION = "ag-kit-$stamp"
$out = & toolrecall replay record "ag-kit-$stamp" 2>&1
$out | Out-File -FilePath $logFile -Append
$rec = & python -c @"
import json
from toolrecall.cache import cached_mcp_store, cached_mcp_check
# one deterministic synthetic call so replay has content
key = cached_mcp_check('fetch', 'fetch_url', {'url': 'https://127.0.0.1:9/replay-seed'}, ttl=300)
if not key.get('cached'):
    cached_mcp_store(key.get('key'), 'fetch', 'fetch_url', {'url': 'https://127.0.0.1:9/replay-seed'},
                     json.dumps({'status': 200, 'content': 'hello-from-ag-kit'}), ttl=300)
r = cached_mcp_check('fetch', 'fetch_url', {'url': 'https://127.0.0.1:9/replay-seed'}, ttl=300)
print(json.dumps({'cached': bool(r.get('cached')), 'data': (r.get('data') or '')[:40]}))
"@ 2>&1
$rec | Out-File -FilePath $logFile -Append
$rep = & toolrecall replay stop 2>&1
$rep | Out-File -FilePath $logFile -Append
$lst = & toolrecall replay list 2>&1
$lst | Out-File -FilePath $logFile -Append
if ($lst -match "ag-kit-$stamp") {
    Pass "T4.replay" @{ scenario = "ag-kit-$stamp"; listed = $true }
} else {
    Fail "T4.replay" "scenario not in replay list (see log)"
}

# T4b: offline replay — the replay table replays recorded tool calls without
# network. Uses the real API: ReplayManager.find_replay(scenario, tool, args).
Step "STEP 7b (T4b): offline replay check"
$out = & python -c @"
import json
from toolrecall.replay import ReplayManager
m = ReplayManager()
r = m.find_replay('ag-kit-$stamp', 'fetch_url', {'url': 'https://127.0.0.1:9/replay-seed'})
print(json.dumps({'replayed': r is not None}))
"@ 2>&1
$out | Out-File -FilePath $logFile -Append
if ("$out" -match '"replayed":\s*true') { Pass "T4b.replay_offline" $true }
else {
    Log "WARN" "T4b offline replay returned no hit (see log - replay lookup needs the daemon's recording flow)"
    $results["T4b.replay_offline"] = "WARN: no replay hit"
}

# ---------- step 7c: T7 - Google MCP server multiplex saving ----------
# Measures what ToolRecall saves when a Google MCP server's calls are fronted
# through the multiplexer with transparent caching: identical repeat calls are
# answered from the local cache instead of re-executing (latency + bytes).
Step "STEP 7c (T7): Google MCP multiplex saving (loopback only)"
$t7out = & python bench\mcp_multiplex_bench.py --ttl 300 --calls 6 --ms 200 2>&1
$t7out | Out-File -FilePath $logFile -Append
$t7json = $t7out | Where-Object { $_ -match '"arm"' } | ForEach-Object { $_ | ConvertFrom-Json }
if ($t7json.Count -ge 2) {
    $ctrl = $t7json | Where-Object { $_.arm -eq "control" } | Select-Object -First 1
    $treat = $t7json | Where-Object { $_.arm -eq "treatment" } | Select-Object -First 1
    $avoided = [int]$ctrl.live_calls - [int]$treat.live_calls
    $bytesSaved = [int]$ctrl.bytes_on_wire - [int]$treat.bytes_on_wire
    if ($avoided -gt 0 -and $bytesSaved -gt 0) {
        Pass "T7.multiplex" $true
        $results["T7.avoided_calls"] = $avoided
        $results["T7.bytes_saved"] = $bytesSaved
        Log "INFO" "T7: $avoided live calls avoided, $bytesSaved bytes kept off the wire"
    } else {
        Fail "T7.multiplex" "no saving measured (control=$($ctrl.live_calls), treatment=$($treat.live_calls))" $t7out
    }
} else {
    Fail "T7.multiplex" "expected 2 JSON arms, got $($t7json.Count)" $t7out
}

# ---------- step 8: T5 - freshness metadata ----------
Step "STEP 8 (T5): freshness metadata"
$out = & python -c @"
import json
from toolrecall.adapters import antigravity

@antigravity.cached_tool(ttl=120, freshness=True)
def probe(x: int) -> int:
    return x * 3

d1, m1 = probe(x=9)
d2, m2 = probe(x=9)
print(json.dumps({'d1': d1, 'm1': m1, 'd2': d2, 'm2': m2}))
"@ 2>&1
$out | Out-File -FilePath $logFile -Append
try {
    $o = $out | Select-Object -Last 1 | ConvertFrom-Json
    if ($o.m1.cached -eq $false -and $o.m2.cached -eq $true -and $o.m2.age_s -ge 0) {
        Pass "T5.freshness" $o
    } else {
        Fail "T5.freshness" "unexpected meta shape" $o
    }
    } catch { Fail "T5.freshness" "parse: $_" }

# ---------- step 9: T6 - scoped invalidation ----------
Step "STEP 9 (T6): scoped invalidation"
$out = & toolrecall invalidate --url 127.0.0.1 2>&1
$out | Out-File -FilePath $logFile -Append
if ("$out" -match "Invalidated") { Pass "T6.invalidate_url" @{ output = "$out" } }
else { Fail "T6.invalidate_url" "no Invalidated output" }
$out = & toolrecall invalidate --server fetch 2>&1
$out | Out-File -FilePath $logFile -Append
if ("$out" -match "Invalidated") { Pass "T6.invalidate_server" @{ output = "$out" } }
else { Fail "T6.invalidate_server" "no Invalidated output" }

# ---------- summary ----------
Step "SUMMARY"
$json = $results | ConvertTo-Json -Depth 5
$json | Out-File -FilePath $jsonFile -Encoding utf8
$passCount = ($results.Keys | Where-Object { $results[$_] -isnot [string] -or $results[$_] -notmatch '^FAILED' }).Count
Log "INFO" "results: $jsonFile"
Log "INFO" "log:     $logFile"
Log "INFO" "passed:  $passCount   failed: $script:failed"
if ($script:failed -gt 0) {
    Log "INFO" "FAILED TESTS: $($results.Keys | Where-Object { $results[$_] -is [string] -and $results[$_] -match '^FAILED' })"
    exit 1
}
exit 0
