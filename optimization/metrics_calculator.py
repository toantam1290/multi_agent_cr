"""
optimization/metrics_calculator.py — Metrics Calculator

Chuyển BacktestResult (multi_agent_cr) sang MetricsResult chuẩn.
Tính Calmar, kiểm tra targets (PF, WR, Sharpe, MDD, AvgRR).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backtest import BacktestResult


@dataclass
class MetricsResult:
    """Kết quả metrics chuẩn cho optimization."""
    profit_factor: float
    win_rate: float  # 0-1
    sharpe_ratio: float
    max_drawdown_pct: float
    avg_rr: float
    calmar_ratio: float
    total_trades: int
    total_return_pct: float


# Target mặc định theo REQUEST.md
DEFAULT_TARGETS = {
    "profit_factor": 1.3,
    "win_rate": 0.38,
    "sharpe": 1.0,
    "max_drawdown_pct": 15.0,  # 15%
    "avg_rr": 1.8,
    "min_trades_is": 100,
    "min_trades_oos": 30,
    "calmar": 0.8,
}


class MetricsCalculator:
    """Chuyển BacktestResult → MetricsResult, kiểm tra targets."""

    def calculate(self, result: BacktestResult) -> MetricsResult:
        trades = result.trades
        if not trades:
            return MetricsResult(
                profit_factor=0.0,
                win_rate=0.0,
                sharpe_ratio=0.0,
                max_drawdown_pct=result.max_drawdown_pct,
                avg_rr=0.0,
                calmar_ratio=0.0,
                total_trades=0,
                total_return_pct=0.0,
            )

        wr_decimal = result.win_rate / 100.0  # backtest dùng 0-100
        total_ret = result.total_pnl_pct

        # Calmar = annualized return / max_drawdown
        calmar = 0.0
        if result.max_drawdown_pct > 0 and trades:
            first_ts = trades[0].entry_time.timestamp()
            last_t = trades[-1].exit_time or trades[-1].entry_time
            last_ts = last_t.timestamp()
            years = max(0.25, (last_ts - first_ts) / (365.25 * 24 * 3600))
            ann_ret = ((1 + total_ret / 100) ** (1 / years) - 1) if years > 0 else total_ret / 100
            calmar = ann_ret / (result.max_drawdown_pct / 100)

        return MetricsResult(
            profit_factor=result.profit_factor,
            win_rate=wr_decimal,
            sharpe_ratio=result.sharpe_ratio,
            max_drawdown_pct=result.max_drawdown_pct,
            avg_rr=result.avg_rr,
            calmar_ratio=calmar,
            total_trades=len(trades),
            total_return_pct=total_ret,
        )

    def meets_targets(
        self,
        metrics: MetricsResult,
        min_trades: Optional[int] = None,
    ) -> bool:
        mt = min_trades if min_trades is not None else DEFAULT_TARGETS["min_trades_is"]
        return (
            metrics.profit_factor >= DEFAULT_TARGETS["profit_factor"]
            and metrics.win_rate >= DEFAULT_TARGETS["win_rate"]
            and metrics.sharpe_ratio >= DEFAULT_TARGETS["sharpe"]
            and metrics.max_drawdown_pct <= DEFAULT_TARGETS["max_drawdown_pct"]
            and metrics.avg_rr >= DEFAULT_TARGETS["avg_rr"]
            and metrics.total_trades >= mt
            and metrics.calmar_ratio >= DEFAULT_TARGETS["calmar"]
        )
