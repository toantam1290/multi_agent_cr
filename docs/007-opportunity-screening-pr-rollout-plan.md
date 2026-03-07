# 007 — Opportunity Screening PR Rollout Plan

**Ngày:** 2026-03-07  
**Mục tiêu:** Chia triển khai Opportunity Screening thành các PR nhỏ, review dễ, rollback an toàn, hạn chế rủi ro production.

---

## 0) Nguyên tắc rollout

- Mỗi PR chỉ giải quyết 1 nhóm vấn đề.
- PR sau chỉ merge khi PR trước đã pass test và chạy smoke test ổn.
- Không bật ngay toàn bộ feature ở production; luôn có giai đoạn `dry-run`.
- Ưu tiên backward-compatible, giữ `SCAN_MODE=fixed` là default.

---

## 1) Kế hoạch theo PR

| PR | Mục tiêu | File chính | Rủi ro | Thời lượng ước tính |
|---|---|---|---|---|
| PR1 | Config + validation + docs env | `config.py`, `.env.example` | Thấp | 0.5 ngày |
| PR2 | Fetcher market-wide data | `utils/market_data.py` | Thấp-Trung bình | 0.5 ngày |
| PR3 | Opportunity filter core | `utils/market_data.py` | Trung bình | 1 ngày |
| PR4 | Tích hợp `run_full_scan()` + fallback | `agents/research_agent.py` | Trung bình | 1 ngày |
| PR5 | Observability + dry-run | `agents/research_agent.py`, `database.py` | Trung bình | 0.5-1 ngày |
| PR6 | Confluence + cooldown/hysteresis | `utils/market_data.py`, `agents/research_agent.py` | Trung bình-Cao | 1 ngày |
| PR7 | Export metrics CSV + score automation | `utils/backtest_report.py` (hoặc script mới), docs | Trung bình | 1 ngày |

---

## PR1 — Config, env, validation (foundation)

## Phạm vi

- Thêm config cho opportunity mode.
- Thêm parse list env (`CORE_PAIRS`, `SCAN_BLACKLIST`).
- Validation config ngay khi startup.

## Files

- `config.py`
- `.env.example`
- (Optional) `CLAUDE.md` hoặc docs hướng dẫn env

## Checklist implement

- [ ] Thêm fields:
  - `scan_mode`
  - `opportunity_volatility_pct`
  - `opportunity_volatility_max_pct`
  - `min_quote_volume_usd`
  - `max_pairs_per_scan`
  - `core_pairs`
  - `scan_blacklist`
  - `opportunity_use_whitelist`
  - `scan_dry_run`
- [ ] Validation:
  - `scan_mode` chỉ `fixed|opportunity`
  - `min < max`
  - `max_pairs_per_scan > 0`
  - `core_pairs` không nằm trong blacklist

## Test cần chạy

- [ ] Unit: parse env list đúng (có khoảng trắng, empty item).
- [ ] Unit: validation fail đúng message cho case sai config.
- [ ] Smoke: không set env mới vẫn chạy với default.

## Done criteria

- Startup không crash với cấu hình hợp lệ.
- Có error rõ ràng với cấu hình không hợp lệ.

## Rollback

- Revert PR1 hoặc set `SCAN_MODE=fixed` + dùng default cũ.

---

## PR2 — Fetcher cho market-wide screening

## Phạm vi

- Thêm hàm lấy toàn bộ ticker 24h.
- Thêm hàm lấy tập symbols có futures.

## Files

- `utils/market_data.py`

## Checklist implement

- [ ] `get_all_tickers_24hr() -> list[dict]`
  - `GET /api/v3/ticker/24hr`
  - parse số bằng `float(x or 0)` an toàn.
- [ ] `get_futures_symbols() -> set[str]`
  - `GET /fapi/v1/premiumIndex` all symbols
  - thất bại -> return `set()` + warning log.
- [ ] Dùng `_http_get_with_retry` nhất quán.

## Test cần chạy

- [ ] Unit mock API: trả dữ liệu hợp lệ.
- [ ] Unit mock API fail: không throw chết loop, có fallback.
- [ ] Perf smoke: gọi 2 endpoint/cycle không timeout bất thường.

## Done criteria

- Có thể lấy được ticker list và futures symbols trong 1 cycle.
- Lỗi endpoint không làm crash process.

## Rollback

- Revert hàm mới, scan quay về flow cũ.

---

## PR3 — Opportunity filter core

## Phạm vi

- Implement logic chọn cặp cơ hội.
- Giữ deterministic output để dễ test.

## Files

- `utils/market_data.py`

## Checklist implement

- [ ] Thêm `get_opportunity_pairs(...)`.
- [ ] Basic filters:
  - `symbol.endswith("USDT")`
  - `quoteVolume >= min_quote_volume_usd`
  - blacklist
- [ ] Volatility range: `min <= abs(priceChangePercent) <= max`
- [ ] Futures filter (nếu futures set có dữ liệu).
- [ ] Whitelist mode (khi bật).
- [ ] Sort theo `abs(priceChangePercent)` giảm dần.
- [ ] Cap `max_pairs_per_scan`.
- [ ] Add `core_pairs` vào đầu, dedupe.

## Test matrix tối thiểu

- [ ] `priceChangePercent` dạng string
- [ ] thiếu key/null value
- [ ] symbol không USDT
- [ ] blacklist trùng core pair
- [ ] futures_symbols rỗng
- [ ] cap + dedupe đúng thứ tự
- [ ] output rỗng khi không cặp nào pass

## Done criteria

- Hàm thuần (pure-ish), test pass đầy đủ.
- Không ném exception khi dữ liệu Binance bẩn.

## Rollback

- Tạm không gọi hàm mới trong agent (giữ code hàm nhưng không bật).

---

## PR4 — Tích hợp Research Agent (scan mode switch)

## Phạm vi

- Nối opportunity filter vào `run_full_scan()`.
- Bổ sung fallback khi API lỗi.

## Files

- `agents/research_agent.py`
- `utils/market_data.py` (import/use helper)

## Checklist implement

- [ ] Nếu `cfg.scan_mode == "opportunity"`:
  - fetch `tickers` + `futures_symbols` song song
  - build `pairs_to_scan = get_opportunity_pairs(...)`
- [ ] Nếu fixed mode: giữ `ALLOWED_PAIRS`.
- [ ] Fallback:
  - ticker/premiumIndex fail -> fallback `ALLOWED_PAIRS` nếu có
  - nếu fallback rỗng -> skip cycle + error log

## Test cần chạy

- [ ] Integration: fixed mode output không đổi.
- [ ] Integration: opportunity mode có list hợp lệ.
- [ ] Failure test: endpoint fail vẫn không crash.

## Done criteria

- Có thể đổi mode bằng env, không sửa code.
- Số Claude calls không tăng bất thường so với baseline.

## Rollback

- Force `SCAN_MODE=fixed` ngay lập tức.

---

## PR5 — Observability + dry-run (Must)

## Phạm vi

- Ghi funnel metrics mỗi cycle.
- Thêm dry-run để tune threshold không đốt budget.

## Files

- `agents/research_agent.py`
- `database.py` (chỉ khi cần helper ghi log metrics)

## Checklist implement

- [ ] Log payload/cycle:
  - `scan_mode`
  - `opportunity_candidates`
  - `pairs_scanned`
  - `rule_based_passed`
  - `signals_generated`
  - `fallback_used`
- [ ] `scan_dry_run=true`:
  - log list pairs
  - không gọi `analyze_pair`
  - không tạo signal

## Test cần chạy

- [ ] Dry-run không làm tăng `signals.total`.
- [ ] Có thể tái dựng funnel từ `agent_logs`.

## Done criteria

- Dashboard có dữ liệu đủ để tuning hằng ngày.

## Rollback

- Tắt `SCAN_DRY_RUN` hoặc disable metrics log nếu quá noisy.

---

## PR6 — Phase 1.5 Quality upgrades

## Phạm vi

- Confluence score.
- Cooldown/hysteresis giảm churn.

## Files

- `utils/market_data.py`
- `agents/research_agent.py`

## Checklist implement

- [ ] Score rule:
  - +1 volatility
  - +1 volume spike
  - +1 funding extreme
- [ ] Filter theo `confluence_min_score`.
- [ ] Cooldown theo symbol và hysteresis vào/ra list.

## Test cần chạy

- [ ] Backtest/replay: số cặp re-enter/cycle giảm.
- [ ] Signal quality không giảm đáng kể.

## Done criteria

- Churn giảm rõ và cost/quality tốt hơn baseline.

## Rollback

- Set `confluence_min_score=1`
- disable cooldown/hysteresis flag.

---

## PR7 — Metrics exporter + score automation

## Phạm vi

- Export CSV daily.
- Tính `quality_score` và action suggestion.

## Files

- `utils/backtest_report.py` (mở rộng) hoặc script mới `utils/daily_metrics_report.py`
- Docs: `006-daily-metrics-score-spec.md`

## Checklist implement

- [ ] SQL extraction theo spec 006.
- [ ] Generate:
  - `daily_dashboard.csv`
  - `pair_daily.csv`
  - `funnel_daily.csv`
- [ ] Tính:
  - approve/execute/win rates
  - efficiency
  - quality_score
  - action

## Test cần chạy

- [ ] File CSV sinh đúng cột.
- [ ] Ngày không có trade/signal vẫn không crash (zero-safe).
- [ ] Score nhất quán với công thức docs.

## Done criteria

- Chạy 1 lệnh là có dashboard daily đầy đủ.

## Rollback

- Tắt cron exporter, giữ trading loop chạy bình thường.

---

## 2) Branch & merge strategy

- Nhánh đề xuất: `feature/opportunity-screening`
- Nhánh con theo PR:
  - `feature/opportunity-pr1-config`
  - `feature/opportunity-pr2-fetchers`
  - ...
- Merge tuần tự, không squash toàn bộ thành 1 mega-PR.
- Mỗi PR nên < 400 LOC thay đổi thuần logic (không tính docs/test data).

---

## 3) PR template gợi ý (ngắn gọn)

## Summary

- Mục tiêu PR
- Phạm vi file thay đổi
- Backward compatibility

## Test Plan

- Unit tests đã chạy
- Integration/smoke đã chạy
- Kết quả chính

## Risk & Rollback

- Rủi ro chính
- Cách tắt nhanh bằng config

---

## 4) Rollout environment sequence

1. Local dev + unit tests.
2. Dry-run trên staging/paper 24-48h.
3. Paper trading live (size nhỏ) 3-7 ngày.
4. Mới cân nhắc live size lớn hơn theo score.

---

## 5) Gate để chuyển phase

## Từ PR4 -> PR5

- Opportunity mode chạy ổn định 24h, không crash scan loop.

## Từ PR5 -> PR6

- Có đủ dữ liệu funnel ít nhất 3 ngày.

## Từ PR6 -> PR7

- Confluence/cooldown cải thiện churn hoặc cost-efficiency.

## Từ paper -> live size lớn hơn

- `quality_score >= 65` tối thiểu 5/7 ngày gần nhất
- Không có ngày drawdown vượt guardrail nội bộ.

---

## 6) Anti-pattern cần tránh

- Không merge nhiều nhóm logic lớn trong 1 PR.
- Không bật opportunity + confluence + cooldown cùng lúc từ ngày đầu.
- Không tuning threshold mỗi vài giờ dựa trên cảm xúc.
- Không dùng data ngày lỗi (missing logs) để quyết định config.

---

## 7) Tài liệu liên quan

- `004-dynamic-pair-screening-plan.md`
- `005-opportunity-screening-implementation-checklist.md`
- `006-daily-metrics-score-spec.md`
- `008-opportunity-screening-task-board.md`
- `009-opportunity-screening-operations-runbook.md`
- `010-opportunity-screening-single-command-execution-guide.md`
