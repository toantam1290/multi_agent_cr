# 001 — Implement Bug Fixes (Paper Trading)

**Ngày:** 2025-03-04  
Tài liệu mô tả các thay đổi đã thực hiện để fix các bug được phát hiện trong codebase.

---

## Mục lục

| # | Mục | File | Trạng thái |
|---|-----|------|------------|
| [001](#001-bug-1-model-id-sai) | Bug #1: Model ID sai | `agents/research_agent.py` | ✅ Done |
| [002](#002-bug-2-sync-anthropic-client) | Bug #2: Sync Anthropic client | `agents/research_agent.py` | ✅ Done |
| [003](#003-bug-4-circuit-breaker) | Bug #4: Circuit breaker | `main.py` | ✅ Done |
| [004](#004-bug-9-sqlite-check_same_thread) | Bug #9: SQLite check_same_thread | `database.py` | ✅ Đã có sẵn |
| [007](#007-whale-data-label) | Whale data label sai | `agents/research_agent.py` | ✅ Done |
| [008](#008-telegram-markdown) | Telegram Markdown silent fail | `telegram_bot.py` | ✅ Done |
| [005](#005-chưa-implement) | Chưa implement | — | 📋 Backlog |
| [006](#006-cách-test) | Cách test | — | — |

---

## 001. Bug #1: Model ID sai (Nghiêm trọng)

**File:** `agents/research_agent.py`

**Vấn đề:** Model `claude-sonnet-4-20250514` không tồn tại → mọi API call fail → không có signal nào được tạo.

**Fix:**
- Đổi model ID từ `claude-sonnet-4-20250514` → `claude-opus-4-6`
- **Lý do chọn Opus:** Trading signal analysis ảnh hưởng trực tiếp tiền; 6 pairs × 15 phút = rất ít API call, chi phí chênh lệch không đáng kể so với chất lượng reasoning tốt hơn.

```diff
- model="claude-sonnet-4-20250514",
+ model="claude-opus-4-6",
```

---

## 002. Bug #2: Sync Anthropic client chặn event loop

**File:** `agents/research_agent.py`

**Vấn đề:** `anthropic.Anthropic()` (sync client) được gọi trong `async def _claude_analyze()`. Lệnh `messages.create()` block toàn bộ asyncio event loop 2–5 giây mỗi call. Khi 6 pairs chạy song song qua `asyncio.gather`, chúng serialize thay vì parallel thực sự.

**Fix:**
- Đổi sang `AsyncAnthropic` từ `anthropic`
- Dùng `await self.client.messages.create(...)` thay vì gọi sync
- Các pair giờ có thể gọi Claude API song song thực sự

```diff
- import anthropic
+ from anthropic import AsyncAnthropic

- self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
+ self.client = AsyncAnthropic(api_key=cfg.anthropic_api_key)

- response = self.client.messages.create(
+ response = await self.client.messages.create(
```

---

## 003. Bug #4: Circuit breaker không bao giờ reset

**File:** `main.py`

**Vấn đề:** Khi circuit breaker trigger (daily loss vượt limit), market_scan bị pause. Logic cũ đã có resume khi `daily_pnl >= -max_loss` trong cùng ngày; tuy nhiên để rõ ràng và robust hơn, thêm logic **reset khi qua ngày mới** (date-based).

**Verify:** Code gốc đã có `if daily_pnl >= -max_loss: ... resume_job("market_scan")`. Fix bổ sung `_circuit_breaker_date` để reset chắc chắn khi sang ngày mới.

**Fix:**
- Thêm `_circuit_breaker_date: date | None` để lưu ngày trigger
- Khi `date.today() > _circuit_breaker_date` → reset và resume ngay (không cần đợi PnL hồi phục)
- Khi PnL hồi phục trong cùng ngày (`daily_pnl >= -max_loss`) → cũng reset và resume

```diff
+ self._circuit_breaker_date: date | None = None

+ # Reset khi qua ngày mới
+ if self._circuit_breaker_triggered and self._circuit_breaker_date and today > self._circuit_breaker_date:
+     self._circuit_breaker_triggered = False
+     self._circuit_breaker_date = None
+     self.scheduler.resume_job("market_scan")
+     ...
```

---

## 004. Bug #9: SQLite check_same_thread

**File:** `database.py`

**Trạng thái:** Đã có sẵn `check_same_thread=False` trong code hiện tại (line 16). Không cần thay đổi.

```python
self.conn = sqlite3.connect(db_path, check_same_thread=False)
```

---

## 007. Whale data label sai

**File:** `agents/research_agent.py`

**Vấn đề:** WhaleDataFetcher lấy Binance aggTrades (spot order gộp lại), nhưng prompt ghi "Exchange Inflow/Outflow" (on-chain whale flow) → Claude phân tích dựa trên data bị hiểu lầm.

**Fix:** Đổi label trong prompt thành "Spot Aggregate Volume" — Aggregate Buy Volume / Aggregate Sell Volume.

```diff
- WHALE ACTIVITY (score: ...):
- - Exchange Inflow: ... (bearish - người bán nạp vào sàn)
- - Exchange Outflow: ... (bullish - rút ra cold wallet)
+ SPOT AGGREGATE VOLUME (score: ...):
+ - Aggregate Sell Volume: ... (bearish pressure)
+ - Aggregate Buy Volume: ... (bullish pressure)
```

---

## 008. Telegram Markdown silent failures

**File:** `telegram_bot.py`

**Vấn đề:** Nếu reasoning chứa ký tự `_` hoặc `*` (rất hay với text tiếng Việt/số float), `send_signal_alert` với `parse_mode=Markdown` throw exception → signal bị drop.

**Fix:** `parse_mode=None` trong `send_signal_alert` — mất format đẹp nhưng signal không bao giờ bị drop.

---

## 005. Chưa implement (để sau)

| Bug | Mô tả | Lý do chưa làm |
|-----|-------|----------------|
| #3 | OCO orders cho live trading (tránh double exposure) | Cần Binance OCO API, phức tạp hơn |
| #5 | Portfolio available thay vì total | Cần fetch balance từ Binance |
| #6 | Price rounding theo tickSize | Cần Binance exchange info API |
| #7–12 | Timezone, retry, health endpoint, backtest | Ưu tiên thấp |

---

## 006. Cách test

1. **Chạy:** `python main.py` với `PAPER_TRADING=true`
2. **Verify Claude API:** Trong log tìm `"Signal created:"` hoặc `"No trade opportunity"` — confirm response hợp lệ
3. **Test /approve:** Khi có signal, gửi `/approve <8_chars_id>` để verify execute flow
4. **Test circuit breaker:** Tạm set `MAX_DAILY_LOSS_PCT=0.0001` trong `.env` → trigger loss → verify pause → đổi sang ngày mới hoặc PnL hồi phục → verify resume
