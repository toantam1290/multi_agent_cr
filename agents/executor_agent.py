"""
agents/executor_agent.py - Đặt lệnh Binance sau khi user approve
"""
import math
import uuid
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from config import cfg
from models import TradingSignal, Trade, TradeStatus, Direction, SignalStatus
from database import Database


class ExecutorAgent:
    """
    Executor Agent - chỉ chạy SAU khi user approve qua Telegram
    Hỗ trợ cả paper trading và real trading
    """

    def __init__(self, db: Database):
        self.db = db
        self._binance_client = None
        logger.info(f"ExecutorAgent initialized | Paper trading: {cfg.trading.paper_trading}")

    def _get_binance_client(self):
        """Lazy init Binance client"""
        if self._binance_client is None and not cfg.trading.paper_trading:
            from binance.client import Client
            self._binance_client = Client(
                cfg.binance.api_key,
                cfg.binance.api_secret,
                testnet=cfg.binance.testnet,
            )
        return self._binance_client

    async def execute(self, signal: TradingSignal) -> Optional[Trade]:
        """
        Execute signal → tạo Trade
        """
        logger.info(
            f"Executing signal: {signal.pair} {signal.direction.value} "
            f"@ ${signal.entry_price:,.2f} | Size: ${signal.position_size_usdt:,.0f}"
        )

        if cfg.trading.paper_trading:
            trade = await self._paper_execute(signal)
        else:
            trade = await self._real_execute(signal)

        if trade:
            self.db.save_trade(trade)
            self.db.update_signal_status(signal.id, SignalStatus.EXECUTED)
            self.db.log("executor_agent", "INFO",
                        f"Trade executed: {signal.pair} {signal.direction.value}",
                        {"trade_id": trade.id, "signal_id": signal.id})
            logger.success(f"Trade saved: {trade.id}")

        return trade

    async def _paper_execute(self, signal: TradingSignal) -> Trade:
        """
        Paper trading - dùng signal.entry_price (pullback level) để maintain R:R.
        Scalp: entry = pullback level → fill tại đó (simulate limit order).
        Swing: entry = market → fill tại current + slippage.
        """
        from config import cfg
        from utils.market_data import BinanceDataFetcher
        fetcher = BinanceDataFetcher()
        try:
            current_price = await fetcher.get_current_price(signal.pair)
        finally:
            await fetcher.close()

        SLIPPAGE_PCT = 0.0015  # 0.15% mỗi chiều (market)
        LIMIT_SLIPPAGE_PCT = 0.0005  # 0.05% (limit fill)
        FEE_PCT = 0.001  # 0.1% mỗi chiều → 0.2% round trip

        # Scalp pullback entry: fill tại signal.entry_price (limit order simulated)
        # Nếu giá đã vượt entry > 0.2% → limit không fill → simulate no fill (return None)
        if cfg.scan.trading_style == "scalp":
            if signal.direction == Direction.LONG and current_price > signal.entry_price * 1.002:
                logger.warning(f"[PAPER] Scalp LONG: price ${current_price:,.2f} > entry*1.002 — no fill (limit missed)")
                return None
            if signal.direction == Direction.SHORT and current_price < signal.entry_price * 0.998:
                logger.warning(f"[PAPER] Scalp SHORT: price ${current_price:,.2f} < entry*0.998 — no fill (limit missed)")
                return None
            if signal.direction == Direction.LONG:
                filled_price = signal.entry_price * (1 + LIMIT_SLIPPAGE_PCT)
            else:
                filled_price = signal.entry_price * (1 - LIMIT_SLIPPAGE_PCT)
            fill_type = "limit (pullback)"
        else:
            if signal.direction == Direction.LONG:
                filled_price = current_price * (1 + SLIPPAGE_PCT)
            else:
                filled_price = current_price * (1 - SLIPPAGE_PCT)
            fill_type = "market"
        quantity = signal.position_size_usdt / filled_price
        fee_cost = signal.position_size_usdt * FEE_PCT * 2  # Cả 2 chiều

        trade = Trade(
            id=str(uuid.uuid4()),
            signal_id=signal.id,
            pair=signal.pair,
            direction=signal.direction,
            entry_price=filled_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            quantity=quantity,
            position_size_usdt=signal.position_size_usdt,
            binance_order_id=f"PAPER_{uuid.uuid4().hex[:8].upper()}",
            status=TradeStatus.OPEN,
            fees_usdt=fee_cost,
            is_paper=True,
        )

        drift_pct = abs(filled_price - signal.entry_price) / signal.entry_price * 100 if signal.entry_price > 0 else 0
        if drift_pct > 0.2:
            logger.warning(f"[PAPER] Fill drift {drift_pct:.2f}% vs signal entry")
        logger.info(
            f"[PAPER] Trade opened: {signal.direction.value} {quantity:.6f} {signal.pair} "
            f"@ ${filled_price:,.2f} ({fill_type})"
        )
        return trade

    async def _real_execute(self, signal: TradingSignal) -> Optional[Trade]:
        """Real trading - đặt lệnh Binance thực. OCO chưa fix → guard."""
        raise NotImplementedError(
            "Live disabled until OCO fixed. Set PAPER_TRADING=true."
        )
        client = self._get_binance_client()
        if not client:
            logger.error("Binance client not initialized!")
            return None

        try:
            # Lấy symbol info để làm tròn quantity
            info = client.get_symbol_info(signal.pair)
            step_size = self._get_step_size(info)
            quantity = self._round_quantity(
                signal.position_size_usdt / signal.entry_price,
                step_size
            )

            # 1. Đặt lệnh entry (LIMIT)
            side = "BUY" if signal.direction == Direction.LONG else "SELL"
            entry_order = client.create_order(
                symbol=signal.pair,
                side=side,
                type="LIMIT",
                timeInForce="GTC",
                quantity=quantity,
                price=str(round(signal.entry_price, 2)),
            )
            logger.info(f"Entry order placed: {entry_order['orderId']}")

            # 2. Đặt stop-loss ngay (OCO hoặc STOP_LOSS_LIMIT)
            sl_side = "SELL" if signal.direction == Direction.LONG else "BUY"
            sl_price = signal.stop_loss * (0.999 if signal.direction == Direction.LONG else 1.001)

            sl_order = client.create_order(
                symbol=signal.pair,
                side=sl_side,
                type="STOP_LOSS_LIMIT",
                timeInForce="GTC",
                quantity=quantity,
                stopPrice=str(round(signal.stop_loss, 2)),
                price=str(round(sl_price, 2)),
            )
            logger.info(f"Stop-loss order placed: {sl_order['orderId']}")

            # 3. Đặt take-profit
            tp_order = client.create_order(
                symbol=signal.pair,
                side=sl_side,
                type="LIMIT",
                timeInForce="GTC",
                quantity=quantity,
                price=str(round(signal.take_profit, 2)),
            )
            logger.info(f"Take-profit order placed: {tp_order['orderId']}")

            trade = Trade(
                id=str(uuid.uuid4()),
                signal_id=signal.id,
                pair=signal.pair,
                direction=signal.direction,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                quantity=quantity,
                position_size_usdt=signal.position_size_usdt,
                binance_order_id=str(entry_order["orderId"]),
                status=TradeStatus.OPEN,
                is_paper=False,
            )
            return trade

        except Exception as e:
            logger.error(f"Binance execution error: {e}")
            self.db.log("executor_agent", "ERROR", f"Execution failed: {e}",
                        {"signal_id": signal.id, "pair": signal.pair})
            return None

    async def close_trade_market(self, trade: Trade) -> bool:
        """
        Đóng vị thế theo giá market (circuit breaker / emergency)
        """
        logger.warning(f"EMERGENCY CLOSE: {trade.pair} {trade.id}")

        if cfg.trading.paper_trading:
            from utils.market_data import BinanceDataFetcher
            fetcher = BinanceDataFetcher()
            try:
                current_price = await fetcher.get_current_price(trade.pair)
                pnl = self._calc_pnl(trade, current_price)
                FEE_PCT = 0.001
                fee_cost = trade.position_size_usdt * FEE_PCT * 2
                pnl = pnl - fee_cost
                pnl_pct = pnl / trade.position_size_usdt * 100 if trade.position_size_usdt else 0
                self.db.close_trade(
                    trade_id=trade.id,
                    status=TradeStatus.CLOSED.value,
                    closed_at=datetime.now(timezone.utc).isoformat(),
                    exit_price=current_price,
                    pnl_usdt=pnl,
                    pnl_pct=pnl_pct,
                    fees_usdt=fee_cost,
                )
                logger.info(f"[PAPER] Emergency close: PnL ${pnl:.2f}")
                return True
            finally:
                await fetcher.close()

        # Real: market sell
        try:
            client = self._get_binance_client()
            side = "SELL" if trade.direction == Direction.LONG else "BUY"
            client.create_order(
                symbol=trade.pair,
                side=side,
                type="MARKET",
                quantity=trade.quantity,
            )
            return True
        except Exception as e:
            logger.error(f"Emergency close failed: {e}")
            return False

    def _calc_pnl(self, trade: Trade, exit_price: float) -> float:
        if trade.direction == Direction.LONG:
            return (exit_price - trade.entry_price) * trade.quantity
        return (trade.entry_price - exit_price) * trade.quantity

    def _get_step_size(self, symbol_info: dict) -> float:
        for f in symbol_info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                return float(f["stepSize"])
        return 0.001

    def _round_quantity(self, qty: float, step_size: float) -> float:
        if step_size == 0:
            return round(qty, 6)
        precision = int(round(-math.log(step_size, 10), 0))
        return round(qty - (qty % step_size), precision)
