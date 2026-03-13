# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A DeFi crypto trading agent system that runs two parallel scanners (ResearchAgent + SMCAgent), validates signals through a RiskManager, sends alerts via Telegram for user approval, then executes trades through paper or live Binance. Written in Python 3.11+, async-first with APScheduler.

## Commands

```bash
# Install
pip install -r requirements.txt
cp .env.example .env  # fill ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Run (starts scheduler + web UI on :8080)
python main.py

# Run without Telegram (for local dev / API blocked)
SKIP_TELEGRAM=true python main.py

# Backtest
python backtest.py
python scripts/run_backtest_suite.py
python scripts/run_backtest_full_report.py

# SMC-specific backtest
python scripts/run_smc_backtest.py

# Optimizer (walk-forward)
python scripts/run_optimizer.py

# Utilities
python scripts/check_metrics.py
python scripts/review_trades.py
python scripts/clear_pending_signals.py
```

## Architecture

### Signal Pipeline

```
Scheduler (APScheduler)
  ├── ResearchAgent.run_full_scan()  ─┐
  │   (Claude Sonnet pre-mortem +     │
  │    rule-based technical analysis) │
  │                                    ├──> _process_signal()
  └── SMCAgent.run_full_scan()  ──────┘        │
      (ICT methodology, no LLM)               ▼
                                      RiskManagerAgent.validate()
                                               │
                                    ┌──────────┴──────────┐
                                    │                     │
                             SKIP_TELEGRAM=true    Telegram alert
                             auto-execute          user /approve or /skip
                                    │                     │
                                    └──────────┬──────────┘
                                               ▼
                                      ExecutorAgent.execute()
                                      (paper or live Binance)
                                               │
                                               ▼
                                      Position monitor (SL/TP/trail/time-exit)
```

### Two Independent Scanners

- **ResearchAgent** (`agents/research_agent.py`): Fetches technicals (RSI, EMA, MACD, BB, VWAP, ADX), whale data, sentiment, derivatives. Calls Claude Sonnet as a pre-mortem risk assessor (not predictor). Budget-capped via `ANTHROPIC_DAILY_BUDGET_USD`.
- **SMCAgent** (`agents/smc_agent.py`): Pure rule-based ICT/Smart Money Concepts. Uses `SMCStrategy` for top-down multi-TF analysis (Daily→4h/1h→15m/5m). Adjusts confidence via crypto confluence (funding rate, OI, CVD). No LLM calls. Supports both `fixed` (ALLOWED_PAIRS) and `opportunity` (dynamic screening) scan modes — same screening logic as ResearchAgent.

Both scanners produce `TradingSignal` objects that flow through the same pipeline.

### Trading Styles

Controlled by `TRADING_STYLE` env var (auto-detected from `SCAN_MODE`):
- **scalp**: 5m entry timing, 15m direction, 1h ADX, 4h trend. 5-min scan interval, 1-min position monitor, 45-min max hold, trailing stop (breakeven → lock 50%), limit order fill simulation.
- **swing**: 1h/4h/1d timeframes. 15-min scan, 2-min monitor, market order fill.

### Key Modules

- `config.py`: Singleton `cfg = AppConfig()` — all config from env vars via dataclasses. `ScanConfig.__post_init__` auto-derives trading_style, intervals, pair limits.
- `models.py`: Pydantic models — `TradingSignal` (with nested `TechnicalSignal`, `WhaleSignal`, `SentimentSignal`, `DerivativesSignal`), `Trade`, `PortfolioState`.
- `database.py`: SQLite with thread-safe `_write_lock` (RLock). Idempotent migrations in `migrate()`. Tables: signals, trades, daily_stats, agent_logs, scan_state.
- `utils/market_data.py`: `BinanceDataFetcher` (OHLCV, derivatives, CVD, 24h stats), `WhaleDataFetcher`, `FearGreedFetcher`, `get_opportunity_pairs()`, `classify_regime()`, `calc_entry_sl_tp()`.
- `utils/smc.py`: `SMCAnalyzer` — order blocks, FVGs, BOS/CHoCH, liquidity sweeps.
- `utils/smc_strategy.py`: `SMCStrategy` — top-down HTF→MTF→LTF analysis producing `SMCSetup`.
- `utils/crypto_confluence.py`: `interpret_funding()`, `interpret_oi()`, `interpret_cvd()` — point adjustments (-12 to +10) added to SMC signal confidence.
- `telegram_bot.py`: `TelegramNotifier` — signal alerts, /approve, /skip, daily reports, heartbeat.
- `optimization/`: Walk-forward optimizer, metrics calculator, improvement engine, change registry.

### Safety Systems

- **RiskManagerAgent**: 7 sequential checks (daily loss, open position count, correlation, confidence, R:R, duplicate pair, position size). All must pass. Portfolio balance is computed from cumulative PnL (`paper_balance_usdt + cumulative_pnl`) — not just the static config value.
- **Circuit breaker**: Pauses scanning + emergency-closes all positions when daily loss (including unrealized PnL from open positions) exceeds `MAX_DAILY_LOSS_PCT`. Hysteresis logic: must recover to 50% of max loss threshold AND wait at least 1 hour cooldown before resuming. Also auto-resets on new day.
- **Price freshness guard** (scalp): Rejects signal if price broke SL between signal generation and approval.
- **Pair cooldown**: 30-min cooldown per pair to prevent duplicate signals.
- **Anthropic budget cap**: Tracks daily Claude API spend in daily_stats; skips Claude calls when budget exceeded.

## Important Patterns

- All agents share a single `Database` instance from the orchestrator. Thread safety via `_write_lock`.
- `BinanceDataFetcher` uses httpx async client — must call `await fetcher.close()` in finally blocks.
- Live trading (`_real_execute`) is disabled with `raise NotImplementedError` until OCO order flow is fixed. Only paper trading works.
- The project uses Vietnamese comments throughout. Config validation errors and log messages are in Vietnamese.
- DB path is absolute (`Path(__file__).resolve().parent / "data" / "trading.db"`) so web UI and scripts share the same DB.
- Scan modes: `fixed` (uses `ALLOWED_PAIRS`) vs `opportunity` (dynamic screening by volatility/volume from Binance tickers).
- **SMC entry**: OB entry uses OB zone fill (low→high range, not exact midpoint). Entry = better of (midpoint, current_price). SL uses ATR-based buffer (`0.5 × ATR`, floor 0.2% of price) instead of fixed percentage. `SMCSetup` has `ob_zone_low/ob_zone_high` fields.
- **SMC entry cascade**: `ob_entry → sweep_reversal → bpr_entry`. `ce_entry` is disabled (25% WR on 67 backtest trades was clear negative edge).
- **Confluence adjustment**: Weighted average of point adjustments (-12 to +10, weights: funding 0.4, OI 0.3, CVD 0.3), capped ±15, added to confidence (not multiplied). Example: base 80 + worst case adjustment = ~70 (vs old multiplicative approach that could collapse to 52).
- **Displacement detection**: Full displacement = 1.2x ATR + 50% body ratio. Near-displacement (1.0-1.2x ATR + 55% body) grants +12 confidence. Lookback = 15 candles. FVG bonus +5 for full displacement with FVG.
- **Funding filter**: Symmetric ±0.03% — blocks LONG when funding > +0.03% and SHORT when funding < -0.03%.
