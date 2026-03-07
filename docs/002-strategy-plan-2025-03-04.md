# 002 — Strategy & Capital Deployment Plan

**Ngày:** 2025-03-04  
Đánh giá thẳng thắn về kiến trúc bot hiện tại và roadmap để có thể profitable.

---

## Tổng quan thực tế

**Câu hỏi cốt lõi:** Con bot này có thể kiếm tiền không?

**Câu trả lời thẳng:** Ở kiến trúc hiện tại — không. Không phải vì code tệ, mà vì không có edge (lợi thế thống kê) nào cả. Nhưng có thể sửa được nếu làm đúng cách.

---

## 1. STRATEGY LAYER — Edge thực sự là gì?

### Vấn đề cốt lõi

OHLCV + RSI + MACD + Fear&Greed là dữ liệu mà hàng triệu trader khác đang nhìn vào cùng lúc. Bất kỳ pattern nào từ những indicator này đều đã bị arbitrage đi từ lâu. Claude đọc những gì người khác đã đọc và ra quyết định giống họ — đó là định nghĩa của zero edge.

### Claude có thể dùng được không?

Có, nhưng không phải để dự đoán giá. Claude có giá trị thực ở:

1. **Behavioral discipline** — enforce R:R rules, tránh FOMO, tránh revenge trading. Đây là edge thực sự khi so với retail trader cảm xúc.
2. **Narrative synthesis** — đọc nhiều signals rời rạc và tổng hợp thành một context nhất quán. Không phải để predict, mà để filter noise.
3. **Regime detection** — phân biệt trending vs ranging market, bull vs bear macro. Claude có thể làm tốt hơn một threshold đơn giản. *(Quyết định cuối: dùng ADX deterministic — Section 6 Sprint 3b, Section 7.)*

### Thay đổi cụ thể cho prompt và data

**Thêm vào data inputs (miễn phí):**

- Binance order book imbalance (bid/ask ratio tại các price levels) — signals short-term momentum tốt hơn MACD
- Funding rate trên perpetual futures (Binance public API) — predictive cho mean reversion
- Open interest change (Binance public) — divergence giữa OI và price là leading indicator
- Liquidation levels (Coinglass free tier) — price thường bị kéo về cluster liquidations

**Lưu ý:** Funding + OI + basis đều derived từ cùng underlying (Binance perpetual positioning). Khi cả 3 nói "bullish" — đó là 1 signal nhìn từ 3 góc, không phải 3 confirmations độc lập. Combination này giảm noise, không tạo edge mới. Edge thực sự cần ít nhất 1 uncorrelated signal (on-chain, social sentiment). Với public data hiện tại: funding/OI/basis = **1 factor**, không phải 3.

**Thay đổi prompt architecture:**

Thay vì hỏi Claude "Should I buy/sell?", cấu trúc lại thành 3 bước. **Lưu ý:** Rule-based Gate chạy trước Claude — không dùng regime (output của Claude) trong filter.

| Step | Tên | Input | Output | Gọi Claude? |
|------|-----|-------|--------|--------------|
| 1 | Rule-based Gate | technical indicators + derivatives | LONG / SHORT / None | Không |
| 2 | Regime Classifier | OHLCV + volume | ADX(14) + BB Width + ATR ratio → regime | Không (deterministic) |
| 3 | Entry Refiner + Risk Assessment | regime + price + ATR + factors | entry, SL, TP, PROCEED/WAIT/AVOID | Có (1 call duy nhất) |

**Thay đổi từ 3-round review:** Regime detection dùng ADX + Bollinger Bandwidth + ATR ratio thay vì Claude — backtestable, free, giảm 50% API cost. Claude chỉ dùng cho **risk assessment** (pre-mortem), không phải direction prediction.

**Rule-based Gate (Step 1)** — chỉ dùng raw indicators, không dùng regime:

```
LONG:  trend_1d != "downtrend" AND rsi_1h < 45 AND funding_rate < 0.05% AND net_score > 10
SHORT: trend_1d != "uptrend" AND rsi_1h > 55 AND funding_rate > 0.05% AND net_score < -10
ELSE:  → No trade (không gọi Claude)
```

Claude chỉ được gọi khi Gate trả về LONG hoặc SHORT. Có thể backtest riêng phần rule-based.

### Thêm một signal thực sự có edge

**Binance Spot vs Perp premium** (hoàn toàn miễn phí): Khi spot price > perp price đáng kể → shorts bị kẹt, dễ squeeze lên. Khi perp > spot → longs overextended. Đây là dữ liệu ít người retail dùng hơn RSI.

**Cụ thể:** `(perp_price - spot_price) / spot_price * 100` là basis.

- Khi basis > +0.3% và giảm → short opportunity
- Khi basis < -0.2% và tăng → long opportunity

**Freshness check:** Khi basis signal triggered → re-fetch price NGAY trước khi send Telegram. Nếu price đã move > 0.5% so với analysis time → discard signal. 5 dòng code, không cần websocket.

---

## 2. VALIDATION LAYER — Bằng chứng tối thiểu trước khi live

Không được live trade khi chưa có đủ các số này:

| Metric | Minimum để tiếp tục | Target trước khi scale |
|--------|---------------------|------------------------|
| Win rate | > 45% (với R:R 1:2 thì breakeven ở 33%) | > 50% |
| Profit Factor | > 1.3 | > 1.5 |
| Max Drawdown | < 20% của capital | < 15% |
| Sharpe Ratio | > 0.8 | > 1.2 |
| Sample size | Tối thiểu 150 closed trades | 200+ |
| Paper trading duration | Tối thiểu 5-6 tháng | 6 tháng |

**150 trades là minimum cho statistical significance.** Với 50 trades, p-value > 0.1 cho hầu hết test — không có ý nghĩa thống kê. Minimum viable: Win rate ≠ 50% cần ~170 trades, Sharpe > 0 cần ~100, Profit factor > 1 cần ~150. Với 15-phút scan và $500 capital, 150 trades mất ít nhất 5-6 tháng.

### Cách đo với SQLite hiện tại

Viết một script `backtest_report.py` query từ trades table:

```python
# Các query cần thiết:
# 1. Win rate = COUNT(pnl > 0) / COUNT(*)
# 2. Profit factor = SUM(pnl WHERE pnl > 0) / ABS(SUM(pnl WHERE pnl < 0))
# 3. Max drawdown: dùng running cumsum của pnl, tìm peak-to-trough lớn nhất
# 4. Sharpe = (mean_daily_pnl / std_daily_pnl) * sqrt(365)  # crypto 24/7. Khi so sánh với TradFi dùng sqrt(252).
# 5. Phân tích by regime, by pair, by time-of-day
```

**Quan trọng:** Phân tách kết quả theo symbol và time_of_day. Nếu win rate tốt chỉ ở BTC/USDT lúc 9-11h UTC, đó là insight thực sự. Nếu đều tốt ở mọi pair và mọi giờ → có thể là overfitting hoặc may mắn.

### Walk-forward test

**Quan trọng:** Không được tune parameters trên toàn bộ dữ liệu rồi gọi là "out-of-sample". Đây là lỗi phổ biến.

- **Ngày 1-60:** Chạy paper, không dùng kết quả để điều chỉnh strategy (in-sample). Không thay đổi rule-based filter thresholds dựa trên kết quả.
- **Đóng băng strategy** sau ngày 60 — không chỉnh gì. **Lưu ý:** Anthropic có thể update model giữa chừng → ghi `model_version` trong mỗi signal (DB) để detect drift.
- **Ngày 61-90:** Chạy với strategy đã đóng băng, dùng kết quả để quyết định go/no-go (out-of-sample).

Nếu metrics tốt ở in-sample nhưng xấu ở out-of-sample → mô hình overfit.

---

## 3. INFRASTRUCTURE LAYER — Những gì thực sự ảnh hưởng PnL

Ngoài các bug đã biết, theo thứ tự ưu tiên:

### P0 — Ảnh hưởng trực tiếp đến tiền

1. **OCO bug** — Fix trước khi live. Không có OCO, stop-loss có thể không fire. Đây là critical.
2. **Slippage accounting** — Paper mode hiện tại có tính slippage không? Với $500 capital trên Binance, slippage 0.1% mỗi chiều = 0.2% round trip. Binance fee là 0.1% maker/taker → tổng cost per trade ≈ 0.3-0.5%. Nếu paper mode bỏ qua điều này, mọi số liệu paper đều bị inflate.
3. **Position sizing** — Hiện tại dùng fixed % hay fixed dollar? Với $500, 2% risk/trade = $10 risk. Với R:R 1:2, target là $20. Sau fee và slippage, expected value per trade ≈ $20×0.5 - $10×0.5 - $2.5 cost ≈ $2.5. Rất nhỏ, cần win rate cao hơn threshold.

### P1 — Ảnh hưởng uptime

4. **Circuit breaker reset** — Fix ngay. Bot tự tắt và không tự bật lại = missed opportunities.
5. **Reconnect logic** — httpx và Telegram connection drops sau vài ngày. Cần exponential backoff + alerting khi bot offline > 5 phút.

### P2 — Nice to have

6. **State persistence qua restart** — Nếu VPS restart, open positions phải được load lại từ DB. Paper mode hiện tại có handle này không?

### Production-grade additions

7. **Heartbeat (Silence detection)** — Bot có thể không trade vì (1) không có signal tốt, hoặc (2) đã crash silently. Cần phân biệt. Mỗi 6 giờ gửi Telegram: "Bot alive. Last scan: X. Signals today: N. Trades today: M." Nếu không nhận trong 7 giờ → bot đã chết. Quan trọng hơn reconnect logic vì detect mọi loại failure.

8. **Database schema migration** — Sprint 2+3 thêm fields mới (regime, atr_pct, derivatives, net_score). Cần `database.migrate()` chạy khi start: check columns tồn tại, nếu không thì ALTER TABLE ADD COLUMN. Gọi trước khi start scheduler. Không làm → crash khi save signal với field mới vào schema cũ.

9. **Claude API cost budget** — **Fatal flaw nếu bỏ qua.** Với pre-filter block ~80%: ~115 scans pass × 2 prompts ≈ 230 calls/ngày. Sonnet ~$0.005/call → ~$1.15/ngày ≈ $35/tháng. $500 × 5% monthly = $25 — API cost > potential profit. **Fix:** `ANTHROPIC_DAILY_BUDGET_USD` hard cap từ ngày 1 (trước Sprint 1). Với 6 pairs paper: ~$0.58/ngày → cap $0.75. Với 3 pairs: cap $0.50. Lưu spend vào `daily_stats.anthropic_spend_usd` (survive restart). Estimated cost ~$0.005/call (Sonnet). Khi daily spend >= cap → skip Claude, reject signal, alert Telegram.

---

## 4. RISK MANAGEMENT LAYER

### Đánh giá setup hiện tại

| Tham số | Hiện tại | Đánh giá |
|---------|----------|----------|
| R:R minimum 1:2 | Hợp lý — breakeven chỉ cần 33% win rate | ✅ |
| SL = 2% | Quá chặt cho crypto — BTC thường noise 1-1.5% trong 15 phút | ⚠️ |
| Confidence >= 85% | Vấn đề nghiêm trọng — xem dưới | ❌ |
| Max daily loss | Cần có | ✅ |

### Vấn đề với 85% confidence threshold

Claude không có calibrated probabilities. Khi Claude nói "confidence 87%", đó là token generation, không phải Bayesian probability. Không thể validate xem 87% confidence thực sự có win rate 87% hay không nếu không có đủ lịch sử.

**Giải pháp thực tế:** Bỏ confidence threshold của Claude. Thay bằng rule-based pre-filter trước khi call Claude. Claude chỉ được call khi đã pass rule-based filter. Sau đó track: "Trong số signals Claude approve, bao nhiêu % win?" — đây mới là số thực sự có ý nghĩa.

### Điều chỉnh SL cho crypto

Với $500 capital và volatile crypto:

- SL nên dựa trên **ATR (Average True Range)** thay vì fixed %. `SL = entry - 1.5 * ATR(14)`
- Với BTC, ATR(14) trên 1h chart thường 0.8-1.5%. Fixed 2% SL đôi khi quá chặt (stopped out early) đôi khi quá rộng (loss quá lớn).
- pandas-ta đã có sẵn ATR, dùng luôn.

### Kelly Criterion cho position sizing

```
Kelly % = W - (1-W)/R
W = win rate, R = reward/risk ratio
```

Với W=0.5, R=2: Kelly = 25% (quá lớn). **Fractional Kelly (1/4):** 6.25% risk per trade.

**Lưu ý capital nhỏ:** Với Stage 1 ($200), 6.25% × $200 = $12.50 risk. ATR-based SL ≈ 1-1.5% cho BTC → position_size = $12.50 / 1.5% = $833 — vượt capital. **Kelly không áp dụng được với $200 khi SL động theo ATR.**

- **Stage 1:** Fixed risk = min(Kelly_risk, available_capital × 40%). Không dùng full position.
- **Stage 2+ ($500):** Kelly áp dụng được. 6.25% × $500 = $31.25 risk per trade.

---

## 5. CAPITAL DEPLOYMENT ROADMAP

### Stage 0: Paper trading + Strategy fix (ngay bây giờ, 0-8 tuần)

**Việc cần làm trước khi bất kỳ cent nào live:**

1. **ANTHROPIC_DAILY_BUDGET_USD = 0.75** (ngày 1, cho 6 pairs paper)
2. Viết backtest_report.py **với buy-and-hold benchmark** (baseline trước fix)
3. Fix OCO bug, slippage, position sizing, asyncio.Lock
4. Thêm funding rate + OI + basis vào data inputs
5. Regime = ADX + BB Width (deterministic), Claude = pre-mortem risk assessment
6. Chạy paper trading 5-6 tháng, 150+ trades

**Go/no-go criteria để sang Stage 1:**

- 150+ closed trades trong paper (statistical significance)
- Profit factor > 1.3
- Max drawdown < 15%
- **Bot beat buy-and-hold BTC** trên risk-adjusted basis (Sharpe)
- Không có incident nào bot offline > 1 giờ trong 2 tuần

---

### Stage 1: Micro live ($200, 8-16 tuần)

Chỉ $200 — không phải vì sợ, mà vì $200 đủ để phát hiện bug live mà không phá sản.

**Constraint nghiêm ngặt:**

- Max 1 open position tại một thời điểm
- Max risk per trade: $4 (2% của $200)
- Chỉ trade BTC/USDT và ETH/USDT — thanh khoản cao nhất, slippage nhỏ nhất
- Không scale up trong Stage 1, kể cả khi winning streak
- **Không thay đổi bất kỳ parameter nào** — không chỉnh RSI threshold, ATR multiplier, rule-based filter. Mỗi khi thay đổi strategy = phải reset trade counter về 0. Cần so sánh performance theo thời gian.

**Go/no-go criteria để sang Stage 2:**

- 30+ live trades
- Live metrics không tệ hơn paper metrics quá 20% (slippage, win rate)
- Không có bug nào gây mất tiền ngoài ý muốn
- Profit factor > 1.2 trong live

---

### Stage 2: Scale ($500 total, 16-24 tuần)

Thêm $300 vào, nâng lên 2 positions tối đa, mở rộng sang SOL/USDT.

**Go/no-go criteria để scale thêm:**

- 60+ live trades tổng
- Sharpe > 1.0 trên rolling 30 ngày
- Consistent profitability ít nhất 2 tháng

---

## Đánh giá thẳng thắn cuối cùng

**Bạn có thể kiếm tiền với kiến trúc này không?**

Có thể, nhưng xác suất thấp ở trạng thái hiện tại. Lý do:

1. OHLCV + RSI + MACD là overcrowded signals — bất kỳ edge nào từ đây đã bị quant fund arbitrage sạch.
2. Claude không phải một oracle giá — nó không biết gì bạn chưa biết. Giá trị của nó là discipline và synthesis, không phải prediction.
3. $500 capital với fee 0.3-0.5%/trade là cực kỳ khó profitable — cần win rate và R:R rất cao để vượt qua friction cost.

**Thứ có thể cứu được con bot này:**

- Funding + OI + basis (1 factor, multi-source) + uncorrelated signal (on-chain, sentiment) nếu có
- Regime detection deterministic (ADX) thay vì Claude
- Claude cho risk assessment (pre-mortem), không prediction
- Strict rule-based pre-filter trước khi dùng Claude
- Kiên nhẫn paper trade 5-6 tháng, 150+ trades trước khi live

**Thứ sẽ giết chết con bot này:**

- Live trade sớm khi chưa có đủ data validation
- Tin vào Claude confidence score mà không backtest
- Ignore slippage + fee trong paper mode

**Nói thẳng:** Nếu không thêm funding rate và OI signals, và không restructure prompt thành 3-step flow, xác suất profitable sau 6 tháng là dưới 30%. Với những thay đổi đó, xác suất có edge tăng lên đáng kể — nhưng con số cụ thể không thể biết trước khi có backtest data. 50-60% là estimate không có cơ sở thống kê.

---

## 6. IMPLEMENTATION ROADMAP — 4 Sprints

*Phần này là ý kiến triển khai cụ thể dựa trên review codebase.*

### Phát hiện từ code hiện tại

1. **AsyncAnthropic đã dùng đúng** — bug "sync blocking async" trong PRODUCTION_NOTES đã được fix. ✅
2. **ArbitrageSignal** có field `funding_rate` (models.py:66) nhưng chưa có fetcher nào điền vào — low-hanging fruit.
3. **Technical score bias LONG** — `compute_technical_signal` (market_data.py) chỉ cộng điểm bullish (RSI < 40 = oversold → long, uptrend +10). Không có điểm tương đương cho SHORT. Kết quả: bot thiên về LONG ngay cả trong downtrend.

---

### Sprint 1: Bug fixes P0 (1-2 ngày)

| Task | Mô tả |
|------|-------|
| **1a. Fix technical score bias** | Trong `compute_technical_signal`: thay score 0-100 (bullish only) bằng `bullish_score` + `bearish_score` hoặc `net_score` (-100 đến +100). Thêm `direction_bias: "LONG" / "SHORT" / "NEUTRAL"` vào TechnicalSignal. Prompt phải hỏi "Given bias is {direction_bias}, confirm or reject?" |
| **1b. Circuit Breaker reset** | Lưu `reset_date` vào DB hoặc file. Mỗi lần check: so sánh `date.today()` với ngày reset cuối. Nếu khác → reset `daily_pnl` về 0 và resume job. |
| **1c. Slippage trong paper mode** | Trong `executor_agent.py` paper mode: `SLIPPAGE_PCT = 0.0015`, `FEE_PCT = 0.001`. LONG: `filled_price = entry * (1 + SLIPPAGE_PCT)`, SHORT: `entry * (1 - SLIPPAGE_PCT)`. Trừ fee vào pnl khi close. |
| **1d. Position sizing bug (C4)** | `portfolio_value` trong research_agent không trừ locked capital → over-leverage khi nhiều positions. Dùng available balance, không total portfolio. |
| **1e. SQLite asyncio.Lock** | `check_same_thread=False` không protect concurrent writes. 2 coroutines write cùng lúc → data corruption. Bọc tất cả write operations trong `database.py` bằng `asyncio.Lock()`. |
| **1f. Database migration** | `migrate()`: signals + regime, net_score, model_version; daily_stats + anthropic_spend_usd; trades + fees_usdt. Idempotent (ignore duplicate column). Gọi trước khi start scheduler. |

---

### Sprint 2: Data layer — thêm signals có edge (3-4 ngày)

| Task | Mô tả |
|------|-------|
| **2a. 3 fetchers mới trong BinanceDataFetcher** | Binance Futures public API (không cần API key): `GET fapi/v1/fundingRate`, `fapi/v1/openInterest`, `fapi/v1/markPrice`. Basis = `(markPrice - indexPrice) / indexPrice * 100`. |
| **2b. ATR vào compute_technical_signal** | `ta.atr(high, low, close, length=14)`, `atr_pct = atr_value / current_price * 100`. Thêm `atr_pct` vào TechnicalSignal. SL = entry ± 1.5 * ATR thay vì fixed 2%. |
| **2c. DerivativesSignal model** | Tạo model mới (hoặc rename ArbitrageSignal): `funding_rate`, `funding_rate_annualized`, `open_interest_usdt`, `oi_change_pct`, `basis_pct`, `signal` ("LONG_SQUEEZE" / "SHORT_SQUEEZE" / "NEUTRAL"), `score` (-100 đến +100). Logic: funding > 0.05% AND basis > 0.2% → SHORT_SQUEEZE; funding < -0.03% AND basis < -0.1% → LONG_SQUEEZE. |
| **2d. Gather trong analyze_pair** | Thêm `get_derivatives_signal(pair)` vào `asyncio.gather` cùng với technical, whale, sentiment. |

---

### Sprint 3: Restructure strategy — 3-step flow (3-4 ngày)

| Task | Mô tả |
|------|-------|
| **3a. Rule-based pre-filter** | `_rule_based_filter(technical, derivatives) -> Optional[str]` trong research_agent. Returns "LONG" / "SHORT" / None. LONG: trend_1d != downtrend, RSI < 45, funding < 0.05%, net_score > 10. SHORT: trend_1d != uptrend, RSI > 55, funding > 0.05%, net_score < -10. Claude chỉ được gọi khi filter trả về non-None. |
| **3b. Regime = ADX + BB Width + ATR** | Thay Claude Regime Classifier bằng deterministic: ADX(14) + Bollinger Bandwidth + ATR ratio. Output: trending_up / trending_down / ranging / volatile. Tiết kiệm 1 Claude call/signal = giảm 50% API cost. |
| **3c. Claude prompt → Pre-mortem (Risk Assessment)** | Claude KHÔNG predict direction. Prompt mới: "Given this setup: [data]. 1) Top 3 risks that could invalidate this trade. 2) Most likely way this trade loses money. 3) News/event catalyst in 24h? → Rate: PROCEED / WAIT / AVOID." Stress-test thesis, không hỏi "giá đi đâu?". |
| **3d. Flow mới trong analyze_pair** | 1) gather data → 2) _rule_based_filter() → 3) None thì return → 4) _regime_deterministic() (ADX+BB) → 5) _claude_risk_assessment() (1 call) → 6) PROCEED thì build TradingSignal với ATR-based SL → 7) Save DB. |

---

### Sprint 4 (thực hiện Tuần 1): Backtest report — baseline trước bug fix

**Methodology:** Backtest report phải có TRƯỚC khi fix bất cứ gì — cần baseline để so sánh "trước fix" vs "sau fix". Nếu DB trống → chạy paper trade song song trong khi viết script.

Tạo `utils/backtest_report.py` — standalone: `python utils/backtest_report.py [--days 30]`. Output: text report + CSV.

**Queries:** win_rate, profit_factor, total_trades, avg_pnl | max drawdown (cumsum peak-to-trough) | Sharpe `(mean_daily_pnl / std_daily_pnl) * sqrt(365)` (crypto 24/7; so sánh TradFi dùng sqrt(252)) | per-symbol (BTC/ETH/SOL) | per-hour UTC | rolling 30-day stats.

**Dependency:** Chỉ query columns hiện có trong DB (signals, trades). Không query regime/atr_pct — chưa tồn tại cho đến sau Sprint 1f migration. Khi có migration → thêm regime breakdown vào report sau.

**BẮT BUỘC — Buy-and-hold benchmark:** Thêm column `benchmark_buy_hold_return`. Câu hỏi đúng không phải "bot profitable không?" mà "bot có beat buy-and-hold BTC không, trên risk-adjusted basis?". Nếu BTC +80% mà bot +30% → bot profitable nhưng vô nghĩa. Failure mode phổ biến: beat $0 nhưng thua passive strategy.

Thêm `regime: Optional[str]` vào TradingSignal và DB (sau migration).

---

### Thứ tự thực hiện (sau 3-round review)

| Tuần | Sprint |
|------|--------|
| 1 | **Sprint 4 (backtest report) TRƯỚC** — baseline. **+ Budget cap** (daily_stats.anthropic_spend_usd). Nếu DB trống → paper song song. |
| 2 | Sprint 1 (bugs: score bias, slippage, position sizing, asyncio.Lock, migration) |
| 3 | Sprint 2 (data fetchers) |
| 4 | Sprint 3 (restructure: ADX regime, pre-mortem Claude, entry/SL/TP rule-based) |
| 5+ | Paper trade 5-6 tháng, 150+ trades, 6 pairs |
| Pre-Live | OCO fix + Binance balance fetch + testnet → Stage 1 ($200, 2 pairs) |

---

### Quyết định cần làm

**ArbitrageSignal** trong models.py có `funding_rate` nhưng tên sai semantics (không phải arbitrage data).

| Option | Mô tả |
|--------|-------|
| **A** | Rename ArbitrageSignal → DerivativesSignal và mở rộng. Sạch hơn, cần cập nhật references. ArbitrageSignal hiện không được dùng trong TradingSignal → rename an toàn. |
| **B** | Giữ ArbitrageSignal, tạo DerivativesSignal mới riêng. Ít rủi ro nhưng thêm model trùng lặp. |

*Đề xuất: Option A.*

---

## TỔNG KẾT 3-ROUND REVIEW (2025-03-04)

*Synthesis từ Senior Quant Review + Counter-review + Counter-counter-review.*

### Verdict chung

- **Review gốc đúng ~70%** — overstated API cost ($15/ngày), funding rate "hoàn toàn dead"
- **Phản biện đúng ~75%** — nhưng "multi-factor = edge" chưa có evidence; burden of proof ở người claim
- **Kết luận:** Câu hỏi "bot có edge không?" không trả lời bằng thêm review — chỉ trả lời bằng **data**. Backtest không hoàn hảo, nhưng không có backtest thì không có gì cả.

### 5 việc cần làm — theo thứ tự (hội tụ sau 3 rounds)

| # | Việc | Effort | Lý do |
|---|------|--------|-------|
| 1 | Budget cap (daily_stats.anthropic_spend_usd) | 30 phút | Tránh API runaway, survive restart. |
| 2 | `backtest_report.py` với **buy-and-hold benchmark** | 1-2 ngày | Baseline trước khi thay đổi bất cứ gì. Nếu DB trống → paper song song. |
| 3 | Sửa `RESEARCH_SYSTEM_PROMPT` → **pre-mortem style** | 1 giờ | Thay đổi có giá trị nhất, effort thấp nhất. Không predict, stress-test thesis. |
| 4 | Fix 3 bugs: position sizing (C4), slippage, asyncio.Lock | 1 ngày | P0 correctness bugs. |
| 5 | ADX + BB Width cho regime thay vì Claude call | 3-4 giờ | Giảm 50% API cost, tăng determinism. |

**Tổng: ~4-5 ngày.** Sau đó chạy paper trade, thu thập data, để data trả lời câu hỏi mà 3 rounds review không thể trả lời.

### Điểm đồng thuận 3 rounds

| Điểm | Verdict |
|------|---------|
| API cost là fatal flaw nếu không có budget cap | Đồng thuận |
| 50 trades quá ít → 150 minimum, 200+ target | Đồng thuận |
| Position sizing bug (C4) — dùng available balance | Đồng thuận |
| SQLite asyncio.Lock cho concurrent writes | Đồng thuận |
| Buy-and-hold benchmark bắt buộc trong backtest | Đồng thuận |
| Claude cho risk assessment, không prediction | Đồng thuận |
| ADX cho regime, không Claude | Đồng thuận |
| Funding/OI/basis = 1 factor (correlated), không 3 | Đồng thuận |
| Grid trading — hợp lý nhưng khác scope, không pivot | Đồng thuận |

### Không thay đổi

- Funding rate strategy — giữ, clarify multi-factor không standalone
- Basis signal — giữ, 15-phút polling đủ cho momentum; thêm freshness check
- Architecture tổng thể — không pivot sang grid trading

---

## 7. IMPLEMENTATION DECISIONS — 8 Quyết định cụ thể

*Tích hợp từ 2 bản trả lời. Khi có mâu thuẫn, ghi rõ lựa chọn.*

### 1. Budget cap — persist ở đâu?

**Quyết định:** Lưu vào `daily_stats.anthropic_spend_usd` (column mới).

**Lý do:** Survive restart. `daily_stats` đã có `date` PRIMARY KEY. Mỗi lần call Claude xong → `UPDATE daily_stats SET anthropic_spend_usd = anthropic_spend_usd + 0.005 WHERE date = today`. Khi start: load spend của ngày → nếu >= cap thì block.

**⚠️ Khi code:** `daily_stats` row có thể chưa tồn tại cho ngày hôm nay (row chỉ tạo khi có trade close). Ngày đầu chạy → query NULL → crash. **Fix:** `INSERT OR IGNORE INTO daily_stats (date, anthropic_spend_usd) VALUES (today, 0.0)` mỗi khi bot start hoặc khi ngày đổi.

**Alternative (không chọn):** agent_logs count API_CALL — không cần column mới nhưng estimate kém chính xác, query phức tạp hơn.

---

### 2. Regime detection — logic cụ thể

**Quyết định:** ADX trên 4h (ổn định hơn 1h), BB Width trên 1h, ATR ratio = ATR14/ATR50.

```python
def classify_regime(adx, plus_di, minus_di, bb_width, atr_ratio) -> str:
    if atr_ratio > 1.5: return "volatile"   # ATR14 > 1.5× ATR50
    if adx > 25:
        return "trending_up" if plus_di > minus_di else "trending_down"
    if adx < 20 and bb_width < 0.03: return "ranging"
    return "ranging"  # default conservative
```

**Ngưỡng:** ADX > 25 = trending (Wilder), ADX < 20 = ranging, BB Width < 3% = squeeze, ATR ratio > 1.5 = volatile. Tất cả có trong pandas-ta.

---

### 3. Entry / SL / TP — ai tính?

**Quyết định:** Rule-based hoàn toàn. Claude KHÔNG output entry/SL/TP.

```python
def calc_entry_sl_tp(direction, current_price, atr_value, regime) -> tuple:
    mult = 1.5 if regime in ("trending_up", "trending_down") else 1.2
    entry = current_price
    if direction == "LONG":
        sl = entry - mult * atr_value
        tp = entry + 2.0 * (entry - sl)
    else:
        sl = entry + mult * atr_value
        tp = entry - 2.0 * (sl - entry)
    return entry, sl, tp
```

Claude prompt nhận entry/SL/TP đã tính → chỉ trả lời PROCEED / WAIT / AVOID.

---

### 4. OCO bug — sprint nào?

**Quyết định:** KHÔNG trong Sprint 1. Tách Sprint "Pre-Live" trước Stage 1.

**Lý do:** Paper mode dùng `_monitor_positions` check SL/TP → không cần OCO. OCO chỉ relevant cho live. Sprint 1–4 đều paper → defer.

**Pre-Live (trước Stage 1):** Fix OCO (Binance OCO order hoặc cancel remaining khi 1 fill) + Binance balance fetch + test testnet.

**Guard tạm thời:** Trong `_real_execute`, nếu chưa có OCO → `raise NotImplementedError("Live disabled until OCO. Set PAPER_TRADING=true.")`.

---

### 5. Migration — schema cụ thể

| Bảng | Cột mới | Type | Ghi chú |
|------|---------|------|---------|
| signals | regime | TEXT | trending_up / trending_down / ranging / volatile |
| signals | net_score | INTEGER | -100 đến +100 |
| signals | model_version | TEXT | "claude-sonnet-4-6" |
| daily_stats | anthropic_spend_usd | REAL | DEFAULT 0.0 |
| trades | fees_usdt | REAL | Slippage + fee cho PnL chính xác |

**Không thêm column:** atr_pct, derivatives — đã trong raw_json (TechnicalSignal/TradingSignal serialized).

---

### 6. Available balance

**Quyết định:** `_get_available_balance() = paper_balance - sum(open_trades.position_size_usdt)`.

Logic đã có trong risk_manager; research_agent cần dùng thay vì `_get_portfolio_value()` (total). `get_open_trades()` từ trades table.

---

### 7. Số cặp trade

**Quyết định:** 6 pairs paper, 2 pairs Stage 1 live (BTC, ETH), 3 pairs Stage 2 (+ SOL).

**Lý do:** 6 pairs → nhiều data hơn → đạt 150 trades nhanh hơn. API cost ~$0.58/ngày (115 calls × $0.005) — vượt cap $0.50. **Giải pháp:** Cap $0.75/ngày cho paper, hoặc giảm 4 pairs. Recommend: 6 pairs + cap $0.75.

**Config:** `ALLOWED_PAIRS` đọc từ env. Paper: 6. Live S1: 2. Live S2: 3.

---

### 8. Model cho risk assessment

**Quyết định:** Sonnet (claude-sonnet-4-6) — quality baseline.

**Lý do:** Risk assessment là lý do duy nhất gọi Claude; reasoning depth quan trọng. Nếu budget tight sau 1–2 tuần → downgrade Haiku. Bắt đầu Sonnet để có baseline so sánh.

---

## Ý tưởng cho Claude Code skill

Tạo một skill `/backtest-report` để tự động query SQLite và sinh ra performance report với các metrics đã nêu (win rate, PF, Sharpe, drawdown) — đây sẽ là workflow dùng lặp lại nhiều lần nhất trong quá trình validation.
