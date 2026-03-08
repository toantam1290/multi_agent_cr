# Run full backtest report (7 steps)
# Usage: .\scripts\run_full_report.ps1 [--days 180] [--skip-download]
# Requires: data in data/backtest_cache/ (run with --download first if needed)

$env:PYTHONIOENCODING = "utf-8"
$ErrorActionPreference = "Stop"

$days = 180
$skipDownload = $false

# Parse args
foreach ($arg in $args) {
    if ($arg -eq "--skip-download") { $skipDownload = $true }
    elseif ($arg -match "^--days=(\d+)$") { $days = [int]$Matches[1] }
    elseif ($arg -match "^--days\s+(\d+)$") { $days = [int]$Matches[1] }
}

$stepArgs = @()
if ($skipDownload) { $stepArgs += "--skip-download" }
$stepArgs += "--days", $days
$stepArgs += "--use-cache"

Write-Host "Running full backtest report ($days days)..."
Write-Host ""

Push-Location $PSScriptRoot\..
try {
    python scripts/run_backtest_full_report.py @stepArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host ""
    Write-Host "Report: docs/018-backtest-full-report.txt"
} finally {
    Pop-Location
}
