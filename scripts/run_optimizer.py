#!/usr/bin/env python3
"""
Chạy Optimization pipeline: Improvement Engine + Walk-Forward Validator.

Usage:
  python scripts/run_optimizer.py
  python scripts/run_optimizer.py --symbol BTCUSDT,ETHUSDT --from 2024-01-01 --to 2024-12-31
  python scripts/run_optimizer.py --wf-only   # Chỉ chạy Walk-Forward
  python scripts/run_optimizer.py --improve-only  # Chỉ chạy Improvement Engine
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone


async def main():
    parser = argparse.ArgumentParser(description="Optimization pipeline")
    parser.add_argument("--symbol", default="BTCUSDT", help="Comma-separated symbols")
    parser.add_argument("--from", dest="date_from_str", default="2024-01-01", help="Start date")
    parser.add_argument("--to", dest="date_to_str", default="", help="End date (default: today)")
    parser.add_argument("--wf-only", action="store_true", help="Chỉ chạy Walk-Forward")
    parser.add_argument("--improve-only", action="store_true", help="Chỉ chạy Improvement Engine")
    parser.add_argument("--rule-based", action="store_true", help="Rule-based mode (confluence, RR) thay SMC standalone")
    parser.add_argument("--max-iter", type=int, default=15, help="Max improvement iterations (default 15)")
    parser.add_argument("--no-cache", action="store_true", help="Download fresh data")
    args = parser.parse_args()

    date_from = datetime.strptime(args.date_from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = (
        datetime.strptime(args.date_to_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.date_to_str
        else datetime.now(timezone.utc)
    )
    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]

    from backtest import BacktestConfig, download_all_data
    from optimization.improvement_engine import ImprovementEngine
    from optimization.walk_forward import WalkForwardValidator

    use_smc_standalone = not args.rule_based
    config = BacktestConfig(
        symbols=symbols,
        style="scalp",
        date_from=date_from,
        date_to=date_to,
        use_smc_standalone=use_smc_standalone,
        use_rule_filter=use_smc_standalone is False,
        use_smc_filter=use_smc_standalone is False,
        use_confluence_filter=use_smc_standalone is False,
        use_session_filter=True,
        use_trail_stop=True,
    )

    print("\n" + "=" * 60)
    print("  OPTIMIZATION PIPELINE")
    print(f"  Period: {date_from.strftime('%Y-%m-%d')} -> {date_to.strftime('%Y-%m-%d')}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Mode: {'rule-based' if args.rule_based else 'SMC standalone'}")
    print("=" * 60)

    state = None
    if not args.wf_only:
        print("\n[Improvement Engine]")
        engine = ImprovementEngine(max_iterations=args.max_iter)
        state = await engine.run(config, use_smc_standalone=use_smc_standalone)
        print(f"  Iterations: {state.iteration + 1}")
        print(f"  Targets met: {state.targets_met}")
        if state.best_metrics:
            m = state.best_metrics
            print(f"  Best PF={m.profit_factor:.2f} WR={m.win_rate:.1%} Sharpe={m.sharpe_ratio:.2f}")
            print(f"  MDD={m.max_drawdown_pct:.1f}% AvgRR={m.avg_rr:.2f} Calmar={m.calmar_ratio:.2f}")

    if not args.improve_only:
        print("\n[Walk-Forward Validator]")
        wf = WalkForwardValidator(n_windows=4, train_ratio=0.7, min_trades_oos=30)
        wf_config = (state.best_config if state and state.best_config else config)
        wf_result = await wf.run(wf_config, use_smc_standalone=use_smc_standalone)
        print(f"  Passed: {wf_result.passed_count}/{wf_result.total_windows} windows")
        print(f"  WFO PASS: {wf_result.passed}")

    print("\nDone.\n")


if __name__ == "__main__":
    asyncio.run(main())
