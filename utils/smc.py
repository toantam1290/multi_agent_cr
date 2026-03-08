"""
utils/smc.py - Smart Money Concepts (SMC) Analyzer

Inputs: pandas DataFrame OHLCV (từ BinanceDataFetcher.get_klines)
Outputs: SMCSignal dataclass

Thành phần:
  1. Swing High/Low detection (nền tảng của tất cả SMC)
  2. Market Structure — CHoCH / BoS
  3. Order Blocks (OB) — vùng tổ chức đặt lệnh
  4. Fair Value Gaps (FVG) — vùng mất cân bằng
  5. Liquidity Levels — equal highs/lows (nơi stop loss cụm lại)
  6. Liquidity Sweep detection — tổ chức vừa "hunt" stops
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
# Dataclasses (kết quả trung gian)
# ─────────────────────────────────────────────

@dataclass
class OrderBlock:
    """Vùng nến gốc trước khi impulse xảy ra."""
    price_high: float
    price_low: float
    mid: float
    direction: str        # "bullish" | "bearish"
    candle_index: int     # iloc position trong df
    strength_pct: float   # % move của impulse theo sau


@dataclass
class FairValueGap:
    """Khoảng trống giữa nến i-2 và nến i (3-candle pattern)."""
    top: float
    bottom: float
    mid: float
    direction: str    # "bullish" | "bearish"
    filled: bool      # True = giá đã quay lại lấp đầy


@dataclass
class LiquidityLevel:
    """Vùng stop loss cụm (equal highs / equal lows)."""
    price: float
    side: str      # "buy_side" (above price) | "sell_side" (below price)
    touches: int   # Số lần chạm
    swept: bool    # True = đã bị sweep


# ─────────────────────────────────────────────
# SMCSignal — output chính
# ─────────────────────────────────────────────

@dataclass
class SMCSignal:
    # Market Structure
    bias: str = "NEUTRAL"
    # "CHoCH_bull" | "CHoCH_bear" | "BoS_bull" | "BoS_bear" | "none"
    last_structure_event: str = "none"
    structure_strength_pct: float = 0.0  # % move phá vỡ structure

    # Order Blocks
    nearest_bullish_ob: Optional[OrderBlock] = None
    nearest_bearish_ob: Optional[OrderBlock] = None
    price_in_ob: bool = False
    ob_direction: str = "none"   # "bullish" | "bearish" | "none"

    # Fair Value Gaps
    nearest_bullish_fvg: Optional[FairValueGap] = None
    nearest_bearish_fvg: Optional[FairValueGap] = None
    price_in_fvg: bool = False
    fvg_direction: str = "none"

    # Liquidity
    buy_side_liquidity: Optional[LiquidityLevel] = None   # equal highs trên giá
    sell_side_liquidity: Optional[LiquidityLevel] = None  # equal lows dưới giá
    sweep_direction: str = "none"  # "buy_side_swept" | "sell_side_swept" | "none"

    # Summary
    smc_score: int = 0        # -100..+100 (dương = bullish context)
    smc_valid: bool = False   # True khi |smc_score| >= 30
    summary: str = ""         # Text ngắn gọn đưa vào Claude prompt


# ─────────────────────────────────────────────
# SMCAnalyzer — class chính
# ─────────────────────────────────────────────

class SMCAnalyzer:
    """
    Phân tích SMC từ OHLCV data.
    Không phụ thuộc vào bất kỳ logic indicator nào hiện tại.
    Gọi độc lập sau khi compute_technical_signal đã chạy.
    """

    def __init__(self, fetcher: "BinanceDataFetcher"):
        self.fetcher = fetcher

    def analyze_from_dataframes(
        self,
        df_structure: pd.DataFrame,
        df_timing: pd.DataFrame,
        current_price: float,
    ) -> SMCSignal:
        """
        Sync version cho backtest — không cần fetcher, không async.
        Dùng DataFrames đã có sẵn từ rolling window của backtest engine.
        """
        try:
            return self._run_detection(df_structure, df_timing, current_price)
        except Exception as e:
            logger.warning(f"SMC analyze_from_dataframes failed: {e}")
            return SMCSignal(summary=f"SMC error: {type(e).__name__}")

    def _run_detection(
        self,
        df_structure: pd.DataFrame,
        df_timing: pd.DataFrame,
        current_price: float,
    ) -> SMCSignal:
        """
        Core detection logic — dùng chung bởi analyze() (async) và
        analyze_from_dataframes() (sync/backtest). Không gọi self.fetcher.
        """
        if len(df_structure) < 30 or len(df_timing) < 10:
            return SMCSignal(summary="Insufficient data for SMC analysis")
        swing_highs, swing_lows = self._detect_swings(df_structure, n=5)
        bias, last_event, struct_strength = self._detect_structure(
            df_structure, swing_highs, swing_lows
        )
        bull_obs, bear_obs = self._detect_order_blocks(
            df_structure, swing_highs, swing_lows
        )
        bull_fvgs, bear_fvgs = self._detect_fvg(df_structure, current_price)
        buy_liq, sell_liq = self._detect_liquidity(
            df_structure, swing_highs, swing_lows, current_price
        )
        sweep_dir = self._detect_sweep(df_timing, buy_liq, sell_liq, lookback=10)
        nearest_bull_ob = self._nearest_ob(bull_obs, current_price, "bullish")
        nearest_bear_ob = self._nearest_ob(bear_obs, current_price, "bearish")
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
        smc_score = self._calc_score(
            bias, last_event, price_in_ob, ob_dir,
            price_in_fvg, fvg_dir, sweep_dir
        )
        smc_valid = abs(smc_score) >= 30
        summary = self._build_summary(
            bias, last_event, struct_strength,
            nearest_bull_ob, nearest_bear_ob,
            nearest_bull_fvg, nearest_bear_fvg,
            buy_liq, sell_liq, sweep_dir,
            smc_score, current_price, price_in_ob, ob_dir,
        )
        return SMCSignal(
            bias=bias,
            last_structure_event=last_event,
            structure_strength_pct=struct_strength,
            nearest_bullish_ob=nearest_bull_ob,
            nearest_bearish_ob=nearest_bear_ob,
            price_in_ob=price_in_ob,
            ob_direction=ob_dir,
            nearest_bullish_fvg=nearest_bull_fvg,
            nearest_bearish_fvg=nearest_bear_fvg,
            price_in_fvg=price_in_fvg,
            fvg_direction=fvg_dir,
            buy_side_liquidity=buy_liq,
            sell_side_liquidity=sell_liq,
            sweep_direction=sweep_dir,
            smc_score=smc_score,
            smc_valid=smc_valid,
            summary=summary,
        )

    async def analyze(self, symbol: str, style: str = "scalp") -> SMCSignal:
        """
        Entry point. Fetch klines → detect SMC → trả về SMCSignal.

        Timeframes:
          scalp : structure=15m (100 candle), timing=5m (50 candle)
          swing : structure=1h  (100 candle), timing=15m (50 candle)
        """
        try:
            if style == "scalp":
                df_structure, df_timing = await asyncio.gather(
                    self.fetcher.get_klines(symbol, "15m", 100),
                    self.fetcher.get_klines(symbol, "5m", 50),
                )
            else:
                df_structure, df_timing = await asyncio.gather(
                    self.fetcher.get_klines(symbol, "1h", 100),
                    self.fetcher.get_klines(symbol, "15m", 50),
                )
            if len(df_structure) < 30 or len(df_timing) < 10:
                logger.warning(f"SMC {symbol}: Insufficient data")
                return SMCSignal(summary="Insufficient data for SMC analysis")
            current_price = float(df_timing["close"].iloc[-1])
            signal = self._run_detection(df_structure, df_timing, current_price)
            logger.info(f"SMC {symbol}: bias={signal.bias} event={signal.last_structure_event} score={signal.smc_score} | {signal.summary[:80]}")
            return signal
        except Exception as e:
            logger.warning(f"SMC analysis failed for {symbol}: {e}")
            return SMCSignal(summary=f"SMC error: {type(e).__name__}")

    # ─────────────────────────────────────────
    # DETECTION METHODS
    # ─────────────────────────────────────────

    def _detect_swings(
        self, df: pd.DataFrame, n: int = 5
    ) -> tuple[list[int], list[int]]:
        """
        Swing High: high[i] = max trong cửa sổ [i-n .. i+n]
        Swing Low : low[i]  = min trong cửa sổ [i-n .. i+n]
        n=5 → cần 5 nến xác nhận mỗi bên → khá vững trên 15m
        """
        highs, lows = [], []
        for i in range(n, len(df) - n):
            window_h = df["high"].iloc[i - n : i + n + 1]
            window_l = df["low"].iloc[i - n : i + n + 1]
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
        """
        Phân tích market structure dựa vào 2 swing gần nhất.

        Higher High + Higher Low = Uptrend → BoS_bull nếu close > last swing high
        Lower Low + Lower High   = Downtrend → BoS_bear nếu close < last swing low
        Phá ngược lại            = CHoCH (đảo chiều)

        Returns: (bias, last_event, strength_pct)
        """
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "NEUTRAL", "none", 0.0

        last_sh = float(df["high"].iloc[swing_highs[-1]])
        prev_sh = float(df["high"].iloc[swing_highs[-2]])
        last_sl = float(df["low"].iloc[swing_lows[-1]])
        prev_sl = float(df["low"].iloc[swing_lows[-2]])
        current = float(df["close"].iloc[-2])  # nến đã đóng, tránh false CHoCH khi nến chưa close

        hh = last_sh > prev_sh   # Higher High
        hl = last_sl > prev_sl   # Higher Low
        ll = last_sl < prev_sl   # Lower Low
        lh = last_sh < prev_sh   # Lower High

        bias = "NEUTRAL"
        last_event = "none"
        strength = 0.0

        if hh and hl:
            # Đang uptrend
            bias = "BULLISH"
            if current > last_sh:
                last_event = "BoS_bull"
                strength = (current - last_sh) / last_sh * 100
        elif ll and lh:
            # Đang downtrend
            bias = "BEARISH"
            if current < last_sl:
                last_event = "BoS_bear"
                strength = (last_sl - current) / last_sl * 100
        else:
            # Mixed structure — check CHoCH
            if current > last_sh:
                # Giá phá swing high trong downtrend → bias đổi sang bullish
                bias = "BULLISH"
                last_event = "CHoCH_bull"
                strength = (current - last_sh) / last_sh * 100
            elif current < last_sl:
                # Giá phá swing low trong uptrend → bias đổi sang bearish
                bias = "BEARISH"
                last_event = "CHoCH_bear"
                strength = (last_sl - current) / last_sl * 100

        return bias, last_event, round(strength, 3)

    def _detect_order_blocks(
        self,
        df: pd.DataFrame,
        swing_highs: list[int],
        swing_lows: list[int],
    ) -> tuple[list[OrderBlock], list[OrderBlock]]:
        """
        Bullish OB: nến bearish (đỏ) cuối cùng TRƯỚC khi giá bật lên tạo swing high.
        Bearish OB: nến bullish (xanh) cuối cùng TRƯỚC khi giá rớt xuống tạo swing low.

        Logic: nhìn ngược từ swing high/low → tìm nến gốc ngược chiều.
        Chỉ tính OB nếu impulse theo sau >= 0.3% (tránh noise).
        """
        bull_obs: list[OrderBlock] = []
        bear_obs: list[OrderBlock] = []

        # Bullish OB: từ các swing high gần nhất (tối đa 3)
        for sh_idx in swing_highs[-3:]:
            for i in range(sh_idx - 1, max(0, sh_idx - 20), -1):
                o = float(df["open"].iloc[i])
                c = float(df["close"].iloc[i])
                if c < o:  # Nến đỏ (bearish)
                    if i + 1 < len(df):
                        next_c = float(df["close"].iloc[i + 1])
                        impulse = (next_c - c) / c * 100
                        if impulse >= 0.3:  # Có impulse bullish thực sự
                            bull_obs.append(OrderBlock(
                                price_high=float(df["high"].iloc[i]),
                                price_low=float(df["low"].iloc[i]),
                                mid=(float(df["high"].iloc[i]) + float(df["low"].iloc[i])) / 2,
                                direction="bullish",
                                candle_index=i,
                                strength_pct=round(impulse, 3),
                            ))
                    break  # Chỉ lấy nến bearish CUỐI CÙNG trước impulse

        # Bearish OB: từ các swing low gần nhất (tối đa 3)
        for sl_idx in swing_lows[-3:]:
            for i in range(sl_idx - 1, max(0, sl_idx - 20), -1):
                o = float(df["open"].iloc[i])
                c = float(df["close"].iloc[i])
                if c > o:  # Nến xanh (bullish)
                    if i + 1 < len(df):
                        next_c = float(df["close"].iloc[i + 1])
                        impulse = (c - next_c) / c * 100
                        if impulse >= 0.3:
                            bear_obs.append(OrderBlock(
                                price_high=float(df["high"].iloc[i]),
                                price_low=float(df["low"].iloc[i]),
                                mid=(float(df["high"].iloc[i]) + float(df["low"].iloc[i])) / 2,
                                direction="bearish",
                                candle_index=i,
                                strength_pct=round(impulse, 3),
                            ))
                    break

        return bull_obs, bear_obs

    def _detect_fvg(
        self, df: pd.DataFrame, current_price: float
    ) -> tuple[list[FairValueGap], list[FairValueGap]]:
        """
        Bullish FVG: df["low"][i] > df["high"][i-2]  → gap UP giữa nến i và i-2
        Bearish FVG: df["high"][i] < df["low"][i-2]  → gap DOWN giữa nến i và i-2

        filled=True khi giá hiện tại đã chạm vào vùng gap (đã lấp đầy).
        Chỉ lấy 20 nến gần nhất để tránh FVG quá cũ.
        """
        bull_fvgs: list[FairValueGap] = []
        bear_fvgs: list[FairValueGap] = []

        start = max(2, len(df) - 20)  # Chỉ 20 nến gần nhất

        for i in range(start, len(df)):
            low_i = float(df["low"].iloc[i])
            high_i = float(df["high"].iloc[i])
            high_i_m2 = float(df["high"].iloc[i - 2])
            low_i_m2 = float(df["low"].iloc[i - 2])

            # Bullish FVG
            if low_i > high_i_m2:
                bull_fvgs.append(FairValueGap(
                    top=low_i,
                    bottom=high_i_m2,
                    mid=(low_i + high_i_m2) / 2,
                    direction="bullish",
                    filled=(current_price <= high_i_m2),
                ))

            # Bearish FVG
            if high_i < low_i_m2:
                bear_fvgs.append(FairValueGap(
                    top=low_i_m2,
                    bottom=high_i,
                    mid=(low_i_m2 + high_i) / 2,
                    direction="bearish",
                    filled=(current_price >= high_i),  # Price rallied back into gap from below
                ))

        return bull_fvgs, bear_fvgs

    def _detect_liquidity(
        self,
        df: pd.DataFrame,
        swing_highs: list[int],
        swing_lows: list[int],
        current_price: float,
        tolerance: float = 0.0015,  # 0.15% — equal highs/lows
    ) -> tuple[Optional[LiquidityLevel], Optional[LiquidityLevel]]:
        """
        Equal Highs (buy-side liquidity): 2+ swing high trong biên độ tolerance,
        nằm TRÊN current_price → stop loss của short đang cụm ở đây.

        Equal Lows (sell-side liquidity): 2+ swing low trong biên độ tolerance,
        nằm DƯỚI current_price → stop loss của long đang cụm ở đây.
        """
        buy_liq: Optional[LiquidityLevel] = None
        sell_liq: Optional[LiquidityLevel] = None

        # Buy-side: equal highs above price
        sh_prices = sorted(
            [float(df["high"].iloc[i]) for i in swing_highs
             if float(df["high"].iloc[i]) > current_price]
        )
        for i in range(len(sh_prices) - 1):
            ratio = abs(sh_prices[i + 1] - sh_prices[i]) / sh_prices[i]
            if ratio < tolerance:
                buy_liq = LiquidityLevel(
                    price=(sh_prices[i] + sh_prices[i + 1]) / 2,
                    side="buy_side",
                    touches=2,
                    swept=False,
                )
                break

        # Sell-side: equal lows below price
        sl_prices = sorted(
            [float(df["low"].iloc[i]) for i in swing_lows
             if float(df["low"].iloc[i]) < current_price],
            reverse=True,
        )
        for i in range(len(sl_prices) - 1):
            ratio = abs(sl_prices[i] - sl_prices[i + 1]) / sl_prices[i + 1]
            if ratio < tolerance:
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
        """
        Sweep = wick vượt qua liquidity level nhưng CLOSE trở lại phía trong.
        Đây là tín hiệu mạnh nhất trong SMC — tổ chức vừa grab stops.

        buy_side_swept  → wick lên trên equal highs → close xuống dưới → BEARISH signal
        sell_side_swept → wick xuống dưới equal lows → close lên trên → BULLISH signal
        """
        recent = df.iloc[-lookback:]

        if buy_liq:
            for i in range(len(recent)):
                h = float(recent["high"].iloc[i])
                c = float(recent["close"].iloc[i])
                if h > buy_liq.price and c < buy_liq.price:
                    return "buy_side_swept"

        if sell_liq:
            for i in range(len(recent)):
                l = float(recent["low"].iloc[i])
                c = float(recent["close"].iloc[i])
                if l < sell_liq.price and c > sell_liq.price:
                    return "sell_side_swept"

        return "none"

    # ─────────────────────────────────────────
    # HELPER METHODS
    # ─────────────────────────────────────────

    def _nearest_ob(
        self,
        obs: list[OrderBlock],
        current_price: float,
        direction: str,
    ) -> Optional[OrderBlock]:
        """OB gần giá nhất, phía đúng (bullish OB phải nằm dưới giá, bearish trên)."""
        if not obs:
            return None
        if direction == "bullish":
            candidates = [ob for ob in obs if ob.price_high <= current_price * 1.005]
        else:
            candidates = [ob for ob in obs if ob.price_low >= current_price * 0.995]
        if not candidates:
            return None
        return min(candidates, key=lambda ob: abs(ob.mid - current_price))

    def _price_in_zone(
        self,
        price: float,
        bull_low: Optional[float], bull_high: Optional[float],
        bear_low: Optional[float], bear_high: Optional[float],
    ) -> tuple[bool, str]:
        """Kiểm tra giá có nằm trong zone không. Trả về (in_zone, direction)."""
        if bull_low is not None and bull_high is not None and bull_low <= price <= bull_high:
            return True, "bullish"
        if bear_low is not None and bear_high is not None and bear_low <= price <= bear_high:
            return True, "bearish"
        return False, "none"

    def _calc_score(
        self,
        bias: str,
        last_event: str,
        price_in_ob: bool,
        ob_dir: str,
        price_in_fvg: bool,
        fvg_dir: str,
        sweep_dir: str,
    ) -> int:
        """
        SMC Score: -100 (full bearish) → +100 (full bullish)

        Weights:
          Bias (structure context)   : ±30
          CHoCH (bias change)       : ±25  ← signal mạnh nhất
          BoS (continuation)       : ±15
          Price in OB (entry zone)  : ±25
          Price in FVG (imbalance) : ±15
          Liquidity sweep          : ±20  ← confirmation mạnh
        """
        score = 0

        if bias == "BULLISH":
            score += 30
        elif bias == "BEARISH":
            score -= 30

        if last_event == "CHoCH_bull":
            score += 25
        elif last_event == "CHoCH_bear":
            score -= 25
        elif last_event == "BoS_bull":
            score += 15
        elif last_event == "BoS_bear":
            score -= 15

        if price_in_ob:
            score += 25 if ob_dir == "bullish" else -25

        if price_in_fvg:
            score += 15 if fvg_dir == "bullish" else -15

        # Sweep: đọc ngược chiều sweep
        # sell_side_swept = tổ chức đã grab short stops → expect UP
        if sweep_dir == "sell_side_swept":
            score += 20
        elif sweep_dir == "buy_side_swept":
            score -= 20

        return max(-100, min(100, score))

    def _build_summary(
        self,
        bias: str,
        last_event: str,
        struct_strength: float,
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
    ) -> str:
        """Build human-readable summary để đưa vào Claude prompt."""
        parts = [f"SMC bias={bias} ({last_event}, strength={struct_strength:.2f}%)"]

        if price_in_ob:
            ob = bull_ob if ob_dir == "bullish" else bear_ob
            if ob:
                parts.append(
                    f"Price IN {ob_dir.upper()} OB "
                    f"[{ob.price_low:.2f}-{ob.price_high:.2f}] "
                    f"strength={ob.strength_pct:.1f}%"
                )
        else:
            if bull_ob:
                dist = (current_price - bull_ob.mid) / current_price * 100
                parts.append(f"Nearest bullish OB @ {bull_ob.mid:.2f} ({dist:+.1f}%)")
            if bear_ob:
                dist = (bear_ob.mid - current_price) / current_price * 100
                parts.append(f"Nearest bearish OB @ {bear_ob.mid:.2f} ({dist:+.1f}%)")

        if bull_fvg and not bull_fvg.filled:
            parts.append(f"Bullish FVG unfilled [{bull_fvg.bottom:.2f}-{bull_fvg.top:.2f}]")
        if bear_fvg and not bear_fvg.filled:
            parts.append(f"Bearish FVG unfilled [{bear_fvg.bottom:.2f}-{bear_fvg.top:.2f}]")

        if buy_liq:
            parts.append(f"Buy-side liquidity @ {buy_liq.price:.2f} (stops above)")
        if sell_liq:
            parts.append(f"Sell-side liquidity @ {sell_liq.price:.2f} (stops below)")

        if sweep_dir != "none":
            parts.append(f"*** LIQUIDITY SWEEP: {sweep_dir} — strong reversal signal ***")

        parts.append(f"SMC_score={smc_score}/100")
        return " | ".join(parts)
