# Run backtest test suite and capture results
# Usage: .\scripts\run_backtest_tests.ps1

$env:PYTHONIOENCODING = "utf-8"
$outDir = "docs"
$outFile = "$outDir\backtest_test_output.txt"

$tests = @(
    @{ name = "1.1 Baseline 60d"; cmd = "python backtest.py --symbol BTCUSDT --style scalp --days 60" },
    @{ name = "1.2 Baseline 90d"; cmd = "python backtest.py --symbol BTCUSDT --style scalp --days 90" },
    @{ name = "1.3 Multi-symbol 60d"; cmd = "python backtest.py --symbol BTCUSDT,ETHUSDT --style scalp --days 60" },
    @{ name = "1.4 Confluence 2"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --confluence 2" },
    @{ name = "2.1 No EMA9"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --no-ema9" },
    @{ name = "2.2 No Confluence"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --no-confluence" },
    @{ name = "2.3 No Chop"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --no-chop" },
    @{ name = "2.4 No CVD"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --no-cvd" },
    @{ name = "2.5 No Session"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --no-session" },
    @{ name = "2.6 No VWAP"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --no-vwap" },
    @{ name = "2.7 No Regime"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --no-regime" },
    @{ name = "2.8 No Correlation"; cmd = "python backtest.py --symbol BTCUSDT,ETHUSDT --days 60 --no-correlation" },
    @{ name = "2.9 No Dynamic Confluence"; cmd = "python backtest.py --symbol BTCUSDT --days 60 --no-dynamic-confluence" },
    @{ name = "3.1 Optimize 90d"; cmd = "python backtest.py --symbol BTCUSDT --days 90 --optimize" },
    @{ name = "4.1 Walk-forward"; cmd = "python backtest.py --symbol BTCUSDT --days 180 --walk-forward --wf-train 90 --wf-test 30" }
)

"Backtest Test Suite - $(Get-Date -Format 'yyyy-MM-dd HH:mm')" | Out-File $outFile -Encoding utf8
"=" * 80 | Out-File $outFile -Append -Encoding utf8

foreach ($t in $tests) {
    "`n`n### $($t.name)`n" | Out-File $outFile -Append -Encoding utf8
    "Command: $($t.cmd)`n" | Out-File $outFile -Append -Encoding utf8
    Invoke-Expression "$($t.cmd) 2>&1" | Out-File $outFile -Append -Encoding utf8
}

Write-Host "Done. Results in $outFile"
