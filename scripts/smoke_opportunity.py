#!/usr/bin/env python3
"""
scripts/smoke_opportunity.py - Smoke test cho opportunity screening (cross-platform)

Chạy: python scripts/smoke_opportunity.py

Kiểm tra: get_all_tickers_24hr, get_premium_index_full, get_opportunity_pairs
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.market_data import BinanceDataFetcher, get_opportunity_pairs


async def main():
    print("Smoke test: Opportunity screening")
    print("-" * 50)

    f = BinanceDataFetcher()
    try:
        tickers = await f.get_all_tickers_24hr()
        print(f"tickers: {len(tickers)}")
        if not tickers:
            print("FAIL: No tickers")
            return 1

        premium = await f.get_premium_index_full()
        print(f"futures symbols: {len(premium)}")
        futures_symbols = set(p["symbol"] for p in premium) if premium else set()
        funding_map = {p["symbol"]: float(p.get("lastFundingRate") or 0) for p in premium} if premium else {}

        pairs = get_opportunity_pairs(
            tickers=tickers,
            futures_symbols=futures_symbols or None,
            funding_map=funding_map or None,
            min_volatility_pct=5.0,
            max_volatility_pct=25.0,
            min_quote_volume_usd=5_000_000,
            max_pairs_per_scan=30,
            core_pairs=["BTCUSDT", "ETHUSDT"],
            blacklist=["USDCUSDT", "BUSDUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT"],
        )
        print(f"opportunity pairs: {len(pairs)}")
        print(f"  sample: {pairs[:10]}")
        if not pairs:
            print("WARN: No opportunity pairs (có thể thị trường ít biến động)")
        else:
            print("PASS: get_opportunity_pairs OK")
    finally:
        await f.close()

    print("-" * 50)
    print("Smoke test done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
