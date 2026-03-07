# 004 — Opportunity Screening Plan

**Ngày:** 2025-03-07  
**Mục đích:** Chỉ scan những cặp có **tín hiệu bất thường / cơ hội** — không fix số lượng, chất lượng hơn số lượng. Phát hiện cặp có setup trade được, xu hướng rõ, cơ hội ăn tiền.

---

## Bối cảnh

- **Hiện tại:** `ALLOWED_PAIRS` cố định 6 cặp. Rule-based filter reject gần như tất cả → không có signal.
- **Vấn đề:** Cặp ít biến động, RSI trung tính → không bao giờ pass. Scan tràn lan vô ích.
- **Mục tiêu:**
  1. Trade cặp **theo xu hướng**, setup có xác suất cao.
  2. **Không fix số lượng** — có thể 0, 3, 10 cặp tùy thị trường.
  3. **Phát hiện bất thường** — cơ hội nhảy vào khi có tín hiệu rõ.

---

## Nguyên tắc: Opportunity / Anomaly Detection

Chỉ scan cặp có **≥ 1** tín hiệu bất thường (từ ticker 24hr, không cần full analysis):

| Tín hiệu | Điều kiện | Ý nghĩa |
|----------|-----------|---------|
| **Price move mạnh** | \|priceChangePercent\| ≥ X% | Đang biến động, có thể có RSI extreme |
| **Volume spike** | quoteVolume > Y × median (hoặc top % volume) | Dòng tiền mới, interest tăng |
| **Funding extreme** | Cần API futures riêng — Phase 2 | Positioning lệch, cơ hội mean reversion |

**Output:** Số cặp **biến động** theo thị trường — 0 nếu không có cơ hội, nhiều nếu nhiều setup.

---

## Review bổ sung (2025-03-07)

| # | Ý tưởng | Áp dụng |
|---|---------|---------|
| 1 | **Funding pre-filter** — API premiumIndex all symbols | Phase 1 nếu API hỗ trợ |
| 2 | **Volume spike** — quoteVolume > 2× median (không cần lịch sử) | Optional |
| 3 | **Confluence score** — ≥2 tín hiệu trùng nhau mới scan | Optional |
| 4 | **Blacklist stablecoin** | ✅ Đã thêm |
| 5 | **Cap volatility trên** — tránh pump & dump (>25%) | ✅ Đã thêm |
| 6 | **Price + funding direction** — ưu tiên setup trùng hướng | Phase 2 |
| 7 | **Dedupe core pairs** | ✅ Đã thêm |
| 8 | **Futures-only filter** — chỉ scan cặp có futures (tránh reject chắc chắn) | ⭐ Nên thêm |
| 9 | **Whitelist mode** — ALLOWED_PAIRS làm whitelist khi opportunity | Optional |
| 10 | **Direction bias** — ưu tiên price down cho LONG, up cho SHORT | Optional |
| 11 | **Observability** — log metrics mỗi cycle | Optional |
| 12 | **priceChangePercent type safety** — handle string từ API | ✅ Nên thêm |
| 13 | **Config validation** — min < max, core ∉ blacklist | ✅ Nên thêm |
| 14 | **Dry-run mode** — log only, không chạy analysis | Optional |
| 15 | **BTC regime filter** — ưu tiên scan khi BTC biến động | Phase 2 |

---

## Mục lục

| # | Task | File(s) | Trạng thái |
|---|------|---------|------------|
| [001](#001-binance-ticker-fetcher) | Hàm fetch ticker 24hr + premiumIndex (futures symbols) | utils/market_data.py | ⬜ |
| [002](#002-opportunity-filter) | Hàm lọc cặp có tín hiệu bất thường + futures filter | utils/market_data.py | ⬜ |
| [003](#003-config-env) | Config: SCAN_MODE, opportunity thresholds | config.py, .env.example | ⬜ |
| [004](#004-research-agent-integration) | run_full_scan dùng opportunity pairs | agents/research_agent.py | ⬜ |
| [005](#005-core-pairs-fallback) | Core pairs (BTC, ETH) + fallback | agents/research_agent.py | ⬜ |
| [006](#006-docs-update) | Cập nhật CLAUDE.md, .env.example | CLAUDE.md, .env.example | ⬜ |

---

## 001. Binance ticker fetcher

**File:** `utils/market_data.py`

### API 1: Spot ticker 24hr

```
GET https://api.binance.com/api/v3/ticker/24hr
```

- Không truyền `symbol` → trả về tất cả cặp.
- Response: `[{ symbol, quoteVolume, priceChangePercent, volume, lastPrice, ... }, ...]`
- **Lưu ý:** `priceChangePercent` có thể là string → dùng `float(x or 0)`.

### API 2: Futures premiumIndex (lấy danh sách cặp có futures)

```
GET https://fapi.binance.com/fapi/v1/premiumIndex
```

- Không truyền `symbol` → trả về array tất cả symbols có futures.
- Response: `[{ symbol, markPrice, indexPrice, lastFundingRate, ... }, ...]`
- Dùng để lấy `set(s["symbol"] for s in response)` = futures_symbols.

### Thay đổi

1. Thêm `get_all_tickers_24hr() -> list[dict]` trong `BinanceDataFetcher`.
2. Thêm `get_futures_symbols() -> set[str]` — gọi premiumIndex, return set symbol.
3. Cả hai dùng `_http_get_with_retry`. premiumIndex fail → return `set()` (không filter futures).

---

## 002. Opportunity filter

**File:** `utils/market_data.py`

### Logic

```python
def get_opportunity_pairs(
    tickers: list[dict],
    futures_symbols: set[str] | None = None,  # Chỉ scan cặp có futures
    min_volatility_pct: float = 5.0,
    max_volatility_pct: float = 25.0,   # Tránh pump & dump
    min_quote_volume_usd: float = 5_000_000,
    max_pairs_per_scan: int = 30,
    core_pairs: list[str] | None = None,
    blacklist: list[str] | None = None,
) -> list[str]:
    """
    Chỉ trả về cặp có tín hiệu bất thường (opportunity).
    Số lượng biến động — không ép phải có N cặp.
    """
```

1. **Filter cơ bản:** `symbol.endswith("USDT")`, `float(quoteVolume) >= min_quote_volume_usd`.
2. **Blacklist:** Loại stablecoin (`USDCUSDT`, `BUSDUSDT`, ...).
3. **Futures filter:** Nếu `futures_symbols` không rỗng → chỉ giữ cặp trong set (cặp không có futures chắc chắn reject rule-based).
4. **Opportunity filter:** Cặp pass nếu `min_volatility_pct <= abs(float(priceChangePercent or 0)) <= max_volatility_pct`.
5. **Sort:** Theo `abs(priceChangePercent)` desc — ưu tiên cặp biến động mạnh nhất.
6. **Cap:** Lấy tối đa `max_pairs_per_scan` (tránh quá tải API/Claude).
7. **Core pairs:** Luôn thêm `core_pairs` (BTC, ETH) vào đầu danh sách nếu chưa có, dedupe, không tính vào cap. Core pairs **bypass** futures filter (BTC/ETH luôn có futures).

### Không fix số lượng

- Nếu 0 cặp pass → return `[]` (hoặc chỉ core_pairs).
- Nếu 5 cặp pass → return 5.
- `max_pairs_per_scan` là **giới hạn trên**, không phải target.

---

## 003. Config & env

**Files:** `config.py`, `.env.example`

### Thêm vào config

```python
# config.py (TradingConfig hoặc ScanConfig)
scan_mode: str = "fixed"       # "fixed" | "opportunity"
opportunity_volatility_pct: float = 5.0   # min |priceChange%|
opportunity_volatility_max_pct: float = 25.0   # max — tránh pump & dump
min_quote_volume_usd: float = 5_000_000
max_pairs_per_scan: int = 30   # Cap, không phải target
core_pairs: list[str] = ["BTCUSDT", "ETHUSDT"]  # Luôn scan
scan_blacklist: list[str] = ["USDCUSDT", "BUSDUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT"]
```

### .env.example

```env
# Opportunity screening (khi SCAN_MODE=opportunity)
SCAN_MODE=fixed
OPPORTUNITY_VOLATILITY_PCT=5.0
OPPORTUNITY_VOLATILITY_MAX_PCT=25.0
MIN_QUOTE_VOLUME_USD=5000000
MAX_PAIRS_PER_SCAN=30
CORE_PAIRS=BTCUSDT,ETHUSDT
SCAN_BLACKLIST=USDCUSDT,BUSDUSDT,FDUSDUSDT,TUSDUSDT,DAIUSDT
```

- `SCAN_MODE=fixed`: dùng `ALLOWED_PAIRS` như hiện tại.
- `SCAN_MODE=opportunity`: dùng opportunity filter, luôn thêm core pairs.

---

## 004. Research agent integration

**File:** `agents/research_agent.py`

### Thay đổi `run_full_scan()`

```python
async def run_full_scan(self) -> list[TradingSignal]:
    if cfg.scan_mode == "opportunity":
        tickers, futures_symbols = await asyncio.gather(
            self.binance.get_all_tickers_24hr(),
            self.binance.get_futures_symbols(),
        )
        pairs_to_scan = get_opportunity_pairs(
            tickers,
            futures_symbols=futures_symbols or None,
            min_volatility_pct=cfg.opportunity_volatility_pct,
            max_volatility_pct=cfg.opportunity_volatility_max_pct,
            min_quote_volume_usd=cfg.min_quote_volume_usd,
            max_pairs_per_scan=cfg.max_pairs_per_scan,
            core_pairs=cfg.core_pairs,
            blacklist=cfg.scan_blacklist,
        )
        logger.info(f"Opportunity scan: {len(pairs_to_scan)} pairs (volatility {cfg.opportunity_volatility_pct}–{cfg.opportunity_volatility_max_pct}%, futures_only)")
    else:
        pairs_to_scan = ALLOWED_PAIRS

    if not pairs_to_scan:
        logger.info("No pairs to scan")
        return []

    # ... rest unchanged (asyncio.gather analyze_pair for pairs_to_scan)
```

---

## 005. Core pairs & fallback

**File:** `agents/research_agent.py`, `config.py`

- **Core pairs:** BTC, ETH luôn được scan (cặp chính, thanh khoản cao), bypass futures filter.
- **Fallback:** Nếu `get_all_tickers_24hr()` hoặc `get_futures_symbols()` fail → dùng `ALLOWED_PAIRS`, log warning.
- **Empty:** Nếu opportunity filter trả về 0 cặp → vẫn có core_pairs (trừ khi core_pairs = []).
- **Validation:** Khi opportunity mode, nếu fallback và `ALLOWED_PAIRS` rỗng → skip scan, log error.

---

## 006. Docs update

**Files:** `CLAUDE.md`, `.env.example`

- Thêm mục "Opportunity Screening" trong CLAUDE.md.
- Liệt kê env vars mới trong .env.example.

---

## Ràng buộc & lưu ý

| Vấn đề | Giải pháp |
|--------|-----------|
| Binance rate limit | 1 request ticker/24hr/cycle. Weight thấp. |
| Claude budget | Số cặp biến động, max 30. Rule-based reject trước Claude. |
| 0 opportunity | Chỉ scan core pairs (BTC, ETH). Không ép có signal. |
| Futures | Một số alt không có futures → `get_derivatives_signal` có thể fail. Handle gracefully. |

---

## Ý tưởng bổ sung (review)

### 1. Funding pre-filter — Phase 1 luôn

- **API:** `GET /fapi/v1/premiumIndex` (không truyền symbol → trả về tất cả, cần verify).
- **Logic:** Cặp có `|funding_rate| > 0.1%` = positioning lệch → cơ hội mean reversion.
- **Lợi ích:** Trùng với rule-based (SHORT cần funding > 0.05%) → tăng xác suất pass.

### 2. Volume spike — không cần lịch sử

- **Từ ticker 24hr:** Có `quoteVolume` cho mọi cặp.
- **Logic:** `median_volume = median(quoteVolume của tất cả USDT)`. Cặp có `quoteVolume > 2 × median` = volume spike.
- **Ý nghĩa:** Dòng tiền mới, interest tăng → có thể có breakout.

### 3. Confluence score — ưu tiên chất lượng

- Thay vì OR (≥1 điều kiện), dùng **điểm**:
  - +1: volatility ≥ X%
  - +1: volume spike
  - +1: funding extreme
- Chỉ scan cặp có **score ≥ 2** (nhiều tín hiệu trùng nhau = setup mạnh hơn).

### 4. Blacklist stablecoin

- Loại: `USDCUSDT`, `BUSDUSDT`, `FDUSDUSDT`, `TUSDUSDT`, `DAIUSDT`.
- Tránh nhầm stablecoin với cơ hội thật.

### 5. Cap volatility trên — tránh pump & dump

- `|priceChangePercent| > 25%` → có thể scam / illiquid.
- Chỉ lấy cặp có `min_volatility_pct ≤ |priceChange%| ≤ max_volatility_pct` (ví dụ 5–25%).

### 6. Hướng price + funding

- **LONG setup:** price giảm (priceChangePercent < 0) + funding âm → oversold, longs bị squeeze.
- **SHORT setup:** price tăng (priceChangePercent > 0) + funding dương → overbought, shorts bị squeeze.
- Có thể ưu tiên cặp có **price direction + funding direction** cùng chiều với rule-based.

### 7. Dedupe core pairs

- Nếu `BTCUSDT` vừa trong core_pairs vừa trong opportunity list → chỉ xuất hiện 1 lần.

---

## Deep review — ý tưởng bổ sung

### 8. Futures-only filter ⭐ (quan trọng)

- **Vấn đề:** `get_derivatives_signal` fail hoặc trả `funding_rate=0.0005` cho cặp không có futures. Với 0.05%, LONG cần `< 0.05%` và SHORT cần `> 0.05%` → cặp không có futures **không bao giờ** pass rule-based.
- **Giải pháp:** Chỉ scan cặp **có futures**. Gọi `GET /fapi/v1/premiumIndex` (không symbol) → trả về array tất cả symbols có futures. Intersect: `opportunity_pairs ∩ futures_symbols`.
- **Lợi ích:** Không lãng phí API/Claude cho cặp chắc chắn reject.

### 9. Whitelist mode

- **ALLOWED_PAIRS** khi `SCAN_MODE=opportunity` có thể đóng vai trò **whitelist**: chỉ xét cặp nằm trong danh sách.
- **Logic:** Nếu `OPPORTUNITY_USE_WHITELIST=true` và `ALLOWED_PAIRS` không rỗng → `opportunity_pairs = opportunity_pairs ∩ ALLOWED_PAIRS`.
- **Lợi ích:** User giới hạn "chỉ trade 20 cặp tôi tin" thay vì toàn bộ market.

### 10. Direction bias — ưu tiên hướng

- **LONG:** Ưu tiên cặp có `priceChangePercent < 0` (giá giảm → dễ oversold).
- **SHORT:** Ưu tiên cặp có `priceChangePercent > 0` (giá tăng → dễ overbought).
- **Cách làm:** Sort riêng 2 nhóm. Hoặc dùng `(priceChangePercent < 0 ? 1 : 0)` làm tie-breaker khi sort — ưu tiên cặp có hướng rõ.

### 11. Observability / metrics

- Log mỗi cycle: `opportunity_candidates`, `pairs_scanned`, `rule_based_passed`, `claude_passed`, `signals_generated`.
- Lưu vào `agent_logs` hoặc bảng `scan_metrics`.
- **Lợi ích:** Đo hiệu quả screening, tinh chỉnh threshold.

### 12. priceChangePercent type safety

- Binance có thể trả `priceChangePercent` dạng string. Dùng `float(t.get("priceChangePercent") or 0)` để tránh lỗi.

### 13. Config validation

- `min_volatility_pct < max_volatility_pct`.
- `core_pairs` không nằm trong `scan_blacklist`.
- Khi `SCAN_MODE=opportunity`: nếu `get_all_tickers_24hr` fail và `ALLOWED_PAIRS` rỗng → log warning, skip scan.

### 14. Dry-run mode

- `SCAN_DRY_RUN=true`: Chỉ log danh sách cặp sẽ scan, không chạy full analysis.
- Dùng để kiểm tra threshold trước khi bật thật.

### 15. BTC regime filter (Phase 2)

- Khi BTC `|priceChange%| < 2%` (sideways) → alt có thể range. Khi BTC `|priceChange%| > 5%` → trend rõ.
- Có thể ưu tiên scan khi BTC biến động (market có direction).

---

## Phase 2 (mở rộng sau)

- **Funding bulk API:** Nếu premiumIndex không hỗ trợ all symbols → gọi từng cặp (tốn request) hoặc bỏ qua.
- **Session filter:** Ưu tiên cặp active theo session (Asian/US/EU) — phức tạp hơn.
- **1h price change:** Ticker 24h có thể "cũ". Dùng klines 1h để tính priceChange 1h — tốn thêm request.

---

## Thứ tự triển khai

1. 001 (market_data: get_all_tickers_24hr)
2. 002 (market_data: get_opportunity_pairs) + blacklist + volatility cap + **futures filter**
3. 003 (config) + validation
4. 004 + 005 (research_agent)
5. 006 (docs)
6. (Optional) Funding pre-filter, volume spike, confluence score, whitelist mode, metrics

---

## Tóm tắt deep review

| Ưu tiên | Ý tưởng | Lý do |
|---------|---------|-------|
| ⭐ Must | **Futures-only filter** | Cặp không có futures không bao giờ pass rule-based (funding=0.05% neutral) |
| ⭐ Must | **priceChangePercent type safety** | Binance trả string, cần float() |
| Nên | **Config validation** | Tránh min > max, core trong blacklist |
| Optional | Whitelist mode | User giới hạn cặp được phép |
| Optional | Direction bias | Ưu tiên price down cho LONG, up cho SHORT |
| Optional | Observability | Đo hiệu quả screening |
| Optional | Dry-run | Tune threshold trước khi bật |

---

## Addendum sau audit code (2026-03-07)

### Cập nhật ưu tiên

| Trạng thái cũ | Cập nhật mới | Lý do |
|---|---|---|
| Optional | **Must: Observability cơ bản** | Không có funnel metrics thì không thể tuning |
| Nên | **Must: Config validation** | Tránh crash/reject âm thầm khi bật opportunity mode |
| Optional | **Should: Confluence >= 2 (Phase 1.5)** | Giảm nhiễu khi market loạn |
| Chưa nêu rõ | **Should: Cooldown/hysteresis** | Giảm churn cặp vào/ra liên tục |

### Ghi chú triển khai

- `run_full_scan()` hiện tại vẫn dùng `ALLOWED_PAIRS` cố định.
- Các hàm `get_all_tickers_24hr()`, `get_futures_symbols()`, `get_opportunity_pairs()` chưa có trong code.
- Vì vậy cần đi theo checklist implement riêng tại:
  - `README-opportunity-screening.md`
  - `005-opportunity-screening-implementation-checklist.md`
  - `006-daily-metrics-score-spec.md`
  - `007-opportunity-screening-pr-rollout-plan.md`
  - `008-opportunity-screening-task-board.md`
  - `009-opportunity-screening-operations-runbook.md`
  - `010-opportunity-screening-single-command-execution-guide.md`
