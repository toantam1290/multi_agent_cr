"""
optimization/walk_forward.py — Walk-Forward Validator

SPEC: 4 windows trên in-sample, pass 3/4.
OOS lock: không điều chỉnh sau khi optimization xong.
Tích hợp với multi_agent_cr backtest (download_all_data, run_backtest_combined, calc_stats).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from backtest import (
    BacktestConfig,
    calc_stats,
    download_all_data,
    run_backtest_combined,
    run_smc_backtest_for_symbol,
)
from optimization.metrics_calculator import MetricsCalculator, MetricsResult


@dataclass
class WFWindowResult:
    """Kết quả 1 window."""
    start: datetime
    end: datetime
    metrics: MetricsResult
    passed: bool


@dataclass
class WalkForwardResult:
    """Kết quả WFO."""
    windows: List[WFWindowResult]
    passed_count: int
    total_windows: int
    passed: bool  # pass 3/4


class WalkForwardValidator:
    """
    True WFO: mỗi window chia train/test theo train_ratio.
    Train: optimize (hiện chưa có) — test: validate trên OOS portion của window.
    Pass nếu >= 3/4 windows đạt target trên test period.
    """

    def __init__(
        self,
        n_windows: int = 4,
        train_ratio: float = 0.7,
        min_trades_oos: int = 30,
    ):
        self.n_windows = n_windows
        self.train_ratio = train_ratio
        self.min_trades_oos = min_trades_oos
        self.calculator = MetricsCalculator()

    async def run(
        self,
        config: BacktestConfig,
        use_smc_standalone: bool = True,
    ) -> WalkForwardResult:
        """
        Chạy WFO: mỗi window split train/test theo train_ratio.
        Validate trên test period (phần cuối của window).
        """
        date_from = config.date_from
        date_to = config.date_to
        delta = (date_to - date_from).days
        window_days = max(1, delta // self.n_windows)
        windows: List[WFWindowResult] = []

        # Download data once for full period
        all_data = await download_all_data(config, use_cache=True)

        for i in range(self.n_windows):
            w_start = date_from + timedelta(days=i * window_days)
            w_end = date_from + timedelta(days=(i + 1) * window_days - 1)
            w_days = (w_end - w_start).days + 1
            test_start = w_start + timedelta(days=int(w_days * self.train_ratio))

            cfg = BacktestConfig(
                symbols=config.symbols,
                style=config.style,
                date_from=test_start,
                date_to=w_end,
                use_smc_standalone=use_smc_standalone,
                use_rule_filter=config.use_rule_filter,
                use_smc_filter=config.use_smc_filter,
                use_confluence_filter=config.use_confluence_filter,
                use_session_filter=config.use_session_filter,
                use_trail_stop=config.use_trail_stop,
                confluence_threshold=config.confluence_threshold,
                scalp_rr=config.scalp_rr,
                smc_min_rr_tp1=config.smc_min_rr_tp1,
                smc_min_confidence=config.smc_min_confidence,
                smc_sl_buffer_pct=config.smc_sl_buffer_pct,
                smc_use_chop_filter=config.smc_use_chop_filter,
                smc_use_funding_filter=config.smc_use_funding_filter,
            )

            if use_smc_standalone:
                trades = []
                for sym in config.symbols:
                    t = run_smc_backtest_for_symbol(
                        sym, all_data[sym], cfg, test_start, w_end, verbose=False
                    )
                    trades.extend(t)
            else:
                trades = run_backtest_combined(
                    config.symbols, all_data, cfg, test_start, w_end, verbose=False
                )

            result = calc_stats(trades, cfg, test_start, w_end)
            met = self.calculator.calculate(result)
            passed = self.calculator.meets_targets(met, min_trades=self.min_trades_oos)
            windows.append(WFWindowResult(start=test_start, end=w_end, metrics=met, passed=passed))

        passed_count = sum(1 for w in windows if w.passed)
        return WalkForwardResult(
            windows=windows,
            passed_count=passed_count,
            total_windows=self.n_windows,
            passed=passed_count >= 3,
        )
