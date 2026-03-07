# 010 — Opportunity Screening Single Command Execution Guide

**Ngày:** 2026-03-07  
**Mục tiêu:** Cung cấp luồng lệnh thực thi theo ngày để dev/ops có thể copy chạy nhanh, giảm sai sót khi rollout.

---

## 0) Scope và nguyên tắc

- Guide này bám theo bộ tài liệu:
  - `005-opportunity-screening-implementation-checklist.md`
  - `006-daily-metrics-score-spec.md`
  - `007-opportunity-screening-pr-rollout-plan.md`
  - `009-opportunity-screening-operations-runbook.md`
- Mặc định an toàn:
  - ưu tiên `SCAN_MODE=fixed` cho tới khi pass dry-run
  - khi bật `opportunity`, dùng `SCAN_DRY_RUN=true` trước
- Tất cả command chạy tại root project: `multi_agent_cr`.

---

## 1) Preflight (chạy 1 lần trước tuần triển khai)

## 1.1 Cài dependencies

```bash
pip install -r requirements.txt
```

Expected:

- Install thành công, không lỗi dependency conflict.

## 1.2 Kiểm tra env

```bash
python -c "from config import cfg; print('Config loaded OK')"
```

Expected:

- In ra `Config loaded OK`.
- Nếu lỗi config, dừng và sửa `.env`.

## 1.3 Kiểm tra DB bootstrap

```bash
python -c "from database import Database; db=Database(); print('DB OK'); db.close()"
```

Expected:

- In ra `DB OK`.
- Không lỗi migration.

---

## 2) Day-by-day command plan (2 tuần)

## Day 1 — PR1 (config + validation)

```bash
python -m pytest -q
```

Nếu chưa có test đầy đủ:

```bash
python -c "from config import cfg; print(cfg.trading.min_confidence)"
```

Checklist pass:

- App load được config.
- Validation reject đúng khi env sai.

---

## Day 2 — PR2 (fetchers)

Sanity check fetchers:

```bash
python - <<'PY'
import asyncio
from utils.market_data import BinanceDataFetcher

async def main():
    f = BinanceDataFetcher()
    tickers = await f.get_all_tickers_24hr()
    futures = await f.get_futures_symbols()
    print("tickers:", len(tickers))
    print("futures:", len(futures))
    await f.close()

asyncio.run(main())
PY
```

Checklist pass:

- `tickers > 0`
- `futures > 0` (hoặc warning rõ ràng nếu endpoint lỗi tạm thời)

---

## Day 3 — PR3 (opportunity filter core)

Quick functional test:

```bash
python - <<'PY'
import asyncio
from utils.market_data import BinanceDataFetcher, get_opportunity_pairs

async def main():
    f = BinanceDataFetcher()
    tickers = await f.get_all_tickers_24hr()
    futures = await f.get_futures_symbols()
    pairs = get_opportunity_pairs(
        tickers=tickers,
        futures_symbols=futures or None,
        min_volatility_pct=5.0,
        max_volatility_pct=25.0,
        min_quote_volume_usd=5_000_000,
        max_pairs_per_scan=30,
        core_pairs=["BTCUSDT", "ETHUSDT"],
        blacklist=["USDCUSDT", "BUSDUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT"],
    )
    print("pairs:", len(pairs))
    print(pairs[:10])
    await f.close()

asyncio.run(main())
PY
```

Checklist pass:

- Không crash parse dữ liệu.
- Có list output hợp lý, không duplicate.

---

## Day 4 — PR4 (research integration)

Chạy app ở fixed mode (regression check):

```bash
set SCAN_MODE=fixed && python main.py
```

Sau đó chạy opportunity + dry-run:

```bash
set SCAN_MODE=opportunity && set SCAN_DRY_RUN=true && python main.py
```

Checklist pass:

- Fixed mode behavior không đổi.
- Opportunity dry-run không tạo signal/trade.

---

## Day 5 — PR5 (metrics + dry-run verification)

Run 24h dry-run, sau đó trích log gần nhất:

```bash
python - <<'PY'
from database import Database
db = Database()
rows = db.get_recent_logs(limit=30)
for r in rows[:10]:
    print(r["timestamp"], r["agent"], r["level"], r["message"])
db.close()
PY
```

Checklist pass:

- Có log funnel metrics mỗi cycle.
- Không có signal được lưu khi dry-run bật.

---

## Day 6 — PR6 (confluence + cooldown/hysteresis)

Chạy paper trong 1 ngày:

```bash
set SCAN_MODE=opportunity && set SCAN_DRY_RUN=false && python main.py
```

Cuối ngày kiểm tra churn sơ bộ:

```bash
python - <<'PY'
from database import Database
db = Database()
signals = db.get_recent_signals(limit=100)
print("recent_signals:", len(signals))
db.close()
PY
```

Checklist pass:

- Không tăng đột biến số cặp scan so với baseline.
- Churn giảm (ít pair lặp liên tục giữa các cycle).

---

## Day 7 — PR7 (export CSV + score)

Nếu mở rộng `utils/backtest_report.py`:

```bash
python utils/backtest_report.py --days 7
```

Nếu dùng script export riêng:

```bash
python utils/daily_metrics_report.py --days 14 --out data/reports
```

Checklist pass:

- Sinh được:
  - `daily_dashboard.csv`
  - `pair_daily.csv`
  - `funnel_daily.csv`
- Có cột `quality_score` và `action`.

---

## Day 8-14 — Paper rollout monitoring

Mỗi ngày chạy:

```bash
python utils/daily_metrics_report.py --days 14 --out data/reports
```

Xem snapshot:

```bash
python - <<'PY'
import pandas as pd
df = pd.read_csv("data/reports/daily_dashboard.csv")
print(df.head(5).to_string(index=False))
PY
```

Decision rule:

- `score >= 80` -> `SCALE_UP_SMALL`
- `65 <= score < 80` -> `HOLD`
- `50 <= score < 65` -> `TIGHTEN_FILTER`
- `< 50` -> `DEFENSIVE_MODE`

---

## 3) One-command profiles (copy nhanh)

## 3.1 Safe fixed mode

```bash
set SCAN_MODE=fixed && set SCAN_DRY_RUN=false && python main.py
```

## 3.2 Opportunity dry-run

```bash
set SCAN_MODE=opportunity && set SCAN_DRY_RUN=true && python main.py
```

## 3.3 Opportunity paper run

```bash
set SCAN_MODE=opportunity && set SCAN_DRY_RUN=false && python main.py
```

## 3.4 Emergency fallback

```bash
set SCAN_MODE=fixed && set SCAN_DRY_RUN=true && python main.py
```

---

## 4) Command pack cho reviewer (quick audit)

## 4.1 Check signals/trades stats nhanh

```bash
python - <<'PY'
from database import Database
db = Database()
print("stats:", db.get_stats())
print("open_trades:", len(db.get_open_trades()))
print("pending_signals:", len(db.get_pending_signals()))
db.close()
PY
```

## 4.2 Check Anthropic spend hôm nay

```bash
python - <<'PY'
from database import Database
db = Database()
print("today_spend:", db.get_today_spend())
db.close()
PY
```

---

## 5) Fail-fast checklist

Dừng rollout ngay nếu:

- Không đọc được config khi startup.
- Dry-run vẫn tạo signals/trades.
- Scan loop crash lặp lại.
- CSV export thiếu cột bắt buộc.
- `quality_score < 50` liên tiếp 2 ngày mà chưa vào `DEFENSIVE_MODE`.

---

## 6) Rollback nhanh (copy-run)

```bash
set SCAN_MODE=fixed && set SCAN_DRY_RUN=true && python main.py
```

Sau đó:

1. Thu thập log lỗi gần nhất.
2. Gắn cờ incident theo `009`.
3. Chỉ quay lại opportunity sau khi xác nhận root cause fix.

---

## 7) Artifact cần lưu cuối mỗi ngày

- `data/reports/daily_dashboard.csv`
- `data/reports/pair_daily.csv`
- `data/reports/funnel_daily.csv`
- short note:
  - config hôm đó
  - action hôm sau
  - rủi ro chính

---

## 8) Liên kết tài liệu

- `004-dynamic-pair-screening-plan.md`
- `005-opportunity-screening-implementation-checklist.md`
- `006-daily-metrics-score-spec.md`
- `007-opportunity-screening-pr-rollout-plan.md`
- `008-opportunity-screening-task-board.md`
- `009-opportunity-screening-operations-runbook.md`
