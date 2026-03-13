# 027 — Crypto Confluence cho SMC (Funding, OI, CVD)

## Tổng quan

SMC engine hiện tại chỉ dùng OHLCV đa timeframe. Với **crypto perpetual futures**, có thêm 3 lớp dữ liệu quan trọng mà forex không có:

| Lớp | Nguồn | Ý nghĩa |
|-----|-------|---------|
| **Funding Rate** | Binance Futures | Đám đông đang lean long hay short → squeeze potential |
| **Open Interest** | Binance Futures | Money đang vào hay ra → xác nhận trend thật vs unwind |
| **CVD** | Futures aggTrades (buy_vol - sell_vol) | Áp lực mua/bán thật tại OB/FVG zone |

Data layer: `get_derivatives_signal`, `get_cvd_signal(use_futures=True)`, `get_24h_stats(use_futures=True)`. SMC dùng **futures** cho cả 3 để khớp với funding/OI (tránh spot/futures mismatch).

---

## Lớp 1: Funding Rate

**Logic:** Funding cao dương = thị trường overleveraged long → SHORT setup được boost, LONG bị penalize. Ngược lại với funding âm.

**Funding filter (symmetric ±0.03%):** Config `FUNDING_LONG_MAX_PCT=0.03`, `FUNDING_SHORT_MIN_PCT=-0.03`. Block LONG khi funding > +0.03%, block SHORT khi funding < -0.03%. Đảm bảo filter symmetric — trước đây chỉ filter 1 chiều.

| Funding (8h) | LONG | SHORT |
|--------------|------|-------|
| > 0.10% (extreme) | -12 pts | +10 pts |
| > 0.05% (elevated) | -5 pts | +5 pts |
| -0.03% ~ +0.03% (neutral) | 0 pts | 0 pts |
| < -0.03% (elevated) | +5 pts | -5 pts |
| < -0.07% (extreme) | +10 pts | -12 pts |

> **Phase 1 update:** Funding giờ trả point adjustment thay vì multiplier. Points được cộng vào confidence (không nhân).

---

## Lớp 2: Open Interest × Price

**4 tình huống cổ điển:**

| OI 24h | Price 24h | Ý nghĩa |
|--------|-----------|---------|
| Tăng | Tăng | Long buildup — trend thật |
| Tăng | Giảm | Short buildup — trend thật |
| Giảm | Tăng | Short squeeze / unwind |
| Giảm | Giảm | Long liquidation / unwind |

- OI tăng + price cùng chiều → xác nhận SMC direction → +8 pts
- OI tăng + price ngược chiều → SMC ngược trend → -5 pts
- OI giảm + price tăng → short squeeze → LONG +5 pts, SHORT -10 pts
- OI giảm + price giảm → long liq → SHORT +5 pts, LONG -10 pts

> **Phase 1 update:** OI giờ trả point adjustment thay vì multiplier.

---

## Lớp 3: CVD tại OB/FVG

**Quan trọng nhất khi giá đang TRONG OB hoặc FVG zone.**

- `cvd_ratio` > 0.58 = buy pressure dominant
- `cvd_trend` = "accelerating_buy" | "accelerating_sell" | "neutral"

| Setup | Zone | CVD | Adjustment |
|-------|------|-----|------------|
| LONG | In OB/FVG | ratio > 0.58 hoặc accelerating_buy | +8 pts |
| LONG | In OB/FVG | ratio < 0.42 hoặc accelerating_sell | -12 pts |
| SHORT | In OB/FVG | ratio < 0.42 hoặc accelerating_sell | +8 pts |
| SHORT | In OB/FVG | ratio > 0.58 hoặc accelerating_buy | -12 pts |
| Ngoài zone | — | accelerating theo direction | +3 pts |

> **Phase 1 update:** CVD giờ trả point adjustment thay vì multiplier.

---

## Tích hợp vào SMCAgent

**Quan trọng (Phase 1 update):** Confidence adjustment dùng **weighted average of point adjustments**, không dùng multiplier nữa. Mỗi lớp trả point adjustment (range -12 to +10). Weighted average được **cộng** vào base confidence, cap tại ±15 pts. Tránh trường hợp multiplier cascade collapse confidence quá mức (base 80 × 0.6 × 0.7 × 0.65 = 22 — quá khắc nghiệt).

Weights: Funding = 0.4, OI = 0.3, CVD = 0.3.

```
scan_pair(symbol)
  ├── asyncio.gather: setup, deriv, cvd, stats_24h, fear_greed
  ├── Nếu setup None/invalid → return None
  ├── Lớp 1: interpret_funding(deriv.funding_rate, direction) → adjs.append(pts), weights.append(0.4)
  ├── Lớp 2: interpret_oi(deriv.oi_change_pct, stats.price_change_pct, direction) → adjs.append(pts), weights.append(0.3)
  ├── Lớp 3: interpret_cvd(cvd_data, direction, price_in_ob, price_in_fvg) → adjs.append(pts), weights.append(0.3)
  ├── combined_adj = weighted_average(adjs, weights), capped to [-15, +15]
  ├── confidence = clamp(base_confidence + combined_adj, 0, 100)
  ├── Nếu confidence < min_confidence → reject, log
  └── setup.confidence = confidence; setup.reasoning += confluence notes
```

**Impact:** Base 80 + worst case (-15) = 65. So với trước: base 80 × worst multiplier = ~52. Hệ thống ổn định hơn, ít false reject.

---

## File thay đổi

| File | Thay đổi |
|------|----------|
| `utils/crypto_confluence.py` | **Mới** — `interpret_funding`, `interpret_oi`, `interpret_cvd` (trả point adjustments thay vì multipliers từ Phase 1) |
| `agents/smc_agent.py` | Gọi 3 fetcher song song, áp dụng confluence, reject nếu confidence < min |

---

## Lưu ý

- **Futures symbol:** Binance dùng cùng format (BTCUSDT). Một số pair có thể không có futures → `get_derivatives_signal` trả `fetch_ok=False`, confluence bỏ qua.
- **fetch_ok:** `DerivativesSignal.fetch_ok=False` khi API fail → không dùng magic number `funding_rate != 0.0005`.
- **Fail gracefully:** Nếu deriv/cvd/stats fail → vẫn emit SMC signal (chỉ không adjust).
- **Backtest:** Confluence chưa tích hợp vào backtest; cần thêm nếu muốn so sánh live vs backtest.
