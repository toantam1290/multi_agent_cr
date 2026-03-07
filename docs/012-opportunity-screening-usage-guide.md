# 012 — Hướng dẫn sử dụng Opportunity Screening

**Mục tiêu:** Hướng dẫn cách dùng từng chế độ, lệnh chạy, và quy trình vận hành hàng ngày.

---

## 1. Chuẩn bị

### 1.1 Cấu hình `.env`

Copy từ `.env.example` và điền các biến bắt buộc:

```env
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ALLOWED_PAIRS=BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT
```

### 1.2 Kiểm tra config

```powershell
cd multi_agent_cr
python -c "from config import cfg; cfg.validate(); print('Config OK')"
```

### 1.3 Smoke test (chạy 1 lần trước khi dùng opportunity)

```powershell
python scripts/smoke_opportunity.py
```

Kỳ vọng: in ra `tickers`, `futures`, `opportunity pairs` và `PASS`.

---

## 2. Các chế độ scan

| Chế độ | SCAN_MODE | SCAN_DRY_RUN | Mô tả |
|--------|-----------|--------------|-------|
| **Fixed** | fixed | false | Scan cố định theo ALLOWED_PAIRS (như cũ) |
| **Opportunity dry-run** | opportunity | true | Chỉ log danh sách cặp sẽ scan, không gọi Claude |
| **Opportunity live** | opportunity | false | Scan động theo thị trường, gọi Claude |

### 2.1 Trading style (swing vs scalp)

| TRADING_STYLE | Timeframes | Trend | ATR mult | Mô tả |
|---------------|------------|-------|----------|-------|
| **swing** (mặc định khi fixed) | 1h, 4h, 1d | EMA50 vs EMA200 (1d) | 1.5 / 1.2 | Hold lâu hơn, majors |
| **scalp** (mặc định khi opportunity) | 5m, 15m, 1h, 4h | EMA20 vs EMA50 (4h) | 1.0 / 0.8 | 15m direction, 5m timing+ATR SL/TP, 1h ADX, 4h trend |

Để trống `TRADING_STYLE` → auto: `opportunity` → scalp, `fixed` → swing. Có thể override: `TRADING_STYLE=scalp` hoặc `TRADING_STYLE=swing`.

**Scalp optimizations (tự động khi scalp):**
- Scan mỗi 5 phút (swing: 15 phút)
- Position monitor mỗi 1 phút (swing: 2 phút)
- Min confidence 80 (swing: 75)
- Approval timeout 120s (swing: 300s)
- RSI filter nới: 50/50 (swing: 45/55)

---

## 3. Cách chạy từng chế độ

### 3.1 Fixed mode (mặc định)

```powershell
$env:SCAN_MODE="fixed"
$env:SCAN_DRY_RUN="false"
python main.py
```

Hoặc không set env (mặc định đã là fixed):

```powershell
python main.py
```

### 3.2 Opportunity dry-run (kiểm tra trước khi bật thật)

```powershell
$env:SCAN_MODE="opportunity"
$env:SCAN_DRY_RUN="true"
python main.py
```

- Chỉ log danh sách cặp sẽ scan, **không tạo signal**.
- Dùng để xem screening có hợp lý không trước khi chạy thật.

### 3.3 Opportunity live (chạy thật)

```powershell
$env:SCAN_MODE="opportunity"
$env:SCAN_DRY_RUN="false"
python main.py
```

Hoặc set trong `.env`:

```env
SCAN_MODE=opportunity
SCAN_DRY_RUN=false
```

---

## 4. Tùy chỉnh opportunity (khi SCAN_MODE=opportunity)

| Biến | Ý nghĩa | Ví dụ |
|------|---------|-------|
| OPPORTUNITY_VOLATILITY_PCT | Min \|priceChange%\| để vào list | 5.0 |
| OPPORTUNITY_VOLATILITY_MAX_PCT | Max \|priceChange%\| (tránh pump & dump) | 25.0 |
| MIN_QUOTE_VOLUME_USD | Thanh khoản tối thiểu (USD) | 5000000 |
| MAX_PAIRS_PER_SCAN | Cap số cặp mỗi cycle | 30 |
| CORE_PAIRS | Luôn scan (BTC, ETH) | BTCUSDT,ETHUSDT |
| OPPORTUNITY_USE_WHITELIST | Chỉ scan cặp trong ALLOWED_PAIRS | false |

Ví dụ siết filter: tăng `MIN_QUOTE_VOLUME_USD`, giảm `MAX_PAIRS_PER_SCAN`.

---

## 5. Web UI (khuyến nghị)

Khi chạy `main.py`, Web UI tự động chạy tại **http://localhost:8080**.

Dashboard gồm:
- **Opportunity Screening** — Scan mode, dry-run, funnel (candidates → scanned → rule passed → claude passed → signals)
- **Daily Dashboard** — Quality score, action (SCALE_UP_SMALL, HOLD, TIGHTEN_FILTER, DEFENSIVE_MODE) theo ngày

Chỉ cần mở trình duyệt, không cần chạy script riêng.

---

## 6. Lệnh kiểm tra nhanh

### 6.1 Xem stats và logs

```powershell
python scripts/check_metrics.py
```

In ra: total_trades, open_trades, pending_signals, today_spend.

### 6.2 Export daily metrics (CSV)

```powershell
python utils/daily_metrics_report.py --days 14 --out data/reports
```

Tạo: `daily_dashboard.csv`, `pair_daily.csv`, `funnel_daily.csv` trong `data/reports`.

### 6.3 Xem funnel gần nhất

```powershell
python -c "
from database import Database
db = Database()
rows = db.get_recent_logs(limit=20)
for r in rows:
    if r.get('data') and 'pairs_scanned' in str(r.get('data','')):
        print(r)
"
```

---

## 7. Quy trình vận hành đề xuất

### Lần đầu bật opportunity

1. Chạy **dry-run 24–48h** để kiểm tra.
2. Xem log: số cặp không quá cao, không fallback liên tục.
3. Bật **opportunity live** với size nhỏ.
4. Theo dõi 3–5 ngày, export daily metrics.

### Hàng ngày

1. Sau UTC rollover, chạy export:

   ```powershell
   python utils/daily_metrics_report.py --days 7 --out data/reports
   ```

2. Xem `quality_score` và `action` trong `daily_dashboard.csv`:
   - `>= 80` → SCALE_UP_SMALL
   - `65–80` → HOLD
   - `50–65` → TIGHTEN_FILTER
   - `< 50` → DEFENSIVE_MODE

3. Điều chỉnh config theo action (xem spec 006).

---

## 8. Rollback nhanh

Khi gặp sự cố:

```powershell
$env:SCAN_MODE="fixed"
$env:SCAN_DRY_RUN="false"
python main.py
```

Hoặc sửa `.env`: `SCAN_MODE=fixed`, rồi restart.

---

## 9. Tài liệu liên quan

- `010-opportunity-screening-single-command-execution-guide.md` — Lệnh chi tiết theo ngày
- `009-opportunity-screening-operations-runbook.md` — Vận hành, incident
- `006-daily-metrics-score-spec.md` — Công thức quality score, action
