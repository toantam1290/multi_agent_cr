# Backtest Engine — Chi tiết Code và Kết quả Chạy

**Ngày tạo:** 2026-03-08  
**Phiên bản:** backtest.py + run_backtest_full_report.py  
**Kết quả:** 90 ngày BTCUSDT (cache)

---

## 1. Tổng quan kiến trúc

### 1.1 Luồng xử lý chính

```
Download OHLCV (Binance) → Compute Indicators → Filter Pipeline → Entry/SL/TP → Simulate Trade → Stats
```

| Bước | Module | Mô tả |
|------|--------|-------|
| 1 | `download_all_data()` | Lấy 5m, 15m, 1h, 4h, funding từ Binance, lưu `data/backtest_cache/` |
| 2 | `compute_indicators()` | RSI, EMA9/21, MACD, volume, trend, CVD proxy, VWAP, Chop Index |
| 3 | Filter pipeline | Session → Rule → CVD → VWAP → EMA9 → Regime → Chop → Correlation → Confluence |
| 4 | `calc_entry_sl_tp()` | Entry, SL (swing structure hoặc ATR), TP theo RR |
| 5 | `simulate_trade()` | Replay future candles, check SL/TP hit, trail stop |
| 6 | `calc_stats()` | Win rate, PF, max DD, Sharpe, PnL |

### 1.2 Timeframe (scalp)

| Mục đích | TF | Số nến warmup |
|----------|-----|----------------|
| Scan step | 15m | 200 |
| Fast (RSI, EMA9) | 15m | 200 |
| Slow | 5m | 200 |
| Trend (EMA20/50) | 4h | 100 |
| ATR | 5m | 200 |
| ADX (regime) | 1h | 200 |
| Simulate outcome | 5m | max 9 candles (45 phút) |

---

## 2. Cấu hình BacktestConfig

### 2.1 Filter toggles

```python
use_rule_filter: bool = True      # rule_based_filter (net_score, momentum, volume)
use_ema9_filter: bool = True      # EMA9 cross timing
use_confluence_filter: bool = True # min N confluence score
use_cvd_proxy: bool = True        # taker_buy_base / volume ratio
use_vwap_filter: bool = True      # distance from VWAP < 1.5%
use_session_filter: bool = True   # skip dead_zone (20h-8h UTC)
use_regime_filter: bool = True    # skip volatile (ADX, BB width)
use_chop_filter: bool = True      # Chop Index > 61.8 → skip
use_correlation_filter: bool = True # max 2 positions same direction
use_dynamic_confluence: bool = True # win rate < 45% → min_confluence=4
```

### 2.2 Tham số mới (momentum gate, net score)

```python
use_momentum_gate: bool = True    # True = hard gate (phải có momentum)
                                 # False = momentum chỉ bonus +15 net_score
net_score_min: int = 0            # 0 = auto (scalp=20, swing=10)
                                 # >0 = override threshold
```

### 2.3 Strategy presets

| Preset | no_ema9 | no_cvd | no_momentum_gate | net_score | confluence |
|--------|---------|--------|------------------|-----------|------------|
| v1 (default) | False | False | False | 20 | 3 |
| **v2** | True | False | True | 10 | 2 |
| **loose** | True | True | True | 5 | 1 |

---

## 3. Rule-based filter (chi tiết)

### 3.1 Điều kiện LONG

```python
# Volume (scalp, trừ rule_case=no_volume)
vol_ok = volume_spike OR volume_ratio >= 1.2 OR volume_trend_up

# Net score
net_score > net_long_min   # scalp: 20 (hoặc net_score_min nếu set)

# LONG pass
trend_1d != "downtrend"
AND rsi_1h < rsi_long_max  # scalp: 50
AND funding_pct < funding_long_max_pct  # 0.05
AND net_score > net_long_min

# Momentum gate (scalp, use_momentum_gate=True, rule_case != no_momentum)
# → phải có momentum_bullish mới return LONG
```

### 3.2 Điều kiện SHORT

```python
trend_1d != "uptrend"
AND rsi_1h > rsi_short_min  # scalp: 55
AND funding_pct > 0.005
AND net_score < net_short_max  # -20 (scalp) hoặc -net_score_min
# + momentum_bearish nếu use_momentum_gate
```

### 3.3 Rule cases

| rule_case | Ý nghĩa |
|-----------|---------|
| full | Tất cả điều kiện |
| long_only | Chỉ LONG |
| short_only | Chỉ SHORT |
| no_volume | Bỏ volume check |
| no_momentum | Bỏ momentum gate |

---

## 4. Filter funnel (thứ tự áp dụng)

```
1. Session (dead_zone)     → skip 20h-8h UTC
2. Rule filter             → net_score, trend, RSI, funding, volume, momentum
3. CVD proxy               → LONG: cvd_ratio >= 0.45, SHORT: <= 0.55
4. VWAP bias               → LONG: vwap_distance < 1.5%, SHORT: > -1.5%
5. EMA9 timing              → ema9_crossed_recent_up/down
6. Regime                  → skip volatile (ADX, BB, ATR ratio)
7. Chop Index              → skip nếu > 61.8
8. Correlation             → max 2 cùng hướng
9. Confluence              → score >= threshold (2/3/4)
10. calc_entry_sl_tp       → swing structure hoặc ATR
11. Future candles         → cần >= 3 candles để simulate
```

---

## 5. Trade simulation

### 5.1 Logic

- Duyệt từng candle tương lai (5m cho scalp)
- SL hit: `low <= sl` (LONG) hoặc `high >= sl` (SHORT)
- TP hit: `high >= tp` (LONG) hoặc `low <= tp` (SHORT)
- Nếu cùng candle: ưu tiên SL (conservative)
- Max hold: 9 candles (45 phút) → TIME_EXIT

### 5.2 Trail stop (mirror production)

| Điều kiện | Hành động |
|-----------|-----------|
| Unrealized >= 50% target | Breakeven (sl = entry ± 0.1%) |
| Unrealized >= 80% target | Lock 50% (sl = entry + 50% profit) |

### 5.3 Fee & slippage

- Fee: 0.1% mỗi chiều (0.2% round trip)
- Slippage: 0.05% (constant)

---

## 6. Kết quả chạy (90 ngày BTCUSDT)

### 6.1 Filter funnel (Step 2)

| Filter | Count | % |
|--------|-------|---|
| dead_zone skip | 960 | 16.7% |
| **rule filter** | **4,770** | **82.8%** |
| CVD proxy | 12 | 0.2% |
| EMA9 timing | 17 | 0.3% |
| confluence < N | 1 | 0.0% |
| calc SL/TP fail | 1 | 0.0% |
| **OK Traded** | **0** | **0.00%** |

**Nhận xét:** Rule filter là bottleneck chính — loại 82.8% signal. Với config mặc định (v1), không có trade nào pass.

### 6.2 Strategy comparison (Step 3)

| Strategy | Trades | Win% | PF | MaxDD | PnL |
|----------|--------|------|-----|-------|-----|
| loose | 45 | 17.8% | 0.13 | 0.1% | -0.14% |
| v2 | 24 | 25.0% | 0.19 | 0.1% | -0.06% |

**Target v2:** >= 100 trades, PF >= 1.2, Win% >= 52%

**Kết luận:** Cả hai đều chưa đạt. Win rate < 45% → strategy hiện tại không có edge trong thời gian test.

### 6.3 Walk-forward (Step 5)

- Train 90d + Test 30d
- 0/0 windows (90 ngày không đủ cho 1 window đầy đủ)

### 6.4 Rule cases (Step 7)

| Rule case | Trades | Win% | PF | PnL |
|-----------|--------|------|-----|-----|
| full | 24 | 25.0% | 0.19 | -0.06% |
| long_only | 23 | 26.1% | 0.19 | -0.06% |
| short_only | 6 | 16.7% | 0.49 | -0.01% |
| no_volume | 44 | 13.6% | 0.09 | -0.15% |
| no_momentum | 24 | 25.0% | 0.19 | -0.06% |

- `short_only`: PF cao hơn (0.49) nhưng ít trade (6)
- `no_volume`: nhiều trade hơn (44) nhưng win rate thấp (13.6%)

---

## 7. Metrics target (trước khi live)

| Metric | Minimum | Target |
|--------|---------|--------|
| Trades | >= 100 | >= 200 |
| Win Rate | >= 50% | >= 55% |
| Profit Factor | >= 1.1 | >= 1.3 |
| Max Drawdown | < 5% | < 3% |
| Sharpe Ratio | > 0.8 | > 1.2 |
| OOS consistency | 2/3 windows | 3/3 windows |

---

## 8. File và script

| File | Mô tả |
|------|-------|
| `backtest.py` | Engine chính: download, indicators, filters, simulate, stats |
| `scripts/run_backtest_full_report.py` | Chạy 7 bước, ghi report |
| `scripts/run_full_report.ps1` | PowerShell wrapper |
| `docs/018-backtest-full-report.txt` | Output text report |
| `docs/019-backtest-code-and-results.md` | Tài liệu này |

### Lệnh chạy

```bash
# Download data
python backtest.py --symbol BTCUSDT,ETHUSDT,SOLUSDT --days 180 --download-only

# Full report (7 bước)
python scripts/run_backtest_full_report.py --days 180 --use-cache

# Chỉ bước 2, 3, 5, 7
python scripts/run_backtest_full_report.py --days 90 --use-cache --steps 2,3,5,7

# Funnel diagnosis (CLI)
python backtest.py --symbol BTCUSDT --days 60 --use-cache --funnel
```

### Quy trình workflow (4 bước sau khi sửa ATR/RSI/entry)

```bash
# Bước 1 & 2 — Fix fee + Full 5 thay đổi (loose, 90d, funnel)
python backtest.py --symbol BTCUSDT --days 90 --use-cache --strategy loose --funnel

# Bước 3 — Mở rộng (v2, BTCUSDT+ETHUSDT, 180d, funnel)
python backtest.py --symbol BTCUSDT,ETHUSDT --days 180 --use-cache --strategy v2 --funnel

# Bước 4 — Walk-forward (v2, 180d, train 90, test 30)
python backtest.py --symbol BTCUSDT --days 180 --use-cache --strategy v2 \
  --walk-forward --wf-train 90 --wf-test 30
```

**Script tự động chạy 4 bước:**
```bash
python scripts/run_workflow_backtest.py
python scripts/run_workflow_backtest.py --no-cache
```

**run_backtest_full_report.py với workflow params:**
```bash
python scripts/run_backtest_full_report.py --days 90 --use-cache \
  --days-step4 180 --days-step5 180 --steps 2,3,4,5
```

---

## 9. Khuyến nghị

1. **Rule filter quá chặt** — Thử `--no-momentum-gate`, `--net-score 5`, `--strategy loose` để tăng số trade.
2. **Kiểm tra edge** — Chạy 180 ngày với strategy loose; nếu win rate vẫn < 45% thì cần đổi logic.
3. **Walk-forward** — Dùng `--days 180` trở lên để có đủ windows (train 90d + test 30d).
4. **Multi-symbol** — Thử BTCUSDT, ETHUSDT, SOLUSDT để tăng sample size.
