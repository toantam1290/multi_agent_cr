# 028 — P0 + P1 Bug Fixes & Backtest Results

## Tổng quan

Sau review SMC engine (doc 026–027), phát hiện 8 bug P0 (critical) và 7 bug P1 (important). Fix lần lượt, chạy backtest sau mỗi đợt để đo impact.

---

## P0 Fixes (8 bugs — ảnh hưởng trực tiếp đến tính đúng đắn)

| # | Bug | File | Fix |
|---|-----|------|-----|
| 1 | Portfolio balance dùng static config, không phản ánh PnL tích lũy | `risk_manager.py`, `smc_agent.py`, `database.py` | Tính `paper_balance_usdt + cumulative_pnl` từ DB (`get_cumulative_pnl()`) |
| 2 | Funding filter chỉ block 1 chiều (LONG), SHORT không bị filter | `config.py` | Symmetric ±0.03%: `FUNDING_LONG_MAX_PCT=0.03`, `FUNDING_SHORT_MIN_PCT=-0.03` |
| 3 | Time exit luôn gán status `STOPPED` bất kể PnL dương hay âm | `main.py` | Dùng `TOOK_PROFIT` nếu `pnl > 0`, `STOPPED` nếu `pnl <= 0` |
| 4 | F&G filter ngược: block LONG khi Fear (sai, Fear = oversold = nên LONG) | `smc_agent.py` | Đảo lại: block LONG khi Greed (>75), block SHORT khi Fear (<25) |
| 5 | Confidence cascade nhân 3 multiplier liên tiếp → collapse quá mức | `smc_agent.py` | Weighted average (funding 0.4, OI 0.3, CVD 0.3) thay vì nhân |
| 6 | HTF/LTF alignment quá strict — reject khi HTF NEUTRAL hoặc disagree | `smc_strategy.py` | Cho phép với penalty: NEUTRAL → 0.8×, disagree → 0.7× |
| 7 | `rsi_4h` tính trên data 15m (sai TF) | `market_data.py` | Fetch data 4h thực tế, RSI-14 trên `df_4h` |
| 8 | Paper exit không có slippage → backtest quá lạc quan | `main.py` | SL slippage 0.1% bất lợi, TP slippage 0.05% bất lợi |

---

## P1 Fixes (7 bugs — cải thiện chất lượng tín hiệu)

| # | Bug | File | Fix |
|---|-----|------|-----|
| A | OB detection `break` sau candle đầu tiên khớp → miss OB tốt hơn | `smc.py` | Đổi `break` → `continue`, scan hết candidates |
| B | Entry tại OB edge (high/low) → fill rate thấp, dễ bị wick | `smc_strategy.py` | Entry tại OB midpoint (`ob.mid`) |
| C | A+ grade không include `ob_entry` → miss best setups | `smc_strategy.py` | Thêm `ob_entry` vào A+ criteria (cùng sweep_reversal, bpr_entry, ce_entry + in_ote) |
| D | Swing detection bỏ sót cuối data (right edge) | `smc.py` | Mở rộng scan đến cuối df (right-side asymmetric window) |
| E | SL buffer cố định % → không adapt theo volatility | `smc.py`, `smc_strategy.py` | ATR-based buffer: `0.5 × ATR`, floor = 0.2% price |
| F | Circuit breaker chỉ tính realized PnL → miss floating loss lớn | `main.py` | Cộng unrealized PnL từ open positions vào daily_pnl |
| G | Circuit breaker reset quá nhanh khi PnL hồi nhẹ | `main.py` | Hysteresis: phải recover đến 50% threshold VÀ chờ ít nhất 1h cooldown |

---

## Backtest Results

### So sánh trước → sau

| Metric | Trước P0 | Sau P0 | Sau P0+P1 |
|--------|----------|--------|-----------|
| Trades | — | — | 236 |
| Win Rate | — | — | 55.9% |
| Profit Factor | — | — | 2.68 |
| Sharpe Ratio | — | — | 5.16 |

> **Lưu ý:** Số liệu "Trước P0" và "Sau P0" chưa được ghi lại chi tiết do chạy incremental. Kết quả cuối cùng (P0+P1) là benchmark chính.

### Entry Model Breakdown

| Entry Model | Trades | Win Rate | Avg RR | Ghi chú |
|-------------|--------|----------|--------|---------|
| `ob_entry` | 150 | 74% | 2.65 | Best performer — entry tại OB midpoint + ATR SL |
| `ce_entry` | 67 | 25% | — | Vẫn yếu, cần review logic CE detection |
| `sweep_reversal` | 19 | 21% | — | Sample nhỏ, WR thấp |

### Trail Stop Performance

- Activation rate: 58.9% (trail stop triggered trên tổng trades)
- Win rate khi activated: 82%
- Trail logic: breakeven tại 50% target, lock 50% profit tại 80% target

---

## Verdict

**EDGE DETECTED** — Backtest cho thấy edge thống kê rõ ràng (PF 2.68, Sharpe 5.16) nhưng cần lưu ý:

1. Đây là backtest, không phải live. Slippage/fill rate thực tế có thể khác.
2. `ob_entry` là driver chính (150/236 trades, 74% WR). System phụ thuộc nặng vào model này.
3. `ce_entry` (25% WR) và `sweep_reversal` (21% WR) đang kéo performance xuống.

---

## Remaining Concerns & Recommendations

### Concerns
- **ce_entry weakness:** 67 trades với 25% WR là negative edge. Có thể disable hoặc thêm filter nghiêm hơn.
- **sweep_reversal sample size:** 19 trades quá nhỏ để kết luận. Cần thêm data.
- **Backtest bias:** Chưa test walk-forward hoặc out-of-sample. Kết quả có thể overfit.
- **Live execution gap:** Paper exit slippage (SL 0.1%, TP 0.05%) là ước lượng, thực tế phụ thuộc vào liquidity và order book depth.

### Recommendations
1. **Disable hoặc filter ce_entry:** Chỉ cho phép ce_entry khi confidence >= 70 hoặc khi có CVD confirmation mạnh.
2. **Walk-forward validation:** Chạy `run_optimizer.py` với walk-forward để kiểm tra robustness.
3. **Paper trading phase:** Chạy live paper ít nhất 2 tuần trước khi xem xét real money.
4. **Monitor ob_entry concentration:** Nếu ob_entry WR giảm dưới 60% trong live, cần re-evaluate toàn bộ.
5. **Circuit breaker live test:** Hysteresis logic (50% recovery + 1h cooldown) chưa được test trong điều kiện volatile thực tế.
