# SMC Integration Design — Smart Money Concepts

## Tổng quan

Thiết kế này mô tả việc thêm **Smart Money Concepts (SMC)** vào pipeline research agent một cách **minimally invasive** — không sửa logic hiện tại, chỉ thêm context SMC để Claude đánh giá tốt hơn.

---

## Bối cảnh

### Chẩn đoán project hiện tại

- **Trường phái**: Indicator-based Hybrid — không thuộc trường phái rõ ràng
- **Mạnh**: Claude risk assessor, kill zone check, SL từ swing structure, regime classifier, derivatives
- **Yếu**: Không có Order Flow thật (CVD proper, Footprint, DOM), không có SMC thật (OB, FVG, CHoCH/BoS)
- **Kết luận**: Swing/Position Trading disguised as Scalping + Indicator voting + Structure SL (nhẹ SMC) + Claude risk filter

### Lý do chọn SMC thay vì Order Flow

| Tiêu chí | Order Flow | SMC |
|----------|------------|-----|
| Dữ liệu cần | Level 2, Footprint, CVD real-time | OHLCV là đủ |
| Binance API | Một phần | Có đầy đủ |
| Backtestable | Rất khó | Dễ hơn |
| Phù hợp bot 15 phút | Không | Có |

**SMC Core** + **Order Flow nhẹ** (CVD, orderbook imbalance) để confirm entry.

---

## Kiến trúc Integration

### Luồng sau khi thêm SMC

```
Data fetch (parallel)
    ↓
Rule-based filter (giữ nguyên)
    ↓
CVD divergence (giữ nguyên)
    ↓
VWAP bias (giữ nguyên)
    ↓
EMA9 timing (giữ nguyên)
    ↓
SMC analyze ← MỚI (15m + 5m klines, non-blocking)
    ↓ smc_score → confluence +2 (if valid)
    ↓ sweep → confluence +1 (if swept)
    ↓
Regime + Chop (giữ nguyên)
    ↓
Confluence gate (giữ nguyên, SMC đã cộng điểm)
    ↓
calc_entry_sl_tp (giữ nguyên)
    ↓
Claude prompt ← MỚI: thêm smc_signal.summary
    ↓
Claude PROCEED/WAIT/AVOID (thông minh hơn nhờ context SMC)
    ↓
TradingSignal (thêm field smc=dict)
```

### Vai trò SMC

- **SMC làm context cho Claude**, không phải hard filter
- Claude nhận thêm: Bias (CHoCH xác nhận?), Entry zone (OB gần?), TP zone (FVG?), Risk (Liquidity pool?)
- Claude tự weigh SMC context vào PROCEED/WAIT/AVOID

---

## File thay đổi

| File | Thay đổi | Số dòng |
|------|----------|---------|
| `utils/smc.py` | Tạo mới | ~320 dòng |
| `models.py` | Thêm 1 field optional | 1 dòng |
| `research_agent.py` | Import + init + call + prompt | ~15 dòng |

**Không động vào**: config.py, risk_manager.py, executor_agent.py, market_data.py, database.py, telegram_bot.py

---

## Chi tiết kỹ thuật

### 1. utils/smc.py — Thành phần

1. **Swing High/Low detection** — nền tảng của tất cả SMC
2. **Market Structure** — CHoCH / BoS
3. **Order Blocks (OB)** — vùng tổ chức đặt lệnh
4. **Fair Value Gaps (FVG)** — vùng mất cân bằng
5. **Liquidity Levels** — equal highs/lows (stop loss cụm)
6. **Liquidity Sweep detection** — tổ chức vừa "hunt" stops

### 2. Dataclasses

- `OrderBlock`: price_high, price_low, mid, direction, candle_index, strength_pct
- `FairValueGap`: top, bottom, mid, direction, filled
- `LiquidityLevel`: price, side, touches, swept
- `SMCSignal`: bias, last_structure_event, nearest OB/FVG, liquidity, smc_score, summary

> **Phase 1 update:** `SMCSetup` (từ doc 025) thêm fields `ob_zone_low` / `ob_zone_high` cho OB zone fill. Entry cascade giờ là `ob_entry → sweep_reversal → bpr_entry` (`ce_entry` disabled — 25% WR negative edge). Displacement detection loosened: full = 1.2x ATR + 50% body (was 1.5x + 60%), near-displacement = 1.0-1.2x ATR + 55% body → +12 confidence, lookback = 15 candles (was 10). Confluence dùng additive point adjustments thay vì multipliers (xem doc 027).

### 3. SMC Score (-100..+100)

| Thành phần | Weight |
|------------|--------|
| Bias (structure) | ±30 |
| CHoCH (bias change) | ±25 |
| BoS (continuation) | ±15 |
| Price in OB | ±25 |
| Price in FVG | ±15 |
| Liquidity sweep | ±20 |

### 4. Timeframes

- **scalp**: structure=15m (100 candle), timing=5m (50 candle)
- **swing**: structure=1h (100 candle), timing=15m (50 candle)

---

## Rủi ro cần biết

- SMC detection trên OHLCV không hoàn hảo — Order Block có false positive
- CHoCH/BoS cần định nghĩa rõ "swing" là bao nhiêu nến
- FVG có thể bị lấp đầy trước khi bot chạy chu kỳ tiếp theo (15 phút delay)

---

## Lợi thế

| Điểm | Lý do |
|------|-------|
| Ít rủi ro | Không phá logic hiện tại — SMC chỉ là thêm context |
| Dễ test | Có thể bật/tắt SMC bằng 1 flag config |
| Dễ rollback | Xóa 1 file + 2 dòng là xong |
| Claude hiểu SMC | Claude được train trên nhiều SMC content — prompt SMC context hiệu quả |
