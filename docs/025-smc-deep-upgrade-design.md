# 025 — SMC Deep Upgrade Design

## Tổng quan

Nâng cấp SMC từ mức cơ bản lên mức hiện đại (ICT 2024-2025, prop firm methodology), với kiến trúc 3 lớp:

1. **utils/smc.py** — Detection engine (nâng cấp)
2. **utils/smc_strategy.py** — Top-down multi-TF reasoning (MỚI)
3. **agents/smc_agent.py** — Standalone SMC scanner (MỚI)

## Kiến trúc

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  main.py (TradingOrchestrator)                                                │
├─────────────────────────────────────────────────────────────────────────────┤
│  research_scan (every 15m)     │  smc_scan (every 5m)                        │
│  └── ResearchAgent             │  └── SMCAgent                                │
│      └── smc.py (basic)        │      └── smc_strategy.py (deep)              │
│      context cho Claude        │      → TradingSignal độc lập                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Bước 1: Nâng cấp utils/smc.py

### Thêm mới

| Thuật toán | Mô tả |
|------------|-------|
| OB Mitigation + Breaker Block | OB bị break → mark mitigated, flip thành Breaker Block ngược chiều |
| Displacement candle | range > 1.5× ATR, body > 60% range, tạo FVG — xác nhận MSS thật |
| Premium/Discount + OTE | Fib 50% = equilibrium, 62-79% = OTE entry zone |
| PDH/PDL/PWH/PWL | Institutional reference levels |
| FVG: CE + BPR + Inversion | CE = 50% FVG, BPR = 2 FVG overlap, Inversion = FVG filled + flip |

### Interface giữ nguyên

- `SMCAnalyzer.analyze(symbol, style)` — async entry point
- `SMCAnalyzer.analyze_from_dataframes(df_structure, df_timing, current_price, df_daily=None)` — sync cho backtest

## Bước 2: utils/smc_strategy.py

### SMCStrategy

Top-down multi-TF analysis:

- **Scalp**: Daily + 1h (HTF) + 15m + 5m (LTF)
- **Swing**: Weekly + 4h (HTF) + 1h + 15m (LTF)

### SMCSetup output

```python
@dataclass
class SMCSetup:
    symbol: str
    direction: str           # LONG | SHORT
    entry_model: str         # ob_entry | ce_entry | bpr_entry | sweep_reversal
    entry_model_quality: str # A+ | A | B | C
    htf_bias: str
    mtf_bias: str
    ltf_trigger: str         # displacement | choch | bos | sweep | none
    draw_on_liquidity: float
    entry: float
    sl: float
    tp1: float
    tp2: float
    risk_reward_tp1: float
    risk_reward_tp2: float
    confidence: int
    reasoning: str
    valid: bool
```

### Quy tắc top-down

1. HTF và LTF bias phải cùng chiều
2. LTF phải có trigger (displacement, CHoCH, sweep)
3. Có entry zone (OB, CE, BPR, sweep)
4. Draw on Liquidity = target (PDH/PDL/PWH/PWL)
5. R:R tối thiểu 1:1.5 ở TP1

## Bước 3: agents/smc_agent.py

### SMCAgent

- Chạy độc lập, không cần rule-based filter, CVD, VWAP, whale
- Chỉ cần OHLCV đa timeframe
- `scan_pair(symbol)` → `Optional[TradingSignal]`
- `run_full_scan()` → `list[TradingSignal]`

### Integration

- Job `smc_scan` chạy mỗi 5 phút (scalp) hoặc 15 phút (swing)
- Cùng pipeline `_process_signal` với research signals
- Circuit breaker áp dụng cho cả 2 nguồn

## Bước 4: main.py

- Import `SMCAgent`
- `__init__`: `self.smc_agent = SMCAgent(db, telegram=self.telegram)`
- Thêm job `_smc_scan` (interval: 5 min scalp / 15 min swing)
- `stop`: `await self.smc_agent.close()`
- Circuit breaker: pause/resume cả `market_scan` và `smc_scan` khi trigger

## Tóm tắt thay đổi

| File | Hành động |
|------|-----------|
| utils/smc.py | Thay toàn bộ — OB mitigation, displacement, PD zone, PDH/PDL, FVG CE/BPR |
| utils/smc_strategy.py | Tạo mới |
| agents/smc_agent.py | Tạo mới |
| main.py | Import + init + job + _smc_scan + close |

research_agent.py không cần sửa — interface SMCAnalyzer giữ nguyên.
