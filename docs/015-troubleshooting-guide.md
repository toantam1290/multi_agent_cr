# Hướng dẫn troubleshoot — Khi system không chạy đúng ý

Khi trading system không hoạt động như mong đợi, thu thập data theo guide này để tự phân tích hoặc gửi người khác review.

---

## Khi nào cần phân tích?

| Triệu chứng | Nguyên nhân thường gặp |
|-------------|------------------------|
| **Ít/không có signal** | Filter quá chặt, budget hết, scan không tìm được pair |
| **Signal nhiều nhưng thua liên tục** | Indicator sai hướng, SL quá tight, entry stale |
| **Signal tốt nhưng không execute** | Freshness guard cancel, paper no-fill, approval timeout |
| **ConnectTimeout / HTTP request failed** | Mạng chậm, Binance bị throttle, firewall/VPN chặn |

---

## Data cần gửi — theo từng trường hợp

### Trường hợp 1: Ít/không có signal

1. **Log** `data/logs/trading_*.log` — lọc dòng chứa `Rule-based filter` và `Scan cycle funnel`
2. **Funnel metrics** từ log: `opportunity_candidates` / `pairs_scanned` / `rule_based_passed` / `claude_passed` / `signals_generated`
3. **Config** — `.env` (che API keys trước khi gửi)
4. **Query DB:**
   ```sql
   SELECT * FROM agent_logs
   WHERE agent='research_agent' AND timestamp > datetime('now','-1 day')
   ORDER BY timestamp DESC LIMIT 100;
   ```

### Trường hợp 2: Signal thua liên tục

1. **Trade history:**
   ```sql
   SELECT t.pair, t.direction, t.entry_price, t.stop_loss, t.take_profit,
          t.exit_price, t.pnl_usdt, t.pnl_pct, t.status,
          s.confidence, s.regime, s.reasoning, s.cancel_reason,
          s.raw_json
   FROM trades t
   JOIN signals s ON t.signal_id = s.id
   WHERE t.status IN ('CLOSED','STOPPED','TOOK_PROFIT')
   ORDER BY t.opened_at DESC LIMIT 50;
   ```
2. **Pattern cần note tay** (DB không lưu tự động):
   - Giá thực tế lúc fill vs `entry_price` (drift)
   - Giá lúc SL hit vs `stop_loss` (slippage tại exit)
   - Khoảng cách entry → SL hit (phút)
3. **Claude reasoning** — field `reasoning` trong signals

### Trường hợp 3: Signal bị cancel/không execute

1. **Signals bị cancel:**
   ```sql
   SELECT id, pair, direction, entry_price, stop_loss,
          status, cancel_reason, created_at, reasoning
   FROM signals
   WHERE status IN ('CANCELLED','SKIPPED')
   ORDER BY created_at DESC LIMIT 50;
   ```
2. **Log cancel:**
   ```bash
   grep -i "cancelled\|no fill\|expired\|broke SL\|rejected" data/logs/trading_*.log | tail -50
   ```

---

## Template gửi khi cần phân tích

```markdown
## Vấn đề:
[Mô tả ngắn: ít signal / thua nhiều / không execute]

## Thời gian xảy ra:
[Từ ... đến ...]

## Scan funnel (từ log):
opportunity_candidates: X
pairs_scanned: X
rule_based_passed: X
claude_passed: X
signals_generated: X

## Trade history (paste output SQL):
[copy kết quả query trades JOIN signals]

## Signal bị cancel (nếu relevant):
[copy kết quả query signals WHERE status=CANCELLED]

## Config hiện tại:
SCAN_MODE=...
TRADING_STYLE=...
SCAN_INTERVAL_MIN=...
FUNDING_SHORT_MIN_PCT=...
SCALP_MIN_CONFIDENCE=...
SCALP_1H_RANGE_MIN_PCT=...
```

---

## ConnectTimeout / HTTP request failed

Khi log xuất hiện `ConnectTimeout`, `ReadTimeout`, `HTTP request failed (attempt 1/3)`:

| Nguyên nhân | Cách xử lý |
|-------------|------------|
| Mạng chậm / Binance xa | Tăng `HTTP_TIMEOUT_SEC=45` hoặc `60` trong `.env` |
| Binance bị chặn (VN, một số ISP) | Dùng VPN hoặc proxy |
| Firewall chặn `api.binance.com`, `fapi.binance.com` | Mở port 443, whitelist domain |

**Env:**
```bash
# .env — tăng timeout (mặc định 30s)
HTTP_TIMEOUT_SEC=45
```

---

## Gợi ý tự debug trước khi gửi

| Check | Command / Cách làm |
|-------|--------------------|
| Budget còn không? | `SELECT * FROM daily_stats WHERE date=date('now');` |
| Có pairs nào đang scan? | Log dòng `Opportunity scan:` |
| Filter reject vì gì? | Log dòng `Rule-based filter → No trade` |
| Claude WAIT/AVOID vì gì? | Field `reasoning` trong signals |
| Lý do cancel? | Field `cancel_reason` trong signals (nếu có) |
| Fill rate | Count `status='EXECUTED'` vs `status='CANCELLED'` có `cancel_reason='no_fill'` |

---

## Giá trị `cancel_reason` (từ v9)

| Giá trị | Ý nghĩa |
|---------|---------|
| `risk_check:...` | Risk Manager reject (re-validate trước execute) |
| `freshness_broke_sl` | Giá phá SL trước khi approve (scalp) |
| `no_fill` | Paper: limit không fill (giá vượt entry > 0.2%) |
| `exec_failed` | Execute trả None (swing) |
| `exec_error:...` | Exception khi execute |
| `timeout` | User không approve trong thời gian (scalp 2 phút) |
| `user_skip` | User bấm Skip |

---

## Script nhanh để export data

```bash
# Funnel từ log (Linux/Mac)
grep "Scan cycle funnel" data/logs/trading_*.log | tail -5

# SQLite export
sqlite3 data/trading.db "SELECT * FROM signals WHERE status='CANCELLED' ORDER BY created_at DESC LIMIT 20;" -header -csv
```
