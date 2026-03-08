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

| Funding (8h) | LONG | SHORT |
|--------------|------|-------|
| > 0.10% (extreme) | 0.6× | 1.3× |
| > 0.05% (elevated) | 0.85× | 1.15× |
| -0.07% ~ +0.05% (neutral) | 1.0× | 1.0× |
| < -0.03% (elevated) | 1.15× | 0.85× |
| < -0.07% (extreme) | 1.3× | 0.6× |

---

## Lớp 2: Open Interest × Price

**4 tình huống cổ điển:**

| OI 24h | Price 24h | Ý nghĩa |
|--------|-----------|---------|
| Tăng | Tăng | Long buildup — trend thật |
| Tăng | Giảm | Short buildup — trend thật |
| Giảm | Tăng | Short squeeze / unwind |
| Giảm | Giảm | Long liquidation / unwind |

- OI tăng + price cùng chiều → xác nhận SMC direction → 1.2×
- OI tăng + price ngược chiều → SMC ngược trend → 0.85×
- OI giảm + price tăng → short squeeze → LONG 1.15×, SHORT 0.7×
- OI giảm + price giảm → long liq → SHORT 1.15×, LONG 0.7×

---

## Lớp 3: CVD tại OB/FVG

**Quan trọng nhất khi giá đang TRONG OB hoặc FVG zone.**

- `cvd_ratio` > 0.58 = buy pressure dominant
- `cvd_trend` = "accelerating_buy" | "accelerating_sell" | "neutral"

| Setup | Zone | CVD | Multiplier |
|-------|------|-----|------------|
| LONG | In OB/FVG | ratio > 0.58 hoặc accelerating_buy | 1.25× |
| LONG | In OB/FVG | ratio < 0.42 hoặc accelerating_sell | 0.65× |
| SHORT | In OB/FVG | ratio < 0.42 hoặc accelerating_sell | 1.25× |
| SHORT | In OB/FVG | ratio > 0.58 hoặc accelerating_buy | 0.65× |
| Ngoài zone | — | accelerating theo direction | 1.1× |

---

## Tích hợp vào SMCAgent

```
scan_pair(symbol)
  ├── asyncio.gather: setup, deriv, cvd, stats_24h
  ├── Nếu setup None/invalid → return None
  ├── Lớp 1: interpret_funding(deriv.funding_rate, direction) → confidence *= mult
  ├── Lớp 2: interpret_oi(deriv.oi_change_pct, stats.price_change_pct, direction) → confidence *= mult
  ├── Lớp 3: interpret_cvd(cvd_data, direction, price_in_ob, price_in_fvg) → confidence *= mult
  ├── confidence = clamp(0, 100)
  ├── Nếu confidence < min_confidence → reject, log
  └── setup.confidence = confidence; setup.reasoning += confluence notes
```

---

## File thay đổi

| File | Thay đổi |
|------|----------|
| `utils/crypto_confluence.py` | **Mới** — `interpret_funding`, `interpret_oi`, `interpret_cvd` |
| `agents/smc_agent.py` | Gọi 3 fetcher song song, áp dụng confluence, reject nếu confidence < min |

---

## Lưu ý

- **Futures symbol:** Binance dùng cùng format (BTCUSDT). Một số pair có thể không có futures → `get_derivatives_signal` trả `fetch_ok=False`, confluence bỏ qua.
- **fetch_ok:** `DerivativesSignal.fetch_ok=False` khi API fail → không dùng magic number `funding_rate != 0.0005`.
- **Fail gracefully:** Nếu deriv/cvd/stats fail → vẫn emit SMC signal (chỉ không adjust).
- **Backtest:** Confluence chưa tích hợp vào backtest; cần thêm nếu muốn so sánh live vs backtest.
