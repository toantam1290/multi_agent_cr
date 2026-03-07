# 013 — Deep Review: Kế hoạch Trading với Scalping

**Ngày:** 2025-03-07  
**Mục đích:** Đánh giá toàn diện flow scalping hiện tại, chỉ ra gaps và đề xuất cải thiện.

---

## 1. Tổng quan flow Scalping

```
SCAN_MODE=opportunity → trading_style=scalp (auto)
     ↓
Opportunity screening (24h volatility 5–25%, confluence, cooldown)
     ↓
analyze_pair(pair) với style=scalp
     ↓
Technical: 15m direction + 5m timing/ATR + 1h ADX + 4h trend
     ↓
Rule-based filter → Regime → calc_entry_sl_tp (ATR 5m, mult 1.0/0.8)
     ↓
Claude pre-mortem → Signal → Risk Manager → Telegram → User approve → Execute
     ↓
Monitor positions (2 min) → SL/TP check
```

---

## 2. Điểm mạnh hiện tại

| Thành phần | Đánh giá |
|------------|----------|
| **Option A structure** | 15m direction + 5m timing hợp lý, giảm noise |
| **ATR 5m** | SL/TP tight phù hợp scalp |
| **Trend filter 4h** | Không trade ngược HTF |
| **Pullback bonus** | RSI 5m oversold/overbought +10 điểm |
| **Config separation** | trading_style tách biệt swing/scalp |

---

## 3. Gaps & Rủi ro

### 3.1 Scan interval vs Scalp timing

| Vấn đề | Chi tiết |
|--------|----------|
| **15 phút quá chậm** | Scalp setup có thể fade trong 5–15 phút. Cơ hội 5m có thể mất trước khi scan chạy. |
| **Opportunity dùng 24h volatility** | Screening dựa trên `priceChangePercent` 24h. Scalp cần volatility ngắn hơn (1h, 4h). |
| **Cooldown 2 cycle (30 phút)** | Cặp vừa scan xong nghỉ 30 phút. Scalp có thể có re-entry trong 15–30 phút. |

**Đề xuất:**
- Thêm `SCAN_INTERVAL_MIN` (env): 15 (swing) vs 5 (scalp) — scalp scan mỗi 5 phút.
- Opportunity screening: thêm mode `volatility_window=1h` cho scalp (cần API ticker 1h hoặc derive từ klines).

---

### 3.2 Rule-based filter — RSI threshold

```python
# LONG: rsi_1h < 45
# SHORT: rsi_1h > 55
```

- Với scalp: `rsi_1h` = RSI 15m. Ngưỡng 45/55 có thể quá chặt cho scalp (15m RSI dao động nhanh).
- **Đề xuất:** Config `RSI_LONG_MAX` / `RSI_SHORT_MIN` theo style, hoặc nới: scalp 50/50.

---

### 3.3 Pullback logic — thiếu alignment check

```python
if style == "scalp":
    if rsi_4h < 40: bullish += 10  # rsi_4h = RSI 5m
    if rsi_4h > 70: bearish += 10
```

- **Vấn đề:** 5m oversold cộng bullish ngay cả khi 15m bearish. Có thể tạo net_score dương khi direction mâu thuẫn.
- **Đề xuất:** Chỉ cộng khi aligned:
  - `rsi_5m < 40` và `trend_1d == "uptrend"` → bullish +10 (pullback long)
  - `rsi_5m > 70` và `trend_1d == "downtrend"` → bearish +10 (pullback short)

---

### 3.4 Regime — ADX/BB từ TF khác nhau

- ADX từ 1h, BB width từ 15m, ATR ratio từ 5m.
- `classify_regime` dùng chung cho swing và scalp. Ngưỡng ADX 25, BB 0.03 có thể không tối ưu cho scalp.
- **Đề xuất:** (Optional) Thêm `regime_adx_trending` / `regime_bb_squeeze` config theo style.

---

### 3.5 Position monitor — 2 phút có thể trễ

- Paper trading: check SL/TP mỗi 2 phút.
- Scalp: giá có thể chạm SL/TP trong vòng 1–2 phút. Có thể bị "overshoot" (giá vượt SL rồi quay lại trước khi check).
- **Đề xuất:** Khi `trading_style=scalp`, dùng `minutes=1` cho position monitor.

---

### 3.6 Claude prompt — nhãn RSI gây hiểu nhầm

```
- RSI 1h: {technical.rsi_1h:.1f} | RSI 4h: {technical.rsi_4h:.1f}
```

- Với scalp: rsi_1h = 15m, rsi_4h = 5m. Claude có thể hiểu sai.
- **Đề xuất:** Khi scalp, đổi label: `RSI 15m` / `RSI 5m (timing)`.

---

### 3.7 Không có config riêng cho scalp

| Config | Swing | Scalp | Ghi chú |
|--------|-------|-------|--------|
| MIN_CONFIDENCE | 75 | 75 | Scalp có thể cần cao hơn (80) vì setup nhanh |
| MIN_RISK_REWARD | 2.0 | 2.0 | Scalp có thể chấp nhận 1.5 |
| MAX_POSITION_PCT | 2% | 2% | Có thể giảm scalp (1%) vì risk cao hơn |
| APPROVAL_TIMEOUT | 300s | 300s | Scalp setup fade nhanh, 5 phút có thể quá lâu |

**Đề xuất:** Thêm `SCALP_MIN_CONFIDENCE`, `SCALP_APPROVAL_TIMEOUT_SEC` (optional).

---

### 3.8 Whale data — hours_back=4

- Whale `hours_back=4` cố định. Scalp có thể chỉ cần 1–2h.
- **Đề xuất:** (Low priority) Param `whale_hours_back` theo style.

---

### 3.9 R:R cố định 1:2

- `calc_entry_sl_tp` luôn R:R 1:2. Một số scalp strategy dùng 1:1.5 hoặc partial TP.
- **Đề xuất:** (Optional) `SCALP_RISK_REWARD_RATIO=1.5` env.

---

## 4. Checklist cải thiện (ưu tiên)

| # | Item | Effort | Impact | Trạng thái |
|---|------|--------|--------|------------|
| 1 | Pullback alignment: chỉ +10 khi trend aligned | S | M | ✅ Done |
| 2 | Claude prompt: đổi label RSI khi scalp | S | L | ✅ Done |
| 3 | Position monitor: 1 phút khi scalp | S | M | ✅ Done |
| 4 | SCAN_INTERVAL_MIN: 5 phút khi scalp | M | H | ✅ Done |
| 5 | Rule-based: RSI threshold config theo style | S | L | ✅ Done |
| 6 | Scalp-specific config (confidence, timeout) | M | M | ✅ Done |
| 7 | Opportunity: volatility window ngắn hơn cho scalp | L | M | ⬜ Future |

---

## 5. Đã triển khai (2025-03-07)

- **Pullback alignment:** Chỉ +10 bullish khi 5m oversold VÀ 4h uptrend; +10 bearish khi 5m overbought VÀ 4h downtrend.
- **Claude prompt:** Label RSI 15m/5m khi scalp; Trend 4h.
- **Scan interval:** scalp=5 min, swing=15 min (SCAN_INTERVAL_MIN).
- **Position monitor:** scalp=1 min, swing=2 min (POSITION_MONITOR_INTERVAL_MIN).
- **Scalp config:** SCALP_MIN_CONFIDENCE=80, SCALP_APPROVAL_TIMEOUT_SEC=120, SCALP_RSI_LONG_MAX=50, SCALP_RSI_SHORT_MIN=50.

---

## 6. Kết luận

- **Cấu trúc Option A (15m direction, 5m timing)** ổn và phù hợp scalp.
- **Gaps chính:** scan interval 15 phút quá chậm, pullback logic chưa check alignment, position monitor 2 phút có thể trễ.
- **Đã triển khai:** Pullback alignment, Claude RSI labels, Monitor 1 phút, Scan 5 phút, config scalp.
- **Future:** Opportunity volatility window ngắn hơn (1h/4h) cho scalp.

---

## 7. Antigravity AI Review — Đã triển khai (2026-03-07)

| # | Issue | Status |
|---|-------|--------|
| 6 | Price freshness guard 0.3% | ✅ _on_user_approve reject nếu drift > 0.3% |
| 2 | Pullback entry (LONG -0.2*ATR, SHORT +0.2*ATR) | ✅ calc_entry_sl_tp scalp |
| 1 | 1h active volatility filter | ✅ _filter_by_1h_range, SCALP_1H_RANGE_MIN_PCT |
| 3 | R:R 1:1.5 config | ✅ SCALP_RISK_REWARD_RATIO=1.5 |
| 4 | Volume confirmation | ✅ volume_ratio >= 1.2 hoặc volume_spike |
| 5 | Time-of-day filter | ✅ SCALP_ACTIVE_HOURS_UTC=8-16 |
| 7 | Volume spike directional | ✅ green candle +10 bullish, red +10 bearish |
| 8 | Regime trending_volatile | ✅ ATR mult 1.2 cho scalp |
| 10 | EMA cross + price confirm | ✅ close > EMA21, weight 15 |

---

## 9. Deep Review Final — Đã triển khai (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| 1.1 | SHORT funding > 0.05% quá cao | FUNDING_SHORT_MIN_PCT=0.02 (config) |
| 1.2 | EMA cross + MACD lagging cho scalp | Scalp: bỏ EMA/MACD, thêm RSI momentum + candle body |
| 1.3 | Claude confidence 75 vs scalp cần 80 | System prompt scalp: confidence >= 80, horizon 15-60 min |
| 2.7 | max_tokens 500 truncate | 800 |

---

## 10. Deep Review v2 — Đã triển khai (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| 1.1 | Freshness guard reject LONG khi giá tăng (valid pullback) | Reject chỉ khi price breaks SL: LONG nếu current < SL, SHORT nếu current > SL |
| 1.2 | RSI momentum single-candle, r2 dead | r0 > r1 AND r1 > r2 AND (r0-r2) > 2.0 (2 nến + delta) |
| 1.3 | volatile regime → SL tight như ranging | Skip scalp khi regime = "volatile" |

---

## 11. Deep Review v3 — Đã triển khai (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| 1.2 | Freshness guard miss: price < entry (LONG) | Reject thêm: LONG nếu current < entry, SHORT nếu current > entry |
| 1.3 | RSI momentum optional | Required gate: scalp cần momentum_bullish/bearish để pass filter |
| 1.4 | Candle body dùng nến chưa đóng | iloc[-2] (nến đã đóng) |
| 2.1 | RSI 40-50 bullish không check trend | Chỉ cộng khi trend_1d == "uptrend" / "downtrend" |
| 2.4 | Claude không biết momentum_triggered | Thêm vào prompt khi scalp |

---

## 12. Deep Review v4 — Đã triển khai (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| 1.1 | Freshness guard v3 regression — reject trade R:R tốt hơn | Revert về v2: chỉ reject khi SL bị phá (current < SL LONG, current > SL SHORT) |
| 2.1 | Volume spike + direction dùng iloc[-1] (nến chưa đóng) | Đổi sang iloc[-2] cho volume, close, open — nhất quán với candle body |

---

## 13. Deep Review v5 — Đã triển khai (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| 1 | vol_slice tự bao gồm prev_volume (bug) | vol_slice = iloc[-22:-2] — 20 nến TRƯỚC iloc[-2] |
| 2 | Funding SHORT 0.02% block nhiều setup | Default 0.005%, env FUNDING_SHORT_MIN_PCT |
| 3 | Whale hours_back=4 quá dài cho scalp | scalp_whale_hours config (API vẫn dùng limit=1000 = recent) |

---

## 14. Deep Review v6 — Đã triển khai (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| 1 | startTime whale API làm data cũ hơn (regression) | Bỏ startTime — limit=1000 = 1000 trades mới nhất, đúng cho scalp |

---

## 15. Deep Review v6+ — Volume filter + Paper fill (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| 2 | Volume filter 1.2x reject early-in-move | Thêm volume_trend_up (3 nến tăng liên tiếp) — pass nếu spike OR ratio>=1.2 OR trend |
| 3 | Paper fill 100% pullback | Scalp: nếu price > entry*1.002 (LONG) hoặc < entry*0.998 (SHORT) → return None (no fill) |

---

## 16. Deep Review v7 — Đã triển khai (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| 1 | volume_trend_up không có min floor (micro-volume pass) | v2 > avg_volume * 0.5 |
| 2 | Regime mix timeframes (BB 15m, ATR 5m) | Scalp: bb_width_regime, atr_ratio_regime từ df_adx (1h) — nhất quán với ADX |
| 3 | Comment outdated funding 0.02% | Sửa thành 0.005% |

---

## 17. Deep Review v8 — Đã triển khai (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| A1 | net_score > 10 dead gate cho scalp | Scalp: net_long_min=20, net_short_max=-20 (cần thêm 1 confirmation) |
| A2 | Claude không biết entry gap | Thêm "Entry gap: X.XX% from current (pullback limit)" vào prompt |

---

## 8. Round 2 Fixes (2026-03-07)

| # | Issue | Fix |
|---|-------|-----|
| A | Executor bỏ qua pullback entry | Paper scalp: fill tại signal.entry_price (limit simulated) |
| B | 1h range dùng 24 candles | Dùng iloc[-2:] (1-2h gần nhất) + semaphore 10 |
| D | Telegram timeout hardcoded 5 phút | _approval_timeout_min() dynamic (scalp 2, swing 5) |
| E | Volume filter block core pairs | Core pairs exempt volume_ratio check |
| G | 1h range parallel rate limit | Semaphore(10) |
| H | RSI 30-70 triệt tiêu | 40-50: +5 bullish, 50-60: +5 bearish |

---

## Run guide

Config env và hướng dẫn chạy: [docs/014-run-guide.md](014-run-guide.md)
