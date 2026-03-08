"""
utils/crypto_confluence.py - Crypto-specific confluence cho SMC

3 lớp: Funding Rate, Open Interest, CVD.
Dùng để điều chỉnh confidence của SMC setup trước khi emit signal.
"""
from __future__ import annotations


def interpret_funding(funding_rate: float, direction: str) -> tuple[float, str]:
    """
    funding_rate: decimal từ Binance (0.0001 = 0.01% per 8h)
    direction: "LONG" | "SHORT"

    Returns: (confidence_multiplier, reason)

    Nguyên tắc:
    - Funding cao dương (+0.05%): thị trường long nhiều → longs sẽ bị squeeze
      → SHORT setup được boost, LONG setup bị penalize
    - Funding cao âm (-0.03%): thị trường short nhiều → shorts bị squeeze
      → LONG setup được boost, SHORT setup bị penalize
    - Funding neutral (±0.01%): không ảnh hưởng

    Thresholds thực tế trên crypto perp:
      Normal range: -0.01% đến +0.03% per 8h
      Elevated: > 0.05% hoặc < -0.03%
      Extreme: > 0.1% hoặc < -0.07%
    """
    fr_pct = funding_rate * 100  # convert sang %

    # Funding extreme dương → thị trường overleveraged long
    if fr_pct > 0.10:
        if direction == "SHORT":
            return 1.3, f"funding extreme +{fr_pct:.3f}% → market overleveraged long, SHORT boost"
        return 0.6, f"funding extreme +{fr_pct:.3f}% → LONG risky, crowd too long"

    # Funding elevated dương
    if fr_pct > 0.05:
        if direction == "SHORT":
            return 1.15, f"funding elevated +{fr_pct:.3f}% → SHORT confluence"
        return 0.85, f"funding elevated +{fr_pct:.3f}% → LONG caution"

    # Funding extreme âm → thị trường overleveraged short
    if fr_pct < -0.07:
        if direction == "LONG":
            return 1.3, f"funding extreme {fr_pct:.3f}% → market overleveraged short, LONG boost"
        return 0.6, f"funding extreme {fr_pct:.3f}% → SHORT risky, crowd too short"

    # Funding elevated âm
    if fr_pct < -0.03:
        if direction == "LONG":
            return 1.15, f"funding negative {fr_pct:.3f}% → LONG confluence"
        return 0.85, f"funding negative {fr_pct:.3f}% → SHORT caution"

    # Neutral zone
    return 1.0, f"funding neutral {fr_pct:.3f}%"


def interpret_oi(
    oi_change_pct: float,
    price_change_pct: float,
    direction: str,
) -> tuple[float, str]:
    """
    4 tình huống OI × Price:

    ┌──────────────┬────────────┬──────────────────────────────────┐
    │ OI           │ Price      │ Ý nghĩa                          │
    ├──────────────┼────────────┼──────────────────────────────────┤
    │ Tăng         │ Tăng       │ Long buildup — trend thật        │
    │ Tăng         │ Giảm      │ Short buildup — trend thật       │
    │ Giảm         │ Tăng       │ Short squeeze / unwind           │
    │ Giảm         │ Giảm       │ Long liquidation / unwind        │
    └──────────────┴────────────┴──────────────────────────────────┘

    "Trend thật" = displacement có tổ chức vào → boost SMC signal
    "Unwind" = có thể đảo chiều → caution
    """
    oi_up = oi_change_pct > 2.0
    oi_down = oi_change_pct < -2.0
    price_up = price_change_pct > 0

    # Tình huống 1: OI tăng + price tăng → longs vào thật → xác nhận LONG SMC
    if oi_up and price_up:
        if direction == "LONG":
            return 1.2, f"OI+{oi_change_pct:.1f}% + price up → real long buildup, LONG confirmed"
        return 0.85, f"OI+{oi_change_pct:.1f}% + price up → long trend building, SHORT risky"

    # Tình huống 2: OI tăng + price giảm → shorts vào thật → xác nhận SHORT SMC
    if oi_up and not price_up:
        if direction == "SHORT":
            return 1.2, f"OI+{oi_change_pct:.1f}% + price down → real short buildup, SHORT confirmed"
        return 0.85, f"OI+{oi_change_pct:.1f}% + price down → short trend building, LONG risky"

    # Tình huống 3: OI giảm + price tăng → short squeeze hoặc long unwind đảo chiều
    if oi_down and price_up:
        if direction == "LONG":
            return 1.15, f"OI{oi_change_pct:.1f}% + price up → short squeeze, LONG momentum"
        return 0.7, f"OI{oi_change_pct:.1f}% + price up → short squeeze ongoing, SHORT dangerous"

    # Tình huống 4: OI giảm + price giảm → long liquidation
    if oi_down and not price_up:
        if direction == "SHORT":
            return 1.15, f"OI{oi_change_pct:.1f}% + price down → long liq, SHORT momentum"
        return 0.7, f"OI{oi_change_pct:.1f}% + price down → long liquidation, LONG dangerous"

    # OI thay đổi nhỏ — không đủ signal
    return 1.0, f"OI change {oi_change_pct:.1f}% — neutral"


def interpret_cvd(
    cvd_data: dict,
    direction: str,
    price_in_ob: bool,
    price_in_fvg: bool,
) -> tuple[float, str]:
    """
    CVD (Cumulative Volume Delta) = buy_vol - sell_vol từ aggTrades.

    Quan trọng nhất khi giá đang TRONG OB hoặc FVG zone:
    - LONG setup + giá ở OB + CVD tăng → buyers đang vào zone = CONFIRM
    - LONG setup + giá ở OB + CVD giảm → sellers vẫn control = WARNING

    cvd_data keys: cvd, cvd_ratio, buy_vol, sell_vol, cvd_trend
      cvd_ratio: 0.0-1.0, >0.55 = buy pressure dominant
      cvd_trend: "accelerating_buy" | "accelerating_sell" | "neutral"
    """
    cvd_ratio = cvd_data.get("cvd_ratio", 0.5)
    cvd_trend = cvd_data.get("cvd_trend", "neutral")
    in_zone = price_in_ob or price_in_fvg

    # CVD chỉ quan trọng khi giá ĐÃ VÀO zone — nếu không ở zone thì neutral
    if not in_zone:
        if direction == "LONG" and cvd_trend == "accelerating_buy":
            return 1.1, f"CVD accelerating buy (ratio={cvd_ratio:.2f}), LONG momentum"
        if direction == "SHORT" and cvd_trend == "accelerating_sell":
            return 1.1, f"CVD accelerating sell (ratio={cvd_ratio:.2f}), SHORT momentum"
        return 1.0, f"CVD ratio={cvd_ratio:.2f} (not in SMC zone)"

    # Giá đang ở trong OB/FVG zone → CVD là confirmation cực quan trọng
    if direction == "LONG":
        if cvd_ratio > 0.58 or cvd_trend == "accelerating_buy":
            return 1.25, f"CVD confirms LONG at OB/FVG zone (ratio={cvd_ratio:.2f}, {cvd_trend})"
        if cvd_ratio < 0.42 or cvd_trend == "accelerating_sell":
            return 0.65, f"CVD contradicts LONG — sellers in zone (ratio={cvd_ratio:.2f})"
        return 1.05, f"CVD neutral at zone (ratio={cvd_ratio:.2f})"

    if direction == "SHORT":
        if cvd_ratio < 0.42 or cvd_trend == "accelerating_sell":
            return 1.25, f"CVD confirms SHORT at OB/FVG zone (ratio={cvd_ratio:.2f}, {cvd_trend})"
        if cvd_ratio > 0.58 or cvd_trend == "accelerating_buy":
            return 0.65, f"CVD contradicts SHORT — buyers in zone (ratio={cvd_ratio:.2f})"
        return 1.05, f"CVD neutral at zone (ratio={cvd_ratio:.2f})"

    return 1.0, "CVD: direction unknown"
