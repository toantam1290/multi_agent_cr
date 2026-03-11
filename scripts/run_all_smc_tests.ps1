# Run 5 SMC backtest tests theo thu tu
# Usage: .\scripts\run_all_smc_tests.ps1

$ErrorActionPreference = "Stop"
$outFile = "backtest_tests_output.txt"

"=== SMC Backtest Tests $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" | Tee-Object -FilePath $outFile

# Test 1: Baseline swing ob-only 2024
"`n--- Test 1: Baseline swing ob-only 2024 ---" | Tee-Object -FilePath $outFile -Append
python scripts/run_smc_backtest.py --from 2024-01-01 --to 2024-12-31 --style swing --ob-only --quiet 2>&1 | Tee-Object -FilePath $outFile -Append

# Test 2: swing --no-ce
"`n--- Test 2: swing --no-ce ---" | Tee-Object -FilePath $outFile -Append
python scripts/run_smc_backtest.py --from 2024-01-01 --to 2024-12-31 --style swing --no-ce --quiet 2>&1 | Tee-Object -FilePath $outFile -Append

# Test 3: Multi-pair
"`n--- Test 3: Multi-pair BTC,ETH,SOL ---" | Tee-Object -FilePath $outFile -Append
python scripts/run_smc_backtest.py --from 2024-01-01 --to 2024-12-31 --style swing --ob-only --symbol BTCUSDT,ETHUSDT,SOLUSDT --quiet 2>&1 | Tee-Object -FilePath $outFile -Append

# Test 4: 4 nam
"`n--- Test 4: 4 nam 2022-now ---" | Tee-Object -FilePath $outFile -Append
python scripts/run_smc_backtest.py --from 2022-01-01 --style swing --ob-only --quiet 2>&1 | Tee-Object -FilePath $outFile -Append

# Test 5: Break-even 12
"`n--- Test 5: Break-even 12 candles ---" | Tee-Object -FilePath $outFile -Append
python scripts/run_smc_backtest.py --from 2024-01-01 --to 2024-12-31 --style swing --ob-only --breakeven 12 --quiet 2>&1 | Tee-Object -FilePath $outFile -Append

"`n=== DONE $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" | Tee-Object -FilePath $outFile -Append
Write-Host "`nResults saved to $outFile"
