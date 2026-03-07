# Production Readiness Notes

Phân tích và lộ trình dựa trên review code thực tế.

## Phase 1 — Đã implement (Debug & Visibility)

- [x] **MIN_CONFIDENCE=75** — Khớp với system prompt (Claude recommend >= 75)
- [x] **Log signal filtered** — Khi confidence < min, log vào agent_logs + console
- [x] **Scan song song** — 6 pairs chạy parallel, 1 pair lỗi không block các pair khác
- [x] **Heartbeat** — Telegram mỗi giờ để biết bot còn sống

## Phase 2 — Paper Trading Validation (1–3 tháng)

- [ ] Chạy paper trading với real market data
- [ ] Track: win rate, avg R:R thực tế, max drawdown
- [ ] Backtest 6–12 tháng historical data
- [ ] Chỉ tiến nếu win rate >= 55% và Sharpe >= 1.0

## Phase 3 — Infrastructure

- [ ] Deploy VPS/cloud (không chạy local)
- [ ] Systemd service + auto-restart
- [ ] Health endpoint (UptimeRobot)
- [ ] Fix TODO: fetch real Binance balance (hiện hardcode 10000)

## Phase 4 — Live Trading (nhỏ)

- [ ] $500–1000 thật, KHÔNG $10,000
- [ ] Monitor mỗi ngày tháng đầu
- [ ] So sánh paper vs live

## Vấn đề còn tồn tại

| Vấn đề | Chi tiết |
|--------|----------|
| Whale data | aggTrades ≠ on-chain whale. Cần Glassnode/CryptoQuant API nếu muốn on-chain thật |
| R:R constraint | SL max 2%, TP min 4% — khó đạt trong sideways market |
| Chưa backtest | Không có bằng chứng strategy profitable |
| Chưa market regime | Cùng strategy trong bull/bear/sideways = thua |
| SQLite | Không scale với nhiều pairs |

## Kiểm tra log

```sql
SELECT agent, level, message, data FROM agent_logs ORDER BY id DESC LIMIT 50;
```

Tìm dòng `"Signal filtered"` để xem confidence bị reject.

## Câu hỏi cốt lõi

**Claude có edge thật không?** Mọi data feed cho Claude (OHLCV, RSI, MACD, Fear&Greed) đều là public information. LLM phân tích public data = không có informational edge.

Giá trị thật nhất của project: **behavioral edge** — discipline qua Telegram giúp không trade impulsively.
