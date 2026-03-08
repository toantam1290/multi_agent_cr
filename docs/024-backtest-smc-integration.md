# Backtest SMC Integration — Thiết kế & Mô tả

## Mục đích

Thêm SMC (Smart Money Concepts) vào backtest engine để **validate SMC trước khi chạy live** — đo impact lên win rate, profit factor, số trades.

---

## Thay đổi

### 1. utils/smc.py

**Thêm `analyze_from_dataframes()`** — sync version cho backtest, không fetch API:

```python
def analyze_from_dataframes(
    self,
    df_structure: pd.DataFrame,   # 15m (scalp) hoặc 1h (swing)
    df_timing: pd.DataFrame,     # 5m (scalp) hoặc 15m (swing)
    current_price: float,
) -> SMCSignal
```

**Refactor `analyze()`** — tách logic detection vào `_run_detection()`, dùng chung bởi `analyze()` (async) và `analyze_from_dataframes()` (sync).

### 2. backtest.py

| Vị trí | Thay đổi |
|--------|----------|
| `BacktestConfig` | Thêm `use_smc_filter: bool = True` |
| Funnel | Thêm `"smc"` — đếm SMC opposing reject |
| Sau Chop filter | Gọi `SMCAnalyzer(None).analyze_from_dataframes(df_15m, df_5m, current_price)` |
| | SMC opposing: score ≤ -50 (LONG) hoặc ≥ 50 (SHORT) → skip |
| Confluence | Cộng +2 nếu `smc_valid` + `_smc_has_precision` + score ≥ 50 (LONG) / ≤ -50 (SHORT) |
| Sau calc_entry_sl_tp | OB override: entry = current_price ± 0.1×ATR, SL = OB boundary ± 0.1×ATR |
| CLI | `--no-smc` để tắt SMC (so sánh baseline) |

### 3. Data flow

**Scalp:**
- `df_structure` = 15m, 100 nến (từ `get_window("15m", 100)`)
- `df_timing` = 5m, 50 nến (từ `get_window("5m", 50)`)
- Backtest đã có 15m và 5m trong rolling window → không thêm API call

**Swing:**
- SMC chỉ áp dụng cho scalp (production cũng vậy)
- Swing: `use_smc_filter` bỏ qua, `smc_signal = None`

---

## Logic mirror production

| Production (research_agent) | Backtest |
|-----------------------------|----------|
| 2e. SMC opposing (score ≤ -50 / ≥ 50) | ✅ Skip, funnel["smc"]++ |
| Confluence: _smc_has_precision + score ≥ 50 | ✅ +2 confluence |
| 5b. OB entry override (price_in_ob) | ✅ entry ± 0.1×ATR, SL = OB boundary |

---

## Cách test

### Chạy với SMC (mặc định)

```bash
python backtest.py --symbol BTCUSDT --style scalp --days 90 --funnel
```

### Chạy không SMC (baseline)

```bash
python backtest.py --symbol BTCUSDT --style scalp --days 90 --funnel --no-smc
```

### So sánh

```bash
# With SMC
python backtest.py --symbol BTCUSDT --style scalp --days 90

# Without SMC
python backtest.py --symbol BTCUSDT --style scalp --days 90 --no-smc
```

So sánh: trades, win_rate, profit_factor, max_drawdown.

---

## Funnel

Khi `--funnel`, output thêm dòng:

```
-> SMC opposing          :   xxx (x.x%)
```

Số lần bị reject do SMC score ngược chiều direction.

---

## Kỳ vọng

- **SMC ON** vs **SMC OFF**: ít trades hơn (nhiều reject), win rate có thể cao hơn nếu SMC filter đúng
- Nếu SMC ON làm win rate **giảm** → cần review threshold (50, _smc_has_precision)
- OB override: số trade dùng OB entry có thể nhỏ, cần funnel chi tiết hơn nếu muốn đo

---

## Giới hạn

- SMC chỉ chạy cho **scalp** (style=swing không có SMC)
- Không có Claude → không có pre-mortem risk assess
- Whale, orderbook, CVD thật: backtest dùng proxy (volume-based)
