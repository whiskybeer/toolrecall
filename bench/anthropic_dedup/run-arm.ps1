<#
.SYNOPSIS
  Run ONE arm of the Claude Code x ToolRecall request-body-dedup A/B.

.DESCRIPTION
  Sets the proxy/dedup environment for THIS TERMINAL ONLY — never `setx`.
  A machine-wide ANTHROPIC_BASE_URL would route every Claude Code session
  (including the desktop app) through the proxy, and other sessions' rows
  would land inside the arm's measurement window.

  Start mode: exports the arm's config, restarts the daemon so it picks the
  config up, and records the arm start time.
  End mode:   records the end time and calls the summarizer for that window.
  Compare:    summarizes A vs B.

  The arms differ ONLY in daemon config:
     Arm A  dedup ON   (TOOLRECALL_FORWARD_DEDUP=1)  — the candidate
     Arm B  dedup OFF  (TOOLRECALL_FORWARD_DEDUP=0)  — the baseline

  Run the two arms in two separate terminals, and run `claude` FROM the
  terminal you configured, so the process inherits the environment.

.NOTES
  TOOLRECALL_DEDUP_PROTECT_LAST defaults to 0 on purpose. The shipped default
  (2) defers a duplicate that arrives inside the protected tail by one turn;
  stubbing it a turn later rewrites bytes the provider already cached, which
  costs ~1.25x on the whole suffix instead of ~0.1x. Keep-first already keeps
  the full copy, so 0 loses nothing. Pinned by
  tests/test_dedup_anthropic.py::TestProtectedTailVsCache.

  Do NOT use `toolrecall stop` between arms: it also reverts the base-URL
  wiring. `toolrecall restart` is the health-check + clean restart.

.EXAMPLE
  # terminal 1 — arm A (dedup on), then run claude, then end the window
  .\run-arm.ps1 -Arm A
  claude
  .\run-arm.ps1 -Arm A -End

.EXAMPLE
  # terminal 2 — arm B (baseline), same task
  .\run-arm.ps1 -Arm B
  claude
  .\run-arm.ps1 -Arm B -End
  .\run-arm.ps1 -Compare
#>
[CmdletBinding()]
param(
    [ValidateSet("A", "B")][string]$Arm,
    [int]$ProtectLast = 0,
    [string]$ProxyUrl = "http://localhost:8569",
    [string]$UsageCsv = (Join-Path $env:USERPROFILE ".toolrecall\proxy_usage.csv"),
    [string]$OutDir = ".\arm-results",
    [string]$Summarizer = (Join-Path $PSScriptRoot "summarize_arm_window.py"),
    [switch]$End,
    [switch]$Compare,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"

function Get-Iso { (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss") }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

if ($Compare) {
    $a = Join-Path $OutDir "arm-A-summary.json"
    $b = Join-Path $OutDir "arm-B-summary.json"
    foreach ($f in @($a, $b)) {
        if (-not (Test-Path $f)) { throw "missing $f — run both arms with -End first" }
    }
    python $Summarizer --compare $a $b
    exit $LASTEXITCODE
}

if (-not $Arm) { throw "specify -Arm A|B, or -Compare" }

$stateFile = Join-Path $OutDir "arm-$Arm.json"

if ($End) {
    if (-not (Test-Path $stateFile)) { throw "no start state for arm $Arm — start it first" }
    $st = Get-Content $stateFile -Raw | ConvertFrom-Json
    $end = Get-Iso
    $summary = Join-Path $OutDir "arm-$Arm-summary.json"

    python $Summarizer --csv $UsageCsv --since $st.start --until $end --label $Arm --out $summary

    $st | Add-Member -NotePropertyName end -NotePropertyValue $end -Force
    $st | ConvertTo-Json | Set-Content $stateFile
    Write-Host ""
    Write-Host "arm $Arm window recorded: $($st.start) -> $end"
    Write-Host "summary: $summary"
    if ($Arm -eq "A" -and -not (Test-Path (Join-Path $OutDir "arm-B-summary.json"))) {
        Write-Host "next: run arm B in a SECOND terminal, then .\run-arm.ps1 -Compare"
    }
    exit 0
}

# ── start mode ────────────────────────────────────────────────────────────────
$dedup = if ($Arm -eq "B") { "0" } else { "1" }
$env:ANTHROPIC_BASE_URL = $ProxyUrl
$env:TOOLRECALL_FORWARD_DEDUP = $dedup
$env:TOOLRECALL_DEDUP_PROTECT_LAST = "$ProtectLast"

Write-Host "arm $Arm"
Write-Host "  ANTHROPIC_BASE_URL          = $ProxyUrl      (this terminal only)"
Write-Host "  TOOLRECALL_FORWARD_DEDUP    = $dedup"
Write-Host "  TOOLRECALL_DEDUP_PROTECT_LAST = $ProtectLast"

if (-not $NoRestart) {
    Write-Host "restarting daemon so it picks up the arm config..."
    toolrecall restart
    if ($LASTEXITCODE -ne 0) {
        Write-Host "restart returned $LASTEXITCODE — falling back to stop/start"
        toolrecall daemon --stop
        toolrecall daemon
    }
}

$start = Get-Iso
@{
    arm         = $Arm
    start       = $start
    dedup       = $dedup
    protect_last = $ProtectLast
    proxy       = $ProxyUrl
    usage_csv   = $UsageCsv
} | ConvertTo-Json | Set-Content $stateFile

Write-Host ""
Write-Host "window started at $start  (written to $stateFile)"
Write-Host ""
Write-Host "NOW:"
Write-Host "  1. run  claude  from THIS terminal (so it inherits the env)"
Write-Host "  2. paste the prompt from WORKLOAD.md — it produces byte-identical"
Write-Host "     Bash output over >= 8 turns, which is what dedup targets"
Write-Host "  3. when done:  .\run-arm.ps1 -Arm $Arm -End"
