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
        calc_stats,
        print_report,
        BacktestConfig,
    )

    config = BacktestConfig(
        symbols=symbols,
        style="scalp",
        date_from=date_from,
        date_to=date_to,
        use_smc_standalone=True,
        use_rule_filter=False,
        use_session_filter=not args.no_session,
        use_trail_stop=True,
    )

    print("\n" + "=" * 60)
    print("  SMC STANDALONE BACKTEST")
    print(f"  Period: {date_from.strftime('%Y-%m-%d')} -> {date_to.strftime('%Y-%m-%d')} ({(date_to - date_from).days} days)")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Session filter: {config.use_session_filter}")
    print("=" * 60)

    print("\n📥 Downloading data...")
    all_data = await download_all_data(config, use_cache=not args.no_cache)
    print("   Done.\n")

    all_trades = []
    for symbol in symbols:
        print(f"⏳ Running SMC backtest for {symbol}...")
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

    result = calc_stats(all_trades, config, date_from, date_to)
    symbol_label = "+".join(symbols) if len(symbols) > 1 else symbols[0]
    print_report(result, symbol_label, date_from, date_to)


if __name__ == "__main__":
    asyncio.run(main())
