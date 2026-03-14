"""
backtest.py — Walk-forward Backtest Engine
==========================================
Replay đúng logic production trên historical data để validate:
- Win rate / Avg RR / Max drawdown / Sharpe ratio
- Filter nào thực sự giúp (toggle từng filter)
- Market condition nào bot hoạt động tốt / tệ
- Parameter optimization (confluence threshold, ATR mult, RR ratio...)

Usage:
    python backtest.py --symbol BTCUSDT --style scalp --from 2024-09-01 --to 2025-03-01
    python backtest.py --symbol BTCUSDT,ETHUSDT --style scalp --days 180
    python backtest.py --symbol BTCUSDT --style scalp --walk-forward --wf-train 120 --wf-test 30
    python backtest.py --symbol BTCUSDT --style scalp --optimize  # sweep params

Không cần API key — dùng Binance public endpoints.
CVD, orderbook imbalance, whale signal: không có historical data
→ Được thay thế bằng volume proxy và funding rate historical.

Design:
- Mỗi "step" = 1 candle của fast TF (15m cho scalp, 1h cho swing)
- Rolling window: lấy đủ candles cho mỗi TF, compute indicators đúng như production
- Simulate trade: check future candles xem SL hay TP hit trước
- Trail stop: apply đúng logic production (breakeven tại 50%, lock 50% tại 80%)
- Time exit: 45 phút (9×5m) cho scalp — force close
- Session filter: apply từ timestamp của candle
- Chop Index: > 61.8 → skip (scalp)
- Correlation: tối đa 2 vị thế cùng hướng (multi-symbol combined mode)
- Dynamic confluence: win rate < 45% (last 20) → min_confluence=4
- News blackout: không có historical calendar → bỏ qua trong backtest
- Walk-forward: chia data thành các windows IS/OOS, báo cáo OOS performance
"""

import argparse
import asyncio
import math
import sys
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd
import pandas_ta as ta

# ─── Constants (mirror production config) ────────────────────────────────────

FEE_PCT = 0.001          # 0.1% mỗi chiều
SLIPPAGE_PCT = 0.0005    # 0.05% slippage khi fill (base, adjusted per exit type)
SLIPPAGE_SL_PCT = 0.0015 # 0.15% SL exit slippage (market order hitting bid)
SLIPPAGE_TP_PCT = 0.0005 # 0.05% TP exit slippage (limit order)
SCALP_RR = 2.0
SWING_RR = 2.0
MAX_HOLD_CANDLES_SCALP = 9    # 9 × 5m = 45 phút
MAX_HOLD_CANDLES_SWING = 48   # 48 × 1h = 2 ngày
INITIAL_BALANCE = 10_000.0    # USDT paper — hiển thị trong report
MAX_POSITION_PCT = 0.02       # 2% per trade
BACKTEST_CACHE_DIR = "data/backtest_cache"
MAX_OPEN_POSITIONS = 3
MAX_SAME_DIRECTION = 2        # Correlation: tối đa 2 vị thế cùng hướng (LONG/SHORT)
MAX_DAILY_LOSS_PCT = 0.01     # 1% daily loss limit
CHOP_SKIP_THRESHOLD = 61.8    # Chop Index > 61.8 = choppy, skip scalp

# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    symbols: list[str]
    style: str                   # scalp | swing
    date_from: datetime
    date_to: datetime
    # Filter toggles — set False để measure impact của từng filter
    use_rule_filter: bool = True
    use_ema9_filter: bool = True
    use_confluence_filter: bool = True
    use_cvd_proxy: bool = True   # volume-based CVD proxy
    use_vwap_filter: bool = True
    use_session_filter: bool = True
    use_regime_filter: bool = True
    use_chop_filter: bool = True       # Chop Index > 61.8 → skip (scalp)
    use_smc_filter: bool = True        # SMC opposing + confluence + OB override
    use_smc_standalone: bool = False   # True = chạy SMC standalone thay rule-based
    use_correlation_filter: bool = True # Tối đa 2 vị thế cùng hướng
    use_dynamic_confluence: bool = True # Win rate < 45% → min_confluence=4
    use_sl_structure: bool = True
    use_trail_stop: bool = True
    use_partial_close: bool = True   # Chốt 50% tại 1:1 RR, move SL về entry, phần còn lại chạy đến TP
    # Momentum gate: True = hard gate (phải có momentum mới pass),
    # False = momentum chỉ là bonus +15 vào net_score
    use_momentum_gate: bool = True
    # Net score threshold override (0 = auto: scalp=20, swing=10)
    net_score_min: int = 0
    # Tunable parameters
    confluence_threshold: int = 3
    scalp_rr: float = SCALP_RR
    swing_rr: float = SWING_RR
    scalp_rsi_long_max: float = 50.0
    scalp_rsi_short_min: float = 50.0
    funding_long_max_pct: float = 0.05
    # Rule cases: full | long_only | short_only | no_volume | no_momentum
    rule_case: str = "full"
    # SMC standalone tunable (khi use_smc_standalone=True)
    smc_min_rr_tp1: float = 1.8      # Min R:R TP1 — tăng lọc setup chất lượng (was 1.5)
    smc_min_confidence: int = 55     # Min confidence — tăng filter (was 50)
    smc_sl_buffer_pct: float = 0.003  # SL buffer 0.3% — tránh wick hit (was 0.2%)
    smc_ob_entry_only: bool = True    # Chỉ trade ob_entry — bpr(21%WR) và sweep(36%WR) là negative edge
    smc_disable_ce_entry: bool = False  # No-op: ce_entry đã bị disable trong smc_strategy.py, giữ lại cho backward compat
    smc_min_grade: str = ""           # Chỉ grade A+ hoặc A: "A" | "" = không filter
    smc_displacement_only: bool = False  # Chỉ ltf_trigger=displacement, bỏ sweep/choch
    smc_chop_threshold: float = 61.8   # Chop > threshold = skip (strict: 50)
    # SMC standalone extra filters (nhẹ, không gây 0 trades)
    smc_use_chop_filter: bool = True   # Chop > threshold = choppy, skip
    smc_use_funding_filter: bool = True  # LONG khi funding <= 0, SHORT khi funding >= 0
    smc_use_adx_filter: bool = False  # ADX > min = trending only (default off)
    smc_adx_min: float = 20.0
    smc_breakeven_candles: int = 0     # 0=off; >0: sau N candles move SL lên entry
    # Walk-forward
    walk_forward: bool = False
    wf_train_days: int = 120
    wf_test_days: int = 30


@dataclass
class TradeResult:
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: Optional[datetime]
    entry_price: float
    sl: float
    tp: float
    exit_price: float
    outcome: str                 # TP | SL | TIME_EXIT | MAX_HOLD
    pnl_pct: float
    pnl_usdt: float
    confluence_score: int
    regime: str
    session: str
    hold_candles: int
    sl_trailing_state: str = "original"
    # SMC standalone fields
    entry_model: str = ""
    entry_model_quality: str = ""
    smc_htf_bias: str = ""
    smc_ltf_trigger: str = ""
    smc_confidence: int = 0


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[TradeResult] = field(default_factory=list)
    # Computed after run
    win_rate: float = 0.0
    avg_rr: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    total_pnl_pct: float = 0.0
    trades_per_day: float = 0.0
    avg_hold_candles: float = 0.0


# ─── Binance data fetcher (sync version for backtest) ────────────────────────

BINANCE_BASE = "https://api.binance.com/api/v3"
FUTURES_BASE = "https://fapi.binance.com/fapi/v1"

async def fetch_klines(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
) -> pd.DataFrame:
    """Fetch klines với pagination. Trả về DataFrame OHLCV."""
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        resp = await client.get(
            f"{BINANCE_BASE}/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": end_ms,
                "limit": limit,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        all_klines.extend(data)
        # Next page: timestamp của nến cuối + 1ms
        last_open_time = data[-1][0]
        current_start = last_open_time + 1
        if len(data) < limit:
            break
        await asyncio.sleep(0.1)  # Rate limit courtesy

    if not all_klines:
        return pd.DataFrame()

    df = pd.DataFrame(all_klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume", "taker_buy_base", "taker_buy_quote"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)
    return df


async def fetch_funding_history(
    client: httpx.AsyncClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """Fetch funding rate history từ Binance futures."""
    try:
        all_rates = []
        current_start = start_ms
        while current_start < end_ms:
            resp = await client.get(
                f"{FUTURES_BASE}/fundingRate",
                params={"symbol": symbol, "startTime": current_start, "endTime": end_ms, "limit": 1000},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            all_rates.extend(data)
            current_start = data[-1]["fundingTime"] + 1
            if len(data) < 1000:
                break
            await asyncio.sleep(0.05)

        if not all_rates:
            return pd.DataFrame()

        df = pd.DataFrame(all_rates)
        df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
        df["fundingRate"] = df["fundingRate"].astype(float)
        df.set_index("fundingTime", inplace=True)
        return df
    except Exception:
        return pd.DataFrame()


def _cache_path(symbol: str, key: str, start_ms: int, end_ms: int) -> Path:
    """Path cho cache file: data/backtest_cache/{symbol}_{key}_{start}_{end}.csv"""
    Path(BACKTEST_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    return Path(BACKTEST_CACHE_DIR) / f"{symbol}_{key}_{start_ms}_{end_ms}.csv"


def _load_cached(path: Path) -> Optional[pd.DataFrame]:
    p = path.with_suffix(".csv")
    if p.exists():
        try:
            df = pd.read_csv(p, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True)
            return df
        except Exception:
            pass
    return None


def _save_cache(df: pd.DataFrame, path: Path):
    path = path.with_suffix(".csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=True)


async def download_all_data(
    config: BacktestConfig,
    use_cache: bool = False,
    download_only: bool = False,
) -> dict[str, dict[str, pd.DataFrame]]:
    """
    Download tất cả klines cần thiết cho tất cả symbols.
    use_cache: load từ data/backtest_cache nếu có, else download
    download_only: chỉ download và lưu cache, không return (dùng để prefetch)
    """
    warmup_days = 30
    fetch_from = config.date_from - timedelta(days=warmup_days)
    start_ms = int(fetch_from.timestamp() * 1000)
    end_ms = int(config.date_to.timestamp() * 1000)

    if config.style == "scalp":
        intervals = ["5m", "15m", "1h", "4h", "1d"]   # 1d cho PDH/PDL
    else:
        intervals = ["15m", "1h", "4h", "1d", "1w"]   # 1w cho PWH/PWL

    result = {}
    async with httpx.AsyncClient() as client:
        for symbol in config.symbols:
            symbol_data = {}
            for interval in intervals:
                cache_path = _cache_path(symbol, interval, start_ms, end_ms)
                if use_cache:
                    df = _load_cached(cache_path)
                    if df is not None:
                        symbol_data[interval] = df
                        if not download_only:
                            print(f"  {symbol} {interval}({len(df)}) [cache]", end=" ", flush=True)
                        continue
                df = await fetch_klines(client, symbol, interval, start_ms, end_ms)
                symbol_data[interval] = df
                _save_cache(df, cache_path)
                if not download_only:
                    print(f"  {symbol} {interval}({len(df)})", end=" ", flush=True)
            # Funding rate
            cache_path = _cache_path(symbol, "funding", start_ms, end_ms)
            if use_cache:
                funding_df = _load_cached(cache_path)
                if funding_df is not None:
                    symbol_data["funding"] = funding_df
                    if not download_only:
                        print("funding[cache]", end="", flush=True)
                else:
                    funding_df = await fetch_funding_history(client, symbol, start_ms, end_ms)
                    symbol_data["funding"] = funding_df
                    _save_cache(funding_df, cache_path)
                    if not download_only:
                        print("funding", end="", flush=True)
            else:
                funding_df = await fetch_funding_history(client, symbol, start_ms, end_ms)
                symbol_data["funding"] = funding_df
                _save_cache(funding_df, cache_path)
                if not download_only:
                    print("funding", end="", flush=True)
            if not download_only:
                print()
            result[symbol] = symbol_data

    if download_only:
        print(f"\n  Data saved to {BACKTEST_CACHE_DIR}/")
    return result


# ─── Indicator computation (mirrors production compute_technical_signal) ──────

def compute_indicators(
    df_fast: pd.DataFrame,
    df_slow: pd.DataFrame,
    df_trend: pd.DataFrame,
    df_atr: pd.DataFrame,
    df_adx: pd.DataFrame,
    style: str,
    step_ts: Optional["datetime"] = None,
) -> Optional[dict]:
    """
    Compute tất cả indicators từ rolling window DataFrames.
    Returns dict với tất cả fields của TechnicalSignal, hoặc None nếu không đủ data.
    """
    if len(df_fast) < 50 or len(df_slow) < 50 or len(df_trend) < 50:
        return None
    if style == "scalp" and (len(df_atr) < 50 or len(df_adx) < 50):
        return None

    def safe_rsi(df, length=14, default=50.0):
        s = ta.rsi(df["close"], length=length)
        if s is None or s.empty or pd.isna(s.iloc[-1]):
            return default
        return float(s.iloc[-1])

    rsi_1h = safe_rsi(df_fast, 14)
    rsi_4h = safe_rsi(df_slow, 14)

    # EMA9/21 crossover
    ema9 = ta.ema(df_fast["close"], length=9)
    ema21 = ta.ema(df_fast["close"], length=21)
    ema_cross_bullish = ema_cross_bearish = False
    ema9_crossed_recent_up = ema9_crossed_recent_down = False

    if ema9 is not None and ema21 is not None and len(ema9) >= 6:
        e9, e21 = float(ema9.iloc[-1]), float(ema21.iloc[-1])
        close = float(df_fast["close"].iloc[-1])
        prev_close = float(df_fast["close"].iloc[-2])
        prev_e9 = float(ema9.iloc[-2])
        cross_bull = e9 > e21 and float(ema9.iloc[-2]) <= float(ema21.iloc[-2])
        cross_bear = e9 < e21 and float(ema9.iloc[-2]) >= float(ema21.iloc[-2])
        ema_cross_bullish = cross_bull and close > e21
        ema_cross_bearish = cross_bear and close < e21
        # Check cross trong 3 nến đã đóng (iloc[-2,-3,-4])
        for i in range(2, 5):
            if len(ema9) > i and len(ema21) > i:
                ci = float(df_fast["close"].iloc[-i])
                pi = float(df_fast["close"].iloc[-(i+1)]) if len(df_fast) > i else ci
                e9i = float(ema9.iloc[-i])
                e9pi = float(ema9.iloc[-(i+1)]) if len(ema9) > i else e9i
                e21i = float(ema21.iloc[-i])
                if ci > e9i and pi <= e9pi and ci > e21i:
                    ema9_crossed_recent_up = True
                if ci < e9i and pi >= e9pi and ci < e21i:
                    ema9_crossed_recent_down = True

    # MACD
    macd_df = ta.macd(df_fast["close"])
    macd_bullish = macd_bearish = False
    if macd_df is not None and not macd_df.empty and len(macd_df) >= 2:
        macd_line = float(macd_df.iloc[:, 0].iloc[-1])
        signal_line = float(macd_df.iloc[:, 2].iloc[-1])
        macd_bullish = macd_line > signal_line
        macd_bearish = macd_line < signal_line

    # Volume
    vol_slice = df_fast["volume"].iloc[-22:-2]
    avg_volume = vol_slice.mean() if len(vol_slice) > 0 else 0.0
    prev_volume = float(df_fast["volume"].iloc[-2]) if len(df_fast) >= 2 else 0.0
    volume_spike = avg_volume > 0 and prev_volume > avg_volume * 2
    volume_ratio = prev_volume / avg_volume if avg_volume > 0 else 0.0
    volume_trend_up = False
    if len(df_fast) >= 4 and avg_volume > 0:
        v4 = float(df_fast["volume"].iloc[-4])
        v3 = float(df_fast["volume"].iloc[-3])
        v2 = float(df_fast["volume"].iloc[-2])
        volume_trend_up = (v4 < v3 < v2) and (v2 > avg_volume * 0.5)

    # BB
    bb = ta.bbands(df_fast["close"], length=20)
    bb_squeeze = False
    bb_width = 0.0
    if bb is not None and not bb.empty:
        cols = bb.columns.tolist()
        upper = float(bb[cols[2]].iloc[-1]) if len(cols) > 2 else 0
        lower = float(bb[cols[0]].iloc[-1]) if len(cols) > 0 else 0
        mid = float(bb[cols[1]].iloc[-1]) if len(cols) > 1 else 1
        if mid > 0:
            bb_width = (upper - lower) / mid
            bb_squeeze = bb_width < 0.02

    # Trend (EMA50 vs EMA200 cho swing trên 1d, EMA20 vs EMA50 cho scalp trên 4h)
    if style == "swing":
        ema_short = ta.ema(df_trend["close"], length=50)
        ema_long = ta.ema(df_trend["close"], length=200)
    else:
        ema_short = ta.ema(df_trend["close"], length=20)
        ema_long = ta.ema(df_trend["close"], length=50)

    trend_1d = "sideways"
    if ema_short is not None and ema_long is not None and len(ema_short) >= 1 and len(ema_long) >= 1:
        es = float(ema_short.iloc[-1])
        el = float(ema_long.iloc[-1])
        if not (pd.isna(es) or pd.isna(el) or el <= 0):
            if es > el * 1.01:
                trend_1d = "uptrend"
            elif es < el * 0.99:
                trend_1d = "downtrend"

    # Scoring (mirrors production)
    bullish = bearish = 0
    momentum_bullish = momentum_bearish = False

    if 50 < rsi_1h <= 65 and trend_1d == "uptrend":
        bullish += 5
    elif 35 <= rsi_1h < 50 and trend_1d == "downtrend":
        bearish += 5
    if 52 < rsi_1h < 75 and trend_1d == "uptrend":
        bullish += 20
    if 25 < rsi_1h < 48 and trend_1d == "downtrend":
        bearish += 20

    if style == "scalp":
        if rsi_4h < 40 and trend_1d == "uptrend":
            bullish += 10
        if rsi_4h > 70 and trend_1d == "downtrend":
            bearish += 10
        rsi_series = ta.rsi(df_fast["close"], length=14)
        if rsi_series is not None and len(rsi_series) >= 4:
            r0 = float(rsi_series.iloc[-2])
            r1 = float(rsi_series.iloc[-3])
            r2 = float(rsi_series.iloc[-4])
            if rsi_1h > 50 and r0 > r1 and r1 > r2 and (r0 - r2) > 2.0:
                bullish += 15
                momentum_bullish = True
            if rsi_1h < 50 and r0 < r1 and r1 < r2 and (r2 - r0) > 2.0:
                bearish += 15
                momentum_bearish = True
        if len(df_fast) >= 2:
            last_o = float(df_fast["open"].iloc[-2])
            last_h = float(df_fast["high"].iloc[-2])
            last_l = float(df_fast["low"].iloc[-2])
            last_c = float(df_fast["close"].iloc[-2])
            candle_range = last_h - last_l if last_h > last_l else 0.0001
            body_pct = abs(last_c - last_o) / candle_range * 100
            if body_pct > 50:
                if last_c > last_o:
                    bullish += 10
                else:
                    bearish += 10
    else:
        if ema_cross_bullish:
            bullish += 15
        if ema_cross_bearish:
            bearish += 15
        if macd_bullish:
            bullish += 20
        if macd_bearish:
            bearish += 20

    if volume_spike and len(df_fast) >= 2:
        pv_c = float(df_fast["close"].iloc[-2])
        pv_o = float(df_fast["open"].iloc[-2])
        if pv_c > pv_o:
            bullish += 10
        else:
            bearish += 10

    if trend_1d == "uptrend":
        bullish += 10
    if trend_1d == "downtrend":
        bearish += 10

    net_score = bullish - bearish

    # ATR
    atr_series = ta.atr(df_atr["high"], df_atr["low"], df_atr["close"], length=14)
    atr_value = float(atr_series.iloc[-1]) if atr_series is not None and not atr_series.empty else 0.0
    current_price_val = float(df_fast["close"].iloc[-1])
    atr_pct = atr_value / current_price_val * 100 if current_price_val > 0 else 0.0

    # ADX
    adx_df = ta.adx(df_adx["high"], df_adx["low"], df_adx["close"], length=14)
    adx = plus_di = minus_di = 0.0
    if adx_df is not None and not adx_df.empty:
        cols = adx_df.columns.tolist()
        if len(cols) >= 3:
            adx = float(adx_df[cols[0]].iloc[-1]) if not pd.isna(adx_df[cols[0]].iloc[-1]) else 0.0
            plus_di = float(adx_df[cols[1]].iloc[-1]) if not pd.isna(adx_df[cols[1]].iloc[-1]) else 0.0
            minus_di = float(adx_df[cols[2]].iloc[-1]) if not pd.isna(adx_df[cols[2]].iloc[-1]) else 0.0

    # BB width cho regime (từ ADX timeframe)
    bb_regime = ta.bbands(df_adx["close"], length=20)
    bb_width_regime = 0.0
    atr_regime = ta.atr(df_adx["high"], df_adx["low"], df_adx["close"], length=14)
    atr_ratio_regime = 1.0
    if bb_regime is not None and not bb_regime.empty:
        cols = bb_regime.columns.tolist()
        if len(cols) >= 3:
            u = float(bb_regime[cols[2]].iloc[-1])
            l = float(bb_regime[cols[0]].iloc[-1])
            m = float(bb_regime[cols[1]].iloc[-1])
            bb_width_regime = (u - l) / m if m > 0 else 0.0
    if atr_regime is not None and len(atr_regime) >= 20:
        curr_atr = float(atr_regime.iloc[-1])
        avg_atr = float(atr_regime.iloc[-20:].mean())
        atr_ratio_regime = curr_atr / avg_atr if avg_atr > 0 else 1.0

    # Swing structure (10 nến 5m gần nhất)
    recent_atr = df_atr.iloc[-10:] if len(df_atr) >= 10 else df_atr
    swing_low = float(recent_atr["low"].min())
    swing_high = float(recent_atr["high"].max())

    # Chop Index (scalp: > 61.8 = choppy, skip)
    chop_index = 50.0
    chop_series = ta.chop(df_fast["high"], df_fast["low"], df_fast["close"], length=14, atr_length=1)
    if chop_series is not None and not chop_series.empty and not pd.isna(chop_series.iloc[-1]):
        chop_index = float(chop_series.iloc[-1])

    # VWAP: reset mỗi ngày UTC (intraday VWAP thực, không phải VWMA 200 nến)
    vwap_val = vwap_distance_pct = 0.0
    if step_ts is not None and len(df_atr) >= 3:
        today_date = step_ts.date() if hasattr(step_ts, "date") else step_ts
        df_today = df_atr[df_atr.index.date == today_date]
        if len(df_today) >= 3 and df_today["volume"].sum() > 0:
            typical = (df_today["high"] + df_today["low"] + df_today["close"]) / 3
            vwap_val = float((typical * df_today["volume"]).sum() / df_today["volume"].sum())
            if vwap_val > 0:
                vwap_distance_pct = (current_price_val - vwap_val) / vwap_val * 100
    elif len(df_atr) >= 20 and step_ts is None:
        # Fallback khi không có step_ts (backward compat)
        typical = (df_atr["high"] + df_atr["low"] + df_atr["close"]) / 3
        vwap_val = float((typical * df_atr["volume"]).sum() / df_atr["volume"].sum()) if df_atr["volume"].sum() > 0 else 0.0
        if vwap_val > 0:
            vwap_distance_pct = (current_price_val - vwap_val) / vwap_val * 100

    # Volume-based CVD proxy (không có historical tick data)
    # Dùng taker_buy_base volume nếu có (Binance klines có field này)
    cvd_ratio = 0.5
    cvd_trend = "neutral"
    if "taker_buy_base" in df_fast.columns and df_fast["volume"].sum() > 0:
        recent_buy = df_fast["taker_buy_base"].iloc[-20:].sum()
        recent_total = df_fast["volume"].iloc[-20:].sum()
        cvd_ratio = recent_buy / recent_total if recent_total > 0 else 0.5
        # CVD trend: so sánh 10 nến đầu vs 10 nến sau trong 20 nến
        early_buy = df_fast["taker_buy_base"].iloc[-20:-10].sum()
        early_total = df_fast["volume"].iloc[-20:-10].sum()
        late_buy = df_fast["taker_buy_base"].iloc[-10:].sum()
        late_total = df_fast["volume"].iloc[-10:].sum()
        er = early_buy / early_total if early_total > 0 else 0.5
        lr = late_buy / late_total if late_total > 0 else 0.5
        if lr - er > 0.05:
            cvd_trend = "accelerating_buy"
        elif er - lr > 0.05:
            cvd_trend = "accelerating_sell"

    return {
        "rsi_1h": rsi_1h,
        "rsi_4h": rsi_4h,
        "trend_1d": trend_1d,
        "net_score": net_score,
        "momentum_bullish": momentum_bullish,
        "momentum_bearish": momentum_bearish,
        "volume_spike": volume_spike,
        "volume_ratio": volume_ratio,
        "volume_trend_up": volume_trend_up,
        "bb_squeeze": bb_squeeze,
        "bb_width": bb_width,
        "atr_value": atr_value,
        "atr_pct": atr_pct,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "bb_width_regime": bb_width_regime,
        "atr_ratio_regime": atr_ratio_regime,
        "swing_low": swing_low,
        "swing_high": swing_high,
        "vwap": vwap_val,
        "vwap_distance_pct": vwap_distance_pct,
        "ema9_crossed_recent_up": ema9_crossed_recent_up,
        "ema9_crossed_recent_down": ema9_crossed_recent_down,
        "current_price": current_price_val,
        "cvd_ratio": cvd_ratio,
        "cvd_trend": cvd_trend,
        "chop_index": chop_index,
    }


# ─── Regime classification (exact copy from production) ──────────────────────

def classify_regime(adx, plus_di, minus_di, bb_width, atr_ratio):
    if atr_ratio > 1.5 and adx > 25:
        return "trending_volatile"
    if atr_ratio > 1.5:
        return "volatile"
    if adx > 25:
        return "trending_up" if plus_di > minus_di else "trending_down"
    if adx < 20 and bb_width < 0.03:
        return "ranging"
    return "ranging"


# ─── Rule-based filter (mirrors production) ──────────────────────────────────

def rule_based_filter(ind: dict, funding_rate: float, config: BacktestConfig) -> Optional[str]:
    """
    Rule cases: full | long_only | short_only | no_volume | no_momentum
    - full: tất cả điều kiện
    - long_only: chỉ chấp nhận LONG (bỏ qua SHORT)
    - short_only: chỉ chấp nhận SHORT (bỏ qua LONG)
    - no_volume: bỏ qua volume check (scalp)
    - no_momentum: bỏ qua momentum_bullish/bearish (scalp)
    """
    funding_pct = funding_rate * 100
    style = config.style
    # Trend-following: LONG cần RSI 45-75, SHORT cần RSI 22-50 (nới cho loose)
    rsi_long_max = 78.0   # Hard ceiling tránh overbought extreme
    rsi_long_min = 45.0   # Nới xuống 45 cho sideways + uptrend
    rsi_short_min = 22.0  # Hard floor tránh oversold extreme
    rsi_short_max = 62.0 if ind["trend_1d"] == "sideways" else 55.0  # Sideways: 58–62 = overbought cục bộ → SHORT
    if style != "scalp":
        rsi_long_max = config.scalp_rsi_long_max
        rsi_long_min = 35.0
        rsi_short_min = config.scalp_rsi_short_min
        rsi_short_max = 65.0
    rule_case = getattr(config, "rule_case", "full")
    use_momentum_gate = getattr(config, "use_momentum_gate", True)

    # Volume check (scalp) — skip nếu no_volume
    if style == "scalp" and rule_case != "no_volume":
        vol_ok = ind["volume_spike"] or ind["volume_ratio"] >= 1.2 or ind["volume_trend_up"]
        if not vol_ok:
            return None

    # Net score threshold: dùng config nếu set, không thì auto theo style
    cfg_min = getattr(config, "net_score_min", 0)
    if cfg_min > 0:
        net_long_min = cfg_min
        net_short_max = -cfg_min
    else:
        net_long_min = 20 if style == "scalp" else 10
        net_short_max = -20 if style == "scalp" else -10

    # LONG
    if rule_case != "short_only":
        long_ok = (
            ind["trend_1d"] != "downtrend"
            and rsi_long_min < ind["rsi_1h"] < rsi_long_max
            and funding_pct < config.funding_long_max_pct
            and ind["net_score"] > net_long_min
        )
        if long_ok:
            # Khi use_momentum_gate=False: momentum là bonus trong net_score, không gate cứng
            if style == "scalp" and use_momentum_gate and rule_case not in ("no_momentum",):
                if ind["momentum_bullish"]:
                    return "LONG"
            else:
                return "LONG"

    # SHORT
    if rule_case != "long_only":
        short_ok = (
            ind["trend_1d"] != "uptrend"
            and rsi_short_min < ind["rsi_1h"] < rsi_short_max
            and funding_pct > 0.005
            and ind["net_score"] < net_short_max
        )
        if short_ok:
            if style == "scalp" and use_momentum_gate and rule_case not in ("no_momentum",):
                if ind["momentum_bearish"]:
                    return "SHORT"
            else:
                return "SHORT"

    return None


# ─── Confluence score (mirrors production) ───────────────────────────────────

def calc_confluence(ind: dict, direction: str, funding_rate: float, oi_change_pct: float) -> int:
    score = 0
    if direction == "LONG" and ind["trend_1d"] == "uptrend":
        score += 1
    if direction == "SHORT" and ind["trend_1d"] == "downtrend":
        score += 1
    if ind["volume_spike"] or ind["volume_trend_up"]:
        score += 1
    if direction == "LONG" and funding_rate < 0.0002:
        score += 1
    if direction == "SHORT" and funding_rate > 0.0002:
        score += 1
    # CVD: tối đa 1 điểm. SHORT: ratio HOẶC trend (cả hai cùng lúc quá hiếm)
    cvd_ok = (
        (direction == "LONG" and ind["cvd_ratio"] > 0.5 and ind["cvd_trend"] == "accelerating_buy")
        or (direction == "SHORT" and (ind["cvd_ratio"] < 0.45 or ind["cvd_trend"] == "accelerating_sell"))
    )
    if cvd_ok:
        score += 1
    if oi_change_pct > 5 and (
        (direction == "LONG" and ind["trend_1d"] == "uptrend")
        or (direction == "SHORT" and ind["trend_1d"] == "downtrend")
    ):
        score += 1
    # VWAP
    if direction == "LONG" and -0.5 <= ind["vwap_distance_pct"] <= 0:
        score += 1
    if direction == "SHORT" and 0 <= ind["vwap_distance_pct"] <= 0.5:
        score += 1
    return score


# ─── Entry/SL/TP calculation (mirrors production calc_entry_sl_tp) ────────────

def calc_entry_sl_tp(
    direction: str,
    current_price: float,
    atr_value: float,
    regime: str,
    style: str,
    rr_ratio: float,
    swing_low: float = 0.0,
    swing_high: float = 0.0,
) -> Optional[tuple[float, float, float]]:
    if style == "scalp":
        # Scalp: ATR-only SL (bỏ swing structure — 1h swing 10 nến mismatch với scalp timeframe)
        rr = rr_ratio
        entry = current_price
        mult = 1.2 if regime == "trending_volatile" else (1.0 if regime in ("trending_up", "trending_down") else 0.8)
        if direction == "LONG":
            sl = entry - mult * atr_value
        else:
            sl = entry + mult * atr_value
        tp = entry + rr * (entry - sl) if direction == "LONG" else entry - rr * (sl - entry)
    else:
        mult = 1.5 if regime in ("trending_up", "trending_down") else 1.2
        rr = rr_ratio
        entry = current_price
        if direction == "LONG":
            sl = entry - mult * atr_value
            tp = entry + rr * (entry - sl)
        else:
            sl = entry + mult * atr_value
            tp = entry - rr * (sl - entry)
    return entry, sl, tp


# ─── Session detection ────────────────────────────────────────────────────────

def get_session(ts: datetime) -> str:
    hour = ts.hour
    if 8 <= hour < 13:
        return "london"
    elif 13 <= hour < 20:
        return "ny_overlap"
    elif 0 <= hour < 8:
        return "asia"
    else:
        return "dead_zone"


# ─── Trade simulation (simulate outcome từ future candles) ───────────────────

def simulate_trade(
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    future_df: pd.DataFrame,
    use_trail_stop: bool = True,
    max_hold_candles: int = MAX_HOLD_CANDLES_SCALP,
    breakeven_candles: int = 0,
    use_partial_close: bool = False,
    partial_close_tp_mult: float = 1.5,
) -> tuple[str, float, float, int, str]:
    """
    Simulate trade outcome bằng cách replay từng candle tương lai.

    Partial close logic (use_partial_close=True):
      Khi unrealized >= 50% of target (1:1 RR với RR=2):
        - Chốt 50% position tại close candle đó
        - Move SL về entry (breakeven cho 50% còn lại)
        - TP cho 50% còn lại tighten về partial_close_tp_mult × risk (default 1.5×)
      Final PnL = 0.5 * partial_pnl + 0.5 * remainder_pnl

    Returns: (outcome, exit_price, pnl_pct, hold_candles, sl_trailing_state)
    outcome: TP | SL | TIME_EXIT
    """
    current_sl = sl
    sl_state = "original"
    partial_closed = False
    partial_price = 0.0
    current_tp = tp  # TP có thể thay đổi sau partial close

    if direction == "LONG":
        risk = entry - sl
        target = tp - entry
    else:
        risk = sl - entry
        target = entry - tp

    if risk <= 0:
        return "INVALID", entry, 0.0, 0, sl_state

    target_pct = target / entry * 100

    def _blended_pnl(remainder_price: float, remainder_outcome: str) -> float:
        """50% tại partial_price + 50% tại remainder_price."""
        p1 = _calc_pnl(direction, entry, partial_price, "TP")
        p2 = _calc_pnl(direction, entry, remainder_price, remainder_outcome)
        return 0.5 * p1 + 0.5 * p2

    for i, (idx, candle) in enumerate(future_df.iterrows()):
        # Time exit: chỉ áp dụng khi CHƯA partial close
        # Sau partial close (SL = entry, house money) → không còn risk → để chạy tự nhiên đến TP
        if i >= max_hold_candles and not partial_closed:
            exit_price = float(candle["close"])
            pnl_pct = _calc_pnl(direction, entry, exit_price, "TIME_EXIT")
            return "TIME_EXIT", exit_price, pnl_pct, i, sl_state

        # Break-even move: sau N candles move SL lên entry
        if breakeven_candles > 0 and i >= breakeven_candles:
            if direction == "LONG" and current_sl < entry:
                current_sl = entry
            elif direction == "SHORT" and current_sl > entry:
                current_sl = entry

        low = float(candle["low"])
        high = float(candle["high"])

        # Check SL hit trước (conservative: giả sử xấu nhất trong candle)
        sl_hit = (direction == "LONG" and low <= current_sl) or \
                 (direction == "SHORT" and high >= current_sl)
        tp_hit = (direction == "LONG" and high >= current_tp) or \
                 (direction == "SHORT" and low <= current_tp)

        # Trail stop + partial close logic (mirrors production)
        if use_trail_stop and sl_state != "locked_50":
            current_price_candle = float(candle["close"])
            if direction == "LONG":
                unrealized_pct = (current_price_candle - entry) / entry * 100
            else:
                unrealized_pct = (entry - current_price_candle) / entry * 100

            if unrealized_pct >= target_pct * 0.8 and sl_state != "locked_50":
                new_sl = entry + (current_price_candle - entry) * 0.5 if direction == "LONG" \
                    else entry - (entry - current_price_candle) * 0.5
                if (direction == "LONG" and new_sl > current_sl) or \
                   (direction == "SHORT" and new_sl < current_sl):
                    current_sl = new_sl
                    sl_state = "locked_50"
            elif unrealized_pct >= target_pct * 0.5 and sl_state == "original":
                new_sl = entry * 1.001 if direction == "LONG" else entry * 0.999
                if (direction == "LONG" and new_sl > current_sl) or \
                   (direction == "SHORT" and new_sl < current_sl):
                    current_sl = new_sl
                    sl_state = "breakeven"
                # Partial close: chốt 50% tại close candle này, SL về entry
                # Tighten TP cho 50% còn lại → partial_close_tp_mult × risk
                if use_partial_close and not partial_closed:
                    partial_closed = True
                    partial_price = current_price_candle
                    current_tp = (entry + risk * partial_close_tp_mult) if direction == "LONG" \
                        else (entry - risk * partial_close_tp_mult)

        if sl_hit and tp_hit:
            # Ambiguous — assume SL hit (conservative)
            exit_price = current_sl
            if partial_closed:
                pnl_pct = _blended_pnl(exit_price, "SL")
            else:
                pnl_pct = _calc_pnl(direction, entry, exit_price, "SL")
            return "SL", exit_price, pnl_pct, i + 1, sl_state

        if sl_hit:
            exit_price = current_sl
            if partial_closed:
                pnl_pct = _blended_pnl(exit_price, "SL")
            else:
                pnl_pct = _calc_pnl(direction, entry, exit_price, "SL")
            return "SL", exit_price, pnl_pct, i + 1, sl_state

        if tp_hit:
            if partial_closed:
                pnl_pct = _blended_pnl(current_tp, "TP")
            else:
                pnl_pct = _calc_pnl(direction, entry, current_tp, "TP")
            return "TP", current_tp, pnl_pct, i + 1, sl_state

    # Hết future data — exit tại close cuối
    last_close = float(future_df["close"].iloc[-1]) if len(future_df) > 0 else entry
    if partial_closed:
        pnl_pct = _blended_pnl(last_close, "TIME_EXIT")
    else:
        pnl_pct = _calc_pnl(direction, entry, last_close, "TIME_EXIT")
    return "TIME_EXIT", last_close, pnl_pct, len(future_df), sl_state


def _calc_pnl(direction: str, entry: float, exit_price: float,
              exit_type: str = "") -> float:
    """PnL % sau fee + slippage cả 2 chiều.

    Slippage model:
    - Entry: 0.03% (limit order inside OB zone)
    - SL exit: 0.15% (market order hitting bid/ask)
    - TP exit: 0.05% (limit order)
    - TIME_EXIT: 0.10% (market order, moderate)
    """
    # Apply entry slippage (worse entry price)
    entry_slip = entry * (1 + SLIPPAGE_PCT * (1 if direction == "LONG" else -1))

    # Apply exit slippage based on exit type
    if exit_type == "SL":
        slip = SLIPPAGE_SL_PCT
    elif exit_type == "TP":
        slip = SLIPPAGE_TP_PCT
    else:  # TIME_EXIT or unknown
        slip = (SLIPPAGE_SL_PCT + SLIPPAGE_TP_PCT) / 2  # 0.10%

    exit_slip = exit_price * (1 - slip * (1 if direction == "LONG" else -1))

    raw = (exit_slip - entry_slip) / entry_slip * 100 if direction == "LONG" \
        else (entry_slip - exit_slip) / entry_slip * 100
    fee = FEE_PCT * 2 * 100  # 0.2% round trip
    return raw - fee


# ─── Funding rate lookup helper ───────────────────────────────────────────────

def get_funding_at(funding_df: pd.DataFrame, ts: datetime) -> float:
    """Lấy funding rate gần nhất tại thời điểm ts."""
    if funding_df.empty:
        return 0.0001  # Default neutral
    mask = funding_df.index <= ts
    if not mask.any():
        return float(funding_df["fundingRate"].iloc[0])
    return float(funding_df["fundingRate"][mask].iloc[-1])


# ─── Core backtest loop ───────────────────────────────────────────────────────

def run_backtest_for_symbol(
    symbol: str,
    data: dict[str, pd.DataFrame],
    config: BacktestConfig,
    date_from: datetime,
    date_to: datetime,
    verbose: bool = False,
) -> list[TradeResult]:
    """
    Chạy backtest cho 1 symbol trong khoảng [date_from, date_to].
    Returns list of TradeResult.
    """
    style = config.style

    if style == "scalp":
        step_tf = "15m"       # Scan mỗi nến 15m
        df_fast_key = "1h"    # RSI 1h — match production ResearchAgent
        df_slow_key = "4h"    # RSI 4h — match production ResearchAgent
        df_atr_key = "1h"     # ATR(1h) đủ lớn để cover fee; 5m quá nhỏ
        df_adx_key = "1h"
        df_trend_key = "4h"
        max_hold = MAX_HOLD_CANDLES_SCALP  # 9 × 5m = 45 phút — match production
        future_tf = "5m"      # Simulate outcome trên 5m candles
    else:
        step_tf = "1h"
        df_fast_key = "1h"
        df_slow_key = "4h"
        df_atr_key = "1h"
        df_adx_key = "4h"
        df_trend_key = "1d"
        max_hold = MAX_HOLD_CANDLES_SWING
        future_tf = "1h"

    df_step = data.get(step_tf, pd.DataFrame())
    df_future = data.get(future_tf, pd.DataFrame())
    funding_df = data.get("funding", pd.DataFrame())

    if df_step.empty:
        print(f"  [WARN] No {step_tf} data for {symbol}")
        return []

    # Filter to backtest window
    df_step_bt = df_step[(df_step.index >= date_from) & (df_step.index <= date_to)]
    if df_step_bt.empty:
        return []

    trades = []
    open_positions = []  # Track open positions for max_positions check

    # Filter funnel diagnostic
    funnel = {
        "total": 0, "session": 0, "rule": 0, "cvd": 0, "vwap": 0,
        "ema9": 0, "regime": 0, "chop": 0, "smc": 0, "correlation": 0,
        "confluence": 0, "no_future": 0, "calc_sl_tp": 0, "traded": 0,
    }

    WARMUP_CANDLES = {
        "5m": 200, "15m": 200, "1h": 200, "4h": 100, "1d": 400
    }

    _smc_analyzer = None
    if config.use_smc_filter and style == "scalp":
        from utils.smc import SMCAnalyzer
        _smc_analyzer = SMCAnalyzer(None)

    total_steps = len(df_step_bt)
    report_every = max(1, total_steps // 20)

    for step_i, (step_ts, _) in enumerate(df_step_bt.iterrows()):
        if verbose and step_i % report_every == 0:
            pct = step_i / total_steps * 100
            print(f"\r  {symbol} [{style}] {pct:.0f}% ({step_ts.strftime('%Y-%m-%d')})", end="", flush=True)

        # ── Build rolling windows cho từng timeframe ──────────────────────
        def get_window(tf_key: str, n: int) -> pd.DataFrame:
            df = data.get(tf_key, pd.DataFrame())
            if df.empty:
                return pd.DataFrame()
            mask = df.index <= step_ts
            available = df[mask]
            return available.iloc[-n:] if len(available) >= 50 else pd.DataFrame()

        warmup = WARMUP_CANDLES
        df_fast  = get_window(df_fast_key, warmup[df_fast_key])
        df_slow  = get_window(df_slow_key, warmup[df_slow_key])
        df_trend = get_window(df_trend_key, warmup[df_trend_key])
        df_atr   = get_window(df_atr_key, warmup[df_atr_key])
        df_adx   = get_window(df_adx_key, warmup[df_adx_key])

        if df_fast.empty or df_slow.empty or df_trend.empty:
            continue

        # ── Close expired positions ────────────────────────────────────────
        open_positions = [p for p in open_positions if p.exit_time is None or p.exit_time > step_ts]

        # ── Compute indicators ─────────────────────────────────────────────
        ind = compute_indicators(df_fast, df_slow, df_trend, df_atr, df_adx, style, step_ts=step_ts)
        if ind is None:
            continue

        current_price = ind["current_price"]
        if current_price <= 0:
            continue

        # ── Session filter ─────────────────────────────────────────────────
        session = get_session(step_ts)
        funnel["total"] += 1
        if config.use_session_filter and style == "scalp":
            if session not in ("london", "ny_overlap"):
                funnel["session"] += 1
                continue

        # ── Funding rate ───────────────────────────────────────────────────
        funding_rate = get_funding_at(funding_df, step_ts)
        oi_change_pct = 0.0  # Không có historical OI change theo candle

        # ── Rule-based filter ──────────────────────────────────────────────
        if config.use_rule_filter:
            direction = rule_based_filter(ind, funding_rate, config)
            if direction is None:
                funnel["rule"] += 1
                continue
        else:
            # Không filter: chỉ dùng net_score để determine direction
            direction = "LONG" if ind["net_score"] > 0 else ("SHORT" if ind["net_score"] < 0 else None)
            if direction is None:
                funnel["rule"] += 1
                continue

        # ── CVD divergence check ───────────────────────────────────────────
        if config.use_cvd_proxy and style == "scalp":
            if direction == "LONG" and ind["cvd_ratio"] < 0.45:
                funnel["cvd"] += 1
                continue
            if direction == "SHORT" and ind["cvd_ratio"] > 0.55:
                funnel["cvd"] += 1
                continue

        # ── VWAP bias filter ───────────────────────────────────────────────
        if config.use_vwap_filter and style == "scalp":
            vd = ind["vwap_distance_pct"]
            if direction == "LONG" and vd > 1.5:
                funnel["vwap"] += 1
                continue
            if direction == "SHORT" and vd < -1.5:
                funnel["vwap"] += 1
                continue

        # ── EMA9 timing filter ─────────────────────────────────────────────
        if config.use_ema9_filter and style == "scalp":
            timing_ok = (direction == "LONG" and ind["ema9_crossed_recent_up"]) or \
                        (direction == "SHORT" and ind["ema9_crossed_recent_down"])
            if not timing_ok:
                funnel["ema9"] += 1
                continue

        # ── Regime check ───────────────────────────────────────────────────
        regime = classify_regime(
            ind["adx"], ind["plus_di"], ind["minus_di"],
            ind["bb_width_regime"], ind["atr_ratio_regime"]
        )
        if config.use_regime_filter and style == "scalp" and regime == "volatile":
            funnel["regime"] += 1
            continue

        # ── Chop Index filter (scalp: > 61.8 = choppy) ─────────────────────
        if config.use_chop_filter and style == "scalp" and ind["chop_index"] > CHOP_SKIP_THRESHOLD:
            funnel["chop"] += 1
            continue

        # ── SMC analysis (mirrors production, scalp only) ──────────────────
        smc_signal = None
        if _smc_analyzer is not None:
            df_structure = get_window("15m", 100)
            df_timing = get_window("5m", 50)
            if len(df_structure) >= 30 and len(df_timing) >= 10:
                smc_signal = _smc_analyzer.analyze_from_dataframes(
                    df_structure, df_timing, current_price
                )
                # SMC opposing hard-reject
                if smc_signal.smc_valid:
                    smc_opposing = (
                        (direction == "LONG" and smc_signal.smc_score <= -50)
                        or (direction == "SHORT" and smc_signal.smc_score >= 50)
                    )
                    if smc_opposing:
                        funnel["smc"] += 1
                        continue

        # ── Correlation filter: tối đa 2 vị thế cùng hướng ──────────────────
        if config.use_correlation_filter:
            open_now = [p for p in open_positions if p.exit_time is None or p.exit_time > step_ts]
            same_dir = sum(1 for p in open_now if p.direction == direction)
            if same_dir >= MAX_SAME_DIRECTION:
                funnel["correlation"] += 1
                continue

        # ── Dynamic confluence: win rate < 45% → thắt chặt ──────────────────
        effective_confluence = config.confluence_threshold
        if config.use_dynamic_confluence and len(trades) >= 5:
            recent = trades[-20:]
            wins = sum(1 for t in recent if t.pnl_usdt > 0)
            wr = wins / len(recent)
            if wr < 0.45:
                effective_confluence = 4

        # ── Confluence check ───────────────────────────────────────────────
        confluence_score = calc_confluence(ind, direction, funding_rate, oi_change_pct)
        # SMC confluence (mirrors production: _smc_has_precision + score >= 50)
        if config.use_smc_filter and style == "scalp" and smc_signal is not None:
            _smc_has_precision = (
                smc_signal.price_in_ob
                or smc_signal.price_in_fvg
                or smc_signal.sweep_direction != "none"
            )
            if smc_signal.smc_valid and _smc_has_precision:
                if direction == "LONG" and smc_signal.smc_score >= 50:
                    confluence_score += 2
                elif direction == "SHORT" and smc_signal.smc_score <= -50:
                    confluence_score += 2
        if config.use_confluence_filter and confluence_score < effective_confluence:
            funnel["confluence"] += 1
            continue

        # ── Duplicate pair check ───────────────────────────────────────────
        already_open = any(p.symbol == symbol for p in open_positions if p.exit_time is None)
        if already_open:
            continue

        # ── Max positions check ────────────────────────────────────────────
        open_count = sum(1 for p in open_positions if p.exit_time is None)
        if open_count >= MAX_OPEN_POSITIONS:
            continue

        # ── Entry/SL/TP ────────────────────────────────────────────────────
        rr = config.scalp_rr if style == "scalp" else config.swing_rr
        sl_args = {"swing_low": ind["swing_low"], "swing_high": ind["swing_high"]} if config.use_sl_structure else {}
        result = calc_entry_sl_tp(
            direction, current_price, ind["atr_value"], regime, style, rr, **sl_args
        )
        if result is None:
            funnel["calc_sl_tp"] += 1
            continue
        entry, sl, tp = result

        # ── SMC OB entry override (mirrors production 5b) ─────────────────────
        if config.use_smc_filter and style == "scalp" and smc_signal is not None:
            if smc_signal.price_in_ob and smc_signal.smc_valid:
                atr_val = ind["atr_value"]
                if direction == "LONG" and smc_signal.nearest_bullish_ob:
                    ob = smc_signal.nearest_bullish_ob
                    ob_entry = current_price - 0.1 * atr_val
                    ob_sl = ob.price_low - 0.1 * atr_val
                    if ob_sl < ob_entry and (ob_entry - ob_sl) <= 2.0 * atr_val:
                        ob_tp = ob_entry + rr * (ob_entry - ob_sl)
                        entry, sl, tp = ob_entry, ob_sl, ob_tp
                elif direction == "SHORT" and smc_signal.nearest_bearish_ob:
                    ob = smc_signal.nearest_bearish_ob
                    ob_entry = current_price + 0.1 * atr_val
                    ob_sl = ob.price_high + 0.1 * atr_val
                    if ob_sl > ob_entry and (ob_sl - ob_entry) <= 2.0 * atr_val:
                        ob_tp = ob_entry - rr * (ob_sl - ob_entry)
                        entry, sl, tp = ob_entry, ob_sl, ob_tp

        # ── Simulate outcome ───────────────────────────────────────────────
        future_mask = df_future.index > step_ts
        future_candles = df_future[future_mask].iloc[:max_hold * 2]  # Buffer

        if len(future_candles) < 3:
            funnel["no_future"] += 1
            continue

        funnel["traded"] += 1

        outcome, exit_price, pnl_pct, hold_candles, sl_state = simulate_trade(
            direction, entry, sl, tp, future_candles,
            use_trail_stop=config.use_trail_stop,
            max_hold_candles=max_hold,
            use_partial_close=config.use_partial_close,
        )

        # Approximate exit time
        if hold_candles < len(future_candles):
            exit_ts = future_candles.index[hold_candles - 1]
        else:
            exit_ts = future_candles.index[-1]

        pnl_usdt = INITIAL_BALANCE * MAX_POSITION_PCT * pnl_pct / 100

        trade = TradeResult(
            symbol=symbol,
            direction=direction,
            entry_time=step_ts,
            exit_time=exit_ts,
            entry_price=entry,
            sl=sl,
            tp=tp,
            exit_price=exit_price,
            outcome=outcome,
            pnl_pct=pnl_pct,
            pnl_usdt=pnl_usdt,
            confluence_score=confluence_score,
            regime=regime,
            session=session,
            hold_candles=hold_candles,
            sl_trailing_state=sl_state,
        )
        trades.append(trade)
        open_positions.append(trade)

    if verbose:
        print()

    # In filter funnel nếu verbose
    total = funnel["total"]
    if verbose and total > 0:
        print(f"\n  Filter Funnel [{symbol}] ({total} candles scanned):")
        for label, key in [
            ("session skip (asia/dead)", "session"),
            ("rule filter",     "rule"),
            ("CVD proxy",       "cvd"),
            ("VWAP bias",       "vwap"),
            ("EMA9 timing",     "ema9"),
            ("volatile regime", "regime"),
            ("chop > 61.8",     "chop"),
            ("SMC opposing",     "smc"),
            ("correlation",     "correlation"),
            ("confluence < N",  "confluence"),
            ("calc SL/TP fail", "calc_sl_tp"),
            ("no future data",  "no_future"),
        ]:
            count = funnel[key]
            if count > 0:
                print(f"    -> {label:<22}: {count:5d} ({count/total*100:.1f}%)")
        print(f"    OK Traded              : {funnel['traded']:5d} ({funnel['traded']/total*100:.2f}%)")

    return trades


# ─── SMC Standalone backtest ─────────────────────────────────────────────────

def run_smc_backtest_for_symbol(
    symbol: str,
    data: dict[str, pd.DataFrame],
    config: BacktestConfig,
    date_from: datetime,
    date_to: datetime,
    verbose: bool = False,
) -> list[TradeResult]:
    """
    Backtest SMC standalone — không dùng rule-based, CVD, VWAP, EMA9, regime, chop.
    Chỉ SMCStrategy.analyze_from_dataframes() -> setup -> simulate_trade.
    """
    style = config.style
    if style == "scalp":
        step_tf = "15m"
        df_htf_key, df_htf_timing_key = "1h", "15m"
        df_ltf_key, df_ltf_timing_key = "15m", "5m"
        df_daily_key = "1d"
        max_hold = MAX_HOLD_CANDLES_SCALP  # 9 × 5m = 45 phút — match production
        future_tf = "5m"
    else:
        step_tf = "1h"
        df_htf_key, df_htf_timing_key = "4h", "1h"
        df_ltf_key, df_ltf_timing_key = "1h", "15m"
        df_daily_key = "1d"
        max_hold = MAX_HOLD_CANDLES_SWING
        future_tf = "1h"

    df_step = data.get(step_tf, pd.DataFrame())
    df_future = data.get(future_tf, pd.DataFrame())

    if df_step.empty:
        print(f"  [WARN] No {step_tf} data for {symbol}")
        return []

    df_step_bt = df_step[(df_step.index >= date_from) & (df_step.index <= date_to)]
    if df_step_bt.empty:
        return []

    from utils.smc_strategy import SMCStrategy
    smc_strategy = SMCStrategy(
        min_rr_tp1=config.smc_min_rr_tp1,
        min_confidence=config.smc_min_confidence,
        sl_buffer_pct=config.smc_sl_buffer_pct,
    )

    WARMUP = {"5m": 60, "15m": 160, "1h": 160, "4h": 160, "1d": 15}
    trades = []
    open_positions = []
    total_steps = len(df_step_bt)
    report_every = max(1, total_steps // 20)

    for step_i, (step_ts, _) in enumerate(df_step_bt.iterrows()):
        if verbose and step_i % report_every == 0:
            pct = step_i / total_steps * 100
            print(f"\r  {symbol} [SMC {style}] {pct:.0f}% ({step_ts.strftime('%Y-%m-%d')})", end="", flush=True)

        def get_window(tf_key: str, n: int) -> pd.DataFrame:
            df = data.get(tf_key, pd.DataFrame())
            if df.empty:
                return pd.DataFrame()
            mask = df.index <= step_ts
            available = df[mask]
            # Min 30 rows (SMCAnalyzer requirement) — tránh bỏ sót data đầu backtest
            return available.iloc[-n:] if len(available) >= 30 else pd.DataFrame()

        df_htf = get_window(df_htf_key, WARMUP.get(df_htf_key, 150))
        df_htf_timing = get_window(df_htf_timing_key, WARMUP.get(df_htf_timing_key, 150))
        df_ltf = get_window(df_ltf_key, WARMUP.get(df_ltf_key, 150))
        df_ltf_timing = get_window(df_ltf_timing_key, WARMUP.get(df_ltf_timing_key, 60))
        df_d = get_window(df_daily_key, WARMUP.get(df_daily_key, 15)) if df_daily_key in data else None

        if len(df_htf) < 30 or len(df_ltf) < 30 or df_ltf_timing.empty:
            continue

        current_price = float(df_ltf_timing["close"].iloc[-1])
        if current_price <= 0:
            continue

        session = get_session(step_ts)
        if config.use_session_filter:
            if style == "scalp" and session not in ("london", "ny_overlap"):
                continue
            if style == "swing" and session == "asia":
                continue

        setup = smc_strategy.analyze_from_dataframes(
            symbol=symbol,
            df_htf_structure=df_htf,
            df_htf_timing=df_htf_timing,
            df_ltf_structure=df_ltf,
            df_ltf_timing=df_ltf_timing,
            current_price=current_price,
            df_daily=df_d if df_d is not None and not df_d.empty else None,
        )

        if setup is None or not setup.valid:
            continue
        if config.smc_ob_entry_only and setup.entry_model != "ob_entry":
            continue
        if config.smc_disable_ce_entry and setup.entry_model == "ce_entry":
            continue
        if config.smc_min_grade and setup.entry_model_quality not in ("A+", "A"):
            continue
        if config.smc_displacement_only and setup.ltf_trigger != "displacement":
            continue

        direction = setup.direction
        entry, sl, tp = setup.entry, setup.sl, setup.tp1

        # SMC standalone extra filters (nhẹ)
        if config.smc_use_chop_filter and style == "scalp" and len(df_ltf) >= 14:
            chop_series = ta.chop(df_ltf["high"], df_ltf["low"], df_ltf["close"], length=14, atr_length=1)
            if chop_series is not None and not chop_series.empty and not pd.isna(chop_series.iloc[-1]):
                if float(chop_series.iloc[-1]) > config.smc_chop_threshold:
                    continue
        if config.smc_use_adx_filter and len(df_htf) >= 14:
            adx_df = ta.adx(df_htf["high"], df_htf["low"], df_htf["close"], length=14)
            if adx_df is not None and not adx_df.empty:
                adx_col = "ADX_14" if "ADX_14" in adx_df.columns else adx_df.columns[0]
                adx_val = float(adx_df[adx_col].iloc[-1])
                if not pd.isna(adx_val) and adx_val < config.smc_adx_min:
                    continue
        if config.smc_use_funding_filter:
            funding_df = data.get("funding", pd.DataFrame())
            funding_rate = get_funding_at(funding_df, step_ts)
            if direction == "LONG" and funding_rate > 0.0005:
                continue
            if direction == "SHORT" and funding_rate < -0.0005:
                continue

        open_positions = [p for p in open_positions if p.exit_time is None or p.exit_time > step_ts]
        if sum(1 for p in open_positions if p.symbol == symbol) > 0:
            continue
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            continue

        future_mask = df_future.index > step_ts
        future_candles = df_future[future_mask].iloc[: max_hold * 2]
        if len(future_candles) < 3:
            continue

        outcome, exit_price, pnl_pct, hold_candles, sl_state = simulate_trade(
            direction, entry, sl, tp, future_candles,
            use_trail_stop=config.use_trail_stop,
            max_hold_candles=max_hold,
            breakeven_candles=config.smc_breakeven_candles,
            use_partial_close=config.use_partial_close,
        )

        exit_ts = future_candles.index[hold_candles - 1] if hold_candles < len(future_candles) else future_candles.index[-1]
        pnl_usdt = INITIAL_BALANCE * MAX_POSITION_PCT * pnl_pct / 100

        trade = TradeResult(
            symbol=symbol,
            direction=direction,
            entry_time=step_ts,
            exit_time=exit_ts,
            entry_price=entry,
            sl=sl,
            tp=tp,
            exit_price=exit_price,
            outcome=outcome,
            pnl_pct=pnl_pct,
            pnl_usdt=pnl_usdt,
            confluence_score=0,
            regime="smc",
            session=session,
            hold_candles=hold_candles,
            sl_trailing_state=sl_state,
            entry_model=setup.entry_model,
            entry_model_quality=setup.entry_model_quality,
            smc_htf_bias=setup.htf_bias,
            smc_ltf_trigger=setup.ltf_trigger,
            smc_confidence=setup.confidence,
        )
        trades.append(trade)
        open_positions.append(trade)

    if verbose:
        print()
    return trades


# ─── Statistics calculation ───────────────────────────────────────────────────

def calc_stats(trades: list[TradeResult], config: BacktestConfig, date_from: datetime, date_to: datetime) -> BacktestResult:
    result = BacktestResult(config=config, trades=trades)
    if not trades:
        return result

    wins = [t for t in trades if t.pnl_usdt > 0]
    losses = [t for t in trades if t.pnl_usdt <= 0]
    result.win_rate = len(wins) / len(trades) * 100

    avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t.pnl_pct for t in losses) / len(losses)) if losses else 1
    result.avg_rr = avg_win / avg_loss if avg_loss > 0 else 0

    gross_profit = sum(t.pnl_usdt for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_usdt for t in losses)) if losses else 1
    result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    # Max drawdown (running equity curve)
    equity = INITIAL_BALANCE
    peak = equity
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_time):
        equity += t.pnl_usdt
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown_pct = max_dd
    result.total_pnl_pct = (equity - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    # Sharpe ratio (daily PnL)
    daily_pnl: dict[str, float] = {}
    for t in trades:
        day = t.entry_time.strftime("%Y-%m-%d")
        daily_pnl[day] = daily_pnl.get(day, 0) + t.pnl_usdt
    pnl_series = list(daily_pnl.values())
    if len(pnl_series) >= 2:
        import statistics
        mean_daily = sum(pnl_series) / len(pnl_series)
        std_daily = statistics.stdev(pnl_series)
        result.sharpe_ratio = (mean_daily / std_daily * math.sqrt(252)) if std_daily > 0 else 0

    total_days = max(1, (date_to - date_from).days)
    result.trades_per_day = len(trades) / total_days
    result.avg_hold_candles = sum(t.hold_candles for t in trades) / len(trades)

    return result


# ─── Combined multi-symbol backtest (correlation filter cần shared open_positions) ─

def run_backtest_combined(
    symbols: list[str],
    all_data: dict[str, dict[str, pd.DataFrame]],
    config: BacktestConfig,
    date_from: datetime,
    date_to: datetime,
    verbose: bool = False,
) -> list[TradeResult]:
    """
    Chạy backtest cho nhiều symbol trong 1 loop — shared open_positions để correlation filter hoạt động.
    """
    if len(symbols) <= 1:
        if symbols:
            return run_backtest_for_symbol(symbols[0], all_data[symbols[0]], config, date_from, date_to, verbose)
        return []

    style = config.style
    if style == "scalp":
        step_tf, df_fast_key, df_slow_key, df_atr_key, df_adx_key, df_trend_key = "15m", "1h", "4h", "1h", "1h", "4h"
        max_hold, future_tf = MAX_HOLD_CANDLES_SCALP, "5m"  # 9×5m=45min — match production
    else:
        step_tf, df_fast_key, df_slow_key, df_atr_key, df_adx_key, df_trend_key = "1h", "1h", "4h", "1h", "4h", "1d"
        max_hold, future_tf = MAX_HOLD_CANDLES_SWING, "1h"

    # Master timeline: dùng symbol đầu tiên
    df_step = all_data[symbols[0]].get(step_tf, pd.DataFrame())
    if df_step.empty:
        return []
    df_step_bt = df_step[(df_step.index >= date_from) & (df_step.index <= date_to)]
    if df_step_bt.empty:
        return []

    WARMUP_CANDLES = {"5m": 200, "15m": 200, "1h": 200, "4h": 100, "1d": 400}
    _smc_analyzer = None
    if config.use_smc_filter and style == "scalp":
        from utils.smc import SMCAnalyzer
        _smc_analyzer = SMCAnalyzer(None)
    trades = []
    open_positions = []
    total_steps = len(df_step_bt)
    report_every = max(1, total_steps // 20)

    for step_i, (step_ts, _) in enumerate(df_step_bt.iterrows()):
        if verbose and step_i % report_every == 0:
            pct = step_i / total_steps * 100
            print(f"\r  Combined [{style}] {pct:.0f}% ({step_ts.strftime('%Y-%m-%d')})", end="", flush=True)

        open_positions = [p for p in open_positions if p.exit_time is None or p.exit_time > step_ts]

        for symbol in symbols:
            data = all_data.get(symbol, {})
            df_future = data.get(future_tf, pd.DataFrame())
            funding_df = data.get("funding", pd.DataFrame())

            def get_window(tf_key: str, n: int) -> pd.DataFrame:
                df = data.get(tf_key, pd.DataFrame())
                if df.empty:
                    return pd.DataFrame()
                mask = df.index <= step_ts
                available = df[mask]
                return available.iloc[-n:] if len(available) >= 50 else pd.DataFrame()

            warmup = WARMUP_CANDLES
            df_fast = get_window(df_fast_key, warmup[df_fast_key])
            df_slow = get_window(df_slow_key, warmup[df_slow_key])
            df_trend = get_window(df_trend_key, warmup[df_trend_key])
            df_atr = get_window(df_atr_key, warmup[df_atr_key])
            df_adx = get_window(df_adx_key, warmup[df_adx_key])

            if df_fast.empty or df_slow.empty or df_trend.empty:
                continue

            ind = compute_indicators(df_fast, df_slow, df_trend, df_atr, df_adx, style, step_ts=step_ts)
            if ind is None or ind["current_price"] <= 0:
                continue

            session = get_session(step_ts)
            if config.use_session_filter and style == "scalp" and session not in ("london", "ny_overlap"):
                continue

            funding_rate = get_funding_at(funding_df, step_ts)
            oi_change_pct = 0.0

            if config.use_rule_filter:
                direction = rule_based_filter(ind, funding_rate, config)
            else:
                direction = "LONG" if ind["net_score"] > 0 else ("SHORT" if ind["net_score"] < 0 else None)
            if direction is None:
                continue

            if config.use_cvd_proxy and style == "scalp":
                if (direction == "LONG" and ind["cvd_ratio"] < 0.45) or (direction == "SHORT" and ind["cvd_ratio"] > 0.55):
                    continue

            if config.use_vwap_filter and style == "scalp":
                vd = ind["vwap_distance_pct"]
                if (direction == "LONG" and vd > 1.5) or (direction == "SHORT" and vd < -1.5):
                    continue

            if config.use_ema9_filter and style == "scalp":
                timing_ok = (direction == "LONG" and ind["ema9_crossed_recent_up"]) or (direction == "SHORT" and ind["ema9_crossed_recent_down"])
                if not timing_ok:
                    continue

            regime = classify_regime(ind["adx"], ind["plus_di"], ind["minus_di"], ind["bb_width_regime"], ind["atr_ratio_regime"])
            if config.use_regime_filter and style == "scalp" and regime == "volatile":
                continue

            if config.use_chop_filter and style == "scalp" and ind["chop_index"] > CHOP_SKIP_THRESHOLD:
                continue

            smc_signal = None
            if _smc_analyzer is not None:
                df_structure = get_window("15m", 100)
                df_timing = get_window("5m", 50)
                if len(df_structure) >= 30 and len(df_timing) >= 10:
                    smc_signal = _smc_analyzer.analyze_from_dataframes(
                        df_structure, df_timing, ind["current_price"]
                    )
                    if smc_signal.smc_valid:
                        smc_opposing = (
                            (direction == "LONG" and smc_signal.smc_score <= -50)
                            or (direction == "SHORT" and smc_signal.smc_score >= 50)
                        )
                        if smc_opposing:
                            continue

            if config.use_correlation_filter:
                open_now = [p for p in open_positions if p.exit_time is None or p.exit_time > step_ts]
                same_dir = sum(1 for p in open_now if p.direction == direction)
                if same_dir >= MAX_SAME_DIRECTION:
                    continue

            effective_confluence = config.confluence_threshold
            if config.use_dynamic_confluence and len(trades) >= 5:
                recent = trades[-20:]
                wr = sum(1 for t in recent if t.pnl_usdt > 0) / len(recent)
                if wr < 0.45:
                    effective_confluence = 4

            confluence_score = calc_confluence(ind, direction, funding_rate, oi_change_pct)
            if config.use_smc_filter and style == "scalp" and smc_signal is not None:
                _smc_has_precision = (
                    smc_signal.price_in_ob
                    or smc_signal.price_in_fvg
                    or smc_signal.sweep_direction != "none"
                )
                if smc_signal.smc_valid and _smc_has_precision:
                    if direction == "LONG" and smc_signal.smc_score >= 50:
                        confluence_score += 2
                    elif direction == "SHORT" and smc_signal.smc_score <= -50:
                        confluence_score += 2
            if config.use_confluence_filter and confluence_score < effective_confluence:
                continue

            if any(p.symbol == symbol for p in open_positions if p.exit_time is None or p.exit_time > step_ts):
                continue

            if sum(1 for p in open_positions if p.exit_time is None or p.exit_time > step_ts) >= MAX_OPEN_POSITIONS:
                continue

            rr = config.scalp_rr if style == "scalp" else config.swing_rr
            sl_args = {"swing_low": ind["swing_low"], "swing_high": ind["swing_high"]} if config.use_sl_structure else {}
            result = calc_entry_sl_tp(direction, ind["current_price"], ind["atr_value"], regime, style, rr, **sl_args)
            if result is None:
                continue
            entry, sl, tp = result

            if config.use_smc_filter and style == "scalp" and smc_signal is not None:
                if smc_signal.price_in_ob and smc_signal.smc_valid:
                    atr_val = ind["atr_value"]
                    if direction == "LONG" and smc_signal.nearest_bullish_ob:
                        ob = smc_signal.nearest_bullish_ob
                        ob_entry = ind["current_price"] - 0.1 * atr_val
                        ob_sl = ob.price_low - 0.1 * atr_val
                        if ob_sl < ob_entry and (ob_entry - ob_sl) <= 2.0 * atr_val:
                            ob_tp = ob_entry + rr * (ob_entry - ob_sl)
                            entry, sl, tp = ob_entry, ob_sl, ob_tp
                    elif direction == "SHORT" and smc_signal.nearest_bearish_ob:
                        ob = smc_signal.nearest_bearish_ob
                        ob_entry = ind["current_price"] + 0.1 * atr_val
                        ob_sl = ob.price_high + 0.1 * atr_val
                        if ob_sl > ob_entry and (ob_sl - ob_entry) <= 2.0 * atr_val:
                            ob_tp = ob_entry - rr * (ob_sl - ob_entry)
                            entry, sl, tp = ob_entry, ob_sl, ob_tp

            future_mask = df_future.index > step_ts
            future_candles = df_future[future_mask].iloc[: max_hold * 2]
            if len(future_candles) < 3:
                continue

            outcome, exit_price, pnl_pct, hold_candles, sl_state = simulate_trade(
                direction, entry, sl, tp, future_candles,
                use_trail_stop=config.use_trail_stop,
                max_hold_candles=max_hold,
                use_partial_close=config.use_partial_close,
            )

            exit_ts = future_candles.index[hold_candles - 1] if hold_candles < len(future_candles) else future_candles.index[-1]
            pnl_usdt = INITIAL_BALANCE * MAX_POSITION_PCT * pnl_pct / 100

            trade = TradeResult(
                symbol=symbol,
                direction=direction,
                entry_time=step_ts,
                exit_time=exit_ts,
                entry_price=entry,
                sl=sl,
                tp=tp,
                exit_price=exit_price,
                outcome=outcome,
                pnl_pct=pnl_pct,
                pnl_usdt=pnl_usdt,
                confluence_score=confluence_score,
                regime=regime,
                session=session,
                hold_candles=hold_candles,
                sl_trailing_state=sl_state,
            )
            trades.append(trade)
            open_positions.append(trade)

    if verbose:
        print()
    return trades


# ─── Walk-forward analysis ────────────────────────────────────────────────────

def run_walk_forward(
    symbol: str,
    data: dict,
    config: BacktestConfig,
) -> list[dict]:
    """
    Walk-forward: chia data thành windows IS + OOS.
    IS = train (optimize), OOS = test (validate).
    Returns list of window results.
    """
    windows = []
    cursor = config.date_from
    while cursor + timedelta(days=config.wf_train_days + config.wf_test_days) <= config.date_to:
        is_from = cursor
        is_to = cursor + timedelta(days=config.wf_train_days)
        oos_from = is_to
        oos_to = oos_from + timedelta(days=config.wf_test_days)

        is_trades = run_backtest_for_symbol(symbol, data, config, is_from, is_to)
        oos_trades = run_backtest_for_symbol(symbol, data, config, oos_from, oos_to)

        is_stats = calc_stats(is_trades, config, is_from, is_to)
        oos_stats = calc_stats(oos_trades, config, oos_from, oos_to)

        windows.append({
            "window": f"{is_from.strftime('%Y-%m-%d')} / {oos_to.strftime('%Y-%m-%d')}",
            "is_win_rate": is_stats.win_rate,
            "is_trades": len(is_trades),
            "oos_win_rate": oos_stats.win_rate,
            "oos_trades": len(oos_trades),
            "oos_pnl_pct": oos_stats.total_pnl_pct,
            "oos_max_dd": oos_stats.max_drawdown_pct,
        })
        cursor += timedelta(days=config.wf_test_days)  # Roll forward by OOS size

    return windows


# ─── Parameter optimization ───────────────────────────────────────────────────

def run_optimization(
    symbol: str,
    data: dict,
    config: BacktestConfig,
) -> list[dict]:
    """
    Sweep các params chính, báo cáo kết quả.
    Chú ý: chỉ dùng trên IS period — không dùng kết quả này cho OOS trading.
    """
    results = []

    param_sets = [
        {"confluence_threshold": c, "scalp_rr": rr}
        for c in [2, 3, 4, 5]
        for rr in [1.2, 1.5, 2.0]
    ]

    for params in param_sets:
        cfg_copy = BacktestConfig(
            symbols=config.symbols,
            style=config.style,
            date_from=config.date_from,
            date_to=config.date_to,
            confluence_threshold=params["confluence_threshold"],
            scalp_rr=params["scalp_rr"],
            swing_rr=params["swing_rr"] if "swing_rr" in params else config.swing_rr,
        )
        trades = run_backtest_for_symbol(symbol, data, cfg_copy, config.date_from, config.date_to)
        stats = calc_stats(trades, cfg_copy, config.date_from, config.date_to)
        results.append({
            "confluence": params["confluence_threshold"],
            "rr": params["scalp_rr"],
            "trades": len(trades),
            "win_rate": stats.win_rate,
            "profit_factor": stats.profit_factor,
            "max_dd": stats.max_drawdown_pct,
            "total_pnl_pct": stats.total_pnl_pct,
        })
        print(f"  conf={params['confluence_threshold']} rr={params['scalp_rr']}: "
              f"win={stats.win_rate:.1f}% pf={stats.profit_factor:.2f} "
              f"trades={len(trades)} pnl={stats.total_pnl_pct:+.1f}%")

    return sorted(results, key=lambda x: x["profit_factor"], reverse=True)


# ─── Report printing ──────────────────────────────────────────────────────────

def print_report(result: BacktestResult, symbol: str, date_from: datetime, date_to: datetime):
    trades = result.trades
    print(f"\n{'='*65}")
    print(f"  BACKTEST REPORT - {symbol} [{result.config.style.upper()}]")
    print(f"  {date_from.strftime('%Y-%m-%d')} -> {date_to.strftime('%Y-%m-%d')}")
    print(f"{'='*65}")

    if not trades:
        print("  [WARN] Khong co trade nao trong khoang thoi gian nay.")
        print("  -> Filter too tight, try --relax or shorter period")
        return

    print(f"\n[PERFORMANCE SUMMARY]")
    print(f"  Initial balance    : ${INITIAL_BALANCE:,.0f} USDT")
    print(f"  Position size      : {MAX_POSITION_PCT*100:.0f}% = ${INITIAL_BALANCE*MAX_POSITION_PCT:,.0f} per trade")
    print(f"  Total trades       : {len(trades)}")
    print(f"  Win rate           : {result.win_rate:.1f}%")
    print(f"  Avg RR (realized)  : {result.avg_rr:.2f}")
    print(f"  Profit factor      : {result.profit_factor:.2f}")
    pnl_usdt = result.total_pnl_pct / 100 * INITIAL_BALANCE
    print(f"  Total PnL          : {result.total_pnl_pct:+.2f}%  (${pnl_usdt:+,.2f})")
    print(f"  Max drawdown       : {result.max_drawdown_pct:.1f}%")
    print(f"  Sharpe ratio       : {result.sharpe_ratio:.2f}")
    print(f"  Trades/day         : {result.trades_per_day:.1f}")
    print(f"  Avg hold (candles) : {result.avg_hold_candles:.1f}")

    # Breakdown by outcome
    outcomes = {}
    for t in trades:
        outcomes[t.outcome] = outcomes.get(t.outcome, 0) + 1
    print(f"\n[OUTCOME BREAKDOWN]")
    for outcome, count in sorted(outcomes.items()):
        pct = count / len(trades) * 100
        print(f"  {outcome:<12}: {count:4d} ({pct:.1f}%)")

    # Breakdown by session
    if result.config.style == "scalp":
        print(f"\n[BY SESSION]")
        for sess in ["london", "ny_overlap", "asia", "dead_zone"]:
            sess_trades = [t for t in trades if t.session == sess]
            if not sess_trades:
                continue
            sess_wins = [t for t in sess_trades if t.pnl_usdt > 0]
            print(f"  {sess:<12}: {len(sess_trades):4d} trades, "
                  f"win={len(sess_wins)/len(sess_trades)*100:.1f}%")

    # Breakdown by regime
    print(f"\n[BY REGIME]")
    regimes = {}
    for t in trades:
        if t.regime not in regimes:
            regimes[t.regime] = {"trades": [], "wins": 0}
        regimes[t.regime]["trades"].append(t)
        if t.pnl_usdt > 0:
            regimes[t.regime]["wins"] += 1
    for r, v in sorted(regimes.items()):
        wr = v["wins"] / len(v["trades"]) * 100
        print(f"  {r:<20}: {len(v['trades']):4d} trades, win={wr:.1f}%")

    # Breakdown by direction
    print(f"\n[BY DIRECTION]")
    for direction in ["LONG", "SHORT"]:
        dir_trades = [t for t in trades if t.direction == direction]
        if not dir_trades:
            continue
        dir_wins = [t for t in dir_trades if t.pnl_usdt > 0]
        avg_pnl = sum(t.pnl_pct for t in dir_trades) / len(dir_trades)
        print(f"  {direction:<6}: {len(dir_trades):4d} trades, "
              f"win={len(dir_wins)/len(dir_trades)*100:.1f}%, "
              f"avg_pnl={avg_pnl:+.2f}%")

    # Monthly breakdown
    print(f"\n[MONTHLY PNL]")
    monthly: dict[str, float] = {}
    for t in trades:
        month = t.entry_time.strftime("%Y-%m")
        monthly[month] = monthly.get(month, 0) + t.pnl_usdt
    for month, pnl in sorted(monthly.items()):
        bar_len = int(abs(pnl) / 20)
        bar = "#" * min(bar_len, 30)
        sign = "+" if pnl >= 0 else "-"
        print(f"  {month}: {sign}${abs(pnl):6.0f}  {bar}")

    # SMC Standalone breakdown (khi có entry_model)
    smc_trades = [t for t in trades if getattr(t, "entry_model", "")]
    if smc_trades:
        print(f"\n[SMC STANDALONE BREAKDOWN]")
        # By Entry Model
        by_model: dict[str, list] = {}
        for t in smc_trades:
            m = t.entry_model or "unknown"
            by_model.setdefault(m, []).append(t)
        print(f"  By Entry Model:")
        for m, lst in sorted(by_model.items()):
            wins = sum(1 for x in lst if x.pnl_usdt > 0)
            wr = wins / len(lst) * 100
            avg_rr = 0.0
            if lst:
                wins_pct = [x.pnl_pct for x in lst if x.pnl_usdt > 0]
                losses_pct = [abs(x.pnl_pct) for x in lst if x.pnl_usdt <= 0]
                avg_win = sum(wins_pct) / len(wins_pct) if wins_pct else 0
                avg_loss = sum(losses_pct) / len(losses_pct) if losses_pct else 1
                avg_rr = avg_win / avg_loss if avg_loss > 0 else 0
            print(f"    {m:<16}: {len(lst):3d} trades | WR {wr:.0f}% | Avg RR {avg_rr:.2f}")
        # By Quality Grade
        by_quality: dict[str, list] = {}
        for t in smc_trades:
            q = getattr(t, "entry_model_quality", "") or "?"
            by_quality.setdefault(q, []).append(t)
        print(f"  By Quality Grade:")
        for q in ["A+", "A", "B", "C"]:
            if q in by_quality:
                lst = by_quality[q]
                wins = sum(1 for x in lst if x.pnl_usdt > 0)
                wr = wins / len(lst) * 100
                print(f"    {q:<4}: {len(lst):3d} trades | WR {wr:.0f}%")
        # By LTF Trigger
        by_trigger: dict[str, list] = {}
        for t in smc_trades:
            tr = getattr(t, "smc_ltf_trigger", "") or "?"
            by_trigger.setdefault(tr, []).append(t)
        print(f"  By LTF Trigger:")
        for tr, lst in sorted(by_trigger.items()):
            wins = sum(1 for x in lst if x.pnl_usdt > 0)
            wr = wins / len(lst) * 100
            print(f"    {tr:<12}: WR {wr:.0f}% ({len(lst)} trades)")

    # Trail stop effectiveness
    trail_trades = [t for t in trades if t.sl_trailing_state != "original"]
    if trail_trades:
        print(f"\n[TRAIL STOP]")
        print(f"  Activated          : {len(trail_trades)}/{len(trades)} ({len(trail_trades)/len(trades)*100:.1f}%)")
        trail_wins = [t for t in trail_trades if t.pnl_usdt > 0]
        print(f"  Win rate (trailed) : {len(trail_wins)/len(trail_trades)*100:.1f}%")

    # Top 5 best / worst trades
    sorted_trades = sorted(trades, key=lambda x: x.pnl_pct, reverse=True)
    print(f"\n[BEST 5 TRADES]")
    for t in sorted_trades[:5]:
        print(f"  {t.entry_time.strftime('%Y-%m-%d %H:%M')} {t.direction:<5} {t.symbol} "
              f"entry={t.entry_price:.4f} -> {t.outcome} {t.pnl_pct:+.2f}%")
    print(f"\n[WORST 5 TRADES]")
    for t in sorted_trades[-5:]:
        print(f"  {t.entry_time.strftime('%Y-%m-%d %H:%M')} {t.direction:<5} {t.symbol} "
              f"entry={t.entry_price:.4f} -> {t.outcome} {t.pnl_pct:+.2f}%")

    # Assessment
    print(f"\n{'='*65}")
    if result.win_rate >= 55 and result.profit_factor >= 1.5:
        verdict = "EDGE - co the trade"
    elif result.win_rate >= 50 and result.profit_factor >= 1.2:
        verdict = "MARGINAL - can toi uu them"
    else:
        verdict = "NO EDGE - khong nen trade"
    print(f"  VERDICT: {verdict}")
    print(f"  (Min target: win_rate>=55%, profit_factor>=1.5)")
    print(f"{'='*65}\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="Backtest engine cho multi_agent_cr scalping system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Backtest BTCUSDT scalp 6 tháng gần nhất
  python backtest.py --symbol BTCUSDT --style scalp --days 180

  # Backtest nhiều pairs
  python backtest.py --symbol BTCUSDT,ETHUSDT,SOLUSDT --style scalp --days 90

  # Walk-forward: 120 ngày train, 30 ngày test, lặp lại rolling
  python backtest.py --symbol BTCUSDT --style scalp --days 270 --walk-forward

  # Optimize parameters (chỉ nên dùng trên IS period)
  python backtest.py --symbol BTCUSDT --style scalp --days 180 --optimize

  # So sánh tắt từng filter để đo impact
  python backtest.py --symbol BTCUSDT --style scalp --days 180 --no-ema9
  python backtest.py --symbol BTCUSDT --style scalp --days 180 --no-confluence
  python backtest.py --symbol BTCUSDT --style scalp --days 180 --no-session
  python backtest.py --symbol BTCUSDT --style scalp --days 180 --no-chop
  python backtest.py --symbol BTCUSDT,ETHUSDT --style scalp --days 180 --no-correlation
        """
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Comma-separated symbols, e.g. BTCUSDT,ETHUSDT")
    parser.add_argument("--style", default="scalp", choices=["scalp", "swing"], help="Chỉ scalp được hỗ trợ đầy đủ")
    parser.add_argument("--mode", default="rule", choices=["rule", "smc", "combined"],
                        help="rule=rule-based (default) | smc=SMC standalone | combined=both")
    parser.add_argument("--from", dest="date_from", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="End date YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=180, help="Backtest last N days (override --from/--to)")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--wf-train", type=int, default=120, help="Walk-forward train days")
    parser.add_argument("--wf-test", type=int, default=30, help="Walk-forward test days")
    parser.add_argument("--optimize", action="store_true", help="Sweep params để find optimal")
    # Filter toggles
    parser.add_argument("--no-rule", action="store_true", help="Disable rule-based filter")
    parser.add_argument("--no-ema9", action="store_true", help="Disable EMA9 timing filter")
    parser.add_argument("--no-confluence", action="store_true", help="Disable confluence filter")
    parser.add_argument("--no-cvd", action="store_true", help="Disable CVD proxy filter")
    parser.add_argument("--no-vwap", action="store_true", help="Disable VWAP filter")
    parser.add_argument("--no-session", action="store_true", help="Disable session filter")
    parser.add_argument("--no-regime", action="store_true", help="Disable regime filter")
    parser.add_argument("--no-chop", action="store_true", help="Disable Chop Index filter")
    parser.add_argument("--no-smc", action="store_true", help="Disable SMC filter (opposing + confluence + OB override)")
    parser.add_argument("--no-correlation", action="store_true", help="Disable correlation filter (max 2 same dir)")
    parser.add_argument("--no-dynamic-confluence", action="store_true", help="Disable dynamic confluence (win rate < 45%% -> 4)")
    parser.add_argument("--no-sl-structure", action="store_true", help="Use ATR SL instead of swing structure")
    parser.add_argument("--no-trail", action="store_true", help="Disable trail stop")
    parser.add_argument("--no-momentum-gate", dest="no_momentum_gate", action="store_true",
                        help="Momentum thanh bonus (+15 net_score) thay vi hard gate")
    # Params
    parser.add_argument("--confluence", type=int, default=3, help="Confluence threshold (default 3)")
    parser.add_argument("--rr", type=float, default=2.0, help="Risk:Reward ratio")
    parser.add_argument("--scalp-rr", type=float, default=None, help="Override RR for scalp only (e.g. 1.3 để giảm TIME_EXIT)")
    parser.add_argument("--net-score", dest="net_score", type=int, default=0,
                        help="Net score threshold LONG (0=auto: scalp=20, swing=10)")
    parser.add_argument("--strategy", default="", choices=["", "v1", "v2", "loose"],
                        help="Preset: v1=default, v2=no-ema9+no-momentum-gate+net10+conf2, loose=minimal")
    parser.add_argument("--funnel", action="store_true",
                        help="In filter funnel: bao nhieu signal bi kill boi tung filter")
    parser.add_argument("--verbose", action="store_true")
    # Data cache
    parser.add_argument("--download-only", action="store_true", help="Chỉ download data và lưu cache, không chạy backtest")
    parser.add_argument("--use-cache", action="store_true", help="Load từ data/backtest_cache nếu có")
    # Rule cases (để hiểu rõ từng nhánh trong rule)
    parser.add_argument("--rule-case", default="full", choices=["full", "long_only", "short_only", "no_volume", "no_momentum"],
                       help="Rule case: full | long_only | short_only | no_volume | no_momentum")
    parser.add_argument("--rule-cases", action="store_true", help="Chạy tất cả rule cases và so sánh")

    args = parser.parse_args()

    # Date range
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.date_from:
        date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        date_from = now - timedelta(days=args.days)
    if args.date_to:
        date_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        date_to = now

    # Apply strategy presets
    if args.strategy == "v2":
        args.no_ema9 = True
        args.no_momentum_gate = True
        if args.net_score == 0:
            args.net_score = 10
        if args.confluence == 3:
            args.confluence = 2
    elif args.strategy == "loose":
        args.no_ema9 = True
        args.no_cvd = True
        args.no_momentum_gate = True
        if args.net_score == 0:
            args.net_score = 3
        if args.confluence == 3:
            args.confluence = 2 if args.style == "swing" else 1

    symbols = [s.strip().upper() for s in args.symbol.split(",")]

    use_smc_standalone = args.mode in ("smc", "combined")
    config = BacktestConfig(
        symbols=symbols,
        style=args.style,
        date_from=date_from,
        date_to=date_to,
        use_smc_standalone=use_smc_standalone,
        use_rule_filter=not args.no_rule if args.mode != "smc" else False,
        use_ema9_filter=not args.no_ema9,
        use_confluence_filter=not args.no_confluence,
        use_cvd_proxy=not args.no_cvd,
        use_vwap_filter=not args.no_vwap,
        use_session_filter=not args.no_session,
        use_regime_filter=not args.no_regime,
        use_chop_filter=not args.no_chop,
        use_smc_filter=not args.no_smc,
        use_correlation_filter=not args.no_correlation,
        use_dynamic_confluence=not args.no_dynamic_confluence,
        use_sl_structure=not args.no_sl_structure,
        use_trail_stop=not args.no_trail,
        use_momentum_gate=not args.no_momentum_gate,
        net_score_min=args.net_score,
        rule_case=args.rule_case,
        confluence_threshold=args.confluence,
        scalp_rr=args.scalp_rr if args.scalp_rr is not None else args.rr,
        swing_rr=args.rr if args.style == "swing" else SWING_RR,
        walk_forward=args.walk_forward,
        wf_train_days=args.wf_train,
        wf_test_days=args.wf_test,
    )

    print(f"\n[Backtest Engine] {args.style.upper()} [mode={args.mode}]")
    print(f"   Symbols : {', '.join(symbols)}")
    print(f"   Period  : {date_from.strftime('%Y-%m-%d')} -> {date_to.strftime('%Y-%m-%d')} ({(date_to-date_from).days} days)")
    print(f"   Filters : rule={config.use_rule_filter} ema9={config.use_ema9_filter} "
          f"conf={config.use_confluence_filter}(>={config.confluence_threshold}) "
          f"cvd={config.use_cvd_proxy} vwap={config.use_vwap_filter} "
          f"session={config.use_session_filter} chop={config.use_chop_filter} smc={config.use_smc_filter} "
          f"corr={config.use_correlation_filter} dyn_conf={config.use_dynamic_confluence}")
    print(f"   Params  : RR={config.scalp_rr} trail={config.use_trail_stop} sl_structure={config.use_sl_structure}")
    print(f"   Rule    : {config.rule_case}")
    print()

    # Download data
    print("[Data...]")
    all_data = await download_all_data(config, use_cache=args.use_cache, download_only=args.download_only)

    if args.download_only:
        print("\n  Done. Chạy backtest với --use-cache để dùng data đã lưu.")
        return

    if args.rule_cases:
        print("\n[Rule cases comparison (scalp)...]")
        for rule_case in ["full", "long_only", "short_only", "no_volume", "no_momentum"]:
            cfg = replace(config, rule_case=rule_case)
            trades = run_backtest_for_symbol(symbols[0], all_data[symbols[0]], cfg, date_from, date_to)
            stats = calc_stats(trades, cfg, date_from, date_to)
            print(f"  {rule_case:<12}: {len(trades):4d} trades | win={stats.win_rate:5.1f}% | PF={stats.profit_factor:.2f} | PnL={stats.total_pnl_pct:+.2f}%")
        return

    if args.walk_forward:
        print(f"\n[Walk-forward analysis] (train={args.wf_train}d, test={args.wf_test}d)")
        for symbol in symbols:
            windows = run_walk_forward(symbol, all_data[symbol], config)
            print(f"\n  {symbol} Walk-forward results:")
            print(f"  {'Window':<30} {'IS Win%':>8} {'IS #':>6} {'OOS Win%':>9} {'OOS #':>6} {'OOS PnL':>8} {'OOS DD':>7}")
            print(f"  {'-'*80}")
            for w in windows:
                print(f"  {w['window']:<30} "
                      f"{w['is_win_rate']:>7.1f}% "
                      f"{w['is_trades']:>6} "
                      f"{w['oos_win_rate']:>8.1f}% "
                      f"{w['oos_trades']:>6} "
                      f"{w['oos_pnl_pct']:>+7.1f}% "
                      f"{w['oos_max_dd']:>6.1f}%")

            # Summary: consistency của OOS
            oos_profitable = [w for w in windows if w["oos_pnl_pct"] > 0]
            if windows:
                consistency = len(oos_profitable) / len(windows) * 100
                avg_oos_wr = sum(w["oos_win_rate"] for w in windows) / len(windows)
                print(f"\n  OOS consistency: {consistency:.0f}% profitable windows")
                print(f"  Avg OOS win rate: {avg_oos_wr:.1f}%")

    elif args.optimize:
        print(f"\n[Parameter optimization] (warning: in-sample only)")
        for symbol in symbols:
            print(f"\n  {symbol} optimization:")
            results = run_optimization(symbol, all_data[symbol], config)
            print(f"\n  Top 5 by profit factor:")
            print(f"  {'Confluence':>10} {'RR':>5} {'Trades':>7} {'Win%':>6} {'PF':>6} {'MaxDD':>7} {'PnL':>8}")
            for r in results[:5]:
                print(f"  {r['confluence']:>10} {r['rr']:>5.1f} {r['trades']:>7} "
                      f"{r['win_rate']:>5.1f}% {r['profit_factor']:>6.2f} "
                      f"{r['max_dd']:>6.1f}% {r['total_pnl_pct']:>+7.1f}%")

    else:
        # Standard backtest
        if args.mode == "smc":
            # SMC standalone only
            all_trades = []
            for symbol in symbols:
                print(f"[Running SMC backtest for {symbol}...]")
                trades = run_smc_backtest_for_symbol(
                    symbol, all_data[symbol], config,
                    date_from, date_to,
                    verbose=args.verbose or args.funnel,
                )
                all_trades.extend(trades)
                print(f"   -> {len(trades)} trades generated")
        elif len(symbols) > 1:
            # Combined mode khi multi-symbol
            print(f"[Running combined backtest for {', '.join(symbols)}...]")
            all_trades = run_backtest_combined(
                symbols, all_data, config,
                date_from, date_to,
                verbose=args.verbose or args.funnel,
            )
            print(f"   -> {len(all_trades)} trades generated")
        else:
            all_trades = []
            for symbol in symbols:
                print(f"[Running backtest for {symbol}...]")
                trades = run_backtest_for_symbol(
                    symbol, all_data[symbol], config,
                    date_from, date_to,
                    verbose=args.verbose or args.funnel,
                )
                all_trades.extend(trades)
                print(f"   -> {len(trades)} trades generated")

        if len(symbols) > 1:
            print(f"\n[COMBINED RESULTS] ({len(symbols)} symbols, {len(all_trades)} total trades)")

        result = calc_stats(all_trades, config, date_from, date_to)
        symbol_label = "+".join(symbols) if len(symbols) > 1 else symbols[0]
        print_report(result, symbol_label, date_from, date_to)


if __name__ == "__main__":
    asyncio.run(main())
