# 004 — Dynamic Pair Screening Plan

**Ngày:** 2025-03-07  
**Mục đích:** Thay vì scan cố định 6 cặp mỗi 15 phút (hầu hết bị rule-based filter reject), dùng Binance API để chọn động top N cặp có khả năng trade cao nhất mỗi cycle.

---

## Bối cảnh

- **Hiện tại:** `ALLOWED_PAIRS` cố định 6 cặp (BTC, ETH, BNB, SOL, XRP, ADA). Rule-based filter reject gần như tất cả → không có signal.
- **Vấn đề:** Cặp ít biến động, RSI trung tính, funding thấp → không bao giờ pass điều kiện LONG/SHORT.
- **Giải pháp:** Mỗi scan, lấy top cặp theo volume + volatility từ Binance, rồi mới chạy full analysis.

---

## Mục lục

| # | Task | File(s) | Trạng thái |
|---|------|---------|------------|
| [001](#001-binance-ticker-fetcher) | Hàm fetch ticker 24hr (all symbols) | utils/market_data.py | ⬜ |
| [002](#002-top-pairs-selector) | Hàm chọn top N pairs theo volume/volatility | utils/market_data.py | ⬜ |
| [003](#003-config-env) | Config: SCAN_MODE, TOP_PAIRS_COUNT, MIN_QUOTE_VOLUME | config.py, .env.example | ⬜ |
| [004](#004-research-agent-integration) | run_full_scan dùng dynamic pairs khi SCAN_MODE=auto | agents/research_agent.py | ⬜ |
| [005](#005-docs-update) | Cập nhật CLAUDE.md, .env.example | CLAUDE.md, .env.example | ⬜ |

---

## 001. Binance ticker fetcher

**File:** `utils/market_data.py`

### API

```
GET https://api.binance.com/api/v3/ticker/24hr
```

- Không truyền `symbol` → trả về tất cả cặp.
- Response: `[{ symbol, quoteVolume, priceChangePercent, volume, lastPrice, ... }, ...]`

### Thay đổi

1. Thêm method `get_all_tickers_24hr() -> list[dict]` trong `BinanceDataFetcher`.
2. Gọi `_http_get_with_retry` với URL `{base}/ticker/24hr` (không params).
3. Parse JSON, return list.

---

## 002. Top pairs selector

**File:** `utils/market_data.py`

### Logic

```python
def get_top_pairs_for_scan(
    tickers: list[dict],
    top_n: int = 25,
    min_quote_volume_usd: float = 5_000_000,
    sort_by: str = "volatility",  # "volume" | "volatility" | "combined"
) -> list[str]:
    """
    Lọc USDT pairs, sort theo volume/volatility, trả về top N.
    """
```

1. **Filter:** `symbol.endswith("USDT")`, `float(quoteVolume) >= min_quote_volume_usd`.
2. **Sort:**
   - `volume`: sort by quoteVolume desc.
   - `volatility`: sort by abs(priceChangePercent) desc.
   - `combined`: score = log(quoteVolume) * abs(priceChangePercent), sort desc.
3. **Return:** `[symbol, ...]` top N.

### Lý do

- **Volume:** Cặp thanh khoản cao, spread thấp, dễ execute.
- **Volatility:** Cặp đang biến động mạnh → RSI dễ cực đoan (RSI < 45 hoặc > 55) → dễ pass rule-based filter.

---

## 003. Config & env

**Files:** `config.py`, `.env.example`

### Thêm vào config

```python
# config.py
scan_mode: str = "fixed"  # "fixed" | "auto"
top_pairs_count: int = 25
min_quote_volume_usd: float = 5_000_000
scan_sort_by: str = "volatility"  # "volume" | "volatility" | "combined"
```

### .env.example

```env
# Pair screening (khi SCAN_MODE=auto)
SCAN_MODE=fixed
TOP_PAIRS_COUNT=25
MIN_QUOTE_VOLUME_USD=5000000
SCAN_SORT_BY=volatility
```

- `SCAN_MODE=fixed`: dùng `ALLOWED_PAIRS` như hiện tại.
- `SCAN_MODE=auto`: bỏ qua `ALLOWED_PAIRS`, dùng dynamic screening.

---

## 004. Research agent integration

**File:** `agents/research_agent.py`

### Thay đổi `run_full_scan()`

```python
async def run_full_scan(self) -> list[TradingSignal]:
    if cfg.scan_mode == "auto":
        tickers = await self.binance.get_all_tickers_24hr()
        pairs_to_scan = get_top_pairs_for_scan(
            tickers,
            top_n=cfg.top_pairs_count,
            min_quote_volume_usd=cfg.min_quote_volume_usd,
            sort_by=cfg.scan_sort_by,
        )
        logger.info(f"Dynamic scan: top {len(pairs_to_scan)} pairs by {cfg.scan_sort_by}")
    else:
        pairs_to_scan = ALLOWED_PAIRS

    # ... rest unchanged (asyncio.gather analyze_pair for pairs_to_scan)
```

---

## 005. Docs update

**Files:** `CLAUDE.md`, `.env.example`

- Thêm mục "Dynamic Pair Screening" trong CLAUDE.md.
- Liệt kê env vars mới trong .env.example.

---

## Ràng buộc & lưu ý

| Vấn đề | Giải pháp |
|--------|-----------|
| Binance rate limit | 1 request ticker/24hr/cycle. Weight thấp. |
| Claude budget | Top 25 pairs, nhiều nhất 25 Claude calls/cycle nếu tất cả pass filter. Thực tế ít hơn (rule-based reject trước). |
| max_open_positions | Vẫn 3. Scan nhiều pair chỉ tăng cơ hội có signal, không tăng số lệnh đồng thời. |
| Fallback | Nếu `get_all_tickers_24hr()` fail → fallback về `ALLOWED_PAIRS`. |

---

## Thứ tự triển khai

1. 001 + 002 (market_data)
2. 003 (config)
3. 004 (research_agent)
4. 005 (docs)
