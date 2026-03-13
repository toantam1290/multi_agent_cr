"""
utils/smc.py - Smart Money Concepts (SMC) Analyzer — nâng cấp v2

Thêm so với v1:
  - OB Mitigation + Breaker Block
  - Displacement candle detection
  - Premium / Discount zone + OTE (Optimal Trade Entry)
  - PDH / PDL / PWH / PWL (institutional reference levels)
  - FVG: CE (Consequent Encroachment), BPR, Inversion FVG
  - Inducement detection
  - Nâng cấp SMC scoring (có trọng số động)

Interface public giữ nguyên — research_agent.py không cần sửa.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

import pandas as pd
from loguru import logger

if TYPE_CHECKING:
    from utils.market_data import BinanceDataFetcher


# ─────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────

@dataclass
class OrderBlock:
    """Vùng nến gốc trước khi impulse xảy ra."""
    price_high: float
    price_low: float
    mid: float
    direction: str        # "bullish" | "bearish"
    candle_index: int
    strength_pct: float
    mitigated: bool = False   # True = giá đã quay lại và phá qua OB
    has_fvg_overlap: bool = False  # OB overlap với unmitigated FVG = Propulsion Block


@dataclass
class BreakerBlock:
    """
    OB đã bị mitigated → flip thành support/resistance ngược chiều.
    Bullish OB bị phá → BreakerBlock bearish (resistance mới).
    """
    price_high: float
    price_low: float
    mid: float
    direction: str   # "bullish" | "bearish" (chiều của breaker, ngược OB gốc)


@dataclass
class FairValueGap:
    """3-candle imbalance."""
    top: float
    bottom: float
    mid: float
    ce: float          # Consequent Encroachment = 50% của FVG (entry lý tưởng)
    direction: str     # "bullish" | "bearish"
    filled: bool
    inverted: bool = False   # True = đã fill và đang retest từ phía ngược lại


@dataclass
class LiquidityLevel:
    price: float
    side: str      # "buy_side" | "sell_side"
    touches: int
    swept: bool


@dataclass
class DisplacementCandle:
    """
    Nến xác nhận MSS thật — range > 1.5x ATR, body > 60% range, tạo FVG.
    Đây là bằng chứng tổ chức đã vào thị trường thực sự.
    """
    candle_index: int
    direction: str      # "bullish" | "bearish"
    range_atr_ratio: float   # range / ATR — càng cao càng mạnh
    created_fvg: bool
    is_near_displacement: bool = False


@dataclass
class PremiumDiscount:
    """
    Phân tích vùng Premium / Discount dựa trên swing range gần nhất.
    Reference: ICT PD Array methodology.
    OTE LONG = discount zone (62-79% retrace từ high). OTE SHORT = premium zone (62-79% từ low).
    """
    swing_high: float
    swing_low: float
    equilibrium: float      # 50% của range
    ote_long_low: float     # 79% retrace từ high (discount zone for long)
    ote_long_high: float    # 62% retrace từ high
    ote_short_low: float    # 62% retrace từ low (premium zone for short)
    ote_short_high: float   # 79% retrace từ low
    current_zone: str       # "premium" | "discount" | "equilibrium"
    in_ote_long: bool      # Giá trong OTE zone cho LONG (buy the dip)
    in_ote_short: bool     # Giá trong OTE zone cho SHORT (sell the rally)


@dataclass
class InstitutionalLevels:
    """PDH/PDL/PWH/PWL — cần df_daily (cung cấp từ SMCStrategy, optional trong analyzer cơ bản)."""
    pdh: Optional[float] = None   # Previous Day High
    pdl: Optional[float] = None   # Previous Day Low
    pwh: Optional[float] = None   # Previous Week High
    pwl: Optional[float] = None   # Previous Week Low


@dataclass
class SMCSignal:
    # Market Structure
    bias: str = "NEUTRAL"
    last_structure_event: str = "none"
    structure_strength_pct: float = 0.0
    has_displacement: bool = False     # MSS có displacement candle = đáng tin hơn
    has_near_displacement: bool = False  # Near-displacement (1.0-1.2x ATR, body>55%)

    # Order Blocks
    nearest_bullish_ob: Optional[OrderBlock] = None
    nearest_bearish_ob: Optional[OrderBlock] = None
    price_in_ob: bool = False
    ob_direction: str = "none"
    nearest_bullish_breaker: Optional[BreakerBlock] = None
    nearest_bearish_breaker: Optional[BreakerBlock] = None

    # Fair Value Gaps
    nearest_bullish_fvg: Optional[FairValueGap] = None
    nearest_bearish_fvg: Optional[FairValueGap] = None
    price_in_fvg: bool = False
    fvg_direction: str = "none"
    price_at_ce: bool = False          # Giá tại CE (50% FVG) — entry lý tưởng
    has_bpr: bool = False              # Có BPR (bullish + bearish FVG overlap)
    bpr_overlap_top: Optional[float] = None   # BPR zone — dùng cho entry chính xác
    bpr_overlap_bottom: Optional[float] = None

    # Premium / Discount
    pd_zone: Optional[PremiumDiscount] = None
    in_ote: bool = False           # True nếu giá trong OTE zone phù hợp với bias (long→ote_long, short→ote_short)

    # Liquidity
    buy_side_liquidity: Optional[LiquidityLevel] = None
    sell_side_liquidity: Optional[LiquidityLevel] = None
    sweep_direction: str = "none"

    # Institutional Levels
    institutional: Optional[InstitutionalLevels] = None
    price_near_pdh: bool = False
    price_near_pdl: bool = False

    # ATR (từ structure TF)
    atr: float = 0.0

    # Summary
    smc_score: int = 0
    smc_valid: bool = False
    summary: str = ""


# ─────────────────────────────────────────────
# SMCAnalyzer
# ─────────────────────────────────────────────

class SMCAnalyzer:
    """
    Detection engine — detect từng SMC feature.
    Interface public giữ nguyên để research_agent.py hoạt động bình thường.
    """

    def __init__(self, fetcher: "BinanceDataFetcher"):
        self.fetcher = fetcher

    # ── Public interface (giữ nguyên) ──────────

    async def analyze(self, symbol: str, style: str = "scalp") -> SMCSignal:
        """Entry point cho research_agent.py (không đổi interface)."""
        try:
            if style == "scalp":
                df_structure, df_timing = await asyncio.gather(
                    self.fetcher.get_klines(symbol, "15m", 150),
                    self.fetcher.get_klines(symbol, "5m", 50),
                )
            else:
                df_structure, df_timing = await asyncio.gather(
                    self.fetcher.get_klines(symbol, "1h", 150),
                    self.fetcher.get_klines(symbol, "15m", 50),
                )
            if len(df_structure) < 30 or len(df_timing) < 10:
                logger.warning(f"SMC {symbol}: Insufficient data")
                return SMCSignal(summary="Insufficient data for SMC analysis")
            current_price = float(df_timing["close"].iloc[-1])
            signal = self._run_detection(df_structure, df_timing, current_price)
            logger.info(
                f"SMC {symbol}: bias={signal.bias} event={signal.last_structure_event} "
                f"score={signal.smc_score} | {signal.summary[:80]}"
            )
            return signal
        except Exception as e:
            logger.warning(f"SMC analysis failed for {symbol}: {e}")
            return SMCSignal(summary=f"SMC error: {type(e).__name__}")

    def analyze_from_dataframes(
        self,
        df_structure: pd.DataFrame,
        df_timing: pd.DataFrame,
        current_price: float,
        df_daily: Optional[pd.DataFrame] = None,   # MỚI: optional cho institutional levels
    ) -> SMCSignal:
        """Sync version cho backtest — không đổi interface, chỉ thêm df_daily optional."""
        try:
            if len(df_structure) < 30 or len(df_timing) < 10:
                return SMCSignal(summary="Insufficient data for SMC analysis")
            return self._run_detection(df_structure, df_timing, current_price, df_daily)
        except Exception as e:
            return SMCSignal(summary=f"SMC error: {type(e).__name__}")

    # ── Core detection ─────────────────────────

    def _run_detection(
        self,
        df_structure: pd.DataFrame,
        df_timing: pd.DataFrame,
        current_price: float,
        df_daily: Optional[pd.DataFrame] = None,
    ) -> SMCSignal:
        # ATR — dùng cho displacement detection và filter OB noise
        atr = self._calc_atr(df_structure, period=14)

        swing_highs, swing_lows = self._detect_swings(df_structure, n=5)
        bias, last_event, struct_strength = self._detect_structure(
            df_structure, swing_highs, swing_lows
        )

        # Displacement — xác nhận MSS có tổ chức vào không
        displacements = self._detect_displacement(df_structure, atr)
        recent_full = [d for d in displacements if not d.is_near_displacement and d.candle_index >= len(df_structure) - 15]
        recent_near = [d for d in displacements if d.is_near_displacement and d.candle_index >= len(df_structure) - 15]
        has_displacement = len(recent_full) > 0
        has_near_displacement = (not has_displacement) and len(recent_near) > 0

        # OB với mitigation check
        bull_obs, bear_obs, bull_breakers, bear_breakers = self._detect_order_blocks(
            df_structure, swing_highs, swing_lows, current_price
        )

        # FVG với CE, BPR, Inversion
        bull_fvgs, bear_fvgs = self._detect_fvg(df_structure, current_price)

        # FVG+OB confluence: OB overlap với unmitigated FVG = Propulsion Block
        for ob in bull_obs:
            for fvg in bull_fvgs:
                if not fvg.filled and ob.price_low < fvg.top and ob.price_high > fvg.bottom:
                    ob.has_fvg_overlap = True
                    break
        for ob in bear_obs:
            for fvg in bear_fvgs:
                if not fvg.filled and ob.price_low < fvg.top and ob.price_high > fvg.bottom:
                    ob.has_fvg_overlap = True
                    break

        # BPR check
        has_bpr, bpr_top, bpr_bottom = self._detect_bpr(bull_fvgs, bear_fvgs)

        # Premium / Discount
        pd_zone = self._detect_premium_discount(df_structure, swing_highs, swing_lows, current_price)

        # Liquidity
        buy_liq, sell_liq = self._detect_liquidity(
            df_structure, swing_highs, swing_lows, current_price
        )
        sweep_dir = self._detect_sweep(df_timing, buy_liq, sell_liq, lookback=10)

        # Institutional levels (chỉ khi có df_daily)
        institutional = None
        price_near_pdh = False
        price_near_pdl = False
        if df_daily is not None and len(df_daily) >= 2:
            institutional = self._detect_institutional_levels(df_daily)
            if institutional:
                tol = current_price * 0.003  # 0.3% tolerance
                price_near_pdh = institutional.pdh is not None and abs(current_price - institutional.pdh) < tol
                price_near_pdl = institutional.pdl is not None and abs(current_price - institutional.pdl) < tol

        # Nearest OB / FVG
        nearest_bull_ob = self._nearest_ob(bull_obs, current_price, "bullish")
        nearest_bear_ob = self._nearest_ob(bear_obs, current_price, "bearish")
        # Breaker naming: bull_breakers = từ bullish OB mitigated → direction bearish (resistance)
        # bear_breakers = từ bearish OB mitigated → direction bullish (support)
        nearest_bull_breaker = self._nearest_breaker(bear_breakers, current_price)  # bullish support
        nearest_bear_breaker = self._nearest_breaker(bull_breakers, current_price)   # bearish resistance

        price_in_ob, ob_dir = self._price_in_zone(
            current_price,
            nearest_bull_ob.price_low if nearest_bull_ob else None,
            nearest_bull_ob.price_high if nearest_bull_ob else None,
            nearest_bear_ob.price_low if nearest_bear_ob else None,
            nearest_bear_ob.price_high if nearest_bear_ob else None,
        )

        unfilled_bull = [f for f in bull_fvgs if not f.filled]
        unfilled_bear = [f for f in bear_fvgs if not f.filled]
        nearest_bull_fvg = min(unfilled_bull, key=lambda f: abs(f.mid - current_price)) if unfilled_bull else None
        nearest_bear_fvg = min(unfilled_bear, key=lambda f: abs(f.mid - current_price)) if unfilled_bear else None

        price_in_fvg, fvg_dir = self._price_in_zone(
            current_price,
            nearest_bull_fvg.bottom if nearest_bull_fvg else None,
            nearest_bull_fvg.top if nearest_bull_fvg else None,
            nearest_bear_fvg.bottom if nearest_bear_fvg else None,
            nearest_bear_fvg.top if nearest_bear_fvg else None,
        )

        # Giá tại CE (50% FVG) — entry lý tưởng
        price_at_ce = False
        active_fvg = nearest_bull_fvg if fvg_dir == "bullish" else nearest_bear_fvg
        if active_fvg and price_in_fvg:
            ce_tol = (active_fvg.top - active_fvg.bottom) * 0.15  # ±15% quanh CE
            price_at_ce = abs(current_price - active_fvg.ce) < ce_tol

        smc_score = self._calc_score(
            bias, last_event, has_displacement,
            price_in_ob, ob_dir, price_in_fvg, fvg_dir,
            price_at_ce, has_bpr, sweep_dir,
            pd_zone, price_near_pdh, price_near_pdl,
            has_near_displacement=has_near_displacement,
        )
        smc_valid = abs(smc_score) >= 30

        in_ote = (
            (pd_zone.in_ote_long if bias == "BULLISH" else False) or
            (pd_zone.in_ote_short if bias == "BEARISH" else False)
        ) if pd_zone else False

        summary = self._build_summary(
            bias, last_event, struct_strength, has_displacement,
            nearest_bull_ob, nearest_bear_ob,
            nearest_bull_fvg, nearest_bear_fvg,
            buy_liq, sell_liq, sweep_dir,
            smc_score, current_price, price_in_ob, ob_dir,
            pd_zone, in_ote=in_ote,
            institutional=institutional,
            has_near_displacement=has_near_displacement,
        )

        return SMCSignal(
            bias=bias,
            last_structure_event=last_event,
            structure_strength_pct=struct_strength,
            has_displacement=has_displacement,
            has_near_displacement=has_near_displacement,
            atr=atr,
            nearest_bullish_ob=nearest_bull_ob,
            nearest_bearish_ob=nearest_bear_ob,
            price_in_ob=price_in_ob,
            ob_direction=ob_dir,
            nearest_bullish_breaker=nearest_bull_breaker,
            nearest_bearish_breaker=nearest_bear_breaker,
            nearest_bullish_fvg=nearest_bull_fvg,
            nearest_bearish_fvg=nearest_bear_fvg,
            price_in_fvg=price_in_fvg,
            fvg_direction=fvg_dir,
            price_at_ce=price_at_ce,
            has_bpr=has_bpr,
            bpr_overlap_top=bpr_top,
            bpr_overlap_bottom=bpr_bottom,
            pd_zone=pd_zone,
            in_ote=in_ote,
            buy_side_liquidity=buy_liq,
            sell_side_liquidity=sell_liq,
            sweep_direction=sweep_dir,
            institutional=institutional,
            price_near_pdh=price_near_pdh,
            price_near_pdl=price_near_pdl,
            smc_score=smc_score,
            smc_valid=smc_valid,
            summary=summary,
        )

    # ── Detection methods ──────────────────────

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """ATR đơn giản dùng trong nội bộ (không phụ thuộc pandas-ta)."""
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        trs = []
        for i in range(1, len(df)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        if not trs:
            return float(df["high"].iloc[-1] - df["low"].iloc[-1])
        return float(pd.Series(trs[-period:]).mean())

    def _detect_swings(self, df: pd.DataFrame, n: int = 5) -> tuple[list[int], list[int]]:
        """Swing High/Low với window n nến mỗi bên. Mở rộng đến cuối df (right-side asymmetric)."""
        highs, lows = [], []
        for i in range(n, len(df)):
            right_bound = min(i + n + 1, len(df))
            # Yêu cầu ít nhất 2 nến bên phải để tránh false swing ở biên
            if right_bound - i < 2 and i < len(df) - 1:
                continue
            window_h = df["high"].iloc[i - n : right_bound]
            window_l = df["low"].iloc[i - n : right_bound]
            if float(df["high"].iloc[i]) == float(window_h.max()):
                highs.append(i)
            if float(df["low"].iloc[i]) == float(window_l.min()):
                lows.append(i)
        return highs, lows

    def _detect_structure(
        self,
        df: pd.DataFrame,
        swing_highs: list[int],
        swing_lows: list[int],
    ) -> tuple[str, str, float]:
        """CHoCH / BoS detection."""
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "NEUTRAL", "none", 0.0

        last_sh = float(df["high"].iloc[swing_highs[-1]])
        prev_sh = float(df["high"].iloc[swing_highs[-2]])
        last_sl = float(df["low"].iloc[swing_lows[-1]])
        prev_sl = float(df["low"].iloc[swing_lows[-2]])
        current = float(df["close"].iloc[-2])  # nến đã đóng — tránh false signal

        hh = last_sh > prev_sh
        hl = last_sl > prev_sl
        ll = last_sl < prev_sl
        lh = last_sh < prev_sh

        bias, last_event, strength = "NEUTRAL", "none", 0.0

        if hh and hl:
            bias = "BULLISH"
            if current > last_sh:
                last_event = "BoS_bull"
                strength = (current - last_sh) / last_sh * 100
        elif ll and lh:
            bias = "BEARISH"
            if current < last_sl:
                last_event = "BoS_bear"
                strength = (last_sl - current) / last_sl * 100
        else:
            if current > last_sh:
                bias = "BULLISH"
                last_event = "CHoCH_bull"
                strength = (current - last_sh) / last_sh * 100
            elif current < last_sl:
                bias = "BEARISH"
                last_event = "CHoCH_bear"
                strength = (last_sl - current) / last_sl * 100

        return bias, last_event, round(strength, 3)

    def _detect_displacement(
        self, df: pd.DataFrame, atr: float
    ) -> list[DisplacementCandle]:
        """
        Displacement candle detection with two tiers:
          Full displacement: range >= 1.2x ATR AND body >= 50% range
          Near-displacement: range >= 1.0x ATR AND body > 55% range
          Below 1.0x ATR: skip
        Chỉ xét 30 nến gần nhất.
        """
        result = []
        start = max(2, len(df) - 30)
        for i in range(start, len(df) - 1):
            h = float(df["high"].iloc[i])
            l = float(df["low"].iloc[i])
            o = float(df["open"].iloc[i])
            c = float(df["close"].iloc[i])
            candle_range = h - l
            if atr <= 0 or candle_range < 1.0 * atr:
                continue
            body = abs(c - o)

            # Determine tier
            is_full = candle_range >= 1.2 * atr and body >= 0.5 * candle_range
            is_near = (not is_full) and candle_range >= 1.0 * atr and body > 0.55 * candle_range
            if not is_full and not is_near:
                continue

            direction = "bullish" if c > o else "bearish"
            # Kiểm tra tạo FVG
            created_fvg = False
            if direction == "bullish" and i >= 1 and i + 1 < len(df):
                prev_high = float(df["high"].iloc[i - 1])
                next_low = float(df["low"].iloc[i + 1])
                created_fvg = next_low > prev_high  # gap UP
            elif direction == "bearish" and i >= 1 and i + 1 < len(df):
                prev_low = float(df["low"].iloc[i - 1])
                next_high = float(df["high"].iloc[i + 1])
                created_fvg = next_high < prev_low  # gap DOWN

            result.append(DisplacementCandle(
                candle_index=i,
                direction=direction,
                range_atr_ratio=round(candle_range / atr, 2),
                created_fvg=created_fvg,
                is_near_displacement=is_near,
            ))
        return result

    def _detect_order_blocks(
        self,
        df: pd.DataFrame,
        swing_highs: list[int],
        swing_lows: list[int],
        current_price: float,
    ) -> tuple[list[OrderBlock], list[OrderBlock], list[BreakerBlock], list[BreakerBlock]]:
        """
        OB detection với mitigation check.
        Nếu OB bị mitigated → tạo BreakerBlock ngược chiều.
        """
        bull_obs: list[OrderBlock] = []
        bear_obs: list[OrderBlock] = []
        bull_breakers: list[BreakerBlock] = []
        bear_breakers: list[BreakerBlock] = []

        # ── Bullish OB (bearish candle trước swing high) ──
        for sh_idx in swing_highs[-4:]:
            for i in range(sh_idx - 1, max(0, sh_idx - 20), -1):
                o = float(df["open"].iloc[i])
                c = float(df["close"].iloc[i])
                if c < o:  # nến đỏ
                    if i + 1 >= len(df):
                        break
                    next_c = float(df["close"].iloc[i + 1])
                    impulse = (next_c - c) / c * 100
                    if impulse < 0.3:
                        continue  # Tìm OB tiếp theo thay vì bỏ qua swing này
                    ob_high = float(df["high"].iloc[i])
                    ob_low = float(df["low"].iloc[i])

                    # Mitigation check: giá sau OB có đóng dưới OB low không?
                    mitigated = False
                    for j in range(i + 1, len(df)):
                        future_close = float(df["close"].iloc[j])
                        if future_close < ob_low:
                            mitigated = True
                            break

                    ob = OrderBlock(
                        price_high=ob_high,
                        price_low=ob_low,
                        mid=(ob_high + ob_low) / 2,
                        direction="bullish",
                        candle_index=i,
                        strength_pct=round(impulse, 3),
                        mitigated=mitigated,
                    )
                    if mitigated:
                        bull_breakers.append(BreakerBlock(
                            price_high=ob_high,
                            price_low=ob_low,
                            mid=(ob_high + ob_low) / 2,
                            direction="bearish",
                        ))
                    else:
                        bull_obs.append(ob)
                    break

        # ── Bearish OB (bullish candle trước swing low) ──
        for sl_idx in swing_lows[-4:]:
            for i in range(sl_idx - 1, max(0, sl_idx - 20), -1):
                o = float(df["open"].iloc[i])
                c = float(df["close"].iloc[i])
                if c > o:  # nến xanh
                    if i + 1 >= len(df):
                        break
                    next_c = float(df["close"].iloc[i + 1])
                    impulse = (c - next_c) / c * 100
                    if impulse < 0.3:
                        continue  # Tìm OB tiếp theo thay vì bỏ qua swing này
                    ob_high = float(df["high"].iloc[i])
                    ob_low = float(df["low"].iloc[i])

                    # Mitigation check: giá sau OB có đóng trên OB high không?
                    mitigated = False
                    for j in range(i + 1, len(df)):
                        future_close = float(df["close"].iloc[j])
                        if future_close > ob_high:
                            mitigated = True
                            break

                    ob = OrderBlock(
                        price_high=ob_high,
                        price_low=ob_low,
                        mid=(ob_high + ob_low) / 2,
                        direction="bearish",
                        candle_index=i,
                        strength_pct=round(impulse, 3),
                        mitigated=mitigated,
                    )
                    if mitigated:
                        bear_breakers.append(BreakerBlock(
                            price_high=ob_high,
                            price_low=ob_low,
                            mid=(ob_high + ob_low) / 2,
                            direction="bullish",
                        ))
                    else:
                        bear_obs.append(ob)
                    break

        return bull_obs, bear_obs, bull_breakers, bear_breakers

    def _detect_fvg(
        self, df: pd.DataFrame, current_price: float
    ) -> tuple[list[FairValueGap], list[FairValueGap]]:
        """
        FVG với CE (50%), filled check, và inversion detection.
        Chỉ FVG đủ lớn (> 0.05% range) để tránh noise.
        """
        bull_fvgs: list[FairValueGap] = []
        bear_fvgs: list[FairValueGap] = []
        start = max(2, len(df) - 30)

        for i in range(start, len(df)):
            low_i = float(df["low"].iloc[i])
            high_i = float(df["high"].iloc[i])
            high_i_m2 = float(df["high"].iloc[i - 2])
            low_i_m2 = float(df["low"].iloc[i - 2])
            price_ref = float(df["close"].iloc[i])

            # Bullish FVG: low[i] > high[i-2], gap từ high_i_m2 (bottom) đến low_i (top)
            # 3 trạng thái: Untouched (price>=low_i) | Testing (high_i_m2<=price<low_i) | Mitigated (price<high_i_m2)
            # Filled = fully mitigated = price closed strictly BELOW gap bottom
            if low_i > high_i_m2:
                gap_size = low_i - high_i_m2
                if gap_size / price_ref > 0.0005:  # tối thiểu 0.05%
                    filled = current_price < high_i_m2   # strictly below gap bottom = fully mitigated
                    inverted = filled
                    bull_fvgs.append(FairValueGap(
                        top=low_i,
                        bottom=high_i_m2,
                        mid=(low_i + high_i_m2) / 2,
                        ce=(low_i + high_i_m2) / 2,   # CE = mid của FVG
                        direction="bullish",
                        filled=filled,
                        inverted=inverted,
                    ))

            # Bearish FVG: high[i] < low[i-2], gap từ high_i (bottom) đến low_i_m2 (top)
            # Filled = fully mitigated = price closed strictly ABOVE gap top
            if high_i < low_i_m2:
                gap_size = low_i_m2 - high_i
                if gap_size / price_ref > 0.0005:
                    filled = current_price > low_i_m2   # strictly above gap top = fully mitigated
                    inverted = filled
                    bear_fvgs.append(FairValueGap(
                        top=low_i_m2,
                        bottom=high_i,
                        mid=(low_i_m2 + high_i) / 2,
                        ce=(low_i_m2 + high_i) / 2,
                        direction="bearish",
                        filled=filled,
                        inverted=inverted,
                    ))

        return bull_fvgs, bear_fvgs

    def _detect_bpr(
        self,
        bull_fvgs: list[FairValueGap],
        bear_fvgs: list[FairValueGap],
    ) -> tuple[bool, Optional[float], Optional[float]]:
        """
        BPR (Balanced Price Range): bullish FVG và bearish FVG overlap nhau.
        Vùng overlap = institutional sweet spot. Trả về (has_bpr, overlap_top, overlap_bottom).
        """
        for bf in bull_fvgs:
            if bf.filled:
                continue
            for baf in bear_fvgs:
                if baf.filled:
                    continue
                overlap_bottom = max(bf.bottom, baf.bottom)
                overlap_top = min(bf.top, baf.top)
                if overlap_top > overlap_bottom:
                    return True, overlap_top, overlap_bottom
        return False, None, None

    def _detect_premium_discount(
        self,
        df: pd.DataFrame,
        swing_highs: list[int],
        swing_lows: list[int],
        current_price: float,
    ) -> Optional[PremiumDiscount]:
        """
        Premium / Discount dựa trên swing range gần nhất.
        OTE = 62-79% retracement.
        """
        if not swing_highs or not swing_lows:
            return None

        last_sh_idx = swing_highs[-1]
        last_sl_idx = swing_lows[-1]
        sh = float(df["high"].iloc[last_sh_idx])
        sl = float(df["low"].iloc[last_sl_idx])

        if sh <= sl:
            return None

        eq = (sh + sl) / 2
        rng = sh - sl

        # OTE LONG: 62-79% retrace từ swing high xuống (discount zone, buy the dip)
        ote_long_low = sh - 0.79 * rng
        ote_long_high = sh - 0.62 * rng

        # OTE SHORT: 62-79% retrace từ swing low lên (premium zone, sell the rally)
        ote_short_low = sl + 0.62 * rng
        ote_short_high = sl + 0.79 * rng

        # Xác định zone hiện tại
        if current_price > eq + rng * 0.1:
            zone = "premium"
        elif current_price < eq - rng * 0.1:
            zone = "discount"
        else:
            zone = "equilibrium"

        in_ote_long = ote_long_low <= current_price <= ote_long_high
        in_ote_short = ote_short_low <= current_price <= ote_short_high

        return PremiumDiscount(
            swing_high=sh,
            swing_low=sl,
            equilibrium=eq,
            ote_long_low=ote_long_low,
            ote_long_high=ote_long_high,
            ote_short_low=ote_short_low,
            ote_short_high=ote_short_high,
            current_zone=zone,
            in_ote_long=in_ote_long,
            in_ote_short=in_ote_short,
        )

    def _detect_institutional_levels(
        self, df_daily: pd.DataFrame
    ) -> Optional[InstitutionalLevels]:
        """PDH/PDL từ ngày hôm qua, PWH/PWL từ 5 ngày gần nhất."""
        try:
            if len(df_daily) < 2:
                return None
            pdh = float(df_daily["high"].iloc[-2])
            pdl = float(df_daily["low"].iloc[-2])
            week_slice = df_daily.iloc[-6:-1] if len(df_daily) >= 6 else df_daily.iloc[:-1]
            pwh = float(week_slice["high"].max()) if len(week_slice) > 0 else None
            pwl = float(week_slice["low"].min()) if len(week_slice) > 0 else None
            return InstitutionalLevels(pdh=pdh, pdl=pdl, pwh=pwh, pwl=pwl)
        except Exception:
            return None

    def _detect_liquidity(
        self,
        df: pd.DataFrame,
        swing_highs: list[int],
        swing_lows: list[int],
        current_price: float,
        tolerance: float = 0.0015,
    ) -> tuple[Optional[LiquidityLevel], Optional[LiquidityLevel]]:
        """Equal highs / equal lows clustering."""
        buy_liq: Optional[LiquidityLevel] = None
        sell_liq: Optional[LiquidityLevel] = None

        sh_prices = sorted(
            [float(df["high"].iloc[i]) for i in swing_highs if float(df["high"].iloc[i]) > current_price]
        )
        for i in range(len(sh_prices) - 1):
            if abs(sh_prices[i + 1] - sh_prices[i]) / sh_prices[i] < tolerance:
                buy_liq = LiquidityLevel(
                    price=(sh_prices[i] + sh_prices[i + 1]) / 2,
                    side="buy_side",
                    touches=2,
                    swept=False,
                )
                break

        sl_prices = sorted(
            [float(df["low"].iloc[i]) for i in swing_lows if float(df["low"].iloc[i]) < current_price],
            reverse=True,
        )
        for i in range(len(sl_prices) - 1):
            if abs(sl_prices[i] - sl_prices[i + 1]) / sl_prices[i + 1] < tolerance:
                sell_liq = LiquidityLevel(
                    price=(sl_prices[i] + sl_prices[i + 1]) / 2,
                    side="sell_side",
                    touches=2,
                    swept=False,
                )
                break

        return buy_liq, sell_liq

    def _detect_sweep(
        self,
        df: pd.DataFrame,
        buy_liq: Optional[LiquidityLevel],
        sell_liq: Optional[LiquidityLevel],
        lookback: int = 10,
    ) -> str:
        """Liquidity sweep: wick vượt qua level nhưng close quay lại."""
        recent = df.iloc[-lookback:]
        if buy_liq:
            for i in range(len(recent)):
                if float(recent["high"].iloc[i]) > buy_liq.price and float(recent["close"].iloc[i]) < buy_liq.price:
                    return "buy_side_swept"
        if sell_liq:
            for i in range(len(recent)):
                if float(recent["low"].iloc[i]) < sell_liq.price and float(recent["close"].iloc[i]) > sell_liq.price:
                    return "sell_side_swept"
        return "none"

    # ── Helpers ────────────────────────────────

    def _nearest_ob(self, obs: list[OrderBlock], current_price: float, direction: str) -> Optional[OrderBlock]:
        valid = [ob for ob in obs if not ob.mitigated]
        if direction == "bullish":
            candidates = [ob for ob in valid if ob.price_high <= current_price * 1.005]
        else:
            candidates = [ob for ob in valid if ob.price_low >= current_price * 0.995]
        if not candidates:
            return None
        return min(candidates, key=lambda ob: abs(ob.mid - current_price))

    def _nearest_breaker(self, breakers: list[BreakerBlock], current_price: float) -> Optional[BreakerBlock]:
        if not breakers:
            return None
        return min(breakers, key=lambda b: abs(b.mid - current_price))

    def _price_in_zone(
        self, price: float,
        bull_low: Optional[float], bull_high: Optional[float],
        bear_low: Optional[float], bear_high: Optional[float],
    ) -> tuple[bool, str]:
        if bull_low is not None and bull_high is not None and bull_low <= price <= bull_high:
            return True, "bullish"
        if bear_low is not None and bear_high is not None and bear_low <= price <= bear_high:
            return True, "bearish"
        return False, "none"

    def _calc_score(
        self,
        bias: str,
        last_event: str,
        has_displacement: bool,
        price_in_ob: bool,
        ob_dir: str,
        price_in_fvg: bool,
        fvg_dir: str,
        price_at_ce: bool,
        has_bpr: bool,
        sweep_dir: str,
        pd_zone: Optional[PremiumDiscount],
        price_near_pdh: bool,
        price_near_pdl: bool,
        has_near_displacement: bool = False,
    ) -> int:
        """
        SMC Score -100..+100

        Weights:
          Bias                  : ±20
          CHoCH                 : ±20 (đảo chiều)
          BoS                   : ±10 (tiếp diễn)
          Displacement confirm  : ±10 (MSS xác nhận)
          Price in OB           : ±20 (entry zone)
          Price at CE (FVG)     : ±15 (entry lý tưởng)
          Price in FVG          : ±10 (imbalance)
          BPR present           : ±10 (institutional zone)
          Sweep                 : ±15 (confirmation mạnh)
          Premium/Discount align: ±10 (context vị trí giá)
          PDH/PDL proximity     : ±5  (institutional reference)
        """
        score = 0

        if bias == "BULLISH":
            score += 20
        elif bias == "BEARISH":
            score -= 20

        if last_event == "CHoCH_bull":
            score += 20
        elif last_event == "CHoCH_bear":
            score -= 20
        elif last_event == "BoS_bull":
            score += 10
        elif last_event == "BoS_bear":
            score -= 10

        if has_displacement:
            if bias == "BULLISH":
                score += 10
            elif bias == "BEARISH":
                score -= 10
            # FVG bonus: displacement that also created FVG
            if price_in_fvg:
                score += 5 if bias == "BULLISH" else -5
        elif has_near_displacement:
            if bias == "BULLISH":
                score += 5
            elif bias == "BEARISH":
                score -= 5

        if price_in_ob:
            score += 20 if ob_dir == "bullish" else -20

        if price_at_ce:
            score += 15 if fvg_dir == "bullish" else -15
        elif price_in_fvg:
            score += 10 if fvg_dir == "bullish" else -10

        if has_bpr:
            score += 10 if bias == "BULLISH" else -10

        if sweep_dir == "sell_side_swept":
            score += 15
        elif sweep_dir == "buy_side_swept":
            score -= 15

        if pd_zone:
            if bias == "BULLISH" and pd_zone.current_zone == "discount":
                score += 10
            elif bias == "BEARISH" and pd_zone.current_zone == "premium":
                score -= 10
            elif bias == "BULLISH" and pd_zone.current_zone == "premium":
                score -= 5
            elif bias == "BEARISH" and pd_zone.current_zone == "discount":
                score += 5

        if price_near_pdh:
            score -= 5
        if price_near_pdl:
            score += 5

        return max(-100, min(100, score))

    def _build_summary(
        self,
        bias: str,
        last_event: str,
        struct_strength: float,
        has_displacement: bool,
        bull_ob: Optional[OrderBlock],
        bear_ob: Optional[OrderBlock],
        bull_fvg: Optional[FairValueGap],
        bear_fvg: Optional[FairValueGap],
        buy_liq: Optional[LiquidityLevel],
        sell_liq: Optional[LiquidityLevel],
        sweep_dir: str,
        smc_score: int,
        current_price: float,
        price_in_ob: bool,
        ob_dir: str,
        pd_zone: Optional[PremiumDiscount],
        in_ote: bool,
        institutional: Optional[InstitutionalLevels],
        has_near_displacement: bool = False,
    ) -> str:
        parts = [f"SMC bias={bias} ({last_event}, strength={struct_strength:.2f}%)"]

        if has_displacement:
            parts.append("DISPLACEMENT confirmed")
        elif has_near_displacement:
            parts.append("NEAR-DISPLACEMENT detected")

        if pd_zone:
            ote_str = " [IN OTE]" if in_ote else ""
            parts.append(f"Zone={pd_zone.current_zone.upper()}{ote_str} EQ={pd_zone.equilibrium:.2f}")

        if price_in_ob:
            ob = bull_ob if ob_dir == "bullish" else bear_ob
            if ob:
                parts.append(
                    f"Price IN {ob_dir.upper()} OB [{ob.price_low:.2f}-{ob.price_high:.2f}] "
                    f"strength={ob.strength_pct:.1f}%"
                )
        else:
            if bull_ob:
                dist = (current_price - bull_ob.mid) / current_price * 100
                parts.append(f"Bullish OB @ {bull_ob.mid:.2f} ({dist:+.1f}%)")
            if bear_ob:
                dist = (bear_ob.mid - current_price) / current_price * 100
                parts.append(f"Bearish OB @ {bear_ob.mid:.2f} ({dist:+.1f}%)")

        if bull_fvg and not bull_fvg.filled:
            parts.append(f"Bull FVG [{bull_fvg.bottom:.2f}-{bull_fvg.top:.2f}] CE={bull_fvg.ce:.2f}")
        if bear_fvg and not bear_fvg.filled:
            parts.append(f"Bear FVG [{bear_fvg.bottom:.2f}-{bear_fvg.top:.2f}] CE={bear_fvg.ce:.2f}")

        if buy_liq:
            parts.append(f"Buy-side liq @ {buy_liq.price:.2f}")
        if sell_liq:
            parts.append(f"Sell-side liq @ {sell_liq.price:.2f}")

        if sweep_dir != "none":
            parts.append(f"*** SWEEP: {sweep_dir} ***")

        if institutional:
            if institutional.pdh:
                parts.append(f"PDH={institutional.pdh:.2f} PDL={institutional.pdl:.2f}")

        parts.append(f"SMC_score={smc_score}/100")
        return " | ".join(parts)
