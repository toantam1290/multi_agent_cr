# 009 — Opportunity Screening Operations Runbook

**Ngày:** 2026-03-07  
**Mục tiêu:** Chuẩn hóa quy trình vận hành từ preflight, rollout, monitoring, đến incident handling.

---

## 1) Preflight checklist (trước khi bật feature)

- [ ] Đã merge đủ PR1 -> PR5.
- [ ] `SCAN_MODE=opportunity` đã có nhưng chưa bật production.
- [ ] `SCAN_DRY_RUN=true` sẵn sàng cho giai đoạn kiểm tra.
- [ ] Validation config pass khi startup.
- [ ] Có log funnel metrics mỗi cycle.
- [ ] Có script exporter theo spec 006 (hoặc kế hoạch export rõ ràng).

---

## 2) Rollout stages

## Stage A — Dry-run (24-48h)

Mục tiêu:

- Kiểm tra screening logic không crash.
- Kiểm tra số cặp và funnel hợp lý.
- Không tạo signal/trade.

Criteria pass:

- [ ] Không có crash cycle scan.
- [ ] `signals_generated = 0` (vì dry-run).
- [ ] `pairs_scanned` không vượt cap bất thường.
- [ ] Không fallback liên tục.

## Stage B — Paper small size (3-5 ngày)

Mục tiêu:

- Kiểm tra chất lượng sau full flow.
- Đo efficiency và drawdown.

Criteria pass:

- [ ] Có đủ dữ liệu cho `daily_dashboard.csv`.
- [ ] `quality_score >= 65` ít nhất 3/5 ngày.
- [ ] Không có incident P0.

## Stage C — Paper full config (5-7 ngày)

Mục tiêu:

- Kiểm tra tính ổn định khi tăng coverage.
- Kiểm tra action policy theo score.

Criteria pass:

- [ ] `quality_score >= 65` ít nhất 5/7 ngày.
- [ ] Không vượt guardrail drawdown nội bộ.
- [ ] Churn không tăng đột biến sau confluence/cooldown.

## Stage D — Live guarded

Mục tiêu:

- Bật live với rủi ro kiểm soát.

Điều kiện vào stage:

- [ ] Đủ dữ liệu 2 tuần paper.
- [ ] Có rollback playbook chạy được.
- [ ] Có owner trực monitor trong khung giờ rollout.

---

## 3) Daily operations checklist

Mỗi ngày (sau UTC rollover):

1. Export `daily_dashboard.csv`, `pair_daily.csv`, `funnel_daily.csv`.
2. Kiểm tra data quality:
   - `signals_total >= approved_signals >= executed_trades`
   - không null bất thường ở `fees_usdt`, `closed_at`.
3. Tính `quality_score` và `action`.
4. Ghi quyết định ngày mới:
   - `HOLD`
   - `TIGHTEN_FILTER`
   - `DEFENSIVE_MODE`
   - `SCALE_UP_SMALL`
5. Ghi change log nếu chỉnh config.

---

## 4) Guardrails bắt buộc

- Hard stop theo ngày (theo quy định risk của hệ thống).
- Max number of scans/pairs per cycle theo config.
- Không tăng đồng thời quá 2 tham số trong 1 ngày.
- Khi `DATA_ISSUE`: không dùng score để tuning.

---

## 5) Incident response

## P0 — Scan loop crash / bot không chạy

Triệu chứng:

- Không có log cycle mới.
- Không có signal/trade update dù scheduler đang chạy.

Hành động:

1. Chuyển `SCAN_MODE=fixed` tạm thời.
2. Restart service.
3. Thu thập log lỗi cuối cùng.
4. Mở incident ticket + owner xử lý root cause.

## P1 — Data quality lỗi nặng

Triệu chứng:

- CSV thiếu cột, tỷ lệ không hợp lệ, số liệu âm vô lý.

Hành động:

1. Gắn cờ `action=DATA_ISSUE`.
2. Dừng mọi config tuning.
3. Re-run exporter và xác nhận DB consistency.

## P2 — Chất lượng tín hiệu giảm liên tục

Triệu chứng:

- `quality_score < 50` >= 2 ngày liên tiếp.

Hành động:

1. Bật `DEFENSIVE_MODE`.
2. Tăng filter quality (`min_quote_volume`, confluence).
3. Giảm coverage hoặc chỉ giữ core + majors.

---

## 6) Emergency rollback

Rollback cấp tốc khi có sự cố runtime:

1. Set `SCAN_MODE=fixed`.
2. (Nếu cần) set `SCAN_DRY_RUN=true`.
3. Restart process.
4. Xác nhận cycle scan phục hồi.
5. Ghi postmortem ngắn:
   - Trigger
   - Blast radius
   - Recovery time
   - Preventive action

---

## 7) Change management

Mọi thay đổi threshold cần ghi:

- Ngày giờ
- Người thay đổi
- Giá trị cũ -> mới
- Lý do thay đổi
- Kỳ vọng KPI

Không thay công thức score quá 1 lần/tuần.

---

## 8) Weekly review agenda

1. Tóm tắt score 7 ngày.
2. Funnel conversion theo ngày.
3. Top/Bottom pairs theo net PnL.
4. Cost efficiency (PnL / Anthropic spend).
5. Quyết định tuần tới:
   - giữ config
   - siết filter
   - mở coverage

---

## 9) Handover checklist (khi đổi ca vận hành)

- [ ] Trạng thái rollout hiện tại (A/B/C/D).
- [ ] Config đang chạy.
- [ ] 3 risk nổi bật hiện tại.
- [ ] Incident mở/chưa đóng.
- [ ] Việc cần làm trong 24h tới.
