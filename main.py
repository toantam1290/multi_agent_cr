"""
main.py - Entry point, orchestrate toàn bộ hệ thống
"""
import asyncio
import logging
import signal as sys_signal
import sys
import threading
from datetime import datetime, date, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

# Giảm log APScheduler "job was missed" (chỉ trễ vài giây, job vẫn chạy)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

from config import cfg, ALLOWED_PAIRS, get_effective_approval_timeout_sec
from database import Database
from models import TradingSignal, SignalStatus, Trade, Direction, TradeStatus
from agents.research_agent import ResearchAgent
from agents.risk_manager import RiskManagerAgent
from agents.executor_agent import ExecutorAgent
from agents.smc_agent import SMCAgent
from telegram_bot import TelegramNotifier


# ─── Logging setup ───────────────────────────────────────────────────────────
logger.add("data/logs/trading_{time}.log", rotation="1 day", retention="30 days", level="INFO")
logger.add("data/logs/errors_{time}.log", rotation="1 week", level="ERROR")


class TradingOrchestrator:
    """
    Orchestrator - điều phối tất cả agents

    Flow:
    1. ResearchAgent scan market mỗi 15 phút
    2. RiskManagerAgent validate signal
    3. TelegramNotifier gửi alert
    4. User approve/skip
    5. ExecutorAgent thực hiện lệnh
    6. Monitor positions
    """

    def __init__(self):
        self.db = Database()
        self.risk_manager = RiskManagerAgent(self.db)
        self.executor = ExecutorAgent(self.db)
        self.telegram = TelegramNotifier(
            db=self.db,
            on_approve_callback=self._on_user_approve,
        )
        self.research = ResearchAgent(self.db)
        self.smc_agent = SMCAgent(self.db, telegram=self.telegram)
        self.scheduler = AsyncIOScheduler()
        self._running = False
        self._circuit_breaker_triggered = False
        self._circuit_breaker_date: date | None = None  # Ngày trigger để reset khi qua ngày mới
        self._circuit_breaker_triggered_at: datetime | None = None

    async def start(self):
        """Khởi động toàn bộ hệ thống"""
        logger.info("=" * 60)
        logger.info("DeFi Trading Agent Starting...")
        logger.info(f"Paper Trading: {cfg.trading.paper_trading}")
        logger.info(f"Min Confidence: {cfg.trading.min_confidence} (scalp: {cfg.trading.scalp_min_confidence})")
        logger.info(f"Max Position: {cfg.trading.max_position_pct*100}%")
        logger.info("=" * 60)

        # Validate config
        try:
            cfg.validate()
        except ValueError as e:
            logger.error(f"Config invalid: {e}")
            raise

        # Ensure daily_stats row exists (for budget cap on first run)
        self.db.ensure_daily_stats_row()

        # Expire stale PENDING signals (recovery sau restart)
        self.db.expire_stale_pending_signals(get_effective_approval_timeout_sec())

        # Start Telegram bot (skip nếu SKIP_TELEGRAM=true hoặc API bị chặn)
        if not cfg.skip_telegram:
            try:
                await self.telegram.start_polling()
                pair_names = [p.replace("USDT", "") for p in ALLOWED_PAIRS]
                await self.telegram.send_message(
                    f"🤖 *Trading Agent Started*\n"
                    f"Mode: `{'PAPER' if cfg.trading.paper_trading else 'LIVE'}`\n"
                    f"Pairs: `{', '.join(pair_names)}`\n"
                    f"Scan interval: `every {cfg.scan.scan_interval_min} min` ({cfg.scan.trading_style})"
                )
            except Exception as e:
                logger.warning(f"Telegram unavailable ({e}), running without notifications")
                self.telegram._bot = None
        else:
            logger.info("SKIP_TELEGRAM=true, running without Telegram")

        # Setup scheduled jobs (scalp: 5 min scan, 1 min monitor | swing: 15 min, 2 min)
        scan_min = cfg.scan.scan_interval_min
        monitor_min = cfg.scan.position_monitor_interval_min
        logger.info(f"Scan interval: {scan_min} min | Position monitor: {monitor_min} min ({cfg.scan.trading_style})")

        # misfire_grace_time: mặc định 1s → job trễ >1s bị skip. Scalp scan có thể 5–15 phút
        # (fetch tickers, premium, nhiều pairs, Claude API) → tăng lên để lần chạy tiếp vẫn chạy
        # coalesce=True: nếu nhiều lần bị miss, chỉ chạy 1 lần khi có thể
        scan_misfire_sec = max(600, scan_min * 120)  # ít nhất 10 phút, hoặc 2× interval
        monitor_misfire_sec = max(120, monitor_min * 60)  # ít nhất 2 phút

        self.scheduler.add_job(
            self._scan_market,
            "interval",
            minutes=scan_min,
            id="market_scan",
            next_run_time=datetime.now(),  # Run immediately on start
            misfire_grace_time=scan_misfire_sec,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._monitor_positions,
            "interval",
            minutes=monitor_min,
            id="position_monitor",
            misfire_grace_time=monitor_misfire_sec,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._daily_report,
            "cron",
            hour=8, minute=0,
            timezone="Asia/Ho_Chi_Minh",  # 8h sáng VN
            id="daily_report",
        )
        self.scheduler.add_job(
            self._circuit_breaker_check,
            "interval",
            minutes=5,
            id="circuit_breaker",
            misfire_grace_time=300,
            coalesce=True,
        )
        # SMC standalone scan — chạy riêng, độc lập với research_scan
        smc_interval_min = 5 if cfg.scan.trading_style == "scalp" else 15
        self.scheduler.add_job(
            self._smc_scan,
            "interval",
            minutes=smc_interval_min,
            id="smc_scan",
            misfire_grace_time=max(300, smc_interval_min * 60),
            coalesce=True,
        )
        self.scheduler.add_job(
            self._heartbeat,
            "interval",
            hours=6,  # Plan: mỗi 6 giờ (tránh spam Telegram)
            id="heartbeat",
            misfire_grace_time=600,
        )

        self.scheduler.start()
        self._running = True

        # Start web UI (chạy trong thread riêng, WSL: http://localhost:8080)
        web_port = int(__import__("os").environ.get("WEB_UI_PORT", "8080"))
        def run_web():
            try:
                import uvicorn
                from web.app import app
                uvicorn.run(app, host="0.0.0.0", port=web_port, log_level="warning")
            except Exception as e:
                logger.warning(f"Web UI failed to start: {e}")

        self._web_thread = threading.Thread(target=run_web, daemon=True)
        self._web_thread.start()
        logger.success(f"Trading Agent is running! Web UI: http://localhost:{web_port}")

        # Keep running
        while self._running:
            await asyncio.sleep(1)

    async def _smc_scan(self):
        """SMC standalone scan — chạy độc lập, song song với research_scan."""
        try:
            signals = await self.smc_agent.run_full_scan()
            for signal in signals:
                await self._process_signal(signal)
        except Exception as e:
            logger.error(f"SMC scan error: {e}")

    async def _scan_market(self):
        """Scan market và process signals"""
        logger.info("Starting market scan...")
        try:
            signals = await self.research.run_full_scan()

            for signal in signals:
                await self._process_signal(signal)

            logger.info(f"Market scan done ({len(signals)} signals)")
        except Exception as e:
            logger.error(f"Market scan error: {e}")
            await self.telegram.send_message(f"⚠️ Market scan error: {e}")

    async def _process_signal(self, signal: TradingSignal):
        """Pipeline: Risk Check → Alert user (or auto-execute if SKIP_TELEGRAM)"""
        # Risk check
        portfolio = self.risk_manager.get_portfolio_state()
        is_valid, reason = self.risk_manager.validate(signal, portfolio)

        if not is_valid:
            self.db.update_signal_status(signal.id, SignalStatus.REJECTED, cancel_reason=f"risk_check:{reason[:100]}")
            logger.info(f"Signal rejected by Risk Manager: {reason}")
            return

        if cfg.skip_telegram:
            # Không cần Telegram: auto-execute ngay (paper/live tùy config)
            logger.info(f"SKIP_TELEGRAM=true: auto-executing signal {signal.pair} {signal.direction.value}")
            await self._on_user_approve(signal)
        else:
            # Forward to user via Telegram, chờ /approve hoặc /skip
            await self.telegram.send_signal_alert(signal)

    async def _on_user_approve(self, signal: TradingSignal):
        """Callback khi user approve signal"""
        logger.info(f"User approved signal: {signal.pair} {signal.direction.value}")

        # Re-validate trước khi execute (giá có thể đã thay đổi)
        portfolio = self.risk_manager.get_portfolio_state()
        is_valid, reason = self.risk_manager.validate(signal, portfolio)

        if not is_valid:
            self.db.update_signal_status(signal.id, SignalStatus.CANCELLED, cancel_reason=f"risk_check:{reason[:100]}")
            await self.telegram.send_message(
                f"⚠️ Cannot execute: Risk check failed\n`{reason}`"
            )
            return

        # Price freshness guard (scalp pullback entry): reject CHỈ khi SL bị phá
        # LONG: reject nếu current < SL. SHORT: reject nếu current > SL.
        # Lưu ý: current < entry (LONG) = limit fill tại giá tốt hơn → R:R cải thiện, KHÔNG reject.
        if cfg.scan.trading_style == "scalp":
            from utils.market_data import BinanceDataFetcher
            fetcher = BinanceDataFetcher()
            try:
                current_price = await fetcher.get_current_price(signal.pair)
                setup_broken = (
                    (signal.direction == Direction.LONG and current_price < signal.stop_loss)
                    or (signal.direction == Direction.SHORT and current_price > signal.stop_loss)
                )
                if setup_broken:
                    self.db.update_signal_status(signal.id, SignalStatus.CANCELLED, cancel_reason="freshness_broke_sl")
                    await self.telegram.send_message(
                        f"⚠️ *Signal expired* — Price broke SL (setup invalidated)\n"
                        f"Entry: ${signal.entry_price:,.2f} | SL: ${signal.stop_loss:,.2f} | Now: ${current_price:,.2f}"
                    )
                    logger.warning(f"Scalp signal rejected: price {current_price:.2f} broke SL {signal.stop_loss:.2f}")
                    return
            finally:
                await fetcher.close()

        try:
            trade = await self.executor.execute(signal)
        except Exception as e:
            logger.error(f"Execution exception for {signal.pair}: {e}")
            self.db.update_signal_status(signal.id, SignalStatus.CANCELLED, cancel_reason=f"exec_error:{str(e)[:80]}")
            await self.telegram.send_message(f"❌ Execution error: `{e}`")
            return

        if trade:
            await self.telegram.send_message(
                f"✅ *Trade Opened*\n"
                f"{'[PAPER] ' if trade.is_paper else ''}"
                f"{trade.direction.value} *{trade.pair}*\n"
                f"Entry: `${trade.entry_price:,.2f}`\n"
                f"Stop Loss: `${trade.stop_loss:,.2f}`\n"
                f"Take Profit: `${trade.take_profit:,.2f}`\n"
                f"Size: `${trade.position_size_usdt:,.0f} USDT`"
            )
        else:
            reason = "no_fill" if cfg.scan.trading_style == "scalp" else "exec_failed"
            self.db.update_signal_status(signal.id, SignalStatus.CANCELLED, cancel_reason=reason)
            msg = f"❌ Execution failed for {signal.pair}."
            if cfg.scan.trading_style == "scalp":
                msg += " (Paper: limit no fill — price moved past entry?)"
            msg += " Check logs."
            await self.telegram.send_message(msg)

    async def _monitor_positions(self):
        """Monitor open positions, check SL/TP (paper trading only)"""
        if not cfg.trading.paper_trading:
            return  # Binance handles SL/TP natively for real trading

        open_trades = self.db.get_open_trades()
        if not open_trades:
            return

        from utils.market_data import BinanceDataFetcher
        from models import TradeStatus, Direction

        fetcher = BinanceDataFetcher()
        try:
            for t in open_trades:
                try:
                    current_price = await fetcher.get_current_price(t["pair"])
                    direction = Direction(t["direction"])
                    entry = t["entry_price"]
                    sl_state = t.get("sl_trailing_state") or "original"

                    # Trail stop (scalp): move SL khi đang có lời
                    if cfg.scan.trading_style == "scalp" and sl_state != "locked_50":
                        target_pct = (
                            (t["take_profit"] - entry) / entry * 100
                            if direction == Direction.LONG
                            else (entry - t["take_profit"]) / entry * 100
                        )
                        unrealized_pnl_pct = (
                            (current_price - entry) / entry * 100
                            if direction == Direction.LONG
                            else (entry - current_price) / entry * 100
                        )
                        if target_pct > 0:
                            current_sl = t["stop_loss"]
                            if unrealized_pnl_pct >= target_pct * 0.8:
                                new_sl = entry + (current_price - entry) * 0.5 if direction == Direction.LONG else entry - (entry - current_price) * 0.5
                                # Chỉ trail lên (LONG) hoặc xuống (SHORT), không bao giờ overwrite với SL tệ hơn
                                if (direction == Direction.LONG and new_sl > current_sl) or (direction == Direction.SHORT and new_sl < current_sl):
                                    self.db.update_trade_sl(t["id"], new_sl, "locked_50")
                                    t["stop_loss"] = new_sl
                                    t["sl_trailing_state"] = "locked_50"
                                    logger.info(f"Trail stop: {t['pair']} SL → lock 50% profit")
                            elif unrealized_pnl_pct >= target_pct * 0.5 and sl_state == "original":
                                new_sl = entry * 1.001 if direction == Direction.LONG else entry * 0.999
                                if (direction == Direction.LONG and new_sl > current_sl) or (direction == Direction.SHORT and new_sl < current_sl):
                                    self.db.update_trade_sl(t["id"], new_sl, "breakeven")
                                    t["stop_loss"] = new_sl
                                    t["sl_trailing_state"] = "breakeven"
                                    logger.info(f"Trail stop: {t['pair']} SL → breakeven")

                    # Time-based exit (scalp): không giữ quá 45 phút
                    if cfg.scan.trading_style == "scalp" and t.get("opened_at"):
                        try:
                            opened_at = datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00"))
                            hold_minutes = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
                            if hold_minutes > 45:
                                quantity = t["quantity"]
                                exit_price = current_price
                                if direction == Direction.LONG:
                                    pnl = (exit_price - t["entry_price"]) * quantity
                                else:
                                    pnl = (t["entry_price"] - exit_price) * quantity
                                FEE_PCT = 0.001
                                fee_cost = t["position_size_usdt"] * FEE_PCT * 2
                                pnl = pnl - fee_cost
                                pnl_pct = pnl / t["position_size_usdt"] * 100
                                time_exit_status = TradeStatus.TOOK_PROFIT if pnl > 0 else TradeStatus.STOPPED
                                self.db.close_trade(
                                    trade_id=t["id"],
                                    status=time_exit_status.value,
                                    closed_at=datetime.now(timezone.utc).isoformat(),
                                    exit_price=exit_price,
                                    pnl_usdt=pnl,
                                    pnl_pct=pnl_pct,
                                    fees_usdt=fee_cost,
                                )
                                await self.telegram.send_message(
                                    f"⏱ *Time Exit* [PAPER]\n"
                                    f"{t['pair']} {t['direction']}\n"
                                    f"Held {hold_minutes:.0f}min > 45min\n"
                                    f"Exit: `${exit_price:,.2f}` | PnL: `${pnl:+.2f}` ({pnl_pct:+.1f}%)"
                                )
                                logger.info(f"[TIME EXIT] {t['pair']} held {hold_minutes:.0f}min, closed PnL=${pnl:+.2f}")
                                continue  # Đã close, skip hit_sl/hit_tp
                        except Exception as ex:
                            logger.warning(f"Time exit parse error for {t['pair']}: {ex}")

                    hit_sl = (direction == Direction.LONG and current_price <= t["stop_loss"]) or \
                             (direction == Direction.SHORT and current_price >= t["stop_loss"])
                    hit_tp = (direction == Direction.LONG and current_price >= t["take_profit"]) or \
                             (direction == Direction.SHORT and current_price <= t["take_profit"])

                    if hit_sl or hit_tp:
                        # Slippage thực tế: SL 0.1% bất lợi, TP 0.05% bất lợi
                        if hit_sl:
                            sl_slip = 0.001
                            exit_price = t["stop_loss"] * (1 - sl_slip) if direction == Direction.LONG else t["stop_loss"] * (1 + sl_slip)
                        else:
                            tp_slip = 0.0005
                            exit_price = t["take_profit"] * (1 - tp_slip) if direction == Direction.LONG else t["take_profit"] * (1 + tp_slip)
                        quantity = t["quantity"]

                        if direction == Direction.LONG:
                            pnl = (exit_price - t["entry_price"]) * quantity
                        else:
                            pnl = (t["entry_price"] - exit_price) * quantity

                        # Trừ fee (0.1% mỗi chiều = 0.2% round trip)
                        FEE_PCT = 0.001
                        fee_cost = t["position_size_usdt"] * FEE_PCT * 2
                        pnl = pnl - fee_cost

                        pnl_pct = pnl / t["position_size_usdt"] * 100
                        status = TradeStatus.STOPPED if hit_sl else TradeStatus.TOOK_PROFIT

                        self.db.close_trade(
                            trade_id=t["id"],
                            status=status.value,
                            closed_at=datetime.now(timezone.utc).isoformat(),
                            exit_price=exit_price,
                            pnl_usdt=pnl,
                            pnl_pct=pnl_pct,
                            fees_usdt=fee_cost,
                        )

                        emoji = "🛑" if hit_sl else "🎯"
                        await self.telegram.send_message(
                            f"{emoji} *Position Closed* [PAPER]\n"
                            f"{t['pair']} {t['direction']}\n"
                            f"Exit: `${exit_price:,.2f}`\n"
                            f"PnL: `${pnl:+.2f}` ({pnl_pct:+.1f}%)\n"
                            f"Reason: `{'Stop Loss' if hit_sl else 'Take Profit'}`"
                        )
                        logger.info(f"Position closed: {t['pair']} PnL=${pnl:+.2f} ({status.value})")

                except Exception as e:
                    logger.error(f"Monitor error for {t['pair']}: {e}")
        finally:
            await fetcher.close()

    async def _circuit_breaker_check(self):
        """Dừng toàn bộ nếu loss vượt giới hạn ngày. Resume khi qua ngày mới hoặc PnL hồi phục đủ."""
        today = datetime.now(timezone.utc).date()
        daily_pnl = self.db.get_daily_pnl()

        # Tính unrealized PnL từ open positions
        open_trades = self.db.get_open_trades()
        if open_trades and cfg.trading.paper_trading:
            from utils.market_data import BinanceDataFetcher
            fetcher = BinanceDataFetcher()
            try:
                for t in open_trades:
                    try:
                        cp = await fetcher.get_current_price(t["pair"])
                        direction = Direction(t["direction"])
                        if direction == Direction.LONG:
                            unrealized = (cp - t["entry_price"]) / t["entry_price"] * t["position_size_usdt"]
                        else:
                            unrealized = (t["entry_price"] - cp) / t["entry_price"] * t["position_size_usdt"]
                        daily_pnl += unrealized
                    except Exception:
                        pass
            finally:
                await fetcher.close()

        cumulative_pnl = self.db.get_cumulative_pnl()
        portfolio_value = cfg.trading.paper_balance_usdt + cumulative_pnl if cfg.trading.paper_trading else 10000.0 + cumulative_pnl
        max_loss = portfolio_value * cfg.trading.max_daily_loss_pct

        # Reset khi qua ngày mới (circuit breaker chỉ áp dụng trong ngày trigger)
        if self._circuit_breaker_triggered and self._circuit_breaker_date and today > self._circuit_breaker_date:
            self._circuit_breaker_triggered = False
            self._circuit_breaker_date = None
            self._circuit_breaker_triggered_at = None
            self.scheduler.resume_job("market_scan")
            self.scheduler.resume_job("smc_scan")
            logger.info("Circuit breaker: Resumed market scan (new day)")
            await self.telegram.send_message("✅ *Circuit breaker lifted* — New day, trading resumed.")
            return

        # Hysteresis: phải recover đến 50% max_loss VÀ chờ ít nhất 1 giờ
        recovery_threshold = -max_loss * 0.5
        if daily_pnl >= recovery_threshold:
            if self._circuit_breaker_triggered:
                elapsed = (datetime.now(timezone.utc) - self._circuit_breaker_triggered_at) if self._circuit_breaker_triggered_at else timedelta(hours=2)
                if elapsed >= timedelta(hours=1):
                    self._circuit_breaker_triggered = False
                    self._circuit_breaker_date = None
                    self.scheduler.resume_job("market_scan")
                    self.scheduler.resume_job("smc_scan")
                    logger.info("Circuit breaker: Resumed (PnL recovered past 50% threshold)")
                    await self.telegram.send_message("✅ *Circuit breaker lifted* — PnL recovered sufficiently.")
            return

        if daily_pnl < -max_loss:
            if not self._circuit_breaker_triggered:
                self._circuit_breaker_triggered = True
                self._circuit_breaker_date = today
                self._circuit_breaker_triggered_at = datetime.now(timezone.utc)
                self.scheduler.pause_job("market_scan")
                self.scheduler.pause_job("smc_scan")
                logger.warning(f"CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} exceeded limit ${-max_loss:.2f}")
                await self.telegram.send_message(
                    f"🚨 *CIRCUIT BREAKER TRIGGERED*\n"
                    f"Daily loss: `${daily_pnl:.2f}`\n"
                    f"Limit: `-${max_loss:.2f}`\n\n"
                    f"Trading PAUSED for today. All new signals will be rejected."
                )
                # Emergency close all open positions
                for t in self.db.get_open_trades():
                    trade = Trade(
                        id=t["id"],
                        signal_id=t["signal_id"],
                        pair=t["pair"],
                        direction=Direction(t["direction"]),
                        entry_price=t["entry_price"],
                        stop_loss=t["stop_loss"],
                        take_profit=t["take_profit"],
                        quantity=t["quantity"],
                        position_size_usdt=t["position_size_usdt"],
                        binance_order_id=t.get("binance_order_id"),
                        status=TradeStatus.OPEN,
                        opened_at=datetime.fromisoformat(t["opened_at"]) if t.get("opened_at") else datetime.now(timezone.utc),
                        is_paper=bool(t.get("is_paper", 1)),
                    )
                    try:
                        ok = await self.executor.close_trade_market(trade)
                        if ok:
                            await self.telegram.send_message(
                                f"🛑 *Emergency close* — {trade.pair} {trade.direction.value} (circuit breaker)"
                            )
                    except Exception as e:
                        logger.error(f"Emergency close failed for {trade.pair}: {e}")

    async def _daily_report(self):
        """Gửi báo cáo hàng ngày lúc 8h"""
        await self.telegram.send_daily_report(self.db)

    async def _heartbeat(self):
        """Heartbeat mỗi 6 giờ — biết bot còn sống"""
        open_count = len(self.db.get_open_trades())
        await self.telegram.send_message(
            f"💓 *Heartbeat* — Bot running\n"
            f"Open positions: `{open_count}`"
        )

    async def stop(self):
        """Graceful shutdown"""
        logger.info("Shutting down...")
        self._running = False
        self.scheduler.shutdown()
        # Web thread là daemon nên tự tắt khi main exit
        await self.research.close()
        await self.smc_agent.close()
        await self.telegram.stop()
        self.db.close()
        logger.info("Trading Agent stopped")


# ─── Entry point ─────────────────────────────────────────────────────────────

async def main():
    orchestrator = TradingOrchestrator()

    # Graceful shutdown khi nhận SIGINT/SIGTERM (chỉ trên Unix/WSL, không hỗ trợ Windows)
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        for sig in (sys_signal.SIGINT, sys_signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(orchestrator.stop()),
                )
            except NotImplementedError:
                pass

    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
