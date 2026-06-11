<#
.SYNOPSIS
    One-click test for the ToolRecall Browser Cache extension on Windows.
    Starts daemon + proxy, opens Chrome with the extension loaded.

.DESCRIPTION
    Run this script from the repo root (where browser-extension/ lives).
    It will:
      1. Start ToolRecall daemon (background)
      2. Start ToolRecall HTTP proxy on port 8569 (background)
      3. Open Chrome with the extension loaded
      4. Open DevTools console showing cache messages

    Requirements:
      - Python 3.11+ with ToolRecall installed (pip install toolrecall)
      - Chrome/Edge/Brave installed
      - Node.js (only if rebuilding — pre-built extension is in dist/)

.EXAMPLE
    PS C:\projects\toolrecall> .\browser-extension\scripts\test-on-windows.ps1
#>

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path "$PSScriptRoot\..\.."
$ExtPath = Join-Path $RepoRoot "browser-extension\dist\chrome-mv3"
$LogDir  = Join-Path $RepoRoot "browser-extension\logs"

# ─── Check prerequisites ─────────────────────────

Write-Host "🔍 Checking prerequisites..." -ForegroundColor Cyan

# 1. ToolRecall installed?
try {
    $version = & toolrecall --version 2>&1
    Write-Host "   ✅ ToolRecall $version" -ForegroundColor Green
} catch {
    Write-Host "   ⚠️  ToolRecall not found — installing..." -ForegroundColor Yellow
    pip install toolrecall
}

# 2. Built extension exists?
if (!(Test-Path (Join-Path $ExtPath "manifest.json"))) {
    Write-Host "   ⚠️  Extension not built — rebuilding..." -ForegroundColor Yellow
    Push-Location (Join-Path $RepoRoot "browser-extension")
    npm install
    npx wxt build --browser chrome
    Pop-Location
}

Write-Host "   ✅ Extension ready at $ExtPath" -ForegroundColor Green

# 3. Chrome installed?
$ChromePaths = @(
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "${env:LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    # Brave
    "${env:ProgramFiles}\BraveSoftware\Brave-Browser\Application\brave.exe",
    "${env:LOCALAPPDATA}\BraveSoftware\Brave-Browser\Application\brave.exe",
)

$ChromeExe = $null
foreach ($p in $ChromePaths) {
    if (Test-Path $p) { $ChromeExe = $p; break }
}

if (!$ChromeExe) {
    # Try PATH
    $ChromeExe = (Get-Command "chrome" -ErrorAction SilentlyContinue).Source
}
if (!$ChromeExe) {
    $ChromeExe = (Get-Command "msedge" -ErrorAction SilentlyContinue).Source
}

if (!$ChromeExe) {
    Write-Warning "Could not find Chrome, Edge, or Brave. Install one and re-run."
    Write-Host "   You can also load the extension manually:" -ForegroundColor Yellow
    Write-Host "   1. Open chrome://extensions" -ForegroundColor Yellow
    Write-Host "   2. Enable Developer mode" -ForegroundColor Yellow  
    Write-Host "   3. Load unpacked → select: $ExtPath" -ForegroundColor Yellow
    exit 1
}

Write-Host "   ✅ Browser found: $ChromeExe" -ForegroundColor Green

# ─── Create log directory ──────────────────────────
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ─── Kill any existing ToolRecall processes ──────────
Write-Host "🔄 Cleaning up old ToolRecall processes..." -ForegroundColor Cyan
Get-Process -Name "python*" -ErrorAction SilentlyContinue | 
    Where-Object { $_.CommandLine -match "toolrecall" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

# ─── Start daemon ──────────────────────────────────
Write-Host "🚀 Starting ToolRecall daemon..." -ForegroundColor Cyan
$DaemonLog = Join-Path $LogDir "daemon.log"
$DaemonJob = Start-Job -Name "toolrecall-daemon" -ScriptBlock {
    param($log)
    toolrecall daemon 2>&1 | Out-File -FilePath $log -Encoding utf8
} -ArgumentList $DaemonLog

Start-Sleep -Milliseconds 1000

# ─── Start proxy ──────────────────────────────────
Write-Host "🚀 Starting HTTP proxy on port 8569..." -ForegroundColor Cyan
$ProxyLog = Join-Path $LogDir "proxy.log"
$ProxyJob = Start-Job -Name "toolrecall-proxy" -ScriptBlock {
    param($log)
    toolrecall serve --port 8569 2>&1 | Out-File -FilePath $log -Encoding utf8
} -ArgumentList $ProxyLog

Start-Sleep -Milliseconds 2000

# ─── Verify they're running ───────────────────────
$HealthCheck = try { Invoke-WebRequest -Uri "http://127.0.0.1:8569/health" -TimeoutSec 2 -ErrorAction Stop } catch { $null }
if ($HealthCheck) {
    Write-Host "   ✅ Daemon + proxy running on 127.0.0.1:8569" -ForegroundColor Green
} else {
    Write-Warning "Health check failed. Check logs:"
    Write-Host "   Daemon: $DaemonLog"
    Write-Host "   Proxy:  $ProxyLog"
    Write-Host "   Starting Chrome anyway — extension will connect when proxy is up."
}

# ─── Open Chrome with extension ───────────────────
Write-Host "🌐 Opening browser with extension loaded..." -ForegroundColor Cyan
Write-Host ""
Write-Host "══════════════════════════════════════════════════" -ForegroundColor Magenta
Write-Host "  ✅ Ready! Test by visiting a page twice:" -ForegroundColor Green
Write-Host "  1. Go to example.com — first visit = CACHE MISS" -ForegroundColor White
Write-Host "  2. Go again — second visit = CACHE HIT " -ForegroundColor White
Write-Host "  3. Open DevTools → Console to see:" -ForegroundColor White
Write-Host "     [ToolRecall] ✅ Cache HIT for ..." -ForegroundColor Cyan
Write-Host "══════════════════════════════════════════════════" -ForegroundColor Magenta
Write-Host ""

# Use --incognito for a clean test session
Start-Process -FilePath $ChromeExe -ArgumentList @(
    "--new-window",
    "--incognito",
    "--load-extension=`"$ExtPath`"",
    "--auto-open-devtools-for-tabs",
    "https://example.com"
)

Write-Host "💡 To stop: run  Get-Job | Stop-Job" -ForegroundColor Gray
Write-Host "   Or just close this terminal — processes will stop on exit." -ForegroundColor Gray