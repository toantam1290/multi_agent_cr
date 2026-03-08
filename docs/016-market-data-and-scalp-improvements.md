# Market Data & Scalp Win Rate — Các cải tiến đã triển khai

Tài liệu mô tả lý do và chi tiết code cho các vấn đề đã sửa trong project, gồm: rate limit, market data accuracy, opportunity screening, và scalp win rate.

---

## Phần 1: Rate limit & API tối ưu

### 1.1 Semaphore cho analyze_pair

**Vấn đề:** 30 pairs × ~10 requests/pair = ~300 requests burst trong vài milliseconds → Binance throttle → ConnectTimeout, retry 3 lần × 30 pairs.

**Giải pháp:** Thêm `_pair_semaphore = asyncio.Semaphore(5)` — tối đa 5 pairs phân tích đồng thời.

**Code:**

```python
# research_agent.py — __init__
self._pair_semaphore = asyncio.Semaphore(5)  # Tối đa 5 pairs đồng thời

# analyze_pair — wrapper
async def analyze_pair(self, pair: str, prefetched_sentiment=None):
    async with self._pair_semaphore:
        return await self._analyze_pair_inner(pair, prefetched_sentiment)
```

**Kết quả:** ~50 concurrent max thay vì 300 burst.

---

### 1.2 Fear & Greed prefetch

**Vấn đề:** Fear & Greed là daily index — giá trị giống nhau cho mọi pair trong cùng cycle. Fetch 30 lần = lãng phí.

**Giải pháp:** Fetch 1 lần trước `asyncio.gather`, truyền `prefetched_sentiment` vào mỗi `analyze_pair`.

**Code:**

```python
# run_full_scan()
try:
    shared_sentiment = await self.fear_greed.get()
    logger.info(f"Fear & Greed: {shared_sentiment.fear_greed_index} ({shared_sentiment.fear_greed_label})")
except Exception as e:
    logger.warning(f"Fear & Greed pre-fetch failed: {e}, fallback fetch riêng từng pair")
    shared_sentiment = None

results = await asyncio.gather(
    *[self.analyze_pair(pair, prefetched_sentiment=shared_sentiment) for pair in pairs_to_scan],
    return_exceptions=True,
)
```

---

### 1.3 Bỏ get_current_price() thừa (swing)

**Vấn đề:** Swing dùng `get_current_price()` riêng trong khi giá đã có trong klines `df_fast["close"].iloc[-1]`. Thừa ~15 requests/cycle (15 swing pairs).

**Giải pháp:** Thêm `current_price` vào `TechnicalSignal`, set trong `compute_technical_signal`. Swing dùng `technical.current_price`, scalp vẫn gọi `get_current_price` (cần real-time).

**Code:**

```python
# models.py — TechnicalSignal
current_price: float = 0.0   # Close của nến cuối

# market_data.py — compute_technical_signal
current_price = float(df_fast["close"].iloc[-1])
return TechnicalSignal(..., current_price=current_price)

# research_agent.py — _analyze_pair_inner
if style == "scalp":
    current_price, technical, ... = await asyncio.gather(get_current_price(...), ...)
else:
    technical, ... = await asyncio.gather(compute_technical_signal(...), ...)
    current_price = technical.current_price
```

---

## Phần 2: Market data accuracy

### 2.1 EMA200 warm-up thiếu

**Vấn đề:** EMA(200) cần ~200–400 candle để hội tụ. Với 210 candles 1D, chỉ 10 candle đệm → EMA200 sai trong 150–180 candle đầu → `trend_1d` classify sai → rule-based filter pass/reject nhầm.

**Giải pháp:** Tăng limit 1D từ 210 lên 400.

**Code:**

```python
# market_data.py — compute_technical_signal (swing mode)
df_fast, df_slow, df_trend = await asyncio.gather(
    self.get_klines(symbol, "1h", 100),
    self.get_klines(symbol, "4h", 100),
    self.get_klines(symbol, "1d", 400),  # EMA200 cần ~400 candle warm-up
)
```

---

### 2.2 Opportunity pairs — tách long/short thay vì sort theo abs

**Vấn đề:** `sort(key=lambda x: abs(priceChangePercent))` ưu tiên pair biến động nhất (momentum chasing). Pair tăng 20% có thể đã pump xong, pair giảm 15% có thể đã dump xong — vào muộn.

**Giải pháp:** Tách LONG/SHORT candidates, filter "chưa ở đỉnh/đáy 24h", sort riêng từng nhóm, lấy đều mỗi bên.

**Code:**

```python
# market_data.py — get_opportunity_pairs
# Thêm lastPrice, highPrice, lowPrice vào candidate
candidates.append({
    "symbol": symbol, "priceChangePercent": pct, "quoteVolume": qv,
    "lastPrice": last_price, "highPrice": high_price, "lowPrice": low_price,
})

# Tách LONG / SHORT, filter chưa ở đỉnh/đáy
long_candidates = []
short_candidates = []
for c in candidates:
    pct, last, high, low = c["priceChangePercent"], c.get("lastPrice", 0), c.get("highPrice", 0), c.get("lowPrice", 0)
    if pct >= 3.0 and high > 0 and last < high * 0.95:
        long_candidates.append(c)
    elif pct <= -3.0 and low > 0 and last > low * 1.05:
        short_candidates.append(c)

long_candidates.sort(key=lambda x: x["priceChangePercent"], reverse=True)
short_candidates.sort(key=lambda x: x["priceChangePercent"])
half = max_pairs_per_scan // 2
long_picked = long_candidates[:half]
short_picked = short_candidates[:half]
shortage = half - len(long_picked)
if shortage > 0:
    short_picked = short_candidates[: half + shortage]
shortage = half - len(short_picked)
if shortage > 0:
    long_picked = long_candidates[: half + shortage]
capped = [c["symbol"] for c in long_picked] + [c["symbol"] for c in short_picked]
```

---

### 2.3 max_pairs_per_scan auto theo trading_style

**Vấn đề:** Scalp scan 30 pairs × 4 TF = nhiều requests, quality thấp hơn. Nên ít pair hơn nhưng chất lượng cao.

**Giải pháp:** Khi `MAX_PAIRS_PER_SCAN=0` → auto: scalp=15, swing=30.

**Code:**

```python
# config.py — ScanConfig.__post_init__
if self.max_pairs_per_scan <= 0:
    self.max_pairs_per_scan = 15 if self.trading_style == "scalp" else 30
```

---

## Phần 3: Scalp win rate

### 3.1 Confluence check trước Claude

**Vấn đề:** Gọi Claude cho mọi setup pass rule-based filter → lãng phí budget, nhiều setup thiếu confluence thực sự.

**Giải pháp:** Đếm 5 yếu tố align (trend, volume, funding, whale flow, OI change). Chỉ gọi Claude khi ≥ 3/5.

**Code:**

```python
# research_agent.py — _analyze_pair_inner
confluence_score = 0
if direction == "LONG" and technical.trend_1d == "uptrend": confluence_score += 1
if direction == "SHORT" and technical.trend_1d == "downtrend": confluence_score += 1
if technical.volume_spike or technical.volume_trend_up: confluence_score += 1
if direction == "LONG" and derivatives.funding_rate < 0.0002: confluence_score += 1
if direction == "SHORT" and derivatives.funding_rate > 0.0002: confluence_score += 1
if direction == "LONG" and whale_data.net_flow > 0: confluence_score += 1
if direction == "SHORT" and whale_data.net_flow < 0: confluence_score += 1
if derivatives.oi_change_pct > 5: confluence_score += 1

if confluence_score < 3:
    logger.info(f"{pair}: Confluence {confluence_score}/6 < 3, skip Claude")
    return None, meta
```

Max score = 6. `RELAX_FILTER=true` → bỏ qua confluence.

---

### 3.2 SL từ swing structure thay vì ATR flat

**Vấn đề:** ATR mult 1.0 cho scalp quá tight. Noise 5m của BTC ~$100–150, SL cách entry $150–200 → dễ bị sweep rồi giá mới chạy đúng hướng.

**Giải pháp:** SL đặt dưới swing low (LONG) hoặc trên swing high (SHORT) của 10 nến 5m gần nhất. Nếu structure quá xa (> 2×ATR) → reject setup.

**Code:**

```python
# models.py — TechnicalSignal
swing_low: float = 0.0   # Min low của 10 nến 5m
swing_high: float = 0.0   # Max high của 10 nến 5m

# market_data.py — compute_technical_signal
recent_atr = df_atr.iloc[-10:] if len(df_atr) >= 10 else df_atr
swing_low = float(recent_atr["low"].min())
swing_high = float(recent_atr["high"].max())

# market_data.py — calc_entry_sl_tp (scalp)
if direction == "LONG" and swing_low > 0:
    sl = swing_low - 0.1 * atr_value
    if entry - sl > 2.0 * atr_value:
        return None  # Setup không hợp lệ
else:
    sl = entry - mult * atr_value  # fallback ATR
```

---

### 3.3 Entry timing — EMA9 cross (nới: 3 nến gần nhất)

**Vấn đề:** "Just crossed" quá strict — EMA9 cross xảy ra hiếm (~20–50 nến/lần). Với scan 5 phút, cơ hội cross đúng lúc rất nhỏ → 0 signal/ngày.

**Giải pháp:** Nới sang "cross trong 3 nến gần nhất" — nhiều signal hơn, vẫn giữ ý nghĩa timing.

**Code:**

```python
# market_data.py — compute_technical_signal
ema9_crossed_recent_up = False
ema9_crossed_recent_down = False
for i in range(1, 4):  # check candles -1, -2, -3
    c, c_prev = close.iloc[-i], close.iloc[-i-1]
    e, e_prev = ema9.iloc[-i], ema9.iloc[-i-1]
    if c > e and c_prev <= e_prev: ema9_crossed_recent_up = True
    if c < e and c_prev >= e_prev: ema9_crossed_recent_down = True

# research_agent.py — dùng ema9_crossed_recent_* thay vì ema9_just_crossed_*
```

`RELAX_FILTER=true` → bỏ qua.

---

### 3.4 BTC volatility filter

**Vấn đề:** Khi scalp altcoin mà BTC đang volatile mạnh → altcoin bị kéo theo BTC, tín hiệu riêng không đáng tin.

**Giải pháp:** Fetch BTC technical trước scan. Nếu `atr_pct > 0.5%` (5m) → chỉ scan BTC/ETH. Threshold chưa validated — log thực tế vài ngày để điều chỉnh.

**Code:**

```python
# research_agent.py — run_full_scan
if sc.trading_style == "scalp" and len(pairs_to_scan) > 0:
    try:
        btc_tech = await self.binance.compute_technical_signal("BTCUSDT", style="scalp")
        if btc_tech.atr_pct > 0.3:
            core = set(sc.core_pairs or ["BTCUSDT", "ETHUSDT"])
            pairs_to_scan = [p for p in pairs_to_scan if p in core]
    except Exception as e:
        logger.warning(f"BTC volatility filter failed: {e}")
```

---

### 3.5 Trail stop trong position monitor

**Vấn đề:** Exit cứng ở TP. Khi trade đang có lời rồi pullback → mất profit.

**Giải pháp:** Khi PnL ≥ 50% target → move SL lên breakeven. Khi PnL ≥ 80% target → lock 50% profit.

**Code:**

```python
# models.py — Trade
sl_trailing_state: str = "original"  # original | breakeven | locked_50

# database.py — migration + update_trade_sl
ALTER TABLE trades ADD COLUMN sl_trailing_state TEXT DEFAULT 'original';

def update_trade_sl(self, trade_id: str, new_sl: float, sl_trailing_state: str):
    self.conn.execute(
        "UPDATE trades SET stop_loss=?, sl_trailing_state=? WHERE id=? AND status='OPEN'",
        (new_sl, sl_trailing_state, trade_id),
    )

# main.py — _monitor_positions (scalp only)
# Chỉ update khi new_sl tốt hơn current_sl (trail lên LONG, trail xuống SHORT)
current_sl = t["stop_loss"]
if unrealized_pnl_pct >= target_pct * 0.8 and sl_state != "locked_50":
    new_sl = entry + (current_price - entry) * 0.5  # lock 50%
    if (direction == Direction.LONG and new_sl > current_sl) or (direction == Direction.SHORT and new_sl < current_sl):
        self.db.update_trade_sl(t["id"], new_sl, "locked_50")
elif unrealized_pnl_pct >= target_pct * 0.5 and sl_state == "original":
    new_sl = entry * 1.001  # breakeven
    if (direction == Direction.LONG and new_sl > current_sl) or (direction == Direction.SHORT and new_sl < current_sl):
        self.db.update_trade_sl(t["id"], new_sl, "breakeven")
```

---

### 3.6 Spread check từ orderbook

**Vấn đề:** Spread rộng = slippage cao. Fee 0.1% + spread 0.05% = quá cao cho scalp.

**Giải pháp:** Lấy best bid/ask từ `/depth?limit=5`. Nếu spread > 0.05% → skip pair.

**Code:** Spread được fetch song song trong `asyncio.gather` cùng technical/whale/derivatives (tiết kiệm ~200–300ms/pair).

```python
# research_agent.py — gather cho scalp
current_price, technical, whale_data, derivatives, spread_pct = await asyncio.gather(
    self.binance.get_current_price(pair),
    self.binance.compute_technical_signal(pair, style=style),
    self.whale.get_whale_transactions(pair, hours_back=whale_hours),
    self.binance.get_derivatives_signal(pair),
    self.binance.get_orderbook_spread_pct(pair),
)

# market_data.py — BinanceDataFetcher
async def get_orderbook_spread_pct(self, symbol: str) -> float:
    resp = await _http_get_with_retry(self._client, f"{self.base}/depth", params={"symbol": symbol, "limit": 5})
    book = resp.json()
    best_bid = float(book["bids"][0][0])
    best_ask = float(book["asks"][0][0])
    return (best_ask - best_bid) / best_bid * 100

# research_agent.py — _analyze_pair_inner (scalp, spread_pct đã có từ gather)
if spread_pct > 0.05:
    logger.info(f"{pair}: Spread {spread_pct:.3f}% > 0.05%, skip")
    return None, meta
```

`RELAX_FILTER=true` → bỏ qua.

---

## Claude prompt — context mới

Claude pre-mortem cần đủ context để assessment chính xác. Đã thêm vào prompt:

```python
# _claude_analyze — user_prompt
- Confluence: {confluence_score}/6
- EMA9 just crossed: {ema9_timing}  # yes/no
- Spread: {spread_pct:.3f}%
- OI change 24h: {derivatives.oi_change_pct:+.1f}%
- SL distance: {sl_atr_mult:.1f}×ATR (swing structure based)
```

---

## Observability & debug

### Funnel metrics (ema9_rejected, confluence_rejected)

Để theo dõi EMA9 + confluence có quá strict hay không:

```python
# run_full_scan — funnel log
funnel["ema9_rejected"] = ema9_rejected
funnel["confluence_rejected"] = confluence_rejected
```

Nếu sau 1–2 ngày paper mà `ema9_rejected` chiếm > 70% rule_passed → cân nhắc nới (vd. chấp nhận "close trên EMA9 trong 2 nến gần nhất").

### SL structure reject log

Khi `calc_entry_sl_tp` reject vì structure quá xa:

```python
logger.info(f"SL structure quá xa ({entry - sl:.2f} > 2×ATR {2 * atr_value:.2f}), reject")
```

---

## Paper trading — metrics cần theo dõi

Tất cả thay đổi trên đều là hypothesis. Cần chạy paper ít nhất 1–2 tuần và theo dõi:

| Metric | Mục tiêu | Hành động nếu lệch |
|--------|----------|--------------------|
| Signal/ngày | ≥ 1 | < 1 → filter quá strict, nới EMA9 (2 nến thay vì 1) hoặc confluence |
| Win rate | ≥ 55% (scalp) | < 50% → xem lại confluence threshold |
| Avg RR thực tế | ~1:1.5 (scalp) | So sánh vs kỳ vọng |
| ema9_rejected % funnel | Theo dõi | > 70% rule_passed → nới EMA9 |
| confluence_rejected % funnel | Theo dõi | Điều chỉnh MIN_CONFLUENCE |
| SL structure reject % | Theo dõi | Nhiều → cân nhắc tăng 2.0×ATR lên 2.5 |
| Trail stop triggered | Theo dõi | Có lock profit thực tế không |

**Không có số thực thì không biết nên nới hay chặt thêm.**

---

## Order flow — CVD, VWAP, Session (production scalping)

### CVD (Cumulative Volume Delta)

Phân biệt buy pressure vs sell pressure từ aggTrades. `isBuyerMaker=False` → market BUY → CVD tăng.

```python
# market_data.py — get_cvd_signal()
buy_vol = sum(float(t["q"]) for t in trades if not t["m"])
sell_vol = sum(float(t["q"]) for t in trades if t["m"])
cvd_ratio = buy_vol / total_vol  # >0.55 bullish, <0.45 bearish
cvd_trend = "accelerating_buy" | "accelerating_sell" | "neutral"  # late vs early half
```

**CVD divergence:** LONG khi cvd_ratio < 0.45 → reject. SHORT khi cvd_ratio > 0.55 → reject.

### Orderbook imbalance

`imbalance = bid_stack_5 / ask_stack_5`. >1.5 bullish, <0.7 bearish. Tính trong `get_orderbook_data()` cùng spread.

### VWAP

VWAP = (HLC/3 × volume).sum() / volume.sum(). `vwap_distance_pct` = % distance từ giá hiện tại. LONG khi giá > 1.5% trên VWAP → overextended, skip. SHORT tương tự khi < -1.5%.

### Session filter

| Session | UTC | Hành động |
|---------|-----|-----------|
| dead_zone | 20–24 UTC | Skip scalp cycle (sau US close) |
| asia | 0–8 | Chỉ BTC/ETH |
| london | 8–13 | Scan bình thường |
| ny_overlap | 13–20 | Scan bình thường |

`SCALP_SESSION_FILTER=false` để tắt.

---

## Tóm tắt theo file

| File | Thay đổi |
|------|----------|
| `research_agent.py` | Semaphore, Fear&Greed prefetch, current_price logic, confluence, entry timing, spread, BTC filter, CVD, VWAP, session |
| `market_data.py` | EMA200 400, opportunity long/short split, swing structure SL, EMA9 cross, get_orderbook_data, get_cvd_signal, VWAP |
| `models.py` | current_price, swing_low/high, ema9_just_crossed_*, sl_trailing_state |
| `config.py` | max_pairs_per_scan auto |
| `database.py` | sl_trailing_state migration, update_trade_sl |
| `main.py` | Trail stop trong _monitor_positions |

---

## Review feedback đã áp dụng (2025-03)

| Issue | Fix |
|-------|-----|
| EMA9 "just crossed" quá strict → 0 signal/ngày | Nới sang `ema9_crossed_recent` (cross trong 3 nến đã đóng) |
| BTC atr_pct 0.3% chưa validated | Đổi 0.5% (conservative) |
| Session dead_zone 17 UTC sai (giữa NY) | Đổi 20–24 UTC (sau US close) |
| SL swing structure SHORT | Code đã có `swing_high + 0.1*ATR` cho SHORT |
| Trail stop breakeven | Đã check direction: LONG `entry*1.001`, SHORT `entry*0.999` |
| **relax dùng trước khi define** | Move `relax = getattr(...)` lên đầu try block |
| scalp_session_filter | Đã có trong ScanConfig, dùng `sc.scalp_session_filter` |
| EMA9 dùng nến chưa đóng | Đổi sang iloc[-2,-3,-4] (3 nến đã đóng) |
| Confluence 3/9 thấp | Giữ 3, cân nhắc nâng 4 khi có data |

**VWAP sign:** `(price - vwap)/vwap*100` — dương = trên VWAP, âm = dưới. Đã verify đúng.

**Đề xuất:** Chạy 1–2 ngày với `RELAX_FILTER=true` để đo funnel metrics trước khi bật full filter.

---

## Deep review 2 — SL/EMA9/OI/RSI/Spread (2025-03)

| Issue | Fix |
|-------|-----|
| **SL trên entry (LONG)** | `calc_entry_sl_tp`: nếu `sl >= entry` → return None (swing_low quá cao) |
| **SL dưới entry (SHORT)** | Nếu `sl <= entry` → return None (swing_high quá thấp) |
| **Claude prompt ema9 sai** | Đổi `ema9_just_crossed_*` → `ema9_crossed_recent_*` (khớp gate 3 nến) |
| **OI confluence direction-neutral** | Chỉ cộng khi OI > 5% **và** trend aligned (LONG+uptrend, SHORT+downtrend) |
| **RSI momentum live candle** | Đổi `iloc[-1,-2,-3]` → `iloc[-2,-3,-4]` (3 nến đã đóng) |
| **Spread check thứ tự** | Chuyển lên bước 1b (sau gather, trước rule-based filter) |

**CVD limit=500:** Ghi nhận — time window khác nhau giữa pairs (BTC ~1–2s vs alt ~30min). Future: time-based window.

---

## Deep review 3 — CVD/OB/Trail (2025-03)

| Issue | Fix |
|-------|-----|
| **CVD trend early_cvd * 1.2 sai khi âm** | Đổi sang delta-based: `late_cvd - early_cvd > total_vol*0.05` → accelerating_buy |
| **OBD limit=10 thừa** | Đổi limit=5 (chỉ dùng 5 bids/asks) |
| **Trail stop inner sl_state != "locked_50" redundant** | Xóa inner check (outer guard đã guard) |
| **Swing VWAP 4-day spurious confluence** | Ghi nhận — xem lại khi swing improvement |

---

## Production scalping improvements (2025-03)

| Feature | File | Mô tả |
|---------|------|-------|
| **Correlation filter** | risk_manager.py | Không quá 2 vị thế cùng hướng (LONG/SHORT) |
| **Time-based exit** | main.py | Scalp: force close sau 45 phút (opened_at) |
| **Chop Index** | market_data.py, models.py | < 38.2 trending, > 61.8 skip (scalp) |
| **News blackout** | research_agent.py | High-impact event trong 30 phút → skip cycle |
| **get_recent_performance** | database.py | Rolling win_rate, avg_rr trên 20 trades |
| **Dynamic confluence** | research_agent.py | Win rate < 45% → MIN_CONFLUENCE=4 |

**Backtest engine** (`backtest.py`) đã tích hợp tất cả filters trên:
- Chop Index: `--no-chop` để tắt
- Correlation: `--no-correlation` (multi-symbol dùng `run_backtest_combined`)
- Dynamic confluence: `--no-dynamic-confluence` để tắt
- News blackout: không có historical calendar → bỏ qua trong backtest

---

## RELAX_FILTER

Khi `RELAX_FILTER=true` (test pipeline), các filter sau được bỏ qua:
- Confluence check
- Entry timing EMA9
- Spread check

Các thay đổi khác (semaphore, SL structure, trail stop, BTC filter, opportunity split) vẫn áp dụng.
