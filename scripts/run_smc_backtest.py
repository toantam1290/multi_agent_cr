#!/usr/bin/env python3
"""
Chạy SMC standalone backtest từ 2024-01-01.

Usage:
  python scripts/run_smc_backtest.py
  python scripts/run_smc_backtest.py --symbol BTCUSDT,ETHUSDT
  python scripts/run_smc_backtest.py --no-session
  python scripts/run_smc_backtest.py --from 2024-06-01 --to 2024-12-31
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone


async def main():
    parser = argparse.ArgumentParser(description="SMC standalone backtest")
    parser.add_argument("--symbol", default="BTCUSDT", help="Comma-separated symbols")
    parser.add_argument("--from", dest="date_from_str", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to_str", default="", help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--no-session", action="store_true", help="Disable session filter (London/NY)")
    parser.add_argument("--no-cache", action="store_true", help="Download fresh data, ignore cache")
    parser.add_argument("--quiet", action="store_true", help="Less verbose output")
    parser.add_argument("--rule-based", action="store_true", help="Rule-based mode (confluence, RR) thay SMC standalone")
    parser.add_argument("--no-chop", action="store_true", help="SMC standalone: tắt Chop filter (chop>61.8 skip)")
    parser.add_argument("--no-funding", action="store_true", help="SMC standalone: tắt Funding alignment filter")
    parser.add_argument("--ob-only", action="store_true", help="SMC standalone: chỉ trade ob_entry, bỏ ce/sweep/bpr")
    parser.add_argument("--no-ce", action="store_true", help="SMC standalone: bỏ ce_entry (WR thấp ~23%)")
    parser.add_argument("--adx", action="store_true", help="SMC standalone: chỉ trade khi ADX > 20 (trending)")
    parser.add_argument("--breakeven", type=int, default=0, metavar="N", help="Sau N candles move SL lên entry (0=off)")
    parser.add_argument("--strict", action="store_true", help="Chọn lọc cao: ob-only + chop 55 + min_conf 60")
    parser.add_argument("--style", default="scalp", choices=["scalp", "swing"], help="scalp=5m/15m, swing=1h/4h")
    args = parser.parse_args()

    date_from = datetime.strptime(args.date_from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = (
        datetime.strptime(args.date_to_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.date_to_str
        else datetime.now(timezone.utc)
    )

    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]

    from backtest import (
        download_all_data,
        run_smc_backtest_for_symbol,
        run_backtest_combined,
        calc_stats,
        print_report,
        BacktestConfig,
    )

    use_smc_standalone = not args.rule_based
    strict = args.strict and use_smc_standalone
    config = BacktestConfig(
        symbols=symbols,
        style=args.style,
        date_from=date_from,
        date_to=date_to,
        use_smc_standalone=use_smc_standalone,
        use_rule_filter=use_smc_standalone is False,
        use_smc_filter=use_smc_standalone is False,
        use_confluence_filter=use_smc_standalone is False,
        use_session_filter=not args.no_session,
        use_trail_stop=True,
        smc_use_chop_filter=use_smc_standalone and not args.no_chop,
        smc_use_funding_filter=use_smc_standalone and not args.no_funding,
        smc_use_adx_filter=use_smc_standalone and args.adx,
        smc_breakeven_candles=args.breakeven,
        smc_ob_entry_only=use_smc_standalone and (args.ob_only or strict),
        smc_disable_ce_entry=use_smc_standalone and args.no_ce,
        smc_min_grade="",
        smc_displacement_only=False,  # displacement_only quá chặt -> 0 trades
        smc_chop_threshold=55.0 if strict else 61.8,
        smc_min_rr_tp1=1.8 if strict else 1.8,
        smc_min_confidence=60 if strict else 55,
    )

    print("\n" + "=" * 60)
    print(f"  SMC {args.style.upper()} BACKTEST")
    print(f"  Period: {date_from.strftime('%Y-%m-%d')} -> {date_to.strftime('%Y-%m-%d')} ({(date_to - date_from).days} days)")
    print(f"  Symbols: {', '.join(symbols)}")
    if args.rule_based:
        mode = "rule-based"
    elif args.strict:
        mode = "SMC standalone (strict)"
    elif args.ob_only:
        mode = "SMC standalone (ob-only)"
    else:
        mode = "SMC standalone"
    print(f"  Mode: {mode}")
    print(f"  Session filter: {config.use_session_filter}")
    print("=" * 60)

    print("\n[Downloading data...]")
    all_data = await download_all_data(config, use_cache=not args.no_cache)
    print("   Done.\n")

    all_trades = []
    if use_smc_standalone:
        for symbol in symbols:
            print(f"[Running SMC backtest for {symbol}...]")
            trades = run_smc_backtest_for_symbol(
                symbol,
                all_data[symbol],
                config,
                date_from,
                date_to,
                verbose=not args.quiet,
            )
            all_trades.extend(trades)
            print(f"   -> {len(trades)} trades\n")
    else:
        print("[Running rule-based backtest...]")
        all_trades = run_backtest_combined(
            symbols, all_data, config, date_from, date_to, verbose=not args.quiet
        )
        print(f"   -> {len(all_trades)} trades\n")

    result = calc_stats(all_trades, config, date_from, date_to)
    symbol_label = "+".join(symbols) if len(symbols) > 1 else symbols[0]
    print_report(result, symbol_label, date_from, date_to)


if __name__ == "__main__":
    asyncio.run(main())
