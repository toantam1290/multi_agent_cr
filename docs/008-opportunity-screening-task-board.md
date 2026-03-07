# 008 — Opportunity Screening Task Board

**Ngày:** 2026-03-07  
**Mục đích:** Board copy-paste để giao việc theo ngày, theo owner, theo độ ưu tiên.

---

## Cách dùng board

- Mỗi task có 1 owner chính và 1 reviewer.
- Chỉ cho phép 1 task `IN_PROGRESS` trên mỗi owner.
- Chỉ đánh `DONE` khi có evidence (test output, log, screenshot, hoặc CSV).
- Nếu blocked > 1 ngày, phải ghi rõ blocker và phương án tháo gỡ.

---

## Trạng thái

- `TODO`
- `IN_PROGRESS`
- `REVIEW`
- `DONE`
- `BLOCKED`

---

## Sprint board (2 tuần)

| ID | Task | Ưu tiên | Owner | Reviewer | ETA | Status | Deliverable |
|---|---|---|---|---|---|---|---|
| OS-001 | Add config + env + validation | Must | Dev A | Lead | Day 1 | TODO | PR1 merged |
| OS-002 | Implement market-wide fetchers | Must | Dev A | Dev B | Day 1 | TODO | PR2 merged |
| OS-003 | Implement `get_opportunity_pairs()` | Must | Dev B | Lead | Day 2 | TODO | PR3 merged + unit tests |
| OS-004 | Integrate `run_full_scan()` mode switch | Must | Dev B | Dev A | Day 3 | TODO | PR4 merged |
| OS-005 | Add fallback behavior + logs | Must | Dev B | Lead | Day 3 | TODO | Fallback test pass |
| OS-006 | Add observability funnel metrics | Must | Dev C | Lead | Day 4 | TODO | Metrics visible in logs |
| OS-007 | Add dry-run mode | Must | Dev C | Dev A | Day 4 | TODO | Dry-run no signal creation |
| OS-008 | Confluence score >= 2 | Should | Dev A | Dev C | Day 5 | TODO | PR6 part 1 |
| OS-009 | Cooldown/hysteresis | Should | Dev A | Lead | Day 5 | TODO | PR6 part 2 |
| OS-010 | CSV exporter daily dashboard | Should | Dev C | Dev B | Day 6 | TODO | `daily_dashboard.csv` |
| OS-011 | Pair/funnel CSV exports | Should | Dev C | Lead | Day 6 | TODO | `pair_daily.csv`, `funnel_daily.csv` |
| OS-012 | Quality score + action mapping | Should | Dev C | Dev A | Day 7 | TODO | Score in CSV |
| OS-013 | 48h dry-run verification | Must | QA | Lead | Day 8-9 | TODO | Verification report |
| OS-014 | 3-5 ngày paper trading rollout | Must | QA + Ops | Lead | Day 10-14 | TODO | Rollout summary |

---

## Worklog template (copy/paste)

```md
### [OS-XXX] <Task name>
- Owner:
- Reviewer:
- Start:
- End:
- Status: TODO | IN_PROGRESS | REVIEW | DONE | BLOCKED
- Scope:
  - file1
  - file2
- Test evidence:
  - Unit:
  - Integration:
  - Runtime logs:
- Risks:
- Rollback plan:
- Notes:
```

---

## Definition of Done (DoD)

Một task chỉ `DONE` khi đủ:

1. Code merged vào branch chính theo đúng thứ tự PR.
2. Có test evidence cho thay đổi chính.
3. Không phá backward compatibility của `SCAN_MODE=fixed`.
4. Không tạo lỗi mới trong cycle scan cơ bản.
5. Có cập nhật docs tương ứng (005/006/007).

---

## Blocker log template

```md
### Blocker [OS-XXX]
- Time:
- Symptom:
- Root cause hypothesis:
- Impact:
- Temporary workaround:
- Owner follow-up:
- ETA unblock:
```

---

## Cuối mỗi ngày cần báo cáo gì

1. % task done theo Must/Should.
2. Các task đang blocked và nguyên nhân.
3. Rủi ro ảnh hưởng timeline.
4. Quyết định config cho ngày hôm sau.
