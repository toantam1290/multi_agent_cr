"""
agents/risk_manager.py - Kiểm tra risk trước khi forward signal đến user
"""
from loguru import logger
from config import cfg, get_effective_min_confidence, get_effective_min_risk_reward
from models import TradingSignal, PortfolioState, SignalStatus
from database import Database


class RiskRejectionReason:
    DAILY_LOSS_EXCEEDED = "Daily loss limit exceeded"
    TOO_MANY_POSITIONS = "Too many open positions"
    CORRELATION_RISK = "Correlation risk: too many same-direction positions"
    LOW_CONFIDENCE = "Confidence below minimum threshold"
    LOW_RISK_REWARD = "Risk:Reward ratio too low"
    DUPLICATE_PAIR = "Already have open position on this pair"
    POSITION_TOO_LARGE = "Position size too large for portfolio"


class RiskManagerAgent:
    """
    Risk Manager - gate keeper trước khi signal đến user
    Tất cả checks phải pass → forward signal
    Một check fail → reject
    """

    def __init__(self, db: Database):
        self.db = db
        self.cfg = cfg.trading
        logger.info("RiskManagerAgent initialized")

    def validate(self, signal: TradingSignal, portfolio: PortfolioState) -> tuple[bool, str]:
        """
        Validate signal against risk rules.
        Returns: (is_valid, reason)
        """
        checks = [
            self._check_daily_loss(portfolio),
            self._check_open_positions(portfolio),
            self._check_correlation(signal, portfolio),
            self._check_confidence(signal),
            self._check_risk_reward(signal),
            self._check_duplicate_pair(signal, portfolio),
            self._check_position_size(signal, portfolio),
        ]

        for passed, reason in checks:
            if not passed:
                logger.warning(f"Risk check failed: {reason} | Signal: {signal.pair} {signal.direction.value}")
                self.db.log("risk_manager", "WARNING",
                            f"Signal rejected: {reason}",
                            {"signal_id": signal.id, "pair": signal.pair})
                return False, reason

        logger.info(f"Risk checks passed for {signal.pair} {signal.direction.value}")
        return True, "OK"

    def _check_daily_loss(self, portfolio: PortfolioState) -> tuple[bool, str]:
        """Dừng trading nếu đã lỗ quá giới hạn ngày"""
        max_loss = portfolio.total_usdt * self.cfg.max_daily_loss_pct
        if portfolio.daily_pnl_usdt < -max_loss:
            return False, (
                f"{RiskRejectionReason.DAILY_LOSS_EXCEEDED}: "
                f"${portfolio.daily_pnl_usdt:.0f} (limit: -${max_loss:.0f})"
            )
        return True, ""

    def _check_open_positions(self, portfolio: PortfolioState) -> tuple[bool, str]:
        """Không mở quá nhiều vị thế cùng lúc"""
        if portfolio.open_position_count >= self.cfg.max_open_positions:
            return False, (
                f"{RiskRejectionReason.TOO_MANY_POSITIONS}: "
                f"{portfolio.open_position_count}/{self.cfg.max_open_positions}"
            )
        return True, ""

    def _check_correlation(self, signal: TradingSignal, portfolio: PortfolioState) -> tuple[bool, str]:
        """Không quá N vị thế cùng hướng — tránh over-exposure khi BTC dump"""
        if not portfolio.open_trades:
            return True, ""
        max_same_dir = self.cfg.max_open_positions // 2  # 50% mỗi hướng
        same_dir = sum(1 for t in portfolio.open_trades if t.direction.value == signal.direction.value)
        if same_dir >= max_same_dir:
            return False, (
                f"{RiskRejectionReason.CORRELATION_RISK}: "
                f"already {same_dir}/{max_same_dir} {signal.direction.value} positions open"
            )
        return True, ""

    def _check_confidence(self, signal: TradingSignal) -> tuple[bool, str]:
        """Confidence phải đủ cao (scalp: 80, swing: 75)"""
        min_conf = get_effective_min_confidence()
        if signal.confidence < min_conf:
            return False, (
                f"{RiskRejectionReason.LOW_CONFIDENCE}: "
                f"{signal.confidence} < {min_conf}"
            )
        return True, ""

    def _check_risk_reward(self, signal: TradingSignal) -> tuple[bool, str]:
        """R:R tối thiểu (scalp: 1.5, swing: 2.0)"""
        min_rr = get_effective_min_risk_reward()
        if signal.risk_reward < min_rr:
            return False, (
                f"{RiskRejectionReason.LOW_RISK_REWARD}: "
                f"1:{signal.risk_reward:.1f} < 1:{min_rr}"
            )
        return True, ""

    def _check_duplicate_pair(self, signal: TradingSignal, portfolio: PortfolioState) -> tuple[bool, str]:
        """Không có 2 vị thế trên cùng 1 pair"""
        open_pairs = {t.pair for t in portfolio.open_trades}
        if signal.pair in open_pairs:
            return False, f"{RiskRejectionReason.DUPLICATE_PAIR}: {signal.pair}"
        return True, ""

    def _check_position_size(self, signal: TradingSignal, portfolio: PortfolioState) -> tuple[bool, str]:
        """Position size không quá lớn so với available balance"""
        if signal.position_size_usdt > portfolio.available_usdt:
            return False, (
                f"{RiskRejectionReason.POSITION_TOO_LARGE}: "
                f"${signal.position_size_usdt:.0f} > available ${portfolio.available_usdt:.0f}"
            )
        # Cũng check vs max portfolio pct
        max_size = portfolio.total_usdt * self.cfg.max_position_pct
        if signal.position_size_usdt > max_size * 1.005:  # 0.5% tolerance — tránh lỗi floating point
            return False, (
                f"{RiskRejectionReason.POSITION_TOO_LARGE}: "
                f"${signal.position_size_usdt:.0f} > {self.cfg.max_position_pct*100}% of portfolio"
            )
        # Cảnh báo ATR-normalized risk: risk thực tế = (entry - SL) / entry * position_size
        if signal.entry_price and signal.stop_loss:
            sl_distance_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
            actual_risk_usdt = signal.position_size_usdt * sl_distance_pct
            max_risk_usdt = portfolio.total_usdt * 0.01  # Max 1% equity per trade
            if actual_risk_usdt > max_risk_usdt * 1.2:
                logger.warning(
                    f"Risk thực tế ${actual_risk_usdt:.2f} USDT vượt 1% equity "
                    f"(${max_risk_usdt:.2f} USDT) — SL quá rộng so với position size"
                )
        return True, ""

    def get_portfolio_state(self) -> PortfolioState:
        """Build portfolio state từ database"""
        from datetime import date
        from models import Trade, TradeStatus, Direction

        open_trade_dicts = self.db.get_open_trades()
        open_trades = []
        total_locked = 0.0

        for t in open_trade_dicts:
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
                status=TradeStatus(t["status"]),
                is_paper=bool(t["is_paper"]),
            )
            open_trades.append(trade)
            total_locked += t["position_size_usdt"]

        stats = self.db.get_stats()
        daily_pnl = self.db.get_daily_pnl()

        cumulative_pnl = self.db.get_cumulative_pnl()
        if cfg.trading.paper_trading:
            total = cfg.trading.paper_balance_usdt + cumulative_pnl
        else:
            total = 10000.0 + cumulative_pnl  # TODO: fetch base from Binance

        return PortfolioState(
            total_usdt=total,
            available_usdt=max(0, total - total_locked),
            open_trades=open_trades,
            daily_pnl_usdt=daily_pnl,
            daily_pnl_pct=daily_pnl / total * 100 if total > 0 else 0,
            total_pnl_usdt=stats["total_pnl_usdt"],
            win_rate=stats["win_rate"],
            total_trades=stats["total_trades"],
            winning_trades=stats["winning_trades"],
        )
