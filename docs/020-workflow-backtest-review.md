# Review Kết quả Workflow Backtest (sau 5 thay đổi)

**Ngày chạy:** 2026-03-08  
**Workflow:** 4 bước (loose 90d → v2 180d multi-symbol → walk-forward)

---

## Tóm tắt thay đổi đã áp dụng

| # | Thay đổi | Mô tả |
|---|----------|-------|
| 1 | ATR 5m → 1h | ATR(1h) ~0.25–0.35% đủ cover fee 0.2% |
| 2 | SCALP_RR 1.5 → 2.0 | TP gross lớn hơn, net dương sau fee |
| 3 | RSI trend-following | Mua khi RSI 52–75 (momentum up), bán khi 25–48 |
| 4 | RSI gate trong rule | LONG: 50 < RSI < 78, SHORT: 22 < RSI < 50 |
| 5 | Entry market | Bỏ pullback limit, entry = current_price |

---

## Kết quả Bước 1 & 2 — Loose, 90d, BTCUSDT

| Metric | Kết quả | Kỳ vọng | Đạt? |
|--------|---------|---------|------|
| Trades | 69 | ~45 | ✓ (nhiều hơn) |
| Win rate | 11.6% | > 40% | ❌ |
| Profit Factor | 0.22 | ≥ 0.5 | ❌ |
| PnL | -0.25% | - | ❌ |

### Filter Funnel
| Filter | Count | % |
|--------|-------|---|
| dead_zone skip | 1,440 | 16.7% |
| rule filter | 6,137 | 71.0% |
| VWAP bias | 410 | 4.7% |
| confluence < N | 442 | 5.1% |
| calc SL/TP fail | 99 | 1.1% |
| **OK Traded** | **69** | **0.80%** |

### Phân tích
- **98.6% TIME_EXIT** — Hầu hết trade không chạm TP/SL trong 45 phút, thoát theo thời gian.
- **LONG 7.5% win** vs **SHORT 25% win** — SHORT tốt hơn rõ rệt.
- **Avg RR 1.66** — Cải thiện so với trước (TP/SL lớn hơn nhờ ATR 1h).
- **PF 0.22** — Vẫn thấp, win rate quá thấp.

---

## Kết quả Bước 3 — v2, 180d, BTCUSDT+ETHUSDT

| Metric | Kết quả | Kỳ vọng | Đạt? |
|--------|---------|---------|------|
| Trades | 195 | > 80 | ✓ |
| Win rate | 22.1% | > 45% | ❌ |
| Profit Factor | 0.28 | > 1.0 | ❌ |
| PnL | -0.56% | - | ❌ |

### Outcome breakdown
| Outcome | Count | % |
|---------|-------|---|
| SL | 82 | 42.1% |
| TIME_EXIT | 65 | 33.3% |
| TP | 48 | 24.6% |

### Theo hướng
| Direction | Trades | Win% | Avg PnL |
|-----------|--------|------|---------|
| LONG | 153 | 18.3% | -0.18% |
| SHORT | 42 | 35.7% | -0.02% |

### Theo session
| Session | Trades | Win% |
|---------|--------|------|
| ny_overlap | 63 | 33.3% |
| asia | 78 | 17.9% |
| london | 54 | 14.8% |

---

## Kết luận

### Không đạt target
- Win rate 11.6% (Bước 2) và 22.1% (Bước 3) **< 40%**.
- PF 0.22–0.28 **< 0.5**.
- Strategy hiện tại **không có edge** sau 5 thay đổi.

### Điểm tích cực
1. **Số trade tăng** — 69 (loose) và 195 (v2 multi) so với trước (~24–45).
2. **SHORT tốt hơn LONG** — Win rate SHORT 25–35.7% vs LONG 7.5–18.3%.
3. **ny_overlap tốt nhất** — 33.3% win rate trong Bước 3.
4. **TP đã xuất hiện** — 24.6% trades hit TP (Bước 3), không còn 98% TIME_EXIT.

### Hướng tiếp theo (theo doc gốc)
> Nếu win rate với `--strategy loose` vẫn dưới 40% trên sample 50+ trades → RSI(15m) không có predictive power đủ mạnh cho scalp crypto. Bước tiếp theo: **test swing style** (1h candle, hold 4–24h) với indicators giữ nguyên — fee/move ratio tốt hơn.

### Đề xuất
1. **Thử `--style swing`** — Giữ logic, đổi timeframe.
2. **Chỉ trade SHORT** — `--rule-case short_only` để tận dụng edge SHORT.
3. **Chỉ trade ny_overlap** — Thêm session filter chặt hơn.
4. **Xem lại RSI logic** — Có thể cần indicator khác (MACD, EMA cross) thay RSI cho scalp.
