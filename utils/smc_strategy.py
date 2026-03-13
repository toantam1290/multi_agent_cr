"""
utils/smc_strategy.py - SMC Top-Down Strategy Engine

Top-down multi-TF analysis theo ICT methodology:
  Daily → HTF bias + Draw on Liquidity + PDH/PDL
  4h/1h → MTF structure + OB/FVG zones
  15m/5m → LTF trigger (displacement + MSS + entry)

Output: SMCSetup — entry model đầy đủ với entry/SL/TP
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

import pandas as pd
from loguru import logger

from utils.smc import SMCAnalyzer, SMCSignal

if TYPE_CHECKING:
    from utils.market_data import BinanceDataFetcher


@dataclass
class SMCSetup:
    """
    Output của SMCStrategy — một trade setup hoàn chỉnh từ SMC.
    entry_model mô tả cơ sở kỹ thuật của setup.
    """
    symbol: str
    direction: str             # "LONG" | "SHORT"

    # Entry model
    entry_model: str           # "ob_entry" | "ce_entry" | "bpr_entry" | "sweep_reversal"
    entry_model_quality: str   # "A+" | "A" | "B" | "C"

    # Multi-TF context
    htf_bias: str              # "BULLISH" | "BEARISH" | "NEUTRAL" (từ daily)
    mtf_bias: str              # "BULLISH" | "BEARISH" | "NEUTRAL" (từ 4h/1h)
    ltf_trigger: str           # "displacement" | "choch" | "bos" | "sweep" | "none"

    # Price levels
    draw_on_liquidity: float   # Target — PDH/PWH (long) hoặc PDL/PWL (short)
    entry: float
    sl: float
    tp1: float                 # Conservative target (50% draw to DOL)
    tp2: float                 # Full draw on liquidity
    risk_reward_tp1: float
    risk_reward_tp2: float

    # SMC signals dùng để build setup
    htf_signal: Optional[SMCSignal] = None
    ltf_signal: Optional[SMCSignal] = None

    # Metadata
    confidence: int = 0        # 0-100
    reasoning: str = ""
    valid: bool = False

    # Flag cho orchestrator biết setup này từ SMC standalone
    source: str = "smc_standalone"


class SMCStrategy:
    """
    Top-down multi-TF analysis.
    Fetch nhiều timeframe, chạy SMCAnalyzer từng TF, kết hợp top-down.
    """

    def __init__(
        self,
        fetcher: Optional["BinanceDataFetcher"] = None,
        min_rr_tp1: float = 1.5,
        min_confidence: int = 50,
        sl_buffer_pct: float = 0.005,
    ):
        self.fetcher = fetcher
        self.analyzer = SMCAnalyzer(fetcher)
        self.min_rr_tp1 = min_rr_tp1
        self.min_confidence = min_confidence
        self.sl_buffer_pct = sl_buffer_pct  # 0.3% default — tránh wick hit, không quá rộng làm giảm RR

    def analyze_from_dataframes(
        self,
        symbol: str,
        df_htf_structure: pd.DataFrame,
        df_htf_timing: pd.DataFrame,
        df_ltf_structure: pd.DataFrame,
        df_ltf_timing: pd.DataFrame,
        current_price: float,
        df_daily: Optional[pd.DataFrame] = None,
    ) -> Optional[SMCSetup]:
        """
        Sync version cho backtest — không gọi API.
        Scalp: htf=1h, htf_timing=15m, ltf=15m, ltf_timing=5m.
        Swing: htf=4h, htf_timing=1h, ltf=1h, ltf_timing=15m.
        """
        if len(df_htf_structure) < 30 or len(df_ltf_structure) < 30:
            return None
        htf_signal = self.analyzer.analyze_from_dataframes(
            df_htf_structure, df_htf_timing, current_price, df_daily
        )
        ltf_signal = self.analyzer.analyze_from_dataframes(
            df_ltf_structure, df_ltf_timing, current_price, df_daily
        )
        df_ref = df_daily if df_daily is not None and len(df_daily) >= 2 else df_htf_structure
        return self._build_setup(symbol, current_price, htf_signal, ltf_signal, df_ref)

    async def analyze(self, symbol: str, style: str = "scalp") -> Optional[SMCSetup]:
        """
        Entry point chính.
        style="scalp"  → daily + 1h (HTF) + 15m + 5m (LTF)
        style="swing"  → weekly + 4h (HTF) + 1h + 15m (LTF)
        """
        try:
            if style == "scalp":
                return await self._analyze_scalp(symbol)
            else:
                return await self._analyze_swing(symbol)
        except Exception as e:
            logger.warning(f"SMCStrategy {symbol}: {e}")
            return None

    # ── Scalp: Daily HTF + 1h MTF + 15m/5m LTF ──

    async def _analyze_scalp(self, symbol: str) -> Optional[SMCSetup]:
        df_daily, df_1h, df_15m, df_5m = await asyncio.gather(
            self.fetcher.get_klines(symbol, "1d", 10),
            self.fetcher.get_klines(symbol, "1h", 150),
            self.fetcher.get_klines(symbol, "15m", 150),
            self.fetcher.get_klines(symbol, "5m", 50),
        )

        if len(df_daily) < 3 or len(df_1h) < 30 or len(df_15m) < 30:
            return None

        current_price = float(df_5m["close"].iloc[-1])

        htf_signal = self.analyzer.analyze_from_dataframes(
            df_structure=df_1h,
            df_timing=df_15m,
            current_price=current_price,
            df_daily=df_daily,
        )

        ltf_signal = self.analyzer.analyze_from_dataframes(
            df_structure=df_15m,
            df_timing=df_5m,
            current_price=current_price,
            df_daily=df_daily,
        )

        return self._build_setup(symbol, current_price, htf_signal, ltf_signal, df_daily)

    # ── Swing: Weekly + 4h HTF + 1h/15m LTF ──

    async def _analyze_swing(self, symbol: str) -> Optional[SMCSetup]:
        df_weekly, df_4h, df_1h, df_15m = await asyncio.gather(
            self.fetcher.get_klines(symbol, "1w", 8),
            self.fetcher.get_klines(symbol, "4h", 150),
            self.fetcher.get_klines(symbol, "1h", 150),
            self.fetcher.get_klines(symbol, "15m", 50),
        )

        if len(df_4h) < 30 or len(df_1h) < 30:
            return None

        current_price = float(df_15m["close"].iloc[-1])

        htf_signal = self.analyzer.analyze_from_dataframes(
            df_structure=df_4h,
            df_timing=df_1h,
            current_price=current_price,
            df_daily=df_weekly,
        )
        ltf_signal = self.analyzer.analyze_from_dataframes(
            df_structure=df_1h,
            df_timing=df_15m,
            current_price=current_price,
            df_daily=df_weekly,
        )

        return self._build_setup(symbol, current_price, htf_signal, ltf_signal, df_weekly)

    # ── Core: build SMCSetup từ 2 tầng signal ──

    def _build_setup(
        self,
        symbol: str,
        current_price: float,
        htf: SMCSignal,
        ltf: SMCSignal,
        df_ref: pd.DataFrame,
    ) -> Optional[SMCSetup]:
        """
        Quy tắc kết hợp top-down:
        1. HTF và LTF bias phải cùng chiều (alignment)
        2. LTF phải có trigger (displacement hoặc CHoCH)
        3. Có entry zone cụ thể (OB, CE, BPR, hoặc sweep)
        4. Xác định Draw on Liquidity (target)
        5. Tính SL/TP theo SMC rules
        """
        htf_bias = htf.bias
        ltf_bias = ltf.bias
        # LTF NEUTRAL = không có entry trigger → reject
        if ltf_bias == "NEUTRAL":
            return None

        # HTF NEUTRAL = dùng LTF bias với penalty
        bias_penalty = 1.0
        if htf_bias == "NEUTRAL":
            htf_bias = ltf_bias
            bias_penalty = 0.8
        elif htf_bias != ltf_bias:
            # HTF và LTF disagree — cho phép nhưng penalty, HTF dominates direction
            bias_penalty = 0.7

        direction = "LONG" if htf_bias == "BULLISH" else "SHORT"

        ltf_trigger = self._get_ltf_trigger(ltf)
        if ltf_trigger == "none":
            return None

        entry_model, entry, sl = self._determine_entry(direction, current_price, ltf, htf)
        if entry is None or sl is None:
            return None

        dol = self._find_draw_on_liquidity(direction, current_price, htf)
        if dol is None:
            return None

        tp1 = entry + (dol - entry) * 0.5 if direction == "LONG" else entry - (entry - dol) * 0.5
        tp2 = dol

        risk = abs(entry - sl)
        if risk <= 0:
            return None
        rr_tp1 = abs(tp1 - entry) / risk
        rr_tp2 = abs(tp2 - entry) / risk
        if rr_tp1 < self.min_rr_tp1:
            return None

        confidence, reasoning = self._score_setup(
            direction, htf, ltf, ltf_trigger, entry_model, rr_tp1
        )
        confidence = int(confidence * bias_penalty)
        if bias_penalty < 1.0:
            reasoning += f" | TF alignment penalty ×{bias_penalty}"
        if confidence < self.min_confidence:
            return None

        quality = self._grade_quality(confidence, ltf_trigger, entry_model, ltf)

        return SMCSetup(
            symbol=symbol,
            direction=direction,
            entry_model=entry_model,
            entry_model_quality=quality,
            htf_bias=htf_bias,
            mtf_bias=ltf_bias,
            ltf_trigger=ltf_trigger,
            draw_on_liquidity=dol,
            entry=round(entry, 4),
            sl=round(sl, 4),
            tp1=round(tp1, 4),
            tp2=round(tp2, 4),
            risk_reward_tp1=round(rr_tp1, 2),
            risk_reward_tp2=round(rr_tp2, 2),
            htf_signal=htf,
            ltf_signal=ltf,
            confidence=confidence,
            reasoning=reasoning,
            valid=True,
            source="smc_standalone",
        )

    def _get_ltf_trigger(self, ltf: SMCSignal) -> str:
        if ltf.has_displacement:
            return "displacement"
        if ltf.last_structure_event in ("CHoCH_bull", "CHoCH_bear"):
            return "choch"
        if ltf.last_structure_event in ("BoS_bull", "BoS_bear"):
            return "bos"
        if ltf.sweep_direction != "none":
            return "sweep"
        return "none"

    def _determine_entry(
        self,
        direction: str,
        current_price: float,
        ltf: SMCSignal,
        htf: SMCSignal,
    ) -> tuple[Optional[str], Optional[float], Optional[float]]:
        """
        Ưu tiên entry model theo WR backtest: OB > sweep > CE > BPR.
        SL buffer dùng ATR thay vì % price — scale với volatility thực tế.
        """
        # ATR-based buffer: 0.5 × ATR, floor = 0.2% price
        atr_buf = ltf.atr * 0.5 if ltf.atr > 0 else current_price * self.sl_buffer_pct
        atr_buf = max(atr_buf, current_price * 0.002)

        # Model 1: OB entry — WR cao nhất, entry tại OB midpoint
        if ltf.price_in_ob:
            if direction == "LONG" and ltf.nearest_bullish_ob:
                ob = ltf.nearest_bullish_ob
                entry = ob.mid
                sl = ob.price_low - atr_buf
                return "ob_entry", entry, sl
            if direction == "SHORT" and ltf.nearest_bearish_ob:
                ob = ltf.nearest_bearish_ob
                entry = ob.mid
                sl = ob.price_high + atr_buf
                return "ob_entry", entry, sl

        # Model 2: Sweep reversal
        if ltf.sweep_direction == "sell_side_swept" and direction == "LONG":
            if ltf.sell_side_liquidity:
                sweep_price = ltf.sell_side_liquidity.price
                entry = current_price
                sl = sweep_price - max(atr_buf, current_price * 0.003)
                return "sweep_reversal", entry, sl
        if ltf.sweep_direction == "buy_side_swept" and direction == "SHORT":
            if ltf.buy_side_liquidity:
                sweep_price = ltf.buy_side_liquidity.price
                entry = current_price
                sl = sweep_price + max(atr_buf, current_price * 0.003)
                return "sweep_reversal", entry, sl

        # Model 3: CE entry (FVG 50%)
        if ltf.price_at_ce or ltf.price_in_fvg:
            if direction == "LONG" and ltf.nearest_bullish_fvg and not ltf.nearest_bullish_fvg.filled:
                fvg = ltf.nearest_bullish_fvg
                entry = fvg.ce if ltf.price_at_ce else current_price
                sl = fvg.bottom - atr_buf
                return "ce_entry", entry, sl
            if direction == "SHORT" and ltf.nearest_bearish_fvg and not ltf.nearest_bearish_fvg.filled:
                fvg = ltf.nearest_bearish_fvg
                entry = fvg.ce if ltf.price_at_ce else current_price
                sl = fvg.top + atr_buf
                return "ce_entry", entry, sl

        # Model 4: BPR entry
        if ltf.has_bpr:
            if direction == "LONG" and (ltf.bpr_overlap_bottom is not None and ltf.bpr_overlap_top is not None):
                entry = (ltf.bpr_overlap_top + ltf.bpr_overlap_bottom) / 2
                sl = ltf.bpr_overlap_bottom - atr_buf
                return "bpr_entry", entry, sl
            if direction == "LONG" and ltf.nearest_bullish_fvg:
                fvg = ltf.nearest_bullish_fvg
                entry = fvg.ce
                sl = fvg.bottom - atr_buf
                return "bpr_entry", entry, sl
            if direction == "SHORT" and (ltf.bpr_overlap_bottom is not None and ltf.bpr_overlap_top is not None):
                entry = (ltf.bpr_overlap_top + ltf.bpr_overlap_bottom) / 2
                sl = ltf.bpr_overlap_top + atr_buf
                return "bpr_entry", entry, sl
            if direction == "SHORT" and ltf.nearest_bearish_fvg:
                fvg = ltf.nearest_bearish_fvg
                entry = fvg.ce
                sl = fvg.top + atr_buf
                return "bpr_entry", entry, sl

        return None, None, None

    def _find_draw_on_liquidity(
        self,
        direction: str,
        current_price: float,
        htf: SMCSignal,
    ) -> Optional[float]:
        """
        Draw on Liquidity = nơi giá đang bị kéo về.
        Ưu tiên: PWH/PWL > PDH/PDL > Buy/Sell-side liquidity > OB gần nhất.
        """
        inst = htf.institutional

        if direction == "LONG":
            candidates = []
            if inst and inst.pwh and inst.pwh > current_price:
                candidates.append(inst.pwh)
            if inst and inst.pdh and inst.pdh > current_price:
                candidates.append(inst.pdh)
            if htf.buy_side_liquidity and htf.buy_side_liquidity.price > current_price:
                candidates.append(htf.buy_side_liquidity.price)
            if htf.nearest_bearish_ob and htf.nearest_bearish_ob.mid > current_price:
                candidates.append(htf.nearest_bearish_ob.mid)
            if candidates:
                return min(candidates)
        else:
            candidates = []
            if inst and inst.pwl and inst.pwl < current_price:
                candidates.append(inst.pwl)
            if inst and inst.pdl and inst.pdl < current_price:
                candidates.append(inst.pdl)
            if htf.sell_side_liquidity and htf.sell_side_liquidity.price < current_price:
                candidates.append(htf.sell_side_liquidity.price)
            if htf.nearest_bullish_ob and htf.nearest_bullish_ob.mid < current_price:
                candidates.append(htf.nearest_bullish_ob.mid)
            if candidates:
                return max(candidates)

        return None

    def _score_setup(
        self,
        direction: str,
        htf: SMCSignal,
        ltf: SMCSignal,
        ltf_trigger: str,
        entry_model: str,
        rr_tp1: float,
    ) -> tuple[int, str]:
        score = 0
        reasons = []

        htf_contribution = min(35, max(0, int(abs(htf.smc_score) / 3)))
        score += htf_contribution
        reasons.append(f"HTF score {htf.smc_score:+d} ({htf.last_structure_event})")

        trigger_scores = {
            "displacement": 25,
            "choch": 20,
            "sweep": 18,
            "bos": 10,
        }
        trig_score = trigger_scores.get(ltf_trigger, 0)
        score += trig_score
        reasons.append(f"LTF trigger={ltf_trigger} (+{trig_score})")

        model_scores = {
            "ob_entry": 25,
            "sweep_reversal": 18,
            "bpr_entry": 12,
            "ce_entry": 5,
        }
        model_score = model_scores.get(entry_model, 0)
        score += model_score
        reasons.append(f"Model={entry_model} (+{model_score})")

        pd_zone = ltf.pd_zone
        if pd_zone:
            if direction == "LONG" and pd_zone.current_zone == "discount":
                score += 15
                reasons.append("Buying at discount (+15)")
            elif direction == "SHORT" and pd_zone.current_zone == "premium":
                score += 15
                reasons.append("Selling at premium (+15)")
            else:
                reasons.append(f"Zone={pd_zone.current_zone} (misaligned)")

        if ltf.in_ote:
            score += 10
            reasons.append("Price in OTE (+10)")

        # OB+FVG confluence (Propulsion Block) — chỉ khi entry_model=ob_entry
        if entry_model == "ob_entry":
            if direction == "LONG" and ltf.nearest_bullish_ob and ltf.nearest_bullish_ob.has_fvg_overlap:
                score += 15
                reasons.append("OB+FVG confluence (+15)")
            elif direction == "SHORT" and ltf.nearest_bearish_ob and ltf.nearest_bearish_ob.has_fvg_overlap:
                score += 15
                reasons.append("OB+FVG confluence (+15)")

        if rr_tp1 >= 3:
            score += 5
            reasons.append(f"R:R {rr_tp1:.1f} (+5)")
        elif rr_tp1 >= 2:
            score += 3

        score = min(100, score)
        return score, " | ".join(reasons)

    def _grade_quality(
        self,
        confidence: int,
        ltf_trigger: str,
        entry_model: str,
        ltf: SMCSignal,
    ) -> str:
        if (
            confidence >= 80
            and ltf_trigger in ("displacement", "sweep")
            and entry_model in ("ob_entry", "sweep_reversal", "bpr_entry", "ce_entry")
            and ltf.in_ote
        ):
            return "A+"
        if confidence >= 65 and ltf_trigger in ("displacement", "choch", "sweep"):
            return "A"
        if confidence >= 55:
            return "B"
        return "C"
