# README — Opportunity Screening Docs (2-Minute Onboarding)

**Mục tiêu:** Giúp thành viên mới biết phải đọc gì trước, làm gì ngay, và kiểm tra gì để không bị lạc trong bộ tài liệu.

---

## 1) Nếu chỉ có 2 phút, đọc theo thứ tự này

1. `004-dynamic-pair-screening-plan.md`  
   -> Hiểu mục tiêu, phạm vi, và vì sao cần opportunity mode.
2. `007-opportunity-screening-pr-rollout-plan.md`  
   -> Nắm trình tự triển khai theo PR nhỏ.
3. `010-opportunity-screening-single-command-execution-guide.md`  
   -> Copy lệnh chạy ngay theo từng ngày.

---

## 2) Bạn là ai thì đọc gì trước

## Developer

Đọc:

1. `005-opportunity-screening-implementation-checklist.md`
2. `011-opportunity-screening-implementation-details.md` — Chi tiết code đã viết
3. `007-opportunity-screening-pr-rollout-plan.md`
4. `010-opportunity-screening-single-command-execution-guide.md`

Mục tiêu:

- Biết file nào cần sửa trong từng PR.
- Hiểu logic từng hàm (config, market_data, research_agent, database).
- Biết test nào phải pass trước khi merge.

## Ops / QA

Đọc:

1. `009-opportunity-screening-operations-runbook.md`
2. `006-daily-metrics-score-spec.md`
3. `010-opportunity-screening-single-command-execution-guide.md`

Mục tiêu:

- Biết cách rollout an toàn (dry-run -> paper -> guarded live).
- Biết khi nào cần rollback.

## Lead / Reviewer

Đọc:

1. `007-opportunity-screening-pr-rollout-plan.md`
2. `008-opportunity-screening-task-board.md`
3. `006-daily-metrics-score-spec.md`

Mục tiêu:

- Theo dõi tiến độ theo task/owner.
- Quyết định `HOLD / TIGHTEN_FILTER / DEFENSIVE_MODE / SCALE_UP_SMALL`.

---

## 3) Bộ tài liệu đầy đủ

- `004-dynamic-pair-screening-plan.md` — Strategy + addendum sau audit code
- `005-opportunity-screening-implementation-checklist.md` — Checklist implement theo file
- `006-daily-metrics-score-spec.md` — CSV/SQL/quality score spec
- `007-opportunity-screening-pr-rollout-plan.md` — Rollout theo PR nhỏ
- `008-opportunity-screening-task-board.md` — Task board giao việc
- `009-opportunity-screening-operations-runbook.md` — Vận hành + incident + rollback
- `010-opportunity-screening-single-command-execution-guide.md` — Command guide theo ngày
- `011-opportunity-screening-implementation-details.md` — Mô tả chi tiết code đã implement
- `012-opportunity-screening-usage-guide.md` — Hướng dẫn cách dùng (chế độ, lệnh, vận hành)

---

## 4) Day-1 quick start checklist

- [ ] Đọc `004`, `007`, `010` (<= 30 phút).
- [ ] Xác nhận `SCAN_MODE=fixed` đang chạy ổn.
- [ ] Chạy preflight commands trong `010`.
- [ ] Chọn PR đầu tiên theo `007` (thường là PR1).
- [ ] Cập nhật trạng thái task trong `008`.

---

## 5) 3 lỗi onboarding thường gặp (cần tránh)

1. Bật `SCAN_MODE=opportunity` ngay khi chưa chạy dry-run.
2. Merge nhiều thay đổi lớn trong một PR.
3. Tuning threshold theo cảm xúc khi chưa có đủ dữ liệu từ `006`.

---

## 6) Khi cần hỗ trợ nhanh

- Cần implement chi tiết theo file -> xem `005`.
- Cần hiểu code đã viết -> xem `011`.
- Cần hướng dẫn cách dùng -> xem `012`.
- Cần lệnh chạy ngay -> xem `010`.
- Cần xử lý sự cố/rollback -> xem `009`.
