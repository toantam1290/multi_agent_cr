# Review Backtest — Sau 5 Sửa (Hold Time, Trend, RSI, Session, Net Score)

**Ngày chạy:** 2026-03-08

---

## Các sửa đổi đã áp dụng

| # | Sửa | Mô tả |
|---|-----|-------|
| 1 | max_hold 9 → 12 | 12×5m = 60 phút, match ATR(1h) |
| 2 | Trend strict | LONG: trend_1d == "uptrend", SHORT: == "downtrend" |
| 3 | RSI align trend | bullish/bearish chỉ khi trend_1d khớp |
| 4 | Session | Chỉ london + ny_overlap (bỏ asia, dead_zone) |
| 5 | loose net_score | 5 → 3 |

---

## Kết quả Bước 1 — Loose 90d, BTCUSDT

| Metric | Trước (020) | Sau 5 sửa | Ghi chú |
|--------|-------------|-----------|---------|
| Trades | 69 | **5** | Giảm mạnh do trend strict |
| Win rate | 11.6% | 0% | Sample quá nhỏ |
| PF | 0.22 | 0.00 | - |
| TIME_EXIT | 98.6% | **80%** | Cải thiện (4/5) |
| LONG:SHORT | 53:16 | **3:2** | Cân bằng hơn ✓ |

### Filter Funnel
- session skip (asia/dead): **50.0%** — bỏ ½ candles
- rule filter: 47.9%
- OK Traded: **5** (0.06%)

---

## Kết quả Bước 3 — short_only, Loose 180d, BTCUSDT+ETHUSDT

| Metric | Kết quả | Kỳ vọng |
|--------|---------|----------|
| Trades | **7** | > 35 |
| Win rate | 14.3% | > 35% |
| PF | 0.56 | > 0.6 |
| TIME_EXIT | **100%** | - |
| ny_overlap | 2 trades, 50% win | - |

---

## Kết quả Bước 4 — v2 Full 180d, BTCUSDT+ETHUSDT

| Metric | Trước (020) | Sau 5 sửa |
|--------|-------------|-----------|
| Trades | 195 | **15** |
| Win rate | 22.1% | **33.3%** |
| PF | 0.28 | 0.37 |
| TIME_EXIT | 33.3% | **100%** |
| LONG:SHORT | 153:42 | **10:5** |

### Theo session
- London: 9 trades, **44.4% win**
- ny_overlap: 6 trades, 16.7% win

### Theo direction
- LONG: 10 trades, **50% win**
- SHORT: 5 trades, 0% win

---

## Phân tích

### Vấn đề: Trend quá chặt
- `trend_1d == "uptrend"` và `== "downtrend"` loại bỏ **sideways** (phần lớn thời gian).
- Kết quả: 5–15 trades thay vì 69–195 → không đủ sample để đánh giá edge.

### Điểm tích cực
1. **LONG:SHORT cân bằng** — 3:2, 10:5 thay vì 53:16, 153:42.
2. **London 44.4% win** (v2) — cao nhất.
3. **LONG 50% win** (v2) — cải thiện so với 18.3% trước.

### TIME_EXIT vẫn cao
- Bước 1: 80% (5 trades)
- short_only: 100% (7 trades)
- v2: 100% (15 trades)
- 60 phút có thể vẫn chưa đủ cho ATR(1h) move. Cân nhắc tăng lên **24 nến (2h)**.

---

## Khuyến nghị

1. **Nới trend** — Thử `!= "downtrend"` cho LONG và `!= "uptrend"` cho SHORT (như cũ) nhưng **thêm RSI + net_score chặt** để giảm LONG khi sideways.
2. **Tăng max_hold** — Thử 24 (2h) nếu TIME_EXIT vẫn > 60%.
3. **Chỉ London** — Test `session == "london"` (44.4% win trong v2).
4. **LONG only trong uptrend** — Với trend strict, LONG 50% win có tiềm năng; cần thêm sample.
