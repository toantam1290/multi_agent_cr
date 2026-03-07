#!/usr/bin/env python3
"""
utils/daily_metrics_report.py - Daily dashboard CSV + quality score (spec 006)

Chạy: python utils/daily_metrics_report.py [--days 14] [--out data/reports]

Output: daily_dashboard.csv, pair_daily.csv, funnel_daily.csv
"""
import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
from config import DB_PATH


def get_db_connection():
    import sqlite3
    db_path = Path(DB_PATH)
    if not db_path.is_absolute():
        db_path = _project_root / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db_path))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _compute_quality_score(row: dict) -> tuple[float, str]:
    """Compute quality_score (0-100) and action per spec 006."""
    signals_total = row.get("signals_total") or 0
    approved_signals = row.get("approved_signals") or 0
    executed_trades = row.get("executed_trades") or 0
    winning_trades = row.get("winning_trades") or 0
    gross_pnl = row.get("gross_pnl_usdt") or 0
    fees = row.get("fees_usdt") or 0
    net_pnl = gross_pnl - fees
    avg_pnl_pct = row.get("avg_trade_pnl_pct") or 0
    worst_pnl_pct = row.get("worst_trade_pnl_pct") or 0
    avg_confidence = row.get("avg_confidence") or 0
    spend = row.get("anthropic_spend_usd") or 0

    approve_rate = approved_signals / signals_total * 100 if signals_total > 0 else 0
    execute_rate = executed_trades / approved_signals * 100 if approved_signals > 0 else 0
    win_rate = winning_trades / executed_trades * 100 if executed_trades > 0 else 0
    efficiency = net_pnl / spend if spend > 0 else 0

    # Data quality check
    if signals_total > 0 and (approved_signals > signals_total or executed_trades > approved_signals):
        return 0.0, "DATA_ISSUE"

    S_win = _clamp(win_rate, 0, 100)
    S_pnl = _clamp(50 + 8 * avg_pnl_pct, 0, 100)
    S_drawdown = _clamp(100 - 6 * abs(min(worst_pnl_pct, 0)), 0, 100)
    S_eff = _clamp(10 * efficiency, 0, 100)
    S_conf = _clamp(avg_confidence, 0, 100)

    quality_score = 0.35 * S_win + 0.25 * S_pnl + 0.15 * S_drawdown + 0.15 * S_eff + 0.10 * S_conf

    if quality_score >= 80:
        action = "SCALE_UP_SMALL"
    elif quality_score >= 65:
        action = "HOLD"
    elif quality_score >= 50:
        action = "TIGHTEN_FILTER"
    else:
        action = "DEFENSIVE_MODE"

    return round(quality_score, 2), action


def get_dashboard_data(days: int = 7) -> list[dict]:
    """Lấy dữ liệu daily dashboard (dùng cho Web UI). Return list[dict]."""
    conn = get_db_connection()
    conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
    cur = conn.cursor()
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    cur.execute("""
        WITH signals_d AS (
          SELECT date(created_at) AS d, COUNT(*) AS signals_total,
                 AVG(confidence) AS avg_confidence, AVG(risk_reward) AS avg_risk_reward
          FROM signals GROUP BY date(created_at)
        ),
        approved_d AS (
          SELECT date(created_at) AS d,
                 SUM(CASE WHEN status IN ('APPROVED', 'EXECUTED') THEN 1 ELSE 0 END) AS approved_signals
          FROM signals GROUP BY date(created_at)
        ),
        trades_d AS (
          SELECT date(closed_at) AS d, COUNT(*) AS executed_trades,
                 SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS winning_trades,
                 COALESCE(SUM(pnl_usdt), 0) AS gross_pnl_usdt, COALESCE(SUM(fees_usdt), 0) AS fees_usdt,
                 AVG(pnl_pct) AS avg_trade_pnl_pct, MIN(pnl_pct) AS worst_trade_pnl_pct
          FROM trades WHERE status != 'OPEN' AND closed_at IS NOT NULL GROUP BY date(closed_at)
        ),
        spend_d AS (SELECT date AS d, COALESCE(anthropic_spend_usd, 0) AS anthropic_spend_usd FROM daily_stats)
        SELECT COALESCE(s.d, t.d, sp.d) AS date_utc,
               COALESCE(s.signals_total, 0) AS signals_total, COALESCE(a.approved_signals, 0) AS approved_signals,
               COALESCE(t.executed_trades, 0) AS executed_trades, COALESCE(t.winning_trades, 0) AS winning_trades,
               COALESCE(t.gross_pnl_usdt, 0) AS gross_pnl_usdt, COALESCE(t.fees_usdt, 0) AS fees_usdt,
               COALESCE(t.gross_pnl_usdt, 0) - COALESCE(t.fees_usdt, 0) AS net_pnl_usdt,
               COALESCE(t.avg_trade_pnl_pct, 0) AS avg_trade_pnl_pct, COALESCE(t.worst_trade_pnl_pct, 0) AS worst_trade_pnl_pct,
               COALESCE(s.avg_confidence, 0) AS avg_confidence, COALESCE(s.avg_risk_reward, 0) AS avg_risk_reward,
               COALESCE(sp.anthropic_spend_usd, 0) AS anthropic_spend_usd
        FROM signals_d s
        LEFT JOIN approved_d a ON a.d = s.d LEFT JOIN trades_d t ON t.d = s.d LEFT JOIN spend_d sp ON sp.d = s.d
        UNION
        SELECT t.d, COALESCE(s.signals_total, 0), COALESCE(a.approved_signals, 0), COALESCE(t.executed_trades, 0),
               COALESCE(t.winning_trades, 0), COALESCE(t.gross_pnl_usdt, 0), COALESCE(t.fees_usdt, 0),
               COALESCE(t.gross_pnl_usdt, 0) - COALESCE(t.fees_usdt, 0), COALESCE(t.avg_trade_pnl_pct, 0),
               COALESCE(t.worst_trade_pnl_pct, 0), COALESCE(s.avg_confidence, 0), COALESCE(s.avg_risk_reward, 0),
               COALESCE(sp.anthropic_spend_usd, 0)
        FROM trades_d t
        LEFT JOIN signals_d s ON s.d = t.d LEFT JOIN approved_d a ON a.d = t.d LEFT JOIN spend_d sp ON sp.d = t.d
        WHERE s.d IS NULL
        ORDER BY date_utc DESC
    """)
    rows = [dict(r) for r in cur.fetchall() if r.get("date_utc") and r["date_utc"] >= cutoff]
    conn.close()

    result = []
    for r in rows:
        approve_rate = (r["approved_signals"] / r["signals_total"] * 100) if r["signals_total"] > 0 else 0
        execute_rate = (r["executed_trades"] / r["approved_signals"] * 100) if r["approved_signals"] > 0 else 0
        win_rate = (r["winning_trades"] / r["executed_trades"] * 100) if r["executed_trades"] > 0 else 0
        efficiency = (r["net_pnl_usdt"] / r["anthropic_spend_usd"]) if r["anthropic_spend_usd"] > 0 else 0
        quality_score, action = _compute_quality_score(r)
        net_pnl = (r.get("gross_pnl_usdt") or 0) - (r.get("fees_usdt") or 0)
        result.append({
            "date_utc": r["date_utc"],
            "signals_total": r["signals_total"],
            "approved_signals": r["approved_signals"],
            "executed_trades": r["executed_trades"],
            "winning_trades": r["winning_trades"],
            "approve_rate_pct": round(approve_rate, 2),
            "execute_rate_pct": round(execute_rate, 2),
            "win_rate_pct": round(win_rate, 2),
            "gross_pnl_usdt": r["gross_pnl_usdt"],
            "fees_usdt": r["fees_usdt"],
            "net_pnl_usdt": net_pnl,
            "avg_trade_pnl_pct": r["avg_trade_pnl_pct"],
            "worst_trade_pnl_pct": r["worst_trade_pnl_pct"],
            "avg_confidence": r["avg_confidence"],
            "avg_risk_reward": r["avg_risk_reward"],
            "anthropic_spend_usd": r["anthropic_spend_usd"],
            "efficiency_usdt_per_usd": round(efficiency, 2),
            "quality_score": quality_score,
            "action": action,
        })
    return result


def run_export(days: int = 14, out_dir: str = "data/reports") -> dict:
    conn = get_db_connection()
    conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
    cur = conn.cursor()

    # Daily base query (spec 006)
    cur.execute("""
        WITH signals_d AS (
          SELECT
            date(created_at) AS d,
            COUNT(*) AS signals_total,
            AVG(confidence) AS avg_confidence,
            AVG(risk_reward) AS avg_risk_reward
          FROM signals
          GROUP BY date(created_at)
        ),
        approved_d AS (
          SELECT
            date(created_at) AS d,
            SUM(CASE WHEN status IN ('APPROVED', 'EXECUTED') THEN 1 ELSE 0 END) AS approved_signals
          FROM signals
          GROUP BY date(created_at)
        ),
        trades_d AS (
          SELECT
            date(closed_at) AS d,
            COUNT(*) AS executed_trades,
            SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS winning_trades,
            COALESCE(SUM(pnl_usdt), 0) AS gross_pnl_usdt,
            COALESCE(SUM(fees_usdt), 0) AS fees_usdt,
            AVG(pnl_pct) AS avg_trade_pnl_pct,
            MIN(pnl_pct) AS worst_trade_pnl_pct
          FROM trades
          WHERE status != 'OPEN' AND closed_at IS NOT NULL
          GROUP BY date(closed_at)
        ),
        spend_d AS (
          SELECT date AS d, COALESCE(anthropic_spend_usd, 0) AS anthropic_spend_usd
          FROM daily_stats
        )
        SELECT
          COALESCE(s.d, t.d, sp.d) AS date_utc,
          COALESCE(s.signals_total, 0) AS signals_total,
          COALESCE(a.approved_signals, 0) AS approved_signals,
          COALESCE(t.executed_trades, 0) AS executed_trades,
          COALESCE(t.winning_trades, 0) AS winning_trades,
          COALESCE(t.gross_pnl_usdt, 0) AS gross_pnl_usdt,
          COALESCE(t.fees_usdt, 0) AS fees_usdt,
          COALESCE(t.gross_pnl_usdt, 0) - COALESCE(t.fees_usdt, 0) AS net_pnl_usdt,
          COALESCE(t.avg_trade_pnl_pct, 0) AS avg_trade_pnl_pct,
          COALESCE(t.worst_trade_pnl_pct, 0) AS worst_trade_pnl_pct,
          COALESCE(s.avg_confidence, 0) AS avg_confidence,
          COALESCE(s.avg_risk_reward, 0) AS avg_risk_reward,
          COALESCE(sp.anthropic_spend_usd, 0) AS anthropic_spend_usd
        FROM signals_d s
        LEFT JOIN approved_d a ON a.d = s.d
        LEFT JOIN trades_d t ON t.d = s.d
        LEFT JOIN spend_d sp ON sp.d = s.d
        UNION
        SELECT
          t.d AS date_utc,
          COALESCE(s.signals_total, 0),
          COALESCE(a.approved_signals, 0),
          COALESCE(t.executed_trades, 0),
          COALESCE(t.winning_trades, 0),
          COALESCE(t.gross_pnl_usdt, 0),
          COALESCE(t.fees_usdt, 0),
          COALESCE(t.gross_pnl_usdt, 0) - COALESCE(t.fees_usdt, 0),
          COALESCE(t.avg_trade_pnl_pct, 0),
          COALESCE(t.worst_trade_pnl_pct, 0),
          COALESCE(s.avg_confidence, 0),
          COALESCE(s.avg_risk_reward, 0),
          COALESCE(sp.anthropic_spend_usd, 0)
        FROM trades_d t
        LEFT JOIN signals_d s ON s.d = t.d
        LEFT JOIN approved_d a ON a.d = t.d
        LEFT JOIN spend_d sp ON sp.d = t.d
        WHERE s.d IS NULL
        ORDER BY date_utc DESC
    """)
    daily_rows = cur.fetchall()

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    daily_rows = [r for r in daily_rows if r["date_utc"] and r["date_utc"] >= cutoff]

    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # daily_dashboard.csv
    dashboard_path = Path(out_dir) / "daily_dashboard.csv"
    dashboard_rows = []
    for r in daily_rows:
        approve_rate = (r["approved_signals"] / r["signals_total"] * 100) if r["signals_total"] > 0 else 0
        execute_rate = (r["executed_trades"] / r["approved_signals"] * 100) if r["approved_signals"] > 0 else 0
        win_rate = (r["winning_trades"] / r["executed_trades"] * 100) if r["executed_trades"] > 0 else 0
        efficiency = (r["net_pnl_usdt"] / r["anthropic_spend_usd"]) if r["anthropic_spend_usd"] > 0 else 0
        quality_score, action = _compute_quality_score(r)

        dashboard_rows.append({
            "date_utc": r["date_utc"],
            "signals_total": r["signals_total"],
            "approved_signals": r["approved_signals"],
            "executed_trades": r["executed_trades"],
            "winning_trades": r["winning_trades"],
            "approve_rate_pct": round(approve_rate, 2),
            "execute_rate_pct": round(execute_rate, 2),
            "win_rate_pct": round(win_rate, 2),
            "gross_pnl_usdt": r["gross_pnl_usdt"],
            "fees_usdt": r["fees_usdt"],
            "net_pnl_usdt": r["net_pnl_usdt"],
            "avg_trade_pnl_pct": r["avg_trade_pnl_pct"],
            "worst_trade_pnl_pct": r["worst_trade_pnl_pct"],
            "avg_confidence": r["avg_confidence"],
            "avg_risk_reward": r["avg_risk_reward"],
            "anthropic_spend_usd": r["anthropic_spend_usd"],
            "efficiency_usdt_per_usd": round(efficiency, 2),
            "quality_score": quality_score,
            "action": action,
        })

    dashboard_fields = ["date_utc", "signals_total", "approved_signals", "executed_trades", "winning_trades",
        "approve_rate_pct", "execute_rate_pct", "win_rate_pct", "gross_pnl_usdt", "fees_usdt", "net_pnl_usdt",
        "avg_trade_pnl_pct", "worst_trade_pnl_pct", "avg_confidence", "avg_risk_reward", "anthropic_spend_usd",
        "efficiency_usdt_per_usd", "quality_score", "action"]
    with open(dashboard_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=dashboard_fields)
        w.writeheader()
        w.writerows(dashboard_rows)

    # pair_daily.csv
    cur.execute("""
        SELECT
          date(t.closed_at) AS date_utc,
          t.pair AS pair,
          COUNT(*) AS trades,
          SUM(CASE WHEN t.pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
          COALESCE(SUM(t.pnl_usdt), 0) AS gross_pnl_usdt,
          COALESCE(SUM(t.fees_usdt), 0) AS fees_usdt,
          COALESCE(SUM(t.pnl_usdt), 0) - COALESCE(SUM(t.fees_usdt), 0) AS net_pnl_usdt,
          AVG(t.pnl_pct) AS avg_pnl_pct,
          MIN(t.pnl_pct) AS worst_pnl_pct,
          AVG(s.confidence) AS avg_confidence
        FROM trades t
        JOIN signals s ON t.signal_id = s.id
        WHERE t.status != 'OPEN' AND t.closed_at IS NOT NULL AND date(t.closed_at) >= ?
        GROUP BY date(t.closed_at), t.pair
        ORDER BY date_utc DESC, pair
    """, (cutoff,))
    pair_rows = cur.fetchall()

    pair_path = Path(out_dir) / "pair_daily.csv"
    pair_fields = ["date_utc", "pair", "trades", "wins", "win_rate_pct",
        "gross_pnl_usdt", "fees_usdt", "net_pnl_usdt", "avg_pnl_pct", "worst_pnl_pct", "avg_confidence"]
    with open(pair_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=pair_fields)
        w.writeheader()
        for r in pair_rows:
            win_rate = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
            w.writerow({
                "date_utc": r["date_utc"],
                "pair": r["pair"],
                "trades": r["trades"],
                "wins": r["wins"],
                "win_rate_pct": round(win_rate, 2),
                "gross_pnl_usdt": r["gross_pnl_usdt"],
                "fees_usdt": r["fees_usdt"],
                "net_pnl_usdt": r["net_pnl_usdt"],
                "avg_pnl_pct": r["avg_pnl_pct"] or 0,
                "worst_pnl_pct": r["worst_pnl_pct"] or 0,
                "avg_confidence": r["avg_confidence"] or 0,
            })

    # funnel_daily.csv
    funnel_path = Path(out_dir) / "funnel_daily.csv"
    funnel_rows = []
    for r in dashboard_rows:
        funnel_rows.append({
            "date_utc": r["date_utc"],
            "signals_total": r["signals_total"],
            "approved_signals": r["approved_signals"],
            "executed_trades": r["executed_trades"],
            "approve_rate_pct": r["approve_rate_pct"],
            "execute_rate_pct": r["execute_rate_pct"],
            "win_rate_pct": r["win_rate_pct"],
        })
    funnel_fields = ["date_utc", "signals_total", "approved_signals", "executed_trades",
        "approve_rate_pct", "execute_rate_pct", "win_rate_pct"]
    with open(funnel_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=funnel_fields)
        w.writeheader()
        w.writerows(funnel_rows)

    conn.close()

    result = {
        "days": days,
        "records": len(dashboard_rows),
        "dashboard": str(dashboard_path),
        "pair_daily": str(pair_path),
        "funnel_daily": str(funnel_path),
    }
    print(f"Exported {len(dashboard_rows)} days to {out_dir}")
    print(f"  - {dashboard_path}")
    print(f"  - {pair_path}")
    print(f"  - {funnel_path}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Daily metrics report (spec 006)")
    parser.add_argument("--days", type=int, default=14, help="Days to export")
    parser.add_argument("--out", type=str, default="data/reports", help="Output directory")
    args = parser.parse_args()
    run_export(days=args.days, out_dir=args.out)


if __name__ == "__main__":
    main()
