#!/usr/bin/env python3
"""
utils/backtest_report.py - Performance report từ trades table

Chạy: python utils/backtest_report.py [--days 30] [--csv output.csv]

Output: win_rate, profit_factor, max_drawdown, Sharpe, buy-and-hold benchmark,
        per-symbol, per-hour breakdown, rolling 30-day stats.
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH


def get_db_connection():
    return sqlite3.connect(DB_PATH)


def fetch_btc_prices(days: int) -> tuple[float, float]:
    """Fetch BTC price at start and end of period (for buy-and-hold benchmark)."""
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    try:
        with httpx.Client(timeout=10) as client:
            # Get daily klines - need start and end timestamps
            end_ts = int(datetime(end_date.year, end_date.month, end_date.day).timestamp() * 1000)
            start_ts = int(datetime(start_date.year, start_date.month, start_date.day).timestamp() * 1000)
            url = "https://fapi.binance.com/fapi/v1/klines"
            params = {
                "symbol": "BTCUSDT",
                "interval": "1d",
                "startTime": start_ts,
                "endTime": end_ts,
                "limit": days + 5,
            }
            r = client.get(url, params=params)
            r.raise_for_status()
            klines = r.json()
            if len(klines) < 2:
                return 0.0, 0.0
            start_price = float(klines[0][4])  # close of first day
            end_price = float(klines[-1][4])   # close of last day
            return start_price, end_price
    except Exception as e:
        print(f"Warning: Could not fetch BTC prices: {e}")
        return 0.0, 0.0


def run_report(days: int = 30, csv_path: str | None = None) -> dict:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        return _run_report_impl(conn, days, csv_path)
    finally:
        conn.close()


def _run_report_impl(conn: sqlite3.Connection, days: int, csv_path: str | None) -> dict:
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    # Closed trades in period
    rows = conn.execute("""
        SELECT id, pair, direction, entry_price, stop_loss, take_profit,
               quantity, position_size_usdt, opened_at, closed_at,
               exit_price, pnl_usdt, pnl_pct, status
        FROM trades
        WHERE status != 'OPEN' AND closed_at >= ?
        ORDER BY closed_at
    """, (cutoff,)).fetchall()

    trades = [dict(r) for r in rows]
    total = len(trades)

    if total == 0:
        report = {
            "period_days": days,
            "total_trades": 0,
            "message": "No closed trades in period",
        }
        _print_report(report)
        return report

    # Basic stats
    pnls = [t["pnl_usdt"] or 0 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    win_rate = wins / total * 100 if total > 0 else 0

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)

    avg_pnl = sum(pnls) / total
    total_pnl = sum(pnls)

    # Max drawdown (daily PnL cumsum)
    by_date: dict[str, float] = {}
    for t in trades:
        d = (t["closed_at"] or "")[:10]
        if d:
            by_date[d] = by_date.get(d, 0) + (t["pnl_usdt"] or 0)
    sorted_dates = sorted(by_date.keys())
    cumsum = 0
    peak = 0
    max_dd = 0
    for d in sorted_dates:
        cumsum += by_date[d]
        peak = max(peak, cumsum)
        max_dd = max(max_dd, peak - cumsum)

    # Sharpe (daily returns, annualized with sqrt(365))
    daily_pnls = [by_date[d] for d in sorted_dates]
    if len(daily_pnls) >= 2:
        mean_daily = sum(daily_pnls) / len(daily_pnls)
        var = sum((x - mean_daily) ** 2 for x in daily_pnls) / (len(daily_pnls) - 1)
        std_daily = var ** 0.5 if var > 0 else 1e-10
        sharpe = (mean_daily / std_daily) * (365 ** 0.5) if std_daily > 0 else 0
    else:
        sharpe = 0

    # Buy-and-hold benchmark
    start_price, end_price = fetch_btc_prices(days)
    if start_price > 0 and end_price > 0:
        btc_return_pct = (end_price - start_price) / start_price * 100
    else:
        btc_return_pct = None

    # Per-symbol
    by_symbol: dict[str, list] = {}
    for t in trades:
        s = t["pair"]
        if s not in by_symbol:
            by_symbol[s] = []
        by_symbol[s].append(t["pnl_usdt"] or 0)
    symbol_stats = {}
    for sym, pnls_s in by_symbol.items():
        n = len(pnls_s)
        w = sum(1 for p in pnls_s if p > 0)
        symbol_stats[sym] = {
            "trades": n,
            "win_rate": w / n * 100 if n > 0 else 0,
            "total_pnl": sum(pnls_s),
            "avg_pnl": sum(pnls_s) / n if n > 0 else 0,
        }

    # Per-hour (UTC)
    by_hour: dict[int, list] = {}
    for t in trades:
        closed = t.get("closed_at") or ""
        if closed:
            try:
                dt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                h = dt.hour
            except Exception:
                h = 0
        else:
            h = 0
        if h not in by_hour:
            by_hour[h] = []
        by_hour[h].append(t["pnl_usdt"] or 0)
    hour_stats = {}
    for h in range(24):
        pnls_h = by_hour.get(h, [])
        n = len(pnls_h)
        hour_stats[h] = {
            "trades": n,
            "win_rate": sum(1 for p in pnls_h if p > 0) / n * 100 if n > 0 else 0,
            "total_pnl": sum(pnls_h),
        }

    report = {
        "period_days": days,
        "total_trades": total,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl_usdt": round(total_pnl, 2),
        "avg_pnl_usdt": round(avg_pnl, 2),
        "max_drawdown_usdt": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "benchmark_buy_hold_btc_pct": round(btc_return_pct, 2) if btc_return_pct is not None else None,
        "by_symbol": symbol_stats,
        "by_hour_utc": hour_stats,
    }

    _print_report(report)

    if csv_path:
        _write_csv(report, csv_path)

    return report


def _print_report(r: dict):
    print("\n" + "=" * 60)
    print("BACKTEST REPORT")
    print("=" * 60)
    print(f"Period: last {r['period_days']} days")
    print(f"Total trades: {r.get('total_trades', 0)}")
    if r.get("message"):
        print(r["message"])
        return
    print(f"Win rate: {r['win_rate']}%")
    print(f"Profit factor: {r['profit_factor']}")
    print(f"Total PnL: ${r['total_pnl_usdt']:,.2f}")
    print(f"Avg PnL/trade: ${r['avg_pnl_usdt']:,.2f}")
    print(f"Max drawdown: ${r['max_drawdown_usdt']:,.2f}")
    print(f"Sharpe ratio: {r['sharpe_ratio']}")
    if r.get("benchmark_buy_hold_btc_pct") is not None:
        print(f"Buy-and-hold BTC return: {r['benchmark_buy_hold_btc_pct']}%")
    print("\n--- By symbol ---")
    for sym, s in r.get("by_symbol", {}).items():
        print(f"  {sym}: {s['trades']} trades, win_rate={s['win_rate']:.1f}%, pnl=${s['total_pnl']:,.2f}")
    print("\n--- By hour (UTC) ---")
    for h in sorted(r.get("by_hour_utc", {}).keys()):
        s = r["by_hour_utc"][h]
        if s["trades"] > 0:
            print(f"  {h:02d}:00 - {s['trades']} trades, win_rate={s['win_rate']:.1f}%, pnl=${s['total_pnl']:,.2f}")
    print("=" * 60 + "\n")


def _write_csv(r: dict, path: str):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerow(["period_days", r["period_days"]])
        w.writerow(["total_trades", r.get("total_trades", 0)])
        w.writerow(["win_rate", r.get("win_rate", 0)])
        w.writerow(["profit_factor", r.get("profit_factor", 0)])
        w.writerow(["total_pnl_usdt", r.get("total_pnl_usdt", 0)])
        w.writerow(["max_drawdown_usdt", r.get("max_drawdown_usdt", 0)])
        w.writerow(["sharpe_ratio", r.get("sharpe_ratio", 0)])
        w.writerow(["benchmark_buy_hold_btc_pct", r.get("benchmark_buy_hold_btc_pct", "")])
    print(f"CSV written to {path}")


def main():
    parser = argparse.ArgumentParser(description="Backtest report from trades table")
    parser.add_argument("--days", type=int, default=30, help="Number of days to analyze")
    parser.add_argument("--csv", type=str, default=None, help="Output CSV path")
    args = parser.parse_args()
    run_report(days=args.days, csv_path=args.csv)


if __name__ == "__main__":
    main()
