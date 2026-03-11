#!/usr/bin/env python3
"""Clear PENDING signals: update to SKIPPED or DELETE.
Chạy từ bất kỳ đâu: python scripts/clear_pending_signals.py [--delete]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import Database
from config import DB_PATH


def main():
    parser = argparse.ArgumentParser(description="Clear PENDING signals from DB")
    parser.add_argument("--delete", action="store_true", help="Xóa hẳn PENDING thay vì chuyển sang SKIPPED")
    parser.add_argument("--delete-skipped", action="store_true", help="Xóa luôn signal SKIPPED (để không còn trong Recent Signals)")
    args = parser.parse_args()

    db_path = Path(DB_PATH)
    print(f"DB path: {db_path}")

    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    db = Database(DB_PATH)

    cur = db.conn.execute("SELECT COUNT(*) FROM signals WHERE status = 'PENDING'")
    n_pending = cur.fetchone()[0]
    cur = db.conn.execute("SELECT COUNT(*) FROM signals WHERE status = 'SKIPPED'")
    n_skipped = cur.fetchone()[0]
    print(f"PENDING: {n_pending} | SKIPPED: {n_skipped}")

    if args.delete_skipped and n_skipped > 0:
        db.conn.execute("DELETE FROM signals WHERE status = 'SKIPPED'")
        db.conn.commit()
        print(f"Done: deleted {n_skipped} SKIPPED signals. Refresh Web UI.")
        return

    if n_pending == 0:
        print("No PENDING to clear. Use --delete-skipped để xóa signal SKIPPED khỏi Recent Signals.")
        return

    if args.delete:
        db.conn.execute("DELETE FROM signals WHERE status = 'PENDING'")
        db.conn.commit()
        print(f"Done: deleted {n_pending} PENDING signals. Refresh Web UI.")
    else:
        db.conn.execute(
            "UPDATE signals SET status = 'SKIPPED', cancel_reason = 'manual_clear' WHERE status = 'PENDING'"
        )
        db.conn.commit()
        print(f"Done: {n_pending} signals updated to SKIPPED. Refresh Web UI.")


if __name__ == "__main__":
    main()
