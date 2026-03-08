"""
Chạy toàn bộ kịch bản backtest và ghi kết quả.
Dùng cache database (data/backtest_cache/) mặc định — chạy backtest.py --download-only trước nếu chưa có.
Usage: python scripts/run_backtest_suite.py [--days 60] [--no-cache]
"""
import asyncio
import sys
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone
from backtest import (
    BacktestConfig,
    download_all_data,
    run_backtest_for_symbol,
    run_backtest_combined,
    calc_stats,
    INITIAL_BALANCE,
)


SCENARIOS = [
    # Phase 1: Baseline (full filters)
    {"id": "1.1", "name": "Baseline full", "kwargs": {}},
    {"id": "1.2", "name": "Confluence 2", "kwargs": {"confluence": 2}},
    # Phase 2: Filter impact (no-ema9 để có trades)
    {"id": "2.1", "name": "No EMA9", "kwargs": {"no_ema9": True}},
    {"id": "2.2", "name": "No EMA9 + No Confluence", "kwargs": {"no_ema9": True, "no_confluence": True}},
    {"id": "2.3", "name": "No EMA9 + No Chop", "kwargs": {"no_ema9": True, "no_chop": True}},
    {"id": "2.4", "name": "No EMA9 + No CVD", "kwargs": {"no_ema9": True, "no_cvd": True}},
    {"id": "2.5", "name": "No EMA9 + No Session", "kwargs": {"no_ema9": True, "no_session": True}},
    {"id": "2.6", "name": "No Rule", "kwargs": {"no_rule": True}},
    # Phase 3: Rule cases
    {"id": "3.1", "name": "Rule: full", "kwargs": {"no_ema9": True, "rule_case": "full"}},
    {"id": "3.2", "name": "Rule: long_only", "kwargs": {"no_ema9": True, "rule_case": "long_only"}},
    {"id": "3.3", "name": "Rule: short_only", "kwargs": {"no_ema9": True, "rule_case": "short_only"}},
    {"id": "3.4", "name": "Rule: no_volume", "kwargs": {"no_ema9": True, "rule_case": "no_volume"}},
    {"id": "3.5", "name": "Rule: no_momentum", "kwargs": {"no_ema9": True, "rule_case": "no_momentum"}},
]


def build_config(symbols: list[str], days: int = 60, **kwargs) -> BacktestConfig:
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    date_from = now - timedelta(days=days)
    date_to = now
    return BacktestConfig(
        symbols=symbols,
        style="scalp",
        date_from=date_from,
        date_to=date_to,
        use_rule_filter=not kwargs.get("no_rule", False),
        use_ema9_filter=not kwargs.get("no_ema9", False),
        use_confluence_filter=not kwargs.get("no_confluence", False),
        use_cvd_proxy=not kwargs.get("no_cvd", False),
        use_vwap_filter=True,
        use_session_filter=not kwargs.get("no_session", False),
        use_regime_filter=True,
        use_chop_filter=not kwargs.get("no_chop", False),
        use_correlation_filter=True,
        use_dynamic_confluence=True,
        use_sl_structure=True,
        use_trail_stop=True,
        rule_case=kwargs.get("rule_case", "full"),
        confluence_threshold=kwargs.get("confluence", 3),
        scalp_rr=1.5,
        swing_rr=2.0,
    )


async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--no-cache", action="store_true", help="Không dùng cache, download mới")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--output", default="docs/017-backtest-results.txt")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbol.split(",")]
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    date_from = now - timedelta(days=args.days)
    date_to = now

    # Download once (dùng cache mặc định)
    config0 = build_config(symbols, args.days)
    use_cache = not args.no_cache
    print(f"Data: {'cache' if use_cache else 'download'}...")
    all_data = await download_all_data(config0, use_cache=use_cache, download_only=False)
    if not all_data:
        print("No data")
        return

    results = []
    for sc in SCENARIOS:
        kwargs = {**sc["kwargs"], "days": args.days}
        config = build_config(symbols, **kwargs)
        if len(symbols) > 1:
            trades = run_backtest_combined(symbols, all_data, config, date_from, date_to)
        else:
            trades = run_backtest_for_symbol(symbols[0], all_data[symbols[0]], config, date_from, date_to)
        stats = calc_stats(trades, config, date_from, date_to)
        results.append({
            "id": sc["id"],
            "name": sc["name"],
            "trades": len(trades),
            "win_rate": stats.win_rate,
            "pf": stats.profit_factor,
            "max_dd": stats.max_drawdown_pct,
            "pnl_pct": stats.total_pnl_pct,
            "pnl_usdt": stats.total_pnl_pct / 100 * INITIAL_BALANCE,
        })
        print(f"  {sc['id']} {sc['name']}: {len(trades)} trades, win={stats.win_rate:.1f}%, PnL={stats.total_pnl_pct:+.2f}%")

    # Write report
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"Backtest Suite Results — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Symbols: {', '.join(symbols)} | Period: {args.days} days",
        "=" * 90,
        "",
        f"{'ID':<6} {'Scenario':<30} {'Trades':>7} {'Win%':>6} {'PF':>6} {'MaxDD':>7} {'PnL%':>8} {'PnL$':>10}",
        "-" * 90,
    ]
    for r in results:
        lines.append(f"{r['id']:<6} {r['name']:<30} {r['trades']:>7} {r['win_rate']:>5.1f}% {r['pf']:>6.2f} "
                     f"{r['max_dd']:>6.1f}% {r['pnl_pct']:>+7.2f}% ${r['pnl_usdt']:>+8.2f}")
    lines.append("")
    lines.append("=" * 90)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nResults written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
