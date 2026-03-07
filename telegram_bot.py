"""
telegram_bot.py - Telegram bot để giao tiếp với user
Nhận /approve và /skip commands
"""
import asyncio
from datetime import datetime
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from loguru import logger

from config import cfg
from models import TradingSignal, SignalStatus, Trade
from database import Database


class TelegramNotifier:
    """
    Gửi notifications và nhận commands từ user
    """

    def __init__(self, db: Database, on_approve_callback=None):
        self.db = db
        self.bot_token = cfg.telegram.bot_token
        self.chat_id = cfg.telegram.chat_id
        self.on_approve = on_approve_callback  # Callback khi user approve
        self._bot = Bot(token=self.bot_token) if self.bot_token else None
        self._app = None
        self._pending_signals: dict[str, TradingSignal] = {}  # short_id → signal

    async def send_signal_alert(self, signal: TradingSignal) -> bool:
        """Gửi signal alert đến user, chờ approve/skip"""
        if not self._bot:
            logger.warning("Telegram bot not configured, printing to console")
            print("\n" + signal.to_telegram_message())
            return True

        # Lưu signal đang chờ
        short_id = signal.id[:8]
        self._pending_signals[short_id] = signal

        try:
            msg = await self._bot.send_message(
                chat_id=self.chat_id,
                text=signal.to_telegram_message(),
                parse_mode="Markdown",
            )
            signal.telegram_message_id = msg.message_id
            self.db.save_signal(signal)

            logger.info(f"Signal alert sent to Telegram: {signal.pair} (msg_id: {msg.message_id})")

            # Auto-expire sau timeout
            asyncio.create_task(
                self._auto_expire_signal(short_id, cfg.trading.approval_timeout_sec)
            )
            return True

        except Exception as e:
            self._pending_signals.pop(short_id, None)  # Cleanup on failure (ghost signal)
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    async def _auto_expire_signal(self, short_id: str, timeout_sec: int):
        """Tự động expire signal nếu không có response"""
        await asyncio.sleep(timeout_sec)
        if short_id in self._pending_signals:
            signal = self._pending_signals.pop(short_id)
            self.db.update_signal_status(signal.id, SignalStatus.SKIPPED)
            logger.info(f"Signal expired (timeout): {signal.pair} {signal.id[:8]}")
            await self.send_message(
                f"⏰ Signal expired: *{signal.pair}* {signal.direction.value} "
                f"(no response in {timeout_sec//60} min)"
            )

    async def send_trade_result(self, trade: Trade):
        """Báo kết quả trade"""
        if trade.pnl_usdt is None:
            return

        emoji = "✅" if trade.pnl_usdt > 0 else "❌"
        paper_tag = " [PAPER]" if trade.is_paper else ""
        msg = (
            f"{emoji} *Trade Closed*{paper_tag}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Pair: *{trade.pair}*\n"
            f"Direction: `{trade.direction.value}`\n"
            f"Entry: `${trade.entry_price:,.2f}`\n"
            f"Exit: `${trade.exit_price:,.2f}`\n"
            f"PnL: `${trade.pnl_usdt:+.2f}` ({trade.pnl_pct:+.1f}%)\n"
            f"Status: `{trade.status.value}`"
        )
        await self.send_message(msg)

    async def send_daily_report(self, db: Database):
        """Báo cáo hàng ngày"""
        stats = db.get_stats()
        daily_pnl = db.get_daily_pnl()
        open_trades = db.get_open_trades()

        paper_tag = " [PAPER TRADING]" if cfg.trading.paper_trading else ""
        msg = (
            f"📊 *Daily Report*{paper_tag}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📈 Today PnL: `${daily_pnl:+.2f}`\n"
            f"🔢 Open Positions: `{len(open_trades)}`\n"
            f"🏆 All-time Win Rate: `{stats['win_rate']:.1f}%`\n"
            f"💰 All-time PnL: `${stats['total_pnl_usdt']:+.2f}`\n"
            f"📝 Total Trades: `{stats['total_trades']}`"
        )
        await self.send_message(msg)

    async def send_message(self, text: str):
        """Send plain message"""
        if not self._bot:
            print(f"\n[TELEGRAM] {text}")
            return
        try:
            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    # ─── Command Handlers ────────────────────────────────────────────────────

    async def _cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /approve_XXXXXXXX"""
        if str(update.effective_chat.id) != self.chat_id:
            return  # Ignore messages not from owner

        args = context.args
        if not args:
            await update.message.reply_text("Usage: /approve <signal_id>")
            return

        short_id = args[0].strip()
        signal_data = self.db.get_signal_by_short_id(short_id)

        if not signal_data:
            await update.message.reply_text(f"❌ Signal not found: {short_id}")
            return

        # Lấy từ _pending_signals hoặc DB (recovery sau restart)
        if short_id in self._pending_signals:
            signal = self._pending_signals.pop(short_id)
        elif signal_data.get("status") == "PENDING":
            signal = TradingSignal.model_validate(signal_data)
        else:
            await update.message.reply_text(f"⚠️ Signal {short_id} already processed or expired")
            return
        self.db.update_signal_status(signal.id, SignalStatus.APPROVED)

        await update.message.reply_text(
            f"✅ *Approved!* Executing {signal.pair} {signal.direction.value}...",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Trigger execution
        if self.on_approve:
            task = asyncio.create_task(self.on_approve(signal))

            def _done(t):
                if t.cancelled():
                    return
                exc = t.exception()
                if exc:
                    logger.error(f"Approval callback error: {exc}")

            task.add_done_callback(_done)

        logger.info(f"Signal approved by user: {signal.pair} {signal.id[:8]}")

    async def _cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /skip_XXXXXXXX"""
        if str(update.effective_chat.id) != self.chat_id:
            return

        args = context.args
        if not args:
            await update.message.reply_text("Usage: /skip <signal_id>")
            return

        short_id = args[0].strip()

        signal_data = self.db.get_signal_by_short_id(short_id)
        if short_id in self._pending_signals:
            signal = self._pending_signals.pop(short_id)
            self.db.update_signal_status(signal.id, SignalStatus.SKIPPED)
            await update.message.reply_text(f"⏭️ Skipped: {signal.pair}")
            logger.info(f"Signal skipped: {signal.pair} {signal.id[:8]}")
        elif signal_data and signal_data.get("status") == "PENDING":
            signal = TradingSignal.model_validate(signal_data)
            self.db.update_signal_status(signal.id, SignalStatus.SKIPPED)
            await update.message.reply_text(f"⏭️ Skipped: {signal.pair}")
            logger.info(f"Signal skipped (from DB): {signal.pair} {signal.id[:8]}")
        else:
            await update.message.reply_text(f"⚠️ Signal {short_id} not found or already processed")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status"""
        if str(update.effective_chat.id) != self.chat_id:
            return

        open_trades = self.db.get_open_trades()
        stats = self.db.get_stats()
        daily_pnl = self.db.get_daily_pnl()

        if not open_trades:
            positions_text = "No open positions"
        else:
            positions_text = "\n".join([
                f"  • {t['pair']} {t['direction']} @ ${t['entry_price']:,.2f}"
                for t in open_trades
            ])

        paper = " [PAPER]" if cfg.trading.paper_trading else ""
        await update.message.reply_text(
            f"📊 *Bot Status*{paper}\n"
            f"━━━━━━━━━━━━━━\n"
            f"Open Positions ({len(open_trades)}):\n{positions_text}\n\n"
            f"Today PnL: `${daily_pnl:+.2f}`\n"
            f"Win Rate: `{stats['win_rate']:.1f}%`\n"
            f"Total Trades: `{stats['total_trades']}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Xem signals đang chờ approve"""
        if str(update.effective_chat.id) != self.chat_id:
            return

        if not self._pending_signals:
            await update.message.reply_text("No pending signals")
            return

        text = f"⏳ *{len(self._pending_signals)} Pending Signals:*\n"
        for short_id, sig in self._pending_signals.items():
            text += f"  • `{short_id}` — {sig.pair} {sig.direction.value} (confidence: {sig.confidence})\n"

        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def start_polling(self):
        """Start Telegram bot polling"""
        if not self.bot_token:
            logger.warning("Telegram bot token not set, skipping polling")
            return

        # Xóa webhook trước (fix Conflict khi bot từng dùng webhook hoặc token bị dùng ở nơi khác)
        try:
            await self._bot.delete_webhook(drop_pending_updates=True)
            logger.debug("Telegram webhook cleared")
        except Exception as e:
            logger.warning(f"Could not clear webhook (may be ok): {e}")

        self._app = Application.builder().token(self.bot_token).build()
        self._app.add_handler(CommandHandler("approve", self._cmd_approve))
        self._app.add_handler(CommandHandler("skip", self._cmd_skip))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))

        logger.info("Telegram bot started polling...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
