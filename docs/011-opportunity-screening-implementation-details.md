# 011 — Opportunity Screening Implementation Details

**Ngày:** 2026-03-07  
**Mục đích:** Mô tả chi tiết các đoạn code đã implement cho Opportunity Screening, giúp dev đọc hiểu và maintain.

---

## Tổng quan kiến trúc

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         run_full_scan()                                  │
├─────────────────────────────────────────────────────────────────────────┤
│  SCAN_MODE=fixed          │  SCAN_MODE=opportunity                       │
│  → ALLOWED_PAIRS          │  → get_all_tickers_24hr()                   │
│                           │  → get_premium_index_full()                  │
│                           │  → get_opportunity_pairs()                   │
│                           │  → (fallback: ALLOWED_PAIRS nếu API lỗi)     │
├─────────────────────────────────────────────────────────────────────────┤
│  SCAN_DRY_RUN=true → log only, không analyze_pair                        │
│  SCAN_DRY_RUN=false → asyncio.gather(analyze_pair(pair) for pair...)     │
├─────────────────────────────────────────────────────────────────────────┤
│  analyze_pair() → (TradingSignal|None, {rule_passed, claude_proceed})     │
│  → Log funnel metrics vào agent_logs                                     │
│  → Update scan_state (cooldown/hysteresis) khi opportunity mode         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Config (`config.py`)

### 1.1 `_parse_list_env()`

```python
def _parse_list_env(key: str, default: str) -> list[str]:
    raw = os.getenv(key, default)
    return [p.strip() for p in raw.split(",") if p.strip()]
```

- **Mục đích:** Parse env dạng `CORE_PAIRS=BTCUSDT,ETHUSDT` thành `["BTCUSDT", "ETHUSDT"]`.
- **Xử lý:** Bỏ khoảng trắng, bỏ item rỗng.

### 1.2 `ScanConfig` dataclass

| Field | Type | Env | Default | Mô tả |
|-------|------|-----|---------|------|
| `scan_mode` | str | SCAN_MODE | "fixed" | "fixed" \| "opportunity" |
| `opportunity_volatility_pct` | float | OPPORTUNITY_VOLATILITY_PCT | 5.0 | Min \|priceChange%\|
| `opportunity_volatility_max_pct` | float | OPPORTUNITY_VOLATILITY_MAX_PCT | 25.0 | Max (tránh pump & dump) |
| `min_quote_volume_usd` | float | MIN_QUOTE_VOLUME_USD | 5_000_000 | Thanh khoản tối thiểu |
| `max_pairs_per_scan` | int | MAX_PAIRS_PER_SCAN | 30 | Cap số cặp/cycle |
| `core_pairs` | list[str] | CORE_PAIRS | BTCUSDT,ETHUSDT | Luôn scan, bypass futures filter |
| `scan_blacklist` | list[str] | SCAN_BLACKLIST | USDCUSDT,... | Stablecoin blacklist |
| `opportunity_use_whitelist` | bool | OPPORTUNITY_USE_WHITELIST | false | opportunity ∩ ALLOWED_PAIRS |
| `scan_dry_run` | bool | SCAN_DRY_RUN | false | Chỉ log, không analyze |
| `market_regime_mode` | str | MARKET_REGIME_MODE | "auto" | "auto" \| "manual" |
| `market_regime` | str | MARKET_REGIME | "sideways" | "sideways" \| "trend" (khi manual) |
| `cooldown_cycles` | int | COOLDOWN_CYCLES | 2 | Nghỉ N cycle sau scan |
| `cycle_interval_sec` | int | CYCLE_INTERVAL_SEC | 900 | 1 cycle = 15 phút |
| `hysteresis_entry_pct` | float | HYSTERESIS_ENTRY_PCT | 5.0 | Vào khi \|change\| >= X |
| `hysteresis_exit_pct` | float | HYSTERESIS_EXIT_PCT | 3.0 | Ra khi \|change\| < X |
| `funding_extreme_threshold` | float | FUNDING_EXTREME_THRESHOLD | 0.001 | 0.1% cho confluence |

### 1.3 `_validate_scan()` trong `AppConfig.validate()`

- `scan_mode` ∈ {"fixed", "opportunity"}
- `opportunity_volatility_pct` < `opportunity_volatility_max_pct`
- `max_pairs_per_scan` > 0
- `core_pairs` ∩ `scan_blacklist` = ∅
- `market_regime_mode=manual` → `market_regime` ∈ {"sideways", "trend"}

---

## 2. Market Data (`utils/market_data.py`)

### 2.1 `get_all_tickers_24hr()`

```python
async def get_all_tickers_24hr(self) -> list[dict]:
```

- **Endpoint:** `GET /api/v3/ticker/24hr` (không truyền symbol → all pairs).
- **Return:** `list[dict]` mỗi item có `symbol`, `quoteVolume`, `priceChangePercent`, ...
- **Lỗi:** Return `[]`, log warning.

### 2.2 `get_premium_index_full()`

```python
async def get_premium_index_full(self) -> list[dict]:
```

- **Endpoint:** `GET /fapi/v1/premiumIndex` (all symbols).
- **Return:** `list[dict]` mỗi item có `symbol`, `lastFundingRate`, `markPrice`, `indexPrice`, ...
- **Dùng để:** `futures_symbols = set(p["symbol"] for p in data)`, `funding_map = {p["symbol"]: float(p.get("lastFundingRate") or 0) for p in data}`.
- **Lỗi:** Return `[]`, log warning.

### 2.3 `get_opportunity_pairs()`

```python
def get_opportunity_pairs(
    tickers: list[dict],
    futures_symbols: set[str] | None = None,
    funding_map: dict[str, float] | None = None,
    min_volatility_pct: float = 5.0,
    max_volatility_pct: float = 25.0,
    min_quote_volume_usd: float = 5_000_000,
    max_pairs_per_scan: int = 30,
    core_pairs: list[str] | None = None,
    blacklist: list[str] | None = None,
    allowed_pairs: list[str] | None = None,
    use_whitelist: bool = False,
    confluence_min_score: int = 1,
    funding_extreme_threshold: float = 0.001,
    symbols_in_cooldown: set[str] | None = None,
    scan_states: dict[str, dict] | None = None,
    hysteresis_entry_pct: float = 5.0,
    hysteresis_exit_pct: float = 3.0,
) -> list[str]:
```

**Luồng xử lý:**

1. **Parse an toàn:** `_safe_float(val)` cho `priceChangePercent`, `quoteVolume` (Binance có thể trả string).
2. **Median volume:** Tính `median_volume` từ `quoteVolume` của tất cả USDT pairs → dùng cho volume spike.
3. **Filter cơ bản:**
   - `symbol.endswith("USDT")`
   - Không trong blacklist
   - Không trong `symbols_in_cooldown`
   - `quoteVolume >= min_quote_volume_usd`
4. **Hysteresis:**
   - Nếu `in_opportunity` (từ scan_states): giữ khi `abs(pct) >= hysteresis_exit_pct`.
   - Nếu chưa in: chỉ vào khi `abs(pct) >= hysteresis_entry_pct`.
5. **Volatility:** `min_volatility_pct <= abs(priceChangePercent) <= max_volatility_pct`.
6. **Confluence score:**
   - +1: volatility pass
   - +1: `quoteVolume >= 2 * median_volume` (volume spike)
   - +1: `|funding_map[symbol]| >= funding_extreme_threshold`
   - Chỉ giữ cặp có `score >= confluence_min_score`.
7. **Futures filter:** Chỉ giữ cặp trong `futures_symbols` (nếu có).
8. **Whitelist:** Nếu `use_whitelist` và `allowed_pairs` → intersect.
9. **Sort:** Theo `abs(priceChangePercent)` giảm dần.
10. **Cap:** Lấy tối đa `max_pairs_per_scan`.
11. **Core pairs:** Thêm `core_pairs` ở đầu, dedupe, không tính vào cap.

---

## 3. Research Agent (`agents/research_agent.py`)

### 3.1 `analyze_pair()` — return type mới

**Trước:** `Optional[TradingSignal]`  
**Sau:** `tuple[Optional[TradingSignal], dict]`

`dict` metadata:
- `rule_passed`: `_rule_based_filter` trả LONG hoặc SHORT
- `claude_proceed`: Claude trả `should_trade=True` (verdict PROCEED)

### 3.2 `run_full_scan()` — logic chính

```
1. Budget check → skip nếu vượt daily limit

2. SCAN_MODE?
   - fixed: pairs_to_scan = ALLOWED_PAIRS
   - opportunity:
     a. asyncio.gather(get_all_tickers_24hr, get_premium_index_full)
     b. Nếu tickers rỗng → fallback ALLOWED_PAIRS, fallback_used=True
     c. Nếu fallback và ALLOWED_PAIRS rỗng → return []
     d. Ngược lại:
        - futures_symbols, funding_map từ premium_data
        - confluence_min: auto từ BTC |priceChange%| (< 2 → sideways → 2, else 1)
        - scan_states, symbols_in_cooldown từ DB
        - pairs_to_scan = get_opportunity_pairs(...)
        - ticker_volatility_map = {symbol: abs(priceChangePercent)} cho scan_state

3. pairs_to_scan rỗng? → return []

4. SCAN_DRY_RUN? → log pairs, return [] (không analyze)

5. asyncio.gather(analyze_pair(pair) for pair in pairs_to_scan)

6. Aggregate: rule_based_passed, claude_passed, signals_generated

7. Log funnel vào agent_logs:
   {scan_mode, opportunity_candidates, pairs_scanned, rule_based_passed,
    claude_passed, signals_generated, fallback_used}

8. Nếu opportunity mode: upsert scan_state cho mỗi pair (last_scanned_at, last_seen_volatility, in_opportunity=True)
```

### 3.3 Cooldown logic

```python
cutoff = now.timestamp() - cooldown_cycles * cycle_interval_sec
symbols_in_cooldown = {
    s for s, st in scan_states.items()
    if st.get("last_scanned_at") and _parse_ts(st["last_scanned_at"]) > cutoff
}
```

- Symbol được scan lúc T → trong cooldown đến T + N×900s.
- `_parse_ts()` xử lý ISO string, fallback UTC nếu thiếu timezone.

---

## 4. Database (`database.py`)

### 4.1 Bảng `scan_state`

```sql
CREATE TABLE IF NOT EXISTS scan_state (
    symbol TEXT PRIMARY KEY,
    last_scanned_at TEXT,
    last_seen_volatility REAL,
    in_opportunity INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);
```

| Cột | Mô tả |
|-----|-------|
| symbol | Mã cặp (PK) |
| last_scanned_at | ISO timestamp lần scan gần nhất |
| last_seen_volatility | \|priceChangePercent\| lúc đó |
| in_opportunity | 0/1 — dùng cho hysteresis |
| updated_at | Lần cập nhật gần nhất |

### 4.2 `get_scan_state(symbol)` / `get_all_scan_states()`

- `get_scan_state`: Lấy 1 row theo symbol.
- `get_all_scan_states`: Trả `dict[symbol, {last_scanned_at, last_seen_volatility, in_opportunity}]`.

### 4.3 `upsert_scan_state(symbol, last_scanned_at, last_seen_volatility, in_opportunity)`

- INSERT hoặc UPDATE (ON CONFLICT) theo symbol.

---

## 5. Daily Metrics Report (`utils/daily_metrics_report.py`)

### 5.1 Chức năng

- Query SQL theo spec 006 (signals_d, approved_d, trades_d, spend_d).
- Tính `approve_rate_pct`, `execute_rate_pct`, `win_rate_pct`, `efficiency_usdt_per_usd`.
- Tính `quality_score` (0–100) và `action` (SCALE_UP_SMALL, HOLD, TIGHTEN_FILTER, DEFENSIVE_MODE).

### 5.2 Công thức quality score

```
S_win   = clamp(win_rate_pct, 0, 100)
S_pnl   = clamp(50 + 8 * avg_trade_pnl_pct, 0, 100)
S_drawdown = clamp(100 - 6 * |min(worst_pnl_pct, 0)|, 0, 100)
S_eff   = clamp(10 * efficiency_usdt_per_usd, 0, 100)
S_conf  = clamp(avg_confidence, 0, 100)

quality_score = 0.35*S_win + 0.25*S_pnl + 0.15*S_drawdown + 0.15*S_eff + 0.10*S_conf
```

### 5.3 Output files

| File | Nội dung |
|------|----------|
| daily_dashboard.csv | 1 row/ngày: signals, approved, executed, win_rate, PnL, score, action |
| pair_daily.csv | 1 row/(ngày, pair): trades, wins, PnL |
| funnel_daily.csv | 1 row/ngày: funnel rates (approve, execute, win) |

### 5.4 Chạy

```bash
python utils/daily_metrics_report.py --days 14 --out data/reports
```

---

## 6. Scripts

### 6.1 `scripts/smoke_opportunity.py`

- Gọi `get_all_tickers_24hr()`, `get_premium_index_full()`.
- Gọi `get_opportunity_pairs()` với config mặc định.
- In số lượng tickers, futures, opportunity pairs.
- Exit 0 nếu pass.

### 6.2 `scripts/check_metrics.py`

- Gọi `db.get_stats()`, `get_open_trades()`, `get_pending_signals()`, `get_today_spend()`.
- In ra console.

---

## 7. Env vars mới (`.env.example`)

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
MARKET_REGIME_MODE=auto
MARKET_REGIME=sideways
COOLDOWN_CYCLES=2
CYCLE_INTERVAL_SEC=900
HYSTERESIS_ENTRY_PCT=5.0
HYSTERESIS_EXIT_PCT=3.0
FUNDING_EXTREME_THRESHOLD=0.001
```

---

## 8. Luồng dữ liệu

```
Binance API
    │
    ├── /api/v3/ticker/24hr ────► get_all_tickers_24hr() ──┐
    │                                                       │
    └── /fapi/v1/premiumIndex ──► get_premium_index_full() ─┼──► get_opportunity_pairs()
                                                            │         │
                                                            │         ├── futures_symbols
                                                            │         ├── funding_map
                                                            │         ├── scan_states (DB)
                                                            │         └── symbols_in_cooldown
                                                            │
                                                            ▼
                                                    pairs_to_scan
                                                            │
                                                            ▼
                                                    analyze_pair() × N
                                                            │
                                                            ├── rule_based_filter ──► rule_passed
                                                            ├── _claude_analyze ──► claude_proceed
                                                            └── TradingSignal (nếu confidence >= min)
                                                            │
                                                            ▼
                                                    agent_logs (funnel)
                                                    scan_state (upsert)
```

---

## 9. Tài liệu liên quan

- `004-dynamic-pair-screening-plan.md` — Chiến lược
- `005-opportunity-screening-implementation-checklist.md` — Checklist
- `006-daily-metrics-score-spec.md` — Spec CSV/score
- `007-opportunity-screening-pr-rollout-plan.md` — Kế hoạch PR
- `010-opportunity-screening-single-command-execution-guide.md` — Lệnh chạy
