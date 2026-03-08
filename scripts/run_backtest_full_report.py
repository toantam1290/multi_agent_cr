"""
Chay day du 7 buoc backtest theo huong dan va ghi report.
Usage:
  python scripts/run_backtest_full_report.py [--days 180] [--symbol BTCUSDT] [--skip-download]
  python scripts/run_backtest_full_report.py --steps 2,3,5  # Chi chay buoc 2, 3, 5
"""
import argparse
import asyncio
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import (
    BacktestConfig,
    download_all_data,
    run_backtest_for_symbol,
    run_backtest_combined,
    run_walk_forward,
    run_optimization,
    calc_stats,
    INITIAL_BALANCE,
)


def build_config(
    symbols: list[str],
    days: int,
    strategy: str = "",
    **kwargs,
) -> BacktestConfig:
    """Build config, apply strategy presets if set."""
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    date_from = now - timedelta(days=days)
    date_to = now

    # Apply strategy presets
    no_ema9 = kwargs.get("no_ema9", False)
    no_cvd = kwargs.get("no_cvd", False)
    no_momentum_gate = kwargs.get("no_momentum_gate", False)
    net_score = kwargs.get("net_score", 0)
    confluence = kwargs.get("confluence", 3)

    if strategy == "v2":
        no_ema9 = True
        no_momentum_gate = True
        net_score = net_score or 10
        confluence = 2 if confluence == 3 else confluence
    elif strategy == "loose":
        no_ema9 = True
        no_cvd = True
        no_momentum_gate = True
        net_score = net_score or 3
        confluence = 1 if confluence == 3 else confluence

    return BacktestConfig(
        symbols=symbols,
        style="scalp",
        date_from=date_from,
        date_to=date_to,
        use_rule_filter=not kwargs.get("no_rule", False),
        use_ema9_filter=not no_ema9,
        use_confluence_filter=not kwargs.get("no_confluence", False),
        use_cvd_proxy=not no_cvd,
        use_vwap_filter=not kwargs.get("no_vwap", False),
        use_session_filter=not kwargs.get("no_session", False),
        use_regime_filter=not kwargs.get("no_regime", False),
        use_chop_filter=not kwargs.get("no_chop", False),
        use_correlation_filter=not kwargs.get("no_correlation", False),
        use_dynamic_confluence=not kwargs.get("no_dynamic_confluence", False),
        use_sl_structure=not kwargs.get("no_sl_structure", False),
        use_trail_stop=not kwargs.get("no_trail", False),
        use_momentum_gate=not no_momentum_gate,
        net_score_min=net_score,
        rule_case=kwargs.get("rule_case", "full"),
        confluence_threshold=confluence,
        scalp_rr=kwargs.get("rr", 2.0),
        swing_rr=kwargs.get("swing_rr", 2.0),
        walk_forward=kwargs.get("walk_forward", False),
        wf_train_days=kwargs.get("wf_train_days", 90),
        wf_test_days=kwargs.get("wf_test_days", 30),
    )


async def main():
    p = argparse.ArgumentParser(description="Full backtest report - 7 steps")
    p.add_argument("--days", type=int, default=180, help="Backtest period (days)")
    p.add_argument("--symbol", default="BTCUSDT", help="Comma-separated symbols")
    p.add_argument("--multi-symbol", default="BTCUSDT,ETHUSDT,SOLUSDT", help="For step 4")
    p.add_argument("--use-cache", action="store_true", default=True, help="Use cached data")
    p.add_argument("--no-cache", dest="use_cache", action="store_false", help="Download fresh data")
    p.add_argument("--skip-download", action="store_true", help="Skip step 1, assume cache exists")
    p.add_argument("--steps", default="1,2,3,4,5,6,7", help="Comma-separated steps to run (e.g. 2,3,5)")
    p.add_argument("--output", default="docs/018-backtest-full-report.txt", help="Output report path")
    p.add_argument("--wf-train", type=int, default=90)
    p.add_argument("--wf-test", type=int, default=30)
    p.add_argument("--days-step4", type=int, default=0, help="Override days for step 4 (0=use --days). Workflow Buoc 3: 180")
    p.add_argument("--days-step5", type=int, default=0, help="Override days for step 5 (0=use --days). Workflow Buoc 4: 180")
    args = p.parse_args()

    steps_to_run = {int(s.strip()) for s in args.steps.split(",") if s.strip()}
    symbols = [s.strip().upper() for s in args.symbol.split(",")]
    multi_symbols = [s.strip().upper() for s in args.multi_symbol.split(",")]

    # Days for data load: step 4/5 workflow may need 180d
    days_load = max(args.days, args.days_step4 or args.days, args.days_step5 or args.days)

    # All symbols we need (for step 4)
    all_symbols_needed = list(dict.fromkeys(symbols + ([x for x in multi_symbols if x not in symbols] if 4 in steps_to_run else [])))

    report_lines = [
        f"Backtest Full Report - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Symbols: {', '.join(symbols)} | Period: {args.days} days | Cache: {args.use_cache}",
        "=" * 90,
        "",
    ]

    # Step 1: Download data
    if 1 in steps_to_run and not args.skip_download:
        report_lines.append("## Step 1: Download data")
        report_lines.append("")
        config0 = build_config(all_symbols_needed, days_load)
        print("[Step 1] Downloading data...")
        all_data = await download_all_data(config0, use_cache=args.use_cache, download_only=True)
        report_lines.append("  Data saved to data/backtest_cache/")
        report_lines.append("  Run with --use-cache for subsequent runs.")
        report_lines.append("")
        # download_only still returns data; reload with cache for consistency
        all_data = await download_all_data(config0, use_cache=True, download_only=False)
    else:
        print("[Step 1] Skipped (--skip-download or not in --steps)")
        config0 = build_config(all_symbols_needed, days_load)
        all_data = await download_all_data(config0, use_cache=args.use_cache, download_only=False)

    if not all_data:
        print("ERROR: No data. Run step 1 first or ensure cache exists.")
        return

    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    date_from = now - timedelta(days=args.days)
    date_to = now

    # Step 2: Funnel diagnosis (dùng loose để match workflow Bước 1/2)
    if 2 in steps_to_run:
        report_lines.append("## Step 2: Filter Funnel Diagnosis (strategy loose)")
        report_lines.append("")
        print("[Step 2] Running funnel diagnosis (loose)...")
        config = build_config(symbols, min(args.days, 90), strategy="loose")
        cfg_date_from = now - timedelta(days=min(args.days, 90))
        cfg_date_to = now
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_backtest_for_symbol(
                symbols[0], all_data[symbols[0]], config,
                cfg_date_from, cfg_date_to,
                verbose=True,
            )
        funnel_out = buf.getvalue()
        # Extract funnel section
        if "Filter Funnel" in funnel_out:
            for line in funnel_out.split("\n"):
                if "Filter Funnel" in line or line.strip().startswith("->") or "OK Traded" in line:
                    report_lines.append(f"  {line}")
        report_lines.append("")

    # Step 3: Loose + v2 strategies
    if 3 in steps_to_run:
        report_lines.append("## Step 3: Strategy Comparison (loose vs v2)")
        report_lines.append("")
        print("[Step 3] Running loose and v2 strategies...")

        for strat_name, strategy in [("loose", "loose"), ("v2", "v2")]:
            config = build_config(symbols, args.days, strategy=strategy)
            if len(symbols) > 1:
                trades = run_backtest_combined(symbols, all_data, config, date_from, date_to)
            else:
                trades = run_backtest_for_symbol(symbols[0], all_data[symbols[0]], config, date_from, date_to)
            stats = calc_stats(trades, config, date_from, date_to)

            verdict = "PASS" if (stats.win_rate >= 45 and len(trades) >= 20) else "CHECK"
            if strat_name == "v2":
                verdict = "PASS" if (stats.win_rate >= 52 and stats.profit_factor >= 1.2 and len(trades) >= 100) else "CHECK"

            report_lines.append(f"  {strat_name.upper():<8}: trades={len(trades):4d} | win={stats.win_rate:5.1f}% | "
                               f"PF={stats.profit_factor:.2f} | maxDD={stats.max_drawdown_pct:.1f}% | "
                               f"PnL={stats.total_pnl_pct:+.2f}% | {verdict}")
            print(f"    {strat_name}: {len(trades)} trades, win={stats.win_rate:.1f}%, PF={stats.profit_factor:.2f}")

        report_lines.append("")
        report_lines.append("  Target v2: >=100 trades, PF>=1.2, Win%>=52%")
        report_lines.append("")

    # Step 4: Multi-symbol (workflow Buoc 3: BTCUSDT,ETHUSDT, 180d)
    if 4 in steps_to_run:
        days_4 = args.days_step4 or args.days
        report_lines.append("## Step 4: Multi-Symbol (v2)")
        report_lines.append("")
        print(f"[Step 4] Running multi-symbol ({days_4}d)...")
        config = build_config(multi_symbols, days_4, strategy="v2")
        date_from_4 = now - timedelta(days=days_4)
        date_to_4 = now
        trades = run_backtest_combined(multi_symbols, all_data, config, date_from_4, date_to_4)
        stats = calc_stats(trades, config, date_from_4, date_to_4)

        report_lines.append(f"  Symbols: {', '.join(multi_symbols)}")
        report_lines.append(f"  Trades: {len(trades)} | Win%: {stats.win_rate:.1f} | PF: {stats.profit_factor:.2f} | "
                           f"MaxDD: {stats.max_drawdown_pct:.1f}% | PnL: {stats.total_pnl_pct:+.2f}%")
        report_lines.append("")
        print(f"    {len(trades)} trades, win={stats.win_rate:.1f}%, PF={stats.profit_factor:.2f}")

    # Step 5: Walk-forward (workflow Buoc 4: 180d, train 90, test 30)
    if 5 in steps_to_run:
        days_5 = args.days_step5 or args.days
        report_lines.append("## Step 5: Walk-Forward Validation")
        report_lines.append("")
        print(f"[Step 5] Running walk-forward ({days_5}d)...")
        config = build_config(symbols, days_5, strategy="v2",
                              wf_train_days=args.wf_train, wf_test_days=args.wf_test)
        config = replace(config, walk_forward=True, wf_train_days=args.wf_train, wf_test_days=args.wf_test)

        windows = run_walk_forward(symbols[0], all_data[symbols[0]], config)
        report_lines.append(f"  Train={args.wf_train}d, Test={args.wf_test}d")
        report_lines.append(f"  {'Window':<35} {'IS Win%':>8} {'IS#':>5} {'OOS Win%':>9} {'OOS#':>5} {'OOS PnL':>9} {'OOS DD':>7}")
        report_lines.append("  " + "-" * 85)

        oos_profitable = 0
        for w in windows:
            prof = "OK" if w["oos_pnl_pct"] > 0 else "--"
            if w["oos_pnl_pct"] > 0:
                oos_profitable += 1
            report_lines.append(f"  {w['window']:<35} {w['is_win_rate']:>7.1f}% {w['is_trades']:>5} "
                               f"{w['oos_win_rate']:>8.1f}% {w['oos_trades']:>5} "
                               f"{w['oos_pnl_pct']:>+8.1f}% {w['oos_max_dd']:>6.1f}%  {prof}")

        report_lines.append("")
        report_lines.append(f"  OOS profitable: {oos_profitable}/{len(windows)} windows")
        report_lines.append("  Target: at least 2/3 OOS windows profitable")
        report_lines.append("")

    # Step 6: Optimize
    if 6 in steps_to_run:
        report_lines.append("## Step 6: Parameter Optimization (v2 base)")
        report_lines.append("")
        print("[Step 6] Running optimization...")
        config = build_config(symbols, args.days, strategy="v2")
        opt_results = run_optimization(symbols[0], all_data[symbols[0]], config)

        report_lines.append(f"  Top 5 by Profit Factor:")
        report_lines.append(f"  {'Conf':>5} {'RR':>5} {'Trades':>7} {'Win%':>6} {'PF':>6} {'MaxDD':>7} {'PnL%':>8}")
        report_lines.append("  " + "-" * 55)
        for r in opt_results[:5]:
            report_lines.append(f"  {r['confluence']:>5} {r['rr']:>5.1f} {r['trades']:>7} "
                               f"{r['win_rate']:>5.1f}% {r['profit_factor']:>6.2f} "
                               f"{r['max_dd']:>6.1f}% {r['total_pnl_pct']:>+7.2f}%")
        report_lines.append("")

    # Step 7: Rule cases
    if 7 in steps_to_run:
        report_lines.append("## Step 7: LONG vs SHORT (rule cases)")
        report_lines.append("")
        print("[Step 7] Running rule cases...")
        config_base = build_config(symbols, args.days, strategy="v2")

        report_lines.append(f"  {'Rule Case':<14} {'Trades':>7} {'Win%':>6} {'PF':>6} {'PnL%':>8}")
        report_lines.append("  " + "-" * 50)

        for rule_case in ["full", "long_only", "short_only", "no_volume", "no_momentum"]:
            cfg = replace(config_base, rule_case=rule_case)
            trades = run_backtest_for_symbol(symbols[0], all_data[symbols[0]], cfg, date_from, date_to)
            stats = calc_stats(trades, cfg, date_from, date_to)
            report_lines.append(f"  {rule_case:<14} {len(trades):>7} {stats.win_rate:>5.1f}% "
                               f"{stats.profit_factor:>6.2f} {stats.total_pnl_pct:>+7.2f}%")

        report_lines.append("")

    # Metrics summary
    report_lines.append("=" * 90)
    report_lines.append("## Metrics Target (before live)")
    report_lines.append("  | Metric          | Minimum | Target  |")
    report_lines.append("  |-----------------|---------|---------|")
    report_lines.append("  | Trades          | >= 100  | >= 200  |")
    report_lines.append("  | Win Rate        | >= 50%  | >= 55%  |")
    report_lines.append("  | Profit Factor   | >= 1.1  | >= 1.3  |")
    report_lines.append("  | Max Drawdown    | < 5%    | < 3%    |")
    report_lines.append("  | Sharpe Ratio    | > 0.8   | > 1.2   |")
    report_lines.append("  | OOS consistency | 2/3 win | 3/3 win |")
    report_lines.append("=" * 90)

    # Write report
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
