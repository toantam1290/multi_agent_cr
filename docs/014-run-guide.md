# Hướng dẫn chạy Trading Agent

## 1. Chuẩn bị môi trường

```bash
cd multi_agent_cr
pip install -r requirements.txt
```

## 2. Cấu hình `.env`

Copy từ template và điền các giá trị bắt buộc:

```bash
cp .env.example .env
# Chỉnh sửa .env
```

### Bắt buộc

| Biến | Mô tả | Ví dụ |
|------|-------|-------|
| `ANTHROPIC_API_KEY` | API key từ [console.anthropic.com](https://console.anthropic.com) | `sk-ant-api03-...` |
| `TELEGRAM_BOT_TOKEN` | Bot token từ @BotFather | `123456:ABC...` |
| `TELEGRAM_CHAT_ID` | Chat ID từ @userinfobot | `5508280959` |

**Lưu ý:** Nếu API bị chặn, set `SKIP_TELEGRAM=true` để chạy không cần Telegram (không nhận alert/approve).

### Trading (có default)

| Biến | Default | Mô tả |
|------|---------|-------|
| `PAPER_TRADING` | `true` | Paper = giả lập, không đặt lệnh thật |
| `PAPER_BALANCE_USDT` | `10000` | Số dư giả lập |
| `ALLOWED_PAIRS` | `BTCUSDT,ETHUSDT,...` | Cặp phân tích (fixed mode) |
| `ANTHROPIC_DAILY_BUDGET_USD` | `0.75` | Giới hạn chi phí Claude/ngày |

### Scalp vs Swing

| Biến | Mô tả | Scalp | Swing |
|------|-------|-------|-------|
| `SCAN_MODE` | `fixed` = pairs cố định, `opportunity` = dynamic | — | — |
| `TRADING_STYLE` | Để trống = auto (opportunity→scalp, fixed→swing) | `scalp` | `swing` |
| `SCAN_INTERVAL_MIN` | Chu kỳ scan (phút) | 5 | 15 |
| `POSITION_MONITOR_INTERVAL_MIN` | Chu kỳ monitor (phút) | 1 | 2 |
| `SCALP_MIN_CONFIDENCE` | Min confidence | 80 | — |
| `SCALP_APPROVAL_TIMEOUT_SEC` | Timeout approve (giây) | 120 | 300 |
| `SCALP_RISK_REWARD_RATIO` | R:R tối thiểu | 1.5 | 2.0 |

### Funding (SHORT/LONG filter)

| Biến | Default | Mô tả |
|------|---------|-------|
| `FUNDING_SHORT_MIN_PCT` | `0.005` | SHORT cần funding > min (0.005% = neutral+) |
| `FUNDING_LONG_MAX_PCT` | `0.05` | LONG cần funding < max |

### Scalp tùy chọn

| Biến | Default | Mô tả |
|------|---------|-------|
| `SCALP_1H_RANGE_MIN_PCT` | `0.5` | Coin phải active (1h range min %) |
| `SCALP_ACTIVE_HOURS_UTC` | rỗng (24/7) | Ví dụ `8-16` = 8h–16h UTC |
| `SCALP_WHALE_HOURS` | `1` | Whale data context (1h cho scalp) |

## 3. Chạy agent

```bash
python main.py
```

Hoặc với `uv`:

```bash
uv run python main.py
```

### Flow khi chạy

1. **Start** → Validate config, khởi động Telegram (nếu không skip)
2. **Scan** (mỗi 5 phút scalp / 15 phút swing) → Phân tích pairs
3. **Signal** → Rule filter + Claude → Nếu pass → gửi Telegram
4. **Approve** → User bấm Approve/Skip trong 2 phút (scalp) hoặc 5 phút (swing)
5. **Execute** → Paper: fill tại entry (hoặc no-fill nếu giá vượt > 0.2%)
6. **Monitor** → Check SL/TP mỗi 1 phút (scalp) hoặc 2 phút (swing)

### Dừng

`Ctrl+C` → Graceful shutdown.

## 4. Chế độ chạy phổ biến

### Paper + Scalp + Opportunity (dynamic pairs)

```env
PAPER_TRADING=true
SCAN_MODE=opportunity
TRADING_STYLE=scalp
SKIP_TELEGRAM=false
```

### Paper + Swing + Fixed pairs

```env
PAPER_TRADING=true
SCAN_MODE=fixed
TRADING_STYLE=swing
ALLOWED_PAIRS=BTCUSDT,ETHUSDT,BNBUSDT
```

### Chạy không Telegram (test)

```env
SKIP_TELEGRAM=true
```

→ Agent chạy nhưng không gửi alert, không có approve. Signals vẫn được tạo và lưu DB.

## 5. Kiểm tra config trước khi chạy

```bash
python -c "
from config import cfg
cfg.validate()
print('Config OK')
print('Trading style:', cfg.scan.trading_style)
print('Scan interval:', cfg.scan.scan_interval_min, 'min')
print('Paper:', cfg.trading.paper_trading)
"
```

## 6. Web UI (optional)

```bash
python -m web.app
```

→ Mở http://localhost:5000 để xem dashboard, signals, trades.

## 7. Data & logs

- **DB:** `data/trading.db`
- **Logs:** `data/logs/trading_*.log`, `data/logs/errors_*.log`
- **Daily report:** Chạy lúc 8h sáng (VN time)

## 8. Lưu ý

- **Budget:** $0.75/ngày ≈ 150 Claude calls. Khi hết budget, agent ngừng analyze nhưng vẫn scan.
- **Paper no-fill:** Scalp pullback entry — nếu giá vượt entry > 0.2% khi approve → signal bị cancel (simulate limit không fill).
- **Live trading:** Hiện disabled (`NotImplementedError`). Chỉ chạy paper.
