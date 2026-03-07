# 003 — Implementation Plan (Strategy 002)

**Ngày:** 2025-03-04  
Mô tả chi tiết các thay đổi code theo [002-strategy-plan-2025-03-04.md](./002-strategy-plan-2025-03-04.md).

---

## Mục lục

| # | Task | File(s) | Sprint | Trạng thái |
|---|------|---------|--------|------------|
| [001](#001-budget-cap) | Budget cap (daily_stats) | database.py, research_agent.py, config.py | Tuần 1 | ✅ Done |
| [002](#002-backtest-report) | backtest_report.py + buy-and-hold | utils/backtest_report.py | Tuần 1 | ✅ Done |
| [003](#003-pre-mortem-prompt) | RESEARCH_SYSTEM_PROMPT → pre-mortem | agents/research_agent.py | Tuần 1 | ✅ Done |
| [004](#004-technical-score-bias) | Fix technical score (net_score, direction_bias) | utils/market_data.py, models.py | Tuần 2 | ✅ Done |
| [005](#005-slippage-fee) | Slippage + fee trong paper mode | agents/executor_agent.py, main.py | Tuần 2 | ✅ Done |
| [006](#006-position-sizing) | Position sizing (available balance) | agents/research_agent.py | Tuần 2 | ✅ Done |
| [007](#007-asyncio-lock) | threading.RLock cho DB writes | database.py | Tuần 2 | ✅ Done |
| [008](#008-migration) | Database migration | database.py | Tuần 2 | ✅ Done |
| [009](#009-derivatives-fetchers) | Funding, OI, basis fetchers + DerivativesSignal | utils/market_data.py, models.py | Tuần 3 | ✅ Done |
| [010](#010-regime-filter-entry) | ADX regime + rule-based filter + entry/SL/TP | utils/market_data.py, agents/research_agent.py | Tuần 4 | ✅ Done |

---

## 001. Budget cap

**Files:** `database.py`, `research_agent.py`, `config.py`, `.env.example`

### Thay đổi

1. **config.py** — Thêm `anthropic_daily_budget_usd: float = 0.75` (đọc từ env).
2. **database.py** — Migration: `ALTER TABLE daily_stats ADD COLUMN anthropic_spend_usd REAL DEFAULT 0.0`.
3. **database.py** — `ensure_daily_stats_row(date)` — `INSERT OR IGNORE INTO daily_stats (date, anthropic_spend_usd) VALUES (?, 0.0)`.
4. **database.py** — `get_today_spend() -> float`, `add_anthropic_spend(amount: float)`.
5. **research_agent.py** — Trước mỗi Claude call: `if db.get_today_spend() >= cfg.anthropic_daily_budget_usd: return None`. Sau call: `db.add_anthropic_spend(0.005)`.
6. **main.py** — Khi start: gọi `db.ensure_daily_stats_row(date.today().isoformat())`.

### Lưu ý

- `daily_stats` row có thể chưa tồn tại ngày đầu → phải INSERT OR IGNORE khi start.
- Estimated cost $0.005/call (Sonnet). Cap $0.75 cho 6 pairs.

---

## 002. backtest_report.py

**Files:** `utils/backtest_report.py` (mới)

### Thay đổi

- Standalone script: `python utils/backtest_report.py [--days 30] [--csv output.csv]`.
- Query từ `trades` table: win_rate, profit_factor, total_trades, avg_pnl, max_drawdown, Sharpe (sqrt(365)).
- **Bắt buộc:** Column `benchmark_buy_hold_return` — fetch BTC price start/end period, tính return buy-and-hold.
- Per-symbol breakdown (BTC/ETH/SOL...).
- Per-hour UTC breakdown.
- Rolling 30-day stats.
- Chỉ query columns hiện có (không regime/atr_pct cho đến sau migration).

---

## 003. Pre-mortem prompt

**Files:** `agents/research_agent.py`

### Thay đổi

- Sửa `RESEARCH_SYSTEM_PROMPT` và user prompt.
- Claude nhận entry/SL/TP đã tính sẵn (rule-based).
- Output: PROCEED / WAIT / AVOID + brief reasoning.
- Prompt template:
  - "Given this {direction} setup for {pair}: Entry $X | SL $Y | TP $Z | Regime: {regime} | Funding: {funding}%"
  - "1. Top 3 risks that could invalidate this trade?"
  - "2. Most likely way this trade loses money?"
  - "3. News/event catalyst in 24h?"
  - "Answer: PROCEED / WAIT / AVOID"

---

## 004. Technical score bias

**Files:** `utils/market_data.py`, `models.py`

### Thay đổi

- `TechnicalSignal`: thêm `net_score: int` (-100 đến +100), `direction_bias: str` ("LONG"/"SHORT"/"NEUTRAL").
- `compute_technical_signal`: thêm điểm bearish (RSI > 70, downtrend, MACD bearish, EMA cross bearish).
- `net_score = bullish_score - bearish_score` hoặc tương đương.
- `direction_bias` từ net_score: > 10 → LONG, < -10 → SHORT, else NEUTRAL.

---

## 005. Slippage + fee trong paper mode

**Files:** `agents/executor_agent.py`, `main.py` (position monitor)

### Thay đổi

- `executor_agent.py` `_paper_execute`: `SLIPPAGE_PCT = 0.0015`, `FEE_PCT = 0.001`.
- LONG: `filled_price = entry * (1 + SLIPPAGE_PCT)`.
- SHORT: `filled_price = entry * (1 - SLIPPAGE_PCT)`.
- Fee: `fee_cost = position_size_usdt * FEE_PCT * 2` (cả 2 chiều).
- Khi close (trong `_monitor_positions`): `pnl = ... - fee_cost`. Lưu `fees_usdt` vào trades.

---

## 006. Position sizing (available balance)

**Files:** `agents/research_agent.py`

### Thay đổi

- Thêm `_get_available_balance() -> float`: `paper_balance - sum(open_trades.position_size_usdt)`.
- Thay `_get_portfolio_value()` bằng `_get_available_balance()` khi tính `position_size`.
- `position_size = min(available * max_position_pct, available * 0.4)` (hard cap 40%).

---

## 007. threading.RLock cho DB writes

**Files:** `database.py`

### Đã implement

- `self._write_lock = threading.RLock()` — RLock vì `ensure_daily_stats_row` được gọi từ `add_anthropic_spend` (đã giữ lock).
- Bọc tất cả write operations trong `with self._write_lock:`: `save_signal`, `update_signal_status`, `save_trade`, `log`, `add_anthropic_spend`, `ensure_daily_stats_row`, `migrate`.
- Dùng sync lock vì `database.py` dùng sync sqlite3; các agent gọi DB từ sync context.

---

## 008. Database migration

**Files:** `database.py`

### Đã implement

- `database.migrate()` — idempotent, gọi trong `Database.__init__`:
  - `signals`: `regime`, `net_score`, `model_version`
  - `daily_stats`: `anthropic_spend_usd`
  - `trades`: `fees_usdt`
- Mỗi ALTER trong try/except, ignore "duplicate column".
- Không cần gọi trong `main.py` — migration chạy khi Database init.

---

## 009. Derivatives fetchers (Sprint 2)

**Files:** `utils/market_data.py`, `models.py`

### Đã implement

- `BinanceDataFetcher`: `get_funding_rate(symbol)` (premiumIndex), `get_open_interest(symbol)` + mark, `get_mark_price(symbol)` — Binance Futures `fapi.binance.com`.
- `DerivativesSignal` model: funding_rate, funding_rate_annualized, open_interest_usdt, oi_change_pct (openInterestHist 1d), basis_pct, signal (LONG_SQUEEZE/SHORT_SQUEEZE/NEUTRAL), score.
- `get_derivatives_signal(pair)` — gọi funding + OI + mark song song, OI hist cho change %, logic: funding>0.05% & basis>0.2% → SHORT_SQUEEZE; funding<-0.03% & basis<-0.1% → LONG_SQUEEZE.
- `analyze_pair`: thêm `get_derivatives_signal(pair)` vào `asyncio.gather`.

---

## 010. ADX regime + rule-based filter + entry/SL/TP (Sprint 3)

**Files:** `utils/market_data.py`, `agents/research_agent.py`, `models.py`, `database.py`

### Đã implement

- `TechnicalSignal`: thêm atr_value, atr_pct, atr_ratio, adx, plus_di, minus_di, bb_width. `compute_technical_signal`: ADX trên 4h, ATR14/ATR50 trên 1h.
- `classify_regime(adx, plus_di, minus_di, bb_width, atr_ratio)` — atr_ratio>1.5→volatile; adx>25→trending_up/down; adx<20 & bb<3%→ranging.
- `_rule_based_filter(technical, derivatives)` — LONG: trend!=downtrend, RSI<45, funding<0.05%, net_score>10. SHORT: trend!=uptrend, RSI>55, funding>0.05%, net_score<-10.
- `calc_entry_sl_tp(direction, price, atr_value, regime)` — mult 1.5 (trending) / 1.2 (ranging), R:R 1:2.
- Flow: gather (technical, whale, sentiment, derivatives) → filter → None thì return → regime → calc entry/SL/TP → Claude pre-mortem → PROCEED thì build TradingSignal.
- `TradingSignal`: regime, model_version. `save_signal`: lưu regime, model_version.

---

## Bug fixes (review 2025-03-04)

| # | File | Fix |
|---|------|-----|
| 1 | database.py | save_signal: thêm net_score vào INSERT |
| 2 | database.py | save_trade: thêm fees_usdt vào INSERT |
| 3 | executor_agent.py | _paper_execute: FEE_PCT, fees_usdt khi mở trade |
| 4 | main.py | _monitor_positions: dùng db.close_trade() thay vì conn trực tiếp (lock + merge UPDATE) |
| 5 | executor_agent.py | _real_execute: NotImplementedError guard (OCO chưa fix) |
| Minor | research_agent.py | Docstring: Claude Opus → Sonnet pre-mortem |

## Bug fixes (review 2 — P0/P1/P2)

**P0:**
- A. market_data.py: BB width (BBU - BBL) / BBM (trước: BBL - BBU → âm)
- B. market_data.py: df_1d limit 50 → 210 (EMA200 cần ≥200 nến)
- C. market_data.py: base luôn BASE_URL (data mainnet, testnet chỉ cho orders)
- D. research_agent.py: model "claude-sonnet-4-6" (đúng ID)

**P1:**
- 1. research_agent.py: min position_size $10 guard
- 2. config.py: ALLOWED_PAIRS đọc từ env
- 3. market_data.py: mempool usd_approx = btc_price từ Binance (không hardcode $95k)
- 4. main.py: cron timezone="Asia/Ho_Chi_Minh"
- 5. executor_agent.py: close_trade_market trừ fee + set fees_usdt

**P2:**
- 6. backtest_report.py: conn try/finally
- 7. telegram_bot.py: parse_mode="Markdown" + escape reasoning
- 8. main.py: heartbeat 6h (plan)
- 9. market_data.py: get_derivatives_signal — premiumIndex 1 lần (không gọi 2x)

## Bug fixes (review 3 — operational)

**Bugs:**
- 1. executor_agent: _paper_execute dùng current_price tại execute (không giá cũ)
- 2. research_agent: ATR=0 guard trước calc entry/SL/TP (tránh đốt budget)
- 3. telegram_bot + database: approve/skip từ DB khi restart; expire_stale_pending_signals
- 4. database: _inc_daily_stat — total_signals, approved_signals, executed_trades, winning_trades, pnl
- 5. models: reasoning_safe = re.sub strip chars (MarkdownV1 không hỗ trợ backslash escape)

**Minor:**
- orderbook_walls: depth + price parallel (asyncio.gather)
- FearGreedFetcher: shared _client (không tạo mới mỗi get)
- _heartbeat docstring: 6 giờ

## Bug fixes (review 4)

- 1. research_agent: ATR guard `not (atr_value > 0)` (bắt cả NaN)
- 2. research_agent: await self.fear_greed.close()
- 3. main.py: pause_job vào trong if not _circuit_breaker_triggered
- Minor: database.py bỏ TradeStatus import; research_agent CLAUDE_ESTIMATED_COST_PER_CALL sau imports

## Bug fixes (review 5)

- 1. research_agent: analysis None → verdict/confidence safe access (tránh AttributeError)
- 2. main.py: khi re-validation fail → update_signal_status CANCELLED (signal không kẹt APPROVED)
- 3. research_agent: budget check run_full_scan + _claude_semaphore(2) (tránh budget race)
- 4. research_agent: position size check trước Claude (tránh lãng phí budget khi available=0)

## Bug fixes (review 6)

- 1. main.py: try/except quanh execute(), CANCELLED khi exception hoặc trade=None
- 2. research_agent: confidence < min_confidence guard trước save_signal (tránh zombie PENDING)
- Trivial: main.py startup message dùng ALLOWED_PAIRS thay hardcode

## Bug fixes (review 7)

- 1. research_agent: add_anthropic_spend ngay sau API call (trước json.loads) — budget track khi JSON fail
- Trivial: main.py bỏ Trade import; run_full_scan xoá dead else block

## Bug fixes (review 8)

- 1. executor_agent: close_trade_market dùng db.close_trade() thay save_trade (update daily_stats)
- 2. market_data: get_derivatives_signal fail → return funding_rate=0.0005 (tránh LONG filter pass sai)

## Bug fixes (review 9)

- 1. database.py: date.today() → datetime.utcnow().date() (timezone mismatch vs closed_at UTC)
- 2. main.py: circuit breaker trigger → emergency close all open positions via close_trade_market

## Bug fixes (review 10)

- 1. database.py: close_trade() — WHERE id=? AND status='OPEN', skip daily_stats if rowcount=0 (double-close race safe)

## Bug fixes (review 11)

- 1. telegram_bot.py: _pending_signals.pop(short_id, None) on send_message failure (ghost signal cleanup)
- 2. models.py: datetime.utcnow → datetime.now(timezone.utc) (Python 3.12 deprecation)

---

## Thứ tự thực hiện

```
Tuần 1: 001 (budget) + 002 (backtest) + 003 (prompt)
Tuần 2: 004, 005, 006, 007, 008
Tuần 3: 009
Tuần 4: 010
+ Bug fixes (review)
```

---

## Config / .env cần thêm

```env
# Budget cap (6 pairs paper ~$0.58/ngày)
ANTHROPIC_DAILY_BUDGET_USD=0.75

# Pairs: 6 paper, 2 live S1, 3 live S2
ALLOWED_PAIRS=BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT
```
