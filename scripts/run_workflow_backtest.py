"""
Chay dung 4 buoc workflow sau khi sua (ATR 1h, RR 2.0, RSI trend-following, entry market).

Workflow:
  Buoc 1 — Fix fee (ATR 1h + RR 2.0): loose, 90d, funnel
  Buoc 2 — Full 5 thay doi: loose, 90d, funnel
  Buoc 3 — Mo rong: v2, BTCUSDT+ETHUSDT, 180d, funnel
  Buoc 4 — Walk-forward: v2, BTCUSDT, 180d, wf-train 90, wf-test 30

Usage:
  python scripts/run_workflow_backtest.py
  python scripts/run_workflow_backtest.py --no-cache
"""
import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_cmd(cmd: list[str], desc: str) -> bool:
    """Run command, return True if success."""
    print(f"\n{'='*60}")
    print(f"  {desc}")
    print(f"  Command: {' '.join(cmd)}")
    print("=" * 60)
    result = subprocess.run(
        [sys.executable, "backtest.py"] + cmd,
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    return result.returncode == 0


async def main():
    p = argparse.ArgumentParser(description="Run 4-step workflow backtest")
    p.add_argument("--no-cache", action="store_true", help="Download fresh, khong dung cache")
    args = p.parse_args()

    cache = [] if args.no_cache else ["--use-cache"]

    steps = [
        (
            "Buoc 1 & 2: Fix fee + Full 5 thay doi (loose, 90d, funnel)",
            ["--symbol", "BTCUSDT", "--days", "90", "--strategy", "loose", "--funnel"] + cache,
        ),
        (
            "Buoc 3: Mo rong (v2, BTCUSDT+ETHUSDT, 180d, funnel)",
            ["--symbol", "BTCUSDT,ETHUSDT", "--days", "180", "--strategy", "v2", "--funnel"] + cache,
        ),
        (
            "Buoc 4: Walk-forward (v2, 180d, train 90, test 30)",
            ["--symbol", "BTCUSDT", "--days", "180", "--strategy", "v2",
             "--walk-forward", "--wf-train", "90", "--wf-test", "30"] + cache,
        ),
    ]

    print("\nWorkflow Backtest — 4 buoc")
    print("Ky vong Buoc 1/2: trades ~45, PF >= 0.5 (fix fee), win% > 40% (full 5)")
    print("Ky vong Buoc 3: trades > 80, win% > 45%, PF > 1.0")
    print("Ky vong Buoc 4: it nhat 2/3 OOS windows profitable")

    for desc, cmd in steps:
        ok = run_cmd(cmd, desc)
        if not ok:
            print(f"\n[FAIL] {desc}")
            sys.exit(1)

    print("\n" + "=" * 60)
    print("  Workflow hoan thanh. Xem ket qua tren.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
