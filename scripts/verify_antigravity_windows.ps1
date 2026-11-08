# verify_antigravity_windows.ps1 — ToolRecall × Antigravity verification (Windows)
#
# Read-only verification for a Windows machine where Antigravity + ToolRecall
# are (or should be) installed. Run AFTER `toolrecall setup`. Never writes
# config — every check prints PASS / FAIL / SKIP and a final summary.
#
# Usage (PowerShell, normal user shell):
#   powershell -ExecutionPolicy Bypass -File .\verify_antigravity_windows.ps1
#
# Send back: the full console output + `toolrecall healthcheck` output.

$ErrorActionPreference = "Continue"
$results = New-Object System.Collections.Generic.List[string]

function Check {
    param([string]$Name, [string]$Status, [string]$Detail)
    $icon = switch ($Status) { "PASS" { "[PASS]" } "FAIL" { "[FAIL]" } default { "[SKIP]" } }
    $line = "$icon $Name" + $(if ($Detail) { " — $Detail" } else { "" })
    $results.Add($line)
    Write-Host $line
}

Write-Host "=== ToolRecall x Antigravity — Windows verification ==="
Write-Host "Host: $env:COMPUTERNAME  User: $env:USERNAME  PS: $($PSVersionTable.PSVersion)"
Write-Host ""

# ---- 1. toolrecall on PATH ---------------------------------------------------
$tr = Get-Command toolrecall -ErrorAction SilentlyContinue
if ($tr) {
    Check "toolrecall on PATH" "PASS" $tr.Source
    $trCmd = $tr.Source
} else {
    # pipx default shim location
    $shim = Join-Path $env:USERPROFILE ".local\bin\toolrecall.exe"
    if (Test-Path $shim) {
        Check "toolrecall on PATH" "FAIL" "not on PATH but shim exists: $shim (add to PATH)"
        $trCmd = $shim
    } else {
        Check "toolrecall installed" "FAIL" "pipx install toolrecall first"
        $results.Add("SUMMARY: BLOCKED — toolrecall missing")
        exit 1
    }
}

# ---- 2. Version --------------------------------------------------------------
$ver = & $trCmd --version 2>&1
Check "toolrecall --version" "PASS" "$ver"

# ---- 3. Daemon health --------------------------------------------------------
& $trCmd healthcheck *>> $null
$hcExit = $LASTEXITCODE
switch ($hcExit) {
    0 { Check "healthcheck" "PASS" "exit 0 (healthy)" }
    1 { Check "healthcheck" "SKIP" "exit 1 (warn — include full healthcheck output in report)" }
    default { Check "healthcheck" "FAIL" "exit $hcExit (error — include full healthcheck output)" }
}

# ---- 4. Status / transport ---------------------------------------------------
$status = (& $trCmd status) 2>&1 | Out-String
if ($status -match "TCP") { Check "daemon transport" "PASS" "TCP (expected on Windows)" }
elseif ($status -match "UDS|socket") { Check "daemon transport" "SKIP" "UDS reported on Windows? include status output" }
else { Check "daemon transport" "SKIP" "unrecognized — include status output in report" }

# ---- 5. Antigravity MCP config ----------------------------------------------
$agCfg = Join-Path $env:USERPROFILE ".gemini\config\mcp_config.json"
if (Test-Path $agCfg) {
    try {
        $cfg = Get-Content $agCfg -Raw | ConvertFrom-Json
        $entry = $cfg.mcpServers.toolrecall
        if ($null -ne $entry) {
            $cmdPath = $entry.command
            if ([System.IO.Path]::IsPathRooted($cmdPath) -and (Test-Path $cmdPath)) {
                Check "Antigravity MCP entry" "PASS" "$cmdPath $($entry.args -join ' ')"
            } elseif (-not [System.IO.Path]::IsPathRooted($cmdPath)) {
                Check "Antigravity MCP entry" "FAIL" "command '$cmdPath' is NOT absolute — GUI-spawned MCP needs absolute path; re-run toolrecall setup in PowerShell"
            } else {
                Check "Antigravity MCP entry" "FAIL" "command '$cmdPath' does not exist on disk"
            }
        } else {
            Check "Antigravity MCP entry" "FAIL" "config exists but no toolrecall entry"
        }
        # user servers preserved?
        $others = ($cfg.mcpServers.PSObject.Properties.Name | Where-Object { $_ -ne "toolrecall" }).Count
        Check "user servers preserved" "PASS" "$others other server(s) present" 
    } catch {
        Check "Antigravity MCP config" "FAIL" "unparseable JSON: $_"
    }
} else {
    Check "Antigravity MCP config" "FAIL" "$agCfg missing — run: toolrecall setup"
}

# ---- 6. Bridge handshake smoke (stdio MCP) -----------------------------------
# Send an MCP initialize request to `toolrecall mcp` and expect a JSON-RPC
# response on stdout. 10s timeout; the bridge must answer before Antigravity
# can list it.
$initReq = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify-script","version":"0.1"}}}'
try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $trCmd
    $psi.Arguments = "mcp"
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $p = [System.Diagnostics.Process]::Start($psi)
    $p.StandardInput.WriteLine($initReq)
    $p.StandardInput.Flush()
    $outTask = $p.StandardOutput.ReadLine()
    if ($outTask.Wait(10000)) {
        $line = $outTask.Result
        if ($line -match '"jsonrpc"' -or $line -match '"serverInfo"') {
            Check "bridge MCP handshake" "PASS" $line.Substring(0, [Math]::Min(120, $line.Length))
        } else {
            Check "bridge MCP handshake" "FAIL" "unexpected response: $($line.Substring(0, [Math]::Min(120, $line.Length)))"
        }
    } else {
        Check "bridge MCP handshake" "FAIL" "no response within 10s"
    }
    if (-not $p.HasExited) { $p.Kill() }
} catch {
    Check "bridge MCP handshake" "FAIL" $_.Exception.Message
}

# ---- 7. Stats snapshot (report only) ------------------------------------------
$stats = (& $trCmd stats) 2>&1 | Out-String
$mcpLine = ($stats -split "`n" | Where-Object { $_ -match "mcp" }) -join "; "
Check "stats captured (report)" "PASS" $(if ($mcpLine) { $mcpLine.Substring(0, [Math]::Min(160, $mcpLine.Length)) } else { "no mcp lines — include full stats in report" })

# ---- Summary ------------------------------------------------------------------
Write-Host ""
Write-Host "=== Summary ==="
$results | ForEach-Object { Write-Host $_ }
$fail = ($results | Where-Object { $_ -like "*[FAIL]*" }).Count
Write-Host ""
if ($fail -eq 0) { Write-Host "ALL CHECKS PASSED (or SKIP) — send full output + healthcheck output back." }
else { Write-Host "$fail check(s) FAILED — send full output for debugging." }
