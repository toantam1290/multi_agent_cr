# 005 — Opportunity Screening Implementation Checklist

**Ngày:** 2026-03-07  
**Mục đích:** Chuyển `004-dynamic-pair-screening-plan.md` thành checklist kỹ thuật có thể implement trực tiếp, bám code hiện tại.

---

## Bối cảnh code hiện tại (audit)

- `agents/research_agent.py` đang scan cố định theo `ALLOWED_PAIRS` trong `run_full_scan()`.
- `config.py` chưa có nhóm config cho `SCAN_MODE=opportunity`.
- `utils/market_data.py` chưa có:
  - `get_all_tickers_24hr()`
  - `get_futures_symbols()`
  - `get_opportunity_pairs()`
- `database.py` đã có nền tảng tốt cho đo lường (`daily_stats`, `signals`, `trades`, `agent_logs`).

---

## Ưu tiên triển khai (cập nhật)

| Ưu tiên | Hạng mục | Ghi chú |
|---|---|---|
| Must | Futures-only filter | Tránh scan cặp chắc chắn fail derivatives gate |
| Must | Config validation | Chặn cấu hình sai ngay khi start |
| Must | Observability cơ bản | Có số liệu để tuning hằng ngày |
| Should | Confluence score >= 2 | Giảm nhiễu khi thị trường loạn |
| Should | Cooldown/hysteresis | Giảm churn cặp vào/ra liên tục |
| Optional | Whitelist mode | Giới hạn universe theo user |

---

## Mục lục task

| # | Task | File(s) | Trạng thái |
|---|------|---------|------------|
| [001](#001-config--env) | Thêm config opportunity mode | `config.py`, `.env.example` | ⬜ |
| [002](#002-market-data-fetchers) | Ticker 24h + futures symbols fetcher | `utils/market_data.py` | ⬜ |
| [003](#003-opportunity-filter-core) | Hàm lọc cặp cơ hội (futures-only, core, cap) | `utils/market_data.py` | ⬜ |
| [004](#004-research-agent-integration) | Tích hợp vào `run_full_scan()` | `agents/research_agent.py` | ⬜ |
| [005](#005-validation--fallback) | Validation + fallback behavior | `config.py`, `agents/research_agent.py` | ⬜ |
| [006](#006-observability-minimum) | Metrics/log tối thiểu mỗi cycle | `agents/research_agent.py`, `database.py` | ⬜ |
| [007](#007-phase-15-quality-upgrades) | Confluence + cooldown/hysteresis | `utils/market_data.py`, `agents/research_agent.py` | ⬜ |

---

## 001. Config & env

**Files:** `config.py`, `.env.example`

### Thay đổi cần có

Thêm vào config:

```python
scan_mode: str = "fixed"  # "fixed" | "opportunity"
opportunity_volatility_pct: float = 5.0
opportunity_volatility_max_pct: float = 25.0
min_quote_volume_usd: float = 5_000_000
max_pairs_per_scan: int = 30
core_pairs: list[str] = ["BTCUSDT", "ETHUSDT"]
scan_blacklist: list[str] = ["USDCUSDT", "BUSDUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT"]
opportunity_use_whitelist: bool = False
scan_dry_run: bool = False
```

`.env.example`:

```env
SCAN_MODE=fixed
OPPORTUNITY_VOLATILITY_PCT=5.0
OPPORTUNITY_VOLATILITY_MAX_PCT=25.0
MIN_QUOTE_VOLUME_USD=5000000
MAX_PAIRS_PER_SCAN=30
CORE_PAIRS=BTCUSDT,ETHUSDT
SCAN_BLACKLIST=USDCUSDT,BUSDUSDT,FDUSDUSDT,TUSDUSDT,DAIUSDT
OPPORTUNITY_USE_WHITELIST=false
SCAN_DRY_RUN=false
```

### Acceptance criteria

- `scan_mode=fixed` giữ nguyên behavior cũ.
- `scan_mode=opportunity` không crash khi thiếu optional env.

---

## 002. Market data fetchers

**File:** `utils/market_data.py`

### Thay đổi cần có

Trong `BinanceDataFetcher`:

1. `get_all_tickers_24hr() -> list[dict]`
   - Endpoint: `GET /api/v3/ticker/24hr`
   - Parse an toàn: `float(x or 0)` cho các field số.
2. `get_futures_symbols() -> set[str]`
   - Endpoint: `GET /fapi/v1/premiumIndex` (all symbols).
   - Return set symbol.
   - Nếu fail: return `set()` và log warning (không block scan).

### Acceptance criteria

- Có thể gọi song song 2 API mà không tăng retry error bất thường.
- `get_futures_symbols()` fail không làm chết cycle scan.

---

## 003. Opportunity filter core

**File:** `utils/market_data.py`

### Thay đổi cần có

Thêm hàm:

```python
def get_opportunity_pairs(
    tickers: list[dict],
    futures_symbols: set[str] | None = None,
    min_volatility_pct: float = 5.0,
    max_volatility_pct: float = 25.0,
    min_quote_volume_usd: float = 5_000_000,
    max_pairs_per_scan: int = 30,
    core_pairs: list[str] | None = None,
    blacklist: list[str] | None = None,
    allowed_pairs: list[str] | None = None,
    use_whitelist: bool = False,
) -> list[str]:
    ...
```

Logic bắt buộc:

1. Chỉ giữ symbol `*USDT`.
2. Lọc blacklist.
3. Lọc thanh khoản theo `quoteVolume`.
4. Lọc volatility trong khoảng `[min, max]`.
5. Futures-only:
   - Nếu `futures_symbols` có dữ liệu -> intersect.
   - Nếu rỗng -> bỏ qua filter này (degrade gracefully).
6. Whitelist mode (optional):
   - Nếu bật và `allowed_pairs` có dữ liệu -> intersect.
7. Sort theo `abs(priceChangePercent)` giảm dần.
8. Cap `max_pairs_per_scan`.
9. Add `core_pairs` ở đầu, dedupe, core không tính vào cap.

### Acceptance criteria

- Không ném exception khi `priceChangePercent` là string, null, hoặc thiếu key.
- Danh sách trả về không có duplicate.
- `core_pairs` luôn hiện diện nếu có cấu hình.

---

## 004. Research agent integration

**File:** `agents/research_agent.py`

### Thay đổi cần có

Trong `run_full_scan()`:

- Nếu `cfg.scan_mode == "opportunity"`:
  - Gọi song song:
    - `self.binance.get_all_tickers_24hr()`
    - `self.binance.get_futures_symbols()`
  - Build `pairs_to_scan = get_opportunity_pairs(...)`.
  - Ghi log metrics screening.
- Else:
  - `pairs_to_scan = ALLOWED_PAIRS`.

Flow sau đó giữ nguyên:

- Nếu rỗng -> return `[]`.
- `asyncio.gather(self.analyze_pair(pair) for pair in pairs_to_scan)`.

### Acceptance criteria

- Không tăng số Claude call ngoài dự kiến.
- Scan mode có thể chuyển qua lại bằng env, không cần sửa code.

---

## 005. Validation & fallback

**Files:** `config.py`, `agents/research_agent.py`

### Validation bắt buộc

- `opportunity_volatility_pct < opportunity_volatility_max_pct`.
- `max_pairs_per_scan > 0`.
- `core_pairs` không nằm trong `scan_blacklist`.
- `scan_mode` chỉ nhận `fixed` hoặc `opportunity`.

### Fallback behavior

- Nếu ticker fetch fail và `ALLOWED_PAIRS` có dữ liệu -> fallback `ALLOWED_PAIRS`.
- Nếu cả ticker fail và `ALLOWED_PAIRS` rỗng -> log error + skip cycle.
- Nếu futures symbols fail -> vẫn scan nhưng log rằng futures-only bị disable tạm thời.

### Acceptance criteria

- Trạng thái fallback được log rõ nguyên nhân.
- Không có crash khi 1 trong 2 endpoint Binance lỗi.

---

## 006. Observability minimum

**Files:** `agents/research_agent.py`, `database.py` (nếu cần bổ sung lưu metrics)

### Metrics tối thiểu/cycle

- `opportunity_candidates`
- `pairs_scanned`
- `rule_based_passed`
- `signals_generated`
- `scan_mode`
- `fallback_used` (true/false)

### Logging

- Ghi 1 log tổng hợp/cycle vào `agent_logs` với payload JSON.
- Nếu `scan_dry_run=true`: log danh sách cặp, không gọi `analyze_pair`.

### Acceptance criteria

- Có thể tái dựng funnel theo ngày từ log/DB.
- Dry-run không tạo `signals`.

---

## 007. Phase 1.5 quality upgrades

**Files:** `utils/market_data.py`, `agents/research_agent.py`

### 7.1 Confluence score (Should)

Điểm gợi ý:

- +1: volatility pass
- +1: volume spike
- +1: funding extreme

Rule:

- Sideways/high-noise: scan score >= 2.
- Trend rõ: cho phép score >= 1.

### 7.2 Cooldown/hysteresis (Should)

- Cooldown theo symbol: đã scan thì tạm nghỉ `N` cycle.
- Hysteresis:
  - vào list khi `abs(change) >= entry_threshold`
  - ra list khi `abs(change) < exit_threshold` (thấp hơn entry)

### Acceptance criteria

- Số lần symbol re-enter liên tục giảm rõ rệt.
- Chất lượng signal không giảm khi giảm churn.

---

## Test plan tối thiểu

1. Unit tests cho `get_opportunity_pairs()`:
   - parse string/None, blacklist, futures filter, dedupe core.
2. Integration test cho `run_full_scan()`:
   - fixed mode
   - opportunity mode
   - fallback path
   - dry-run path
3. Replay 24h logs:
   - so sánh số Claude calls trước/sau.

---

## Rollout đề xuất

1. Bật `scan_dry_run=true` trong 1-2 ngày.
2. Chạy opportunity mode với position size nhỏ.
3. Sau khi funnel ổn định, bật confluence >= 2 cho market nhiễu.
4. Sau 1 tuần, chốt threshold theo regime.
