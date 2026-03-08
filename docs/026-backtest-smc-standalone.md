# 026 — Backtest SMC Standalone

## Tổng quan

Backtest SMC standalone — không dùng rule-based, CVD, VWAP, EMA9, regime, chop. Chỉ SMCStrategy.

## Cách chạy

```bash
# SMC standalone chỉ
python backtest.py --mode smc --symbol BTCUSDT --style scalp --days 90

# Số ngày tùy chọn
python backtest.py --mode smc --symbol BTCUSDT --style scalp --from 2024-01-01 --to 2024-06-30

# Dùng cache
python backtest.py --mode smc --symbol BTCUSDT --style scalp --days 90 --use-cache

# Walk-forward SMC (chưa có — dùng mode rule)
python backtest.py --symbol BTCUSDT --style scalp --days 270 --walk-forward
```

## Mode

| Mode | Mô tả |
|------|-------|
| `rule` | Rule-based (default) | 
| `smc` | SMC standalone only |
| `combined` | Giống rule (multi-symbol combined) |

## Thay đổi

### 1. utils/smc_strategy.py
- Thêm `analyze_from_dataframes()` — sync version cho backtest
- `__init__(fetcher=None)` — hỗ trợ backtest (không cần API)

### 2. backtest.py
- **download_all_data**: thêm `1d` (scalp), `1w` (swing)
- **BacktestConfig**: `use_smc_standalone: bool = False`
- **TradeResult**: `entry_model`, `entry_model_quality`, `smc_htf_bias`, `smc_ltf_trigger`, `smc_confidence`
- **run_smc_backtest_for_symbol()**: pipeline SMC standalone
- **print_report()**: SMC breakdown (By Entry Model, Quality Grade, LTF Trigger)
- **--mode smc**: chạy SMC standalone

### 3. Data flow

```
step_ts (15m) → get_window(1h, 15m, 5m, 1d)
  → smc_strategy.analyze_from_dataframes()
  → setup valid? → simulate_trade(entry, sl, tp1)
  → TradeResult
```

## Report breakdown

```
📐 SMC STANDALONE BREAKDOWN
  By Entry Model:
    sweep_reversal : 12 trades | WR 67% | Avg RR 2.3
    bpr_entry      :  8 trades | WR 62% | Avg RR 1.9
    ...
  By Quality Grade:
    A+ : 5 trades  | WR 80%
    A  : 18 trades | WR 61%
    ...
  By LTF Trigger:
    displacement : WR 65%
    sweep        : WR 63%
    ...
```
