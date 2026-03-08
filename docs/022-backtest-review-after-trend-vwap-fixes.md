# Review Backtest — Sau Trend Filter + VWAP Fix (022)

**Ngày chạy:** 2026-03-08

---

## Các sửa đổi đã áp dụng (trước khi chạy)

| # | Sửa | Mô tả |
|---|-----|-------|
| 1 | Trend filter | LONG: `!= "downtrend"`, SHORT: `!= "uptrend"` (cho phép sideways) |
| 2 | VWAP | Reset mỗi ngày UTC — intraday VWAP thực, không còn VWMA 200 nến |
| 3 | Scalp SL | ATR-only (bỏ swing structure) |
| 4 | Confluence | CVD max 1 điểm |

---

## Kết quả Bước 1 — Loose Scalp 180d, BTCUSDT

**Lệnh:** `python backtest.py --symbol BTCUSDT --style scalp --days 180 --strategy loose --funnel`

| Metric | 021 (trend strict) | 022 (trend loose) | Ghi chú |
|--------|-------------------|-------------------|---------|
| Trades | 5 | **61** | Sample tăng mạnh ✓ |
| Win rate | 0% | **24.6%** | Cải thiện |
| PF | 0.00 | 0.32 | Vẫn thấp |
| TIME_EXIT | 80% | **65.6%** | Giảm |
| LONG:SHORT | 3:2 | **57:4** | LONG chiếm đa số |

### Filter Funnel
| Filter | Count | % |
|--------|-------|---|
| session skip (asia/dead) | 8,641 | 50.0% |
| rule filter | 7,499 | 43.4% |
| VWAP bias | 74 | 0.4% |
| chop > 61.8 | 73 | 0.4% |
| correlation | 15 | 0.1% |
| confluence < N | 918 | 5.3% |
| **OK Traded** | **61** | **0.35%** |

### Theo direction
| Direction | Trades | Win% | Avg PnL |
|-----------|--------|------|---------|
| LONG | 57 | 21.1% | -0.25% |
| SHORT | 4 | **75.0%** | +0.29% |

### Theo session
| Session | Trades | Win% |
|---------|--------|------|
| london | 21 | 14.3% |
| ny_overlap | 40 | 30.0% |

---

## Kết quả Bước 2 — Swing 180d, BTCUSDT+ETHUSDT

**Lệnh:** `python backtest.py --symbol BTCUSDT,ETHUSDT --style swing --days 180 --funnel`

| Metric | Kết quả | Kỳ vọng |
|--------|---------|---------|
| Trades | **5** | > 50 |
| Win rate | 20.0% | > 35% |
| PF | 0.54 | > 1.0 |
| TIME_EXIT | 0% | - |
| SL | 80% | - |

**Nhận xét:** Swing dùng filter chặt (ema9, conf≥3, cvd) → quá ít signal. Không đủ sample để đánh giá edge.

---

## Kết quả Bước 3 — Short Only, Loose Scalp 180d, BTCUSDT+ETHUSDT

**Lệnh:** `python backtest.py --symbol BTCUSDT,ETHUSDT --style scalp --days 180 --strategy loose --rule-case short_only --funnel`

| Metric | 021 short_only | 022 short_only | Ghi chú |
|--------|----------------|----------------|---------|
| Trades | 7 | **19** | Tăng gấp đôi |
| Win rate | 14.3% | **42.1%** | Cải thiện rõ |
| PF | 0.56 | **0.91** | Gần breakeven |
| TIME_EXIT | 100% | 84.2% | Giảm |

### Theo session
| Session | Trades | Win% |
|---------|--------|------|
| london | 5 | 0.0% |
| ny_overlap | 14 | **57.1%** |

**Nhận xét:** SHORT + ny_overlap có tiềm năng — 57.1% win rate trên 14 trades. PF 0.91 gần 1.0.

---

## Bước 4 — Walk-Forward Swing

**Lệnh:** `python backtest.py --symbol BTCUSDT,ETHUSDT --style swing --days 365 --walk-forward --wf-train 120 --wf-test 30`

**Không chạy:** Swing chỉ 5 trades trong 180d → walk-forward không có ý nghĩa. Cần nới filter swing trước.

---

## Phân tích tổng hợp

### Điểm tích cực
1. **Sample size** — Trend loose tăng trades từ 5 lên 61 (BTCUSDT 180d).
2. **SHORT edge** — 75% win (4 trades) full, 42.1% win (19 trades) short_only. ny_overlap SHORT 57.1%.
3. **VWAP fix** — Filter VWAP bias chỉ loại 74 candles (0.4%), hợp lý hơn.
4. **TIME_EXIT giảm** — 65.6% (scalp full) vs 80–100% trước.

### Vấn đề còn lại
1. **LONG thống trị** — 57:4 ratio, SHORT quá ít trong full mode.
2. **Swing quá chặt** — 5 trades/180d, cần loose preset cho swing.
3. **PF < 1** — Chưa profitable.

### Khuyến nghị
1. **Test SHORT only + ny_overlap** — Chỉ trade SHORT trong ny_overlap session.
2. **Nới swing filter** — Thêm `--strategy loose` cho swing hoặc giảm confluence/ema9.
3. **Giảm scalp_rr** — Thử RR 1.2–1.3 để TP dễ hit hơn (giảm TIME_EXIT).
