## Session Start — MANDATORY
Always run initial_instructions Serena tool at the start of every session.

## Codebase Navigation — MANDATORY
ALWAYS use Serena tools FIRST before reading any file:
- get_symbols_overview -> xem structure cua file
- find_symbol -> tim class/method cu the
- find_referencing_symbols -> tim noi symbol duoc dung
NEVER use Read on .dart files without trying Serena first.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.


## Commands

```bash
# Setup (WSL) — mặc định dùng venv2
./setup_wsl.sh

# Run (WSL)
source venv2/bin/activate
python main.py

# Chỉ chạy Web UI (xem dashboard khi agent không chạy)
source venv2/bin/activate
python -m web.app
```

Required env vars: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Set `PAPER_TRADING=true` (default) để test.

**Web UI**: Khi chạy `main.py`, dashboard tại http://localhost:8080. Set `WEB_UI_PORT` để đổi port.

## Architecture

This is an AI-powered crypto trading agent. The core flow is:

```
ResearchAgent (every 15min)
  → fetches Binance OHLCV + whale txs + Fear&Greed in parallel
  → calls Claude API to analyze and produce a TradingSignal JSON
  → if confidence >= MIN_CONFIDENCE (default 85): passes to RiskManager

RiskManagerAgent
  → validates: daily loss limit, max open positions, confidence, R:R ratio, duplicate pairs
  → if valid: sends Telegram alert to user

User (via Telegram bot)
  → /approve <id> → ExecutorAgent places order
  → /skip <id>    → signal discarded
  → no action     → auto-expires after APPROVAL_TIMEOUT_SEC (default 300s)

ExecutorAgent
  → PAPER_TRADING=true: simulates the trade
  → PAPER_TRADING=false: places LIMIT order on Binance with stop-loss OCO

PositionMonitor (every 2min, paper only)
  → checks SL/TP hits, closes positions, sends P&L report via Telegram

CircuitBreaker (every 5min)
  → if daily_pnl < -MAX_DAILY_LOSS_PCT of portfolio: pauses market scan job
```

### Key Files

- **`main.py`** — `TradingOrchestrator` wires all components together using APScheduler. This is the only entry point.
- **`config.py`** — All config via `dataclass` + env vars. Import as `from config import cfg`. Also exports `ALLOWED_PAIRS`, `ANALYSIS_INTERVALS`, `WHALE_MIN_USD`, `DB_PATH`.
- **`models.py`** — Pydantic models: `TradingSignal` (core signal with sub-signals), `Trade` (executed position), `PortfolioState`. Enums: `Direction`, `SignalStatus`, `TradeStatus`.
- **`database.py`** — SQLite wrapper. Tables: `signals`, `trades`, `daily_stats`, `agent_logs`. `Database.conn` is the raw sqlite3 connection (used directly in some places).
- **`telegram_bot.py`** — `TelegramNotifier` handles sending alerts and processing `/approve`, `/skip`, `/status`, `/pending` commands. The `on_approve_callback` is injected from the orchestrator.
- **`agents/research_agent.py`** — Calls `claude-sonnet-4-20250514` synchronously (note: `anthropic.Anthropic` not async client) inside an async method. Returns `Optional[TradingSignal]`.
- **`agents/risk_manager.py`** — Pure synchronous validation. `RiskManagerAgent.validate()` returns `(bool, str)`.
- **`agents/executor_agent.py`** — Places orders. Paper mode simulates immediately; live mode uses `python-binance`.
- **`utils/market_data.py`** — Three async fetchers: `BinanceDataFetcher` (OHLCV + technicals via `pandas-ta`), `WhaleDataFetcher` (large txs from Binance aggTrades), `FearGreedFetcher` (Alternative.me API). All use `httpx.AsyncClient`.

### Important Patterns

- **Async throughout**: all agent `run_*` and `analyze_*` methods are `async`. The Anthropic SDK call in `research_agent.py` is synchronous — if you add async Anthropic calls, use `anthropic.AsyncAnthropic`.
- **Signal ID**: UUIDs stored full in DB. Telegram commands use the first 8 chars (`id[:8]`) as short ID, looked up via `LIKE '{short_id}%'` in `get_signal_by_short_id`.
- **Paper vs live**: `cfg.trading.paper_trading` controls behavior throughout. Never remove the paper-trading guard before adding proper Binance balance fetching (the `TODO` in `risk_manager.py` and `research_agent.py`).
- **Config validation**: `cfg.validate()` is called at startup and raises `ValueError` listing all missing keys. Add new required keys there.
- **Logs**: loguru to `data/logs/trading_{time}.log` (INFO) and `data/logs/errors_{time}.log` (ERROR). Agent-level events also go to `agent_logs` table via `db.log(agent, level, message, data_dict)`.
