"""
optimization/improvement_engine.py — Improvement Engine

Mỗi iteration: phân tích bottleneck → chọn 1 thay đổi expected impact cao → implement → so sánh.
Dừng khi đạt toàn bộ target hoặc sau max_iterations.
Tích hợp với multi_agent_cr backtest (BacktestConfig, run_backtest_combined, calc_stats).
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

from backtest import (
    BacktestConfig,
    calc_stats,
    download_all_data,
    run_backtest_combined,
    run_smc_backtest_for_symbol,
)
from optimization.metrics_calculator import MetricsCalculator, MetricsResult
from optimization.change_registry import ChangeRecord, ChangeRegistry


@dataclass
class ImprovementState:
    """Trạng thái hiện tại của improvement loop."""
    iteration: int
    metrics: MetricsResult
    best_metrics: Optional[MetricsResult]
    best_config: Optional[BacktestConfig]
    targets_met: bool


# SMC standalone: params mà SMCStrategy thực sự dùng
PARAM_CANDIDATES_SMC: List[Tuple[str, List[Any]]] = [
    ("smc_min_rr_tp1", [1.2, 1.5, 1.8, 2.0]),
    ("smc_min_confidence", [40, 50, 60]),
]

# Rule-based: confluence, RR
PARAM_CANDIDATES_RULE: List[Tuple[str, List[Any]]] = [
    ("confluence_threshold", [2, 3, 4]),
    ("scalp_rr", [1.6, 1.8, 2.0, 2.2]),
    ("swing_rr", [1.6, 2.0, 2.5]),
]


class ImprovementEngine:
    """
    Vòng lặp cải thiện: backtest → analyze → change 1 param → backtest → compare.
    Dùng BacktestConfig thay vì BacktestEngine.
    """

    def __init__(self, max_iterations: int = 15):
        self.calculator = MetricsCalculator()
        self.registry = ChangeRegistry()
        self.max_iterations = max_iterations
        self._value_idx: Dict[int, int] = {}

    def _bottleneck_candidate(self, met: MetricsResult, candidates: List) -> int:
        """Chọn param nào cần tune dựa trên bottleneck."""
        if met.avg_rr < 1.5 or met.profit_factor < 1.2:
            return 1 % len(candidates)
        if met.win_rate < 0.38:
            return 0
        return 2 % len(candidates)

    def _run_backtest(
        self,
        config: BacktestConfig,
        all_data: dict,
        use_smc_standalone: bool,
    ) -> MetricsResult:
        date_from = config.date_from
        date_to = config.date_to
        if use_smc_standalone:
            trades = []
            for sym in config.symbols:
                t = run_smc_backtest_for_symbol(
                    sym, all_data[sym], config, date_from, date_to, verbose=False
                )
                trades.extend(t)
        else:
            trades = run_backtest_combined(
                config.symbols, all_data, config, date_from, date_to, verbose=False
            )
        result = calc_stats(trades, config, date_from, date_to)
        return self.calculator.calculate(result)

    async def run(
        self,
        config: BacktestConfig,
        use_smc_standalone: bool = True,
    ) -> ImprovementState:
        """
        Chạy improvement loop.
        Mỗi iteration thay đúng 1 param trong BacktestConfig.
        """
        all_data = await download_all_data(config, use_cache=True)
        met = self._run_backtest(config, all_data, use_smc_standalone)
        targets_met = self.calculator.meets_targets(met)

        best_metrics: Optional[MetricsResult] = met
        best_config: Optional[BacktestConfig] = config
        self.registry.log(
            ChangeRecord(
                iteration=0,
                component="baseline",
                change_type="init",
                old_value="",
                new_value="",
                reasoning="Initial run",
                metrics_before={},
                metrics_after=asdict(met),
                improved=True,
            )
        )

        if targets_met:
            return ImprovementState(
                iteration=0,
                metrics=met,
                best_metrics=best_metrics,
                best_config=config,
                targets_met=True,
            )

        candidates = PARAM_CANDIDATES_SMC if use_smc_standalone else PARAM_CANDIDATES_RULE
        for it in range(1, self.max_iterations):
            cand_idx = self._bottleneck_candidate(met, candidates) % len(candidates)
            param_name, values = candidates[cand_idx]
            val_idx = self._value_idx.get(cand_idx, 0)
            if val_idx >= len(values):
                continue
            new_val = values[val_idx]
            old_val = getattr(config, param_name)
            cfg_new = replace(config, **{param_name: new_val})

            prev_met = met
            met2 = self._run_backtest(cfg_new, all_data, use_smc_standalone)
            improved = met2.profit_factor > prev_met.profit_factor

            if improved:
                config = cfg_new
                met = met2
                best_metrics = met
                best_config = config
            self._value_idx[cand_idx] = val_idx + 1

            self.registry.log(
                ChangeRecord(
                    iteration=it,
                    component=param_name,
                    change_type="param",
                    old_value=old_val,
                    new_value=new_val if improved else f"{new_val}(reverted)",
                    reasoning=f"PF={prev_met.profit_factor:.2f} WR={prev_met.win_rate:.1%} → thử {param_name}={new_val}",
                    metrics_before=asdict(prev_met),
                    metrics_after=asdict(met2),
                    improved=improved,
                )
            )

            targets_met = self.calculator.meets_targets(met)
            if targets_met:
                return ImprovementState(
                    iteration=it,
                    metrics=met,
                    best_metrics=best_metrics,
                    best_config=best_config,
                    targets_met=True,
                )

        return ImprovementState(
            iteration=self.max_iterations - 1,
            metrics=met,
            best_metrics=best_metrics,
            best_config=best_config,
            targets_met=False,
        )
