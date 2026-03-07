#!/usr/bin/env python3
"""
scripts/check_metrics.py - Quick check signals/trades/stats (cross-platform)

Chạy: python scripts/check_metrics.py

Kiểm tra: stats, open_trades, pending_signals, today_spend
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database


def main():
    print("Check metrics")
    print("-" * 50)

    db = Database()
    try:
        stats = db.get_stats()
        print("stats:", stats)

        open_trades = db.get_open_trades()
        print("open_trades:", len(open_trades))

        pending = db.get_pending_signals()
        print("pending_signals:", len(pending))

        spend = db.get_today_spend()
        print("today_spend:", spend)

        print("-" * 50)
        print("Check done")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
