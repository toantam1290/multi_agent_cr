# 006 — Daily Metrics & Quality Score Spec

**Ngày:** 2026-03-07  
**Mục đích:** Chuẩn hóa log/CSV/score để mỗi ngày có thể quyết định giữ config hay chỉnh filter một cách nhất quán.

---

## Scope

Spec này bám schema hiện tại:

- `signals`
- `trades`
- `daily_stats`
- `agent_logs`

Không yêu cầu thay đổi kiến trúc trading core. Có thể áp dụng ngay để theo dõi.

---

## 1) CSV outputs

### 1.1 `daily_dashboard.csv` (bắt buộc)

```csv
date_utc,signals_total,approved_signals,executed_trades,winning_trades,approve_rate_pct,execute_rate_pct,win_rate_pct,gross_pnl_usdt,fees_usdt,net_pnl_usdt,avg_trade_pnl_pct,worst_trade_pnl_pct,avg_confidence,avg_risk_reward,anthropic_spend_usd,efficiency_usdt_per_usd,quality_score,action
```

### 1.2 `pair_daily.csv` (nên có)

```csv
date_utc,pair,trades,wins,win_rate_pct,gross_pnl_usdt,fees_usdt,net_pnl_usdt,avg_pnl_pct,worst_pnl_pct,avg_confidence
```

### 1.3 `funnel_daily.csv` (nên có)

```csv
date_utc,signals_total,approved_signals,executed_trades,approve_rate_pct,execute_rate_pct,win_rate_pct
```

---

## 2) SQL data extraction (SQLite)

## 2.1 Daily base table

```sql
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
  SELECT
    date AS d,
    COALESCE(anthropic_spend_usd, 0) AS anthropic_spend_usd
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

ORDER BY date_utc DESC;
```

---

## 3) Metric formulas

Các công thức sau tính ở tầng export script (Python/Sheets), không nhét vào DB:

- `approve_rate_pct = approved_signals / signals_total * 100` (nếu mẫu số = 0 -> 0)
- `execute_rate_pct = executed_trades / approved_signals * 100` (nếu mẫu số = 0 -> 0)
- `win_rate_pct = winning_trades / executed_trades * 100` (nếu mẫu số = 0 -> 0)
- `efficiency_usdt_per_usd = net_pnl_usdt / anthropic_spend_usd` (nếu spend = 0 -> 0)

---

## 4) Quality score model (0-100)

## 4.1 Component scores

- `S_win = clamp(win_rate_pct, 0, 100)`
- `S_pnl = clamp(50 + 8 * avg_trade_pnl_pct, 0, 100)`
- `S_drawdown = clamp(100 - 6 * abs(min(worst_trade_pnl_pct, 0)), 0, 100)`
- `S_eff = clamp(10 * efficiency_usdt_per_usd, 0, 100)`
- `S_conf = clamp(avg_confidence, 0, 100)`

## 4.2 Final score

```text
quality_score =
  0.35 * S_win +
  0.25 * S_pnl +
  0.15 * S_drawdown +
  0.15 * S_eff +
  0.10 * S_conf
```

## 4.3 Action mapping

- `score >= 80` -> `SCALE_UP_SMALL`
- `65 <= score < 80` -> `HOLD`
- `50 <= score < 65` -> `TIGHTEN_FILTER`
- `score < 50` -> `DEFENSIVE_MODE`

---

## 5) Action policy (khuyến nghị vận hành)

### `SCALE_UP_SMALL`

- Tăng nhẹ `max_pairs_per_scan` (+10%).
- Không nới đồng thời tất cả threshold.
- Theo dõi 2 ngày kế tiếp, nếu score tụt -> rollback.

### `HOLD`

- Giữ nguyên config.
- Chỉ quan sát funnel và execution quality.

### `TIGHTEN_FILTER`

- Tăng `min_quote_volume_usd`.
- Nâng confluence lên `>= 2` nếu đang quá nhiễu.
- Giảm `max_pairs_per_scan`.

### `DEFENSIVE_MODE`

- Ưu tiên `core_pairs` + majors.
- Tạm siết `max_volatility_pct`.
- Giảm tần suất scan hoặc giảm size.

---

## 6) Data quality checks

Mỗi ngày trước khi đọc score:

1. `daily_stats` có row ngày hiện tại.
2. `signals_total >= approved_signals >= executed_trades` (lý tưởng).
3. `fees_usdt` không null khi `executed_trades > 0`.
4. Không có trade đóng nhưng thiếu `closed_at`.

Nếu fail check:

- Gắn cờ `action = DATA_ISSUE`.
- Không dùng score ngày đó để quyết định tuning.

---

## 7) Minimum exporter contract

Exporter (script hoặc notebook) cần:

1. Query dữ liệu theo SQL mục 2.
2. Tính rate + score theo mục 3,4.
3. Ghi `daily_dashboard.csv`.
4. (Nên) Ghi thêm `pair_daily.csv`, `funnel_daily.csv`.
5. Log thời gian export và số bản ghi.

---

## 8) Governance

- Không đổi công thức score quá 1 lần/tuần.
- Mọi thay đổi trọng số/threshold phải ghi changelog.
- Khi thay công thức, giữ song song `quality_score_v1` và `v2` ít nhất 7 ngày để so sánh.
