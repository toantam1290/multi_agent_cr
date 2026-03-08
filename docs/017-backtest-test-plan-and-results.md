# Backtest Test Plan & Results

**Date:** 2026-03-07  
**Engine:** `backtest.py`  
**Scope:** Scalp mode, BTCUSDT + ETHUSDT  
**Period:** 60–180 ngày (2026-01-06 → 2026-03-07)

---

## 1. Test Plan

### 1.1 Phase 1: Baseline & Sanity

| # | Scenario | Command | Mục đích |
|---|----------|---------|----------|
| 1.1 | Baseline 60d | `--symbol BTCUSDT --style scalp --days 60` | Full filters |
| 1.2 | Baseline 90d | `--symbol BTCUSDT --style scalp --days 90` | Mở rộng thời gian |
| 1.3 | Multi-symbol 60d | `--symbol BTCUSDT,ETHUSDT --style scalp --days 60` | Combined mode |
| 1.4 | Confluence 2 | `--symbol BTCUSDT --days 60 --confluence 2` | Nới confluence |

### 1.2 Phase 2: Filter Impact

| # | Scenario | Command |
|---|----------|---------|
| 2.1 | No EMA9 | `--no-ema9` |
| 2.2 | No Confluence | `--no-confluence` |
| 2.3 | No Chop | `--no-chop` |
| 2.4 | No CVD | `--no-cvd` |
| 2.5 | No Session | `--no-session` |
| 2.6 | No VWAP | `--no-vwap` |
| 2.7 | No Regime | `--no-regime` |
| 2.8 | No Correlation | `--no-correlation` (multi-symbol) |
| 2.9 | No Dynamic Confluence | `--no-dynamic-confluence` |

### 1.3 Phase 3: Parameter Optimization

| # | Scenario | Command |
|---|----------|---------|
| 3.1 | Optimize 90d | `--days 90 --optimize` (sweep conf 2–5, RR 1.2/1.5/2.0) |

### 1.4 Phase 4: Walk-Forward

| # | Scenario | Command |
|---|----------|---------|
| 4.1 | Walk-forward | `--days 180 --walk-forward --wf-train 90 --wf-test 30` |

---

## 2. Results Summary

### 2.1 Phase 1: Baseline

| Scenario | Trades | Win% | PF | MaxDD | PnL% | Verdict |
|----------|--------|------|-----|-------|------|---------|
| 1.1 Baseline 60d | 0 | - | - | - | - | Filter quá chặt |
| 1.2 Baseline 90d | 0 | - | - | - | - | Filter quá chặt |
| 1.3 Multi-symbol 60d | 0 | - | - | - | - | Filter quá chặt |
| 1.4 Confluence 2 | 0 | - | - | - | - | Vẫn 0 trade |

**Phát hiện:** Full filters (EMA9 + confluence + chop + CVD + VWAP + session) → **0 trades** trong 60–90 ngày. EMA9 timing là bottleneck chính.

### 2.2 Phase 2: Filter Impact (60 ngày)

| Scenario | Trades | Win% | PF | MaxDD | PnL% | Ghi chú |
|----------|--------|------|-----|-------|------|---------|
| 2.1 No EMA9 | **4** | 50.0 | 0.95 | 0.0 | -0.0 | Chỉ nới EMA9 → có trades |
| 2.2 No EMA9 + No Confluence | **6** | 50.0 | 0.90 | 0.0 | -0.0 | 6 trades, marginal |
| 2.3 No EMA9 + No Chop | 4 | 50.0 | 0.95 | 0.0 | -0.0 | Chop không phải bottleneck |
| 2.4 No EMA9 + No CVD | 4 | 50.0 | 0.95 | 0.0 | -0.0 | CVD không phải bottleneck |
| 2.5 No EMA9 + No Session | 4 | 50.0 | 0.95 | 0.0 | -0.0 | Session không phải bottleneck |
| 2.6 No Rule | **36** | 16.7 | 0.17 | 0.1 | -0.1 | Nhiều trades nhưng rất tệ |

### 2.3 Chi tiết No EMA9 (4 trades)

- **Outcome:** TP 2, SL 1, TIME_EXIT 1
- **Session:** London 2 (50%), NY 2 (50%)
- **Regime:** Ranging 2 (0% win), Trending_up 2 (100% win)
- **Direction:** Tất cả LONG
- **Trail stop:** 1/4 activated (25%)

### 2.4 Chi tiết No Rule (36 trades)

- **Outcome:** TP 15, SL 13, TIME_EXIT 8
- **Session:** Asia 7.7%, London 12.5%, NY 26.7% win
- **Regime:** Ranging 5% win, Trending_up 31% win
- **Direction:** LONG 18, SHORT 18 — cả hai đều 16.7% win
- **Kết luận:** Rule-based filter + EMA9 đang lọc bớt noise; bỏ hoàn toàn → performance tệ

### 2.5 Phase 3 & 4

| Scenario | Status | Ghi chú |
|----------|--------|---------|
| 3.1 Optimize 90d | Chạy với full filters → 0 trades mọi params | Cần `--no-ema9` để có sample |
| 4.1 Walk-forward | Chạy với `--no-ema9` | Đang chạy |

---

## 3. Findings & Recommendations

### 3.1 Bottleneck chính: EMA9 timing

- **EMA9 crossed recent** (3 nến) quá strict → hầu hết setup bị reject
- Chỉ khi tắt EMA9 mới có trades (4–6 trades/60 ngày)
- **Đề xuất:** Cân nhắc nới EMA9: (a) mở rộng sang 4–5 nến, hoặc (b) thêm điều kiện "close gần EMA9" thay vì bắt buộc cross

### 3.2 Filter stack hiệu quả

- **Rule-based + EMA9** giữ được quality: 4 trades 50% win vs 36 trades 16.7% win khi bỏ rule
- Confluence, Chop, CVD, Session: khi EMA9 pass thì mới có tác dụng; hiện tại đều 0 trades nên chưa đo được impact riêng

### 3.3 Regime phân biệt rõ

- **Trending_up:** 100% win (2/2) với no-ema9, 31% với no-rule
- **Ranging:** 0–5% win — nên skip hoặc thắt chặt hơn

### 3.4 Next steps

1. **Nới EMA9** trong production hoặc backtest: thử 4 nến, hoặc "close trong 0.5% EMA9"
2. **Optimize với --no-ema9:** chạy sweep confluence/RR để tìm params tốt nhất khi có trades
3. **Paper trade 3–5 ngày** với RELAX_FILTER để đo funnel (ema9_rejected %, confluence_rejected %)
4. **Session:** Asia 7.7% win — cân nhắc giới hạn Asia session hơn nữa

---

## 4. Automated Suite (run_backtest_suite.py)

Script chạy 13 kịch bản tự động, dùng **cache mặc định** (data/backtest_cache/).

### 4.1 Kết quả mới nhất (60 ngày, 2026-03-08)

| ID | Scenario | Trades | Win% | PF | MaxDD | PnL% | PnL$ |
|----|----------|--------|------|-----|-------|------|------|
| 1.1 | Baseline full | 0 | 0.0 | 0.00 | 0.0 | +0.00 | $0 |
| 1.2 | Confluence 2 | 0 | 0.0 | 0.00 | 0.0 | +0.00 | $0 |
| 2.1 | No EMA9 | 4 | 50.0 | 0.95 | 0.0 | -0.00 | -$0.05 |
| 2.2 | No EMA9 + No Confluence | 6 | 50.0 | 0.90 | 0.0 | -0.00 | -$0.13 |
| 2.3 | No EMA9 + No Chop | 4 | 50.0 | 0.95 | 0.0 | -0.00 | -$0.05 |
| 2.4 | No EMA9 + No CVD | 4 | 50.0 | 0.95 | 0.0 | -0.00 | -$0.05 |
| 2.5 | No EMA9 + No Session | 4 | 50.0 | 0.95 | 0.0 | -0.00 | -$0.05 |
| 2.6 | No Rule | 36 | 16.7 | 0.17 | 0.1 | -0.11 | -$11.11 |
| 3.1 | Rule: full | 4 | 50.0 | 0.95 | 0.0 | -0.00 | -$0.05 |
| 3.2 | Rule: long_only | 4 | 50.0 | 0.95 | 0.0 | -0.00 | -$0.05 |
| 3.3 | Rule: short_only | 0 | 0.0 | 0.00 | 0.0 | +0.00 | $0 |
| 3.4 | Rule: no_volume | 5 | 20.0 | 0.17 | 0.0 | -0.02 | -$2.33 |
| 3.5 | Rule: no_momentum | 9 | 22.2 | 0.34 | 0.0 | -0.02 | -$2.09 |

**Ghi chú:** Kết quả ghi vào `docs/017-backtest-results.txt`. Chạy ~21 phút (60 ngày, dùng cache).

---

## 5. Cách chạy lại tests

```powershell
# Set encoding (Windows)
$env:PYTHONIOENCODING = "utf-8"

# 1. Download data trước (nếu chưa có cache)
python backtest.py --symbol BTCUSDT --days 60 --download-only

# 2. Chạy suite tự động (dùng cache mặc định, ~21 phút cho 60 ngày)
python scripts/run_backtest_suite.py --days 60
# Kết quả → docs/017-backtest-results.txt

# 3. Chạy nhanh với 7 ngày
python scripts/run_backtest_suite.py --days 7

# 4. Không dùng cache (download mới)
python scripts/run_backtest_suite.py --days 60 --no-cache

# 5. Chạy thủ công từng case
python backtest.py --symbol BTCUSDT --days 60 --no-ema9 --rule-case long_only
python backtest.py --symbol BTCUSDT,ETHUSDT --days 60
```

**Rule cases:** `full` | `long_only` | `short_only` | `no_volume` | `no_momentum`

**PnL:** Initial balance $10,000, position 2% = $200/trade.

