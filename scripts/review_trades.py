#!/usr/bin/env python3
"""
Review trades — xem lệnh đã trade, phân tích thành công/thất bại.
Chạy: python scripts/review_trades.py [--limit N] [--csv]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import Database
from config import DB_PATH


def main():
    parser = argparse.ArgumentParser(description="Review closed trades với signal reasoning")
    parser.add_argument("--limit", type=int, default=30, help="Số trade gần nhất (default 30)")
    parser.add_argument("--csv", action="store_true", help="Export CSV thay vì in console")
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        print(f"ERROR: DB not found: {DB_PATH}")
        sys.exit(1)

    db = Database(DB_PATH)

    rows = db.conn.execute("""
        SELECT
            t.id, t.signal_id, t.pair, t.direction,
            t.entry_price, t.stop_loss, t.take_profit,
            t.exit_price, t.pnl_usdt, t.pnl_pct, t.status,
            t.opened_at, t.closed_at, t.is_paper,
            s.confidence, s.regime, s.reasoning, s.raw_json
        FROM trades t
        LEFT JOIN signals s ON t.signal_id = s.id
        WHERE t.status IN ('TOOK_PROFIT', 'STOPPED')
        ORDER BY t.closed_at DESC
        LIMIT ?
    """, (args.limit,)).fetchall()

    if not rows:
        print("No closed trades yet. Run paper/live and wait for positions to hit SL/TP.")
        return

    # Stats
    wins = [r for r in rows if (r["pnl_usdt"] or 0) > 0]
    losses = [r for r in rows if (r["pnl_usdt"] or 0) <= 0]
    total_pnl = sum(r["pnl_usdt"] or 0 for r in rows)
    win_rate = len(wins) / len(rows) * 100 if rows else 0

    if args.csv:
        # CSV export
        out = ["pair,direction,entry,exit,pnl_usdt,pnl_pct,status,confidence,regime,reasoning"]
        for r in rows:
            reason = (r["reasoning"] or "").replace('"', '""').replace("\n", " ")
            out.append(
                f'{r["pair"]},{r["direction"]},{r["entry_price"]:.4f},{r["exit_price"] or 0:.4f},'
                f'{r["pnl_usdt"] or 0:.2f},{r["pnl_pct"] or 0:.2f},{r["status"]},'
                f'{r["confidence"] or ""},{r["regime"] or ""},"{reason}"'
            )
        csv_path = ROOT / "data" / "trade_review.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text("\n".join(out), encoding="utf-8")
        print(f"Exported {len(rows)} trades to {csv_path}")
        return

    # Console report (ASCII-safe for Windows console)
    print("=" * 80)
    print("TRADE REVIEW - Paper/Live")
    print("=" * 80)
    print(f"Total: {len(rows)} trades | Win: {len(wins)} | Loss: {len(losses)} | Win rate: {win_rate:.1f}%")
    print(f"Total PnL: ${total_pnl:+,.2f}")
    print("=" * 80)

    for i, r in enumerate(rows, 1):
        pnl = r["pnl_usdt"] or 0
        pnl_pct = r["pnl_pct"] or 0
        status = r["status"]
        outcome = "[TP]" if status == "TOOK_PROFIT" else "[SL]"
        paper = " [PAPER]" if r["is_paper"] else ""

        print(f"\n--- Trade #{i} {outcome} {r['pair']} {r['direction']}{paper} ---")
        print(f"  Entry: ${r['entry_price']:,.2f} -> Exit: ${(r['exit_price'] or 0):,.2f}")
        print(f"  SL: ${r['stop_loss']:,.2f} | TP: ${r['take_profit']:,.2f}")
        print(f"  PnL: ${pnl:+,.2f} ({pnl_pct:+.2f}%)")
        print(f"  Opened: {r['opened_at'][:19] if r['opened_at'] else '—'} | Closed: {(r['closed_at'] or '')[:19]}")
        print(f"  Confidence: {r['confidence'] or '—'} | Regime: {r['regime'] or '—'}")

        reasoning = (r["reasoning"] or "").strip()
        # Safe for Windows console (avoid UnicodeEncodeError)
        reasoning = reasoning.encode("ascii", "replace").decode("ascii")
        if reasoning and reasoning != "?":
            # Wrap long reasoning
            words = reasoning.split()
            lines = []
            cur = ""
            for w in words:
                if len(cur) + len(w) + 1 > 76:
                    if cur:
                        lines.append("  " + cur)
                    cur = w
                else:
                    cur = cur + (" " if cur else "") + w
            if cur:
                lines.append("  " + cur)
            print("  Reasoning:")
            for line in lines[:5]:  # Max 5 lines
                print(line)
            if len(lines) > 5:
                print("  ...")
        else:
            # Try raw_json for SMC signals
            try:
                raw = json.loads(r["raw_json"] or "{}")
                if isinstance(raw, dict) and raw.get("reasoning"):
                    rj = (raw["reasoning"] or "")[:200]
                    rj = rj.encode("ascii", "replace").decode("ascii")
                    print("  Reasoning:", rj + ("..." if len(str(raw.get("reasoning", ""))) > 200 else ""))
            except (json.JSONDecodeError, TypeError):
                pass

    print("\n" + "=" * 80)
    print("Tip: Run with --csv to export trade_review.csv")
    print("=" * 80)


if __name__ == "__main__":
    main()
